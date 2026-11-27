#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""updater.py —— cdbg 更新检查与后台下载。

做什么
------
启动时在后台线程静默检查 GitHub Release 上的 ``versions.json``（CI 发布时
生成上传，一个文件同时带 version / 下载地址 / sha256 / 更新日志），比对
版本号后：

- 「按 Ctrl+C 结束调试会话」之后打印一行 ``本地版本 x 云端版本 y``；
- 有新版本时紧接着一行绿字提示；
- Ctrl+C 退出时问一句要不要后台下载（y / n / o，o = 今天不再提醒）；
- 答 y 拉起独立进程 ``--download-update`` 下载 zip，主程序立刻退出。

网络逻辑与 GCC 工具链下载（``compiler_scanner``）完全一致：
- 同一张加速节点表 ``GHPROXY_NODES``（前缀式反代）+ 同一个加速判断
  ``choose_acceleration(interactive=False)``（大陆默认开，
  ``CDBG_NO_ACCELERATE=1`` 关）；
- zip 下载直接复用 ``_download_once``（断点续传 / 416 / 206）与
  ``_sha256_file``，多节点轮询 + 3 轮重试，模式与 ``_download_package`` 同构；
- 检查用同一个 UA，节点轮询 + 直连兜底，全程静默（检查在主线程之外跑，
  打进度会搅乱主控制台）。

约束
----
- 提示只出现在退出路径，web 界面零改动；
- ``--headless`` 不检查（那是给 IDE 读的机器输出）；
- snooze 记在 ``config/update_snooze``（内容 = 今天日期，一天一档）；
- 下载的 zip、日志、ready 标记都落在用户目录（程序目录可能只读）：
  Windows ``%LOCALAPPDATA%\\cdbg\\update\\``，其他系统 ``~/.cdbg/update/``。

版本号来自 ``cdbg.__version__``：源码里是基线值，CI 打包前会把发布号
注入进去（见 .github/workflows/build.yml「注入发布版本号」步骤）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import date

from cdbg import __version__
from cdbg import compiler_scanner as cs

#: 项目根（放 cdbg.py 的地方）。cdbg/updater.py -> 上两级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 更新元数据地址。CI 每次发布都往 release 传 versions.json，
#: releases/latest 永远指向最新那份 —— 一个请求拿到全部所需字段。
#: 可用 CDBG_UPDATE_URL 覆盖（内网镜像 / 测试）。
DEFAULT_UPDATE_URL = (
    "https://github.com/zuoyexingchen-uw4r/VisualizeCprogramMemory"
    "/releases/latest/download/versions.json"
)

#: 今日不再提醒的标记文件（内容 = YYYY-MM-DD）
SNOOZE_FILE = os.path.join(PROJECT_ROOT, "config", "update_snooze")

#: 检查最多等这么久：打印必须发生在「按 Ctrl+C」那行之后、开浏览器之前，
#: 网络再慢也不能把启动卡住。一般检查在编译那几秒里早跑完了。
CHECK_TIMEOUT = 2.5

#: 每个地址的检查超时（秒）。小 JSON，不用等太久；节点轮询会兜底。
CHECK_URL_TIMEOUT = 5


# ---------------------------------------------------------------- 版本比较

def _parse_version(v) -> tuple:
    """``"v1.1.2"`` -> ``(1, 1, 2)``。非数字段按 0 算，段数不齐补 0。"""
    parts = []
    for seg in str(v or "").strip().lstrip("vV").split("."):
        m = re.match(r"\d+", seg)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts)


def _is_newer(cloud, local) -> bool:
    """云端版本是不是比本地新（元组比较，与 Ghost Downloader 同思路）。"""
    a, b = _parse_version(cloud), _parse_version(local)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


# ---------------------------------------------------------------- 路径与状态

