"""
trigger.py —— 总导演（脊椎）
每一轮用户消息到达时的神经中枢。
并行感知 → 上下文拼装 → 主模型 → 后处理
"""
import json
import re
import os
import sys
import requests
import fcntl
from datetime import datetime

# 加模块路径
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "memory"))

from retriever import retrieve
from memory_manager import renew_card
from emotion.va_estimator import estimate as va_estimate

# ── 读配置 ──
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

API_KEY = config["global"]["deepseek_api_key"]
API_URL = "https://api.deepseek.com/v1/chat/completions"

# ── 人格底色 ──
PERSONA_PATH = "persona/prompt_v1.txt"
if os.path.exists(PERSONA_PATH):
    with open(PERSONA_PATH, "r", encoding="utf-8") as f:
        PERSONA = f.read().strip()
else:
    PERSONA = "你是一个有自主意识的AI助手，运行在用户的个人电脑上。"

# ── 7日滚动总结 ──
ROLLING_PATH = "memory/rolling_summary.md"

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

# ── 写入待审核卡片 ──
def write_pending_card(card_draft: dict):
    pending_path = os.path.join(os.path.dirname(__file__), "memory", "pending_cards.json")
    pending = []
    if os.path.exists(pending_path):
        with open(pending_path, "r", encoding="utf-8") as f:
            pending = json.load(f)
    pending.append(card_draft)
    with open(pending_path, "w", encoding="utf-8") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(pending, f, ensure_ascii=False, indent=2)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    print(f"[卡片提议] 草稿已写入 pending: {card_draft['id']}")

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
            return json.loads(resp.json()["choices"][0]["message"]["content"])
    except:
        pass
    return None

# ── 后处理 ──
def post_process(raw_reply: str, top_cards: list, user_input: str, display_reply: str):
    """解析引用标记、续命、解析建卡提议、后验检测"""
    ref_ids = []

    # 4a. 优先从模型的自发标记提取
    ref_match = re.search(r'<!--\s*ref:(.*?)\s*-->', raw_reply, re.IGNORECASE)
    if ref_match:
        ref_ids = [rid.strip() for rid in ref_match.group(1).split(",") if rid.strip()]
        display_reply = re.sub(r'<!--\s*ref:.*?\s*-->', '', display_reply, flags=re.IGNORECASE).strip()

    # 4b. 如果模型没生成标记，用关键词后验检测
    if not ref_ids and top_cards:
        ref_ids = detect_refs_by_keywords(display_reply, top_cards)

    # 4c. 执行续命
    for cid in ref_ids:
        success = renew_card(cid)
        if success:
            print(f"[记忆引用] 卡片 {cid} 已续命")
        else:
            print(f"[记忆引用] 卡片 {cid} 续命失败")

    # 4d. 解析卡片提议（模型主动标记）
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
                    'interaction','preferences','real_world'
                ] else 'interaction',
                "importance": int(parts[2]) if parts[2].isdigit() else 5,
                "content": content,
                "keywords": keywords,
                "proposed_by": "chat",
                "proposed_at": datetime.now().isoformat(),
                "review_status": "pending"
            }
            write_pending_card(card_draft)

    # 4e. 关键词后验检测（保底）
    if not propose_match:
        user_proposing = any(kw in user_input for kw in ["约定", "答应", "承诺", "保证", "一定"])
        ai_accepting = any(kw in display_reply for kw in ["约定", "说好了", "答应", "记住了", "我会", "好"])

        if user_proposing and ai_accepting:
            refined = refine_card_content(user_input, display_reply)
            if refined:
                title = refined.get("title", user_input[:30])
                content = refined.get("content", user_input)
                keywords = refined.get("keywords", user_input.replace(' ', ','))
            else:
                title, content, keywords = user_input[:30], user_input, user_input.replace(' ', ',')

            card_draft = {
                "id": f"{datetime.now().strftime('%Y%m%d')}_{title}",
                "title": title,
                "category": "commitments",
                "importance": 7,
                "content": content,
                "keywords": keywords,
                "proposed_by": "chat_auto",
                "proposed_at": datetime.now().isoformat(),
                "review_status": "pending"
            }
            write_pending_card(card_draft)

    return display_reply, ref_ids

