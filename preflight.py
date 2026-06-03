"""
preflight.py — Claude Code 家模式每轮消息的 ghost-trigger 管线
用法:
  python preflight.py "用户消息"
  python preflight.py --json "用户消息"    纯 JSON 输出（给 Claude Code 解析）
  python preflight.py --skip-va "用户消息"  跳过 VA 估测（省钱，固定 mid 档）
  echo "用户消息" | python preflight.py     管道输入

做的事:
  VA 估测 → 记忆检索 → 输出卡片上下文 + 管线反馈
"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "memory"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "emotion"))

from memory.retriever import retrieve, get_va_tier


def preflight(user_input: str, json_mode: bool = False, skip_va: bool = False) -> dict:
    # 1. VA — console.py Claude Code 开关优先
    va = None
    va_tier = "mid"
    va_info = {}
    mva_path = os.path.join(PROJECT_ROOT, "manual_va.json")
    mva_cc_override = False

    if os.path.exists(mva_path):
        try:
            with open(mva_path, "r", encoding="utf-8") as f:
                mva = json.load(f)
            mva_cc_override = mva.get("claude_va_override", False)
        except Exception:
            pass

    if mva_cc_override:
        # console.py 开了 Claude Code 手动VA → 100% 手动，不调 DS
        mva_enabled = mva.get("enabled", False)
        if mva_enabled:
            va = {"description": "console手动设定(CC)", "valence": mva.get("valence", 0.0),
                  "arousal": mva.get("arousal", 0.5)}
            va_tier = get_va_tier(va["arousal"])
            va_info = {
                "valence": round(va["valence"], 3),
                "arousal": round(va["arousal"], 3),
                "tier": va_tier,
                "description": va["description"],
                "source": "console_manual",
            }
        else:
            va = {"description": "", "valence": 0.0, "arousal": 0.5}
            va_info = {"valence": 0.0, "arousal": 0.5, "tier": "mid", "description": "",
                        "source": "console_override(no_manual_data)"}
    elif not skip_va:
        from emotion.va_estimator import estimate as va_estimate
        try:
            va = va_estimate(user_input)
            va_tier = get_va_tier(va["arousal"])
            va_info = {
                "valence": round(va["valence"], 3),
                "arousal": round(va["arousal"], 3),
                "tier": va_tier,
                "description": va.get("description", ""),
                "source": "deepseek",
            }
        except Exception as e:
            va_info = {"valence": 0.0, "arousal": 0.5, "tier": "mid", "description": "",
                        "error": str(e)}
            va = {"description": "", "valence": 0.0, "arousal": 0.5}
    else:
        va = {"description": "", "valence": 0.0, "arousal": 0.5}
        va_info = {"valence": 0.0, "arousal": 0.5, "tier": "mid", "description": "",
                    "source": "skip-va"}

    # 2. 记忆检索
    top_cards = []
    va_description = va.get("description", "") if va else ""
    try:
        top_cards = retrieve(
            user_input, top_k=3,
            va_tier=va_tier,
            va_description=va_description,
            va_valence=va.get("valence") if va else None,
            va_arousal=va.get("arousal") if va else None,
        )
        # touch cards
        try:
            from memory.memory_manager import touch_cards
            touch_cards([c["id"] for c in top_cards])
        except Exception:
            pass
    except Exception as e:
        top_cards = [{"error": str(e), "title": "检索失败"}]

    result = {
        "va": va_info,
        "cards": [
            {
                "id": c.get("id", ""),
                "title": c.get("title", ""),
                "category": c.get("category", ""),
                "content": c.get("content", "")[:200],
                "score": round(c.get("score", 0), 1),
                "importance": c.get("importance", 0),
                "hit_count": c.get("hit_count", 0),
            }
            for c in top_cards
        ],
    }

    if json_mode:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"┌{' VA ' + '─' * 51}┐")
        print(f"│ 情绪: v={va_info['valence']:.2f} a={va_info['arousal']:.2f}  tier={va_info['tier']:<5}                │")
        if va_info.get("description"):
            desc_short = va_info["description"][:42]
            print(f"│ 描述: {desc_short:<46} │")
        print(f"├{' 记忆检索 ' + '─' * 47}┤")
        if top_cards:
            for card in top_cards:
                title = card.get("title", "")[:35]
                cat = card.get("category", "?")[:12]
                score = card.get("score", 0)
                imp = card.get("importance", 0)
                print(f"│ [{cat:<12}] {title:<35} s={score:4.1f} imp={imp} │")
        else:
            print(f"│ (无相关记忆)                                               │")
        print(f"└{'─' * 55}┘")

    return result


if __name__ == "__main__":
    args = sys.argv[1:]
    json_mode = "--json" in args
    skip_va = "--skip-va" in args
    input_args = [a for a in args if a not in ("--json", "--skip-va")]
    user_input = " ".join(input_args).strip()

    if not user_input:
        # try stdin
        if not sys.stdin.isatty():
            user_input = sys.stdin.read().strip()
        if not user_input:
            print("用法: python preflight.py [--json] \"用户消息\"")
            sys.exit(1)

    preflight(user_input, json_mode=json_mode, skip_va=skip_va)
