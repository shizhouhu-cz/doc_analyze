"""查询 Agent 的 4 个工具（design.md 6.2）。

每个工具自开连接（线程安全简单化），Settings 用 load_settings()，返回有界文本。
"""
from __future__ import annotations

import json
import sqlite3

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from doc_analyze import db
from doc_analyze.config import load_settings

_DEFAULT_SETTINGS = load_settings()
DEFAULT_MAX_CHARS = _DEFAULT_SETTINGS.max_chars
DEFAULT_TOP_K = _DEFAULT_SETTINGS.top_k

_MAX_MAP_ROWS_FULL = 300  # 超出则只注入一二级 + 子树计数


def _chapter_name(prefix: str | None, title: str) -> str:
    prefix = (prefix or "").strip()
    return f"{prefix} {title}".strip() if prefix else title


def _map_line(row: sqlite3.Row) -> str:
    return f"{row['id']}|{row['level']}|{row['chapter_path'] or ''}|{_chapter_name(row['number_prefix'], row['title'])}"


def render_document_map(conn: sqlite3.Connection, doc_id: int) -> str:
    """紧凑文档地图（get_document_map 工具与 system prompt 预注入共用同一格式）。"""
    rows = conn.execute(
        "SELECT id, level, number_prefix, title, chapter_path FROM chapters "
        "WHERE doc_id=? ORDER BY order_num",
        (doc_id,),
    ).fetchall()
    if not rows:
        return f"文档 {doc_id} 暂无章节数据。"
    if len(rows) > _MAX_MAP_ROWS_FULL:
        counts: dict[int, int] = {}
        current_l1: int | None = None
        for r in rows:
            if r["level"] == 1:
                current_l1 = int(r["id"])
                counts[current_l1] = 0
            elif current_l1 is not None:
                counts[current_l1] += 1
        lines: list[str] = []
        for r in rows:
            if r["level"] == 1:
                lines.append(_map_line(r))
                lines.append(f"…含{counts.get(int(r['id']), 0)}个小节")
            elif r["level"] <= 2:
                lines.append(_map_line(r))
        return "\n".join(lines)
    return "\n".join(_map_line(r) for r in rows)


# ---------- get_document_map ----------


class GetDocumentMapArgs(BaseModel):
    doc_id: int = Field(description="文档 ID")


@tool("get_document_map", args_schema=GetDocumentMapArgs, parse_docstring=False)
def get_document_map(doc_id: int) -> str:
    """获取文档的紧凑标题树地图，每行格式 id|level|chapter_path|编号+标题。

    用于了解文档整体结构并定位章节，超过 300 个章节时只列一二级并附子树小节计数。
    拿到目标章节后配合 get_chapter 下钻获取正文。
    """
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        return render_document_map(conn, doc_id)
    finally:
        conn.close()


# ---------- get_chapter ----------


class GetChapterArgs(BaseModel):
    doc_id: int = Field(description="文档 ID")
    chapter_id: int = Field(description="章节 ID")
    max_chars: int = Field(default=DEFAULT_MAX_CHARS, description="单次返回正文字符上限")
    offset: int = Field(default=0, description="正文起始偏移，用于续读")


@tool("get_chapter", args_schema=GetChapterArgs, parse_docstring=False)
def get_chapter(
    doc_id: int, chapter_id: int, max_chars: int = DEFAULT_MAX_CHARS, offset: int = 0
) -> str:
    """读取指定章节自身正文（不含子章节正文）与子章节目录。

    下钻协议（design 6.2）：
    1. 返回的 content 不含子章节正文；children 为子章节目录（含 chapter_id/content_chars）。
    2. 需要子章节细节必须按其 chapter_id 继续调用本工具；content_chars 大的子章节可优先取。
    3. 正文被截断时按提示的 offset 续读。
    """
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT id, level, number_prefix, title, content, content_chars, chapter_path "
            "FROM chapters WHERE id=? AND doc_id=?",
            (chapter_id, doc_id),
        ).fetchone()
        if row is None:
            return f"未找到文档 {doc_id} 下的章节 {chapter_id}。"
        content = row["content"] or ""
        chunk = content[offset : offset + max_chars]
        truncated = offset + max_chars < len(content)
        parts = [
            "提示：以下 content 不含子章节正文，需要细节请用 children 里的 chapter_id "
            "继续取（design 6.2 下钻协议）。",
            f"chapter_id={row['id']} level={row['level']} "
            f"chapter_path={row['chapter_path'] or ''} "
            f"title={_chapter_name(row['number_prefix'], row['title'])}",
            chunk,
        ]
        if truncated:
            parts.append(f"[截断] 用 offset={offset + max_chars} 续读")
        children = conn.execute(
            "SELECT id, level, number_prefix, title, content_chars FROM chapters "
            "WHERE parent_id=? AND doc_id=? ORDER BY order_num",
            (chapter_id, doc_id),
        ).fetchall()
        if children:
            parts.append("children:")
            parts.extend(
                json.dumps(
                    {
                        "chapter_id": c["id"],
                        "level": c["level"],
                        "title": _chapter_name(c["number_prefix"], c["title"]),
                        "content_chars": c["content_chars"] or 0,
                    },
                    ensure_ascii=False,
                )
                for c in children
            )
        else:
            parts.append("children: []（叶子章节，无需再下钻）")
        return "\n".join(parts)
    finally:
        conn.close()


