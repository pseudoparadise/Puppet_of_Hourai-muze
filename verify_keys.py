"""
verify_keys.py — 逐个测试坐标，人工标注正确音名
每个坐标弹 3 下，你听到后输入对应音名（如 C4 D5 等），不确定就按 Enter 跳过
"""
import json
import subprocess
import time

ADB = r"D:\Program Files\Netease\MuMu\nx_main\adb.exe"
DEVICE = "127.0.0.1:7555"
NOTE_NAMES = ['C4','D4','E4','F4','G4','A4','B4','C5','D5','E5','F5','G5','A5','B5','C6']

# 从 sky_keys.json 读原始物理坐标
with open("sky_keys.json", "r") as f:
    physical = json.load(f)

# 拿回原始 getevent 记录的坐标（按 tap 顺序）
# 重新从 touch_log 解析
import re
with open("touch_log.txt", "r") as f:
    text = f.read()

taps = []
cur = None
for line in text.split('\n'):
    if 'BTN_TOUCH' in line and 'DOWN' in line:
        if cur and cur.get('x') is not None:
            taps.append((cur['x'], cur['y']))
        cur = {'x': None, 'y': None}
    mx = re.search(r'ABS_MT_POSITION_X\s+([0-9a-fA-F]+)', line)
    my = re.search(r'ABS_MT_POSITION_Y\s+([0-9a-fA-F]+)', line)
    if mx and cur: cur['x'] = int(mx.group(1), 16)
    if my and cur: cur['y'] = int(my.group(1), 16)
if cur and cur.get('x') is not None:
    taps.append((cur['x'], cur['y']))

print(f"共 {len(taps)} 个坐标点，逐个验证...")
print("听到音后输入正确音名（C4~C6），不确定直接 Enter\n")

mapping = {}

for i, (px, py) in enumerate(taps):
    # 用当前 _load_keys 逻辑（ROTATION_90: x'=py, y'=900-px）
    lx, ly = py, 900 - px
    print(f"[{i+1}/{len(taps)}] 物理({px},{py}) 逻辑({lx},{ly})")
    for _ in range(3):
        subprocess.run([ADB, "-s", DEVICE, "shell", "input", "tap", str(lx), str(ly)], capture_output=True)
        time.sleep(0.4)

    label = input("  → 音名: ").strip().upper()
    if label in NOTE_NAMES:
        mapping[label] = [lx, ly]
        print(f"     ✓ {label}\n")
    elif label:
        # 可能输入了不标准的名字，仍然记录
        mapping[label] = [lx, ly]
        print(f"     ? {label}\n")
    else:
        print(f"     跳过\n")

print("\n" + "=" * 40)
print("验证完成！")
print(json.dumps(mapping, indent=2))

with open("sky_keys.json", "w", encoding="utf-8") as f:
    json.dump(mapping, f, indent=2)
print("\n已保存到 sky_keys.json")
