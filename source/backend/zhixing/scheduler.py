"""调度 —— 决定"什么时候跑",以及"错过了算什么"。

## 这个模块不做什么

**它不睡觉,不起线程,不读系统时钟。**

排期是纯计算:给定日期和配置,算出今天六个时点分别在几点几分几秒;
给定当前时刻和已跑记录,算出现在该不该跑。真正的 sleep 循环是外面
薄薄一层驱动的事。

这样拆的理由很实际:调度逻辑里最容易错的是"进程重启后会怎样""错过了
会不会补跑""两个时点挤到一起会不会跑两遍"——这些全是时间相关的分支,
如果模块自己读 ``datetime.now()``,就只能靠等到那个时刻才能验证。
现在这些分支全部可以在自检里当场喂进去。

## 三条硬规矩

### 一、错过的时点**永不补跑**

进程 09:35 该跑那轮时是停的,11:20 起来了——不跑。

补跑会产出一份"标称 09:35、实际看的是 11:20 行情"的判断,归档里那个
``生成时间`` 就是假的。这正是三代要修的二代毛病:记录不能说谎。

错过不是静默跳过,是**记一条 ``错过`` 状态**,和被拦下的指令一样必须
在界面上看得见。

### 二、同时到期的时点只跑最晚的那个

极端情况下(窗口开得宽、进程断了一阵)可能有两个时点同时处于可触发
状态。这时只跑序号最大的,更早的那个标成 ``被取代``。

因为两轮都会基于同一份当前行情,跑两遍只是产出两份几乎一样的判断,
外加一倍的 token 账单。

⚠️ **但这条规矩会吃掉轮次,所以它不该被日常触发。** 只要
``有效窗口 + 抖动上限`` 超过最小的相邻时点间隔,前一轮的窗口就会伸进
后一轮里,于是前一轮每次都被顶掉——不报错、不算失败,一天六轮悄悄变五轮。
窗口取值的约束写在 ``ScheduleConfig.window_minutes`` 上,2026-08-21
把它从 30 分钟收到 20 分钟就是为了这个。
``被取代`` 应该是"进程断了一阵"的产物,不该是排期本身的产物。

### 三、抖动是确定的,不是随机的

见下面 ``_jitter_seconds``。

## 时点落在交易时段之外怎么办

允许。六个时点里本来就可能有盘前预读和盘后复盘。

那种时点跑出来的判断和指令照样归档。下单时段检查已按使用者要求拆除;
调度器只决定轮次何时启动,不会额外阻断模型产出的指令。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Final, Iterable, Sequence

from .guards import default_is_trading_day

logger = logging.getLogger("zhixing.scheduler")


#: 每个交易日的时点数量。写死是刻意的:界面上是六个固定输入框,
#: 契约 2 的 ``/api/settings/schedule`` 也按六个定义。
#: 要改数量得同时改契约、前端和这里,改不动正是想要的效果。
SLOT_COUNT: Final[int] = 6


# ---------------------------------------------------------------------------
#  状态
# ---------------------------------------------------------------------------

#: 还没到点
PENDING: Final[str] = "待触发"
#: 到点了、没跑过、还在窗口内
DUE: Final[str] = "可触发"
#: 今天已经跑过
FIRED: Final[str] = "已触发"
#: 过了有效窗口还没跑 —— **不补跑**
MISSED: Final[str] = "错过"
#: 同时可触发,让位给更晚的时点
SUPERSEDED: Final[str] = "被取代"
#: 当天非交易日
NOT_TRADING_DAY: Final[str] = "非交易日"


# ---------------------------------------------------------------------------
#  配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleConfig:
    """调度配置。不可变——改它要走 ``apply_config``,强制留痕。"""

    #: 六个时点,严格递增
    times: tuple[time, ...]
    #: 抖动上限(秒)。实际偏移在 [0, 上限] 之间,**只向后不向前**。
    max_jitter_seconds: int = 180
    #: 有效窗口(分钟)。超过就是错过,不补跑。
    #:
    #: ⚠️ **这个值不能大于最小的相邻时点间隔**,还要给抖动留出余量,
    #: 也就是 ``window_minutes * 60 + max_jitter_seconds < 最小间隔秒数``。
    #: 否则前一轮的有效截止会越过后一轮的触发时刻,两轮同时可触发,
    #: 于是靠"同时到期只跑最晚的"那条规矩,**前一轮被标成 ``被取代``,
    #: 永远不会跑**——它不报错、不算失败,只是悄悄少了一轮。
    #:
    #: 2026-08-21 线上配的是 09:35 / 10:00 / 11:15 / 13:15 / 14:00 / 14:45,
    #: 最小间隔 25 分钟(09:35→10:00)。原来的 30 分钟窗口 + 180 秒抖动
    #: 最远能伸到时点后 33 分钟,越过下一轮 8 分钟——第一轮但凡晚跑一点,
    #: 第二轮就会把它顶掉。改成 20 分钟后最远伸到 23 分钟,离 25 分钟还差
    #: 2 分钟,不再相交。
    window_minutes: int = 20
    #: 抖动盐。改它可以换一套偏移分布而不动时点。
    #: **这不是密钥**,不要往里塞任何机密——它会进配置、进归档、进界面。
    jitter_salt: str = "zhixing"

    def as_text(self) -> tuple[str, ...]:
        """给界面和审计用的 HH:MM 列表。"""
        return tuple(t.strftime("%H:%M") for t in self.times)


def parse_times(raw: Sequence[str]) -> tuple[tuple[time, ...], list[str]]:
    """解析 HH:MM 列表。**一次报全部问题**,不是遇错即停。

    与 ``guards.validate`` / ``catalog.validate_draft`` /
    ``archive.validate_payload`` 同一个取向:人改一次配置就该看到全部毛病,
    而不是改一条冒一条。
    """
    problems: list[str] = []

    if len(raw) != SLOT_COUNT:
        problems.append(f"必须恰好 {SLOT_COUNT} 个时点,收到 {len(raw)} 个")

    parsed: list[tuple[int, time]] = []
    for i, item in enumerate(raw):
        text = (item or "").strip()
        if not text:
            problems.append(f"第 {i + 1} 个时点为空")
            continue
        try:
            parsed.append((i, time.fromisoformat(text)))
        except ValueError:
            problems.append(f"第 {i + 1} 个时点 {text!r} 不是合法的 HH:MM")

    # 连**原始序号**一起记下来。只留 time 值的话,中间有一条解析失败,后面
    # 所有条目的下标就和用户填的栏位错开了,报出来的"第 3 个"会指错栏。
    # 早先为了躲开这个问题,干脆在有任何解析失败时整个跳过重复与升序检查——
    # 结果是用户改完格式错误再提交,才发现还有个重复。那就是"改一条冒一条",
    # 正是本函数开头承诺不做的事。
    seen: dict[time, int] = {}
    for i, t in parsed:
        if t in seen:
            problems.append(
                f"第 {i + 1} 个时点 {t:%H:%M} 与第 {seen[t] + 1} 个重复"
            )
        else:
            seen[t] = i

    # 比较的是"解析成功的相邻两条"。中间那条没解析出来不影响结论:
    # 无论它填的是什么,前后这两条本身也必须是升序的。
    # 相等只报重复不报乱序,同一个毛病不报两遍。
    for (prev_i, prev_t), (cur_i, cur_t) in zip(parsed, parsed[1:]):
        if cur_t < prev_t:
            problems.append(
                f"第 {cur_i + 1} 个时点 {cur_t:%H:%M} 早于第 {prev_i + 1} 个 "
                f"{prev_t:%H:%M},时点必须按时间升序"
            )

    return tuple(t for _, t in parsed), problems


def validate_config(
    raw_times: Sequence[str],
    *,
    max_jitter_seconds: int = 180,
    window_minutes: int = 20,
    jitter_salt: str = "zhixing",
) -> tuple[ScheduleConfig | None, list[str]]:
    """校验一份待写入的配置。通过才返回配置对象,否则返回全部问题。"""
    times, problems = parse_times(raw_times)

    if max_jitter_seconds < 0:
        problems.append("抖动上限不能为负")
    if window_minutes <= 0:
        problems.append("有效窗口必须大于 0 分钟")
    if max_jitter_seconds >= window_minutes * 60:
        problems.append(
            f"抖动上限 {max_jitter_seconds} 秒不得达到有效窗口 "
            f"{window_minutes} 分钟——否则刚抖完就已经算错过"
        )

    if problems:
        return None, problems

    return (
        ScheduleConfig(
            times=times,
            max_jitter_seconds=max_jitter_seconds,
            window_minutes=window_minutes,
            jitter_salt=jitter_salt,
        ),
        [],
    )


@dataclass(frozen=True)
class ConfigChange:
    """一次调度配置变更的审计记录。"""

    before: tuple[str, ...]
    after: tuple[str, ...]
    changed_by: str
    changed_at: datetime
    reason: str


#: 配置审计落盘钩子。默认只写日志,接上归档层后替换。
#: 与 ``runmode.audit_sink`` 同一个套路:调度层不依赖存储层。
config_audit_sink: Callable[[ConfigChange], None] = lambda change: logger.info(
    "调度配置变更 %s -> %s,操作者=%s,原因=%s",
    change.before,
    change.after,
    change.changed_by,
    change.reason,
)


def apply_config(
    raw_times: Sequence[str],
    *,
    current: ScheduleConfig,
    changed_by: str,
    reason: str,
    now: datetime,
) -> tuple[ScheduleConfig, ConfigChange]:
    """改调度时点。``changed_by`` 和 ``reason`` 一个都不能省。

    和 ``runmode.set_unattended`` 一样:配置本身也是要留痕的事实。
    "那天为什么把 14:30 挪到 14:50" 是事后最常问的问题之一。

    没通过校验就抛 ``ValueError``,问题一次报全。
    """
    if not reason or not reason.strip():
        raise ValueError("变更调度时点必须写明原因")
    if not changed_by or not changed_by.strip():
        raise ValueError("变更调度时点必须标明操作者")

    config, problems = validate_config(
        raw_times,
        max_jitter_seconds=current.max_jitter_seconds,
        window_minutes=current.window_minutes,
        jitter_salt=current.jitter_salt,
    )
    if config is None:
        raise ValueError("调度时点不合法:" + ";".join(problems))

    change = ConfigChange(
        before=current.as_text(),
        after=config.as_text(),
        changed_by=changed_by,
        changed_at=now,
        reason=reason,
    )
    config_audit_sink(change)
    return config, change


#: 默认时点。二代跑的也是六轮,这里先按盘前 / 早盘 / 午前 / 午后 / 尾盘 / 盘后铺开,
#: 具体数值等基线对比出结果后再定——所以它只是默认值,不是结论。
DEFAULT_CONFIG: Final[ScheduleConfig] = ScheduleConfig(
    times=(
        time(9, 15),
        time(9, 45),
        time(11, 0),
        time(13, 30),
        time(14, 40),
        time(15, 30),
    )
)


# ---------------------------------------------------------------------------
#  排期
# ---------------------------------------------------------------------------


def _jitter_seconds(day: date, index: int, config: ScheduleConfig) -> int:
    """确定性抖动:同一天同一个时点,算多少遍都是同一个偏移。

    为什么不用 ``random``:事后复盘时"那天为什么是 09:47:23 跑的"必须能
    回答。真随机答不上来,除非把偏移量也存下来——那就多了一份要维护的
    状态,而且状态和事实一旦不一致就没法判断谁对。

    用 ``sha256(日期 + 序号 + 盐)`` 取模,偏移量是配置的**函数**而不是
    副产品,任何时候重算都能得到同一个值。归档里存不存它都不影响复盘。

    **只向后偏,不向前。** 所以配置写 09:45,这一轮绝不会早于 09:45 发生。
    向前抖会让盘前时点抖到开盘前更远的地方,或者让 09:30 那种卡着开盘的
    时点抖到开盘之前去,行为就不好讲了。
    """
    if config.max_jitter_seconds <= 0:
        return 0
    seed = f"{day.isoformat()}#{index}#{config.jitter_salt}"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (config.max_jitter_seconds + 1)


@dataclass(frozen=True)
class Slot:
    """当天某一轮的排期。"""

    index: int
    #: 配置里写的那个时刻,落到当天
    planned: datetime
    #: 加了抖动之后的实际触发时刻
    fire_at: datetime
    jitter_seconds: int
    #: 过了这个时刻还没跑就是错过,**不补跑**
    deadline: datetime

    @property
    def label(self) -> str:
        return f"第 {self.index + 1} 轮 {self.planned:%H:%M}"


@dataclass(frozen=True)
class DayPlan:
    """某一天的完整排期。"""

    day: date
    trading_day: bool
    slots: tuple[Slot, ...]
    config: ScheduleConfig


def plan_day(
    day: date,
    *,
    config: ScheduleConfig = DEFAULT_CONFIG,
    is_trading_day: Callable[[date], bool] = default_is_trading_day,
) -> DayPlan:
    """算出某一天六轮各自的触发时刻。

    非交易日照样把六个 Slot 算出来,只是 ``trading_day=False``。
    这样界面上"今天不跑,因为不是交易日"能说得具体,而不是空白一片。
    """
    slots = []
    window = timedelta(minutes=config.window_minutes)
    for i, t in enumerate(config.times):
        planned = datetime.combine(day, t)
        offset = _jitter_seconds(day, i, config)
        fire_at = planned + timedelta(seconds=offset)
        slots.append(
            Slot(
                index=i,
                planned=planned,
                fire_at=fire_at,
                jitter_seconds=offset,
                deadline=fire_at + window,
            )
        )
    return DayPlan(
        day=day,
        trading_day=is_trading_day(day),
        slots=tuple(slots),
        config=config,
    )


# ---------------------------------------------------------------------------
#  判定
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotState:
    """一个时点此刻的状态。**六个时点每个都有一条**,没有隐身的。"""

    slot: Slot
    status: str
    reason: str


def evaluate(
    plan: DayPlan,
    *,
    now: datetime,
    fired: Iterable[int] = (),
) -> tuple[SlotState, ...]:
    """给出当天六个时点此刻各自的状态。

    :param fired: 今天已经跑过的时点序号。重启后从归档重建即可——
        归档里有当天每轮的记录,这个集合是**派生**的,不是第二份事实。
    """
    done = frozenset(fired)
    states: list[SlotState] = []

    for slot in plan.slots:
        if not plan.trading_day:
            states.append(SlotState(slot, NOT_TRADING_DAY, f"{plan.day} 非交易日"))
        elif slot.index in done:
            states.append(SlotState(slot, FIRED, "今日已跑过"))
        elif now < slot.fire_at:
            states.append(
                SlotState(slot, PENDING, f"未到 {slot.fire_at:%H:%M:%S}")
            )
        elif now > slot.deadline:
            states.append(
                SlotState(
                    slot,
                    MISSED,
                    f"已过有效窗口({slot.fire_at:%H:%M:%S} 起 "
                    f"{plan.config.window_minutes} 分钟),按规矩不补跑",
                )
            )
        else:
            states.append(SlotState(slot, DUE, "到点且在窗口内"))

    # 同时到期只跑最晚的一个,更早的让位。
    due_indexes = [s.slot.index for s in states if s.status == DUE]
    if len(due_indexes) > 1:
        winner = max(due_indexes)
        states = [
            SlotState(
                s.slot,
                SUPERSEDED,
                f"与第 {winner + 1} 轮同时到期,让位给更晚的时点"
                "(两轮基于同一份行情,跑两遍只多一份账单)",
            )
            if s.status == DUE and s.slot.index != winner
            else s
            for s in states
        ]

    return tuple(states)


def due_slot(
    plan: DayPlan, *, now: datetime, fired: Iterable[int] = ()
) -> Slot | None:
    """此刻该跑哪一轮。没有就返回 None。最多返回一个。"""
    for state in evaluate(plan, now=now, fired=fired):
        if state.status == DUE:
            return state.slot
    return None


# ---------------------------------------------------------------------------
#  时钟本身对不对
# ---------------------------------------------------------------------------

#: 市场所在时区相对 UTC 的偏移。**写死 +08:00 是对的**:这是上交所和深交所
#: 的开市时间,不随这套代码部署在哪台机器上而变。
MARKET_UTC_OFFSET: Final[timedelta] = timedelta(hours=8)


def clock_zone_problem(now: datetime) -> str | None:
    """本机时钟的时区对不对。**返回 None 表示对**,否则返回一句人话。

    ## 为什么这个检查非有不可

    这个模块开头第一句就是"**它不读系统时钟**"——时点全靠外面喂进来,
    所以自检能把任何时刻当场喂给它。这是好设计,但它有一个代价:

    **模块永远不会发现喂进来的那个"现在"来自一个错时区的时钟。**

    2026-08-20 就是这么撞上的:容器里没设 ``TZ``,基础镜像默认 UTC,而
    时点("09:15")是按北京时间配的。于是接口上显示"下次触发 09:16:46,
    未到"——而北京时间已经 10:43,前两轮早该跑完了。

    这种错**长得完全不像错**:没有异常、没有失败轮次、归档里干干净净,
    界面上每个字段都自洽。它只是把六轮整整推后八小时,全部落到收盘之后,
    然后一天一天地什么都不产出。真跑起来更糟——收盘后取到的行情会被
    归档成一份标称盘中的判断,而"记录不能说谎"是这套系统的头一条规矩。

    ⚠️ ``now`` 必须是 **aware** 的(``datetime.now().astimezone()``)。
    naive 的 datetime 不知道自己在哪个时区——**而这正是这个 bug 藏得住的
    原因**,所以这里不接受 naive,也不替调用方猜。
    """
    offset = now.utcoffset()
    if offset is None:
        return (
            "时钟没带时区信息(naive datetime),没法判断这台机器在哪个时区。"
            "调用方应该传 datetime.now().astimezone()。"
        )
    if offset == MARKET_UTC_OFFSET:
        return None

    # 差多少小时是**算出来的**,不写死"八小时"——换个时区部署时,写死的
    # 那个数字会变成又一句"承诺了一件没在查的事"的假话。
    shift = MARKET_UTC_OFFSET - offset
    hours = shift.total_seconds() / 3600
    def _tag(delta: timedelta) -> str:
        total = int(delta.total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return f"UTC{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"

    return (
        f"本机时区是 {_tag(offset)},而时点是按北京时间({_tag(MARKET_UTC_OFFSET)})"
        f"配置的,差 {hours:+.1f} 小时。照这个时钟跑,配在 09:15 的那一轮会在"
        f"北京时间 {(datetime.min + timedelta(hours=9, minutes=15) + shift).strftime('%H:%M')}"
        f" 才触发——六轮很可能全部落在收盘之后,而且看不出任何异常。"
        "容器里设 TZ=Asia/Shanghai 即可(基础镜像自带时区库,不用装东西)。"
    )


def next_wakeup(
    plan: DayPlan, *, now: datetime, fired: Iterable[int] = ()
) -> datetime | None:
    """驱动层该睡到什么时候。当天没有待触发的时点则返回 None。

    只回答"下一个待触发时点的触发时刻",不回答"睡多少秒"——后者要读
    当前时钟,那是驱动层的事。
    """
    upcoming = [
        s.slot.fire_at
        for s in evaluate(plan, now=now, fired=fired)
        if s.status == PENDING
    ]
    return min(upcoming) if upcoming else None


# ---------------------------------------------------------------------------
#  留痕
# ---------------------------------------------------------------------------


def slot_record(state: SlotState, plan: DayPlan, *, now: datetime) -> dict[str, object]:
    """把一个时点的状态变成可归档的记录。

    键名和归档层一致(中文、可读),值里**不含任何账号、金额、密钥**——
    调度层压根不接触这些东西。
    """
    return {
        "日期": plan.day.isoformat(),
        "轮次": state.slot.index + 1,
        "计划时刻": state.slot.planned.isoformat(),
        "触发时刻": state.slot.fire_at.isoformat(),
        "抖动秒数": state.slot.jitter_seconds,
        "有效截止": state.slot.deadline.isoformat(),
        "状态": state.status,
        "说明": state.reason,
        "记录时刻": now.isoformat(),
    }


def day_report(
    plan: DayPlan, *, now: datetime, fired: Iterable[int] = ()
) -> dict[str, object]:
    """当天调度全貌,给 ``/api/status`` 和运行页用。

    六个时点全在里面,**错过的和被取代的一样列出来**。
    "看起来什么都没发生"是二代最要命的表现形式之一。
    """
    states = evaluate(plan, now=now, fired=fired)
    counts: dict[str, int] = {}
    for s in states:
        counts[s.status] = counts.get(s.status, 0) + 1

    wakeup = next_wakeup(plan, now=now, fired=fired)
    return {
        "日期": plan.day.isoformat(),
        "是否交易日": plan.trading_day,
        "时点配置": list(plan.config.as_text()),
        "抖动上限秒": plan.config.max_jitter_seconds,
        "有效窗口分钟": plan.config.window_minutes,
        "下次触发": wakeup.isoformat() if wakeup else None,
        "状态计数": counts,
        "时点": [slot_record(s, plan, now=now) for s in states],
    }
