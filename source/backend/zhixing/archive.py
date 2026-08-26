"""归档 —— 全系统的事实来源。

## 一句话规矩

    归档 JSON  =  事实。只追加,不修改,不删除。
    数据库     =  从归档算出来的索引,**可以整个删掉重建**。

判断一张表该不该存在,只有一条标准:**删掉它会不会丢数据**。会丢,
它就不是索引,是第二个事实来源,不许有。`index.rebuild()` 存在的
唯一意义就是随时能证明这条规矩没被破坏。

二代的教训:策略结果同时写进文件和一张表,两边都能改,而且改法不一样。
出事时没人知道该信哪个——这不是"数据不一致"的小毛病,是**系统没有事实**。

## 这一层拦三件事

一、**字段缺失**。归档写进去之后就不改了,少一个字段就是永久少一个。
   所以在写之前查,不是读的时候容错。

二、**覆盖已有归档**。同一个 `strategy_id` 写第二次直接报错。
   "只追加"如果靠自觉,迟早有人为了修一个错字重写一份。

三、**机密混进归档**。归档是要进 Git、进备份、进百度同步盘的东西。
   模型 Key、券商密码、Cookie、完整账号一旦写进去,就等于上传了云端,
   而且**归档不许改**,发现了也删不掉。所以只能在入口挡。
   见 `scan_for_secrets()`。

## 目录结构

    runs/2026-08/20260817-144703.json
         ^^^^^^^ 从 `生成时间` 算,不从 strategy_id 解析

按月分目录是因为契约第二节写了「归档全量保留,按月翻」——7 个标的 ×
一天六轮,一年一千多份,一个目录塞得下但翻不动。月份从时间戳算而不是
从 id 里抠,是因为 id 的格式将来可能变,时间戳不会。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

logger = logging.getLogger("zhixing.archive")


#: 归档必须有的顶层字段。契约 1.1。
#: 少一个就拒写——归档不许改,写进去之后补不上了。
REQUIRED_FIELDS = (
    "strategy_id",
    "system_name",
    "app_version",
    "生成时间",
    "总体判断",
    "风险控制",
    "交易对象判断",
    "待执行指令",
    "model",
    "llm_provider",
    "model_usage",
    "data_window",
    "context_digest",
    "context",
)

#: 列表接口(契约 1.5)唯一排除的字段。**只有这一个。**
#: 前端 `validate-fixtures.mjs` 里那条键集合相等的断言,对应的就是这里。
SUMMARY_EXCLUDED = ("context",)

#: 列表项额外附带的冗余计数,省得前端为了显示数量去数数组。
SUMMARY_COUNTS = {
    "判断条数": "交易对象判断",
    "指令条数": "待执行指令",
}


class ArchiveError(RuntimeError):
    """归档写入被拒。全部拒写路径都用它,调用方不需要分辨子类。"""


# ---------------------------------------------------------------------------
#  机密扫描
# ---------------------------------------------------------------------------

#: 出现即拒的键名(小写比对)。**不做模糊匹配**——`input_tokens` 里也有
#: "token" 两个字,模糊匹配会把正常字段一起毙掉,然后有人为了让它过去
#: 把扫描关掉,那才是真出事。
_FORBIDDEN_KEYS = frozenset({
    "password", "passwd", "pwd", "密码", "交易密码", "资金密码",
    "cookie", "cookies", "set-cookie",
    "api_key", "apikey", "api-key", "secret", "secret_key",
    "access_token", "refresh_token", "session_id", "sessionid",
    "authorization", "auth_token",
    "chromium_profile", "user_data_dir",
})

#: 值里出现即拒的形状。带说明,报错时直接告诉人踩了哪一条。
_FORBIDDEN_VALUES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "疑似 Anthropic API Key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "疑似模型 API Key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥"),
)

#: 这些键的值必须是**脱敏后**的,即含 `*`。契约 1.3:
#: 「`账户标识` 永远是脱敏后的,后端不下发完整账号」。
#:
#: ⚠️ `账户ID` 是**错的键名**(契约里叫 `账户标识`),之所以也列在这儿:
#: 这道闸的失效方式是静默的——换个键名它就不检查了,而漏检和检查通过
#: 长得一模一样。所以宁可把见过的错名字一并收着,多认一个不会误伤,
#: 少认一个是完整账号明文进归档。
_MUST_BE_MASKED = frozenset({"账户标识", "账户ID", "账号", "资金账号", "account_id"})

#: 脱敏判定:含至少一个掩码字符即可。不校验掩了几位——那是格式偏好,
#: 不是安全边界,管太细只会逼人绕过去。
_MASK_CHARS = ("*", "＊", "x", "X", "·")


@dataclass(frozen=True)
class SecretFinding:
    """一处疑似机密。`path` 是它在归档里的位置,例如 `context.账户.cookie`。"""

    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


def scan_for_secrets(payload: Any, *, _path: str = "") -> tuple[SecretFinding, ...]:
    """递归找机密。**一次找全,不是遇到第一条就返回**。

    取向和 `guards.validate()` 一致:一次把问题报完,人改一遍就能过,
    而不是改一条撞一条。

    这个函数**不猜测、不脱敏、不删字段**,只报告。自动脱敏听起来贴心,
    实际上会让"归档里到底有没有那个东西"变得说不清——被改过的归档
    就不是事实来源了。发现了就拒写,让上游别放进来。
    """
    findings: list[SecretFinding] = []

    if isinstance(payload, Mapping):
        for key, value in payload.items():
            key_text = str(key)
            here = f"{_path}.{key_text}" if _path else key_text
            lowered = key_text.strip().lower()

            if lowered in _FORBIDDEN_KEYS:
                findings.append(SecretFinding(here, f"字段名 `{key_text}` 属于禁止入档的机密"))
                continue  # 值就不看了,报一次够了

            if key_text in _MUST_BE_MASKED:
                # 空值和 None **不是机密**——没有内容就没有东西能泄露。
                #
                # 这一句不是放宽,是修一个会反噬的洞:没配账号时
                # `资金账号` 是空串,原来会报"必须脱敏",于是"这项还没配"
                # 和"账号明文泄露了"在报错里长得一模一样。碰上的人查不明白,
                # 十有八九会把 `资金账号` 从 _MUST_BE_MASKED 里删掉了事——
                # **那才是真的把门拆了。**
                if value is None or (isinstance(value, str) and not value.strip()):
                    continue
                text = str(value)
                if not any(ch in text for ch in _MASK_CHARS):
                    findings.append(
                        SecretFinding(here, f"`{key_text}` 必须脱敏后再入档(契约 1.3)")
                    )
                    continue

            findings.extend(scan_for_secrets(value, _path=here))

    elif isinstance(payload, (list, tuple)):
        for i, item in enumerate(payload):
            findings.extend(scan_for_secrets(item, _path=f"{_path}[{i}]"))

    elif isinstance(payload, str):
        for pattern, reason in _FORBIDDEN_VALUES:
            if pattern.search(payload):
                findings.append(SecretFinding(_path or "<根>", reason))

    return tuple(findings)


# ---------------------------------------------------------------------------
#  路径
# ---------------------------------------------------------------------------


def month_of(payload: Mapping[str, Any]) -> str:
    """归档所属月份,形如 `2026-08`。从 `生成时间` 算,不解析 strategy_id。"""
    raw = payload.get("生成时间")
    if not isinstance(raw, str):
        raise ArchiveError("`生成时间` 缺失或不是字符串,无法决定归档目录")
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m")
    except ValueError as exc:
        raise ArchiveError(f"`生成时间` 不是合法 ISO 时间:{raw!r}") from exc


def path_for(payload: Mapping[str, Any], *, root: Path) -> Path:
    """归档应当落在哪儿。**只算路径,不碰磁盘。**"""
    strategy_id = payload.get("strategy_id")
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise ArchiveError("`strategy_id` 缺失或为空")
    if "/" in strategy_id or "\\" in strategy_id or strategy_id.startswith("."):
        raise ArchiveError(f"`strategy_id` 含路径分隔符或以点开头,不能做文件名:{strategy_id!r}")
    return Path(root) / month_of(payload) / f"{strategy_id}.json"


# ---------------------------------------------------------------------------
#  写
# ---------------------------------------------------------------------------


def validate_payload(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """写之前查一遍。返回全部问题,空元组表示可以写。"""
    problems: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in payload:
            problems.append(f"缺少必填字段 `{field}`")

    for field in ("交易对象判断", "待执行指令", "model_usage"):
        value = payload.get(field)
        if field in payload and not isinstance(value, list):
            problems.append(f"`{field}` 必须是数组,实际是 {type(value).__name__}")

    for count_key in SUMMARY_COUNTS:
        if count_key in payload:
            problems.append(
                f"`{count_key}` 是列表接口现算的冗余计数,不入档"
                "——存下来就会和数组本身对不上"
            )

    for finding in scan_for_secrets(payload):
        problems.append(f"机密不得入档 —— {finding}")

    return tuple(problems)


def write_run(payload: Mapping[str, Any], *, root: Path) -> Path:
    """把一轮归档落盘。返回落盘路径。

    三道闸,按顺序:字段齐 → 没机密 → 不覆盖已有。

    **不覆盖**是硬的:同一个 `strategy_id` 写第二次直接报错,不提供
    `overwrite=True`。想改归档的场景一律是错的——要么补一轮新的,
    要么承认这轮本来就长这样。留个开关,就等于把"只追加"降级成建议。

    落盘用 `临时文件 + os.replace`。中途断电只会留下一个 `.tmp`,
    不会留下半份 JSON——半份 JSON 比没有更糟,它看起来像数据。
    """
    problems = validate_payload(payload)
    if problems:
        raise ArchiveError("归档被拒,共 %d 条:\n  - %s" % (len(problems), "\n  - ".join(problems)))

    target = path_for(payload, root=root)
    if target.exists():
        raise ArchiveError(
            f"归档已存在,不覆盖:{target.name}。"
            "归档只追加——要更正请补一轮新的,不要改旧的。"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(tmp, target)

    logger.info("归档落盘 %s(%d 条判断 / %d 条指令)",
                target.name,
                len(payload.get("交易对象判断") or ()),
                len(payload.get("待执行指令") or ()))
    return target


def write_execution(payload: Mapping[str, Any], *, root: Path) -> Path:
    """追加一条不属于轮次内写入的执行事件,例如人工撤单。

    文件放在 ``_execution/YYYY-MM`` 两级目录下,不会被 ``iter_paths`` 当成
    策略轮次。仍然先过机密扫描,并且按 ``record_id`` 永不覆盖。
    """
    record_id = str(payload.get("record_id") or "").strip()
    attempted_at = str(payload.get("attempted_at") or "").strip()
    problems: list[str] = []
    if not record_id or not re.fullmatch(r"[A-Za-z0-9_-]+", record_id):
        problems.append("`record_id` 缺失或含非法字符")
    try:
        month = datetime.fromisoformat(attempted_at).strftime("%Y-%m")
    except ValueError:
        month = ""
        problems.append("`attempted_at` 不是合法 ISO 时间")
    problems.extend(f"机密不得入档 —— {finding}" for finding in scan_for_secrets(payload))
    if problems:
        raise ArchiveError(
            "执行留痕被拒,共 %d 条:\n  - %s" % (len(problems), "\n  - ".join(problems))
        )

    target = Path(root) / "_execution" / month / f"{record_id}.json"
    if target.exists():
        raise ArchiveError(f"执行留痕已存在,不覆盖:{record_id}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    logger.info("执行留痕落盘 %s", target.name)
    return target


def iter_executions(root: Path) -> Iterator[dict[str, Any]]:
    """按时间正序读取独立执行事件;坏文件跳过并记日志。"""
    base = Path(root) / "_execution"
    if not base.exists():
        return
    for path in sorted(base.glob("*/*.json")):
        try:
            yield read_path(path)
        except ArchiveError as exc:
            logger.error("跳过读不出来的执行留痕 %s:%s", path.name, exc)


# ---------------------------------------------------------------------------
#  读
# ---------------------------------------------------------------------------


def read_path(path: Path) -> dict[str, Any]:
    """读一份归档。解析失败带上文件名——不带的话你在一千份里找不到是哪份。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"归档不是合法 JSON:{path}({exc})") from exc


