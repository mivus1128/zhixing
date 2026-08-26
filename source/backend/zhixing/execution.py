"""执行层 —— 系统里**唯一**能把单子发给券商的地方。

## 三条结构性约束

1. ``submit()`` 只接受 ``guards.ValidatedOrder``,而该类型只能由
   ``guards.validate()`` 完成类型规范化后产出。执行层没有第二条输入通路。
2. ``SIMULATION`` 与源码验证锁都会直接走 dry-run,连券商适配器都不会调用。
3. 每一单都要带 ``Authorization``,说明**这单是凭什么下的**。

## 关于第 3 条(替代二代的伪造确认)

二代 `main.py:516-518` 是这样下单的:

    execute_strategy(confirm=True, confirmation_text="确认执行")

代码替人签了字。出事后查不出是谁授权的、什么时候、按什么规则——因为
根本没有"授权"这个概念,只有一个写死的字符串。

三代把它换成显式的授权对象:无人值守模式下的单子标 ``UNATTENDED``,
人在界面上接管下的单子标 ``MANUAL``,两者都带时间戳和来源,一并进留痕。
**这不降低自动化程度**——无人值守开着的时候流程一样不停;
变的只是"这单是自动下的"这件事从此有据可查。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Protocol, Sequence

from . import SYSTEM_NAME, __version__, runmode
from .broker import BrokerError
from .guards import GuardReport, ValidatedOrder

logger = logging.getLogger("zhixing.execution")


class AuthorizationKind(str, Enum):
    """这单是凭什么下的。"""

    #: 无人值守模式自动下单。要求下单时 runmode 的开关确实是开着的。
    UNATTENDED = "unattended"
    #: 人在界面上手动接管下单。
    MANUAL = "manual"
    #: 演练 / 回放,永不触达券商。
    SIMULATION = "simulation"


@dataclass(frozen=True)
class Authorization:
    """一单的授权凭据。进留痕,事后复盘查的就是它。"""

    kind: AuthorizationKind
    #: 操作者标识。UNATTENDED 填 "scheduler",MANUAL 填操作者标识。
    #: **不要填账号**——这里是身份标记,不是券商凭证。
    actor: str
    #: 触发来源描述,例如 "scheduler:14:00" 或 "ui:takeover"
    source: str
    issued_at: datetime


class Outcome(str, Enum):
    SUBMITTED = "submitted"          # 已发给券商
    SUBMITTED_UNKNOWN = "submitted_unknown"  # 可能已发出,结果不明,绝不能重试
    DRY_RUN = "dry_run"              # SIMULATION 或验证锁生效,未发出
    REJECTED = "rejected"            # 授权或运行模式不允许
    FAILED = "failed"                # 发出过程中出错


@dataclass(frozen=True)
class ExecutionRecord:
    """一次下单尝试的完整留痕。归档和界面都读它。

    这个结构是"事后查得清"的全部依据,字段只增不减。
    """

    record_id: str
    system_name: str
    app_version: str
    instruction_code: str
    action: str
    market: str
    symbol: str
    name: str
    qty: int
    limit_price: Decimal
    notional: Decimal
    outcome: Outcome
    authorization: Authorization
    attempted_at: datetime
    #: 完成了哪些规范化步骤。当前仅有类型转换,不代表通过本地风控。
    guards_passed: tuple[str, ...] = ()
    #: 券商返回的委托编号(成功时)
    wtbh: str | None = None
    #: 失败或拒绝的原因,面向人
    message: str = ""
    #: True 表示券商可能已收到委托。它不是普通失败,绝不能据此自动重试。
    submitted_unknown: bool = False
    reason: str = ""
    risk_note: str = ""


def record_entry(record: ExecutionRecord) -> dict[str, Any]:
    """把执行记录变成可归档/下发的 JSON 结构。机密不在这个类型里。"""
    return {
        "record_id": record.record_id,
        "system_name": record.system_name,
        "app_version": record.app_version,
        "instruction_code": record.instruction_code,
        "action": record.action,
        "market": record.market,
        "symbol": record.symbol,
        "name": record.name,
        "qty": record.qty,
        "limit_price": str(record.limit_price),
        "notional": str(record.notional),
        "outcome": record.outcome.value,
        "submitted_unknown": record.submitted_unknown,
        "attempted_at": record.attempted_at.isoformat(),
        "wtbh": record.wtbh,
        "message": record.message,
        "reason": record.reason,
        "risk_note": record.risk_note,
        "规范化步骤": list(record.guards_passed),
        "授权": {
            "kind": record.authorization.kind.value,
            "actor": record.authorization.actor,
            "source": record.authorization.source,
            "issued_at": record.authorization.issued_at.isoformat(),
        },
    }


class BrokerAdapter(Protocol):
    """券商适配器接口。

    真实实现由登录/下单模块提供(Claude 独占范围)。这里只声明形状,
    使执行层可以脱离浏览器自动化被单独测试。
    """

    def place_order(self, order: ValidatedOrder) -> str:
        """下单,返回委托编号。失败抛异常。"""
        ...

    def cancel_order(self, wtbh: str) -> None:
        """撤单。失败抛异常。"""
        ...


#: 留痕落盘钩子。默认只写日志;归档层就绪后替换。
#: 和 runmode.audit_sink 一样保持可替换,让执行层不依赖存储实现。
record_sink: Callable[[ExecutionRecord], None] = lambda rec: logger.info(
    "执行留痕 %s %s %s %s qty=%s price=%s 结果=%s 授权=%s/%s",
    rec.record_id,
    rec.action,
    rec.market,
    rec.symbol,
    rec.qty,
    rec.limit_price,
    rec.outcome.value,
    rec.authorization.kind.value,
    rec.authorization.actor,
)


def _new_record_id() -> str:
    return uuid.uuid4().hex[:16]


def _record(
    order: ValidatedOrder,
    auth: Authorization,
    outcome: Outcome,
    *,
    now: datetime,
    wtbh: str | None = None,
    message: str = "",
    submitted_unknown: bool = False,
) -> ExecutionRecord:
    rec = ExecutionRecord(
        record_id=_new_record_id(),
        system_name=SYSTEM_NAME,
        app_version=__version__,
        instruction_code=order.instruction_code,
        action=order.action,
        market=order.market,
        symbol=order.symbol,
        name=order.name,
        qty=order.qty,
        limit_price=order.limit_price,
        notional=order.notional,
        outcome=outcome,
        authorization=auth,
        attempted_at=now,
        guards_passed=order.passed,
        wtbh=wtbh,
        message=message,
        submitted_unknown=submitted_unknown,
        reason=order.reason,
        risk_note=order.risk_note,
    )
    record_sink(rec)
    return rec


def _authorization_valid(auth: Authorization) -> str | None:
    """授权本身是否成立。返回拒绝原因,None 表示通过。"""
    if not auth.actor.strip():
        return "授权缺少操作者标识"
    if not auth.source.strip():
        return "授权缺少触发来源"
    if auth.kind is AuthorizationKind.UNATTENDED:
        # 关键:声称自己是无人值守下的单,那开关就必须真的开着。
        # 这一条防的是"关掉开关后仍有残留任务继续下单"。
        if not runmode.unattended_state().enabled:
            return "声明为无人值守下单,但无人值守开关当前是关闭状态"
    return None


def submit(
    order: ValidatedOrder,
    auth: Authorization,
    *,
    broker: BrokerAdapter | None = None,
    now: datetime | None = None,
) -> ExecutionRecord:
    """下一单。全系统唯一的下单入口。

    :param order: 必须是 ``guards.validate()`` 的产物,无法手工伪造。
    :param auth: 授权凭据,说明这单凭什么下。
    :param broker: 券商适配器。验证锁生效时不会被调用,可以传 None。
    """
    stamp = now or datetime.now()

    denial = _authorization_valid(auth)
    if denial:
        logger.error("拒绝下单:%s(指令 %s)", denial, order.instruction_code)
        return _record(order, auth, Outcome.REJECTED, now=stamp, message=denial)

    # SIMULATION 是调用级硬隔离。它不能依赖 VERIFICATION_LOCK 恰好为 True:
    # 正式运行把总闸打开后,演练仍必须永远不触达券商。
    if auth.kind is AuthorizationKind.SIMULATION:
        return _record(
            order,
            auth,
            Outcome.DRY_RUN,
            now=stamp,
            message="模拟发送,委托未发给券商",
        )

    # 验证锁。放在授权检查之后,是为了让"授权有问题"这件事在
    # dry-run 阶段也能暴露出来,而不是被 dry-run 掩盖过去。
    if not runmode.live_trading_allowed():
        return _record(
            order,
            auth,
            Outcome.DRY_RUN,
            now=stamp,
            message="验证锁生效,单子未发出(这是预期行为,不是故障)",
        )

    # 到这里说明验证锁已在源码里解除。再断言一次,防止上面的判断被改坏。
    runmode.assert_live_trading_allowed(
        what=f"{order.action} {order.market}{order.symbol} x{order.qty}"
    )

    if broker is None:
        return _record(
            order, auth, Outcome.FAILED, now=stamp, message="未提供券商适配器"
        )

    try:
        if order.action == "cancel":
            if not order.wtbh:
                return _record(
                    order, auth, Outcome.FAILED, now=stamp, message="撤单缺少委托编号"
                )
            broker.cancel_order(order.wtbh)
            return _record(order, auth, Outcome.SUBMITTED, now=stamp, wtbh=order.wtbh)

        wtbh = broker.place_order(order)
        return _record(order, auth, Outcome.SUBMITTED, now=stamp, wtbh=wtbh)
    except BrokerError as exc:
        if exc.submitted_unknown:
            # 原异常可能含浏览器内部路径或会话信息,不写进日志/归档。
            logger.error(
                "委托结果不明:指令 %s,异常类型=%s,禁止重试",
                order.instruction_code,
                exc.__class__.__name__,
            )
            what = "撤单请求" if order.action == "cancel" else "委托"
            return _record(
                order,
                auth,
                Outcome.SUBMITTED_UNKNOWN,
                now=stamp,
                message=f"券商提交过程异常,{what}可能已经发出；禁止自动重试,请查委托列表",
                submitted_unknown=True,
            )
        logger.error("券商拒绝或提交失败:指令 %s", order.instruction_code)
        return _record(
            order, auth, Outcome.FAILED, now=stamp, message=str(exc)
        )
    except Exception as exc:                      # noqa: BLE001 —— 留痕优先
        logger.error(
            "下单出现未分类异常:指令 %s,异常类型=%s",
            order.instruction_code,
            exc.__class__.__name__,
        )
        return _record(
            order,
            auth,
            Outcome.FAILED,
            now=stamp,
            message=f"券商调用出现未分类异常:{exc.__class__.__name__}",
        )


@dataclass(frozen=True)
class BatchResult:
    """一轮执行的汇总。被拦下的和下出去的都在里面。"""

    records: tuple[ExecutionRecord, ...] = ()
    blocked: tuple[GuardReport, ...] = ()

    @property
    def submitted_count(self) -> int:
        return sum(1 for r in self.records if r.outcome is Outcome.SUBMITTED)

    @property
    def submitted_unknown_count(self) -> int:
        return sum(1 for r in self.records if r.submitted_unknown)


def submit_reports(
    reports: Sequence[GuardReport],
    auth: Authorization,
    *,
    broker: BrokerAdapter | None = None,
    now: datetime | None = None,
) -> BatchResult:
    """把一批规范化报告执行掉:可构造的下单,转换失败的原样留痕。

    **被拦下的指令也要进留痕。** 二代的问题之一是拦截行为不可见——
    界面上看不出"模型出了单但被拦了",只看得出"没下单",两者天差地别。
    """
    records: list[ExecutionRecord] = []
    blocked: list[GuardReport] = []

    for report in reports:
        if report.ok and report.order is not None:
            records.append(submit(report.order, auth, broker=broker, now=now))
        else:
            blocked.append(report)
            logger.warning(
                "指令 %s 无法完成类型规范化,未执行:%s",
                report.proposed.instruction_code,
                [f.code for f in report.failures],
            )

    return BatchResult(records=tuple(records), blocked=tuple(blocked))
