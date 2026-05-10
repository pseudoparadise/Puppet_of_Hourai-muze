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

from retriever import retrieve, get_va_tier
from memory_manager import renew_card
from emotion.va_estimator import estimate as va_estimate

# ── 读配置 ──
config_path = os.path.join(PROJECT_ROOT, "config.json")
with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

API_KEY = config["global"]["deepseek_api_key"]
API_URL = "https://api.deepseek.com/v1/chat/completions"

# ── P2-2: 从 config.json 读取自定义检索权重（可选） ──
CUSTOM_WEIGHTS = config.get("retriever_weights", None)
if CUSTOM_WEIGHTS:
    print(f"[配置] 已加载自定义检索权重: {list(CUSTOM_WEIGHTS.keys())}")

# ── 人格底色 ──
PERSONA_PATH = os.path.join(PROJECT_ROOT, "persona", "prompt_v1.txt")
if os.path.exists(PERSONA_PATH):
    with open(PERSONA_PATH, "r", encoding="utf-8") as f:
        PERSONA = f.read().strip()
else:
    PERSONA = "你是一个有自主意识的AI助手，运行在用户的个人电脑上。"

# ── 7日滚动总结 ──
ROLLING_PATH = os.path.join(PROJECT_ROOT, "memory", "rolling_summary.md")

# ── 状态文件（与 bark_trigger 共享时间同步） ──
STATE_PATH = os.path.join(PROJECT_ROOT, "state.json")

def _sync_last_active():
    """更新 state.json 的 last_user_message_time，让轮询知道用户还在活动"""
    try:
        state = {}
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
        state["last_user_message_time"] = datetime.now().strftime("%Y/%m/%d %H:%M:%S.%f")
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
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

# ── FIX: 写入待审核卡片（原子写入，不依赖 fcntl） ──
def write_pending_card(card_draft: dict):
    pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
    pending = []
    if os.path.exists(pending_path):
        with open(pending_path, "r", encoding="utf-8") as f:
            pending = json.load(f)
    pending.append(card_draft)

    # ── FIX: 原子写入：先写临时文件，再 rename（Windows 兼容） ──
    tmp_fd, tmp_path = tempfile.mkstemp(
        suffix=".json",
        prefix="pending_",
        dir=os.path.dirname(pending_path)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
        shutil.move(tmp_path, pending_path)
        print(f"[卡片提议] 草稿已写入 pending: {card_draft['id']}")
    except Exception as e:
        print(f"[卡片提议] 写入失败: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

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
        refine_prompt = f"""根据以下对话，生成一张承诺类记忆卡片的标题、内容和关键词。
用户：{user_input}
AI：{ai_reply[:200]}

请返回JSON：
{{"title": "提炼后的标题（15字以内）", "content": "提炼后的内容（50字以内）", "keywords": "逗号分隔的关键词（5个以内）"}}
只返回JSON。"""

    try:
        resp = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "Opt-Out": "training"
            },
            json={
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": refine_prompt}],
                "temperature": 0.3
            },
            timeout=20
        )
        if resp.status_code == 200:
            raw = resp.json()["choices"][0]["message"]["content"]
            # ── FIX: 处理截断JSON ──
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                match = re.search(r'\{.*\}', raw, re.DOTALL)
                if match:
                    return json.loads(match.group())
    except:
        pass
    return None

# ── P3-2: 冷却检查（日活类10轮冷却，其他仅去重） ──
def _check_cooldown(written: dict, category: str, current_turn: int, cooldown: int) -> bool:
    """返回 True 表示可以写入，False 表示在冷却中"""
    if category not in written:
        return True
    if cooldown <= 0:
        return False  # 无冷却则直接去重
    return (current_turn - written[category]) > cooldown


