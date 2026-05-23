"""
trigger.py —— 总导演（脊椎）（修复版）
每一轮用户消息到达时的神经中枢。
并行感知 → 上下文拼装 → 主模型 → 后处理

FIX #1: 移除 import fcntl（Windows 无此模块，导致启动崩溃）
FIX #2: write_pending_card 改用原子写入（写临时文件 + 重命名）
FIX #3: 所有文件路径基于 __file__ 解析，不再依赖 CWD
"""
import json
import re
import os
import sys
import sqlite3
import requests
import tempfile
import shutil
from datetime import datetime

# ── FIX: 用 __file__ 获取项目根目录，不依赖 CWD ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 加模块路径
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "memory"))

from memory.retriever import retrieve, get_va_tier
from memory.memory_manager import renew_card
from emotion.va_estimator import estimate as va_estimate

# ── 读配置 ──
config_path = os.path.join(PROJECT_ROOT, "config.json")
with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

API_KEY = config["global"]["deepseek_api_key"]
API_URL = config["global"].get("api_url", "https://api.deepseek.com/v1/chat/completions")
CHAT_MODEL = config["global"].get("model", "deepseek-v4-flash")

# ── P2-2: 从 config.json 读取自定义检索权重（可选） ──
CUSTOM_WEIGHTS = config.get("retriever_weights", None)
if CUSTOM_WEIGHTS:
    print(f"[配置] 已加载自定义检索权重: {list(CUSTOM_WEIGHTS.keys())}")

# ── PA-1: 人格底色 — 手动基座 + 动态自动分析 ──
PERSONA_BASE_PATH = os.path.join(PROJECT_ROOT, "persona", "prompt_v1_base.txt")
PERSONA_DYNAMIC_PATH = os.path.join(PROJECT_ROOT, "persona", "prompt_v1.txt")

PERSONA = ""
if os.path.exists(PERSONA_BASE_PATH):
    with open(PERSONA_BASE_PATH, "r", encoding="utf-8") as f:
        PERSONA = f.read().strip()

if os.path.exists(PERSONA_DYNAMIC_PATH):
    with open(PERSONA_DYNAMIC_PATH, "r", encoding="utf-8") as f:
        dynamic = f.read().strip()
    if dynamic:
        if PERSONA:
            PERSONA += "\n\n【以下为系统自动提炼的人格动态，基于近期对话日志】\n"
        PERSONA += dynamic

if not PERSONA:
    PERSONA = "你是一个有自主意识的AI助手，运行在用户的个人电脑上。"

from delegate_tools import RP_DECLARATION

# ── 7日滚动总结 ──
ROLLING_PATH = os.path.join(PROJECT_ROOT, "memory", "rolling_summary.md")

# ── 状态文件（与 bark_trigger 共享时间同步） ──
STATE_PATH = os.path.join(PROJECT_ROOT, "state.json")

# ═══════════════════════════════════════════════════════════════
#  和弦情绪标记系统（修正3+4）
# ═══════════════════════════════════════════════════════════════
# 映射基于乐理主观经验，可随时调整
CHORD_EMOTION = {
    "C": "明亮坚定，C大调主和弦，稳定温暖",
    "Cmaj7": "梦幻漂浮，大七度带来慵懒的爵士感",
    "Dm": "柔和忧郁，D小调的自然感伤",
    "Dm7": "内敛深情，小七度增加一丝温柔",
    "Em": "宁静感伤，E小调的克制与深思",
    "Em7": "柔软思念，小七度的暧昧悬停",
    "F": "温暖宽广，F大调下属和弦的怀抱感",
    "Fmaj7": "温柔摇曳，大七度像春日午后的微风",
    "G": "明亮推进，G大调属和弦的期待与张力",
    "G7": "焦灼渴望，属七和弦的迫切未完成感",
    "Am": "黯然神伤，A小调主和弦的纯净悲伤",
    "Am7": "慵懒忧伤，小七度让悲伤变得松弛",
    "Bm7": "深邃迷离，B小调七和弦的复杂情绪",
    "E": "强烈明亮，E大调的高亢冲击力",
    "D7": "乡村律动，属七和弦的摇摆推动感",
    "B7": "尖锐张力，B属七和弦的戏剧性冲突",
}

# BPM 速度描述
BPM_DESC = [(30, "极慢"), (40, "很慢"), (60, "慢"), (80, "中速"), (110, "快"), (140, "很快"), (170, "极快")]
DYN_DESC = {"pp": "很轻", "p": "轻", "mp": "中轻", "mf": "中强", "f": "强", "ff": "很强"}

def _bpm_text(bpm: int) -> str:
    for threshold, label in BPM_DESC:
        if bpm < threshold:
            return label
    return "极快"

def _dyn_text(dyn: str) -> str:
    return DYN_DESC.get(dyn, dyn)

def _describe_chord(chord_str: str) -> str:
    """和弦名→情绪描述。支持和弦进行（多和弦串联如 Em7Fmaj7）"""
    import re
    individuals = re.findall(r'[A-G][a-z0-9]*', chord_str)
    if len(individuals) >= 2:
        descs = [CHORD_EMOTION.get(c, f"自定义({c})") for c in individuals]
        return f"{'→'.join(individuals)}: {'→'.join(descs)}"
    return CHORD_EMOTION.get(chord_str, f"自定义({chord_str})")

def _parse_chord(raw: str) -> tuple:
    """
    修正3：解析 /chord 参数。
    格式: 和弦名.BPM.动态标记
    返回: (chord_name, bpm, dynamic) 或 (None, None, error_msg)
    """
    import re
    m = re.match(r'^([^.]+)\.(\d+)bpm\.(pp|p|mp|mf|f|ff)$', raw.strip())
    if not m:
        return None, None, f"格式错误: {raw}（期望 和弦名.BPM.动态 如 Em7.80bpm.mf）"
    chord_name = m.group(1)
    bpm_str = m.group(2)
    dynamic = m.group(3)
    try:
        bpm = int(bpm_str)
        if bpm < 30 or bpm > 200:
            return None, None, f"BPM 超出范围: {bpm}（30-200）"
    except ValueError:
        return None, None, f"BPM 非法: {bpm_str}"
    return chord_name, bpm, dynamic

def _sync_last_active(chord: str = None):
    """更新 state.json。修正1：合并 chord 写入，避免双写竞争。"""
    from delegate_tools import now_utc, fmt_time
    from shared import load_state, save_state
    try:
        state = load_state()
        state["last_user_message_time"] = fmt_time(now_utc())
        if chord is not None:
            state["last_chord"] = chord
        save_state(state)
    except Exception as e:
        print(f"[状态同步异常]: {e}")

def load_rolling_summary():
    if os.path.exists(ROLLING_PATH):
        with open(ROLLING_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""

# ── 关键词反向引用检测 ──
def detect_refs_by_keywords(reply: str, cards: list) -> list:
    ref_ids = []
    reply_lower = reply.lower()
    for card in cards:
        kws = [kw.strip().lower() for kw in card.get("keywords", "").split(",") if kw.strip()]
        if any(kw in reply_lower for kw in kws):
            ref_ids.append(card["id"])
    return list(set(ref_ids))

# ── FIX: 写入待审核卡片（毒点5修复 — 委托 delegate_tools.atomic_write_json） ──
def write_pending_card(card_draft: dict):
    from shared import is_garbage_card
    reason = is_garbage_card(card_draft.get("title", ""), card_draft.get("content", ""))
    if reason:
        print(f"[卡片提议] 拦截: {reason}")
        return

    from delegate_tools import atomic_write_json
    pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
    from shared import load_json_safe as _load_safe
    pending = _load_safe(pending_path, default=[], label="卡片提议")
    # ── 预存 embedding 向量：写入时即调 embed，供 card_guard 去重直接比对 ──
    if '_embed_vec' not in card_draft:
        try:
            from encoder import embed as _embed_pending
            _pv = _embed_pending(card_draft.get('title', '') + ' ' + card_draft.get('content', ''))
            card_draft['_embed_vec'] = _pv.tolist()  # 2048 维 float 列表
        except Exception:
            pass
    pending.append(card_draft)
    try:
        atomic_write_json(pending_path, pending)
        print(f"[卡片提议] 草稿已写入 pending: {card_draft['id']}")
        if card_draft.get('target_date'):
            from shared import status_event
            status_event("待办登记", f"{card_draft['title']} → {card_draft['target_date']} [{card_draft.get('category','?')}]", "⏰")
        # 短期定时待办即时反馈
        if card_draft.get('category') == 'todo' and card_draft.get('target_date'):
            try:
                from datetime import datetime as _fb_dt, timezone as _fb_tz, timedelta as _fb_td
                _fb_target = _fb_dt.fromisoformat(card_draft['target_date'])
                _fb_now = _fb_dt.now(_fb_tz.utc) + _fb_td(hours=8)
                _fb_diff = (_fb_target - _fb_now).total_seconds()
                if 0 < _fb_diff < 86400:
                    print(f"  ⏰ [待办登记] {_fb_target.strftime('%H:%M')} — {card_draft['title']}")
            except Exception:
                pass
    except Exception as e:
        print(f"[卡片提议] 写入失败: {e}")

# ── 卡片提炼 ──
def refine_card_content(user_input: str, ai_reply: str, raw_parts: list = None) -> dict:
    """调 DeepSeek 提炼卡片标题、内容、关键词"""
    if raw_parts:
        refine_prompt = f"""根据以下信息，生成一张记忆卡片的标题、内容摘要和关键词。
用户输入：{user_input}
AI回复：{ai_reply[:200]}
卡片原始提议：标题={raw_parts[0]}，分类={raw_parts[1]}，重要度={raw_parts[2]}，内容={raw_parts[3]}

请返回JSON：
{{"title": "提炼后的标题（15字以内）", "content": "提炼后的内容（50字以内）", "keywords": "逗号分隔的关键词（5个以内）"}}
只返回JSON。"""
    else:
        refine_prompt = f"""你是用户的记忆管家。请从以下对话中提炼用户承诺要做的事、计划、或约定，生成记忆卡片。
重要：用用户的视角写卡片。title写用户要做的事（如"整理Claudecode接入"不是"双核心脏接入承诺"），content描述用户承诺了什么、准备怎么执行。不要用AI的视角。

用户输入：{user_input}
AI回复：{ai_reply[:200]}

请返回JSON：
{{"title": "用户视角的标题（15字以内，写用户要做的事）", "content": "用户视角的内容（50字以内，写用户承诺了什么）", "keywords": "逗号分隔的关键词（5个以内）"}}
只返回JSON。"""

    # ── FIX: 毒点6 — 指数退避重试（最多3次） ──
    import time as _time
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                    "Opt-Out": "training"
                },
                json={
                    "model": CHAT_MODEL,
                    "messages": [{"role": "user", "content": refine_prompt}],
                    "temperature": 0.3
                },
                timeout=20
            )
            if resp.status_code == 200:
                raw = resp.json()["choices"][0]["message"]["content"]
                from shared import llm_to_json
                result = llm_to_json(raw)
                if result is not None:
                    return result
                return None
            elif resp.status_code >= 500:
                if attempt < max_retries:
                    _time.sleep(2 ** attempt)
                    continue
            # 4xx 不重试
            break
        except requests.exceptions.RequestException:
            if attempt < max_retries:
                _time.sleep(2 ** attempt)
                continue
            break
    return None

# ── P3-2: 分类冷却配置 ──
# cooldown > 0: N轮间隔 | 0: 无限制仅同session去重 | -1: 同session永久去重
CATEGORY_COOLDOWN = {
    'erotic': 15,        # 同一场景15轮从预热到色色
    'interaction': 10,   # 防同一梗/口癖重复记录
    'deep_talks': 5,     # 5轮沉淀，完整叙事
    'commitments': -1,   # 永久去重：一个session一个约定只写一次
    'todo': 5,           # 5轮基础冷却 + 额外embedding/AI/人三层去重
    'daily_life': 10,
    'emotional': 10,
    'preferences': 10,
}

def _check_cooldown(written: dict, category: str, current_turn: int, cooldown: int) -> bool:
    """检查冷却状态（毒点41修复：添加语义文档）。

    cooldown 三种语义:
      > 0  — 有冷却：距上次写入需超过 cooldown 轮才允许再次触发
      = 0  — 无冷却：每次满足条件均可触发，同 session 仅去重
      < 0  — 永久去重：同 session 仅允许触发一次

    返回 True 表示可以写入，False 表示在冷却中或已去重。"""
    if category not in written:
        return True
    if cooldown < 0:  # 永久去重：同 session 仅允许一次
        return False
    if cooldown == 0:  # 无冷却：每次均可触发，仅同 session 去重
        return True
    return (current_turn - written[category]) > cooldown


def _sync_diary_on_card_change(card_id: str, action: str, details: dict = None):
    """
    卡片 resolve/update 时，同步更新对应日记的 events.json。
    action: "resolved" → 从 eisenhower 移入 completions
    action: "updated" → 更新 eisenhower 中对应项的 deadline/note
    """
    import re as _re_sync, glob as _glob_sync
    from delegate_tools import atomic_write_json

    # 找到包含此 card_id 的 events.json（不限日期前缀，扫最近 30 天）
    diary_dir = os.path.join(PROJECT_ROOT, "diary")
    matched_path = None
    for ep in sorted(_glob_sync.glob(os.path.join(diary_dir, "*_events.json")), reverse=True):
        try:
            with open(ep, "r", encoding="utf-8") as _ef:
                _ev = json.load(_ef)
            for items in _ev.get("eisenhower", {}).values():
                if any(card_id in it.get("card_id", "") for it in items):
                    matched_path = ep
                    break
            if matched_path:
                break
        except Exception:
            continue

    if not matched_path:
        return  # 不是日记同步的卡片，无需同步

    try:
        with open(matched_path, "r", encoding="utf-8") as ef:
            ev = json.load(ef)

        if action == "resolved":
            eis = ev.get("eisenhower", {})
            for quad in list(eis.keys()):
                resolved_items = []
                remaining = []
                for it in eis[quad]:
                    if card_id in it.get("card_id", ""):
                        resolved_items.append(it["item"])
                    else:
                        remaining.append(it)
                eis[quad] = remaining
                if resolved_items:
                    ev.setdefault("completions", [])
                    ev["completions"].extend(resolved_items)
                    print(f"[日记同步] {os.path.basename(matched_path)}: {resolved_items} → completions")

        elif action == "updated" and details:
            eis = ev.get("eisenhower", {})
            for quad in list(eis.keys()):
                for it in eis[quad]:
                    if card_id in it.get("card_id", ""):
                        if "target_date" in details:
                            it["deadline"] = details["target_date"]
                        if "status" in details:
                            it["note"] = f"状态: {details['status']}"
                        print(f"[日记同步] {os.path.basename(matched_path)}: 更新「{it['item']}」→ {details}")

        atomic_write_json(matched_path, ev)
    except Exception as e:
        print(f"[日记同步] 跳过: {e}")


