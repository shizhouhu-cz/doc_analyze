"""查询 Agent 与工具测试（mock，无网络，tmp_path 建库）。"""
from __future__ import annotations

import pytest

from doc_analyze import db
from doc_analyze.agent.graph import build_graph, run_query
from doc_analyze.agent.tools import find_chapter, get_chapter, get_document_map, search_content
from doc_analyze.config import load_settings

# (parent_idx, level, number_prefix, title, content, order_num, chapter_path)
_CHAPTERS = [
    (None, 1, "第一章", "通用要求", "本章规定通用要求。", 1, "第一章 通用要求"),
    (0, 2, "1.1", "项目概况", "本项目为维护工程设计服务项目。", 2,
     "第一章 通用要求 > 1.1 项目概况"),
    (0, 2, "1.2", "评标办法", "评标采用综合评分法，评分细则见附件。", 3,
     "第一章 通用要求 > 1.2 评标办法"),
    (0, 2, "1.3", "保证金要求", "履约保证金为合同金额的10%。", 4,
     "第一章 通用要求 > 1.3 保证金要求"),
]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DOC_ANALYZE_DB", str(db_file))  # 工具内部用 load_settings()
    monkeypatch.setenv("DOC_ANALYZE_LLM_PROVIDER", "mock")  # 隔离全局 config.toml/env，钉住 mock
    settings = load_settings()
    conn = db.connect(db_file)
    db.init_schema(conn)
    cur = conn.execute(
        "INSERT INTO documents(doc_name, doc_hash, status, total_chars) VALUES (?,?,?,?)",
        ("测试标书", "hash-agent-1", "done", 500),
    )
    doc_id = int(cur.lastrowid)
    ids: list[int] = []
    for parent_idx, level, prefix, title, content, order_num, path in _CHAPTERS:
        parent_id = 0 if parent_idx is None else ids[parent_idx]
        cur = conn.execute(
            "INSERT INTO chapters(doc_id, parent_id, level, number_prefix, title, content, "
            "content_chars, order_num, chapter_path) VALUES (?,?,?,?,?,?,?,?,?)",
            (doc_id, parent_id, level, prefix, title, content, len(content), order_num, path),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    db.fts_index_doc(conn, doc_id)
    conn.close()
    return settings, doc_id, ids


def test_search_content_fts_hit(env):
    settings, doc_id, _ = env
    out = search_content.invoke({"doc_id": doc_id, "keywords": "保证金"})
    assert "保证金" in out
    assert "【保证金】" in out  # snippet 高亮
    assert "chapter_id=" in out
    assert "chapter_path=" in out


def test_search_content_like_fallback(env):
    settings, doc_id, _ = env
    out = search_content.invoke({"doc_id": doc_id, "keywords": "评分"})  # 2 字 -> LIKE 兜底
    assert "评分" in out
    assert "chapter_id=" in out


def test_find_chapter_hit(env):
    settings, doc_id, _ = env
    out = find_chapter.invoke({"doc_id": doc_id, "title_keywords": "评标"})  # 2 字 -> LIKE 兜底
    assert "评标办法" in out
    assert "|" in out


def test_get_chapter_truncation_and_offset(env):
    settings, doc_id, ids = env
    out = get_chapter.invoke({"doc_id": doc_id, "chapter_id": ids[3], "max_chars": 5})
    assert "下钻协议" in out
    assert "[截断] 用 offset=5 续读" in out
    assert "children: []" in out  # 叶子章节


def test_get_document_map_format(env):
    settings, doc_id, ids = env
    out = get_document_map.invoke({"doc_id": doc_id})
    assert f"{ids[0]}|1|" in out
    assert "第一章 通用要求" in out
    assert "评标办法" in out


def test_build_graph_and_run_query_mock(env):
    settings, doc_id, _ = env
    graph = build_graph(settings, doc_id)
    assert graph is not None
    answer = run_query(settings, doc_id, "履约保证金的比例是多少")
    assert answer.startswith("[MOCK 回答]")
    assert "chapter_path" in answer  # 工具输出带章节路径，可溯源


def test_build_graph_requires_provider(tmp_path):
    from doc_analyze.config import Settings
    from doc_analyze.llm.provider import LLMError

    settings = Settings(db_path=tmp_path / "x.db", llm_provider="unknown")
    with pytest.raises(LLMError):
        build_graph(settings, 1)
