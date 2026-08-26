"""HTTP 接口层 —— 契约 docs/contracts.md 第二节的实现。

## 这个模块不做什么

**它不监听端口,不解析 HTTP,不认识框架。**

对外只有一个入口:``handle(app, Request) -> Response``。``Request`` 是
「方法 + 路径 + 查询参数 + 已解析的 body」,``Response`` 是「状态码 + 统一
响应包」。真正收发字节的是 ``serve.py`` 那一薄层。

理由和 ``scheduler.py`` 一样:接口层最容易错的地方是**取数与拼装**,
不是收发。拆开之后十五条路由全部可以在自检里当场喂进去跑一遍,不用起服务、
不用装框架、不用等端口。项目到今天为止零依赖,这一层也不该是第一个破例的。

将来要换成 FastAPI/Starlette,写一个三十行的适配器即可,本模块一行不动。

## 数据从哪儿来

**归档 JSON 是事实来源,数据库是派生索引**——这是全项目的第一条规矩,
所以列表、详情、对比、用量一律直接读归档目录,不需要数据库在线。

``index.list_runs()`` 返回的也是 ``archive.summarize()`` 的结果,所以
两条路径给出的**列表项形状本来就是同一个定义**,将来归档多到扫不动时
换成走索引,响应形状不会变。用量聚合则在 ``tests/smoke.py`` 里有一条
「两条路径同一答案」的对照检查,防止 SQL 和 Python 各算各的。

## 没有身份认证

**这一层不认人。** 没有登录、没有令牌、没有权限。所有留痕里的操作者都记
成 ``web``,意思是"有人从界面上做的",仅此而已。

所以 ``serve.py`` 默认只绑 ``127.0.0.1``。要放到公网上必须先解决认证,
在那之前它就不该被解决地暴露出去——这是个管钱的系统。

## 每一份响应都过机密扫描

``handle()`` 最外层对**整个响应体**跑一遍 ``archive.scan_for_secrets()``,
命中就把响应换成 500,并且只记路径不记值。

代价是详情接口那份三十多万字符的 ``context`` 每次都要扫一遍。单用户系统,
这点开销买的是"接口层结构上不可能漏出密钥",值。**不提供关闭开关**——
留个开关就等于把它降级成建议,而绕过它的那天正是最需要它的那天。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from . import SYSTEM_NAME, __version__, archive, catalog as catalog_mod
from . import execution, guards, runmode, scheduler, state
from . import captcha, model

logger = logging.getLogger("zhixing.api")


# ---------------------------------------------------------------------------
#  请求 / 响应
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """一次调用。**已经解析好**——本模块不碰原始字节。"""

    method: str
    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    #: 已解析的 JSON body。没有 body 时为 None。
    body: Any = None


@dataclass(frozen=True)
class Response:
    status: int
    payload: dict[str, Any]


def ok(data: Any) -> Response:
    """契约 2.1 的成功包。"""
    return Response(200, {"ok": True, "data": data})


def fail(
    status: int,
    code: str,
    message: str,
    *,
    problems: tuple[tuple[str, str], ...] = (),
) -> Response:
    """契约 2.1 的失败包。

    ``problems`` 非空时额外挂 ``error.问题[]``,把**全部**失败原因列出来。
    这是 ``guards.validate`` / ``catalog.validate_draft`` /
    ``scheduler.parse_times`` 一路下来的同一个取向:人改一次就该看到全部
    毛病,不是改一条冒一条。``message`` 仍然是完整的一句话,只认
    ``code`` + ``message`` 的老前端不会因此坏掉。
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if problems:
        error["问题"] = [{"code": c, "message": m} for c, m in problems]
    return Response(status, {"ok": False, "error": error})


def _joined(problems: tuple[tuple[str, str], ...]) -> str:
    return ";".join(m for _, m in problems)


# ---------------------------------------------------------------------------
#  应用
# ---------------------------------------------------------------------------


@dataclass
class App:
    """接口层的全部外部依赖。显式传入,不读全局——自检才好喂假数据。"""

    store: state.Store
    #: 归档根目录。**默认也在仓库外**,理由同 state 模块开头。
    archive_root: Path
    now: Callable[[], datetime] = datetime.now
    #: 留痕里的操作者。这一层不认人,见模块开头。
    actor: str = "web"
    #: 人工确认、撤单与委托查询用的券商适配器。None 表示当前没有会话来源。
    broker_provider: Callable[[], Any] | None = None
    #: 这一份数据是从哪儿来的,原样进 ``/api/status`` 的「数据源」。
    #:
    #: **默认值是"还没有",不是某种来源。** 这里原先写死着一句
    #: 「只读挂载二代 runtime」——而部署上去之后,容器里根本没有那个挂载,
    #: 状态页却照样把它当事实显示了一整天。二代的缺陷 6 就是这个形状:
    #: 一句承诺了某件事的字面量,而那件事没有任何代码在做。
    #:
    #: 可以传字符串,**也可以传一个每次现算的函数**(``serve.py`` 传的是
    #: 后者:``lambda: collect.describe_source(store)``)。之所以留这个口子:
    #: 券商配置是能在界面上改的,启动时定死一次会让状态页一直说「未配置」,
    #: 直到有人想起来重启服务——那正好又是一句和现状不符的话。
    data_source: str | Callable[[], str] = "采集层尚未接入"

    def describe_data_source(self) -> str:
        """取「数据源」的当前值。传的是函数就现算,是字符串就原样返回。

        **算的时候出了岔子不许把状态页带走。** 状态页是出事时第一个要看的
        东西,让它因为一句描述文本挂掉是本末倒置——所以这里兜底成一句
        能看出"没算出来"的话,而不是让异常冒到 500。
        """
        if callable(self.data_source):
            try:
                return str(self.data_source())
            except Exception as exc:      # noqa: BLE001 - 见 docstring
                logger.warning("数据源描述算不出来:%s", exc)
                return f"数据源描述算不出来({exc.__class__.__name__}),这不是数据本身的问题。"
        return str(self.data_source)


