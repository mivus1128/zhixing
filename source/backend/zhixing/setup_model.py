"""``python -m zhixing.setup_model`` —— 在命令行里把模型接口配好。

## 为什么界面有了还要这个

界面上那张表单走的是 HTTP。在服务器上,那条链路要么是明文 http,要么得先
搭 SSH 端口转发。而**录密钥这件事只做几次**,为它先把链路弄安全,顺序反了。

这个工具走的是另一条路:你已经 SSH 进去了,那条通道本来就是加密的,
密钥从键盘直接进 0600 的文件,中间不经过网络。

## 三处不留痕

1. ``getpass`` 读密钥——**不回显**,肩后看不到;
2. 不经过命令行参数——**不进 shell 历史**,``history`` 里翻不到;
3. 不打印、不记日志——本模块任何时候都只输出 ``state.mask_secret`` 的结果。

第 2 条是刻意不做 ``--secret`` 参数的理由。加一个参数是十秒钟的事,
但它会让密钥出现在 ``~/.bash_history`` 和 ``ps`` 的输出里,而这两个地方
**没有人会想起来去清**。少一个便利选项,换掉一整类泄露方式。

## 空回车 = 不改

和契约 2.5/2.7 同一个规矩:密钥留空表示"这次没动它",不是"清空"。
后端不下发明文,所以这里也没有原值可回填,只能这么约定。
"""

from __future__ import annotations

import argparse
import getpass
import sys

from . import llm, model, state


def _ask(prompt: str, current: str) -> str:
    """问一个非机密字段。空回车沿用原值。"""
    hint = f"[{current}]" if current else "[未配置]"
    answer = input(f"{prompt} {hint}: ").strip()
    return answer or current


def _ask_protocol(current: str) -> str:
    options = list(model.PROTOCOLS)
    print("\n协议(这是线上格式,不是模型家族——走 OpenAI 兼容接口的中转一律选 1):")
    for i, name in enumerate(options, 1):
        print(f"  {i}) {name}" + ("  ← 当前" if name == current else ""))
    while True:
        answer = input(f"选一个 [1-{len(options)}],回车沿用 {current}: ").strip()
        if not answer:
            return current
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        if answer in options:
            return answer
        print("  没听懂,再来一次。")


def _probe(settings: state.ModelSettings) -> int:
    """真打一次,确认这套配置能用。

    **值得做。** 配置"看起来对"和"打得通"是两件事:中转可能只开了某一家的
    权限,密钥可能贴漏了一位,协议可能选反。这些全都在保存的那一刻是看不出来的,
    要等到盘中某一轮才暴露——而那一轮就白跑了。
    """
    target = settings.to_target()
    caller = llm.HttpCaller(credential=llm.Credential(settings.secret), retries=0)
    # ⚠️ 这两句里**必须出现 "json" 这个词**。配置里 force_json 是开着的,
    # 而 DeepSeek 这类服务端在 response_format=json_object 时会检查提示词里
    # 有没有提到 json,没有就直接 400。探针要是不按真实请求的样子打,
    # 它就会把一套好配置判成坏配置——那比没有探针更糟,因为它给的三条
    # "常见原因"会把人支去改密钥、改地址、改协议,而那三样本来都是对的。
    body = model.build_request(
        target,
        system_prompt="你是一个连通性探针。只回一个 JSON 对象,不要多说。",
        user_text='只回复这个 json:{"收到": true}',
    )
    print(f"\n正在试打 {target.provider} / {target.name} ……")
    try:
        reply = caller.call(target, body, object_id="probe")
    except llm.LlmError as exc:
        print(f"  打不通:{exc}", file=sys.stderr)
        print("  常见原因:密钥没有这个模型的权限、协议选反了、地址少了 /v1。",
              file=sys.stderr)
        return 1
    except model.ModelError as exc:
        print(f"  回复解析不了:{exc}", file=sys.stderr)
        return 1

    head = reply.text.strip().replace("\n", " ")[:40]
    print(f"  通了。回了 {len(reply.text)} 字:{head}")
    if reply.model_echo and reply.model_echo != target.name:
        # 中转把请求转给别的模型是会发生的事,而且不报错。
        print(f"  ⚠️ 服务端回显的模型是 {reply.model_echo},和你配的 {target.name} 不一样。")
    print(f"  用量:输入 {reply.usage.input_tokens} / 输出 {reply.usage.output_tokens}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zhixing.setup_model",
        description="交互式配置模型接口。密钥不回显、不进命令行参数、不进日志。",
    )
    parser.add_argument(
        "--runtime-dir", default=None,
        help=f"运行状态目录,默认取环境变量 {state.RUNTIME_DIR_ENV} 或 ~/.zhixing",
    )
    parser.add_argument(
        "--probe", action="store_true",
        help="保存后真打一次,确认这套配置能用",
    )
    args = parser.parse_args(argv)

    try:
        store = state.Store(args.runtime_dir)
    except state.StateError as exc:
        print(f"起不来:{exc}", file=sys.stderr)
        return 2

    current = store.model()
    print(f"运行目录:{store.root}")
    print(f"配置文件:{store.model_path}(密钥另存 {store.model_secret_path.name},权限 0600)\n")

    endpoint = _ask("接口地址", current.endpoint)
    if not endpoint:
        print("接口地址不能为空。", file=sys.stderr)
        return 1
    if not endpoint.startswith(("http://", "https://")):
        print("接口地址要以 http:// 或 https:// 开头。", file=sys.stderr)
        return 1

    name = _ask("模型(会原样进归档)", current.name)
    provider = _ask("提供方(会原样进归档,换了中转就改这栏)", current.provider)
    if not name or not provider:
        print("模型和提供方都不能为空,它们要原样进归档。", file=sys.stderr)
        return 1

    protocol = _ask_protocol(current.protocol or "openai_chat")

    print("\n密钥(输入时不显示;直接回车表示不修改)")
    if current.secret:
        print(f"  当前:{state.mask_secret(current.secret)}")
    else:
        print("  当前:未配置(头一次配置必须填)")
    secret = getpass.getpass("  密钥: ").strip()
    if not secret and not current.secret:
        print("还没有配过密钥,这次必须填。", file=sys.stderr)
        return 1

    settings = state.ModelSettings(
        endpoint=endpoint, name=name, provider=provider,
        protocol=protocol, secret=secret or current.secret,
    )

    store.save_model(state.ModelSettings(
        endpoint=endpoint, name=name, provider=provider,
        protocol=protocol, secret=secret,      # 空 = 不改,由 save_model 处理
    ))

    print("\n已保存:")
    for key, value in settings.as_public().items():
        print(f"  {key}:{value}")

    transport = settings.transport()
    if "明文" in transport:
        print(
            "\n  ⚠️ 这条链路是明文的。API 密钥、七个标的的全部行情、可用资金和持仓,"
            "\n     每轮六次摊在链路上。这是可以选择承担的风险,但不该是忘掉的风险。"
        )

    if args.probe:
        return _probe(settings)
    print("\n(加 --probe 可以现在真打一次,确认这套配置能用)")
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
