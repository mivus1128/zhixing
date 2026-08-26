"""轮次驱动 —— 到点跑一轮。**这是系统里唯一会自己动的进程。**

## 和 ``serve.py`` 的分工

    serve.py    只答 HTTP。**它永远不会自己跑一轮。**
    daemon.py   只跑轮次。**它不监听任何端口。**

分成两个进程不是洁癖,是因为它们的失败方式必须分开:

- 界面挂了,轮次照跑(判断不会因为没人看而停);
- 轮次挂了,界面照样打得开——而且**这时候界面才是最要紧的**,
  人要能看见"停摆了、停了多久、上一次失败原因是什么"(契约 1.4)。

把两件事塞进一个进程,第二条就没了:驱动循环里一个没接住的异常会
把 HTTP 一起带走,于是系统停摆的同时,唯一能看出它停摆的东西也没了。

## 为什么是轮询,不是"睡到下一个时点"

``scheduler`` 算得出下一次该几点,照着睡是更省的写法。这里故意不那么做:

1. **配置能在界面上改。** 睡到 14:30 的进程不知道你把时点挪到了 10:30。
   轮询每 20 秒问一次 ``store.schedule()``,改完下一次就生效。
2. **时点有 20 分钟的有效窗口**(``scheduler.window_minutes``),
   20 秒的轮询周期在它面前有六十倍的余量。省下的那点 CPU 买不到什么。
3. 长睡对改系统时间、休眠唤醒、NTP 跳变都很敏感,而这些在服务器上
   都真的会发生。轮询对它们免疫——它只问"现在该不该跑"。

**错过的时点永不补跑**,这条在 ``scheduler.due_slot`` 里,不在这儿。

## 一轮炸了,进程不能死

跑一轮要碰网络、碰浏览器、碰模型中转,没有一样是可靠的。所以驱动循环
**接住一切异常**,记进运行事实(契约 1.4 的三个停摆字段),然后继续。

进程因为一次超时而退出,后果是当天剩下五轮全没了,而 systemd 拉起来
之前没人知道。相比之下,一轮失败只是一轮失败。

## 前置条件不满足时,不跑,但要**说**

模型没配、券商没配的时候,硬跑一轮的结果是:登录失败 + 七次
``CALL_FAILED`` + 一份除了错误什么都没有的归档,而且每一轮都要去撞一次
券商的登录失败计数器。

所以到点之前先查一遍(``preflight``),缺什么就写进 ``最近失败原因``——
界面上看得见,而不是让人对着一个"什么都没发生"的系统猜。

运行:

    python -m zhixing.daemon --archive-root /var/lib/zhixing/archives
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import SYSTEM_NAME, __version__
from . import captcha, collect, llm, runner, runmode, scheduler, state, tradingdays

logger = logging.getLogger("zhixing.daemon")

#: 轮询周期(秒)。理由见模块开头——时点窗口是 20 分钟,这里有六十倍余量。
DEFAULT_POLL_SECONDS = 20.0

#: 前置条件不满足时,多久抱怨一次(秒)。
#:
#: 每 20 秒打一条"模型没配"会把日志刷满,而日志刷满的直接后果是**真正的
#: 错误被淹掉**。半小时一条足够让人发现,又不至于盖住别的。
COMPLAIN_EVERY = 1800.0


# ---------------------------------------------------------------------------
#  算 —— 前置条件
# ---------------------------------------------------------------------------


def preflight(store: state.Store, *, now: datetime | None = None) -> tuple[str, ...]:
    """跑一轮之前必须具备的东西。返回缺了什么,**空元组表示可以跑**。

    **一次报全部**,和 ``guards.validate`` / ``BrokerSettings.missing``
    同一个取向:让人一次看全比试错三轮有用。

    ⚠️ 这里**不查行情能不能取到**。行情是逐个标的容错的(取不到就跳过那个
    标的,见 ``collect``),不是开跑的前置条件;把它列进来会让"某个代码写错了"
    变成"整天不跑",那是不成比例的。
    """
    缺: list[str] = []

    # 时区放在最前面,因为它错的时候**别的每一项看起来都是对的**。
    # 详见 scheduler.clock_zone_problem:时区错了不会报错,只会把六轮
    # 整体推到收盘之后,然后一天天地什么都不产出。
    #
    # 这一条拦得住轮次,是刻意的:时钟差八小时的时候,**不跑比跑对**。
    # 跑起来会拿收盘后的行情产出一份标称盘中的判断,归档就说谎了。
    # ⚠️ ``.astimezone()`` **只在 now 没给的时候调**。对一个已经带时区的
    # datetime 调它是"换算到本机时区",于是传进来的 UTC 会被就地改成本机
    # 时间,检查永远通过——自检当场逮到了这个,写的时候完全没想到。
    现在 = now if now is not None else datetime.now().astimezone()
    zone = scheduler.clock_zone_problem(现在)
    if zone is not None:
        缺.append(zone)

    model_settings = store.model()
    if not model_settings.endpoint.strip():
        缺.append("模型接口地址(设置 → 模型)")
    if not model_settings.name.strip():
        缺.append("模型名称(设置 → 模型)")
    if not model_settings.secret.strip():
        缺.append("模型密钥(设置 → 模型)")

    catalog = store.catalog()
    if not catalog.tradable:
        缺.append("标的清单里一个可交易标的都没有(交易对象页)")

    # 券商没配**不拦**这一轮:没有账户照样能出判断,只是出不了指令
    # (``collect`` 会把它记成 ACCOUNT_UNAVAILABLE 进归档)。判断本身是
    # 这个系统的主要产出,不该被"还没填账号"卡住。
    return tuple(缺)


def build_runner(
    store: state.Store,
    *,
    archive_root: Path,
    source: runner.DataSource,
) -> runner.Runner:
    """按**当前配置**装一个 Runner。

    每轮重装一次,不在启动时装死。模型、中转、密钥都是能在界面上改的,
    装死意味着"改了配置要重启才生效"——而重启这件事没人会想起来做,
    于是界面上显示的配置和实际在用的配置会长期不一致,且**看不出来**。

    重装的代价是几个 dataclass,一天六次,不值一提。
    """
    settings = store.model()
    broker_provider = getattr(source, "execution_broker", None)
    return runner.Runner(
        store=store,
        archive_root=archive_root,
        caller=llm.HttpCaller(credential=llm.Credential(settings.secret)),
        target=settings.to_target(),
        source=source,
        broker_provider=broker_provider if callable(broker_provider) else None,
    )


# ---------------------------------------------------------------------------
#  碰 —— 驱动循环
# ---------------------------------------------------------------------------


def _record_blocked(store: state.Store, reason: str) -> None:
    """把"没能开跑"写进运行事实。**不动连续失败轮数。**

    缺配置不是"失败了一轮"——一轮都没开始。把它算进失败计数会让那个数字
    在没配好的机器上一天涨几千,从此再也说明不了任何问题。
    """
    facts = store.runtime()
    store.save_runtime(replace(facts, 最近失败原因=reason))


class Daemon:
    """驱动循环。``stop()`` 之后 ``run()`` 会在当前这一觉睡完后返回。"""

    def __init__(
        self,
        store: state.Store,
        *,
        archive_root: Path,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        clock: Any = datetime.now,
        sleep: Any = time.sleep,
    ) -> None:
        self.store = store
        self.archive_root = archive_root
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.sleep = sleep
        self._stopping = False
        self._complained_at = 0.0
        # 采集器**跨轮复用**:浏览器会话保持着,省掉每轮一次登录。
        # 每登一次都是一次被券商锁卡的机会,这不是性能优化。
        self.collector = collect.Collector(
            store=store, solver=captcha.solver_from_settings(store.captcha())
        )

    # -- 一次滴答 ---------------------------------------------------------

    def tick(self) -> runner.RoundResult | None:
        """问一次"该不该跑",该跑就跑。**任何异常都不往外抛。**"""
        # API 与 daemon 是两个进程。每次滴答都从共享运行目录恢复开关,
        # 才能保证界面上关闭后,下一轮不会继续沿用 daemon 的旧内存状态。
        try:
            runmode.restore_unattended(self.store.unattended())
        except state.StateError as exc:
            self._complain(f"无人值守开关状态读不出来:{exc}")
            return None

        try:
            缺 = preflight(self.store)
        except state.StateError as exc:
            self._complain(f"配置读不出来:{exc}")
            return None

        if 缺:
            self._complain("还不能开跑,缺:" + "、".join(缺))
            return None

        # 识别器按当前配置重装(验证码密钥会轮换),采集器本身留着。
        self.collector.solver = captcha.solver_from_settings(self.store.captcha())
        run = build_runner(
            self.store, archive_root=self.archive_root, source=self.collector
        )

        try:
            return run.tick()
        except Exception as exc:      # noqa: BLE001 - 见模块开头
            # 到这儿说明连归档都没写成(写成了的话 run_round 内部就消化了)。
            # **整条栈都记下来**:这是"一轮凭空消失"的唯一线索。
            logger.exception("这一轮没跑成")
            try:
                facts = self.store.runtime()
                self.store.save_runtime(replace(
                    facts,
                    连续失败轮数=facts.连续失败轮数 + 1,
                    最近失败原因=f"{exc.__class__.__name__}: {exc}",
                ))
            except OSError as write_exc:
                logger.warning("失败原因没能落盘:%s", write_exc)
            return None

    def _complain(self, message: str) -> None:
        """前置条件不满足。**限频打日志,但每次都更新运行事实。**

        日志限频是为了不淹掉真错误;运行事实不限频,因为界面读的是它,
        而界面上那句话必须一直是当前的——限频会让它停在半小时前的状态。
        """
        try:
            _record_blocked(self.store, message)
        except OSError as exc:
            logger.warning("停跑原因没能落盘:%s", exc)

        now = time.monotonic()
        if now - self._complained_at >= COMPLAIN_EVERY:
            self._complained_at = now
            logger.warning("%s", message)

    # -- 循环 -------------------------------------------------------------

    def run(self) -> int:
        logger.info(
            "%s %s 轮次驱动已启动(归档 %s,运行目录 %s,轮询 %.0f 秒)",
            SYSTEM_NAME, __version__, self.archive_root, self.store.root,
            self.poll_seconds,
        )
        logger.info("数据源:%s", collect.describe_source(self.store))
        logger.info("运行模式:%s", runmode.describe()["运行模式"])

        while not self._stopping:
            self.tick()
            # 睡在最后:停止信号来的时候,当前这一觉睡完就退,不多跑一轮。
            if not self._stopping:
                self.sleep(self.poll_seconds)

        logger.info("正在停止,关掉浏览器会话")
        self.collector.close()
        return 0

    def stop(self, *_: Any) -> None:
        """收到停止信号。**幂等**,可以从信号处理器里调。"""
        if not self._stopping:
            logger.info("收到停止信号,当前这一轮跑完就退")
        self._stopping = True


# ---------------------------------------------------------------------------
#  入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zhixing.daemon", description="知行三代轮次驱动"
    )
    parser.add_argument(
        "--archive-root", required=True,
        help="归档根目录。**必须在仓库外**——仓库在同步盘里。",
    )
    parser.add_argument(
        "--runtime-dir", default=None,
        help=f"运行状态目录,默认取环境变量 {state.RUNTIME_DIR_ENV} 或 ~/.zhixing",
    )
    parser.add_argument(
        "--poll", type=float, default=DEFAULT_POLL_SECONDS,
        help=f"轮询周期(秒),默认 {DEFAULT_POLL_SECONDS:.0f}",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="只问一次该不该跑,跑完(或判定不该跑)就退出。给 cron 和手动验证用。",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    try:
        store = state.Store(args.runtime_dir)
    except state.StateError as exc:
        print(f"启动失败:{exc}", file=sys.stderr)
        return 2

    # 交易日历:**这里比 serve.py 严**。
    #
    # serve.py 只警告,因为它还要负责把归档翻给人看,日历过期不该让"看历史"
    # 也用不了。本进程只干一件事——按交易日驱动轮次;日历缺失时调度器
    # 无法回答当天是否该跑。所以这里直接不启动,而不是空转一整年。
    #
    # ⚠️ 但要严在**对的年份**上。这行原来写的是
    # ``assert_covers(today.year, today.year + 1)``,2026-08-20 上线时它让
    # 进程反复重启,理由是"缺少 2027 年的交易日历"——而次年日历要到当年
    # 11 月才发布,八月份谁都没有。那道门槛拦下的是系统本身,不是风险。
    # 现在用 ``required_years`` 算:当年是硬要求,次年只在年底 45 天内才是。
    today = date.today()
    try:
        tradingdays.assert_covers(*tradingdays.required_years(today))
    except tradingdays.CalendarError as exc:
        print(f"启动失败:交易日历不完整 —— {exc}", file=sys.stderr)
        print("假期安排一年一发。补上 tradingdays 里的日历再启动。", file=sys.stderr)
        return 2
    if not tradingdays.covers(today.year + 1):
        # 只提醒。到年底 45 天内它会自动变成上面那道硬门槛。
        logger.warning(
            "还没有 %d 年的交易日历。假期安排一般当年 11 月发布,"
            "发布后要补进 tradingdays.py —— 到 12 月中旬还没补,本进程会拒绝启动。",
            today.year + 1,
        )
    if tradingdays.covers(today.year) and not tradingdays.calendar_for(today.year).verified:
        logger.warning(
            "%d 年的交易日历尚未对照权威来源核实(verified=False),"
            "调度日期不能视为已核验。", today.year,
        )

    daemon = Daemon(store, archive_root=Path(args.archive_root), poll_seconds=args.poll)

    if args.once:
        result = daemon.tick()
        daemon.collector.close()
        if result is None:
            logger.info("此刻不该跑(或前置条件不满足),没有产出归档。")
        return 0

    # SIGTERM 是 systemd stop 发的;SIGINT 是 Ctrl-C。两个都接,
    # 为的是**把浏览器会话关干净**——漏下的 Chromium 会一直占着内存,
    # 而这台机器上二代还在跑,内存本来就紧。
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, daemon.stop)
        except (ValueError, OSError):     # pragma: no cover - 非主线程 / 平台不支持
            logger.debug("装不上 %s 处理器,靠 KeyboardInterrupt 兜底", sig)

    try:
        return daemon.run()
    except KeyboardInterrupt:
        daemon.stop()
        daemon.collector.close()
        return 0


__all__ = [
    "DEFAULT_POLL_SECONDS", "COMPLAIN_EVERY",
    "preflight", "build_runner", "Daemon", "main",
]


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
