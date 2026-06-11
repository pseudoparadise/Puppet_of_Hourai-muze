"""
mycelium.py — 信息素层
SQLite WAL 统一状态层，替代散落 JSON 文件群。

核心机制：
- 每条痕写入时打时间戳 + 半衰期
- 查询时计算有效强度：effective = intensity × 0.5^(age/halflife_s) + refs × 0.15
- 引用升温：同 key 重复 write → refs+1 → 自催化
- 锚定：refs ≥ 3 → 永不衰减
- MMAS 上下界：τ ∈ [0.01, 0.95]
- 冷阈值 0.1：标记 cold，不删除但 clean() 清理
- 产量上限 5000：超限清理最冷的非锚定痕
- 不设后台衰减进程，仅查询时计算
"""

import sqlite3
import os
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "memory", "mycelium.db")

_COLD_THRESHOLD = 0.1
_MAX_TRACES = 5000
_MMAS_FLOOR = 0.01
_MMAS_CEIL = 0.95
_REF_BOOST = 0.15
_ANCHOR_REFS = 3


def _ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pheromones (
            source      TEXT NOT NULL,
            key         TEXT NOT NULL,
            intensity   REAL NOT NULL DEFAULT 1.0,
            halflife_s  REAL NOT NULL DEFAULT 3600,
            refs        INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL DEFAULT '',
            meta        TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (source, key)
        )
    """)
    conn.commit()
    conn.close()


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts_str: str):
    try:
        from datetime import datetime
        s = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def write(source: str, key: str, intensity: float = 1.0, halflife_s: float = 3600,
          meta: str = "{}", bump_refs: bool = True, refs: int = None):
    """写入/更新一条信息素。bump_refs=True 时同 source+key 重复写 → refs+1（引用升温）。
    refs 参数用于迁移/手动设置初始引用计数（仅对新记录有效，已存在记录忽略）。"""
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    now = _now_iso()

    cur = conn.execute(
        "SELECT refs, created_at FROM pheromones WHERE source=? AND key=?",
        (source, key)
    )
    row = cur.fetchone()

    if row:
        if refs is not None:
            new_refs = refs
        elif bump_refs:
            new_refs = row[0] + 1
        else:
            new_refs = row[0]
        created_at = row[1]
        conn.execute(
            "UPDATE pheromones SET intensity=?, halflife_s=?, refs=?, updated_at=?, meta=? "
            "WHERE source=? AND key=?",
            (intensity, halflife_s, new_refs, now, meta, source, key)
        )
    else:
        init_refs = refs if refs is not None else 0
        conn.execute(
            "INSERT INTO pheromones (source, key, intensity, halflife_s, refs, created_at, updated_at, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source, key, intensity, halflife_s, init_refs, now, now, meta)
        )

    conn.commit()
    conn.close()


def read(source: str, key: str) -> float:
    """读取一条信息素的有效强度。锚定(refs≥3) → 0.95，否则按公式计算。"""
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT intensity, halflife_s, refs, updated_at FROM pheromones WHERE source=? AND key=?",
        (source, key)
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return 0.0

    intensity, halflife_s, refs, updated_at = row

    if refs >= _ANCHOR_REFS:
        return _MMAS_CEIL

    dt = _parse_iso(updated_at)
    if dt is None:
        return max(_MMAS_FLOOR, min(_MMAS_CEIL, intensity))

    from datetime import datetime, timezone
    age_s = (datetime.now(timezone.utc) - dt).total_seconds()

    if halflife_s <= 0 or halflife_s == float('inf'):
        decay = 1.0
    else:
        decay = 0.5 ** (age_s / halflife_s)

    effective = intensity * decay + refs * _REF_BOOST
    return max(_MMAS_FLOOR, min(_MMAS_CEIL, effective))


def sniff(source: str, min_effective: float = None) -> list:
    """读取 source 来源的所有信息素，返回 [{key, intensity, effective, refs, meta}, ...]。
    按有效强度降序排列。min_effective 过滤冷痕（默认不过滤）。"""
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT key, intensity, halflife_s, refs, updated_at, meta FROM pheromones WHERE source=?",
        (source,)
    )
    rows = cur.fetchall()
    conn.close()

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    results = []

    for r in rows:
        if r["refs"] >= _ANCHOR_REFS:
            effective = _MMAS_CEIL
        else:
            dt = _parse_iso(r["updated_at"])
            if dt is None:
                effective = max(_MMAS_FLOOR, min(_MMAS_CEIL, r["intensity"]))
            else:
                age_s = (now - dt).total_seconds()
                hl = r["halflife_s"]
                if hl <= 0 or hl == float('inf'):
                    decay = 1.0
                else:
                    decay = 0.5 ** (age_s / hl)
                effective = r["intensity"] * decay + r["refs"] * _REF_BOOST
                effective = max(_MMAS_FLOOR, min(_MMAS_CEIL, effective))

        if min_effective is not None and effective < min_effective:
            continue

        results.append({
            "key": r["key"],
            "intensity": r["intensity"],
            "effective": round(effective, 4),
            "refs": r["refs"],
            "meta": r["meta"],
        })

    results.sort(key=lambda x: x["effective"], reverse=True)
    return results


def clean():
    """清理冷痕（有效强度 < 0.1 且 refs < 3）+ 超 5000 上限清最冷的非锚定痕。"""
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)

    # 1. 清冷痕
    conn.execute(
        "DELETE FROM pheromones WHERE refs < ?",
        (_ANCHOR_REFS,)
    )
    # 进一步清理冷痕：读出来算 effective
    cur = conn.execute(
        "SELECT source, key, intensity, halflife_s, refs, updated_at FROM pheromones WHERE refs < ?",
        (_ANCHOR_REFS,)
    )
    cold_keys = []
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for r in cur.fetchall():
        dt = _parse_iso(r[5])
        if dt is None:
            continue
        age_s = (now - dt).total_seconds()
        hl = r[3]
        if hl <= 0 or hl == float('inf'):
            decay = 1.0
        else:
            decay = 0.5 ** (age_s / hl)
        effective = r[2] * decay + r[4] * _REF_BOOST
        if effective < _COLD_THRESHOLD:
            cold_keys.append((r[0], r[1]))

    for source, key in cold_keys:
        conn.execute("DELETE FROM pheromones WHERE source=? AND key=?", (source, key))

    # 2. 上限裁剪：超 5000 条清最冷的非锚定痕
    count_row = conn.execute("SELECT COUNT(*) FROM pheromones").fetchone()
    if count_row and count_row[0] > _MAX_TRACES:
        excess = count_row[0] - _MAX_TRACES
        cur = conn.execute(
            "SELECT source, key FROM pheromones WHERE refs < ? ORDER BY updated_at ASC LIMIT ?",
            (_ANCHOR_REFS, excess)
        )
        for r in cur.fetchall():
            conn.execute("DELETE FROM pheromones WHERE source=? AND key=?", (r[0], r[1]))

    conn.commit()
    conn.close()


def stats() -> dict:
    """返回信息素层统计：总数、锚定数、冷痕数、各 source 分布。"""
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM pheromones").fetchone()[0]
    anchored = conn.execute("SELECT COUNT(*) FROM pheromones WHERE refs >= ?", (_ANCHOR_REFS,)).fetchone()[0]
    sources = {}
    for r in conn.execute("SELECT source, COUNT(*) as n FROM pheromones GROUP BY source"):
        sources[r[0]] = r[1]
    conn.close()
    return {
        "total": total,
        "anchored": anchored,
        "cold_threshold": _COLD_THRESHOLD,
        "max_traces": _MAX_TRACES,
        "sources": sources,
    }


def bump(source: str, key: str) -> float:
    """便捷函数：对一条痕 +1 引用（intensity=1.0, halflife_s=3600）。
    等同于 write(source, key, intensity=1.0, halflife_s=3600, bump_refs=True)。
    返回当前有效强度。"""
    write(source, key, intensity=1.0, halflife_s=3600, bump_refs=True)
    return read(source, key)


def heat(source: str) -> float:
    """返回指定 source 的平均有效强度（0~1）。
    0=冷，>0.5=热。用于快速判断某个来源的活跃程度。"""
    traces = sniff(source)
    if not traces:
        return 0.0
    return sum(t["effective"] for t in traces) / len(traces)
