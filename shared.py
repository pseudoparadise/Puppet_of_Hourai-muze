"""
shared.py — 全局共享工具模块（DRY 重构）
所有重复的加载/解析/配置逻辑统一出口。
"""
import json, os, re, shutil
from datetime import datetime

# ── 项目根目录（shared.py 本身在根目录） ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════
# 1. JSON 安全加载（损坏时备份 + 返回默认值）
# ═══════════════════════════════════════════════════════════════

def load_json_safe(path: str, default=None, label: str = ""):
    """加载 JSON 文件。损坏时备份并返回 default。"""
    if default is None:
        default = []
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        backup = f"{path}.corrupted_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(path, backup)
        tag = f"[{label}] " if label else ""
        print(f"{tag}⚠ {os.path.basename(path)} 损坏({e.lineno}:{e.colno})，已备份至 {os.path.basename(backup)}，重建空列表")
        return default


# ═══════════════════════════════════════════════════════════════
# 2. LLM JSON 提取（json.loads 失败时用正则兜底）
# ═══════════════════════════════════════════════════════════════

def llm_to_json(raw: str, default=None):
    """
    从 LLM 输出中提取 JSON。先 json.loads，失败时正则匹配 {…}。
    raw 为空或全部失败时返回 default。
    """
    if not raw or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return default


# ═══════════════════════════════════════════════════════════════
# 3. 中文停用字符集（单例）
# ═══════════════════════════════════════════════════════════════

_ZH_STOP_CHARS = None

def zh_stop_chars() -> set:
    """中文特征提取停用字符集（全局单例）。"""
    global _ZH_STOP_CHARS
    if _ZH_STOP_CHARS is None:
        _ZH_STOP_CHARS = set(
            '的了是在我有他个这着就和也要可会你他们来到说去为上对得大子能过下一地出道自以时年看没那天家开小成把前还但只想中里用生种起知好些间因所如然后其最她它已当两从方实长更应什'
        )
    return _ZH_STOP_CHARS


def zh_extract_features(s: str) -> set:
    """从文本提取中文特征字 + 英文词干（去停用字后）。"""
    s = s.lower()
    chars = set(re.findall(r'[一-鿿]', s)) - zh_stop_chars()
    for t in re.findall(r'[a-z][a-z0-9]+', s):
        chars.add(t)
    return chars


# ═══════════════════════════════════════════════════════════════
# 5. state.json 读写
# ═══════════════════════════════════════════════════════════════

STATE_PATH = os.path.join(PROJECT_ROOT, "state.json")

def load_state() -> dict:
    return load_json_safe(STATE_PATH, default={}, label="state")

def save_state(state: dict):
    from delegate_tools import atomic_write_json
    atomic_write_json(STATE_PATH, state)


# ═══════════════════════════════════════════════════════════════
# 6. config.json 加载（带缓存）
# ═══════════════════════════════════════════════════════════════

_config_cache = None

def load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    config_path = os.path.join(PROJECT_ROOT, "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        _config_cache = json.load(f)
    return _config_cache


def invalidate_config_cache():
    """强制下次 load_config() 重新读取文件。"""
    global _config_cache
    _config_cache = None
