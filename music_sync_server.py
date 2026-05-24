"""
music_sync_server.py — 接收浏览器推送的播放状态，抓歌词，写入 .music_state.json
"""
import json
import os
import re
import subprocess
import sys
import shutil
import time as _time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(PROJECT_ROOT, ".music_state.json")

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

def fetch_lyrics(song_name, artist):
    """用 neteasecli 搜歌 → 返回 (track_id, lyrics, corrected_name, corrected_artist)"""
    if not song_name:
        return None, [], song_name, artist

    query = song_name
    if artist:
        query = song_name + " " + artist
    result = _run("search", "track", query, "--limit", "1")
    if not result or not result.get("data", {}).get("tracks"):
        return None, [], song_name, artist

    track = result["data"]["tracks"][0]
    track_id = track["id"]
    corrected_name = track.get("name", song_name)
    corrected_artist = ", ".join(a["name"] for a in track.get("artists", [])) if track.get("artists") else artist

    # 获取歌词... (rest of function)

    # 获取歌词 — 优先翻译版 (tlyric)，其次原版 (lrc)
    lyric_result = _run("track", "lyric", str(track_id))
    lyrics = []
    if lyric_result and lyric_result.get("data"):
        ld = lyric_result["data"]
        raw = ld.get("tlyric", "") or ld.get("lrc", "") or ld.get("lyric", "")

        # 解析 [mm:ss.xx] 时间戳格式
        timed = []
        for line in raw.split("\n"):
            m = re.match(r'\[(\d+):(\d+)(?:[.:](\d+))?\](.*)', line)
            if m:
                mins, secs, text = m.group(1), m.group(2), m.group(4).strip()
                # 过滤 meta 行 (作词/作曲/编曲/by:)
                if text and not re.match(r'^(作词|作曲|编曲|混音|制作|by[:：])', text):
                    timed.append({
                        "seconds": int(mins)*60 + int(secs),
                        "time": f"{mins}:{secs}",
                        "text": text,
                    })

        if timed:
            lyrics = timed
        else:
            # 无时间戳的纯文本歌词
            text_lines = [l.strip() for l in raw.replace("\\n", "\n").split("\n")
                          if l.strip()
                          and not re.match(r'^(作词|作曲|编曲|混音|制作|by[:：])', l.strip())
                          and not l.strip().startswith("(") and not l.strip().startswith("（")]
            for i, text in enumerate(text_lines[:12]):
                lyrics.append({
                    "seconds": i * 5,
                    "time": f"0:{i*5:02d}",
                    "text": text,
                })

    return str(track_id), lyrics, corrected_name, corrected_artist


# 跨请求状态：记录当前歌曲和开始时间，用于歌词滚动
_current_song = None
_started_at = None


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global _current_song, _started_at
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length)

        if self.path == "/stopped":
            try:
                os.remove(STATE_FILE)
            except:
                pass
            print(">> 播放停止")
            self._reply(200, {"ok": True})
            return

        if not raw:
            self._reply(400, {"error": "empty body"})
            return

        body = None
        for enc in ["utf-8", "gbk", "gb2312"]:
            try:
                body = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if body is None:
            self._reply(400, {"error": "decode failed"})
            return
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._reply(400, {"error": "bad json"})
            return

        if self.path == "/nowplaying":
            global _current_song, _started_at

            raw_name = data.get("song_name", data.get("title", ""))
            clean_name = re.sub(r'^[\s▶▷►♫♪🎵🎶🎧🔊🔉🔈🎤🎼🎹]+', '', raw_name).strip()
            raw_artist = data.get("artist", "").strip().rstrip(" -–—")
            track_id = data.get("track_id", "")

            # 检测切歌：歌名变了就重置开始时间
            song_key = f"{clean_name}|{raw_artist}"
            now_ts = datetime.now(timezone.utc).isoformat()
            if song_key != _current_song:
                _current_song = song_key
                _started_at = now_ts

            # 搜歌词 + 补全歌手信息
            lyrics = []
            if not track_id:
                tid, lyrics, corrected_name, corrected_artist = fetch_lyrics(clean_name, raw_artist)
                if tid:
                    track_id = tid
                if corrected_name:
                    clean_name = corrected_name
                if corrected_artist:
                    raw_artist = corrected_artist

            state = {
                "playing": True,
                "paused": False,
                "track_id": track_id,
                "song_name": clean_name or raw_name,
                "artist": raw_artist,
                "album": data.get("album", ""),
                "album_pic": data.get("album_pic", ""),
                "duration_formatted": data.get("duration_formatted", ""),
                "lyrics": lyrics,
                "started_at": _started_at,
                "source": "web",
            }
            try:
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                lyric_count = len(lyrics)
                print(f">> {state.get('song_name','?')} -- {state.get('artist','?')} [{lyric_count} lyrics]")
            except Exception as e:
                print(f"write error: {e}", file=sys.stderr)
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()

    def _reply(self, code, data):
        self.send_response(code)
        self._cors()
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _cors(self):
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "POST, OPTIONS")
        self.send_header("access-control-allow-headers", "content-type")
        self.send_header("access-control-allow-private-network", "true")

    def log_message(self, fmt, *args):
        pass

if __name__ == "__main__":
    port = 8766
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"[music sync] http://127.0.0.1:{port}")
    print(f"[music sync] neteasecli: {NETEASECLI}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        server.shutdown()
