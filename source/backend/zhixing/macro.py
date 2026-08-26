"""宏观行情对象 —— 美元指数、离岸人民币、WTI原油、COMEX黄金、富时A50。

## 为什么单开一个模块,不塞进 `quotes.py`

`quotes.py` 采的是**沪深两市的证券**:有市场代码(SH/SZ)、有整手、有涨跌停、
有持仓,采回来的东西要进 `guards` 当校验依据。这里采的东西一样都没有 ——
它们不可下单,存在的意义只有一个:**给模型一个大盘之外的参照系**。

把两者揉在一起会出一件具体的坏事:`quotes.fetch` 的返回值会流进
`collect.snapshots()`,而那张表是校验层查价用的。美元指数一旦进了那张表,
就意味着某条委托可以拿 98.76 当"最新价"去过涨跌停校验。所以**结构上就
不让它们同源**,而不是靠调用方记得别传。

## 这份数据补的是什么洞

三代原来的 7 个标的里有 **纳指ETF、黄金ETF、标普油气ETF嘉实** —— 它们的
价格分别由美股、COMEX黄金、WTI原油驱动。在这个模块之前,模型判断这三个
标的时手上只有它们自己的 K 线,**驱动因素一个都看不见**。

二代是有这些数据的(`collectors.fetch_market_metrics`),三代把这块漏了。
这里是补回来,不是新发明:符号表、字段位置、Yahoo→新浪的降级顺序,
都照二代那份在生产里跑了几个月的实现。

## 为什么主源是新浪,不是 Yahoo

二代的顺序是 Yahoo 优先、新浪兜底。三代反过来,理由是实测:

- 新浪五个符号、两套历史接口,**十个请求全通**(2026-08-20 21:00 实测)
- Yahoo 对国内出口 IP 会 403 / 429,二代代码里专门为此写了退避重试 ——
  也就是说"主源"其实经常是在走兜底那条路

**没有把 Yahoo 留成第二源**,因为留了就得维护两套字段映射,而其中一套
平时根本不执行 —— 不执行的代码坏了没人知道,等真需要它的那天才发现。
新浪挂了就明说挂了,让人去看,比悄悄降级到一条没验过的路上强。

## ⚠️ 境外品种的"当日"和 A 股不是一个意思

WTI、COMEX黄金、富时A50 近乎全天候交易,**数据源的交易日可能比北京
日历领先一天**(实测 A50 在 8-20 晚间返回的日期就是 8-21)。

所以这里**不写「非当日行情」了事** —— 那句话在 A 股语境里的意思是"数据
是陈的,别信",而这里恰恰相反,领先一天说明数据是最新的。三种情形分开写,
见 `_market_state()`。混成一句会让模型把最新的数据当成过期数据。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Sequence

from . import indicators as ind
from .quotes import QuoteError, _fetch

logger = logging.getLogger("zhixing.macro")

DEFAULT_TIMEOUT = 18.0

#: 历史日线保留多少根。20 日波动率要 21 个收盘价,留 80 根是为了留余量,
#: 同时不至于让上下文变大 —— 进模型的只有算好的几个数,不是这 80 根本身。
HISTORY_BARS = 80

SINA_REFERER = "https://finance.sina.com.cn/"

REALTIME_URL = "https://hq.sinajs.cn/list="
FUTURES_HISTORY_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/json.php/"
    "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol="
)
FOREX_HISTORY_URL = (
    "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20"
    "{s}=/NewForexService.getDayKLine?symbol={s}"
)


class MacroError(RuntimeError):
    """宏观对象没采到。和 `quotes.QuoteError` 平行,故意不共用一个类型 ——
    调用方对这两种失败的处置不同:行情对象采不到只是少一份背景,
    交易标的采不到那个标的整个不问模型。"""


# ---------------------------------------------------------------------------
#  符号表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MacroSpec:
    """一个宏观对象的采集参数。

    `realtime_layout` / `history_kind` 决定用哪套字段位置和哪个历史接口 ——
    新浪这两类接口的返回格式完全不同,**不能靠符号前缀猜**:`DINIW`
    没有 `hf_` 前缀却和 `fx_susdcnh` 同属外汇那一套。
    """

    object_id: str
    name: str
    short: str                    # 进标的清单的 symbol 位,和 A 股六位代码不会撞
    category: str                 # 外汇 / 大宗商品 / 指数 / 股指期货
    unit: str
    realtime_symbol: str
    realtime_layout: str          # futures / forex
    history_kind: str             # futures / forex
    history_symbol: str


#: 五个宏观对象。**符号和字段位置抄自二代**(`collectors.SINA_REALTIME_SYMBOLS`
#: 及其下面那三个 `fetch_sina_realtime` 分支),不是重新试出来的。
MACRO_SPECS: dict[str, MacroSpec] = {
    "FX_USDCNH": MacroSpec(
        object_id="FX_USDCNH", name="离岸人民币", short="USDCNH",
        category="外汇", unit="CNH",
        realtime_symbol="fx_susdcnh", realtime_layout="forex",
        history_kind="forex", history_symbol="fx_susdcnh",
    ),
    "INDEX_DXY": MacroSpec(
        object_id="INDEX_DXY", name="美元指数", short="DXY",
        category="指数", unit="点",
        realtime_symbol="DINIW", realtime_layout="forex",
        history_kind="forex", history_symbol="DINIW",
    ),
    "COMMODITY_WTI": MacroSpec(
        object_id="COMMODITY_WTI", name="WTI原油", short="WTI",
        category="大宗商品", unit="USD",
        realtime_symbol="hf_CL", realtime_layout="futures",
        history_kind="futures", history_symbol="CL",
    ),
    "COMMODITY_GOLD": MacroSpec(
        object_id="COMMODITY_GOLD", name="COMEX黄金", short="GOLD",
        category="大宗商品", unit="USD",
        realtime_symbol="hf_GC", realtime_layout="futures",
        history_kind="futures", history_symbol="GC",
    ),
    "FUTURE_A50": MacroSpec(
        object_id="FUTURE_A50", name="富时中国A50", short="A50",
        category="股指期货", unit="点",
        realtime_symbol="hf_CHA50CFD", realtime_layout="futures",
        history_kind="futures", history_symbol="CHA50CFD",
    ),
}


def spec_for(object_id: str) -> MacroSpec:
    spec = MACRO_SPECS.get(str(object_id).strip())
    if spec is None:
        raise MacroError(
            f"{object_id!r} 不在宏观对象符号表里。"
            f"已支持的是:{'、'.join(sorted(MACRO_SPECS))}。"
        )
    return spec


# ---------------------------------------------------------------------------
#  算 —— 解析,不碰网络
# ---------------------------------------------------------------------------


def _f(value: Any) -> float | None:
    """取一个数。**取不到返回 None,不返回 0。**

    和 `quotes._num` 同一个规矩:0 在这些品种里是不可能的价格,
    拿它顶替"没取到"会让涨跌幅算出 -100%,而那个数字看着像真的。
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _at(fields: Sequence[str], index: int) -> str:
    return fields[index].strip() if 0 <= index < len(fields) else ""


