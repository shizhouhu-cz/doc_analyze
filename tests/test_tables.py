"""tables.py 单测：gridSpan / vMerge 网格还原与 Markdown 渲染。"""
import pytest
from lxml import etree

from doc_analyze.pipeline.tables import extract_grid, to_markdown

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _tbl(rows_xml: str) -> etree._Element:
    xml = (
        f'<w:tbl {W_NS}><w:tblPr/><w:tblGrid>'
        '<w:gridCol/><w:gridCol/><w:gridCol/>'
        f'</w:tblGrid>{rows_xml}</w:tbl>'
    )
    return etree.fromstring(xml.encode("utf-8"))


def _bcell(text: str, tcpr: str = "") -> str:
    return (
        f'<w:tc>{tcpr}<w:p><w:r><w:rPr><w:b/></w:rPr>'
        f'<w:t>{text}</w:t></w:r></w:p></w:tc>'
    )


def _cell(text: str, tcpr: str = "") -> str:
    return f'<w:tc>{tcpr}<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>'


@pytest.fixture()
def merged_tbl():
    """首行加粗表头；第二行 gridSpan=2；第三/四行 vMerge restart+continue。"""
    rows = (
        "<w:tr>" + _bcell("名称") + _bcell("数量") + _bcell("备注") + "</w:tr>"
        "<w:tr>" + _cell("合计", '<w:tcPr><w:gridSpan w:val="2"/></w:tcPr>')
        + _cell("2") + "</w:tr>"
        "<w:tr>" + _cell("项目A", '<w:tcPr><w:vMerge w:val="restart"/></w:tcPr>')
        + _cell("1") + _cell("x") + "</w:tr>"
        "<w:tr>" + _cell("", '<w:tcPr><w:vMerge/></w:tcPr>')
        + _cell("3") + _cell("y") + "</w:tr>"
    )
    return _tbl(rows)


class TestExtractGrid:
    def test_dimensions(self, merged_tbl):
        grid = extract_grid(merged_tbl)
        assert grid["rows"] == 4
        assert grid["cols"] == 3

    def test_header_row(self, merged_tbl):
        grid = extract_grid(merged_tbl)
        row0 = [c for c in grid["cells"] if c["row"] == 0]
        assert len(row0) == 3
        assert all(c["is_header"] for c in row0)
        others = [c for c in grid["cells"] if c["row"] != 0]
        assert all(not c["is_header"] for c in others)

    def test_grid_span(self, merged_tbl):
        grid = extract_grid(merged_tbl)
        span_cell = [c for c in grid["cells"] if c["row"] == 1 and c["col"] == 0]
        assert len(span_cell) == 1
        assert span_cell[0]["col_span"] == 2
        assert span_cell[0]["text"] == "合计"
        assert span_cell[0]["row_span"] == 1

    def test_vmerge_restart_and_continue(self, merged_tbl):
        grid = extract_grid(merged_tbl)
        merged = [c for c in grid["cells"] if c["row"] == 2 and c["col"] == 0]
        assert len(merged) == 1
        assert merged[0]["row_span"] == 2  # continue 行并入，row_span 累加
        assert merged[0]["text"] == "项目A"
        # continue 行不单独产出 cell
        assert not [c for c in grid["cells"] if c["row"] == 3 and c["col"] == 0]
        # 其余列正常落位
        assert [c["col"] for c in grid["cells"] if c["row"] == 3] == [1, 2]

    def test_tbl_header_flag(self):
        rows = (
            '<w:tr><w:trPr><w:tblHeader/></w:trPr>'
            + _cell("甲") + _cell("乙") + _cell("丙") + "</w:tr>"
            "<w:tr>" + _cell("1") + _cell("2") + _cell("3") + "</w:tr>"
        )
        grid = extract_grid(_tbl(rows))
        assert all(c["is_header"] for c in grid["cells"] if c["row"] == 0)

    def test_non_bold_first_row_not_header(self):
        rows = (
            "<w:tr>" + _cell("甲") + _cell("乙") + _cell("丙") + "</w:tr>"
            "<w:tr>" + _cell("1") + _cell("2") + _cell("3") + "</w:tr>"
        )
        grid = extract_grid(_tbl(rows))
        assert all(not c["is_header"] for c in grid["cells"])

    def test_nested_table_text_flattened(self):
        rows = (
            "<w:tr>" + _bcell("外层")
            + '<w:tc><w:p/><w:tbl><w:tr><w:tc><w:p><w:r><w:t>内层</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:tc>'
            + _bcell("尾") + "</w:tr>"
        )
        grid = extract_grid(_tbl(rows))
        assert grid["cells"][1]["text"] == "内层"  # 取内层文本拼接，不递归建格


class TestToMarkdown:
    def test_markdown_with_spans(self, merged_tbl):
        md = to_markdown(extract_grid(merged_tbl))
        assert md == (
            "| 名称 | 数量 | 备注 |\n"
            "| --- | --- | --- |\n"
            "| 合计 | 合计 | 2 |\n"
            "| 项目A | 1 | x |\n"
            "| 项目A | 3 | y |"
        )

    def test_pipe_escaped(self):
        rows = "<w:tr>" + _cell("a|b") + _cell("c") + "</w:tr>"
        md = to_markdown(extract_grid(_tbl(rows)))
        assert "a\\|b" in md

    def test_empty(self):
        assert to_markdown({"rows": 0, "cols": 0, "cells": []}) == ""
