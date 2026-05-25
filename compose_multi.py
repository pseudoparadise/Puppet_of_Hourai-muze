"""
compose_multi.py — 多声部简谱 → 多轨 MIDI
用法: python compose_multi.py 歌名 BPM 谱面.txt
      或 cat 谱面.txt | python compose_multi.py 歌名 BPM
"""
import sys
import os
from mido import MidiFile, MidiTrack, Message, MetaMessage

MIDI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "midi")

# 简谱数字 → 音名 (C 大调基准)
JIANPU = {'1': 'C', '2': 'D', '3': 'E', '4': 'F', '5': 'G', '6': 'A', '7': 'B'}
NOTE_MAP = {
    'C4': 60, 'D4': 62, 'E4': 64, 'F4': 65, 'G4': 67, 'A4': 69, 'B4': 71,
    'C5': 72, 'D5': 74, 'E5': 76, 'F5': 77, 'G5': 79, 'A5': 81, 'B5': 83,
    'C6': 84,
}


def parse_jianpu_note(token: str, base_octave: int = 4) -> int | None:
    """'1' → 60, '1\'' → 72, '1\'\'' → 84, '-': hold, '': rest. 返回 MIDI 音符或 None."""
    token = token.strip()
    if not token or token == '-':
        return None  # rest 或 hold（由调用方处理）

    digit = token[0]
    if digit not in JIANPU:
        return None

    letter = JIANPU[digit]
    octave = base_octave + token.count("'")

    name = f"{letter}{octave}"
    return NOTE_MAP.get(name)


def parse_voice(lines: list, base_octave: int = 4) -> list[list]:
    """解析一个声部，返回 [[(midi_note_or_None, beats)], ...] 每小节一个列表。
    每个 token 是 1 拍，'-' 合并到前一个音符。"""
    measures = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        tokens = line.split()
        beats = []
        held_note = None
        held_beats = 0
        for tok in tokens:
            midi = parse_jianpu_note(tok, base_octave)
            if tok == '-':
                # 延长前一个音符
                held_beats += 1
            elif midi is not None:
                # 先输出之前积累的音符
                if held_note is not None and held_beats > 0:
                    beats.append((held_note, held_beats))
                held_note = midi
                held_beats = 1
            else:
                # 空拍 → 输出之前的音符，插入休止
                if held_note is not None and held_beats > 0:
                    beats.append((held_note, held_beats))
                held_note = None
                held_beats = 0
                beats.append((None, 1))  # 休止
        # 节末残留
        if held_note is not None and held_beats > 0:
            beats.append((held_note, held_beats))
        measures.append(beats)
    return measures


def make_midi(voices: list, bpm: int, path: str):
    """三声部 → 三轨 MIDI。voices = [高声部, 中声部, 低声部] 每个是 parse_voice 的输出。"""
    mid = MidiFile()
    tempo = int(60_000_000 / bpm)
    ticks_per_beat = mid.ticks_per_beat

    for voice_idx, measures in enumerate(voices):
        track = MidiTrack()
        mid.tracks.append(track)
        if voice_idx == 0:
            track.append(MetaMessage('set_tempo', tempo=tempo, time=0))

        pending_rest = 0
        for measure in measures:
            for midi_note, beats in measure:
                dur_ticks = int(ticks_per_beat * beats)
                if midi_note is not None:
                    track.append(Message('note_on', note=midi_note, velocity=90, time=pending_rest))
                    track.append(Message('note_off', note=midi_note, velocity=90, time=dur_ticks))
                    pending_rest = 0
                else:
                    pending_rest += dur_ticks

    mid.save(path)
    return path


def main():
    if len(sys.argv) < 2:
        print("用法: python compose_multi.py 歌名 BPM [谱面文件]")
        print("      若不指定文件，从 stdin 读取")
        sys.exit(1)

    song_name = sys.argv[1]
    bpm = int(sys.argv[2]) if len(sys.argv) > 2 else 120

    # 读谱面（管道输入或文件重定向）
    if len(sys.argv) > 3:
        with open(sys.argv[3], "r", encoding="utf-8") as f:
            text = f.read().strip()
    else:
        sys.stdin.reconfigure(encoding='utf-8')
        text = sys.stdin.read().strip()
    if not text:
        print("用法: python compose_multi.py 歌名 BPM < 谱面.txt")
        sys.exit(1)

    # 解析三个声部
    lines = text.split('\n')
    high_lines, mid_lines, low_lines = [], [], []
    current = None
    for line in lines:
        line = line.strip()
        if '高声部' in line:
            current = 'high'
            continue
        elif '中声部' in line:
            current = 'mid'
            continue
        elif '低声部' in line:
            current = 'low'
            continue
        elif line.startswith('小节') or line.startswith('第') or not line:
            continue
        if current == 'high':
            high_lines.append(line)
        elif current == 'mid':
            mid_lines.append(line)
        elif current == 'low':
            low_lines.append(line)

    # 统一 C4 基准，上标 ' 管八度 (1'=C5, 1''=C6)
    high_voice = parse_voice(high_lines, base_octave=4)
    mid_voice = parse_voice(mid_lines, base_octave=4)
    low_voice = parse_voice(low_lines, base_octave=4)

    voices = [high_voice, mid_voice, low_voice]
    path = os.path.join(MIDI_DIR, f"{song_name}.mid")
    make_midi(voices, bpm, path)

    total_notes = sum(
        sum(1 for n, _ in m if n is not None)
        for v in voices for m in v
    )
    print(f"[compose_multi] {song_name}: {len(high_voice)} 小节, {total_notes} 音符, BPM={bpm}")
    print(f"  → python midi_player.py midi/{song_name}.mid")


if __name__ == "__main__":
    main()
