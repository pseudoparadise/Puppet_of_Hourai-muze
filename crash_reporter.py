"""
crash_reporter.py — 强制异常暴露模块
统一错误上报：终端打印完整 traceback（给机看）+ 弹窗错误消息（给沐泽看）。
所有模块在最顶层 import 即可覆盖 sys.excepthook。

隐私保护：console_crash.log 中的敏感类别内容（erotic 等）会被自动脱敏。
stderr 终端输出保留完整信息供调试，弹窗仅显示异常类型+消息。
"""
import sys
import os
import re
import traceback
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CRASH_LOG = os.path.join(PROJECT_ROOT, "console_crash.log")

_CENSORED_CATEGORIES = {"erotic"}

_CATEGORY_PATTERNS = [
    re.compile(rb"""["']category["']\s*[:=]\s*["'](erotic)["']"""),
    re.compile(rb"""\b(erotic)\b"""),
]

_CARD_CONTENT_BOUNDARY = re.compile(
    b"(\"content\"\\s*[:=]\\s*\")|(\"content\":\\s*\")|('content':\\s*')|"
    + "原话：".encode("utf-8"),
)


def _sanitize_for_log(msg: str) -> str:
    raw = msg.encode("utf-8", errors="replace")
    for cat_pat in _CATEGORY_PATTERNS:
        if not cat_pat.search(raw):
            continue
        parts = []
        last_end = 0
        for m in _CARD_CONTENT_BOUNDARY.finditer(raw):
            start = m.start()
            parts.append(raw[last_end:start])
            end = raw.find(b"\n", start + len(m.group(0)))
            if end < 0:
                end = len(raw)
            parts.append(b"[REDACTED]")
            last_end = end
        if parts:
            parts.append(raw[last_end:])
            raw = b"".join(parts)
            break
    return raw.decode("utf-8", errors="replace")


def _write_crash_log(msg: str):
    try:
        safe = _sanitize_for_log(msg)
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n=== {datetime.now().isoformat()} ===\n{safe}\n")
    except Exception:
        pass


def crash_print(e: Exception, context: str = ""):
    """终端打印完整错误 + 弹窗。stderr 保留完整信息，crash log 脱敏。"""
    tb = traceback.format_exc()
    header = f"[CRASH] {context}" if context else "[CRASH]"
    msg = f"{header}\n{type(e).__name__}: {e}\n\n{tb}"
    print(msg, file=sys.stderr)
    _write_crash_log(msg)

    try:
        import tkinter.messagebox as _mb
        _mb.showerror(
            f"异常 — {context}" if context else "异常",
            f"{type(e).__name__}: {e}\n\n详情已打印到终端并写入 crash log。"
        )
    except Exception:
        pass


def _global_excepthook(exc_type, exc_val, exc_tb):
    """全局未处理异常钩子 — 终端 + 弹窗 + 日志。"""
    tb_str = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
    msg = f"[UNHANDLED] {exc_type.__name__}: {exc_val}\n\n{tb_str}"
    print(msg, file=sys.stderr)
    _write_crash_log(msg)

    try:
        import tkinter.messagebox as _mb
        _mb.showerror(
            "未处理异常",
            f"{exc_type.__name__}: {exc_val}\n\n完整 traceback 已打印到终端。"
        )
    except Exception:
        pass

    sys.__excepthook__(exc_type, exc_val, exc_tb)


def install():
    """安装全局异常钩子。在任何模块开头调用一次即可覆盖全进程。"""
    sys.excepthook = _global_excepthook
    print("[crash_reporter] 全局异常钩子已安装", file=sys.stderr)
