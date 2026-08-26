"""技术指标(算)—— 从日线算 MA / MACD / KDJ / BOLL / RSI / ATR。

## 这一层只出数,不出判断

二代这里有个 ``market_trend_note()``,返回"短中期同向上行"之类的话,
直接进模型上下文。**三代不做这件事。**

理由:那句话是judgment,而判断是模型的活。喂给它一句"同向上行",它极
大概率顺着这句话说下去——本该由它权衡的东西,被上一层用五行 if 替
模型定了。而那五行 if 里没有任何模型不具备的信息,它看的是同一批数。

所以本模块只出数值。"MA5 上穿 MA20 意味着什么"由模型自己说,说错了
是模型的责任,能在归档里查到;藏在 if 里说错了没人找得到。

## 三个必须说清楚的口径

指标这东西每家算得都不一样,数值对不上第三方软件时,人第一反应是
"代码写错了"。所以口径写在这儿,以后对不上先来看这一段:

1. **KDJ 用 SMA(3,1) 不是 EMA**。国内(通达信、同花顺)的 KDJ 是
   ``K = 2/3·K' + 1/3·RSV``,初值 K=D=50。用 EMA 算出来的 KDJ 数值
   完全不同,而且**看着一样合理**。
2. **BOLL 的标准差除以 n,不是 n-1**。这是通达信口径。Python 的
   ``statistics.stdev`` 除的是 n-1,谁哪天"顺手改成标准库"就会让上下轨
   偏窄——偏得不多,不会报错,只是从此和行情软件对不上。
3. **ATR 是简单平均不是 Wilder 平滑**。两种都在用,这里取简单平均,
   和二代一致(换口径会让新旧归档的同名字段不可比)。

## ⚠️ 指标值绝不能当委托价用

本模块返回的全是 ``float``。而 ``guards.ValidatedOrder`` 的价格字段要
``Decimal``——所以一个指标值**在类型上就无法**变成挂单价格。

这是有意的:委托价必须是精确十进制(浮点的 0.1+0.2 会多出尾数,东财
按最小变动价位一对就报"价格不合法")。而指标是统计量,浮点正合适,
用 Decimal 反而算不了开方和指数平滑。两边各用各的,中间那道墙由类型
系统守着,不靠人记得。

## 数据不够就返回 None

不返回 0,也不"能算多少算多少"。**一个用 8 天数据凑出来的 MA20 比没有
更坏**——它是个合理的数字,人和模型都看不出它是假的。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

#: 各指标要的最少日线根数。**不够就整项为 None。**
MIN_BARS = {"MA60": 60, "MACD": 35, "KDJ": 9, "BOLL": 20, "RSI14": 15, "ATR14": 15}

#: 输出保留几位小数。指标是给人和模型看的,六位足够,再多是噪音。
PRECISION = 6


def _f(value: Any) -> float | None:
    """转 float。``Decimal``、字符串、None 都吃。

    **转不了返回 None,不返回 0。** 见模块开头。
    """
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, PRECISION)


def _window(values: Sequence[float | None], period: int) -> list[float] | None:
    """取最后 ``period`` 个值,**要求它们全都有**。

    ⚠️ 这里和二代不一样。二代是先把 None 滤掉再看剩几个,于是当中间
    散落着缺失值时,它会拿**跨了 30 个交易日的 20 个点**算出一个数,
    叫它 MA20。那个数不是 MA20,但没有任何地方看得出来。

    宁可返回"算不出来"。算不出来是可见的,算错了不是。
    """
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    if any(v is None for v in window):
        return None
    return [float(v) for v in window]      # type: ignore[arg-type]


# ---------------------------------------------------------------------------
#  基础量
# ---------------------------------------------------------------------------


def moving_average(values: Sequence[float | None], period: int) -> float | None:
    """简单移动平均。窗口内有缺失值就返回 None,见 ``_window``。"""
    window = _window(values, period)
    return None if window is None else _round(sum(window) / period)


def stddev(values: Sequence[float]) -> float | None:
    """总体标准差,**除以 n**。BOLL 用的是这个口径,见模块开头第 2 条。"""
    if not values:
        return None
    avg = sum(values) / len(values)
    return math.sqrt(sum((v - avg) ** 2 for v in values) / len(values))


def ema(values: Sequence[float], period: int) -> list[float]:
    """指数移动平均的整条序列。

    首项直接取原值作为种子(而不是先算 SMA 再接上)。这是常见做法,
    序列足够长时两者收敛;MACD 要求 35 根以上正是为了让它收敛。
    """
    out: list[float] = []
    current: float | None = None
    factor = 2 / (period + 1)
    for value in values:
        current = value if current is None else value * factor + current * (1 - factor)
        out.append(current)
    return out


def pct_change(current: float | None, previous: float | None) -> float | None:
    """涨跌幅(%)。分母是 0 或缺失时返回 None——**不返回 0%**。"""
    if current is None or previous is None or previous == 0:
        return None
    return _round((current - previous) / previous * 100)


# ---------------------------------------------------------------------------
#  指标
# ---------------------------------------------------------------------------


def macd(closes: Sequence[float]) -> tuple[float | None, float | None, float | None]:
    """MACD(12, 26, 9) → (DIF, DEA, MACD柱)。

    柱子按 ``DIF - DEA`` 算,**不乘 2**。有些软件显示的是 2 倍值;
    换算只差一个常数,但数值对不上时得知道差在哪。
    """
    if len(closes) < MIN_BARS["MACD"]:
        return None, None, None
    fast, slow = ema(closes, 12), ema(closes, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    return _round(dif[-1]), _round(dea[-1]), _round(dif[-1] - dea[-1])


def kdj(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 9
) -> tuple[float | None, float | None, float | None]:
    """KDJ(9, 3, 3) → (K, D, J)。**国内口径**,见模块开头第 1 条。

    三条序列**必须等长**,否则直接返回 None:长度不齐意味着某一天的
    高/低/收对不上号,那时候算出来的 RSV 是拿 A 日的收盘配 B 日的高低,
    数值合理但完全是错的。
    """
    n = len(closes)
    if n < period or len(highs) != n or len(lows) != n:
        return None, None, None
    k = d = 50.0                      # 国内约定的初值
    for i in range(period - 1, n):
        window_high = max(highs[i - period + 1: i + 1])
        window_low = min(lows[i - period + 1: i + 1])
        # 高低相等 = 一字板/停牌。此时 RSV 无定义,取 50(中性)而不是
        # 0 或 100——后两者会让 KDJ 直接打到极值,看着像强烈信号。
        rsv = 50.0 if window_high == window_low else \
            (closes[i] - window_low) / (window_high - window_low) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
    return _round(k), _round(d), _round(3 * k - 2 * d)


def boll(closes: Sequence[float], period: int = 20, width: float = 2.0
         ) -> tuple[float | None, float | None, float | None, float | None]:
    """BOLL(20, 2) → (中轨, 上轨, 下轨, %B)。

    ``%B`` 是价格在通道里的相对位置:0 = 贴下轨,1 = 贴上轨,可以出界。
    它比"上轨 3.2 元"更直接,而且**不含判断**——只是换了个单位。
    """
    if len(closes) < period:
        return None, None, None, None
    window = list(closes[-period:])
    mid = sum(window) / period
    sigma = stddev(window)
    if sigma is None:
        return None, None, None, None
    upper, lower = mid + width * sigma, mid - width * sigma
    # 通道宽度为 0(连续一字板)时 %B 无定义。返回 None,不返回 0.5。
    percent_b = None if upper == lower else (closes[-1] - lower) / (upper - lower)
    return _round(mid), _round(upper), _round(lower), _round(percent_b)


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """RSI(period),Wilder 平滑。

    全程无下跌时返回 100.0——这是定义上的极值,不是异常。
    """
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, period + 1)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, period + 1)]
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    return _round(100 - 100 / (1 + avg_gain / avg_loss))


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        period: int = 14) -> float | None:
    """ATR(period),**简单平均**不是 Wilder 平滑,见模块开头第 3 条。"""
    n = len(closes)
    if n < period + 1 or len(highs) != n or len(lows) != n:
        return None
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, n)
    ]
    return _round(sum(trs[-period:]) / period)


def annualized_volatility(closes: Sequence[float], trading_days: int = 252) -> float | None:
    """年化波动率(%)。日收益率标准差 × √252。"""
    if len(closes) < 3:
        return None
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1]
               for i in range(1, len(closes)) if closes[i - 1] != 0]
    sigma = stddev(returns)
    return None if sigma is None else _round(sigma * math.sqrt(trading_days) * 100)


# ---------------------------------------------------------------------------
#  汇总
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Indicators:
    """一个标的的全部指标。**每一项都可能是 None,含义是"算不出来"。**

    ``notes`` 记下哪些项因为数据不够没算——这比让人对着一堆 null 猜
    强得多。null 可能是没数据,也可能是代码坏了,写清楚才分得开。
    """

    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    dif: float | None = None
    dea: float | None = None
    macd_hist: float | None = None
    k: float | None = None
    d: float | None = None
    j: float | None = None
    boll_mid: float | None = None
    boll_upper: float | None = None
    boll_lower: float | None = None
    boll_percent_b: float | None = None
    rsi6: float | None = None
    rsi14: float | None = None
    atr14: float | None = None
    volatility: float | None = None
    change_5d: float | None = None
    change_20d: float | None = None
    change_60d: float | None = None
    bars_used: int = 0
    notes: tuple[str, ...] = ()

    def as_entry(self) -> dict[str, Any]:
        """给模型上下文用的形状。

        **null 原样送出去,不填 0 也不省略字段。** 省略字段会让模型以为
        我们没算;填 0 会让它以为算出来是 0。两者都比一个明写的 null 坏。
        """
        return {
            "均线": {"MA5": self.ma5, "MA10": self.ma10, "MA20": self.ma20, "MA60": self.ma60},
            "MACD": {"DIF": self.dif, "DEA": self.dea, "柱": self.macd_hist,
                     "口径": "12/26/9,柱=DIF-DEA(未乘2)"},
            "KDJ": {"K": self.k, "D": self.d, "J": self.j, "口径": "9/3/3,国内 SMA 平滑"},
            "BOLL": {"中轨": self.boll_mid, "上轨": self.boll_upper, "下轨": self.boll_lower,
                     "%B": self.boll_percent_b, "口径": "20日,2倍标准差(除以n)"},
            "RSI": {"RSI6": self.rsi6, "RSI14": self.rsi14},
            "ATR14": self.atr14,
            "年化波动率%": self.volatility,
            "区间涨跌幅%": {"近5日": self.change_5d, "近20日": self.change_20d,
                            "近60日": self.change_60d},
            "参与计算的日线根数": self.bars_used,
            "未能计算的项": list(self.notes),
        }


def compute(bars: Sequence[Any]) -> Indicators:
    """从 ``quotes.Bar`` 序列算全部指标。**纯函数,不碰网络不碰磁盘。**

    只要求 ``bars`` 的元素有 ``open/high/low/close`` 属性,不 import
    ``quotes``——指标是数学,不该认识行情源。

    收盘价缺失的那些行**整行丢掉**,因为 KDJ / ATR 要求高低收对齐,
    留一个只有高低没有收的行会让下标错位。丢了几行会记在 ``notes`` 里。
    """
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    dropped = 0
    for bar in bars:
        close = _f(getattr(bar, "close", None))
        high = _f(getattr(bar, "high", None))
        low = _f(getattr(bar, "low", None))
        if close is None:
            dropped += 1
            continue
        closes.append(close)
        # 高/低缺失时拿收盘价顶上。这**只影响 KDJ 和 ATR 的波动幅度**
        # (会算小),不会让它们错位。比整行丢掉损失小。
        highs.append(high if high is not None else close)
        lows.append(low if low is not None else close)

    notes: list[str] = []
    if dropped:
        notes.append(f"有 {dropped} 根日线没有收盘价,已丢弃。")

    def need(name: str) -> bool:
        if len(closes) >= MIN_BARS.get(name, 0):
            return True
        notes.append(f"{name} 需要 {MIN_BARS[name]} 根日线,只有 {len(closes)} 根,没算。")
        return False

    dif, dea, hist = macd(closes) if need("MACD") else (None, None, None)
    k, d, j = kdj(highs, lows, closes) if need("KDJ") else (None, None, None)
    mid, upper, lower, pb = boll(closes) if need("BOLL") else (None, None, None, None)

    def back(n: int) -> float | None:
        """``n`` 个交易日前的收盘价。不够就 None。"""
        return closes[-(n + 1)] if len(closes) > n else None

    last = closes[-1] if closes else None
    return Indicators(
        ma5=moving_average(closes, 5),
        ma10=moving_average(closes, 10),
        ma20=moving_average(closes, 20),
        ma60=moving_average(closes, 60),
        dif=dif, dea=dea, macd_hist=hist,
        k=k, d=d, j=j,
        boll_mid=mid, boll_upper=upper, boll_lower=lower, boll_percent_b=pb,
        rsi6=rsi(closes, 6), rsi14=rsi(closes, 14) if need("RSI14") else None,
        atr14=atr(highs, lows, closes) if need("ATR14") else None,
        volatility=annualized_volatility(closes[-60:]) if len(closes) >= 20 else None,
        change_5d=pct_change(last, back(5)),
        change_20d=pct_change(last, back(20)),
        change_60d=pct_change(last, back(60)),
        bars_used=len(closes),
        notes=tuple(notes),
    )


__all__ = [
    "MIN_BARS", "PRECISION", "Indicators", "compute",
    "moving_average", "stddev", "ema", "pct_change",
    "macd", "kdj", "boll", "rsi", "atr", "annualized_volatility",
]