# ---------------------------------------------------------------------------
#  取归档
# ---------------------------------------------------------------------------


def _in_range(stamp: str, since: str | None, until: str | None) -> bool:
    """按日期区间过滤。比的是 ISO 时间串的前十位,字典序即时间序。

    ``until`` 是**含当天**的:界面上填 8-18 的人指的是"到 18 号为止",
    不是"到 18 号零点"。
    """
    day = stamp[:10]
    if since and day < since:
        return False
    if until and day > until:
        return False
    return True


def _iter_payloads(app: App, *, month: str | None = None) -> list[dict[str, Any]]:
    """读归档,**一份坏的不拖垮整轮**。

    和 ``index.rebuild`` 同一个取向:某份 JSON 被写坏了(断电、手改),
    列表接口该少一条并在日志里喊一声,而不是整页白屏。白屏会让人以为
    "今天没跑",那正是契约 2.1 明令不许发生的事。
    """
    payloads = []
    for path in archive.iter_paths(app.archive_root, month=month):
        try:
            payloads.append(archive.read_path(path))
        except Exception as exc:
            logger.error("跳过读不出来的归档 %s:%s", path.name, exc)
    return payloads


def _all_runs(app: App, *, since: str | None = None, until: str | None = None,
              system_name: str | None = None) -> list[dict[str, Any]]:
    """按时间倒序的全部归档(完整体,含 context)。

    月份目录能先筛掉大部分文件,但跨月区间的边界要靠 ``生成时间`` 判,
    所以这里不做目录级优化——七个标的一天六轮,一年一千多份,扫得动。
    真扫不动那天换 ``index.list_runs()``,列表项形状不会变。
    """
    runs = []
    for payload in _iter_payloads(app):
        stamp = str(payload.get("生成时间", ""))
        if not _in_range(stamp, since, until):
            continue
        if system_name and payload.get("system_name") != system_name:
            continue
        runs.append(payload)
    runs.sort(key=lambda p: str(p.get("生成时间", "")), reverse=True)
    return runs


# ---------------------------------------------------------------------------
#  /api/status
# ---------------------------------------------------------------------------


def _fired_slots(app: App, plan: scheduler.DayPlan) -> tuple[int, ...]:
    """今天哪几轮已经跑过了——**从归档反推,不另存一份**。

    调度器的 ``fired`` 是派生量:归档里有当天每一轮的记录,再单独存一份
    "跑过没"的状态,两份迟早对不上,而且对不上的时候没法判断谁对。

    归属判定用时点自己的窗口:一轮归档的 ``生成时间`` 落在第 i 轮的
    [触发时刻, 有效截止] 内,就算第 i 轮跑过了。窗口互不重叠(重叠的配置
    在 ``validate_config`` 就被抖动上限那条挡掉了),所以不会一份归档算两轮。
    """
    day = plan.day.isoformat()
    fired: set[int] = set()
    for payload in _iter_payloads(app, month=plan.day.strftime("%Y-%m")):
        if payload.get("system_name") != SYSTEM_NAME:
            continue
        raw = str(payload.get("生成时间", ""))
        if not raw.startswith(day):
            continue
        try:
            # 排期是本地墙上时间(``datetime.combine(day, 时点)``),归档时间带
            # 时区。丢掉时区、只比墙上时间是对的:配置里写的 09:45 指的就是
            # 服务器所在时区的 09:45。系统跨时区那天要一起改,不是只改这里。
            stamp = datetime.fromisoformat(raw).replace(tzinfo=None)
        except ValueError:
            continue
        for slot in plan.slots:
            if slot.fire_at <= stamp <= slot.deadline:
                fired.add(slot.index)
                break
    return tuple(sorted(fired))


def get_status(app: App, req: Request) -> Response:
    now = app.now()
    plan = scheduler.plan_day(now.date(), config=app.store.schedule())
    facts = app.store.runtime()

    latest = _all_runs(app, system_name=SYSTEM_NAME)
    最近策略时间 = str(latest[0]["生成时间"]) if latest else None

    # API 与 daemon 分属两个进程,每次都从共享运行目录恢复开关状态。
    runmode.restore_unattended(app.store.unattended())
    mode = runmode.describe()
    return ok({
        "system_name": SYSTEM_NAME,
        "app_version": __version__,
        # 联合类型,不是字面量。解锁那天这里自己会变,前端不用改。
        "运行模式": mode["运行模式"],
        "验证锁": mode["验证锁"],
        "无人值守": mode["无人值守"],
        "数据源": app.describe_data_source(),
        "最近策略时间": 最近策略时间,
        **facts.as_public(),
        # 契约 2.4:当天六轮**每轮都有一条**,错过的和被取代的一样列出来。
        "调度": scheduler.day_report(plan, now=now, fired=_fired_slots(app, plan)),
    })


# ---------------------------------------------------------------------------
#  /api/objects
# ---------------------------------------------------------------------------

#: 采集层还没写,所以这三项没有值。
#:
#: **不填零。** 契约 4 里「某标的无持仓(数量 0、成本价 0)」是一种正常状态,
#: 填零会让"确实空仓"和"根本没采到"在界面上长得一模一样——这正是三代要修的
#: 二代毛病。``持仓`` 给 null 表示"未采集",前端据此显示"—"而不是"0 股"。
_UNCOLLECTED = {"持仓": None, "最新切片时间": None, "是否当日行情": False}


def _object_view(obj: catalog_mod.TradeObject) -> dict[str, Any]:
    return {
        "object_id": obj.object_id,
        "market": obj.market,
        "symbol": obj.symbol,
        "名称": obj.name,
        "类型": obj.kind,
        "资产类型": obj.asset_type,
        "交易单位": obj.lot_size,
        **_UNCOLLECTED,
    }


