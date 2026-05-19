"""
memory_manager.py - 记忆卡片全生命周期管理（修复版）

FIX #1: delete_card 不再 int(card_id)，改用 encoder.remove_from_index
FIX #2: renew_card ID清洗逻辑修正 — 只处理 "card:" 前缀，不误伤含冒号的合法ID（如时间戳）
"""
import sqlite3
import os
import json
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")

def renew_card(card_id: str) -> bool:
    """
    续命：更新卡片的 last_referenced_at 并令 usage_count += 1。
    FIX: ID 清洗只处理 "card:" 前缀，不拆分所有冒号（避免误伤时间戳等合法ID）。
    """
    clean_id = card_id.strip()
    # ── FIX: 只处理特定的 "card:" 前缀 ──
    if clean_id.lower().startswith("card:"):
        clean_id = clean_id[5:].strip()

    if not clean_id:
        print(f"[memory_manager] 续命失败：无效ID格式 ({card_id})")
        return False

    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        now_utc = datetime.now(timezone.utc)
        now = now_utc.isoformat()
        c.execute(
            "UPDATE cards SET last_referenced_at = ?, usage_count = usage_count + 1 WHERE id = ?",
            (now, clean_id)
        )
        if c.rowcount == 0:
            return False
        conn.commit()
        # ── 锚定集自动追加 ──
        try:
            c.execute("SELECT usage_count, importance, title, category FROM cards WHERE id = ?", (clean_id,))
            row = c.fetchone()
            if row and row[0] >= 5 and row[1] >= 7:
                anchor_path = os.path.join(os.path.dirname(__file__), "anchor_set.json")
                if os.path.exists(anchor_path):
                    with open(anchor_path, "r", encoding="utf-8") as f:
                        anchor_data = json.load(f)
                else:
                    anchor_data = {"updated_at": "", "count": 0, "cards": []}
                existing_ids = {c["id"] for c in anchor_data.get("cards", [])}
                if clean_id not in existing_ids:
                    anchor_data["cards"].append({
                        "id": clean_id,
                        "title": row[2],
                        "category": row[3],
                        "importance": row[1],
                        "usage_count": row[0]
                    })
                    anchor_data["count"] = len(anchor_data["cards"])
                    anchor_data["updated_at"] = datetime.now(timezone.utc).isoformat()
                    from delegate_tools import atomic_write_json
                    atomic_write_json(anchor_path, anchor_data)
        except Exception:
            pass  # 锚定追加失败不阻塞续命
        return True
    except Exception as e:
        print(f"[memory_manager] 续命失败 card_id={card_id}: {e}")
        return False
    finally:
        conn.close()

def touch_cards(card_ids: list):
    """检索命中自动计数：usage_count+1 并更新 last_referenced_at。轻量批量更新。"""
    if not card_ids:
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        now_utc = datetime.now(timezone.utc)
        c = conn.cursor()
        for cid in card_ids:
            c.execute(
                "UPDATE cards SET usage_count = usage_count + 1, last_referenced_at = ? WHERE id = ? AND review_status='final'",
                (now_utc.isoformat(), cid)
            )
        conn.commit()
    except Exception as e:
        print(f"[memory_manager] touch_cards 失败: {e}")
    finally:
        conn.close()

def update_active_status():
    """定期调用，更新卡片活跃状态"""
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        now_utc = datetime.now(timezone.utc)
        c.execute("UPDATE cards SET enabled_in_context = 0 WHERE review_status = 'final'")
        c.execute("""
            UPDATE cards SET enabled_in_context = 1
            WHERE review_status = 'final'
            AND (
                category IN ('milestone', 'commitments', 'deep_talks', 'preferences')
                OR importance >= 8
                OR (julianday(?) - julianday(COALESCE(last_referenced_at, created_at) || '+00:00')) <= 30
            )
        """, (now_utc.isoformat(),))
        conn.commit()
        print(f"[memory_manager] 活跃状态已更新，当前时间: {now_utc.isoformat()}")
    except Exception as e:
        print(f"[memory_manager] 活跃状态更新失败: {e}")
    finally:
        conn.close()

def check_duplicates(new_content: str, threshold: float = 0.85) -> list:
    try:
        from encoder import embed, load_index, search_index
    except ImportError:
        print("[memory_manager] 无法导入 encoder，跳过去重检测")
        return []

    try:
        new_vec = embed(new_content)
        index = load_index()
        if index.ntotal == 0:
            return []

        candidates = search_index(index, new_vec, k=5)
        duplicates = []
        for card_id, distance in candidates:
            similarity = 1.0 / (1.0 + distance)
            if similarity >= threshold:
                duplicates.append(card_id)
        if duplicates:
            print(f"[memory_manager] 去重检测：发现 {len(duplicates)} 张相似卡片 {duplicates}")
        return duplicates
    except Exception as e:
        print(f"[memory_manager] 去重检测异常: {e}")
        return []

