"""行情采集(碰 + 算)—— 沪深个股的实时快照与前复权日线。

## 两个源,腾讯在前

| 源 | 实时 | 日线 | 从部署机器可达 |
|---|---|---|---|
| 腾讯 ``qt.gtimg.cn`` / ``web.ifzq.gtimg.cn`` | ✓ | ✓ | **✓** |
| 东财 ``push2`` / ``push2his`` | ✓ | ✓ | **✗** |

**东财的行情接口从这套系统所在的服务器连不上**(连接直接被重置,不是
超时也不是 403)。这是实测,不是推测——二代在同一台机器上跑了几个月,
一直走的就是腾讯这条路。所以腾讯排在前面,东财是补位。

顺序写死在 ``SOURCES`` 里。**不要"哪个快用哪个"**:两个源的数据在盘中
会有细微差异,今天用这个明天用那个,回头查一轮判断为什么是那样,
会对不上任何一份数据。

## ⚠️ 东财那条路的价格是整数

东财返回 ``f43=130788`` 表示 1307.88——要除以倍率,股票 100、ETF 1000。
**判错倍率就是十倍价差**,而十倍的价格会一路通过校验(涨跌停、资金
都是拿同一份数据算的,自洽),最后按十倍价格挂出去。

腾讯那条路没有这个问题,它返回的就是 ``1307.88``。这是把腾讯放前面的
第二个理由。

东财路径上另加一道:拿日线收盘价**交叉校验**快照价格,差 5 倍以上直接
拒绝(见 ``verify_scale``)。涨跌停一天最多 20%,5 倍不可能被正常行情触发。

## 和券商会话无关

这些都是公开接口,不需要登录。**行情拿不到不该导致重新登录**,反过来
也一样——把两件事绑在一起,一个源抖一下就会去动券商会话,那才是危险的。

## 零依赖

``urllib``。二代这里用 httpx,买到的是 ``follow_redirects`` 和连接池:
前者标准库默认就做,后者对"一天六轮、一轮几十个请求"毫无意义。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping, Sequence

logger = logging.getLogger("zhixing.quotes")

# -- 腾讯 -------------------------------------------------------------------

TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

# -- 东财 -------------------------------------------------------------------

EASTMONEY_QUOTE_ENDPOINTS = (
    "https://push2.eastmoney.com/api/qt/stock/get",
    "https://push2delay.eastmoney.com/api/qt/stock/get",
)
EASTMONEY_KLINE_ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

#: 要哪些字段。**不给 ``fields`` 会返回上百个**,大半看不懂也用不上。
EASTMONEY_QUOTE_FIELDS = ",".join([
    "f43",   # 最新价      f44 最高   f45 最低   f46 今开
    "f44", "f45", "f46",
    "f47",   # 成交量(手)  f48 成交额(元)  f50 量比
    "f48", "f50",
    "f57",   # 代码        f58 名称   f60 昨收   f71 均价
    "f58", "f60", "f71",
    "f86",   # 行情时间戳(秒)
    "f116", "f117",          # 总市值 / 流通市值
    "f168",  # 换手率      f169 涨跌额  f170 涨跌幅  f171 振幅
    "f169", "f170", "f171",
])
EASTMONEY_KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6"
EASTMONEY_KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

#: 浏览器样子的头。**东财不带 Referer 会 403。**
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

DEFAULT_TIMEOUT = 15.0
DEFAULT_KLINE_LIMIT = 320

#: 日线最少要几根。少于这个数算不出 20 日均线之类的东西,而一个"用 8 天
#: 数据凑出来的 20 日均线"比没有更坏——它看着是个正常数字。
MIN_KLINE_ROWS = 60

#: 交叉校验的容忍倍数。见模块开头。
SCALE_TOLERANCE = 5

#: 源的先后。**顺序是语义的一部分,不要按快慢重排。** 理由见模块开头。
SOURCES = ("腾讯", "东财")


class QuoteError(RuntimeError):
    """行情没取到。"""


# ---------------------------------------------------------------------------
#  算
# ---------------------------------------------------------------------------


def tencent_symbol(market: str, symbol: str) -> str:
    text = str(market or "").strip().upper()
    if text not in {"SH", "SZ"}:
        raise QuoteError(f"不认识的市场 {market!r},只支持 SH / SZ。")
    return ("sh" if text == "SH" else "sz") + str(symbol)


def secid(market: str, symbol: str) -> str:
    """东财的证券标识。**沪是 1,深是 0**,反了会取到另一只股票。"""
    text = str(market or "").strip().upper()
    if text not in {"SH", "SZ"}:
        raise QuoteError(f"不认识的市场 {market!r},只支持 SH / SZ。")
    return f"{'1' if text == 'SH' else '0'}.{symbol}"


def price_factor(asset_type: str) -> int:
    """东财价格倍率。ETF / 基金三位小数,其余两位。**腾讯那条路用不着。**"""
    return 1000 if str(asset_type or "").strip() in {"ETF", "基金", "LOF"} else 100


def _num(value: Any) -> Decimal | None:
    """取一个数。**取不到返回 None,不返回 0。**

    行情里 0 是个合法价格(停牌、集合竞价前),拿它顶替"没取到",
    会让下游把停牌当成价格暴跌。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--", "—"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def scaled(value: Any, factor: int) -> Decimal | None:
    """东财的整数价 → 真实价。"""
    raw = _num(value)
    if raw is None or factor <= 0:
        return None
    return raw / Decimal(factor)


