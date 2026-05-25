"""
calibrate_sky.py — 后台录制光遇乐器键位坐标
用法:
  1. python calibrate_sky.py
  2. 切到光遇（弹出乐器）
  3. 从 C4 到 C6 按顺序轻点 15 个键
  4. 切回终端按 Enter
  5. 自动解析 sky_keys.json
"""
import subprocess
import sys
import re
import json
import os
import signal

ADB = r"D:\Program Files\Netease\MuMu\nx_main\adb.exe"
DEVICE = "127.0.0.1:7555"
EVENT_LOG = "/data/local/tmp/touch_events.txt"
NOTE_ORDER = [
    "C4", "D4", "E4", "F4", "G4", "A4", "B4",
    "C5", "D5", "E5", "F5", "G5", "A5", "B5", "C6",
]


def main():
    print("=" * 55)
    print("  光遇乐器键位校准 — 后台录制模式")
    print("=" * 55)
    print()
    print("  Step 1: 切到光遇，拿出乐器")
    print("  Step 2: 按 C4→D4→E4→...→C6 顺序")
    print("          每个键轻点一下，共 15 次")
    print("          (顺序不能乱，不要多按，不要划屏幕)")
    print("  Step 3: 切回这里按 Enter")
    print()

    subprocess.run([ADB, "-s", DEVICE, "shell", "rm", "-f", EVENT_LOG],
                   capture_output=True)

    # 后台启动 getevent 录制
    proc = subprocess.Popen(
        [ADB, "-s", DEVICE, "shell",
         "getevent -l > " + EVENT_LOG],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    input("  准备好了就按 Enter 开始 → 立刻切到光遇敲键！")
    print("  录制中...敲完 15 个键后切回来按 Enter 停止。")

    input("\n  按 Enter 停止录制...")

    # 杀 getevent 进程
    subprocess.run([ADB, "-s", DEVICE, "shell", "pkill", "-f", "getevent"],
                   capture_output=True)
    proc.kill()

    print("  解析触摸事件...")
    # 拉日志
    try:
        result = subprocess.run(
            [ADB, "-s", DEVICE, "pull", EVENT_LOG,
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "touch_log.txt")],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        pass

    # 从设备读日志到 stdout
    result = subprocess.run(
        [ADB, "-s", DEVICE, "shell", "cat", EVENT_LOG],
        capture_output=True, text=True, timeout=10
    )
    log_text = result.stdout

    if not log_text.strip():
        print("  X 没抓到任何触摸事件！")
        print("  可能原因：getevent 权限不足 / 设备不兼容")
        print("  替代方案：打开截图 sky_screen.png → 画图工具 → 标出每键坐标给我")
        return

    # 解析触摸按下事件
    touches = []
    for line in log_text.split("\n"):
        mx = re.search(r'ABS_MT_POSITION_X\s+([0-9a-fA-F]+)', line)
        my = re.search(r'ABS_MT_POSITION_Y\s+([0-9a-fA-F]+)', line)
        down = re.search(r'BTN_TOUCH\s+DOWN', line)
        if down:
            touches.append({"x": None, "y": None})
        if touches and mx:
            touches[-1]["x"] = int(mx.group(1), 16)
        if touches and my:
            touches[-1]["y"] = int(my.group(1), 16)

    # 过滤有效触摸 (有坐标的 DOWN 事件)
    coords = [(t["x"], t["y"]) for t in touches if t["x"] is not None and t["y"] is not None]

    print(f"  捕获到 {len(coords)} 次触摸")

    if len(coords) < 15:
        print(f"  ! 只有 {len(coords)} 次，需要至少 15 次。")
        print("  可能点了多次或漏了点。重来一次，严格按顺序 15 下。")
        # 即使不够也显示已有的
        if coords:
            print(f"\n  前 {len(coords)} 个坐标:")
            for i, (x, y) in enumerate(coords):
                label = NOTE_ORDER[i] if i < len(NOTE_ORDER) else "?"
                print(f"    {label}: ({x}, {y})")
        return

    # 取按顺序的 15 个
    mapping = {}
    for i, note in enumerate(NOTE_ORDER):
        if i < len(coords):
            mapping[note] = list(coords[i])

    print("\n" + "=" * 55)
    print("  校准结果:")
    print()
    print(json.dumps(mapping, indent=2))
    print()

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sky_keys.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)
    print(f"  已保存到 {out_path}")

    # 清理
    subprocess.run([ADB, "-s", DEVICE, "shell", "rm", "-f", EVENT_LOG],
                   capture_output=True)


if __name__ == "__main__":
    main()
