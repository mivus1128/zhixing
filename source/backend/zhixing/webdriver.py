"""浏览器远端控制(碰) —— W3C WebDriver 协议的最小客户端。

## 为什么不用 selenium

WebDriver 是一套 **JSON over HTTP** 的协议,不是什么需要 SDK 的东西。
这一层真正要用的只有六个动作:开会话、关会话、跳转、读地址、跑脚本、
设超时。为这六个动作引入 selenium,会连带 trio、trio-websocket、
urllib3、certifi 一整串,而它们买到的东西这里一样都用不上——
不用元素定位(二代早就发现点 DOM 不如注脚本可靠)、不用等待策略、
不用浏览器管理(Grid 在容器里现成)。

所以后端到这一步为止仍然**零第三方依赖**。

## 密码会从这里过

``execute_async`` 的 ``args`` 里会出现交易密码——登录脚本要拿它填表单。
所以本模块**任何日志都不打 args,也不打 script 正文**,出错时只报
动作名和 HTTP 状态。这不是谨慎过头:WebDriver 的错误响应会回显请求体,
照原样记下来等于把密码写进日志。

## 和 Grid 的关系

远端地址形如 ``http://browser:4444/wd/hub``(容器内)或
``http://127.0.0.1:4444``(直连)。两种写法都认,见 ``_endpoint``。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

logger = logging.getLogger("zhixing.webdriver")

#: 单次 HTTP 请求超时(秒)。脚本执行本身另有超时,见 ``set_script_timeout``。
DEFAULT_TIMEOUT = 60.0

#: 异步脚本的超时(毫秒)。东财的接口慢起来能到十几秒。
DEFAULT_SCRIPT_TIMEOUT_MS = 30_000

#: 建会话时给浏览器的能力。
#:
#: ``--no-sandbox`` 是容器里的必需项;``--disable-dev-shm-usage`` 是因为
#: Docker 默认 /dev/shm 只有 64M,Chromium 在那上面会莫名其妙崩。
#: 窗口尺寸写死是为了**验证码的位置可复现**——尺寸一变,截图裁剪的坐标
#: 就全变了(虽然我们走 canvas 不走裁剪,但页面布局本身也会跟着变)。
DEFAULT_CAPABILITIES: dict[str, Any] = {
    "browserName": "chrome",
    "goog:chromeOptions": {
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1600,1000",
            "--disable-blink-features=AutomationControlled",
        ],
    },
}


class WebDriverError(RuntimeError):
    """WebDriver 那头出的错。

    ``error`` 是 W3C 定的错误码(``no such element``、``timeout`` 等),
    照原样带出来是为了让上层能分辨"脚本超时"和"会话没了"——
    这两件事的处置完全不同。
    """

    def __init__(self, message: str, *, error: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.error = error
        self.status = status

    @property
    def session_gone(self) -> bool:
        """会话已经不在了。**这种错重连有用,重试同一个会话没用。**"""
        return self.error in {"invalid session id", "no such window", "session not created"}


def _endpoint(remote_url: str) -> str:
    """规范化远端地址。``/wd/hub`` 加不加都认。"""
    return remote_url.rstrip("/")


def _request(
    method: str, url: str, body: Mapping[str, Any] | None, *, timeout: float, action: str
) -> Any:
    """发一次请求,把 W3C 的 ``{"value": ...}` 剥出来。

    **不记 body。** 见模块开头。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        error_code = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            value = payload.get("value") if isinstance(payload, Mapping) else None
            if isinstance(value, Mapping):
                error_code = str(value.get("error") or "")
                # message 里可能回显请求体,截断。密码不会因为截断而安全,
                # 但这里的 message 是 WebDriver 自己写的错误说明,不含 args。
                detail = str(value.get("message") or "")[:300]
        except Exception:      # noqa: BLE001 - 解析不了就用状态码说话
            pass
        raise WebDriverError(
            f"{action} 失败(HTTP {exc.code}{'/' + error_code if error_code else ''}):{detail}",
            error=error_code, status=exc.code,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WebDriverError(f"{action} 连不上浏览器远端:{exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WebDriverError(f"{action} 返回的不是 JSON:{exc}") from exc
    if not isinstance(payload, Mapping) or "value" not in payload:
        raise WebDriverError(f"{action} 返回里没有 value 字段")
    return payload["value"]


@dataclass
class Session:
    """一个浏览器会话。**用完要关**,不关会在 Grid 上挂着占内存。

    推荐用 ``with open_session(...) as s:``,异常路径也会关。
    """

    remote_url: str
    session_id: str
    timeout: float = DEFAULT_TIMEOUT
    #: 已经关过了。重复关不报错,但也不再发请求。
    closed: bool = field(default=False, repr=False)

    def _url(self, suffix: str) -> str:
        return f"{_endpoint(self.remote_url)}/session/{self.session_id}{suffix}"

    def set_script_timeout(self, milliseconds: int = DEFAULT_SCRIPT_TIMEOUT_MS) -> None:
        _request("POST", self._url("/timeouts"), {"script": milliseconds},
                 timeout=self.timeout, action="设置脚本超时")

    def navigate(self, url: str) -> None:
        """跳转。**页面加载超时不当失败**——东财有些资源加载得没完没了,
        但接口早就能用了。真正判断"到没到"的是后面的脚本。"""
        try:
            _request("POST", self._url("/url"), {"url": url},
                     timeout=self.timeout, action="跳转")
        except WebDriverError as exc:
            if exc.error == "timeout":
                logger.warning("页面加载超时,继续往下走(接口可能已经可用)")
                return
            raise

    def current_url(self) -> str:
        return str(_request("GET", self._url("/url"), None,
                            timeout=self.timeout, action="读地址") or "")

    def execute_async(self, script: str, *args: Any) -> Any:
        """跑一段异步脚本。脚本用最后一个参数作为回调。

        ⚠️ ``args`` 里可能有交易密码。**这里和下游都不记 args。**
        """
        return _request("POST", self._url("/execute/async"),
                        {"script": script, "args": list(args)},
                        timeout=self.timeout, action="执行异步脚本")

    def execute(self, script: str, *args: Any) -> Any:
        return _request("POST", self._url("/execute/sync"),
                        {"script": script, "args": list(args)},
                        timeout=self.timeout, action="执行脚本")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            _request("DELETE", self._url(""), None, timeout=self.timeout, action="关会话")
        except WebDriverError as exc:
            # 关不掉不该把调用方的异常盖掉。Grid 有自己的空闲回收。
            logger.warning("会话关闭失败(Grid 会自行回收):%s", exc)

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def new_session(
    remote_url: str,
    *,
    capabilities: Mapping[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Session:
    """开一个新会话。"""
    caps = dict(capabilities or DEFAULT_CAPABILITIES)
    value = _request(
        "POST", f"{_endpoint(remote_url)}/session",
        {"capabilities": {"alwaysMatch": caps}},
        timeout=timeout, action="建会话",
    )
    if not isinstance(value, Mapping):
        raise WebDriverError("建会话返回格式不对")
    session_id = str(value.get("sessionId") or "")
    if not session_id:
        raise WebDriverError("建会话没拿到 sessionId")
    session = Session(remote_url=remote_url, session_id=session_id, timeout=timeout)
    session.set_script_timeout()
    logger.info("浏览器会话已建立")
    return session


def attach(remote_url: str, session_id: str, *, timeout: float = DEFAULT_TIMEOUT) -> Session:
    """接上一个已经存在的会话。

    用途是**跨进程复用登录态**:登录一次要过验证码,重启服务不该重来一遍。
    会话 id 存在 runtime 目录里,起来先试着接,接不上再开新的。
    """
    return Session(remote_url=remote_url, session_id=session_id, timeout=timeout)


def sessions(remote_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """Grid 上现有的会话。**只用于排查**,业务逻辑不该依赖它。

    这是 Grid 的扩展接口,不在 W3C 标准里;拿不到就返回空表,不抛错。
    """
    try:
        value = _request("GET", f"{_endpoint(remote_url)}/status", None,
                         timeout=timeout, action="读 Grid 状态")
    except WebDriverError:
        return []
    if isinstance(value, Mapping):
        nodes = value.get("nodes")
        if isinstance(nodes, Sequence):
            return [n for n in nodes if isinstance(n, dict)]
    return []


__all__ = [
    "DEFAULT_TIMEOUT", "DEFAULT_SCRIPT_TIMEOUT_MS", "DEFAULT_CAPABILITIES",
    "WebDriverError", "Session", "new_session", "attach", "sessions",
]