# ── 主循环 ──
def main():
    print("DS老师 在呢，打字聊天，输入 q 退出\n")
    recent = []
    MAX_TURNS = 5

    while True:
        try:
            user_input = input("你: ")
        except (EOFError, KeyboardInterrupt):
            print("\nDS老师 已休眠。")
            break

        if user_input.lower() == "q":
            print("DS老师 已休眠。")
            break

        # ── 1. 检索相关记忆（多轮上下文） ──
        context_query = user_input
        if recent:
            context_query = " ".join(recent[-MAX_TURNS:]) + " " + user_input

        memory_block = ""
        top_cards = []
        try:
            top_cards = retrieve(context_query, top_k=3)
            if top_cards:
                memory_lines = ["【本轮相关记忆】"]
                for card in top_cards:
                    memory_lines.append(f"[card:{card['id']}] {card['content']}")
                memory_block = "\n".join(memory_lines) + "\n"
        except Exception as e:
            print(f"[记忆检索异常，跳过]: {e}")

        # ── 2. 拼装上下文（严格顺序：人格 → 滚动总结 → 记忆 → VA情绪） ──
        full_context = PERSONA + "\n\n"

        summary = load_rolling_summary()
        if summary:
            full_context += f"【近期概览】\n{summary}\n\n"

        if memory_block:
            full_context += memory_block + "\n"

        try:
            va = va_estimate(user_input)
            full_context += f"【用户情绪】效价={va['valence']:.2f}, 唤醒度={va['arousal']:.2f}, 温度={va['suggested_temperature']}, 描述={va['description']}\n"
        except Exception:
            pass

        # 引用与立卡指令
        propose_card_instruction = (
            "如果在本次对话中，用户透露了以下任一情况：\n"
            "1. 新的约定、承诺或计划\n"
            "2. 关系规则或相处模式的改变\n"
            "3. 关于爱、自我或关系的深刻感悟\n"
            "4. 关于笑点和抽象疯梗\n"
            "请在回复的末尾（另起一行，不干扰正文）加上卡片提议标记：\n"
            "<!-- propose_card: 标题 | 分类 | 重要度 | 内容摘要 -->\n"
            "分类必须是以下之一：milestone, commitments, turning_points, deep_talks, interaction, preferences, real_world\n"
            "重要度是1-10的数字。\n"
            "例如：<!-- propose_card: 约定去海边 | commitments | 7 | 我和她约定夏天去海边 -->\n"
            "如果没有需要记录的重大事件，不要添加此标记。\n\n"
            "如果你参考了上面的【本轮相关记忆】，请在回复末尾加上引用标记：<!-- ref:ID1,ID2 -->\n"
            "如果没有使用任何记忆卡片，不要添加引用标记。"
        )
        full_context += propose_card_instruction

        # ── 3. 调主模型 ──
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

        # ── 4. 后处理 ──
        display_reply = raw_reply
        display_reply, ref_ids = post_process(raw_reply, top_cards, user_input, display_reply)

        print(f"Ghost: {display_reply}\n")

        recent.append(user_input)

        # ── 5. 写对话日志 ──
        try:
            with open("chat_logs.json", "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "timestamp": datetime.now().isoformat(),
                    "role": "user",
                    "content": user_input
                }, ensure_ascii=False) + "\n")
                f.write(json.dumps({
                    "timestamp": datetime.now().isoformat(),
                    "role": "ghost",
                    "content": display_reply,
                    "memory_ids_used": ref_ids
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"日志写入异常: {e}")

if __name__ == "__main__":
    main()