# ---------- search_content ----------


class SearchContentArgs(BaseModel):
    doc_id: int = Field(description="文档 ID")
    keywords: str = Field(description="空格分隔的关键词")
    chapter_id: int | None = Field(default=None, description="限定章节（可选）")
    top_k: int = Field(default=DEFAULT_TOP_K, description="返回条数上限")


def _search_like(
    conn: sqlite3.Connection,
    doc_id: int,
    terms: list[str],
    chapter_id: int | None,
    top_k: int,
) -> str:
    conds = ["doc_id=?"]
    params: list[object] = [doc_id]
    for t in terms:
        conds.append("(COALESCE(content,'') LIKE ? OR COALESCE(title,'') LIKE ?)")
        params.extend([f"%{t}%", f"%{t}%"])
    if chapter_id is not None:
        conds.append("id=?")
        params.append(chapter_id)
    sql = (
        "SELECT id, chapter_path, title, content FROM chapters WHERE "
        + " AND ".join(conds)
        + " ORDER BY order_num LIMIT ?"
    )
    params.append(top_k)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return "未检索到相关内容（LIKE 兜底），可尝试其他关键词或用 find_chapter 定位章节。"
    lines = []
    for r in rows:
        content = r["content"] or ""
        hit = ""
        for t in terms:
            pos = content.find(t)
            if pos >= 0:
                start = max(0, pos - 20)
                hit = ("…" if start > 0 else "") + content[start : pos + len(t) + 30] + "…"
                break
        if not hit:
            hit = r["title"] or ""
        lines.append(f"chapter_id={r['id']} chapter_path={r['chapter_path'] or ''} hit={hit}")
    return "\n".join(lines)


@tool("search_content", args_schema=SearchContentArgs, parse_docstring=False)
def search_content(
    doc_id: int,
    keywords: str,
    chapter_id: int | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> str:
    """FTS5 关键词检索章节正文，返回命中章节与片段（snippet）。

    返回行格式：chapter_id=<id> chapter_path=<路径> hit=<命中片段>，可溯源到具体章节。
    任一关键词不足 3 字时自动回退 LIKE 匹配。
    """
    settings = load_settings()
    terms = keywords.split()
    if not terms:
        return "关键词为空，请提供至少一个关键词。"
    conn = db.connect(settings.db_path)
    try:
        if any(len(t) < 3 for t in terms):
            return _search_like(conn, doc_id, terms, chapter_id, top_k)
        match_expr = " ".join(f'"{t}"' for t in terms)
        sql = (
            "SELECT c.id, c.chapter_path, c.title, "
            "snippet(chapters_fts,0,'【','】','…',12) AS hit, bm25(chapters_fts) AS rank "
            "FROM chapters_fts f JOIN chapters c ON c.id=f.rowid "
            "WHERE chapters_fts MATCH ? AND c.doc_id=?"
        )
        params: list[object] = [match_expr, doc_id]
        if chapter_id is not None:
            sql += " AND c.id=?"
            params.append(chapter_id)
        sql += " ORDER BY rank LIMIT ?"
        params.append(top_k)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return _search_like(conn, doc_id, terms, chapter_id, top_k)
        if not rows:
            return "未检索到相关内容，可尝试其他关键词或用 find_chapter 定位章节。"
        return "\n".join(
            f"chapter_id={r['id']} chapter_path={r['chapter_path'] or ''} hit={r['hit']}"
            for r in rows
        )
    finally:
        conn.close()


# ---------- find_chapter ----------


class FindChapterArgs(BaseModel):
    doc_id: int = Field(description="文档 ID")
    title_keywords: str = Field(description="标题关键词，空格分隔")


@tool("find_chapter", args_schema=FindChapterArgs, parse_docstring=False)
def find_chapter(doc_id: int, title_keywords: str) -> str:
    """按关键词模糊定位章节标题，每行格式 id|level|chapter_path|title。

    用于获取 chapter_id 后配合 get_chapter 下钻；关键词不足 3 字时回退 LIKE 匹配。
    """
    settings = load_settings()
    terms = title_keywords.split()
    if not terms:
        return "标题关键词为空，请提供至少一个关键词。"
    conn = db.connect(settings.db_path)
    try:
        rows: list[sqlite3.Row] = []
        if all(len(t) >= 3 for t in terms):
            match_expr = " ".join(f'"{t}"' for t in terms)
            try:
                rows = conn.execute(
                    "SELECT c.id, c.level, c.chapter_path, c.title "
                    "FROM chapter_titles_fts f JOIN chapters c ON c.id=f.rowid "
                    "WHERE chapter_titles_fts MATCH ? AND c.doc_id=? "
                    "ORDER BY c.order_num LIMIT 20",
                    (match_expr, doc_id),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            conds = ["doc_id=?"]
            params: list[object] = [doc_id]
            for t in terms:
                conds.append("COALESCE(title,'') LIKE ?")
                params.append(f"%{t}%")
            rows = conn.execute(
                "SELECT id, level, chapter_path, title FROM chapters WHERE "
                + " AND ".join(conds)
                + " ORDER BY order_num LIMIT 20",
                params,
            ).fetchall()
        if not rows:
            return "未找到匹配的章节标题。"
        return "\n".join(
            f"{r['id']}|{r['level']}|{r['chapter_path'] or ''}|{r['title']}" for r in rows
        )
    finally:
        conn.close()
