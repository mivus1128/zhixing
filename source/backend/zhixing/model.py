"""模型层(算) —— 构造请求体、解析回复、把模型的话变成可校验的数据。

## 这一层不发请求

发请求在 ``llm.py``。分开的理由和 ``api`` / ``serve`` 一样:请求体长什么样、
回复怎么解析、模型胡说八道怎么拦——这些是这个系统最该被反复测的逻辑,
而它们**不需要网络就能测**。自检里喂一份存下来的原始回复即可。

## 模型是不可信输入

它会漏字段、会多加解释、会把 JSON 包在 ```json 里、会把 `置信度` 写成
"0.62"、会编一个清单里根本没有的代码。二代在这里几乎不设防(缺陷 4:
``qty``/``limit_price`` 解析失败也不拒绝),所以本模块的立场是:

**模型说的每一样东西都要过一遍,而且一次报全部问题,不是遇错即停。**

和 ``guards`` / ``catalog.validate_draft`` / ``scheduler.parse_times`` 同一个
取向:改一次就该看到全部毛病,不是改一条冒一条。

## 密钥不在 ``ModelTarget`` 里

``ModelTarget`` 只有身份和参数,**没有 API Key**。所以它可以整个写进归档、
打进日志、在接口里下发。密钥由 ``llm.py`` 在发送时单独带上,和
``state.CaptchaSettings.secret`` 同一个套路:需要保护的东西只待在一个地方。

## 为什么有 ``protocol`` 这个字段

二代把模型配置写成全局常量(缺陷 2),一个进程只能用一个模型,
**多模型对比根本做不了**。三代把它做成参数,顺带把线上格式也参数化——
基线验证要用二代同款(OpenAI 兼容的 chat completions),第 9 步换模型时
可能换成 Anthropic 的 messages 格式。两种都是纯函数,加第三种只是再写一对。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

logger = logging.getLogger("zhixing.model")


#: 允许的操作。契约 1.1。
ACTIONS = ("buy", "sell", "hold", "cancel")

#: 会带出指令的操作。``hold`` 不带。
ACTING = ("buy", "sell", "cancel")

#: 支持的线上格式。
PROTOCOLS = ("openai_chat", "anthropic_messages")


class ModelError(RuntimeError):
    """模型层出错。**只用于结构性错误**(协议不支持、回复不是 JSON 等),
    模型内容上的毛病走 ``Problem``,不抛异常——一条判断坏掉不该让整轮炸。"""


# ---------------------------------------------------------------------------
#  目标模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelTarget:
    """一次调用要打给谁,用什么参数。

    **这里没有 API Key。** 见模块开头。

    ``name`` / ``provider`` 直接进归档的 ``model`` / ``llm_provider`` 字段
    (契约 1.1),所以它们必须是能给人看的字符串,不是内部代号。
    """

    name: str
    provider: str
    base_url: str
    protocol: str = "openai_chat"
    temperature: float | None = None
    max_tokens: int | None = None
    #: 要不要求模型只输出 JSON。二代用的是 OpenAI 的 response_format。
    #:
    #: ⚠️ **有的中转收下这个字段但不执行**——实测当前这台会照样回大白话,
    #: 既不报错也不生效。所以它只是"能开就开"的锦上添花,**真正保证只输出
    #: JSON 的是提示词加 ``strip_fence`` 加 ``parse_judgment``**。
    #: 别把它当成一道防线,那正是二代缺陷 5(一个从不触发的检查)。
    force_json: bool = True
    #: 流式收。**默认开**,理由是不对称的:流式所有端点都支持,非流式有的
    #: 端点直接 400(当前这台中转的 OpenAI 路由就是,``stream:false`` 报
    #: ``Stream must be set to true``)。
    #:
    #: 知行 不需要逐字显示,开流式纯粹是为了能连上;收完照样一次性交给
    #: ``parse_judgment``,见 ``merge_stream``。
    stream: bool = True

    def __post_init__(self) -> None:
        if self.protocol not in PROTOCOLS:
            raise ModelError(
                f"不认识的线上格式 {self.protocol!r},支持:{'、'.join(PROTOCOLS)}"
            )
        if not self.name.strip() or not self.provider.strip():
            raise ModelError("ModelTarget 的 name / provider 不能为空,它们要进归档")
        if self.stream and self.protocol == "anthropic_messages":
            # 在配置的时候就炸,不留到跑一半再炸。
            #
            # Anthropic 的流式事件格式(message_start / content_block_delta /
            # message_delta)没在这儿实现:手上这把中转 key 没有 Claude 权限,
            # 写了也验不了。**没验过的代码不该装成能用的。**
            raise ModelError(
                "anthropic_messages 的流式还没实现(手上没有能验证它的通道),"
                "这个协议请用 stream=False"
            )

    def as_public(self) -> dict[str, Any]:
        """可以随便打印、随便入档的视图。本来就没有机密,这里只是把它写明白。"""
        return {
            "model": self.name,
            "llm_provider": self.provider,
            "base_url": self.base_url,
            "protocol": self.protocol,
        }


# ---------------------------------------------------------------------------
#  用量
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelUsage:
    """一次调用的 token 用量。对应契约 1.1 ``model_usage`` 的一条。

    四个数都可能是 0——**0 和"没这项"在这里是同一件事**,不区分。
    契约把它定义成整数,前端要对 7 个标的求和,给 null 只会让求和处处判空。
    """

    object_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0

    def as_entry(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
        }


# ---------------------------------------------------------------------------
#  判断
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    """模型输出里的一处毛病。``code`` 供程序判断,``message`` 供人阅读。

    形状和 ``guards.GuardFailure`` 一致,是刻意的:两处最后都会以
    ``{code, message}`` 的样子出现在界面上(契约 2.1 的 ``error.问题[]``)。
    """

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class RawInstruction:
    """模型给出的一条指令,**未经任何校验**。

    ``qty`` / ``limit_price`` 保持原始类型,和 ``guards.ProposedOrder`` 一样:
    "1,000" 这种脏值要留到校验层去报错,在这里悄悄转换等于把问题藏起来。

    没有 ``instruction_code`` / ``market`` / ``symbol`` / ``name``——那四样
    由后端从 ``object_id`` 和标的清单算出来,不让模型写(契约 1.1:
    模型捏造代码是必须拦下的事故)。
    """

    action: str
    qty: object = None
    limit_price: object = None
    wtbh: str | None = None
    reason: str = ""
    risk_note: str = ""


@dataclass(frozen=True)
class Judgment:
    """一个标的的一条判断,已经过结构检查,但**尚未完成指令规范化**。

    本地下单风控已经拆除;``guards`` 只把数量与限价转换为执行层类型。
    本模块保证字段齐、类型对、取值在允许集合里、自相一致。
    """

    object_id: str
    操作: str
    理由: tuple[str, ...]
    风险: tuple[str, ...]
    置信度: float
    改判条件: str
    指令: RawInstruction | None = None

    def as_entry(self, *, 名称: str) -> dict[str, Any]:
        """转成契约 1.1 ``交易对象判断`` 的一条。

        ``名称`` 由调用方从标的清单取,**不来自模型**——契约 1.1 写明了
        名字由后端 join,模型复述会让"代码与名字对不上时谁错了"无法判断。
        """
        return {
            "object_id": self.object_id,
            "名称": 名称,
            "操作": self.操作,
            "理由": list(self.理由),
            "风险": list(self.风险),
            "置信度": self.置信度,
            "改判条件": self.改判条件,
        }


# ---------------------------------------------------------------------------
#  构造请求体
# ---------------------------------------------------------------------------


def build_request(
    target: ModelTarget, *, system_prompt: str, user_text: str
) -> dict[str, Any]:
    """拼请求体。**纯函数,不发送。**

    两种协议的差别只在外壳:系统提示词放哪儿、参数怎么叫。正文一个字不变——
    这很重要,基线验证要求两代吃同一份输入,换协议不能顺手改内容。
    """
    if target.protocol == "openai_chat":
        body: dict[str, Any] = {
            "model": target.name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
        }
        if target.force_json:
            body["response_format"] = {"type": "json_object"}
        if target.temperature is not None:
            body["temperature"] = target.temperature
        if target.max_tokens is not None:
            body["max_tokens"] = target.max_tokens
        if target.stream:
            body["stream"] = True
        return body

    # anthropic_messages:系统提示词是顶层字段,不混在 messages 里
    body = {
        "model": target.name,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_text}],
        # max_tokens 在这个协议里是必填,没给就用一个够用的默认值
        "max_tokens": target.max_tokens if target.max_tokens is not None else 8192,
    }
    if target.temperature is not None:
        body["temperature"] = target.temperature
    return body


# ---------------------------------------------------------------------------
#  解析回复
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    """一次调用的回复:模型说的话 + 这次花了多少。"""

    text: str
    usage: ModelUsage
    #: 服务端回显的模型名。和 ``ModelTarget.name`` 不一致时值得记一笔——
    #: 中转把请求转给了别的模型是会发生的事,而且不报错。
    model_echo: str = ""
    finish_reason: str = ""


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def merge_stream(
    target: ModelTarget, events: Iterable[Mapping[str, Any]], *, object_id: str
) -> dict[str, Any]:
    """把一串流式分片折回成一份"跟非流式一模一样"的回复。**纯函数。**

    ## 为什么折回去,而不是另写一个解析器

    正文怎么取、用量怎么算、模型名对不对——这些判断只该有**一份**。
    再写一个流式版本,就有了两处会分叉的代码,而它们的差异只会在
    "某天线上换成流式之后用量统计突然全是 0"这种时候才被发现。

    所以这里只负责"把碎片拼回整块",拼完仍旧交给 ``parse_reply``。

    ## ⚠️ ``reasoning_content`` 必须丢掉

    实测这台中转会在**同一个 ``delta`` 里**混着发 ``content`` 和
    ``reasoning_content``。推理过程不是答案:把它拼进正文,``parse_judgment``
    看到的就是"一段思考 + 一份 JSON",整段解析失败——而且失败得很像
    "模型不听话",查起来会往错的方向走很远。

    ``events`` 里不含 ``[DONE]`` 那一行,终止判断在 ``llm.py``——
    那是传输层的事。
    """
    if target.protocol != "openai_chat":
        raise ModelError(
            f"{target.protocol} 的流式还没实现,这个协议请用 stream=False"
        )

    pieces: list[str] = []
    finish = ""
    usage: Mapping[str, Any] | None = None
    echo = ""
    saw_any = False

    for event in events:
        if not isinstance(event, Mapping):
            continue
        saw_any = True
        if isinstance(event.get("model"), str) and event["model"]:
            echo = event["model"]
        # 用量只在最后一片上,但不假定它一定在最后一片——取到就留,
        # 后来的覆盖先前的。
        if isinstance(event.get("usage"), Mapping):
            usage = event["usage"]

        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        first = choices[0]
        if not isinstance(first, Mapping):
            continue
        if isinstance(first.get("finish_reason"), str) and first["finish_reason"]:
            finish = first["finish_reason"]

        delta = first.get("delta")
        if not isinstance(delta, Mapping):
            continue
        # **只取 content**。reasoning_content 等一切别的键一律不要。
        chunk = delta.get("content")
        if isinstance(chunk, str):
            pieces.append(chunk)

    if not saw_any:
        raise ModelError("流式回复里一个分片都没有")

    return {
        "model": echo or target.name,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "".join(pieces)},
            "finish_reason": finish,
        }],
        "usage": dict(usage) if usage else {},
    }


def parse_reply(
    target: ModelTarget, raw: Mapping[str, Any], *, object_id: str
) -> Reply:
    """从原始回复里取出正文和用量。**纯函数。**

    取不到正文就抛 ``ModelError``——那是协议层面的错(回复根本不是这个形状),
    和"模型说的内容有毛病"不是一回事,后者走 ``parse_judgment`` 的 ``Problem``。
    """
    if target.protocol == "openai_chat":
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelError("回复里没有 choices,无法取出正文")
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
        text = message.get("content") if isinstance(message, Mapping) else None
        finish = choices[0].get("finish_reason") if isinstance(choices[0], Mapping) else ""

        usage_raw = raw.get("usage") or {}
        prompt_details = usage_raw.get("prompt_tokens_details") or {}
        completion_details = usage_raw.get("completion_tokens_details") or {}
        usage = ModelUsage(
            object_id=object_id,
            input_tokens=_int(usage_raw.get("prompt_tokens")),
            output_tokens=_int(usage_raw.get("completion_tokens")),
            reasoning_tokens=_int(completion_details.get("reasoning_tokens")),
            cached_tokens=_int(prompt_details.get("cached_tokens")),
        )
    else:
        blocks = raw.get("content")
        if not isinstance(blocks, list):
            raise ModelError("回复里没有 content 数组,无法取出正文")
        text = "".join(
            b.get("text", "")
            for b in blocks
            if isinstance(b, Mapping) and b.get("type") == "text"
        )
        finish = raw.get("stop_reason") or ""

        usage_raw = raw.get("usage") or {}
        usage = ModelUsage(
            object_id=object_id,
            input_tokens=_int(usage_raw.get("input_tokens")),
            output_tokens=_int(usage_raw.get("output_tokens")),
            # 这个协议不单列推理 token,留 0 而不是拿输出冒充
            reasoning_tokens=0,
            cached_tokens=_int(usage_raw.get("cache_read_input_tokens")),
        )

    if not isinstance(text, str) or not text.strip():
        raise ModelError("模型回复的正文为空")

    echo = raw.get("model")
    if isinstance(echo, str) and echo and echo != target.name:
        # 不报错:中转换模型是它的自由,但这件事必须留下痕迹,
        # 否则"我以为在测 A,其实一直在测 B"查不出来。
        logger.warning("请求的是 %s,服务端回显 %s", target.name, echo)

    return Reply(
        text=text,
        usage=usage,
        model_echo=echo if isinstance(echo, str) else "",
        finish_reason=finish if isinstance(finish, str) else "",
    )


# ---------------------------------------------------------------------------
#  把话变成数据
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def strip_fence(text: str) -> str:
    """去掉 ```json 包裹。

    提示词里写了"不要代码块标记",但模型照写不误是常态。为这个丢掉一整轮
    判断不值得——**能无歧义地修好的就修**,修不了的才报错。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE.sub("", stripped).strip()
    return stripped


def _confidence(value: object, out: list[Problem]) -> float:
    """置信度必须是 0–1 的数。字符串 "0.62" 也收——模型经常这么写,
    而这个转换是无歧义的。转不了或超范围才报错。"""
    if isinstance(value, bool) or value is None:
        out.append(Problem("BAD_CONFIDENCE", f"置信度必须是 0 到 1 的小数,收到 {value!r}"))
        return 0.0
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        out.append(Problem("BAD_CONFIDENCE", f"置信度不是数字,收到 {value!r}"))
        return 0.0
    if not 0.0 <= number <= 1.0:
        out.append(Problem("BAD_CONFIDENCE", f"置信度必须在 0 到 1 之间,收到 {number}"))
        return 0.0
    return number


def _string_list(value: object, field_name: str, out: list[Problem]) -> tuple[str, ...]:
    """``理由`` / ``风险`` 必须是数组。

    契约 1.1 写明「是**数组**,不是字符串」。模型给一整段字符串时**不拆**——
    按标点拆会拆出一堆半句话,看起来像做对了。报错让它重来更诚实。
    """
    if not isinstance(value, list):
        out.append(Problem(f"BAD_{field_name}", f"`{field_name}` 必须是数组,收到 {type(value).__name__}"))
        return ()
    items = tuple(str(v).strip() for v in value if str(v).strip())
    if not items:
        out.append(Problem(f"EMPTY_{field_name}", f"`{field_name}` 是空的"))
    return items


def _instruction(value: object, 操作: str, out: list[Problem]) -> RawInstruction | None:
    """指令与操作必须自洽。

    ``hold`` 带指令、``buy`` 不带指令,都是模型自相矛盾——**这种时候不猜**。
    猜错的方向是会下单的那个方向。
    """
    has = isinstance(value, Mapping)

    if 操作 == "hold":
        if has:
            out.append(Problem("HOLD_WITH_INSTRUCTION", "操作是 hold,却给了指令"))
        return None

    if not has:
        out.append(Problem("MISSING_INSTRUCTION", f"操作是 {操作},必须给出指令"))
        return None

    data: Mapping[str, Any] = value  # type: ignore[assignment]
    action = str(data.get("action", "")).strip().lower()
    if action != 操作:
        out.append(
            Problem("ACTION_MISMATCH", f"指令的 action 是 {action!r},与操作 {操作!r} 不一致")
        )

    return RawInstruction(
        action=action,
        qty=data.get("qty"),
        limit_price=data.get("limit_price"),
        wtbh=data.get("wtbh") if isinstance(data.get("wtbh"), str) else None,
        reason=str(data.get("理由", "")).strip(),
        risk_note=str(data.get("风险提示", "")).strip(),
    )


def parse_judgment(
    text: str, *, expect_object_id: str
) -> tuple[Judgment | None, tuple[Problem, ...]]:
    """把模型说的话变成一条判断。**一次报全部问题。**

    :param expect_object_id: 这次问的是哪个标的。模型抄回来的必须与它一致——
        对不上意味着模型编了一个代码,或者把上一次的答案抄了回来。
        契约 1.1 把这一条定为校验点:二代拦不住。

    返回 ``(判断, 问题)``。有任何问题时判断为 ``None``——**不返回一条
    带毛病的判断**。半对的判断比没有更糟,它会被下游当成好的用。
    """
    problems: list[Problem] = []

    try:
        data = json.loads(strip_fence(text))
    except json.JSONDecodeError as exc:
        return None, (Problem("NOT_JSON", f"模型输出不是合法 JSON:{exc}"),)

    if not isinstance(data, Mapping):
        return None, (Problem("NOT_OBJECT", f"模型输出是 {type(data).__name__},不是 JSON 对象"),)

    got_id = str(data.get("object_id", "")).strip()
    if got_id != expect_object_id:
        problems.append(
            Problem(
                "OBJECT_ID_MISMATCH",
                f"问的是 {expect_object_id},模型答的是 {got_id!r}",
            )
        )

    操作 = str(data.get("操作", "")).strip().lower()
    if 操作 not in ACTIONS:
        problems.append(Problem("BAD_ACTION", f"操作只能是 {'/'.join(ACTIONS)},收到 {操作!r}"))

    理由 = _string_list(data.get("理由"), "理由", problems)
    风险 = _string_list(data.get("风险"), "风险", problems)
    置信度 = _confidence(data.get("置信度"), problems)

    改判条件 = str(data.get("改判条件", "")).strip()
    if not 改判条件:
        # 说不出改判条件的判断没法被检验,而检验是认识闭环的全部原材料。
        problems.append(Problem("MISSING_TRIGGER", "`改判条件` 是空的,四个操作都必须填"))

    指令 = _instruction(data.get("指令"), 操作, problems) if 操作 in ACTIONS else None

    if problems:
        logger.warning(
            "%s 的判断被拒,共 %d 条:%s",
            expect_object_id, len(problems), ";".join(str(p) for p in problems),
        )
        return None, tuple(problems)

    return (
        Judgment(
            object_id=expect_object_id,
            操作=操作,
            理由=理由,
            风险=风险,
            置信度=置信度,
            改判条件=改判条件,
            指令=指令,
        ),
        (),
    )


__all__ = [
    "ACTIONS", "ACTING", "PROTOCOLS", "ModelError",
    "ModelTarget", "ModelUsage", "Problem",
    "RawInstruction", "Judgment", "Reply",
    "build_request", "parse_reply", "merge_stream", "parse_judgment", "strip_fence",
]
