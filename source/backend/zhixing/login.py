"""券商登录(碰)—— 开浏览器、填表、认验证码、确认真的登进去了。

## 这一层的一条硬规矩:**登录失败不许瞎重试**

自动化最容易犯、代价最大的错,是把"密码不对"当成"网络抖了"然后重试五次。
那不叫重试,那叫**拿账户去撞券商的错误次数上限**,试满就锁卡,锁了卡这套
系统当天就没了,而且要人拿身份证去柜台。

所以本模块把失败分成三类,只有中间那一类重试:

- **配置不全** —— 缺远端地址 / 账号 / 密码 / 识别接口。一次报全,不重试。
- **验证码没认对** —— 换张图重来,最多 ``MAX_ATTEMPTS`` 次。
- **其它一切** —— 包括"判不出来是哪种"。**不重试。** 见
  ``eastmoney.classify_login_error`` 里那段关于代价不对称的说明。

## "登进去了"以什么为准

不以「提交没报错」为准,也不以「地址跳走了」为准。**以查一次账户接口
拿到了 Status=0 为准。** 前两者都能在没真登录的情况下成立——东财会
先跳转再把你踢回来,那中间有几百毫秒的窗口,盯着地址看就会看错。

这条对应二代缺陷 6 的反面:不做没有验证的断言。

## 涉密

``BrokerSettings.account`` 和 ``password`` 从这里流进浏览器脚本的参数。
**本模块所有日志都不打这两样,也不打脚本参数。** 出错时只说到哪一步、
页面报了什么话。``webdriver`` 那一层同样不记 args,两层都守住才算守住。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import captcha as captcha_mod
from . import eastmoney as em
from . import webdriver as wd

logger = logging.getLogger("zhixing.login")

#: 验证码最多认几次。**这个数字是安全上限,不是性能参数。**
#:
#: 东财对登录失败次数有限制(具体多少不公开,二代实测里没撞到过)。
#: 而万一是密码错了,``classify_login_error`` 会在第一次就拦下来,
#: 根本走不到第三次。
#:
#: ⚠️ **三次的价值取决于三次不是同一条路。** 原先这三次都从识别链的
#: 第一条起认,于是三次全落在同一个识别接口上;那个接口对哪类字形认不
#: 准是稳定的,换张新图仍然大概率栽在同一类字形上——三次尝试的成功率
#: 远不是 1-(1-p)³,它们的错误是相关的。2026-08-21 实测:主路当天准确率
#: 45%(11 次错 6 次),两轮登录各试满三次全败。
#:
#: 现在第 N 次尝试从链的第 N 条路起认(见 ``captcha.ChainSolver.solve_from``),
#: 三次尝试落在三个互相独立的识别器上,才谈得上"三次里总有一次对"。
MAX_ATTEMPTS = 3

#: 两次尝试之间等多久(秒)。**不是指数退避**——退避是为了让过载的服务
#: 缓过来,而这里的失败是认错了字,和券商忙不忙没关系。等一下只是为了
#: 让页面把新验证码加载出来。
RETRY_PAUSE = 2.0


class LoginError(RuntimeError):
    """登录没成。

    ``stage`` 说明卡在哪一步,``retryable`` 说明还值不值得再来一次。
    **默认 False**:判不出来就当不能重试,理由见模块开头。

    ``exhausted`` 只有一个用处:区分「一次就判定不能重试」和「该试的都
    试完了还是不行」。这两件事对人的意思完全不同——前者是"我认出这是
    密码错了/配置缺了",后者是"每一条识别路都用过了,原因不明"。
    上层要据此决定界面上写什么,不能靠去匹配错误文字。
    """

    def __init__(self, message: str, *, stage: str = "", retryable: bool = False,
                 exhausted: bool = False) -> None:
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable
        self.exhausted = exhausted


@dataclass(frozen=True)
class LoginResult:
    """登录成功之后拿到的东西。

    顺手把账户报表带出来:验证登录本来就要查一次账户接口,那一次的返回
    扔掉再查一遍是白花一个来回,而且两次之间账户可能已经变了。
    """

    account: em.AccountReport
    attempts: int
    #: 这次是复用了已有会话,还是重新登的。**用于运行事实,不是装饰**——
    #: 天天重登说明会话保持有问题,这个字段是唯一能看出来的地方。
    reused: bool


def _ok(result: Any, stage: str) -> Mapping[str, Any]:
    """脚本返回值的统一入口。**不把返回值原样记进日志**——里面可能有回显。"""
    if not isinstance(result, Mapping):
        raise LoginError(f"{stage}:脚本没返回对象(拿到 {type(result).__name__})。", stage=stage)
    if not result.get("ok", True):
        detail = str(result.get("detail") or "").strip() or "脚本报告失败,但没说原因。"
        raise LoginError(f"{stage}:{detail}", stage=stage)
    return result


# ---------------------------------------------------------------------------
#  单步(碰)
# ---------------------------------------------------------------------------


def capture_captcha(session: wd.Session) -> bytes:
    """抓验证码图。canvas → PNG,不截屏、不裁剪、不引图像库。

    抓不到就抛。**绝不返回空字节**:空图交给识别模型,模型会给出一个
    看着挺合理的四位答案,然后以「验证码错误」的面目出现——人会去查识别
    接口,而问题在于压根没抓到图。
    """
    result = _ok(session.execute_async(em.CAPTURE_CAPTCHA_JS, f"#{em.CAPTCHA_IMG_ID}"), "抓验证码")
    try:
        image = em.decode_data_url(str(result.get("data_url") or ""))
    except ValueError as exc:
        raise LoginError(f"抓验证码:{exc}", stage="抓验证码") from exc
    logger.info("验证码已抓取:%s×%s,%d 字节", result.get("width"), result.get("height"), len(image))
    return image


def read_login_error(
    session: wd.Session, *, password_proven: bool = False
) -> tuple[bool, str]:
    """读登录页上的报错,判断还能不能重试。

    读不出来时返回"不能重试"——和 ``classify_login_error`` 的默认一致。

    ``password_proven`` 原样转给分类函数,用来分东财那句含混的
    「您输入的信息有误」。这里不做判断,只递。
    """
    try:
        result = session.execute_async(em.READ_LOGIN_ERROR_JS)
    except wd.WebDriverError as exc:
        return False, f"登录没成功,而且读不到页面上的提示({exc})。"
    messages = result.get("messages") if isinstance(result, Mapping) else None
    return em.classify_login_error(
        messages if isinstance(messages, list) else [], password_proven=password_proven
    )


def verify_logged_in(session: wd.Session) -> em.AccountReport:
    """查一次账户接口。**这是"登进去了"的唯一判据。**

    会话失效抛 ``em.SessionExpired``,其它问题抛 ``em.EastmoneyError``。
    """
    if em.looks_logged_out(session.current_url()):
        session.navigate(em.POSITION_URL)
    result = session.execute_async(em.QUERY_ASSET_JS, em.ASSET_ENDPOINT)
    if not isinstance(result, Mapping):
        raise em.EastmoneyError("账户接口脚本没返回对象。")
    if not result.get("ok"):
        detail = str(result.get("detail") or "账户接口失败,但没说原因。")
        # 页面跳回登录页 = 会话没了,这和"接口出错"不是一回事。
        if em.looks_logged_out(str(result.get("current_url") or "")):
            raise em.SessionExpired(f"账户接口调用时已在登录页:{detail}")
        raise em.EastmoneyError(detail)
    payload = result.get("response")
    if not isinstance(payload, Mapping):
        raise em.EastmoneyError("账户接口返回的不是对象。")
    return em.parse_asset_and_position(payload)      # 里面会分出 SessionExpired


def _已经跳走(session: wd.Session, 时机: str) -> bool:
    """还在不在登录页?不在就顺手确认一下是不是真登着了。

    ``looks_logged_out`` 只看地址,是个便宜的预检;真判据仍然是账户接口,
    所以这里跳走之后还要 ``verify_logged_in`` 一次才敢返回 True。
    地址已经离开登录页、账户接口却不通,那是另一回事,交给调用方按原路走。
    """
    if em.looks_logged_out(session.current_url()):
        return False
    try:
        verify_logged_in(session)
    except (em.SessionExpired, em.EastmoneyError):
        return False
    logger.info("%s东财已经跳出登录页,且账户接口通,不再走登录动作。", 时机)
    return True


def _认验证码(solver: captcha_mod.CaptchaSolver, image: bytes, 轮次: int) -> str:
    """认一次验证码。**第 N 次尝试从识别链的第 N 条路起认。**

    识别链本身只会在"这条路哑了"时降级,认错字是不降级的(它没法知道
    自己错了)。所以换路这件事只能由**知道上一次失败了**的这一层来推——
    也就是登录的重试循环。见 ``captcha.ChainSolver.solve_from``。

    用 ``getattr`` 探而不是 isinstance 判:只配了一条识别路时
    ``solver_from_settings`` 返回的是那个识别器本身,不是链(它特意不套壳)。
    那种情况下没有"下一条"可换,退回普通的 ``solve`` 就是对的。
    """
    换起点 = getattr(solver, "solve_from", None)
    if callable(换起点):
        return 换起点(image, start=max(0, 轮次 - 1))
    return solver.solve(image)


def _attempt(
    session: wd.Session, *, account: str, password: str, solver: captcha_mod.CaptchaSolver,
    password_proven: bool = False, 轮次: int = 1,
) -> None:
    """走一遍完整的登录动作。成功返回,失败抛 ``LoginError``。

    ``轮次`` 从 1 起,只用来决定这次从识别链的第几条路起认。

    ⚠️ ``account`` / ``password`` 只在这里作为脚本参数传出去,**不落任何日志**。
    """
    session.navigate(em.LOGIN_URL)

    # 已经有会话的浏览器**根本看不到登录页**:东财直接把它跳到 LOGIN_URL 里
    # returl 指的地方。那个页面上没有账号框、没有验证码框,于是下面每一步都会
    # 失败,报出来的却是「登录页上找不到 xx」——指向选择器,而真相是已经登进去了。
    if _已经跳走(session, "打开登录页时"):
        return

    filled = _ok(session.execute_async(em.FILL_LOGIN_JS, account, password), "填账号密码")
    # 脚本只回报"填没填上",不回显值。这两个布尔量是可以记的。
    if not filled.get("account_filled", False):
        raise LoginError("填账号密码:账号反复被页面清空,填不进去。", stage="填账号密码")

    image = capture_captcha(session)
    try:
        answer = _认验证码(solver, image, 轮次)
    except captcha_mod.CaptchaError as exc:
        # 「认不出来」是可以重试的:换一张图它可能就认出来了。
        raise LoginError(f"认验证码:{exc}", stage="认验证码", retryable=True) from exc

    # 跳转是异步的,可能正好落在填表和抓图这几秒里。再判一次,否则就是拿着
    # 一个认好的验证码去一个没有验证码框的页面上填。
    if _已经跳走(session, "填表期间"):
        return

    submitted = session.execute_async(em.SUBMIT_LOGIN_JS, answer)
    said = submitted if isinstance(submitted, Mapping) else {}
    complaint = "" if said.get("ok") else str(said.get("detail") or "提交登录脚本没说原因。")

    # ⚠️ **现在就把页面上的报错读下来,不能等到确认失败之后再回头读。**
    #
    # ``verify_logged_in`` 干的第一件事是「地址里有 Login 就导航到持仓页」。
    # 而登录失败时人就还留在登录页上,这个条件**必然成立**——那一跳把带着
    # ``#ertips`` 的页面刷掉,持仓页再把浏览器弹回一张重新加载过的干净登录页。
    # 等回头再读,读到的永远是空的,于是分类不出来,于是按"不可重试"一次就停。
    #
    # 2026-08-21 11:16 那次登录失败就是这么把原因弄丢的:验证码认对了、提交
    # 脚本报的是成功、账户接口说没登进去,而页面上"什么都没说"——**是我们
    # 自己在读之前把那一页刷掉了。诊断的步骤销毁了要诊断的证据。**
    #
    # 代价是每次登录(包括成功的那些)多一次只读的 JS 调用。提交脚本点完按钮
    # 已经等了 2.5 秒,报错这时候是渲染好的;登录成功时页面正在跳走,读不到
    # 也无所谓——``read_login_error`` 自己吞 WebDriverError,不会打断成功路径。
    现场 = read_login_error(session, password_proven=password_proven)

    # **提交脚本说失败,不等于没登上。** 跳转也可能落在脚本跑到一半:元素在它
    # 手里被卸载,它只能说"找不到",而登录其实已经成立。判据只有账户接口
    # ——这和 verify_logged_in 的文档说的是同一件事,这里只是照做。
    try:
        verify_logged_in(session)
    except em.SessionExpired as exc:
        if complaint:
            raise LoginError(f"提交登录:{complaint}", stage="提交登录") from exc
        retryable, detail = 现场
        raise LoginError(f"确认登录:{detail}", stage="确认登录", retryable=retryable) from exc


# ---------------------------------------------------------------------------
#  编排(碰)
# ---------------------------------------------------------------------------


def login(
    session: wd.Session,
    *,
    account: str,
    password: str,
    solver: captcha_mod.CaptchaSolver,
    max_attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    password_proven: bool = False,
) -> em.AccountReport:
    """在一个已有的浏览器会话上完成登录。

    **只有"验证码没认对"这一类会重试。** 其余一次就抛,包括判不出类别的。

    ``password_proven`` 见 ``em.AMBIGUOUS_LOGIN_HINTS``:东财只回一句
    「您输入的信息有误」,不说错的是哪一项。这套账号密码成功登过,才
    敢把那句话当成验证码没认对去换张图重来;没登过就一次都不多试。

    **每次重试换一条识别路**,不是拿同一条再撞一遍——见 ``MAX_ATTEMPTS``
    和 ``_认验证码``。
    """
    last: LoginError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            _attempt(session, account=account, password=password, solver=solver,
                     password_proven=password_proven, 轮次=attempt)
        except LoginError as exc:
            last = exc
            if not exc.retryable:
                logger.warning("登录失败且不可重试(第 %d 次):%s", attempt, exc)
                raise
            logger.info("登录第 %d 次没过(可重试):%s", attempt, exc)
            if attempt < max_attempts:
                sleep(RETRY_PAUSE)
            continue
        logger.info("登录成功(第 %d 次尝试)", attempt)
        return verify_logged_in(session)

    assert last is not None
    # **走到这里就不该再说"验证码没认对"了。** 该换的路都换过了,每一条
    # 都给了答案、每一条都没被券商认下,再把锅扣给识别是在编原因——
    # 到这一步真实的情况是「不知道为什么进不去」。``exhausted`` 让上层
    # 能把这句话原样端到界面上,而不是继续显示成一项"已知缺项"。
    raise LoginError(
        f"连试 {max_attempts} 次都没能登进去,每次换了一条识别路,"
        f"仍然全部被退回 —— 这已经不是验证码认不认得出的问题,**属于未知问题**,"
        f"需要人去看一眼。最后一次:{last}",
        stage=last.stage, retryable=False, exhausted=True,
    ) from last


def ensure_session(
    settings: Any,
    *,
    solver: captcha_mod.CaptchaSolver,
    session_id: str = "",
    sleep: Callable[[float], None] = time.sleep,
    password_proven: bool = False,
) -> tuple[wd.Session, LoginResult]:
    """拿一个**确认登录着**的浏览器会话。

    先试着接上 ``session_id`` 指的旧会话——登录一次要过验证码,重启服务
    就重登一遍是没必要的开销,而且每登一次都是一次被锁的机会。接不上
    (会话没了 / 已掉登录)才开新的重登。

    ``settings`` 接受 ``state.BrokerSettings``(只读 ``remote_url`` /
    ``account`` / ``password`` / ``missing()``,**故意不 import state**——
    登录这件事不该反过来依赖落盘层)。

    返回 ``(会话, 结果)``。**调用方负责关会话**,或者把 session_id 存下来
    留给下次接。
    """
    missing = tuple(getattr(settings, "missing", lambda: ())())
    if missing:
        # 一次报全部,不是撞上第一个就返回。缺三样却只说缺一样,
        # 人会配好一样再跑一次,再被告知还缺一样——三轮才配完。
        raise LoginError(
            "券商登录没配全,缺:" + "、".join(missing) + "(设置 → 运行 → 券商登录)。"
            "这不是登录失败,是这项配置还没填。",
            stage="读配置",
        )

    remote_url = str(settings.remote_url).strip()
    account = str(settings.account).strip()
    password = str(settings.password)

    if session_id:
        existing = wd.attach(remote_url, session_id)
        try:
            report = verify_logged_in(existing)
        except (em.SessionExpired, em.EastmoneyError, wd.WebDriverError) as exc:
            logger.info("旧会话不能用了(%s),重新登录。", type(exc).__name__)
            existing.closed = True          # 已经没了,别再发 DELETE
        else:
            logger.info("复用已有浏览器会话,未重新登录。")
            return existing, LoginResult(account=report, attempts=0, reused=True)

    session = wd.new_session(remote_url)
    try:
        report = login(session, account=account, password=password,
                       solver=solver, sleep=sleep, password_proven=password_proven)
    except BaseException:
        # 登录没成就把会话关掉。留着不管的话,每失败一次 Grid 上就多挂一个
        # 浏览器,而那台机器只有 3.6G 内存,二代还在上面跑着真钱。
        session.close()
        raise
    return session, LoginResult(account=report, attempts=1, reused=False)


__all__ = [
    "MAX_ATTEMPTS", "RETRY_PAUSE", "LoginError", "LoginResult",
    "capture_captcha", "read_login_error", "verify_logged_in",
    "login", "ensure_session",
]
