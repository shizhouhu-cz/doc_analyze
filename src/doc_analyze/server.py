"""FastAPI 服务（design.md 第六章的 HTTP 面）：入库 / 章节树 / 问答 Agent。

启动：doc-analyze serve（或 uvicorn doc_analyze.server:app）。
GET /            本地调试页（static/debug.html）
GET  /api/documents           文档列表（含章节/表格计数）
POST /api/ingest              上传 DOCX 入库（multipart，字段名 file）
GET  /api/tree/{doc_id}       平铺章节树（按 order_num，前端按 level 缩进）
GET  /api/chapters/{id}       章节详情（含正文）
GET  /api/stats/{doc_id}      文档统计
POST /api/query               Agent 问答（返回 answer + 工具调用轨迹）
"""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from doc_analyze import db
from doc_analyze.config import load_settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动时确保库与表存在（只读端点对空库返回空结果而非 500）。"""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        db.init_schema(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="doc-analyze",
    description="DOCX 标书结构化提取与查询服务",
    lifespan=lifespan,
)

_DEBUG_HTML = Path(__file__).parent / "static" / "debug.html"


class QueryBody(BaseModel):
    doc_id: int
    question: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_DEBUG_HTML, media_type="text/html")


@app.get("/api/documents")
def list_documents() -> list[dict]:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT d.id, d.doc_name, d.status, d.total_chars, d.error_msg,"
            " (SELECT COUNT(*) FROM chapters c WHERE c.doc_id=d.id) AS chapters,"
            " (SELECT COUNT(*) FROM tables t WHERE t.doc_id=d.id) AS tables"
            " FROM documents d ORDER BY d.id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/ingest")
async def ingest(file: UploadFile) -> dict:
    """上传 DOCX 解析入库；同内容文件（sha256 命中）复用文档行强制重跑。"""
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="仅支持 .docx 文件")
    settings = load_settings()
    data = await file.read()
    tmp = Path(tempfile.gettempdir()) / f"doc_analyze_{uuid.uuid4().hex}.docx"
    tmp.write_bytes(data)
    try:
        from doc_analyze.pipeline.ingest import ingest_file

        return ingest_file(tmp, settings, display_name=file.filename)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"解析失败：{e}") from e
    finally:
        tmp.unlink(missing_ok=True)


@app.get("/api/tree/{doc_id}")
def chapter_tree(doc_id: int) -> list[dict]:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT id, level, number_prefix, title, needs_review, content_chars, source"
            " FROM chapters WHERE doc_id=? ORDER BY order_num",
            (doc_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise HTTPException(status_code=404, detail=f"文档 {doc_id} 无章节数据")
    return [dict(r) for r in rows]


@app.get("/api/chapters/{chapter_id}")
def chapter_detail(chapter_id: int) -> dict:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT id, doc_id, level, number_prefix, title, content, content_chars,"
            " chapter_path, source, needs_review FROM chapters WHERE id=?",
            (chapter_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"章节 {chapter_id} 不存在")
    return dict(row)


@app.get("/api/stats/{doc_id}")
def doc_stats(doc_id: int) -> dict:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        doc = conn.execute(
            "SELECT id, doc_name, status, total_chars, error_msg FROM documents WHERE id=?",
            (doc_id,),
        ).fetchone()
        if doc is None:
            raise HTTPException(status_code=404, detail=f"文档 {doc_id} 不存在")
        chapters = conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
        review = conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE doc_id=? AND needs_review=1", (doc_id,)
        ).fetchone()[0]
        tables = conn.execute(
            "SELECT COUNT(*) FROM tables WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "doc_id": doc_id,
        "doc_name": doc["doc_name"],
        "status": doc["status"],
        "total_chars": doc["total_chars"],
        "error_msg": doc["error_msg"],
        "chapters": chapters,
        "needs_review": review,
        "tables": tables,
    }


@app.post("/api/query")
def query(body: QueryBody) -> dict:
    from doc_analyze.agent.graph import run_query_trace
    from doc_analyze.llm.provider import LLMError

    settings = load_settings()
    try:
        return run_query_trace(
            settings,
            body.doc_id,
            body.question,
            thread_id=f"http-{uuid.uuid4().hex}",  # 每请求独立会话，避免 checkpoint 串历史
        )
    except LLMError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