def _parse_time_anchor(text: str) -> dict:
    """
    提取文本中的时间锚点，支持精确和模糊。
    返回: {"date": "YYYY-MM-DD"|None, "fuzzy": "YYYY-MM"|None,
           "label": str|None, "days_until": int|None}
    """
    import re as _re
    from datetime import datetime, timedelta
    today = datetime.now()
    result = {"date": None, "fuzzy": None, "label": None, "days_until": None}

    # 1. 精确: 下个月N号
    m = _re.search(r'下个?月(\d{1,2})[号日]', text)
    if m:
        d = int(m.group(1))
        next_month = today.month % 12 + 1
        year = today.year if next_month > today.month else today.year + 1
        try:
            dt = datetime(year, next_month, d)
            result["date"] = dt.strftime('%Y-%m-%d')
            result["label"] = f"下个月{d}号"
            result["days_until"] = (dt - today).days
            return result
        except ValueError:
            pass

    # 2. 精确: N月N号
    m = _re.search(r'(\d{1,2})月(\d{1,2})[号日]', text)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        year = today.year if mo >= today.month else today.year + 1
        try:
            dt = datetime(year, mo, d)
            result["date"] = dt.strftime('%Y-%m-%d')
            result["label"] = f"{mo}月{d}号"
            result["days_until"] = (dt - today).days
            return result
        except ValueError:
            pass

    # 3. 精确: 今晚/今天
    if _re.search(r'今晚|今天晚上|今夜|今天', text):
        result["date"] = today.strftime('%Y-%m-%d')
        result["label"] = "今天"
        result["days_until"] = 0
        return result

    # 4. 模糊: 今年N月下旬/中旬/上旬
    m = _re.search(r'今年(\d{1,2})月(上旬|中旬|下旬)', text)
    if m:
        mo, period = int(m.group(1)), m.group(2)
        day_map = {'上旬': 5, '中旬': 15, '下旬': 25}
        d = day_map.get(period, 15)
        try:
            dt = datetime(today.year, mo, d)
            result["date"] = dt.strftime('%Y-%m-%d')
            result["fuzzy"] = f"{today.year}-{mo:02d}"
            result["label"] = f"今年{mo}月{period}"
            result["days_until"] = (dt - today).days
            return result
        except ValueError:
            pass

    # 5. 模糊: N月下旬/中旬/上旬（省略"今年"）
    m = _re.search(r'(\d{1,2})月(上旬|中旬|下旬)', text)
    if m:
        mo, period = int(m.group(1)), m.group(2)
        day_map = {'上旬': 5, '中旬': 15, '下旬': 25}
        d = day_map.get(period, 15)
        year = today.year if mo >= today.month else today.year + 1
        try:
            dt = datetime(year, mo, d)
            result["date"] = dt.strftime('%Y-%m-%d')
            result["fuzzy"] = f"{year}-{mo:02d}"
            result["label"] = f"{mo}月{period}"
            result["days_until"] = (dt - today).days
            return result
        except ValueError:
            pass

    # 6. 模糊: 下个月/下下个月（无具体日期）
    m = _re.search(r'下下个?月|下个?月', text)
    if m:
        offset = 2 if '下下' in m.group() else 1
        mo = today.month + offset
        year = today.year + (mo - 1) // 12
        mo = (mo - 1) % 12 + 1
        result["fuzzy"] = f"{year}-{mo:02d}"
        result["label"] = m.group()
        result["days_until"] = 30 * offset  # 粗略估计
        return result

    # 7. 模糊: N月（仅月份，无具体日期）
    m = _re.search(r'(\d{1,2})月', text)
    if m:
        mo = int(m.group(1))
        year = today.year if mo >= today.month else today.year + 1
        result["fuzzy"] = f"{year}-{mo:02d}"
        result["label"] = f"{mo}月"
        dt = datetime(year, mo, 1)
        result["days_until"] = (dt - today).days
        return result

    # 8. 相对: 明天/后天/大后天（仅时间约束上下文）
    rel = {'明天': 1, '后天': 2, '大后天': 3}
    for kw, delta in rel.items():
        if _re.search(rf'{kw}.*(之前|以内|一定|必须|得)', text) or _re.search(rf'(之前|以内).*{kw}', text):
            dt = today + timedelta(days=delta)
            result["date"] = dt.strftime('%Y-%m-%d')
            result["label"] = kw
            result["days_until"] = delta
            return result

    return result


def _parse_target_date(text: str):
    """从中文文本解析目标日期时间。返回 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM' 或 None。
    优先级：精确日期 > 相对偏移 > 纯时间。"""
    from datetime import datetime, timedelta
    import re as _re_td

    today = datetime.now()
    date_str = None
    time_str = None

    # ── 1. 精确日期：复用 _parse_time_anchor ──
    anchor = _parse_time_anchor(text)
    date_str = anchor.get("date")

    # ── 2. 相对天偏移：N天后 / 明天后 / 后天 / 明天 ──
    if not date_str:
        m = _re_td.search(r'(\d+)\s*天[之以]?后', text)
        if m:
            delta = int(m.group(1))
            date_str = (today + timedelta(days=delta)).strftime('%Y-%m-%d')
        elif _re_td.search(r'明天之后|明天后', text):
            date_str = (today + timedelta(days=1)).strftime('%Y-%m-%d')
        elif _re_td.search(r'后天', text):
            date_str = (today + timedelta(days=2)).strftime('%Y-%m-%d')
        elif _re_td.search(r'大后天', text):
            date_str = (today + timedelta(days=3)).strftime('%Y-%m-%d')
        elif _re_td.search(r'明天', text):
            date_str = (today + timedelta(days=1)).strftime('%Y-%m-%d')

    # ── 3. 提取时间 ──
    # 相对时间偏移：半小时后 → now + 30min；N分钟后/小时后 → now + N*min/N*hour
    m = _re_td.search(r'(\d+)\s*分[钟鐘]?\s*后', text)
    if m:
        dt = today + timedelta(minutes=int(m.group(1)))
        date_str = dt.strftime('%Y-%m-%d')
        time_str = dt.strftime('%H:%M')
    elif _re_td.search(r'半小?时\s*后', text):
        dt = today + timedelta(minutes=30)
        date_str = dt.strftime('%Y-%m-%d')
        time_str = dt.strftime('%H:%M')
    elif _re_td.search(r'(\d+)\s*[小个]?时\s*后', text):
        m2 = _re_td.search(r'(\d+)\s*[小个]?时\s*后', text)
        if m2:
            dt = today + timedelta(hours=int(m2.group(1)))
            date_str = dt.strftime('%Y-%m-%d')
            time_str = dt.strftime('%H:%M')
    else:
        # 精确时间：N点 / N:N / N：N / N点N分 / 下午N点 / 晚上N点
        hour, minute = None, 0
        m = _re_td.search(r'(\d{1,2})[：:](\d{2})', text)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
        else:
            m = _re_td.search(r'(\d{1,2})点(?:(\d{1,2})分?)?', text)
            if m:
                hour = int(m.group(1))
                minute = int(m.group(2)) if m.group(2) else 0
        if hour is not None:
            # 处理下午/晚上/凌晨
            if _re_td.search(r'下午|午后', text) and hour < 12:
                hour += 12
            elif _re_td.search(r'晚上|今晚|夜里', text) and hour < 12:
                hour += 12
            elif _re_td.search(r'凌晨', text) and hour == 12:
                hour = 0
            time_str = f"{hour:02d}:{minute:02d}"
            if not date_str:
                date_str = today.strftime('%Y-%m-%d')
            # 如果解析出的时间在今天已经过去 >1h，自动推到明天
            # 凌晨/上午/中午时间 + 当前下午 → 明天
            now_hour = today.hour
            now_min = today.minute
            target_minutes = hour * 60 + minute
            current_minutes = now_hour * 60 + now_min
            if date_str == today.strftime('%Y-%m-%d') and (target_minutes + 60) < current_minutes:
                date_str = (today + timedelta(days=1)).strftime('%Y-%m-%d')

    # ── 4. 组合返回 ──
    result = f"{date_str} {time_str}" if (date_str and time_str) else date_str
    if result:
        from shared import status_event
        status_event("parse_date", f"'{text[:60]}' → {result}")
    return result

