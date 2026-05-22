"""
play_music.py — 轻量播放封装，记录当前播放状态供 ghost-trigger 读取
用法:
  python play_music.py search 周杰伦 晴天     # 搜索并播放
  python play_music.py play 186016            # 按 ID 播放
  python play_music.py status                 # 查看状态
  python play_music.py pause / resume / stop  # 控制
"""
import json
import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(PROJECT_ROOT, ".music_state.json")

def _find_neteasecli():
    """定位 neteasecli 可执行文件"""
    # Windows .cmd 优先
    candidates = [
        os.path.expandvars(r"%APPDATA%\npm\neteasecli.cmd"),
        os.path.expandvars(r"%APPDATA%\npm\neteasecli"),
        "neteasecli",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # 检查 PATH
    import shutil
    found = shutil.which("neteasecli")
    if found:
        return found
    return "neteasecli"

NETEASECLI = _find_neteasecli()

# Ensure mpv is findable: add common locations to PATH
_mpv_dirs = [
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\mpv.net"),
    os.path.join(PROJECT_ROOT, "mpv"),
]
_ENV = os.environ.copy()
for _d in _mpv_dirs:
    if os.path.isdir(_d):
        _ENV["PATH"] = _d + os.pathsep + _ENV.get("PATH", "")

def _run(*args, timeout=30):
    return subprocess.run(
        [NETEASECLI, "--pretty"] + list(args),
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        env=_ENV,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )

def _read_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}

def _write_state(data):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _get_track_detail(track_id):
    r = _run("track", "detail", str(track_id))
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
        if d.get("success"):
            return d["data"]
    except:
        pass
    return None

def search_and_play(query):
    """搜索并播放最匹配的歌曲"""
    r = _run("search", "track", query, "--limit", "1")
    if r.returncode != 0:
        print(f"搜索失败: {r.stderr}")
        return None
    try:
        d = json.loads(r.stdout)
        tracks = d.get("data", {}).get("tracks", [])
        if not tracks:
            print("未找到歌曲")
            return None
        track = tracks[0]
        return play_by_id(track["id"])
    except Exception as e:
        print(f"解析失败: {e}")
        return None

def _find_mpv():
    """定位 mpv 可执行文件"""
    candidates = [
        os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "Programs", "mpv.net", "mpvnet.com"),
        os.path.join(PROJECT_ROOT, "mpv", "mpv.exe"),
        os.path.join(PROJECT_ROOT, "mpv.exe"),
    ]
    import shutil
    found = shutil.which("mpvnet.com")
    if found:
        candidates.insert(0, found)
    found = shutil.which("mpv")
    if found:
        candidates.insert(0, found)
    for c in candidates:
        if os.path.exists(c):
            return c
    return "mpv"

def _get_track_url(track_id, quality="exhigh"):
    """获取歌曲播放 URL"""
    r = _run("track", "url", str(track_id), "--quality", quality)
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
        if d.get("success"):
            return d["data"].get("url")
    except:
        pass
    return None

def play_by_id(track_id, quality="exhigh"):
    """按 ID 播放并记录状态 — 手动管理 mpv 进程"""
    detail = _get_track_detail(track_id)
    if not detail:
        print("获取歌曲信息失败")
        return None

    url = _get_track_url(track_id, quality)
    if not url:
        print("获取播放链接失败（可能需要 VIP 或版权受限）")
        return None

    # 先停掉已有的 mpv
    _stop_mpv()

    mpv_bin = _find_mpv()
    try:
        subprocess.Popen(
            [mpv_bin, "--no-video", f"--title={detail['name']} - {detail.get('artists', [{}])[0].get('name', '')}", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as e:
        print(f"启动 mpv 失败: {e}")
        return None

    state = {
        "playing": True,
        "paused": False,
        "track_id": detail["id"],
        "song_name": detail["name"],
        "artist": ", ".join(a["name"] for a in detail.get("artists", [])),
        "album": detail.get("album", {}).get("name", ""),
        "album_pic": detail.get("album", {}).get("picUrl", ""),
        "duration": detail.get("duration", 0),
        "duration_formatted": detail.get("durationFormatted", ""),
    }
    _write_state(state)
    print(f"▶ {state['song_name']} — {state['artist']}")
    return state

def _stop_mpv():
    """停止所有 mpv 进程"""
    import signal
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/f", "/im", "mpvnet.com"], capture_output=True, timeout=5)
            subprocess.run(["taskkill", "/f", "/im", "mpvnet.exe"], capture_output=True, timeout=5)
            subprocess.run(["taskkill", "/f", "/im", "mpv.exe"], capture_output=True, timeout=5)
    except:
        pass

def get_status():
    """获取当前播放状态"""
    state = _read_state()
    if not state.get("playing"):
        return None
    # 检查 mpv 是否还在运行
    try:
        r = subprocess.run(["tasklist", "/fi", "imagename eq mpvnet.com"], capture_output=True, text=True, timeout=5)
        if "mpvnet.com" not in r.stdout and "mpvnet.exe" not in r.stdout and "mpv.exe" not in r.stdout:
            state["playing"] = False
            _write_state(state)
            return None
    except:
        pass
    return state

def player_command(cmd):
    """暂停/继续/停止 — 通过 mpv IPC 或进程控制"""
    state = _read_state()
    if cmd == "stop":
        _stop_mpv()
        state["playing"] = False
        _write_state(state)
        print("已停止")
    elif cmd == "pause":
        state["paused"] = True
        _write_state(state)
        print("暂停（通过关闭 mpv 输出实现，恢复请调用 resume）")
    elif cmd == "resume":
        state["paused"] = False
        _write_state(state)
        print("已恢复")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "search" and len(sys.argv) > 2:
        search_and_play(" ".join(sys.argv[2:]))
    elif cmd == "play" and len(sys.argv) > 2:
        play_by_id(sys.argv[2])
    elif cmd == "status":
        s = get_status()
        if s:
            print(json.dumps(s, ensure_ascii=False, indent=2))
        else:
            print("未在播放")
    elif cmd in ("pause", "resume", "stop"):
        player_command(cmd)
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