def _update_dir() -> str:
    """下载 / 日志 / ready 标记的落点。程序目录可能只读，放用户目录最稳。"""
    if os.name == "nt":
        d = os.path.join(cs._local_appdata(), "cdbg", "update")
    else:
        d = os.path.join(os.path.expanduser("~"), ".cdbg", "update")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _log(msg: str) -> None:
    """往 download.log 追加一行。后台进程没有控制台，日志是唯一反馈。"""
    try:
        with open(os.path.join(_update_dir(), "download.log"), "a",
                  encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _snoozed_today() -> bool:
    try:
        with open(SNOOZE_FILE, encoding="utf-8") as fh:
            return fh.read().strip() == date.today().isoformat()
    except OSError:
        return False


def _snooze_today() -> None:
    try:
        os.makedirs(os.path.dirname(SNOOZE_FILE), exist_ok=True)
        with open(SNOOZE_FILE, "w", encoding="utf-8") as fh:
            fh.write(date.today().isoformat())
    except OSError:
        pass


def _read_ready() -> dict:
    """上次后台下载完成时写的标记；没有 / 坏了返回 {}。"""
    try:
        with open(os.path.join(_update_dir(), "ready.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_ready(info: dict) -> None:
    try:
        with open(os.path.join(_update_dir(), "ready.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(info, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- 网络

def _accel_nodes() -> tuple:
    """加速节点，判定逻辑与 GCC 下载同一个入口。

    非交互（检查线程 / 后台下载都没法问用户）：大陆默认启用，
    ``CDBG_NO_ACCELERATE=1`` 强制直连。``choose_acceleration`` 在非交互
    分支不打印任何东西，不会搅乱控制台。
    """
    try:
        use, _note = cs.choose_acceleration(interactive=False)
    except Exception:
        use = False
    return cs.GHPROXY_NODES if use else ()


def _fetch_meta() -> dict:
    """拉并解析 versions.json。节点轮询 + 直连兜底，全失败抛异常。

    地址组装与 ``_download_package`` 一致：加速节点前缀在前，直连最后。
    """
    url = (os.environ.get("CDBG_UPDATE_URL", "").strip() or DEFAULT_UPDATE_URL)
    urls = [n + url for n in _accel_nodes()] + [url]
    urls = list(dict.fromkeys(urls))
    last = "没有可用的地址"
    for u in urls:
        try:
            req = urllib.request.Request(
                u, headers={"User-Agent": cs.USER_AGENT})
            with urllib.request.urlopen(req, timeout=CHECK_URL_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("version"):
                return data
            last = "versions.json 里没有 version 字段"
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, exc)
    raise RuntimeError(last)


def _download_zip(url: str, dest: str, size: int, sha256: str,
                  nodes=()) -> tuple:
    """按节点顺序下载 + sha256 校验。与 ``_download_package`` 同构，
    只是包信息来自 versions.json 而不是写死的 TOOLCHAIN_PACKAGE。
    返回 (成功?, 错误说明)。"""
    urls = [n + url for n in nodes] + [url]
    urls = list(dict.fromkeys(urls))
    last = "没有可用的下载地址"
    for rnd in range(1, 4):
        for u in urls:
            host = re.sub(r"^https?://([^/]+).*$", r"\1", u)
            _log("第 %d 轮：%s" % (rnd, host))
            ok, err = cs._download_once(u, dest, size)
            if ok:
                if not sha256:
                    return True, ""
                _log("校验 sha256 …")
                actual = cs._sha256_file(dest)
                if actual.lower() == sha256.lower():
                    _log("sha256 对上了")
                    return True, ""
                last = "sha256 不匹配（得到 %s…）" % actual[:12]
                _log("%s，删掉重下" % last)
                try:
                    os.remove(dest)
                except OSError:
                    pass
                continue
            last = err
            _log("这个源不行：%s" % err)
        if rnd < 3:
            _log("全部源失败，3 秒后再来一轮（已下载的部分会接着下）")
            time.sleep(3)
    return False, last


# ---------------------------------------------------------------- 检查线程

class UpdateCheck:
    """一次后台检查：线程跑网络，结果放这儿，主流程按需等待。"""

    def __init__(self):
        self._evt = threading.Event()
        self.info = None     # versions.json 解析结果；失败为 None
        self.error = None    # 失败原因（只进日志/调试，不往控制台打）
        self._thread = threading.Thread(
            target=self._run, name="cdbg-update-check", daemon=True)

    def start(self) -> "UpdateCheck":
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self.info = _fetch_meta()
        except Exception as exc:
            self.error = str(exc)
        finally:
            self._evt.set()

    def wait(self, timeout: float = CHECK_TIMEOUT) -> bool:
        return self._evt.wait(timeout)

    @property
    def cloud_version(self) -> str:
        if not self.info:
            return ""
        return str(self.info.get("version") or "")

    @property
    def has_update(self) -> bool:
        cloud = self.cloud_version
        return bool(cloud) and _is_newer(cloud, __version__)


def start_check() -> UpdateCheck | None:
    """起后台检查线程。``CDBG_NO_UPDATE_CHECK=1`` 可整体关掉（测试/CI）。"""
    flag = os.environ.get("CDBG_NO_UPDATE_CHECK", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return None
    try:
        return UpdateCheck().start()
    except Exception:
        return None


# ---------------------------------------------------------------- 输出

def _green(text: str) -> str:
    """绿字。管道 / 重定向时原样返回（isatty 才上色）。

    Windows 上先 ``os.system("")`` 激活终端的 VT 处理，不然 ANSI 序列
    会原样漏出来变成乱码。
    """
    try:
        if not sys.stdout.isatty():
            return text
    except Exception:
        return text
    if os.name == "nt":
        try:
            os.system("")
        except Exception:
            pass
    return "\033[32m%s\033[0m" % text


def _changelog_lines(info: dict, max_lines: int = 10) -> list:
    """更新日志：截 max_lines 行，末尾附 release 页地址。"""
    text = str(info.get("changelog") or "").strip()
    lines = text.splitlines() if text else []
    out = lines[:max_lines]
    if not out:
        out = ["（本次更新没有附更新日志）"]
    if len(lines) > max_lines:
        out.append("……（日志还有 %d 行）" % (len(lines) - max_lines))
    notes = str(info.get("notes_url") or "").strip()
    if notes:
        out.append(notes)
    return out


def print_status(check: UpdateCheck | None) -> None:
    """在「按 Ctrl+C 结束调试会话」之后打印：版本对比行 + 绿色提示行。

    ``check`` 为 None（headless / 主动关掉）时什么都不打 —— 那是压根没检查；
    检查了但失败打「云端版本未知」。
    """
    if check is None:
        return
    local = __version__
    ready = _read_ready()
    check.wait(CHECK_TIMEOUT)
    cloud = check.cloud_version

    print("  本地版本%s 云端版本%s" % (local, cloud or "未知"))

    # 上次答 y 下来的包已经就绪：提示解压即可，别再劝下载一遍
    ready_ver = str(ready.get("version") or "")
    if ready_ver and _is_newer(ready_ver, local):
        print(_green("  更新包已下载：%s（退出后解压覆盖即可升级）"
                     % ready.get("path", "")))
        return
    if cloud and _is_newer(cloud, local) and not _snoozed_today():
        print(_green("  检测到新版本 %s，退出时可后台下载" % cloud))


def prompt_on_exit(check: UpdateCheck | None) -> None:
    """Ctrl+C 退出时的询问。无更新 / 包已就绪 / 今天免打扰 / 拿不到云端
    版本 → 直接返回；输入读不到（管道、EOF）也按「不下载」返回。

    文案是产品定死的三段：建议语 → 更新日志 → 选项行。
    """
    if check is None or not check.info:
        return
    cloud = check.cloud_version
    if not cloud or not _is_newer(cloud, __version__):
        return
    ready = _read_ready()
    if str(ready.get("version") or "") == cloud:
        return                      # 这个版本已经下好了，不用再问
    if _snoozed_today():
        return

    info = check.info
    print()
    print("  程序有更新，可以后台下载，建议下载以获取最新功能")
    for ln in _changelog_lines(info):
        print("  " + ln)
    print("  输入y开始后台下载并退出，输入n不下载并退出，"
          "输入o今日不再提醒并退出")
    while True:
        try:
            ans = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return                  # 再按一次 Ctrl+C = 不下载直接走
        if ans == "y":
            if _spawn_download_worker():
                print("  已在后台开始下载，日志：%s"
                      % os.path.join(_update_dir(), "download.log"))
            else:
                print("  启动后台下载失败，可手动访问 release 页面下载。")
            return
        if ans == "n":
            return
        if ans == "o":
            _snooze_today()
            print("  好，今天不再提醒。")
            return
        print("  请输入 y / n / o")


# ---------------------------------------------------------------- 后台下载

def _spawn_download_worker() -> bool:
    """拉起独立进程跑 ``--download-update``。主程序随即退出，下载继续。

    - 源码形态：``python cdbg.py --download-update``
    - Nuitka standalone：``cdbg.exe --download-update``（同目录没有 cdbg.py）
    Windows 上 DETACHED_PROCESS 脱离控制台，输出全部进 download.log。
    """
    script = os.path.join(PROJECT_ROOT, "cdbg.py")
    is_source = (os.path.isfile(script)
                 and os.path.basename(sys.executable).lower().startswith("python"))
    cmd = [sys.executable, script, "--download-update"] if is_source \
        else [sys.executable, "--download-update"]

    try:
        logf = open(os.path.join(_update_dir(), "download.log"), "a",
                    encoding="utf-8", errors="replace")
    except OSError:
        logf = subprocess.DEVNULL
    kw = dict(stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
              cwd=PROJECT_ROOT)
    if os.name == "nt":
        kw["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kw)
        return True
    except Exception:
        return False
    finally:
        if logf is not subprocess.DEVNULL:
            try:
                logf.close()        # 子进程已复制句柄，父进程这份可以关
            except Exception:
                pass


def _zip_name(url: str, version: str) -> str:
    """下载文件名：``cdbg-windows-x64.zip`` -> ``cdbg-windows-x64-1.1.2.zip``
    （按版本分开存，旧包不覆盖，回滚也有得用）。"""
    base = os.path.basename(re.sub(r"[?#].*$", "", url)) or "cdbg-update.zip"
    stem, ext = os.path.splitext(base)
    return "%s-%s%s" % (stem, version, ext)


def run_download_worker() -> int:
    """``--download-update``：独立进程入口。拉元数据 → 下载 → 校验 →
    写 ready 标记。所有反馈进 download.log（没有控制台）。"""
    try:
        _log("开始下载更新")
        info = _fetch_meta()
        version = str(info.get("version") or "")
        url = str(info.get("url") or "")
        if not version or not url:
            _log("versions.json 缺 version / url 字段")
            return 1
        dest = os.path.join(_update_dir(), _zip_name(url, version))
        _log("目标版本 %s -> %s" % (version, dest))
        ok, err = _download_zip(
            url, dest,
            int(info.get("size") or 0),
            str(info.get("sha256") or ""),
            _accel_nodes(),
        )
        if not ok:
            _log("下载失败：%s" % err)
            return 1
        _write_ready({
            "version": version,
            "path": dest,
            "sha256": str(info.get("sha256") or ""),
            "done_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        _log("下载完成并校验通过：%s" % dest)
        _log("解压覆盖程序目录即可完成升级。")
        return 0
    except Exception as exc:
        _log("异常：%s" % exc)
        return 1
