"""交易日历 —— 哪天开市,哪天不开。

## 为什么这件事值一个模块

``guards.default_is_trading_day`` 原来只判 ``weekday() < 5``,它在春节和国庆
连休期间会认为是交易日。后果是调度层在休市日照常起轮次,采集拿不到当日
行情,一轮白跑并留下一条失败记录。下单风控已按使用者要求拆除,本模块现在
只决定自动轮次是否启动,不参与单笔委托阻断。

## 覆盖不到的年份**直接报错**,不退回工作日判断

这是本模块最要紧的一条。假期数据是一年一发的(国务院办公厅年底发次年的),
所以任何静态日历都必然有"到期"的那天。到期之后有两种做法:

1. 悄悄退回 ``weekday() < 5``——系统继续跑,春节那周照常起轮次,而且**不报错**;
2. 明确地失败。

选 2。理由和 ``runner.MissingDataSource`` 一样:**"没有这项能力"和"这项能力
说没问题"必须分得开**。第 1 种做法的坏处不是它会错,是它错了没人知道——
等到发现的时候,已经按错的日历跑了一整个假期。

``assert_covers()`` 让缺年份在**启动时**就暴露,而不是等到某天早上九点半。

## A 股不认调休

国务院的放假安排里常有"某周六上班"(调休补班)。**证券交易所不跟这个走**:
调休补班的周六周日照样休市。所以本模块的判断是

    交易日 = 周一到周五 且 不在休市日集合里

调休日**故意不参与运算**。它们仍然记在 ``makeup_workdays`` 里,不是因为要用,
而是为了让下一个读这段代码的人知道"这里不是忘了处理,是刻意不处理"——
把已经想过的事写下来,否则半年后有人会好心地把它加回去。

## 数据来源要可复查

每一年是一条 ``YearCalendar``,带 ``verified`` 标记。``verified=False`` 表示
日期是照公开安排录的、**还没有人对着权威来源逐条核过**。这种状态下日历照常
可用;``assert_ready_for_live()`` 保留给需要查验来源状态的运维与自检调用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Final

logger = logging.getLogger("zhixing.tradingdays")


class CalendarError(RuntimeError):
    """问了一个日历答不上来的日期。

    **不要用 ``except Exception`` 吞掉它。** 它的意思是"这天开不开市我不知道",
    而不是"这天不开市"。把它当成后者,就是本模块开头说的第 1 种做法。
    """


@dataclass(frozen=True)
class YearCalendar:
    """一年的休市安排。

    :param holidays: 周一到周五当中**不开市**的日子。周末不必列进来,
        它们本来就不开市;列进来只会让"这份数据对不对"更难看出来。
    :param makeup_workdays: 调休补班的周末。**本模块不使用它**,见模块开头。
    :param verified: 有没有人对着权威来源逐条核过。
    :param source: 数据出处,写清楚到能复查的程度。
    """

    year: int
    holidays: frozenset[date]
    makeup_workdays: frozenset[date] = frozenset()
    verified: bool = False
    source: str = ""

    def __post_init__(self) -> None:
        for day in self.holidays:
            if day.year != self.year:
                raise CalendarError(f"{self.year} 年的日历里混进了 {day}")
            if day.weekday() >= 5:
                # 周末列进休市日不会算错,但它是个信号:多半是照着放假安排
                # 整段抄的,没有区分"法定假期"和"交易所休市"。
                raise CalendarError(
                    f"{day} 是周末,不必列进休市日。"
                    f"只列周一到周五当中不开市的日子,便于核对。"
                )
        for day in self.makeup_workdays:
            if day.year != self.year:
                raise CalendarError(f"{self.year} 年的调休表里混进了 {day}")
            if day.weekday() < 5:
                raise CalendarError(f"{day} 不是周末,不该出现在调休补班表里")


# ---------------------------------------------------------------------------
#  数据
# ---------------------------------------------------------------------------

#: 2026 年逐日对照国务院办公厅 2025-11-04 发布的官方通知。这里只列
#: 周一到周五的放假日;通知里的周末补班不作为交易日,见模块开头。
_2026 = YearCalendar(
    year=2026,
    holidays=frozenset({
        # 元旦(1/1 周四、1/2 周五)
        date(2026, 1, 1), date(2026, 1, 2),
        # 春节(2/15 至 2/23;周末不列)
        date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
        date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 23),
        # 清明(4/6 周一)
        date(2026, 4, 6),
        # 劳动节(5/1 周五、5/4 周一、5/5 周二)
        date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5),
        # 端午(6/19 周五)
        date(2026, 6, 19),
        # 中秋(9/25 周五)
        date(2026, 9, 25),
        # 国庆(10/1 周四 起,10/3、10/4 为周末不列)
        date(2026, 10, 1), date(2026, 10, 2),
        date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7),
    }),
    verified=True,
    source=(
        "国务院办公厅《关于2026年部分节假日安排的通知》,2025-11-04,"
        "https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm"
    ),
)


_CALENDARS: dict[int, YearCalendar] = {c.year: c for c in (_2026,)}


def register(calendar: YearCalendar) -> None:
    """加一年,或替换已有的一年。

    留这个口子是为了自检能塞进构造好的年份,以及以后从配置加载。
    **生产数据仍然写在本模块里**——日历是代码的一部分,改它应该走
    提交和 review,不该是运行时能改的东西。
    """
    _CALENDARS[calendar.year] = calendar


def coverage() -> tuple[int, ...]:
    """覆盖了哪些年份,升序。"""
    return tuple(sorted(_CALENDARS))


def covers(year: int) -> bool:
    return year in _CALENDARS


def calendar_for(year: int) -> YearCalendar:
    cal = _CALENDARS.get(year)
    if cal is None:
        raise CalendarError(
            f"没有 {year} 年的交易日历(已有:{', '.join(map(str, coverage())) or '无'})。"
            f"假期安排一年一发,请在 tradingdays.py 里补上这一年再运行。"
        )
    return cal


def is_trading_day(day: date) -> bool:
    """这天开不开市。**答不上来就抛 CalendarError,不猜。**

    用作 ``scheduler`` 的日历钩子,由 ``guards.default_is_trading_day`` 转接。
    """
    cal = calendar_for(day.year)
    if day.weekday() >= 5:
        return False                      # 调休补班也不开市,见模块开头
    return day not in cal.holidays


#: 距年末多少天以内,次年日历从"提醒"升级成"硬要求"。
#:
#: 沪深两市的假期安排由国务院办公厅在**前一年 11 月前后**发布,交易所随后
#: 跟发。取 45 天(大致 11 月中起)是让硬要求出现在日历**已经能拿到**之后。
NEXT_YEAR_REQUIRED_WITHIN: Final[int] = 45


def required_years(today: date) -> tuple[int, ...]:
    """跑轮次**必须**具备哪些年份的日历。次年只在年底附近才算必须。

    ## 这个函数是补出来的,补的是一次真实事故

    2026-08-20 上线时,轮次驱动起不来,退出码 2,反复重启,理由是
    "缺少 2027 年的交易日历"。当时的启动门槛写的是
    ``assert_covers(today.year, today.year + 1)``。

    那行代码的注释说得很对——"日历不全的时候轮次跑出来的东西不能用,
    所以直接不启动,而不是空转一整年"。**但它挡错了年份。**

    次年日历要到当年 11 月才发布。八月份的机器不可能有 2027 年的日历,
    这个世界上任何一台机器都没有。于是那道门槛的实际效果不是"不空转一整年",
    正好是**一整年起不来**——而它想防的风险(跨年那几天把交易日算错)
    只在年末存在。

    所以规矩改成:**当年的日历是硬要求**(没有它连今天开不开市都答不上来);
    次年的在年底 45 天内才是硬要求,平时缺了只提醒。

    教训不是"门槛设松一点",是**fail-closed 的条件必须是调用方有办法满足的**。
    一个满足不了的前置条件,拦下的是系统本身,不是风险。
    """
    years = [today.year]
    if (date(today.year, 12, 31) - today).days <= NEXT_YEAR_REQUIRED_WITHIN:
        years.append(today.year + 1)
    return tuple(years)


def assert_covers(*years: int) -> None:
    """启动时喊一嗓子:这些年份的日历在不在。

    调用方应该用 ``required_years(date.today())`` 算出年份再传进来,
    **不要自己写 ``(year, year + 1)``**——那正是 2026-08-20 那次事故的写法。
    """
    missing = [y for y in years if not covers(y)]
    if missing:
        raise CalendarError(
            f"缺少 {', '.join(map(str, missing))} 年的交易日历。"
            f"请在 tradingdays.py 里补上。"
        )


def assert_ready_for_live(*years: int) -> None:
    """解除验证锁之前必须过的一关:这些年份的日历**已经有人核过**。

    和 ``runmode.assert_live_trading_allowed`` 是两把不同的锁,故意分开:
    前者管"日历对不对",后者管"准不准下单"。合成一把的话,补完日历就等于
    自动获得了下单授权,那不是想要的。
    """
    assert_covers(*years)
    unverified = [y for y in years if not _CALENDARS[y].verified]
    if unverified:
        raise CalendarError(
            f"{', '.join(map(str, unverified))} 年的交易日历尚未核实"
            f"(verified=False),不得用于真实下单。"
            f"请对照交易所休市公告逐条核对后再翻这个标记。"
        )


__all__ = [
    "CalendarError", "YearCalendar",
    "register", "coverage", "covers", "calendar_for",
    "is_trading_day", "required_years", "assert_covers", "assert_ready_for_live",
    "NEXT_YEAR_REQUIRED_WITHIN",
]
