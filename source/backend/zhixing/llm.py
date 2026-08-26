"""模型调用层(碰) —— 把请求体发出去,把原始回复拿回来。

## 这一层只做三件事

发送、重试、超时。**它不构造请求体,也不解析回复**——那两样在 ``model.py``,
是纯函数,自检里不用网络就能跑。本模块能被测的只有"什么时候该重试",
所以那个判断被单独摘成了纯函数 ``retry_delay()``。

流式也照这个分工:本模块只管"从 socket 上一行一行拿下来、认出 ``data:``
和 ``[DONE]``",拼接和取用量在 ``model.merge_stream``。所以自检验拼接
不需要起服务。

## 为什么这里可以重试

契约里的规矩是 **写操作绝不自动重试**。模型调用是读:同一份上下文问两次,
第二次不会多下一张单。真正不能重试的是 ``broker`` 那一层——一笔委托发出去
可能已经成交却没拿回回执,重试就是重复下单。两者放在不同模块、不同规则,
就是为了不让"反正都重试"这种习惯从这边渗到那边。

**但 4xx 不重试。** 密钥错了、模型名不存在、请求体不合法——重试十次还是错,
只是把错误发生的时间往后推,顺带把配额烧掉。只有超时、连接失败、429、5xx
才重试,因为只有它们是"再来一次可能就好了"。

## 密钥

密钥在 ``Credential`` 里,**它的 ``repr`` 是打码的**。这不是客气,是因为
``logging`` 在记录异常时会把局部变量的 repr 打出来,而这个进程的日志将来
是要贴给人看的。同样的理由,请求体一个字节都不进日志——里面有全部上下文。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from .model import ModelError, ModelTarget, Reply, merge_stream, parse_reply

logger = logging.getLogger("zhixing.llm")

#: 单次请求超时(秒)。一轮七个标的,推理模型单个能跑一两分钟。
DEFAULT_TIMEOUT = 180.0

#: 最多重试几次(不含首次)。三次之后还不行,这一轮就该带着原因失败,
#: 而不是一直杵在那里——错过的时点永不补跑,拖着不等于救回来。
DEFAULT_RETRIES = 2

#: 重试基准间隔(秒)。
DEFAULT_BACKOFF = 2.0

#: 会重试的 HTTP 状态。429 是限流,5xx 是对面的问题。
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class LlmError(RuntimeError):
    """调用失败。``retryable`` 说明这次失败重试有没有意义。"""

    def __init__(self, message: str, *, retryable: bool, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


# ---------------------------------------------------------------------------
#  密钥
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credential:
    """一把 API Key。**打印出来是打码的。**

    和 ``state.mask_secret`` 一个规矩:留后四位,够核对"是不是换了一把",
    不够拿去用。
    """

    secret: str = field(repr=False, default="")

    def __repr__(self) -> str:      # noqa: D105 - 见类文档
        if not self.secret:
            return "Credential(未配置)"
        return f"Credential(****{self.secret[-4:]})"

    __str__ = __repr__

    def require(self) -> str:
        if not self.secret.strip():
            raise LlmError("没有配置模型 API 密钥", retryable=False)
        return self.secret


# ---------------------------------------------------------------------------
#  重试判断(算,单独摘出来是为了能测)
# ---------------------------------------------------------------------------


def retry_delay(
    attempt: int, *, backoff: float = DEFAULT_BACKOFF, seed: str = ""
) -> float:
    """第 ``attempt`` 次重试之前等多久。**纯函数。**

    指数退避 + **确定性抖动**:抖动来自 ``seed`` 的哈希,不来自随机数。
    七个标的同时撞上 429 时需要错开,但同一次失败重跑两遍应该等一样久——
    调试的时候"这次跑得不一样"是最浪费时间的一类现象。
    """
    if attempt < 1:
        return 0.0
    base = backoff * (2 ** (attempt - 1))
    # 0 到 0.5 个 backoff 之间的固定偏移
    jitter = (sum(seed.encode("utf-8")) % 500) / 1000.0 * backoff
    return base + jitter


def _is_retryable(status: int | None) -> bool:
    return status is None or status in RETRYABLE_STATUS


# ---------------------------------------------------------------------------
#  可替换的调用口
# ---------------------------------------------------------------------------


class ModelCaller(Protocol):
    """打给模型的那一下。**三个 Protocol 边界之一。**

    (架构里管它叫 ``ModelTarget``;那个名字给了描述"打给谁"的数据类,
    更贴切,所以这个接口改叫 ``ModelCaller``。)

    存在的理由是二代的缺陷 2:模型配置写成了全局常量,一个进程只能用一个
    模型,**多模型对比根本做不了**。这里做成接口之后:

    * 自检塞一个假的,不联网就能跑完整条链路;
    * 第 9 步换中转、换模型,只是换一个实现;
    * 想同时问两个模型再比,就是两个实例。
    """

    def call(
        self, target: ModelTarget, body: Mapping[str, Any], *, object_id: str
    ) -> Reply:
        ...


@dataclass
class HttpCaller:
    """真正发 HTTPS 的实现。标准库 ``urllib``,不引第三方。

    并发是 1(一天六轮、一轮七个标的、串行),连接池省不到什么,
    而少一个依赖是实打实的。
    """

    credential: Credential
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    backoff: float = DEFAULT_BACKOFF
    #: 注进来是为了自检不用真睡。
    sleep: Callable[[float], None] = time.sleep

    # -- 组装 -------------------------------------------------------------

    def _url(self, target: ModelTarget) -> str:
        base = target.base_url.rstrip("/")
        if target.protocol == "openai_chat":
            # 中转常常把 /v1 也写进 base_url,两种都收
            return base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        return base + ("/messages" if base.endswith("/v1") else "/v1/messages")

    def _headers(self, target: ModelTarget) -> dict[str, str]:
        key = self.credential.require()
        headers = {"Content-Type": "application/json"}
        headers["Accept"] = "text/event-stream" if target.stream else "application/json"
        if target.protocol == "openai_chat":
            headers["Authorization"] = f"Bearer {key}"
        else:
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        return headers

    # -- 发送 -------------------------------------------------------------

    def _open(self, target: ModelTarget, body: Mapping[str, Any]):
        """发出去,把连接交回调用方。**HTTP 层面的错在这里就翻译成 LlmError。**

        流式和非流式只有"怎么读"不同,"怎么发、怎么翻译错误"完全一样,
        所以那部分只写一遍。
        """
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url(target), data=payload, headers=self._headers(target), method="POST"
        )
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            # 对面的错误正文可能带诊断信息,但也可能把请求原样回显——
            # 那里面有全部上下文,所以只留状态码和前 200 字。
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:       # pragma: no cover - 读不出就算了
                pass
            raise LlmError(
                f"模型接口返回 {exc.code}:{detail}",
                retryable=_is_retryable(exc.code),
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise LlmError(f"连不上模型接口:{exc.reason}", retryable=True) from exc
        except TimeoutError as exc:
            raise LlmError(f"模型接口超时({self.timeout:g} 秒)", retryable=True) from exc

    def _once_stream(
        self, target: ModelTarget, body: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """按 SSE 读完,折回成非流式的形状交给 ``parse_reply``。

        知行 不逐字显示,开流式只是因为**这台中转的 OpenAI 路由不接受非流式**
        (``stream:false`` 直接 400)。所以这里读完就拼,不做任何增量输出。

        解析部分全在 ``model.merge_stream``(纯函数),本方法只负责"从 socket
        上一行一行拿下来、认出 ``data:`` 前缀、认出 ``[DONE]``"——
        也就是只做碰的那部分。
        """
        events: list[Mapping[str, Any]] = []
        done = False
        try:
            with self._open(target, body) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        # 空行是 SSE 的分片边界;event:/id:/: 心跳一律跳过
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        done = True
                        break
                    try:
                        parsed = json.loads(chunk)
                    except json.JSONDecodeError:
                        # 单个分片坏掉不该让整次调用作废——**但要记下来**,
                        # 静默跳过会变成"回答莫名其妙少了一段"。
                        logger.warning("流式分片不是 JSON,已跳过(%d 字节)", len(chunk))
                        continue
                    if isinstance(parsed, Mapping):
                        events.append(parsed)
        except OSError as exc:
            # 读到一半断线。前面拿到的半份正文**一律不要**:半份 JSON 比
            # 没有更糟,它看起来像数据。
            raise LlmError(f"流式读取中断:{exc}", retryable=True) from exc

        if not done:
            # 没见到 [DONE] 说明流没走完。正文可能被截在半路,
            # 而截断的 JSON 解析出来可能"恰好"合法。宁可重试。
            raise LlmError("流式回复没有正常结束(没收到 [DONE])", retryable=True)

        return merge_stream(target, events, object_id="")

    def _once(self, target: ModelTarget, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if target.stream:
            return self._once_stream(target, body)

        with self._open(target, body) as response:
            raw = response.read().decode("utf-8")

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            # 网关的 HTML 错误页会走到这里。当作可重试:多半是临时的。
            raise LlmError(f"模型接口回的不是 JSON:{exc}", retryable=True) from exc
        if not isinstance(parsed, Mapping):
            raise LlmError("模型接口回的不是 JSON 对象", retryable=False)
        return parsed

    def call(
        self, target: ModelTarget, body: Mapping[str, Any], *, object_id: str
    ) -> Reply:
        """发送并解析。失败时抛 ``LlmError``,**已经重试过了**。"""
        last: LlmError | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                delay = retry_delay(attempt, backoff=self.backoff, seed=object_id)
                logger.info(
                    "%s 第 %d 次重试,等 %.1f 秒(上次:%s)",
                    object_id, attempt, delay, last,
                )
                self.sleep(delay)
            try:
                raw = self._once(target, body)
            except LlmError as exc:
                last = exc
                if not exc.retryable:
                    raise
                continue

            try:
                return parse_reply(target, raw, object_id=object_id)
            except ModelError as exc:
                # 回复拿到了但形状不对。重试一次有可能碰上正常的一份,
                # 但更可能是协议配错了——所以只当作可重试,不特殊照顾。
                last = LlmError(f"回复无法解析:{exc}", retryable=True)
                continue

        assert last is not None      # 循环至少跑一次,失败才会走到这里
        raise last


__all__ = [
    "DEFAULT_TIMEOUT", "DEFAULT_RETRIES", "DEFAULT_BACKOFF", "RETRYABLE_STATUS",
    "LlmError", "Credential", "retry_delay", "ModelCaller", "HttpCaller",
]
