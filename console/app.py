import sys
import tkinter as tk
from tkinter import ttk

from .dashboard import DashboardTab

_TAB_SPECS = [
    ("总览", None),
    ("卡片管理", "card_mgr"),
    ("待办事项", "todo_mgr"),
    ("日记/Persona", "diary_tab"),
    ("Bark/日志", "log_tab"),
    ("工作日志", "work_log_tab"),
    ("卡片编辑", "edit_tab"),
    ("召回反馈", "feedback_tab"),
    ("人类写卡", "write_tab"),
    ("因果链", "chain_tab"),
]


class Console:
    def __init__(self, root):
        self.root = root
        self.root.title("phantom-trigger 控制台")
        self.root.geometry("1100x800")
        self.root.resizable(True, True)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self._tab_frames = {}
        self._next_load_idx = 1

        for idx, (title, _attr) in enumerate(_TAB_SPECS):
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=title)
            self._tab_frames[idx] = frame

        self.dashboard = DashboardTab(self._tab_frames[0])
        self.dashboard.pack(fill=tk.BOTH, expand=True)

        self.root.after(100, self._load_next_tab)

    def _load_next_tab(self):
        if self._next_load_idx >= len(_TAB_SPECS):
            return
        idx = self._next_load_idx
        self._next_load_idx += 1
        _attr = _TAB_SPECS[idx][1]
        frame = self._tab_frames[idx]
        try:
            if _attr == "card_mgr":
                from card_manager import CardManager
                self.card_mgr = CardManager(frame, standalone=False)
            elif _attr == "todo_mgr":
                from todo_manager import TodoManager
                self.todo_mgr = TodoManager(frame, standalone=False)
            elif _attr == "diary_tab":
                from .diary_persona import DiaryPersonaTab
                self.diary_tab = DiaryPersonaTab(frame)
                self.diary_tab.pack(fill=tk.BOTH, expand=True)
            elif _attr == "log_tab":
                from .bark_log import BarkLogTab
                self.log_tab = BarkLogTab(frame)
                self.log_tab.pack(fill=tk.BOTH, expand=True)
            elif _attr == "edit_tab":
                from .card_edit import CardEditTab
                self.edit_tab = CardEditTab(frame)
                self.edit_tab.pack(fill=tk.BOTH, expand=True)
            elif _attr == "feedback_tab":
                from .recall_feedback import RecallFeedbackTab
                self.feedback_tab = RecallFeedbackTab(frame)
                self.feedback_tab.pack(fill=tk.BOTH, expand=True)
            elif _attr == "write_tab":
                from .human_write import HumanWriteCardTab
                self.write_tab = HumanWriteCardTab(frame)
                self.write_tab.pack(fill=tk.BOTH, expand=True)
            elif _attr == "chain_tab":
                from .causal_chain import CausalChainTab
                self.chain_tab = CausalChainTab(frame)
                self.chain_tab.pack(fill=tk.BOTH, expand=True)
            elif _attr == "work_log_tab":
                from .work_log_tab import WorkLogTab
                self.work_log_tab = WorkLogTab(frame)
                self.work_log_tab.pack(fill=tk.BOTH, expand=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[console] Tab {idx} ({_attr}) 加载失败: {e}", file=sys.stderr)
        self.root.after(50, self._load_next_tab)


def run():
    from crash_reporter import install, crash_print
    install()
    root = tk.Tk()
    def _tk_error_handler(exc_type, exc_val, exc_tb):
        crash_print(exc_val, "console.py Tk回调异常")
    root.report_callback_exception = _tk_error_handler
    Console(root)
    root.mainloop()
