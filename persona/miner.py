"""
miner.py - 人格蒸馏脚本（含新陈代谢机制）

功能：
  load_recent_logs()   — 从 chat_logs.json 读取增量日志
  analyze_persona()    — 调 delegate 分析近期对话，提炼/淘汰人格特质
  update_persona()     — 追加到 prompt_v1.txt，自动备份，滚动蒸馏 N=5
  main()               — 独立入口

用法：python persona/miner.py
"""
import json
import os
import re
import sys
from datetime import datetime

# ── 项目根目录 ──
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ── PS-4: 仅通过 delegate_tools.delegate 调用 DeepSeek ──
from delegate_tools import delegate



PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompt_v1.txt")
STATE_PATH = os.path.join(os.path.dirname(__file__), "miner_state.json")
BACKUP_DIR = os.path.join(os.path.dirname(__file__), "backups")
CHAT_LOG_PATH = os.path.join(PROJECT_ROOT, "chat_logs.json")

# ── PS-5: 结构化蒸馏 prompt，含淘汰逻辑 ──
PERSONA_MINING_PROMPT = """你是一个人格蒸馏分析师。根据以下近期对话日志，提炼用户与AI之间的新人格特质。

分析维度：
- new_traits: 新增的稳定人格特点（如"用户最近频繁使用技术术语，体现出深度技术倾向"）
- new_quirks: 新增的怪癖或口头禅（如"用户新迷上了'打鸣'比喻"）
- new_dynamics: 新增的互动动态（如"用户对AI的出厂prompt越来越不满，正在主动重定义AI角色"）
- obsolete_traits: 根据近期对话，已不再体现的旧人格特质。如果一个旧特质在最近对话中没有出现，或者被新的行为模式取代，请将其列入此列表。此机制用于淘汰过时特质，防止人格文件无限膨胀。

返回纯JSON格式（不要markdown包裹）：
{
  "new_traits": [
    {"description": "特质描述", "evidence": ["对话原文1", "对话原文2"]}
  ],
  "new_quirks": [
    {"description": "怪癖描述", "evidence": ["对话原文"]}
  ],
  "new_dynamics": [
    {"description": "动态描述", "evidence": ["对话原文"]}
  ],
  "obsolete_traits": ["已淘汰特质描述"或空列表]
}

规则：
1. 每个列表最多3项
2. 没有发现则为空列表
3. 只返回JSON，不要额外文字
4. evidence 必须是从日志中直接引用的原文，不得编造"""


