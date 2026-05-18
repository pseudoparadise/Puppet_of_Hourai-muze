"""
ghost_status.py — ghost-trigger 实时状态快照
用法：python ghost_status.py  或  双击 查看状态.bat
输出：状态快照_YYYYMMDD_HHMMSS.txt
"""
import os
import re
import sys
import json
import sqlite3
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(ROOT, f"状态快照_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")

# ── 内容排除：日志 + 向量/数据库 + 缓存 ──
EXCLUDE_CONTENT_NAMES = {
    "trigger.log", "trigger_error.log",    # 日志
    "vectors.faiss", "cards.db",            # 向量 / 数据库
    "id_map.json",                          # FAISS 映射表
    "chat_logs.json",                       # 对话日志
}
EXCLUDE_CONTENT_DIRS = {"__pycache__", ".git"}

# ── 文本文件扩展名白名单 ──
TEXT_EXTENSIONS = {
    ".py", ".json", ".yaml", ".yml", ".toml",
    ".txt", ".md", ".sql", ".bat", ".cfg",
    ".ini", ".env", ".csv", ".xml", ".html",
    ".css", ".js", ".ts", ".sh",
}

# ── 敏感字段名（大小写不敏感匹配） ──
SENSITIVE_KEYS = {
    "deepseek_api_key", "api_key", "ark_api_key", "access_key",
    "bark_device_key", "supabase_key", "supabase_url",
    "openai_api_key", "token", "secret", "password",
}

# ── 脱敏函数 ──
def mask_value(value: str) -> str:
    """保留首尾片段，中间替换为星号"""
    s = str(value)
    if len(s) <= 8:
        return s[:2] + "****" if len(s) > 4 else "****"
    return s[:4] + "****" + s[-4:]

def mask_sensitive(data, parent_key: str = ""):
    """递归脱敏 dict / list / str"""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            key_lower = k.lower().replace("-", "_").replace(" ", "_")
            if key_lower in SENSITIVE_KEYS or any(sk in key_lower for sk in SENSITIVE_KEYS):
                result[k] = mask_value(v)
            else:
                result[k] = mask_sensitive(v, k)
        return result
    elif isinstance(data, list):
        return [mask_sensitive(item, parent_key) for item in data]
    else:
        return data

def text_mask(content: str) -> str:
    """对纯文本中的 key=value / key: value 做脱敏"""
    for sk in SENSITIVE_KEYS:
        # JSON-style: "key": "value"
        pattern = re.compile(
            rf'("{re.escape(sk)}"\s*:\s*)"([^"]+)"',
            re.IGNORECASE
        )
        content = pattern.sub(
            lambda m: m.group(1) + '"' + mask_value(m.group(2)) + '"',
            content
        )
        # YAML-style: key: value
        pattern_yaml = re.compile(
            rf'(^|\n)(\s*{re.escape(sk)}\s*:\s*)(\S+)',
            re.IGNORECASE
        )
        content = pattern_yaml.sub(
            lambda m: m.group(1) + m.group(2) + mask_value(m.group(3)),
            content
        )
    return content


# ── 各模块 ──
def section(title: str) -> str:
    return f"\n{'='*60}\n  {title}\n{'='*60}\n"

def tree_walk(base: str, prefix: str = "") -> list:
    """递归目录树"""
    lines = []
    try:
        entries = sorted(os.listdir(base))
    except PermissionError:
        return [f"{prefix}[拒绝访问]"]
    for name in entries:
        full = os.path.join(base, name)
        if name.startswith(".") and name != ".gitignore":
            continue
        if name == "__pycache__":
            continue
        if os.path.isdir(full):
            lines.append(f"{prefix}{name}/")
            lines.extend(tree_walk(full, prefix + "  "))
        else:
            size = os.path.getsize(full)
            size_str = f"{size:,} B" if size < 1024 else f"{size/1024:.1f} KB"
            lines.append(f"{prefix}{name}  ({size_str})")
    return lines

def file_listing() -> str:
    """目录树"""
    lines = ["ghost-trigger/"]
    lines.extend(tree_walk(ROOT, "  "))
    return "\n".join(lines)

def config_snapshot() -> str:
    """读取并脱敏配置文件"""
    parts = []

    # config.json
    cfg_json = os.path.join(ROOT, "config.json")
    if os.path.exists(cfg_json):
        with open(cfg_json, "r", encoding="utf-8") as f:
            raw = f.read()
        try:
            data = json.loads(raw)
            safe = mask_sensitive(data)
            parts.append("── config.json（已脱敏）──")
            parts.append(json.dumps(safe, ensure_ascii=False, indent=2))
        except:
            parts.append("── config.json（纯文本脱敏）──")
            parts.append(text_mask(raw))

    # 毒点37修复：删除重复读取 config.json，volcengine 已在上面脱敏展示

    return "\n".join(parts)