def iter_paths(root: Path, *, month: str | None = None) -> Iterator[Path]:
    """按时间正序遍历归档路径。`month` 形如 `2026-08`,不给就是全部。

    跳过 `.tmp`(写到一半的残留)。文件名带日期,排序即时序,
    所以这里不需要把每份都读出来再排。
    """
    base = Path(root)
    if not base.exists():
        return
    pattern = f"{month}/*.json" if month else "*/*.json"
    for path in sorted(base.glob(pattern)):
        if path.name.endswith(".json.tmp"):
            continue
        yield path


def iter_runs(root: Path, *, month: str | None = None) -> Iterator[dict[str, Any]]:
    """按时间正序读出归档内容。重建索引用的就是它。"""
    for path in iter_paths(root, month=month):
        yield read_path(path)


def read_run(strategy_id: str, *, root: Path) -> dict[str, Any]:
    """按 id 取一份。找不到抛 `ArchiveError`。

    不知道月份的时候只能扫目录——一年一千多份,glob 一次可以接受;
    真嫌慢的时候走索引拿路径,那正是索引该干的事。
    """
    for path in iter_paths(root):
        if path.stem == strategy_id:
            return read_path(path)
    raise ArchiveError(f"找不到归档:{strategy_id}")


# ---------------------------------------------------------------------------
#  列表项(契约 1.5)
# ---------------------------------------------------------------------------


def summarize(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把完整归档削成列表项。**契约 1.5 只在这一处实现。**

    规则就一句:去掉 `context`,加两个计数,**其余原样**。

    这个函数存在的理由是 v0.4 那次事故:契约里写的是「摘要不含 context
    和交易对象判断」,前端照做,结果判断页只能对每一轮再拉一次完整归档
    ——单次翻页 43 MB、132 个请求。字段清单一旦散落在几个地方(接口一份、
    前端一份、文档一份),它们就必然分头漂。所以后端只留这一个出口,
    前端 `validate-fixtures.mjs` 那条键集合相等的断言是它的对照。

    **不做截断。** `总体判断` 原文返回,要截由前端按自己的版面截——
    后端截了,前端想显示全的时候就没地方要了。
    """
    summary = {k: v for k, v in payload.items() if k not in SUMMARY_EXCLUDED}
    for count_key, source_key in SUMMARY_COUNTS.items():
        summary[count_key] = len(payload.get(source_key) or ())
    return summary
