"""
music_poll.py — 读网易云桌面客户端窗口标题 + neteasecli 搜歌词 → .music_state.json
替代 Tampermonkey 油猴脚本 + music_sync_server HTTP 服务。
用法: python music_poll.py
"""
import json
import os
import re
import sys
import time
import subprocess
import shutil
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(PROJECT_ROOT, ".music_state.json")
HISTORY_FILE = os.path.join(PROJECT_ROOT, "memory", "music_history.jsonl")


def _find_neteasecli():
    found = shutil.which("neteasecli")
    if found:
        return found
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for name in ["neteasecli.exe", "neteasecli.cmd", "neteasecli"]:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    return "neteasecli"


NETEASECLI = _find_neteasecli()


def _run(*args, timeout=15):
    try:
        r = subprocess.run(
            [NETEASECLI, "--pretty"] + list(args),
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if r.returncode == 0:
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def _get_cloudmusic_window_title():
    """枚举 cloudmusic.exe 的可见窗口，返回 (title, pid) 或 None。
    桌面客户端在播放时窗口标题格式: 'SongName - ArtistName'"""
    user32 = ctypes.windll.user32
    result = []

    def enum_cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd) + 1
        if length <= 1:
            return True
        buf = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(hwnd, buf, length)
        title = buf.value
        if not title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        result.append((title, pid.value))
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

    for title, pid in result:
        if pid in _cloudmusic_pids():
            return title
    return None


