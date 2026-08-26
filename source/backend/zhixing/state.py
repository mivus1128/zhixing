"""可变状态的落盘 —— 标的清单、调度配置、验证码接口配置、运行事实。

## 为什么单独一个模块

这四样东西的共同点是:**它们是状态,不是事实**。

归档是事实——只追加、不覆盖、写错了也不改。这里的四样正相反,它们本来
就要被改:标的清单用户一个月动一次,调度时点偶尔挪,验证码密钥会轮换。
把两种东西放进同一个模块,迟早有人给归档加一个 `overwrite=True`。

## ⚠️ 运行目录**必须在仓库之外**

本仓库位于百度同步盘内。**写进仓库目录的任何东西等于上传云端**,而且
删不干净。验证码接口的密钥是明文机密,它一旦落进仓库就等于泄露了,
`.gitignore` 拦不住同步盘——同步的是工作区,不是 git 索引。

所以运行目录默认取 ``~/.zhixing``,并且可以用环境变量
``ZHIXING_RUNTIME_DIR`` 指到别处(服务器上应指向 ``/var/lib/zhixing``)。
**不要把它指回仓库里。** 下面 ``resolve_root()`` 会当场拒绝这种配置。

## 密钥怎么存

明文密钥单独落一个 ``captcha.secret``,权限 0600,**不进 JSON**。
这样 ``captcha.json`` 是可以随便打开看、随便贴给别人的,而需要保护的
东西只有一个文件。接口层任何时候都只下发脱敏值,见 ``mask_secret``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import captcha as captcha_mod
from . import catalog as catalog_mod
from . import model as model_mod
from . import runmode as runmode_mod
from . import scheduler as scheduler_mod

logger = logging.getLogger("zhixing.state")


#: 运行目录的环境变量名。
RUNTIME_DIR_ENV = "ZHIXING_RUNTIME_DIR"

#: 默认运行目录。**在用户主目录下,不在仓库里**——理由见模块开头。
DEFAULT_RUNTIME_DIR = Path.home() / ".zhixing"


class StateError(RuntimeError):
    """状态读写被拒。"""


def resolve_root(explicit: str | Path | None = None) -> Path:
    """定运行目录。优先级:显式参数 > 环境变量 > 默认。

    **落在仓库内就直接拒绝。** 这不是洁癖:仓库在同步盘里,把密钥写进去
    等于上传,而且事后删不干净。宁可起不来,也不要静默地把机密同步出去。
    """
    raw = explicit or os.environ.get(RUNTIME_DIR_ENV) or DEFAULT_RUNTIME_DIR
    root = Path(raw).expanduser().resolve()

    repo_root = Path(__file__).resolve().parents[2]
    if root == repo_root or repo_root in root.parents:
        raise StateError(
            f"运行目录 {root} 在仓库内({repo_root})。"
            f"本仓库在同步盘里,验证码密钥写进去等于上传云端。"
            f"请用 {RUNTIME_DIR_ENV} 指到仓库外面。"
        )
    return root


def mask_secret(secret: str) -> str:
    """脱敏。**接口层下发的永远是这个,不是明文。**

    只留后四位。留后四位是为了让人能确认"我配的是不是这一把",
    留更多就开始有用了——有用就是泄露。
    """
    text = (secret or "").strip()
    if not text:
        return ""
    return "****" + text[-4:] if len(text) > 4 else "****"


# ---------------------------------------------------------------------------
#  结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSettings:
    """调用哪个模型、打到哪个中转。

    和 ``CaptchaSettings`` 同一个规矩:``secret`` 是明文,**只在进程内和
    0600 的文件里存在**,往接口/日志/归档送一律走 ``as_public()``。

    ``provider`` 和 ``name`` 会原样进归档的 ``llm_provider`` / ``model``
    (契约 1.1),所以它们得是能给人看的字符串。换了中转就改 ``provider``,
    否则半年后翻归档分不清"同一个模型名"其实来自两家。
    """

    endpoint: str = ""
    name: str = ""
    provider: str = ""
    protocol: str = "openai_chat"
    secret: str = ""

    def as_public(self) -> dict[str, Any]:
        return {
            "接口地址": self.endpoint,
            "模型": self.name,
            "提供方": self.provider,
            "协议": self.protocol,
            "密钥": mask_secret(self.secret),
            "传输": self.transport(),
        }

    def transport(self) -> str:
        """链路是不是明文。**这个值下发给界面,让它常年挂在那儿。**

        原来这里是一道硬拦截:非 ``https://`` 直接拒绝保存。改掉了,理由是
        拦截只在保存的那一秒起作用,而风险是持续存在的——人绕过去(换个
        写法、直接改文件)之后,系统就再也不提这件事了。

        明文 http 的实际代价:API 密钥、七个标的的全部行情、可用资金和
        持仓,每轮六次摊在链路上。**这是用户可以选择承担的风险,但不该是
        他忘掉的风险**,所以改成常驻的一行字而不是一次性的一道门。

        回环地址不算明文:流量根本没出网卡。
        """
        low = self.endpoint.lower()
        if low.startswith("https://"):
            return "https"
        if not low.startswith("http://"):
            return "未知"
        host = low[len("http://"):].split("/")[0].split(":")[0]
        if host in ("127.0.0.1", "localhost", "::1", "[::1]"):
            return "http(本机回环,不出网卡)"
        return "明文 http(密钥与整份上下文会摊在链路上)"

    def to_target(self) -> model_mod.ModelTarget:
        """转成 ``model.ModelTarget``。**转出来的东西里没有密钥**,
        所以它可以整个入档、打日志。密钥另走 ``llm.Credential``。

        ``stream`` 不做成界面上的开关:它不是偏好,是**当前实现到哪儿了**。
        openai_chat 的流式拼接在 ``model.merge_stream`` 里,验过;
        anthropic_messages 的没写。让用户去勾一个勾了就报错的框没有意义。
        """
        return model_mod.ModelTarget(
            name=self.name,
            provider=self.provider,
            base_url=self.endpoint,
            protocol=self.protocol,
            stream=self.protocol == "openai_chat",
        )


@dataclass(frozen=True)
class CaptchaLink:
    """一条识别路。**和主用那条同样的四个字段,故意长得一模一样。**

    单独一个类而不是复用 ``CaptchaSettings``:后者还挂着"备用有哪些"和
    "样本存哪",备用里再套备用是没有意义的形状,能表示出来就迟早有人填。
    """

    provider: str = ""
    endpoint: str = ""
    model: str = ""
    secret: str = ""

    def as_public(self) -> dict[str, Any]:
        return {
            "识别方式": self.provider,
            "接口地址": self.endpoint,
            "模型": self.model,
            "密钥": mask_secret(self.secret),
        }


@dataclass(frozen=True)
class CaptchaSettings:
    """验证码识别接口配置。

    ``secret`` 是明文,**只在进程内和 0600 的文件里存在**。
    任何要往接口、日志、归档里送的地方都必须走 ``as_public()``。

    ## 为什么有"备用"

    验证码这一步失败**不会表现成"验证码平台不行"**,它表现成"今天没
    交易"。一家平台欠费或者当天挂了就是这个后果。所以主用那条之外还能
    再配几条,顺序试,见 ``captcha.ChainSolver``。
    """

    endpoint: str = ""
    model: str = ""
    secret: str = ""
    #: ``captcha.PROVIDER_VISION`` / ``PROVIDER_TTSHITU`` / ``PROVIDER_CHAOJIYING``。
    #: **默认是视觉模型**,这样改这一项之前落盘的配置读出来行为不变。
    provider: str = captcha_mod.PROVIDER_VISION
    #: 备用识别路,按顺序试。空 = 只有主用那一条。
    backups: tuple[CaptchaLink, ...] = ()
    #: 验证码样本目录。空 = 不攒。攒它是为了将来训自研的识别器,
    #: 见 ``captcha.record_sample``——那里也写清楚了这些标注为什么不能直接复用。
    sample_dir: str = ""

    def as_public(self) -> dict[str, Any]:
        """给接口层用的脱敏视图。字段名对齐前端 ``CaptchaSettings``。"""
        return {
            "接口地址": self.endpoint,
            "模型": self.model,
            "识别方式": self.provider,
            "密钥": mask_secret(self.secret),
            "备用识别": [条.as_public() for 条 in self.backups],
        }


@dataclass(frozen=True)
class BrokerSettings:
    """券商登录与浏览器远端。

    ## 这里有两样机密,不是一样

    ``password`` 是交易密码,这不用说。**``account`` 同样按机密对待**——
    完整资金账号在本项目的约束里和交易密码同级,不许进日志、不许进归档、
    不许进提交。所以它和密码一样落 0600 的旁挂文件,不进 ``broker.json``。

    界面上要能看出"配的是哪个账户",靠的是 ``as_public()`` 里遮过的那份
    (``123****9012``),不是原值。

    ## ``remote_url`` 不是机密,但它决定了动谁

    三代**必须用自己的浏览器容器**。二代的 ``tradepilot-browser`` 里挂着
    一个正在用真钱交易的登录会话,共用会把它挤掉——那不是三代的故障,
    是把二代弄停了。所以这一项没有"猜一个默认值"的余地,配错了宁可连不上。
    """

    #: WebDriver 远端。空表示没配,登录会明确失败而不是去猜一个地址。
    remote_url: str = ""
    #: 资金账号。**机密**,见类文档。
    account: str = ""
    #: 交易密码。**机密**。
    password: str = ""

    def as_public(self) -> dict[str, Any]:
        """给接口层用的脱敏视图。**两样机密都不出原值。**

        ⚠️ 这里**没有** ``交易密码`` 这个键,是有意的两层原因:

        1. ``archive._FORBIDDEN_KEYS`` 把这个键名列为出现即拒。出站扫描
           会把整个响应拦下——那道拦截是对的,该改的是这里。
        2. 遮过的密码(``****``)本来就不传达任何信息。账号遮成
           ``123****9012`` 还能让人确认"配的是不是这个账户",密码遮完
           只剩一个占位符,不如直接说"配了没有"。
        """
        return {
            "浏览器远端": self.remote_url,
            "资金账号": _mask_account(self.account),
            "交易密码已配置": bool(self.password),
        }

    @property
    def configured(self) -> bool:
        """三样齐了才算配好。缺任何一样,登录都该明确说缺什么。"""
        return bool(self.remote_url.strip() and self.account.strip() and self.password)

    def missing(self) -> tuple[str, ...]:
        """缺了哪几样。**一次报全部**,不是撞上第一个就返回。"""
        return tuple(
            名 for 名, 值 in (
                ("浏览器远端", self.remote_url.strip()),
                ("资金账号", self.account.strip()),
                ("交易密码", self.password),
            ) if not 值
        )


def _mask_account(value: str) -> str:
    """遮资金账号。和 ``eastmoney.mask_account_id`` 同一个规矩。

    这里重写一份而不是 import,是因为 ``state`` 不该依赖券商模块——
    落盘层认识某一家券商是反过来的依赖。两处都短,重复比耦合便宜。
    """
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 7:
        return "****"
    return f"{text[:3]}****{text[-4:]}"


@dataclass(frozen=True)
class RuntimeFacts:
    """采集与运行的最近状况(契约 1.4 后半段)。

    这些值**没有任何地方在产生**——采集层和驱动层都还没写。默认值是
    "不知道",不是"正常":``登录状态`` 默认 ``未知`` 而不是 ``已登录``,
    ``上一轮成功时间`` 默认 ``None`` 而不是当前时间。

    契约 1.4 那三个字段就是为了让"停摆"和"今天没信号"分得开;
    这里要是拿默认值冒充正常,那个区分从第一天起就是假的。
    """

    登录状态: str = "未知"
    最近采集时间: str | None = None
    上一轮成功时间: str | None = None
    连续失败轮数: int = 0
    最近失败原因: str | None = None

    def as_public(self) -> dict[str, Any]:
        return {
            "登录状态": self.登录状态,
            "最近采集时间": self.最近采集时间,
            "上一轮成功时间": self.上一轮成功时间,
            "连续失败轮数": self.连续失败轮数,
            "最近失败原因": self.最近失败原因,
        }


# ---------------------------------------------------------------------------
#  落盘
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: Any, *, private: bool = False) -> None:
    """原子写。和 ``archive.write_run`` 同一个套路:临时文件 + replace。

    中途断电只会留下一个 ``.tmp``,不会留下半份 JSON——半份 JSON 比没有
    更糟,它看起来像数据。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if private:
        _restrict(tmp)
    os.replace(tmp, path)


