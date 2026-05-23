"""
music_sync_server.py — 接收浏览器传来的网易云播放状态，写入 .music_state.json
启动: python music_sync_server.py
端口: 8766，仅监听 localhost
"""
import json
import os
import sys
import re
from http.server import HTTPServer, BaseHTTPRequestHandler

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(PROJECT_ROOT, ".music_state.json")

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length)

        # /stopped 不需要 body
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
            raw_name = data.get("song_name", data.get("title", ""))
            # 清洗 ▶ 等播放图标前缀
            clean_name = re.sub(r'^[\s▶▷►♫♪🎵🎶🎧🔊🔉🔈🎤🎼🎹🥁🎸🎺🎻📻🎙🎚🎛]+', '', raw_name).strip()
            raw_artist = data.get("artist", "")
            clean_artist = raw_artist.strip().rstrip(" -–—")
            state = {
                "playing": data.get("playing", True),
                "paused": data.get("paused", False),
                "track_id": data.get("track_id", ""),
                "song_name": clean_name or raw_name,
                "artist": clean_artist,
                "album": data.get("album", ""),
                "album_pic": data.get("album_pic", ""),
                "duration_formatted": data.get("duration_formatted", ""),
                "source": "web",
            }
            try:
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                print(f">> {state.get('song_name','?')} -- {state.get('artist','?')}")
            except Exception as e:
                print(f"写文件失败: {e}", file=sys.stderr)
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
        pass  # 安静模式

if __name__ == "__main__":
    port = 8766
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"音乐同步服务已启动 → http://127.0.0.1:{port}")
    print("等待浏览器推送切歌...\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
        server.shutdown()
 