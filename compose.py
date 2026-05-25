"""
compose.py — 简谱转 MIDI
用法: python compose.py 歌名
      然后在 midi/ 目录下会生成同名 .mid 文件
      曲库在下面 SONG_BOOK 里加
"""
import os
import sys

try:
    import mido
    from mido import MidiFile, MidiTrack, Message
except ImportError:
    print("pip install mido")
    sys.exit(1)

MIDI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "midi")

# 音符名 → MIDI 音高 (C4=60)
NOTE_MAP = {
    'C4': 60, 'D4': 62, 'E4': 64, 'F4': 65, 'G4': 67, 'A4': 69, 'B4': 71,
    'C5': 72, 'D5': 74, 'E5': 76, 'F5': 77, 'G5': 79, 'A5': 81, 'B5': 83, 'C6': 84,
}

# ═══════════════════════════════════════════
#  曲库 — 格式: { "歌名": { "bpm": N, "notes": [(音名, 拍数), ...] } }
#  1 拍 = 四分音符，0.5 = 八分音符，2 = 二分音符
# ═══════════════════════════════════════════
# 简谱映射 (1=C 基准): 数字 + 八度后缀 → MIDI 音名
# 1=C4, 1·=C5, ·1=C3  (·在上=升八度, ·在下=降八度)
# 简谱记号: - = 延长一拍, _ = 减半拍, . = 附点
JIANPU_SCALE = {
    '1': 'C', '2': 'D', '3': 'E', '4': 'F', '5': 'G', '6': 'A', '7': 'B',
}


def jianpu_to_notes(jianpu_text: str, base_octave: int = 4) -> list:
    """简谱文本 → [(音名, 拍数), ...] 列表。
    格式: 空格分隔，如 "1 1 5 5 6 6 5 -"
    - = 延长一拍, _ = 八分音符, . = 附点, 数字后跟 octave 偏移
    """
    result = []
    tokens = jianpu_text.strip().split()
    duration = 1.0  # 默认四分音符 = 1 拍

    for token in tokens:
        dur = duration

        # 带附点: "1." = 1.5 拍
        if token.endswith('.'):
            dur *= 1.5
            token = token[:-1]

        # 检查延长/缩短标记
        if token == '-':
            # 延长记号: "-" = 再延长当前音 1 拍
            if result:
                prev_name, prev_dur = result[-1]
                result[-1] = (prev_name, prev_dur + 1.0)
            continue
        if token == '_':
            dur = 0.5
            continue

        # 提取数字（去掉八度标记）
        digit = token[0] if token[0] in '1234567' else None
        if digit is None:
            continue

        note_letter = JIANPU_SCALE.get(digit, 'C')
        oct_shift = 0

        # 检查八度标记: ·1 (低八度), 1· (高八度)
        if token.startswith('·'):
            oct_shift = -1
        elif token.endswith('·'):
            oct_shift = 1

        octave = base_octave + oct_shift
        note_name = f"{note_letter}{octave}"

        # 检查音符是否在可用范围内
        if note_name not in NOTE_MAP:
            note_name = f"{note_letter}{base_octave}"

        result.append((note_name, dur))
        duration = 1.0  # 重置

    return result


