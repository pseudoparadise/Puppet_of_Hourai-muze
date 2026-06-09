"""
archive_diary.py — 日记/周收拢/工位日志归档
超过30天的文件搬到桌面 ghost-archive 文件夹，弹窗提醒拷U盘。
可由 polling_loop.py 调用，或手动运行：python archive_diary.py
"""
import os
import sys
import shutil
import json
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DESKTOP = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
ARCHIVE_DIR = os.path.join(DESKTOP, "ghost-archive")
CUTOFF_DAYS = 30


def get_cutoff_date():
    return datetime.now() - timedelta(days=CUTOFF_DAYS)


def is_old(filepath, cutoff):
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
        return mtime < cutoff
    except:
        # 从文件名提取日期
        basename = os.path.basename(filepath)
        # 匹配 YYYY-MM-DD 格式
        import re
        m = re.match(r'(\d{4})-(\d{2})-(\d{2})', basename)
        if m:
            try:
                file_date = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return file_date < cutoff
            except:
                pass
        return False


def count_old_files():
    """统计待归档文件数"""
    cutoff = get_cutoff_date()
    count = 0
    dirs_to_scan = [
        os.path.join(PROJECT_ROOT, "diary"),
        os.path.join(PROJECT_ROOT, "diary", "work"),
    ]
    for scan_dir in dirs_to_scan:
        if not os.path.isdir(scan_dir):
            continue
        for fname in os.listdir(scan_dir):
            fpath = os.path.join(scan_dir, fname)
            if os.path.isfile(fpath) and is_old(fpath, cutoff):
                count += 1
    return count


def archive_old_files(dry_run=False):
    """将超过30天的文件移到桌面归档。返回移动数量。"""
    cutoff = get_cutoff_date()
    moved = 0

    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    dirs_to_scan = [
        os.path.join(PROJECT_ROOT, "diary"),
        os.path.join(PROJECT_ROOT, "diary", "work"),
    ]

    archived_list = []

    for scan_dir in dirs_to_scan:
        if not os.path.isdir(scan_dir):
            continue
        for fname in os.listdir(scan_dir):
            fpath = os.path.join(scan_dir, fname)
            if not os.path.isfile(fpath):
                continue
            if not is_old(fpath, cutoff):
                continue

            dest = os.path.join(ARCHIVE_DIR, fname)
            # 避免覆盖
            if os.path.exists(dest):
                base, ext = os.path.splitext(fname)
                dest = os.path.join(ARCHIVE_DIR, f"{base}_{int(datetime.now().timestamp())}{ext}")

            if dry_run:
                print(f"[DRY RUN] 将移动: {fname}")
            else:
                shutil.move(fpath, dest)
                print(f"[归档] {fname} -> {ARCHIVE_DIR}")

            archived_list.append(fname)
            moved += 1

    return moved, archived_list


def show_popup(moved_count):
    """Windows 弹窗提醒"""
    if moved_count == 0:
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "ghost-archive",
            f"日记归档完成：{moved_count} 个文件已移至\n{ARCHIVE_DIR}\n\n请尽快拷贝到U盘。"
        )
        root.destroy()
    except Exception:
        print(f"[归档弹窗] {moved_count} 个文件已移至 {ARCHIVE_DIR}")


def send_bark_reminder(moved_count):
    """Bark 推送归档提醒"""
    config_path = os.path.join(PROJECT_ROOT, "config.json")
    if not os.path.exists(config_path):
        return
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        bark_key = config["global"].get("bark_device_key", "")
        if not bark_key or bark_key == "你的BarkKey填这里":
            return
        import requests
        from urllib.parse import quote
        msg = f"日记归档提醒：{moved_count}个文件超过30天，已移至桌面ghost-archive，请拷贝到U盘"
        requests.get(f"https://api.day.app/{bark_key}/{quote(msg)}", timeout=10)
    except Exception:
        pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="归档超30天的日记文件")
    parser.add_argument("--dry-run", action="store_true", help="只预览不移动")
    parser.add_argument("--count", action="store_true", help="只统计数量")
    args = parser.parse_args()

    if args.count:
        count = count_old_files()
        print(f"待归档: {count} 个文件")
        return

    cutoff = get_cutoff_date()
    print(f"归档截止日期: {cutoff.strftime('%Y-%m-%d')} (超过{CUTOFF_DAYS}天)")
    print(f"归档目标: {ARCHIVE_DIR}")

    if args.dry_run:
        moved, _ = archive_old_files(dry_run=True)
        print(f"\n[dry run] 共 {moved} 个文件待移动")
        return

    moved, archived = archive_old_files(dry_run=False)

    if moved > 0:
        print(f"\n归档完成: {moved} 个文件")
        show_popup(moved)
        send_bark_reminder(moved)

        # 写归档记录
        log_path = os.path.join(PROJECT_ROOT, "trigger.log")
        try:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps({
                    "timestamp": datetime.now().isoformat(),
                    "event": "archive_diary",
                    "count": moved,
                    "files": archived[:20]
                }, ensure_ascii=False) + "\n")
        except:
            pass
    else:
        print("没有需要归档的文件。")


if __name__ == "__main__":
    main()
