"""SQLite 存储层：Schema（design.md 第五章）+ 连接管理 + FTS 辅助。

FTS5 虚表独立存储（rowid = chapters.id），流水线尾部一次性填充。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_name TEXT NOT NULL,
    doc_type TEXT DEFAULT 'docx',
    doc_hash TEXT UNIQUE,
    status TEXT DEFAULT 'pending',
    error_msg TEXT,
    total_chars INTEGER,
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    parent_id INTEGER DEFAULT 0,
    level INTEGER NOT NULL,
    number_prefix TEXT,
    title TEXT NOT NULL,
    content TEXT,
    content_chars INTEGER,
    order_num INTEGER NOT NULL,
    chapter_path TEXT,
    source TEXT DEFAULT 'rule',
    confidence REAL DEFAULT 1.0,
    needs_review INTEGER DEFAULT 0,
    FOREIGN KEY (doc_id) REFERENCES documents(id)
);
CREATE INDEX IF NOT EXISTS idx_chapters_doc ON chapters(doc_id, order_num);

CREATE TABLE IF NOT EXISTS tables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    chapter_id INTEGER NOT NULL,
    table_caption TEXT,
    total_rows INTEGER NOT NULL,
    total_cols INTEGER NOT NULL,
    order_num INTEGER,
    chapter_path TEXT,
    FOREIGN KEY (doc_id) REFERENCES documents(id)
);

CREATE TABLE IF NOT EXISTS table_cells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id INTEGER NOT NULL,
    row_idx INTEGER NOT NULL,
    col_idx INTEGER NOT NULL,
    row_span INTEGER DEFAULT 1,
    col_span INTEGER DEFAULT 1,
    content TEXT NOT NULL,
    is_header INTEGER DEFAULT 0,
    FOREIGN KEY (table_id) REFERENCES tables(id)
);
CREATE INDEX IF NOT EXISTS idx_cells_table ON table_cells(table_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chapters_fts USING fts5(content, tokenize='trigram');
CREATE VIRTUAL TABLE IF NOT EXISTS chapter_titles_fts USING fts5(title, tokenize='trigram');
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_by_hash(conn: sqlite3.Connection, doc_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM documents WHERE doc_hash = ?", (doc_hash,)
    ).fetchone()


def create_document(conn: sqlite3.Connection, doc_name: str, doc_hash: str) -> int:
    cur = conn.execute(
        "INSERT INTO documents(doc_name, doc_hash, status) VALUES (?, ?, 'processing')",
        (doc_name, doc_hash),
    )
    conn.commit()
    return int(cur.lastrowid)


def set_status(
    conn: sqlite3.Connection,
    doc_id: int,
    status: str,
    error_msg: str | None = None,
    total_chars: int | None = None,
) -> None:
    conn.execute(
        "UPDATE documents SET status=?, error_msg=?, total_chars=COALESCE(?, total_chars) WHERE id=?",
        (status, error_msg, total_chars, doc_id),
    )
    conn.commit()


def clear_doc_data(conn: sqlite3.Connection, doc_id: int) -> None:
    """重新解析前清空旧数据（含 FTS 行）。"""
    for sql in (
        "DELETE FROM chapters_fts WHERE rowid IN (SELECT id FROM chapters WHERE doc_id=?)",
        "DELETE FROM chapter_titles_fts WHERE rowid IN (SELECT id FROM chapters WHERE doc_id=?)",
        "DELETE FROM table_cells WHERE table_id IN (SELECT id FROM tables WHERE doc_id=?)",
        "DELETE FROM tables WHERE doc_id=?",
        "DELETE FROM chapters WHERE doc_id=?",
    ):
        conn.execute(sql, (doc_id,))
    conn.commit()


def fts_index_doc(conn: sqlite3.Connection, doc_id: int) -> None:
    """流水线尾部一次性填充 FTS（外部不维护触发器）。"""
    conn.execute(
        "INSERT INTO chapters_fts(rowid, content) "
        "SELECT id, COALESCE(title,'') || ' ' || COALESCE(content,'') FROM chapters WHERE doc_id=?",
        (doc_id,),
    )
    conn.execute(
        "INSERT INTO chapter_titles_fts(rowid, title) "
        "SELECT id, title FROM chapters WHERE doc_id=?",
        (doc_id,),
    )
    conn.commit()
