"""历史判断(算) —— 「我上次对这个标的说过什么」。

## 为什么要有

模型每一轮都是从零开始的:同一个标的,今天上午说 hold,下午还是从头看一遍
数据,不知道上午说过什么、为什么说、当时打算什么情况下改口。二代把最近
5 个交易日的切片喂进去正是为了这个,三代一开始漏掉了。

漏掉的后果不是"少一点参考",是**判断会在相邻两轮之间无理由翻烧饼**——
而"改判条件"这个字段本来就是为了让上一轮的判断可被检验,没有历史,
那个字段写了也没人读。

## 窗口是 5 个交易日,和二代一致

不是自然日。周末和假期不该把窗口空掉。

## 两个来源,自己的优先

三代是新系统,自己的归档只有一天。同一台机器上二代跑了几个月、同一批标的,
那份记录先垫上,存在 ``<运行目录>/history_seed.json``。

**同一个交易日两边都有时,用三代自己的。** 二代是另一个模型、另一套提示词,
它的判断可以当参考,但不能盖过本系统自己说过的话——否则"我上次说过什么"
这句话就不成立了。

随着三代自己攒够 5 个交易日,种子里的日期会被挤出窗口,自然失效。
不需要谁去删它,也不该去删:它是那几天真实发生过的事。

## 每个交易日留哪几轮

当天**最后一轮**,加上当天**出过指令的每一轮**。

一天六轮,相邻两轮的判断通常一字不差,全留只是让上下文变长。而出过指令的
那几轮不能丢——"我那天动过手"正是历史里最该被看见的部分。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import archive

logger = logging.getLogger(__name__)

#: 窗口长度,单位是**交易日**,不是自然日。和二代的 ``days_limit`` 对齐。
HISTORY_DAYS = 5

#: 二代垫底数据的文件名,放在运行目录下。
SEED_NAME = "history_seed.json"


def _entry(条: Mapping[str, Any], *, 交易日: str, 时间: str,
           指令: list[Any]) -> dict[str, Any]:
    """一条历史判断。**只留能帮下一次判断的字段。**

    不留 ``风险``:它是"这个判断可能错在哪",对当时有用,过了几天再看,
    它既不是事实也不是结论,只占地方。
    """
    记: dict[str, Any] = {
        "交易日": 交易日,
        "时间": 时间,
        "操作": 条.get("操作"),
        "置信度": 条.get("置信度"),
        "理由": list(条.get("理由") or [])[:2],
    }
    改判 = str(条.get("改判条件") or "").strip()
    if 改判:
        记["改判条件"] = 改判
    if 指令:
        记["指令"] = 指令
    return 记


def _指令归谁(指令: Mapping[str, Any]) -> str:
    """一条 ``待执行指令`` 属于哪个标的。

    **归档里的指令没有 ``object_id``。** 它记的是 ``market`` + ``symbol``,
    因为指令是要发给券商的,券商认代码不认我们的内部编号。标的的 ``object_id``
    恰好就是这两段拼起来,所以拼回去即可。

    这里曾经直接读 ``指令["object_id"]``:字段不存在,``.get`` 返回 None,
    没有任何报错,只是每一条指令都匹配不上任何标的,于是"我那天动过手"这件事
    从历史里整个消失。**对不上的字段名不会报错,只会让东西悄悄消失。**
    """
    oid = str(指令.get("object_id") or "").strip()
    if oid:
        return oid
    市场 = str(指令.get("market") or "").strip()
    代码 = str(指令.get("symbol") or "").strip()
    return f"{市场}_{代码}" if 市场 and 代码 else ""


def _own(root: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """读三代自己的归档,整理成 ``{标的: {交易日: [判断...]}}``。

    整份扫。一天六轮、一份归档几十 KB,几个月也就几千份;为了省这点读盘
    去维护一个索引,是拿"会和事实分叉的东西"换"不值一提的时间"。
    """
    按日: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for payload in archive.iter_runs(root):
        时间 = str(payload.get("生成时间") or "")
        日 = 时间[:10]
        if len(日) != 10:
            continue
        按日.setdefault(日, []).append((时间, payload))

    出: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for 日, 轮次 in 按日.items():
        轮次.sort(key=lambda x: x[0])
        最后 = 轮次[-1][1]
        for 时间, payload in 轮次:
            指令表 = list(payload.get("待执行指令") or [])
            是最后 = payload is 最后
            if not 是最后 and not 指令表:
                continue
            有指令 = {_指令归谁(i) for i in 指令表} - {""}
            for 条 in payload.get("交易对象判断") or []:
                oid = str(条.get("object_id") or "")
                if not oid:
                    continue
                if not 是最后 and oid not in 有指令:
                    continue
                这条 = [i for i in 指令表 if _指令归谁(i) == oid]
                出.setdefault(oid, {}).setdefault(日, []).append(
                    _entry(条, 交易日=日, 时间=时间[:19], 指令=这条)
                )
    return 出


def _seed(runtime_dir: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """读二代垫底数据。**没有、坏了、格式不对,一律当成没有。**

    这是"锦上添花"的输入:它缺席时系统照常跑,只是模型少一点参考。
    为它抛异常会让一份坏掉的垫底文件把整轮判断拖垮——那个代价和它的
    重要性完全不匹配。
    """
    path = Path(runtime_dir) / SEED_NAME
    if not path.exists():
        return {}
    try:
        种子 = json.loads(path.read_text(encoding="utf-8"))
        按标的 = 种子["按标的"]
        if not isinstance(按标的, dict):
            raise TypeError("按标的 不是对象")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("垫底历史读不出来,本轮不带历史参考:%s(%s)", path.name, exc)
        return {}

    出: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for oid, 条目 in 按标的.items():
        if not isinstance(条目, list):
            continue
        for 记 in 条目:
            if not isinstance(记, Mapping):
                continue
            日 = str(记.get("交易日") or "")
            if len(日) != 10:
                continue
            带 = dict(记)
            带["来源"] = "二代"
            出.setdefault(str(oid), {}).setdefault(日, []).append(带)
    return 出


def recent(
    archive_root: Path,
    runtime_dir: Path,
    *,
    object_ids: Iterable[str],
    days: int = HISTORY_DAYS,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """给每个标的取最近 ``days`` 个**有记录的交易日**的判断。

    :param object_ids: 本轮要问的标的。不在里面的一律不算——省下的不是
        内存,是"上下文里出现了本轮没问的标的"这种会让人看半天的怪事。

    返回值按时间正序(老的在前),空元组表示这个标的还没有历史。
    """
    要的 = {str(o) for o in object_ids}
    自己 = _own(Path(archive_root))
    垫底 = _seed(Path(runtime_dir))

    出: dict[str, tuple[dict[str, Any], ...]] = {}
    for oid in 要的:
        我的 = 自己.get(oid, {})
        他的 = 垫底.get(oid, {})
        日期 = sorted(set(我的) | set(他的), reverse=True)[:days]
        条目: list[dict[str, Any]] = []
        for 日 in sorted(日期):
            # 同一天两边都有,用三代自己的。见模块文档。
            条目.extend(我的.get(日) or 他的.get(日) or ())
        出[oid] = tuple(条目)
    return 出


__all__ = ["HISTORY_DAYS", "SEED_NAME", "recent"]
