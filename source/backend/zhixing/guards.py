"""指令规范化 —— ``ValidatedOrder`` 的唯一构造入口。

本项目已按使用者要求拆除全部本地下单风控。这里不再判断交易时段、交易日、
市场/代码、标的类型、重复指令、持仓、资金、整手、价格步长、涨跌停、金额或
价格偏离。券商是否接受一笔委托,以券商实际返回为准。

仍保留这一条结构性通路:

    模型产出 ──▶ ProposedOrder ──▶ validate() ──▶ ValidatedOrder ──▶ 下单

``qty`` 和 ``limit_price`` 必须先转成券商调用需要的 ``int`` / ``Decimal``。
这不是风控判断,只是类型规范化;无法转换时没有可提交的参数,因此会返回失败报告。
``ValidatedOrder`` 仍只能由本模块构造,执行层没有第二条旁路。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Sequence

from . import tradingdays

logger = logging.getLogger("zhixing.guards")


def default_is_trading_day(day: date) -> bool:
    """默认交易日历。**全系统唯一的一份**——由调度器使用。

    实现在 ``tradingdays.py``:节假日按年录,查不到的年份**当场报错**而不是
    退回工作日判断。函数保留在这里是为了兼容调度器原有导入路径;指令规范化
    已不再调用它,也不会据此阻断委托。

    ⚠️ 会抛 ``tradingdays.CalendarError``:意思是"这天开不开市我不知道",
    **不是"这天不开市"**。别用 ``except Exception`` 把它压成 False。

    """
    return tradingdays.is_trading_day(day)


# ---------------------------------------------------------------------------
#  输入 / 输出结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposedOrder:
    """模型产出的一条待执行指令,**未经校验**。

    ``qty`` 和 ``limit_price`` 保持原始类型(可能是字符串、可能是 None、
    可能是 "1,000" 这种脏值)。解析在校验里做,解析失败就是一条失败原因——
    二代在这里直接放行(缺陷 4)。
    """

    instruction_code: str
    action: str                     # buy / sell / cancel
    market: str                     # SH / SZ
    symbol: str
    name: str
    qty: object = None
    limit_price: object = None
    wtbh: str | None = None
    reason: str = ""
    risk_note: str = ""


@dataclass(frozen=True)
class ObjectSnapshot:
    """校验一条指令所需的标的现状。取自最新切片。"""

    symbol: str
    last_price: Decimal
    prev_close: Decimal | None = None
    available_qty: int = 0          # 可用(可卖)数量
    holding_qty: int = 0            # 持仓总量
    is_etf: bool = False
    is_st: bool = False
    is_tradable: bool = True        # False = 行情对象,只作宏观背景,不可下单
    quote_is_today: bool = True     # 最新切片是否当日行情
    #: 一手多少股。**跟着标的走,不是全局常量**——由 catalog.to_snapshot() 填。
    #: 二代把它写死在代码里,清单里明明存着这个字段却没人读。
    lot_size: int = 100


@dataclass(frozen=True)
class AccountSnapshot:
    """校验所需的账户现状。不含账号等身份信息——校验用不着。"""

    available_cash: Decimal


@dataclass(frozen=True)
class ValidationContext:
    """一次规范化的上下文。

    账户与行情快照保留在结构里,供现有采集链和归档调用保持兼容;规范化过程
    不读取它们,也不据此拦截指令。
    """

    account: AccountSnapshot | None
    objects: dict[str, ObjectSnapshot]
    now: datetime


@dataclass(frozen=True)
class GuardFailure:
    """一条校验未通过的记录。``code`` 供程序判断,``message`` 供人阅读。"""

    code: str
    message: str


# 令牌:只有本模块持有。ValidatedOrder 构造时核对它。
_GUARD_TOKEN = object()


@dataclass(frozen=True)
class ValidatedOrder:
    """完成类型规范化的指令。**只能由本模块的 validate() 产出。**

    ``execution.submit()`` 只接受这个类型。手工构造会在 __post_init__ 抛异常,
    所以执行层始终只有一条输入通路。
    """

    instruction_code: str
    action: str
    market: str
    symbol: str
    name: str
    qty: int
    limit_price: Decimal
    notional: Decimal               # 预估金额 = qty * limit_price
    wtbh: str | None
    reason: str
    risk_note: str
    validated_at: datetime
    passed: tuple[str, ...]         # 通过了哪些校验,进留痕
    _token: object = field(repr=False, default=None)

    def __post_init__(self) -> None:
        if self._token is not _GUARD_TOKEN:
            raise RuntimeError(
                "ValidatedOrder 只能由 guards.validate() 构造。"
                "直接构造意味着绕过了指令规范化——这是 bug,不是用法问题。"
            )


@dataclass(frozen=True)
class GuardReport:
    """一条指令的校验结论。``order`` 非空即通过。"""

    proposed: ProposedOrder
    order: ValidatedOrder | None
    failures: tuple[GuardFailure, ...]

    @property
    def ok(self) -> bool:
        return self.order is not None


# ---------------------------------------------------------------------------
#  各项校验
# ---------------------------------------------------------------------------


def _to_int(value: object) -> int | None:
    """宽松解析数量。脏值返回 None,由调用方转成失败原因。"""
    if isinstance(value, bool):          # bool 是 int 的子类,单独挡掉
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("股", "").replace("份", "")
        if not text:
            return None
        try:
            dec = Decimal(text)
        except InvalidOperation:
            return None
        return int(dec) if dec == dec.to_integral_value() else None
    return None


def _to_decimal(value: object) -> Decimal | None:
    """宽松解析价格。脏值返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        parsed = Decimal(value)
        return parsed if parsed.is_finite() else None
    if isinstance(value, float):
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("元", "")
        if not text:
            return None
        try:
            parsed = Decimal(text)
            return parsed if parsed.is_finite() else None
        except InvalidOperation:
            return None
    return None