def get_objects(app: App, req: Request) -> Response:
    return ok([_object_view(o) for o in app.store.catalog()])


def post_object(app: App, req: Request) -> Response:
    body = req.body if isinstance(req.body, Mapping) else {}
    existing = app.store.catalog()
    obj, failures = catalog_mod.validate_draft(body, existing=existing)
    if obj is None:
        problems = tuple((f.code, f.message) for f in failures)
        return fail(400, "INVALID_OBJECT", _joined(problems), problems=problems)

    app.store.save_catalog([*existing.objects, obj])
    logger.info("新增标的 %s", obj.display)
    return ok(_object_view(obj))


def put_object(app: App, req: Request, object_id: str) -> Response:
    existing = app.store.catalog()
    current = existing.get(object_id)
    if current is None:
        return fail(404, "NOT_FOUND", f"标的清单里没有 {object_id}")

    body = dict(req.body) if isinstance(req.body, Mapping) else {}

    # `object_id` 是 `市场_代码` 算出来的(契约 1.2.1),改市场或代码就是换了
    # 一个标的。允许改的话历史归档里那些引用会指向一个改了身份的东西,
    # 而且不报错。改成删一个加一个,历史至少还是自洽的。
    for key, was in (("market", current.market), ("symbol", current.symbol)):
        submitted = str(body.get(key, was)).strip()
        if key == "market":
            submitted = submitted.upper()
        if submitted and submitted != was:
            return fail(
                409, "IDENTITY_IMMUTABLE",
                f"{object_id} 的 {key} 不能改({was} → {submitted})——"
                f"object_id 由市场与代码算出,改了就是另一个标的,"
                f"历史归档里的引用会失配。请删除后重新添加。",
            )

    draft = {"market": current.market, "symbol": current.symbol, **body}
    obj, failures = catalog_mod.validate_draft(draft)   # 不传 existing:自己撞自己不算重复
    if obj is None:
        problems = tuple((f.code, f.message) for f in failures)
        return fail(400, "INVALID_OBJECT", _joined(problems), problems=problems)

    app.store.save_catalog(
        [obj if o.object_id == object_id else o for o in existing.objects]
    )
    logger.info("修改标的 %s", obj.display)
    return ok(_object_view(obj))


def delete_object(app: App, req: Request, object_id: str) -> Response:
    """删标的。二次确认是界面的事(契约 2 接口表),后端只管删得干净。"""
    existing = app.store.catalog()
    if existing.get(object_id) is None:
        return fail(404, "NOT_FOUND", f"标的清单里没有 {object_id}")

    app.store.save_catalog([o for o in existing.objects if o.object_id != object_id])
    logger.info("删除标的 %s", object_id)
    return ok({})


# ---------------------------------------------------------------------------
#  /api/account
# ---------------------------------------------------------------------------


def get_account(app: App, req: Request) -> Response:
    """账户摘要(契约 1.3)。**读最近一次采集的结果,不现场去查。**

    现场查要开浏览器、可能重新登录、过验证码,几十秒起。界面一打开就转
    圈还是小事——**每刷新一次页面就多一次登录尝试**,那是在自己撞券商的
    失败次数上限。所以这里读 ``account.json``,由采集层每轮写。

    代价是数据可能是陈的,所以 ``采集时间`` 一并返回,**前端必须显示它**。
    陈数据可以接受,看不出它陈不可以。

    没采过就明确返回 ``ok:false``,不返回一份零值摘要。契约 2.1 那句
    「界面上显示"暂无数据"而实际是请求挂了,是不可接受的」反过来同样成立:
    显示一份总资产 0 元的假摘要,比明说"取不到"要糟得多。
    """
    snapshot = app.store.account()
    if not snapshot or not isinstance(snapshot.get("账户"), Mapping):
        return fail(
            503, "NO_ACCOUNT_SNAPSHOT",
            "还没有采到过账户数据。可能是券商登录还没配好(设置 → 券商登录),"
            "也可能是还没跑过一轮。这不是请求失败,是这项数据还没有。",
        )
    return ok({
        "采集时间": snapshot.get("采集时间"),
        **dict(snapshot["账户"]),
    })


# ---------------------------------------------------------------------------
#  /api/runs
# ---------------------------------------------------------------------------


def get_runs(app: App, req: Request) -> Response:
    """归档列表**摘要**。契约 1.5 = 1.1 去掉 ``context``。

    形状只由 ``archive.summarize()` 定义,这里不挑字段——挑一次就多一份
    定义,v0.4 那次 43 MB 的事故就是定义分了家。
    """
    since, until = req.query.get("from"), req.query.get("to")
    if since and until and since > until:
        return fail(400, "INVALID_RANGE", f"开始日期 {since} 晚于结束日期 {until}")

    raw_limit = req.query.get("limit")
    limit: int | None = None
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError:
            return fail(400, "INVALID_LIMIT", f"limit 必须是整数,收到 {raw_limit!r}")
        if limit < 0:
            return fail(400, "INVALID_LIMIT", f"limit 不能为负:{limit}")

    runs = _all_runs(
        app, since=since, until=until, system_name=req.query.get("system_name")
    )
    if limit is not None:
        runs = runs[:limit]
    return ok([archive.summarize(p) for p in runs])


def get_run(app: App, req: Request, strategy_id: str) -> Response:
    """单轮完整归档,**含 context**。列表不返回它,这里返回。"""
    try:
        return ok(archive.read_run(strategy_id, root=app.archive_root))
    except (archive.ArchiveError, FileNotFoundError):
        return fail(404, "NOT_FOUND", f"没有 strategy_id 为 {strategy_id} 的归档")


# ---------------------------------------------------------------------------
#  /api/runs/compare(验证期专用,上线后连同界面一起删)
# ---------------------------------------------------------------------------