def _num_at(fields: Sequence[str], index: int) -> float | None:
    return _f(_at(fields, index))


def _last_date(fields: Sequence[str]) -> str:
    """从后往前找一个 `YYYY-MM-DD`。

    离岸人民币那条实时返回里**根本没有日期字段**(实测),所以这里可能
    返回空串 —— 那是真的没有,不是解析错了。调用方拿历史最后一天补。
    """
    for field in reversed(list(fields)):
        text = str(field).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text
    return ""


@dataclass(frozen=True)
class Realtime:
    latest: float | None
    prev: float | None
    high: float | None
    low: float | None
    time_text: str
    day: str


def parse_realtime(text: str, spec: MacroSpec) -> Realtime:
    """解析 `var hq_str_xxx="...";`。

    字段位置**按 `realtime_layout` 分两套**,抄二代。位置错了不会报错,
    只会把最高价当成最新价 —— 那种错在数字上看不出来,所以自检里
    针对两套布局各钉了一条样本。
    """
    symbol = spec.realtime_symbol
    match = re.search(rf'var\s+hq_str_{re.escape(symbol)}="([^"]*)";', text)
    if not match:
        raise MacroError(f"{spec.name}:新浪实时返回格式不认识(找不到 hq_str_{symbol})。")
    fields = match.group(1).split(",")
    if not fields or not fields[0].strip():
        raise MacroError(f"{spec.name}:新浪实时返回为空(符号 {symbol} 可能已下线)。")

    if spec.realtime_layout == "futures":
        # 0 最新 / 4 最高 / 5 最低 / 6 时间 / 7 昨收 / 12 日期
        return Realtime(
            latest=_num_at(fields, 0), prev=_num_at(fields, 7),
            high=_num_at(fields, 4), low=_num_at(fields, 5),
            time_text=_at(fields, 6), day=_at(fields, 12),
        )
    if spec.realtime_layout == "forex":
        # 0 时间 / 1 最新 / 3 昨收 / 6 最高 / 7 最低 / 日期在末尾(可能没有)
        return Realtime(
            latest=_num_at(fields, 1), prev=_num_at(fields, 3),
            high=_num_at(fields, 6), low=_num_at(fields, 7),
            time_text=_at(fields, 0), day=_last_date(fields),
        )
    raise MacroError(f"{spec.name}:不认识的字段布局 {spec.realtime_layout!r}。")


