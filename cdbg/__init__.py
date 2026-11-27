"""cdbg — 面向 C 语言初学者的可视化调试器。

设计目标：把「程序内存视图」和「源代码」双向关联起来，让初学者直观看到
变量在哪、指针指向哪、递归时每层栈帧长什么样。
"""

#: 源码基线版本（= 当前最新 release）。CI 打包前会把发布号注入这里，
#: 所以本地跑源码看到的是基线值，装机包里看到的是真实发布号。
__version__ = "1.1.1"
__all__ = [
    "snapshot", "launcher", "gdb_session", "collector", "memmap", "curriculum",
    "toolchain_config", "compiler_scanner", "updater",
]