def _restrict(path: Path) -> None:
    """把文件权限收到 0600。

    ⚠️ **Windows 上 chmod 基本不起作用**,这里是尽力而为。生产在 Linux 上,
    这一行在那儿是实的。开发机上密钥的实际保护来自"运行目录在仓库外"。
    """
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:  # pragma: no cover - 平台相关
        logger.warning("无法收紧 %s 的权限:%s", path.name, exc)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"读取 {path.name} 失败:{exc}") from exc


class Store:
    """一份运行状态。构造时不碰磁盘,读到才碰。

    没有缓存——每次读都回文件。单用户、七个标的、一天六轮,这个量级下
    缓存买不到什么,却会带来"界面上改了但另一处还是旧值"这种最难查的问题。
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = resolve_root(root)

    # -- 标的清单 ---------------------------------------------------------

    @property
    def catalog_path(self) -> Path:
        return self.root / "catalog.json"

    def catalog(self) -> catalog_mod.Catalog:
        entries = _read_json(self.catalog_path, {"objects": []}).get("objects", [])
        return catalog_mod.Catalog(catalog_mod.from_entry(e) for e in entries)

    def save_catalog(self, objects: Iterable[catalog_mod.TradeObject]) -> None:
        """整份重写。清单只有七条,不值得做增量。"""
        items = list(objects)
        catalog_mod.Catalog(items)  # 重复 object_id / symbol 在这里就炸,不留到读的时候
        _write_json(
            self.catalog_path,
            {
                "objects": [
                    {
                        "object_id": o.object_id,
                        "market": o.market,
                        "symbol": o.symbol,
                        "名称": o.name,
                        "类型": o.kind,
                        "资产类型": o.asset_type,
                        "交易单位": o.lot_size,
                    }
                    for o in items
                ]
            },
        )

    # -- 调度配置 ---------------------------------------------------------

    @property
    def schedule_path(self) -> Path:
        return self.root / "schedule.json"

    def schedule(self) -> scheduler_mod.ScheduleConfig:
        raw = _read_json(self.schedule_path, None)
        if not raw:
            return scheduler_mod.DEFAULT_CONFIG

        config, problems = scheduler_mod.validate_config(
            raw.get("时点", []),
            max_jitter_seconds=int(raw.get("抖动上限秒", 180)),
            window_minutes=int(raw.get("有效窗口分钟", 20)),
            jitter_salt=str(raw.get("抖动盐", "zhixing")),
        )
        if config is None:
            # 存盘的配置不合法,只可能是有人手改了文件。**不静默退回默认值**:
            # 那会让"我明明改了时点怎么没生效"变成一个查不出来的问题。
            raise StateError(
                f"{self.schedule_path.name} 里的调度配置不合法:" + ";".join(problems)
            )
        return config

    def save_schedule(self, config: scheduler_mod.ScheduleConfig) -> None:
        _write_json(
            self.schedule_path,
            {
                "时点": list(config.as_text()),
                "抖动上限秒": config.max_jitter_seconds,
                "有效窗口分钟": config.window_minutes,
                "抖动盐": config.jitter_salt,
            },
        )

    # -- 模型接口 ---------------------------------------------------------

    @property
    def model_path(self) -> Path:
        return self.root / "model.json"

    @property
    def model_secret_path(self) -> Path:
        return self.root / "model.secret"

    def model(self) -> ModelSettings:
        raw = _read_json(self.model_path, {})
        secret = ""
        if self.model_secret_path.exists():
            secret = self.model_secret_path.read_text(encoding="utf-8").strip()
        return ModelSettings(
            endpoint=str(raw.get("接口地址", "")),
            name=str(raw.get("模型", "")),
            provider=str(raw.get("提供方", "")),
            protocol=str(raw.get("协议", "openai_chat")),
            secret=secret,
        )

    def save_model(self, settings: ModelSettings) -> None:
        """和 ``save_captcha`` 完全同构:明文落单独的 0600 文件,
        JSON 里只有非机密字段,``secret`` 为空表示**不改密钥**。

        两处长得一样不是重复,是**同一条规矩的两次落实**。哪天要加第三个
        带密钥的配置,照抄这一份,而不是发明一个新写法。
        """
        _write_json(
            self.model_path,
            {
                "接口地址": settings.endpoint,
                "模型": settings.name,
                "提供方": settings.provider,
                "协议": settings.protocol,
            },
        )
        if settings.secret:
            self.model_secret_path.parent.mkdir(parents=True, exist_ok=True)
            self.model_secret_path.write_text(settings.secret, encoding="utf-8")
            _restrict(self.model_secret_path)

    # -- 验证码接口 -------------------------------------------------------

    @property
    def captcha_path(self) -> Path:
        return self.root / "captcha.json"

    @property
    def captcha_secret_path(self) -> Path:
        return self.root / "captcha.secret"

    @property
    def captcha_chain_path(self) -> Path:
        """备用识别路。**整份都是机密**(每一条都带密钥),所以 0600。"""
        return self.root / "captcha.chain"

    @property
    def captcha_sample_dir(self) -> Path:
        return self.root / "captcha-samples"

    def captcha_backups(self) -> tuple[CaptchaLink, ...]:
        """读备用识别路。**读不出来当成"没有备用"**,不抛。

        这个文件坏了不该让登录整个起不来:没有备用只是少一层兜底,
        而抛异常是连主用那条都用不上了。
        """
        try:
            raw = _read_json(self.captcha_chain_path, [])
        except StateError as exc:
            logger.warning("备用验证码识别路读不出来(%s),按「没有备用」处理。", exc)
            return ()
        if not isinstance(raw, list):
            return ()
        条目 = []
        for 项 in raw:
            if not isinstance(项, Mapping):
                continue
            条目.append(CaptchaLink(
                provider=str(项.get("识别方式", "") or ""),
                endpoint=str(项.get("接口地址", "") or ""),
                model=str(项.get("模型", "") or ""),
                secret=str(项.get("密钥", "") or ""),
            ))
        return tuple(条目)

    def captcha(self) -> CaptchaSettings:
        raw = _read_json(self.captcha_path, {})
        secret = ""
        if self.captcha_secret_path.exists():
            secret = self.captcha_secret_path.read_text(encoding="utf-8").strip()
        return CaptchaSettings(
            endpoint=str(raw.get("接口地址", "")),
            model=str(raw.get("模型", "")),
            # 老配置里没有这一项。**读不到按视觉模型算**,不是按"没配"算——
            # 后者会让一份本来能用的配置在升级后静悄悄失效。
            provider=str(raw.get("识别方式", "") or captcha_mod.PROVIDER_VISION),
            secret=secret,
            backups=self.captcha_backups(),
            # 样本目录跟着运行目录走,不是配置项:它没有"配错"这种状态,
            # 而多一个配置项就多一个能填错的地方。
            sample_dir=str(self.captcha_sample_dir),
        )

    def save_captcha_backups(self, links: Iterable[CaptchaLink]) -> None:
        """整份覆盖备用识别路。0600。

        整份覆盖而不是逐条增删:这份东西一共两三条,增删接口的复杂度全花在
        "第二条现在是第几条"上,而那正是最容易搞错的地方。
        """
        _write_json(
            self.captcha_chain_path,
            [
                {
                    "识别方式": 条.provider,
                    "接口地址": 条.endpoint,
                    "模型": 条.model,
                    "密钥": 条.secret,
                }
                for 条 in links
            ],
            private=True,
        )

    def save_captcha(self, settings: CaptchaSettings) -> None:
        """明文密钥落单独的 0600 文件,JSON 里只有非机密字段。

        ``secret`` 为空表示**不改密钥**,不是"清空密钥"——前端提交空密钥
        意味着"这次没动它"(契约 2 :后端不下发明文,所以前端手上也没有
        原值可回填)。要真清空得删文件,那是运维动作不是界面动作。
        """
        _write_json(
            self.captcha_path,
            {
                "接口地址": settings.endpoint,
                "模型": settings.model,
                "识别方式": settings.provider or captcha_mod.PROVIDER_VISION,
            },
        )
        # 空的备用列表 = **这次没动它**,不是"清空备用"。和密钥同一个约定,
        # 理由也一样:前端手上只有脱敏值,回填不了,所以它提交的"空"只能
        # 解释成"没碰"。真要清空,删 captcha.chain —— 那是运维动作。
        if settings.backups:
            self.save_captcha_backups(settings.backups)
        if settings.secret:
            self.captcha_secret_path.parent.mkdir(parents=True, exist_ok=True)
            self.captcha_secret_path.write_text(settings.secret, encoding="utf-8")
            _restrict(self.captcha_secret_path)

    # -- 账户快照 ---------------------------------------------------------

    @property
    def account_path(self) -> Path:
        return self.root / "account.json"

    def account(self) -> dict[str, Any] | None:
        """最近一次采到的账户摘要。**没采过返回 None,不返回空账户。**

        界面拿这个显示,而不是现场去券商查——查一次要开浏览器、可能要
        重新登录、过验证码,几十秒。**HTTP 请求绝不能等这个**,否则界面
        一打开就转圈,而且每刷新一次页面就多一次登录尝试,那是在自己撞
        券商的失败次数上限。

        代价是数据可能是陈的。所以存的时候一起存 ``采集时间``,界面必须
        把它显示出来——**陈数据可以接受,看不出它陈不可以**。
        """
        raw = _read_json(self.account_path, None)
        return raw if isinstance(raw, dict) else None

    def save_account(self, summary: Mapping[str, Any], *, collected_at: str) -> None:
        """落一份账户摘要。**0600**——里面有持仓金额和账户 ID。"""
        _write_json(
            self.account_path,
            {"采集时间": collected_at, "账户": dict(summary)},
            private=True,
        )

    # -- 券商 -------------------------------------------------------------

    @property
    def broker_path(self) -> Path:
        return self.root / "broker.json"

    @property
    def broker_account_path(self) -> Path:
        return self.root / "broker.account"

    @property
    def broker_password_path(self) -> Path:
        return self.root / "broker.password"

    def broker(self) -> BrokerSettings:
        raw = _read_json(self.broker_path, {})
        account = ""
        if self.broker_account_path.exists():
            account = self.broker_account_path.read_text(encoding="utf-8").strip()
        password = ""
        if self.broker_password_path.exists():
            # **不 strip 密码。** 首尾空格在密码里是合法字符,strip 掉之后
            # 登录会以「密码错误」的面目出现,而人查不出为什么——他明明配对了。
            password = self.broker_password_path.read_text(encoding="utf-8").rstrip("\n")
        return BrokerSettings(
            remote_url=str(raw.get("浏览器远端", "")),
            account=account,
            password=password,
        )

    def save_broker(self, settings: BrokerSettings) -> None:
        """两样机密各落一个 0600 文件,JSON 里只有远端地址。

        和 ``save_captcha`` 同一个规矩:**空值表示"这次没动它",不是"清空"。**
        前端拿不到原值(后端从不下发明文),提交空字符串只可能意味着没改。
        真要清空得删文件,那是运维动作。
        """
        _write_json(self.broker_path, {"浏览器远端": settings.remote_url})
        for value, path in (
            (settings.account.strip(), self.broker_account_path),
            (settings.password, self.broker_password_path),
        ):
            if value:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding="utf-8")
                _restrict(path)

    # -- 登录成功指纹 -----------------------------------------------------
    #
    # 只回答一个问题:**当前这套账号+密码,此前有没有成功登进去过。**
    # 东财登录失败时只说「您输入的信息有误」,不说错的是哪一项;有没有
    # 这条记录,决定了那句话该按"验证码没认对(可以换张图重来)"处理,
    # 还是按"密码可能就是错的(一次都不多试)"处理。见
    # ``eastmoney.AMBIGUOUS_LOGIN_HINTS``。

    @property
    def login_proof_path(self) -> Path:
        return self.root / "broker.loginproof"

    @staticmethod
    def _login_fingerprint(account: str, password: str) -> str:
        """账号+密码的指纹。**存这个,不存密码本身。**

        换行做分隔,免得 ("ab", "c") 和 ("a", "bc") 撞成同一个指纹。
        """
        raw = f"{account}\n{password}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def login_proven(self, account: str, password: str) -> bool:
        """这套账号密码成功登过没有。

        **读不出来一律当成"没登过"**,不抛异常:这个判断只用来决定"要不要
        多试一次",判保守了顶多少试一次,判宽了是拿账户去撞券商的次数上限。
        文件坏了不该让整轮登录崩掉。
        """
        try:
            raw = _read_json(self.login_proof_path, {})
        except StateError as exc:
            logger.warning("登录指纹读不出来(%s),按「没成功登过」处理。", exc)
            return False
        if not isinstance(raw, Mapping):
            return False
        return str(raw.get("fingerprint") or "") == self._login_fingerprint(account, password)

    def save_login_proof(self, account: str, password: str, *, at: datetime | None = None) -> None:
        """记下"这套账号密码刚刚登进去了"。登录成功之后调。"""
        _write_json(
            self.login_proof_path,
            {
                "fingerprint": self._login_fingerprint(account, password),
                "at": (at or datetime.now()).isoformat(timespec="seconds"),
                "说明": "只是指纹,不是密码;账号或密码一改就对不上,自动退回「没登过」。",
            },
            private=True,
        )

    # -- 无人值守开关 -----------------------------------------------------

    @property
    def unattended_path(self) -> Path:
        return self.root / "unattended.json"

    def unattended(self) -> runmode_mod.UnattendedState:
        """读跨进程共享的无人值守状态。文件不存在时默认关闭。"""
        raw = _read_json(self.unattended_path, {})
        if not raw:
            return runmode_mod.UnattendedState(
                enabled=False,
                changed_by="system",
                changed_at=datetime.min,
                reason="初始状态:默认关闭,需显式开启",
            )
        if not isinstance(raw, Mapping) or not isinstance(raw.get("enabled"), bool):
            raise StateError(f"{self.unattended_path.name} 里的 enabled 必须是布尔值")
        try:
            changed_at = datetime.fromisoformat(str(raw.get("changed_at") or ""))
        except ValueError as exc:
            raise StateError(
                f"{self.unattended_path.name} 里的 changed_at 不是合法 ISO 时间"
            ) from exc
        changed_by = str(raw.get("changed_by") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        if not changed_by or not reason:
            raise StateError(
                f"{self.unattended_path.name} 缺少 changed_by 或 reason,不能恢复授权状态"
            )
        return runmode_mod.UnattendedState(
            enabled=raw["enabled"],
            changed_by=changed_by,
            changed_at=changed_at,
            reason=reason,
        )

    def save_unattended(self, value: runmode_mod.UnattendedState) -> None:
        """原子保存开关,供 API 与 daemon 两个进程读取同一份状态。"""
        _write_json(
            self.unattended_path,
            {
                "enabled": value.enabled,
                "changed_by": value.changed_by,
                "changed_at": value.changed_at.isoformat(),
                "reason": value.reason,
            },
            private=True,
        )

    # -- 运行事实 ---------------------------------------------------------

    @property
    def runtime_path(self) -> Path:
        return self.root / "runtime.json"

    def runtime(self) -> RuntimeFacts:
        raw = _read_json(self.runtime_path, {})
        base = RuntimeFacts()
        if not isinstance(raw, Mapping):
            return base
        return replace(
            base,
            **{k: raw[k] for k in raw if k in RuntimeFacts.__dataclass_fields__},
        )

    def save_runtime(self, facts: RuntimeFacts) -> None:
        _write_json(self.runtime_path, facts.as_public())


__all__ = [
    "RUNTIME_DIR_ENV", "DEFAULT_RUNTIME_DIR", "StateError",
    "resolve_root", "mask_secret",
    "ModelSettings", "CaptchaSettings", "CaptchaLink", "BrokerSettings",
    "RuntimeFacts", "Store",
]