def _decision(item: Mapping[str, Any]) -> dict[str, Any]:
    return {"操作": item.get("操作"), "置信度": item.get("置信度")}


def get_compare(app: App, req: Request) -> Response:
    """按 ``context_digest`` 配对二代与三代的判断(契约 2.2)。

    **只有吃了同一份输入才有可比性**,所以配对键是 digest 不是时间。
    两套系统同一秒各跑各的、输入却不同,那种"对比"什么都说明不了。

    一方缺数据的行照样出(``tradepilot`` 或 ``zhixing`` 为 null),
    因为"二代判了三代没判"本身就是基线验证要看的结果之一。
    """
    since, until = req.query.get("from"), req.query.get("to")
    if since and until and since > until:
        return fail(400, "INVALID_RANGE", f"开始日期 {since} 晚于结束日期 {until}")

    # (digest, object_id) -> 行
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for payload in _all_runs(app, since=since, until=until):
        system = str(payload.get("system_name", ""))
        if system not in {"tradepilot", SYSTEM_NAME}:
            continue
        digest = str(payload.get("context_digest", ""))
        if not digest:
            continue
        for judgment in payload.get("交易对象判断") or ():
            if not isinstance(judgment, Mapping):
                continue
            object_id = str(judgment.get("object_id", ""))
            key = (digest, object_id)
            row = rows.setdefault(key, {
                "context_digest": digest,
                "生成时间": payload.get("生成时间"),
                "object_id": object_id,
                "名称": judgment.get("名称"),
                "tradepilot": None,
                SYSTEM_NAME: None,
            })
            row[system] = _decision(judgment)
            # 名称由后端 join(契约 1.1),两边都有时以非空的为准
            if judgment.get("名称"):
                row["名称"] = judgment["名称"]

    items = sorted(
        rows.values(),
        key=lambda r: (str(r["生成时间"]), str(r["object_id"])),
        reverse=True,
    )
    for row in items:
        both = row["tradepilot"] and row[SYSTEM_NAME]
        row["一致"] = bool(both) and row["tradepilot"]["操作"] == row[SYSTEM_NAME]["操作"]

    总条数 = len(items)
    一致条数 = sum(1 for r in items if r["一致"])
    return ok({
        "对比项": items,
        "汇总": {
            "总条数": 总条数,
            "一致条数": 一致条数,
            # 一条都没有时给 0.0 而不是 null:前端 `汇总` 的字段是必有的,
            # 空区间走的是「对比项为空」那个空态,不靠这个数判断。
            "一致率": round(一致条数 / 总条数, 4) if 总条数 else 0.0,
        },
    })


# ---------------------------------------------------------------------------
#  /api/usage
# ---------------------------------------------------------------------------

_USAGE_KEYS = ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens")

_GROUP_BY = {"day": "日期", "object": "object_id", "model": "model"}


