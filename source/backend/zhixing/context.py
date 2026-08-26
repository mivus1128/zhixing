"""上下文构建 —— 一轮里 N 个标的的模型输入,共享部分必须逐字节相同。

## 为什么这件事值得单独一层

二代每轮对 7 个标的各调一次模型,7 次输入里约 5 万 token 是完全一样的
(输出要求、行情数据、账户流水)。按说前缀缓存能把这部分吃掉,实测却是:

    object_id    prompt   cache命中   cache未命中
    SH_510300     56354      1152       55202
    SH_601939     53933      1152       52781
    ...           (7 个全是 1152)

**7 次全部只命中 1152**,而 1152 恰好是系统提示词的长度——也就是说
一进入上下文正文,缓存立刻断掉。原因有两个,都在 `build_strategy_context`:

1. `生成时间` 是返回字典的**第一个键**,而且每个标的各调一次
   `now_iso()`,7 个值互不相同。前缀缓存是从第 1 个 token 起逐字节比对、
   **遇到第一个不同就整段作废**的,断在第一个字段,后面 5 万 token
   一字不差也用不上。
2. 7 个请求 `asyncio.gather` 同时发出。缓存要等第一个请求处理完才写入,
   同时发谁也命中不了谁。

代价:每轮约 39 万未命中 token,一天 6 轮约 234 万。

## 这一层的三条保证

一、**顺序固定**。段落顺序写死在 `_LAYOUT` 里,共享的全部排在前面,
   `生成时间` 排到最后。挪一个字段就要改这个常量,改动会进 diff。

二、**共享段只渲染一次**。`SharedBlock.rendered` 是一个字符串对象,
   N 个标的的提示词都由它拼出来。所以"前缀逐字节相同"**不是靠自觉,
   是拼接的必然结果**——想让它不同得先绕过类型。

三、**发送顺序有明确的表达**。`dispatch_plan()` 把一轮拆成
   「先跑一个,再并发剩下的」,而不是让调度器自己记得别一把 gather。

## 这件事不改变模型看到的内容

N 次调用依然各自独立、互不可见,每次的 `交易对象数据` 里只有它自己那一个标的。
前缀缓存是服务端对相同 token 前缀的计算复用,不是把请求合并。

唯一真实的差异是 `生成时间` 从最前挪到了最后——token 序列变了,
所以输出理论上可能有出入。挪到末尾而非删掉,是因为末尾位置通常更受重视,
不存在"被忽略"的问题。这一点在启用前应当由对比视图验证,不要口头保证。
"""

from __future__ import annotations

import hashlib
import json
import logging
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

logger = logging.getLogger("zhixing.context")


#: 段落顺序。**共享的在前,因标的而异的在后,时间戳垫底。**
#: 这个元组就是"缓存能不能命中"的全部秘密,不要随手调整。
_LAYOUT_SHARED = ("输出要求", "读取范围", "市场数据列表", "账户交易流水表")
_LAYOUT_TAIL = ("交易对象数据", "生成时间")

#: 缓存边界:`_LAYOUT_SHARED` 渲染完为止的内容,N 次请求逐字节相同。
CACHE_BOUNDARY_AFTER = _LAYOUT_SHARED[-1]


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _render_field(key: str, value: Any) -> str:
    """渲染成 `  "键": <值>`,值内部多行时缩进对齐。

    序列化参数固定:`ensure_ascii=False`(中文不转义,省 token)、
    `sort_keys=False`(保留写入顺序,排序会打乱 _LAYOUT 的用心)。
    """
    dumped = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)
    if "\n" in dumped:
        dumped = textwrap.indent(dumped, "  ").lstrip()
    return f'  {json.dumps(key, ensure_ascii=False)}: {dumped}'


# ---------------------------------------------------------------------------
#  共享段
# ---------------------------------------------------------------------------

_SHARED_TOKEN = object()


@dataclass(frozen=True)
class SharedBlock:
    """一轮里所有标的共用的那段,**已经渲染成字符串**。

    只能由 `build_shared()` 构造。直接 new 一个会在 `__post_init__` 抛异常——
    理由和 `guards.ValidatedOrder` 一样:如果谁能在别处拼一段"看起来一样"的
    文本塞进来,这一层的保证就退化成了口头约定。
    """

    #: 渲染结果,以 `{\n` 开头,以最后一个共享字段的 `,\n` 结尾。
    #: 拼接时直接接 `_LAYOUT_TAIL` 的内容即可得到完整 JSON。
    rendered: str
    #: 共享段自身的哈希。同一轮内 N 个标的的这个值必须相同,可用来自查。
    digest: str
    _token: object = field(repr=False, default=None)

    def __post_init__(self) -> None:
        if self._token is not _SHARED_TOKEN:
            raise RuntimeError(
                "SharedBlock 只能由 context.build_shared() 构造。"
                "手工构造意味着共享段可能不是同一个字符串对象,"
                "前缀逐字节相同的保证就没有了。"
            )

    def __len__(self) -> int:
        return len(self.rendered)


def build_shared(
    *,
    输出要求: Any,
    读取范围: Any,
    市场数据列表: Any,
    账户交易流水表: Any,
) -> SharedBlock:
    """构建一轮的共享段。**一轮只调一次。**

    参数全是关键字,而且一个都不能省——不是为了好看,是为了让"漏了一段"
    在调用点就报错,而不是悄悄产生一个短一截的前缀。

    二代在这里犯的错是把整个构建函数放进 `_generate_one()`,于是每个标的
    重新读一遍磁盘、重新渲染一遍。内容通常相同,但采集进程恰好在这几秒里
    写了新文件时就会不同——一个窗口很小、至今没暴露、但确实存在的竞态。
    """
    values = {
        "输出要求": 输出要求,
        "读取范围": 读取范围,
        "市场数据列表": 市场数据列表,
        "账户交易流水表": 账户交易流水表,
    }

    parts = ["{\n"]
    for key in _LAYOUT_SHARED:
        parts.append(_render_field(key, values[key]))
        parts.append(",\n")
    rendered = "".join(parts)

    return SharedBlock(rendered=rendered, digest=_digest(rendered), _token=_SHARED_TOKEN)


