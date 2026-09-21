"""SQLite 数据访问层。

容器启动时调用 init_db() 建表（IF NOT EXISTS，幂等）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

DB_PATH = os.environ.get("DB_PATH", "/data/review.db")

# 对齐状态：pending（待审阅）/ confirmed（已决定）
VALID_STATUSES = ("pending", "confirmed")
# 变更类型
VALID_TYPES = ("unchanged", "added", "deleted", "split", "merged", "rewritten")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    db_path = DB_PATH
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def db_conn():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    old_filename TEXT,
    new_filename TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paragraphs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id  INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    side      TEXT NOT NULL CHECK(side IN ('old', 'new')),
    idx       INTEGER NOT NULL,           -- 1 起始的段落序号
    text      TEXT NOT NULL,
    UNIQUE(batch_id, side, idx)
);
CREATE INDEX IF NOT EXISTS idx_paragraphs_batch ON paragraphs(batch_id, side);

CREATE TABLE IF NOT EXISTS alignments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    old_idx       TEXT,                  -- 逗号分隔的 1 基序号，空串表示无
    new_idx       TEXT,
    change_type   TEXT NOT NULL CHECK(change_type IN
                    ('unchanged','added','deleted','split','merged','rewritten')),
    confidence    REAL NOT NULL DEFAULT 0,
    rationale     TEXT NOT NULL DEFAULT '{}',   -- JSON：可解释依据
    is_manual     INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','confirmed')),
    decision      TEXT,                          -- 审阅决定：approve / reject / modify
    decision_note TEXT,                          -- 审阅备注
    decided_at    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alignments_batch ON alignments(batch_id);
"""


def init_db() -> None:
    """应用启动时初始化数据库（幂等）。"""
    with db_conn() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- batch 操作

def create_batch(
    conn: sqlite3.Connection,
    name: str,
    old_filename: Optional[str],
    new_filename: Optional[str],
    old_paragraphs: list[str],
    new_paragraphs: list[str],
    rows: list[dict[str, Any]],
) -> int:
    """在单个事务中创建批次、段落与对齐候选。rows 来自 alignment.align()。"""
    created = _now()
    cur = conn.execute(
        "INSERT INTO batches(name, old_filename, new_filename, created_at) "
        "VALUES (?,?,?,?)",
        (name, old_filename, new_filename, created),
    )
    batch_id = cur.lastrowid

    conn.executemany(
        "INSERT INTO paragraphs(batch_id, side, idx, text) VALUES (?,?,?,?)",
        [
            (batch_id, "old", i + 1, t)
            for i, t in enumerate(old_paragraphs)
        ]
        + [
            (batch_id, "new", i + 1, t)
            for i, t in enumerate(new_paragraphs)
        ],
    )

    conn.executemany(
        """INSERT INTO alignments
           (batch_id, old_idx, new_idx, change_type, confidence,
            rationale, is_manual, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,0,'pending',?,?)""",
        [
            (
                batch_id,
                _pack_idx(r["old_idx"]),
                _pack_idx(r["new_idx"]),
                r["change_type"],
                float(r["confidence"]),
                json.dumps(r["rationale"], ensure_ascii=False),
                created,
                created,
            )
            for r in rows
        ],
    )
    return batch_id


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT b.*,
                      (SELECT COUNT(*) FROM paragraphs p
                       WHERE p.batch_id=b.id AND p.side='old') AS old_count,
                      (SELECT COUNT(*) FROM paragraphs p
                       WHERE p.batch_id=b.id AND p.side='new') AS new_count,
                      (SELECT COUNT(*) FROM alignments a WHERE a.batch_id=b.id)
                          AS alignment_count,
                      (SELECT COUNT(*) FROM alignments a
                       WHERE a.batch_id=b.id AND a.status='confirmed')
                          AS confirmed_count
               FROM batches b ORDER BY b.id DESC"""
        ).fetchall()
    )


def get_batch(conn: sqlite3.Connection, batch_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batches WHERE id=?", (batch_id,)
    ).fetchone()


def delete_batch(conn: sqlite3.Connection, batch_id: int) -> bool:
    cur = conn.execute("DELETE FROM batches WHERE id=?", (batch_id,))
    return cur.rowcount > 0


def clear_alignments(conn: sqlite3.Connection, batch_id: int) -> None:
    conn.execute("DELETE FROM alignments WHERE batch_id=?", (batch_id,))


def insert_alignment_rows(
    conn: sqlite3.Connection, batch_id: int, rows: Iterable[dict[str, Any]]
) -> None:
    created = _now()
    conn.executemany(
        """INSERT INTO alignments
           (batch_id, old_idx, new_idx, change_type, confidence,
            rationale, is_manual, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                batch_id,
                _pack_idx(r["old_idx"]),
                _pack_idx(r["new_idx"]),
                r["change_type"],
                float(r["confidence"]),
                json.dumps(r["rationale"], ensure_ascii=False),
                int(r.get("is_manual", 0)),
                r.get("status", "pending"),
                created,
                created,
            )
            for r in rows
        ],
    )