SONG_BOOK = {
    "小星星": {
        "bpm": 120,
        "notes": [
            ("C4", 1), ("C4", 1), ("G4", 1), ("G4", 1),
            ("A4", 1), ("A4", 1), ("G4", 2),
            ("F4", 1), ("F4", 1), ("E4", 1), ("E4", 1),
            ("D4", 1), ("D4", 1), ("C4", 2),
        ],
    },
    "欢乐颂": {
        "bpm": 140,
        "notes": [
            ("E4", 0.75), ("E4", 0.25), ("F4", 1), ("G4", 1),
            ("G4", 0.75), ("F4", 0.25), ("E4", 1), ("D4", 1),
            ("C4", 0.75), ("C4", 0.25), ("D4", 1), ("E4", 1),
            ("E4", 1.5), ("D4", 0.5), ("D4", 2),
            ("E4", 0.75), ("E4", 0.25), ("F4", 1), ("G4", 1),
            ("G4", 0.75), ("F4", 0.25), ("E4", 1), ("D4", 1),
            ("C4", 0.75), ("C4", 0.25), ("D4", 1), ("E4", 1),
            ("D4", 1.5), ("C4", 0.5), ("C4", 2),
        ],
    },
    "卡农": {
        "bpm": 100,
        "notes": [
            ("C4", 1), ("G4", 1), ("A4", 1), ("E4", 1),
            ("F4", 1), ("C5", 1), ("F4", 1), ("G4", 1),
            ("C4", 1), ("G4", 1), ("A4", 1), ("E4", 1),
            ("F4", 1), ("G4", 1), ("C4", 2),
            ("C4", 1), ("D4", 1), ("E4", 1), ("B4", 1),
            ("C5", 1), ("G4", 1), ("A4", 1), ("E4", 1),
            ("F4", 1), ("C5", 1), ("F4", 1), ("G4", 1),
            ("C4", 1), ("G4", 1), ("A4", 1), ("E4", 1),
            ("F4", 1), ("G4", 1), ("C4", 2),
        ],
    },
}


def notes_to_midi(song_name: str, notes: list, bpm: int = 120, path: str = None):
    """把 (音名, 拍数) 列表写成 MIDI 文件。"""
    mid = MidiFile()
    track = MidiTrack()
    mid.tracks.append(track)

    # 速度 (microseconds per beat)
    from mido import MetaMessage
    tempo = int(60_000_000 / bpm)
    track.append(MetaMessage('set_tempo', tempo=tempo, time=0))

    ticks_per_beat = mid.ticks_per_beat

    for note_name, beats in notes:
        midi_note = NOTE_MAP.get(note_name)
        if midi_note is None:
            print(f"  ? {note_name}")
            continue
        duration = int(ticks_per_beat * beats)
        # note_on → wait duration → note_off
        track.append(Message('note_on', note=midi_note, velocity=100, time=0))
        track.append(Message('note_off', note=midi_note, velocity=0, time=duration))

    if path is None:
        os.makedirs(MIDI_DIR, exist_ok=True)
        path = os.path.join(MIDI_DIR, f"{song_name}.mid")

    mid.save(path)
    print(f"[compose] {song_name} → {path} ({len(notes)} 音符, BPM={bpm})")
    return path


def list_songs():
    print("曲库:")
    for name, data in SONG_BOOK.items():
        print(f"  {name} ({len(data['notes'])} 音符, BPM={data['bpm']})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        list_songs()
        sys.exit(0)

    name = sys.argv[1]
    if name == "--list":
        list_songs()
    elif name == "--jianpu":
        # 从 stdin 读简谱: echo "1 1 5 5 6 6 5 -" | python compose.py --jianpu 歌名 [BPM]
        song_name = sys.argv[2] if len(sys.argv) > 2 else "jianpu"
        bpm = int(sys.argv[3]) if len(sys.argv) > 3 else 120
        jianpu_text = sys.stdin.read().strip()
        if not jianpu_text:
            print("用法: echo '1 1 5 5 6 6 5 -' | python compose.py --jianpu 歌名 120")
            sys.exit(1)
        notes = jianpu_to_notes(jianpu_text)
        notes_to_midi(song_name, notes, bpm)
        print(f"  → python midi_player.py midi/{song_name}.mid")
    elif name in SONG_BOOK:
        song = SONG_BOOK[name]
        notes_to_midi(name, song["notes"], song["bpm"])
        print(f"  → python midi_player.py midi/{name}.mid")
    else:
        print(f"曲库没有「{name}」。")
        list_songs()
        print("\n  简谱模式: echo '1 1 5 5' | python compose.py --jianpu 歌名 120")
