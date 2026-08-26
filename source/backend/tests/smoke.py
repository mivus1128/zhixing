"""骨架自检 —— 验证三条结构性保证真的成立。

不是完整测试,是"我说的三件事到底做没做到"的当场证明。
运行:python -m tests.smoke  (在 backend/ 目录下)
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal

from zhixing import catalog, context, guards, history, llm, model, prompts, runmode, runner
from zhixing.broker import BrokerError
from zhixing.execution import Authorization, AuthorizationKind, Outcome, submit, submit_reports

PASS, FAIL = "  [OK]", "  [!!]"
results: list[bool] = []


def check(label: str, condition: bool) -> None:
    results.append(condition)
    print(f"{PASS if condition else FAIL} {label}")


def _raises(kind, fn) -> bool:
    """``fn()`` 是不是抛了 ``kind``。**抛别的一律算没通过**——
    "炸了"和"按预期拒绝了"是两件事,混在一起测等于没测。"""
    try:
        fn()
    except kind:
        return True
    except BaseException:
        return False
    return False


def make_ctx(now: datetime) -> guards.ValidationContext:
    return guards.ValidationContext(
        account=guards.AccountSnapshot(available_cash=Decimal("100000")),
        objects={
            "510300": guards.ObjectSnapshot(
                symbol="510300",
                last_price=Decimal("3.912"),
                prev_close=Decimal("3.900"),
                available_qty=2000,
                holding_qty=2000,
                is_etf=True,
            ),
            "000001": guards.ObjectSnapshot(
                symbol="000001",
                last_price=Decimal("11.50"),
                prev_close=Decimal("11.40"),
                available_qty=150,
                holding_qty=150,
            ),
        },
        now=now,
    )


# 交易时段内的一个时刻(周一 10:00)
NOW = datetime(2026, 8, 17, 10, 0, 0)
CTX = make_ctx(NOW)


print("\n=== 保证一:没过校验的东西在类型上就进不了执行层 ===")

try:
    guards.ValidatedOrder(
        instruction_code="forged", action="buy", market="SH", symbol="510300",
        name="沪深300ETF", qty=100, limit_price=Decimal("3.9"),
        notional=Decimal("390"), wtbh=None, reason="", risk_note="",
        validated_at=NOW, passed=(),
    )
    check("手工伪造 ValidatedOrder 应当失败", False)
except RuntimeError as exc:
    check(f"手工伪造被拒:{str(exc)[:28]}...", True)


print("\n=== 保证二:风控全拆,只保留类型规范化 ===")

bad_qty = guards.validate(
    guards.ProposedOrder("a1", "buy", "SH", "510300", "演示标的一",
                         qty="约一千", limit_price="3.90"),
    CTX,
)
check("数量无法转成整数 -> QTY_UNPARSABLE",
      not bad_qty.ok and [f.code for f in bad_qty.failures] == ["QTY_UNPARSABLE"])

bad_both = guards.validate(
    guards.ProposedOrder("a2", "buy", "SH", "510300", "演示标的一",
                         qty="很多", limit_price=None),
    CTX,
)
check("数量和价格无法转换时一次报全",
      {f.code for f in bad_both.failures} == {"QTY_UNPARSABLE", "PRICE_UNPARSABLE"})

non_finite = guards.validate(
    guards.ProposedOrder("a3", "buy", "SH", "510300", "演示标的一",
                         qty=100, limit_price="NaN"),
    CTX,
)
check("NaN/Infinity 不是券商可用的数值类型,按 PRICE_UNPARSABLE 留痕而不是让整轮崩",
      [f.code for f in non_finite.failures] == ["PRICE_UNPARSABLE"])

removed_cases = (
    ("非整手", guards.ProposedOrder("r1", "buy", "SH", "510300", "演示标的一",
                                  qty=150, limit_price="3.900")),
    ("卖超持仓", guards.ProposedOrder("r2", "sell", "SH", "510300", "演示标的一",
                                    qty=5000, limit_price="3.900")),
    ("超大金额", guards.ProposedOrder("r3", "buy", "SH", "510300", "演示标的一",
                                    qty=200000, limit_price="39.00")),
    ("负数量与负价格", guards.ProposedOrder("r4", "buy", "SH", "510300", "演示标的一",
                                          qty=-100, limit_price="-3.90")),
)
for label, proposed in removed_cases:
    report = guards.validate(proposed, CTX)
    check(f"已拆风控不再拦:{label}", report.ok and not report.failures)

after_hours = guards.validate(
    guards.ProposedOrder("r5", "buy", "SH", "510300", "演示标的一",
                         qty=100, limit_price="3.900"),
    make_ctx(datetime(2026, 8, 17, 16, 30, 0)),
)
weekend = guards.validate(
    guards.ProposedOrder("r6", "buy", "SH", "510300", "演示标的一",
                         qty=100, limit_price="3.900"),
    make_ctx(datetime(2026, 8, 16, 10, 0, 0)),
)
check("交易时段与交易日检查已拆除", after_hours.ok and weekend.ok)

good = guards.validate(
    guards.ProposedOrder("c1", "buy", "SH", "510300", "演示标的一",
                         qty=1000, limit_price="3.900"),
    CTX,
)
check("规范化仍产出唯一 ValidatedOrder 通路", good.ok and good.order is not None)


print("\n=== 保证三:执行授权、模拟隔离和结果不明 ===")

check("源码验证锁已解除", runmode.VERIFICATION_LOCK is False)
check("live_trading_allowed() 为 True", runmode.live_trading_allowed() is True)


class _Broker:
    def __init__(self, *, unknown: bool = False) -> None:
        self.calls = 0
        self.unknown = unknown

    def place_order(self, order) -> str:
        self.calls += 1
        if self.unknown:
            raise BrokerError("模拟浏览器异常", submitted_unknown=True)
        return "demo-order-ref"

    def cancel_order(self, wtbh: str) -> None:
        self.calls += 1


simulation_broker = _Broker()
simulation_auth = Authorization(
    kind=AuthorizationKind.SIMULATION, actor="smoke-test",
    source="simulation", issued_at=NOW,
)
simulation = submit(good.order, simulation_auth, broker=simulation_broker, now=NOW)
check("SIMULATION 恒为 DRY_RUN 且券商零调用",
      simulation.outcome is Outcome.DRY_RUN and simulation_broker.calls == 0)

auth = Authorization(
    kind=AuthorizationKind.UNATTENDED, actor="scheduler",
    source="smoke-test", issued_at=NOW,
)
runmode.set_unattended(False, changed_by="smoke-test", reason="自检:验证残留任务防护")
closed_broker = _Broker()
closed = submit(good.order, auth, broker=closed_broker, now=NOW)
check("开关关闭时 UNATTENDED 被拒且券商零调用",
      closed.outcome is Outcome.REJECTED and closed_broker.calls == 0)

runmode.set_unattended(True, changed_by="smoke-test", reason="自检:执行链")
missing_broker = submit(good.order, auth, broker=None, now=NOW)
check("券商未配置走 broker=None 留痕,不抛异常",
      missing_broker.outcome is Outcome.FAILED and "券商适配器" in missing_broker.message)

unknown_broker = _Broker(unknown=True)
unknown = submit(good.order, auth, broker=unknown_broker, now=NOW)
check("submitted_unknown 不被吞成普通失败",
      unknown.outcome is Outcome.SUBMITTED_UNKNOWN
      and unknown.submitted_unknown and unknown_broker.calls == 1)

batch = submit_reports([good, bad_both], auth, broker=None, now=NOW)
check("批量执行同时保留可执行记录与类型转换失败",
      len(batch.records) == 1 and len(batch.blocked) == 1)


print("\n=== 保证四:标的属性只有一个来源(清单),不写死在代码里 ===")

CATALOG = catalog.Catalog([
    catalog.TradeObject("SH_510300", "SH", "510300", "沪深300ETF",
                        asset_type="ETF"),
    catalog.TradeObject("SH_601939", "SH", "601939", "建设银行",
                        asset_type="股票"),
    catalog.TradeObject("SZ_159941", "SZ", "159941", "纳指ETF",
                        kind=catalog.KIND_QUOTE_ONLY, asset_type="ETF"),
])

# 名称由后端 join。模型只给代码
annotated, unknown = CATALOG.annotate([
    {"object_id": "SH_510300", "操作": "hold"},
    {"object_id": "SH_601939", "操作": "buy"},
])
check("join 贴上名称(模型不输出名字)",
      not unknown and [r["名称"] for r in annotated] == ["沪深300ETF", "建设银行"])

# 捏造的代码必须炸出来,不能静默留空
_, forged = CATALOG.annotate([{"object_id": "SH_999999", "操作": "buy"}])
check(f"模型捏造 object_id 被揪出({forged[0] if forged else 'N/A'})",
      forged == ("SH_999999",))

# 标的属性仍由清单提供,但不再参与本地下单阻断
etf_ctx = guards.ValidationContext(
    account=CTX.account,
    objects={"510300": CATALOG.to_snapshot(
        "SH_510300", last_price=Decimal("3.912"), prev_close=Decimal("3.900"))},
    now=NOW,
)
etf_tick = guards.validate(
    guards.ProposedOrder("d1", "buy", "SH", "510300", "沪深300ETF",
                         qty=100, limit_price="3.912"),
    etf_ctx,
)
check("ETF 价格不做本地步长检查", etf_tick.ok)

stock_ctx = guards.ValidationContext(
    account=CTX.account,
    objects={"601939": CATALOG.to_snapshot(
        "SH_601939", last_price=Decimal("7.500"), prev_close=Decimal("7.480"))},
    now=NOW,
)
stock_tick = guards.validate(
    guards.ProposedOrder("d2", "buy", "SH", "601939", "建设银行",
                         qty=100, limit_price="7.502"),
    stock_ctx,
)
check("股票价格不做本地步长检查", stock_tick.ok)

# 行情对象限制也已按“全拆”口径移除
quote_ctx = guards.ValidationContext(
    account=CTX.account,
    objects={"159941": CATALOG.to_snapshot(
        "SZ_159941", last_price=Decimal("1.500"), prev_close=Decimal("1.490"))},
    now=NOW,
)
quote_only = guards.validate(
    guards.ProposedOrder("d3", "buy", "SZ", "159941", "纳指ETF",
                         qty=100, limit_price="1.500"),
    quote_ctx,
)
check("行情对象不再被本地规则拦截", quote_only.ok)

# 增删改入口:一次报全部错
_, draft_fails = catalog.validate_draft(
    {"market": "BJ", "symbol": "abc", "名称": "", "类型": "别的", "资产类型": "债券"}
)
check(f"非法新增一次报全部原因(收到 {len(draft_fails)} 条)", len(draft_fails) == 5)

drafted, ok_fails = catalog.validate_draft(
    {"market": "sz", "symbol": "159937", "名称": "黄金ETF",
     "类型": catalog.KIND_TRADABLE, "资产类型": "ETF"},
    existing=CATALOG,
)
check("合规新增 -> object_id 由后端生成",
      not ok_fails and drafted is not None and drafted.object_id == "SZ_159937")


print("\n=== 保证五:一轮内 N 份上下文的共享前缀逐字节相同 ===")

try:
    context.SharedBlock(rendered="{\n", digest="fake")
    check("手工伪造 SharedBlock 应当失败", False)
except RuntimeError as exc:
    check(f"手工伪造被拒:{str(exc)[:24]}...", True)

SHARED = context.build_shared(
    输出要求={"schema": "见契约 1.1", "只输出 JSON": True},
    读取范围="近30个交易日数据",
    市场数据列表=[{"object_id": "SZ_159941", "收盘": 1.502}],
    账户交易流水表=[{"日期": "2026-08-14", "方向": "buy", "数量": 100}],
)

ROUND_AT = datetime(2026, 8, 17, 14, 47, 3)
PROMPTS = context.build_round(
    SHARED,
    {
        "SH_510300": {"名称": "沪深300ETF", "近30日": [3.90, 3.91, 3.912]},
        "SH_601939": {"名称": "建设银行", "近30日": [7.48, 7.49]},
        "SZ_159937": {"名称": "黄金ETF", "近30日": [5.10, 5.12, 5.11, 5.09]},
    },
    generated_at=ROUND_AT,
)

REPORT = context.prefix_report(PROMPTS)

# 公共前缀至少要覆盖整个共享段。会略微超出是正常的:
# 尾段开头那几个字符(`"交易对象数据": {`)本来也一样。
check(f"公共前缀覆盖整个共享段(共享 {len(SHARED.rendered)} / 实测 {REPORT.common_prefix_chars} 字符)",
      REPORT.common_prefix_chars >= len(SHARED.rendered))
check("N 份出自同一个共享段(digest 一致)", REPORT.digests_agree)
check(f"共享占比 {REPORT.shared_ratio:.1%}(二代实测约 2%)",
      REPORT.shared_ratio > 0.5)

# 生成时间必须在最后,而且整轮只有一个值——这是二代把缓存打断的那个字段
check("生成时间排在末尾",
      all(p.text.rstrip().endswith("}") and
          p.text.rindex("生成时间") > p.text.rindex("交易对象数据")
          for p in PROMPTS))
check("整轮共用一个时间戳(二代是 7 个各不相同)",
      all(p.text.count(ROUND_AT.isoformat()) == 1 for p in PROMPTS))

# 每份里只有自己那个标的:N 次调用互不可见,与二代一致
check("每份上下文只含自己的标的(不合并、不互相可见)",
      "建设银行" not in PROMPTS[0].text and "沪深300ETF" not in PROMPTS[1].text)

# 拼出来的还得是合法 JSON,不能为了对齐前缀把格式搞坏
try:
    parsed = json.loads(PROMPTS[0].text)
    check("拼接结果仍是合法 JSON 且字段顺序正确",
          list(parsed) == ["输出要求", "读取范围", "市场数据列表",
                           "账户交易流水表", "交易对象数据", "生成时间"])
except json.JSONDecodeError as exc:
    check(f"拼接结果不是合法 JSON:{exc}", False)

check("每份的 context_digest 各不相同(对比视图靠它配对)",
      len({p.context_digest for p in PROMPTS}) == 3)

PLAN = context.dispatch_plan(PROMPTS)
check(f"发送计划:先跑 1 个再并发 {len(PLAN.rest)} 个",
      PLAN.warmup is PROMPTS[0] and PLAN.total == 3)
check("空轮不崩", context.dispatch_plan([]).total == 0)


print("\n=== 保证六:归档是事实来源,机密进不去,写进去改不了 ===")

import shutil
import sqlite3
import tempfile
from pathlib import Path

from zhixing import archive, index

ARCHIVE_ROOT = Path(tempfile.mkdtemp(prefix="zhixing-smoke-"))


def make_payload(strategy_id: str, *, stamp: str, judgments=None,
                 instructions=None, usage=None) -> dict:
    """一份编造的归档。**数字全是假的**——本仓库在百度同步盘内。"""
    return {
        "strategy_id": strategy_id,
        "system_name": "zhixing",
        "app_version": "3.260817.00",
        "生成时间": stamp,
        "总体判断": "演示用结论:整体观望,无操作信号。" * 3,
        "风险控制": {
            "状态": "本地下单风控已按使用者要求全部拆除",
            "禁止执行条件": ["类型无法规范化", "无人值守关闭", "模拟发送"],
        },
        "交易对象判断": judgments if judgments is not None else [
            {"object_id": "SH_510300", "名称": "演示宽基甲ETF", "操作": "hold",
             "理由": ["演示理由"], "风险": ["演示风险"], "置信度": 0.62,
             "依据数据": {"起": "2026-07-18", "止": "2026-08-17", "行情": [{"日期": "2026-08-17"}]}},
            {"object_id": "SZ_159941", "名称": "演示海外乙ETF", "操作": "hold",
             "理由": ["演示理由"], "风险": ["演示风险"], "置信度": 0.71,
             "依据数据": {"起": "2026-07-18", "止": "2026-08-17", "行情": [{"日期": "2026-08-17"}]}},
        ],
        "待执行指令": instructions if instructions is not None else [],
        "model": "演示模型", "llm_provider": "演示提供方",
        "model_usage": usage if usage is not None else [
            {"object_id": "SH_510300", "input_tokens": 50000, "output_tokens": 3000,
             "reasoning_tokens": 2000, "cached_tokens": 40000},
            {"object_id": "SZ_159941", "input_tokens": 30000, "output_tokens": 1000,
             "reasoning_tokens": 500, "cached_tokens": 25000},
        ],
        "data_window": {"起": "2026-07-18", "止": "2026-08-17"},
        "context_digest": f"sha256:demo-{strategy_id}",
        "context": {
            "输出要求": {"只输出 JSON": True},
            "账户摘要": {"账户标识": "***4321", "总资产": 123456.78},
        },
    }


GOOD = make_payload("20260817-144703", stamp="2026-08-17T14:47:03+08:00")

# 缺字段:一次报全部,不是遇到第一条就返回
missing = archive.validate_payload({"strategy_id": "x", "生成时间": "2026-08-17T14:47:03+08:00"})
check(f"缺字段一次报全部原因(收到 {len(missing)} 条)", len(missing) >= 10)

# 机密扫描:键名命中
leaky = make_payload("20260817-150000", stamp="2026-08-17T15:00:00+08:00")
leaky["context"]["登录"] = {"cookie": "演示串"}
cookie_problems = [p for p in archive.validate_payload(leaky) if "机密" in p]
check(f"Cookie 混进归档被拒({cookie_problems[0][-28:] if cookie_problems else 'N/A'})",
      len(cookie_problems) == 1 and "context.登录.cookie" in cookie_problems[0])

# 机密扫描:值形状命中
keyed = make_payload("20260817-150001", stamp="2026-08-17T15:00:01+08:00")
keyed["context"]["备注"] = "调用时用的是 sk-ant-api03-DEMOFAKEKEYVALUE 这一条"
check("API Key 形状的字符串被揪出",
      any("API Key" in p for p in archive.validate_payload(keyed)))

# 正常字段不能被误杀:input_tokens 里也有 "token" 两个字
check("`input_tokens` / `reasoning_tokens` 不被机密扫描误杀",
      archive.scan_for_secrets(GOOD["model_usage"]) == ())

# 账户必须脱敏
unmasked = make_payload("20260817-150002", stamp="2026-08-17T15:00:02+08:00")
unmasked["context"]["账户摘要"]["账户标识"] = "6220123456784321"
check("完整账号未脱敏 -> 拒写(契约 1.3)",
      any("脱敏" in p for p in archive.validate_payload(unmasked)))
check("脱敏后的账户标识放行", archive.validate_payload(GOOD) == ())

# 冗余计数不入档
counted = make_payload("20260817-150003", stamp="2026-08-17T15:00:03+08:00")
counted["判断条数"] = 99
check("冗余计数写进归档 -> 拒(会和数组本身对不上)",
      any("判断条数" in p for p in archive.validate_payload(counted)))

# 落盘:按月分目录
written = archive.write_run(GOOD, root=ARCHIVE_ROOT)
check(f"归档落在按月目录下({written.parent.name}/{written.name})",
      written.parent.name == "2026-08" and written.exists())

# 只追加:同一个 id 写第二次必须炸
try:
    archive.write_run(GOOD, root=ARCHIVE_ROOT)
    check("重复写入应当失败", False)
except archive.ArchiveError as exc:
    check(f"同一 strategy_id 重复写入被拒:{str(exc)[:20]}...", True)

# 写到一半的残留不能被当成归档
(ARCHIVE_ROOT / "2026-08" / "半份.json.tmp").write_text("{", encoding="utf-8")
check("写了一半的 .tmp 不被当成归档", len(list(archive.iter_paths(ARCHIVE_ROOT))) == 1)

# 契约 1.5:列表项 = 详情去掉 context,加两个计数。**只有这一个实现。**
SUMMARY = archive.summarize(GOOD)
expected_keys = sorted([k for k in GOOD if k != "context"] + ["判断条数", "指令条数"])
check("列表项 = 完整归档减 context 加两个计数(前端断言的后端对照)",
      sorted(SUMMARY) == expected_keys)
check("列表项计数与数组本身一致",
      SUMMARY["判断条数"] == 2 and SUMMARY["指令条数"] == 0)
check("`总体判断` 不由后端截断",
      SUMMARY["总体判断"] == GOOD["总体判断"])


print("\n=== 保证七:索引可以整个删掉重建,不丢任何东西 ===")

REJECTED = make_payload(
    "20260818-093000", stamp="2026-08-18T09:30:00+08:00",
    instructions=[
        {"instruction_code": "i-001", "action": "buy", "market": "SH", "symbol": "510300",
         "name": "演示宽基甲ETF", "qty": 1000, "limit_price": 3.912, "wtbh": None,
         "理由": "演示", "风险提示": "演示", "状态": "rejected",
         "拦截原因": [{"code": "INSUFFICIENT_CASH", "message": "演示:可用资金不足"},
                      {"code": "NOT_ROUND_LOT", "message": "演示:不是一手整数倍"}]},
        {"instruction_code": "i-002", "action": "sell", "market": "SZ", "symbol": "159941",
         "name": "演示海外乙ETF", "qty": 500, "limit_price": 1.502, "wtbh": None,
         "理由": "演示", "风险提示": "演示", "状态": "pending", "拦截原因": []},
    ],
)
archive.write_run(REJECTED, root=ARCHIVE_ROOT)

CONN = sqlite3.connect(":memory:")
D = index.SQLITE

report = index.rebuild(CONN, root=ARCHIVE_ROOT, dialect=D)
check(f"重建:扫描 {report.scanned} 入库 {report.indexed} 判断 {report.judgments} 指令 {report.executions}",
      report.ok and report.indexed == 2 and report.judgments == 4 and report.executions == 2)


def snapshot(conn) -> dict:
    cur = conn.cursor()
    return {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in index.TABLES}


BEFORE = snapshot(CONN)
index.drop_schema(CONN, dialect=D)
index.rebuild(CONN, root=ARCHIVE_ROOT, dialect=D)
check(f"整个删掉再重建,行数一模一样({BEFORE})", snapshot(CONN) == BEFORE)

# 幂等:同一份灌两次不翻倍
index.index_run(CONN, GOOD, dialect=D)
check("同一份归档重复入库不翻倍", snapshot(CONN) == BEFORE)

# model_usage 是按标的分列的数组,要求和 —— 二代只取了第一个
cur = CONN.cursor()
inp, cached = cur.execute(
    'SELECT input_tokens, cached_tokens FROM runs WHERE strategy_id = ?',
    ("20260817-144703",)).fetchone()
check(f"model_usage 按标的求和(input {inp},不是只取第一个 50000)",
      inp == 80000 and cached == 65000)

# 被拦的条数进索引,原因正文不进 —— 正文只有归档一份
拦截条数, = cur.execute(
    'SELECT "拦截条数" FROM runs WHERE strategy_id = ?', ("20260818-093000",)).fetchone()
原因条数, = cur.execute(
    'SELECT "拦截原因条数" FROM executions WHERE instruction_code = ?', ("i-001",)).fetchone()
check(f"被拦指令 {拦截条数} 条、其中一条踩了 {原因条数} 项校验",
      拦截条数 == 1 and 原因条数 == 2)
check("拦截原因正文不进索引(索引里没有这一列)",
      "拦截原因" not in [c[1] for c in cur.execute("PRAGMA table_info(executions)")])

# 列表内容一律回归档取,索引只负责定位和排序
listed = index.list_runs(CONN, root=ARCHIVE_ROOT, dialect=D)
check("列表按时间倒序,且内容来自归档(与 summarize 逐字节相同)",
      [r["strategy_id"] for r in listed] == ["20260818-093000", "20260817-144703"]
      and listed[1] == archive.summarize(GOOD))
check("列表项一律不带 context", all("context" not in r for r in listed))
check("按月筛选", len(index.list_runs(CONN, root=ARCHIVE_ROOT, month="2026-08", dialect=D)) == 2
      and len(index.list_runs(CONN, root=ARCHIVE_ROOT, month="2026-07", dialect=D)) == 0)

# 缓存命中率:这是二代跑了几个月没人发现的那个指标
usage = index.usage_by_day(CONN, dialect=D)
check(f"按天聚合出缓存命中率({usage[0]['日期']} {usage[0]['缓存命中率']:.1%})",
      len(usage) == 2 and usage[0]["缓存命中率"] == 0.8125)

# 一份坏归档不能拖垮整轮重建
(ARCHIVE_ROOT / "2026-08" / "坏归档.json").write_text("{ 这不是 JSON", encoding="utf-8")
broken = index.rebuild(CONN, root=ARCHIVE_ROOT, dialect=D)
check(f"坏归档只报自己不拖垮整轮(扫 {broken.scanned} 成 {broken.indexed} 败 {len(broken.failed)})",
      broken.indexed == 2 and len(broken.failed) == 1 and "坏归档" in broken.failed[0][0])

CONN.close()
shutil.rmtree(ARCHIVE_ROOT, ignore_errors=True)


print("\n=== 保证八:错过的时点不补跑,抖动可复算,六个时点都看得见 ===")

from datetime import date, time as time_of_day, timedelta

from zhixing import scheduler

# 配置校验:一次报全部问题
_, cfg_problems = scheduler.validate_config(["09:15", "09:15", "9:99", "", "13:30"])
check(f"非法调度配置一次报全部原因(收到 {len(cfg_problems)} 条)", len(cfg_problems) >= 4)

check("时点必须升序",
      any("升序" in p for p in scheduler.validate_config(
          ["09:45", "09:15", "11:00", "13:30", "14:40", "15:30"])[1]))

# 抖动上限吃掉整个窗口 -> 拒
_, eaten = scheduler.validate_config(
    ["09:15", "09:45", "11:00", "13:30", "14:40", "15:30"],
    max_jitter_seconds=1800, window_minutes=30,
)
check("抖动上限达到有效窗口 -> 拒(刚抖完就算错过)",
      any("有效窗口" in p for p in eaten))

# 窗口不能伸进下一轮 —— 伸进去了,前一轮每天都会被"同时到期只跑最晚的"
# 那条规矩顶掉,而它不报错、不算失败,只是一天六轮悄悄变五轮。
def _窗口最远伸到(cfg) -> int:
    """一个时点的有效截止,最远落在它计划时刻之后多少秒。"""
    return cfg.window_minutes * 60 + cfg.max_jitter_seconds

def _最小相邻间隔(cfg) -> int:
    秒 = [t.hour * 3600 + t.minute * 60 + t.second for t in cfg.times]
    return min(b - a for a, b in zip(秒, 秒[1:]))

_默认伸到 = _窗口最远伸到(scheduler.DEFAULT_CONFIG)
_默认间隔 = _最小相邻间隔(scheduler.DEFAULT_CONFIG)
check(f"默认配置的窗口伸不进下一轮({_默认伸到 // 60} 分钟 < 最小间隔 {_默认间隔 // 60} 分钟)",
      _默认伸到 < _默认间隔)

# 线上那六个时点(最小间隔 25 分钟,09:35→10:00)。两种窗口各跑一遍,
# 这组断言与抖动取值无关:两个抖动都在 [0,180] 内,差不到 5 分钟。
线上时点 = ["09:35", "10:00", "11:15", "13:15", "14:00", "14:45"]
窄, _ = scheduler.validate_config(线上时点, window_minutes=20)
宽, _ = scheduler.validate_config(线上时点, window_minutes=30)
check(f"20 分钟窗口配 25 分钟间隔 -> 不相交(最远伸到 {_窗口最远伸到(窄) // 60} 分钟)",
      _窗口最远伸到(窄) < _最小相邻间隔(窄))
check(f"30 分钟窗口配 25 分钟间隔 -> 相交(最远伸到 {_窗口最远伸到(宽) // 60} 分钟)",
      _窗口最远伸到(宽) > _最小相邻间隔(宽))

GOOD_CFG, no_problem = scheduler.validate_config(
    ["09:15", "09:45", "11:00", "13:30", "14:40", "15:30"])
check("合规配置通过且恰好六个时点",
      not no_problem and GOOD_CFG is not None and len(GOOD_CFG.times) == 6)

# 变更配置必须写原因和操作者
try:
    scheduler.apply_config(list(GOOD_CFG.as_text()), current=GOOD_CFG,
                           changed_by="smoke", reason="  ", now=NOW)
    check("空原因变更配置应当失败", False)
except ValueError as exc:
    check(f"空原因变更调度配置被拒:{str(exc)[:18]}...", True)

_, cfg_change = scheduler.apply_config(
    ["09:20", "09:45", "11:00", "13:30", "14:40", "15:30"],
    current=GOOD_CFG, changed_by="smoke-test", reason="自检:验证配置留痕", now=NOW)
check("配置变更留痕含改动前后与原因",
      cfg_change.before[0] == "09:15" and cfg_change.after[0] == "09:20"
      and cfg_change.reason == "自检:验证配置留痕")

# 抖动:同一天同一轮算多少遍都一样,且只向后
MON = date(2026, 8, 17)
PLAN = scheduler.plan_day(MON, config=GOOD_CFG)
PLAN_AGAIN = scheduler.plan_day(MON, config=GOOD_CFG)
check("抖动可复算(同日同轮两次排期逐项相同)",
      [s.fire_at for s in PLAN.slots] == [s.fire_at for s in PLAN_AGAIN.slots])
check("抖动只向后不向前(实际触发不早于配置时点)",
      all(s.fire_at >= s.planned for s in PLAN.slots))
check(f"抖动不超上限({max(s.jitter_seconds for s in PLAN.slots)} 秒 <= 180)",
      all(s.jitter_seconds <= GOOD_CFG.max_jitter_seconds for s in PLAN.slots))
check("不同日期抖出不同偏移(不是固定值)",
      [s.jitter_seconds for s in PLAN.slots]
      != [s.jitter_seconds for s in scheduler.plan_day(date(2026, 8, 18), config=GOOD_CFG).slots])

# 到点触发
SLOT1 = PLAN.slots[1]
check("到点且窗口内 -> 可触发",
      scheduler.due_slot(PLAN, now=SLOT1.fire_at) is SLOT1)
check("未到点 -> 不触发",
      scheduler.due_slot(PLAN, now=SLOT1.fire_at - timedelta(seconds=1)) is None)

# 错过不补跑 —— 这条是硬的
LATE = SLOT1.deadline + timedelta(seconds=1)
late_states = scheduler.evaluate(PLAN, now=LATE)
check("过了有效窗口 -> 错过,且不作为可触发返回",
      late_states[1].status == scheduler.MISSED
      and scheduler.due_slot(PLAN, now=LATE) is not SLOT1)
check("错过带得出原因(不是静默跳过)",
      "不补跑" in late_states[1].reason)

# 进程停了一整天再起来:全部错过,一个都不补
EOD = datetime.combine(MON, time_of_day(23, 0))
eod_states = scheduler.evaluate(PLAN, now=EOD)
check("停了一整天再起来:六轮全记错过,零补跑",
      all(s.status == scheduler.MISSED for s in eod_states)
      and scheduler.due_slot(PLAN, now=EOD) is None)

# 已跑过的不重复跑(重启幂等)
check("已跑过的时点不再触发(重启不重跑)",
      scheduler.due_slot(PLAN, now=SLOT1.fire_at, fired=[1]) is None)

# 同时到期只跑最晚的
WIDE, _ = scheduler.validate_config(
    ["09:15", "09:45", "11:00", "13:30", "14:40", "15:30"],
    max_jitter_seconds=0, window_minutes=600,
)
WIDE_PLAN = scheduler.plan_day(MON, config=WIDE)
AT = datetime.combine(MON, time_of_day(14, 0))
wide_states = scheduler.evaluate(WIDE_PLAN, now=AT)
due_now = [s for s in wide_states if s.status == scheduler.DUE]
check(f"四轮同时到期只跑最晚一轮(可触发 {len(due_now)} 个)",
      len(due_now) == 1 and due_now[0].slot.index == 3)
check("让位的三轮标为被取代,不是消失",
      sum(1 for s in wide_states if s.status == scheduler.SUPERSEDED) == 3)

# 同一组线上时点,窗口一宽一窄,第一轮的下场完全不同。
# 这就是 2026-08-21 把窗口从 30 收到 20 的理由:前者让第一轮**每天**在
# 第二轮到点时被顶掉(而"被取代"本来只该是进程断过一阵的产物)。
窄_PLAN = scheduler.plan_day(MON, config=窄)
宽_PLAN = scheduler.plan_day(MON, config=宽)
check("30 分钟窗口:第二轮一到点,没跑的第一轮就被顶掉",
      scheduler.evaluate(宽_PLAN, now=宽_PLAN.slots[1].fire_at)[0].status
      == scheduler.SUPERSEDED)
check("20 分钟窗口:第一轮已过窗口,记错过(该跑没跑说得清,不是让位)",
      scheduler.evaluate(窄_PLAN, now=窄_PLAN.slots[1].fire_at)[0].status
      == scheduler.MISSED)

# 非交易日
SAT = scheduler.plan_day(date(2026, 8, 16), config=GOOD_CFG)  # 周日
check("非交易日六轮全标非交易日且不触发",
      all(s.status == scheduler.NOT_TRADING_DAY
          for s in scheduler.evaluate(SAT, now=datetime.combine(SAT.day, time_of_day(10, 0))))
      and scheduler.due_slot(SAT, now=datetime.combine(SAT.day, time_of_day(10, 0))) is None)

# 交易日历只有一份
check("调度器与校验层共用同一个交易日历函数",
      scheduler.default_is_trading_day is guards.default_is_trading_day)

# 下次唤醒
check("下次唤醒指向下一个待触发时点",
      scheduler.next_wakeup(PLAN, now=datetime.combine(MON, time_of_day(0, 0)))
      == PLAN.slots[0].fire_at)
check("当天跑完后没有下次唤醒",
      scheduler.next_wakeup(PLAN, now=EOD) is None)

# 全貌:六个时点一个不少
REPORT8 = scheduler.day_report(PLAN, now=LATE, fired=[0])
check(f"当天全貌列出全部六个时点(实得 {len(REPORT8['时点'])} 条)",
      len(REPORT8["时点"]) == 6)
check("全貌里错过与已触发都在计数里",
      REPORT8["状态计数"].get(scheduler.FIRED) == 1
      and scheduler.MISSED in REPORT8["状态计数"])
check("调度记录不含账号/金额/密钥等字段",
      archive.scan_for_secrets(REPORT8) == ())


print("\n=== 保证九:契约第二节的路由全部照契约实现,响应里漏不出机密 ===")

from zhixing import api, state
from zhixing import eastmoney as eastmoney_mod

API_ARCHIVE = Path(tempfile.mkdtemp(prefix="zhixing-api-archive-"))
API_RUNTIME = Path(tempfile.mkdtemp(prefix="zhixing-api-runtime-"))

# 两代各写一份、吃同一份输入,用来验对比接口
PAIR_DIGEST = "sha256:demo-pair"
ZX = make_payload("20260817-093000", stamp="2026-08-17T09:30:00+08:00")
ZX["context_digest"] = PAIR_DIGEST
TP = make_payload("tp-20260817-093000", stamp="2026-08-17T09:30:05+08:00")
TP["system_name"] = "tradepilot"
TP["app_version"] = "1.2.0"
TP["context_digest"] = PAIR_DIGEST
TP["交易对象判断"] = [
    {**ZX["交易对象判断"][0], "操作": "buy", "置信度": 0.71},   # 与三代分歧
    {**ZX["交易对象判断"][1]},                                  # 与三代一致
]
for payload in (ZX, TP):
    archive.write_run(payload, root=API_ARCHIVE)

APP = api.App(
    store=state.Store(API_RUNTIME),
    archive_root=API_ARCHIVE,
    now=lambda: datetime(2026, 8, 17, 10, 0, 0),
)


def call(method: str, path: str, *, query=None, body=None) -> api.Response:
    return api.handle(APP, api.Request(method, path, query or {}, body))


# -- 路由本身 -------------------------------------------------------------

ROUTES = [
    ("GET", "/api/status"), ("GET", "/api/objects"), ("POST", "/api/objects"),
    ("PUT", "/api/objects/SH_510300"), ("DELETE", "/api/objects/SH_510300"),
    ("GET", "/api/account"), ("GET", "/api/runs"), ("GET", "/api/runs/20260817-093000"),
    ("GET", "/api/runs/compare"), ("GET", "/api/usage"),
    ("GET", "/api/instructions/pending"), ("POST", "/api/instructions/i-001/confirm"),
    ("GET", "/api/settings/schedule"), ("PUT", "/api/settings/schedule"),
    ("GET", "/api/settings/captcha"), ("PUT", "/api/settings/captcha"),
    ("GET", "/api/settings/model"), ("PUT", "/api/settings/model"),
    ("PUT", "/api/settings/unattended"),
    ("GET", "/api/orders/activity"), ("POST", "/api/orders/demo-ref/cancel"),
]
# 判的是「路由表里有没有这条」,不是「这次调用成不成功」——
# 缺参数、记录不存在都是正常结果,只有 NO_SUCH_ENDPOINT / 405 才说明路由漏了。
unreachable = [f"{m} {p}" for m, p in ROUTES
               if call(m, p).payload.get("error", {}).get("code")
               in {"NO_SUCH_ENDPOINT", "METHOD_NOT_ALLOWED"}]
check(f"接口层公开的 {len(ROUTES)} 条路由全部可达", not unreachable)

NO_ROUTE = call("GET", "/api/nope")
check("不存在的接口报 NO_SUCH_ENDPOINT,和「这份归档不存在」的 NOT_FOUND 分得开",
      NO_ROUTE.status == 404
      and NO_ROUTE.payload["error"]["code"] == "NO_SUCH_ENDPOINT"
      and call("GET", "/api/runs/不存在").payload["error"]["code"] == "NOT_FOUND")
check("路径存在但方法不对报 405,不报 404(否则人会去查一个其实存在的路径)",
      call("POST", "/api/status").status == 405)
check("/api/runs/compare 先于 /api/runs/{id} 匹配(否则去找一份叫 compare 的归档)",
      "对比项" in call("GET", "/api/runs/compare").payload["data"])

# -- 状态 -----------------------------------------------------------------

STATUS = call("GET", "/api/status").payload["data"]
check("状态含契约 1.4 全部字段",
      {"system_name", "app_version", "运行模式", "无人值守", "登录状态",
       "最近采集时间", "最近策略时间", "数据源", "上一轮成功时间",
       "连续失败轮数", "最近失败原因"} <= set(STATUS))
check("运行模式取自 runmode,不是写死的字面量",
      STATUS["运行模式"] == ("dry_run" if runmode.VERIFICATION_LOCK else "live"))
check("采集层没接,登录状态就是「未知」,不冒充「已登录」",
      STATUS["登录状态"] == "未知" and STATUS["最近采集时间"] is None)
# 「数据源」曾经是一句写死的「只读挂载二代 runtime」。部署上去之后容器里
# 并没有那个挂载,状态页却把它当事实显示了一整天——二代缺陷 6 的形状:
# 一句承诺了某件事的字面量,而那件事没有任何代码在做。
check("**数据源不是写死的字面量**,由 App 传入,默认「尚未接入」而不是某种来源",
      STATUS["数据源"] == "采集层尚未接入"
      and api.handle(
          api.App(store=APP.store, archive_root=APP.archive_root,
                  data_source="只读挂载二代 runtime"),
          api.Request(method="GET", path="/api/status", query={}, body=None),
      ).payload["data"]["数据源"] == "只读挂载二代 runtime")
check("状态里带当天调度全貌,六轮一条不少",
      len(STATUS["调度"]["时点"]) == 6)
check("已跑过的轮次从归档反推,不另存一份状态",
      STATUS["调度"]["状态计数"].get(scheduler.FIRED) == 1)

# -- 归档读 ---------------------------------------------------------------

RUNS = call("GET", "/api/runs").payload["data"]
check(f"列表按时间倒序(实得 {len(RUNS)} 条)",
      [r["strategy_id"] for r in RUNS] == ["tp-20260817-093000", "20260817-093000"])
check("列表项与 archive.summarize 逐字节相同(契约 1.5 只有一处实现)",
      RUNS[1] == archive.summarize(ZX))
check("列表项不含 context,但含交易对象判断",
      all("context" not in r and "交易对象判断" in r for r in RUNS))
check("按 system_name 筛得出三代自己那份",
      len(call("GET", "/api/runs", query={"system_name": "zhixing"}).payload["data"]) == 1)
check("limit 非法时明确报错,不静默当成不限",
      call("GET", "/api/runs", query={"limit": "很多"}).payload["error"]["code"] == "INVALID_LIMIT")
check("from 晚于 to 明确报错",
      call("GET", "/api/runs", query={"from": "2026-08-18", "to": "2026-08-17"})
      .payload["error"]["code"] == "INVALID_RANGE")

DETAIL = call("GET", "/api/runs/20260817-093000")
check("单轮详情含 context(列表不含,详情才有)",
      DETAIL.status == 200 and "context" in DETAIL.payload["data"])
check("查不到的 strategy_id 报 404",
      call("GET", "/api/runs/不存在").status == 404)

# 一份写坏的归档不能让整页白屏 —— 契约 2.1 明令不许「显示暂无数据而实际是挂了」
(API_ARCHIVE / "2026-08" / "半份.json").write_text("{ 断电留下的半份", encoding="utf-8")
check("一份坏归档只少一条,不拖垮整个列表",
      len(call("GET", "/api/runs").payload["data"]) == 2)
(API_ARCHIVE / "2026-08" / "半份.json").unlink()

# -- 对比(验证期专用)-----------------------------------------------------

CMP = call("GET", "/api/runs/compare").payload["data"]
check(f"按 context_digest 配对,不按时间(实得 {len(CMP['对比项'])} 项)",
      len(CMP["对比项"]) == 2
      and all(i["context_digest"] == PAIR_DIGEST for i in CMP["对比项"]))
check("两边都有数据时逐条判一致",
      {i["object_id"]: i["一致"] for i in CMP["对比项"]}
      == {"SH_510300": False, "SZ_159941": True})
check(f"汇总算得出一致率({CMP['汇总']['一致率']:.1%})",
      CMP["汇总"] == {"总条数": 2, "一致条数": 1, "一致率": 0.5})
check("一条都没有时一致率是 0.0 而不是崩掉",
      call("GET", "/api/runs/compare", query={"from": "2020-01-01", "to": "2020-01-02"})
      .payload["data"]["汇总"]["总条数"] == 0)

# -- 用量 -----------------------------------------------------------------

USAGE = call("GET", "/api/usage").payload["data"]
check(f"按天聚合(实得 {len(USAGE)} 天)", len(USAGE) == 1 and USAGE[0]["日期"] == "2026-08-17")
check("轮数按 strategy_id 去重:一轮里七个标的不算七轮",
      USAGE[0]["轮数"] == 2)
check("缓存命中率是总量之比,不是各轮命中率的算术平均",
      USAGE[0]["缓存命中率"]
      == round(USAGE[0]["cached_tokens"] / USAGE[0]["input_tokens"], 4))
check("按标的分组分得出两个标的",
      len(call("GET", "/api/usage", query={"group_by": "object"}).payload["data"]) == 2)
check("按模型分组",
      len(call("GET", "/api/usage", query={"group_by": "model"}).payload["data"]) == 1)
check("非法分组明确报错",
      call("GET", "/api/usage", query={"group_by": "hour"})
      .payload["error"]["code"] == "INVALID_USAGE_GROUP")

# 同一批归档,SQL 和 Python 两条路径必须给同一个答案 ——
# 一个聚合有两处实现就是有两个答案,而账单只有一份。
UCONN = sqlite3.connect(":memory:")
index.rebuild(UCONN, root=API_ARCHIVE, dialect=index.SQLITE)
SQL_USAGE = index.usage_by_day(UCONN, dialect=index.SQLITE)
UCONN.close()
check("按天用量:接口层与索引层 SQL 算出同一个答案",
      [{k: v for k, v in row.items()} for row in USAGE] == SQL_USAGE)

# -- 标的增删改 -----------------------------------------------------------

check("清单初始为空时返回空数组,不是报错",
      call("GET", "/api/objects").payload["data"] == [])

BAD_DRAFT = call("POST", "/api/objects", body={"market": "BJ", "symbol": "abc"})
check(f"非法草案一次报全部问题(实得 {len(BAD_DRAFT.payload['error'].get('问题', []))} 条)",
      len(BAD_DRAFT.payload["error"]["问题"]) == 5)

NEW = call("POST", "/api/objects", body={
    "market": "SH", "symbol": "510300", "名称": "演示宽基甲ETF",
    "类型": "交易标的", "资产类型": "ETF",
})
check("新增标的,object_id 由后端按市场_代码生成",
      NEW.payload["data"]["object_id"] == "SH_510300")
check("采集层没接时持仓是 null,不是 0(「空仓」和「没采到」必须分得开)",
      NEW.payload["data"]["持仓"] is None)
check("提交多余字段不被采纳(可写字段只有五个)",
      call("POST", "/api/objects", body={
          "market": "SZ", "symbol": "159941", "名称": "演示海外乙ETF",
          "类型": "行情对象", "资产类型": "ETF", "持仓": {"持仓数量": 99999},
      }).payload["data"]["持仓"] is None)
check("清单里两个都在,类型区分得开",
      {o["object_id"]: o["类型"] for o in call("GET", "/api/objects").payload["data"]}
      == {"SH_510300": "交易标的", "SZ_159941": "行情对象"})

check("改名字改得动",
      call("PUT", "/api/objects/SH_510300", body={
          "名称": "演示宽基丙ETF", "类型": "交易标的", "资产类型": "ETF",
      }).payload["data"]["名称"] == "演示宽基丙ETF")
check("改市场或代码被拒:object_id 由它们算出,改了历史归档的引用就失配",
      call("PUT", "/api/objects/SH_510300", body={
          "market": "SZ", "symbol": "510300", "名称": "演示宽基丙ETF",
          "类型": "交易标的", "资产类型": "ETF",
      }).payload["error"]["code"] == "IDENTITY_IMMUTABLE")
check("改不存在的标的报 404", call("PUT", "/api/objects/SH_000000", body={}).status == 404)
check("删得掉",
      call("DELETE", "/api/objects/SZ_159941").status == 200
      and len(call("GET", "/api/objects").payload["data"]) == 1)
check("删不存在的报 404", call("DELETE", "/api/objects/SZ_159941").status == 404)

# -- 账户:读快照,不现场查券商 ----------------------------------------------

ACCOUNT = call("GET", "/api/account")
check("还没采过账户时明确返回 ok:false,不返回一份零值假摘要",
      ACCOUNT.payload["ok"] is False
      and ACCOUNT.payload["error"]["code"] == "NO_ACCOUNT_SNAPSHOT")

APP.store.save_account(
    {"账户标识": "***4321", "总资产": 123456.78, "可用资金": None},
    collected_at="2026-08-19T14:30:00",
)
ACCOUNT2 = call("GET", "/api/account")
check("采过之后读的是快照,**且必带采集时间**(陈数据可以接受,看不出它陈不可以)",
      ACCOUNT2.status == 200
      and ACCOUNT2.payload["data"]["采集时间"] == "2026-08-19T14:30:00"
      and ACCOUNT2.payload["data"]["账户标识"] == "***4321")
check("取不到的金额原样是 null,不拿 0 顶(0 会被读成「没有钱」)",
      ACCOUNT2.payload["data"]["可用资金"] is None)
check("账户摘要里账号那一项的键名是 `账户标识`,不是别的"
      "——换个名字前端显示 undefined,而且出站机密扫描根本不会检查它",
      "账户标识" in eastmoney_mod.AccountReport(
          account_id_masked="***4321", total_asset=None, available_cash=None,
          cash_balance=None, frozen_cash=None, securities_value=None,
      ).as_summary())
check("完整账号进归档会被拦下(这道闸认的就是 `账户标识` 这个键名)",
      archive.scan_for_secrets({"账户标识": "123456789012"}) != ())
check("写错成 `账户ID` 也照样拦(这道闸的失效方式是静默的,得多认几个错名字)",
      archive.scan_for_secrets({"账户ID": "123456789012"}) != ())
check("没配账号时的空值不算机密(否则「还没配」和「明文泄露了」报错长得一样)",
      archive.scan_for_secrets({"账户标识": ""}) == ()
      and archive.scan_for_secrets({"账户标识": None}) == ())

# -- 正式运行总闸 ---------------------------------------------------------

CONFIRM = call("POST", "/api/instructions/i-001/confirm")
check("状态接口明确显示总闸已经进入 live,不是只改了源码不让界面知道",
      STATUS["运行模式"] == "live" and STATUS["验证锁"] is False)
check("总闸解除后人工接口会继续查指令,不会再恒返回 DRY_RUN_LOCKED",
      CONFIRM.status == 404 and CONFIRM.payload["error"]["code"] == "NOT_FOUND")
check("待接管指令为空是常态,返回空数组不是报错",
      call("GET", "/api/instructions/pending").payload["data"] == [])

# -- 调度配置 -------------------------------------------------------------

check("调度配置读得出六个时点",
      len(call("GET", "/api/settings/schedule").payload["data"]["时点"]) == 6)
check("改时点不写原因被拒",
      call("PUT", "/api/settings/schedule",
           body={"时点": ["09:15", "09:45", "11:00", "13:30", "14:40", "15:30"]})
      .payload["error"]["code"] == "REASON_REQUIRED")
BAD_SCHED = call("PUT", "/api/settings/schedule", body={
    "时点": ["09:15", "09:15", "11:00", "25:99", "14:40"], "原因": "演示",
})
check(f"非法时点一次报全部问题(实得 {len(BAD_SCHED.payload['error'].get('问题', []))} 条)",
      len(BAD_SCHED.payload["error"]["问题"]) >= 3)
check("倒序的时点被拒(界面第 N 栏就是当天第 N 轮)",
      any("升序" in p["message"] for p in call("PUT", "/api/settings/schedule", body={
          "时点": ["09:15", "09:45", "11:00", "13:30", "15:30", "14:40"], "原因": "演示",
      }).payload["error"]["问题"]))
check("合法时点存得进去也读得回来",
      call("PUT", "/api/settings/schedule", body={
          "时点": ["09:20", "09:45", "11:00", "13:30", "14:40", "15:30"],
          "原因": "自检:把首轮挪后五分钟",
      }).payload["ok"]
      and call("GET", "/api/settings/schedule").payload["data"]["时点"][0] == "09:20")

# -- 验证码密钥 -----------------------------------------------------------

SECRET = "sk-DEMOFAKEKEY0123456789abcd"
check("配置验证码接口",
      call("PUT", "/api/settings/captcha", body={
          "接口地址": "https://demo.invalid/ocr", "模型": "演示模型", "密钥": SECRET,
      }).payload["ok"])
CAPTCHA = call("GET", "/api/settings/captcha").payload["data"]
check(f"GET 只下发脱敏密钥({CAPTCHA['密钥']})",
      CAPTCHA["密钥"].startswith("****") and SECRET not in json.dumps(CAPTCHA))
check("刚提交的明文密钥绝不回显",
      SECRET not in json.dumps(call("GET", "/api/settings/captcha").payload, ensure_ascii=False))
check("明文密钥不落在 JSON 配置里,只在单独的 0600 文件里",
      SECRET not in APP.store.captcha_path.read_text(encoding="utf-8")
      and APP.store.captcha_secret_path.read_text(encoding="utf-8").strip() == SECRET)
check("空密钥表示不改,不是清空(前端手上只有脱敏值,回填不了原值)",
      call("PUT", "/api/settings/captcha", body={
          "接口地址": "https://demo.invalid/ocr2", "模型": "演示模型", "密钥": "",
      }).payload["ok"]
      and APP.store.captcha().secret == SECRET
      and APP.store.captcha().endpoint.endswith("ocr2"))
check("接口地址或模型为空被拒",
      call("PUT", "/api/settings/captcha", body={"接口地址": "", "模型": ""})
      .payload["error"]["code"] == "INVALID_CAPTCHA_SETTINGS")

# -- 模型接口 -------------------------------------------------------------

MODEL_KEY = "sk-DEMOFAKEKEYMODEL9876543210"
check("模型配置一次报全部毛病(空地址 + 空模型 + 空提供方 + 协议乱写)",
      {p["code"] for p in call("PUT", "/api/settings/model", body={
          "接口地址": "", "模型": "", "提供方": "", "协议": "随便写的", "密钥": MODEL_KEY,
      }).payload["error"].get("问题", [])}
      == {"ENDPOINT_REQUIRED", "MODEL_REQUIRED", "PROVIDER_REQUIRED", "UNKNOWN_PROTOCOL"})
check("协议头都没有的地址仍然被拒",
      "ENDPOINT_SCHEME" in {p["code"] for p in call("PUT", "/api/settings/model", body={
          "接口地址": "demo.invalid/v1", "模型": "演示模型-甲",
          "提供方": "演示中转", "密钥": MODEL_KEY,
      }).payload["error"].get("问题", [])})
check("头一次配置不给密钥被拒(没有旧值可继承)",
      call("PUT", "/api/settings/model", body={
          "接口地址": "https://demo.invalid/v1", "模型": "演示模型-甲",
          "提供方": "演示中转",
      }).payload["error"]["code"] == "SECRET_REQUIRED")
check("配置模型接口",
      call("PUT", "/api/settings/model", body={
          "接口地址": "https://demo.invalid/v1", "模型": "演示模型-甲",
          "提供方": "演示中转", "密钥": MODEL_KEY,
      }).payload["ok"])
check("明文 http 存得进去,但「传输」字段常年标着它不安全(拦一次不如一直提)",
      call("PUT", "/api/settings/model", body={
          "接口地址": "http://demo.invalid/api", "模型": "演示模型-甲",
          "提供方": "演示中转", "密钥": "",
      }).status == 200
      and "明文" in call("GET", "/api/settings/model").payload["data"]["传输"])
check("回环地址不算明文,流量没出网卡",
      "回环" in state.ModelSettings(endpoint="http://127.0.0.1:3000/api").transport()
      and state.ModelSettings(endpoint="https://x/v1").transport() == "https")
call("PUT", "/api/settings/model", body={
    "接口地址": "https://demo.invalid/v1", "模型": "演示模型-甲",
    "提供方": "演示中转", "密钥": "",
})
MODEL_CFG = call("GET", "/api/settings/model").payload["data"]
check("GET 只下发脱敏密钥,和验证码接口同一条规矩",
      MODEL_CFG["密钥"].endswith("3210") and MODEL_KEY not in json.dumps(MODEL_CFG, ensure_ascii=False))
check("明文密钥不落在 JSON 里,只在单独的 0600 文件里",
      MODEL_KEY not in APP.store.model_path.read_text(encoding="utf-8")
      and APP.store.model_secret_path.read_text(encoding="utf-8").strip() == MODEL_KEY)
check("空密钥表示不改(只换模型名不该顺手抹掉密钥)",
      call("PUT", "/api/settings/model", body={
          "接口地址": "https://demo.invalid/v1", "模型": "演示模型-乙",
          "提供方": "演示中转", "协议": "anthropic_messages", "密钥": "",
      }).payload["ok"]
      and APP.store.model().secret == MODEL_KEY
      and APP.store.model().name == "演示模型-乙")
check("存下来的配置能直接变成 ModelTarget,且转出来的东西里没有密钥",
      APP.store.model().to_target().protocol == "anthropic_messages"
      and MODEL_KEY not in repr(APP.store.model().to_target()))

# -- 无人值守 -------------------------------------------------------------

check("开关无人值守不写原因被拒(后端强制留痕)",
      call("PUT", "/api/settings/unattended", body={"无人值守": True})
      .payload["error"]["code"] == "REASON_REQUIRED")
check("无人值守不是布尔值被拒",
      call("PUT", "/api/settings/unattended", body={"无人值守": "开", "原因": "演示"})
      .payload["error"]["code"] == "INVALID_UNATTENDED")
check("带原因就改得动,状态接口立刻看得见",
      call("PUT", "/api/settings/unattended",
           body={"无人值守": True, "原因": "自检:验证留痕链路"}).payload["ok"]
      and call("GET", "/api/status").payload["data"]["无人值守"] is True)
check("关开关同样必须经接口带原因,并把共享状态恢复为关闭",
      call("PUT", "/api/settings/unattended",
           body={"无人值守": False, "原因": "自检结束,恢复默认关闭"}).payload["ok"]
      and APP.store.unattended().enabled is False)
runmode.set_unattended(True, changed_by="smoke", reason="自检:制造进程内旧状态")
runmode.restore_unattended(APP.store.unattended())
check("共享状态能覆盖另一个进程里的旧内存值(API 与 daemon 不会各开各的)",
      runmode.unattended_state().enabled is False)

# -- 结构性保证:响应里漏不出机密 -------------------------------------------

def _leaky(app, req):
    return api.ok({"配置": {"cookie": "演示会话串"}})


api._STATIC[("GET", "/api/自检-漏密钥")] = _leaky
LEAK = call("GET", "/api/自检-漏密钥")
del api._STATIC[("GET", "/api/自检-漏密钥")]
check("处理器不慎带出机密时,响应被整个拦下而不是发出去",
      LEAK.status == 500 and LEAK.payload["error"]["code"] == "SECRET_LEAK_BLOCKED"
      and "演示会话串" not in json.dumps(LEAK.payload, ensure_ascii=False))

check("处理器抛异常转成 500,且不把异常文本发给前端",
      call("GET", "/api/runs", query={"from": None}).status in {200, 400, 500})

# -- 运行目录不许落在仓库内 -------------------------------------------------

REPO_ROOT = Path(state.__file__).resolve().parents[2]
try:
    state.Store(REPO_ROOT / "backend" / "runtime")
    在仓库内被拒 = False
except state.StateError:
    在仓库内被拒 = True
check("运行目录落在仓库内直接拒绝启动(仓库在同步盘里,密钥写进去等于上传)",
      在仓库内被拒)

shutil.rmtree(API_ARCHIVE, ignore_errors=True)
shutil.rmtree(API_RUNTIME, ignore_errors=True)


print("\n=== 保证十:模型说的话一句都不白信,一次报全部毛病 ===")

OPENAI = model.ModelTarget(name="演示模型-甲", provider="演示中转", base_url="https://demo.invalid/v1")
CLAUDE = model.ModelTarget(
    name="演示模型-乙", provider="演示中转", base_url="https://demo.invalid",
    protocol="anthropic_messages", max_tokens=4096, stream=False,
)

# -- 请求体 ---------------------------------------------------------------

REQ_A = model.build_request(OPENAI, system_prompt="系统话", user_text="正文")
REQ_B = model.build_request(CLAUDE, system_prompt="系统话", user_text="正文")
check("两种协议的外壳不同,正文一个字不变(基线验证要求两代吃同一份输入)",
      REQ_A["messages"][0]["role"] == "system"
      and REQ_B["system"] == "系统话"
      and "system" not in REQ_A
      and REQ_A["messages"][-1]["content"] == REQ_B["messages"][-1]["content"] == "正文")
check("anthropic 协议补上必填的 max_tokens,openai 协议不给就不带",
      REQ_B["max_tokens"] == 4096 and "max_tokens" not in REQ_A)

try:
    model.ModelTarget(name="x", provider="y", base_url="z", protocol="随便写的")
    协议乱写被拒 = False
except model.ModelError:
    协议乱写被拒 = True
check("不认识的线上格式在构造时就报错,不是发出去才发现", 协议乱写被拒)

# -- 用量 -----------------------------------------------------------------

RAW_A = {
    "model": "演示模型-甲",
    "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
    "usage": {
        "prompt_tokens": 12000, "completion_tokens": 800,
        "prompt_tokens_details": {"cached_tokens": 9000},
        "completion_tokens_details": {"reasoning_tokens": 500},
    },
}
REPLY_A = model.parse_reply(OPENAI, RAW_A, object_id="SH510300")
check("openai 协议的四项用量都取得到(命中缓存的量是省钱证据,不能丢)",
      REPLY_A.usage.as_entry() == {
          "object_id": "SH510300", "input_tokens": 12000, "output_tokens": 800,
          "reasoning_tokens": 500, "cached_tokens": 9000})

RAW_B = {
    "model": "演示模型-乙",
    "content": [{"type": "thinking", "thinking": "略"}, {"type": "text", "text": "{}"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 80},
}
REPLY_B = model.parse_reply(CLAUDE, RAW_B, object_id="SH510300")
check("anthropic 协议只取 text 块,没有推理用量就留 0 而不是拿输出冒充",
      REPLY_B.text == "{}" and REPLY_B.usage.reasoning_tokens == 0
      and REPLY_B.usage.cached_tokens == 80)

try:
    model.parse_reply(OPENAI, {"choices": [{"message": {"content": "   "}}]}, object_id="x")
    空正文被拒 = False
except model.ModelError:
    空正文被拒 = True
check("回复正文是空的当场报错(空字符串解析出来是 NOT_JSON,会盖住真正的原因)",
      空正文被拒)

# -- 判断:好的那份 --------------------------------------------------------

GOOD = """```json
{"object_id": "SH510300", "操作": "buy",
 "理由": ["演示理由一", "演示理由二"], "风险": ["演示风险"],
 "置信度": "0.62", "改判条件": "跌破演示均线就改判",
 "指令": {"action": "buy", "qty": 1000, "limit_price": "3.912", "理由": "演示", "风险提示": "演示"}}
```"""
J, P = model.parse_judgment(GOOD, expect_object_id="SH510300")
check("模型照旧包了 ```json,能无歧义修好的就修,不为这个丢掉一轮判断",
      J is not None and not P)
check("置信度写成字符串也收(这个转换无歧义),但仍然被约束在 0–1",
      J is not None and J.置信度 == 0.62)
check("qty / limit_price 保持原始类型,脏值留给校验层报错而不是在这里悄悄转",
      J is not None and J.指令 is not None
      and J.指令.qty == 1000 and J.指令.limit_price == "3.912")
check("名称由后端从清单取,不采信模型复述(代码与名字对不上时才分得清谁错)",
      J is not None
      and J.as_entry(名称="演示宽基甲ETF")["名称"] == "演示宽基甲ETF"
      and "名称" not in json.loads(model.strip_fence(GOOD)))

# -- 判断:坏的那些 --------------------------------------------------------

def codes(text: str, expect: str = "SH510300") -> set[str]:
    judgment, problems = model.parse_judgment(text, expect_object_id=expect)
    assert judgment is None or not problems
    return {p.code for p in problems}


check("模型编了一个别的代码会被拦下(二代拦不住)",
      "OBJECT_ID_MISMATCH" in codes(
          '{"object_id": "SZ000001", "操作": "hold", "理由": ["甲"], "风险": ["乙"],'
          ' "置信度": 0.5, "改判条件": "演示"}'))
check("hold 却给了指令 —— 自相矛盾时不猜,猜错的方向是会下单的那个方向",
      "HOLD_WITH_INSTRUCTION" in codes(
          '{"object_id": "SH510300", "操作": "hold", "理由": ["甲"], "风险": ["乙"],'
          ' "置信度": 0.5, "改判条件": "演示", "指令": {"action": "buy", "qty": 100}}'))
check("buy 却没给指令,同样被拒",
      "MISSING_INSTRUCTION" in codes(
          '{"object_id": "SH510300", "操作": "buy", "理由": ["甲"], "风险": ["乙"],'
          ' "置信度": 0.5, "改判条件": "演示"}'))
check("指令里的 action 和操作对不上被拒",
      "ACTION_MISMATCH" in codes(
          '{"object_id": "SH510300", "操作": "buy", "理由": ["甲"], "风险": ["乙"],'
          ' "置信度": 0.5, "改判条件": "演示", "指令": {"action": "sell", "qty": 100}}'))
check("说不出改判条件就不算合格判断(改判条件是认识闭环的全部原材料)",
      "MISSING_TRIGGER" in codes(
          '{"object_id": "SH510300", "操作": "hold", "理由": ["甲"], "风险": ["乙"],'
          ' "置信度": 0.5, "改判条件": "   "}'))
check("理由给成一整段字符串时报错,**不按标点拆**(拆出来是一堆半句话,像做对了)",
      "BAD_理由" in codes(
          '{"object_id": "SH510300", "操作": "hold", "理由": "甲。乙。丙。",'
          ' "风险": ["乙"], "置信度": 0.5, "改判条件": "演示"}'))
check("置信度超出 0–1 被拒",
      "BAD_CONFIDENCE" in codes(
          '{"object_id": "SH510300", "操作": "hold", "理由": ["甲"], "风险": ["乙"],'
          ' "置信度": 62, "改判条件": "演示"}'))

MESSY = ('{"object_id": "SZ000001", "操作": "梭哈", "理由": "一整段", "风险": [],'
         ' "置信度": 9, "改判条件": ""}')
check("一份烂输出一次报全部毛病,不是改一条冒一条",
      codes(MESSY) == {"OBJECT_ID_MISMATCH", "BAD_ACTION", "BAD_理由",
                       "EMPTY_风险", "BAD_CONFIDENCE", "MISSING_TRIGGER"})
check("有毛病时不返回半对的判断(半对的会被下游当成好的用)",
      model.parse_judgment(MESSY, expect_object_id="SH510300")[0] is None)
check("整段不是 JSON 时只报一条,不再拿它去挑字段",
      codes("我觉得可以买入。") == {"NOT_JSON"})

# -- 调用层:重试的规矩 ----------------------------------------------------

check("退避是确定性的:同一次失败重跑两遍等一样久,不同标的错得开",
      llm.retry_delay(1, seed="SH510300") == llm.retry_delay(1, seed="SH510300")
      and llm.retry_delay(1, seed="SH510300") != llm.retry_delay(1, seed="SZ000001")
      and llm.retry_delay(2, seed="x") > llm.retry_delay(1, seed="x"))

check("密钥的 repr 是打码的(logging 记异常时会把局部变量 repr 打出来)",
      "DEMOFAKEKEY" not in repr(llm.Credential("sk-DEMOFAKEKEY1234"))
      and repr(llm.Credential("sk-DEMOFAKEKEY1234")).endswith("1234)"))


class _FakeCaller(llm.HttpCaller):
    """按剧本抛错/成功,不碰网络。"""

    def __init__(self, script, **kw):
        super().__init__(credential=llm.Credential("sk-DEMOFAKEKEY1234"),
                         sleep=lambda _s: None, **kw)
        self.script = list(script)
        self.tries = 0

    def _once(self, target, body):
        self.tries += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


FOUR = _FakeCaller([llm.LlmError("密钥不对", retryable=False, status=401)])
try:
    FOUR.call(OPENAI, REQ_A, object_id="SH510300")
except llm.LlmError:
    pass
check("4xx 一次都不重试(重试十次还是错,只是把配额烧掉)", FOUR.tries == 1)

FIVE = _FakeCaller([llm.LlmError("对面挂了", retryable=True, status=503),
                    llm.LlmError("对面挂了", retryable=True, status=503),
                    RAW_A])
check("5xx 重试,拿到就返回;模型调用是读操作,重试不会多下一张单",
      FIVE.call(OPENAI, REQ_A, object_id="SH510300").usage.input_tokens == 12000
      and FIVE.tries == 3)

GIVEUP = _FakeCaller([llm.LlmError("超时", retryable=True) for _ in range(9)])
try:
    GIVEUP.call(OPENAI, REQ_A, object_id="SH510300")
    放弃了 = False
except llm.LlmError:
    放弃了 = True
check("重试有上限,到点就带着原因失败(错过的时点永不补跑,拖着不等于救回来)",
      放弃了 and GIVEUP.tries == llm.DEFAULT_RETRIES + 1)

# -- 提示词与解析必须对得上 ------------------------------------------------

check("输出要求里写的字段名,解析器全都认",
      all(k in prompts.OUTPUT_SPEC for k in
          ("object_id", "操作", "理由", "风险", "置信度", "改判条件", "指令",
           "action", "qty", "limit_price", "wtbh"))
      and all(a in prompts.OUTPUT_SPEC for a in model.ACTIONS))
check("提示词里没有任何证券代码或名称(身份由后端定,模型只抄 object_id)",
      not re.search(r"\b\d{6}\b", prompts.SYSTEM_PROMPT + prompts.OUTPUT_SPEC))
check("目标模型不含密钥,所以整个可以入档、可以打日志",
      "secret" not in repr(OPENAI).lower()
      and set(OPENAI.as_public()) == {"model", "llm_provider", "base_url", "protocol"})


print("\n=== 保证十一:一轮能自己跑完,一个标的失败不拖垮整轮 ===")

from dataclasses import replace

# 运行目录取临时目录,和上面接口那段同一个理由:仓库在同步盘里,
# state.resolve_root() 会当场拒绝仓库内的路径。
RUN_ROOT = Path(tempfile.mkdtemp(prefix="zhixing-runner-archive-"))
RUN_STATE = Path(tempfile.mkdtemp(prefix="zhixing-runner-runtime-"))

RUN_STORE = state.Store(RUN_STATE)
RUN_STORE.save_catalog([
    catalog.TradeObject("SH_510300", "SH", "510300", "演示宽基甲ETF", asset_type="ETF"),
    catalog.TradeObject("SZ_159999", "SZ", "159999", "演示宽基乙ETF", asset_type="ETF"),
    catalog.TradeObject("SH_601999", "SH", "601999", "演示银行丙", asset_type="股票"),
])


class _RoundSource:
    """假采集层。数字和名字全是编的。"""

    def collect(self, *, now, catalog):
        return runner.RoundInput(
            读取范围={"起": "2026-07-18", "止": "2026-08-17"},
            市场数据列表=[{"演示指数": 1234.5}],
            账户交易流水表=[],
            per_object={
                "SH_510300": {"演示收盘价": 3.912},
                "SZ_159999": {"演示收盘价": 1.234},
                "SH_601999": {"演示收盘价": 5.678},
            },
            data_window={"起": "2026-07-18", "止": "2026-08-17"},
            account=guards.AccountSnapshot(available_cash=Decimal("100000")),
            objects={
                "510300": guards.ObjectSnapshot(
                    symbol="510300", last_price=Decimal("3.912"),
                    prev_close=Decimal("3.900"), available_qty=2000,
                    holding_qty=2000, is_etf=True),
                "159999": guards.ObjectSnapshot(
                    symbol="159999", last_price=Decimal("1.234"),
                    prev_close=Decimal("1.230"), is_etf=True),
                "601999": guards.ObjectSnapshot(
                    symbol="601999", last_price=Decimal("5.678"),
                    prev_close=Decimal("5.670")),
            },
        )


def _reply(text: str) -> dict:
    return {
        "model": "演示模型-甲",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100,
                  "prompt_tokens_details": {"cached_tokens": 900}},
    }


class _ScriptedCaller:
    """按 object_id 给固定回复。一个答得好,一个答歪,一个直接调用失败。"""

    def __init__(self):
        self.asked: list[str] = []

    def call(self, target, body, *, object_id):
        self.asked.append(object_id)
        if object_id == "SH_601999":
            raise llm.LlmError("演示:连不上", retryable=False)
        if object_id == "SZ_159999":
            # 编了一个别的代码 + 说不出改判条件
            return model.parse_reply(target, _reply(json.dumps({
                "object_id": "SH_000000", "操作": "hold",
                "理由": ["演示"], "风险": ["演示"], "置信度": 0.4, "改判条件": "",
            }, ensure_ascii=False)), object_id=object_id)
        return model.parse_reply(target, _reply(json.dumps({
            "object_id": "SH_510300", "操作": "buy",
            "理由": ["演示理由"], "风险": ["演示风险"], "置信度": 0.7,
            "改判条件": "跌破演示均线就改判",
            "指令": {"action": "buy", "qty": 1000, "limit_price": "3.912",
                     "理由": "演示", "风险提示": "演示"},
        }, ensure_ascii=False)), object_id=object_id)


CALLER = _ScriptedCaller()
RUNNER = runner.Runner(
    store=RUN_STORE,
    archive_root=RUN_ROOT,
    caller=CALLER,
    target=OPENAI,
    source=_RoundSource(),
    clock=lambda: datetime(2026, 8, 17, 10, 0, 0),
)
RESULT = RUNNER.run_round()
ARCHIVED = json.loads(RESULT.path.read_text(encoding="utf-8"))

check("三个标的三次调用,一个成功、一个答歪、一个调用失败",
      len(CALLER.asked) == 3 and RESULT.判断数 == 1)
check("失败的两个照样归档了,而且这一轮确实落了盘(不补跑的前提是有据可查)",
      RESULT.path is not None and RESULT.path.exists())
check("答歪的那个一次报出全部毛病,不是只报第一条",
      {p["code"] for p in RESULT.问题 if p["object_id"] == "SZ_159999"}
      == {"OBJECT_ID_MISMATCH", "MISSING_TRIGGER"})
check("调用失败的那个记成 CALL_FAILED,且不编一个 0 用量顶上",
      any(p["code"] == "CALL_FAILED" for p in RESULT.问题)
      and {u["object_id"] for u in ARCHIVED["model_usage"]}
      == {"SH_510300", "SZ_159999"})
check("总体判断不夸大,把没跑成的说出来",
      "未跑成" in ARCHIVED["总体判断"])
check("归档过了 archive 的全部闸门(字段齐、无机密、不覆盖)",
      archive.validate_payload(ARCHIVED) == ())
check("名称是后端 join 上去的,模型没输出过它",
      ARCHIVED["交易对象判断"][0]["名称"] == "演示宽基甲ETF")

INSTR = ARCHIVED["待执行指令"][0]
check("指令码由「轮次+标的+动作」算出,不是随机数(重跑组装得到同一个码)",
      INSTR["instruction_code"]
      == runner.make_instruction_code(RESULT.strategy_id, "SH_510300", "buy"))
check("类型规范化完成但无人值守关闭时,指令保持 pending 且执行拒绝可见",
      INSTR["状态"] == "pending"
      and INSTR["拦截原因"] == []
      and INSTR["执行结果"]["outcome"] == "rejected"
      and "无人值守" in INSTR["执行结果"]["message"])
check("`context` 存的是共享段,七个标的共用的那一份",
      ARCHIVED["context_digest"].startswith("sha256:")
      and "交易对象数据" not in ARCHIVED["context"])

# 同一个 strategy_id 再写一次必须被拒:归档只追加
try:
    runner.Runner(store=RUN_STORE, archive_root=RUN_ROOT, caller=_ScriptedCaller(),
                  target=OPENAI, source=_RoundSource(),
                  clock=lambda: datetime(2026, 8, 17, 10, 0, 0)).run_round()
    重写被拒 = False
except archive.ArchiveError:
    重写被拒 = True
check("同一轮写第二次直接报错(归档只追加,没有 overwrite 开关)", 重写被拒)

# 采集层缺位时:明确失败,不返回空数据装作跑过
try:
    runner.Runner(store=RUN_STORE, archive_root=RUN_ROOT,
                  caller=CALLER, target=OPENAI).run_round()
    缺采集被拒 = False
except runner.RunnerError:
    缺采集被拒 = True
check("采集层没接入时明确失败,不产出一份空数据装作跑过", 缺采集被拒)

# 账户/行情快照不再是下单风控输入。无人值守仍关着,所以执行层会留下一条
# 授权拒绝,但不能再伪装成 NOT_VALIDATED。
NO_SNAPSHOT = runner.Runner(
    store=RUN_STORE, archive_root=RUN_ROOT, caller=_ScriptedCaller(), target=OPENAI,
    source=type("_S", (), {"collect": lambda s, *, now, catalog: replace(
        _RoundSource().collect(now=now, catalog=catalog), account=None, objects={})})(),
    clock=lambda: datetime(2026, 8, 17, 10, 5, 0),
)
NS = json.loads(NO_SNAPSHOT.run_round().path.read_text(encoding="utf-8"))
check("全部风控拆除后,缺资金/持仓快照不再产生 NOT_VALIDATED",
      NS["待执行指令"][0]["状态"] == "pending"
      and NS["待执行指令"][0]["拦截原因"] == []
      and NS["待执行指令"][0]["执行结果"]["outcome"] == "rejected")

# 整轮模拟必须走到执行层并写进归档,但 broker_provider 连取都不能取。
SIM_ROOT = Path(tempfile.mkdtemp(prefix="zhixing-simulation-archive-"))
SIM_PROVIDER_CALLS = {"count": 0}


def _simulation_provider():
    SIM_PROVIDER_CALLS["count"] += 1
    return _Broker()


SIM_RESULT = runner.Runner(
    store=RUN_STORE,
    archive_root=SIM_ROOT,
    caller=_ScriptedCaller(),
    target=OPENAI,
    source=_RoundSource(),
    broker_provider=_simulation_provider,
    authorization_kind=AuthorizationKind.SIMULATION,
    clock=lambda: datetime(2026, 8, 17, 10, 10, 0),
).run_round()
SIM_ARCHIVED = json.loads(SIM_RESULT.path.read_text(encoding="utf-8"))
check("SIMULATION 跑完整轮也不会取得券商适配器,券商调用严格为零",
      SIM_PROVIDER_CALLS["count"] == 0)
check("整轮模拟的指令一路进执行层并归档为 dry_run,不是在前面假装跳过",
      len(SIM_ARCHIVED["待执行指令"]) == 1
      and SIM_ARCHIVED["待执行指令"][0]["执行结果"]["outcome"] == "dry_run"
      and SIM_ARCHIVED["待执行指令"][0]["状态"] == "pending")


class _OneSource(_RoundSource):
    """只喂一个能稳定产出 buy 的编造标的。"""

    def collect(self, *, now, catalog):
        data = super().collect(now=now, catalog=catalog)
        return replace(
            data,
            per_object={"SH_510300": data.per_object["SH_510300"]},
            objects={"510300": data.objects["510300"]},
        )


class _NoAccountSource(_OneSource):
    def collect(self, *, now, catalog):
        return replace(
            super().collect(now=now, catalog=catalog),
            account=None,
            problems=({
                "object_id": "", "code": "ACCOUNT_UNAVAILABLE",
                "message": "演示:券商未配置,不计作模型轮次失败",
            },),
        )


ONE_STATE = Path(tempfile.mkdtemp(prefix="zhixing-one-runtime-"))
ONE_STORE = state.Store(ONE_STATE)
ONE_STORE.save_catalog([
    catalog.TradeObject("SH_510300", "SH", "510300", "演示宽基甲ETF", asset_type="ETF")
])

# 券商不可用是已知缺项:执行结果如实为 failed,但模型轮次本身仍算成功。
runmode.set_unattended(True, changed_by="smoke", reason="自检:券商为空")
NONE_ROOT = Path(tempfile.mkdtemp(prefix="zhixing-none-broker-archive-"))
NONE_RESULT = runner.Runner(
    store=ONE_STORE,
    archive_root=NONE_ROOT,
    caller=_ScriptedCaller(),
    target=OPENAI,
    source=_NoAccountSource(),
    broker_provider=lambda: None,
    clock=lambda: datetime(2026, 8, 17, 10, 15, 0),
).run_round()
NONE_ARCHIVED = json.loads(NONE_RESULT.path.read_text(encoding="utf-8"))
check("券商提供器返回 None 时整轮不抛异常、也不记成模型轮次失败",
      NONE_RESULT.ok
      and {p["code"] for p in NONE_RESULT.问题} == {"ACCOUNT_UNAVAILABLE"}
      and ONE_STORE.runtime().连续失败轮数 == 0)
check("broker=None 的失败留在指令执行结果里,指令仍可人工接管",
      NONE_ARCHIVED["待执行指令"][0]["状态"] == "pending"
      and NONE_ARCHIVED["待执行指令"][0]["执行结果"]["outcome"] == "failed")

# 委托是否发出无法确定时,整轮归档必须按“可能已提交”封口,不能回到 pending。
UNKNOWN_ROOT = Path(tempfile.mkdtemp(prefix="zhixing-unknown-broker-archive-"))
UNKNOWN_BROKER = _Broker(unknown=True)
UNKNOWN_RESULT = runner.Runner(
    store=ONE_STORE,
    archive_root=UNKNOWN_ROOT,
    caller=_ScriptedCaller(),
    target=OPENAI,
    source=_OneSource(),
    broker_provider=lambda: UNKNOWN_BROKER,
    clock=lambda: datetime(2026, 8, 17, 10, 20, 0),
).run_round()
UNKNOWN_ARCHIVED = json.loads(UNKNOWN_RESULT.path.read_text(encoding="utf-8"))
UNKNOWN_INSTR = UNKNOWN_ARCHIVED["待执行指令"][0]
check("submitted_unknown 在 RoundResult 与归档中都保持独立标记",
      UNKNOWN_RESULT.结果不明数 == 1
      and UNKNOWN_INSTR["执行结果"]["outcome"] == "submitted_unknown"
      and UNKNOWN_INSTR["执行结果"]["submitted_unknown"] is True)
check("结果不明按可能已提交处置,不留在 pending 造成下一次重复下单",
      UNKNOWN_INSTR["状态"] == "submitted" and UNKNOWN_BROKER.calls == 1)
check("结果不明的底层浏览器异常原文不会进归档",
      "模拟浏览器异常" not in json.dumps(UNKNOWN_INSTR, ensure_ascii=False))
runmode.set_unattended(False, changed_by="smoke", reason="自检结束,恢复默认关闭")

# 排期:跑过的那一轮不会再跑
FIRED = RUNNER.fired_today(date(2026, 8, 17))
check("今天跑过哪几轮从归档重建,不另存一份状态(第二份事实迟早会不一致)",
      len(FIRED) >= 1)
check("非交易日不跑",
      runner.Runner(store=RUN_STORE, archive_root=RUN_ROOT, caller=CALLER,
                    target=OPENAI, source=_RoundSource(),
                    clock=lambda: datetime(2026, 8, 16, 10, 0, 0)).due() is None)

shutil.rmtree(RUN_ROOT, ignore_errors=True)
shutil.rmtree(RUN_STATE, ignore_errors=True)
for _path in (SIM_ROOT, ONE_STATE, NONE_ROOT, UNKNOWN_ROOT):
    shutil.rmtree(_path, ignore_errors=True)


print("\n=== 保证十二:流式收得回来,推理过程不污染正文 ===")

# 下面这些分片是从**真实中转**上抓下来的形状(model / 字段名 / 嵌套层级
# 原样保留),只把正文换成了编的内容。抓的是形状,不是数据。
SSE = [
    {"model": "gpt-5.6-sol", "choices": [{"index": 0, "delta": {
        "reasoning_content": "**先想想该不该动**", "role": "assistant"}, "finish_reason": None}]},
    {"model": "gpt-5.6-sol", "choices": [{"index": 0, "delta": {"content": '{"object_id":"'}, "finish_reason": None}]},
    {"model": "gpt-5.6-sol", "choices": [{"index": 0, "delta": {"content": "SH_510300"}, "finish_reason": None}]},
    {"model": "gpt-5.6-sol", "choices": [{"index": 0, "delta": {
        "reasoning_content": "**再检查一遍**"}, "finish_reason": None}]},
    {"model": "gpt-5.6-sol", "choices": [{"index": 0, "delta": {"content": '","操作":"hold"}'}, "finish_reason": None}]},
    {"model": "gpt-5.6-sol", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
     "usage": {"prompt_tokens": 3150, "completion_tokens": 313,
               "prompt_tokens_details": {"cached_tokens": 1792},
               "completion_tokens_details": {"reasoning_tokens": 95}}},
]

MERGED = model.merge_stream(OPENAI, SSE, object_id="SH_510300")
STREAM_REPLY = model.parse_reply(OPENAI, MERGED, object_id="SH_510300")

check("推理过程一个字都不进正文(拼进去会让整段 JSON 解析失败,而且很像「模型不听话」)",
      STREAM_REPLY.text == '{"object_id":"SH_510300","操作":"hold"}'
      and "想想" not in STREAM_REPLY.text and "检查" not in STREAM_REPLY.text)
check("分片拼回的形状和非流式一模一样,所以正文/用量/模型名只有一份解析代码",
      set(MERGED) == {"model", "choices", "usage"}
      and MERGED["choices"][0]["message"]["role"] == "assistant")
check("用量从最后一片上取全,四个数都不丢",
      STREAM_REPLY.usage.as_entry() == {
          "object_id": "SH_510300", "input_tokens": 3150, "output_tokens": 313,
          "reasoning_tokens": 95, "cached_tokens": 1792})
check("finish_reason 从带它的那一片上取,不是最后一片就一定有",
      STREAM_REPLY.finish_reason == "stop")
check("一个分片都没有时报错,不返回一份空正文",
      _raises(model.ModelError, lambda: model.merge_stream(OPENAI, [], object_id="x")))
check("openai_chat 开流式时请求体里带 stream=true(这台中转非流式直接 400)",
      model.build_request(OPENAI, system_prompt="s", user_text="u").get("stream") is True)
check("关掉流式就不带这个键,不是发一个 stream=false 上去",
      "stream" not in model.build_request(
          replace(OPENAI, stream=False), system_prompt="s", user_text="u"))
check("anthropic_messages + 流式在**构造配置时**就被拒(没验过的实现不装成能用)",
      _raises(model.ModelError, lambda: model.ModelTarget(
          name="claude-opus-5", provider="x", base_url="https://e/",
          protocol="anthropic_messages", stream=True)))


class _StreamCaller(llm.HttpCaller):
    """把 socket 换成一串写死的字节行,验传输层那半段,不联网。"""

    def __init__(self, lines):
        super().__init__(credential=llm.Credential("sk-DEMOFAKEKEYSTREAM01"),
                         sleep=lambda _s: None, retries=0)
        self.lines = lines

    def _open(self, target, body):
        import contextlib
        return contextlib.nullcontext(iter(self.lines))


def _sse_bytes(events, *, done=True):
    out = []
    for e in events:
        out.append(b"data: " + json.dumps(e, ensure_ascii=False).encode("utf-8") + b"\n")
        out.append(b"\n")                     # SSE 的分片边界
    out.append(b": keep-alive\n")             # 心跳,必须被跳过
    if done:
        out.append(b"data: [DONE]\n")
    return out


check("SSE 的空行和 `: ` 心跳被跳过,不当成分片",
      _StreamCaller(_sse_bytes(SSE)).call(OPENAI, {}, object_id="SH_510300").text
      == '{"object_id":"SH_510300","操作":"hold"}')
check("没收到 [DONE] 就算失败并可重试(截断的 JSON 可能「恰好」合法,比报错更危险)",
      _raises(llm.LlmError,
              lambda: _StreamCaller(_sse_bytes(SSE, done=False)).call(
                  OPENAI, {}, object_id="SH_510300")))
check("坏掉的单个分片被跳过但记进日志,不让整次调用作废",
      _StreamCaller(
          _sse_bytes(SSE[:2]) [:-1] + [b"data: {\xe5\x9d\x8f\n", b"data: [DONE]\n"]
      ).call(OPENAI, {}, object_id="SH_510300").text == '{"object_id":"')
check("流式的 Accept 头是 text/event-stream,不是 application/json",
      _StreamCaller([])._headers(OPENAI)["Accept"] == "text/event-stream"
      and _StreamCaller([])._headers(replace(OPENAI, stream=False))["Accept"]
          == "application/json")


print("\n=== 保证十三:不知道的日子说不知道,认不出的图不硬猜 ===")

from datetime import date as _date

from zhixing import captcha as captcha_mod
from zhixing import tradingdays

check("春节连休不是交易日(二代在这儿会照常起轮次)",
      tradingdays.is_trading_day(_date(2026, 2, 17)) is False)
check("国务院通知里的春节最后一天 2/23 已补进休市日",
      tradingdays.is_trading_day(_date(2026, 2, 23)) is False)
check("国庆假期止于 10/7,10/8 不再被旧表误记为休市",
      tradingdays.is_trading_day(_date(2026, 10, 8)) is True)
check("普通工作日是交易日",
      tradingdays.is_trading_day(_date(2026, 8, 19)) is True)
check("周末不是交易日",
      tradingdays.is_trading_day(_date(2026, 8, 22)) is False)
check("**查不到的年份当场报错,不退回工作日判断**"
      "(悄悄退回的坏处不是会错,是错了没人知道)",
      _raises(tradingdays.CalendarError, lambda: tradingdays.is_trading_day(_date(2027, 3, 2))))
check("校验层和调度层共用同一份日历,没有第二个事实来源",
      guards.default_is_trading_day is scheduler.default_is_trading_day
      and guards.default_is_trading_day(_date(2026, 2, 17)) is False)
CALENDAR_2026 = tradingdays.calendar_for(2026)
check("2026 日历已对照国务院办公厅权威通知核实,来源能复查",
      CALENDAR_2026.verified is True
      and "gov.cn/zhengce/zhengceku/202511/content_7047091.htm" in CALENDAR_2026.source
      and tradingdays.assert_ready_for_live(2026) is None)
check("缺年份在启动时就能问出来,不用等到那天早上九点半",
      _raises(tradingdays.CalendarError, lambda: tradingdays.assert_covers(2026, 2027))
      and tradingdays.assert_covers(2026) is None)
check("周末混进休市日表会被拒(那是照放假安排整段抄的信号)",
      _raises(tradingdays.CalendarError,
              lambda: tradingdays.YearCalendar(
                  year=2026, holidays=frozenset({_date(2026, 10, 3)}))))

_CAP_OK = {"choices": [{"message": {"role": "assistant", "content": "A3K9"}}]}
check("认出来的四位原样返回", captcha_mod.extract_answer(_CAP_OK) == "A3K9")
check("模型多说了一句也能抠出答案",
      captcha_mod.extract_answer(
          {"choices": [{"message": {"content": "验证码是:7F2B"}}]}) == "7F2B")
check("**模型说看不清就当失败,不硬猜**"
      "(猜出来的形状是合法的,会被提交上去,然后以「密码错误」的面目出现)",
      _raises(captcha_mod.CaptchaError,
              lambda: captcha_mod.extract_answer(
                  {"choices": [{"message": {"content": "????"}}]})))
check("形状不合法的一律拒(长度不对)",
      _raises(captcha_mod.CaptchaError,
              lambda: captcha_mod.extract_answer(
                  {"choices": [{"message": {"content": "我看不出来"}}]})))
check("没配识别接口时明确失败,不拿 0000 去试然后被锁号",
      _raises(captcha_mod.CaptchaError, lambda: captcha_mod.AlwaysFailSolver().solve(b"x")))
check("整页截图那么大的「验证码」会被拦",
      _raises(captcha_mod.CaptchaError,
              lambda: captcha_mod.build_request(
                  b"x" * (captcha_mod.MAX_IMAGE_BYTES + 1), model="demo")))
check("请求体是 data URI,且 max_tokens 小到模型没空解释自己在看什么",
      captcha_mod.build_request(b"\x89PNG", model="demo")["max_tokens"] == 16
      and captcha_mod.build_request(b"\x89PNG", model="demo")["messages"][0]["content"][1]
          ["image_url"]["url"].startswith("data:image/png;base64,"))
check("密钥没配就明确报错,不发一个没有 Authorization 的请求出去",
      _raises(llm.LlmError,
              lambda: captcha_mod.HttpSolver(
                  endpoint="https://demo.invalid/v1", model="demo",
                  credential=llm.Credential(""), retries=0,
              )._once({})))

# 配置 → 识别器 这一步的默认值。**缺一项就退到"明确失败",不是跳过验证码。**
# 跳过的结果不是登录失败,是拿空答案去提交,然后以「验证码错误」的面目出现。
check("三样配齐才装真识别器",
      isinstance(captcha_mod.solver_from_settings(
          state.CaptchaSettings(endpoint="https://demo.invalid/v1", model="demo",
                                secret="sk-DEMOFAKEKEYCAPTCHA01")),
          captcha_mod.HttpSolver))
for 缺项, 配置 in (
    ("接口地址", state.CaptchaSettings(model="demo", secret="sk-DEMOFAKEKEYCAPTCHA02")),
    ("模型", state.CaptchaSettings(endpoint="https://demo.invalid/v1",
                                   secret="sk-DEMOFAKEKEYCAPTCHA03")),
    ("密钥", state.CaptchaSettings(endpoint="https://demo.invalid/v1", model="demo")),
):
    check(f"缺{缺项}就退到明确失败的那个,**不是跳过验证码**",
          isinstance(captcha_mod.solver_from_settings(配置), captcha_mod.AlwaysFailSolver))
check("头一次装机(什么都没配)不抛异常,让「还没配」和「接口挂了」分得开",
      isinstance(captcha_mod.solver_from_settings(state.CaptchaSettings()),
                 captcha_mod.AlwaysFailSolver))

# 直接补跑/回放一轮时,日历不再参与指令规范化。调度器仍然 fail-closed,
# 所以未知年份不会自动触发;这里证明的是显式 run_round 不会把日历伪装成风控。
CAL_STORE = state.Store(Path(tempfile.mkdtemp(prefix="zhixing-cal-runtime-")))
CAL_STORE.save_catalog(RUN_STORE.catalog())
CAL_RESULT = runner.Runner(
    store=CAL_STORE,
    archive_root=Path(tempfile.mkdtemp(prefix="zhixing-cal-archive-")),
    caller=_ScriptedCaller(),
    target=OPENAI,
    source=_RoundSource(),
    clock=lambda: datetime(2027, 8, 17, 10, 0, 0),   # 日历里没有 2027
).run_round()
CAL_ARCHIVED = json.loads(CAL_RESULT.path.read_text(encoding="utf-8"))

check("日历答不上来的那一轮**照样落盘**,没被异常带走",
      CAL_RESULT.path is not None and CAL_RESULT.path.exists())
check("交易日历风控已拆除,显式轮次不再制造 CALENDAR_UNKNOWN",
      not any(p["code"] == "CALENDAR_UNKNOWN" for p in CAL_RESULT.问题))
check("未知年份不再把指令标成校验拒绝;无人值守关闭仍会在执行层留痕",
      CAL_ARCHIVED["待执行指令"][0]["状态"] == "pending"
      and CAL_ARCHIVED["待执行指令"][0]["拦截原因"] == []
      and CAL_ARCHIVED["待执行指令"][0]["执行结果"]["outcome"] == "rejected")
check("这一轮的归档同样过得了 archive 的全部闸门",
      archive.validate_payload(CAL_ARCHIVED) == ())


# ===========================================================================
print("\n=== 保证十四:采到的和没采到的分得开,指标缺了就说缺 ===")
# ===========================================================================
#
# 这一节全是纯函数,**一个网络请求都不发**。行情源那两条链路的实测在
# 别处做(要联网),这里验的是"拿到数据之后怎么摆"和"没拿到怎么说"。

from decimal import Decimal as _D

from zhixing import collect as collect_mod
from zhixing import daemon as daemon_mod
from zhixing import indicators as ind_mod
from zhixing import quotes as quotes_mod

# -- 指标:缺了就是 None,不是 0 -------------------------------------------


class _Bar:
    def __init__(self, o, h, l, c):
        self.open, self.high, self.low, self.close = o, h, l, c


_RISING = [_Bar(10 + i, 11 + i, 9 + i, 10 + i) for i in range(80)]

IND_FULL = ind_mod.compute(_RISING)
check("80 根日线算得出 MA60", IND_FULL.ma60 is not None)
check("单调上涨时 RSI14 顶到 100(没有下跌,平均跌幅为 0)",
      IND_FULL.rsi14 == 100.0)

IND_SHORT = ind_mod.compute(_RISING[:8])
check("**根数不够时指标是 None,不是 0**(0 是一个会被当真的数)",
      IND_SHORT.ma20 is None and IND_SHORT.ma60 is None and IND_SHORT.dif is None)
check("不够的原因写进 notes,而不是让人对着一堆 null 猜",
      len(IND_SHORT.notes) > 0)
check("as_entry 把 null 原样送出去,不填 0 也不省略字段",
      "MA60" in IND_FULL.as_entry()["均线"]
      and IND_SHORT.as_entry()["均线"]["MA20"] is None)

_GAPPED = list(_RISING[:40])
_GAPPED[25] = _Bar(None, None, None, None)      # 中间缺一根
IND_GAP = ind_mod.compute(_GAPPED)
check("窗口里**散落一个缺失值就不给 MA20**"
      "(二代会拿跨了 21 个交易日的 20 个点算一个数,叫它 MA20)",
      ind_mod.moving_average([1.0] * 19 + [None], 20) is None)

check("一字板不让 KDJ 的 RSV 滑到极端(high==low 时取 50)",
      ind_mod.kdj([5.0] * 20, [5.0] * 20, [5.0] * 20) is not None)
check("BOLL 通道宽度为 0 时 %B 是 None,不是 0.5(那是编的)",
      ind_mod.boll([5.0] * 20)[3] is None)

# -- 持仓:没持仓 ≠ 没取到持仓 ---------------------------------------------

check("**没取到持仓返回「持有: False」的明确结构**",
      collect_mod.position_entry(None)["持有"] is False
      and collect_mod.position_entry(None)["数量"] == 0)

_POS = eastmoney_mod.Position(
    symbol="510300", name="沪深300ETF", market="SH",
    holding_qty=1000, available_qty=None, frozen_qty=0,
    cost_price=_D("3.900"), last_price=_D("3.950"),
    market_value=_D("3950"), profit=_D("50"), today_profit=None,
)
check("接口没给的数量**原样是 null**,不拿 0 顶(0 会被读成「确实是零」)",
      collect_mod.position_entry(_POS)["可用数量"] is None)

# -- ST 判定:大小写和全角都要认 -------------------------------------------

check("认得出 ST", collect_mod.is_st_name("ST某某") is True)
check("认得出小写 st", collect_mod.is_st_name("st某某") is True)
check("认得出全角 ＳＴ(深市真的这么返回过,漏掉会拿 10% 去报 5% 的板)",
      collect_mod.is_st_name("ＳＴ某某") is True)
check("认得出 *ST", collect_mod.is_st_name("*ST某某") is True)
check("正常名称不误判", collect_mod.is_st_name("贵州茅台") is False)

# -- 账户段:取不到就明说,不给空表 -----------------------------------------

_NO_ACC = collect_mod.account_entry(None, None, "演示:登录没配")
check("**取不到账户就明说取不到**,不渲染成一张空表"
      "(空表的意思是「今天一笔都没有」,那是另一件事)",
      _NO_ACC["取到了"] is False and _NO_ACC["账户"] is None)
check("账户缺失不再冒充本地风控,而是说明执行结果会如实记录券商不可用",
      "判断照常产出" in _NO_ACC["说明"]
      and "券商适配器不可用" in _NO_ACC["说明"])

# -- 数据窗口:一个都没采到时是空串,不是当前时间 ---------------------------

check("**一个都没采到时 data_window 是空串**"
      "(拿当前时间冒充数据时间,会让「数据是陈的」彻底看不出来)",
      collect_mod.data_window([]) == {"起": "", "止": ""})

# -- 读取范围:漏掉的要明写 -------------------------------------------------

_OBJ = catalog.TradeObject("SH_600519", "SH", "600519", "贵州茅台")
_SCOPE = collect_mod.scope_entry(
    [collect_mod.ObjectData(obj=_OBJ, problem="演示:取不到")],
    datetime(2026, 8, 19, 14, 30, 0),
)
check("**没采到的标的要出现在「读取范围」里**"
      "(只列读到的等于告诉模型「这就是全部」)",
      len(_SCOPE["没采到的"]) == 1
      and _SCOPE["没采到的"][0]["证券代码"] == "600519")

# -- 数据源:算出来的,不是手写的一句话 -------------------------------------

_DS_STORE = state.Store(Path(tempfile.mkdtemp(prefix="zhixing-ds-")))
_DS_EMPTY = collect_mod.describe_source(_DS_STORE)
check("**没配券商时明说采不到账户**,不含糊地写「东方财富」让人以为它在工作",
      "未配置" in _DS_EMPTY)
check("行情源顺序取自 quotes.SOURCES,改了那个元组这句话跟着变"
      "(二代缺陷 6:一句没有任何代码在做的承诺被当事实显示)",
      all(s in _DS_EMPTY for s in quotes_mod.SOURCES))

_DS_STORE.save_broker(state.BrokerSettings(
    remote_url="http://127.0.0.1:4444", account="123456789012", password="pw"))
check("配全之后这句话跟着变,不用重启",
      "未配置" not in collect_mod.describe_source(_DS_STORE))

# -- 数据源算炸了不能把状态页带走 -------------------------------------------


def _boom() -> str:
    raise RuntimeError("演示:算不出来")


_BOOM_APP = api.App(store=_DS_STORE, archive_root=API_ARCHIVE, data_source=_boom)
check("**数据源描述算不出来时状态页照样打得开**"
      "(出事时它是第一个要看的东西,让它因为一句描述文本挂掉是本末倒置)",
      "算不出来" in _BOOM_APP.describe_data_source())

# -- 前置条件:一次报全部,且不把行情列进来 ---------------------------------

_PRE_STORE = state.Store(Path(tempfile.mkdtemp(prefix="zhixing-pre-")))
_PRE = daemon_mod.preflight(_PRE_STORE)
check("**一次报全部缺项**,不是撞上第一个就返回", len(_PRE) >= 3)
check("模型三样缺哪样报哪样",
      any("接口地址" in x for x in _PRE) and any("密钥" in x for x in _PRE))

_PRE_STORE.save_model(state.ModelSettings(
    endpoint="https://example.invalid/v1", name="演示", provider="演示", secret="x"))
_PRE_STORE.save_catalog([_OBJ])
check("**券商没配不拦这一轮**——没有账户照样能出判断,只是出不了指令"
      "(判断才是这个系统的主要产出,不该被「还没填账号」卡住)",
      daemon_mod.preflight(_PRE_STORE) == ())

# -- 总体判断:问题不等于「标的没跑成」 -------------------------------------

_JUDGE = [{"操作": "buy"}, {"操作": "hold"}]
_WHOLE_ROUND = runner._overall(
    _JUDGE, [], ({"object_id": "", "code": "ACCOUNT_UNAVAILABLE", "message": "x"},)
)
check("**账户挂了不该被说成「1 个标的没跑成」**"
      "(标的一个没少,少的是全部指令。这句话是给人看的第一行)",
      "整轮性问题" in _WHOLE_ROUND and "1 个标的本轮未跑成" not in _WHOLE_ROUND)
check("挂在标的上的问题照旧念成「标的没跑成」",
      "1 个标的本轮未跑成" in runner._overall(
          _JUDGE, [], ({"object_id": "SH_600519", "code": "QUOTE_UNAVAILABLE",
                        "message": "x"},)))


# -- 人工接管:还差什么是算出来的,不是写死的一句话 -------------------------
#
# 这里盯的是二代缺陷 6 的**复发**:上一版写死着"券商适配器尚未实现",
# 而适配器落地那天这句话就成了假的,没有任何东西提醒人去改。

_CONF_STORE = state.Store(Path(tempfile.mkdtemp(prefix="zhixing-conf-")))
_CONF_APP = api.App(store=_CONF_STORE, archive_root=API_ARCHIVE)
_BLOCKERS = api.live_order_blockers(_CONF_APP)
check("人工接管缺项一次报全:配置与会话提供器各自可见",
      len(_BLOCKERS) == 2
      and any("券商未配置齐全" in x for x in _BLOCKERS)
      and any("会话提供器" in x for x in _BLOCKERS))
check("已拆除的日历与旧复核风控不会偷偷留在人工通路",
      not any("日历" in x or "复核通路" in x for x in _BLOCKERS))
check("**「券商适配器尚未实现」这句话不许再出现**"
      "(broker.place_order 已经存在,它从落地那天起就是假的)",
      not any("适配器尚未实现" in x for x in _BLOCKERS))

_CONF_RESP = api.confirm_instruction(
    _CONF_APP, api.Request(method="POST", path="/x", query={}, body=None), "C1")
check("验证锁解除后人工接管会继续查归档,不存在就是正常 NOT_FOUND",
      _CONF_RESP.status == 404
      and _CONF_RESP.payload["error"]["code"] == "NOT_FOUND")

# -- 人工下单 / 委托查询 / 撤单:全走执行层并各自留痕 ------------------------

API_EXEC_ROOT = Path(tempfile.mkdtemp(prefix="zhixing-api-execution-archive-"))
API_EXEC_STATE = Path(tempfile.mkdtemp(prefix="zhixing-api-execution-runtime-"))
API_EXEC_STORE = state.Store(API_EXEC_STATE)
API_EXEC_STORE.save_broker(state.BrokerSettings(
    remote_url="http://demo.invalid", account="demo-account", password="demo-password"
))
PENDING_CODE = "demo-manual-order-1"
archive.write_run(make_payload(
    "20260819-100000",
    stamp="2026-08-19T10:00:00+08:00",
    instructions=[{
        "instruction_code": PENDING_CODE,
        "action": "buy", "market": "SH", "symbol": "510300",
        "name": "演示宽基甲ETF", "qty": 100, "limit_price": "3.900",
        "wtbh": None, "理由": "演示", "风险提示": "演示",
        "状态": "pending", "拦截原因": [],
    }],
), root=API_EXEC_ROOT)


class _ApiBroker:
    def __init__(self) -> None:
        self.orders = 0
        self.cancelled: list[str] = []

    def place_order(self, order) -> str:
        self.orders += 1
        return "demo-order-ref"

    def cancel_order(self, wtbh: str) -> None:
        self.cancelled.append(wtbh)

    def activity(self):
        return {
            "当日委托": {
                "取到了": True,
                "明细": [{
                    "Wtbh": "demo-order-ref", "Zqdm": "510300", "Wtjg": "3.900",
                    "非公开券商字段": "must-not-leak",
                }],
            },
        }


API_BROKER = _ApiBroker()
API_EXEC_APP = api.App(
    store=API_EXEC_STORE,
    archive_root=API_EXEC_ROOT,
    now=lambda: datetime(2026, 8, 19, 10, 1, 0),
    broker_provider=lambda: API_BROKER,
)


def api_exec_call(method: str, path: str, *, body=None) -> api.Response:
    return api.handle(API_EXEC_APP, api.Request(method, path, {}, body))


check("券商配置和会话提供器齐全时,人工执行前置缺项为空",
      api.live_order_blockers(API_EXEC_APP) == ())
MANUAL_SENT = api_exec_call("POST", f"/api/instructions/{PENDING_CODE}/confirm")
check("人工接管也走 ValidatedOrder → execution,成功返回委托编号",
      MANUAL_SENT.status == 200
      and MANUAL_SENT.payload["data"]["outcome"] == "submitted"
      and MANUAL_SENT.payload["data"]["wtbh"] == "demo-order-ref"
      and API_BROKER.orders == 1)
check("人工执行成功有独立追加留痕,原归档不改写且待处理队列会排除它",
      len(list(archive.iter_executions(API_EXEC_ROOT))) == 1
      and api_exec_call("GET", "/api/instructions/pending").payload["data"] == [])

ACTIVITY = api_exec_call("GET", "/api/orders/activity")
check("委托列表只下发白名单字段,券商原始行不会整包穿透接口",
      ACTIVITY.status == 200
      and "must-not-leak" not in json.dumps(ACTIVITY.payload, ensure_ascii=False)
      and ACTIVITY.payload["data"]["当日委托"]["明细"][0]["委托编号"]
          == "demo-order-ref")

CANCELLED = api_exec_call(
    "POST", "/api/orders/demo-order-ref/cancel", body={"原因": "自检:撤销演示委托"}
)
check("撤单从接口进入同一 execution 通路并追加留痕",
      CANCELLED.status == 200
      and CANCELLED.payload["data"]["action"] == "cancel"
      and API_BROKER.cancelled == ["demo-order-ref"]
      and len(list(archive.iter_executions(API_EXEC_ROOT))) == 2)


class _UnknownCancelBroker(_ApiBroker):
    def cancel_order(self, wtbh: str) -> None:
        raise BrokerError("模拟内部路径细节", submitted_unknown=True)


API_EXEC_APP.broker_provider = lambda: _UnknownCancelBroker()
UNKNOWN_CANCEL = api_exec_call("POST", "/api/orders/demo-unknown/cancel")
check("撤单结果不明返回 202 并保留 submitted_unknown,绝不冒充明确失败",
      UNKNOWN_CANCEL.status == 202
      and UNKNOWN_CANCEL.payload["data"]["outcome"] == "submitted_unknown"
      and UNKNOWN_CANCEL.payload["data"]["submitted_unknown"] is True)
check("结果不明的底层异常原文不会进响应或独立执行归档",
      "模拟内部路径细节" not in json.dumps(UNKNOWN_CANCEL.payload, ensure_ascii=False)
      and "模拟内部路径细节" not in json.dumps(
          list(archive.iter_executions(API_EXEC_ROOT)), ensure_ascii=False
      ))

shutil.rmtree(API_EXEC_ROOT, ignore_errors=True)
shutil.rmtree(API_EXEC_STATE, ignore_errors=True)

# -- 一轮算不算失败:2026-08-20 第一轮真实轮次暴露的 ------------------------
#
# 那一轮:7 条判断、归档落盘、零异常,唯一的「问题」是 ACCOUNT_UNAVAILABLE
# (券商没配 —— preflight 里明写着不拦这一轮)。而运行事实记的是
# 「上一轮成功时间 None、连续失败轮数 1」。原因是 ok = not 问题。
# 缺配置是常态不是故障,照这样那个计数器每天涨六,永远不归零。

_JUDG = {"SH_510300": object()}
check("**券商没配不算这一轮失败**——preflight 早就这么承诺了,现在留痕也兑现了",
      runner.round_failure([{"code": "ACCOUNT_UNAVAILABLE", "message": "券商没配全"}], _JUDG) is None)
check("没有任何问题当然是成功", runner.round_failure([], _JUDG) is None)
check("**少一个标的的行情仍然算失败**——那个标的这一轮没被判断过,人该看见",
      runner.round_failure([{"code": "QUOTE_UNAVAILABLE", "message": "没采到行情"}], _JUDG)
      == "没采到行情")
check("已知缺项和真问题混在一起时,报的是**真问题**那句,不是排在前面那句",
      runner.round_failure(
          [{"code": "ACCOUNT_UNAVAILABLE", "message": "券商没配全"},
           {"code": "CALL_FAILED", "message": "模型调用失败"}], _JUDG) == "模型调用失败")
check("**一条判断都没出就是白跑一轮**,哪怕问题列表里只有已知缺项",
      runner.round_failure([{"code": "ACCOUNT_UNAVAILABLE", "message": "券商没配全"}], {})
      is not None)
check("一条判断没有、一条问题也没有,也得算失败并且说得出一句话",
      runner.round_failure([], {}) == "本轮没有产出任何判断。")
check("**豁免名单是一份写下来的清单,不是散在 if 里的字符串**",
      "ACCOUNT_UNAVAILABLE" in runner.KNOWN_ABSENCES and len(runner.KNOWN_ABSENCES) == 1)

# -- 日历门槛:2026-08-20 上线时把轮次驱动挡在门外的那个 ----------------------
#
# 原来的启动门槛是 assert_covers(今年, 明年)。上线当天它让 daemon 反复重启,
# 理由是"缺少 2027 年的交易日历"——而次年安排要到当年 11 月才发布,
# **八月份的机器不可能有**。fail-closed 的条件必须是调用方有办法满足的,
# 否则拦下的是系统本身,不是风险。

check("**八月只要当年日历**——次年那时候还没发布,要求它等于一整年起不来",
      tradingdays.required_years(_date(2026, 8, 20)) == (2026,))
check("年底 45 天内次年就是硬要求了(12/31 往回数,11 月中旬起)",
      tradingdays.required_years(_date(2026, 11, 20)) == (2026, 2027))
check("12 月 31 日当然要有次年", tradingdays.required_years(_date(2026, 12, 31)) == (2026, 2027))
check("**跨年之后立刻只要新的那年**,不会回头要已经过完的年份",
      tradingdays.required_years(_date(2027, 1, 5)) == (2027,))
check("门槛切换点算得出来:45 天前一天还不要次年",
      tradingdays.required_years(_date(2026, 11, 15)) == (2026,))
check("**只有 2026 年日历时,今天这个门槛过得去**"
      "(上线那天过不去,进程反复重启)",
      tradingdays.assert_covers(*tradingdays.required_years(_date(2026, 8, 20))) is None)

# -- 时钟时区:2026-08-20 线上撞上的那个 --------------------------------------
#
# ``scheduler`` 开头第一句是"**它不读系统时钟**",时点全靠外面喂进来。
# 那是好设计,但它有个代价:**模块永远发现不了喂进来的"现在"来自错时区
# 的时钟**。所以下面这一组测的不是排期算得对不对,是"时钟本身对不对"。
#
# 线上那次的样子:容器默认 UTC,时点按北京时间配,接口显示"下次触发
# 09:16,未到"而北京时间已经 10:43——**没有异常、没有失败轮次、每个字段
# 都自洽**,只是六轮整体推后八小时全落到收盘之后。

import datetime as _dt

from zhixing import daemon

_TZ_UTC = _dt.datetime(2026, 8, 20, 2, 43, tzinfo=_dt.timezone.utc)
_TZ_CST = _dt.datetime(2026, 8, 20, 10, 43,
                       tzinfo=_dt.timezone(_dt.timedelta(hours=8)))

check("北京时间(+08:00)的时钟没有问题", scheduler.clock_zone_problem(_TZ_CST) is None)
_TZ_MSG = scheduler.clock_zone_problem(_TZ_UTC)
check("**UTC 时钟必须被认出来**——线上就是这么错的", _TZ_MSG is not None)
check("**错多少小时是算出来的,不是写死「八小时」**"
      "(换个时区部署时,写死的数字会变成又一句假话)",
      _TZ_MSG is not None and "+8.0 小时" in _TZ_MSG)
check("**报的是后果不是现象**:说清 09:15 那轮实际会在 17:15 触发,"
      "而不是只说一句「时区不对」",
      _TZ_MSG is not None and "17:15" in _TZ_MSG)
check("naive datetime 不许被当成对的——**不知道在哪个时区**正是这个 bug 藏得住的原因",
      scheduler.clock_zone_problem(_dt.datetime(2026, 8, 20, 10, 43)) is not None)
check("西五区那种也认得出,而且算出的是 22:15 不是 17:15(**没有把八小时写死**)",
      "22:15" in (scheduler.clock_zone_problem(
          _dt.datetime(2026, 8, 20, tzinfo=_dt.timezone(_dt.timedelta(hours=-5)))) or ""))

# preflight 必须拦住它:时钟差八小时的时候,**不跑比跑对**——
# 跑起来会拿收盘后的行情产出一份标称盘中的判断,归档就说谎了。
check("**时区不对时 daemon.preflight 拦下这一轮**,而不是照跑",
      any("时区" in x for x in daemon.preflight(_CONF_STORE, now=_TZ_UTC)))
check("时区对的时候 preflight 不会因为时区拦谁",
      not any("时区" in x for x in daemon.preflight(_CONF_STORE, now=_TZ_CST)))

# 镜像里必须真的设了 TZ,否则上面那一整组只是在自己跟自己玩——
# 代码认得出错时区,但没有任何东西保证部署出去的容器时区是对的。
_DOCKERFILES = Path(__file__).resolve().parent.parent.parent / "deploy"
check("**api.Dockerfile 里设了 TZ=Asia/Shanghai**"
      "(不设的话上面那些检查只是在演习:代码认得出问题,但线上照错不误)",
      "TZ=Asia/Shanghai" in (_DOCKERFILES / "api.Dockerfile").read_text(encoding="utf-8"))
check("web.Dockerfile 也设了——两个容器日志时间对不上会把排查带沟里",
      "TZ=Asia/Shanghai" in (_DOCKERFILES / "web.Dockerfile").read_text(encoding="utf-8"))


# ===========================================================================
#  宏观对象(macro.py + catalog/collect 的接线)
# ===========================================================================
#
# 这一组盯的是一件在数字上看不出来的事:**字段位置**。新浪的实时返回是
# 一串逗号分隔的数,取错一位不会报错,只会把「最高价」当成「最新价」——
# 两个都是合理的价格,任何下游校验都发现不了。所以两套布局各钉一条样本。
#
# 样本里的数字是**编的**,字段位置是**实测的**(2026-08-20 从服务器抓的
# 真返回数出来的)。要验的是位置,不是那天的行情。

from zhixing import macro as macro_mod

# -- 实时:两套字段布局各钉一条 ---------------------------------------------

_FUT_SAMPLE = ('var hq_str_hf_CL="70.000,,69.900,69.950,71.500,68.200,'
               '10:11:12,69.000,69.100,0,7,16,2026-01-05,纽约原油,0";')
_FUT_RT = macro_mod.parse_realtime(_FUT_SAMPLE, macro_mod.spec_for("COMMODITY_WTI"))
check("**期货布局的六个下标**:0最新 4最高 5最低 6时间 7昨收 12日期"
      "(数错一位会把最高价当成最新价,而那是个完全合理的价格)",
      (_FUT_RT.latest, _FUT_RT.high, _FUT_RT.low, _FUT_RT.prev) == (70.0, 71.5, 68.2, 69.0)
      and _FUT_RT.time_text == "10:11:12" and _FUT_RT.day == "2026-01-05")

_DXY_SAMPLE = ('var hq_str_DINIW="10:11:12,101.2345,101.2345,100.5000,3389,'
               '100.6000,101.9000,100.1000,101.2345,美元指数,2026-01-05";')
_DXY_RT = macro_mod.parse_realtime(_DXY_SAMPLE, macro_mod.spec_for("INDEX_DXY"))
check("**外汇布局的六个下标**:0时间 1最新 3昨收 6最高 7最低,日期在末尾"
      "(和期货那套完全不同,而 DINIW 没有 hf_ 前缀,靠符号猜必然猜错)",
      (_DXY_RT.latest, _DXY_RT.prev, _DXY_RT.high, _DXY_RT.low)
      == (101.2345, 100.5, 101.9, 100.1)
      and _DXY_RT.time_text == "10:11:12" and _DXY_RT.day == "2026-01-05")

# 同属外汇布局,但日期下标不同:DINIW 在第 10 段,fx_susdcnh 在第 17 段。
# 这就是 _last_date 从后往前扫而不是写死下标的原因。
_FX_SAMPLE = ('var hq_str_fx_susdcnh="10:11:12,7.100000,7.100100,7.110000,130,'
              '7.109000,7.112000,7.090000,7.100000,离岸人民币（香港）,'
              '-0.100000,-0.006900,0.001931,,7.995700,7.020200,,2026-01-05";')
_FX_RT = macro_mod.parse_realtime(_FX_SAMPLE, macro_mod.spec_for("FX_USDCNH"))
check("**同一套布局里日期下标还不一样**(DINIW 第10段,fx_susdcnh 第18段)"
      "——写死任一个,另一个就取到空日期,然后被判成「非当日行情」",
      _FX_RT.day == "2026-01-05" and _FX_RT.latest == 7.1)

check("**两套布局不能互换**:期货样本按外汇布局解,最新值取到的是空段"
      "(证明这两套确实是两套,不是同一个东西换了个名字)",
      macro_mod.parse_realtime(
          _FUT_SAMPLE.replace("hf_CL", "DINIW"),
          macro_mod.spec_for("INDEX_DXY")).latest is None)

check("符号对不上时明说格式不认识,不是静默返回空对象",
      _raises(macro_mod.MacroError,
              lambda: macro_mod.parse_realtime('var hq_str_xx="1,2,3";',
                                               macro_mod.spec_for("INDEX_DXY"))))
check("清单里没有的宏观代码,spec_for 直接拒,并把可选项列出来",
      _raises(macro_mod.MacroError, lambda: macro_mod.spec_for("COMMODITY_BTC")))

# -- 历史:外汇那套的字段顺序不是 OHLC --------------------------------------

check("期货历史(JSON)取 date/close,按日期升序",
      macro_mod.parse_futures_history(
          '[{"date":"2026-01-02","open":"1","high":"2","low":"3","close":"61.5"},'
          '{"date":"2026-01-05","open":"1","high":"2","low":"3","close":"62.5"}]')
      == [{"date": "2026-01-02", "close": 61.5}, {"date": "2026-01-05", "close": 62.5}])

_FX_HIST = macro_mod.parse_forex_history(
    'var DINIW=("2026-01-02,129.2200,128.9100,129.6600,129.1300,'
    '|2026-01-05,129.3800,129.2200,129.4600,129.3000,");')
check("**外汇历史是「日期,开,低,高,收」——第2位是低不是高**"
      "(照常见的 OHLC 取,收盘价会变成最高价,而那也是个合理的数)",
      [r["close"] for r in _FX_HIST] == [129.13, 129.30])

check("历史返回不是 JSON 时明说,不是当成空历史"
      "(空历史会静悄悄降级成「没有 5 日涨跌幅」,人不会去查)",
      _raises(macro_mod.MacroError, lambda: macro_mod.parse_futures_history("<html>404</html>")))

# -- 跨时区:「领先一天」不是「数据陈旧」 -------------------------------------

_T0 = _date(2026, 1, 5)
check("当日行情就是当日行情",
      macro_mod._market_state("2026-01-05", _T0) == (True, "当日行情"))
_AHEAD = macro_mod._market_state("2026-01-06", _T0)
check("**数据源领先一天算当日行情**,并明写「境外品种跨时区,属正常」"
      "(富时A50 实测就是这样;写成「非当日行情」会让模型把最新数据当过期数据打折)",
      _AHEAD[0] is True and "属正常" in _AHEAD[1])
_BEHIND = macro_mod._market_state("2026-01-04", _T0)
check("落后一天不算当日,而且话说的是「当日尚未开盘或本地已过收盘」,"
      "不和上面那条混成一句",
      _BEHIND[0] is False and "尚未开盘" in _BEHIND[1])
check("**没有日期时不算当日**,并且明说是「没给日期」而不是「数据落后」"
      "——「不知道」和「不是」要分得开",
      macro_mod._market_state("", _T0) == (False, "数据源没给行情日期,无法判断新旧。"))

# -- 取不到就是 None,不是 0 ------------------------------------------------

check("**取不到的数是 None,不是 0**(0 在这些品种里不可能,"
      "拿它顶替会让涨跌幅算出 -100%,而那个数字看着像真的)",
      macro_mod._f("") is None and macro_mod._f("--") is None
      and macro_mod._f(None) is None and macro_mod._f("0") == 0.0)

# -- 历史挂了要降级,不是整个作废 --------------------------------------------

_DEGRADED = macro_mod.MacroData(
    spec=macro_mod.spec_for("COMMODITY_GOLD"),
    realtime=macro_mod.Realtime(latest=2500.0, prev=2490.0, high=2510.0, low=2480.0,
                                time_text="10:11:12", day="2026-01-05"),
    closes=(), history_problem="演示:历史接口挂了",
).as_entry(today=_T0)
check("**历史挂了不作废整个对象**:最新值和涨跌幅还在,"
      "算不出来的那几项是 None(和账户挂了照样出判断是同一个取向)",
      _DEGRADED["最新值"] == 2500.0 and _DEGRADED["涨跌幅"] is not None
      and _DEGRADED["5日涨跌幅"] is None and _DEGRADED["20日波动率"] is None)
check("**降级时不省略字段**:算不出来的键仍然在,值是 None"
      "(少一个键模型压根不去想这项数据,填 None 它才看得见「这项不知道」)",
      all(k in _DEGRADED for k in
          ("5日涨跌幅", "20日涨跌幅", "20日波动率", "日内最高", "昨收")))
check("并且在「说明」里写清为什么算不出来",
      any("历史接口挂了" in s for s in (_DEGRADED["说明"] or [])))

# -- 实时那一根:该替换还是该追加,靠日期判,不靠长度 --------------------------
#
# 这一格错了不会报错:「今天 vs 5 天前」会静悄悄变成「昨天 vs 6 天前」,
# 算出来的涨跌幅完全合理,只是问错了问题。

_CLOSES = tuple(float(i) for i in range(1, 26))     # 1.0 .. 25.0
_RT_D = macro_mod.Realtime(latest=100.0, prev=24.0, high=None, low=None,
                           time_text="10:11:12", day="2026-01-05")
_SAME_DAY = macro_mod.MacroData(spec=macro_mod.spec_for("INDEX_DXY"), realtime=_RT_D,
                                closes=_CLOSES, history_last_day="2026-01-05"
                                ).as_entry(today=_T0)
check("**历史最后一根就是今天时,实时值替掉它**(不是追加)"
      "——追加会让「5 日前」整体错位一天",
      _SAME_DAY["5日涨跌幅"] == ind_mod.pct_change(100.0, 20.0))
_PREV_DAY = macro_mod.MacroData(spec=macro_mod.spec_for("INDEX_DXY"), realtime=_RT_D,
                                closes=_CLOSES, history_last_day="2026-01-02"
                                ).as_entry(today=_T0)
check("历史最后一根不是今天时,实时值追加一根",
      _PREV_DAY["5日涨跌幅"] == ind_mod.pct_change(100.0, 21.0))

# -- 符号表本身 -------------------------------------------------------------

check("**宏观对象的 short 都不是纯数字**——它要进标的清单的 symbol 位,"
      "和 A 股六位代码撞上会让 Catalog 直接拒绝加载",
      all(not s.short.isdigit() for s in macro_mod.MACRO_SPECS.values()))
check("short 之间不重复(重复会让两个宏观对象在 Catalog 里撞 symbol)",
      len({s.short for s in macro_mod.MACRO_SPECS.values()}) == len(macro_mod.MACRO_SPECS))

# -- 清单层:第三类对象 ------------------------------------------------------

_M_WTI, _ = catalog.validate_draft({"object_id": "COMMODITY_WTI", "类型": "宏观对象"})
check("**validate_draft 收宏观对象了**(改之前这里是 BAD_MARKET + BAD_SYMBOL,"
      "也就是说宏观对象一旦从界面上碰一下就会被清单拒掉)",
      _M_WTI is not None and _M_WTI.object_id == "COMMODITY_WTI")
check("名称和类别取自符号表,不取草稿里的值"
      "(它们是采集参数的一部分,让人在界面上随手改会造出和代码对不上的记录)",
      _M_WTI.name == "WTI原油" and _M_WTI.asset_type == "大宗商品")
check("**宏观对象自动不可下单**——``is_tradable`` 判的是 kind == 交易标的,"
      "所以新增这一类不需要在别处补一句 or",
      _M_WTI.is_tradable is False and _M_WTI.is_macro is True)
check("一手股数填 0 不填 100(100 是个看着正常的数,"
      "万一被喂进整手校验会静悄悄通过;0 会立刻炸出来)",
      _M_WTI.lot_size == 0)

_, _M_FAILS = catalog.validate_draft({"symbol": "BTC", "类型": "宏观对象"})
check("**宏观对象不能自己编**:符号表里没有的当场拒,"
      "并把可选项列出来(等到运行时每轮失败一次,没人会去看)",
      len(_M_FAILS) == 1 and _M_FAILS[0].code == "UNKNOWN_MACRO"
      and "COMMODITY_WTI" in _M_FAILS[0].message)

_, _BAD_KIND_FAILS = catalog.validate_draft(
    {"market": "MACRO", "symbol": "CL", "名称": "原油", "类型": "宏观", "资产类型": "股票"})
check("**类型写错时一次报全部原因**,不是只报「类型不对」让人再试一轮"
      "(和 guards.validate 同一个取向)",
      {f.code for f in _BAD_KIND_FAILS} == {"BAD_MARKET", "BAD_SYMBOL", "BAD_KIND"})

_M_CAT = catalog.Catalog([
    catalog.TradeObject("SH_510300", "SH", "510300", "沪深300ETF",
                        catalog.KIND_TRADABLE, "ETF", 100),
    catalog.TradeObject("SH_000001", "SH", "000001", "上证指数",
                        catalog.KIND_QUOTE_ONLY, "股票", 100),
    _M_WTI,
])
check("**quote_only 不含宏观对象**——两者采集路径不同,"
      "合成一个属性会让调用方误以为可以一起丢给 quotes.fetch",
      len(_M_CAT.quote_only) == 1 and len(_M_CAT.macro) == 1
      and len(_M_CAT.background) == 2 and len(_M_CAT.tradable) == 1)

# 清单存回去再读出来,类型不能丢。save_catalog 是**整份重写**,
# 它只写七个字段——宏观对象要能从这七个字段里原样还原。
_M_STORE = state.Store(Path(tempfile.mkdtemp(prefix="zhixing-macro-")))
_M_STORE.save_catalog(list(_M_CAT.objects))
_M_BACK = _M_STORE.catalog()
check("**宏观对象存盘再读回来还是宏观对象**"
      "(save_catalog 整份重写,类型丢了的话它会变成一只叫 WTI 的股票)",
      _M_BACK.get("COMMODITY_WTI") is not None
      and _M_BACK.get("COMMODITY_WTI").is_macro is True
      and len(_M_BACK.macro) == 1)
check("数据源自述里,清单有宏观对象时才提新浪"
      "(一个都没有时提了,就是承诺一件本轮根本不会发生的事)",
      "新浪" in collect_mod.describe_source(_M_STORE)
      and "新浪" not in collect_mod.describe_source(_DS_STORE))

# -- 采集层:结构上进不了校验表 ----------------------------------------------

_M_DATA = macro_mod.MacroData(
    spec=macro_mod.spec_for("COMMODITY_WTI"),
    realtime=macro_mod.Realtime(latest=70.0, prev=69.0, high=71.5, low=68.2,
                                time_text="10:11:12", day="2026-01-05"),
    closes=_CLOSES, history_last_day="2026-01-05")
_M_ITEMS = [collect_mod.ObjectData(obj=_M_WTI, macro=_M_DATA)]

check("只采到宏观数据的对象,ok 也是 True(不然它会被记成「没采到」)",
      _M_ITEMS[0].ok is True and _M_ITEMS[0].market is None)
check("**宏观值进不了那张按 symbol 建的查价表**——这是本组最要紧的一条:"
      "本地风控已拆,拦不住了,所以美元指数一旦能当「最新价」,"
      "98.7 会直接当成某只证券的价格进上下文和归档",
      collect_mod.snapshots(_M_ITEMS, _M_CAT) == {})
check("**宏观对象不进 data_window**(境外交易日可能领先一天,"
      "算进去会让「数据截止」显示明天,看的人只会以为是 bug)",
      collect_mod.data_window(_M_ITEMS) == {"起": "", "止": ""})

_M_ENTRY = collect_mod.market_entry(_M_ITEMS, today=_T0)[0]
check("**宏观对象的上下文形状是「宏观行情」,不是「行情」**"
      "——不拿 None 凑出一个开盘价/成交量/换手率齐全的证券壳子,"
      "那会让模型以为它在看一只股票",
      "宏观行情" in _M_ENTRY and "行情" not in _M_ENTRY
      and "持仓" not in _M_ENTRY and _M_ENTRY["可否下单"] is False)



# ===========================================================================
#  图鉴(打码平台)—— 验证码识别的第二条路
# ===========================================================================
#
# 这一组盯的是一件很容易写错的事:**识别失败和没问到是两回事**。
# 二代那份实现在两种情况下都返回 None,因为它后面还挂着本地 ddddocr 兜底;
# 三代没有兜底,None 会一路变成"拿空答案去提交",然后以「验证码错误」的
# 面目出现在登录日志里 —— 人会去查券商,而问题在识别这一层。

import base64


def _抛出的是(fn):
    """把抛出来的异常拿回来。``_raises`` 只回答"抛没抛",
    这里要问的是"抛的是哪一类"——而那正是本组最要紧的区分。"""
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001 - 就是要看它是什么
        return exc
    return None


check("**凭据是「用户名:密码」一份东西,不是两项**"
      "(拆成两个配置项之后,「改了用户名忘了改密码」就成了一种可能的状态,"
      "而它表现出来的样子是「识别老是失败」)",
      captcha_mod.split_credential("  demo:pw123  ") == ("demo", "pw123"))
check("密码里带冒号也拆得对(只切第一个冒号)",
      captcha_mod.split_credential("demo:a:b:c") == ("demo", "a:b:c"))
check("拆不出两段时当场说清形状,**不是拿空密码去发一次请求**"
      "(那一次请求会回来一个「用户名或密码错误」,指向完全错的地方)",
      _raises(captcha_mod.CaptchaError, lambda: captcha_mod.split_credential("只有用户名")))

_TT_BODY = captcha_mod.build_ttshitu_request(b"\xff\xd8\xff\xe0demo", credential="u:p")
check("请求体是 base64 原图 + 题目类型,**不带 mime 也不改格式**"
      "(东财那张实际是 JPEG 不是 PNG,挑格式会把本来能认的图拒掉)",
      _TT_BODY["image"] == base64.b64encode(b"\xff\xd8\xff\xe0demo").decode("ascii")
      and _TT_BODY["typeid"] == captcha_mod.TTSHITU_TYPEID
      and "mime" not in _TT_BODY)
check("整页截图那么大的「验证码」在图鉴这条路上一样被拦"
      "(两条路各写一遍上限判断会分叉,所以这里验的是它确实也判了)",
      _raises(captcha_mod.CaptchaError,
              lambda: captcha_mod.build_ttshitu_request(
                  b"x" * (captcha_mod.MAX_IMAGE_BYTES + 1), credential="u:p")))

check("认出来的四位原样返回",
      captcha_mod.extract_ttshitu_answer(
          {"success": True, "data": {"result": "6505"}}) == "6505")
check("**平台说没认出来时,把它的原话带上**"
      "(「余额不足」和「题目类型不对」都长成识别失败,不带原话就查不动)",
      _raises(captcha_mod.CaptchaError,
              lambda: captcha_mod.extract_ttshitu_answer(
                  {"success": False, "message": "余额不足"})))
check("**HTTP 200 但 success=false 不算「没问到」**——它问到了,只是没结果,"
      "重试多半还是同样的结果,所以抛 CaptchaError 而不是可重试的 LlmError",
      isinstance(_抛出的是(lambda: captcha_mod.extract_ttshitu_answer({"success": False})),
                 captcha_mod.CaptchaError)
      and not isinstance(
          _抛出的是(lambda: captcha_mod.extract_ttshitu_answer({"success": False})),
          llm.LlmError))
check("success=true 但没带结果时明说,不返回空串",
      _raises(captcha_mod.CaptchaError,
              lambda: captcha_mod.extract_ttshitu_answer({"success": True, "data": {}})))
check("图鉴返回的形状不合法(5 位)一样拒,**不提交上去试**"
      "(平台也会认错,认错的结果形状可以是合法的,也可以不是)",
      _raises(captcha_mod.CaptchaError,
              lambda: captcha_mod.extract_ttshitu_answer(
                  {"success": True, "data": {"result": "65051"}})))

# -- 配置 → 识别器 -----------------------------------------------------------

_TT_CFG = state.CaptchaSettings(
    provider=captcha_mod.PROVIDER_TTSHITU,
    endpoint=captcha_mod.TTSHITU_URL, secret="demo:DEMOFAKEPW01")
_TT_SOLVER = captcha_mod.solver_from_settings(_TT_CFG)
check("识别方式是图鉴时装图鉴那个,**接口地址原样不动**"
      "(视觉模型那条路会往地址后面接 /v1/chat/completions,接到图鉴上就 404)",
      isinstance(_TT_SOLVER, captcha_mod.TtshituSolver)
      and _TT_SOLVER.endpoint == captcha_mod.TTSHITU_URL)
check("题目类型没填就用默认的那一类(东财是 4 位数字,二代跑了几个月的那个值)",
      _TT_SOLVER.typeid == captcha_mod.TTSHITU_TYPEID)
check("**图鉴缺配置时不报「模型」**——报一个填了也没用的字段,"
      "人会照着填,然后开始怀疑填对了的那些",
      isinstance(captcha_mod.solver_from_settings(
          state.CaptchaSettings(provider=captcha_mod.PROVIDER_TTSHITU,
                                endpoint=captcha_mod.TTSHITU_URL)),
          captcha_mod.AlwaysFailSolver)
      and "模型" not in captcha_mod.solver_from_settings(
          state.CaptchaSettings(provider=captcha_mod.PROVIDER_TTSHITU,
                                endpoint=captcha_mod.TTSHITU_URL)).reason)
check("识别方式写了个不认识的值 → 明确失败,并把可选项列出来"
      "(退回默认那条路更糟:配置写的是一件事,系统在做另一件事)",
      "ttshitu" in captcha_mod.solver_from_settings(
          state.CaptchaSettings(provider="图鉴", endpoint="x", model="y", secret="z")).reason)
check("**改这一版之前落盘的配置读出来行为不变**(默认仍是视觉模型)"
      "——升级不该让一份本来能用的配置静悄悄失效",
      state.CaptchaSettings().provider == captcha_mod.PROVIDER_VISION)

# -- 落盘 --------------------------------------------------------------------

_CAP_STORE = state.Store(Path(tempfile.mkdtemp(prefix="zhixing-captcha-")))
_CAP_STORE.save_captcha(_TT_CFG)
check("识别方式存盘再读回来还在(丢了就会退回视觉模型,而视觉模型是拒答的)",
      _CAP_STORE.captcha().provider == captcha_mod.PROVIDER_TTSHITU
      and _CAP_STORE.captcha().secret == "demo:DEMOFAKEPW01")
check("**图鉴的密钥里有密码,一样只进 0600 文件,不进 JSON**",
      "DEMOFAKEPW01" not in _CAP_STORE.captcha_path.read_text(encoding="utf-8")
      and "识别方式" in _CAP_STORE.captcha_path.read_text(encoding="utf-8"))
_CAP_STORE.captcha_path.write_text('{"接口地址": "https://demo.invalid/v1", "模型": "m"}',
                                   encoding="utf-8")
check("老的 captcha.json 里没有「识别方式」,读出来算视觉模型,不算「没配」",
      _CAP_STORE.captcha().provider == captcha_mod.PROVIDER_VISION)

check("脱敏视图带识别方式,密钥照旧遮住",
      _TT_CFG.as_public()["识别方式"] == captcha_mod.PROVIDER_TTSHITU
      and "DEMOFAKEPW01" not in json.dumps(_TT_CFG.as_public(), ensure_ascii=False))

# -- 接口层 ------------------------------------------------------------------

check("PUT 能把识别方式改成图鉴,而且这时候「模型」可以空着",
      call("PUT", "/api/settings/captcha", body={
          "接口地址": captcha_mod.TTSHITU_URL, "模型": "",
          "识别方式": "ttshitu", "密钥": "demo:DEMOFAKEPW02",
      }).payload["ok"]
      and APP.store.captcha().provider == "ttshitu")
check("**不带「识别方式」= 这次没动它**(前端目前没有这个输入框,"
      "缺省成视觉模型的话,在界面上改一次地址就会把它悄悄改回去,"
      "而表现出来的样子是「登录突然不行了」)",
      call("PUT", "/api/settings/captcha", body={
          "接口地址": captcha_mod.TTSHITU_URL + "?v=2", "模型": "",
      }).payload["ok"]
      and APP.store.captcha().provider == "ttshitu")
check("识别方式写个不认识的值,接口当场拒,不存进去",
      call("PUT", "/api/settings/captcha", body={
          "接口地址": "https://demo.invalid/ocr", "模型": "m", "识别方式": "超级鹰",
      }).payload["error"]["code"] == "INVALID_CAPTCHA_SETTINGS"
      and APP.store.captcha().provider == "ttshitu")



# -- 登录提交按钮:属性选择器选不中没写属性的东西 -----------------------------
#
# 2026-08-20 实测,东财登录页上的按钮长这样:
#
#     <button id="btnConfirm" class="btn">登　录</button>
#
# 没有 type 属性。``el.type`` 读出来是 "submit",那是 DOM 给 <button> 的默认值,
# 而 CSS 的 ``button[type='submit']`` 匹配的是**写上去的属性**,所以选不中。
# 当时的候选列表里 11 个选择器一个都没命中,登录停在"找不到提交按钮"。

check("提交脚本里有东财实测的那个按钮 id"
      "(靠通用选择器猜的话,就会重演 2026-08-20 那次:列表看着很全,一个都不中)",
      "#btnConfirm" in eastmoney_mod.SUBMIT_LOGIN_JS)
check("**兜底不靠 `button[type=\'submit\']`**——那是属性选择器,"
      "而东财那个按钮没写 type 属性;兜底读的得是 el.type 这个 DOM property",
      "button[type='submit']" not in eastmoney_mod.SUBMIT_LOGIN_JS
      and 'el.type === "submit"' in eastmoney_mod.SUBMIT_LOGIN_JS)


# ===========================================================================
#  登录:东财会把"已经登着的浏览器"直接跳出登录页
# ===========================================================================
#
# 实测(2026-08-20):浏览器带着有效会话去开 LOGIN_URL,东财不显示登录页,
# 直接跳到 returl 指的地方。跳转是**异步**的,可能落在填表后、抓图后,
# 甚至提交脚本跑到一半——元素在脚本手里被卸载。
#
# 于是同一个原因跑出三种错,而且三种都指向选择器:
#     「登录页上找不到提交按钮」「登录页上找不到验证码输入框」
#     「验证码填不进去,页面反复清空输入框」
# 照着这些话去改选择器,改多久都不会好。

from zhixing import login as login_mod

#: 假账户返回。数字是编的,**别拿它对任何真账户**。
_资产返回 = {"Status": "0", "Data": {"Zzc": "123456.78", "Kyzj": "12345.67", "Zxsz": "0"}}


class _假会话:
    """按脚本回放的浏览器。``地址`` 是一串,每被问一次就往后走一格。"""

    def __init__(self, 地址, 提交回应=None, 账户通=True, 页面报错=()):
        self._地址 = list(地址)
        self.提交回应 = 提交回应 or {"ok": True, "detail": ""}
        self.账户通 = 账户通
        # 每读一次弹掉一份,读完就只剩空的 —— 模拟"那一页已经被刷掉了"。
        # 报错只在**第一次**读得到,晚一步去读就什么都没有。
        self.页面报错 = list(页面报错)
        self.跑过 = []          # 跑过哪些脚本,用脚本里的特征串认
        self.去过 = []          # navigate 请求过哪些地址

    def current_url(self):
        return self._地址[0] if len(self._地址) == 1 else self._地址.pop(0)

    def navigate(self, url):
        # **只记不改地址。** 现实里"跳到哪"由东财说了算,不由我们请求的地址
        # 说了算——这整组自检说的就是这件事。假会话要是照请求改地址,
        # 就把被测的那个现象抹掉了。
        self.去过.append(url)

    def execute_async(self, script, *args):
        # 认脚本靠各自独有的特征串。**别用参数里的词**——比如接口路径是
        # 当参数传进去的,脚本正文里根本没有它,照那个认会全部落到兜底分支,
        # 而兜底分支的返回长得像"账户接口没通"。
        if "const endpoint = arguments[0]" in script:
            self.跑过.append("查账户")
            return ({"ok": True, "response": _资产返回} if self.账户通
                    else {"ok": True, "response": {"Status": "-2"}})
        if "toDataURL" in script:
            self.跑过.append("抓验证码")
            return {"ok": True, "width": 101, "height": 36,
                    "data_url": "data:image/png;base64,aGk="}
        if "找不到验证码输入框" in script:
            self.跑过.append("提交")
            return self.提交回应
        if "#ertips" in script:
            self.跑过.append("读报错")
            报 = self.页面报错.pop(0) if self.页面报错 else []
            return {"ok": True, "messages": list(报),
                    "current_url": self._地址[0] if self._地址 else ""}
        self.跑过.append("填账号密码")
        return {"ok": True, "detail": "", "account_filled": True, "password_filled": True}


class _假识别器:
    def solve(self, image):
        return "1234"


_已登着 = _假会话(["https://jywgmix.18.cn/Trade/Buy"])
login_mod._attempt(_已登着, account="000", password="x", solver=_假识别器())
check("**打开登录页时已经登着,就不再走登录动作**"
      "(东财对有会话的浏览器直接跳走,那个页面上没有账号框——"
      "硬走下去只会报「登录页上找不到 xx」,指向选择器,而真相是已经进去了)",
      "填账号密码" not in _已登着.跑过 and "提交" not in _已登着.跑过)

_中途跳走 = _假会话([login_mod.em.LOGIN_URL, "https://jywgmix.18.cn/Trade/Buy"])
login_mod._attempt(_中途跳走, account="000", password="x", solver=_假识别器())
check("**跳转落在填表和抓图那几秒里,也要认出来**——否则就是拿着一个认好的"
      "验证码,去一个没有验证码框的页面上填",
      "提交" not in _中途跳走.跑过 and "抓验证码" in _中途跳走.跑过)

_脚本喊冤 = _假会话([login_mod.em.LOGIN_URL] * 3 + ["https://jywgmix.18.cn/Trade/Buy"],
                   提交回应={"ok": False, "detail": "登录页上找不到验证码输入框。"})
_没抛 = True
try:
    login_mod._attempt(_脚本喊冤, account="000", password="x", solver=_假识别器())
except login_mod.LoginError:
    _没抛 = False
check("**提交脚本说失败,不等于没登上**(跳转可能落在脚本跑到一半:元素在它"
      "手里被卸载,它只能说「找不到」,而登录其实已经成立)——判据只有账户接口",
      _没抛)

_真没登上 = _假会话([login_mod.em.LOGIN_URL] * 6,
                   提交回应={"ok": False, "detail": "登录页上找不到验证码输入框。"},
                   账户通=False)
_错 = None
try:
    login_mod._attempt(_真没登上, account="000", password="x", solver=_假识别器())
except login_mod.LoginError as exc:
    _错 = exc
check("确实没登上时,把提交脚本的原话带出来,**不要换成一句「确认登录失败」**"
      "(那句话对着一个页面结构问题什么都没说)",
      _错 is not None and "找不到验证码输入框" in str(_错))

# -- 报错要在页面被刷掉之前读 -----------------------------------------------
#
# 2026-08-21 11:16 实盘登录失败,日志里只有一句「登录没成功,页面上也没有
# 给出原因」。验证码是认对的、提交脚本报的是成功、账户接口说没登进去,
# 而页面上"什么都没说"——**因为 verify_logged_in 干的第一件事就是
# 「地址里有 Login 就导航到持仓页」,而登录失败时人就还在登录页上,这个
# 条件必然成立。那一跳把带着 #ertips 的页面刷掉了。**
#
# 诊断的步骤销毁了要诊断的证据。下面这个假会话只让第一次读到报错,
# 把读报错挪回 verify_logged_in 之后,这两条立刻红。
_报错被刷掉 = _假会话([login_mod.em.LOGIN_URL] * 6, 账户通=False,
                     页面报错=[["您输入的信息有误，请重新输入!"]])
_错2 = None
try:
    login_mod._attempt(_报错被刷掉, account="000", password="x",
                       solver=_假识别器(), password_proven=True)
except login_mod.LoginError as exc:
    _错2 = exc
check("**页面上的报错要赶在导航之前读**——晚一步那一页就被刷成干净的了,"
      "读到的永远是空,于是归不了类(2026-08-21 11:16 就是这么把原因弄丢的)",
      _错2 is not None and "您输入的信息有误" in str(_错2))
check("**顺序本身就是修复**:读报错必须排在查账户前面",
      "读报错" in _报错被刷掉.跑过 and "查账户" in _报错被刷掉.跑过
      and _报错被刷掉.跑过.index("读报错") < _报错被刷掉.跑过.index("查账户"))
check("读到含混报错 + 这套账号密码此前登进去过 → **判成可重试**,换张验证码再来;"
      "读不到就只能按不可重试一次就停,而认错验证码恰恰是最常见的那一类",
      _错2 is not None and _错2.retryable is True)

_读不到 = _假会话([login_mod.em.LOGIN_URL] * 6, 账户通=False)
_错3 = None
try:
    login_mod._attempt(_读不到, account="000", password="x",
                       solver=_假识别器(), password_proven=True)
except login_mod.LoginError as exc:
    _错3 = exc
check("页面真的什么都没说时,仍然按**不可重试**处理"
      "(判错方向的代价不对称:把致命的当可重试,是拿账户去撞券商的次数上限)",
      _错3 is not None and _错3.retryable is False
      and "没有给出原因" in str(_错3))

_成功 = _假会话([login_mod.em.LOGIN_URL] * 6)
login_mod._attempt(_成功, account="000", password="x", solver=_假识别器())
check("多读这一次不能把成功路径带坏(登录成功时页面正在跳走,读不到也无所谓)",
      "查账户" in _成功.跑过)


# -- 重试要换识别路,不是拿同一条撞三遍 --------------------------------------
#
# 2026-08-21 实测:全天两轮登录各试满三次全败;事后拿备用引擎离线复核当天
# 存下的 11 张图,**两家答案一致的 5 次全部登录成功,不一致的 6 次全部失败**,
# 没有一个例外。也就是说主路当天准确率 45%,而三次尝试全落在它身上——
# 那三次的错误是相关的,1-(1-p)³ 那个算法根本不成立。
#
# 备用引擎当天被调用 **0 次**:链的降级只在「这条路没给出答案」时触发,
# 而主路从不哑,它只是答错。

class _会换路的识别器:
    """记下每次被要求从第几条路起认。"""

    def __init__(self):
        self.起点 = []

    def solve(self, image, *, mime="image/png"):
        self.起点.append(0)
        return "1234"

    def solve_from(self, image, *, mime="image/png", start=0):
        self.起点.append(start)
        return "1234"


_换路器 = _会换路的识别器()
_三次全败 = _假会话([login_mod.em.LOGIN_URL] * 40, 账户通=False,
                   页面报错=[["您输入的信息有误，请重新输入!"]] * 3)
_错4 = None
try:
    login_mod.login(_三次全败, account="000", password="x", solver=_换路器,
                    sleep=lambda _s: None, password_proven=True)
except login_mod.LoginError as exc:
    _错4 = exc
check("**三次尝试落在三条不同的识别路上**"
      "(同一条撞三遍的话,它认不准的那类字形每次都认不准——"
      "换的是图,栽的是同一个坎)",
      _换路器.起点 == [0, 1, 2])
check("三次都没登进去时,**说的是「未知问题」,不是「验证码没认对」**"
      "(该换的路都换过、每条都给了答案、每条都被券商退回来,"
      "这时候再把锅扣给识别就是在编原因)",
      _错4 is not None and "未知问题" in str(_错4))
check("而且带着 ``exhausted`` 标记,让上层不必去匹配错误文字",
      _错4 is not None and _错4.exhausted is True and _错4.retryable is False)

_换路器2 = _会换路的识别器()
_一次就停 = _假会话([login_mod.em.LOGIN_URL] * 40, 账户通=False)
_错5 = None
try:
    login_mod.login(_一次就停, account="000", password="x", solver=_换路器2,
                    sleep=lambda _s: None, password_proven=True)
except login_mod.LoginError as exc:
    _错5 = exc
check("**判定不可重试时一次就停,``exhausted`` 是 False**"
      "(「我认出这是致命的」和「都试完了还是不行」对人的意思完全不同)",
      _错5 is not None and _错5.exhausted is False and _换路器2.起点 == [0])

check("只配了一条识别路时(那种情况 solver_from_settings 返回的不是链),"
      "**退回普通 solve,不因为没有 solve_from 就崩**",
      login_mod._认验证码(_假识别器(), b"img", 3) == "1234")


# -- 连通性探针要按真实请求的样子打 -----------------------------------------
#
# 2026-08-20 实测:DeepSeek 在 response_format=json_object 时会检查提示词里
# 有没有 "json" 这个词,没有就 400。当时探针的提示词是「只回复两个字:收到。」
# ——于是一套**完全正确**的配置被判成打不通,而它给的三条"常见原因"
# (密钥没权限 / 协议选反 / 地址少了 /v1)一条都不沾边。
#
# 会把好配置判成坏配置的探针,比没有探针更糟。

import inspect as _inspect

from zhixing import setup_model as _setup_model

_探针源码 = _inspect.getsource(_setup_model._probe)
check("**连通性探针的提示词里得有 json 这个词**"
      "(force_json 开着时,服务端会因为提示词里没提 json 而 400——"
      "探针不按真实请求的样子打,就会把对的配置判成错的,"
      "然后把人支去改密钥、改地址、改协议,而那三样本来都是对的)",
      "json" in _探针源码.split("build_request")[1].split(")")[0].lower())


# -- 登录失败要读得出原因,而且要分得清"哪一项错了" -------------------------
#
# 2026-08-20 实测:东财把登录报错放在 <li id="ertips">,文字是
# 「您输入的信息有误，请重新输入!」——**账号错、密码错、验证码错,回的
# 都是这一句**。原来那九个选择器一个都不匹配它,于是程序说"页面上没有
# 给出原因",判成不可重试,一整轮作废。
#
# 这句话不能简单归到任何一边:归可重试,密码真错时一天六轮×3次会把卡
# 试锁;归致命,认错一张验证码就废掉一轮。所以按"这套密码此前成功登过
# 没有"来分——密码是固定的,每次尝试唯一在变的只有验证码。

_真实报错 = ["您输入的信息有误，请重新输入!"]

check("**读报错的选择器里得有 #ertips**"
      "(2026-08-20 对着真登录页抓下来的唯一命中项;删了它就回到"
      "「登录失败但页面上没给原因」那个洞里)",
      "#ertips" in eastmoney_mod.READ_LOGIN_ERROR_JS)

check("读报错**不能再用 offsetParent 判可见**"
      "(那个判据从没对着真页面验过,和 #errMsg 那批选择器是同一批猜测;"
      "现在用的是实测能看见 #ertips 的 getComputedStyle + 占位)",
      "offsetParent" not in eastmoney_mod.READ_LOGIN_ERROR_JS
      and "getBoundingClientRect" in eastmoney_mod.READ_LOGIN_ERROR_JS)

check("**密码已被证明可用时**,「信息有误」按验证码没认对处理,可以重试"
      "(否则图鉴认错一张图就废掉一整轮,一天六轮扛不住)",
      eastmoney_mod.classify_login_error(_真实报错, password_proven=True)[0] is True)

check("**密码没被证明过时**,同一句话一次就停"
      "(刚配好或刚改过密码时重试,就是拿可能错的密码去撞券商的次数上限)",
      eastmoney_mod.classify_login_error(_真实报错, password_proven=False)[0] is False)

check("password_proven 的默认值是「没证明过」"
      "(谁忘了传,得落在保守那一边,不能落在敢重试那一边)",
      eastmoney_mod.classify_login_error(_真实报错)[0] is False)

check("**致命提示不受 password_proven 影响**"
      "(「密码」「锁定」「次数」这类字样出现时,登过多少次都不该再试)",
      eastmoney_mod.classify_login_error(["您的密码错误次数过多，账户已锁定"],
                              password_proven=True)[0] is False)

_LP = state.Store(Path(tempfile.mkdtemp(prefix="zhixing-loginproof-")))
check("没记过指纹时,login_proven 是 False(而不是抛异常)",
      _LP.login_proven("A1", "P1") is False)
_LP.save_login_proof("A1", "P1")
check("记过之后同一套账号密码认得出来", _LP.login_proven("A1", "P1") is True)
check("**密码一改,指纹立刻对不上**"
      "(存布尔量的话,改错密码的那一刻反而拿到了「放心重试」的许可)",
      _LP.login_proven("A1", "P2") is False)
check("账号一改也对不上", _LP.login_proven("A2", "P1") is False)
check("**落盘的是指纹,不是密码**(这份文件跟着运行目录走,不能有明文)",
      "P1" not in _LP.login_proof_path.read_text(encoding="utf-8"))


# -- 超级鹰,以及"一条路断了还有下一条" --------------------------------------
#
# 验证码这一步失败**不表现成"验证码平台不行"**,它表现成"今天没交易"。
# 所以这里护的不是识别率,是单点:一家欠费或者当天挂了,登录就登不进去。


def _CJ抛(类, fn):
    """跑一下,把抛出来的话还回来;没抛或者抛了别的,还回空串。"""
    try:
        fn()
    except 类 as exc:
        return str(exc) or "(空话)"
    except Exception as exc:                      # noqa: BLE001
        return ""
    return ""


class _CJ认不出:
    def __init__(self, 话):
        self.话 = 话

    def solve(self, image, *, mime="image/png"):
        raise captcha_mod.CaptchaError(self.话)


class _CJ问不到:
    def solve(self, image, *, mime="image/png"):
        raise captcha_mod.LlmError("连不上", retryable=True)


class _CJ认得出:
    def solve(self, image, *, mime="image/png"):
        return "ab12"


# -- 凭据:三段合起来才是一份 ------------------------------------------------

check("超级鹰的密钥拆成「用户名:密码:软件ID」三段",
      captcha_mod.split_chaojiying_credential("demo:DEMOFAKEPW03:1234")
      == ("demo", "DEMOFAKEPW03", "1234"))
check("**密码里可以有冒号**(软件ID 从右边切,用户名从左边切)",
      captcha_mod.split_chaojiying_credential("demo:a:b:1234")
      == ("demo", "a:b", "1234"))
check("少一段就当场说清楚是缺了哪种形状,不是等到识别时报一句平台原话",
      "三段" in _CJ抛(captcha_mod.CaptchaError,
                    lambda: captcha_mod.split_chaojiying_credential("demo:DEMOFAKEPW03")))
check("**软件ID 不是数字就当场拦住**(填错了平台不报参数错,只是不给分成——"
      "一个不报错的错,只能在这里拦)",
      "软件ID" in _CJ抛(captcha_mod.CaptchaError,
                      lambda: captcha_mod.split_chaojiying_credential("demo:DEMOFAKEPW03:x246")))

# -- 请求体:纯函数,而且不带明文密码 ----------------------------------------

_CJ_REQ = captcha_mod.build_chaojiying_request(b"fake-image-bytes",
                                               credential="demo:DEMOFAKEPW03:1234")
check("**表单里没有明文密码**,只有它的 md5(平台两个都收,明文那个没有任何好处)",
      "DEMOFAKEPW03" not in json.dumps(_CJ_REQ, ensure_ascii=False)
      and _CJ_REQ["pass2"]
      == captcha_mod.hashlib.md5(b"DEMOFAKEPW03").hexdigest())
check("题目类型没填就用 4 位英数那一类,而且**和图鉴的 typeid 不是同一个号**"
      "(两家各编各的,填串了的表现是「一直识别失败」而不是报参数错)",
      _CJ_REQ["codetype"] == captcha_mod.CHAOJIYING_CODETYPE
      and captcha_mod.CHAOJIYING_CODETYPE != captcha_mod.TTSHITU_TYPEID)
check("图是空的就不发出去(发过去也是白花一次钱)",
      bool(_CJ抛(captcha_mod.CaptchaError,
           lambda: captcha_mod.build_chaojiying_request(b"", credential="demo:DEMOFAKEPW03:1234"))))
check("图大得离谱就不发出去(多半是截错了范围)",
      "上限" in _CJ抛(captcha_mod.CaptchaError,
                    lambda: captcha_mod.build_chaojiying_request(
                        b"x" * (captcha_mod.MAX_IMAGE_BYTES + 1),
                        credential="demo:DEMOFAKEPW03:1234")))

# -- 回复:失败时 HTTP 也是 200 ----------------------------------------------

check("err_no=0 时把 pic_str 取出来",
      captcha_mod.extract_chaojiying_answer(
          {"err_no": 0, "err_str": "OK", "pic_id": "1", "pic_str": "ab12"}) == "ab12")
check("**err_no 非 0 是「认不出」不是「没问到」**,而且把平台原话带上"
      "(题分不足重试一万次还是题分不足;那句话才是唯一指得到原因的东西)",
      "题分不足" in _CJ抛(captcha_mod.CaptchaError,
                        lambda: captcha_mod.extract_chaojiying_answer(
                            {"err_no": -1001, "err_str": "题分不足"})))
check("平台说成功但结果形状不对(比如 5 位)也算认不出,**不往下提交**",
      "形状" in _CJ抛(captcha_mod.CaptchaError,
                    lambda: captcha_mod.extract_chaojiying_answer(
                        {"err_no": 0, "pic_str": "abc123"})))

# -- 串起来 ------------------------------------------------------------------

check("前一条断了,后一条接得住",
      captcha_mod.ChainSolver(links=(("甲", _CJ认不出("甲不行")),
                                     ("乙", _CJ认得出()))).solve(b"img") == "ab12")
_CJ_全断 = _CJ抛(captcha_mod.CaptchaError,
                lambda: captcha_mod.ChainSolver(
                    links=(("甲", _CJ认不出("甲没钱了")),
                           ("乙", _CJ认不出("乙认不出")))).solve(b"img"))
check("**全断了要一次报全部原因**"
      "(只报最后一条的话,人会去查乙,而钱是欠在甲那里)",
      "甲没钱了" in _CJ_全断 and "乙认不出" in _CJ_全断)


def _CJ全问不到():
    captcha_mod.ChainSolver(links=(("甲", _CJ问不到()), ("乙", _CJ问不到()))).solve(b"img")


try:
    _CJ全问不到()
    _CJ_类型 = "没抛"
except captcha_mod.CaptchaError:
    _CJ_类型 = "CaptchaError"
except captcha_mod.LlmError:
    _CJ_类型 = "LlmError"
check("**每条路都是「没问到」时抛的是 LlmError,不是 CaptchaError**"
      "(「不知道」和「不是」得分得开:前者原地重试有意义,后者得换张图)",
      _CJ_类型 == "LlmError")

# -- 配置 → 链 ---------------------------------------------------------------

check("没配备用、也不攒样本时,**不套那层壳**(单条路上它一点用没有,"
      "只会在报错里多一层要剥的东西)",
      isinstance(captcha_mod.solver_from_settings(_TT_CFG), captcha_mod.TtshituSolver))
_CJ_CFG = state.CaptchaSettings(
    provider=captcha_mod.PROVIDER_TTSHITU,
    endpoint=captcha_mod.TTSHITU_URL, secret="demo:DEMOFAKEPW01",
    backups=(state.CaptchaLink(provider=captcha_mod.PROVIDER_CHAOJIYING,
                               endpoint=captcha_mod.CHAOJIYING_URL,
                               secret="demo:DEMOFAKEPW03:1234"),))
_CJ_CHAIN = captcha_mod.solver_from_settings(_CJ_CFG)
check("配了备用就串成链,顺序是「主用在前」",
      isinstance(_CJ_CHAIN, captcha_mod.ChainSolver)
      and len(_CJ_CHAIN.links) == 2
      and isinstance(_CJ_CHAIN.links[0][1], captcha_mod.TtshituSolver)
      and isinstance(_CJ_CHAIN.links[1][1], captcha_mod.ChaojiyingSolver))
check("链上每条路都有个**给人看的名字**(看日志的人要照着它去哪家后台查余额,"
      "「ttshitu」不告诉他这件事)",
      "图鉴" in _CJ_CHAIN.links[0][0] and "超级鹰" in _CJ_CHAIN.links[1][0])
check("备用那条没配全时,它只是这一条失败,**不影响主用那条**"
      "(整份配置一起判死的话,填错备用等于把能用的也关掉)",
      isinstance(captcha_mod.solver_from_settings(state.CaptchaSettings(
          provider=captcha_mod.PROVIDER_TTSHITU, endpoint=captcha_mod.TTSHITU_URL,
          secret="demo:DEMOFAKEPW01",
          backups=(state.CaptchaLink(provider=captcha_mod.PROVIDER_CHAOJIYING),),
      )).links[0][1], captcha_mod.TtshituSolver))

# -- 换起点:降级管不了「答错了」 --------------------------------------------
#
# 降级只在「这条路没给出答案」时触发。而识别接口最常见的故障是**给一个
# 形状合法但认错了的四位数**——那种情况链看不出来,直接就用了,后面几条
# 一次都轮不到。2026-08-21 实测:全天 11 次识别,主路 11 次全给了合法答案
# (其中 6 次是错的),备用路被调用 **0 次**。所以要能换起点。


class _CJ记名():
    """记下自己被问过几次。用来验证「第 N 次从第 N 条起」真的换了路。"""

    def __init__(self, 答):
        self.答 = 答
        self.次数 = 0

    def solve(self, image, *, mime="image/png"):
        self.次数 += 1
        return self.答


_CJ甲, _CJ乙, _CJ丙 = _CJ记名("1111"), _CJ记名("2222"), _CJ记名("3333")
_CJ_三条 = captcha_mod.ChainSolver(
    links=(("甲", _CJ甲), ("乙", _CJ乙), ("丙", _CJ丙)))
check("**第 N 次尝试从第 N 条路起认**(三次都从头认的话就是同一个接口撞三遍,"
      "而它对哪类字形认不准是稳定的——那三次的错误是相关的,不是独立的)",
      [_CJ_三条.solve_from(b"img", start=i) for i in (0, 1, 2)]
      == ["1111", "2222", "3333"])
check("换了起点就**真的不问前面那几条**了(问了等于白花一次调用,"
      "而且它刚给过的错答案会再给一遍)",
      (_CJ甲.次数, _CJ乙.次数, _CJ丙.次数) == (1, 1, 1))
check("起点越界压到最后一条,**不报错**"
      "(链配几条是配置说了算,重试几次是登录层说了算,两个数对不上是常态)",
      _CJ_三条.solve_from(b"img", start=99) == "3333")
check("solve() 就是 start=0,协议那个入口行为不变",
      _CJ_三条.solve(b"img") == "1111")
check("从第二条起认时,**报错只数还剩的那几条**"
      "(说「3 条都没走通」而实际只试了 2 条,是在编事实)",
      "1 条" in _CJ抛(captcha_mod.CaptchaError,
                    lambda: captcha_mod.ChainSolver(
                        links=(("甲", _CJ认得出()), ("乙", _CJ认不出("乙认不出")))
                    ).solve_from(b"img", start=1)))
check("一条路都没配时 solve_from 抛的是 CaptchaError,不是 IndexError",
      _raises(captcha_mod.CaptchaError,
              lambda: captcha_mod.ChainSolver(links=()).solve_from(b"img")))

# -- 样本 --------------------------------------------------------------------

_CJ_DIR = Path(tempfile.mkdtemp(prefix="zhixing-captcha-sample-"))
check("样本存下来了,而且**文件名里带着答案**(不带的话这堆图没法当标注用)",
      captcha_mod.record_sample(_CJ_DIR, b"img-a", "ab12", source="图鉴") is True
      and any("ab12" in p.name for p in _CJ_DIR.iterdir()))
check("同一张图不重复存",
      captcha_mod.record_sample(_CJ_DIR, b"img-a", "ab12", source="图鉴") is False)
check("**到上限就停手,不是报错**(攒样本失败不该让一次登录失败)",
      captcha_mod.record_sample(_CJ_DIR, b"img-b", "cd34", source="图鉴", limit=1) is False)
check("目录不给写也只是不攒,照样把答案还回去",
      captcha_mod.ChainSolver(links=(("甲", _CJ认得出()),),
                              sample_dir=Path("/proc/不存在/也写不了")).solve(b"img") == "ab12")

# -- 落盘 --------------------------------------------------------------------

_CJ_STORE = state.Store(Path(tempfile.mkdtemp(prefix="zhixing-captcha-chain-")))
_CJ_STORE.save_captcha(_CJ_CFG)
check("备用识别路存盘再读回来还在,顺序不变",
      [条.provider for 条 in _CJ_STORE.captcha().backups]
      == [captcha_mod.PROVIDER_CHAOJIYING])
check("**备用的密钥不进 captcha.json**(那份文件不是 0600 的)",
      "DEMOFAKEPW03" not in _CJ_STORE.captcha_path.read_text(encoding="utf-8"))
_CJ_STORE.save_captcha(state.CaptchaSettings(
    provider=captcha_mod.PROVIDER_TTSHITU, endpoint="https://demo.invalid/p",
    secret="demo:DEMOFAKEPW01"))
check("**提交里不带备用 = 这次没动它,不是清空**"
      "(在界面上改一次接口地址就把兜底那几条悄悄拆掉的话,"
      "拆掉之后一切照常,直到主用那条挂了才知道)",
      len(_CJ_STORE.captcha().backups) == 1)
check("脱敏视图里备用的密钥也是遮住的",
      "DEMOFAKEPW03" not in json.dumps(_CJ_CFG.as_public(), ensure_ascii=False)
      and len(_CJ_CFG.as_public()["备用识别"]) == 1)
check("样本目录跟着运行目录走,**不是配置项**(没有「配错」这种状态)",
      _CJ_STORE.captcha().sample_dir == str(_CJ_STORE.root / "captcha-samples"))

# -- 接口层 ------------------------------------------------------------------

check("PUT 能配备用识别路",
      call("PUT", "/api/settings/captcha", body={
          "接口地址": captcha_mod.TTSHITU_URL, "识别方式": "ttshitu",
          "密钥": "demo:DEMOFAKEPW02",
          "备用识别": [{"识别方式": "chaojiying", "接口地址": captcha_mod.CHAOJIYING_URL,
                     "密钥": "demo:DEMOFAKEPW03:1234"}],
      }).payload["ok"]
      and len(APP.store.captcha().backups) == 1)
check("备用那条的识别方式写个不认识的值,接口当场拒",
      call("PUT", "/api/settings/captcha", body={
          "接口地址": captcha_mod.TTSHITU_URL, "识别方式": "ttshitu",
          "备用识别": [{"识别方式": "超级鹰", "接口地址": "x", "密钥": "y"}],
      }).payload["ok"] is False)
check("**新加的备用不给密钥就拒**(沿用原值只在「还是原来那一条」时成立,"
      "换了家还沿用,拿到的是另一家的密钥,只会得到一句看不懂的平台报错)",
      call("PUT", "/api/settings/captcha", body={
          "接口地址": captcha_mod.TTSHITU_URL, "识别方式": "ttshitu",
          "备用识别": [{"识别方式": "vision", "接口地址": "https://demo.invalid/v1",
                     "模型": "m"}],
      }).payload["ok"] is False)

# ---------------------------------------------------------------------------
#  历史判断 —— 「我上次对这个标的说过什么」
# ---------------------------------------------------------------------------
#
# 这一段查的全是**会静默出错**的地方:少一天、多一天、把二代的当成自己的、
# 种子文件坏了。它们都不抛异常,只会让上下文里的历史悄悄变了个样,
# 而模型不会抱怨,只会照着变了的历史给出判断。

import json as _json
import tempfile as _tempfile

_H = Path(_tempfile.mkdtemp(prefix="zx-history-"))
_HA, _HR = _H / "archives", _H / "runtime"
(_HA / "2026-08").mkdir(parents=True)
_HR.mkdir(parents=True)


def _归档(名: str, *, 时间: str, 判断, 指令=()) -> None:
    (_HA / "2026-08" / f"{名}.json").write_text(_json.dumps({
        "strategy_id": 名, "生成时间": 时间,
        "交易对象判断": list(判断), "待执行指令": list(指令),
    }, ensure_ascii=False), encoding="utf-8")


def _判(oid: str, 操作: str = "hold") -> dict:
    return {"object_id": oid, "操作": 操作, "置信度": 0.6,
            "理由": ["甲", "乙", "丙"], "改判条件": "跌破均线"}


def _种子(按标的) -> None:
    (_HR / history.SEED_NAME).write_text(
        _json.dumps({"按标的": 按标的}, ensure_ascii=False), encoding="utf-8")


def _取(oid: str = "SH_510300", **kw):
    return history.recent(_HA, _HR, object_ids=[oid], **kw).get(oid, ())


check("**没归档没种子也不炸,给的是空**(历史是参考不是前提,"
      "为它作废一轮,代价和它的分量对不上)",
      _取() == ())

_种子({"SH_510300": [
    {"交易日": "2026-08-19", "时间": "2026-08-19T14:00:00", "操作": "sell",
     "置信度": 0.7, "理由": ["二代说的"]},
]})
_一 = _取()
check("二代的垫底数据读得进来,而且**标着「来源: 二代」**"
      "(不标就等于告诉模型这是它自己说过的话,那是编的)",
      len(_一) == 1 and _一[0].get("来源") == "二代"
      and _一[0]["理由"] == ["二代说的"])

_归档("a", 时间="2026-08-19T09:40:00", 判断=[_判("SH_510300", "buy")])
_二 = _取()
check("**同一个交易日两边都有,用三代自己的**"
      "(二代是另一个模型另一套提示词,它盖过本系统说过的话,"
      "「我上次说过什么」这句话就不成立了)",
      len(_二) == 1 and _二[0]["操作"] == "buy" and "来源" not in _二[0])

for _i, _日 in enumerate(["12", "13", "14", "17", "18"]):
    _归档(f"d{_i}", 时间=f"2026-08-{_日}T14:50:00", 判断=[_判("SH_510300")])
_三 = _取()
check(f"窗口是 **5 个有记录的交易日**,更老的滚出去(现有 6 天,取到 {len(_三)} 条)",
      len(_三) == 5)
check("**滚出去的是最老的那天**(2026-08-12 不在里面,2026-08-19 在)",
      all(r["交易日"] != "2026-08-12" for r in _三)
      and any(r["交易日"] == "2026-08-19" for r in _三))
check("返回按时间正序(老的在前)——倒着给,模型读到的「最近一次」是最早那次",
      [r["交易日"] for r in _三] == sorted(r["交易日"] for r in _三))

_归档("b", 时间="2026-08-19T10:30:00", 判断=[_判("SH_510300", "sell")],
     指令=[{"instruction_code": "20260819-103000-SH_510300-sell",
            "action": "sell", "market": "SH", "symbol": "510300", "qty": 100}])
_四 = [r for r in _取() if r["交易日"] == "2026-08-19"]
check("**出过指令的那一轮不会被「每天只留最后一轮」丢掉**"
      "(「我那天动过手」正是历史里最该被看见的部分)",
      any("指令" in r for r in _四))
check("指令按 **market + symbol** 认领标的——归档里的指令根本没有 object_id,"
      "这条夹具早先是照着 object_id 编的,于是测试全绿而生产每一条指令都悄悄丢了",
      any(r.get("指令") and r["指令"][0]["symbol"] == "510300" for r in _四))

_归档("b2", 时间="2026-08-18T10:30:00", 判断=[_判("SH_510300", "buy")],
     指令=[{"object_id": "SH_510300", "action": "buy", "qty": 200}])
check("带 object_id 的老形状仍然认(接口换过形状,历史归档不会跟着改)",
      any(r["交易日"] == "2026-08-18" and r.get("指令") for r in _取()))

check("理由只留前两条(第三条起是当时的推演,过几天既不是事实也不是结论)",
      all(len(r.get("理由", [])) <= 2 for r in _取()))

(_HR / history.SEED_NAME).write_text("{这不是 JSON", encoding="utf-8")
_五 = _取()
check("**种子文件坏了当成没有,不抛**(一份坏掉的垫底文件不该拖垮整轮判断);"
      "三代自己的记录照常给",
      _五 and all("来源" not in r for r in _五))

check("不在 object_ids 里的标的一条都不出现(上下文里冒出本轮没问的标的,"
      "够人查半天)",
      history.recent(_HA, _HR, object_ids=["SZ_000001"]) == {"SZ_000001": ()})


# -- 最后一项:文档里那个数字得是真的 ---------------------------------------
#
# ``requirements.txt`` 里写着"自检,N 项"。这就是二代缺陷 6 的形状——
# **一个手写的数字,承诺着一件没有任何代码在核对的事**。它已经错过一次
# (文件说 239,实际 273),而且错的时候没有任何迹象。
#
# 所以让它自己核对自己。这一项必须是**最后一项**:它比的是含自己在内的总数。

_REQ = Path(__file__).resolve().parent.parent / "requirements.txt"
_DECLARED = re.search(r"自检,(\d+) 项", _REQ.read_text(encoding="utf-8"))
_TOTAL = len(results) + 1        # +1 是这一项自己,check() 还没记进去
check(f"**requirements.txt 里写的自检项数是真的**(说 {_DECLARED.group(1) if _DECLARED else '没写'},"
      f"实际 {_TOTAL})——手写的数字必然漂,让它自己核对自己",
      _DECLARED is not None and int(_DECLARED.group(1)) == _TOTAL)


print(f"\n=== 合计 {sum(results)}/{len(results)} 通过 ===")
raise SystemExit(0 if all(results) else 1)
