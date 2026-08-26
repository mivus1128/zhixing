"""验证码识别 —— 把一张图换成几个字符。

## 三个 Protocol 边界之三

``BrokerAdapter``(下单)、``ModelCaller``(问模型)、``CaptchaSolver``(认图)。
三个都是"要碰外部世界"的地方,三个都做成接口,理由一样:**碰外部世界的那
一下必须能在自检里换掉**,否则整条链路只能靠联网才能测,那就等于测不动。

## 和 llm.py 的关系:借零件,不借结构

密钥容器(``Credential``)、退避算法(``retry_delay``)、错误分类
(``LlmError.retryable``)直接从 ``llm`` 借。这些是**同一条规矩**,
抄一遍就会有两份、迟早分叉。

但**不复用 ``HttpCaller``**。验证码走的是视觉模型,请求体形状、超时预算、
重试语义都不一样:一轮判断可以等 60 秒,一次登录卡在验证码上等 60 秒
意味着这次登录已经废了。硬凑成一个类会让两边都别扭。

## 「认出来了」和「认对了」是两件事

本模块只负责前者。它返回一个形状合法的字符串,**不知道也无法知道它对不对**
——对不对只有券商说了算。所以:

* 形状不合法(长度不对、混进汉字、模型开始讲道理)→ 当场判定失败,不返回;
* 形状合法 → 返回,由调用方拿去试。

调用方(``login.py``)必须把"提交后被拒"当成正常路径处理并**限次**。
这一条写在这里是因为它最容易被漏掉:验证码识别的成功率天然到不了 100%,
把它当成会成功的操作来写,失败时的行为就是没定义的。

## 三条路,而且可以串起来

``provider`` 决定走哪条:

* ``vision`` —— 把图交给视觉模型(OpenAI ``chat/completions`` 那套)。
* ``ttshitu`` —— 交给图鉴打码平台。
* ``chaojiying`` —— 交给超级鹰打码平台。

**再加一条:主用那条之外还可以配备用的**(``ChainSolver``)。
理由不是识别率,是单点:一家平台欠费或者当天挂了,登录就登不进去,
而这种失败在外面看起来不像"平台不行",看起来像"今天没交易"。
串起来之后,前一条抛 ``CaptchaError``/``LlmError`` 就换下一条;
全断了才失败,而且**一次把每条路的原因都报出来**。

**默认是 vision,但当前生产用的是 ttshitu**,理由不是效果而是策略:
2026-08-20 实测,通用大模型对"识别这张验证码"会明确拒答(原话
「抱歉，我不能帮助识别或破解验证码」),换提示词绕不过去。这个系统是拿
自己的账号登录自己的券商、认自己的验证码,所以走一个专做这件事的服务
是对的路;把两条都留着,是因为哪天视觉模型这条通了,不必再改结构。

## ⚠️ 不要打印图,不要打印密钥

验证码图片本身不敏感,但它来自一个**已登录的会话**——把它连同上下文写进
日志,等于在日志里留下这次会话的痕迹。密钥更不必说。本模块的日志只出
尺寸和耗时,不出内容。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from .llm import Credential, LlmError, retry_delay

logger = logging.getLogger("zhixing.captcha")


#: 单次请求超时(秒)。**比模型调用短得多。**
#: 登录是有人在等的动作,而且验证码有有效期——等太久就算认出来也过期了。
DEFAULT_TIMEOUT = 20.0

#: 重试次数。识别是**读操作**,重试安全(与写操作绝不自动重试相对)。
#: 但它比模型调用更吝啬:每多等一秒,验证码过期的概率就高一分。
DEFAULT_RETRIES = 1

DEFAULT_BACKOFF = 1.0

#: 识别方式。**这两个值会落进 ``captcha.json``**,所以是 ASCII 稳定串,
#: 不是给人看的标题——界面上的说法可以改,落盘的判据不能跟着改。
PROVIDER_VISION = "vision"
PROVIDER_TTSHITU = "ttshitu"
PROVIDER_CHAOJIYING = "chaojiying"
PROVIDERS = (PROVIDER_VISION, PROVIDER_TTSHITU, PROVIDER_CHAOJIYING)

#: 给人看的名字。**只用于报错和日志**,不落盘——落盘的判据是上面那几个
#: ASCII 串。失败信息里出现「图鉴」比出现 ``ttshitu`` 有用得多:看日志的
#: 人要去哪个平台的后台查余额,靠的是这个名字。
PROVIDER_NAMES = {
    PROVIDER_VISION: "视觉模型",
    PROVIDER_TTSHITU: "图鉴",
    PROVIDER_CHAOJIYING: "超级鹰",
}

#: 图鉴的接口地址。**没有默认值可用**——它是配置项,写死在这里只会让
#: "换了平台"这件事需要改代码。这个常量只用于自检和文档。
TTSHITU_URL = "https://api.ttshitu.com/predict"

#: 图鉴的题目类型。1 = 常规英数验证码。东财的是 4 位数字,
#: 二代拿这个值在生产里跑了几个月,不是猜的。
TTSHITU_TYPEID = "1"

#: 超级鹰的识别接口。和 ``TTSHITU_URL`` 一样只用于自检和文档:
#: 真正用哪个地址是配置项,写死在这里只会让"换平台"要改代码。
CHAOJIYING_URL = "https://upload.chaojiying.net/Upload/Processing.php"

#: 超级鹰的题目类型。1902 = 4 位英数。**和图鉴的 typeid 不通用**——
#: 两家各编各的号,填错了的表现是"一直识别失败"而不是报参数错。
CHAOJIYING_CODETYPE = "1902"

#: 样本目录里最多留多少张。**留着是为了将来训自研的识别器**,不是日志。
#: 封顶是因为它长在数据盘上,而没人会想起来去清:一天六轮、一轮几张,
#: 这个数够存几年,超过就停手不再写(不删旧的——旧图和新图一样有用)。
SAMPLE_LIMIT = 20000

#: 认出来的东西长什么样才算数。东财的验证码是 4 位数字或字母。
#: 收窄到这个范围是有代价的——万一改版成 5 位就会全线失败——
#: 但**放宽的代价更大**:模型的一句"我看不清"会被当成答案提交上去。
ANSWER_PATTERN = re.compile(r"^[0-9A-Za-z]{4}$")

#: 图片大小上限。超过基本可以断定拿错了东西(比如整页截图)。
MAX_IMAGE_BYTES = 512 * 1024


class CaptchaError(RuntimeError):
    """这张图没能变成一个可用的答案。

    和 ``LlmError`` 分开:后者是"没问到",前者是"问到了但不能用"。
    混成一个会让重试逻辑写错——前者重试有意义,后者重试多半还是同样的结果。
    """


# ---------------------------------------------------------------------------
#  算
# ---------------------------------------------------------------------------


def build_request(
    image: bytes, *, model: str, mime: str = "image/png", prompt: str | None = None
) -> dict[str, Any]:
    """拼一个视觉识别请求。**纯函数,不碰网络。**

    走 OpenAI 的 ``chat/completions`` 视觉格式(``image_url`` + data URI),
    理由和 ``model.build_request`` 一样:中转基本都兼容这一套。

    ``max_tokens`` 给得很小(16)。这不是省钱,是**约束**:留的余量越大,
    模型越容易开始解释自己在看什么。给到只够吐四个字符,它就只能吐四个字符。
    """
    if not image:
        raise CaptchaError("验证码图片是空的")
    if len(image) > MAX_IMAGE_BYTES:
        raise CaptchaError(
            f"验证码图片 {len(image)} 字节,超过 {MAX_IMAGE_BYTES} 的上限,"
            f"多半是截错了范围"
        )

    data_uri = f"data:{mime};base64," + base64.b64encode(image).decode("ascii")
    instruction = prompt or (
        "这是一张验证码图片。只输出图中的字符,4 位,数字或字母。"
        "不要解释,不要加标点,不要说别的。看不清就输出 ????。"
    )
    return {
        "model": model,
        "max_tokens": 16,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
    }


def extract_answer(payload: Mapping[str, Any]) -> str:
    """从回复里抠出答案并验形状。**纯函数。**

    ``????`` 是提示词里给模型的"我看不清"出口。给它一条明确的退路,
    好过让它在不确定的时候硬猜——猜出来的东西形状是合法的,会被当成答案
    提交上去,然后以"密码错误"的面目出现在别的地方。
    """
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise CaptchaError("识别接口没有返回任何结果")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise CaptchaError("识别接口返回的结构不认识")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise CaptchaError("识别接口返回里没有 message")

    raw = message.get("content")
    if not isinstance(raw, str):
        raise CaptchaError("识别接口返回的 content 不是字符串")

    text = raw.strip().strip("`").strip()
    # 模型偶尔会加一句"验证码是:"。只取最后一段连续的字母数字。
    matches = re.findall(r"[0-9A-Za-z?]{4}", text)
    candidate = matches[-1] if matches else text

    if "?" in candidate:
        raise CaptchaError("模型表示看不清这张验证码")
    if not ANSWER_PATTERN.match(candidate):
        raise CaptchaError(
            f"识别结果形状不合法(长度 {len(candidate)},期望 4 位数字或字母)"
        )
    return candidate


# ---------------------------------------------------------------------------
#  碰
# ---------------------------------------------------------------------------


def _post_json(url: str, body: Mapping[str, Any], *, timeout: float,
               headers: Mapping[str, str] | None = None) -> Mapping[str, Any]:
    """POST 一个 JSON,拿回一个 JSON。**两种识别方式共用这一层。**

    共用的只有 HTTP 这一段,不包括请求体和返回形状——那两样在视觉模型和
    图鉴之间完全不同,硬凑成一个函数会让两边都要拿参数去绕。

    错误一律翻成 ``LlmError``(带 ``retryable``),因为这一层能判断的只有
    "有没有问到"。**"问到了但不能用"由调用方判**,那是 ``CaptchaError``。
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **dict(headers or {}),
        },
        method="POST",
    )
    return _send(request, timeout=timeout)