# ------------------------------------------------------------ paragraph 操作

def get_paragraphs(
    conn: sqlite3.Connection, batch_id: int, side: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT idx, text FROM paragraphs WHERE batch_id=? AND side=? "
            "ORDER BY idx",
            (batch_id, side),
        ).fetchall()
    )


# ----------------------------------------------------------- alignment 查询

def list_alignments(
    conn: sqlite3.Connection, batch_id: int
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM alignments WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
    )


def get_alignment(
    conn: sqlite3.Connection, alignment_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alignments WHERE id=?", (alignment_id,)
    ).fetchone()


def reclaim_paragraphs(
    conn: sqlite3.Connection,
    batch_id: int,
    old_idx: list[int],
    new_idx: list[int],
    exclude_id: int,
) -> list[int]:
    """把被其他候选占用的段落收回给当前编辑的候选。

    从冲突候选中移除相应段落。两侧段落都被移空的候选直接删除
    （其段落已全部由当前候选覆盖）；仅剩单侧的候选保留，交由上层
    重判为纯删除/新增。返回受影响候选 id 列表（负数 id 表示被删除）。
    """
    old_take = set(old_idx)
    new_take = set(new_idx)
    affected: list[int] = []
    for row in list_alignments(conn, batch_id):
        if row["id"] == exclude_id:
            continue
        ro = set(parse_idx(row["old_idx"]))
        rn = set(parse_idx(row["new_idx"]))
        keep_o = ro - old_take
        keep_n = rn - new_take
        if keep_o == ro and keep_n == rn:
            continue
        if not keep_o and not keep_n:
            conn.execute("DELETE FROM alignments WHERE id=?", (row["id"],))
            affected.append(-row["id"])
            continue
        affected.append(row["id"])
        conn.execute(
            "UPDATE alignments SET old_idx=?, new_idx=?, is_manual=1, "
            "updated_at=? WHERE id=?",
            (
                _pack_idx(sorted(keep_o)),
                _pack_idx(sorted(keep_n)),
                _now(),
                row["id"],
            ),
        )
    return affected


def update_alignment(
    conn: sqlite3.Connection, alignment_id: int, fields: dict[str, Any]
) -> None:
    fields = {**fields, "updated_at": _now()}
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE alignments SET {sets} WHERE id=?",
        (*fields.values(), alignment_id),
    )


def _pack_idx(indices: Optional[list[int]]) -> str:
    """列表 -> 存储字符串（1 基序号，逗号分隔；无映射存 NULL）。"""
    if not indices:
        return None  # type: ignore[return-value]
    return ",".join(str(i) for i in indices)


def parse_idx(value: Optional[str]) -> list[int]:
    """存储字符串 -> 整数列表。"""
    if not value:
        return []
    return [int(x) for x in value.split(",") if x != ""]
