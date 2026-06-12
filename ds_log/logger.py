import json
import os
import sys
import threading
from datetime import datetime, timezone

from .rotation import rotate_if_needed

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(PROJECT_ROOT, ".dsphantom.log")

_lock = threading.Lock()


def _write(level: str, source: str, msg: str, detail: dict = None):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "source": source,
        "msg": msg,
    }
    if detail:
        entry["detail"] = detail

    line = json.dumps(entry, ensure_ascii=False) + "\n"

    with _lock:
        rotate_if_needed(LOG_PATH)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    print(f"[{source}] {level}: {msg}", file=sys.stderr)


def info(source: str, msg: str, **detail):
    _write("INFO", source, msg, detail if detail else None)


def warn(source: str, msg: str, **detail):
    _write("WARN", source, msg, detail if detail else None)


def error(source: str, msg: str, **detail):
    _write("ERROR", source, msg, detail if detail else None)


def debug(source: str, msg: str, **detail):
    _write("DEBUG", source, msg, detail if detail else None)