def _post_form(url: str, fields: Mapping[str, str], *, timeout: float) -> Mapping[str, Any]:
    """POST 一个表单,拿回一个 JSON。**超级鹰只收表单,不收 JSON。**

    单独一个函数而不是给 ``_post_json`` 加参数:两者只有"编码请求体"这一
    步不同,但那一步决定了整个函数的样子。加个 ``as_form=True`` 开关的
    代价是每次读这段都要先看开关。
    """
    return _send(
        urllib.request.Request(
            url,
            data=urllib.parse.urlencode(dict(fields)).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        ),
        timeout=timeout,
    )


def _send(request: urllib.request.Request, *, timeout: float) -> Mapping[str, Any]:
    """发出去、读回来、翻成 JSON。**三种识别方式共用这一层。**

    共用的只有 HTTP 和"出错了算谁的"这两件事。错误一律翻成 ``LlmError``
    (带 ``retryable``),因为这一层能判断的只有"有没有问到"。
    **"问到了但不能用"由调用方判**,那是 ``CaptchaError``。
    """
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            # 截断到 200 字:出错的响应体里可能回显请求,而请求里有图。
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:      # noqa: BLE001 - 读不到就算了,别把原错误盖掉
            pass
        raise LlmError(
            f"验证码识别接口返回 HTTP {exc.code}:{detail}",
            retryable=exc.code in (408, 429, 500, 502, 503, 504),
            status=exc.code,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LlmError(f"连不上验证码识别接口:{exc}", retryable=True) from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LlmError(f"验证码识别接口返回的不是 JSON:{exc}", retryable=True) from exc
    if not isinstance(parsed, Mapping):
        raise LlmError("验证码识别接口返回的不是一个对象", retryable=True)
    return parsed


class CaptchaSolver(Protocol):
    """认图的那一下。**三个 Protocol 边界之一。**"""

    def solve(self, image: bytes, *, mime: str = "image/png") -> str:
        """返回 4 位答案。认不出来抛 ``CaptchaError``,没问到抛 ``LlmError``。"""
        ...


@dataclass
class AlwaysFailSolver:
    """占位实现。**明确地失败,不返回一个假答案。**

    和 ``runner.MissingDataSource`` 同一个用意:没配识别接口时,登录应该
    停在"没有这项能力"上,而不是拿着 ``0000`` 去试然后被锁号。

    ``reason`` 可以换掉,因为"没配"不止一种:少填了字段、和识别方式
    写了个不认识的值,是两回事。都报"还没配置"会让人去填一个已经填了的
    框——**说不出为什么失败的失败,和不失败一样难查。**
    """

    reason: str = (
        "没有配置验证码识别接口(设置 → 运行 → 验证码接口)。"
        "这不是识别失败,是这项能力还没接。"
    )

    def solve(self, image: bytes, *, mime: str = "image/png") -> str:
        raise CaptchaError(self.reason)


@dataclass
class HttpSolver:
    """真正发请求的实现。标准库 ``urllib``,不引第三方。"""

    endpoint: str
    model: str
    credential: Credential
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    backoff: float = DEFAULT_BACKOFF
    #: 注进来是为了自检不用真睡。
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def _url(self) -> str:
        base = self.endpoint.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")

    def _once(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        return _post_json(
            self._url(), body, timeout=self.timeout,
            headers={"Authorization": f"Bearer {self.credential.require()}"})

    def solve(self, image: bytes, *, mime: str = "image/png") -> str:
        body = build_request(image, model=self.model, mime=mime)
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                payload = self._once(body)
            except LlmError as exc:
                if not exc.retryable or attempt >= self.retries:
                    raise
                attempt += 1
                delay = retry_delay(attempt, backoff=self.backoff, seed=self.model)
                logger.warning("验证码识别第 %d 次重试,等 %.1fs", attempt, delay)
                self.sleep(delay)
                continue
            # 只记尺寸和耗时,**不记内容**——见模块开头。
            logger.info(
                "验证码识别完成:%d 字节,%.2fs", len(image), time.monotonic() - started
            )
            return extract_answer(payload)


# ---------------------------------------------------------------------------
#  图鉴(ttshitu.com)
# ---------------------------------------------------------------------------


def split_credential(raw: str) -> tuple[str, str]:
    """把 ``用户名:密码`` 拆开。**纯函数。**

    为什么合成一项存:这两段合起来才是一份凭据。拆成两个配置项之后,
    "改了用户名忘了改密码"就成了一种可以存在的状态,而它表现出来的样子
    是"识别老是失败",没人会往配置上想。

    密码里可以有冒号(只按第一个冒号切),用户名里不行——图鉴的用户名
    本来就不允许冒号。
    """
    text = (raw or "").strip()
    user, sep, password = text.partition(":")
    if not sep or not user.strip() or not password:
        raise CaptchaError(
            "图鉴的密钥要写成「用户名:密码」两段,当前这份拆不出两段。"
        )
    return user.strip(), password


def build_ttshitu_request(image: bytes, *, credential: str,
                          typeid: str = TTSHITU_TYPEID) -> dict[str, Any]:
    """拼图鉴的请求体。**纯函数,不碰网络。**

    图鉴收的是 base64 后的原图,不挑格式(东财那张实际是 JPEG 不是 PNG,
    所以这里**不看也不改 mime**——看了反而会拒掉本来能认的图)。
    """
    if not image:
        raise CaptchaError("验证码图片是空的")
    if len(image) > MAX_IMAGE_BYTES:
        raise CaptchaError(
            f"验证码图片 {len(image)} 字节,超过 {MAX_IMAGE_BYTES} 的上限,"
            f"多半是截错了范围"
        )
    user, password = split_credential(credential)
    return {
        "username": user,
        "password": password,
        "typeid": str(typeid or TTSHITU_TYPEID),
        "image": base64.b64encode(image).decode("ascii"),
    }


def extract_ttshitu_answer(payload: Mapping[str, Any]) -> str:
    """从图鉴的回复里抠出答案并验形状。**纯函数。**

    图鉴失败时 HTTP 仍然是 200,失败信息在 ``success`` / ``message`` 里。
    **不把这种情况当成"没问到"**——它问到了,只是没结果,重试多半还是
    同样的结果(余额不足、题目类型不对)。所以抛 ``CaptchaError`` 而不是
    ``LlmError``,并把平台的原话带上:那句话才是真正能指向原因的东西。
    """
    if not payload.get("success"):
        message = str(payload.get("message") or "").strip() or "(平台没说原因)"
        raise CaptchaError(f"图鉴没能识别:{message}")
    data = payload.get("data")
    result = data.get("result") if isinstance(data, Mapping) else None
    if not isinstance(result, str) or not result.strip():
        raise CaptchaError("图鉴返回成功,但没带识别结果")
    candidate = result.strip()
    if not ANSWER_PATTERN.match(candidate):
        raise CaptchaError(
            f"图鉴的识别结果形状不合法(长度 {len(candidate)},期望 4 位数字或字母)"
        )
    return candidate


@dataclass
class TtshituSolver:
    """图鉴打码平台。标准库 ``urllib``,不引第三方。

    和 ``HttpSolver`` 并列而不是继承:两者只有"POST 一个 JSON"这一点相同,
    请求体、返回形状、失败语义全不一样。抽一个基类出来,能共用的只有
    十几行 HTTP,而代价是每次看代码都要在两个文件之间跳。
    """

    endpoint: str
    credential: Credential
    typeid: str = TTSHITU_TYPEID
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    backoff: float = DEFAULT_BACKOFF
    #: 注进来是为了自检不用真睡。
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def solve(self, image: bytes, *, mime: str = "image/png") -> str:
        body = build_ttshitu_request(
            image, credential=self.credential.require(), typeid=self.typeid)
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                payload = _post_json(self.endpoint, body, timeout=self.timeout)
            except LlmError as exc:
                if not exc.retryable or attempt >= self.retries:
                    raise
                attempt += 1
                delay = retry_delay(attempt, backoff=self.backoff, seed="ttshitu")
                logger.warning("图鉴识别第 %d 次重试,等 %.1fs", attempt, delay)
                self.sleep(delay)
                continue
            # 只记尺寸和耗时,**不记内容**——见模块开头。
            logger.info(
                "验证码识别完成(图鉴):%d 字节,%.2fs",
                len(image), time.monotonic() - started)
            return extract_ttshitu_answer(payload)


# ---------------------------------------------------------------------------
#  超级鹰(chaojiying.com)
# ---------------------------------------------------------------------------


def split_chaojiying_credential(raw: str) -> tuple[str, str, str]:
    """把 ``用户名:密码:软件ID`` 拆成三段。**纯函数。**

    合成一项存的理由和 ``split_credential`` 一样:三段合起来才是一份凭据,
    拆成三个配置项之后"改了一段忘了另一段"就成了一种能存在的状态,
    而它表现出来的样子是"识别老是失败",没人会往配置上想。

    软件ID 从**右边**切,用户名从左边切,所以密码里可以有冒号。
    软件ID 必须是数字:超级鹰的 ``softid`` 是个整数,填错了它不报参数错,
    只是不给这个软件分成——**一个不报错的错**,所以在这里当场拦住。
    """
    text = (raw or "").strip()
    head, sep, softid = text.rpartition(":")
    user, sep2, password = head.partition(":")
    if not sep or not sep2 or not user.strip() or not password or not softid.strip():
        raise CaptchaError(
            "超级鹰的密钥要写成「用户名:密码:软件ID」三段,当前这份拆不出三段。"
        )
    softid = softid.strip()
    if not softid.isdigit():
        raise CaptchaError(
            f"超级鹰的软件ID 得是数字,当前这份第三段是 {len(softid)} 个字符的非数字。"
            f"它在软件KEY(形如「32位KEY.时间戳.软件ID」)的最后一段里。"
        )
    return user.strip(), password, softid


def build_chaojiying_request(image: bytes, *, credential: str,
                             codetype: str = CHAOJIYING_CODETYPE) -> dict[str, str]:
    """拼超级鹰的表单。**纯函数,不碰网络。**

    密码走 ``pass2``(密码的 md5,32 位小写)而不是 ``pass``:
    平台两个都收,而**明文那一个没有任何好处**——同样的效果,少一次
    密码离开本机的机会。
    """
    if not image:
        raise CaptchaError("验证码图片是空的")
    if len(image) > MAX_IMAGE_BYTES:
        raise CaptchaError(
            f"验证码图片 {len(image)} 字节,超过 {MAX_IMAGE_BYTES} 的上限,"
            f"多半是截错了范围"
        )
    user, password, softid = split_chaojiying_credential(credential)
    return {
        "user": user,
        "pass2": hashlib.md5(password.encode("utf-8")).hexdigest(),
        "softid": softid,
        "codetype": str(codetype or CHAOJIYING_CODETYPE),
        "file_base64": base64.b64encode(image).decode("ascii"),
    }


def extract_chaojiying_answer(payload: Mapping[str, Any]) -> str:
    """从超级鹰的回复里抠出答案并验形状。**纯函数。**

    和图鉴一样:失败时 HTTP 仍然是 200,失败信息在 ``err_no``/``err_str``
    里。**不把这种情况当成"没问到"**——它问到了,只是没结果,而且原因
    (题分不足、用户名密码错、图片格式不对)重试一遍多半还是同样的结果。
    所以抛 ``CaptchaError`` 并把平台原话带上。

    ⚠️ 平台还有一个"报错返分"接口(``ReportError.php``),识别错了可以
    退分。**这里不自动调**:它的文档原话是「此接口不能随便调用」,而这一层
    根本不知道答案对不对——对不对只有券商说了算,见模块开头。
    """
    err_no = payload.get("err_no")
    try:
        码 = int(err_no)
    except (TypeError, ValueError):
        码 = -1
    if 码 != 0:
        原话 = str(payload.get("err_str") or "").strip() or "(平台没说原因)"
        raise CaptchaError(f"超级鹰没能识别(err_no={err_no}):{原话}")
    result = payload.get("pic_str")
    if not isinstance(result, str) or not result.strip():
        raise CaptchaError("超级鹰返回成功,但没带识别结果")
    candidate = result.strip()
    if not ANSWER_PATTERN.match(candidate):
        raise CaptchaError(
            f"超级鹰的识别结果形状不合法(长度 {len(candidate)},期望 4 位数字或字母)"
        )
    return candidate


@dataclass
class ChaojiyingSolver:
    """超级鹰打码平台。标准库 ``urllib``,不引第三方。

    和 ``TtshituSolver`` 并列而不是继承,理由见那边:能共用的只有十几行
    HTTP(已经共用了,在 ``_send`` 里),请求体、返回形状、失败语义全不一样。
    """

    endpoint: str
    credential: Credential
    codetype: str = CHAOJIYING_CODETYPE
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    backoff: float = DEFAULT_BACKOFF
    #: 注进来是为了自检不用真睡。
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def solve(self, image: bytes, *, mime: str = "image/png") -> str:
        fields = build_chaojiying_request(
            image, credential=self.credential.require(), codetype=self.codetype)
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                payload = _post_form(self.endpoint, fields, timeout=self.timeout)
            except LlmError as exc:
                if not exc.retryable or attempt >= self.retries:
                    raise
                attempt += 1
                delay = retry_delay(attempt, backoff=self.backoff, seed="chaojiying")
                logger.warning("超级鹰识别第 %d 次重试,等 %.1fs", attempt, delay)
                self.sleep(delay)
                continue
            # 只记尺寸和耗时,**不记内容**——见模块开头。
            logger.info(
                "验证码识别完成(超级鹰):%d 字节,%.2fs",
                len(image), time.monotonic() - started)
            return extract_chaojiying_answer(payload)


# ---------------------------------------------------------------------------
#  串起来
# ---------------------------------------------------------------------------


def record_sample(directory: Path, image: bytes, answer: str, *, source: str,
                  limit: int = SAMPLE_LIMIT) -> bool:
    """把一张图和它的答案存进样本目录。**存不下不算错。**

    存这个只有一个用途:**将来训自己的识别器**。现在没有第一条路,是因为
    没有带标注的真图;一天六轮攒下去,过阵子就有了。

    ⚠️ 这里的答案是**平台说的,不是券商认过的**。券商认没认只有登录那一步
    知道,而那一步在另一个模块。所以这些标注里必然混着错的,拿它训之前
    得先筛——**不能反过来当缓存直接复用**,那样一个错答案会对同一张图
    永远错下去。

    和模块开头"不要打印图"不冲突:那说的是日志(会被翻阅、会被转发);
    这里写的是数据盘上一个 0700 的目录,和归档同一个待遇。

    任何 IO 错误都只记一行日志:攒样本失败不该让一次登录失败。
    """
    if not directory or not answer:
        return False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass          # 挂载盘不给改权限是常事,不值得为它放弃攒样本
        # 文件名带图的指纹:同一张图不重复存,也省掉一次比对。
        指纹 = hashlib.sha256(image).hexdigest()[:16]
        目标 = directory / f"{指纹}-{answer}-{source}.img"
        if 目标.exists():
            return False
        if sum(1 for _ in directory.iterdir()) >= limit:
            logger.debug("验证码样本已到 %d 张上限,不再攒。", limit)
            return False
        目标.write_bytes(image)
        try:
            目标.chmod(0o600)
        except OSError:
            pass          # 目录已经是 0700,文件位改不动也不至于漏出去
        return True
    except OSError as exc:
        logger.debug("验证码样本没存下(%s),不影响本次登录。", exc)
        return False


@dataclass
class ChainSolver:
    """按顺序试几条识别路,**全断了才失败,而且一次报全部原因**。

    为什么不是"挑一个最好的":这一层根本不知道哪条更好——它不知道答案
    对不对(见模块开头)。它只知道"这条路给出了一个形状合法的答案"或者
    "这条路没给出来"。所以策略只能是顺序,顺序由配置定。

    ``CaptchaError``(认不出)和 ``LlmError``(没问到)都换下一条:
    对登录来说这两种失败是一回事——图没变成字符串。但**最后抛出去的那个
    异常要分**:如果每条路都是"没问到",那是网络/平台侧的事,抛
    ``LlmError`` 让上面按可重试处理;只要有一条是"认不出",就抛
    ``CaptchaError`` ——换张图重来比原地重试有意义。

    ## ⚠️ 光有降级是不够的,还要能换起点

    降级只在"这条路**没给出**答案"时触发。可识别接口最常见的故障模式
    根本不是不给答案,而是**给一个形状合法但认错了的答案**——四位数字,
    格式挑不出毛病,只是不对。那种情况这一层看不出来(它不知道答案对不
    对,见上),于是直接就用了,后面几条路一次都轮不到。

    2026-08-21 实测:全天 11 次登录识别,主路(图鉴)11 次全部返回了合法
    四位数,其中 6 次是错的;备用路(超级鹰)**被调用 0 次**。配着、能用、
    在付费,可它永远等不到自己上场——因为触发条件是主路"哑了",而主路
    从不哑,它只是答错。

    所以除了 ``solve``(从头试),这里还开一个 ``solve_from``:让登录层的
    第 N 次尝试从第 N 条路起认。降级管的是"这条路断了",换起点管的是
    "这条路答错了"——**两种失败,两套办法,不能指望一套顶两套。**
    """

    links: tuple[tuple[str, CaptchaSolver], ...]
    #: 样本目录。``None`` = 不攒。见 ``record_sample``。
    sample_dir: Path | None = None

    def solve(self, image: bytes, *, mime: str = "image/png") -> str:
        """从头试。``CaptchaSolver`` 协议要求的那个入口,行为不变。"""
        return self.solve_from(image, mime=mime, start=0)

    def solve_from(
        self, image: bytes, *, mime: str = "image/png", start: int = 0
    ) -> str:
        """从第 ``start`` 条路起往下试,前面的**跳过不问**。

        ``start`` 超出范围就压到最后一条,不报错:链有几条是配置说了算的,
        而重试几次是登录层说了算的,两个数对不上是常态(配两条、试三次)。
        这种时候"再问一遍最后那条"仍然有意义——每次尝试抓的是**一张新图**,
        同一条路对新图完全可能给出不同的答案,不是原地重复。
        """
        if not self.links:
            raise CaptchaError("验证码识别一条路都没配")
        起 = min(max(0, int(start)), len(self.links) - 1)
        待试 = self.links[起:]
        失败: list[str] = []
        全是没问到 = True
        可重试 = False
        if 起:
            logger.info("验证码识别从第 %d 条路(%s)起认,前 %d 条这次跳过。",
                        起 + 1, 待试[0][0], 起)
        for 名, solver in 待试:
            try:
                answer = solver.solve(image, mime=mime)
            except CaptchaError as exc:
                全是没问到 = False
                失败.append(f"{名}:{exc}")
                continue
            except LlmError as exc:
                可重试 = 可重试 or bool(getattr(exc, "retryable", False))
                失败.append(f"{名}:没问到({exc})")
                continue
            if 失败:
                # 前面有路断了但这条通了。**这行要留**:平台悄悄失效时,
                # 系统仍然能跑,于是没人会发现——直到备用那条也断了。
                logger.warning("验证码识别由「%s」兜住,前面 %d 条没走通:%s",
                               名, len(失败), ";".join(失败))
            if self.sample_dir is not None:
                record_sample(self.sample_dir, image, answer, source=名)
            return answer

        原因 = ";".join(失败) or "一条识别路都没配"
        话 = f"验证码识别的 {len(待试)} 条路都没走通 —— {原因}"
        if 全是没问到 and 待试:
            raise LlmError(话, retryable=可重试)
        raise CaptchaError(话)


def _solver_for(
    provider: str, endpoint: str, model_name: str, secret: str
) -> CaptchaSolver:
    """按配置装一个识别器。**配得不全就装占位的那个,不是抛异常。**

    收的是四个字段而不是配置对象,这样备用那几条(它们不是
    ``CaptchaSettings``,只是同样形状的几个字段)能走同一段逻辑。
    组装成链在 ``solver_from_settings`` 里,那里也故意不 import ``state``
    ——认图这件事不该反过来依赖落盘层。

    三缺一就返回 ``AlwaysFailSolver``。这个选择在这里做一次,是为了不让
    ``login.py`` 各做一次:那种地方最容易写成"没配就跳过验证码"——
    而跳过的结果不是登录失败,是拿着空答案去提交,然后以「验证码错误」的
    面目出现,人会去查识别接口,可问题在于压根没配。

    抛异常同样不行:配置缺失是常态(头一次装机就是),不该让调用方
    用 try 把"还没配"和"接口挂了"接在同一个 except 里。
    """
    endpoint = str(endpoint or "").strip()
    model_name = str(model_name or "").strip()
    secret = str(secret or "")
    # 空 ``provider`` 按视觉模型算:**改这个模块之前落盘的配置里没有这一
    # 项**,让它们的行为保持原样,而不是集体变成"识别方式不认识"。
    provider = str(provider or PROVIDER_VISION).strip()

    if provider == PROVIDER_CHAOJIYING:
        # 超级鹰和图鉴一样不要"模型",它要的是题目类型,而且有默认值。
        缺 = [名 for 名, 值 in (("接口地址", endpoint), ("密钥", secret)) if not 值]
        if 缺:
            logger.info("验证码识别(超级鹰)未配置完整(缺:%s),这一条会明确失败。",
                        "、".join(缺))
            return AlwaysFailSolver(
                reason=f"验证码识别(超级鹰)还缺:{'、'.join(缺)}。"
                       f"这不是识别失败,是这条路还没配全。")
        return ChaojiyingSolver(
            endpoint=endpoint,
            credential=Credential(secret),
            codetype=model_name or CHAOJIYING_CODETYPE,
        )

    if provider == PROVIDER_TTSHITU:
        # 图鉴不要"模型",它要的是题目类型,而且有默认值。
        # **不把这一项算进"缺什么"里**——报一个填了也没用的字段,人会照着
        # 填,然后发现还是不行,于是开始怀疑填对了的那些。
        缺 = [名 for 名, 值 in (("接口地址", endpoint), ("密钥", secret)) if not 值]
        if 缺:
            logger.info("验证码识别(图鉴)未配置完整(缺:%s),登录将明确失败。",
                        "、".join(缺))
            return AlwaysFailSolver(
                reason=f"验证码识别(图鉴)还缺:{'、'.join(缺)}。"
                       f"这不是识别失败,是这项能力还没配全。")
        return TtshituSolver(
            endpoint=endpoint,
            credential=Credential(secret),
            typeid=model_name or TTSHITU_TYPEID,
        )

    if provider != PROVIDER_VISION:
        logger.warning("不认识的验证码识别方式 %r,登录将明确失败。", provider)
        return AlwaysFailSolver(
            reason=f"验证码识别方式 {provider!r} 不认识,可选的是:"
                   f"{'、'.join(PROVIDERS)}。")

    if not (endpoint and model_name and secret):
        缺 = [
            名 for 名, 值 in (("接口地址", endpoint), ("模型", model_name), ("密钥", secret))
            if not 值
        ]
        logger.info("验证码识别接口未配置完整(缺:%s),登录将明确失败。", "、".join(缺))
        return AlwaysFailSolver(
            reason=f"验证码识别接口还缺:{'、'.join(缺)}(设置 → 运行 → 验证码接口)。"
                   f"这不是识别失败,是这项能力还没配全。")
    return HttpSolver(endpoint=endpoint, model=model_name, credential=Credential(secret))


#: 上一次装出来的链长什么样。**只用来决定那行 INFO 要不要再说一遍**,
#: 不参与任何判断——它是日志的状态,不是系统的状态。
_上次装的链 = ""


def solver_from_settings(settings: Any) -> CaptchaSolver:
    """按配置装识别器,**有备用就串成一条链**。

    接受 ``state.CaptchaSettings``:主用那条读 ``endpoint`` / ``model`` /
    ``secret`` / ``provider``,备用那几条读 ``backups``(同样四个属性的
    若干个对象),样本目录读 ``sample_dir``。故意不 import ``state``。

    **没有备用、也不攒样本时,直接返回那一个识别器**,不套一层壳:
    多一层壳就多一层要在报错里剥的东西,而单条路上它一点用都没有。
    """
    主 = _solver_for(
        str(getattr(settings, "provider", "") or PROVIDER_VISION),
        str(getattr(settings, "endpoint", "") or ""),
        str(getattr(settings, "model", "") or ""),
        str(getattr(settings, "secret", "") or ""),
    )
    备用 = tuple(getattr(settings, "backups", ()) or ())
    样本 = str(getattr(settings, "sample_dir", "") or "").strip()
    if not 备用 and not 样本:
        return 主

    def 名(p: str) -> str:
        return PROVIDER_NAMES.get(p, p or "(没写识别方式)")

    links: list[tuple[str, CaptchaSolver]] = [
        (名(str(getattr(settings, "provider", "") or PROVIDER_VISION)), 主)
    ]
    for i, 条 in enumerate(备用, start=1):
        p = str(getattr(条, "provider", "") or "")
        links.append((
            f"备用{i}·{名(p)}",
            _solver_for(p,
                        str(getattr(条, "endpoint", "") or ""),
                        str(getattr(条, "model", "") or ""),
                        str(getattr(条, "secret", "") or "")),
        ))
    # **变了才说。** 守护进程每轮轮询都会重装一次识别器,每次都说一遍的话
    # 一天四千多行,而日志没有轮转——真正要看的那行(「备用兜住了」)会被
    # 埋掉。启动时和改完配置后各说一次,正好是需要它的那两个时刻。
    global _上次装的链
    描述 = "、".join(n for n, _ in links)
    if 描述 != _上次装的链:
        logger.info("验证码识别装了 %d 条路:%s", len(links), 描述)
        _上次装的链 = 描述
    else:
        logger.debug("验证码识别装了 %d 条路:%s", len(links), 描述)
    return ChainSolver(links=tuple(links), sample_dir=Path(样本) if 样本 else None)


__all__ = [
    "DEFAULT_TIMEOUT", "DEFAULT_RETRIES", "ANSWER_PATTERN", "MAX_IMAGE_BYTES",
    "SAMPLE_LIMIT",
    "PROVIDER_VISION", "PROVIDER_TTSHITU", "PROVIDER_CHAOJIYING",
    "PROVIDERS", "PROVIDER_NAMES",
    "TTSHITU_URL", "TTSHITU_TYPEID", "CHAOJIYING_URL", "CHAOJIYING_CODETYPE",
    "CaptchaError", "build_request", "extract_answer",
    "split_credential", "build_ttshitu_request", "extract_ttshitu_answer",
    "split_chaojiying_credential", "build_chaojiying_request",
    "extract_chaojiying_answer",
    "CaptchaSolver", "AlwaysFailSolver", "HttpSolver", "TtshituSolver",
    "ChaojiyingSolver", "ChainSolver", "record_sample",
    "solver_from_settings",
]