def parse_futures_history(raw: str) -> list[dict[str, Any]]:
    """新浪全球期货日 K。返回 `[{date, close}, ...]`,按日期升序。"""
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MacroError(f"期货历史返回的不是 JSON({exc})。") from exc
    if not isinstance(rows, list):
        raise MacroError("期货历史返回的不是数组。")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = str(row.get("date") or "").strip()
        close = _f(row.get("close"))
        if day and close is not None:
            out.append({"date": day, "close": close})
    return out[-HISTORY_BARS:]


def parse_forex_history(raw: str) -> list[dict[str, Any]]:
    """新浪外汇日 K。`日期,开,低,高,收` —— **第 2 位是低不是高**,
    顺序和常见的 OHLC 不一样,照二代的取法。这里只用收盘价。"""
    match = re.search(r'=\("([^"]*)"\)', raw, re.S)
    if not match:
        raise MacroError("外汇历史返回格式不认识(取不到括号里那段)。")
    out = []
    for item in match.group(1).split("|"):
        fields = item.split(",")
        day = _at(fields, 0)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        close = _num_at(fields, 4)
        if close is not None:
            out.append({"date": day, "close": close})
    return out[-HISTORY_BARS:]


def _market_state(day: str, today: date) -> tuple[bool, str]:
    """`(是否当日行情, 一句人话)`。

    **境外品种领先一天是正常的,不是数据陈旧。** 见模块开头那段。
    把这两件事写成同一句话,模型会把最新的数据当成过期数据来打折。
    """
    if not day:
        return False, "数据源没给行情日期,无法判断新旧。"
    try:
        stamp = date.fromisoformat(day)
    except ValueError:
        return False, f"数据源给的行情日期无法解析:{day}。"
    if stamp == today:
        return True, "当日行情"
    if stamp == today + timedelta(days=1):
        return True, f"数据源交易日为 {day},比北京日历领先一天 —— 境外品种跨时区,属正常。"
    if stamp == today - timedelta(days=1):
        return False, f"数据源最新为 {day}(前一日),境外市场当日尚未开盘或本地已过收盘。"
    return False, f"非当日行情:数据源最新日期为 {day},已落后 {(today - stamp).days} 天。"


