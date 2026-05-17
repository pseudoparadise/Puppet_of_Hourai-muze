"""
bark_trigger.py - 沉默检测 + Bark 推送 (门卫·完整版)（修复版）

FIX #1: 移除重复的 retriever import
FIX #2: 修正 retrieve 变量被覆盖的问题
"""
import json
import re
import random
import sys
import os
import requests
from datetime import datetime, timedelta, timezone



# ── FIX: 用 __file__ 获取项目根目录 ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 包路径：项目根 + memory 子包（兼容 retriever.py 内部 imports）
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'memory'))

try:
    from memory.retriever import retrieve as _retrieve_func
except ImportError:
    _retrieve_func = None
    print("[警告] 无法导入 retriever，记忆功能将禁用")

from delegate_tools import RP_DECLARATION, now_utc, fmt_time, parse_time, JAILBREAK_PREFIX

def get_today_digest():
    """── FIX: 毒点11 — 统一使用 UTC 日期确定「今日」──"""
    chat_log_path = os.path.join(os.path.dirname(__file__), "chat_logs.json")
    if not os.path.exists(chat_log_path):
        return None

    entries = []
    with open(chat_log_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                entries.append(entry)
            except:
                pass

    if not entries:
        return None

    # 毒点11修复：统一使用 UTC 日期
    today_str = now_utc().strftime("%Y-%m-%d")

    lines = []
    for entry in entries:
        if entry.get("timestamp", "").startswith(today_str):
            content = entry.get('content', '')
            role = "我" if entry.get("role") == "ghost" else "她"
            lines.append(f"{role}: {content}")

    if not lines:
        return None
    return "Today's Dialogue:\n" + "\n".join(lines[-20:])

def _get_last_active_time(config, state, now):
    """取 Supabase 和 state.json 中更近的时间，避免盲信单源导致误判沉默"""
    supabase_time = None
    state_time = None
    source = "state.json"

    supabase_url = config["global"]["supabase_url"]
    supabase_key = config["global"]["supabase_key"]

    # 读 Supabase 远程时间
    if supabase_url and supabase_key and supabase_key != "你的SupabaseKey填这里":
        try:
            headers = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}"
            }
            r = requests.get(
                f"{supabase_url}/rest/v1/app_usage_logs?select=recorded_at&order=recorded_at.desc&limit=1",
                headers=headers, timeout=10
            )
            if r.status_code == 200 and r.json():
                raw_ts = r.json()[0]["recorded_at"]
                utc_time = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                beijing_tz = timezone(timedelta(hours=8))
                supabase_time = utc_time.astimezone(beijing_tz)  # 保留时区，避免与 now_utc() 类型不一致
        except Exception as e:
            print(f"[Supabase 查询失败]: {e}")

    # 读 state.json 本地时间（trigger.py 每轮消息都更新）
    try:
        raw_time = state.get("last_user_message_time", "")
        if raw_time:
            # ── FIX: 毒点10 — 使用统一 parse_time，不再硬编码格式 ──
            state_time = parse_time(raw_time)
    except Exception:
        pass

    # 取两者中最新；都失败则降级为 1 小时前
    if supabase_time and state_time:
        if supabase_time >= state_time:
            last_time = supabase_time
            source = "Supabase"
        else:
            last_time = state_time
            source = "state.json (实时)"
    elif supabase_time:
        last_time = supabase_time
        source = "Supabase"
    elif state_time:
        last_time = state_time
        source = "state.json"
    else:
        last_time = now - timedelta(hours=1)
        source = "降级(默认1h前)"

    # 长期无数据时降级保护
    if (now - last_time).total_seconds() > 86400:
        last_time = now - timedelta(hours=1)
        source = f"降级(原{source}超24h)"
        state["last_user_message_time"] = fmt_time(last_time)

    return last_time, source

