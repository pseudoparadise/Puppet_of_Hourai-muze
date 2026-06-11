"""
music_poll.py — 读网易云桌面客户端窗口标题 + neteasecli 搜歌词 → .music_state.json
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

# ── neteasecli ──
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

# ── 窗口标题抓取 ──
def _cloudmusic_pids():
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

def _get_window_title():
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
        if buf.value:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            result.append((buf.value, pid.value))
        return True
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
    pids = _cloudmusic_pids()
    for title, pid in result:
        if pid in pids:
            return title
    return None

TITLE_RE = re.compile(r'^(.+?)\s*[-—]\s*(.+)$')
_GARBAGE_TITLES = (
    "网易云音乐", "CloudMusic", "neteasecli",
    "下载网易云音乐", "下载", "iPhone", "iPad", "Mac", "Android", "WP", "PC版",
)

def parse_title(title: str):
    title = title.strip()
    m = TITLE_RE.match(title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return title, ""

# ── 歌词解析 ──
LRC_RE = re.compile(r'\[(\d+):(\d+)(?:[.:](\d+))?(?:-\d+)?\](.*)')
META_RE = re.compile(r'(作词|作曲|编曲|混音|制作|by[:：]|演唱|歌手|专辑|监制|出品'
                      r'|和声|封面|录音|混音师|母带|发行|推广|策划|统筹'
                      r'|吉他|贝斯|键盘|鼓手|琵琶|二胡|笛子|箫'
                      r'|文案|题字|PV|后期|调校|调教|曲绘|立绘|映像|映像制作)')
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
                    if text and not META_RE.search(text) and not _CREDIT_RE.match(text) \
                       and text not in ('·', '★', '☆', '●', '○'):
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


# ═══════════════════════════════════════════════════
#  SongContext — 当前歌曲状态机
# ═══════════════════════════════════════════════════
class SongContext:
    __slots__ = ('song_name', 'artist', 'track_id', 'lyrics', 'started_at', 'playing')

    def __init__(self, song_name, artist, track_id="", lyrics=None, started_at=None, playing=True):
        self.song_name = song_name
        self.artist = artist
        self.track_id = track_id or ""
        self.lyrics = lyrics or []
        self.started_at = started_at or datetime.now(timezone.utc).isoformat()
        self.playing = playing

    @property
    def key(self):
        return f"{self.song_name}|{self.artist}"

    def to_state(self):
        return {
            "playing": self.playing,
            "paused": not self.playing,
            "track_id": self.track_id,
            "song_name": self.song_name,
            "artist": self.artist,
            "album": "",
            "album_pic": "",
            "duration_formatted": "",
            "lyrics": self.lyrics,
            "started_at": self.started_at,
            "source": "desktop_client",
        }

    @classmethod
    def from_state(cls, state: dict):
        return cls(
            song_name=state.get("song_name", ""),
            artist=state.get("artist", ""),
            track_id=state.get("track_id", ""),
            lyrics=state.get("lyrics", []),
            started_at=state.get("started_at"),
            playing=state.get("playing", True),
        )


def write_state(ctx: SongContext):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(ctx.to_state(), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[music_poll] 写入失败: {e}")


def clear_state():
    """不再删除文件。写 idle 心跳，让 daemon 知道进程还活着。"""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "playing": False,
                "song_name": "",
                "artist": "",
                "lyrics": [],
                "heartbeat": datetime.now(timezone.utc).isoformat(),
            }, f, ensure_ascii=False)
    except Exception:
        pass

def _refresh_heartbeat():
    """空闲时每隔 ~30s 刷新心跳 mtime，防止 daemon 误判进程僵死。"""
    try:
        if os.path.exists(STATE_FILE):
            os.utime(STATE_FILE, None)  # touch: 更新 mtime 就够了
    except Exception:
        pass


def main():
    print(f"[music_poll] 启动 (neteasecli: {NETEASECLI})")

    # 空闲心跳计数器：每 ~6 轮 (30s) 刷新一次 idle 心跳 mtime
    _idle_ticks = 0

    # 启动时裁剪历史文件
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as hf:
                lines = hf.readlines()
            if len(lines) > 200:
                with open(HISTORY_FILE, "w", encoding="utf-8") as hf:
                    hf.writelines(lines[-200:])
                print(f"[music_poll] 历史文件裁剪: {len(lines)} → 200")
        except Exception:
            pass

    # 从旧 state 恢复 SongContext，避免重启丢 started_at
    current_song = None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                current_song = SongContext.from_state(json.load(f))
            print(f"[music_poll] 从旧状态恢复: {current_song.song_name} "
                  f"lyrics={len(current_song.lyrics)} started_at={current_song.started_at[:19]}")
        except Exception:
            pass

    while True:
        try:
            title = _get_window_title()
            if not title:
                if current_song is not None:
                    clear_state()
                    current_song = None
                    print("[music_poll] 已停止")
                _idle_ticks += 1
                if _idle_ticks % 6 == 0:
                    _refresh_heartbeat()
                time.sleep(5)
                continue

            song_name, artist = parse_title(title)
            if not song_name or song_name in _GARBAGE_TITLES or \
               any(g in song_name for g in ("下载网易云音乐", "iPhone、iPad")):
                if current_song is not None:
                    clear_state()
                    current_song = None
                _idle_ticks += 1
                if _idle_ticks % 6 == 0:
                    _refresh_heartbeat()
                time.sleep(5)
                continue
            if song_name.isascii() and " " not in song_name and len(song_name) < 4:
                if current_song is not None:
                    clear_state()
                    current_song = None
                _idle_ticks += 1
                if _idle_ticks % 6 == 0:
                    _refresh_heartbeat()
                time.sleep(5)
                continue

            # 从 neteasecli 拿播放状态
            playing = True
            try:
                r = _run("player", "status")
                if r and r.get("success"):
                    playing = r["data"].get("playing", True)
            except Exception:
                pass

            song_key = f"{song_name}|{artist}"

            if current_song is None or current_song.key != song_key:
                # ── 切歌或首次检测：拉歌词，重置 started_at ──
                print(f"[music_poll] 检测到歌曲: {song_name} — {artist}")
                tid, name, art, lyrics = fetch_lyrics("", song_name, artist)
                current_song = SongContext(
                    song_name=name, artist=art, track_id=tid or "",
                    lyrics=lyrics, started_at=datetime.now(timezone.utc).isoformat(),
                    playing=playing,
                )
                write_state(current_song)
                _idle_ticks = 0  # 有歌播时重置空闲心跳计数
                print(f"[music_poll] {'切歌' if current_song else '首发'}: "
                      f"{current_song.song_name} — {current_song.artist} [{len(lyrics)}句]")

                # 写历史
                try:
                    existing = []
                    if os.path.exists(HISTORY_FILE):
                        with open(HISTORY_FILE, "r", encoding="utf-8") as hf:
                            for line in hf:
                                existing.append(line.strip())
                    existing.append(json.dumps({
                        "time": current_song.started_at,
                        "song": current_song.song_name,
                        "artist": current_song.artist,
                        "lyrics_count": len(lyrics),
                    }, ensure_ascii=False))
                    with open(HISTORY_FILE, "w", encoding="utf-8") as hf:
                        hf.write("\n".join(existing[-200:]) + "\n")
                except Exception:
                    pass

            else:
                # ── 同首歌：只更新播放状态，不动歌词和 started_at ──
                if current_song.playing != playing:
                    current_song.playing = playing
                # 空歌词兜底：补拉一次
                if not current_song.lyrics:
                    print(f"[music_poll] 歌词为空，尝试补拉: {current_song.song_name}")
                    tid, name, art, lyrics = fetch_lyrics(
                        current_song.track_id, current_song.song_name, current_song.artist
                    )
                    if lyrics:
                        current_song.track_id = tid or current_song.track_id
                        current_song.lyrics = lyrics
                        print(f"[music_poll] 补拉成功: [{len(lyrics)}句]")
                write_state(current_song)
                _idle_ticks = 0  # 同首歌刷新后也重置空闲心跳计数

        except Exception as e:
            print(f"[music_poll] 异常: {e}")

        time.sleep(5)


if __name__ == "__main__":
    main()
