#!/usr/bin/env python
"""cdbg —— 面向 C 语言初学者的可视化调试器。

用法：
    cdbg prog.c              编译并启动调试会话，自动打开可视化界面
    cdbg prog.exe            调试已编译好的可执行文件（Windows）
    cdbg prog                调试已编译好的可执行文件（Linux）
    cdbg prog.c --no-browser 不自动开浏览器（自己访问打印出来的地址）
    cdbg prog.c --headless   无界面模式，只在终端打印状态（供 IDE 调用）

设计要点：
- 目标程序由 cdbg 启动（GDB 的 attach 模式在 Windows 上会被系统拒绝）
- 采集与执行控制分离：MI2 管执行，GDB Python 探针管数据
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

# 允许直接 `python cdbg.py` 运行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cdbg.browser import open_browser  # noqa: E402
from cdbg.launcher import BuildError, ToolNotFound, find_gdb  # noqa: E402
from cdbg.session import DebugSession  # noqa: E402
from cdbg.web_server import serve  # noqa: E402

BANNER = r"""
  cdbg  --  C 语言可视化调试器
  -----------------------------------------------
"""


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="cdbg",
        description="面向初学者的 C 语言可视化调试器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  cdbg hello.c                 编译并打开可视化调试界面
  cdbg hello.c --no-browser    不自动打开浏览器
  cdbg hello.exe               调试已编译好的程序（Windows）
  cdbg hello                   调试已编译好的程序（Linux）
""",
    )
    parser.add_argument("source", nargs="?", help="要调试的 .c 源文件或可执行文件")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--headless", action="store_true", help="无界面模式（终端输出）")
    parser.add_argument("--port", type=int, default=0, help="Web 服务端口，0 表示自动选择")
    parser.add_argument("--host", default="127.0.0.1", help="Web 服务监听地址")
    parser.add_argument("--keep-workdir", action="store_true", help="保留临时工作目录（调试用）")
    parser.add_argument("--no-instrument", action="store_true", help="关闭源码插桩")
    parser.add_argument("--gdb", default=None, help="指定 gdb 可执行文件路径")
    parser.add_argument(
        "--scan-compiler", action="store_true",
        help="扫描本机编译器并交互选择，写入 config/toolchain.conf 后退出",
    )
    parser.add_argument(
        "--no-tui", action="store_true",
        help="配合 --scan-compiler：不进交互界面，按优先级自动选一套",
    )
    parser.add_argument(
        "--select", type=int, default=0,
        help="配合 --scan-compiler：直接选第 N 套（从 1 开始）",
    )
    parser.add_argument(
        "--toolchain", action="store_true",
        help="打印当前使用的编译器 / 调试器与配置位置，然后退出",
    )
    return parser.parse_args(argv)


