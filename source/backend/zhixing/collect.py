"""采集(碰)—— 把一轮要用的数据凑齐:行情、指标、账户、流水。

实现 ``runner.DataSource``。这是"数据从哪来"的唯一答案。

## 三条线各自成败,不互相拖累

一轮要三样东西:**行情**(公开接口)、**指标**(纯计算)、**账户**(要登录)。
它们的失败必须分开处置,因为后果完全不同:

| 挂了什么 | 后果 | 这一轮还跑不跑 |
|---|---|---|
| 某个标的的行情 | 那个标的**整个跳过**,不问模型 | 跑,其余标的照常 |
| 账户 | 上下文少账户事实;若会话也不可用,执行结果记失败 | 跑,判断与指令照常留痕 |
| 全部行情 | 没有任何标的可问 | 跑,但会是一轮空轮 + 一堆问题记录 |

二代这里是一挂全挂:采集抛异常,整轮没了,归档里什么都没有,事后
只看得到一行日志。三代**每一种失败都要留在归档里**——归档是事实来源,
"这一轮为什么只判断了 5 个标的"必须能从归档本身答出来。

## 取不到行情的标的**不问模型**

这条值得单说。备选做法是照样问,数据字段填 null,让模型自己看着办。
不行:模型看到一堆 null 不会说"我没法判断",它会**基于名称和常识编
一个判断出来**,而那个判断长得和正常判断一模一样,置信度还可能挺高。

所以宁可少一条判断,并且把"少了谁、为什么"写进 ``problems``。

## 会话复用

登录一次要过验证码,而每登一次都是一次被券商锁卡的机会。所以浏览器
会话跨轮保持,只在失效时重登。``close()`` 由调用方在停服时调。

## ⚠️ 涉密

``AccountReport`` 里有完整资金账号和持仓金额。**本模块的日志只打条数和
成败,不打金额、不打账号。** 账户数据会进归档和模型上下文——那是设计
要求(模型得知道有多少钱),但**日志不是**,日志会被复制粘贴到聊天里。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping

from . import broker as broker_mod
from . import captcha as captcha_mod
from . import eastmoney as em
from . import guards
from . import indicators as ind
from . import login as login_mod
from . import macro as macro_mod
from . import quotes as quotes_mod
from . import runner as runner_mod
from . import state as state_mod
from . import webdriver as wd
from .catalog import Catalog, TradeObject

logger = logging.getLogger("zhixing.collect")

#: 两次取行情之间歇多久(秒)。**不是限流礼貌,是自保**——腾讯对同一个
#: IP 连打会开始返回空串,而空串在解析层长得像"这个代码不存在"。
QUOTE_PAUSE = 0.25

#: 日线取多少根。60 根是指标的底线(见 ``quotes.MIN_KLINE_ROWS``),
#: 取 320 是为了让 MA60 有足够的历史,同时不至于让上下文太大——
#: 进模型的只有最近 60 根,见 ``quotes.MarketData.as_entry``。
KLINE_LIMIT = 320

#: 进模型上下文的日线根数。
CONTEXT_BARS = 60


def is_st_name(name: str) -> bool:
    """名称里带不带 ST,用于只读标的快照。

    **大小写和全角都要认。** 深市返回过全角的 "ＳＴ"。
    """
    text = str(name or "").upper().replace("＊", "*").replace("Ｓ", "S").replace("Ｔ", "T")
    return "ST" in text


@dataclass(frozen=True)
class ObjectData:
    """一个标的采到的东西。**采不到的部分是 None,不是空壳。**

    ``market`` 和 ``macro`` **互斥**:沪深证券走前者,境外宏观品种走后者
    (判据是 ``obj.is_macro``)。两个都为 None 就是这个对象本轮没采到。

    ⚠️ **宏观数据不进 ``market``,这是结构上的隔离,不是随手分的字段。**
    ``snapshots()`` 那张按 symbol 建的查价表是从 ``d.market`` 来的。宏观值
    一旦能进 ``market``,美元指数的 98.7 就会成为某只证券的"最新价"进到
    上下文和归档里。分成两个字段之后,这件事**写不出来**,而不是"注意别写"。
    """

    obj: TradeObject
    market: quotes_mod.MarketData | None = None
    indicators: ind.Indicators | None = None
    position: em.Position | None = None
    macro: macro_mod.MacroData | None = None
    problem: str = ""

    @property
    def ok(self) -> bool:
        return self.market is not None or self.macro is not None


# ---------------------------------------------------------------------------
#  算 —— 把采到的东西摆成上下文的形状
# ---------------------------------------------------------------------------


def position_entry(position: em.Position | None) -> Any:
    """持仓段。

    **没持仓和没取到持仓是两回事。** 前者返回一个明写着 0 的结构,
    后者返回 None——模型看到 null 会知道"这项不知道",看到 0 会认为
    "确实空仓"。让它们长得一样,是在制造一个查不出来的错误决策。
    """
    if position is None:
        return {"持有": False, "数量": 0, "可用数量": 0, "成本价": None,
                "市值": None, "浮动盈亏": None, "当日盈亏": None}
    return {
        "持有": True,
        # 数量本身也可能是 None(接口没给)。**原样传出去**,别拿 0 顶——
        # 见 ``eastmoney.Position`` 的类文档,那里也是这个规矩。
        "数量": position.holding_qty,
        "可用数量": position.available_qty,
        "冻结数量": position.frozen_qty,
        "成本价": quotes_mod.plain(position.cost_price),
        "市值": quotes_mod.plain(position.market_value),
        "浮动盈亏": quotes_mod.plain(position.profit),
        "当日盈亏": quotes_mod.plain(position.today_profit),
        "说明": list(position.notes) if position.notes else None,
    }


def object_entry(data: ObjectData, *, today: date) -> dict[str, Any]:
    """``today`` 是**传进来的**,不在函数里取 —— 「算 vs 碰」那条规矩:
    这个函数要能在自检里钉住任意一天的输出。宏观对象判断"是不是当日行情"
    要用到它,而那个判断在跨时区品种上是有讲究的(见 ``macro._market_state``)。
    """
    """单个标的进模型上下文的形状。"""
    obj = data.obj
    entry: dict[str, Any] = {
        "object_id": obj.object_id,
        "市场": obj.market,
        "证券代码": obj.symbol,
        "证券名称": obj.name,
        "类型": obj.kind,
        "资产类型": obj.asset_type,
        "交易单位": obj.lot_size,
        "可否下单": obj.is_tradable,
    }
    if data.macro is not None:
        # 宏观对象的形状和证券完全不同:没有快照、没有日线、没有持仓,
        # 只有一个值和几个变化率。**不硬凑成证券的形状** —— 拿 None 填出
        # 一个「开盘价/成交量/换手率」齐全的壳子,模型会以为它在看一只股票。
        entry["宏观行情"] = data.macro.as_entry(today=today)
        return entry
    if data.market is None:
        # 走不到这儿(取不到的标的根本不进 per_object),留着是防呆。
        entry["行情"] = None
        entry["行情缺失原因"] = data.problem or "未知"
        return entry
    entry["行情"] = data.market.as_entry(bars=CONTEXT_BARS)
    entry["技术指标"] = data.indicators.as_entry() if data.indicators else None
    entry["持仓"] = position_entry(data.position)
    return entry


#: 券商没配全 —— **这不是故障,是还没建。** 归在 ``runner.KNOWN_ABSENCES``
#: 里,不计入连续失败轮数。
ACCOUNT_UNCONFIGURED = "ACCOUNT_UNAVAILABLE"

#: 配了券商,但这一轮没登进去 —— **这是故障。** 不在 ``KNOWN_ABSENCES`` 里,
#: 所以这一轮记为失败,界面上按问题显示而不是按"已知缺项"显示。
#:
#: 为什么非要和上面那条分开:原先两者共用 ``ACCOUNT_UNAVAILABLE``,而那个
#: 码是"已知缺项",于是**登录失败在界面上和"券商还没配"长得一模一样**——
#: 一行灰字。2026-08-21 有两轮登录三次全败、整轮拿不到账户,状态页上却
#: 什么都没红。一个码同时表示"本来就没有"和"本来该有却没了",等于把这两
#: 件事的区别抹掉,而这正是契约 1.4 那组字段存在的理由。
ACCOUNT_LOGIN_FAILED = "ACCOUNT_LOGIN_FAILED"

#: 登进去了,但账户接口没答 —— 同样是故障,同样要红。
ACCOUNT_QUERY_FAILED = "ACCOUNT_QUERY_FAILED"


@dataclass(frozen=True)
class AccountProblem:
    """这一轮账户为什么没有。**带着码走,不让上层去猜。**

    从前 ``fetch_account`` 只返回一句话,码是调用方写死的常量,于是"没配"
    和"登录失败"必然同码。把码和话绑在一起产生它们的地方,是让分类这件事
    只做一次、只在知道答案的地方做。
    """

    code: str
    message: str

    def __str__(self) -> str:            # 归档和提示词里只要那句话
        return self.message


def account_entry(
    report: em.AccountReport | None, activity: Any, problem: str = ""
) -> dict[str, Any]:
    """账户与流水段。**取不到就明说取不到,不给空表。**

    空表的意思是"今天一笔都没有"。把"查不到"渲染成空表,模型会当成
    "今天什么都没发生"然后据此判断——这正是本项目反复要避免的那类错误。
    """
    if report is None:
        return {
            "取到了": False,
            "原因": problem or "账户数据未采集。",
            "说明": "本轮没有账户数据;判断照常产出,执行结果会如实记录券商适配器不可用。",
            "账户": None, "当日流水": activity,
        }
    return {
        "取到了": True,
        "账户": report.as_summary(),
        "当日流水": activity if activity is not None else {"取到了": False, "原因": "未采集。"},
    }


def market_entry(items: list[ObjectData], *, today: date) -> list[dict[str, Any]]:
    """行情对象段(宏观背景)。**清单里标了「行情对象」的那些。**

    它们不可下单,存在的意义是给模型一个大盘/板块的参照。放在共享段里,
    所以对本轮全部标的**逐字节相同**——这是前缀缓存能命中的前提。
    """
    return [object_entry(d, today=today) for d in items if d.ok]


def data_window(items: list[ObjectData]) -> dict[str, str]:
    """本轮数据的时间范围(契约 ``data_window``)。

    ``起`` 取所有日线里最早的一天,``止`` 取所有快照里最晚的时刻。
    **一个都没采到时返回空串而不是当前时间**——拿当前时间冒充数据时间,
    会让"数据是陈的"这件事在归档里彻底看不出来。
    """
    days: list[str] = []
    stamps: list[str] = []
    for d in items:
        if d.market is None:
            continue
        if d.market.bars:
            days.append(d.market.bars[0].day)
        if d.market.quote.quote_time:
            stamps.append(d.market.quote.quote_time)
    return {"起": min(days) if days else "", "止": max(stamps) if stamps else ""}


def snapshots(
    items: list[ObjectData], catalog: Catalog
) -> dict[str, guards.ObjectSnapshot]:
    """造兼容快照。**键是 symbol**。

    本地下单风控已拆除,规范化过程不读取这些值。结构暂时保留给上下文、
    自检和后续只读分析使用。
    """
    out: dict[str, guards.ObjectSnapshot] = {}
    for d in items:
        if d.market is None or d.market.quote.last_price is None:
            continue
        pos = d.position
        # ST 判定取「清单名」和「行情名」的并集,作为只读快照事实保留。
        is_st = is_st_name(d.obj.name) or is_st_name(d.market.quote.name)
        # 数量可能是 None(接口没给)。兼容结构仍要求 int,因此这里用 0;
        # 执行层不会据此判断能否提交。
        out[d.obj.symbol] = catalog.to_snapshot(
            d.obj.object_id,
            last_price=d.market.quote.last_price,
            prev_close=d.market.quote.prev_close,
            available_qty=(pos.available_qty or 0) if pos else 0,
            holding_qty=(pos.holding_qty or 0) if pos else 0,
            is_st=is_st,
            quote_is_today=d.market.quote_is_today,
        )
    return out


def scope_entry(items: list[ObjectData], now: datetime) -> dict[str, Any]:
    """``读取范围`` 段:这一轮到底读了什么、漏了什么。

    **漏掉的要明写。** 只列读到的,等于告诉模型"这就是全部",
    而它没法知道本来还该有两个。
    """
    ok = [d for d in items if d.ok]
    bad = [d for d in items if not d.ok]
    return {
        "采集时间": now.isoformat(timespec="seconds"),
        "标的总数": len(items),
        "采到行情的": len(ok),
        "没采到的": [{"证券代码": d.obj.symbol, "证券名称": d.obj.name,
                      "原因": d.problem} for d in bad],
        "日线根数上限": KLINE_LIMIT,
        "进上下文的日线根数": CONTEXT_BARS,
        "说明": "「没采到的」里的标的本轮不会被问及,也不会有指令。",
    }


def describe_source(store: state_mod.Store) -> str:
    """数据源自述,进契约 1.4 的 ``数据源`` 字段。

    **从代码和配置现状算出来,不是手写的一句话。** 这一条是冲着二代
    缺陷 6 去的:状态页上曾经写死着「只读挂载二代 runtime」,而部署后
    容器里根本没有那个挂载——**一句没有任何代码在做的承诺,被当成事实
    显示了半年**。手写的字符串必然和代码分叉,因为改代码的人不会想起
    去改一句话。

    所以行情源顺序直接取 ``quotes.SOURCES``(改了那个元组,这句话跟着变),
    账户那半句取决于券商配没配全——**没配全就明说采不到**,而不是含糊地
    写「东方财富」让人以为它在工作。
    """
    行情 = " → ".join(quotes_mod.SOURCES)

    # 宏观那半句同理:**清单里有宏观对象才提新浪**。一个都没有的时候写
    # 「宏观:新浪财经」,等于承诺了一件本轮根本不会发生的事。
    宏观 = ""
    try:
        macro_objects = store.catalog().macro
    except (state_mod.StateError, OSError, ValueError):
        # 清单读不出来是清单那边的事,不该让这句自述整个失败——
        # 它是给人看的一行字,不是判断依据。少一段总比整行报错强。
        macro_objects = ()
    if macro_objects:
        宏观 = f";宏观:新浪财经({len(macro_objects)} 项)"

    try:
        broker = store.broker()
    except state_mod.StateError as exc:
        return f"行情:{行情}(按此顺序退化){宏观};账户:配置读不出来({exc})"
    if broker.configured:
        账户 = "东方财富网页版(浏览器会话)"
    else:
        账户 = "未配置,本轮不会有账户数据(缺:%s)" % "、".join(broker.missing())
    return f"行情:{行情}(按此顺序退化){宏观};账户:{账户}"


# ---------------------------------------------------------------------------
#  碰
# ---------------------------------------------------------------------------


@dataclass
class Collector:
    """一轮数据的采集器。**跨轮复用同一个实例**,为的是复用浏览器会话。

    ``solver`` 用于登录时认验证码;没配券商时传什么都行,因为
    ``login.ensure_session`` 会先因为缺配置而失败,根本走不到识别。
    """

    store: state_mod.Store
    solver: captcha_mod.CaptchaSolver
    sleep: Callable[[float], None] = time.sleep
    #: 保持着的浏览器会话。None 表示还没登过 / 上次断了。
    session: wd.Session | None = field(default=None, repr=False)

    # -- 券商 -------------------------------------------------------------

    def _broker(self) -> broker_mod.EastmoneyBroker:
        """拿一个能用的券商适配器。**必要时登录,能复用就复用。**"""
        settings = self.store.broker()
        session_id = self.session.session_id if self.session else ""
        # 东财登录失败时只回一句「您输入的信息有误」,**不说错的是哪一项**。
        # 这套账号密码此前成功登进去过,那句话才敢当成"验证码没认对"去换
        # 一张图重来;没登过就一次都不多试——否则密码要是真错了,一天六轮
        # 每轮三次,就是拿账户去撞券商的错误次数上限。
        # 判断在 em.classify_login_error 里,这里只负责把事实取出来。
        proven = self.store.login_proven(settings.account, settings.password)
        session, result = login_mod.ensure_session(
            settings, solver=self.solver, session_id=session_id, sleep=self.sleep,
            password_proven=proven,
        )
        self.session = session
        if not result.reused:
            logger.info("券商会话已重建(第 %d 次尝试登录)", result.attempts)
            if not proven:
                # 第一次真登进去了,记一笔。**只记指纹,不记密码。**
                try:
                    self.store.save_login_proof(settings.account, settings.password)
                except OSError as exc:
                    logger.warning(
                        "登录指纹没能落盘:%s —— 不影响这一轮,但下次登录失败时"
                        "仍会按「没登过」保守处理,只试一次。", exc
                    )
        return broker_mod.EastmoneyBroker(session=session)

    def connect_broker(self) -> broker_mod.EastmoneyBroker:
        """供人工接口按需取得会话。配置与登录细节仍全部封装在登录层。"""
        return self._broker()

    def execution_broker(self) -> broker_mod.EastmoneyBroker | None:
        """返回本轮采集已经建立好的券商适配器,**不在这里触发登录**。

        下单紧跟在采集之后。复用同一会话既避免重复登录,也保证券商没配或
        本轮登录失败时明确得到 ``None``,由执行层留痕而不是让整轮崩掉。
        """
        if self.session is None:
            return None
        return broker_mod.EastmoneyBroker(session=self.session)

    def fetch_account(self) -> tuple[em.AccountReport | None, Any, AccountProblem | None]:
        """取账户 + 当日流水。返回 ``(报表, 流水, 问题)``,没问题时第三项是 ``None``。

        **不抛异常。** 账户挂了这一轮照样要跑;执行层随后按会话是否可用
        如实记录结果。把异常抛出去会让采集整个失败,连判断都没了。
        """
        try:
            broker = self._broker()
        except login_mod.LoginError as exc:
            self.session = None
            if exc.stage == "读配置":
                # 没配全。**这句话可以原样端出去**:它列的是缺哪几个字段名,
                # 不含任何值。见 ``login.ensure_session``。
                return None, None, AccountProblem(ACCOUNT_UNCONFIGURED, str(exc))
            # 配了却没登上。``LoginError`` 的话是由「到哪一步」加「页面报了
            # 什么」拼出来的,登录层保证不含账号、密码、会话 id 和 profile
            # 路径(模块开头那条规矩),所以这一句可以带到界面上——不带的话
            # 人在界面上只能看到"登录失败",得去翻服务器日志才知道为什么。
            logger.warning("券商登录没成功,卡在=%s,已试完=%s", exc.stage or "未知", exc.exhausted)
            return None, None, AccountProblem(ACCOUNT_LOGIN_FAILED, f"券商登录没成功:{exc}")
        except (wd.WebDriverError, em.EastmoneyError) as exc:
            self.session = None
            # 这两类异常的原文可能带浏览器内部会话或 profile 路径,**只留分类**。
            logger.warning("券商会话建立失败,异常类型=%s", exc.__class__.__name__)
            return None, None, AccountProblem(
                ACCOUNT_LOGIN_FAILED, f"券商会话建立失败({exc.__class__.__name__})")

        try:
            report = broker.account()
        except (em.SessionExpired, em.EastmoneyError, wd.WebDriverError) as exc:
            # 会话失效就丢掉句柄,下一轮重登。**不在这里立刻重登**——
            # 重登要过验证码,而这一轮的时间窗口有限,拖下去等于错过时点。
            if isinstance(exc, em.SessionExpired):
                self.session = None
            logger.warning("查账户失败,异常类型=%s", exc.__class__.__name__)
            return None, None, AccountProblem(
                ACCOUNT_QUERY_FAILED, f"查账户失败({exc.__class__.__name__})")

        activity: Any = None
        try:
            activity = broker.activity()
        except (em.SessionExpired, em.EastmoneyError, wd.WebDriverError) as exc:
            # 流水挂了不影响账户可用。异常原文可能带浏览器内部路径,
            # 所以归档只留分类,不留底层文本。
            activity = {
                "取到了": False,
                "原因": f"券商流水查询失败({exc.__class__.__name__})",
            }
            if isinstance(exc, em.SessionExpired):
                self.session = None
            logger.warning("当日流水没取到(账户正常),异常类型=%s", exc.__class__.__name__)

        logger.info("账户已采集:持仓 %d 只,可用资金%s",
                    len(report.positions), "已取到" if report.usable else "未取到")
        return report, activity, None

    # -- 行情 -------------------------------------------------------------

    def fetch_object(
        self, obj: TradeObject, *, today: date, positions: Mapping[str, em.Position]
    ) -> ObjectData:
        """采一个对象。**不抛异常**,失败写进 ``problem``。

        路由判据是 ``obj.is_macro``,不是市场码 —— 市场码对宏观对象只是个
        占位串,拿它做分支等于把一个显式的类型判断换成一个约定。
        """
        if obj.is_macro:
            return self.fetch_macro(obj)

        try:
            market = quotes_mod.fetch(
                market=obj.market, symbol=obj.symbol, asset_type=obj.asset_type,
                today=today, limit=KLINE_LIMIT, sleep=self.sleep,
            )
        except quotes_mod.QuoteError as exc:
            logger.warning("%s 行情没采到:%s", obj.display, exc)
            return ObjectData(obj=obj, problem=str(exc))

        return ObjectData(
            obj=obj,
            market=market,
            indicators=ind.compute(market.bars),
            position=positions.get(obj.symbol),
        )

    def fetch_macro(self, obj: TradeObject) -> ObjectData:
        """采一个宏观对象。**不抛异常**,失败写进 ``problem``。

        不传持仓、不算 ``indicators``:这两样都是为沪深证券准备的。
        ``indicators.compute`` 要的是 ``quotes.Bar`` 序列(开高低收量),
        而宏观这边只有收盘价 —— 5日/20日涨跌幅和波动率在 ``macro`` 里
        单独算,能算的就那几项,**不硬凑一份 MACD 出来**。
        """
        try:
            data = macro_mod.fetch(obj.object_id)
        except macro_mod.MacroError as exc:
            logger.warning("%s 宏观数据没采到:%s", obj.display, exc)
            return ObjectData(obj=obj, problem=str(exc))
        return ObjectData(obj=obj, macro=data)

    # -- 编排 -------------------------------------------------------------

    def collect(self, *, now: datetime, catalog: Catalog) -> runner_mod.RoundInput:
        """凑齐一轮的输入。实现 ``runner.DataSource``。

        顺序是**先账户后行情**:账户要登录,是最容易失败也最慢的一步;
        先做完可让同一轮后续执行复用已建立的会话。持仓同时用于填每个标的
        的 ``持仓`` 段,但不再参与本地下单阻断。
        """
        report, activity, 账户问题 = self.fetch_account()
        account_problem = str(账户问题) if 账户问题 else ""
        positions = {p.symbol: p for p in report.positions} if report else {}

        today = now.date()
        items: list[ObjectData] = []
        for index, obj in enumerate(catalog.objects):
            if index:
                self.sleep(QUOTE_PAUSE)
            items.append(self.fetch_object(obj, today=today, positions=positions))

        problems: list[dict[str, Any]] = []
        if 账户问题 is not None:
            problems.append({
                "object_id": "", "code": 账户问题.code,
                "message": 账户问题.message + " 本轮判断与指令照常归档,但委托无法发给券商。",
            })
        for d in items:
            if not d.ok:
                problems.append({
                    "object_id": d.obj.object_id, "code": "QUOTE_UNAVAILABLE",
                    "message": f"{d.obj.display} 没采到行情({d.problem}),本轮跳过。",
                })

        # per_object 只放**可下单且采到行情**的标的。行情对象不单独问模型,
        # 它们已经在共享段的「市场数据列表」里了——单独问一遍是白花钱。
        per_object = {
            d.obj.object_id: object_entry(d, today=today)
            for d in items if d.ok and d.obj.is_tradable
        }

        account = None
        if report is not None and report.available_cash is not None:
            account = guards.AccountSnapshot(available_cash=report.available_cash)
        # 可用资金缺失时保持 None,不编成 0,也不再作为下单阻断问题。

        logger.info("本轮采集完成:%d 个标的,%d 个可判断,%d 个问题",
                    len(items), len(per_object), len(problems))

        # 账户摘要落一份给界面用。**只在真取到时写**——写一份空的会把上一次
        # 成功采集的结果冲掉,界面从"有数据(稍陈)"退化成"什么都没有",
        # 而这一轮账户挂了并不意味着上一轮的数据变假了。
        if report is not None:
            try:
                self.store.save_account(
                    report.as_summary(), collected_at=now.isoformat(timespec="seconds")
                )
            except OSError as exc:
                # 落盘失败不该让这一轮作废——账户数据已经在手上,该判断照样判断。
                logger.warning("账户摘要没能落盘:%s", exc)

        self._record_facts(now, report=report, problem=account_problem)

        return runner_mod.RoundInput(
            读取范围=scope_entry(items, now),
            市场数据列表=market_entry(
                [d for d in items if not d.obj.is_tradable], today=today),
            账户交易流水表=account_entry(report, activity, account_problem),
            per_object=per_object,
            data_window=data_window(items),
            account=account,
            objects=snapshots(items, catalog),
            problems=tuple(problems),
        )

    # -- 留痕 -------------------------------------------------------------

    def _record_facts(
        self, now: datetime, *, report: em.AccountReport | None, problem: str
    ) -> None:
        """写契约 1.4 的 ``最近采集时间`` 和 ``登录状态``。

        **这两项只有采集层知道,别处填不出来。** 在此之前它们恒为
        ``None`` / ``未知``——也就是说契约 1.4 那组「停摆可见性」字段有一半
        是摆设:界面上永远显示"未知",而"未知"和"登录挂了"看起来一模一样,
        那组字段本来就是为了把这两件事分开才加的。

        三种状态各有各的意思,**不许合并**:

        - ``已登录``:这一轮真的从券商取到了账户数据。
        - ``未登录``:配了券商但没登上——**这是要人去看的故障**。
        - ``未知``:券商压根没配。不是故障,是还没建。

        ``最近采集时间`` 无论账户成败都要写:它记的是"这个系统最后一次
        睁眼看世界是什么时候",行情采到了就算睁过眼。它和账户快照里的
        ``采集时间`` 不是一回事,后者只在真取到账户时才更新。
        """
        if report is not None:
            登录状态 = "已登录"
        elif self.store.broker().configured:
            登录状态 = "未登录"
        else:
            登录状态 = "未知"

        try:
            facts = self.store.runtime()
            self.store.save_runtime(replace(
                facts,
                最近采集时间=now.isoformat(timespec="seconds"),
                登录状态=登录状态,
            ))
        except (OSError, state_mod.StateError) as exc:
            # 留痕失败不该让这一轮作废。数据已经采到手了,归档才是事实来源;
            # 运行事实只是给界面看的派生状态,丢一次不影响判断。
            logger.warning("运行事实没能落盘:%s", exc)

    # -- 收尾 -------------------------------------------------------------

    def close(self) -> None:
        """停服时关掉浏览器会话。**幂等,不抛异常。**"""
        if self.session is not None:
            self.session.close()
            self.session = None


__all__ = [
    "QUOTE_PAUSE", "KLINE_LIMIT", "CONTEXT_BARS",
    "ACCOUNT_UNCONFIGURED", "ACCOUNT_LOGIN_FAILED", "ACCOUNT_QUERY_FAILED",
    "AccountProblem",
    "ObjectData", "Collector", "is_st_name",
    "position_entry", "object_entry", "account_entry", "market_entry",
    "data_window", "snapshots", "scope_entry", "describe_source",
]