def _usage_key(group_by: str, payload: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
    if group_by == "day":
        return str(payload.get("生成时间", ""))[:10]
    if group_by == "model":
        return str(payload.get("model", ""))
    return str(entry.get("object_id", ""))


def get_usage(app: App, req: Request) -> Response:
    """token 与成本聚合。

    ``缓存命中率`` 单独算,而且是**总量之比**,不是各轮命中率的平均——
    平均会被一堆小请求带偏,而账单是按总量出的。这一点和前端
    ``UsageOverview`` 的加权口径是同一个,两边都不许改成算术平均。

    这份聚合在 ``index.usage_by_day()`` 里有一份 SQL 版。两份实现就是两个
    答案,所以自检里有一条「同一批归档,两条路径结果相等」的对照。
    """
    group_by = req.query.get("group_by", "day")
    if group_by not in _GROUP_BY:
        return fail(400, "INVALID_USAGE_GROUP", "用量分组只支持 day、object 或 model。")

    since, until = req.query.get("from"), req.query.get("to")
    if since and until and since > until:
        return fail(400, "INVALID_USAGE_RANGE", "用量查询的开始日期不能晚于结束日期。")

    buckets: dict[str, dict[str, Any]] = {}
    counted: dict[str, set[str]] = {}
    for payload in _all_runs(app, since=since, until=until):
        strategy_id = str(payload.get("strategy_id", ""))
        for entry in payload.get("model_usage") or ():
            if not isinstance(entry, Mapping):
                continue
            key = _usage_key(group_by, payload, entry)
            bucket = buckets.setdefault(key, {k: 0 for k in _USAGE_KEYS})
            for k in _USAGE_KEYS:
                bucket[k] += int(entry.get(k) or 0)
            # 轮数按 strategy_id 去重:一轮里七个标的各有一条 model_usage,
            # 直接计数会把「一轮」说成「七轮」。
            counted.setdefault(key, set()).add(strategy_id)

    rows = []
    for key in sorted(buckets, reverse=(group_by == "day")):
        bucket = buckets[key]
        inp = bucket["input_tokens"]
        rows.append({
            _GROUP_BY[group_by]: key,
            "轮数": len(counted[key]),
            **bucket,
            "缓存命中率": round(bucket["cached_tokens"] / inp, 4) if inp else 0.0,
        })
    return ok(rows)


# ---------------------------------------------------------------------------
#  /api/instructions
# ---------------------------------------------------------------------------


def get_pending_instructions(app: App, req: Request) -> Response:
    """等人接管的指令。**无人值守开着时长期为空,这是常态。**

    只看三代自己的归档:二代的指令仍由二代负责,两代执行记录不混用。
    """
    completed = {
        str(item.get("instruction_code") or "")
        for item in archive.iter_executions(app.archive_root)
        if item.get("outcome") in {
            execution.Outcome.SUBMITTED.value,
            execution.Outcome.SUBMITTED_UNKNOWN.value,
        }
    }
    pending = []
    for payload in _all_runs(app, system_name=SYSTEM_NAME):
        for item in payload.get("待执行指令") or ():
            if (
                isinstance(item, Mapping)
                and item.get("状态") == "pending"
                and str(item.get("instruction_code") or "") not in completed
            ):
                pending.append({
                    **item,
                    "strategy_id": payload.get("strategy_id"),
                    "生成时间": payload.get("生成时间"),
                })
    return ok(pending)


def live_order_blockers(app: App) -> tuple[str, ...]:
    """人工执行通路当前缺什么。空元组表示可以尝试连接券商。"""
    缺: list[str] = []

    try:
        settings = app.store.broker()
    except state.StateError as exc:
        缺.append(f"券商配置读不出来:{exc}")
    else:
        if not settings.configured:
            缺.append("券商未配置齐全,缺:" + "、".join(settings.missing()))
    if app.broker_provider is None:
        缺.append("接口服务没有券商会话提供器")

    return tuple(缺)


def confirm_instruction(app: App, req: Request, code: str) -> Response:
    """人工接管一条归档中的 pending 指令。仍走唯一 ValidatedOrder 通路。"""
    try:
        runmode.assert_live_trading_allowed(what="人工接管下单")
    except runmode.LiveTradingForbidden:
        return fail(
            403, "DRY_RUN_LOCKED", "三代当前为只读验证模式,不执行任何真实下单"
        )

    item: Mapping[str, Any] | None = None
    for payload in _all_runs(app, system_name=SYSTEM_NAME):
        for candidate in payload.get("待执行指令") or ():
            if (
                isinstance(candidate, Mapping)
                and candidate.get("instruction_code") == code
                and candidate.get("状态") == "pending"
            ):
                item = candidate
                break
        if item is not None:
            break
    if item is None:
        return fail(404, "NOT_FOUND", f"没有待处理指令:{code}")

    now = app.now()
    report = guards.validate(
        guards.ProposedOrder(
            instruction_code=code,
            action=str(item.get("action") or ""),
            market=str(item.get("market") or ""),
            symbol=str(item.get("symbol") or ""),
            name=str(item.get("name") or ""),
            qty=item.get("qty"),
            limit_price=item.get("limit_price"),
            wtbh=str(item.get("wtbh") or "") or None,
            reason=str(item.get("理由") or ""),
            risk_note=str(item.get("风险提示") or ""),
        ),
        guards.ValidationContext(account=None, objects={}, now=now),
    )
    if not report.ok or report.order is None:
        problems = tuple((p.code, p.message) for p in report.failures)
        return fail(
            400, "INVALID_INSTRUCTION", _joined(problems), problems=problems
        )

    broker = _resolve_broker(app)
    auth = execution.Authorization(
        kind=execution.AuthorizationKind.MANUAL,
        actor=app.actor,
        source=f"api:confirm:{code}",
        issued_at=now,
    )
    record = execution.submit(report.order, auth, broker=broker, now=now)
    entry = execution.record_entry(record)
    archive.write_execution(entry, root=app.archive_root)
    if record.outcome is execution.Outcome.SUBMITTED_UNKNOWN:
        return Response(202, {"ok": True, "data": entry})
    if record.outcome is execution.Outcome.SUBMITTED:
        return ok(entry)
    return fail(503, "BROKER_UNAVAILABLE", record.message or "委托没有发出")


def _resolve_broker(app: App) -> Any:
    """取得券商适配器。失败只返回 None,不把异常文本带进响应或日志。"""
    if app.broker_provider is None:
        return None
    try:
        return app.broker_provider()
    except Exception as exc:  # noqa: BLE001 - 不泄露浏览器内部信息
        logger.error("券商会话当前不可用,异常类型=%s", exc.__class__.__name__)
        return None


_ACTIVITY_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("委托编号", ("Wtbh", "wtbh", "ordersno")),
    ("市场", ("Market", "market", "Jysc", "market_ex")),
    ("代码", ("Zqdm", "zqdm", "stkcode", "stockcode")),
    ("名称", ("Zqmc", "zqmc", "stkname", "stockname")),
    ("方向", ("Mmsm", "mmsm", "Mmbz", "mmbz")),
    ("委托数量", ("Wtsl", "wtsl", "orderqty")),
    ("委托价格", ("Wtjg", "wtjg", "orderprice")),
    ("成交数量", ("Cjsl", "cjsl", "matchqty")),
    ("成交均价", ("Cjjg", "cjjg", "matchprice")),
    ("状态", ("Wtzt", "wtzt", "Wtztmc", "status")),
    ("委托时间", ("Wtsj", "wtsj", "ordertime")),
)


def _public_activity(raw: Mapping[str, Any]) -> dict[str, Any]:
    """只下发委托所需白名单字段,不把券商原始行里的账号等字段带出去。"""
    public: dict[str, Any] = {}
    for group, slot in raw.items():
        if not isinstance(slot, Mapping) or not slot.get("取到了"):
            public[str(group)] = {"取到了": False, "原因": "券商该查询接口当前不可用"}
            continue
        rows = []
        for raw_row in slot.get("明细") or ():
            if not isinstance(raw_row, Mapping):
                continue
            normalized = {str(k).lower(): v for k, v in raw_row.items()}
            row: dict[str, Any] = {}
            for label, aliases in _ACTIVITY_FIELDS:
                for alias in aliases:
                    key = alias.lower()
                    if key in normalized and normalized[key] not in (None, ""):
                        row[label] = normalized[key]
                        break
            rows.append(row)
        public[str(group)] = {"取到了": True, "条数": len(rows), "明细": rows}
    return public


