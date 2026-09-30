"""features.py 单测：候选判定/建议层级表驱动用例 + 样式链与段落特征解析。"""
import pytest
from lxml import etree

from doc_analyze.pipeline.features import (
    ParagraphFeatures,
    build_style_map,
    compute_baselines,
    is_candidate,
    paragraph_features,
    split_manual_prefix,
    suggest_level,
)

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
BODY_FONT, BODY_INDENT = 21, 2.0

LONG_BODY = "本项目维护工程范围包括机房设备巡检与线路检修等相关工作内容。" * 2


def F(**kw) -> ParagraphFeatures:
    d = dict(
        para_index=0,
        text="",
        style_id=None,
        style_name=None,
        font_size_halfpt=None,
        bold=False,
        first_line_indent_chars=None,
        outline_lvl=None,
        has_numpr=False,
        num_id=None,
        ilvl=None,
        mixed_run_fmt=False,
    )
    d.update(kw)
    return ParagraphFeatures(**d)


class TestIsCandidate:
    def test_numbered_short_line_no_punct(self):
        # 顶格编号短行（无句读结尾）→ 保留
        f = F(text="项目范围说明", has_numpr=True, num_id=1, ilvl=0,
              first_line_indent_chars=None)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_numbered_long_sentence_with_period(self):
        f = F(text=LONG_BODY + "。", has_numpr=True, num_id=1, ilvl=0,
              first_line_indent_chars=2.0)
        assert len(f.text) > 50
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is False

    def test_manual_numbered_short_line(self):
        f = F(text="1.2 服务要求", first_line_indent_chars=0.0)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_numbered_short_line_trailing_period_big_indent(self):
        f = F(text="主要服务内容如下。", has_numpr=True, num_id=1, ilvl=0,
              first_line_indent_chars=3.0)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is False

    def test_numbered_short_line_trailing_period_normal_indent(self):
        # 缩进与基准相等时该维度无判别力：结尾句读 + 相等缩进 → 交 LLM 裁决，不入候选
        f = F(text="主要服务内容如下。", has_numpr=True, num_id=1, ilvl=0,
              first_line_indent_chars=BODY_INDENT)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is False

    def test_numbered_short_line_trailing_period_smaller_indent(self):
        # 缩进严格小于基准：即使结尾有句读也保留为候选
        f = F(text="主要服务内容如下。", has_numpr=True, num_id=1, ilvl=0,
              first_line_indent_chars=BODY_INDENT - 1.0)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_bold_short_line(self):
        assert is_candidate(F(text="资格审查", bold=True), BODY_FONT, BODY_INDENT) is True

    def test_bold_long_line(self):
        assert is_candidate(F(text=LONG_BODY, bold=True), BODY_FONT, BODY_INDENT) is False

    def test_bold_kv_line_rejected(self):
        # 键值负规则：加粗+带编号的「项目编号：…」信息行不再入候选
        f = F(text="项目编号：XCSSCG2026082001", bold=True, has_numpr=True, num_id=1,
              ilvl=0, font_size_halfpt=BODY_FONT, first_line_indent_chars=BODY_INDENT)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is False

    def test_kv_rule_does_not_block_anchor(self):
        # 键值负规则只约束无属性路径，锚点不受影响
        f = F(text="项目概况：本项目为低碳节能改造工程", outline_lvl=1)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_kv_rule_keeps_colon_ending_lead_line_for_bold_divergence(self):
        # 结尾冒号引导行不被负规则命中；顶格加粗视为缩进差异化，仍为候选
        f = F(text="采购项目简介：", bold=True, font_size_halfpt=BODY_FONT,
              first_line_indent_chars=None)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_bold_same_font_same_indent_rejected(self):
        # 联动收紧：纯加粗、字号与缩进均同正文 → 不入候选
        f = F(text="资格审查条件", bold=True, font_size_halfpt=BODY_FONT,
              first_line_indent_chars=BODY_INDENT)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is False

    def test_bold_same_font_top_indent_kept(self):
        # 同字号但顶格 → 缩进差异化 → 保留（公文三四级标题常见形态）
        f = F(text="资格审查条件", bold=True, font_size_halfpt=BODY_FONT,
              first_line_indent_chars=0.0)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_bold_divergent_font_kept(self):
        f = F(text="资格审查条件", bold=True, font_size_halfpt=32,
              first_line_indent_chars=BODY_INDENT)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_bold_without_baselines_degrades_to_recall(self):
        # 退化文档（基准缺失）：保持召回导向
        assert is_candidate(F(text="资格审查", bold=True), None, None) is True

    def test_mixed_run_fmt_rejected(self):
        # 负规则：段内 run 字号/加粗格式不一的复合信息行（如标签加粗+值不加粗）不入候选
        f = F(text="项目编号 XCSSCG2026082001", bold=True, has_numpr=True, num_id=1,
              ilvl=0, font_size_halfpt=BODY_FONT, first_line_indent_chars=0.0,
              mixed_run_fmt=True)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is False

    def test_mixed_run_fmt_does_not_block_anchor(self):
        # 锚点免疫：带 outlineLvl 的真标题不受 run 格式负规则影响
        f = F(text="十九、基本开户银行情况", outline_lvl=0, mixed_run_fmt=True)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_multi_run_uniform_fmt_kept(self):
        # 多 run 但有效格式统一（中英文/字体拆分、rsid 痕迹）→ 不误伤
        f = F(text="第一章 招标公告", bold=True, font_size_halfpt=32,
              first_line_indent_chars=None, mixed_run_fmt=False)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_numpr_same_indent_as_body_rejected(self):
        # 规则⑤收紧：编号行缩进与正文基准相同（无判别力的正文列表条目）→ 排除
        f = F(text="禁止参加本次采购活动的供应商", has_numpr=True, num_id=1,
              ilvl=0, font_size_halfpt=BODY_FONT, first_line_indent_chars=BODY_INDENT)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is False

    def test_numpr_top_indent_kept(self):
        # 顶格编号短行（无句读结尾）→ 保留，交 LLM 裁决
        f = F(text="禁止参加本次采购活动的供应商", has_numpr=True, num_id=1,
              ilvl=0, font_size_halfpt=BODY_FONT, first_line_indent_chars=None)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_numpr_smaller_indent_with_punct_kept(self):
        # 缩进严格小于基准 + 结尾句读 → 保留（悬挂缩进的标题特征）
        f = F(text="供应商资格要求：", has_numpr=True, num_id=1,
              ilvl=0, font_size_halfpt=BODY_FONT, first_line_indent_chars=BODY_INDENT - 1.0)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_plain_long_body(self):
        f = F(text=LONG_BODY, font_size_halfpt=BODY_FONT, first_line_indent_chars=2.0)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is False

    def test_outline_lvl(self):
        f = F(text=" anything ", outline_lvl=1)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_bigger_font(self):
        f = F(text=LONG_BODY, font_size_halfpt=32, first_line_indent_chars=2.0)
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_empty_text(self):
        assert is_candidate(F(text="  "), BODY_FONT, BODY_INDENT) is False


