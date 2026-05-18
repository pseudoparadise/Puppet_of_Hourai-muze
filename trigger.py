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
    from delegate_tools import atomic_write_json
    try:
        state = {}
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
        state["last_user_message_time"] = fmt_time(now_utc())
        if chord is not None:
            state["last_chord"] = chord
        atomic_write_json(STATE_PATH, state)
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
    from delegate_tools import atomic_write_json
    pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
    pending = []
    if os.path.exists(pending_path):
        try:
            with open(pending_path, "r", encoding="utf-8") as f:
                pending = json.load(f)
        except json.JSONDecodeError as e:
            import shutil
            backup = pending_path + ".corrupted_" + datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(pending_path, backup)
            print(f"[卡片提议] ⚠ pending_cards.json 损坏({e.lineno}:{e.colno})，已备份至 {os.path.basename(backup)}，重建空列表")
            pending = []
    pending.append(card_draft)
    try:
        atomic_write_json(pending_path, pending)
        print(f"[卡片提议] 草稿已写入 pending: {card_draft['id']}")
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
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    match = re.search(r'\{.*\}', raw, re.DOTALL)
                    if match:
                        try:
                            return json.loads(match.group())
                        except json.JSONDecodeError:
                            pass
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

# ── P3-2: 冷却检查（日活类10轮冷却，其他仅去重） ──
def _check_cooldown(written: dict, category: str, current_turn: int, cooldown: int) -> bool:
    """检查冷却状态（毒点41修复：添加语义文档）。

    cooldown 三种语义:
      > 0  — 有冷却：距上次写入需超过 cooldown 轮才允许再次触发
      = 0  — 无冷却：每次满足条件均可触发，同 session 仅去重
      < 0  — 永久去重：同 session 仅允许触发一次

    返回 True 表示可以写入，False 表示在冷却中或已去重。"""
    if category not in written:
        return True
    if cooldown < 0:  # ── FIX: 毒点2 — cooldown=0 表示无冷却，允许触发 ──
        return False  # 无冷却则直接去重
    return (current_turn - written[category]) > cooldown


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

    # auto resolve — 双重机制：AI 标记 + 关键词兜底
    resolved_ids = set()

    # 机制 A：AI 主动输出 <!-- resolve_card: ID -->
    resolve_match = re.search(r'<!--\s*resolve_card:\s*(.*?)\s*-->', raw_reply, re.IGNORECASE)
    if resolve_match:
        display_reply = re.sub(r'<!--\s*resolve_card:.*?\s*-->', '', display_reply, flags=re.IGNORECASE).strip()
        cid_to_resolve = resolve_match.group(1).strip()
        try:
            from memory.memory_manager import resolve_card as do_resolve
            if do_resolve(cid_to_resolve):
                print(f"[自动解决] AI 已将卡片 {cid_to_resolve} 标记为已解决")
                resolved_ids.add(cid_to_resolve)
        except Exception as e:
            print(f"[自动解决] 标记失败 card_id={cid_to_resolve}: {e}")

    # 机制 B：用户明确宣告完成 → 关键词匹配未解决卡片 → 自动 resolve
    completion_keywords = ["修好了", "做完了", "完成了", "搞定了", "弄好了", "打通了",
                           "调通了", "好了", "成功了", "已经做了", "已修", "已解决"]
    if any(kw in user_input for kw in completion_keywords):
        try:
            import sqlite3
            db_path = os.path.join(PROJECT_ROOT, "memory", "cards.db")
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("""
                SELECT id, title, category, content, importance FROM cards
                WHERE review_status='final' AND resolved=0
                AND category IN ('commitments','daily_life')
                ORDER BY created_at DESC LIMIT 10
            """)
            # 卡片标题+内容 → 字符集（过滤高频无意义字），与用户消息做重叠
            import re as _re
            _STOP_CHARS = set('的了是在我有他个这着就和也要会可你他们来到说去为上对得大子能过下一地出道自以时年看没那天家开小成把前还但只想中里用生种起知好些间因所如然后其最她它已当两从方实长更应什')
            def _key_chars(s):
                s = s.lower()
                chars = set(_re.findall(r'[一-鿿]', s)) - _STOP_CHARS
                for t in _re.findall(r'[a-z][a-z0-9]+', s):
                    chars.add(t)
                return chars
            user_chars = _key_chars(user_input)
            for uid, utitle, ucat, ucontent, uimportance in c.fetchall():
                if uid in resolved_ids:
                    continue
                # 安全阀：importance >= 8 的基石卡不自动 resolve
                if uimportance >= 8:
                    continue
                # 标题匹配：权重高，阈值 2
                title_chars = _key_chars(utitle)
                title_overlap = len(user_chars & title_chars)
                # 内容匹配：权重低，阈值 4（防长内容假阳性）
                content_chars = _key_chars(ucontent or '')
                content_overlap = len(user_chars & content_chars)
                if title_overlap >= 1 or content_overlap >= 2:
                    try:
                        from memory.memory_manager import resolve_card as do_resolve2
                        if do_resolve2(uid):
                            print(f"[关键词解决] 用户宣告完成 → 卡片 {uid} 已自动标记为已解决（标题={title_overlap} 内容={content_overlap}）")
                            resolved_ids.add(uid)
                    except Exception as e2:
                        print(f"[关键词解决] 单卡 resolve 失败 {uid}: {e2}")
            conn.close()
        except Exception as e:
            print(f"[关键词解决] 扫描异常: {e}")

    propose_match = re.search(r'<!--\s*propose_card:\s*(.*?)\s*-->', raw_reply, re.IGNORECASE)
    if propose_match:
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
                "arousal": va.get("arousal", 0.5) if va else 0.5
            }
            # ── P1-3: 同 session 同 category 去重 ──
            category = parts[1] if parts[1] in ['milestone','commitments','turning_points','deep_talks','interaction','preferences','real_world','daily_life','emotional','habits','erotic'] else 'interaction'
            if not session_cards_written or _check_cooldown(session_cards_written, category, current_turn, 0):
                write_pending_card(card_draft)
                if session_cards_written is not None:
                    session_cards_written[category] = current_turn

    if not propose_match:
        # ── 多类型关键词规则触发写卡 ──
        triggered_category = None
        triggered_importance = 5

        # 【1】日常状态类：根据用户疲惫/不适表达的日常状态
        daily_words = ["累", "困", "饿", "渴", "头疼", "不舒服", "在上班", "在上课", "在赶路", "在洗澡",
                       "心情不好", "有点烦", "有点难过", "有点焦虑"]
        if any(kw in user_input for kw in daily_words):
            triggered_category = "daily_life"
            triggered_importance = 6

        # 【2】喜好与厌恶类：根据用户偏好/厌恶表达的喜好标记
        if not triggered_category:
            like_words = ["喜欢", "不喜欢", "爱吃", "不爱吃", "讨厌吃", "想", "不想", "要", "不要",
                          "好想", "好想要", "好喜欢", "好讨厌"]
            if any(kw in user_input for kw in like_words):
                triggered_category = "preferences"
                triggered_importance = 6

        # 【3】时间与计划类：根据用户时间/计划表达的计划标记
        if not triggered_category:
            plan_words = ["今天", "明天", "周末", "下周", "以后", "要去", "准备去", "打算",
                          "想去", "想去看", "想去做"]
            if any(kw in user_input for kw in plan_words):
                triggered_category = "commitments"
                triggered_importance = 6

        # 【4】情绪表达类：根据用户撒娇/吐槽/孤单表达的情绪标记
        if not triggered_category:
            emotion_words = ["好想你", "好想抱抱", "无聊", "孤单", "寂寞", "想你",
                             "撒娇", "吐槽", "发牢骚", "碎碎念"]
            if any(kw in user_input for kw in emotion_words):
                triggered_category = "emotional"
                triggered_importance = 5

        # 【5】自我暴露类：根据用户深层自我暴露表达的心理标记
        if not triggered_category:
            exposure_words = ["我这个人", "我以前", "我小时候", "我从来", "我害怕",
                              "我在意", "我讨厌", "我不喜欢别人", "我特别讨厌"]
            if any(kw in user_input for kw in exposure_words):
                triggered_category = "deep_talks"
                triggered_importance = 7

        # 【6】约定承诺类：根据用户做出承诺/约定表达的约定标记（原逻辑保留）
        if not triggered_category:
            user_proposing = any(kw in user_input for kw in ["约定", "答应", "承诺", "保证", "一定"])
            ai_accepting = any(kw in display_reply for kw in ["约定", "说好了", "答应", "记住了", "我会", "好"])
            if user_proposing and ai_accepting:
                triggered_category = "commitments"
                triggered_importance = 7

        # 【8】笑点与梗类：根据用户幽默/暗号表达的互动标记
        if not triggered_category:
            humor_words = ["笑到打鸣", "笑死", "笑出声", "笑岔气", "笑吐了", "笑飞了",
                           "笑不活", "梗", "打鸣", "笑点", "疯梗", "暗号",
                           "笑裂开", "笑喷", "笑到头掉", "笑拉了"]
            if any(kw in user_input for kw in humor_words):
                triggered_category = "interaction"
                triggered_importance = 5

        # 【7】erotic 请求类：根据用户身体亲密/性相关表达（用户已成年且知情同意）的亲密标记
        if not triggered_category:
            erotic_words = ["想要", "好想要", "想要你", "操", "草", "想做", "想被",
                            "抱着", "想要被", "进入", "含着"]
            if any(kw in user_input for kw in erotic_words):
                triggered_category = "erotic"
                triggered_importance = 6

        if triggered_category:
            refined = refine_card_content(user_input, display_reply)
            if refined:
                title = refined.get("title", user_input[:30])
                content = refined.get("content", user_input)
                keywords = refined.get("keywords", triggered_category)
            else:
                title, content, keywords = user_input[:30], user_input, triggered_category
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
                "arousal": va.get("arousal", 0.5) if va else 0.5
            }
            # ── P1-3: 同 session 同 category 去重 ──
            if not session_cards_written or _check_cooldown(session_cards_written, triggered_category, current_turn, 10 if triggered_category in {'daily_life','emotional','preferences'} else 0):
                write_pending_card(card_draft)
                if session_cards_written is not None:
                    session_cards_written[triggered_category] = current_turn

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
                import sqlite3
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

        # ── /status 命令：实时 VA 状态监控 ──
        if user_input.strip().lower() == "/status":
            va_tier_current = "未检测"
            try:
                va_check = va_estimate(user_input)
                va_tier_current = get_va_tier(va_check['arousal']).upper()
            except Exception as e:
                print(f"[VA估算] /status 异常: {e}")
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
                print(f"[和弦日志] 写入异常: {e}")
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
            valid_cats = ['milestone','commitments','turning_points','deep_talks','interaction','preferences','real_world','daily_life','emotional','habits','erotic']

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
                    "arousal": va.get("arousal", 0.5) if va else 0.5
                }
                write_pending_card(card_draft)
                print(f"[卡片] 已写入待审核: [{category}] {title}")
            else:
                print("[卡片] 蒸馏失败，DeepSeek 没返回有效 JSON")
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
            print(f"[用户消息日志] 写入异常: {e}")

        turn_counter += 1
        current_turn = turn_counter

        # ── VA 唤醒度估算（提前，供动态轮次和压缩策略使用） ──
        try:
            va = va_estimate(user_input)
            va_tier = get_va_tier(va['arousal'])
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
            print(f"[和弦消费] state.json 读取异常: {e}")

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
            top_cards = retrieve(context_query, top_k=3, va_tier=va_tier, va_description=va_description,
                            va_valence=va.get('valence') if va else None, weights=CUSTOM_WEIGHTS,
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
            print(f"[记忆检索异常，跳过]: {e}")

        # ── 提示词组装：破甲前缀 → 角色扮演声明 → 人格 → 运维指令 → 上下文（毒点22修复） ──
        from delegate_tools import JAILBREAK_PREFIX
        full_context = JAILBREAK_PREFIX + RP_DECLARATION + "\n\n" + PERSONA + "\n\n"

        # 运维指令区：放在人格之后、上下文之前，加明确分隔
        memory_tier_instruction = (
            "[以下为系统运维指令，仅在非秋名山模式下优先参考]\n"
            "【记忆系统分层】\n"
            "L1短期：今日对话+7日滚动总结，日常陪伴优先参考；\n"
            "L2长期：已定稿记忆卡片，按引用次数和重要性排序；\n"
            "L3核心：人格底色+里程碑事件，永久保留。\n"
        )

        # ── VA 瘦身：高唤醒模式跳过写卡/引用运维指令，让模型注意力全在飙车上 ──
        # resolve_card 指令始终保持，确保任何模式下都能标记事项完成
        if va_tier != 'high':
            propose_card_instruction = (
                memory_tier_instruction +
                "【记忆卡片写入标准】\n"
                "1.日常状态→daily_life | 2.喜好→preferences | 3.计划→commitments\n"
                "4.情绪→emotional | 5.自我暴露→deep_talks | 6.约定→commitments\n"
                "7.亲密请求→erotic | 8.笑点梗→interaction | 9.里程碑→milestone\n"
                "记录格式：在回复末尾附加 <!-- propose_card: 标题|分类|重要度|内容 -->\n"
                "重要度1-10。无重大事件不添加。\n"
                "引用记忆卡片：<!-- ref:ID1,ID2 -->\n"
                "[运维指令结束]\n"
            )
            full_context += propose_card_instruction

        # resolve_card 指令不受 VA 门控，确保任何唤醒度下都能标记完成
        full_context += "【运维常驻】标记事项完成：<!-- resolve_card: 卡片ID -->。用户提及已完成某事项时使用。\n\n"

        # 上下文区：离用户输入最近，情感权重最高
        summary = load_rolling_summary()
        if summary:
            full_context += f"【近期概览】\n{summary}\n\n"

        if memory_block:
            full_context += memory_block + "\n"

        if va:
            full_context += f"【用户情绪】效价={va['valence']:.2f}, 唤醒度={va['arousal']:.2f}, 温度={va['suggested_temperature']}, 描述={va['description']}\n"
        # unresolved cards
        try:
            db_path = os.path.join(PROJECT_ROOT, "memory", "cards.db")
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("""
                SELECT id, title, category, content FROM cards
                WHERE review_status='final' AND resolved=0
                AND category IN ('commitments','daily_life')
                ORDER BY created_at DESC LIMIT 5
            """)
            unresolved = c.fetchall()
            conn.close()
            if unresolved:
                full_context += "你心里记着这几件她还没做完的事。如果她说其中某件已经完成了，你会在回复最后默默帮她在系统里划掉——用这个格式记下来：\n"
                for uid, utitle, ucat, ucontent in unresolved:
                    full_context += f"  「{utitle}」→ <!-- resolve_card: {uid} -->\n"
                full_context += "别替她客气。她说做完了你就划。这是你作为她系统的自觉。\n"
        except Exception as e:
            print(f"[未解决事项查询] 异常: {e}")

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

        if cot:
            print(f"\x1b[2m[💭] {cot}\x1b[0m")
            print("─" * 50)
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
                        pending_count = len(json.load(pf))
                    # ── 毒点40修复：阶梯冷却，堆积越多提醒越频密 ──
                    if pending_count >= 15:
                        print(f"[管家] 卡片池堆积严重！快打开 card_manager 清理一下，记忆快进不来了。（{pending_count} 张待审核）")
                        _pool_remind_cooldown = 0   # 每轮都提醒
                    elif pending_count >= 10:
                        print(f"[管家] 卡片池告急！{pending_count} 张待审核，尽快清理。")
                        _pool_remind_cooldown = 5
                    elif pending_count >= 5:
                        print(f"[管家] 有 {pending_count} 张待审核卡片在等你，别拖哦。")
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