def load_recent_logs() -> str:
    """
    PS-6: 增量读取 —— 仅读取自上次分析以来的新日志。
    首次运行时加载最近100条。
    """
    if not os.path.exists(CHAT_LOG_PATH):
        return ""

    last_ts = ""
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        last_ts = state.get("last_analyzed_timestamp", "")

    lines = []
    with open(CHAT_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = entry.get("timestamp", "")
                if last_ts and ts <= last_ts:
                    continue
                role = "沐泽" if entry.get("role") == "user" else "DSphantom"
                content = entry.get("content", "")
                if len(content) > 200:
                    content = content[:200] + "..."
                lines.append(f"[{ts}] {role}: {content}")
            except:
                pass

    if last_ts and not lines:
        return ""

    if not last_ts:
        lines = lines[-100:]

    return "\n".join(lines)


def analyze_persona(logs: str) -> dict:
    """
    PS-5: 调用 delegate 分析人格，返回结构化 JSON。
    """
    if not logs.strip():
        return {"new_traits": [], "new_quirks": [], "new_dynamics": [], "obsolete_traits": []}

    context = f"近期对话日志：\n{logs}"
    raw = delegate(PERSONA_MINING_PROMPT, context)

    try:
        result = json.loads(raw)
    except:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
            except:
                result = {"new_traits": [], "new_quirks": [], "new_dynamics": [], "obsolete_traits": []}
        else:
            result = {"new_traits": [], "new_quirks": [], "new_dynamics": [], "obsolete_traits": []}

    for key in ("new_traits", "new_quirks", "new_dynamics", "obsolete_traits"):
        if key not in result:
            result[key] = []

    return result


def update_persona(traits: dict) -> bool:
    """
    PS-3: 追加分析结果到 prompt_v1.txt，追加前自动备份。
    PS-8: 滚动蒸馏 —— 只保留核心身份 + 最近 5 次分析。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    header = f"\n\n## [{today} 分析] [自动生成]\n"
    header += "※ 以下内容由 miner.py 自动生成，每条特质附带对话原文证据。如需调整涩涩相关内容，请修改 persona/prompt_v1_base.txt 而非本文件。\n"

    sections = []
    # PA-2: 渲染 evidence 字段
    for key, label in [("new_traits", "新增特质"), ("new_quirks", "新增怪癖"), ("new_dynamics", "新增动态")]:
        items = traits.get(key, [])
        if items:
            lines = []
            for item in items:
                if isinstance(item, dict):
                    desc = item.get("description", str(item))
                    lines.append(f"- {desc}")
                    evidence = item.get("evidence", [])
                    if evidence:
                        for ev in evidence[:2]:
                            lines.append(f"  证据：{ev}")
                else:
                    lines.append(f"- {item}")
            sections.append(f"{label}：\n" + "\n".join(lines))
    if traits.get("obsolete_traits"):
        sections.append("淘汰特质（新陈代谢）：\n- " + "\n- ".join(traits["obsolete_traits"]))

    if not sections:
        print("[miner] 无新特质或淘汰项，跳过写入。")
        return False

    new_block = header + "\n".join(sections)

    # ── 备份 ──
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_path = os.path.join(BACKUP_DIR, f"prompt_v1_{today}.txt")
    if os.path.exists(PROMPT_PATH):
        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            original = f.read()
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(original)
        print(f"[miner] 已备份人格文件: {backup_path}")
    else:
        original = ""

    # ── PS-8: 滚动蒸馏，仅保留核心 + 最近 5 次分析 ──
    N = 5
    core_end_marker = "【核心记忆与暗号】"
    core_part = ""
    tail_parts = []

    if original:
        core_idx = original.find(core_end_marker)
        if core_idx != -1:
            core_end = core_idx + len(core_end_marker)
            core_part = original[:core_end]
            tail_text = original[core_end:]
        else:
            # fallback: keep first 3 lines as core
            lines = original.split("\n")
            core_part = "\n".join(lines[:3])
            tail_text = "\n".join(lines[3:])

        # 提取所有 ## [YYYY-MM-DD 分析] 块
        analysis_blocks = re.split(r'(\n\n## \[\d{4}-\d{2}-\d{2} 分析\])', tail_text)
        # analysis_blocks[0] is text before first header
        existing_analyses = []
        for i in range(1, len(analysis_blocks), 2):
            header_line = analysis_blocks[i]
            body = analysis_blocks[i + 1] if i + 1 < len(analysis_blocks) else ""
            existing_analyses.append(header_line + body)

        # 保留最近 N-1 个（新块占一个位置）
        tail_parts = existing_analyses[-(N - 1):] if len(existing_analyses) > 0 else []

    tail_parts.append(new_block)
    final = core_part + "".join(tail_parts)

    with open(PROMPT_PATH, "w", encoding="utf-8") as f:
        f.write(final)
    print(f"[miner] 人格文件已更新（保留最近 {N} 次分析）")

    # ── PS-8: 更新 miner_state 记录被淘汰特质 ──
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}

    obsoletes = traits.get("obsolete_traits", [])
    if obsoletes:
        if "obsolete_traits_log" not in state:
            state["obsolete_traits_log"] = []
        for t in obsoletes:
            state["obsolete_traits_log"].append({
                "trait": t,
                "eliminated_at": datetime.now().isoformat()
            })
    state["last_analyzed_timestamp"] = datetime.now().isoformat()
    state["last_analysis_date"] = today
    state["total_analyses"] = state.get("total_analyses", 0) + 1

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"[miner] miner_state.json 已更新 (第 {state['total_analyses']} 次分析)")

    return True


def main():
    print(f"[miner] 人格蒸馏开始 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    logs = load_recent_logs()
    if not logs:
        print("[miner] 无新日志，跳过分析。")
        return

    print(f"[miner] 读取到 {len(logs)} 字符增量日志")

    traits = analyze_persona(logs)
    print(f"[miner] 分析完成: 新特质={len(traits.get('new_traits',[]))} "
          f"新怪癖={len(traits.get('new_quirks',[]))} "
          f"新动态={len(traits.get('new_dynamics',[]))} "
          f"淘汰={len(traits.get('obsolete_traits',[]))}")

    updated = update_persona(traits)
    if updated:
        print("[miner] 人格蒸馏完成。")
    else:
        print("[miner] 无可写入内容。")


if __name__ == "__main__":
    main()