def get_order_activity(app: App, req: Request) -> Response:
    broker = _resolve_broker(app)
    if broker is None:
        return fail(503, "BROKER_UNAVAILABLE", "券商会话当前不可用")
    try:
        raw = broker.activity()
    except Exception as exc:  # noqa: BLE001 - 不回显券商/浏览器内部异常
        logger.error("查询委托流水失败,异常类型=%s", exc.__class__.__name__)
        return fail(503, "BROKER_UNAVAILABLE", "当前委托流水查询失败")
    if not isinstance(raw, Mapping):
        return fail(502, "INVALID_BROKER_RESPONSE", "券商委托流水返回结构不正确")
    return ok(_public_activity(raw))


def cancel_order(app: App, req: Request, wtbh: str) -> Response:
    """人工撤单。通过 execution.submit 留下与自动下单同构的结果。"""
    now = app.now()
    report = guards.validate(
        guards.ProposedOrder(
            instruction_code=f"cancel-{wtbh}-{now:%Y%m%d%H%M%S}",
            action="cancel",
            market="",
            symbol="",
            name="人工撤单",
            wtbh=wtbh,
            reason=str((req.body or {}).get("原因") or "人工从运行页撤单")
            if isinstance(req.body, Mapping) else "人工从运行页撤单",
        ),
        guards.ValidationContext(account=None, objects={}, now=now),
    )
    if not report.ok or report.order is None:
        return fail(400, "INVALID_CANCEL", "撤单指令无法完成类型规范化")
    record = execution.submit(
        report.order,
        execution.Authorization(
            kind=execution.AuthorizationKind.MANUAL,
            actor=app.actor,
            source="api:cancel",
            issued_at=now,
        ),
        broker=_resolve_broker(app),
        now=now,
    )
    entry = execution.record_entry(record)
    archive.write_execution(entry, root=app.archive_root)
    if record.outcome is execution.Outcome.SUBMITTED_UNKNOWN:
        return Response(202, {"ok": True, "data": entry})
    if record.outcome is execution.Outcome.SUBMITTED:
        return ok(entry)
    return fail(503, "BROKER_UNAVAILABLE", record.message or "撤单请求没有发出")


# ---------------------------------------------------------------------------
#  /api/settings
# ---------------------------------------------------------------------------


def get_schedule(app: App, req: Request) -> Response:
    config = app.store.schedule()
    return ok({
        "时点": list(config.as_text()),
        "抖动上限秒": config.max_jitter_seconds,
        "有效窗口分钟": config.window_minutes,
    })


def put_schedule(app: App, req: Request) -> Response:
    """改调度时点。四条规则**一次全报**(契约 2.4)。"""
    body = req.body if isinstance(req.body, Mapping) else {}
    reason = str(body.get("原因") or "").strip()
    if not reason:
        return fail(400, "REASON_REQUIRED", "变更调度时点必须写明原因。")

    raw_times = body.get("时点")
    if not isinstance(raw_times, list):
        return fail(400, "INVALID_SCHEDULE", "`时点` 必须是字符串数组。")

    current = app.store.schedule()
    config, problems = scheduler.validate_config(
        [str(t) for t in raw_times],
        max_jitter_seconds=current.max_jitter_seconds,
        window_minutes=current.window_minutes,
        jitter_salt=current.jitter_salt,
    )
    if config is None:
        pairs = tuple(("INVALID_SCHEDULE", p) for p in problems)
        return fail(400, "INVALID_SCHEDULE", _joined(pairs), problems=pairs)

    # 走 apply_config 而不是直接存:变更留痕是它的职责,绕过去就没记录了。
    config, _change = scheduler.apply_config(
        [str(t) for t in raw_times],
        current=current,
        changed_by=app.actor,
        reason=reason,
        now=app.now(),
    )
    app.store.save_schedule(config)
    return ok({})


def get_model(app: App, req: Request) -> Response:
    """**密钥一律脱敏。** 和验证码接口同一条规矩。"""
    return ok(app.store.model().as_public())


def put_model(app: App, req: Request) -> Response:
    body = req.body if isinstance(req.body, Mapping) else {}
    endpoint = str(body.get("接口地址") or "").strip()
    name = str(body.get("模型") or "").strip()
    provider = str(body.get("提供方") or "").strip()
    protocol = str(body.get("协议") or "openai_chat").strip()

    问题: list[tuple[str, str]] = []
    if not endpoint:
        问题.append(("ENDPOINT_REQUIRED", "接口地址不能为空。"))
    elif not endpoint.startswith(("https://", "http://")):
        问题.append(("ENDPOINT_SCHEME", "接口地址要以 http:// 或 https:// 开头。"))
    if not name:
        问题.append(("MODEL_REQUIRED", "模型名不能为空,它要原样进归档。"))
    if not provider:
        问题.append(("PROVIDER_REQUIRED", "提供方不能为空,它要原样进归档。"))
    if protocol not in model.PROTOCOLS:
        问题.append((
            "UNKNOWN_PROTOCOL",
            f"协议只能是 {' / '.join(model.PROTOCOLS)},收到 {protocol!r}。",
        ))

    if 问题:
        return fail(
            400, "INVALID_MODEL_SETTINGS",
            ";".join(m for _, m in 问题), problems=tuple(问题),
        )

    submitted = str(body.get("密钥") or "").strip()
    current = app.store.model()
    if not (submitted or current.secret):
        return fail(400, "SECRET_REQUIRED", "还没有配置过密钥,这次必须填。")

    # 空密钥 = 不改。理由同验证码接口:GET 不下发明文,前端没有原值可回填。
    app.store.save_model(
        state.ModelSettings(
            endpoint=endpoint, name=name, provider=provider, protocol=protocol,
            secret=submitted or current.secret,
        )
    )
    logger.info(
        "模型配置已更新:%s @ %s(密钥%s)",
        name, provider, "已变更" if submitted else "未变更",
    )
    return ok({})


def get_captcha(app: App, req: Request) -> Response:
    """**密钥一律脱敏。** 后端任何时候都不下发明文,见 state 模块开头。"""
    return ok(app.store.captcha().as_public())