# ---------------------------------------------------------------------------
#  单个标的的提示词
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectPrompt:
    """喂给模型的一份完整上下文。对应一个标的、一次调用。"""

    object_id: str
    text: str
    #: 共享段的哈希。一轮之内所有 ObjectPrompt 的这个值必须一致。
    shared_digest: str
    #: 整份上下文的哈希。归档进 `context_digest`,对比视图靠它配对两代系统。
    context_digest: str

    def __len__(self) -> int:
        return len(self.text)


def build_round(
    shared: SharedBlock,
    per_object: Mapping[str, Any],
    *,
    generated_at: datetime,
) -> tuple[ObjectPrompt, ...]:
    """把共享段和每个标的的数据拼成 N 份完整上下文。

    :param per_object: ``{object_id: 该标的的数据}``。**每份里只有它自己**,
        N 次调用互不可见——这一点和二代一致,不要合并成一次调用。
        合并会让模型判断标的 B 时看见自己刚才对 A 的判断,产生锚定。
    :param generated_at: **整轮一个时间戳**,由调用方传入。
        故意不在函数里取当前时间:二代就是因为每个标的各取一次
        `now_iso()`,7 个值互不相同,把缓存从第一个字段就打断了。

    返回顺序与 `per_object` 的迭代顺序一致。
    """
    if not isinstance(shared, SharedBlock):  # pragma: no cover - 防呆
        raise TypeError("shared 必须是 build_shared() 的产物")

    stamp = generated_at.isoformat()
    prompts: list[ObjectPrompt] = []

    for object_id, payload in per_object.items():
        tail = (
            _render_field("交易对象数据", payload)
            + ",\n"
            + _render_field("生成时间", stamp)
            + "\n}"
        )
        text = shared.rendered + tail
        prompts.append(
            ObjectPrompt(
                object_id=object_id,
                text=text,
                shared_digest=shared.digest,
                context_digest=_digest(text),
            )
        )

    report = prefix_report(prompts)
    logger.info(
        "本轮构建 %d 份上下文,共享前缀 %d 字符(约占 %.1f%%)",
        len(prompts),
        report.common_prefix_chars,
        report.shared_ratio * 100,
    )
    return tuple(prompts)


# ---------------------------------------------------------------------------
#  自查:前缀到底有没有对齐
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrefixReport:
    """一轮上下文的前缀对齐情况。

    这不是装饰。缓存命中率是**看不见**的——它不报错、不变慢到你察觉,
    只是账单变贵。所以要有一个能在自检里断言的数字。
    """

    count: int
    common_prefix_chars: int
    shortest_chars: int
    longest_chars: int
    shared_digests: tuple[str, ...]

    @property
    def shared_ratio(self) -> float:
        """共享前缀占最短那份的比例。1.0 以下越接近 1 越好。"""
        if self.shortest_chars == 0:
            return 0.0
        return self.common_prefix_chars / self.shortest_chars

    @property
    def digests_agree(self) -> bool:
        """所有上下文是否出自同一个共享段。False 就是出事了。"""
        return len(set(self.shared_digests)) <= 1


def common_prefix_chars(texts: Sequence[str]) -> int:
    """N 个字符串的公共前缀长度。缓存能复用多少,取决于它。"""
    if not texts:
        return 0
    if len(texts) == 1:
        return len(texts[0])

    shortest = min(texts, key=len)
    for i, ch in enumerate(shortest):
        if any(t[i] != ch for t in texts):
            return i
    return len(shortest)


def prefix_report(prompts: Sequence[ObjectPrompt]) -> PrefixReport:
    """算一轮的前缀对齐报告。给自检和 `/api/usage` 用。"""
    texts = [p.text for p in prompts]
    lengths = [len(t) for t in texts] or [0]
    return PrefixReport(
        count=len(prompts),
        common_prefix_chars=common_prefix_chars(texts),
        shortest_chars=min(lengths),
        longest_chars=max(lengths),
        shared_digests=tuple(p.shared_digest for p in prompts),
    )


# ---------------------------------------------------------------------------
#  发送顺序
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchPlan:
    """一轮的发送计划:先跑 `warmup`,**等它回来**再并发 `rest`。

    顺序不是优化建议,是缓存生效的前提。缓存在第一个请求**处理完之后**
    才写入,N 个一起发出去的话谁都赶在写入之前,全部未命中——
    二代的 `asyncio.gather` 就是这么把缓存作废掉的。

    代价是一轮耗时约翻倍(从"1 个请求的时间"变成"2 个")。
    对一天跑 6 轮的系统,这个代价可以忽略。
    """

    warmup: ObjectPrompt | None
    rest: tuple[ObjectPrompt, ...]

    @property
    def total(self) -> int:
        return (1 if self.warmup else 0) + len(self.rest)


def dispatch_plan(prompts: Sequence[ObjectPrompt]) -> DispatchPlan:
    """把一轮拆成「预热一个 + 并发其余」。

    `warmup` **不是额外多出来的请求**,它就是这批里的第一个,只是让它先跑。
    """
    if not prompts:
        return DispatchPlan(warmup=None, rest=())
    return DispatchPlan(warmup=prompts[0], rest=tuple(prompts[1:]))
