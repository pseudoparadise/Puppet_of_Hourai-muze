"""
card_guard.py — 共享的写卡前置检查（拦截器 + 垃圾过滤）
供 trigger.py 的 propose_card 路径和 auto-trigger 路径共同调用。
"""
import os, re, sqlite3, json

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_STOP_CHARS = set('的了是在我有他个这着就和也要会可你他们来到说去为上对得大子能过下一地出道自以时年看没那天家开小成把前还但只想中里用生种起知好些间因所如然后其最她它已当两从方实长更应什')

# 垃圾标题黑名单
_GARBAGE_TITLES = {
    "暂无计划", "没什么", "不知道", "没想好", "随便", "无", "暂无",
    "不想动", "懒得", "没力气", "好累", "累了",
}


def _extract_features(s: str) -> set:
    s = s.lower()
    chars = set(re.findall(r'[一-鿿]', s)) - _STOP_CHARS
    for t in re.findall(r'[a-z][a-z0-9]+', s):
        chars.add(t)
    return chars


def is_garbage_title(title: str) -> bool:
    """检查标题是否为无意义占位符。"""
    title = title.strip()
    if len(title) <= 1:
        return True
    if title in _GARBAGE_TITLES:
        return True
    # 纯情绪/状态描述但无语义主体
    if title in {"好累", "不想动", "不想去", "不想做"}:
        return True
    return False


def check_before_write(title: str, content: str, user_input: str) -> tuple[bool, str]:
    """
    写卡前检查。返回 (should_block, reason)。
    1. 垃圾标题 → 拦截
    2. 与现有 final 卡特征重叠 ≥2 → 拦截并 auto-resolve 旧卡
    3. 与 pending 卡特征重叠 ≥2 → 拦截并移除旧 pending
    """
    if is_garbage_title(title):
        return True, f"垃圾标题拦截: 「{title}」"

    proposed_text = (title + " " + content).lower()
    proposed_features = _extract_features(proposed_text)

    # 扫 cards.db
    db_path = os.path.join(PROJECT_ROOT, "memory", "cards.db")
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute(
                "SELECT id, title, content, importance FROM cards WHERE review_status='final' AND resolved=0 ORDER BY created_at DESC LIMIT 20"
            )
            for oid, otitle, ocontent, oimp in c.fetchall():
                old_text = (otitle + " " + (ocontent or "")).lower()
                overlap = len(proposed_features & _extract_features(old_text))
                if overlap >= 2 and (oimp or 5) < 8:
                    # 时间锚拒止
                    from memory.memory_manager import should_auto_resolve
                    allowed, reason = should_auto_resolve(oid)
                    if allowed:
                        from memory.memory_manager import resolve_card
                        if resolve_card(oid):
                            print(f"[写卡拦截] auto-trigger 新卡「{title}」与旧卡「{otitle}」重叠({overlap})，auto-resolve 旧卡，丢弃新卡")
                            conn.close()
                            return True, f"与旧卡「{otitle}」特征重叠({overlap})"
            conn.close()
        except Exception as e:
            print(f"[写卡拦截-auto] DB扫描跳过: {e}")

    # 扫 pending_cards.json
    pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
    if os.path.exists(pending_path):
        try:
            with open(pending_path, "r", encoding="utf-8") as f:
                pending = json.load(f)
            removed = False
            new_pending = []
            for pc in pending:
                ptext = (pc.get("title", "") + " " + pc.get("content", "")).lower()
                if len(proposed_features & _extract_features(ptext)) >= 2 and pc.get("importance", 5) < 8:
                    print(f"[写卡拦截-auto] 与待审核卡「{pc.get('title','')}」重叠，移除旧 pending")
                    removed = True
                    continue
                new_pending.append(pc)
            if removed:
                from delegate_tools import atomic_write_json
                atomic_write_json(pending_path, new_pending)
        except Exception as e:
            print(f"[写卡拦截-auto] pending扫描跳过: {e}")

    return False, ""