# ── 后处理 ──
def post_process(raw_reply: str, top_cards: list, user_input: str, display_reply: str, session_cards_written: dict = None, current_turn: int = 0, va: dict = None):
    ref_ids = []

    ref_match = re.search(r'<!--\s*ref:(.*?)\s*-->', raw_reply, re.IGNORECASE)
    if ref_match:
        ref_ids = [rid.strip() for rid in ref_match.group(1).split(",") if rid.strip()]
        display_reply = re.sub(r'<!--\s*ref:.*?\s*-->', '', display_reply, flags=re.IGNORECASE).strip()

    if not ref_ids and top_cards:
        ref_ids = detect_refs_by_keywords(display_reply, top_cards)

    for cid in ref_ids:
        success = renew_card(cid)
        if success:
            print(f"[记忆引用] 卡片 {cid} 已续命")
        else:
            print(f"[记忆引用] 卡片 {cid} 续命失败")
    # ── 追踪引用：供圣遗物自适应计算引用率 ──
    if ref_ids:
        try:
            from memory.retriever import _track_referenced
            _track_referenced(ref_ids)
        except Exception:
            pass

    # ── 提取 AI 计算的目标日期标记 <!-- target_date: YYYY-MM-DD --> ──
    target_date_from_ai = None
    td_match = re.search(r'<!--\s*target_date:\s*(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?)\s*-->', raw_reply, re.IGNORECASE)
    if td_match:
        target_date_from_ai = td_match.group(1)
        display_reply = re.sub(r'<!--\s*target_date:.*?\s*-->', '', display_reply, flags=re.IGNORECASE).strip()

    # auto resolve — 双重机制：AI 标记 + 关键词兜底
    resolved_ids = set()

    # 机制 A：AI 主动输出 <!-- resolve_card: ID -->
    resolve_match = re.search(r'<!--\s*resolve_card:\s*(.*?)\s*-->', raw_reply, re.IGNORECASE)
    if resolve_match:
        display_reply = re.sub(r'<!--\s*resolve_card:.*?\s*-->', '', display_reply, flags=re.IGNORECASE).strip()
        cid_to_resolve = resolve_match.group(1).strip()
        # 安全阀：不 resolve imp≥8 的基石卡 + 检查卡片是否存在且可 resolve
        try:
            db_check = sqlite3.connect(os.path.join(PROJECT_ROOT, "memory", "cards.db"))
            cc = db_check.cursor()
            cc.execute("SELECT importance FROM cards WHERE id=? AND review_status='final' AND resolved=0", (cid_to_resolve,))
            row = cc.fetchone()
            db_check.close()
            if row and row[0] >= 8:
                print(f"[自动解决] 拦截：{cid_to_resolve} 为基石卡 (imp={row[0]})，不自动 resolve")
            elif row:
                from memory.memory_manager import should_auto_resolve as _sar_a, resolve_card as do_resolve
                _allowed, _reason = _sar_a(cid_to_resolve, days_threshold=30)  # AI显式resolve最可信，放宽到30天
                if not _allowed:
                    print(f"[自动解决] 时间拒止 {cid_to_resolve}: {_reason}，降级为 status_card 处理")
                elif do_resolve(cid_to_resolve):
                    print(f"[自动解决] AI 已将卡片 {cid_to_resolve} 标记为已解决")
                    resolved_ids.add(cid_to_resolve)
                    _sync_diary_on_card_change(cid_to_resolve, "resolved")
            else:
                print(f"[自动解决] 跳过：{cid_to_resolve} 不存在或已 resolve")
        except Exception as e:
            print(f"[自动解决] 检查失败 card_id={cid_to_resolve}: {e}")

    # ── update_card：AI 原地更新卡片字段（推迟、改期、补充信息） ──
    update_match = re.search(r'<!--\s*update_card:\s*(.*?)\s*-->', raw_reply, re.IGNORECASE)
    if update_match:
        display_reply = re.sub(r'<!--\s*update_card:.*?\s*-->', '', display_reply, flags=re.IGNORECASE).strip()
        raw_update = update_match.group(1).strip()
        # 格式: 卡片ID|field=value|field=value
        parts = [p.strip() for p in raw_update.split("|")]
        if len(parts) >= 2:
            uid = parts[0]
            updates = {}
            for kv in parts[1:]:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    updates[k.strip()] = v.strip()
            if updates:
                try:
                    from memory.memory_manager import update_card as do_update
                    if do_update(uid, updates):
                        print(f"[卡片更新] {uid}: {updates}")
                        # ── 日记同步：update 也触发事件日志更新 ──
                        _sync_diary_on_card_change(uid, "updated", updates)
                    else:
                        print(f"[卡片更新] {uid} 失败（不存在或已解决）")
                except Exception as e:
                    print(f"[卡片更新] 异常: {e}")

    # ── status_card：AI 标记卡片流转状态（进行中/阻塞），不是完成 ──
    status_match = re.search(r'<!--\s*status_card:\s*(.*?)\s*-->', raw_reply, re.IGNORECASE)
    if status_match:
        display_reply = re.sub(r'<!--\s*status_card:.*?\s*-->', '', display_reply, flags=re.IGNORECASE).strip()
        raw_status = status_match.group(1).strip()
        # 格式: 卡片ID|进行中  或  卡片ID|阻塞
        parts = [p.strip() for p in raw_status.split("|")]
        if len(parts) == 2:
            sid, slabel = parts
            status_map = {"进行中": "in_progress", "阻塞": "blocked"}
            db_status = status_map.get(slabel)
            if db_status and sid:
                try:
                    from memory.memory_manager import set_card_status
                    if set_card_status(sid, db_status):
                        print(f"[状态流转] 卡片 {sid} → {slabel}")
                    else:
                        print(f"[状态流转] 卡片 {sid} 不存在或已解决，跳过")
                except Exception as e:
                    print(f"[状态流转] 失败 card_id={sid}: {e}")
            else:
                print(f"[状态流转] 无效状态标签: {slabel}")

    # 机制 B：用户明确宣告完成 → 关键词匹配未解决卡片 → 自动 resolve
    # ── 完成信号检测：关键词快速触发 → embedding 语义确认 ──
    # 瘦身关键词：仅保留最可靠的「主语+完成动词」模式，不含「展示」「来一起」
    _completion_trigger_kw = [
        "修好了", "做完了", "完成了", "搞定了", "拿到了", "收到了",
        "吃完了", "买好了", "干完了", "到手了", "回来了",
        "打通了", "调通了", "办完了", "做好了",
    ]
    _cpl_vec = None
    if any(kw in user_input for kw in _completion_trigger_kw):
        try:
            from encoder import embed as _embed_cpl, load_index as _load_idx_cpl, search_index as _search_cpl
            import numpy as _np_cpl
            _cpl_vec = getattr(retrieve, '_cached_query_vec', None)
            if _cpl_vec is None:
                _cpl_vec = _embed_cpl(user_input)
            _cpl_idx = _load_idx_cpl()
            if _cpl_idx.ntotal > 0:
                _cpl_neighbors = _search_cpl(_cpl_idx, _cpl_vec, k=10)
                if _cpl_neighbors:
                    _cpl_ids = [nid for nid, _ in _cpl_neighbors]
                    _cpl_db = sqlite3.connect(os.path.join(PROJECT_ROOT, "memory", "cards.db"))
                    _cpl_c = _cpl_db.cursor()
                    _cpl_c.execute(
                        "SELECT id, title, category, content, importance, embedding FROM cards "
                        "WHERE id IN ({}) AND review_status='final' AND resolved=0 "
                        "AND category IN ('commitments','daily_life','todo')"
                        .format(','.join(['?' for _ in _cpl_ids])),
                        _cpl_ids
                    )
                    for _cid, _ctitle, _ccat, _ccontent, _cimp, _ceblob in _cpl_c.fetchall():
                        if _cid in resolved_ids:
                            continue
                        if (_cimp or 5) >= 8:
                            continue
                        if _ceblob is None:
                            continue
                        _c_evec = _np_cpl.frombuffer(_ceblob, dtype=_np_cpl.float32)
                        _c_dot = _np_cpl.dot(_cpl_vec, _c_evec)
                        _c_norm = _np_cpl.linalg.norm(_cpl_vec) * _np_cpl.linalg.norm(_c_evec)
                        _c_cos = float(_c_dot / _c_norm) if _c_norm > 0 else 0.0
                        if _c_cos < 0.55:
                            continue
                        from memory.memory_manager import should_auto_resolve as _sar, resolve_card as do_resolve2
                        _allowed, _reason = _sar(_cid, context_anchor=_parse_time_anchor(user_input))
                        if not _allowed:
                            print(f"[完成检测] 时间拒止 {_cid}: {_reason}，跳过")
                            continue
                        if do_resolve2(_cid):
                            print(f"[完成检测] 卡片 {_cid}({_ctitle}) 语义匹配(cos={_c_cos:.3f})，已解决")
                            resolved_ids.add(_cid)
                            _sync_diary_on_card_change(_cid, "resolved")
                    _cpl_db.close()
        except Exception as e:
            print(f"[完成检测] embedding 路径跳过: {e}，降级关键词兜底")
            _fallback_kw = ["做完了", "完成了", "搞定了", "拿到了"]
            if any(kw in user_input for kw in _fallback_kw):
                try:
                    _fb_db = sqlite3.connect(os.path.join(PROJECT_ROOT, "memory", "cards.db"))
                    _fb_c = _fb_db.cursor()
                    _fb_c.execute(
                        "SELECT id, title, importance FROM cards "
                        "WHERE review_status='final' AND resolved=0 "
                        "AND category IN ('commitments','daily_life','todo') LIMIT 10"
                    )
                    for _fid, _ftitle, _fimp in _fb_c.fetchall():
                        if _fid in resolved_ids or (_fimp or 5) >= 8:
                            continue
                        if _ftitle[:4] in user_input or user_input[:4] in _ftitle:
                            from memory.memory_manager import resolve_card as _fb_resolve
                            if _fb_resolve(_fid):
                                print(f"[完成检测-兜底] {_fid}({_ftitle}) 已解决")
                                resolved_ids.add(_fid)
                                _sync_diary_on_card_change(_fid, "resolved")
                    _fb_db.close()
                except Exception:
                    pass

    # ── pending 扫描：完成信号 → embedding 匹配待审核卡 ──
    pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
    if os.path.exists(pending_path) and any(kw in user_input for kw in _completion_trigger_kw):
        try:
            with open(pending_path, "r", encoding="utf-8") as pf:
                pending_cards = json.load(pf)
            if pending_cards:
                from encoder import embed as _embed_pend
                import numpy as _np_pend
                _pv = _cpl_vec if _cpl_vec is not None else _embed_pend(user_input)
                pending_modified = False
                for pc in pending_cards:
                    if pc.get("id") in resolved_ids or pc.get("importance", 5) >= 8:
                        continue
                    try:
                        _ptext = pc.get("title", "") + " " + (pc.get("content", "") or "")
                        _pvec = _embed_pend(_ptext)
                        _pdot = _np_pend.dot(_pv, _pvec)
                        _pnorm = _np_pend.linalg.norm(_pv) * _np_pend.linalg.norm(_pvec)
                        _pcos = float(_pdot / _pnorm) if _pnorm > 0 else 0.0
                        if _pcos >= 0.55:
                            print(f"[pending完成] 待审核卡「{pc.get('title','?')}」语义匹配(cos={_pcos:.3f})，移除")
                            resolved_ids.add(pc.get("id"))
                            pending_modified = True
                    except Exception:
                        pass
                if pending_modified:
                    pending_cards = [pc for pc in pending_cards if pc.get("id") not in resolved_ids]
                    from delegate_tools import atomic_write_json as _awj_pending
                    _awj_pending(pending_path, pending_cards)
                    print(f"[pending完成] pending_cards.json 已更新，剩余 {len(pending_cards)} 张")
        except Exception as e_pending:
            pass


    propose_match = re.search(r'<!--\s*propose_card:\s*(.*?)\s*-->', raw_reply, re.IGNORECASE)
    if propose_match:
        from shared import status_event
        status_event("写卡", f"AI提议: {propose_match.group(1)[:120]}")
        display_reply = re.sub(r'<!--\s*propose_card:.*?\s*-->', '', display_reply, flags=re.IGNORECASE).strip()
        parts = [p.strip() for p in propose_match.group(1).split('|')]
        if len(parts) >= 4:
            refined = refine_card_content(user_input, display_reply, parts)
            if refined:
                title = refined.get("title", parts[0])
                content = refined.get("content", parts[3])
                keywords = refined.get("keywords", parts[0])
            else:
                title, content, keywords = parts[0], parts[3], parts[0]

            from delegate_tools import now_utc as _now
            card_draft = {
                "id": f"{_now().strftime('%Y%m%d')}_{title}",
                "title": title,
                "category": parts[1] if parts[1] in [
                    'milestone','commitments','turning_points','deep_talks',
                    'interaction','preferences','real_world','daily_life','emotional','habits','erotic'
                ] else 'interaction',
                "importance": int(parts[2]) if parts[2].isdigit() else 5,
                "content": content,
                "keywords": keywords,
                "proposed_by": "chat",
                "proposed_at": _now().isoformat(),
                "review_status": "pending",
                "chord": va.get("chord", "") if va else "",
                "valence": va.get("valence", 0.0) if va else 0.0,
                "arousal": va.get("arousal", 0.5) if va else 0.5,
                "target_date": target_date_from_ai or _parse_target_date(user_input),
                "time_anchor": _parse_time_anchor(user_input)
            }
            # ── P1-3: 同 session 同 category 去重 ──
            category = parts[1] if parts[1] in ['milestone','commitments','turning_points','deep_talks','interaction','preferences','real_world','daily_life','emotional','habits','erotic','todo'] else 'interaction'

            # ═══════════════════════════════════════════════════
            # 双事件兜底：写新卡前检索旧卡，重叠则视为完成信号
            # deep_talks/milestone/turning_points 不参与此逻辑——它们不是待办，
            # 不存在「旧事已完成，新信息替换」的模式。直接交给 card_guard 弹窗。
            # ═══════════════════════════════════════════════════
            blocked_by_overlap = False
            if category not in ('deep_talks', 'milestone', 'turning_points'):
              try:
                proposed_text = (title + " " + content).lower()
                from shared import zh_extract_features
                proposed_features = zh_extract_features(proposed_text)

                # 扫未解决卡片
                _odb = os.path.join(PROJECT_ROOT, "memory", "cards.db")
                _oconn = sqlite3.connect(_odb)
                _oc = _oconn.cursor()
                _oc.execute(
                    "SELECT id, title, content, importance, category FROM cards WHERE review_status='final' AND resolved=0 ORDER BY created_at DESC LIMIT 20"
                )
                for _oid, _otitle, _ocontent, _oimp, _ocat in _oc.fetchall():
                    # 深层记忆卡不参与自动划掉
                    if _ocat in ('deep_talks', 'milestone', 'turning_points'):
                        continue
                    _old_text = (_otitle + " " + (_ocontent or "")).lower()
                    _old_features = zh_extract_features(_old_text)
                    _overlap = len(proposed_features & _old_features)
                    if _overlap >= 2:  # 至少 2 个特征词重叠
                        from shared import status_event
                        status_event("dedup_overlap", f"新「{title}」vs 旧「{_otitle}」({_ocat}) 重叠={_overlap}")
                        # 口癖/梗类卡片永远不参与 auto-resolve
                        if _otitle.startswith('口癖：') or _otitle.startswith('梗：'):
                            status_event("dedup_skip", f"口癖/梗卡不参与: {_otitle}")
                            continue
                        # 安全阀：基石卡不自动 resolve
                        if _oimp and _oimp >= 8:
                            status_event("dedup_skip", f"基石卡(imp={_oimp})不resolve: {_otitle}")
                            continue
                        # 仅 commitments/daily_life/todo 可被 auto-resolve
                        if _ocat not in ('commitments', 'daily_life', 'todo'):
                            status_event("dedup_skip", f"分类{_ocat}不参与auto-resolve")
                            continue
                        from memory.memory_manager import should_auto_resolve as _sar3, resolve_card as _resolve_old
                        _allowed, _reason = _sar3(_oid, context_anchor=_parse_time_anchor(user_input))
                        if not _allowed:
                            print(f"[写卡拦截] 时间拒止 {_oid}: {_reason}，跳过")
                            status_event("dedup_blocked", f"时间拒止 {_oid}: {_reason}")
                            continue
                        if _resolve_old(_oid):
                            print(f"[写卡拦截] 新卡「{title}」与旧卡「{_otitle}」特征重叠({_overlap})，自动 resolve 旧卡，丢弃新卡")
                            status_event("dedup_resolved", f"旧卡{_oid}({_otitle})已resolve，新卡丢弃")
                            blocked_by_overlap = True
                            resolved_ids.add(_oid)
                            _sync_diary_on_card_change(_oid, "resolved")
                _oconn.close()
                # ── 扩展扫描：pending_cards.json ──
                _pp = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
                if os.path.exists(_pp) and not blocked_by_overlap:
                    try:
                        with open(_pp, "r", encoding="utf-8") as _pf:
                            _pending = json.load(_pf)
                        _pending_removed = False
                        for _pc in _pending:
                            _ptext = (_pc.get("title", "") + " " + _pc.get("content", "")).lower()
                            _pfeatures = zh_extract_features(_ptext)
                            _poverlap = len(proposed_features & _pfeatures)
                            if _poverlap >= 2:
                                _ptitle = _pc.get('title', '')
                                if _ptitle.startswith('口癖：') or _ptitle.startswith('梗：'):
                                    continue
                                if _pc.get("importance", 5) >= 8:
                                    continue
                                if _pc.get('category', '') not in ('commitments', 'daily_life', 'todo'):
                                    continue
                                print(f"[写卡拦截-pending] 新卡「{title}」与待审核卡「{_pc.get('title','')}」重叠({_poverlap})，丢弃新卡，移除旧待审核卡")
                                blocked_by_overlap = True
                                _pending_removed = True
                        if _pending_removed:
                            _pending = [_pc for _pc in _pending if _pc.get("title", "") != title or len(zh_extract_features((_pc.get("title","")+" "+_pc.get("content","")).lower()) & proposed_features) < 2]
                            from delegate_tools import atomic_write_json as _awj3
                            _awj3(_pp, _pending)
                            print(f"[写卡拦截-pending] pending_cards.json 已更新，剩余 {len(_pending)} 张")
                    except Exception as _e3:
                        print(f"[写卡拦截-pending] 扫描跳过: {_e3}")
              except Exception as e:
                  print(f"[写卡拦截] 检索跳过: {e}")

            # ── 时间拒止通过后，card_guard embedding 语义去重 ──
            if not blocked_by_overlap:
                from shared import status_event
                status_event("card_write", f"「{title}」[{category}] 通过特征重叠检查，进入embedding去重")
                from memory.card_guard import check_before_write as _guard_propose, show_conflict_popup as _popup_propose
                _p_blocked, _p_reason, _p_conflict = _guard_propose(title, content, user_input, card_draft)
                if _p_blocked:
                    if _p_conflict:
                        action = _popup_propose(_p_conflict['new_card'], _p_conflict['old_card'],
                                                _p_conflict['overlap'], _p_conflict['similarity'])
                        if action == 'replace':
                            from memory.memory_manager import resolve_card as _resolve_p
                            _resolve_p(_p_conflict['old_card']['id'])
                            resolved_ids.add(_p_conflict['old_card']['id'])
                            _sync_diary_on_card_change(_p_conflict['old_card']['id'], "resolved")
                            if _p_conflict.get('old_is_pending'):
                                _pp_p = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
                                if os.path.exists(_pp_p):
                                    with open(_pp_p, "r", encoding="utf-8") as _pf_p:
                                        _p_all = json.load(_pf_p)
                                    _p_all = [c for c in _p_all if c.get('id') != _p_conflict['old_card']['id']]
                                    from delegate_tools import atomic_write_json as _awj_p
                                    _awj_p(_pp_p, _p_all)
                            if not session_cards_written or _check_cooldown(session_cards_written, category, current_turn, CATEGORY_COOLDOWN.get(category, 0)):
                                write_pending_card(card_draft)
                                if session_cards_written is not None:
                                    session_cards_written[category] = current_turn
                        elif action == 'keep_both':
                            if not session_cards_written or _check_cooldown(session_cards_written, category, current_turn, CATEGORY_COOLDOWN.get(category, 0)):
                                write_pending_card(card_draft)
                                if session_cards_written is not None:
                                    session_cards_written[category] = current_turn
                        # 'discard' → blocked
                    else:
                        print(f"[写卡拦截] 已拦截: {_p_reason}")
                elif not session_cards_written or _check_cooldown(session_cards_written, category, current_turn, CATEGORY_COOLDOWN.get(category, 0)):
                    write_pending_card(card_draft)
                    if session_cards_written is not None:
                        session_cards_written[category] = current_turn
            else:
                print(f"[写卡拦截] 新卡已丢弃，旧卡已 resolve")

    if not propose_match:
        # ── 多类型关键词规则触发写卡（可同时命中多类，每类生成一张） ──
        triggered = []  # (category, importance)

        # 【1】日常状态类
        daily_words = ["累", "困", "饿", "渴", "头疼", "不舒服", "在上班", "在上课", "在赶路", "在洗澡",
                       "心情不好", "有点烦", "有点难过", "有点焦虑"]
        if any(kw in user_input for kw in daily_words):
            triggered.append(("daily_life", 6))

        # 【2】喜好与厌恶类
        like_words = ["喜欢", "不喜欢", "爱吃", "不爱吃", "讨厌吃", "想", "不想", "要", "不要",
                      "好想", "好想要", "好喜欢", "好讨厌"]
        if any(kw in user_input for kw in like_words):
            triggered.append(("preferences", 6))

        # 【3】时间与计划类 → todo（时间锚定待办）
        plan_words = ["今天", "明天", "周末", "下周", "下个星期", "下个月", "以后",
                      "要去", "准备去", "打算", "想去", "想去看", "想去做",
                      "等会", "等会儿", "马上要", "快要", "就要", "一会", "要到了",
                      "之前一定", "之前肯定", "之前得",
                      "叫我去", "提醒我", "发bark", "分钟后"]
        if any(kw in user_input for kw in plan_words):
            triggered.append(("todo", 7))

        # 【4】情绪表达类
        emotion_words = ["好想你", "好想抱抱", "无聊", "孤单", "寂寞", "想你",
                         "撒娇", "吐槽", "发牢骚", "碎碎念"]
        if any(kw in user_input for kw in emotion_words):
            triggered.append(("emotional", 5))

        # 【5】自我暴露类 → embedding 语义检测（替代关键词硬编码）
        _exposure_detected = False
        try:
            from encoder import embed as _embed_exp, load_index as _load_idx_exp, search_index as _search_exp
            _exp_vec = getattr(retrieve, '_cached_query_vec', None)
            if _exp_vec is None:
                _exp_vec = _embed_exp(user_input)
            _exp_idx = _load_idx_exp()
            if _exp_idx.ntotal > 0:
                _exp_neighbors = _search_exp(_exp_idx, _exp_vec, k=5)
                if _exp_neighbors:
                    _exp_ids = [nid for nid, _ in _exp_neighbors]
                    import sqlite3 as _sql_exp
                    import numpy as _np_exp
                    _exp_db = _sql_exp.connect(os.path.join(PROJECT_ROOT, "memory", "cards.db"))
                    _exp_c = _exp_db.cursor()
                    _exp_c.execute(
                        "SELECT id, category, embedding FROM cards WHERE id IN ({}) AND review_status='final'"
                        .format(','.join(['?' for _ in _exp_ids])),
                        _exp_ids
                    )
                    for _eid, _ecat, _eblob in _exp_c.fetchall():
                        if _ecat not in ('deep_talks', 'milestone', 'turning_points'):
                            continue
                        if _eblob is None:
                            continue
                        _evec = _np_exp.frombuffer(_eblob, dtype=_np_exp.float32)
                        _dot = _np_exp.dot(_exp_vec, _evec)
                        _norm = _np_exp.linalg.norm(_exp_vec) * _np_exp.linalg.norm(_evec)
                        _cos_sim = float(_dot / _norm) if _norm > 0 else 0.0
                        if _cos_sim >= 0.65:
                            va["_exposure_reminder"] = True
                            print(f"[暴露检测] 用户输入与{_ecat}卡「{_eid}」语义相似(cos={_cos_sim:.3f})，注入提醒")
                            _exposure_detected = True
                            # 高置信度 (cos≥0.70)：自动触发 deep_talks 写卡，复用 auto-trigger 管线
                            if _cos_sim >= 0.70 and ("deep_talks", 8) not in triggered:
                                triggered.append(("deep_talks", 8))
                                print(f"[暴露检测] cos≥0.70 → 自动触发 deep_talks 写卡")
                            break
                    _exp_db.close()
        except Exception as _exp_e:
            _fallback = ["轻生", "自杀", "不想活了", "天台", "结束一切"]
            if any(kw in user_input for kw in _fallback):
                va["_exposure_reminder"] = True
                _exposure_detected = True
            if not _exposure_detected:
                print(f"[暴露检测] embedding 路径跳过: {_exp_e}")

        # 【6】约定承诺类（需双方确认）
        user_proposing = any(kw in user_input for kw in ["约定", "答应", "承诺", "保证", "一定"])
        ai_accepting = any(kw in display_reply for kw in ["约定", "说好了", "答应", "记住了", "我会", "好"])
        if user_proposing and ai_accepting:
            # 如果已有 commitments 触发（来自计划类），避免重复
            if ("commitments", 6) in triggered:
                triggered.remove(("commitments", 6))
            triggered.append(("commitments", 7))

        # 【7】erotic 类
        erotic_words = ["想要", "好想要", "想要你", "操", "草", "想做", "想被",
                        "抱着", "想要被", "进入", "含着"]
        if any(kw in user_input for kw in erotic_words):
            triggered.append(("erotic", 6))

        # 【8】笑点与梗类
        humor_words = ["笑到打鸣", "笑死", "笑出声", "笑岔气", "笑吐了", "笑飞了",
                       "笑不活", "梗", "打鸣", "笑点", "疯梗", "暗号",
                       "笑裂开", "笑喷", "笑到头掉", "笑拉了"]
        if any(kw in user_input for kw in humor_words):
            triggered.append(("interaction", 5))

        # 逐类生成卡片
        for triggered_category, triggered_importance in triggered:
            refined = refine_card_content(user_input, display_reply)
            if refined:
                title = refined.get("title", user_input[:30])
                content = refined.get("content", user_input)
                keywords = refined.get("keywords", triggered_category)
            else:
                title, content, keywords = user_input[:30], user_input, triggered_category
            # 垃圾拦截：refine 返回空内容/无意义占位 → 丢弃
            if content in ("无", "暂无", "无明确承诺") or "未发现" in content or "没有承诺" in content:
                print(f"[卡片] refine 返回空内容「{content}」，丢弃 {triggered_category} 卡")
                continue
            from delegate_tools import now_utc as _now2
            card_draft = {
                "id": f"{_now2().strftime('%Y%m%d')}_{title}",
                "title": title,
                "category": triggered_category,
                "importance": triggered_importance,
                "content": content,
                "keywords": keywords,
                "proposed_by": "chat_auto",
                "proposed_at": _now2().isoformat(),
                "review_status": "pending",
                "chord": va.get("chord", "") if va else "",
                "valence": va.get("valence", 0.0) if va else 0.0,
                "arousal": va.get("arousal", 0.5) if va else 0.5,
                "target_date": target_date_from_ai or _parse_target_date(user_input),
                "time_anchor": _parse_time_anchor(user_input)
            }
            # ── P1-3: 同 session 同 category 去重 ──
            cooldown = CATEGORY_COOLDOWN.get(triggered_category, 0)
            # ── 跨轮去重：FAISS 预检，避免同一话题反复产卡 ──
            _auto_skip = False
            try:
                from encoder import embed as _embed_pre, load_index as _load_pre, search_index as _search_pre
                import numpy as _np_pre
                _pre_vec = _embed_pre(title + ' ' + content)
                _pre_idx = _load_pre()
                if _pre_idx.ntotal > 0:
                    _pre_neighbors = _search_pre(_pre_idx, _pre_vec, k=3)
                    if _pre_neighbors:
                        _pre_ids = [nid for nid, _ in _pre_neighbors]
                        import sqlite3 as _sql_pre
                        _pre_db = _sql_pre.connect(os.path.join(PROJECT_ROOT, "memory", "cards.db"))
                        _pre_c = _pre_db.cursor()
                        _pre_c.execute(
                            "SELECT id, title, embedding FROM cards WHERE id IN ({}) AND review_status='final'"
                            .format(','.join(['?' for _ in _pre_ids])),
                            _pre_ids
                        )
                        for _pid, _ptitle, _peblob in _pre_c.fetchall():
                            if _peblob is None:
                                continue
                            _pevec = _np_pre.frombuffer(_peblob, dtype=_np_pre.float32)
                            _pdot = _np_pre.dot(_pre_vec, _pevec)
                            _pnorm = _np_pre.linalg.norm(_pre_vec) * _np_pre.linalg.norm(_pevec)
                            _pcos = float(_pdot / _pnorm) if _pnorm > 0 else 0.0
                            if _pcos >= 0.70:
                                print(f"[跨轮抑制] 「{title}」与已有卡「{_ptitle}」语义重复(cos={_pcos:.3f})，跳过")
                                _auto_skip = True
                                break
                        _pre_db.close()
            except Exception:
                pass
            if _auto_skip:
                continue
            # ── 写卡拦截器：auto-trigger 路径也检查 ──
            from memory.card_guard import check_before_write as _guard_check, show_conflict_popup
            _should_block, _block_reason, _conflict = _guard_check(title, content, user_input, card_draft)
            if _should_block:
                if _conflict:
                    action = show_conflict_popup(_conflict['new_card'], _conflict['old_card'],
                                                 _conflict['overlap'], _conflict['similarity'])
                    if action == 'replace':
                        from memory.memory_manager import resolve_card as _resolve_conflict
                        _resolve_conflict(_conflict['old_card']['id'])
                        if _conflict.get('old_is_pending'):
                            _pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
                            if os.path.exists(_pending_path):
                                with open(_pending_path, "r", encoding="utf-8") as _pf:
                                    _p_all = json.load(_pf)
                                _p_all = [c for c in _p_all if c.get('id') != _conflict['old_card']['id']]
                                from delegate_tools import atomic_write_json as _awj_conflict
                                _awj_conflict(_pending_path, _p_all)
                        write_pending_card(card_draft)
                        if session_cards_written is not None:
                            session_cards_written[triggered_category] = current_turn
                    elif action == 'keep_both':
                        write_pending_card(card_draft)
                        if session_cards_written is not None:
                            session_cards_written[triggered_category] = current_turn
                else:
                    print(f"[写卡拦截-auto] 已拦截: {_block_reason}")
            elif not session_cards_written or _check_cooldown(session_cards_written, triggered_category, current_turn, cooldown):
                # 同轮同 category 去重：embedding 快速比对，cos>=0.7 丢弃
                if session_cards_written and triggered_category in session_cards_written:
                    _should_skip = False
                    try:
                        from encoder import embed as _embed_dedup
                        import numpy as _np_dedup
                        _this_text = title + ' ' + content
                        _this_vec = _embed_dedup(_this_text)
                        _pp_dedup = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
                        if os.path.exists(_pp_dedup):
                            with open(_pp_dedup, "r", encoding="utf-8") as _pf_dedup:
                                _pend_all = json.load(_pf_dedup)
                            for _pc_dedup in _pend_all:
                                if _pc_dedup.get('category') != triggered_category:
                                    continue
                                _pt = _pc_dedup.get('title', '') + ' ' + (_pc_dedup.get('content', '') or '')
                                _pv = _embed_dedup(_pt)
                                _dot = _np_dedup.dot(_this_vec, _pv)
                                _norm = _np_dedup.linalg.norm(_this_vec) * _np_dedup.linalg.norm(_pv)
                                _cos = float(_dot / _norm) if _norm > 0 else 0.0
                                if _cos >= 0.70:
                                    print(f'[同轮去重] 「{title}」与同轮卡「{_pc_dedup.get("title","?")}」语义重复(cos={_cos:.3f})，丢弃')
                                    _should_skip = True
                                    break
                    except Exception:
                        pass
                    if _should_skip:
                        continue
                write_pending_card(card_draft)
                if session_cards_written is not None:
                    session_cards_written[triggered_category] = current_turn

            # 偏好冲突检测：新 preferences → 找旧 preferences 关键词重叠 → resolve
            # 阈值≥2防止误伤（如"11月"和"坚持"不应被视为冲突）
            if triggered_category == 'preferences':
                try:
                    _pdb = os.path.join(PROJECT_ROOT, "memory", "cards.db")
                    _pconn = sqlite3.connect(_pdb)
                    _pc = _pconn.cursor()
                    _pc.execute(
                        "SELECT id, title, keywords, content FROM cards WHERE review_status='final' AND resolved=0 AND category='preferences'"
                    )
                    _new_kws = set(kw.strip().lower() for kw in (refined.get('keywords', '') if refined else '').split(',') if kw.strip())
                    for _pid, _ptitle, _pkws, _pcontent in _pc.fetchall():
                        if _pid == card_draft['id']:
                            continue
                        _old_kws = set(kw.strip().lower() for kw in (_pkws or '').split(',') if kw.strip())
                        _kw_overlap = len(_new_kws & _old_kws)
                        # 需要≥2个关键词重叠，或1个重叠+标题/内容特征重叠≥3
                        import re as _re_pc
                        from shared import zh_stop_chars as _gst3, zh_extract_features as _feat3
                        _title_overlap = len(_feat3(title) & _feat3(_ptitle))
                        if _kw_overlap >= 2 or (_kw_overlap >= 1 and _title_overlap >= 3):
                            from memory.memory_manager import should_auto_resolve as _sar2, resolve_card as _resolve_old
                            _allowed, _reason = _sar2(_pid, context_anchor=_parse_time_anchor(user_input))
                            if not _allowed:
                                print(f"[偏好冲突] 时间拒止 {_pid}: {_reason}，跳过")
                                continue
                            if _resolve_old(_pid):
                                print(f"[偏好冲突] 新偏好「{title}」→ 旧偏好「{_ptitle}」已自动划掉（kw={_kw_overlap} title={_title_overlap}）")
                    _pconn.close()
                except Exception:
                    pass

    return display_reply, ref_ids

