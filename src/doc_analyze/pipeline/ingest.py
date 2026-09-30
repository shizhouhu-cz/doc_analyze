"""流水线编排（design.md 第四章）。

遍历清洗 → 特征读取与候选粗筛 → 瘦版编号渲染 → LLM 全局恢复（可降级）
→ 复检与栈式建树 → 段落归属与表格 Markdown → 落库 + FTS。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from docx import Document

from doc_analyze import db
from doc_analyze.config import Settings
from doc_analyze.llm import provider as llm_provider
from doc_analyze.pipeline import features as feats
from doc_analyze.pipeline import numbering as numbering_mod
from doc_analyze.pipeline import tables as tables_mod
from doc_analyze.pipeline import treebuild

_W = feats.W

# 模板噪声（仅作用于文档首尾各 3 段）
_TEMPLATE_NOISE_RES = (
    re.compile(r"共\s*\d+\s*页"),
    re.compile(r"第\s*\d+\s*页"),
    re.compile(r"特此声明"),
)


def _is_toc_paragraph(p_el) -> bool:
    """TOC 域段落：instrText 含 TOC，或 fldSimple 的 instr 含 TOC。"""
    for it in p_el.iter(_W + "instrText"):
        if "TOC" in (it.text or ""):
            return True
    for fs in p_el.iter(_W + "fldSimple"):
        if "TOC" in (fs.get(_W + "instr") or ""):
            return True
    return False


def _is_template_noise(text: str) -> bool:
    return any(rx.search(text) for rx in _TEMPLATE_NOISE_RES)


# 单次 LLM 调用的候选预算（design 4.4：>400~500 行按一级锚点分段）
RECOVERY_CHUNK_SIZE = 450


def _recover_decisions(items: list[dict], settings: Settings) -> list[dict] | None:
    """全局结构恢复：候选超预算时按一级锚点分段调用，合并各段决策。

    分段边界取在一级锚点（suggested_level==1）上，每段以一级锚点开头，
    段间靠锚点路径衔接；无锚点可切时按预算硬切。任一段失败整体返回
    None（调用方降级到建议层级建树）。
    """
    from doc_analyze.llm.recovery import recover_structure

    if len(items) <= RECOVERY_CHUNK_SIZE:
        return recover_structure(items, settings)
    anchors = [i for i, it in enumerate(items) if it.get("suggested_level") == 1]
    bounds = [0]
    while bounds[-1] < len(items):
        start = bounds[-1]
        window = [a for a in anchors if start < a <= start + RECOVERY_CHUNK_SIZE]
        if window:
            bounds.append(window[-1])
        elif start + RECOVERY_CHUNK_SIZE >= len(items):
            bounds.append(len(items))
        else:
            bounds.append(start + RECOVERY_CHUNK_SIZE)
    decisions: list[dict] = []
    for a, b in zip(bounds, bounds[1:]):
        part = recover_structure(items[a:b], settings)
        if part is None:
            return None
        decisions.extend(part)
    return decisions


def _iter_body_items(document):
    """按 document.element.body 子节点顺序产出 ('p'|'tbl', element)，保持阅读顺序。"""
    for child in document.element.body:
        if child.tag == _W + "p":
            yield "p", child
        elif child.tag == _W + "tbl":
            yield "tbl", child


def ingest_file(
    path: str | Path, settings: Settings, display_name: str | None = None
) -> dict:
    """解析单个 DOCX 并落库；hash 命中已入库文档时复用行清旧数据强制重跑。

    display_name：落库的文档名（上传场景与磁盘文件名不同时使用），缺省用文件名。
    """
    path = Path(path)
    started = time.perf_counter()
    conn = db.connect(settings.db_path)
    try:
        db.init_schema(conn)
        doc_hash = db.file_sha256(path)
        existing = db.find_by_hash(conn, doc_hash)
        if existing is not None:  # 重复解析不跳过：复用行，清旧数据强制重跑
            doc_id = int(existing["id"])
            db.clear_doc_data(conn, doc_id)
            db.set_status(conn, doc_id, "processing")
        else:
            doc_id = db.create_document(conn, display_name or path.name, doc_hash)
        try:
            return _run_pipeline(conn, doc_id, path, settings, started)
        except Exception as e:
            db.set_status(conn, doc_id, "failed", error_msg=str(e))
            raise
    finally:
        conn.close()


def select_body_items(document) -> list[tuple[str, object]]:
    """按阅读顺序产出清洗后的 body 元素（('p'|'tbl', element)）。

    噪声：空段/全空白段；TOC 域段落及其跨段落结果区（域嵌套深度状态机）；
    文档首尾各 3 段内的模板噪声（共 N 页 / 第 N 页 / 特此声明）。
    """
    body_items = list(_iter_body_items(document))
    p_total = sum(1 for kind, _ in body_items if kind == "p")
    kept: list[tuple[str, object]] = []
    p_seen = 0
    field_depth = 0  # 当前打开的域嵌套深度（跨段落累计）
    toc_skip_depth: int | None = None  # TOC 域结果区：深度降回该值以下即域闭合
    for kind, el in body_items:
        if kind == "tbl":
            kept.append(("tbl", el))
            continue
        p_seen += 1
        # 跨段落域状态机：TOC 域结果区内的条目段落一并剔除（design 4.1 TOC 域剔除）
        in_toc_result = toc_skip_depth is not None and field_depth >= toc_skip_depth
        for fc in el.iter(_W + "fldChar"):
            t = fc.get(_W + "fldCharType")
            if t == "begin":
                field_depth += 1
            elif t == "end" and field_depth > 0:
                field_depth -= 1
        if in_toc_result:
            if field_depth < toc_skip_depth:
                toc_skip_depth = None  # 本段为 TOC 域收尾段，域已闭合
            continue
        if _is_toc_paragraph(el):
            if field_depth > 0:  # TOC 域未在本段闭合：后续段落为域结果
                toc_skip_depth = field_depth
            continue
        text = "".join(t.text or "" for t in el.iter(_W + "t"))
        if not text.strip():
            continue
        if (p_seen <= 3 or p_seen > p_total - 3) and _is_template_noise(text):
            continue
        kept.append(("p", el))
    return kept


def _run_pipeline(conn, doc_id: int, path: Path, settings: Settings, started: float) -> dict:
    document = Document(str(path))
    style_map = feats.build_style_map(document.styles.element)
    renderer = numbering_mod.NumberingRenderer.from_document(document)

    # ---- ① 遍历 + 噪声清洗 ----
    kept = select_body_items(document)

    # ---- ② 段落特征 + 基准 ----
    kept_p_els = [el for kind, el in kept if kind == "p"]
    para_features = [
        feats.paragraph_features(el, i, style_map) for i, el in enumerate(kept_p_els)
    ]
    baseline_font, baseline_indent = feats.compute_baselines(para_features)

    # ---- 瘦版编号渲染：全文按文档顺序推进计数器（含正文列表项）----
    rendered_num: dict[int, str] = {}  # para_index → 渲染编号文本
    for f in para_features:
        num_id, ilvl = f.num_id, f.ilvl
        if num_id is None and f.style_id:  # 段落无 numPr 时查样式映射
            mapped = renderer.pstyle_map.get(f.style_id)
            if mapped:
                num_id, ilvl = mapped
        if num_id:
            txt = renderer.render(num_id, ilvl)
            if txt:
                rendered_num[f.para_index] = txt

    # ---- 候选粗筛：number_prefix / title / suggested_level ----
    candidates: list[dict] = []
    for f in para_features:
        if not feats.is_candidate(f, baseline_font, baseline_indent):
            continue
        text = f.text.strip()
        number_prefix = None
        title = text
        manual = feats.split_manual_prefix(f.text)  # 手动编号：前缀取自段首文本
        if manual:
            number_prefix, title = manual
        else:
            auto = rendered_num.get(f.para_index)  # 自动编号：渲染文本
            if auto:
                number_prefix = auto
        sug = feats.suggest_level(f)
        candidates.append(
            {
                "para_index": f.para_index,
                "text": text,
                "number_prefix": number_prefix,
                "title": title,
                "suggested_level": sug,
                "is_anchor": sug is not None,
                "bold": f.bold,
                "font_size_halfpt": f.font_size_halfpt,
                "first_line_indent_chars": f.first_line_indent_chars,
            }
        )

    # ---- ③ LLM 全局结构恢复（延迟导入；失败/缺失走降级）----
    model_decisions: list[dict] | None = None
    provider = llm_provider.get_provider(settings)
    if provider is not None and candidates:
        try:
            from doc_analyze.llm.recovery import recover_structure
        except ImportError:
            recover_structure = None
        if recover_structure is not None:
            try:
                # 对齐 llm.recovery 契约：items 以 idx 为键（design 4.4）
                items = [
                    {
                        "idx": c["para_index"],
                        "number_prefix": c.get("number_prefix"),
                        "title": c.get("text", ""),
                        "bold": bool(c.get("bold")),
                        "font_size": c.get("font_size_halfpt"),
                        "indent_chars": c.get("first_line_indent_chars"),
                        "suggested_level": c.get("suggested_level"),
                    }
                    for c in candidates
                ]
                result = _recover_decisions(items, settings)
                if result:
                    model_decisions = result
            except Exception:
                model_decisions = None

    # ---- ④ 复检 + 栈式建树 ----
    nodes = treebuild.build_tree(candidates, model_decisions)

    # ---- 段落归属：每个非标题段落归属最近一个已出现的标题节点 ----
    node_by_para = {n["para_index"]: n for n in nodes}
    contents: dict[int, list[str]] = {n["para_index"]: [] for n in nodes}
    tables_acc: list[dict] = []
    node_table_counts: dict[int, int] = {}
    current: dict | None = None
    p_i = 0
    for kind, el in kept:
        if kind == "p":
            idx = p_i
            p_i += 1
            node = node_by_para.get(idx)
            if node is not None:  # 标题段落：切换当前章节，正文不含标题自身
                current = node
                continue
            if current is not None:
                contents[current["para_index"]].append(para_features[idx].text.strip())
        else:
            if current is None:
                continue
            grid = tables_mod.extract_grid(el)
            md = tables_mod.to_markdown(grid)
            if not grid["cells"] or not md:
                continue
            cnt = node_table_counts.get(current["para_index"], 0)
            tables_acc.append(
                {
                    "grid": grid,
                    "markdown": md,
                    "node_para_index": current["para_index"],
                    "order_num": cnt,
                    "chapter_path": current["chapter_path"],
                }
            )
            node_table_counts[current["para_index"]] = cnt + 1
            contents[current["para_index"]].append(md)  # 表格渲染进正文，保持阅读位置

    # ---- ⑤ 落库：先父后子，回填 parent_id ----
    id_map: dict[int, int] = {}
    chapters = 0
    needs_review = 0
    for n in nodes:
        parent_ref = n["parent_id"]
        parent_db_id = (
            id_map.get(parent_ref["para_index"], 0) if isinstance(parent_ref, dict) else 0
        )
        content = "\n".join(contents[n["para_index"]])
        confidence = 0.5 if n["needs_review"] else (1.0 if n["source"] == "rule" else 0.7)
        cur = conn.execute(
            "INSERT INTO chapters(doc_id, parent_id, level, number_prefix, title, content,"
            " content_chars, order_num, chapter_path, source, confidence, needs_review)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                doc_id,
                parent_db_id,
                n["level"],
                n["number_prefix"],
                n["title"],
                content,
                len(content),
                n["order_num"],
                n["chapter_path"],
                n["source"],
                confidence,
                n["needs_review"],
            ),
        )
        id_map[n["para_index"]] = int(cur.lastrowid)
        chapters += 1
        if n["needs_review"]:
            needs_review += 1

    for t in tables_acc:
        grid = t["grid"]
        cur = conn.execute(
            "INSERT INTO tables(doc_id, chapter_id, table_caption, total_rows, total_cols,"
            " order_num, chapter_path) VALUES (?,?,?,?,?,?,?)",
            (
                doc_id,
                id_map[t["node_para_index"]],
                None,
                grid["rows"],
                grid["cols"],
                t["order_num"],
                t["chapter_path"],
            ),
        )
        table_id = int(cur.lastrowid)
        for cell in grid["cells"]:
            conn.execute(
                "INSERT INTO table_cells(table_id, row_idx, col_idx, row_span, col_span,"
                " content, is_header) VALUES (?,?,?,?,?,?,?)",
                (
                    table_id,
                    cell["row"],
                    cell["col"],
                    cell["row_span"],
                    cell["col_span"],
                    cell["text"],
                    1 if cell["is_header"] else 0,
                ),
            )
    conn.commit()

    db.fts_index_doc(conn, doc_id)
    total_chars = sum(len(f.text) for f in para_features)
    db.set_status(conn, doc_id, "done", total_chars=total_chars)

    # ---- ⑥ 统计 ----
    return {
        "doc_id": doc_id,
        "total_paragraphs": len(para_features),
        "candidates": len(candidates),
        "anchors": sum(1 for c in candidates if c["is_anchor"]),
        "pending": sum(1 for c in candidates if not c["is_anchor"]),
        "chapters": chapters,
        "needs_review": needs_review,
        "tables": len(tables_acc),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }
