"""驱动层(碰) —— 唯一持有时钟的模块。

## 为什么只有这里能看表

``scheduler`` 算得出六个时点、算得出该睡到几点,但它**不睡也不读表**;
``context.build_round`` 要一个 ``generated_at`` 参数,故意不自己取当前时间;
``guards.ValidationContext`` 也要求把 ``now`` 传进来。

二代就是因为每个标的各取一次 ``now_iso()``,七个值互不相同,把前缀缓存
从第一个字段就打断了。时钟散在各处的代价不止是缓存:自检没法复现"某一刻"、
同一轮里两处时间对不上、跨零点行为无法测试。

所以规矩是:**时间从这里进入系统,一轮只取一次,往下全靠参数传递。**

## 一轮的形状

    采集 → 拼上下文 → N 次模型调用 → 类型规范化 → 执行 → 组装 → 归档

前四步的"怎么算"都在别的模块里,而且都是纯函数。本模块只负责**按顺序把它们
串起来,并且在中间任何一步出事的时候把事情记下来继续往下走**。

## 一个标的失败,不拖垮一轮

七个标的里有一个模型答歪了,剩下六个的判断仍然有效。所以逐个 try,失败的
记进 ``本轮问题``(契约 1.1),**照样归档**。

只要上下文拼出来了,这一轮就一定有一份归档,哪怕七个全失败。理由是
「错过的时点永不补跑」——不补跑的前提是这一轮发生了什么有据可查,
否则事后看到的只是一个空洞。

## 采集层还没有

``DataSource`` 是个 Protocol,真正的实现要等 ``collect.py``。现在默认挂的是
``MissingDataSource``,调用它会明确地失败,而不是返回一份空数据装作跑过。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Protocol, Sequence

from . import SYSTEM_NAME, __version__, archive, context, execution, guards, history, llm, model, prompts
from . import scheduler, state
from . import runmode
from .catalog import Catalog
from .prompts import HISTORY_NOTE

logger = logging.getLogger("zhixing.runner")


#: 这些「本轮问题」**不算这一轮失败**——它们是已知的、由配置决定的缺项,
#: 不是"系统停摆了"。
#:
#: 目前只有一条:``ACCOUNT_UNAVAILABLE``(券商没配)。这不是新规矩,
#: ``daemon.preflight`` 里早就写着"券商没配**不拦**这一轮:没有账户照样
#: 能出判断,只是出不了指令"。但 ``_record`` 原来用的是 ``ok = not 问题``,
#: 于是那句话只兑现了一半:轮次确实照跑、归档确实照写,可运行事实里
#: **每一轮都记成失败**。
#:
#: 2026-08-20 第一轮实测:7 条判断、归档落盘、零异常,而状态页显示
#: 「上一轮成功时间 None、连续失败轮数 1」。照这样下去那个数字每天涨六,
#: 「最近失败原因」永远挂着一句"券商登录没配全"——而契约 1.4 那三个字段
#: 存在的全部意义,是回答"系统停没停摆、停了多久"。一个只会涨的计数器
#: 回答不了任何问题,和 ``_record_blocked`` 注释里说的是同一件事。
#:
#: ⚠️ **券商没配这件事本身并没有被藏起来**,它在「数据源」那一栏里,
#: 而且是算出来的(``collect.describe_source``:"账户:未配置,本轮不会有
#: 账户数据(缺:…)")。缺配置属于状态,不属于失败原因——放对地方而已。
#: ⚠️ **这里只放「本来就没有」,不放「本来该有却没了」。**
#: 券商登录失败(``collect.ACCOUNT_LOGIN_FAILED``)和查账户失败
#: (``ACCOUNT_QUERY_FAILED``)都**不在**这里,它们算这一轮失败。
#: 拆开之前三者共用 ``ACCOUNT_UNAVAILABLE``:2026-08-21 有两轮验证码
#: 连试三次都没登进去、整轮拿不到账户,而运行事实里记的是成功、界面上
#: 是一行灰的「已知缺项」——和"券商还没配"一模一样。一个码同时表示
#: 「没建」和「坏了」,等于把这两件事的区别抹掉。
KNOWN_ABSENCES: Final[frozenset[str]] = frozenset({"ACCOUNT_UNAVAILABLE"})


def round_failure(
    问题: Sequence[Mapping[str, Any]], judgments: Mapping[str, Any]
) -> str | None:
    """这一轮算不算失败。**不算就返回 None**,算就返回该显示的那句话。

    两条,都对着"这一轮有没有产出它该产出的东西"来判,不是数问题条数:

    1. **一条判断都没出** —— 无论问题列表长什么样,这一轮是白跑的;
    2. 出了判断,但还有 ``KNOWN_ABSENCES`` 之外的问题 —— 那是真出了岔子。

    第 2 条里 ``QUOTE_UNAVAILABLE`` 仍然算失败,是有意的:少一个标的的行情
    意味着那个标的这一轮**没有被判断过**,人应该看见。它是偶发的,下一轮
    干净了计数就归零;而缺配置是常态,不归零。
    """
    if not judgments:
        if 问题:
            return str(问题[0].get("message", ""))
        return "本轮没有产出任何判断。"
    for p in 问题:
        if str(p.get("code", "")) not in KNOWN_ABSENCES:
            return str(p.get("message", ""))
    return None


class RunnerError(RuntimeError):
    """一轮没能开始。**已经开始的轮次不抛这个**,它们走 ``本轮问题``。"""


# ---------------------------------------------------------------------------
#  数据来源
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundInput:
    """一轮要用到的全部输入。**由采集层产出,本模块只消费。**

    前四项直接进 ``context.build_shared()`` 的共享段,所以它们对本轮全部
    标的完全相同——这是前缀缓存能命中的前提,不是巧合。
    """

    读取范围: Any
    市场数据列表: Any
    账户交易流水表: Any
    #: ``{object_id: 该标的自己的数据}``。每份里**只有它自己**。
    per_object: Mapping[str, Any]
    data_window: Mapping[str, str]
    #: 采集时的账户和标的快照。为上下文与历史兼容保留;本地下单风控拆除后,
    #: 指令规范化不再读取这两个字段。
    account: guards.AccountSnapshot | None = None
    objects: Mapping[str, guards.ObjectSnapshot] = field(default_factory=dict)
    #: 采集阶段发生的问题,**原样进归档的 ``问题`` 列表**。
    #:
    #: 采集层做的取舍(某个标的没采到行情、账户没登上)会直接改变这一轮
    #: 的形状:少一条判断、一条指令都没有。没有这个字段,那些取舍就只
    #: 存在于日志里,而归档才是事实来源——"这轮为什么只判断了 5 个标的"
    #: 必须能从归档本身答出来,不能靠去翻日志。
    problems: tuple[Mapping[str, Any], ...] = ()


class DataSource(Protocol):
    """去哪儿拿这一轮的数据。等 ``collect.py`` 来实现。"""

    def collect(self, *, now: datetime, catalog: Catalog) -> RoundInput:
        ...


class MissingDataSource:
    """占位实现。**明确地失败,不返回空数据装作跑过。**"""

    def collect(self, *, now: datetime, catalog: Catalog) -> RoundInput:
        raise RunnerError(
            "采集层尚未接入(collect.py 还没写),这一轮拿不到数据。"
            "这不是失败,是这项能力还没有。"
        )


# ---------------------------------------------------------------------------
#  组装(算)
# ---------------------------------------------------------------------------


def make_strategy_id(now: datetime) -> str:
    """归档主键。形如 ``20260817-093000``,和契约 2 的路径示例一致。"""
    return f"{now:%Y%m%d-%H%M%S}"


def make_instruction_code(strategy_id: str, object_id: str, action: str) -> str:
    """指令的幂等码。**由确定的三样东西算出,不是随机数。**

    同一轮、同一标的、同一动作,重跑组装得到同一个码。执行结果、归档指令和
    委托流水靠它关联;本地重复拦截已按使用者要求拆除。
    """
    return f"{strategy_id}-{object_id}-{action}"


def assemble_payload(
    *,
    strategy_id: str,
    generated_at: datetime,
    target: model.ModelTarget,
    judgments: Mapping[str, model.Judgment],
    names: Mapping[str, str],
    reports: Mapping[str, guards.GuardReport],
    batch: execution.BatchResult,
    usages: Mapping[str, model.ModelUsage],
    problems: tuple[Mapping[str, Any], ...],
    data_window: Mapping[str, str],
    context_text: str,
    context_digest: str,
) -> dict[str, Any]:
    """把一轮的产物拼成一份归档。**纯函数,不碰磁盘、不看表。**

    摘出来单独写的理由:这是最容易在字段名上出错的一段,而字段名对不上
    是前端的直接损坏。纯函数意味着自检可以不联网、不落盘地把它整个验一遍。
    """
    对象判断: list[dict[str, Any]] = []
    指令: list[dict[str, Any]] = []
    执行记录 = {record.instruction_code: record for record in batch.records}

    for object_id, judgment in judgments.items():
        对象判断.append(judgment.as_entry(名称=names.get(object_id, object_id)))

        raw = judgment.指令
        if raw is None:
            continue

        report = reports.get(object_id)
        通过 = report is not None and report.ok
        instruction_code = make_instruction_code(strategy_id, object_id, raw.action)
        record = 执行记录.get(instruction_code)
        状态 = "rejected"
        wtbh = raw.wtbh
        if record is not None:
            # 结果不明按“可能已提交”处置,绝不能回到 pending 后再次执行。
            if record.outcome in {
                execution.Outcome.SUBMITTED,
                execution.Outcome.SUBMITTED_UNKNOWN,
            }:
                状态 = "submitted"
            else:
                状态 = "pending"
            wtbh = record.wtbh or wtbh

        item: dict[str, Any] = {
            "instruction_code": instruction_code,
            "action": raw.action,
            "market": object_id.split("_")[0] if "_" in object_id else "",
            "symbol": object_id.split("_")[-1],
            "name": names.get(object_id, object_id),
            "qty": raw.qty,
            "limit_price": raw.limit_price,
            "wtbh": wtbh,
            "理由": raw.reason,
            "风险提示": raw.risk_note,
            "状态": 状态,
            # 契约 1.1:状态 = rejected 时必填,其余状态为 []
            "拦截原因": [] if 通过 else [
                {"code": f.code, "message": f.message}
                for f in (report.failures if report else ())
            ] or [{"code": "NOT_NORMALIZED", "message": "这条指令没有完成类型规范化。"}],
        }
        if record is not None:
            item["执行结果"] = execution.record_entry(record)
        else:
            item["执行结果"] = {
                "outcome": "blocked",
                "submitted_unknown": False,
                "message": "指令字段无法完成类型规范化,未进入执行层",
            }
        指令.append(item)

    return {
        "strategy_id": strategy_id,
        "system_name": SYSTEM_NAME,
        "app_version": __version__,
        "生成时间": generated_at.isoformat(),

        "总体判断": _overall(对象判断, 指令, problems),
        "风险控制": {
            "状态": "本地下单风控已按使用者要求全部拆除",
            "说明": "交易时段、交易日、标的类型、重复、持仓、资金、整手、价格、涨跌停、金额与偏离均不在本地拦截",
            "禁止执行条件": list(_forbidden()),
        },
        "交易对象判断": 对象判断,
        "待执行指令": 指令,

        "model": target.name,
        "llm_provider": target.provider,
        "model_usage": [u.as_entry() for u in usages.values()],

        "本轮问题": [dict(p) for p in problems],

        "data_window": dict(data_window),
        "context_digest": context_digest,
        "context": context_text,
    }


def _overall(
    判断: list[dict[str, Any]], 指令: list[dict[str, Any]], problems: tuple[Any, ...]
) -> str:
    """一句话结论。**不夸大**:没跑成的部分要说出来。

    ⚠️ **问题不等于"标的没跑成"。** 采集层接进来之后,``问题`` 里混着两种
    东西:挂在某个标的上的(那个标的确实没跑成),和整轮性的(账户没登上、
    日历答不出来——它们的 ``object_id`` 是空的)。

    原来这里把两者一律念成"另有 N 个标的本轮未跑成",于是一次账户登录失败
    会被说成"1 个标的没跑成"——而实际上标的一个没少,少的是全部指令。
    **这句话是给人看的第一行**,它说错了,人对这一轮的第一印象就是错的。
    """
    if not 判断:
        return f"本轮未产出任何判断,共 {len(problems)} 处问题。"
    动作 = sum(1 for j in 判断 if j["操作"] != "hold")
    句 = f"{len(判断)} 个标的产出判断,其中 {动作} 个建议动作,待执行指令 {len(指令)} 条。"

    挂在标的上的 = {
        oid for p in problems
        if (oid := str((p or {}).get("object_id") or "").strip())
    }
    整轮性的 = len(problems) - sum(
        1 for p in problems if str((p or {}).get("object_id") or "").strip()
    )
    if 挂在标的上的:
        句 += f"另有 {len(挂在标的上的)} 个标的本轮未跑成。"
    if 整轮性的:
        句 += f"另有 {整轮性的} 处整轮性问题(不针对某个标的)。"
    return 句


def _forbidden() -> tuple[str, ...]:
    """仍保留的执行层结构约束。**从代码现状读出来,不是风控规则。**

    手写的话它会和代码分叉——二代的缺陷 6 就是一句承诺了某项校验的注释,
    而那项校验并不存在。
    """
    条 = [
        "数量或限价无法转换为券商所需类型时没有可提交参数",
        "无人值守开关关闭时,自动指令只留痕不提交",
        "SIMULATION 永不触达券商",
        "写操作绝不自动重试",
    ]
    if not runmode.live_trading_allowed():
        条.insert(0, "验证锁生效期间,一切真实下单一律降级为 dry_run")
    return tuple(条)


# ---------------------------------------------------------------------------
#  跑一轮(碰)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundResult:
    """一轮跑完的结果。``path`` 为 ``None`` 表示连上下文都没拼出来。"""

    strategy_id: str
    path: Path | None
    判断数: int
    指令数: int
    已提交数: int
    结果不明数: int
    问题: tuple[Mapping[str, Any], ...]

    @property
    def ok(self) -> bool:
        if self.path is None or self.判断数 == 0:
            return False
        return not any(str(p.get("code") or "") not in KNOWN_ABSENCES for p in self.问题)


@dataclass
class Runner:
    """把一轮串起来。**本模块唯一读表的地方是 ``self.clock``。**"""

    store: state.Store
    archive_root: Path
    caller: llm.ModelCaller
    target: model.ModelTarget
    source: DataSource = field(default_factory=MissingDataSource)
    #: 返回采集器本轮已经建立好的券商适配器。None 表示本轮没有可用会话。
    #: 只有无人值守已开启且不是 SIMULATION 时才会调用。
    broker_provider: Callable[[], execution.BrokerAdapter | None] | None = None
    authorization_kind: execution.AuthorizationKind = execution.AuthorizationKind.UNATTENDED
    #: 时钟。注进来是为了自检能指定"现在是几点几分",不用等。
    clock: Callable[[], datetime] = datetime.now
    system_prompt: str = prompts.SYSTEM_PROMPT
    output_spec: str = prompts.OUTPUT_SPEC

    # -- 一轮 -------------------------------------------------------------

    def _带历史(self, per_object: Mapping[str, Any]) -> dict[str, Any]:
        """给每个标的的数据挂上「我最近 5 个交易日说过什么」。

        放在**每个标的自己的段落里**,不放共享段:历史是逐标的的,塞进共享段
        等于让模型判断 A 的时候看见对 B 的判断,那正是 ``build_round`` 特意
        避免的锚定。

        读不出来不拦轮次。历史是参考,不是判断的前提——为它作废一轮,
        代价和它的分量对不上。
        """
        try:
            史 = history.recent(
                self.archive_root, self.store.root, object_ids=per_object.keys()
            )
        except OSError as exc:
            logger.warning("历史判断读不出来,本轮不带历史参考:%s", exc)
            return dict(per_object)

        出: dict[str, Any] = {}
        for oid, payload in per_object.items():
            条目 = 史.get(oid) or ()
            带 = dict(payload) if isinstance(payload, Mapping) else {"数据": payload}
            带["历史判断"] = {
                "说明": HISTORY_NOTE,
                "条数": len(条目),
                "记录": list(条目),
            }
            出[oid] = 带
        有史 = sum(1 for v in 史.values() if v)
        logger.info("历史判断:%d/%d 个标的有记录", 有史, len(per_object))
        return 出

    def run_round(self) -> RoundResult:
        """跑一轮。**整轮只取一次时间。**"""
        now = self.clock()
        strategy_id = make_strategy_id(now)
        catalog = self.store.catalog()

        data = self.source.collect(now=now, catalog=catalog)

        # 采集阶段的问题**立刻并进本轮问题**,不等到最后。放在最前面是因为
        # 它们解释了后面所有的"少了什么":少一个标的的判断、一条指令都没有,
        # 根因都在这儿。排在模型调用失败之后的话,人得从下往上读才明白。
        问题: list[dict[str, Any]] = [dict(p) for p in data.problems]

        shared = context.build_shared(
            输出要求=self.output_spec,
            读取范围=data.读取范围,
            市场数据列表=data.市场数据列表,
            账户交易流水表=data.账户交易流水表,
        )
        prompts_ = context.build_round(shared, self._带历史(data.per_object),
                                       generated_at=now)
        plan = context.dispatch_plan(prompts_)
        顺序 = ((plan.warmup,) if plan.warmup else ()) + plan.rest

        judgments: dict[str, model.Judgment] = {}
        usages: dict[str, model.ModelUsage] = {}

        for prompt in 顺序:
            oid = prompt.object_id
            try:
                reply = self.caller.call(
                    self.target,
                    model.build_request(
                        self.target,
                        system_prompt=self.system_prompt,
                        user_text=prompt.text,
                    ),
                    object_id=oid,
                )
            except llm.LlmError as exc:
                # 调用没成功就没有 usage,不编一个 0 顶上——
                # 那会让"这轮花了多少"和"这轮问了几次"对不上。
                问题.append({"object_id": oid, "code": "CALL_FAILED", "message": str(exc)})
                logger.warning("%s 调用失败:%s", oid, exc)
                continue

            usages[oid] = reply.usage

            judgment, bad = model.parse_judgment(reply.text, expect_object_id=oid)
            if judgment is None:
                问题.extend(
                    {"object_id": oid, "code": p.code, "message": p.message} for p in bad
                )
                continue
            judgments[oid] = judgment

        reports, 整轮问题 = self._validate(strategy_id, judgments, catalog, now, data)
        问题.extend(整轮问题)
        names = {oid: self._name(catalog, oid) for oid in judgments}

        auth = execution.Authorization(
            kind=self.authorization_kind,
            actor="scheduler",
            source=(
                f"simulation:{now:%H:%M}"
                if self.authorization_kind is execution.AuthorizationKind.SIMULATION
                else f"scheduler:{now:%H:%M}"
            ),
            issued_at=now,
        )
        broker: execution.BrokerAdapter | None = None
        should_resolve_broker = (
            self.authorization_kind is not execution.AuthorizationKind.SIMULATION
            and runmode.unattended_state().enabled
            and self.broker_provider is not None
        )
        if should_resolve_broker:
            try:
                broker = self.broker_provider()
            except Exception as exc:  # noqa: BLE001 - broker=None 会形成可见执行记录
                logger.error("取得券商适配器失败,异常类型=%s", exc.__class__.__name__)
        batch = execution.submit_reports(
            list(reports.values()), auth, broker=broker, now=now
        )

        payload = assemble_payload(
            strategy_id=strategy_id,
            generated_at=now,
            target=self.target,
            judgments=judgments,
            names=names,
            reports=reports,
            batch=batch,
            usages=usages,
            problems=tuple(问题),
            data_window=data.data_window,
            # `context` 存**共享段**,不是某一个标的的完整文本。
            #
            # 七份完整上下文只有结尾的「交易对象数据」不同,存一份完整的
            # 等于随便挑一个标的当代表,存七份则是七倍体积去换那点尾巴——
            # 而那点尾巴已经在每条判断的 `依据数据` 里了。
            #
            # ⚠️ `context_digest` 用的是共享段的哈希。契约 2.2 靠这个值
            # 配对两代系统的同一轮,而**二代用的是什么算法、算的哪一段,
            # 目前不知道**(要上服务器读二代代码)。核实之前,跨代对比
            # 可能一条都配不上——这不是 bug,是这项还没对齐。
            context_text=shared.rendered,
            context_digest=shared.digest,
        )
        path = archive.write_run(payload, root=self.archive_root)

        失败原因 = round_failure(问题, judgments)
        self._record(now, ok=失败原因 is None, reason=失败原因)
        logger.info(
            "第 %s 轮归档 %s(判断 %d / 指令 %d / 已提交 %d / 结果不明 %d / 问题 %d)",
            strategy_id, path.name,
            len(payload["交易对象判断"]), len(payload["待执行指令"]),
            batch.submitted_count, batch.submitted_unknown_count, len(问题),
        )
        return RoundResult(
            strategy_id=strategy_id,
            path=path,
            判断数=len(payload["交易对象判断"]),
            指令数=len(payload["待执行指令"]),
            已提交数=batch.submitted_count,
            结果不明数=batch.submitted_unknown_count,
            问题=tuple(问题),
        )

    # -- 类型规范化 -------------------------------------------------------

    def _validate(
        self,
        strategy_id: str,
        judgments: Mapping[str, model.Judgment],
        catalog: Catalog,
        now: datetime,
        data: RoundInput,
    ) -> tuple[dict[str, guards.GuardReport], list[dict[str, Any]]]:
        """逐条走 ``ValidatedOrder`` 的唯一构造通路。

        本地下单风控已经全部拆除,因此账户、行情快照、交易日历和既有指令
        都不参与阻断。这里只把模型字段规范化成执行层所需类型。
        """

        ctx = guards.ValidationContext(
            account=data.account, objects=dict(data.objects), now=now
        )

        reports: dict[str, guards.GuardReport] = {}

        for object_id, judgment in judgments.items():
            raw = judgment.指令
            if raw is None:
                continue
            code = make_instruction_code(strategy_id, object_id, raw.action)
            reports[object_id] = guards.validate(
                guards.ProposedOrder(
                    instruction_code=code,
                    action=raw.action,
                    market=object_id.split("_")[0] if "_" in object_id else "",
                    symbol=object_id.split("_")[-1],
                    name=self._name(catalog, object_id),
                    qty=raw.qty,
                    limit_price=raw.limit_price,
                    wtbh=raw.wtbh,
                    reason=raw.reason,
                    risk_note=raw.risk_note,
                ),
                ctx,
            )

        return reports, []

    @staticmethod
    def _name(catalog: Catalog, object_id: str) -> str:
        annotated, _ = catalog.annotate([{"object_id": object_id}])
        return str(annotated[0].get("名称", object_id)) if annotated else object_id

    # -- 留痕 -------------------------------------------------------------

    def _record(self, now: datetime, *, ok: bool, reason: str | None) -> None:
        facts = self.store.runtime()
        self.store.save_runtime(replace(
            facts,
            上一轮成功时间=now.isoformat() if ok else facts.上一轮成功时间,
            连续失败轮数=0 if ok else facts.连续失败轮数 + 1,
            最近失败原因=None if ok else reason,
        ))

    # -- 排期 -------------------------------------------------------------

    def fired_today(self, day: date) -> frozenset[int]:
        """今天已经跑过哪几轮。**从归档重建,不另存一份状态。**

        归档是事实来源;再存一份"今天跑过哪几轮"就有了第二份事实,
        而两份事实迟早会不一致。重启之后照样算得出来。
        """
        config = self.store.schedule()
        plan = scheduler.plan_day(day, config=config)
        跑过: set[int] = set()
        prefix = f"{day:%Y%m%d}-"
        for path in archive.iter_paths(self.archive_root, month=f"{day:%Y-%m}"):
            if not path.stem.startswith(prefix):
                continue
            fired_at = datetime.strptime(path.stem, "%Y%m%d-%H%M%S")
            # 落到窗口里的那个时点算跑过。落在任何窗口外的说明是手工补的,不认领。
            for slot in plan.slots:
                if slot.fire_at <= fired_at <= slot.deadline:
                    跑过.add(slot.index)
                    break
        return frozenset(跑过)

    def due(self) -> scheduler.Slot | None:
        """此刻该不该跑。**不跑就是不跑,不补。**"""
        now = self.clock()
        plan = scheduler.plan_day(now.date(), config=self.store.schedule())
        return scheduler.due_slot(plan, now=now, fired=self.fired_today(now.date()))

    def tick(self) -> RoundResult | None:
        """到点就跑一轮,没到点返回 ``None``。驱动循环调这个。"""
        slot = self.due()
        if slot is None:
            return None
        logger.info("%s 到点,开跑", slot.label)
        return self.run_round()


__all__ = [
    "RunnerError", "RoundInput", "DataSource", "MissingDataSource",
    "KNOWN_ABSENCES", "round_failure",
    "make_strategy_id", "make_instruction_code", "assemble_payload",
    "RoundResult", "Runner",
]