def suggest_importance_calibration():
    """自动校准 importance：高频低权→提升，虚高零引→审视降级。audit 定期调用。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("SELECT id, title, importance, usage_count FROM cards WHERE review_status='final'")
        rows = c.fetchall()
        bumped = []
        demoted = []
        for row in rows:
            card_id, title, importance, usage_count = row
            if usage_count >= 10 and importance < 7:
                new_imp = 7
                c.execute("UPDATE cards SET importance = ? WHERE id = ?", (new_imp, card_id))
                bumped.append((card_id, title, importance, new_imp))
            elif importance >= 8 and usage_count == 0:
                new_imp = 6
                c.execute("UPDATE cards SET importance = ? WHERE id = ?", (new_imp, card_id))
                demoted.append((card_id, title, importance, new_imp))
        if bumped or demoted:
            conn.commit()
        if bumped:
            print(f"[importance校准] 提升 {len(bumped)} 张（高频低权→7）：")
            for cid, title, old, new in bumped:
                print(f"  {cid}: {old}→{new}")
        if demoted:
            print(f"[importance校准] 审视降级 {len(demoted)} 张（虚高零引→6）：")
            for cid, title, old, new in demoted:
                print(f"  {cid}: {old}→{new}")
        if not bumped and not demoted:
            print("[importance校准] 无需调整")
    except Exception as e:
        print(f"[memory_manager] importance 校准异常: {e}")
    finally:
        conn.close()

def suggest_merges():
    try:
        from encoder import load_index, search_index, embed
    except ImportError:
        print("[memory_manager] 无法导入 encoder，跳过合并建议")
        return []

    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("SELECT id, title, content FROM cards WHERE review_status='final' AND enabled_in_context=1")
        rows = c.fetchall()
        if len(rows) < 2:
            return []

        index = load_index()
        merge_suggestions = []
        checked = set()
        for row in rows:
            card_id, title, content = row
            if card_id in checked:
                continue
            try:
                vec = embed(content)
                similar = search_index(index, vec, k=5)
                for other_id, distance in similar:
                    if other_id == card_id or other_id in checked:
                        continue
                    similarity = 1.0 / (1.0 + distance)
                    if similarity > 0.95:
                        merge_suggestions.append({
                            "card_a": card_id, "card_b": other_id,
                            "similarity": round(similarity, 3),
                            "suggestion": "建议合并"
                        })
                        checked.add(card_id)
                        checked.add(other_id)
                        break
            except Exception:
                continue

        if merge_suggestions:
            print(f"[memory_manager] 发现 {len(merge_suggestions)} 条合并建议：")
            for m in merge_suggestions:
                print(f"  {m['card_a']} <-> {m['card_b']} (相似度: {m['similarity']})")
        return merge_suggestions
    except Exception as e:
        print(f"[memory_manager] 合并建议异常: {e}")
        return []
    finally:
        conn.close()

def resolve_expired_cards():
    """扫描 target_date 已过的卡片，自动标记为已解决。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime('%Y-%m-%d')
        c = conn.cursor()
        c.execute(
            "SELECT id, title, target_date FROM cards WHERE review_status='final' AND resolved=0 AND target_date IS NOT NULL AND target_date != '' AND target_date < ?",
            (today_str,)
        )
        expired = c.fetchall()
        for cid, title, td in expired:
            c.execute("UPDATE cards SET resolved=1 WHERE id=?", (cid,))
            print(f"[过期解决] 目标日期 {td} 已过 → {cid}「{title}」已自动标记为已解决")
        if expired:
            conn.commit()
    except Exception as e:
        print(f"[过期解决] 扫描异常: {e}")
    finally:
        conn.close()

def get_todo_list() -> list:
    """待办清单：所有带 target_date 或 category=commitments 的未解决卡片，按时间排序。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, title, category, importance, target_date, chord, valence, arousal, resolved
            FROM cards
            WHERE review_status='final' AND resolved=0
              AND (target_date IS NOT NULL AND target_date != ''
                   OR category IN ('commitments', 'daily_life'))
            ORDER BY
                CASE WHEN target_date IS NOT NULL AND target_date != '' THEN target_date ELSE '9999-99-99' END ASC,
                importance DESC
        """)
        rows = c.fetchall()
        todos = []
        for row in rows:
            cid, title, cat, imp, td, chord, val, aro, res = row
            # 艾森豪威尔分类
            if imp >= 8:
                quad = "重要不紧急"
            elif td and td < datetime.now(timezone.utc).strftime('%Y-%m-%d'):
                quad = "重要且紧急"
            elif cat == 'daily_life':
                quad = "不重要但紧急" if td else "不重要不紧急"
            elif cat == 'commitments':
                quad = "重要不紧急" if imp >= 7 else "不重要但紧急"
            else:
                quad = "不重要不紧急"
            todos.append({
                "id": cid, "title": title, "category": cat,
                "importance": imp, "target_date": td or "",
                "chord": chord or "", "valence": val, "arousal": aro,
                "quadrant": quad, "resolved": bool(res),
            })
        return todos
    except Exception as e:
        print(f"[memory_manager] 待办查询失败: {e}")
        return []
    finally:
        conn.close()


def run_audit():
    print("[memory_manager] 开始执行完整审计...")
    update_active_status()
    update_anchor_set()
    suggest_importance_calibration()
    suggest_merges()
    resolve_expired_cards()
    print("[memory_manager] 审计完成。")