def put_captcha(app: App, req: Request) -> Response:
    body = req.body if isinstance(req.body, Mapping) else {}
    endpoint = str(body.get("接口地址") or "").strip()
    model = str(body.get("模型") or "").strip()
    current = app.store.captcha()

    # 不带「识别方式」= 这次没动它。和密钥同一个约定,理由也一样:前端目前
    # **没有这个输入框**,它每次提交都不会带这一项。要是缺省成"视觉模型",
    # 那么在界面上改一次接口地址,就会把识别方式悄悄改回去——而表现出来的
    # 样子是"登录突然不行了",没人会想到是刚才改地址那一下。
    provider = str(body.get("识别方式") or "").strip() or current.provider
    if provider not in captcha.PROVIDERS:
        return fail(400, "INVALID_CAPTCHA_SETTINGS",
                    f"识别方式只能是:{'、'.join(captcha.PROVIDERS)}。")

    if not endpoint:
        return fail(400, "INVALID_CAPTCHA_SETTINGS", "接口地址不能为空。")
    # 「模型」只有视觉模型那条路要。图鉴要的是题目类型,有默认值,
    # **不能因为它空着就拒绝保存**——那是在要求人填一个填了也没用的字段。
    if provider == captcha.PROVIDER_VISION and not model:
        return fail(400, "INVALID_CAPTCHA_SETTINGS", "接口地址和模型不能为空。")

    submitted = str(body.get("密钥") or "").strip()

    # 空密钥 = 不改。前端手上永远只有脱敏值(GET 不下发明文),所以它没法
    # 把原值回填;要求必填等于逼人每次改地址都重输一遍密钥,那才会出事。
    # 「备用识别」不带 = 这次没动它。前端还没有这几个输入框,每次提交都不会
    # 带这一项;缺省成"清空"的话,在界面上改一次接口地址就会把兜底那几条
    # 悄悄拆掉——**而拆掉之后一切照常**,直到主用那条挂了才知道。
    raw_backups = body.get("备用识别")
    backups: list[state.CaptchaLink] = []
    if raw_backups is not None:
        if not isinstance(raw_backups, list):
            return fail(400, "INVALID_CAPTCHA_SETTINGS", "「备用识别」得是一个列表。")
        旧表 = list(current.backups)
        for i, 项 in enumerate(raw_backups, start=1):
            if not isinstance(项, dict):
                return fail(400, "INVALID_CAPTCHA_SETTINGS",
                            f"「备用识别」第 {i} 条不是一个对象。")
            bp = str(项.get("识别方式") or "").strip()
            if bp not in captcha.PROVIDERS:
                return fail(400, "INVALID_CAPTCHA_SETTINGS",
                            f"「备用识别」第 {i} 条的识别方式只能是:"
                            f"{'、'.join(captcha.PROVIDERS)}。")
            be = str(项.get("接口地址") or "").strip()
            if not be:
                return fail(400, "INVALID_CAPTCHA_SETTINGS",
                            f"「备用识别」第 {i} 条的接口地址不能为空。")
            bs = str(项.get("密钥") or "").strip()
            if not bs:
                # 空密钥 = 沿用原值,和主用那条一样。但**只有在这一条还是
                # 原来那一条时才沿用**:识别方式或地址变了,原密钥就是另一
                # 家的密钥,拿去用只会得到一句看不懂的平台报错。
                原 = 旧表[i - 1] if i <= len(旧表) else None
                if 原 is not None and 原.provider == bp and 原.endpoint == be:
                    bs = 原.secret
                if not bs:
                    return fail(400, "INVALID_CAPTCHA_SETTINGS",
                                f"「备用识别」第 {i} 条是新配的,密钥不能为空。")
            backups.append(state.CaptchaLink(
                provider=bp, endpoint=be,
                model=str(项.get("模型") or "").strip(), secret=bs))

    app.store.save_captcha(
        state.CaptchaSettings(
            endpoint=endpoint,
            model=model,
            provider=provider,
            secret=submitted or current.secret,
            backups=tuple(backups),
        )
    )
    logger.info("验证码接口配置已更新(识别方式 %s,密钥%s,备用 %s)",
                provider, "已变更" if submitted else "未变更",
                f"{len(backups)} 条" if raw_backups is not None else "未变更")
    return ok({})


def get_broker(app: App, req: Request) -> Response:
    """券商登录配置。**账号和密码一律脱敏。**

    ⚠️ 这里有两样机密不是一样:交易密码不必说,**完整资金账号在本项目的
    约束里和密码同级**。所以两个都只出遮过的值(``123****9012``),
    后端任何时候都不下发原文,见 ``state.BrokerSettings``。
    """
    settings = app.store.broker()
    body = settings.as_public()
    # 把"还缺什么"直接告诉前端,而不是让它自己拿脱敏值去猜有没有配。
    # 脱敏值猜不出来——空账号和 "****" 在界面上都是灰的。
    body["缺项"] = list(settings.missing())
    body["已配全"] = settings.configured
    return ok(body)


def put_broker(app: App, req: Request) -> Response:
    """改券商登录配置。

    **空值 = 这次没动它,不是清空。** 和验证码 / 模型密钥同一个规矩:
    前端手上只有脱敏值(GET 不下发明文),它没法回填原值,所以提交空串
    只可能意味着没改。真要清空得删文件,那是运维动作。
    """
    body = req.body if isinstance(req.body, Mapping) else {}
    remote_url = str(body.get("浏览器远端") or "").strip()
    if not remote_url:
        return fail(
            400, "INVALID_BROKER_SETTINGS",
            "浏览器远端不能为空。三代**必须用自己的浏览器容器**——"
            "共用二代那个会把它正在用真钱交易的登录会话挤掉。",
        )

    current = app.store.broker()
    account = str(body.get("资金账号") or "").strip()
    # 密码**不 strip**。首尾空格在密码里是合法字符,去掉之后登录会以
    # 「密码错误」的面目出现,而人查不出为什么——他明明输对了。
    password = str(body.get("交易密码") or "")

    app.store.save_broker(
        state.BrokerSettings(
            remote_url=remote_url,
            account=account or current.account,
            password=password or current.password,
        )
    )
    # 只记改没改,**不记值**,连长度都不记(长度也是信息)。
    logger.info("券商配置已更新(账号%s,密码%s)",
                "已变更" if account else "未变更",
                "已变更" if password else "未变更")
    return ok({})


