"""标的清单 —— 名称、资产类型、交易单位的唯一来源。

## 这个模块要解决的问题

二代把标的属性散在两处:`runtime/trade_objects/catalog.json` 里明明存着
`名称` / `资产类型` / `交易单位`,但下单相关的代码把一手股数写死成 100、
最小变动价位写死成 0.01。两边不一致时没人会发现——**ETF 的最小变动价位是
0.001**,拿 0.01 去判会把合法报价当成非法。

更要紧的是名称。二代策略输出里只有 `object_id`,界面上全是 `SH_510300`
这种代码,没法看。名字不是拿不到,是没往输出里贴。

## 名称由后端 join,模型不输出它(契约 1.1)

不让模型复述名字有两个原因:

1. 模型有机会编一个不存在的名字,而且代码与名字对不上时你分不清是谁错了
2. join 的时候顺手能发现「模型给的这个代码根本不在清单里」——
   模型凭空捏代码是必须拦下的事故,二代拦不住

**这从一个显示问题变成了一个校验点。** 所以 `annotate()` 不做静默降级:
认不出的 `object_id` 单独返回,由调用方决定怎么拦,不允许"查不到就留空"。

## 与二代数据的兼容

三代验证期只读挂载二代 runtime,所以要能读二代那份 JSON。两代的字段名不同:

    二代:{"object_id", "市场", "代码", "名称", "资产类型", "交易单位"}
    三代:{"object_id", "market", "symbol", "名称", "类型", "资产类型", "交易单位"}

`from_entry()` 两种都认。

⚠️ 二代**没有 `类型` 字段**——它靠 `trade_ids` / `market_ids` 两个列表在外面
区分交易标的与行情对象。所以读二代数据时必须显式传 `market_object_ids`,
否则全部按「交易标的」处理,行情对象会被误判成可下单。这是已知的口径差异,
不是可以猜的东西。

## 三类对象,两条采集路径

    交易标的  SH/SZ 证券,可下单        → quotes.fetch
    行情对象  SH/SZ 证券,只作背景      → quotes.fetch
    宏观对象  境外品种,只作背景        → macro.fetch

`is_tradable` 分的是第一列(能不能下单),`is_macro` 分的是第三列(走哪个
采集模块)。**这两个判据不是一回事**,别拿其中一个代替另一个:上证指数
不可下单但走 quotes,WTI 原油不可下单且走 macro。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from . import macro as macro_mod
from .guards import ObjectSnapshot

logger = logging.getLogger("zhixing.catalog")


KIND_TRADABLE = "交易标的"
KIND_QUOTE_ONLY = "行情对象"

#: 第三类:境外宏观品种(美元指数、离岸人民币、WTI、COMEX黄金、富时A50)。
#:
#: **为什么不复用「行情对象」。** 行情对象是沪深两市的证券(上证指数之类),
#: 走 ``quotes.py``,有 SH/SZ 市场码和六位数字代码。宏观对象一样都没有:
#: 它走 ``macro.py``,符号是 ``hf_CL`` 这种,采回来的东西连 K 线结构都不是。
#: 揉成一类的代价是每个用到 ``kind`` 的地方都要再问一次"这个到底走哪条路",
#: 那种判断迟早会有人漏写一处。
#:
#: ``is_tradable`` 判的是 ``kind == KIND_TRADABLE``,所以**新增这一类自动不可下单**,
#: 不需要在别处补一句 "or kind == 宏观对象"。这是当初那么写的好处,别改成枚举。
KIND_MACRO = "宏观对象"

#: 宏观对象的市场位。**不是真的市场**,是个占位——但必须占,因为
#: ``save_catalog`` 会把 ``market`` 原样写回 JSON,留空会让重载后
#: ``make_object_id`` 拼出个 ``_WTI`` 来。
MACRO_MARKET = "MACRO"

VALID_MARKETS = frozenset({"SH", "SZ"})
VALID_KINDS = frozenset({KIND_TRADABLE, KIND_QUOTE_ONLY, KIND_MACRO})
VALID_ASSET_TYPES = frozenset({"ETF", "股票"})


# ---------------------------------------------------------------------------
#  单个标的
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeObject:
    """清单里的一个标的。

    这些字段全是**用户维护的**,不是采集来的(契约 1.2.1)。
    采集来的持仓、行情不放这里——那些一天变六次,这些一个月动一次,
    混在一个结构里是二代「交易对象」页没法看的根本原因。
    """

    object_id: str
    market: str                     # SH / SZ / MACRO
    symbol: str
    name: str
    kind: str = KIND_TRADABLE       # 交易标的 / 行情对象 / 宏观对象
    asset_type: str = "股票"         # ETF / 股票 / 宏观对象的类别(外汇、大宗商品…)
    lot_size: int = 100

    @property
    def is_etf(self) -> bool:
        return self.asset_type == "ETF"

    @property
    def is_tradable(self) -> bool:
        """行情对象、宏观对象只作背景,永远不可下单。"""
        return self.kind == KIND_TRADABLE

    @property
    def is_macro(self) -> bool:
        """走 ``macro.py`` 而不是 ``quotes.py``。

        **这是采集路由的判据**,不是显示上的分类。判错的后果是拿 SH/SZ
        的接口去请求 ``hf_CL``,那边会返回一个空串,在解析层长得像
        "这个代码不存在"。
        """
        return self.kind == KIND_MACRO

    @property
    def display(self) -> str:
        """界面与日志里的统一写法:`SH_510300 沪深300ETF`。"""
        return f"{self.object_id} {self.name}"


def make_object_id(market: str, symbol: str) -> str:
    """`object_id` 由后端按 `市场_代码` 生成,前端不传(契约 1.2.1)。"""
    return f"{market.strip().upper()}_{symbol.strip()}"


# ---------------------------------------------------------------------------
#  清单
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnknownObjects(Exception):
    """模型给出了清单里不存在的 `object_id`。

    这不是可以忽略的小事:意味着模型捏造了一个代码。二代会把它一路带到
    界面上,三代必须在 join 这一步就炸出来。
    """

    object_ids: tuple[str, ...]

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"标的清单里没有这些 object_id(疑似模型捏造):{', '.join(self.object_ids)}"


class Catalog:
    """一份标的清单的只读视图。

    构造后不可变。要改清单就重新构造一份,不要就地修改——
    调度器、上下文层、校验层可能同时持有同一份,原地改会造成一轮之内
    前后看到不同的清单。
    """

    __slots__ = ("_objects", "_by_id", "_by_symbol")

    def __init__(self, objects: Iterable[TradeObject]) -> None:
        items = tuple(objects)

        seen: dict[str, TradeObject] = {}
        for obj in items:
            if obj.object_id in seen:
                raise ValueError(f"标的清单里 object_id 重复:{obj.object_id}")
            seen[obj.object_id] = obj

        self._objects = items
        self._by_id = seen
        # 校验层按 symbol 查(guards.ValidationContext.objects 的键是 symbol)。
        #
        # ⚠️ **沪深两市的代码段是重叠的**,别信"不会撞"这种说法:
        # ``SH000001`` 是上证指数,``SZ000001`` 是平安银行,同一个 000001。
        # 深市主板 000xxx 和沪市指数 000xxx 整段撞在一起。
        #
        # 所以这道检查不是防呆,是**真的会触发**。撞上时宁可清单加载不了,
        # 也不能让校验层拿上证指数的价去校验平安银行的委托——那一单会以
        # 一个完全合理的价格报出去,任何地方都看不出不对。
        by_symbol: dict[str, TradeObject] = {}
        for obj in items:
            if obj.symbol in by_symbol:
                raise ValueError(
                    f"标的清单里证券代码重复:{obj.symbol}"
                    f"({by_symbol[obj.symbol].object_id} 与 {obj.object_id})"
                )
            by_symbol[obj.symbol] = obj
        self._by_symbol = by_symbol

    # -- 读 ---------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._objects)

    def __iter__(self):
        return iter(self._objects)

    @property
    def objects(self) -> tuple[TradeObject, ...]:
        return self._objects

    @property
    def tradable(self) -> tuple[TradeObject, ...]:
        """可下单的那些。行情对象不在内。"""
        return tuple(o for o in self._objects if o.is_tradable)

    @property
    def quote_only(self) -> tuple[TradeObject, ...]:
        """沪深两市的「行情对象」。**不含宏观对象**——两者采集路径不同,
        合成一个属性会让调用方误以为可以一起丢给 ``quotes.fetch``。"""
        return tuple(o for o in self._objects if o.kind == KIND_QUOTE_ONLY)

    @property
    def macro(self) -> tuple[TradeObject, ...]:
        """境外宏观对象。走 ``macro.fetch``。"""
        return tuple(o for o in self._objects if o.is_macro)

    @property
    def background(self) -> tuple[TradeObject, ...]:
        """全部不可下单的对象(行情对象 + 宏观对象)。

        进模型上下文共享段的就是这些。**按"可不可下单"分,不按"从哪采"分**
        ——对模型来说它们是同一回事:背景,不是可操作的东西。
        """
        return tuple(o for o in self._objects if not o.is_tradable)

    def get(self, object_id: str) -> TradeObject | None:
        return self._by_id.get(object_id)

    def by_symbol(self, symbol: str) -> TradeObject | None:
        return self._by_symbol.get(symbol)

    def require(self, object_id: str) -> TradeObject:
        """查不到就抛。用在"查不到即事故"的场合。"""
        obj = self._by_id.get(object_id)
        if obj is None:
            raise UnknownObjects((object_id,))
        return obj

    # -- join ------------------------------------------------------------

    def annotate(
        self, judgments: Sequence[Mapping[str, object]]
    ) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        """给模型输出的 `交易对象判断` 贴上 `名称`。

        返回 ``(贴好名称的判断, 清单里查不到的 object_id)``。

        **查不到的不会被丢掉,也不会留空名称** —— 原样返回在第一项里
        (`名称` 为 None),同时出现在第二项里。调用方必须显式处理第二项:
        它非空就意味着模型捏了代码,那一条判断连同它派生的指令都不能用。

        故意不在这里直接抛异常:一条判断有问题不该让其余六条也没法用。
        """
        annotated: list[dict[str, object]] = []
        unknown: list[str] = []

        for item in judgments:
            row = dict(item)
            object_id = str(row.get("object_id", "")).strip()
            obj = self._by_id.get(object_id)
            if obj is None:
                unknown.append(object_id)
                row["名称"] = None
            else:
                row["名称"] = obj.name
            annotated.append(row)

        if unknown:
            logger.error(
                "模型输出里有 %d 个 object_id 不在标的清单内:%s",
                len(unknown),
                unknown,
            )
        return annotated, tuple(unknown)

    # -- 转给校验层 --------------------------------------------------------

    def to_snapshot(
        self,
        object_id: str,
        *,
        last_price: Decimal,
        prev_close: Decimal | None = None,
        available_qty: int = 0,
        holding_qty: int = 0,
        is_st: bool = False,
        quote_is_today: bool = True,
    ) -> ObjectSnapshot:
        """按清单里的属性造校验用快照。

        `is_etf` / `lot_size` / `is_tradable` 从清单取,**不由调用方填**——
        二代就是因为这几个值散落在调用点上才会写死成常量。
        """
        obj = self.require(object_id)
        return ObjectSnapshot(
            symbol=obj.symbol,
            last_price=last_price,
            prev_close=prev_close,
            available_qty=available_qty,
            holding_qty=holding_qty,
            is_etf=obj.is_etf,
            is_st=is_st,
            is_tradable=obj.is_tradable,
            quote_is_today=quote_is_today,
            lot_size=obj.lot_size,
        )


# ---------------------------------------------------------------------------
#  载入
# ---------------------------------------------------------------------------


def from_entry(
    entry: Mapping[str, object], *, kind: str | None = None
) -> TradeObject:
    """把一条 JSON 记录转成 `TradeObject`。两代字段名都认。

    :param kind: 显式指定类型。二代数据里没有 `类型` 字段,必须由调用方补。
    """
    market = str(entry.get("market") or entry.get("市场") or "").strip().upper()
    symbol = str(entry.get("symbol") or entry.get("代码") or "").strip()
    name = str(entry.get("名称") or entry.get("name") or "").strip()

    object_id = str(entry.get("object_id") or "").strip() or make_object_id(market, symbol)

    resolved_kind = kind or str(entry.get("类型") or "").strip() or KIND_TRADABLE
    asset_type = str(entry.get("资产类型") or "").strip() or "股票"

    raw_lot = entry.get("交易单位", entry.get("lot_size", 100))
    try:
        lot_size = int(raw_lot)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        lot_size = 100

    return TradeObject(
        object_id=object_id,
        market=market,
        symbol=symbol,
        name=name,
        kind=resolved_kind,
        asset_type=asset_type,
        lot_size=lot_size,
    )


def load_catalog(
    path: str | Path, *, market_object_ids: Iterable[str] = ()
) -> Catalog:
    """从 JSON 文件读清单。

    :param market_object_ids: 这些 id 按「行情对象」处理。
        **读二代数据时必须传**——二代靠外部两个列表区分,记录里没有类型字段,
        不传的话行情对象会被当成可下单标的。
    """
    quote_only = frozenset(market_object_ids)
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    # 二代是裸数组;留一个 {"objects": [...]} 的包装形式给三代自己用
    entries = raw if isinstance(raw, list) else raw.get("objects", [])

    objects: list[TradeObject] = []
    for entry in entries:
        obj = from_entry(entry)
        if obj.object_id in quote_only:
            obj = TradeObject(**{**obj.__dict__, "kind": KIND_QUOTE_ONLY})
        objects.append(obj)

    catalog = Catalog(objects)
    logger.info(
        "载入标的清单:共 %d 个(可交易 %d,行情对象 %d,宏观对象 %d)",
        len(catalog),
        len(catalog.tradable),
        len(catalog.quote_only),
        len(catalog.macro),
    )
    return catalog


# ---------------------------------------------------------------------------
#  增删改的入口校验(契约 1.2.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftFailure:
    code: str
    message: str


def _macro_spec(draft: Mapping[str, object]) -> "macro_mod.MacroSpec | None":
    """从草稿里认出这是哪个宏观对象。``object_id`` 优先,其次拿 ``symbol`` 对 ``short``。

    两条路都留着,是因为两个调用点形状不同:界面「修改」会把整条记录
    (含 ``object_id``)传回来,而「新增」时前端只有一个代码输入框。
    """
    object_id = str(draft.get("object_id") or "").strip()
    if object_id:
        return macro_mod.MACRO_SPECS.get(object_id)
    symbol = str(draft.get("symbol") or "").strip().upper()
    for spec in macro_mod.MACRO_SPECS.values():
        if spec.short == symbol:
            return spec
    return None


def _validate_macro_draft(
    draft: Mapping[str, object], *, existing: Catalog | None
) -> tuple[TradeObject | None, tuple[DraftFailure, ...]]:
    """宏观对象的校验。**只能从符号表里挑,不能自己编。**

    和沪深证券那条路的根本区别:沪深证券填个代码就能采,采不到是数据源的事;
    宏观对象的采集参数(新浪符号、字段布局、历史接口)全在 ``macro.MACRO_SPECS``
    里写死。清单里放一条表里没有的,采集层每轮都会失败,而失败原因是
    "不在符号表里"——那种东西不该等到运行时才说,提交的时候就该拦。

    **名称和类别不取草稿里的值,取符号表的。** 它们是采集参数的一部分
    (``category`` 决定界面怎么分组、``name`` 要和日志对得上),让人在界面上
    随手改成别的,只会造出一条和代码对不上的记录。
    """
    spec = _macro_spec(draft)
    if spec is None:
        return None, (DraftFailure(
            "UNKNOWN_MACRO",
            "宏观对象只能从已支持的里面挑,不能自己填。可选:"
            + "、".join(f"{s.object_id}({s.name})" for s in macro_mod.MACRO_SPECS.values()),
        ),)

    if existing is not None and existing.get(spec.object_id) is not None:
        return None, (DraftFailure("DUPLICATE", f"{spec.object_id} 已在清单里"),)

    return (
        TradeObject(
            object_id=spec.object_id,
            market=MACRO_MARKET,
            symbol=spec.short,
            name=spec.name,
            kind=KIND_MACRO,
            asset_type=spec.category,
            # 一手股数对宏观对象没有意义。**填 0 而不是 100**——100 是个看着
            # 正常的数,万一哪天有人把宏观对象喂进整手校验,0 会立刻炸出来,
            # 100 会静悄悄通过。
            lot_size=0,
        ),
        (),
    )


def validate_draft(
    draft: Mapping[str, object], *, existing: Catalog | None = None
) -> tuple[TradeObject | None, tuple[DraftFailure, ...]]:
    """校验前端提交的新增/修改。

    可写字段只有五个(契约 1.2.1),其余都是采集来的,前端提交了也不采纳。
    与 `guards.validate()` 同一个取向:**一次跑完,收集全部失败原因**,
    不是遇到第一条就返回——让人一次看全比试错三轮有用。

    ⚠️ **类型决定按哪套规则校验。** 宏观对象没有 SH/SZ,代码也不是六位数字,
    拿沪深那套规则去校验它必然全红。类型写错(比如打成「宏观」)时**不走
    宏观分支**,而是落回沪深那套,让 ``BAD_KIND`` 和其余问题一起报出来
    ——早退一条只报「类型不对」,人改完类型还得再试一轮。
    """
    kind = str(draft.get("类型") or "").strip()
    if kind == KIND_MACRO:
        return _validate_macro_draft(draft, existing=existing)

    failures: list[DraftFailure] = []

    market = str(draft.get("market") or "").strip().upper()
    symbol = str(draft.get("symbol") or "").strip()
    name = str(draft.get("名称") or "").strip()
    asset_type = str(draft.get("资产类型") or "").strip()

    if market not in VALID_MARKETS:
        failures.append(DraftFailure("BAD_MARKET", f"市场只能是 SH 或 SZ,收到 {market!r}"))
    if not symbol or not symbol.isdigit():
        failures.append(DraftFailure("BAD_SYMBOL", f"证券代码必须是纯数字,收到 {symbol!r}"))
    if not name:
        failures.append(DraftFailure("NO_NAME", "名称必填——它就是为了让界面上不出现裸代码"))
    if kind not in VALID_KINDS:
        failures.append(DraftFailure(
            "BAD_KIND",
            f"类型只能是「{KIND_TRADABLE}」「{KIND_QUOTE_ONLY}」或「{KIND_MACRO}」,收到 {kind!r}",
        ))
    if asset_type not in VALID_ASSET_TYPES:
        failures.append(
            DraftFailure("BAD_ASSET_TYPE", f"资产类型只能是 ETF 或 股票,收到 {asset_type!r}")
        )

    if failures:
        return None, tuple(failures)

    object_id = make_object_id(market, symbol)
    if existing is not None and existing.get(object_id) is not None:
        failures.append(DraftFailure("DUPLICATE", f"{object_id} 已在清单里"))
        return None, tuple(failures)

    return (
        TradeObject(
            object_id=object_id,
            market=market,
            symbol=symbol,
            name=name,
            kind=kind,
            asset_type=asset_type,
            # 一手股数不由前端填:它跟着资产类型走,作为标的元数据统一维护
            lot_size=100,
        ),
        (),
    )