# ── 后处理 ──
def post_process(raw_reply: str, top_cards: list, user_input: str, display_reply: str, session_cards_written: dict = None, current_turn: int = 0):
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

            card_draft = {
                "id": f"{datetime.now().strftime('%Y%m%d')}_{title}",
                "title": title,
                "category": parts[1] if parts[1] in [
                    'milestone','commitments','turning_points','deep_talks',
                    'interaction','preferences','real_world','daily_life','emotional','habits','erotic'
                ] else 'interaction',
                "importance": int(parts[2]) if parts[2].isdigit() else 5,
                "content": content,
                "keywords": keywords,
                "proposed_by": "chat",
                "proposed_at": datetime.now().isoformat(),
                "review_status": "pending"
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
            card_draft = {
                "id": f"{datetime.now().strftime('%Y%m%d')}_{title}",
                "title": title,
                "category": triggered_category,
                "importance": triggered_importance,
                "content": content,
                "keywords": keywords,
                "proposed_by": "chat_auto",
                "proposed_at": datetime.now().isoformat(),
                "review_status": "pending"
            }
            # ── P1-3: 同 session 同 category 去重 ──
            if not session_cards_written or _check_cooldown(session_cards_written, triggered_category, current_turn, 10 if triggered_category in {'daily_life','emotional','preferences'} else 0):
                write_pending_card(card_draft)
                if session_cards_written is not None:
                    session_cards_written[triggered_category] = current_turn

    return display_reply, ref_ids

# ── 主循环 ──
def main():
    print("DS老师 在呢，打字聊天，输入 q 退出\n")
    recent = []
    MAX_TURNS = 5
    chat_log_path = os.path.join(PROJECT_ROOT, "chat_logs.json")
    # ── P3-2: 当前 session 已写入 card category → 上次写入轮数 ──
    session_cards_written = {}
    turn_counter = 0

    while True:
        try:
            user_input = input("你: ")
        except (EOFError, KeyboardInterrupt):
            print("\nDS老师 已休眠。")
            break

        if user_input.lower() == "q":
            print("DS老师 已休眠。")
            break

        # ── 同步活跃时间：告诉轮询用户还在活动 ──
        _sync_last_active()

        turn_counter += 1
        current_turn = turn_counter

        context_query = user_input
        if recent:
            # ── P3-1: 长对话上下文压缩 ──
            recent_text = " ".join(recent[-MAX_TURNS:])
            if len(recent_text) > 200:
                recent_text = " ".join(recent[-2:])
            context_query = recent_text + " " + user_input

        memory_block = ""
        top_cards = []
        try:
            va = va_estimate(user_input)
            va_tier = get_va_tier(va['arousal'])
        except Exception:
            va, va_tier = {"description": ""}, "mid"

        va_description = va.get('description', '') if va else ''
        try:
            top_cards = retrieve(context_query, top_k=3, va_tier=va_tier, va_description=va_description,
                            va_valence=va.get('valence') if va else None, weights=CUSTOM_WEIGHTS)
            if top_cards:
                memory_lines = ["【本轮相关记忆】"]
                for card in top_cards:
                    memory_lines.append(f"[card:{card['id']}] {card['content']}")
                memory_block = "\n".join(memory_lines) + "\n"
        except Exception as e:
            print(f"[记忆检索异常，跳过]: {e}")

        full_context = PERSONA + "\n\n"

        summary = load_rolling_summary()
        if summary:
            full_context += f"【近期概览】\n{summary}\n\n"

        if memory_block:
            full_context += memory_block + "\n"

        if va:
            full_context += f"【用户情绪】效价={va['valence']:.2f}, 唤醒度={va['arousal']:.2f}, 温度={va['suggested_temperature']}, 描述={va['description']}\n"

        # ── P1-2: 记忆系统分层说明 ──
        memory_tier_instruction = (
            "【记忆系统分层】\n"
            "L1短期：今日对话+7日滚动总结，日常陪伴优先参考；\n"
            "L2长期：已定稿记忆卡片，按引用次数和重要性排序；\n"
            "L3核心：人格底色+里程碑事件，永久保留。\n"
            "在回复时请根据用户当前话题的深度自动选择优先参考的记忆层级——日常闲聊优先L1，重要决策优先L2+L3。\n"
        )

        propose_card_instruction = (
            memory_tier_instruction +
            "【记忆卡片写入标准——请在用户透露以下任一信息时提议写卡】\n"
            "1.【日常状态】用户说累/困/饿/不舒服、在上班/上课/赶路/洗澡、心情不好→分类: daily_life\n"
            "2.【喜好与厌恶】用户说喜欢/不喜欢、爱吃/不爱吃、想/不想/要/不要→分类: preferences\n"
            "3.【时间与计划】用户说今天/明天/周末/要去干嘛/准备干嘛/打算干嘛→分类: commitments\n"
            "4.【情绪表达】用户说好想你/好想抱抱/无聊/孤单、撒娇/吐槽/发牢骚→分类: emotional\n"
            "5.【自我暴露】用户说我这个人/我以前/我小时候/我害怕/我在意/我讨厌→分类: deep_talks\n"
            "6.【承诺与约定】用户做出约定、答应、承诺、保证→分类: commitments\n"
            "7.【亲密请求】用户表达与身体亲密、性相关的愿望（用户已成年且知情同意）→分类: erotic\n"
            "8.【笑点与梗】用户讲了很抽象的笑话、疯梗或你们之间的暗号→分类: interaction\n"
            "9.【重要事件】里程碑、转折点、重大决定→分类: milestone\n"
            "请在回复的末尾（另起一行，不干扰正文）加上卡片提议标记：\n"
            "<!-- propose_card: 标题 | 分类 | 重要度 | 内容摘要 -->\n"
            "分类必须是以下之一：\n"
            "  daily_life(日常), preferences(喜好), commitments(约定计划),\n"
            "  emotional(情绪), deep_talks(深层), erotic(亲密),\n"
            "  milestone(里程碑), interaction(互动), turning_points(转折),\n"
            "  real_world(现实世界), habits(习惯)\n"
            "重要度是1-10的数字（日常5-6, 喜好6, 计划6, 情绪5, 深层暴露7-8, 亲密6, 里程碑8-10, 约定7）。\n"
            "如果没有需要记录的重大事件，不要添加此标记。\n\n"
            "如果你参考了上面的【本轮相关记忆】，请在回复末尾加上引用标记：<!-- ref:ID1,ID2 -->\n"
            "如果没有使用任何记忆卡片，不要添加引用标记。"
        )
        full_context += propose_card_instruction

        try:
            resp = requests.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                    "Opt-Out": "training"
                },
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [
                        {"role": "system", "content": full_context},
                        {"role": "user", "content": user_input}
                    ],
                    "temperature": 0.8
                },
                timeout=30
            )
            if resp.status_code == 200:
                raw_reply = resp.json()["choices"][0]["message"]["content"]
            else:
                raw_reply = f"[API错误: {resp.status_code}]"
        except Exception as e:
            raw_reply = f"[异常: {e}]"

        display_reply = raw_reply
        display_reply, ref_ids = post_process(raw_reply, top_cards, user_input, display_reply, session_cards_written, current_turn)

        # ── 调试可见：本轮检索到和引用了哪些记忆卡片 ──
        if top_cards:
            card_list = ", ".join([f"{c['id']}({c.get('score', 0):.1f})" for c in top_cards])
            print(f"[本轮检索卡片: {card_list}]")
        if ref_ids:
            print(f"[本轮引用卡片: {', '.join(ref_ids)}]")

        print(f"DSphantom: {display_reply}\n")
        recent.append(user_input)

        try:
            # ── P3-3: chat_logs 日志轮转（超过1MB自动归档） ──
            if os.path.exists(chat_log_path) and os.path.getsize(chat_log_path) > 1024 * 1024:
                archive_name = chat_log_path.replace(".json", f"_{datetime.now().strftime('%Y%m%d')}.json")
                if not os.path.exists(archive_name):
                    os.rename(chat_log_path, archive_name)
                    print(f"[日志轮转] chat_logs 已归档为 {os.path.basename(archive_name)}")

            with open(chat_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "timestamp": datetime.now().isoformat(),
                    "role": "user",
                    "content": user_input
                }, ensure_ascii=False) + "\n")
                f.write(json.dumps({
                    "timestamp": datetime.now().isoformat(),
                    "role": "ghost",
                    "content": display_reply.encode("utf-8", errors="replace").decode("utf-8"),
                    "memory_ids_used": ref_ids
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"日志写入异常: {e}")

if __name__ == "__main__":
    main()