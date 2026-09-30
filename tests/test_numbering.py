"""numbering.py 单测：numFmt 转换器逐一验证 + 状态机（计数器/重置/Override）。"""
import pytest

from doc_analyze.pipeline.numbering import (
    NumberingRenderer,
    fmt_bullet,
    fmt_chinese_counting,
    fmt_chinese_legal,
    fmt_decimal,
    fmt_enclosed_circle,
    fmt_ideograph_digital,
    fmt_ideograph_traditional,
    fmt_lower_letter,
    fmt_lower_roman,
    fmt_upper_letter,
    fmt_upper_roman,
)

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

TWO_LEVEL_XML = f"""<w:numbering {W_NS}>
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>
    <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>"""

CHINESE_XML = f"""<w:numbering {W_NS}>
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="chineseCountingThousand"/>
      <w:lvlText w:val="第%1章"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>"""

OVERRIDE_XML = f"""<w:numbering {W_NS}>
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1"/></w:lvl>
    <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/>
    <w:lvlOverride w:ilvl="0"><w:startOverride w:val="3"/></w:lvlOverride></w:num>
  <w:num w:numId="2"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>"""

PSTYLE_XML = f"""<w:numbering {W_NS}>
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1"/>
      <w:pStyle w:val="Heading1"/></w:lvl>
    <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/>
      <w:pStyle w:val="Heading2"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="7"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>"""

BULLET_XML = f"""<w:numbering {W_NS}>
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>"""


class TestNumFmtConverters:
    @pytest.mark.parametrize(
        "n,expected", [(1, "1"), (9, "9"), (10, "10"), (42, "42")]
    )
    def test_decimal(self, n, expected):
        assert fmt_decimal(n) == expected

    @pytest.mark.parametrize(
        "n,expected",
        [
            (1, "一"),
            (9, "九"),
            (10, "十"),
            (12, "十二"),
            (20, "二十"),
            (48, "四十八"),
            (100, "一百"),
            (105, "一百零五"),
            (110, "一百一十"),
            (120, "一百二十"),
            (999, "九百九十九"),
            (1000, "一千"),
            (1100, "一千一百"),
        ],
    )
    def test_chinese_counting(self, n, expected):
        assert fmt_chinese_counting(n) == expected

    @pytest.mark.parametrize(
        "n,expected",
        [(1, "壹"), (10, "拾"), (12, "拾贰"), (110, "壹佰壹拾"), (999, "玖佰玖拾玖")],
    )
    def test_chinese_legal(self, n, expected):
        assert fmt_chinese_legal(n) == expected

    @pytest.mark.parametrize("n,expected", [(1, "一"), (10, "十"), (12, "十二"), (25, "二十五")])
    def test_ideograph_digital(self, n, expected):
        assert fmt_ideograph_digital(n) == expected

    @pytest.mark.parametrize("n,expected", [(1, "甲"), (10, "癸"), (11, "甲"), (23, "丙")])
    def test_ideograph_traditional(self, n, expected):
        assert fmt_ideograph_traditional(n) == expected

    @pytest.mark.parametrize("n,expected", [(1, "a"), (26, "z"), (27, "aa"), (53, "ba")])
    def test_lower_letter(self, n, expected):
        assert fmt_lower_letter(n) == expected

    @pytest.mark.parametrize("n,expected", [(1, "A"), (26, "Z"), (27, "AA")])
    def test_upper_letter(self, n, expected):
        assert fmt_upper_letter(n) == expected

    @pytest.mark.parametrize("n,expected", [(4, "iv"), (9, "ix"), (14, "xiv"), (40, "xl"), (1994, "mcmxciv")])
    def test_lower_roman(self, n, expected):
        assert fmt_lower_roman(n) == expected

    @pytest.mark.parametrize("n,expected", [(4, "IV"), (9, "IX"), (21, "XXI")])
    def test_upper_roman(self, n, expected):
        assert fmt_upper_roman(n) == expected

    @pytest.mark.parametrize("n,expected", [(1, "①"), (9, "⑨"), (10, "⑩"), (11, "(11)"), (12, "(12)")])
    def test_enclosed_circle(self, n, expected):
        assert fmt_enclosed_circle(n) == expected

    def test_bullet(self):
        assert fmt_bullet(1) is None


class TestNumberingRenderer:
    def test_two_level_state_machine(self):
        r = NumberingRenderer(TWO_LEVEL_XML.encode("utf-8"))
        assert r.render(1, 0) == "1."
        assert r.render(1, 1) == "1.1"
        assert r.render(1, 1) == "1.2"
        assert r.render(1, 1) == "1.3"
        assert r.render(1, 0) == "2."  # 一级递增后，二级重置
        assert r.render(1, 1) == "2.1"

    def test_chinese_lvltext(self):
        r = NumberingRenderer(CHINESE_XML.encode("utf-8"))
        assert r.render(1, 0) == "第一章"
        assert r.render(1, 0) == "第二章"
        for i in range(3, 10):
            r.render(1, 0)
        assert r.render(1, 0) == "第十章"

    def test_start_override(self):
        r = NumberingRenderer(OVERRIDE_XML.encode("utf-8"))
        assert r.render(1, 0) == "3"  # startOverride=3
        assert r.render(1, 1) == "3.1"
        assert r.render(2, 0) == "1"  # numId=2 无 Override

    def test_numid_zero_returns_none(self):
        r = NumberingRenderer(TWO_LEVEL_XML.encode("utf-8"))
        assert r.render(0, 0) is None
        assert r.render(None, 0) is None

    def test_unknown_numid_returns_none(self):
        r = NumberingRenderer(TWO_LEVEL_XML.encode("utf-8"))
        assert r.render(99, 0) is None

    def test_unknown_ilvl_returns_none(self):
        r = NumberingRenderer(TWO_LEVEL_XML.encode("utf-8"))
        assert r.render(1, 5) is None

    def test_bullet_returns_none(self):
        r = NumberingRenderer(BULLET_XML.encode("utf-8"))
        assert r.render(1, 0) is None

    def test_empty_renderer(self):
        r = NumberingRenderer(None)
        assert r.render(1, 0) is None
        assert r.pstyle_map == {}

    def test_pstyle_map(self):
        r = NumberingRenderer(PSTYLE_XML.encode("utf-8"))
        assert r.pstyle_map["Heading1"] == (7, 0)
        assert r.pstyle_map["Heading2"] == (7, 1)