def plain(value: Decimal | None) -> float | None:
    """``Decimal`` → JSON 能装的东西。"""
    return None if value is None else float(value)


@dataclass(frozen=True)
class Bar:
    """一根日线。**这里的值都是真实数值**,不缩放。"""

    day: str
    open: Decimal | None
    close: Decimal | None
    high: Decimal | None
    low: Decimal | None
    volume: Decimal | None
    amount: Decimal | None = None
    change_pct: Decimal | None = None
    turnover_pct: Decimal | None = None

    def as_entry(self) -> dict[str, Any]:
        return {
            "日期": self.day, "开": plain(self.open), "收": plain(self.close),
            "高": plain(self.high), "低": plain(self.low),
            "成交量": plain(self.volume), "成交额": plain(self.amount),
            "涨跌幅": plain(self.change_pct), "换手率": plain(self.turnover_pct),
        }


@dataclass(frozen=True)
class Quote:
    """一份实时快照。"""

    symbol: str
    name: str
    last_price: Decimal | None
    prev_close: Decimal | None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    change: Decimal | None = None
    change_pct: Decimal | None = None
    amplitude_pct: Decimal | None = None
    volume: Decimal | None = None
    amount: Decimal | None = None
    turnover_pct: Decimal | None = None
    volume_ratio: Decimal | None = None
    quote_time: str | None = None
    #: 哪个源给的。**必须带出去**——两个源盘中会有细微差异,事后查一轮
    #: 判断为什么是那样,得先知道当时看的是谁的数。
    source: str = ""
    #: 是不是延时行情。用延时数据做的判断和用实时数据做的是两回事,
    #: 而它们长得一模一样。
    delayed: bool = False

    def as_entry(self) -> dict[str, Any]:
        return {
            "证券代码": self.symbol, "证券名称": self.name,
            "最新价": plain(self.last_price), "昨收": plain(self.prev_close),
            "开盘价": plain(self.open), "最高价": plain(self.high), "最低价": plain(self.low),
            "涨跌额": plain(self.change), "涨跌幅": plain(self.change_pct),
            "振幅": plain(self.amplitude_pct),
            "成交量": plain(self.volume), "成交额": plain(self.amount),
            "换手率": plain(self.turnover_pct), "量比": plain(self.volume_ratio),
            "行情时间": self.quote_time,
            "行情源": self.source + ("(延时)" if self.delayed else ""),
        }