class TestSuggestLevel:
    def test_outline_plus_one(self):
        assert suggest_level(F(outline_lvl=1)) == 2

    def test_style_tail(self):
        assert suggest_level(F(style_name="Heading 3")) == 3
        assert suggest_level(F(style_id="heading2")) == 2
        assert suggest_level(F(style_name="标题1")) == 1

    def test_agreement(self):
        assert suggest_level(F(outline_lvl=0, style_name="Heading 1")) == 1

    def test_conflict_returns_none(self):
        assert suggest_level(F(outline_lvl=2, style_name="Heading 1")) is None

    def test_none(self):
        assert suggest_level(F(text="加粗待定", bold=True)) is None


class TestSplitManualPrefix:
    @pytest.mark.parametrize(
        "text,prefix,rest",
        [
            ("第二章 项目概况", "第二章", "项目概况"),
            ("1.2 招标范围", "1.2", "招标范围"),
            ("1.1项目概况", "1.1", "项目概况"),
            ("3.4.2保养要求", "3.4.2", "保养要求"),
            ("一、总体要求", "一、", "总体要求"),
            ("（三）资质要求", "（三）", "资质要求"),
            ("(3)业绩证明", "(3)", "业绩证明"),
            ("①基本要求", "①", "基本要求"),
            ("2.技术规范", "2.", "技术规范"),
            ("第三章 项目概况", "第三章", "项目概况"),
        ],
    )
    def test_match(self, text, prefix, rest):
        assert split_manual_prefix(text) == (prefix, rest)

    @pytest.mark.parametrize("text", ["项目概况", "本项目范围如下。", "2026年招标"])
    def test_no_match(self, text):
        assert split_manual_prefix(text) is None


def _p(xml_body: str):
    return etree.fromstring(f"<w:p {W_NS}>{xml_body}</w:p>".encode("utf-8"))


STYLES_XML = f"""<w:styles {W_NS}>
  <w:style w:type="paragraph" w:styleId="Normal">
    <w:name w:val="Normal"/><w:rPr><w:sz w:val="21"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:outlineLvl w:val="0"/><w:ind w:firstLineChars="0"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="32"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="H1Custom">
    <w:name w:val="My Title"/><w:basedOn w:val="Heading1"/>
  </w:style>
</w:styles>"""


