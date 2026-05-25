"""
midi_player.py — ADB 触摸坐标弹光遇乐器
用法: python midi_player.py <midi文件路径>  [--transpose N]
      python midi_player.py --quick "C4:200 D4:200 E4:400"
      python midi_player.py --daemon   (后台轮询 sky_cmd.json)
"""
import json
import os
import sys
import time
import threading
import subprocess

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CMD_FILE = os.path.join(PROJECT_ROOT, "sky_cmd.json")
KEYS_FILE = os.path.join(PROJECT_ROOT, "sky_keys.json")
ADB = r"D:\Program Files\Netease\MuMu\nx_main\adb.exe"
DEVICE = "127.0.0.1:7555"

# MIDI note number → note name
MIDI_TO_NAME = {
    60: 'C4', 62: 'D4', 64: 'E4', 65: 'F4', 67: 'G4',
    69: 'A4', 71: 'B4',
    72: 'C5', 74: 'D5', 76: 'E5', 77: 'F5', 79: 'G5',
    81: 'A5', 83: 'B5', 84: 'C6',
}
NAME_TO_MIDI = {v: k for k, v in MIDI_TO_NAME.items()}


def _load_keys():
    if not os.path.exists(KEYS_FILE):
        print(f"[midi_player] sky_keys.json 不存在，请先运行 calibrate_sky.py")
        return {}
    with open(KEYS_FILE, "r", encoding="utf-8") as f:
        coords = json.load(f)

    # 如果 max X > 1000，已是逻辑坐标 (1600x900)，直接使用
    if coords:
        max_x = max(c[0] for c in coords.values())
        if max_x > 1000:
            return coords

    # getevent 物理坐标 (900x1600) → 旋转到逻辑坐标
    # ROTATION_90: x'=y, y'=900-x
    r = subprocess.run(
        [ADB, "-s", DEVICE, "shell", "dumpsys", "window"],
        capture_output=True, text=True, timeout=5
    )
    if "mRotation=ROTATION_90" in r.stdout or "ROTATION_90" in r.stdout:
        rotated = {}
        for note, coord in coords.items():
            px, py = coord[0], coord[1]
            rotated[note] = [py, 900 - px]
        return rotated
    return coords


def _adb_tap(x: int, y: int, duration_ms: int = 80):
    """ADB 触摸按下 duration_ms 毫秒。"""
    subprocess.run(
        [ADB, "-s", DEVICE, "shell", "input", "swipe",
         str(x), str(y), str(x), str(y), str(duration_ms)],
        capture_output=True, timeout=5
    )


def _adb_connect():
    r = subprocess.run([ADB, "devices"], capture_output=True, text=True)
    if DEVICE not in r.stdout:
        subprocess.run([ADB, "connect", DEVICE], capture_output=True)


def play_notes(notes: list, bpm: int = 100):
    """直接弹奏。notes: [(note_name, duration_ms), ...]"""
    keys = _load_keys()
    if not keys:
        return
    _adb_connect()

    gap_ms = max(int(60000 / bpm / 4), 40)
    print(f"[midi_player] ADB 弹奏: {len(notes)} 个音符, BPM={bpm}")

    for note_name, dur_ms in notes:
        coord = keys.get(note_name.upper())
        if coord is None:
            print(f"  ? {note_name}")
            continue
        press_ms = max(dur_ms - gap_ms, 60)
        x, y = coord[0], coord[1]
        _adb_tap(x, y, press_ms)
        time.sleep(gap_ms / 1000.0)

    print("[midi_player] 弹奏完成")


def play_midi(path: str, transpose: int = 0):
    try:
        import mido
    except ImportError:
        print("[midi_player] pip install mido")
        return

    keys = _load_keys()
    if not keys:
        return
    _adb_connect()

    try:
        mid = mido.MidiFile(path)
    except Exception as e:
        print(f"[midi_player] 无法读取 MIDI: {e}")
        return

    tempo = 500000
    ticks_per_beat = mid.ticks_per_beat or 480
    events = []

    for track in mid.tracks:
        abs_time = 0
        for msg in track:
            abs_time += msg.time
            if msg.type == 'set_tempo':
                tempo = msg.tempo
            elif msg.type == 'note_on' and msg.velocity > 0:
                note = msg.note + transpose
                name = MIDI_TO_NAME.get(note)
                if name and name in keys:
                    sec = mido.tick2second(abs_time, ticks_per_beat, tempo)
                    events.append((sec, name, 'on'))
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                note = msg.note + transpose
                name = MIDI_TO_NAME.get(note)
                if name and name in keys:
                    sec = mido.tick2second(abs_time, ticks_per_beat, tempo)
                    events.append((sec, name, 'off'))

    events.sort(key=lambda x: x[0])
    print(f"[midi_player] {os.path.basename(path)}: {len(events)} 事件, "
          f"时长 {events[-1][0] if events else 0:.1f}s")

    start = time.time()
    held = {}
    for t, name, action in events:
        elapsed = time.time() - start
        if t > elapsed:
            time.sleep(t - elapsed)
        coord = keys[name]
        x, y = coord[0], coord[1]
        if action == 'on':
            # 按下并保持（release 之前一直按着）
            _adb_tap(x, y, 60)
        held.pop(name, None)

    # 释放所有还在按着的键
    print("[midi_player] 播放完成")


def quick_play(note_string: str, bpm: int = 100):
    notes = []
    for token in note_string.strip().split():
        if ':' in token:
            name, dur = token.split(':', 1)
            notes.append((name.strip(), int(dur)))
        else:
            notes.append((token.strip(), 250))
    play_notes(notes, bpm)


def daemon_loop():
    print(f"[midi_player] ADB 后台模式, 监听 {CMD_FILE}")
    print(f"[midi_player] 设备: {DEVICE}")
    _adb_connect()
    last_updated = 0
    while True:
        try:
            if os.path.exists(CMD_FILE):
                mtime = os.path.getmtime(CMD_FILE)
                if mtime > last_updated:
                    last_updated = mtime
                    with open(CMD_FILE, "r", encoding="utf-8") as f:
                        cmd = json.load(f)
                    if cmd.get("cmd") == "play" and cmd.get("midi"):
                        midi_path = os.path.join(PROJECT_ROOT, "midi", cmd["midi"])
                        transpose = cmd.get("transpose", 0)
                        print(f"[midi_player] 收到: {cmd['midi']} (transpose={transpose})")
                        threading.Thread(target=play_midi, args=(midi_path, transpose), daemon=True).start()
                    elif cmd.get("cmd") == "stop":
                        print("[midi_player] 停止")
        except Exception as e:
            print(f"[midi_player] 轮询异常: {e}")
        time.sleep(1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--daemon":
        daemon_loop()
    elif len(sys.argv) > 1 and sys.argv[1] == "--quick":
        quick_play(" ".join(sys.argv[2:]))
    elif len(sys.argv) > 1:
        path = sys.argv[1]
        transpose = 0
        if "--transpose" in sys.argv:
            idx = sys.argv.index("--transpose")
            if idx + 1 < len(sys.argv):
                transpose = int(sys.argv[idx + 1])
        play_midi(path, transpose)
    else:
        print(__doc__)
