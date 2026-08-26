"""运行模式 —— 全系统唯一决定"能不能下真单"的地方。

## 为什么单独一个模块

二代把两件完全不同的事混进了 `auto_execute` 一个布尔里:

1. 这套系统**允不允许**碰真钱
2. 下单**要不要等人**

混在一起的后果是 `main.py:505-518`——自动路径直接写
`confirm=True, confirmation_text="确认执行"` 替人签字,痕迹都不留。
出了事查不出是谁、什么时候、按什么授权下的单。

三代拆成两个正交的开关:

| 开关 | 管什么 | 能不能运行时改 |
|---|---|---|
| `VERIFICATION_LOCK` | 能不能下真单 | **不能**。改它必须改这份源码并重新 build |
| `UnattendedMode` | 下单要不要等人 | 能,但每次变更强制留痕 |

## 为什么验证锁不读环境变量

三代进入正式运行后仍保留这道源码总闸。正常运行时它为 ``False``;
需要紧急停止真实委托时改回 ``True`` 并重建镜像。

环境变量会被误配、会被 compose 文件覆盖、会在迁移时丢失。
写在源码里,改动会进 git diff、会被审、会留在版本历史里。
这是刻意用不方便换确定性。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Final

logger = logging.getLogger("zhixing.runmode")


# ---------------------------------------------------------------------------
#  验证锁 —— 源码级,运行时不可更改
# ---------------------------------------------------------------------------

#: 正式运行总闸。False 表示允许执行层触达券商;改回 True 即全局 dry-run。
#:
#: 这不是无人值守开关。无人值守仍默认关闭,并且只能经带原因的接口开启。
#: 两道开关分开,才能在保留判断产出的同时立即停止自动发单。
VERIFICATION_LOCK: Final[bool] = False


class LiveTradingForbidden(RuntimeError):
    """试图在验证锁生效时下真单。

    这个异常不该被 ``except Exception`` 吞掉。它意味着有代码路径绕过了
    设计约束,是 bug,不是可恢复的运行时错误。
    """


def live_trading_allowed() -> bool:
    """能否下真单。只读判断,不抛异常。"""
    return not VERIFICATION_LOCK


def assert_live_trading_allowed(*, what: str) -> None:
    """下真单前的最后一道断言。

    execution.py 是唯一该调用它的地方。其他模块调用它说明职责划错了。

    :param what: 被拦下的动作描述,进日志用。不要往里塞账号、金额等敏感值。
    """
    if VERIFICATION_LOCK:
        logger.error("验证锁拦截真实下单:%s", what)
        raise LiveTradingForbidden(
            f"三代当前处于只读验证模式,不执行任何真实下单({what})。"
            "解除需修改 runmode.VERIFICATION_LOCK 源码并重新构建。"
        )


# ---------------------------------------------------------------------------
#  无人值守模式 —— 运行时可改,但强制留痕
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnattendedState:
    """无人值守的当前状态。

    ``enabled=True`` 时,调度器出的指令直接进执行流程,不等人。
    ``enabled=False`` 时,指令进待处理队列,等人在界面上接管。

    注意:**这个开关不决定能不能下真单**,那是 ``VERIFICATION_LOCK`` 的事。
    验证锁生效时,无人值守开着也只会走 dry-run 路径。
    """

    enabled: bool
    #: 谁改的。人工操作填操作者标识,系统默认填 "system"。
    changed_by: str
    #: 什么时候改的
    changed_at: datetime
    #: 为什么改。强制填写——事后复盘时"当时为什么关掉了"是最常问的问题。
    reason: str


@dataclass(frozen=True)
class UnattendedChange:
    """一次开关变更的审计记录。"""

    before: bool
    after: bool
    changed_by: str
    changed_at: datetime
    reason: str


#: 审计落盘钩子。默认只写日志;接上归档层后由 archive.py 替换成真正的落盘。
#: 保持可替换是为了让 runmode 不依赖存储层——它必须是能被单独测试的。
audit_sink: Callable[[UnattendedChange], None] = lambda change: logger.info(
    "无人值守开关变更 %s -> %s,操作者=%s,原因=%s",
    change.before,
    change.after,
    change.changed_by,
    change.reason,
)


_lock = threading.Lock()

#: 默认**关闭**。
#:
#: 二代默认是 ``auto_execute: True``——装好即全自动。三代反过来:
#: 默认不自动执行,要开必须显式开、写明原因、留下记录。
#: 自动程度不打折(开关一开行为与二代一致),但"开着"这件事本身要有据可查。
_state = UnattendedState(
    enabled=False,
    changed_by="system",
    changed_at=datetime.min,
    reason="初始状态:默认关闭,需显式开启",
)


def unattended_state() -> UnattendedState:
    """读当前状态。返回的是不可变快照,拿去随便用。"""
    with _lock:
        return _state


def restore_unattended(state: UnattendedState) -> None:
    """从共享运行目录恢复开关状态,不产生一条新的人工变更审计。

    API 和轮次驱动是两个进程,内存变量不会跨进程同步。状态由 ``state.Store``
    原子落盘后,两个进程各自在处理请求/轮次前调用本函数恢复同一份事实。
    """
    global _state
    with _lock:
        _state = state


def set_unattended(
    enabled: bool,
    *,
    changed_by: str,
    reason: str,
    now: datetime | None = None,
) -> UnattendedChange:
    """改无人值守开关。三个参数一个都不能省。

    ``changed_by`` 和 ``reason`` 是必填关键字参数,不是可选装饰——
    二代最大的问题就是自动下单没有授权链路,事后无从追溯。

    幂等:状态没变时也照样记一条审计,因为"有人试图开但它已经开着"
    本身就是有用的信息。
    """
    if not reason or not reason.strip():
        raise ValueError("变更无人值守开关必须写明原因")
    if not changed_by or not changed_by.strip():
        raise ValueError("变更无人值守开关必须标明操作者")

    stamp = now or datetime.now()

    global _state
    with _lock:
        before = _state.enabled
        _state = replace(
            _state,
            enabled=enabled,
            changed_by=changed_by,
            changed_at=stamp,
            reason=reason,
        )

    change = UnattendedChange(
        before=before,
        after=enabled,
        changed_by=changed_by,
        changed_at=stamp,
        reason=reason,
    )
    audit_sink(change)
    return change


def describe() -> dict[str, object]:
    """给 ``/api/status`` 用的摘要。不含任何敏感值。"""
    state = unattended_state()
    return {
        "运行模式": "dry_run" if VERIFICATION_LOCK else "live",
        "验证锁": VERIFICATION_LOCK,
        "无人值守": state.enabled,
        "开关变更时间": state.changed_at.isoformat() if state.changed_at != datetime.min else None,
        "开关变更人": state.changed_by,
        "开关变更原因": state.reason,
    }
