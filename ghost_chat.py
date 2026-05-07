"""
ghost_chat.py - 桌面对话框快捷入口
直接调 trigger.py 的主循环
"""
import os
import sys

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(__file__))

from trigger import main

if __name__ == "__main__":
    main()