def _cloudmusic_pids():
    """返回所有 cloudmusic.exe 的 PID 集合。"""
    pids = set()
    try:
        r = subprocess.run(
            ["tasklist", "/fi", "imagename eq cloudmusic.exe", "/fo", "csv"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        for line in r.stdout.split("\n"):
            parts = line.replace('"', '').split(",")
            if len(parts) >= 2 and parts[1].strip().isdigit():
                pids.add(int(parts[1].strip()))
    except Exception:
        pass
    return pids


TITLE_RE = re.compile(r'^(.+?)\s*[-—]\s*(.+)$')


def parse_title(title: str):
    """从窗口标题解析歌名和歌手。格式: 'SongName - ArtistName'"""
    title = title.strip()
    m = TITLE_RE.match(title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return title, ""


def player_status():
    """返回当前播放状态。用窗口标题拿歌名，neteasecli 拿播放状态（可选）。"""
    title = _get_cloudmusic_window_title()
    if not title:
        return None

    song_name, artist = parse_title(title)
    _GARBAGE_TITLES = (
        "网易云音乐", "CloudMusic", "neteasecli",
        "下载网易云音乐", "下载", "iPhone", "iPad", "Mac", "Android", "WP", "PC版",
    )
    if not song_name or song_name in _GARBAGE_TITLES or any(
        g in song_name for g in ("下载网易云音乐", "iPhone、iPad")
    ):
        return None
    # 歌名纯英文时至少有空格或合理长度，防止抓到推广语
    if song_name.isascii() and (" " not in song_name) and len(song_name) < 4:
        return None

    # 尝试从 neteasecli 拿播放状态
    playing = True
    try:
        r = _run("player", "status")
        if r and r.get("success"):
            playing = r["data"].get("playing", True)
    except Exception:
        pass

    return {
        "song_name": song_name,
        "artist": artist,
        "album": "",
        "album_pic": "",
        "duration_formatted": "",
        "track_id": "",
        "playing": playing,
    }


LRC_RE = re.compile(r'\[(\d+):(\d+)(?:[.:](\d+))?(?:-\d+)?\](.*)')
META_RE = re.compile(r'(作词|作曲|编曲|混音|制作|by[:：]|演唱|歌手|专辑|监制|出品|和声|封面|录音|混音师|母带|发行|推广|策划|统筹|吉他|贝斯|键盘|鼓手|琵琶|二胡|笛子|箫|文案|题字|PV|后期|调校|调教|曲绘|立绘|映像|映像制作)')
_CREDIT_RE = re.compile(r'.+[：:].+')


def fetch_lyrics(track_id: str, song_name: str = "", artist: str = ""):
    lyrics = []
    corrected_name = song_name
    corrected_artist = artist

    if not track_id and song_name:
        query = song_name
        if artist:
            query = song_name + " " + artist
        result = _run("search", "track", query, "--limit", "1")
        if result and result.get("data", {}).get("tracks"):
            track = result["data"]["tracks"][0]
            track_id = str(track["id"])
            corrected_name = track.get("name", song_name)
            corrected_artist = ", ".join(a["name"] for a in track.get("artists", [])) if track.get("artists") else artist

    if track_id:
        lr = _run("track", "lyric", track_id)
        if lr and lr.get("data"):
            ld = lr["data"]
            raw = ld.get("tlyric", "") or ld.get("lrc", "") or ld.get("lyric", "")
            for line in raw.split("\n"):
                m = LRC_RE.match(line)
                if m:
                    mins, secs, text = m.group(1), m.group(2), m.group(4).strip()
                    if text and not META_RE.search(text) and not _CREDIT_RE.match(text) and text not in ('·', '★', '☆', '●', '○'):
                        lyrics.append({
                            "seconds": int(mins) * 60 + int(secs),
                            "time": f"{mins}:{secs}",
                            "text": text,
                        })
            if not lyrics:
                text_lines = [l.strip() for l in raw.replace("\\n", "\n").split("\n")
                              if l.strip() and not META_RE.search(l.strip())
                              and not l.strip().startswith("(")
                              and not l.strip().startswith("（")]
                for i, text in enumerate(text_lines[:12]):
                    lyrics.append({"seconds": i * 5, "time": f"0:{i*5:02d}", "text": text})

    return track_id, corrected_name, corrected_artist, lyrics


def main():
    print(f"[music_poll] 启动 (neteasecli: {NETEASECLI})")
    current_song_key = None
    started_at = None

    while True:
        try:
            status = player_status()
            if not status:
                if os.path.exists(STATE_FILE):
                    try:
                        os.remove(STATE_FILE)
                    except Exception:
                        pass
                current_song_key = None
                started_at = None
                time.sleep(5)
                continue

            song_key = f"{status['song_name']}|{status['artist']}"
            now_ts = datetime.now(timezone.utc).isoformat()

            need_lyrics = (song_key != current_song_key)
            if not need_lyrics:
                # 同首歌但歌词为空 → 重试拉取
                try:
                    if os.path.exists(STATE_FILE):
                        with open(STATE_FILE, "r", encoding="utf-8") as _sf:
                            _old = json.load(_sf)
                        if not _old.get("lyrics"):
                            need_lyrics = True
                            print(f"[music_poll] 歌词为空，重试拉取: {status['song_name']}")
                except Exception:
                    pass

            if need_lyrics:
                current_song_key = song_key
                started_at = now_ts
                tid, name, artist, lyrics = fetch_lyrics(
                    status["track_id"], status["song_name"], status["artist"]
                )
                status["track_id"] = tid or status["track_id"]
                status["song_name"] = name
                status["artist"] = artist
                status["lyrics"] = lyrics
                if lyrics:
                    print(f"[music_poll] 切歌: {status['song_name']} — {status['artist']} [{len(lyrics)}句]")

            state = {
                "playing": status.get("playing", True),
                "paused": not status.get("playing", True),
                "track_id": status["track_id"],
                "song_name": status["song_name"],
                "artist": status["artist"],
                "album": status.get("album", ""),
                "album_pic": status.get("album_pic", ""),
                "duration_formatted": status.get("duration_formatted", ""),
                "lyrics": status.get("lyrics", []),
                "started_at": started_at,
                "source": "desktop_client",
            }

            try:
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[music_poll] 写入失败: {e}")

            try:
                with open(HISTORY_FILE, "a", encoding="utf-8") as hf:
                    hf.write(json.dumps({
                        "time": now_ts,
                        "song": state["song_name"],
                        "artist": state["artist"],
                        "lyrics_count": len(state["lyrics"]),
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass

        except Exception as e:
            print(f"[music_poll] 异常: {e}")

        time.sleep(5)


if __name__ == "__main__":
    main()
