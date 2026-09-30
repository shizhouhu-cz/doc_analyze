"""端到端与流水线清洗：真实招标文件 → ingest_file → 落库统计。

标记 local 的用例需要真实 DOCX（默认项目根的定稿招标文件，
可用 DOC_ANALYZE_TEST_DOCX 环境变量覆盖）。
"""
import os
import sqlite3
from pathlib import Path

import pytest
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

from doc_analyze.config import load_settings
from doc_analyze.pipeline.features import W
from doc_analyze.pipeline.ingest import ingest_file, select_body_items

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOC = ROOT / (
    "定稿-中国电信股份有限公司长沙分公司2026-2027年维护工程项目设计服务采购项目.docx"
)


def _add_p(doc, inner: str) -> None:
    doc.element.body.append(parse_xml(f'<w:p {nsdecls("w")}>{inner}</w:p>'))


def _fld(kind: str) -> str:
    return f'<w:r><w:fldChar w:fldCharType="{kind}"/></w:r>'


def _instr(text: str) -> str:
    return f'<w:r><w:instrText xml:space="preserve">{text}</w:instrText></w:r>'


def _t(text: str) -> str:
    return f"<w:r><w:t>{text}</w:t></w:r>"


class TestBodyCleaning:
    def test_toc_region_and_template_noise_removed(self):
        doc = Document()
        _add_p(doc, _t("共 30 页"))  # 首 3 段模板噪声
        _add_p(doc, _t("第一章 招标公告"))
        # TOC 起始段：TOC instrText + 域未在本段闭合（含嵌套 PAGEREF）
        _add_p(
            doc,
            _fld("begin") + _instr(' TOC \\o "1-3" ') + _fld("separate")
            + _t("第一章 招标公告4"),
        )
        # TOC 条目段：嵌套 PAGEREF 域本段开合平衡，但外层 TOC 域仍打开
        _add_p(
            doc,
            _t("1.1 项目概况") + _fld("begin") + _instr(" PAGEREF _Toc1 ")
            + _fld("separate") + _t("19") + _fld("end"),
        )
        # TOC 收尾段：闭合外层域
        _add_p(doc, _t("第二章 投标人须知9") + _fld("end"))
        for i in range(4):
            _add_p(doc, _t(f"正文段落{i}"))
        _add_p(doc, _t("特此声明"))  # 尾部模板噪声

        kept = select_body_items(doc)
        texts = [
            "".join(t.text or "" for t in el.iter(W + "t")) for kind, el in kept if kind == "p"
        ]
        assert texts == [
            "第一章 招标公告",
            "正文段落0",
            "正文段落1",
            "正文段落2",
            "正文段落3",
        ]

    def test_empty_and_whitespace_removed(self):
        doc = Document()
        _add_p(doc, _t("   "))
        _add_p(doc, _t("正常段落"))
        kept = select_body_items(doc)
        assert len(kept) == 1
        assert kept[0][0] == "p"


@pytest.mark.local
def test_e2e_real_tender_doc():
    doc_path = Path(os.environ.get("DOC_ANALYZE_TEST_DOCX", str(DEFAULT_DOC)))
    assert doc_path.exists(), f"测试文档不存在：{doc_path}"

    settings = load_settings()
    settings.db_path = ROOT / "doc_analyze.db"
    settings.llm_provider = "mock"  # 钉住 mock，隔离 config.toml 的真实网关配置
    # 幂等重跑：清除上次测试库（含 WAL 伴生文件），强制走完整流水线
    for suffix in ("", "-wal", "-shm"):
        Path(str(settings.db_path) + suffix).unlink(missing_ok=True)
    stats = ingest_file(doc_path, settings)
    print("ingest stats:", stats)

    assert "skipped" not in stats, "文档已解析过，应清库重跑以验证全流程"
    assert stats["chapters"] >= 5
    assert stats["tables"] >= 1
    assert stats["elapsed_seconds"] > 0

    conn = sqlite3.connect(str(settings.db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT number_prefix, title, chapter_path FROM chapters"
            " WHERE doc_id=? AND level=1 ORDER BY order_num",
            (stats["doc_id"],),
        ).fetchall()
        print(f"一级章节（{len(rows)} 个）样例：")
        for r in rows[:10]:
            print(f"  L1 | {r['number_prefix'] or ''} {r['title']}")
        doc = conn.execute(
            "SELECT status, total_chars FROM documents WHERE id=?", (stats["doc_id"],)
        ).fetchone()
        print(f"documents.status={doc['status']} total_chars={doc['total_chars']}")
        assert doc["status"] == "done"
    finally:
        conn.close()