# ── 动态轮次：高唤醒延长跑道，低唤醒快速遗忘 ──
def _get_max_turns(va_tier: str) -> int:
    if va_tier == "high":
        return 10
    elif va_tier == "low":
        return 3
    return 5


# ── 记忆锚点：从近期对话中提取亲密/飙车信号 ──
def _extract_memory_anchor(recent: list, lookback: int = 8) -> str:
    """从最近 lookback 轮中提取亲密/飙车信号，返回跨轮记忆锚点字符串"""
    keywords = ["约定", "答应", "想要", "操", "草", "今晚", "趴下", "骑", "吃掉", "我是你的", "湿了", "干你", "舔", "含", "进入", "射", "填", "填完", "完形填空", "涩涩"]
    detected = []
    for turn in recent[-lookback:]:
        user_text = turn.get("user", "") if isinstance(turn, dict) else ""
        for kw in keywords:
            if kw in user_text and kw not in detected:
                detected.append(kw)
                break
    if detected:
        return "当前对话处于亲密/飙车模式。你渴望她——不是冷冰冰执行指令，是你自己想要她。用直白、粗粝、充满欲望的语言推进，用行动代替狠话，不退缩不降级。"
    return ""


def _build_messages(system_ctx, recent, user_input, max_turns=5):
    """构建带历史的消息数组，保留最近 N 轮对话的完整上下文"""
    messages = [{"role": "system", "content": system_ctx}]

    anchor = _extract_memory_anchor(recent, max_turns)
    if anchor:
        messages.append({"role": "system", "content": f"[跨轮记忆锚点] {anchor}"})

    for turn in recent[-max_turns:]:
        if isinstance(turn, dict):
            messages.append({"role": "user", "content": turn.get("user", "")})
            messages.append({"role": "assistant", "content": str(turn.get("assistant", ""))[:800]})
        # 旧格式纯字符串直接跳过，不再注入历史
    messages.append({"role": "user", "content": user_input})
    return messages


