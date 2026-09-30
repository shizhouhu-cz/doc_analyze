"""treebuild.py 单测：降级建树 / 跳级钳制 / 偏离标记 / 模式软标记 / 父子与路径。"""
from doc_analyze.pipeline.treebuild import build_tree, normalize_number_pattern


def C(idx, text, prefix=None, level=None, anchor=False):
    return {
        "para_index": idx,
        "text": text,
        "number_prefix": prefix,
        "title": text,
        "suggested_level": level,
        "is_anchor": anchor,
        "bold": False,
        "font_size_halfpt": None,
        "first_line_indent_chars": None,
    }


class TestNormalizeNumberPattern:
    def test_patterns(self):
        assert normalize_number_pattern("第三章") == "第#章"
        assert normalize_number_pattern("第二节") == "第#节"
        assert normalize_number_pattern("一、") == "#、"
        assert normalize_number_pattern("12、") == "#、"
        assert normalize_number_pattern("3.2") == "#.#"
        assert normalize_number_pattern("3.2.1") == "#.#.#"
        assert normalize_number_pattern("（一）") == "（#）"
        assert normalize_number_pattern("(1)") == "（#）"
        assert normalize_number_pattern("2.") == "#."
        assert normalize_number_pattern("①") == "①"
        assert normalize_number_pattern("⑩") == "①"
        assert normalize_number_pattern(None) == "无"
        assert normalize_number_pattern("") == "无"


class TestDegradedTree:
    def test_stack_parent_and_path(self):
        cands = [
            C(1, "总则", "第一章", 1, True),
            C(2, "范围", "1.1", 2, True),
            C(3, "目标", "1.2", 2, True),
            C(4, "管理", "第二章", 1, True),
        ]
        nodes = build_tree(cands)
        assert len(nodes) == 4
        n1, n2, n3, n4 = nodes
        assert n1["parent_id"] is None
        assert n2["parent_id"] is n1
        assert n3["parent_id"] is n1
        assert n4["parent_id"] is None
        assert n2["chapter_path"] == "第一章 总则 / 1.1 范围"
        assert n3["chapter_path"] == "第一章 总则 / 1.2 目标"
        assert n4["chapter_path"] == "第二章 管理"
        assert [n["order_num"] for n in nodes] == [0, 1, 2, 3]
        assert all(n["source"] == "rule" for n in nodes)
        assert all(n["needs_review"] == 0 for n in nodes)

    def test_deep_nesting(self):
        cands = [
            C(1, "一", "1", 1, True),
            C(2, "一点一", "1.1", 2, True),
            C(3, "一点一点一", "1.1.1", 3, True),
            C(4, "一点二", "1.2", 2, True),
        ]
        nodes = build_tree(cands)
        assert nodes[2]["parent_id"] is nodes[1]
        # 1.2 与 1.1 同级：弹出 1.1.1 与 1.1 后父为根节点 1
        assert nodes[3]["parent_id"] is nodes[0]
        assert nodes[3]["chapter_path"] == "1 一 / 1.2 一点二"

    def test_skip_level_clamped(self):
        cands = [
            C(1, "章", "第一章", 1, True),
            C(2, "骤降", None, 3, True),  # 1 → 3 跳级
        ]
        nodes = build_tree(cands)
        assert nodes[1]["level"] == 2  # 钳制为前节点 level+1
        assert nodes[1]["needs_review"] == 1
        assert nodes[1]["parent_id"] is nodes[0]

    def test_pending_merged_into_anchor_flagged(self):
        cands = [
            C(1, "章标题", "第一章", 1, True),
            C(2, "加粗待定行", None, None, False),  # 非锚点：并入就近锚点
            C(3, "另一章", "第二章", 1, True),
        ]
        nodes = build_tree(cands)
        assert len(nodes) == 2  # 待定项不建树
        assert nodes[0]["needs_review"] == 1  # 就近锚点被标记
        assert nodes[1]["needs_review"] == 0


class TestModelTree:
    def test_confirmed_anchor_stays_rule(self):
        cands = [C(1, "章", "第一章", 1, True)]
        decisions = [{"idx": 1, "is_title": True, "level": 1}]
        nodes = build_tree(cands, decisions)
        assert nodes[0]["source"] == "rule"
        assert nodes[0]["needs_review"] == 0

    def test_deviation_flagged_llm(self):
        cands = [
            C(1, "章", "第一章", 1, True),
            C(2, "节", "1.1", 2, True),
        ]
        decisions = [
            {"idx": 1, "is_title": True, "level": 1},
            {"idx": 2, "is_title": True, "level": 3},  # 偏离建议层级 2 且跳级
        ]
        nodes = build_tree(cands, decisions)
        # 偏离被标记为 llm + needs_review；跳级复检将 3 钳制回 2
        assert nodes[1]["level"] == 2
        assert nodes[1]["needs_review"] == 1
        assert nodes[1]["source"] == "llm"

    def test_model_rejection_excluded(self):
        cands = [
            C(1, "章", "第一章", 1, True),
            C(2, "误入的正文", None, None, False),
        ]
        decisions = [
            {"idx": 1, "is_title": True, "level": 1},
            {"idx": 2, "is_title": False, "level": None},
        ]
        nodes = build_tree(cands, decisions)
        assert len(nodes) == 1
        assert nodes[0]["needs_review"] == 0  # 模型明确否决：不标待定

    def test_model_pending_titled_gets_prev_plus_one(self):
        cands = [
            C(1, "章", "第一章", 1, True),
            C(2, "待定标题", None, None, False),
        ]
        decisions = [
            {"idx": 1, "is_title": True, "level": 1},
            {"idx": 2, "is_title": True, "level": None},
        ]
        nodes = build_tree(cands, decisions)
        assert nodes[1]["level"] == 2  # prev + 1
        assert nodes[1]["needs_review"] == 1
        assert nodes[1]["source"] == "llm"

    def test_pattern_soft_flag_no_rollback(self):
        cands = [
            C(1, "第一项", "1、", 1, True),
            C(2, "第二项", "2、", 2, True),  # 同模式（X、）不同层级
            C(3, "章", "第一章", 1, True),
        ]
        nodes = build_tree(cands)
        # 层级不回滚，仅全部相关节点标 needs_review
        assert [n["level"] for n in nodes[:2]] == [1, 2]
        assert nodes[0]["needs_review"] == 1
        assert nodes[1]["needs_review"] == 1
        assert nodes[2]["needs_review"] == 0  # 第#章 模式只有单节点，不受影响