def put_unattended(app: App, req: Request) -> Response:
    """开关无人值守。**必须带原因**,后端强制留痕(契约 2 接口表)。"""
    body = req.body if isinstance(req.body, Mapping) else {}
    reason = str(body.get("原因") or "").strip()
    if not reason:
        return fail(400, "REASON_REQUIRED", "变更无人值守模式必须填写原因。")

    enabled = body.get("无人值守")
    if not isinstance(enabled, bool):
        return fail(400, "INVALID_UNATTENDED", "`无人值守` 必须是布尔值。")

    try:
        runmode.set_unattended(
            enabled, changed_by=app.actor, reason=reason, now=app.now()
        )
    except ValueError as exc:
        return fail(400, "INVALID_UNATTENDED", str(exc))
    app.store.save_unattended(runmode.unattended_state())
    return ok({})


# ---------------------------------------------------------------------------
#  路由
# ---------------------------------------------------------------------------

#: 固定路径。**先查这张表**——`/api/runs/compare` 必须在 `/api/runs/{id}`
#: 之前匹配,否则会去找一份 strategy_id 叫 "compare" 的归档。
_STATIC: dict[tuple[str, str], Callable[[App, Request], Response]] = {
    ("GET", "/api/status"): get_status,
    ("GET", "/api/objects"): get_objects,
    ("POST", "/api/objects"): post_object,
    ("GET", "/api/account"): get_account,
    ("GET", "/api/runs"): get_runs,
    ("GET", "/api/runs/compare"): get_compare,
    ("GET", "/api/usage"): get_usage,
    ("GET", "/api/instructions/pending"): get_pending_instructions,
    ("GET", "/api/orders/activity"): get_order_activity,
    ("GET", "/api/settings/schedule"): get_schedule,
    ("PUT", "/api/settings/schedule"): put_schedule,
    ("GET", "/api/settings/captcha"): get_captcha,
    ("PUT", "/api/settings/captcha"): put_captcha,
    ("GET", "/api/settings/model"): get_model,
    ("PUT", "/api/settings/model"): put_model,
    ("GET", "/api/settings/broker"): get_broker,
    ("PUT", "/api/settings/broker"): put_broker,
    ("PUT", "/api/settings/unattended"): put_unattended,
}

#: 带一个路径参数的路由:(方法, 前缀, 后缀, 处理器)。
#: 手写而不是上正则:数量很少,正则只会让"哪条先匹配"变得看不出来。
_DYNAMIC: tuple[tuple[str, str, str, Callable[[App, Request, str], Response]], ...] = (
    ("PUT", "/api/objects/", "", put_object),
    ("DELETE", "/api/objects/", "", delete_object),
    ("POST", "/api/instructions/", "/confirm", confirm_instruction),
    ("POST", "/api/orders/", "/cancel", cancel_order),
    ("GET", "/api/runs/", "", get_run),
)


def _route(app: App, req: Request) -> Response:
    path = req.path.rstrip("/") or req.path
    handler = _STATIC.get((req.method, path))
    if handler is not None:
        return handler(app, req)

    for method, prefix, suffix, dyn in _DYNAMIC:
        if req.method != method or not path.startswith(prefix) or not path.endswith(suffix):
            continue
        param = path[len(prefix):len(path) - len(suffix) if suffix else None]
        if not param or "/" in param:
            continue
        return dyn(app, req, param)

    # 方法不对和路径不存在要分开报:前端调错方法时,405 直接指出问题在哪,
    # 404 会让人去查一个其实存在的路径。
    if any(p == path for (_, p) in _STATIC) or any(
        path.startswith(pre) and path.endswith(suf) for _, pre, suf, _ in _DYNAMIC
    ):
        return fail(405, "METHOD_NOT_ALLOWED", f"{path} 不支持 {req.method}")

    # 用 NO_SUCH_ENDPOINT 而不是 NOT_FOUND:「这个接口不存在」是前端的 bug,
    # 「这份归档不存在」是正常结果。两者都回 NOT_FOUND 的话,前端没法区分
    # 「我路径写错了」和「这条记录被删了」——自检里那条路由可达性检查就是
    # 因为分不开才误报的。
    return fail(404, "NO_SUCH_ENDPOINT", f"没有这个接口:{path}")


def handle(app: App, req: Request) -> Response:
    """唯一入口。路由 → 处理 → **机密扫描** → 返回。

    处理器抛异常一律转成 500,并且**不把异常文本发给前端**——异常里可能带
    路径、配置片段甚至密钥。人要细节去看日志,那是本机的。
    """
    try:
        response = _route(app, req)
    except Exception:
        logger.exception("处理 %s %s 时未捕获异常", req.method, req.path)
        return fail(500, "INTERNAL_ERROR", "后端处理请求时出错,详见服务端日志。")

    findings = archive.scan_for_secrets(response.payload)
    if findings:
        # 只记路径,**不记值**——把泄露内容写进日志等于换个地方泄露一次。
        logger.error(
            "响应被拦下:命中 %d 处疑似机密,位置 %s",
            len(findings), [f.path for f in findings],
        )
        return fail(
            500, "SECRET_LEAK_BLOCKED",
            "响应中检出疑似机密,已拦下不下发。这是后端 bug,详见服务端日志。",
        )
    return response


__all__ = [
    "Request", "Response", "App", "ok", "fail", "handle",
]
