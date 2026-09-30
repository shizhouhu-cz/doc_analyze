"""doc-analyze 命令行入口：ingest / query / tree / stats。"""
from __future__ import annotations

from pathlib import Path

import typer

from doc_analyze.config import load_settings
from doc_analyze.db import connect as db_connect

app = typer.Typer(
    help="DOCX 标书结构化提取与查询系统（第一阶段）",
    no_args_is_help=True,
)


@app.command("ingest")
def ingest(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="DOCX 文件路径"),
) -> None:
    """解析 DOCX 并结构化入库（依赖 pipeline 包）。"""
    try:
        from doc_analyze.pipeline.ingest import ingest_file
    except ImportError as e:
        typer.secho(f"pipeline 未就绪：{e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    settings = load_settings()
    stats = ingest_file(path, settings)
    typer.echo(f"入库完成：{stats}")


@app.command("query")
def query(
    doc_id: int = typer.Argument(..., help="文档 ID"),
    question: str = typer.Argument(..., help="用户问题"),
) -> None:
    """对已入库文档提问（LangGraph Agent，下钻式检索）。"""
    from doc_analyze.agent.graph import run_query

    settings = load_settings()
    answer = run_query(settings, doc_id, question)
    typer.echo(answer)


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", help="监听地址"),
    port: int = typer.Option(8000, help="监听端口"),
) -> None:
    """启动 FastAPI 服务；浏览器打开 http://host:port/ 进入调试台。"""
    try:
        import uvicorn
    except ImportError as e:
        typer.secho(f"缺少服务依赖：请 pip install fastapi uvicorn python-multipart（{e}）",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    uvicorn.run("doc_analyze.server:app", host=host, port=port, log_level="info")


@app.command("tree")
def tree(
    doc_id: int = typer.Argument(..., help="文档 ID"),
) -> None:
    """打印文档章节树（每级缩进两空格，needs_review 标 *）。"""
    settings = load_settings()
    conn = db_connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT id, level, number_prefix, title, needs_review FROM chapters "
            "WHERE doc_id=? ORDER BY order_num",
            (doc_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        typer.echo(f"文档 {doc_id} 无章节数据")
        raise typer.Exit(code=1)
    for r in rows:
        indent = "  " * max((r["level"] or 1) - 1, 0)
        prefix = (r["number_prefix"] or "").strip()
        name = f"{prefix} {r['title']}" if prefix else r["title"]
        mark = " *" if r["needs_review"] else ""
        typer.echo(f"{indent}{r['id']} {name}{mark}")


@app.command("stats")
def stats(
    doc_id: int = typer.Argument(..., help="文档 ID"),
) -> None:
    """打印文档统计：状态、章节数、needs_review 数、表格数。"""
    settings = load_settings()
    conn = db_connect(settings.db_path)
    try:
        doc = conn.execute(
            "SELECT doc_name, status, total_chars, error_msg FROM documents WHERE id=?",
            (doc_id,),
        ).fetchone()
        chapter_count = conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
        review_count = conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE doc_id=? AND needs_review=1", (doc_id,)
        ).fetchone()[0]
        table_count = conn.execute(
            "SELECT COUNT(*) FROM tables WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    if doc is None:
        typer.secho(f"文档 {doc_id} 不存在", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    info = {
        "doc_id": doc_id,
        "doc_name": doc["doc_name"],
        "status": doc["status"],
        "total_chars": doc["total_chars"],
        "error_msg": doc["error_msg"],
        "chapters": chapter_count,
        "needs_review": review_count,
        "tables": table_count,
    }
    typer.echo(str(info))


if __name__ == "__main__":
    app()