def state_snapshot() -> str:
    """运行状态"""
    state_path = os.path.join(ROOT, "state.json")
    if not os.path.exists(state_path):
        return "state.json 不存在"
    with open(state_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    lines = []
    lines.append(f"最后用户消息: {data.get('last_user_message_time', 'N/A')}")
    lines.append(f"最后触发时间: {data.get('last_trigger_time', 'N/A')}")
    lines.append(f"冷却至:       {data.get('cooling_until', 'N/A')}")
    return "\n".join(lines)

def memory_stats() -> str:
    """记忆库统计"""
    db_path = os.path.join(ROOT, "memory", "cards.db")
    if not os.path.exists(db_path):
        return "cards.db 不存在"
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM cards")
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM cards WHERE review_status='final'")
        final = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM cards WHERE enabled_in_context=1")
        active = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM cards WHERE review_status='final' AND usage_count >= 5 AND importance >= 7")
        anchors = c.fetchone()[0]
        conn.close()

        lines = [
            f"卡片总数:     {total}",
            f"已定稿(final): {final}",
            f"当前活跃:     {active}",
            f"锚定候选:     {anchors} (usage≥5 & importance≥7)",
        ]

        # anchor_set.json
        anchor_path = os.path.join(ROOT, "memory", "anchor_set.json")
        if os.path.exists(anchor_path):
            with open(anchor_path, "r", encoding="utf-8") as f:
                ad = json.load(f)
            lines.append(f"锚定集合:     {ad.get('count', 0)} 张卡片 (更新于 {ad.get('updated_at', '?')[:19]})")

        return "\n".join(lines)
    except Exception as e:
        return f"DB 读取失败: {e}"

def recent_logs() -> str:
    """最近日志"""
    log_path = os.path.join(ROOT, "trigger.log")
    if not os.path.exists(log_path):
        return "trigger.log 不存在"
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    recent = lines[-30:] if len(lines) > 30 else lines
    return "".join(recent).rstrip()

def error_logs() -> str:
    """错误日志（最近 15 行）"""
    log_path = os.path.join(ROOT, "trigger_error.log")
    if not os.path.exists(log_path):
        return "trigger_error.log 不存在或为空"
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    recent = lines[-15:] if len(lines) > 15 else lines
    content = "".join(recent).rstrip()
    return content if content else "trigger_error.log 为空"


def _is_text_file(filepath: str) -> bool:
    """判断是否为文本文件"""
    ext = os.path.splitext(filepath)[1].lower()
    return ext in TEXT_EXTENSIONS


def dump_all_files() -> str:
    """递归遍历项目，输出所有文本文件内容（排除日志和向量文件）"""
    parts = []

    def walk(base: str, rel_prefix: str = ""):
        try:
            entries = sorted(os.listdir(base))
        except PermissionError:
            return
        for name in entries:
            full = os.path.join(base, name)
            if name in EXCLUDE_CONTENT_DIRS:
                continue
            if os.path.isdir(full):
                walk(full, os.path.join(rel_prefix, name))
            else:
                if name in EXCLUDE_CONTENT_NAMES:
                    continue
                if not _is_text_file(full):
                    continue
                rel = os.path.join(rel_prefix, name) if rel_prefix else name
                _emit_file(full, rel, parts)

    walk(ROOT)
    return "\n".join(parts)


def _emit_file(filepath: str, rel_path: str, parts: list):
    """读取单个文件内容并追加到 parts"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw = f.read()
    except UnicodeDecodeError:
        # 二进制文件，跳过
        return
    except Exception:
        return

    if not raw.strip():
        return  # 跳过空文件

    # 对 config.json / config.yaml 做脱敏
    fname = os.path.basename(filepath)
    if fname == "config.json":
        try:
            data = json.loads(raw)
            safe = mask_sensitive(data)
            content = json.dumps(safe, ensure_ascii=False, indent=2)
        except:
            content = text_mask(raw)
    else:
        content = raw

    parts.append(f"\n{'─'*50}")
    parts.append(f"  {rel_path}")
    parts.append(f"{'─'*50}")
    parts.append(content.rstrip())


# ── 主函数 ──
def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output_parts = []

    output_parts.append(f"ghost-trigger 实时状态快照")
    output_parts.append(f"生成时间: {now}")
    output_parts.append(f"项目路径: {ROOT}")

    output_parts.append(section("目录结构"))
    output_parts.append(file_listing())

    output_parts.append(section("全部文件内容"))
    output_parts.append(dump_all_files())

    output_parts.append(section("配置文件（已脱敏）"))
    output_parts.append(config_snapshot())

    output_parts.append(section("运行状态"))
    output_parts.append(state_snapshot())

    output_parts.append(section("记忆库统计"))
    output_parts.append(memory_stats())

    output_parts.append(section("最近日志 (trigger.log)"))
    output_parts.append(recent_logs())

    e_log = error_logs()
    if e_log and "不存在" not in e_log and "为空" not in e_log:
        output_parts.append(section("错误日志 (trigger_error.log)"))
        output_parts.append(e_log)

    content = "\n".join(output_parts)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"快照已生成: {OUTPUT}")
    print(f"大小: {len(content):,} 字符")
    return OUTPUT


if __name__ == "__main__":
    path = main()
    # 尝试用默认程序打开
    try:
        os.startfile(path)
    except:
        pass