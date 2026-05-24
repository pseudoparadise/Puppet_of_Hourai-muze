"""
music_context.py — 读取当前网易云播放状态，供 ghost-trigger 注入 prompt
来源优先级：本项目的 .music_state.json > neteasecli player status
"""
import json
import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
OWN_STATE = os.path.join(PROJECT_ROOT, ".music_state.json")

def _find_neteasecli():
    import shutil
    found = shutil.which("neteasecli")
    if found:
        return found
    candidates = [
        os.path.expandvars(r"%APPDATA%\npm\neteasecli.cmd"),
        os.path.expandvars(r"%APPDATA%\npm\neteasecli"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "neteasecli"

NETEASECLI = _find_neteasecli()


def _read_own_state():
    if not os.path.exists(OWN_STATE):
        return None
    try:
        with open(OWN_STATE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if state.get("playing"):
            return state
    except (json.JSONDecodeError, IOError):
        pass
    return None


def _query_neteasecli():
    try:
        r = subprocess.run(
            [NETEASECLI, "--pretty", "player", "status"],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        if data.get("success") and data["data"].get("playing"):
            return data["data"]
    except Exception:
        pass
    return None


def get_music_context() -> str:
    state = _read_own_state()
    if not state:
        # 降级：检查 mpv 是否在播（但可能不知道歌名）
        cli = _query_neteasecli()
        if cli:
            return f"【此刻她正在听的音乐 — 你可以自然提及，但不要刻意】\n  状态: {cli.get('message', '播放中')}\n\n"
        return ""

    lines = ["【此刻她正在听的音乐 — 仅当前一首，你可以自然提及，但不要刻意，不要编造其他歌曲】"]
    song = state.get('song_name', '未知').strip()
    artist = state.get('artist', '').strip()
    lines.append(f"  歌曲: {song if song else '未知'}")
    lines.append(f"  歌手: {artist if artist else '未知'}")
    album = state.get("album", "")
    if album:
        lines.append(f"  专辑: {album}")
    dur = state.get("duration_formatted", "")
    if dur:
        lines.append(f"  时长: {dur}")

    lyrics = state.get("lyrics", [])
    if lyrics:
        # 根据播放进度滚动：找到当前时间对应的歌词位置
        started = state.get("started_at", "")
        elapsed = 0
        if started:
            try:
                from datetime import datetime, timezone, timedelta
                tz_bj = timezone(timedelta(hours=8))
                start_dt = datetime.fromisoformat(started)
                elapsed = (datetime.now(timezone.utc) - start_dt).total_seconds()
            except Exception:
                pass

        # 找到 elapsed 秒后最近的 3 句歌词
        idx = 0
        for i, l in enumerate(lyrics):
            if l.get("seconds", 0) <= elapsed:
                idx = i
        window = lyrics[idx:idx+3]
        if len(window) < 3:
            window = lyrics[max(0, idx-2):idx+1]

        lines.append("  当前歌词:")
        for line in window:
            lines.append(f"    [{line.get('time', '?')}] {line.get('text', '')}")

    return "\n".join(lines) + "\n\n"


def is_playing() -> bool:
    return bool(_read_own_state() or _query_neteasecli())


if __name__ == "__main__":
    ctx = get_music_context()
    if ctx:
        print(ctx)
    else:
        print("[music_context] 当前未在播放")