# ---------------------------------------------------------------------------
#  入口
# ---------------------------------------------------------------------------


def validate(proposed: ProposedOrder, ctx: ValidationContext) -> GuardReport:
    """规范化一条指令。这是产出 ``ValidatedOrder`` 的**唯一途径**。

    所有风控规则都已移除。买卖指令只做数量和限价的类型转换,并一次报告
    全部无法转换的字段;撤单不需要数量和价格。
    """
    failures: list[GuardFailure] = []

    qty = 0
    price = Decimal("0")
    notional = Decimal("0")

    if proposed.action in {"buy", "sell"}:
        parsed_qty = _to_int(proposed.qty)
        parsed_price = _to_decimal(proposed.limit_price)
        if parsed_qty is None:
            failures.append(
                GuardFailure("QTY_UNPARSABLE", f"数量无法转换为整数:{proposed.qty!r}")
            )
        else:
            qty = parsed_qty
        if parsed_price is None:
            failures.append(
                GuardFailure("PRICE_UNPARSABLE", f"限价无法转换为数字:{proposed.limit_price!r}")
            )
        else:
            price = parsed_price
        if not failures:
            notional = (qty * price).quantize(Decimal("0.01"))

    if failures:
        logger.warning(
            "指令 %s 无法完成类型规范化,共 %d 条:%s",
            proposed.instruction_code,
            len(failures),
            [f.code for f in failures],
        )
        return GuardReport(proposed, None, tuple(failures))

    order = ValidatedOrder(
        instruction_code=proposed.instruction_code,
        action=proposed.action,
        market=proposed.market,
        symbol=proposed.symbol,
        name=proposed.name,
        qty=qty or 0,
        limit_price=price or Decimal("0"),
        notional=notional,
        wtbh=proposed.wtbh,
        reason=proposed.reason,
        risk_note=proposed.risk_note,
        validated_at=ctx.now,
        passed=("type_normalization",),
        _token=_GUARD_TOKEN,
    )
    return GuardReport(proposed, order, ())


def validate_all(
    proposals: Iterable[ProposedOrder], ctx: ValidationContext
) -> Sequence[GuardReport]:
    """批量规范化。**一条转换失败不影响其他条**,各自独立成报告。"""
    return [validate(p, ctx) for p in proposals]
