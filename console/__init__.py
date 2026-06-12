import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "memory"))
sys.path.insert(0, PROJECT_ROOT)

DB_PATH = os.path.join(PROJECT_ROOT, "memory", "cards.db")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")


def parse_content_parts(content_str: str) -> tuple:
    if not content_str:
        return "", ""
    raw, summary = "", content_str
    if content_str.startswith("原话："):
        parts = content_str.split(" | 概括：", 1)
        raw = parts[0][3:]
        summary = parts[1] if len(parts) > 1 else ""
    elif content_str.startswith("概括："):
        summary = content_str[3:]
    return raw, summary
