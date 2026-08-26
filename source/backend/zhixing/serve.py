"""HTTP 传输层 —— 把字节换成 ``api.Request``,把 ``api.Response`` 换回字节。

## 为什么是标准库

后端到今天为止**零第三方依赖**。这一层如果引入 FastAPI,就会连带
uvicorn、starlette、pydantic 一整串,而它们买到的东西——路由、校验、
序列化——``api.py`` 里已经有了,而且是照契约写的。

规模也不需要:单用户、七个标的、一天六轮,峰值并发是 1。

``api.py`` 不认识本模块,所以将来真要换框架,写个三十行适配器即可,
业务逻辑一行不动。

## 只绑 127.0.0.1

**默认只监听本机。** 接口层没有任何身份认证(见 ``api`` 模块开头),
在认证做出来之前,它不该被别人访问到。

要从别的机器看界面,正确做法是 SSH 端口转发:

    ssh -L 8765:127.0.0.1:8765 <服务器>

而不是把 ``--host`` 改成 ``0.0.0.0``。真要改,下面会打一条明确的警告日志——
让"我把它开到公网了"这件事至少在日志里留下一行。

运行:

    python -m zhixing.serve --archive-root /var/lib/zhixing/archives
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import SYSTEM_NAME, __version__, api, captcha, collect, scheduler, state, tradingdays

logger = logging.getLogger("zhixing.serve")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: 单份 body 上限。前端最大的一次提交是六个时点加一句原因,几百字节。
#: 卡在这里是为了让一个手滑的 100 MB POST 不至于把内存吃光。
MAX_BODY_BYTES = 1 << 20


class _Handler(BaseHTTPRequestHandler):
    """把 HTTP 拆成 ``api.Request``。**这里不做任何业务判断。**"""

    server_version = f"zhixing/{__version__}"
    #: 不回显 Python 版本。少告诉外面一点是一点。
    sys_version = ""

    app: api.App                     # 由 make_server 注入
    allow_origin: str | None = None

    # -- 收 ---------------------------------------------------------------

    def _read_body(self) -> tuple[object, str | None]:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return None, None
        try:
            length = int(raw_length)
        except ValueError:
            return None, "Content-Length 不是整数"
        if length > MAX_BODY_BYTES:
            return None, f"请求体超过上限 {MAX_BODY_BYTES} 字节"
        if length <= 0:
            return None, None

        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"请求体不是合法 JSON:{exc}"

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        # 同名参数只取第一个:契约里没有任何一个参数是可重复的,
        # 悄悄用最后一个会让 `?limit=10&limit=99999` 这种事查不出来。
        query = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}

        body, problem = self._read_body()
        if problem:
            self._send(api.fail(400, "INVALID_REQUEST_BODY", problem))
            return

        request = api.Request(
            method=method, path=parsed.path, query=query, body=body
        )
        self._send(api.handle(self.app, request))

    # -- 发 ---------------------------------------------------------------

    def _send(self, response: api.Response) -> None:
        body = json.dumps(response.payload, ensure_ascii=False).encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 界面读的是自己的数据,没有任何理由被别的站点嵌进去或读走。
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.allow_origin:
            self.send_header("Access-Control-Allow-Origin", self.allow_origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE")
        self.end_headers()
        self.wfile.write(body)

    # -- HTTP 动词 --------------------------------------------------------

    def do_GET(self) -> None:       # noqa: N802 - BaseHTTPRequestHandler 要求这个名字
        self._dispatch("GET")

    def do_POST(self) -> None:      # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:       # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:    # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:   # noqa: N802
        """开发期跨域预检。``--allow-origin`` 没给就当没有这个方法。"""
        if not self.allow_origin:
            self._send(api.fail(405, "METHOD_NOT_ALLOWED", "OPTIONS 未启用"))
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.allow_origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- 日志 -------------------------------------------------------------

    def log_message(self, fmt: str, *args: object) -> None:
        """走 logging,并且**只记方法与路径**。

        不记查询串、不记 body。body 里有验证码接口的明文密钥,
        访问日志是最容易被顺手贴出来的东西。
        """
        logger.info("%s - %s", self.address_string(), fmt % args)


def make_server(
    app: api.App,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    allow_origin: str | None = None,
) -> ThreadingHTTPServer:
    handler = type("_BoundHandler", (_Handler,), {"app": app, "allow_origin": allow_origin})
    return ThreadingHTTPServer((host, port), handler)  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zhixing.serve", description="知行三代接口服务")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--archive-root", required=True,
        help="归档根目录。**必须在仓库外**——仓库在同步盘里。",
    )
    parser.add_argument(
        "--runtime-dir", default=None,
        help=f"运行状态目录,默认取环境变量 {state.RUNTIME_DIR_ENV} 或 ~/.zhixing",
    )
    parser.add_argument(
        "--allow-origin", default=None,
        help="开发期给前端 dev server 用,例如 http://127.0.0.1:5173。生产不要给。",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    if args.host != DEFAULT_HOST:
        logger.warning(
            "监听在 %s 而不是 %s。这个接口层**没有任何身份认证**,"
            "暴露到本机之外等于把账户数据和配置接口交给网络。",
            args.host, DEFAULT_HOST,
        )

    try:
        store = state.Store(args.runtime_dir)
    except state.StateError as exc:
        print(f"启动失败:{exc}", file=sys.stderr)
        return 2

    # ``数据源``(契约 1.4)由本模块传进去,而且是**每次请求现算的**,
    # 不是一句字面量,也不是启动时定死的一个值。
    #
    # 这个字段有前科:它曾经写死着「只读挂载二代 runtime」,而部署后容器里
    # 根本没有那个挂载——状态页把一句没有任何代码在做的承诺当事实显示着。
    # ``collect.describe_source`` 从 ``quotes.SOURCES`` 和券商配置现状算,
    # 改了行情源顺序或动了券商配置,这句话跟着变。
    #
    # 传函数而不是传字符串,是因为**券商配置能在界面上改**:启动时算一次
    # 会让状态页一直说「未配置」,直到有人想起来重启服务——那又是一句
    # 和现状不符的话,等于把同一个毛病换了个地方犯。
    # 人工确认、撤单和委托查询按需使用自己的采集器。构造本身不登录;
    # 只有对应接口被调用时才走现有 login.ensure_session 通路。
    api_collector = collect.Collector(
        store=store, solver=captcha.solver_from_settings(store.captcha())
    )

    def broker_provider():
        api_collector.solver = captcha.solver_from_settings(store.captcha())
        return api_collector.connect_broker()

    app = api.App(
        store=store,
        archive_root=Path(args.archive_root),
        data_source=lambda: collect.describe_source(store),
        broker_provider=broker_provider,
    )

    # 交易日历够不够用。**只警告,不拒绝启动。**
    #
    # 拒绝启动是不成比例的:这个服务除了跑轮次还负责把归档翻给人看,
    # 日历过期不该让"看历史"也用不了。轮次驱动进程会在启动时核对覆盖年份;
    # 接口进程这里只负责让人早点知道。
    today = date.today()

    # 时区。这里**只警告不拒绝**,和日历同一个道理:接口服务不跑轮次,
    # 时区错了它顶多把「调度」那一栏显示成八小时前的样子,翻归档照样能用。
    # 真正的强制在 daemon.preflight —— 时钟不对就不开跑。
    #
    # 但警告必须有,而且要早:2026-08-20 线上那次,接口显示"下次触发
    # 09:16,未到"而北京时间已经 10:43,**每个字段都自洽**,是对着容器
    # 里的 date 才看出来的。日志里有这一行的话,不用查到那一步。
    zone = scheduler.clock_zone_problem(datetime.now().astimezone())
    if zone is not None:
        logger.warning("时区不对:%s", zone)

    try:
        tradingdays.assert_covers(*tradingdays.required_years(today))
    except tradingdays.CalendarError as exc:
        logger.warning(
            "交易日历不完整:%s 假期安排一年一发,过期后轮次会失败而不是猜。", exc
        )
    if not tradingdays.covers(today.year + 1):
        # 次年日历八月份本来就还没发布,这条**不是**"不完整",只是提个醒。
        # 原来这里写的是 assert_covers(today.year, today.year + 1),于是一年里
        # 有十个月每次启动都报一句"交易日历不完整"——警告天天喊等于没有警告,
        # 真出事那次没人会多看一眼。年底 45 天内它会自动变成上面那一条。
        logger.info(
            "还没有 %d 年的交易日历。假期安排一般当年 11 月发布,发布后补进 "
            "tradingdays.py。", today.year + 1,
        )
    if tradingdays.covers(today.year) and not tradingdays.calendar_for(today.year).verified:
        logger.warning(
            "%d 年的交易日历尚未对照权威来源核实(verified=False)。", today.year,
        )

    server = make_server(
        app, host=args.host, port=args.port, allow_origin=args.allow_origin
    )
    logger.info(
        "%s %s 已启动:http://%s:%d/api/status(归档 %s,运行目录 %s)",
        SYSTEM_NAME, __version__, args.host, args.port,
        app.archive_root, store.root,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断,正在停止")
    finally:
        server.server_close()
        api_collector.close()
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