class TestParagraphFeatures:
    @pytest.fixture()
    def style_map(self):
        root = etree.fromstring(STYLES_XML.encode("utf-8"))
        return build_style_map(root)

    def test_style_chain_inheritance(self, style_map):
        p = _p('<w:pPr><w:pStyle w:val="H1Custom"/></w:pPr><w:r><w:t>第一章 总则</w:t></w:r>')
        f = paragraph_features(p, 0, style_map)
        assert f.style_id == "H1Custom"
        assert f.style_name == "My Title"
        assert f.font_size_halfpt == 32  # 继承 Heading1
        assert f.bold is True
        assert f.outline_lvl == 0  # 继承 Heading1（0 起始）
        assert suggest_level(f) == 1
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is True

    def test_run_bold_override_false(self, style_map):
        p = _p(
            '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            '<w:r><w:rPr><w:b w:val="0"/></w:rPr><w:t>正文但样式加粗</w:t></w:r>'
        )
        f = paragraph_features(p, 0, style_map)
        assert f.bold is False

    def test_outline_lvl_9_is_body_text(self, style_map):
        # OOXML：outlineLvl val 9 表示正文段落，读取时归一化为 None
        p = _p(
            '<w:pPr><w:outlineLvl w:val="9"/></w:pPr>'
            '<w:r><w:t>普通正文段落内容</w:t></w:r>'
        )
        f = paragraph_features(p, 0, style_map)
        assert f.outline_lvl is None
        assert is_candidate(f, BODY_FONT, BODY_INDENT) is False
        assert suggest_level(f) is None

    def test_run_sz_override(self, style_map):
        p = _p(
            '<w:pPr><w:pStyle w:val="Normal"/></w:pPr>'
            '<w:r><w:rPr><w:sz w:val="44"/></w:rPr><w:t>大字标题</w:t></w:r>'
        )
        f = paragraph_features(p, 0, style_map)
        assert f.font_size_halfpt == 44

    def test_first_line_chars(self, style_map):
        p = _p('<w:pPr><w:ind w:firstLineChars="200"/></w:pPr><w:r><w:t>缩进两字符的正文</w:t></w:r>')
        f = paragraph_features(p, 0, style_map)
        assert f.first_line_indent_chars == 2.0

    def test_first_line_twips_conversion(self, style_map):
        # 420 twips @ 24 半点(12pt)：420 / (12*20) = 1.75 字符
        p = _p(
            '<w:pPr><w:ind w:firstLine="420"/></w:pPr>'
            '<w:r><w:rPr><w:sz w:val="24"/></w:rPr><w:t>twips 缩进正文</w:t></w:r>'
        )
        f = paragraph_features(p, 0, style_map)
        assert f.first_line_indent_chars == pytest.approx(1.75)

    def test_numpr_direct_only(self, style_map):
        p = _p(
            '<w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="3"/></w:numPr></w:pPr>'
            '<w:r><w:t>列表项</w:t></w:r>'
        )
        f = paragraph_features(p, 0, style_map)
        assert (f.has_numpr, f.num_id, f.ilvl) == (True, 3, 1)

    def test_numpr_zero_means_none(self, style_map):
        p = _p(
            '<w:pPr><w:numPr><w:numId w:val="0"/></w:numPr></w:pPr><w:r><w:t>无编号</w:t></w:r>'
        )
        f = paragraph_features(p, 0, style_map)
        assert (f.has_numpr, f.num_id) == (False, None)

    def test_text_extraction(self, style_map):
        p = _p('<w:r><w:t>第一段</w:t></w:r><w:r><w:t>第二段</w:t></w:r>')
        f = paragraph_features(p, 7, style_map)
        assert f.text == "第一段第二段"
        assert f.para_index == 7

    def test_outline_direct_beats_style(self, style_map):
        p = _p(
            '<w:pPr><w:pStyle w:val="Heading1"/><w:outlineLvl w:val="2"/></w:pPr>'
            '<w:r><w:t>标题</w:t></w:r>'
        )
        f = paragraph_features(p, 0, style_map)
        assert f.outline_lvl == 2
        # outline 建议 3 与样式尾数字 1 冲突 → 待定（None）
        assert suggest_level(f) is None


class TestBaselines:
    def test_mode(self):
        feats = [
            F(text="短", font_size_halfpt=32),
            F(text="正文一内容够长够长够长够长", font_size_halfpt=21, first_line_indent_chars=2.0),
            F(text="正文二内容够长够长够长够长", font_size_halfpt=21, first_line_indent_chars=2.0),
            F(text="正文三内容够长够长够长够长", font_size_halfpt=24, first_line_indent_chars=0.0),
        ]
        font, indent = compute_baselines(feats)
        assert font == 21
        assert indent == 2.0

    def test_ignores_short_paragraphs(self):
        feats = [F(text="九个字的正文段落哦", font_size_halfpt=32)]
        assert compute_baselines(feats) == (None, None)

    def test_empty(self):
        assert compute_baselines([]) == (None, None)