# ---------------------------------------------------------------------------
#  一个宏观对象采到的东西
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MacroData:
    spec: MacroSpec
    realtime: Realtime
    closes: tuple[float, ...]
    #: 历史序列最后一根是哪一天。空串表示历史没取到。
    #: **单独存一份**,因为 `closes` 只有价格 —— 而"实时那一根要替换还是
    #: 追加"这个判断,只能靠日期对比,拿长度猜必然出错。
    history_last_day: str = ""
    history_problem: str = ""

    @property
    def latest(self) -> float | None:
        if self.realtime.latest is not None:
            return self.realtime.latest
        return self.closes[-1] if self.closes else None

    def as_entry(self, *, today: date) -> dict[str, Any]:
        """进模型上下文的形状。

        算不出来的一律 `None` 并在 `说明` 里写为什么 —— **不省略字段**。
        省掉一个字段和填 None 对模型是两回事:少一个键它可能压根不去想
        这项数据,填 None 它会看见"这项不知道"。
        """
        rt = self.realtime
        latest = self.latest
        closes = list(self.closes)
        # 历史最后一根如果就是实时这一天,拿实时值替掉它;否则追加一根。
        # **这一格错了不会报错**:"今天 vs 5 天前"会静悄悄变成"昨天 vs
        # 6 天前",算出来的涨跌幅完全合理,只是问错了问题。
        if latest is not None:
            if closes and rt.day and rt.day == self.history_last_day:
                closes[-1] = latest
            else:
                closes.append(latest)

        day = rt.day or self.history_last_day
        is_today, state = _market_state(day, today)

        notes: list[str] = []
        if self.history_problem:
            notes.append(self.history_problem)
        if rt.latest is None:
            notes.append("实时值没取到,最新值取自历史日线的最后一个收盘价。")

        return {
            "object_id": self.spec.object_id,
            "名称": self.spec.name,
            "类别": self.spec.category,
            "单位": self.spec.unit,
            "数据源": "新浪财经",
            "行情日期": day or None,
            "行情时间": _time_text(day, rt.time_text),
            "是否当日行情": is_today,
            "行情状态": state,
            "最新值": _round(latest),
            "昨收": _round(rt.prev),
            "涨跌幅": ind.pct_change(latest, rt.prev),
            "日内最高": _round(rt.high),
            "日内最低": _round(rt.low),
            "5日涨跌幅": ind.pct_change(latest, closes[-6] if len(closes) >= 6 else None),
            "20日涨跌幅": ind.pct_change(latest, closes[-21] if len(closes) >= 21 else None),
            "20日波动率": ind.annualized_volatility(closes[-21:]) if len(closes) >= 21 else None,
            "历史根数": len(self.closes),
            "趋势备注": _trend_note(closes),
            "说明": notes or None,
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _time_text(day: str, time_value: str) -> str | None:
    """拼成带时区的时间戳。**拼不出来就返回 None,不拿当前时间顶。**"""
    if not day:
        return None
    text = (time_value or "").strip()
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", text):
        return f"{day}T{text}+08:00"
    return f"{day}T00:00:00+08:00"


def _trend_note(closes: Sequence[float]) -> str:
    """短中期方向。**数据不够就说不够**,不拿现有的几根凑一个趋势出来。"""
    if len(closes) < 21:
        return f"历史只有 {len(closes)} 个收盘价,不足 21 个,不判断趋势。"
    five = ind.pct_change(closes[-1], closes[-6])
    twenty = ind.pct_change(closes[-1], closes[-21])
    if five is None or twenty is None:
        return "涨跌幅算不出来,不判断趋势。"
    if five > 0 and twenty > 0:
        return "短中期同向上行"
    if five < 0 and twenty < 0:
        return "短中期同向下行"
    return "短中期方向不一致"


# ---------------------------------------------------------------------------
#  碰
# ---------------------------------------------------------------------------


def _get(url: str, *, timeout: float) -> str:
    """取一段文本。**把 ``QuoteError`` 翻成 ``MacroError``。**

    HTTP 那一层是从 ``quotes`` 借的(headers、超时、异常分类都一样,没必要
    抄一遍),但它抛的是 ``QuoteError``。不翻的话,本模块声称的"和 quotes
    平行的异常类型"就是句空话——调用方 ``except MacroError`` 会漏掉一半
    真实故障,而那一半恰恰是最常见的那半(连不上、403)。

    新浪返回 GB18030。按 UTF-8 解会把品种名变成乱码 —— 名字虽然不进上下文
    (名字取自符号表),但乱码会让日志和排查完全没法读。
    """
    try:
        return _fetch(url, timeout=timeout, referer=SINA_REFERER, encoding="gb18030")
    except QuoteError as exc:
        raise MacroError(str(exc)) from exc


def fetch_realtime(spec: MacroSpec, *, timeout: float = DEFAULT_TIMEOUT) -> Realtime:
    return parse_realtime(_get(REALTIME_URL + spec.realtime_symbol, timeout=timeout), spec)


def fetch_history(spec: MacroSpec, *, timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    if spec.history_kind == "futures":
        return parse_futures_history(
            _get(FUTURES_HISTORY_URL + spec.history_symbol, timeout=timeout))
    if spec.history_kind == "forex":
        return parse_forex_history(
            _get(FOREX_HISTORY_URL.format(s=spec.history_symbol), timeout=timeout))
    raise MacroError(f"{spec.name}:不认识的历史接口类型 {spec.history_kind!r}。")


def fetch(object_id: str, *, timeout: float = DEFAULT_TIMEOUT) -> MacroData:
    """采一个宏观对象。

    **实时挂了这个对象就没了**(抛 `MacroError`)—— 没有最新值的宏观背景
    对判断没有用,给一个只有历史的半成品会让模型以为它拿到了当前值。

    **历史挂了不影响这个对象**:实时值还在,5日/20日/波动率/趋势那几项
    记成 `None` 并在 `说明` 里写清楚。这一条和 `collect` 里"账户挂了照样
    出判断"是同一个取向 —— 分得清哪一层没了,就不必整个作废。
    """
    spec = spec_for(object_id)
    realtime = fetch_realtime(spec, timeout=timeout)

    history: list[dict[str, Any]] = []
    problem = ""
    try:
        history = fetch_history(spec, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 历史这一层失败一律降级,不作废整个对象
        problem = f"历史日线没取到({exc}),5日/20日涨跌幅、波动率、趋势本轮为空。"
        logger.warning("%s 历史没取到:%s", spec.name, exc)

    return MacroData(
        spec=spec,
        realtime=realtime,
        closes=tuple(row["close"] for row in history),
        history_last_day=history[-1]["date"] if history else "",
        history_problem=problem,
    )


__all__ = [
    "MacroError", "MacroSpec", "MACRO_SPECS", "spec_for",
    "Realtime", "MacroData",
    "parse_realtime", "parse_futures_history", "parse_forex_history",
    "fetch_realtime", "fetch_history", "fetch",
    "HISTORY_BARS", "DEFAULT_TIMEOUT",
]