def print_headless(snapshot):
    """无界面模式下的终端输出。"""
    if snapshot is None:
        return
    line = "─" * 60
    print("\n%s" % line)
    print("位置: %s:%d  (%s)" % (
        os.path.basename(snapshot.current_file or "?"),
        snapshot.current_line,
        snapshot.current_func or "?",
    ))
    print("调用栈深度: %d   栈占用: %d 字节" % (snapshot.depth, snapshot.stack_bytes))
    print(line)
    for fr in snapshot.frames:
        print("  #%d %s()  %s:%d" % (fr.index, fr.func, os.path.basename(fr.file or ""), fr.line))
        for v in fr.vars:
            extra = ""
            if v.is_pointer and v.pointer_target is not None:
                extra = "  → 0x%016X" % v.pointer_target
            elif v.is_string and v.preview:
                extra = '  = "%s"' % v.preview
            print("       %-16s %-14s @0x%016X%s" % (
                v.name, (v.type or "")[:14], v.address or 0, extra))
    if snapshot.globals:
        print("  全局/静态:")
        for v in snapshot.globals:
            print("       %-16s %-14s @0x%016X = %s" % (
                v.name, (v.type or "")[:14], v.address or 0, (v.value or "")[:20]))
    for lesson in snapshot.lessons:
        print("\n  【%s · %s】" % (lesson.chapter, lesson.topic))
        for ln in lesson.message.split("\n"):
            print("     " + ln)
    print(line)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]

    # ---- 更新器的后台下载进程：不进调试流程，下载完就退出 ----
    if "--download-update" in argv:
        from cdbg import updater

        return updater.run_download_worker()

    args = parse_args(argv)

    # ---- 工具链自检 / 重扫：不调试，跑完就退 ----
    if args.scan_compiler:
        from cdbg.toolchain_config import scan_and_write

        print(BANNER)
        sel = scan_and_write(interactive=not args.no_tui and not args.select,
                             select=args.select)
        if not sel:
            print("没有可用的工具链。", file=sys.stderr)
            return 1
        print("\n已写入：%s" % sel.get("名称", ""))
        return 0

    if args.toolchain:
        from cdbg.launcher import toolchain_info
        from cdbg.toolchain_config import describe

        print(BANNER)
        print("当前编译器：%s" % describe())
        info = toolchain_info()
        for err in info.get("errors", []):
            print("  ! %s" % err, file=sys.stderr)
        return 0 if not info.get("errors") else 1

    source = args.source
    if not source:
        print("错误：没有指定要调试的文件。用 -h 看用法。", file=sys.stderr)
        return 2

    if not os.path.isfile(source):
        print("错误：找不到文件 %s" % source, file=sys.stderr)
        return 2

    print(BANNER)

    # 版本检查走后台线程：编译那几秒正好用来联网，不拖慢启动。
    # --headless 不查 —— 那是给 IDE 读的机器输出，别往里掺废话。
    update_check = None
    if not args.headless:
        from cdbg import updater

        update_check = updater.start_check()

    session = DebugSession(
        source=source,
        instrument=not args.no_instrument,
        gdb_path=args.gdb,
    )

    # 编译要等一两秒，空着也是空着 —— 随机抖三条「正在…」。
    # 顺手把 {file_name} 换成真文件名（上游词库是给 LLM 状态条用的）。
    #
    # --headless 不打：那是给 IDE 读的机器输出，别往里掺废话。
    if not args.headless:
        try:
            from cdbg import quips

            for quip in quips.sample(3, file_name=os.path.basename(source)):
                print("  " + quip)
            print()
        except Exception:
            pass

    build_ok = True
    try:
        print("正在编译并启动调试会话...")
        build_ok = session.start()
    except BuildError as exc:
        print("\n编译失败：\n%s" % exc, file=sys.stderr)
        return 1
    except ToolNotFound as exc:
        print("\n环境问题：%s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:
        print("\n启动失败：%s" % exc, file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1

    if not build_ok:
        # 编译没过，但错误位置已经定位好了 —— 照样开界面，
        # 在源码上把出错的行标出来。
        print("  编译未通过，已定位到 %d 处问题：" % len(session.build_errors))
        for d in session.build_errors[:6]:
            print("     第 %d 行: %s" % (d.get("line", 0), d.get("message", "")))
    else:
    
        print("编译成功！\n本次编译中意外发现三位参与编译的人员，他们分别是亚切斯拉夫·巴兰尼科夫、安东·尤丁采夫、尼基塔·布亚诺夫......")
        print("  源码:   %s" % session.original_source_path)
        if session.build and session.build.exe_path:
            print("  可执行: %s" % session.build.exe_path)
        try:
            from cdbg.launcher import find_compiler

            print("  编译器: %s" % find_compiler())
        except ToolNotFound:
            pass
        print("  状态:   未执行（按「调试」开始，或点源码左侧行号设置断点）")


    if args.headless:
        print_headless(session.snapshot)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            session.shutdown()
        return 0

    # ---- Web 界面 ----
    httpd, port = serve(session, host=args.host, port=args.port)
    url = "http://%s:%d/" % (args.host, port)
    print("\n  可视化界面: %s" % url)
    print("  按 Ctrl+C 结束调试会话")
    if update_check is not None:
        from cdbg import updater

        updater.print_status(update_check)
    print()

    if not args.no_browser:
        if not open_browser(url):
            print("  (没能自动打开浏览器，请手动访问上面的地址)")
            if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
                print("  (WSL 环境：请用 Windows 浏览器打开上面的 127.0.0.1 地址；"
                      "也可安装 wslu 后使用 wslview)")

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    # 程序退出的提示只说一次。
    # 之前这里每 0.4 秒检查一次状态，程序一退出就变成刷屏 ——
    # 一次退出能打几十行同样的字。
    exit_reported = False
    try:
        while True:
            time.sleep(0.4)
            if exit_reported:
                continue
            snap = session.snapshot
            if snap is not None and snap.state == "exited":
                exit_reported = True
                print("  程序已退出。调试会话仍在运行，可查看最终状态。")
    except KeyboardInterrupt:
        if update_check is not None:
            from cdbg import updater

            updater.prompt_on_exit(update_check)
        print("\n正在结束...")
    finally:
        httpd.shutdown()
        session.shutdown()
        if not args.keep_workdir and session.build:
            pass  # 工作目录保留在临时区，由系统自行清理，便于事后排查

    return 0


if __name__ == "__main__":
    sys.exit(main())
