"""
clean_bark_sent.py - 一次性清洗 trigger.log 中 bark_sent 字段类型
将 String 类型替换为 Boolean 值
"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(PROJECT_ROOT, "trigger.log")

if not os.path.exists(LOG_PATH):
    print(f"trigger.log 不存在: {LOG_PATH}")
    sys.exit(0)

cleaned_lines = []
string_count = 0
bool_count = 0
other_count = 0

with open(LOG_PATH, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            cleaned_lines.append(line)
            continue

        bs = rec.get("bark_sent")
        if isinstance(bs, str):
            string_count += 1
            # 字符串内容 → 有内容即视为推送成功
            rec["bark_sent"] = True
            # 如果 bark_message 字段为空，将原 bark_sent 字符串移至 bark_message
            if not rec.get("bark_message"):
                rec["bark_message"] = bs
        elif isinstance(bs, bool):
            bool_count += 1
        elif bs is None:
            # None → False
            rec["bark_sent"] = False
            other_count += 1
        else:
            # 其他类型 → 尝试转 Boolean
            rec["bark_sent"] = bool(bs)
            other_count += 1

        cleaned_lines.append(json.dumps(rec, ensure_ascii=False))

# 写回
with open(LOG_PATH, "w", encoding="utf-8") as f:
    for line in cleaned_lines:
        f.write(line + "\n")

print(f"清洗完成: String→Boolean {string_count} 条, Boolean保持 {bool_count} 条, 其他修正 {other_count} 条")
