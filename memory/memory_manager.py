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
                category IN ('milestone', 'commitments', 'deep_talks', 'preferences', 'todo')
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
            SELECT id, title, category, importance, target_date, chord, valence, arousal, resolved, COALESCE(synced_from,'') as synced_from
            FROM cards
            WHERE review_status='final' AND resolved=0
              AND (target_date IS NOT NULL AND target_date != ''
                   OR category IN ('commitments', 'daily_life', 'todo'))
            ORDER BY
                CASE WHEN target_date IS NOT NULL AND target_date != '' THEN target_date ELSE '9999-99-99' END ASC,
                importance DESC
        """)
        rows = c.fetchall()
        todos = []
        for row in rows:
            cid, title, cat, imp, td, chord, val, aro, res, synced_from = row
            # 艾森豪威尔分类
            now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if imp >= 8:
                quad = "重要不紧急"
            elif td and td < now_str:
                quad = "重要且紧急"  # 过期未完成
            elif cat == 'todo':
                if td:
                    try:
                        days_left = (datetime.strptime(td, '%Y-%m-%d').replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
                        quad = "重要且紧急" if days_left <= 7 else "重要不紧急"
                    except ValueError:
                        quad = "重要不紧急"
                else:
                    quad = "不重要但紧急"  # todo 无 deadline → 需要定时间
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
                "synced_from": synced_from or "",
            })
        return todos
    except Exception as e:
        print(f"[memory_manager] 待办查询失败: {e}")
        return []
    finally:
        conn.close()


def get_pending_todos() -> list:
    """读取 pending_cards.json 中有 target_date 的待办卡片（尚未审核通过）。
    返回格式与 get_todo_list() 一致，额外包含 status='pending'。"""
    import json as _json, os as _os3
    from datetime import datetime as _dt_pend
    pending_path = os.path.join(os.path.dirname(__file__), "pending_cards.json")
    if not os.path.exists(pending_path):
        return []
    try:
        with _os3.open(pending_path, "r", encoding="utf-8") as f:
            pending = _json.load(f)
    except Exception:
        return []

    todos = []
    for pc in pending:
        cat = pc.get("category", "")
        if cat not in ('todo', 'commitments', 'daily_life'):
            continue
        td = pc.get("target_date", "")
        imp = pc.get("importance", 5)
        # 艾森豪威尔分类
        now_str = _dt_pend.now().strftime('%Y-%m-%d')
        if imp >= 8:
            quad = "重要不紧急"
        elif td and td < now_str:
            quad = "重要且紧急"
        elif cat == 'todo':
            if td:
                try:
                    import re as _re_q
                    m = _re_q.match(r'(\d{4}-\d{2}-\d{2})', td)
                    if m:
                        days_left = (_dt_pend.strptime(m.group(1), '%Y-%m-%d') - _dt_pend.now()).days
                        quad = "重要且紧急" if days_left <= 7 else "重要不紧急"
                    else:
                        quad = "重要不紧急"
                except Exception:
                    quad = "不重要但紧急"
            else:
                quad = "不重要但紧急"
        elif cat == 'daily_life':
            quad = "不重要但紧急" if td else "不重要不紧急"
        elif cat == 'commitments':
            quad = "重要不紧急" if imp >= 7 else "不重要但紧急"
        else:
            quad = "不重要不紧急"

        todos.append({
            "id": pc.get("id", ""),
            "title": pc.get("title", ""),
            "category": cat,
            "importance": imp,
            "target_date": td or "",
            "chord": pc.get("chord", "") or "",
            "valence": pc.get("valence", 0.0),
            "arousal": pc.get("arousal", 0.5),
            "quadrant": quad,
            "resolved": False,
            "synced_from": "",
            "status": "pending",
        })
    return todos


def run_audit():
    print("[memory_manager] 开始执行完整审计...")
    update_active_status()
    update_anchor_set()
    suggest_importance_calibration()
    suggest_merges()
    resolve_expired_cards()
    abyss_challenge()
    print("[memory_manager] 审计完成。")


def abyss_challenge():
    """
    深渊挑战：对最优卡片做局部深度搜索。
    找出 usage_count 最高的 5 张卡，通过 FAISS 找最近邻居。
    对邻居中未被充分使用的卡（usage≤1），小幅提升 importance（上限 6）。
    """
    try:
        from .encoder import load_index, search_index, embed
        index = load_index()
        if index.ntotal == 0:
            return

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""SELECT id, title, usage_count, importance, content FROM cards
                     WHERE review_status='final' AND resolved=0 AND enabled_in_context=1
                     ORDER BY usage_count DESC LIMIT 5""")
        top_cards = c.fetchall()

        boosted = 0
        for cid, title, usage, imp, content in top_cards:
            if usage < 3:
                continue
            try:
                query_vec = embed(content or title)
                neighbors = search_index(index, query_vec, k=6)  # 含自身
                for nid, dist in neighbors:
                    if nid == cid:
                        continue
                    c.execute("SELECT importance, usage_count, title FROM cards WHERE id=? AND resolved=0",
                              (nid,))
                    row = c.fetchone()
                    if row and row[1] is not None and row[1] <= 1 and row[0] < 6:
                        new_imp = min(row[0] + 1, 6)
                        c.execute("UPDATE cards SET importance=? WHERE id=?", (new_imp, nid))
                        print(f"[深渊挑战] {title} → 邻居「{row[2]}」importance {row[0]}→{new_imp}")
                        boosted += 1
                        if boosted >= 5:  # 每次审计最多提 5 张
                            break
                if boosted >= 5:
                    break
            except Exception as e:
                print(f"[深渊挑战] 单卡跳过 {cid}: {e}")
        conn.commit()
        conn.close()
        if boosted > 0:
            print(f"[深渊挑战] 共提升 {boosted} 张邻居卡片")
    except Exception as e:
        print(f"[深渊挑战] 跳过: {e}")

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