def get_card_status() -> list:
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, title, category, importance,
                   created_at, last_referenced_at, enabled_in_context, resolved
            FROM cards WHERE review_status='final'
            ORDER BY created_at DESC
        """)
        rows = c.fetchall()
        now = datetime.now(timezone.utc)
        result = []
        for row in rows:
            card_id, title, category, importance, created_at, last_ref, enabled, resolved = row
            is_permanent = (category in ('milestone', 'commitments', 'deep_talks') or importance >= 8)

            if is_permanent:
                days_remaining = -1
            else:
                created = datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc) if created_at else None
                last = datetime.fromisoformat(last_ref).replace(tzinfo=timezone.utc) if last_ref else None
                ref_point = created or last or now
                if created and last:
                    ref_point = max(created, last)
                elapsed = (now - ref_point).days
                days_remaining = max(0, 30 - elapsed)

            result.append({
                "id": card_id, "title": title,
                "category": category, "importance": importance,
                "is_permanent": is_permanent,
                "days_remaining": days_remaining,
                "enabled": bool(enabled),
                "resolved": bool(resolved)
            })
        return result
    except Exception as e:
        print(f"[memory_manager] 状态查询失败: {e}")
        return []
    finally:
        conn.close()

def delete_card(card_id: str) -> bool:
    """从数据库和 FAISS 索引中彻底删除一张卡片。FIX: 使用 encoder.remove_from_index"""
    # ── 毒点27修复：先删 FAISS 索引，成功后再删 DB，避免幽灵结果 ──
    try:
        from encoder import remove_from_index
        remove_from_index(card_id)
        print(f"[memory_manager] 已从索引中移除 {card_id}")
    except Exception as e:
        print(f"[memory_manager] 索引移除失败，放弃删除: {e}")
        return False

    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        if c.rowcount == 0:
            print(f"[memory_manager] 删除失败：数据库中不存在 {card_id}")
            # 索引已移除但 DB 无记录 → 尝试回加索引
            try:
                from encoder import load_index, save_index
                idx = load_index()
                save_index(idx)
            except:
                pass
            return False
        conn.commit()
        print(f"[memory_manager] 卡片 {card_id} 已彻底删除")
        return True
    except Exception as e:
        print(f"[memory_manager] 删除失败: {e}")
        return False
    finally:
        conn.close()


def resolve_card(card_id: str) -> bool:
    """标记卡片为已解决（resolved=1, status='completed'）。用于对话中自动检测「我做完了」等信号。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("UPDATE cards SET resolved = 1, status = 'completed' WHERE id = ? AND review_status='final'", (card_id,))
        if c.rowcount == 0:
            return False
        conn.commit()
        print(f"[memory_manager] 卡片 {card_id} 已标记为已解决")
        return True
    except Exception as e:
        print(f"[memory_manager] 标记已解决失败 card_id={card_id}: {e}")
        return False
    finally:
        conn.close()


def set_card_status(card_id: str, status: str) -> bool:
    """更新卡片流转状态（进行中/阻塞），不标记为已完成。"""
    if status not in ('in_progress', 'blocked', 'active'):
        print(f"[memory_manager] 无效状态: {status}，跳过 card_id={card_id}")
        return False
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE cards SET status = ? WHERE id = ? AND review_status='final' AND resolved = 0",
            (status, card_id)
        )
        if c.rowcount == 0:
            return False
        conn.commit()
        label = {'in_progress': '进行中', 'blocked': '阻塞', 'active': '活跃'}[status]
        print(f"[memory_manager] 卡片 {card_id} 状态 → {label}")
        return True
    except Exception as e:
        print(f"[memory_manager] 状态更新失败 card_id={card_id}: {e}")
        return False
    finally:
        conn.close()


def update_anchor_set():
    """扫描 usage_count >= 5、importance >= 7、存活≥30天的 final 卡片，写入 anchor_set.json"""
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        now_utc = datetime.now(timezone.utc)
        c.execute("""
            SELECT id, title, category, importance, usage_count
            FROM cards
            WHERE review_status = 'final'
              AND usage_count >= 5
              AND importance >= 7
              AND (julianday(?) - julianday(created_at || '+00:00')) >= 10
            ORDER BY usage_count DESC
        """, (now_utc.isoformat(),))
        rows = c.fetchall()
        
        anchors = []
        for row in rows:
            anchors.append({
                "id": row[0],
                "title": row[1],
                "category": row[2],
                "importance": row[3],
                "usage_count": row[4]
            })

        anchor_path = os.path.join(os.path.dirname(__file__), "anchor_set.json")
        from delegate_tools import atomic_write_json
        atomic_write_json(anchor_path, {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "count": len(anchors),
                "cards": anchors
        })
        
        print(f"[memory_manager] 锚定集合已更新：{len(anchors)} 张卡片")
        return anchors
    except Exception as e:
        print(f"[memory_manager] 锚定集合更新失败: {e}")
        return []
    finally:
        conn.close()

if __name__ == "__main__":
    run_audit()