# -- 腾讯的解析 --------------------------------------------------------------

_TENCENT_PAYLOAD = re.compile(r'="([^"]*)"')

#: 腾讯那串 ``~`` 分隔字段的下标。**下标是从实测数据数出来的,不是文档。**
#: 改这里之前请先拿一条真返回数一遍——数错一位,"昨收"就变成"今开",
#: 而两个都是合理的价格,任何校验都发现不了。
_T = {
    "名称": 1, "代码": 2, "最新价": 3, "昨收": 4, "今开": 5, "成交量": 6,
    "时间": 30, "涨跌额": 31, "涨跌幅": 32, "最高": 33, "最低": 34,
    "量额串": 35, "换手率": 38, "振幅": 43, "量比": 49,
}


def _at(parts: Sequence[str], index: int) -> str:
    return parts[index] if 0 <= index < len(parts) else ""


def parse_tencent_quote(text: str) -> Quote:
    """腾讯实时行情 → ``Quote``。**纯函数。**"""
    match = _TENCENT_PAYLOAD.search(str(text or "").strip())
    if not match:
        raise QuoteError("腾讯行情返回里没有数据段。")
    parts = match.group(1).split("~")
    if len(parts) < 35:
        raise QuoteError(f"腾讯行情字段只有 {len(parts)} 段,不够用(至少要 35 段)。")
    last = _num(_at(parts, _T["最新价"]))
    if last is None:
        raise QuoteError("腾讯行情没有最新价(代码可能不存在或已退市)。")

    stamp = _at(parts, _T["时间"]).strip()
    quote_time: str | None = None
    if len(stamp) == 14 and stamp.isdigit():
        try:
            quote_time = datetime.strptime(stamp, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            quote_time = None

    # 成交额藏在 "价/量/额" 那一串里。取不到就算了,它不进任何判断。
    amount: Decimal | None = None
    combo = _at(parts, _T["量额串"])
    if "/" in combo:
        amount = _num(combo.rsplit("/", 1)[-1])

    return Quote(
        symbol=_at(parts, _T["代码"]).strip(),
        name=_at(parts, _T["名称"]).strip(),
        last_price=last,
        prev_close=_num(_at(parts, _T["昨收"])),
        open=_num(_at(parts, _T["今开"])),
        high=_num(_at(parts, _T["最高"])),
        low=_num(_at(parts, _T["最低"])),
        change=_num(_at(parts, _T["涨跌额"])),
        change_pct=_num(_at(parts, _T["涨跌幅"])),
        amplitude_pct=_num(_at(parts, _T["振幅"])),
        volume=_num(_at(parts, _T["成交量"])),
        amount=amount,
        turnover_pct=_num(_at(parts, _T["换手率"])),
        volume_ratio=_num(_at(parts, _T["量比"])),
        quote_time=quote_time,
        source="腾讯",
    )


def parse_tencent_klines(rows: Iterable[Any]) -> tuple[Bar, ...]:
    """腾讯日线 → ``Bar``。

    腾讯只给 日期/开/收/高/低/量 六项,**涨跌幅要自己算**。算不出来
    (没有前一根)就留 ``None``,不填 0——第一根的涨跌幅本来就不知道。
    """
    bars: list[Bar] = []
    prev_close: Decimal | None = None
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(row, str) or len(row) < 6:
            continue
        close = _num(row[2])
        change_pct: Decimal | None = None
        if close is not None and prev_close is not None and prev_close != 0:
            change_pct = ((close - prev_close) / prev_close * 100).quantize(Decimal("0.0001"))
        bars.append(Bar(
            day=str(row[0]), open=_num(row[1]), close=close,
            high=_num(row[3]), low=_num(row[4]), volume=_num(row[5]),
            change_pct=change_pct,
        ))
        if close is not None:
            prev_close = close
    return tuple(bars)


# -- 东财的解析 --------------------------------------------------------------


def parse_eastmoney_quote(
    data: Mapping[str, Any], *, factor: int, delayed: bool = False
) -> Quote:
    """东财快照 JSON → ``Quote``。**纯函数。**"""
    stamp = _num(data.get("f86"))
    quote_time: str | None = None
    if stamp is not None and stamp > 0:
        try:
            quote_time = datetime.fromtimestamp(int(stamp)).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            quote_time = None
    return Quote(
        symbol=str(data.get("f57") or "").strip(),
        name=str(data.get("f58") or "").strip(),
        last_price=scaled(data.get("f43"), factor),
        prev_close=scaled(data.get("f60"), factor),
        open=scaled(data.get("f46"), factor),
        high=scaled(data.get("f44"), factor),
        low=scaled(data.get("f45"), factor),
        change=scaled(data.get("f169"), factor),
        # 百分数字段固定两位小数,和价格倍率无关。混用会让 ETF 的涨跌幅差十倍。
        change_pct=scaled(data.get("f170"), 100),
        amplitude_pct=scaled(data.get("f171"), 100),
        volume=_num(data.get("f47")),
        amount=_num(data.get("f48")),
        turnover_pct=scaled(data.get("f168"), 100),
        volume_ratio=scaled(data.get("f50"), 100),
        quote_time=quote_time,
        source="东财",
        delayed=delayed,
    )


def parse_eastmoney_klines(rows: Iterable[Any]) -> tuple[Bar, ...]:
    """东财日线(逗号分隔的字符串)→ ``Bar``。段数不足 11 的行直接丢。"""
    bars: list[Bar] = []
    for row in rows:
        parts = str(row).split(",")
        if len(parts) < 11:
            continue
        bars.append(Bar(
            day=parts[0], open=_num(parts[1]), close=_num(parts[2]),
            high=_num(parts[3]), low=_num(parts[4]), volume=_num(parts[5]),
            amount=_num(parts[6]), change_pct=_num(parts[8]), turnover_pct=_num(parts[10]),
        ))
    return tuple(bars)


# -- 交叉校验 ----------------------------------------------------------------


def verify_scale(quote: Quote, bars: Sequence[Bar]) -> None:
    """拿日线收盘价校验快照价格的量级。**对不上就抛,不猜。**

    两边走的是完全不同的代码路径:东财快照是整数除倍率,日线本来就是
    真实值。倍率判错时只有快照那边会差十倍——这就是能查出来的原因。

    **校验不了(缺任一边)就放过。** 没有数据不等于数据错了;在这里把
    "查不了"当成"查出问题",第一天没有日线的新标的就永远起不来。
    """
    if quote.last_price is None or quote.last_price <= 0:
        return
    closes = [b.close for b in bars if b.close is not None and b.close > 0]
    if not closes:
        return
    reference = closes[-1]
    ratio = quote.last_price / reference
    if ratio > SCALE_TOLERANCE or ratio < Decimal(1) / SCALE_TOLERANCE:
        raise QuoteError(
            f"{quote.symbol} 的快照价({quote.last_price})与日线收盘价({reference})"
            f"差了 {ratio:.1f} 倍,几乎肯定是价格倍率取错了。已放弃这份行情——"
            "十倍的价格会一路通过校验然后挂出去。"
        )


def is_today(bars: Sequence[Bar], today: date) -> bool:
    """最新一根日线是不是今天的。

    **这是「是否当日行情」的判据。** 停牌、休市、数据源滞后都会让它为假,
    而那时候拿昨天的价去下单是错的。
    """
    return bool(bars) and bars[-1].day == today.isoformat()


# ---------------------------------------------------------------------------
#  碰
# ---------------------------------------------------------------------------


def _fetch(url: str, *, timeout: float, referer: str = "", encoding: str = "utf-8") -> str:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode(encoding, "replace")
    except urllib.error.HTTPError as exc:
        raise QuoteError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # 连接被重置在这里很常见——东财对某些出口 IP 就是这个反应。
        # 它和"代码不存在"完全不同,所以话要说得能分开。
        raise QuoteError(f"连不上({exc})") from exc


def _fetch_json(url: str, params: Mapping[str, str], *, timeout: float, referer: str = "") -> Any:
    raw = _fetch(f"{url}?{urllib.parse.urlencode(params)}", timeout=timeout, referer=referer)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QuoteError(f"返回的不是 JSON({exc})") from exc


# -- 单个源 -----------------------------------------------------------------


def fetch_tencent_quote(market: str, symbol: str, *, timeout: float = DEFAULT_TIMEOUT) -> Quote:
    # 腾讯返回 GBK。按 UTF-8 解会把证券名称变成乱码,而乱码的名字会一路
    # 进到模型的上下文里——模型看不懂,但也不会报错。
    raw = _fetch(TENCENT_QUOTE_URL + tencent_symbol(market, symbol),
                 timeout=timeout, encoding="gbk")
    return parse_tencent_quote(raw)


def fetch_tencent_klines(
    market: str, symbol: str, *, limit: int = DEFAULT_KLINE_LIMIT,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[Bar, ...]:
    sym = tencent_symbol(market, symbol)
    payload = _fetch_json(TENCENT_KLINE_URL, {"param": f"{sym},day,,,{limit},qfq"},
                          timeout=timeout, referer="https://gu.qq.com/")
    data = (payload or {}).get("data") if isinstance(payload, Mapping) else None
    node = data.get(sym) if isinstance(data, Mapping) else None
    rows = None
    if isinstance(node, Mapping):
        rows = node.get("qfqday") or node.get("day")
    return parse_tencent_klines(rows if isinstance(rows, list) else [])


def fetch_eastmoney_quote(
    market: str, symbol: str, *, asset_type: str = "", timeout: float = DEFAULT_TIMEOUT
) -> Quote:
    factor = price_factor(asset_type)
    sid = secid(market, symbol)
    last: QuoteError | None = None
    for index, endpoint in enumerate(EASTMONEY_QUOTE_ENDPOINTS):
        try:
            payload = _fetch_json(endpoint, {"secid": sid, "fields": EASTMONEY_QUOTE_FIELDS},
                                  timeout=timeout, referer="https://quote.eastmoney.com/")
            data = (payload or {}).get("data") if isinstance(payload, Mapping) else None
            if not isinstance(data, Mapping) or data.get("f43") in {None, "-", ""}:
                raise QuoteError("返回了空数据(代码可能不存在或已退市)")
            return parse_eastmoney_quote(data, factor=factor, delayed=index > 0)
        except QuoteError as exc:
            last = exc
    raise QuoteError(str(last))


def fetch_eastmoney_klines(
    market: str, symbol: str, *, limit: int = DEFAULT_KLINE_LIMIT,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[Bar, ...]:
    payload = _fetch_json(EASTMONEY_KLINE_ENDPOINT, {
        "secid": secid(market, symbol),
        "fields1": EASTMONEY_KLINE_FIELDS1, "fields2": EASTMONEY_KLINE_FIELDS2,
        "klt": "101", "fqt": "1", "end": "20500101", "lmt": str(limit),
    }, timeout=timeout, referer="https://quote.eastmoney.com/")
    data = (payload or {}).get("data") if isinstance(payload, Mapping) else None
    rows = data.get("klines") if isinstance(data, Mapping) else None
    return parse_eastmoney_klines(rows if isinstance(rows, list) else [])


# -- 编排 -------------------------------------------------------------------


@dataclass(frozen=True)
class MarketData:
    """一个标的的全部行情。"""

    quote: Quote
    bars: tuple[Bar, ...]
    quote_is_today: bool

    def as_entry(self, *, bars: int = 60) -> dict[str, Any]:
        """给模型上下文用的形状。

        日线只带最近 ``bars`` 根。全带上去(320 根)会把上下文撑爆,而
        指标已经在 ``indicators`` 里算过了——原始日线是给人和给模型看
        近期形态的,不是给模型自己算均线的。
        """
        return {
            "快照": self.quote.as_entry(),
            "是否当日行情": self.quote_is_today,
            "行情状态": "当日行情" if self.quote_is_today else
                        f"非当日行情:数据源最新交易日为 {self.bars[-1].day if self.bars else '未知'}",
            "日线根数": len(self.bars),
            "近期日线": [b.as_entry() for b in self.bars[-bars:]],
        }


def fetch(
    *, market: str, symbol: str, asset_type: str = "", today: date | None = None,
    limit: int = DEFAULT_KLINE_LIMIT, timeout: float = DEFAULT_TIMEOUT,
    sources: Sequence[str] = SOURCES, sleep: Callable[[float], None] = time.sleep,
) -> MarketData:
    """按 ``sources`` 的顺序取一个标的的行情。

    **快照和日线必须来自同一个源。** 混着用会得到"腾讯的价 + 东财的
    日线",两边的复权口径不一定一致,交叉校验就失去意义了。所以这里是
    整组成败,不是逐项退化。

    行情是**读操作**,重试安全——这和下单那边"绝不重试"不矛盾,区别在
    于读没有副作用。

    全部源都不成才抛,并且**一次报全部源的原因**,不是只报最后一个。
    只说"东财连不上"会让人去查东财,而腾讯那边可能是另一个毛病。
    """
    problems: list[str] = []
    for index, name in enumerate(sources):
        if index:
            sleep(0.4)
        try:
            if name == "腾讯":
                quote = fetch_tencent_quote(market, symbol, timeout=timeout)
                bars = fetch_tencent_klines(market, symbol, limit=limit, timeout=timeout)
            elif name == "东财":
                quote = fetch_eastmoney_quote(market, symbol, asset_type=asset_type, timeout=timeout)
                bars = fetch_eastmoney_klines(market, symbol, limit=limit, timeout=timeout)
            else:
                problems.append(f"{name}:不认识的行情源")
                continue
            if len(bars) < MIN_KLINE_ROWS:
                raise QuoteError(
                    f"日线只有 {len(bars)} 根,少于 {MIN_KLINE_ROWS} 根,"
                    "不足以算指标,按取不到处理"
                )
            verify_scale(quote, bars)
        except QuoteError as exc:
            problems.append(f"{name}:{exc}")
            continue
        if index:
            logger.info("%s%s 的行情由备源「%s」提供(前面的源:%s)",
                        market, symbol, name, ";".join(problems))
        return MarketData(quote=quote, bars=bars,
                          quote_is_today=is_today(bars, today or date.today()))
    raise QuoteError(f"{market}{symbol} 的行情所有源都取不到 —— " + ";".join(problems))


__all__ = [
    "TENCENT_QUOTE_URL", "TENCENT_KLINE_URL",
    "EASTMONEY_QUOTE_ENDPOINTS", "EASTMONEY_KLINE_ENDPOINT",
    "HEADERS", "DEFAULT_TIMEOUT", "DEFAULT_KLINE_LIMIT",
    "MIN_KLINE_ROWS", "SCALE_TOLERANCE", "SOURCES",
    "QuoteError", "Bar", "Quote", "MarketData",
    "tencent_symbol", "secid", "price_factor", "scaled", "plain",
    "parse_tencent_quote", "parse_tencent_klines",
    "parse_eastmoney_quote", "parse_eastmoney_klines",
    "verify_scale", "is_today",
    "fetch_tencent_quote", "fetch_tencent_klines",
    "fetch_eastmoney_quote", "fetch_eastmoney_klines", "fetch",
]
