import ctypes
import sys


def panic_popup(module: str, detail: str):
    msg = f"我不中了！\n\n{module} 反复崩溃，已达重试上限。\n{detail}\n\n请人类介入排查。"
    try:
        ctypes.windll.user32.MessageBoxW(
            0, msg, "DSphantom 守护进程 — 嘎巴了",
            0x00000010 | 0x00000030 | 0x00001000 | 0x00040000
        )
        print(f"[daemon] PANIC POPUP: {module} — {detail}", file=sys.stderr)
    except Exception as e:
        print(f"[daemon] 弹窗也失败了: {e}", file=sys.stderr)
        print(f"[daemon] PANIC: {module} — {detail}", file=sys.stderr)