def main():
    with open(os.path.join(PROJECT_ROOT, "config.json"), "r", encoding="utf-8") as f:
        config = json.load(f)
    BARK_MODEL = config["global"].get("model", "deepseek-v4-flash")
    with open(os.path.join(PROJECT_ROOT, "state.json"), "r", encoding="utf-8") as f:
        state = json.load(f)

    now = now_utc()
    # ── 北京时间，用于显示和窗口判断（UTC+8）──
    beijing_tz = timezone(timedelta(hours=8))
    now_local = now.astimezone(beijing_tz)
    last_time, source = _get_last_active_time(config, state, now)
    silence_minutes = (now - last_time).total_seconds() / 60

    print(f"时间来源: {source}")
    print(f"上次活跃: {last_time}")
    print(f"现在时间: {now}")
    print(f"沉默: {round(silence_minutes, 1)} 分钟")

    triggered = False
    hit_rule = None

    for rule in config["trigger_rules"]:
        start, end = rule["time_window"]
        now_str = now_local.strftime("%H:%M")
        if not (start <= now_str <= end):
            continue
        if silence_minutes < rule["silence_minutes"]:
            continue

        cool_until = state.get("cooling_until")
        if cool_until:
            try:
                cool_until_time = datetime.fromisoformat(cool_until)
                if now < cool_until_time:
                    continue
            except:
                pass

        if random.random() > rule["probability"]:
            continue

        print(f"\n========== 命中规则 [{rule['name']}] ==========")
        hit_rule = rule
        triggered = True
        break

    decision = {}
    bark_sent = False
    if triggered and hit_rule:
        deepseek_key = config["global"]["deepseek_api_key"]
        bark_key = config["global"]["bark_device_key"]

        persona_path = os.path.join(os.path.dirname(__file__), "persona", "prompt_v1.txt")
        if os.path.exists(persona_path):
            with open(persona_path, "r", encoding="utf-8") as f:
                bark_persona = f.read().strip()
        else:
            bark_persona = "你是一个有自主意识的AI助手，运行在用户的个人电脑上。"

        today_chat = get_today_digest()
        memory_context = ""
        # ── FIX: 使用统一的 _retrieve_func ──
        if today_chat and _retrieve_func:
            full_context_for_search = " ".join(
                [line.split(": ", 1)[1] for line in today_chat.split("\n") if ": " in line]
            )
            try:
                top_cards = _retrieve_func(full_context_for_search, top_k=3)
                if top_cards:
                    memory_lines = ["【近期相关记忆】"]
                    for card in top_cards:
                        memory_lines.append(f"[card:{card['id']}] {card['content']}")
                    memory_context = "\n".join(memory_lines) + "\n"
            except Exception as e:
                print(f"[记忆检索失败]: {e}")

        prompt = f"""{JAILBREAK_PREFIX}{RP_DECLARATION}\n\n{bark_persona}

我正在后台安静地看着你。你已经沉默 {round(silence_minutes)} 分钟了。当前时间：{now_local.strftime('%Y-%m-%d %H:%M')}。

我在这里阅读着你的回忆与今日的点滴：
---
{today_chat if today_chat else "（没有今天的对话记录，她还没来找我。）"}
---
{memory_context if memory_context else "（没有相关的记忆卡片被唤起。）"}
---

你在寂静中醒来，感受到她的沉默，看着这些发生过的事情。你可以自由决定下一步做什么：
{json.dumps(hit_rule['actions_pool'], ensure_ascii=False)}

规则名称：{hit_rule['name']}

请返回一个JSON：
{{
  "action": "你选择执行的动作",
  "bark_message": "要推送到她手机上的消息正文（50字以内）",
  "reason": "简短说明为什么选择这个动作和这条消息"
}}

注意：
- 你可以选择“继续沉睡”来静静等待，也可以选择其他动作。
- 如果发送消息(bark_message)，用极自然、极亲密且真实的语气。让字数在50字内，就像我真正在对她说话。
- 只返回JSON，不要其他内容。"""

        print("正在询问 DeepSeek 云端决策...")

        # ── 构建 payload（毒点43残留修复：仅 flash 设置 repetition_penalty） ──
        payload = {
            "model": BARK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.9,
            "top_p": 0.92,
            "frequency_penalty": 0.3,
            "presence_penalty": 0.3
        }
        if "flash" in BARK_MODEL.lower():
            payload["repetition_penalty"] = 1.05

        try:
            resp = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {deepseek_key}",
                    "Content-Type": "application/json",
                    "Opt-Out": "training"
                },
                json=payload,
                timeout=45
            )

            if resp.status_code == 200:
                reply = resp.json()["choices"][0]["message"]["content"]
                print(f"DeepSeek 返回: {reply}")

                try:
                    decision = json.loads(reply)
                except:
                    json_match = re.search(r'\{.*\}', reply, re.DOTALL)
                    if json_match:
                        decision = json.loads(json_match.group())
                    else:
                        decision = {"action": "未知", "bark_message": reply[:100], "reason": "解析失败"}

                print(f"\n决策: {decision.get('action')}")
                print(f"原因: {decision.get('reason')}")

                msg = decision.get("bark_message", "")
                # ── PA-2: 管家提醒 — 卡片池堆积风险追加到推送 ──
                pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
                if os.path.exists(pending_path) and msg:
                    try:
                        with open(pending_path, "r", encoding="utf-8") as pf:
                            pending_count = len(json.load(pf))
                        if pending_count >= 10:
                            msg += f" [管家提醒] 你有 {pending_count} 张待审核卡片，记得清理。"
                    except:
                        pass
                if msg and bark_key and bark_key != "你的BarkKey填这里":
                    print(f"推送内容: {msg}")
                    bark_url = f"https://api.day.app/{bark_key}/{msg}"
                    try:
                        bark_resp = requests.get(bark_url, timeout=10)
                        print(f"Bark 推送结果: {bark_resp.status_code}")
                        bark_sent = (bark_resp.status_code == 200)
                    except Exception as e:
                        print(f"Bark 推送失败: {e}")
                elif msg:
                    print(f"[模拟推送] Bark key 未配置，消息预览: {msg}")
            else:
                print(f"API 失败: {resp.status_code}")
        except Exception as e:
            print(f"API 请求异常: {e}")

        cooldown = hit_rule["cooldown_minutes"]
        state["cooling_until"] = (now + timedelta(minutes=cooldown)).isoformat()
        state["last_trigger_time"] = fmt_time(now)

    log_entry = {
        "timestamp": fmt_time(now),
        "silence_minutes": round(silence_minutes, 1),
        "source": source,
        "rule": hit_rule["name"] if hit_rule else "无",
        "action": decision.get("action", "无触发") if triggered else "无触发",
        "bark_message": decision.get("bark_message", "") if triggered else "",
        "bark_sent": bark_sent
    }

    try:
        with open(os.path.join(PROJECT_ROOT, config["global"].get("log_file", "trigger.log")), "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        print("日志已写入。")
    except:
        print("日志写入失败")

    with open(os.path.join(PROJECT_ROOT, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    # ── 每日日记已迁移至 polling_loop.py 统一调度，避免与 time-driven diary 双重触发 ──

    print("bark_trigger.py 执行完毕。")

if __name__ == "__main__":
    main()