"""cdbg 云编译打包脚本。

把 Nuitka standalone 的产物组织成「免安装」目录，再压成 zip：
    cdbg-windows-x64/
        cdbg.exe          <- 主程序
        cdbg/gdb_probe.py <- GDB 采集脚本
        web/ shim/ examples/
        config/           <- 首次运行自动生成 toolchain.conf
        README.txt        <- 使用说明

设计要点
--------
* 用 standalone（非 onefile）：onefile 会把资源解压到 %TEMP%，
  而源码用 __file__ 定位 config/web/shim，路径全乱，前端打不开。
  standalone 下 exe 和资源同一目录，__file__ 指向真实位置，问题消失。
* --file-reference-choice=runtime：让 __file__ 指向运行时 dist 内的路径。
* config 目录不放本机 toolchain.conf（那是绝对路径，换机器就废），
  留空目录，首次运行自动扫描生成。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# GitHub Actions 的 Windows runner 默认 stdout/stderr 编码是 cp1252，
# 直接 print 中文会抛 UnicodeEncodeError（编译其实已经成功，挂在日志上）。
# 强制切到 UTF-8；errors="replace" 作为兜底，保证任何环境都不会因日志崩掉。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent          # tools/
ROOT = HERE.parent                              # 项目根（放 cdbg.py 的地方）


def run(cmd: list[str]) -> None:
    print("[build] " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def write_readme(dist: Path) -> None:
    text = """cdbg - C 语言可视化调试器（Windows x64 免安装版）
==================================================

解压到任意目录，双击 cdbg.exe 即可使用，无需安装 Python。

用法
----
1) 把 .c 源文件拖到 cdbg.exe 上；或
2) 把 .c 源文件拖到 start.bat 上（cdbg.exe 被占用 / 出问题时的兜底入口）；或
3) 命令行： cdbg.exe 你的程序.c

第一次运行会自动扫描本机的 gcc / gdb（gdb 需带 Python 支持），
把结果写到同目录的 config\\toolchain.conf。之后再运行直接读它。

换机器后配置里的绝对路径会失效，程序会自动重扫并覆盖配置。

完整说明见项目仓库的 README.md。
"""
    (dist / "README.txt").write_text(text, encoding="utf-8")


def write_start_bat(dist: Path) -> None:
    """启动兜底入口：源码树有 start.bat，压缩包里原来丢了，这里补回来。

    平时双击 cdbg.exe 就够；这个脚本是给「exe 被占用 / 损坏 / 拖拽打开方式」
    留的第二入口 —— bat 不是运行中的进程映像，不会像 exe 那样把自己锁住。

    bat 必须 CRLF + UTF-8（chcp 65001 后生效），跟源码树的 start.bat 一致。
    """
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        'set "SCRIPT_DIR=%~dp0"',
        "",
        'if not exist "%SCRIPT_DIR%cdbg.exe" (',
        "    echo [cdbg] 找不到 cdbg.exe，请确认解压完整",
        "    pause",
        "    exit /b 1",
        ")",
        "",
        'if "%~1"=="" (',
        "    echo 用法：把 .c 源文件拖到本文件上，或直接运行 cdbg.exe",
        "    pause",
        "    exit /b 1",
        ")",
        "",
        '"%SCRIPT_DIR%cdbg.exe" %*',
        "if errorlevel 1 pause",
        "endlocal",
    ]
    (dist / "start.bat").write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))


def build(args) -> Path:
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = out / "_nuitka_work"
    if work.exists():
        shutil.rmtree(work)

    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--file-reference-choice=runtime",
        "--output-dir=" + str(work),
        "--include-data-dir=web=web",
        "--include-data-dir=shim=shim",
        "--include-data-dir=examples=examples",
        "--include-data-files=cdbg/gdb_probe.py=cdbg/gdb_probe.py",
        "--assume-yes-for-downloads",
    ]
    if args.msvc:
        cmd.append("--msvc=latest")
    cmd.append("cdbg.py")
    run(cmd)

    dist = work / "cdbg.dist"
    if not dist.is_dir():
        raise SystemExit("找不到构建产物: %s" % dist)

    # config 目录留空（首次运行自动生成 toolchain.conf）
    (dist / "config").mkdir(exist_ok=True)
    write_readme(dist)
    write_start_bat(dist)
    print("[build] dist 就绪: %s" % dist, flush=True)
    return dist


def make_zip(dist: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    top = "cdbg-windows-x64"
    if zip_path.exists():
        zip_path.unlink()

    # 打包前清理运行时残留：config 里的本机配置、log 日志。
    # CI 里这些本就不存在，但本地复用 dist 时需保证产物纯净。
    cfg = dist / "config" / "toolchain.conf"
    if cfg.exists():
        cfg.unlink()
    log_dir = dist / "log"
    if log_dir.is_dir():
        shutil.rmtree(log_dir)

    print("[zip] 打包 %s -> %s" % (dist, zip_path), flush=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # 显式放入空的 config 目录条目（首次运行会在这里生成 toolchain.conf）
        zf.writestr(top + "/config/", "")
        for path in sorted(dist.rglob("*")):
            if path.is_file():
                arc = str(Path(top) / path.relative_to(dist))
                zf.write(path, arc)
    print("[zip] 完成: %s (%d bytes)" % (zip_path, zip_path.stat().st_size), flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="build_out", help="构建输出根目录")
    p.add_argument("--zip", default=None, help="zip 输出路径（默认 <output>/cdbg-windows-x64.zip）")
    p.add_argument("--msvc", action="store_true", help="用系统 MSVC 编译（CI 里 Windows runner 已自带）")
    args = p.parse_args()

    dist = build(args)
    zip_path = Path(args.zip).resolve() if args.zip else Path(args.output).resolve() / "cdbg-windows-x64.zip"
    make_zip(dist, zip_path)


if __name__ == "__main__":
    main()
