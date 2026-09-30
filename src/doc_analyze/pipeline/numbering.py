"""瘦版编号渲染（design.md 4.3）。

只渲染编号文本，不做树构建/层级映射/一致性校验。
状态机：按文档顺序 render(numId, ilvl)，维护 9 级计数器，高级别递增时低级别重置。
numFmt 转换器全部为纯函数。
"""
from __future__ import annotations

from typing import NamedTuple

from doc_analyze.pipeline.features import W

# 中文数字（chineseCountingThousand / ideographDigital）
_CN_DIGITS = "一二三四五六七八九"
# 中文大写（chineseLegalSimplified）
_LEGAL_DIGITS = "壹贰叁肆伍陆柒捌玖"
# 天干（ideographTraditional）
_STEMS = "甲乙丙丁戊己庚辛壬癸"

_ROMAN = (
    (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"),
    (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"),
    (5, "v"), (4, "iv"), (1, "i"),
)


def fmt_decimal(n: int) -> str:
    return str(n)


def _cn_u999(n: int) -> str:
    """1..999 → 一…九百九十九（一百零五 / 一百一十；10-19 拾位一省略）。"""
    if n < 10:
        return _CN_DIGITS[n - 1]
    h, r = divmod(n, 100)
    out = _CN_DIGITS[h - 1] + "百" if h else ""
    if r:
        if h and r < 10:
            out += "零"
        t, o = divmod(r, 10)
        if t:
            out += ("" if (t == 1 and not h) else _CN_DIGITS[t - 1]) + "十"
        if o:
            out += _CN_DIGITS[o - 1]
    return out


def _legal_u999(n: int) -> str:
    """1..999 → 壹…玖佰玖拾玖（拾前壹省略：10→拾、12→拾贰）。"""
    if n < 10:
        return _LEGAL_DIGITS[n - 1]
    h, r = divmod(n, 100)
    out = _LEGAL_DIGITS[h - 1] + "佰" if h else ""
    if r:
        if h and r < 10:
            out += "零"
        t, o = divmod(r, 10)
        if t:
            out += ("" if (t == 1 and not h) else _LEGAL_DIGITS[t - 1]) + "拾"
        if o:
            out += _LEGAL_DIGITS[o - 1]
    return out


def _with_thousand(
    n: int, u999_fn, digits: str, thousand: str
) -> str:
    """千位拼装：q千 + （零）+ u999；超出 1..9999 回退 str(n)。"""
    if n < 1 or n > 9999:
        return str(n)
    q, r = divmod(n, 1000)
    out = digits[q - 1] + thousand if q else ""
    if r:
        if q and r < 100:
            out += "零"
        out += u999_fn(r)
    return out


def fmt_chinese_counting(n: int) -> str:
    """chineseCountingThousand：1→一、10→十、12→十二、20→二十、100→一百、110→一百一十、999→九百九十九。"""
    return _with_thousand(n, _cn_u999, _CN_DIGITS, "千")


def fmt_chinese_legal(n: int) -> str:
    """chineseLegalSimplified：1→壹、10→拾、12→拾贰、110→壹佰壹拾。"""
    return _with_thousand(n, _legal_u999, _LEGAL_DIGITS, "仟")


def fmt_ideograph_digital(n: int) -> str:
    """ideographDigital：一二三…顺序计数。"""
    return fmt_chinese_counting(n)


def fmt_ideograph_traditional(n: int) -> str:
    """ideographTraditional：甲乙丙…癸 循环。"""
    if n < 1:
        return str(n)
    return _STEMS[(n - 1) % 10]


def _letters(n: int, base_ord: int) -> str:
    """双射 26 进制：1→a、26→z、27→aa。"""
    if n < 1:
        return str(n)
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(base_ord + r) + out
    return out


def fmt_lower_letter(n: int) -> str:
    return _letters(n, ord("a"))


def fmt_upper_letter(n: int) -> str:
    return _letters(n, ord("A"))


def _roman(n: int) -> str:
    if n < 1:
        return str(n)
    out = []
    for v, sym in _ROMAN:
        c, n = divmod(n, v)
        out.append(sym * c)
    return "".join(out)


def fmt_lower_roman(n: int) -> str:
    """lowerRoman：4→iv、9→ix。"""
    return _roman(n)


def fmt_upper_roman(n: int) -> str:
    return _roman(n).upper()


def fmt_enclosed_circle(n: int) -> str:
    """decimalEnclosedCircle：1→① … 10→⑩；圆圈外溢回退 "(11)"。"""
    if 1 <= n <= 10:
        return chr(0x2460 + n - 1)
    return f"({n})"


def fmt_bullet(n: int) -> str | None:
    """bullet 不参与编号文本。"""
    return None


NUM_FMT_CONVERTERS = {
    "decimal": fmt_decimal,
    "chineseCountingThousand": fmt_chinese_counting,
    "chineseLegalSimplified": fmt_chinese_legal,
    "ideographDigital": fmt_ideograph_digital,
    "ideographTraditional": fmt_ideograph_traditional,
    "lowerLetter": fmt_lower_letter,
    "upperLetter": fmt_upper_letter,
    "lowerRoman": fmt_lower_roman,
    "upperRoman": fmt_upper_roman,
    "decimalEnclosedCircle": fmt_enclosed_circle,
    "bullet": fmt_bullet,
}


class _Lvl(NamedTuple):
    num_fmt: str
    lvl_text: str
    start: int
    lvl_restart: int | None


class NumberingRenderer:
    """numbering.xml 状态机渲染器：numId → abstractNumId → lvl(ilvl 0~8)。

    构造时一次性解析；render 按文档顺序调用以维护计数器。
    """

    def __init__(self, numbering_xml: bytes | bytearray | None = None):
        self._num_map: dict[int, tuple[int, dict[int, int]]] = {}
        self._abstract_map: dict[int, dict[int, _Lvl]] = {}
        self._pstyle_abstract: dict[str, tuple[int, int]] = {}
        # {styleId: (numId, ilvl)}，来自 abstractNum/lvl/pStyle
        self.pstyle_map: dict[str, tuple[int, int]] = {}
        self._counters: dict[tuple[int, int], int] = {}
        if numbering_xml:
            self._parse(numbering_xml)

    @classmethod
    def from_document(cls, document) -> "NumberingRenderer":
        """python-docx Document → 渲染器；无 numbering part 视为无编号文档。"""
        try:
            part = document.part.numbering_part
            xml = part.blob
        except Exception:
            xml = None
        return cls(xml)

    # ---- 解析 ----
    def _parse(self, xml: bytes | bytearray) -> None:
        from lxml import etree

        root = etree.fromstring(bytes(xml))
        for num in root.findall(W + "num"):
            try:
                num_id = int(num.get(W + "numId"))
            except (TypeError, ValueError):
                continue
            abs_el = num.find(W + "abstractNumId")
            if abs_el is None:
                continue
            try:
                abstract_id = int(abs_el.get(W + "val"))
            except (TypeError, ValueError):
                continue
            overrides: dict[int, int] = {}
            for lo in num.findall(W + "lvlOverride"):
                try:
                    oil = int(lo.get(W + "ilvl"))
                except (TypeError, ValueError):
                    continue
                so = lo.find(W + "startOverride")
                if so is not None:
                    try:
                        overrides[oil] = int(so.get(W + "val"))
                    except (TypeError, ValueError):
                        pass
            self._num_map[num_id] = (abstract_id, overrides)

        for an in root.findall(W + "abstractNum"):
            try:
                aid = int(an.get(W + "abstractNumId"))
            except (TypeError, ValueError):
                continue
            lvls: dict[int, _Lvl] = {}
            for lvl in an.findall(W + "lvl"):
                try:
                    ilvl = int(lvl.get(W + "ilvl"))
                except (TypeError, ValueError):
                    continue
                fmt_el = lvl.find(W + "numFmt")
                text_el = lvl.find(W + "lvlText")
                start_el = lvl.find(W + "start")
                restart_el = lvl.find(W + "lvlRestart")
                fmt = fmt_el.get(W + "val") if fmt_el is not None else "decimal"
                text = text_el.get(W + "val") if text_el is not None else ""
                start = 1
                if start_el is not None:
                    try:
                        start = int(start_el.get(W + "val"))
                    except (TypeError, ValueError):
                        pass
                restart = None
                if restart_el is not None:
                    try:
                        restart = int(restart_el.get(W + "val"))
                    except (TypeError, ValueError):
                        pass
                lvls[ilvl] = _Lvl(fmt, text or "", start, restart)
                ps = lvl.find(W + "pStyle")
                if ps is not None and ps.get(W + "val"):
                    self._pstyle_abstract[ps.get(W + "val")] = (aid, ilvl)
            self._abstract_map[aid] = lvls

        # abstractNumId → 首个引用它的 numId（pstyle_map 落地）
        abs_to_num: dict[int, int] = {}
        for num_id, (aid, _) in self._num_map.items():
            abs_to_num.setdefault(aid, num_id)
        for style_id, (aid, ilvl) in self._pstyle_abstract.items():
            num_id = abs_to_num.get(aid)
            if num_id is not None:
                self.pstyle_map[style_id] = (num_id, ilvl)

    # ---- 渲染 ----
    def render(self, num_id: int | None, ilvl: int | None) -> str | None:
        """渲染当前编号文本（如「第二章」「2.1」）；无法渲染返回 None。"""
        if not num_id or num_id <= 0:  # numId=0 视为无编号
            return None
        if ilvl is None:
            ilvl = 0
        entry = self._num_map.get(num_id)
        if entry is None:
            return None
        abstract_id, overrides = entry
        lvls = self._abstract_map.get(abstract_id, {})
        lvl = lvls.get(ilvl)
        if lvl is None:
            return None
        conv = NUM_FMT_CONVERTERS.get(lvl.num_fmt)
        if conv is None or lvl.num_fmt == "bullet":
            return None

        # 递增当前层级（无值取 start，startOverride 优先）
        eff_start = overrides.get(ilvl)
        if eff_start is None:
            eff_start = lvl.start
        cur = self._counters.get((num_id, ilvl))
        self._counters[(num_id, ilvl)] = eff_start if cur is None else cur + 1
        # 更高级别计数器重置（下次渲染时取 start）
        for l2 in range(ilvl + 1, 9):
            self._counters.pop((num_id, l2), None)
        # lvlRestart：被指定层级递增时重置对应层级（简单实现）
        for l2, info in lvls.items():
            if l2 != ilvl and info.lvl_restart == ilvl:
                self._counters.pop((num_id, l2), None)

        # lvlText 模板 %1..%9 → 各层格式化值（占位层无计数时取其 start）
        out = lvl.lvl_text
        for k in range(1, 10):
            ph = f"%{k}"
            if ph not in out:
                continue
            l0 = k - 1
            v = self._counters.get((num_id, l0))
            if v is None:
                so = overrides.get(l0)
                if so is not None:
                    v = so
                else:
                    info = lvls.get(l0)
                    v = info.start if info else 1
            linfo = lvls.get(l0)
            lconv = (
                NUM_FMT_CONVERTERS.get(linfo.num_fmt, fmt_decimal)
                if linfo
                else fmt_decimal
            )
            fv = lconv(v)
            out = out.replace(ph, fv if fv is not None else "")
        return out

    def reset(self) -> None:
        """清空计数器（按文档切分时使用）。"""
        self._counters.clear()
