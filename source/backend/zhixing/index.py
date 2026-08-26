"""索引 —— 从归档算出来的查询加速层,**可以整个删掉重建**。

## 它不是第二个事实来源

`archive.py` 是事实。这一层只做一件事:把归档里已经有的东西抄进表里,
好让「按月翻页」「按标的筛」「统计 token」不用把一千份 JSON 全读一遍。

所以有一条铁律:

    **这里的每一列,都必须能从归档重新算出来。**

判断方法就是 `rebuild()`:清空所有表,从归档目录重新灌一遍,结果必须
和原来一模一样。哪一列做不到,那一列就不该在这儿——它是数据,不是索引,
该回归档里去。

二代把执行结果只写进数据库、不进归档,于是数据库既是索引又是事实,
`DROP TABLE` 就丢数据,谁也不敢重建,表结构错了也只能将就。

## 三张表

    runs        一轮一行。翻页、按月筛、成本汇总。
    judgments   一轮 × 一个标的一行。按标的看历史判断。
    executions  一条指令一行。查"哪些单被拦了、为什么拦"的条数。

**拦截原因的正文不进索引**,只存条数。要看原因去归档——索引存了正文,
就等于在索引里放了一份只此一家的事实。

## 为什么带方言参数

生产用 PostgreSQL。但自检要能在没有数据库的机器上跑,所以 SQL 写成
两种方言都认的形状,自检走 `sqlite3`。差异只有三处,全在 `Dialect` 里:
占位符、JSON 列类型、时间列类型。**其余 SQL 一个字不分叉**——分叉了
就等于自检测的不是生产跑的那套。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from . import archive

logger = logging.getLogger("zhixing.index")


class Connection(Protocol):  # pragma: no cover - 仅用于类型
    """PEP 249 连接。`sqlite3.Connection` 和 `psycopg.Connection` 都满足。"""

    def cursor(self) -> Any: ...
    def commit(self) -> None: ...


@dataclass(frozen=True)
class Dialect:
    """两种数据库的**全部**差异。列在这里就是为了它短——一长就说明抽漏了。"""

    name: str
    placeholder: str      # 参数占位符
    json_type: str        # 放 JSON 的列类型
    timestamp_type: str   # 放时间戳的列类型


SQLITE = Dialect("sqlite", "?", "TEXT", "TEXT")
POSTGRES = Dialect("postgresql", "%s", "JSONB", "TIMESTAMPTZ")


# ---------------------------------------------------------------------------
#  表结构
# ---------------------------------------------------------------------------

#: 每张表的列。**顺序有意义**:插入语句按它生成,改顺序要同时改取值函数。
_RUN_COLUMNS = (
    "strategy_id", "system_name", "app_version", "生成时间",
    "总体判断", "context_digest", "model", "llm_provider",
    "判断条数", "指令条数", "拦截条数",
    "input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens",
    "archive_month",
)
_JUDGMENT_COLUMNS = ("strategy_id", "object_id", "名称", "操作", "置信度", "生成时间")
_EXECUTION_COLUMNS = (
    "instruction_code", "strategy_id", "action", "market", "symbol",
    "qty", "limit_price", "状态", "拦截原因条数", "生成时间",
)

TABLES = ("executions", "judgments", "runs")   # 删除顺序:先子后父


def ddl(dialect: Dialect) -> tuple[str, ...]:
    """建表语句。`IF NOT EXISTS` 让重复调用无害——重建流程会先 DROP。

    中文列名一律加双引号。不加也大概率能跑,但两种方言对未加引号标识符的
    折叠规则不一样,而我**只在 sqlite 上实测过**。加引号让这个问题不存在,
    比赌它无所谓便宜。
    """
    ts = dialect.timestamp_type
    return (
        f"""
        CREATE TABLE IF NOT EXISTS runs (
            strategy_id      TEXT PRIMARY KEY,
            system_name      TEXT NOT NULL,
            app_version      TEXT NOT NULL,
            "生成时间"        {ts} NOT NULL,
            "总体判断"        TEXT,
            context_digest   TEXT,
            model            TEXT,
            llm_provider     TEXT,
            "判断条数"        INTEGER NOT NULL,
            "指令条数"        INTEGER NOT NULL,
            "拦截条数"        INTEGER NOT NULL,
            input_tokens     INTEGER NOT NULL,
            output_tokens    INTEGER NOT NULL,
            reasoning_tokens INTEGER NOT NULL,
            cached_tokens    INTEGER NOT NULL,
            archive_month    TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS judgments (
            strategy_id TEXT NOT NULL,
            object_id   TEXT NOT NULL,
            "名称"       TEXT,
            "操作"       TEXT,
            "置信度"     REAL,
            "生成时间"   TEXT NOT NULL,
            PRIMARY KEY (strategy_id, object_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS executions (
            instruction_code TEXT PRIMARY KEY,
            strategy_id      TEXT NOT NULL,
            action           TEXT,
            market           TEXT,
            symbol           TEXT,
            qty              INTEGER,
            limit_price      REAL,
            "状态"            TEXT,
            "拦截原因条数"     INTEGER NOT NULL,
            "生成时间"        TEXT NOT NULL
        )
        """,
        # 三个查询模式各一个索引:按月翻页 / 按标的看历史 / 只看被拦的
        'CREATE INDEX IF NOT EXISTS runs_by_month ON runs (archive_month, "生成时间")',
        'CREATE INDEX IF NOT EXISTS judgments_by_object ON judgments (object_id, "生成时间")',
        'CREATE INDEX IF NOT EXISTS executions_by_status ON executions ("状态", "生成时间")',
    )


def create_schema(conn: Connection, *, dialect: Dialect = POSTGRES) -> None:
    cur = conn.cursor()
    for statement in ddl(dialect):
        cur.execute(statement)
    conn.commit()


def drop_schema(conn: Connection, *, dialect: Dialect = POSTGRES) -> None:
    """清空索引。**这个操作永远是安全的**——事实在归档里,重建即可。

    这句话是这一层的全部意义。哪天它不再成立,说明有人往索引里塞了
    归档没有的东西,那是要修的 bug,不是要保护的数据。
    """
    cur = conn.cursor()
    for table in TABLES:
        cur.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


# ---------------------------------------------------------------------------
#  从归档取值
# ---------------------------------------------------------------------------


def _usage_total(payload: Mapping[str, Any], key: str) -> int:
    """`model_usage` 是**按标的分列的数组**(契约 1.1),这里求和。

    二代把它当成单个对象,于是界面上只显示了第一个标的的用量。
    求和放在索引层,是因为每次翻页都现算七条相加没道理。
    """
    total = 0
    for row in payload.get("model_usage") or ():
        if isinstance(row, Mapping):
            value = row.get(key)
            if isinstance(value, (int, float)):
                total += int(value)
    return total


def _rejected_count(payload: Mapping[str, Any]) -> int:
    return sum(
        1 for i in payload.get("待执行指令") or ()
        if isinstance(i, Mapping) and i.get("状态") == "rejected"
    )


def run_row(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    instructions = payload.get("待执行指令") or ()
    return (
        payload.get("strategy_id"),
        payload.get("system_name"),
        payload.get("app_version"),
        payload.get("生成时间"),
        payload.get("总体判断"),
        payload.get("context_digest"),
        payload.get("model"),
        payload.get("llm_provider"),
        len(payload.get("交易对象判断") or ()),
        len(instructions),
        _rejected_count(payload),
        _usage_total(payload, "input_tokens"),
        _usage_total(payload, "output_tokens"),
        _usage_total(payload, "reasoning_tokens"),
        _usage_total(payload, "cached_tokens"),
        archive.month_of(payload),
    )


def judgment_rows(payload: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    stamp = payload.get("生成时间")
    strategy_id = payload.get("strategy_id")
    return [
        (strategy_id, j.get("object_id"), j.get("名称"), j.get("操作"),
         j.get("置信度"), stamp)
        for j in payload.get("交易对象判断") or ()
        if isinstance(j, Mapping)
    ]


def execution_rows(payload: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    stamp = payload.get("生成时间")
    strategy_id = payload.get("strategy_id")
    return [
        (i.get("instruction_code"), strategy_id, i.get("action"), i.get("market"),
         i.get("symbol"), i.get("qty"), i.get("limit_price"), i.get("状态"),
         len(i.get("拦截原因") or ()), stamp)
        for i in payload.get("待执行指令") or ()
        if isinstance(i, Mapping)
    ]


# ---------------------------------------------------------------------------
#  写索引
# ---------------------------------------------------------------------------


def _insert(cur: Any, table: str, columns: Sequence[str],
            rows: Iterable[tuple[Any, ...]], dialect: Dialect) -> int:
    rows = list(rows)
    if not rows:
        return 0
    marks = ", ".join([dialect.placeholder] * len(columns))
    names = ", ".join(f'"{c}"' for c in columns)
    # 幂等:同一份归档灌两次结果一样。重建流程靠它容错,
    # 增量索引靠它对付"归档写完了、索引写一半崩了"的重跑。
    sql = f"INSERT INTO {table} ({names}) VALUES ({marks}) " \
          f"ON CONFLICT DO NOTHING"
    cur.executemany(sql, rows)
    return len(rows)


def index_run(conn: Connection, payload: Mapping[str, Any], *,
              dialect: Dialect = POSTGRES) -> None:
    """把一份归档灌进索引。已存在则跳过(不更新)。

    不更新是故意的:归档只追加、不修改,同一个 `strategy_id` 的内容
    不会变。如果这里需要 UPDATE,说明归档被改过——那是上游的事故,
    不该由索引层悄悄抹平。
    """
    cur = conn.cursor()
    _insert(cur, "runs", _RUN_COLUMNS, [run_row(payload)], dialect)
    _insert(cur, "judgments", _JUDGMENT_COLUMNS, judgment_rows(payload), dialect)
    _insert(cur, "executions", _EXECUTION_COLUMNS, execution_rows(payload), dialect)
    conn.commit()


@dataclass(frozen=True)
class RebuildReport:
    """重建结果。`failed` 里是读不动的归档——**不中断整轮重建**。

    一份坏归档不该让整个索引建不起来。报出来,人去看那一份,
    其余一千份该进的照进。
    """

    scanned: int
    indexed: int
    judgments: int
    executions: int
    failed: tuple[tuple[str, str], ...]   # (文件名, 原因)

    @property
    def ok(self) -> bool:
        return not self.failed


def rebuild(conn: Connection, *, root: Path,
            dialect: Dialect = POSTGRES) -> RebuildReport:
    """清空索引,从归档目录整个重灌。

    **这个命令必须随时能跑,而且跑完什么都不少。** 它是"索引可弃"这条
    规矩的可执行证明——不是文档里的一句承诺,是一条随时能验的命令。
    """
    drop_schema(conn, dialect=dialect)
    create_schema(conn, dialect=dialect)

    scanned = indexed = n_judgments = n_executions = 0
    failed: list[tuple[str, str]] = []

    for path in archive.iter_paths(root):
        scanned += 1
        try:
            payload = archive.read_path(path)
            cur = conn.cursor()
            _insert(cur, "runs", _RUN_COLUMNS, [run_row(payload)], dialect)
            n_judgments += _insert(cur, "judgments", _JUDGMENT_COLUMNS,
                                   judgment_rows(payload), dialect)
            n_executions += _insert(cur, "executions", _EXECUTION_COLUMNS,
                                    execution_rows(payload), dialect)
            indexed += 1
        except Exception as exc:  # noqa: BLE001 —— 一份坏归档不能拖垮整轮重建
            failed.append((path.name, f"{type(exc).__name__}: {exc}"))
            logger.warning("归档 %s 索引失败:%s", path.name, exc)

    conn.commit()
    logger.info("索引重建完成:扫描 %d,入库 %d,判断 %d,指令 %d,失败 %d",
                scanned, indexed, n_judgments, n_executions, len(failed))
    return RebuildReport(scanned, indexed, n_judgments, n_executions, tuple(failed))


# ---------------------------------------------------------------------------
#  读索引
# ---------------------------------------------------------------------------


def list_runs(conn: Connection, *, root: Path, month: str | None = None,
              limit: int | None = None, system_name: str | None = None,
              dialect: Dialect = POSTGRES) -> list[dict[str, Any]]:
    """`/api/runs` 的取数。**索引只用来定位,内容一律回归档取。**

    列表项本身可以从索引的列拼出来,但那样就有了两份"列表项该长什么样"
    的定义,迟早分头漂——v0.4 那次 43 MB 的事故就是定义分了家。
    这里让索引只回答"哪几轮、什么顺序",内容走 `archive.summarize()`。

    代价是每页要读 N 个文件。一页几十份、每份去掉 `context` 之后几十 KB,
    这个代价买的是"列表项的形状只有一处定义",值。
    """
    clauses, params = [], []
    if month:
        clauses.append(f"archive_month = {dialect.placeholder}")
        params.append(month)
    if system_name:
        clauses.append(f"system_name = {dialect.placeholder}")
        params.append(system_name)

    sql = "SELECT strategy_id, archive_month FROM runs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += ' ORDER BY "生成时间" DESC'
    if limit is not None:
        sql += f" LIMIT {dialect.placeholder}"
        params.append(int(limit))

    cur = conn.cursor()
    cur.execute(sql, tuple(params))

    summaries: list[dict[str, Any]] = []
    for strategy_id, archive_month in cur.fetchall():
        path = Path(root) / archive_month / f"{strategy_id}.json"
        summaries.append(archive.summarize(archive.read_path(path)))
    return summaries


def usage_by_day(conn: Connection, *, dialect: Dialect = POSTGRES) -> list[dict[str, Any]]:
    """`/api/usage?group_by=day`。缓存命中率单独列出来——

    它是**看不见**的指标:不报错、不变慢到你察觉,只是账单变贵。
    二代 7 次调用全部只命中 1152 token(系统提示词长度),跑了几个月没人发现。
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT substr("生成时间", 1, 10) AS 日期,
               COUNT(*),
               SUM(input_tokens), SUM(output_tokens),
               SUM(reasoning_tokens), SUM(cached_tokens)
        FROM runs
        GROUP BY substr("生成时间", 1, 10)
        ORDER BY 1 DESC
        """
    )
    rows = []
    for 日期, 轮数, inp, out, reasoning, cached in cur.fetchall():
        inp = inp or 0
        rows.append({
            "日期": 日期,
            "轮数": 轮数,
            "input_tokens": inp,
            "output_tokens": out or 0,
            "reasoning_tokens": reasoning or 0,
            "cached_tokens": cached or 0,
            "缓存命中率": round((cached or 0) / inp, 4) if inp else 0.0,
        })
    return rows


__all__ = [
    "Dialect", "SQLITE", "POSTGRES", "RebuildReport",
    "create_schema", "drop_schema", "index_run", "rebuild",
    "list_runs", "usage_by_day", "ddl",
]
