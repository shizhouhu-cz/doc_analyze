"""表格网格还原与 Markdown 渲染（design.md 4.5）。

用占用矩阵还原逻辑网格（gridSpan / vMerge restart-continue）；
嵌套表格取内层文本拼接，不递归建格。
"""
from __future__ import annotations

from doc_analyze.pipeline.features import W

_BOLD_FALSE = ("0", "false", "off")


def _cell_text(tc) -> str:
    """tc 下所有 w:t 拼接 strip（嵌套表格文本一并拼入，不递归建格）。"""
    return "".join(t.text or "" for t in tc.iter(W + "t")).strip()


def _run_bold(run) -> bool:
    rpr = run.find(W + "rPr")
    if rpr is None:
        return False
    b = rpr.find(W + "b")
    if b is None:
        return False
    v = b.get(W + "val")
    return v is None or v.strip().lower() not in _BOLD_FALSE


def extract_grid(tbl_element) -> dict:
    """w:tbl → {rows, cols, cells:[{row, col, row_span, col_span, text, is_header}]}。

    cols 取 tblGrid/gridCol 数；vMerge continue 不单独产出 cell，
    其内容并入 restart 行同列 cell 且 row_span 累加。
    """
    grid_el = tbl_element.find(W + "tblGrid")
    cols = len(grid_el.findall(W + "gridCol")) if grid_el is not None else 0
    trs = tbl_element.findall(W + "tr")
    rows = len(trs)

    cells: list[dict] = []
    occupied: dict[tuple[int, int], dict] = {}  # (row, col) → 占用的逻辑 cell
    merge_source: dict[tuple[int, int], dict] = {}  # (row, col) → 该列上方 vMerge restart cell

    for r, tr in enumerate(trs):
        trpr = tr.find(W + "trPr")
        tbl_header = trpr is not None and trpr.find(W + "tblHeader") is not None
        row_is_header = False
        if r == 0:
            runs = list(tr.iter(W + "r"))
            # 表头：trPr/tblHeader 存在，或整行所有 run 有 w:b
            row_is_header = tbl_header or (bool(runs) and all(_run_bold(run) for run in runs))
        c = 0
        for tc in tr.findall(W + "tc"):
            while (r, c) in occupied:
                c += 1
            col_span = 1
            vmerge: str | None = None
            tcpr = tc.find(W + "tcPr")
            if tcpr is not None:
                gs = tcpr.find(W + "gridSpan")
                if gs is not None:
                    try:
                        col_span = max(1, int(gs.get(W + "val")))
                    except (TypeError, ValueError):
                        col_span = 1
                vm = tcpr.find(W + "vMerge")
                if vm is not None:
                    vmerge = vm.get(W + "val") or "continue"

            if vmerge == "continue":
                src = None
                for rr in range(r - 1, -1, -1):
                    cand = merge_source.get((rr, c))
                    if cand is not None:
                        src = cand
                        break
                if src is not None:
                    # 继承 restart 行同列内容，row_span 累加；本格不再单独产出
                    src["row_span"] += 1
                    for cc in range(c, c + col_span):
                        occupied[(r, cc)] = src
                        merge_source[(r, cc)] = src
                    c += col_span
                    continue
                # 找不到 restart 源（畸形）：按普通空格处理

            cell = {
                "row": r,
                "col": c,
                "row_span": 1,
                "col_span": col_span,
                "text": _cell_text(tc),
                "is_header": bool(row_is_header),
            }
            cells.append(cell)
            if vmerge == "restart":
                for cc in range(c, c + col_span):
                    merge_source[(r, cc)] = cell
            for cc in range(c, c + col_span):
                occupied[(r, cc)] = cell
            c += col_span

    return {"rows": rows, "cols": cols, "cells": cells}


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def to_markdown(grid: dict) -> str:
    """逻辑网格 → 管道表 Markdown（首行为表头；无 caption）。

    跨行/跨列的逻辑格在覆盖位置填充同文本（row_span 拍平）。
    """
    rows, cols = grid["rows"], grid["cols"]
    cells = grid["cells"]
    if rows <= 0 or cols <= 0 or not cells:
        return ""
    matrix = [["" for _ in range(cols)] for _ in range(rows)]
    for cell in cells:
        for r in range(cell["row"], min(cell["row"] + cell["row_span"], rows)):
            for c in range(cell["col"], min(cell["col"] + cell["col_span"], cols)):
                matrix[r][c] = _md_escape(cell["text"])

    # 表头行：is_header 行，否则第一行
    header_idx = 0
    for cell in cells:
        if cell["is_header"]:
            header_idx = cell["row"]
            break
    ordered = [matrix[header_idx]] + [matrix[i] for i in range(rows) if i != header_idx]

    lines = ["| " + " | ".join(ordered[0]) + " |"]
    lines.append("| " + " | ".join(["---"] * cols) + " |")
    for row in ordered[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)