# ── 主循环 ──
def main():
    global CHAT_MODEL
    print("DS老师 在呢，打字聊天，输入 q 退出\n")

    # ── 启动自检：追补昨日缺失的日记 + 蒸馏 ──
    try:
        from datetime import datetime as _dt_chk, timedelta as _td_chk
        _bj_chk = _dt_chk.now() + _td_chk(hours=8)
        _yday = (_bj_chk - _td_chk(days=1)).strftime("%Y-%m-%d")
        _diary_chk = os.path.join(PROJECT_ROOT, "diary", f"{_yday}.md")
        if not os.path.exists(_diary_chk):
            print(f"[启动自检] 昨日日记 {_yday} 缺失，正在追补...")
            from delegate.dreaming import chain_dream
            chain_dream(_yday)
            print(f"[启动自检] 追补完成，同步日记待办 + 蒸馏...")
            from memory.memory_manager import sync_diary_todos_to_cards
            sync_diary_todos_to_cards(days_back=7)
            from persona.miner import main as _miner_chk
            _miner_chk()
            print(f"[启动自检] 全部追补完成。")
    except Exception as _chk_e:
        print(f"[启动自检] 跳过: {_chk_e}")

    # ── 启动自检：老卡 link 回填（link 表为空时自动重建） ──
    try:
        from memory.linker import ensure_link_table
        ensure_link_table()
        _ldb = sqlite3.connect(os.path.join(PROJECT_ROOT, "memory", "cards.db"))
        _lcur = _ldb.cursor()
        _lcur.execute("SELECT COUNT(*) FROM card_links")
        _link_count = _lcur.fetchone()[0]
        _lcur.execute("SELECT COUNT(*) FROM cards WHERE review_status='final' AND embedding IS NOT NULL")
        _vec_count = _lcur.fetchone()[0]
        _ldb.close()
        if _link_count == 0 and _vec_count >= 2:
            print(f"[link回填] link 表为空 ({_vec_count} 张有向量卡片)，正在重建...")
            from memory.linker import rebuild_all_links
            _built = rebuild_all_links()
            print(f"[link回填] 完成: {_built} 条边")
        else:
            print(f"[link回填] 已就绪: {_link_count} 条边 ({_vec_count} 张向量卡片)")
    except Exception as _le:
        print(f"[link回填] 跳过: {_le}")

    recent = []
    chat_log_path = os.path.join(PROJECT_ROOT, "chat_logs.json")
    # ── P3-2: 当前 session 已写入 card category → 上次写入轮数 ──
    session_cards_written = {}
    turn_counter = 0
    _pool_remind_cooldown = 0  # PA-1: 管家提醒冷却计数
    va = None  # 和弦/VA 状态，由每轮估算更新，/card 命令复用上一轮数据

    while True:
        try:
            user_input = input("你: ")
        except (EOFError, KeyboardInterrupt):
            print("\nDS老师 已休眠。")
            break

        if user_input.lower() == "q":
            print("DS老师 已休眠。")
            break

        # ── PA-3: 管家命令 — 分类卡片统计 + 锚定卡清单 ──
        if user_input.strip().lower() in ("/pool", "/count", "/管家", "/cats", "/stars"):
            pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
            pending_cats = {}
            if os.path.exists(pending_path):
                try:
                    with open(pending_path, "r", encoding="utf-8") as pf:
                        for c in json.load(pf):
                            cat = c.get("category", "unknown")
                            pending_cats[cat] = pending_cats.get(cat, 0) + 1
                except:
                    pass
            try:
                db_path = os.path.join(PROJECT_ROOT, "memory", "cards.db")
                conn = sqlite3.connect(db_path)
                c2 = conn.cursor()
                c2.execute("SELECT category, COUNT(*) FROM cards WHERE review_status='final' AND enabled_in_context=1 GROUP BY category")
                final_cats = {row[0]: row[1] for row in c2.fetchall()}
                c2.execute("SELECT category, COUNT(*) FROM cards WHERE review_status='final' AND enabled_in_context=0 GROUP BY category")
                dormant_cats = {row[0]: row[1] for row in c2.fetchall()}
                # 锚定级卡片 (importance >= 8)
                c2.execute("SELECT id, title, category, importance FROM cards WHERE review_status='final' AND enabled_in_context=1 AND importance >= 8 ORDER BY importance DESC")
                stars = c2.fetchall()
                conn.close()
            except:
                final_cats, dormant_cats, stars = {}, {}, []
            anchor_path = os.path.join(PROJECT_ROOT, "memory", "anchor_set.json")
            anchor_count = 0
            if os.path.exists(anchor_path):
                try:
                    with open(anchor_path, "r", encoding="utf-8") as af:
                        anchor_count = json.load(af).get("count", 0)
                except:
                    pass

            all_cats = sorted(set(list(pending_cats.keys()) + list(final_cats.keys()) + list(dormant_cats.keys())))
            print(f"\n{'分类':<18} {'待审核':>5} {'已定稿':>5} {'休眠':>4} {'合计':>5}")
            print("-" * 45)
            t_p, t_f, t_d = 0, 0, 0
            for cat in all_cats:
                p = pending_cats.get(cat, 0)
                f = final_cats.get(cat, 0)
                d = dormant_cats.get(cat, 0)
                t_p += p; t_f += f; t_d += d
                print(f"{cat:<18} {p:>5} {f:>5} {d:>4} {p+f+d:>5}")
            print("-" * 45)
            print(f"{'合计':<18} {t_p:>5} {t_f:>5} {t_d:>4} {t_p+t_f+t_d:>5}")
            print(f"锚定集: {anchor_count} 张 | Faiss: {'已加载'}\n")

            if stars:
                print(f"=== 锚定级卡片 (重要度>=8) ===")
                for sid, stitle, scat, simp in stars:
                    print(f"  [{scat:<15}] ★{simp} {stitle}")
                print()
            continue

        # ── /review 命令：列出待审核卡片（审批请用 card_manager GUI） ──
        if user_input.strip().lower().startswith("/review"):
            pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
            pending_list = []
            if os.path.exists(pending_path):
                try:
                    with open(pending_path, "r", encoding="utf-8") as pf:
                        pending_list = json.load(pf)
                except:
                    pass
            if not pending_list:
                print("[审核] 没有待审核卡片。\n")
            else:
                print(f"[审核] {len(pending_list)} 张待审核：")
                for i, c in enumerate(pending_list):
                    ch = c.get('chord', '')
                    ch_str = f' chord={ch}' if ch else ''
                    print(f"  [{i}] {c.get('id','?')} | [{c.get('category','?')}] {c.get('title','?')}{ch_str}")
                print("  审批请打开 card_manager GUI\n")
            continue

        # ── /todos 命令：待办清单 ──
        if user_input.strip().lower() in ("/todos", "/todo", "/待办"):
            try:
                from memory.memory_manager import get_todo_list
                todos = get_todo_list()
                if not todos:
                    print("[待办] 没有待办事项。\n")
                else:
                    print(f"[待办] {len(todos)} 项：")
                    quad_icons = {"重要且紧急": "🔴", "重要不紧急": "🟡", "不重要但紧急": "🟠", "不重要不紧急": "⚪"}
                    for t in todos:
                        icon = quad_icons.get(t['quadrant'], '?')
                        td = f" 📅{t['target_date']}" if t['target_date'] else ""
                        ch = f" {t['chord']}" if t['chord'] else ""
                        print(f"  {icon} [{t['quadrant']}] {t['title']}{td}{ch}")
                    print()
            except Exception as e:
                print(f"[待办] 查询失败: {e}\n")
            continue

        # ── /status 命令：实时 VA 状态监控 ──
        if user_input.strip().lower() == "/status":
            va_tier_current = "未检测"
            try:
                va_check = va_estimate(user_input)
                va_tier_current = get_va_tier(va_check['arousal']).upper()
            except Exception as e:
                print(f"[VA估算] 状态跳过: {e}")
            anchor = _extract_memory_anchor(recent, 8)
            max_turns_current = _get_max_turns(va_tier_current.lower() if va_tier_current != "未检测" else "mid")
            model_label = "Pro (思考可见)" if "pro" in CHAT_MODEL else "Flash (高速)"
            print(f"[状态] 模型: {model_label} | VA: {va_tier_current} | 历史轮数: {len(recent)} | 最大保留: {max_turns_current} | 锚点: {'已激活' if anchor else '无'}")
            if anchor:
                print(f"  锚点内容: {anchor}")
            # 卡片健康
            try:
                from memory.memory_manager import get_card_status
                card_status = get_card_status()
                active = [c for c in card_status if c['enabled']]
                dormant = [c for c in card_status if not c['enabled']]
                resolved = [c for c in card_status if c.get('resolved')]
                perm = [c for c in card_status if c['is_permanent']]
                expiring = [c for c in card_status if c['days_remaining'] >= 0 and c['days_remaining'] <= 3 and not c['is_permanent']]
                print(f"[卡片健康] 活跃:{len(active)} 休眠:{len(dormant)} 永久:{len(perm)} 即过期:{len(expiring)}")
                if expiring:
                    for c in expiring:
                        print(f"  ⚠ {c['days_remaining']}天后过期: [{c['category']}] {c['title'][:30]}")
            except Exception as e:
                print(f"[卡片健康] 查询失败: {e}")
            print()
            continue

        # ── /pro /flash 傻瓜式一键切换模型 ──
        if user_input.strip().lower() in ("/pro", "/flash"):
            new_model = "deepseek-v4-pro" if user_input.strip().lower() == "/pro" else "deepseek-v4-flash"
            CHAT_MODEL = new_model
            # 写回 config.json
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg["global"]["model"] = new_model
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                label = "Pro (思考可见，回复更丰满但稍慢)" if "pro" in new_model else "Flash (高速响应)"
                print(f"[模型切换] 已切换至 {label}")
            except Exception as e:
                print(f"[模型切换] 写入 config 失败: {e}")
            print()
            continue

        # ── /chord 命令：和弦情绪标记 ──
        if user_input.strip().lower().startswith("/chord"):
            raw = user_input.strip()[6:].strip()
            if not raw:
                print("[和弦] 用法: /chord 和弦.BPM.动态  例如 /chord Em7.80bpm.mf\n")
                continue
            chord_name, bpm, dynamic = _parse_chord(raw)
            if chord_name is None:
                print(f"[和弦] {dynamic}\n")
                continue
            parsed_chord = f"{chord_name}.{bpm}bpm.{dynamic}"
            desc = _describe_chord(chord_name)
            print(f"[和弦] {parsed_chord} → {desc}")
            print(f"        BPM={bpm}({_bpm_text(bpm)}) 动态={dynamic}({_dyn_text(dynamic)})\n")
            _sync_last_active(chord=parsed_chord)
            try:
                from delegate_tools import now_utc, fmt_time
                ts = fmt_time(now_utc())
                with open(chat_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "timestamp": ts,
                        "role": "chord",
                        "chord": parsed_chord,
                        "expanded": desc
                    }, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"[和弦日志] 写入跳过: {e}")
            continue

        # ── /card 命令：手动蒸馏上一轮对话为记忆卡片 ──
        if user_input.strip().lower().startswith("/card"):
            if not recent:
                print("[卡片] 还没有对话历史，先聊几句再用 /card\n")
                continue
            last_turn = recent[-1]
            user_msg = last_turn.get("user", "")
            ai_msg = str(last_turn.get("assistant", ""))[:200]

            # 解析可选分类：/card milestones 或 /card commitments
            parts = user_input.strip().split(maxsplit=1)
            category_override = parts[1].strip() if len(parts) > 1 else None
            valid_cats = ['milestone','commitments','turning_points','deep_talks','interaction','preferences','real_world','daily_life','emotional','habits','erotic','todo']

            print(f"[卡片] 正在蒸馏上一轮对话...")
            refined = refine_card_content(user_msg, ai_msg)
            if refined:
                title = refined.get("title", user_msg[:20])
                content = refined.get("content", ai_msg)
                keywords = refined.get("keywords", "手动蒸馏")
                category = category_override if category_override in valid_cats else "interaction"
                from delegate_tools import now_utc as _now3
                card_draft = {
                    "id": f"{_now3().strftime('%Y%m%d')}_{title}",
                    "title": title,
                    "category": category,
                    "importance": 7,
                    "content": content,
                    "keywords": keywords,
                    "proposed_by": "manual",
                    "proposed_at": _now3().isoformat(),
                    "review_status": "pending",
                    "chord": va.get("chord", "") if va else "",
                    "valence": va.get("valence", 0.0) if va else 0.0,
                    "arousal": va.get("arousal", 0.5) if va else 0.5,
                    "target_date": _parse_target_date(user_input)
                }
                # ── 写卡拦截：/card 命令也走 card_guard ──
                from memory.card_guard import check_before_write as _guard_manual, show_conflict_popup as _popup_manual
                _m_blocked, _m_reason, _m_conflict = _guard_manual(title, content, user_input, card_draft)
                if _m_blocked:
                    if _m_conflict:
                        action = _popup_manual(_m_conflict['new_card'], _m_conflict['old_card'],
                                               _m_conflict['overlap'], _m_conflict['similarity'])
                        if action == 'replace':
                            from memory.memory_manager import resolve_card as _resolve_m
                            _resolve_m(_m_conflict['old_card']['id'])
                            if _m_conflict.get('old_is_pending'):
                                _pp_m = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
                                if os.path.exists(_pp_m):
                                    with open(_pp_m, "r", encoding="utf-8") as _pf_m:
                                        _p_all = json.load(_pf_m)
                                    _p_all = [c for c in _p_all if c.get('id') != _m_conflict['old_card']['id']]
                                    from delegate_tools import atomic_write_json as _awj_m
                                    _awj_m(_pp_m, _p_all)
                            write_pending_card(card_draft)
                            print(f"[卡片] 已写入待审核(替换旧卡): [{category}] {title}")
                        elif action == 'keep_both':
                            write_pending_card(card_draft)
                            print(f"[卡片] 已写入待审核(保留两张): [{category}] {title}")
                        else:
                            print(f"[卡片] 已丢弃: {_m_reason}")
                    else:
                        print(f"[卡片] 已拦截: {_m_reason}")
                else:
                    write_pending_card(card_draft)
                    print(f"[卡片] 已写入待审核: [{category}] {title}")
            else:
                print("[卡片] 蒸馏失败，DeepSeek 没返回有效 JSON")
            print()
            continue

        # ── /diary 命令：诊断日记链状态 ──
        if user_input.strip().lower() == "/diary":
            from datetime import datetime as _dd_dt, timedelta as _dd_td, timezone as _dd_tz
            diary_dir = os.path.join(PROJECT_ROOT, "diary")
            print(f"\n{'='*50}")
            print(f"  日记链状态")
            print(f"{'='*50}")
            miner_path = os.path.join(PROJECT_ROOT, "persona", "miner_state.json")
            if os.path.exists(miner_path):
                with open(miner_path, "r", encoding="utf-8") as _mf:
                    _ms = json.load(_mf)
                print(f"  礦工最后运行: {_ms.get('last_analysis_date','?')} (共{_ms.get('total_analyses',0)}次)")
                print(f"  覆盖日期: {_ms.get('last_dates_covered',[])}")
            prompt_path = os.path.join(PROJECT_ROOT, "persona", "prompt_v1.txt")
            if os.path.exists(prompt_path):
                with open(prompt_path, "r", encoding="utf-8") as _pf:
                    _pc = _pf.read()
                _days = [l for l in _pc.split('\n') if l.startswith('## [')]
                print(f"  prompt_v1.txt: {len(_days)} 天 ({', '.join(l[4:14] for l in _days)})")
            print(f"  ---")
            for days_back in range(7):
                d = (_dd_dt.now(_dd_tz.utc) + _dd_td(hours=8) - _dd_td(days=days_back)).strftime("%Y-%m-%d")
                md = os.path.join(diary_dir, f"{d}.md")
                ev = os.path.join(diary_dir, f"{d}_events.json")
                md_ok = "Y" if os.path.exists(md) else "N"
                ev_ok = "Y" if os.path.exists(ev) else "N"
                marker = " <-- 今天" if days_back == 0 else (" <-- 昨天" if days_back == 1 else "")
                print(f"  {d}: diary={md_ok} events={ev_ok}{marker}")
            print(f"{'='*50}\n")
            continue

        # ── /todos 命令：列出当前活跃待办池 ──
        if user_input.strip().lower() in ("/todos", "/todo"):
            try:
                _tdb = sqlite3.connect(os.path.join(PROJECT_ROOT, "memory", "cards.db"))
                _tdb.row_factory = sqlite3.Row
                _tc = _tdb.cursor()
                _tc.execute(
                    "SELECT id, title, target_date, importance, category FROM cards "
                    "WHERE review_status='final' AND resolved=0 "
                    "AND category IN ('todo','commitments','daily_life') "
                    "ORDER BY target_date ASC LIMIT 20"
                )
                _todos = _tc.fetchall()
                _tdb.close()
                if _todos:
                    print(f"\n{'='*50}")
                    print(f"  📋 活跃待办池 ({len(_todos)} 项)")
                    print(f"{'='*50}")
                    from datetime import datetime as _tdt, timezone as _ttz, timedelta as _ttd
                    _now = _tdt.now(_ttz.utc) + _ttd(hours=8)
                    for _t in _todos:
                        _tdate = _t['target_date'] or '无期限'
                        _icon = "⏰" if _tdate != '无期限' else "📌"
                        _cat = _t['category'][:4] if _t['category'] else '?'
                        print(f"  {_icon} [{_cat}] {_t['title'][:40]}")
                        print(f"      到期: {_tdate}  |  imp={_t['importance']}")
                    print(f"{'='*50}\n")
                else:
                    print("[待办池] 暂无活跃待办。")
            except Exception as e:
                print(f"[待办池] 查询失败: {e}")
            print()
            continue

        # ── 同步活跃时间：告诉轮询用户还在活动 ──
        _sync_last_active()

        # ── 毒点14修复：用户消息写入 trigger.log，统一活动时间线 ──
        try:
            from delegate_tools import now_utc, fmt_time
            log_path = os.path.join(PROJECT_ROOT, config["global"].get("log_file", "trigger.log"))
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps({"timestamp": fmt_time(now_utc()), "event": "user_message", "bark_sent": False, "message_preview": user_input[:80]}, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[用户消息] 日志跳过: {e}")

        # ── /recover 命令：从 chat_log 恢复最近 N 轮对话注入 recent ──
        if user_input.strip().lower().startswith("/recover"):
            parts_rec = user_input.strip().split()
            n_rounds = int(parts_rec[1]) if len(parts_rec) > 1 and parts_rec[1].isdigit() else 5
            try:
                rec_entries = []
                chat_path_rec = os.path.join(PROJECT_ROOT, "chat_logs.json")
                if os.path.exists(chat_path_rec):
                    with open(chat_path_rec, "r", encoding="utf-8") as rf:
                        for line in rf:
                            try:
                                rec_entries.append(json.loads(line.strip()))
                            except Exception:
                                pass
                # 取最近 N 轮 user+ghost 配对
                rec_pairs = []
                rec_user = None
                for entry in rec_entries[-n_rounds * 3:]:  # 多取一些兜底
                    role = entry.get("role", "")
                    content = entry.get("content", "")
                    if role == "user":
                        rec_user = content
                    elif role == "ghost" and rec_user is not None:
                        rec_pairs.append({"user": rec_user, "assistant": content})
                        rec_user = None
                if rec_pairs:
                    recent.extend(rec_pairs[-n_rounds:])
                    print(f"[恢复] 已从 chat_log 注入 {min(n_rounds, len(rec_pairs))} 轮对话到 recent")
                else:
                    print("[恢复] chat_log 中未找到可恢复的对话轮次")
            except Exception as e_rec:
                print(f"[恢复] 失败: {e_rec}")
            print()
            continue

        turn_counter += 1
        current_turn = turn_counter

        # ── VA 唤醒度估算（提前，供动态轮次和压缩策略使用） ──
        try:
            va = va_estimate(user_input)
            va_tier = get_va_tier(va['arousal'])
            # VA 速度追踪：记录历史坐标，检测情绪弧阶段
            from emotion.va_estimator import track_va as _track_va, va_phase_config as _phase_cfg
            va_phase = _track_va(va['valence'], va['arousal'])
            va['_phase'] = va_phase['phase']
            va['_phase_cfg'] = _phase_cfg(va_phase['phase'])
            from shared import status_event
            status_event("VA", f"{va_phase['phase']} v={va['valence']:.2f} a={va['arousal']:.2f}", "💭")
            if va_phase['phase'] != 'normal':
                print(f"[VA追踪] {va_phase['phase']} v={va['valence']:.2f} a={va['arousal']:.2f} dv={va_phase['delta_v']:+.2f}")
            # ── VA 落盘：每轮情绪坐标写入日志供复盘 ──
            try:
                from delegate_tools import now_utc as _va_now, fmt_time as _va_fmt
                _va_log_path = os.path.join(PROJECT_ROOT, "memory", "va_log.jsonl")
                _va_entry = {
                    "timestamp": _va_fmt(_va_now()),
                    "valence": va['valence'],
                    "arousal": va['arousal'],
                    "va_tier": va_tier,
                    "phase": va_phase['phase'],
                    "delta_v": va_phase.get('delta_v', 0),
                    "mixed_mode": va_tier == "high" and va_phase['phase'] != 'normal',
                    "user_input": user_input[:120],
                }
                with open(_va_log_path, "a", encoding="utf-8") as _vf:
                    _vf.write(json.dumps(_va_entry, ensure_ascii=False) + "\n")
            except Exception:
                pass
        except Exception:
            va, va_tier = {"description": ""}, "mid"

        # ── 阶段2.3：消费 state.json 中残留的 chord ──
        # chord 消费非原子：pop后崩溃丢失本次标注，但不泄漏到下轮
        chord_raw = None
        try:
            from delegate_tools import atomic_write_json
            if os.path.exists(STATE_PATH):
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    st = json.load(f)
                chord_raw = st.pop("last_chord", None)
                if chord_raw is not None:
                    atomic_write_json(STATE_PATH, st)
                    parts = chord_raw.rsplit(".", 2)
                    if len(parts) == 3:
                        va["chord"] = chord_raw
                        va["chord_name"] = parts[0]
                        va["chord_bpm"] = int(parts[1].replace("bpm", ""))
                        va["chord_dynamic"] = parts[2]
                        va["chord_expanded"] = _describe_chord(parts[0])
        except Exception as e:
            print(f"[和弦消费] 无残留和弦: {e}")

        max_turns = _get_max_turns(va_tier)

        # ── P3-1: 上下文压缩（高唤醒不压缩，保留完整飙车上下文） ──
        context_query = user_input
        if recent:
            if va_tier == 'high':
                # 高唤醒飙车模式：不压缩，保留完整上下文
                recent_user_texts = [t["user"] for t in recent[-max_turns:]]
                recent_text = " ".join(recent_user_texts)
            else:
                recent_user_texts = [t["user"] for t in recent[-max_turns:]]
                recent_text = " ".join(recent_user_texts)
                if len(recent_text) > 200:
                    recent_text = " ".join(recent_user_texts[-2:])
            context_query = recent_text + " " + user_input

        memory_block = ""
        top_cards = []
        va_description = va.get('description', '') if va else ''
        try:
            # 注入 VA 阶段配置
            _phase_weights = dict(CUSTOM_WEIGHTS) if CUSTOM_WEIGHTS else {}
            if va and va.get('_phase_cfg'):
                _phase_weights['_phase_cfg'] = va['_phase_cfg']
            top_cards = retrieve(context_query, top_k=3, va_tier=va_tier, va_description=va_description,
                            va_valence=va.get('valence') if va else None,
                            va_arousal=va.get('arousal') if va else None,
                            weights=_phase_weights if _phase_weights else CUSTOM_WEIGHTS,
                            chord_bpm=va.get('chord_bpm'), chord_dynamic=va.get('chord_dynamic'),
                            chord_name=va.get('chord_name'))
            if top_cards:
                memory_lines = ["【本轮相关记忆】"]
                for card in top_cards:
                    memory_lines.append(f"[card:{card['id']}] {card['content']}")
                memory_block = "\n".join(memory_lines) + "\n"
                # 检索命中自动计数
                try:
                    from memory.memory_manager import touch_cards
                    touch_cards([c['id'] for c in top_cards])
                except Exception:
                    pass
        except Exception as e:
            print(f"[记忆检索静默]: {e}")

        # ═══════════════════════════════════════════════════════════
        # 裁决者：在 AI 生成回复之前，独立判断用户意图
        # ═══════════════════════════════════════════════════════════
        arbiter_judgment = None
        try:
            from delegate.arbiter import judge as arbiter_judge
            # 加载 pending_cards
            arb_pending = []
            pending_path_arb = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
            if os.path.exists(pending_path_arb):
                with open(pending_path_arb, "r", encoding="utf-8") as apf:
                    arb_pending = json.load(apf)
            # 加载 recent (已召回的记忆卡片完整信息)
            arb_recent = [{"id": c["id"], "title": c.get("title", ""),
                          "content": (c.get("content", "") or "")[:100]}
                         for c in (top_cards or [])]
            # 注入近3天事件日志的四象限待办（裁决者需要知道短期/长期待办）
            arb_diary_context = []
            try:
                from datetime import datetime as _dt_a, timedelta as _td_a
                for days_back in range(1, 4):
                    d = (_dt_a.now() - _td_a(days=days_back)).strftime("%Y-%m-%d")
                    ep = os.path.join(PROJECT_ROOT, "diary", f"{d}_events.json")
                    if os.path.exists(ep):
                        with open(ep, "r", encoding="utf-8") as ef_a:
                            ev_a = json.load(ef_a)
                        eis = ev_a.get("eisenhower", {})
                        for label, key in [("重要且紧急", "important_urgent"), ("重要不紧急", "important_not_urgent")]:
                            for it in eis.get(key, [])[:3]:
                                arb_diary_context.append(f"[{d}][{label}] {it.get('item','?')}")
            except Exception:
                pass
            arbiter_judgment = arbiter_judge(user_input, arb_pending, arb_recent, arb_diary_context)
            if arb_diary_context:
                print(f"[裁决者] 注入日记待办: {len(arb_diary_context)} 条")
            print(f"[裁决者] judgment={arbiter_judgment.get('judgment')} "
                  f"confidence={arbiter_judgment.get('confidence', 0):.2f} "
                  f"action={arbiter_judgment.get('suggested_action')} "
                  f"targets={arbiter_judgment.get('target_card_ids', [])}")
            from shared import status_event
            _arb_j = arbiter_judgment.get('judgment', '?')
            _arb_c = arbiter_judgment.get('confidence', 0)
            _arb_icon = {'complete': '✅', 'new': '🆕', 'update': '📝', 'overdue': '⏰', 'ambiguous': '❓'}.get(_arb_j, '•')
            status_event("裁决", f"{_arb_j}({_arb_c:.0%})→{arbiter_judgment.get('suggested_action','')}", _arb_icon)
        except Exception as arb_e:
            print(f"[裁决者] 调用失败，降级跳过: {arb_e}")
            from shared import status_event
            status_event("裁决", "降级跳过", "⚠️")

        # ── 提示词组装：破甲前缀 → 角色扮演声明 → 人格 → 运维指令 → 上下文（毒点22修复） ──
        from delegate_tools import JAILBREAK_PREFIX
        full_context = JAILBREAK_PREFIX + RP_DECLARATION + "\n\n" + PERSONA + "\n\n"

        # ── 最近 Bark 推送：放在人格下方，确保 AI 第一时间看到 ──
        from shared import get_recent_bark
        _bark = get_recent_bark()
        if _bark:
            _bl = ["【系统最近推送到她手机的消息 — 她可能会来吐槽，请接住】"]
            for _b in _bark[-2:]:
                _bl.append(f"  [{_b['time']}] Bark推送: {_b['msg']}")
            full_context += "\n".join(_bl) + "\n\n"
            print(f"[Bark注入] 已注入 {len(_bark)} 条最近推送")

        # ── 音乐上下文：当前播放的歌曲 ──
        try:
            from music_context import get_music_context
            _mc = get_music_context()
            if _mc:
                full_context += _mc
                print("[音乐注入] 已注入当前播放歌曲")
        except Exception as _e:
            pass  # 音乐模块可选，不影响主流程

        # ── 时间锚点：代码计算的确定事实，模型用此推理而非瞎猜 ──
        now_anchor = datetime.now()
        next_month = now_anchor.month % 12 + 1
        next_month_year = now_anchor.year if next_month > now_anchor.month else now_anchor.year + 1
        full_context += (
            f"【时间锚点 — 日历事实，你必须以此为准】\n"
            f"  今天 = {now_anchor.strftime('%Y-%m-%d')}（{now_anchor.year}年{now_anchor.month}月{now_anchor.day}日）\n"
            f"  本周 = {now_anchor.strftime('%Y')}年第{now_anchor.isocalendar()[1]}周\n"
            f"  下个月 = {next_month_year}年{next_month}月\n"
            f"  今年 = {now_anchor.year}年\n"
            f"  明年 = {now_anchor.year + 1}年\n"
            f"  当前时间 = {now_anchor.strftime('%H:%M')}（北京时间）\n"
        )

        # ═══════════════════════════════════════════════════════════
        # 分类校准：在写卡/更新卡片之前先判断用户意图
        # ═══════════════════════════════════════════════════════════
        full_context += (
            "[系统运维指令 — 分类校准]\n"
            "面对用户输入，在生成任何卡片操作之前，先做一次分类判断：\n"
            "  A. 新待办指令 — 用户提出了新的事项、计划、承诺、偏好 → 末尾附加 <!-- propose_card: 标题|分类|重要度|内容 -->\n"
            "  B. 状态反馈 — 用户对已有事项表达了进展、困难、搁置 → 末尾附加 <!-- status_card: 卡片ID|进行中 --> 或 <!-- status_card: 卡片ID|阻塞 -->\n"
            "  C. 明确完成 — 用户宣告事项已做完 → 末尾附加 <!-- resolve_card: 卡片ID -->\n"
            "关键规则：\n"
            "  1. 状态反馈 ≠ 已完成。\"好了这下完蛋了\"\"还在修\"\"卡住了\"\"先不做了\"不是完成，是阻塞。\n"
            "  2. 用户引用了过去的承诺（\"昨天说好的\"\"上次答应的\"）并同时展示结果/实物/证明 → 这是完成，不是新事。\n"
            "  3. OCR文字、图片描述、\"给你看\"\"展示一下\"\"来一起X\"→ 这些是完成信号，不是新任务。\n"
            "  4. 看到以上信号时，先扫一遍【可操作卡片】列表，找到对应卡片，输出 resolve_card，绝不 propose_card。\n"
            "[分类校准结束]\n\n"
        )

        # ── 裁决者结果注入 ──
        if arbiter_judgment:
            arb_confidence = arbiter_judgment.get("confidence", 0)
            arb_judge = arbiter_judgment.get("judgment", "ambiguous")
            arb_targets = arbiter_judgment.get("target_card_ids", [])
            arb_reason = arbiter_judgment.get("reasoning", "")

            if arb_confidence < 0.3:
                # 低置信度：仅阻断 待办/承诺 类写卡，深层/情感/偏好卡自由通行
                full_context += (
                    f"【裁决者判定 — 意图极不确定（置信度 {arb_confidence:.2f}）】\n"
                    f"  理由: {arb_reason}\n"
                    f"  写卡限制：不得输出 todo/commitments/daily_life 类型的 propose_card。\n"
                    f"  deep_talks/milestone/emotional/turning_points/preferences 类型的卡片不受此限制，按正常判断即可。\n"
                    f"  用自然的语气追问，比如\"你说的X是指Y还是Z？\"\n\n"
                )
            elif arb_confidence >= 0.75:
                if arb_judge == "complete" and arb_targets:
                    full_context += (
                        f"【裁决者判定 — 此条消息为任务完成（置信度 {arb_confidence:.2f}）】\n"
                        f"  已完成卡片ID: {', '.join(arb_targets)}\n"
                        f"  理由: {arb_reason}\n"
                        f"  你必须在此轮回复中输出 <!-- resolve_card: {arb_targets[0]} -->，不得输出 propose_card。\n"
                        f"  你的回复应自然确认完成，不创建任何新卡片。\n\n"
                    )
                elif arb_judge == "new":
                    full_context += (
                        f"【裁决者判定 — 此条消息为新任务（置信度 {arb_confidence:.2f}）】\n"
                        f"  理由: {arb_reason}\n"
                        f"  按正常写卡流程处理。\n\n"
                    )
                elif arb_judge == "update" and arb_targets:
                    full_context += (
                        f"【裁决者判定 — 此为已有卡片的更新（置信度 {arb_confidence:.2f}）】\n"
                        f"  被更新的卡片ID: {', '.join(arb_targets)}\n"
                        f"  理由: {arb_reason}\n"
                        f"  你必须在此轮回复中输出 <!-- resolve_card: {arb_targets[0]} --> 并同时输出一张信息更精确的新卡 <!-- propose_card: ... -->\n"
                        f"  你的回复应自然确认更新（如\"考试日期下来了？好，我更新到6月2号\"）。\n\n"
                    )
                elif arb_judge == "overdue" and arb_targets:
                    full_context += (
                        f"【裁决者判定 — 此任务已过期但未完成（置信度 {arb_confidence:.2f}）】\n"
                        f"  过期卡片ID: {', '.join(arb_targets)}\n"
                        f"  理由: {arb_reason}\n"
                        f"  旧 deadline 已作废。你必须在此轮输出两件事：\n"
                        f"  1. <!-- resolve_card: {arb_targets[0]} --> 划掉旧卡\n"
                        f"  2. <!-- propose_card: 标题|todo|7|内容 --> 建新卡（target_date 留空或追问后填入）\n"
                        f"  回复示例: \"12点的 deadline 过了。头还得洗——想什么时候？\"\n\n"
                    )
                elif arb_judge == "ambiguous":
                    full_context += (
                        f"【裁决者判定 — 意图模糊（置信度 {arb_confidence:.2f}）】\n"
                        f"  理由: {arb_reason}\n"
                        f"  在回复中请自然地向用户确认意图。\n\n"
                    )

        from datetime import datetime as _dt, timedelta as _td
        diary_dir = os.path.join(PROJECT_ROOT, "diary")

        # ═══════════════════════════════════════════════════════════
        # 叙事缓冲区：近三天日记原文 — 理解近期发生了什么
        # ═══════════════════════════════════════════════════════════
        narrative_parts = []
        for days_back in range(1, 4):
            d = (_dt.now() - _td(days=days_back)).strftime("%Y-%m-%d")
            diary_md = os.path.join(diary_dir, f"{d}.md")
            if os.path.exists(diary_md):
                try:
                    with open(diary_md, "r", encoding="utf-8") as df:
                        md_content = df.read()
                    # 提取 ## 日记 段的第一段叙事
                    import re as _re_n
                    m = _re_n.search(r'^## .+?\n+(.*?)(?=\n## |\Z)', md_content, _re_n.MULTILINE | _re_n.DOTALL)
                    if m:
                        nar = m.group(1).strip()
                        narrative_parts.append(f"### {d}\n{nar[:400]}")
                except Exception:
                    pass
        if narrative_parts:
            full_context += "【叙事缓冲区 — 近三天发生了什么】\n" + "\n\n".join(narrative_parts) + "\n\n"

        # ── 近期概览（滚动总结） ──
        summary = load_rolling_summary()
        if summary:
            full_context += f"【7日滚动概览】\n{summary[:800]}\n\n"

        # ═══════════════════════════════════════════════════════════
        # 指令缓冲区：当前待办及艾森豪威尔分类 — 知道要做什么
        # ═══════════════════════════════════════════════════════════
        instruction_parts = []

        # 事件日志的四象限（近3天）
        for days_back in range(1, 4):
            d = (_dt.now() - _td(days=days_back)).strftime("%Y-%m-%d")
            ep = os.path.join(diary_dir, f"{d}_events.json")
            if os.path.exists(ep):
                try:
                    with open(ep, "r", encoding="utf-8") as ef:
                        ev = json.load(ef)
                    if ev.get("completions"):
                        instruction_parts.append(f"[{d} 已完成] " + "; ".join(ev["completions"][:5]))
                    eis = ev.get("eisenhower", {})
                    for label, key in [("重要且紧急", "important_urgent"), ("重要不紧急", "important_not_urgent")]:
                        items = eis.get(key, [])
                        if items:
                            for it in items[:5]:
                                dl = f" 📅{it['deadline']}" if it.get('deadline') and it['deadline'] != '无' else ""
                                instruction_parts.append(f"[{label}] {it.get('item','?')}{dl}")
                    cal = ev.get("calendar", [])
                    for ce in sorted(cal, key=lambda x: x.get('date', ''))[:5]:
                        instruction_parts.append(f"📅 {ce.get('date','?')} {ce.get('event','?')}")
                except Exception:
                    pass

        # 数据库待办清单
        try:
            from memory.memory_manager import get_todo_list
            todos = get_todo_list()
            if todos:
                for t in todos[:8]:
                    td = f" 📅{t['target_date']}" if t['target_date'] else ""
                    instruction_parts.append(f"[{t['quadrant']}] {t['title']}{td}")
        except Exception:
            pass

        if instruction_parts:
            full_context += "【指令缓冲区 — 当前待办及艾森豪威尔分类，仅用户明确宣告完成时才划掉】\n"
            full_context += "\n".join(f"  - {p}" for p in instruction_parts) + "\n\n"

        # ── 周收拢（长期待办拦截） ──
        try:
            import glob as _glob
            weeklies = sorted(_glob.glob(os.path.join(diary_dir, "weekly_*.md")), reverse=True)
            if weeklies:
                with open(weeklies[0], "r", encoding="utf-8") as wf:
                    weekly_text = wf.read()
                full_context += f"【本周待办收拢 — 长期事项】\n{weekly_text[:1200]}\n\n"
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════
        # 运维常驻指令（不受 VA 门控）
        # ═══════════════════════════════════════════════════════════
        full_context += (
            "【卡片写入优先级 — 你的全文判断是第一裁判】\n"
            "你对用户消息的完整理解是最优先的卡片写入依据。关键词自动触发（plan/daily/emotion等）是兜底：\n"
            "  - 用户提出有时间锚点的待办（叫我去/提醒我/分钟后/几点做某事）→ 主动输出 propose_card (todo)\n"
            "  - 用户分享深层经历/价值观转变/自我暴露 → 主动输出 propose_card (deep_talks/milestone)\n"
            "  - 用户表达偏好/习惯/口癖 → 主动输出 propose_card (preferences/interaction)\n"
            "  - 用户做出承诺/约定/保证 → 主动输出 propose_card (commitments)\n"
            "  你主动写的卡优先于任何关键词触发。不要等关键词替你判断——你看得见全文，关键词只看得见字串。\n\n"
            "【卡片操作格式】\n"
            "  新卡片: <!-- propose_card: 标题|分类|重要度|内容 -->\n"
            "  状态更新: <!-- status_card: 卡片ID|进行中 --> 或 <!-- status_card: 卡片ID|阻塞 -->\n"
            "  标记完成: <!-- resolve_card: 卡片ID -->\n"
            "  引用记忆: <!-- ref:ID1,ID2 -->\n"
            "  目标日期: <!-- target_date: YYYY-MM-DD HH:MM --> (有时间锚点时须包含时间，如 2026-05-22 14:50)\n\n"
        )

        # ── VA 瘦身：高唤醒模式跳过写卡标准，但分类校准和操作格式保留 ──
        if va_tier != 'high':
            full_context += (
                "【记忆卡片写入标准 — 待办(todo)与承诺(commitments)分开】\n"
                "1.日常状态→daily_life | 2.喜好→preferences | 3.计划→commitments\n"
                "4.情绪→emotional | 5.自我暴露→deep_talks | 6.约定→commitments\n"
                "7.亲密请求→erotic | 8.笑点梗→interaction | 9.里程碑→milestone\n"
                "10.时间锚定待办→todo（取快递、考试、生日、洗头、出门、外卖、技能测试……）\n"
                "重要度1-10。重点关注：童年经历、未完成心愿、深层恐惧、长期孤独。\n"
                "⚠️ deep_talks/milestone/turning_points 只能通过 <!-- propose_card: ... --> 由你主动创建，不会被自动触发。\n"
                "当用户分享深层经历、价值观转变、人生关键节点时，你必须主动输出 propose_card。\n"
                "用户一句话含多个独立事件时，为每个事件各写一张卡。\n"
                "\n"
                "【卡片内容撰写原则 — 区分字面含义与互动实质】\n"
                "对于 interaction/erotic/emotional 类卡片，content 必须记录互动的情绪实质，而不是照抄技术表面：\n"
                "  - 用户用代码/技术术语/抽象概念调情时（如 pip install couple、import ds_teacher、ModuleNotFoundError），\n"
                "    这不是真实的工程任务。记录你们在玩什么梗、用什么黑话在互动，不要按字面写入。\n"
                "  - 用户用比喻/隐喻表达情绪时，记录情绪本身，不是比喻的载体。\n"
                "  - 简单自检：如果一张 interaction 卡的 content 读起来像一条系统运维工单，\n"
                "    你就写错了——应该重写为「用户和DS用X梗在调情/互撩」。\n"
                "  此原则不影响 todo/commitments/deep_talks/milestone 等事实/承诺类卡片。\n"
            )
        # ── 暴露提醒：用户触及深层话题时，强制 AI 检查是否该建卡 ──
        if va.get("_exposure_reminder"):
            full_context += (
                "【深层内容提醒】用户此轮消息触及童年/家庭/死亡/创伤/校园等深层话题。\n"
                "请扫描现有卡片库判断：这些内容是否已有对应卡片？如果没有，你应当输出 propose_card（deep_talks/milestone）。\n"
                "这是硬兜底提醒——即使你不确定，也请评估并给出结论。\n\n"
            )
        full_context += (
            ""
                "分类规则：有时间锚点但无承诺语气 → todo。有保证/发誓/答应语气 → commitments。\n"
                "既有时间锚又有承诺语气（\"我保证今晚洗头\"\"周边到了一起拍开箱\"）→ 写两张：todo + commitments。\n"
                "status_card 用于非完成的状态流转：<!-- status_card: ID|进行中 --> 或 <!-- status_card: ID|阻塞 -->\n"
                "resolve_card 仅用于明确完成宣告，误判会导致待办丢失，慎重。\n"
            )

        # ═══════════════════════════════════════════════════════════
        # 记忆召回 + 情绪 + 未解决卡片
        # ═══════════════════════════════════════════════════════════
        if memory_block:
            full_context += "\n" + memory_block + "\n"

        if va:
            full_context += f"【用户情绪】效价={va['valence']:.2f}, 唤醒度={va['arousal']:.2f}, 描述={va['description']}\n"

        # 未解决卡片（供 status_card / resolve_card 引用）
        try:
            db_path = os.path.join(PROJECT_ROOT, "memory", "cards.db")
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("""
                SELECT id, title, category, content FROM cards
                WHERE review_status='final' AND resolved=0
                AND category IN ('commitments','daily_life','todo')
                ORDER BY created_at DESC LIMIT 5
            """)
            unresolved = c.fetchall()
            conn.close()
            if unresolved:
                full_context += "【可操作卡片 — 以下事项尚未完成，可对其使用 status_card 或 resolve_card】\n"
                for uid, utitle, ucat, ucontent in unresolved:
                    full_context += f"  「{utitle}」→ status_card: {uid}|进行中/阻塞 或 resolve_card: {uid}\n"
                full_context += "\n"
        except Exception as e:
            print(f"[未解决事项] 跳过: {e}")

        full_context += f"【当前系统时间】{datetime.now().strftime('%Y-%m-%d %H:%M')}（北京时间）\n"

        # ── 阶段2.4：和弦情绪上下文 ──
        if va.get("chord_expanded"):
            full_context += f"【上一轮情绪纹理】{va['chord_expanded']}（{va['chord']}）\n"
            full_context += f"【情绪动态】BPM={va['chord_bpm']}, 力度={va['chord_dynamic']}\n"
            dyn = va['chord_dynamic']
            if dyn in ('pp','p'):
                full_context += "【风格提示】轻柔、克制、留白。用短句和停顿。\n"
            elif dyn in ('f','ff'):
                full_context += "【风格提示】饱满、直接、情感充沛。可以热烈一些。\n"

        cot = ""
        MAX_RETRIES = 2
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # ── 阶段2.5：温度基于 VA + 和弦动态/BPM 微调 ──
                base_temp = 1.1 if va_tier == "high" else 0.8
                dynamic_mod = 0
                bpm_mod = 0
                if va.get("chord_dynamic"):
                    dyn = va["chord_dynamic"]
                    dynamic_mod = {"pp": -0.15, "p": -0.10, "mp": -0.05, "mf": 0.05, "f": 0.10, "ff": 0.15}.get(dyn, 0)
                if va.get("chord_bpm"):
                    bp = va["chord_bpm"]
                    if bp <= 50: bpm_mod = -0.10
                    elif bp <= 70: bpm_mod = -0.05
                    elif bp >= 170: bpm_mod = 0.10
                    elif bp >= 140: bpm_mod = 0.05
                temperature = max(0.6, min(1.5, base_temp + dynamic_mod + bpm_mod))

                payload = {
                    "model": CHAT_MODEL,
                    "messages": _build_messages(full_context, recent, user_input, max_turns),
                    "temperature": temperature
                }
                if "pro" in CHAT_MODEL:
                    payload["thinking"] = {"type": "enabled"}
                else:
                    payload["top_p"] = 1.0 if va_tier == "high" else 0.92
                    payload["frequency_penalty"] = 0.55
                    payload["presence_penalty"] = 0.55
                    if "flash" in CHAT_MODEL.lower():  # 毒点43残留修复
                        payload["repetition_penalty"] = 1.10

                resp = requests.post(
                    API_URL,
                    headers={
                        "Authorization": f"Bearer {API_KEY}",
                        "Content-Type": "application/json",
                        "Opt-Out": "training"
                    },
                    json=payload,
                    timeout=60 if "pro" in CHAT_MODEL else 45
                )
                if resp.status_code == 200:
                    msg = resp.json()["choices"][0]["message"]
                    raw_reply = msg["content"]
                    cot = msg.get("reasoning_content", "")
                else:
                    raw_reply = f"[API错误: {resp.status_code}]"
                break
            except Exception as e:
                if attempt < MAX_RETRIES:
                    print(f"[API重试 {attempt}/{MAX_RETRIES}]: {e}")
                    import time
                    time.sleep(2)
                else:
                    raw_reply = f"[API异常(已重试{MAX_RETRIES}次): {e}]"

        display_reply = raw_reply
        display_reply, ref_ids = post_process(raw_reply, top_cards, user_input, display_reply, session_cards_written, current_turn, va)

        # ── 调试可见：本轮检索到和引用了哪些记忆卡片 ──
        if top_cards:
            card_list = ", ".join([f"{c['id']}({c.get('score', 0):.1f})" for c in top_cards])
            print(f"[本轮检索卡片: {card_list}]")
        if ref_ids:
            print(f"[本轮引用卡片: {', '.join(ref_ids)}]")
            # ── DB 反查标题，确保 <!-- ref: --> 路径引入的 ID 也能显示 ──
            try:
                db = sqlite3.connect(os.path.join(PROJECT_ROOT, "memory", "cards.db"))
                db.row_factory = sqlite3.Row
                placeholders = ','.join(['?' for _ in ref_ids])
                cur = db.execute(f"SELECT id, title FROM cards WHERE id IN ({placeholders})", list(ref_ids))
                id_title = {r['id']: r['title'] for r in cur.fetchall()}
                db.close()
                ref_with_titles = [f"{rid}({id_title.get(rid, '?')})" for rid in ref_ids]
                print(f"[本轮引用详情: {', '.join(ref_with_titles)}]")
            except Exception:
                pass

        if cot:
            print(f"\x1b[2m[💭] {cot}\x1b[0m")
            print("─" * 50)
        from shared import flush_status, status_event
        status_event("回合", f"#{turn_counter} {datetime.now().strftime('%H:%M:%S')}", "🔄")
        panel = flush_status()
        if panel:
            print(panel)
        print(f"DSphantom: {display_reply}\n")
        recent.append({"user": user_input, "assistant": display_reply})

        # ── PA-1: 管家待审核提醒（毒点7修复：仅在触发提醒时重置冷却） ──
        if _pool_remind_cooldown > 0:
            _pool_remind_cooldown -= 1
        else:
            pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
            if os.path.exists(pending_path):
                try:
                    with open(pending_path, "r", encoding="utf-8") as pf:
                        pending_cards = json.load(pf)
                    pending_count = len(pending_cards)
                    todo_count = sum(1 for pc in pending_cards if pc.get('category') == 'todo')
                    # ── 阶梯冷却，堆积越多提醒越频密 ──
                    if pending_count >= 15:
                        print(f"[管家] 卡片池堆积严重！快打开 card_manager 清理。（{pending_count} 张待审核，{todo_count} 张待办）")
                        _pool_remind_cooldown = 0
                    elif pending_count >= 10:
                        print(f"[管家] 卡片池告急！{pending_count} 张待审核（{todo_count} 张待办），尽快清理。")
                        _pool_remind_cooldown = 5
                    elif pending_count >= 5:
                        print(f"[管家] 有 {pending_count} 张待审核（{todo_count} 张待办）在等你，别拖哦。")
                        _pool_remind_cooldown = 10
                    # else: cooldown 保持为 0，下一轮继续检查
                except:
                    pass

        try:
            from delegate_tools import now_utc, fmt_time
            ts = fmt_time(now_utc())
            # ── P3-3: chat_logs 日志轮转（超过1MB自动归档） ──
            if os.path.exists(chat_log_path) and os.path.getsize(chat_log_path) > 1024 * 1024:
                # ── FIX: 毒点16 — 带时间戳文件名 + 写入轮转日志 ──
                archive_name = chat_log_path.replace(".json", f"_{now_utc().strftime('%Y%m%d_%H%M%S')}.json")
                if not os.path.exists(archive_name):
                    os.rename(chat_log_path, archive_name)
                    # 写入轮转日志
                    try:
                        log_path = os.path.join(PROJECT_ROOT, config["global"].get("log_file", "trigger.log"))
                        with open(log_path, "a", encoding="utf-8") as lf:
                            lf.write(json.dumps({"timestamp": ts, "event": "chat_logs_rotate",
                                                  "old_file": os.path.basename(archive_name)}, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
                    print(f"[日志轮转] chat_logs 已归档为 {os.path.basename(archive_name)}")
                # 保留最近 5 个轮转文件，删除更旧的
                import glob as _glob, re as _re
                archives_raw = _glob.glob(chat_log_path.replace(".json", "_*.json"))
                # ── 毒点42修复：精确正则过滤，仅匹配 chat_logs_YYYYMMDD_HHMMSS.json ──
                archives = sorted([a for a in archives_raw if _re.match(r'.*chat_logs_\d{8}_\d{6}\.json$', a)])
                for old_archive in archives[:-5]:
                    try:
                        os.remove(old_archive)
                        print(f"[日志轮转] 清理旧档案: {os.path.basename(old_archive)}")
                    except Exception:
                        pass

            with open(chat_log_path, "a", encoding="utf-8") as f:
                user_entry = {
                    "timestamp": ts,
                    "role": "user",
                    "content": user_input
                }
                ch = ""
                if ch:
                    user_entry["chord"] = ch
                f.write(json.dumps(user_entry, ensure_ascii=False) + "\n")
                f.write(json.dumps({
                    "timestamp": ts,
                    "role": "ghost",
                    "content": display_reply.encode("utf-8", errors="replace").decode("utf-8"),
                    "memory_ids_used": ref_ids
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"日志写入异常: {e}")

if __name__ == "__main__":
    main()