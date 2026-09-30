"""复检与栈式建树（design.md 4.4）。

输入：粗筛候选（文档顺序单一序列）+ 可选 model_decisions（LLM 全局恢复输出）。
流程：定级（模型优先，降级用建议层级）→ 复检（偏离/跳级/同模式软校验）→ 栈式拼树。
乱序在结构上不可能发生：序列即文档顺序，建树为确定性栈操作。
"""
from __future__ import annotations

import re

_CIRCLED = frozenset("①②③④⑤⑥⑦⑧⑨⑩")
_UNIT_RE = re.compile(r"^第.+?(部分|[章节条款])$")
_BRACKET_RE = re.compile(r"^[（(].+[）)]$")
_MULTI_DOT_RE = re.compile(r"^\d+(?:\.\d+)+$")
_DOT_RE = re.compile(r"^\d+\.$")


def normalize_number_pattern(prefix: str | None) -> str:
    """编号前缀 → 模式类：第X章 / X、 / X.Y / （X） / X. / ① / 无。

    未知形状以前缀本身为模式（相同前缀仍可成组）。
    """
    if not prefix:
        return "无"
    p = prefix.strip()
    if not p:
        return "无"
    m = _UNIT_RE.match(p)
    if m:
        return f"第#{m.group(1)}"
    if _BRACKET_RE.match(p):
        return "（#）"
    if _MULTI_DOT_RE.match(p):
        return ".".join("#" for _ in p.split("."))
    if _DOT_RE.match(p):
        return "#."
    if p.endswith("、"):
        return "#、"
    if p in _CIRCLED:
        return "①"
    return p


def build_tree(candidates: list[dict], model_decisions: list[dict] | None = None) -> list[dict]:
    """候选列表 → 章节节点列表（文档顺序，先父后子）。

    降级路径（无 model_decisions）：锚点用建议层级建树；非锚点（待定项）并入
    就近锚点（由段落归属自然完成）并将该锚点标 needs_review=1，不建树。
    模型路径：用 model_decisions 的 is_title/level；偏离建议层级的节点标
    needs_review=1、source='llm'。
    """
    decisions: dict[int, tuple[bool, int | None]] = {}
    if model_decisions:
        for d in model_decisions:
            if not isinstance(d, dict):
                continue
            try:
                idx = int(d.get("idx"))
            except (TypeError, ValueError):
                continue
            lvl = d.get("level")
            decisions[idx] = (bool(d.get("is_title")), int(lvl) if isinstance(lvl, int) else None)

    nodes: list[dict] = []
    excluded: list[dict] = []  # 未成为节点的候选（待定项 / 模型否决项）

    for c in candidates:
        node = {
            "para_index": c["para_index"],
            "level": None,
            "number_prefix": c.get("number_prefix"),
            "title": c.get("title") or (c.get("text") or "").strip(),
            "parent_id": None,
            "order_num": 0,
            "chapter_path": "",
            "source": "rule",
            "needs_review": 0,
            "is_anchor": bool(c.get("is_anchor")),
        }
        idx = c["para_index"]
        is_anchor = bool(c.get("is_anchor"))
        sug = c.get("suggested_level")

        if idx in decisions:
            is_title, lvl = decisions[idx]
            if not is_title:
                excluded.append(c)  # 模型否决：不建树，按正文归属
                continue
            deviated = is_anchor and sug is not None and lvl is not None and lvl != sug
            node["level"] = lvl
            node["needs_review"] = 1 if (deviated or lvl is None) else 0
            # 模型纠偏或待定并入的节点标 llm（design 5.2）
            node["source"] = "llm" if (deviated or not is_anchor or lvl is None) else "rule"
            nodes.append(node)
        elif is_anchor and sug is not None:
            node["level"] = sug
            nodes.append(node)
        else:
            excluded.append(c)  # 待定项

    # ---- 复检：空 level 就近挂接；跳级钳制；无模型时待定项并入就近锚点 ----
    prev: dict | None = None
    for n in nodes:
        if n["level"] is None:
            n["level"] = (prev["level"] + 1) if prev else 1
            n["needs_review"] = 1
            n["source"] = "llm"
        else:
            if n["level"] < 1:
                n["level"] = 1
            if prev is not None and n["level"] > prev["level"] + 1:
                n["level"] = prev["level"] + 1  # 钳制为前节点 level+1
                n["needs_review"] = 1
        prev = n

    if not decisions:
        for c in excluded:
            for n in reversed(nodes):
                if n["para_index"] < c["para_index"]:
                    n["needs_review"] = 1
                    break

    # ---- 同模式软校验：同模式不同 level → 全部相关节点标 needs_review（不回滚）----
    groups: dict[str, list[dict]] = {}
    for n in nodes:
        groups.setdefault(normalize_number_pattern(n["number_prefix"]), []).append(n)
    for group in groups.values():
        if len(group) > 1 and len({n["level"] for n in group}) > 1:
            for n in group:
                n["needs_review"] = 1

    # ---- 栈式建树：弹出栈直到栈顶 level < 当前 level ----
    stack: list[dict] = []
    for i, n in enumerate(nodes):
        while stack and stack[-1]["level"] >= n["level"]:
            stack.pop()
        parent = stack[-1] if stack else None
        n["parent_id"] = parent  # 暂为栈内引用，落库时填真 id
        n["order_num"] = i
        stack.append(n)

    def _path(n: dict) -> str:
        comp = ((n["number_prefix"] + " ") if n.get("number_prefix") else "") + (n["title"] or "")
        if n["parent_id"]:
            return _path(n["parent_id"]) + " / " + comp
        return comp

    for n in nodes:
        n["chapter_path"] = _path(n)
    return nodes