def card_age_days(card_id: str) -> int | None:
    """
    从卡片 ID 前缀提取日期，计算距今多少天。
    ID 格式: YYYYMMDD_标题 (如 20260519_和DS老师拍开箱视频)
    返回: 天数，提取失败返回 None。
    """
    import re as _re_age
    m = _re_age.match(r'^(\d{4})(\d{2})(\d{2})_', card_id)
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        card_date = datetime(y, mo, d, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - card_date).days
    except ValueError:
        return None


def time_match_score(old_card_id: str, context_anchor: dict = None) -> tuple[int, str]:
    """
    计算两张卡的时间锚匹配度。优先于关键词匹配。
    返回 (score, reason)，score 越高越相关。

    context_anchor: {"date": "YYYY-MM-DD"|None, "fuzzy": "YYYY-MM"|None, ...}
    """
    if not context_anchor:
        return 0, "无上下文时间锚"
    ctx_date = context_anchor.get("date")
    ctx_fuzzy = context_anchor.get("fuzzy")

    # 查 DB 的 target_date（唯一的时间锚来源，不降级到卡片创建日期）
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("SELECT target_date FROM cards WHERE id = ?", (old_card_id,))
        row = c.fetchone()
        old_target_date = row[0] if row else None
    finally:
        conn.close()

    # 从 target_date 推导 fuzzy 月份
    old_fuzzy = old_target_date[:7] if old_target_date and len(old_target_date) >= 7 else None

    # 情况1：双方都有精确日期 → 算天数差
    if ctx_date and old_target_date:
        try:
            from datetime import datetime as _dt2
            ctx_dt = _dt2.strptime(ctx_date, "%Y-%m-%d")
            old_dt = _dt2.strptime(old_target_date, "%Y-%m-%d")
            days_apart = abs((ctx_dt - old_dt).days)
            if days_apart <= 3:
                return 10, f"同时间窗口(相差{days_apart}天)"
            elif days_apart <= 14:
                return 8, f"近时间窗口(相差{days_apart}天)"
            elif days_apart <= 30:
                return 5, f"同月(相差{days_apart}天)"
            else:
                return 2, f"不同月份(相差{days_apart}天)"
        except ValueError:
            pass

    # 情况2：双方都有模糊月份 → 同月匹配
    if ctx_fuzzy and old_fuzzy:
        if ctx_fuzzy == old_fuzzy:
            return 7, f"同月({ctx_fuzzy})"
        elif ctx_fuzzy[:4] == old_fuzzy[:4] and abs(int(ctx_fuzzy[5:]) - int(old_fuzzy[5:])) <= 1:
            return 4, f"相邻月({old_fuzzy} vs {ctx_fuzzy})"
        else:
            return 1, f"不同月({old_fuzzy} vs {ctx_fuzzy})"

    # 情况3：一方有精确日期，另一方有模糊月份
    if ctx_date and old_fuzzy:
        if ctx_date[:7] == old_fuzzy:
            return 6, f"同月({old_fuzzy})"
        return 2, f"不同月({old_fuzzy})"

    # 情况4：一方有时间锚，另一方完全没有 → 低相关
    if old_target_date or old_fuzzy:
        return 3, "旧卡有时间锚但匹配度不高"
    else:
        # 旧卡无时间锚（如"坚持干下去"）→ 新卡有时间锚 → 大概率不相关
        if ctx_date or ctx_fuzzy:
            return -1, "旧卡无时间锚，新卡有时间锚 → 不同事件类型"

    return 0, "无法比较"


def should_auto_resolve(card_id: str, days_threshold: int = 7,
                          context_anchor: dict = None) -> tuple[bool, str]:
    """
    时间戳拒止 + 时间锚匹配：检查卡片是否适合被自动划掉。
    优先使用卡片 ID 前缀的时间戳；降级使用 DB 中的 created_at。
    返回 (允许, 原因)。

    规则（按优先级）：
    1. 时间锚匹配度 ≥ 5 → 允许（同时间窗口，高概率相关）
    2. 时间锚匹配度 < 0 → 拒止（旧卡无时间锚，新卡有 → 不同事件类型）
    3. 卡片创建 ≤ days_threshold 天 → 允许
    4. 卡片创建 > days_threshold 天 → 仅 target_date 已过期才允许
    5. 否则 → 拒止
    """
    # ── 时间锚匹配优先 ──
    if context_anchor and (context_anchor.get("date") or context_anchor.get("fuzzy")):
        score, reason = time_match_score(card_id, context_anchor)
        if score >= 5:
            return True, f"时间匹配({reason})"
        if score < 0:
            return False, f"时间锚拒止({reason})"
    # ── 优先用 ID 前缀时间戳 ──
    age_days = card_age_days(card_id)
    if age_days is not None:
        if age_days <= days_threshold:
            return True, f"近期卡片(ID前缀, {age_days}天前)"
        # ID 显示老卡，再查 DB 的 target_date 做二次验证
        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute("SELECT target_date FROM cards WHERE id = ?", (card_id,))
            row = c.fetchone()
            if row and row[0]:
                try:
                    td = datetime.strptime(row[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    if td < now:
                        return True, f"老卡但已过期(ID {age_days}天前, target={row[0]})"
                except ValueError:
                    pass
        finally:
            conn.close()
        return False, f"时间拒止: ID前缀显示{age_days}天前, 无过期target"

    # ── 降级：用 DB created_at ──
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute(
            "SELECT created_at, target_date, title FROM cards WHERE id = ?",
            (card_id,)
        )
        row = c.fetchone()
        if not row:
            return False, "卡片不存在"
        created_str, target_date, title = row

        now = datetime.now(timezone.utc)
        if created_str:
            try:
                created_at = datetime.fromisoformat(created_str)
            except ValueError:
                created_at = datetime.strptime(created_str, "%Y-%m-%d %H:%M:%S")
            age_days = (now - created_at.replace(tzinfo=timezone.utc)).days
        else:
            age_days = 999

        if age_days <= days_threshold:
            return True, f"近期卡片({age_days}天前)"

        # 老卡：检查 target_date 是否也已过期
        if target_date:
            try:
                td = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if td < now:
                    return True, f"老卡但已过期({age_days}天前, target={target_date})"
            except ValueError:
                pass

        return False, f"时间拒止: {age_days}天前创建, 无过期target"
    except Exception as e:
        return False, f"检查失败: {e}"
    finally:
        conn.close()


def update_card(card_id: str, updates: dict) -> bool:
    """
    原地更新卡片字段，不划掉不重建。
    updates: {"target_date": "2026-05-20", "status": "in_progress", "content": "新内容", ...}
    仅更新提供的字段。
    """
    allowed_fields = {'target_date', 'status', 'content', 'title', 'importance', 'keywords', 'category'}
    safe_updates = {k: v for k, v in updates.items() if k in allowed_fields}
    if not safe_updates:
        return False

    conn = sqlite3.connect(DB_PATH)
    try:
        set_clauses = ", ".join(f"{k} = ?" for k in safe_updates)
        values = list(safe_updates.values()) + [card_id]
        c = conn.cursor()
        c.execute(f"UPDATE cards SET {set_clauses} WHERE id = ?", values)
        if c.rowcount == 0:
            conn.rollback()
            return False
        conn.commit()
        print(f"[memory_manager] 卡片 {card_id} 已更新: {safe_updates}")
        return True
    except Exception as e:
        print(f"[memory_manager] 更新失败 card_id={card_id}: {e}")
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

def sync_diary_todos_to_cards(days_back: int = 30) -> int:
    """
    扫描近 N 天日记 events.json 的艾森豪威尔四象限，
    将 card_id='无' 的条目同步为 cards.db 中的卡片。
    返回创建的卡片数量。已有 card_id 的条目自动跳过。
    """
    from datetime import datetime as _dt, timedelta as _td
    import json as _json

    diary_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "diary")
    if not os.path.exists(diary_dir):
        return 0

    conn = sqlite3.connect(DB_PATH)
    # 确保 synced_from 列存在
    try:
        conn.execute("ALTER TABLE cards ADD COLUMN synced_from TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    created = 0

    try:
        for days_back_i in range(days_back, 0, -1):
            d = (_dt.now() - _td(days=days_back_i)).strftime("%Y-%m-%d")
            ep = os.path.join(diary_dir, f"{d}_events.json")
            if not os.path.exists(ep):
                continue

            with open(ep, "r", encoding="utf-8") as f:
                ev = _json.load(f)

            eis = ev.get("eisenhower", {})
            events_modified = False

            for quad_key, items in eis.items():
                for it in items:
                    card_id = it.get("card_id", "无")
                    if card_id and card_id != "无":
                        continue  # 已同步过

                    item_text = it.get("item", "")
                    deadline = it.get("deadline", "")
                    note = it.get("note", "")

                    if not item_text or len(item_text) < 2:
                        continue

                    # 确定 card_id：日期_标题
                    cid = f"{d.replace('-', '')}_{item_text[:20]}"
                    # 检查是否已存在
                    c = conn.cursor()
                    c.execute("SELECT id FROM cards WHERE id=?", (cid,))
                    if c.fetchone():
                        it["card_id"] = cid
                        events_modified = True
                        continue

                    # 确定分类和重要度
                    cat = "todo"
                    imp = 7
                    if quad_key == "important_urgent":
                        cat, imp = "todo", 8
                    elif quad_key == "important_not_urgent":
                        cat, imp = "commitments", 7
                    elif quad_key == "not_important_urgent":
                        cat, imp = "todo", 5
                    elif quad_key == "not_important_not_urgent":
                        cat, imp = "daily_life", 3

                    content = f"{item_text}。" + (f"备注: {note}" if note else "")
                    target_date = deadline if deadline else None

                    try:
                        conn.execute("""
                            INSERT INTO cards (id, title, content, keywords, importance,
                                category, review_status, enabled_in_context, target_date, synced_from)
                            VALUES (?, ?, ?, ?, ?, ?, 'final', 1, ?, 'diary')
                        """, (cid, item_text[:40], content[:200], item_text, imp, cat, target_date))
                        conn.commit()
                        it["card_id"] = cid
                        events_modified = True
                        created += 1
                        print(f"[日记同步] {d} 「{item_text}」→ card:{cid} [{cat}] imp={imp}")
                    except sqlite3.IntegrityError:
                        # 重复 ID — 卡片已被其他方式创建
                        it["card_id"] = cid
                        events_modified = True

            if events_modified:
                from delegate_tools import atomic_write_json
                atomic_write_json(ep, ev)
                print(f"[日记同步] {d}_events.json 已更新 card_id 链接")
    finally:
        conn.close()

    if created > 0:
        print(f"[日记同步] 共新建 {created} 张待办卡片")
    return created


if __name__ == "__main__":
    run_audit()