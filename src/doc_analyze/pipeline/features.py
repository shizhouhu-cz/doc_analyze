"""段落特征读取与候选标题粗筛（design.md 4.2）。

读取特征：字号（半点）、加粗、首行缩进（字符）、outlineLvl（0 起始）、numPr、样式名/ID。
有效值解析顺序（低→高覆盖）：docDefaults 可忽略 → 段落样式继承链（basedOn，最多 3 级）
→ 段落直接 pPr → 第一个非空 run 的 rPr。
粗筛召回导向（宁多勿漏，目标过滤 90% 以上正文段落），精度由上层结构恢复/复检兜底。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = "{%s}" % W_NS

# 手动编号正则（design 4.2 原文，段首匹配）
MANUAL_NUM_RE = re.compile(
    r"^\s*(第[一二三四五六七八九十百千0-9]+[章节条款部分]"
    r"|[一二三四五六七八九十百千]+、"
    r"|[（(][一二三四五六七八九十百千0-9]+[）)]"
    r"|\d+(?:\.\d+)+(?!\.?\d)"  # 多级数字编号（2.1 / 3.4.2），须先于单级分支
    r"|\d+(?:\.\d+)*[、.．\s（(]"
    r"|[①②③④⑤⑥⑦⑧⑨⑩])"
)

# 标题样式名/ID 模式（不区分大小写）
HEADING_STYLE_RE = re.compile(r"^(heading|标题)\s*(\d+)$", re.IGNORECASE)

# 键值行负规则：冒号后跟 ≥4 字非空白内容（如「项目编号：XCSSCG2026082001」），
# 是封面/前言信息行而非标题；仅约束无属性候选，锚点不受影响。
KEY_VALUE_RE = re.compile(r"[:：]\s*\S{4}")

# 编号短行结尾句读标点（标题编号 vs 列表编号判别）
TRAILING_PUNCT = "。；：，"

_BOLD_FALSE = ("0", "false", "off")
_MAX_STYLE_CHAIN = 3


@dataclass
class StyleInfo:
    """styles.xml 单个样式的有界解析结果。"""

    style_id: str
    name: str | None = None
    based_on: str | None = None
    sz: int | None = None  # 半点
    bold: bool | None = None  # 三态：None=样式未定义
    first_line_chars: float | None = None  # w:firstLineChars（1/100 字符）
    first_line_twips: int | None = None  # w:firstLine（twips）
    outline_lvl: int | None = None  # 0 起始


@dataclass
class ParagraphFeatures:
    """单个 w:p 的轻量格式特征。"""

    para_index: int
    text: str
    style_id: str | None
    style_name: str | None
    font_size_halfpt: int | None
    bold: bool
    first_line_indent_chars: float | None
    outline_lvl: int | None
    has_numpr: bool
    num_id: int | None
    ilvl: int | None
    # 段内含文本 run 的有效 (字号, 加粗) 组合数 >1（未显式设置的 run 继承段落/样式值）。
    # 标题视觉上单一格式，run 层因中英文字体/rsid 拆分出多 run 不影响此判定。
    mixed_run_fmt: bool = False


def _parse_bool_val(val: str | None) -> bool:
    """w:b/w:val 语义：显式 0/false/off 为 False，其余（含缺省）为 True。"""
    if val is None:
        return True
    return val.strip().lower() not in _BOLD_FALSE


def _int_or_none(el, attr: str = W + "val") -> int | None:
    if el is None:
        return None
    try:
        return int(el.get(attr))
    except (TypeError, ValueError):
        return None


def build_style_map(styles_el) -> dict[str, StyleInfo]:
    """解析 styles.xml 根元素 → {styleId: StyleInfo}。"""
    out: dict[str, StyleInfo] = {}
    if styles_el is None:
        return out
    for st in styles_el.findall(W + "style"):
        sid = st.get(W + "styleId")
        if not sid:
            continue
        info = StyleInfo(style_id=sid)
        name_el = st.find(W + "name")
        if name_el is not None:
            info.name = name_el.get(W + "val")
        based_el = st.find(W + "basedOn")
        if based_el is not None:
            info.based_on = based_el.get(W + "val")
        ppr = st.find(W + "pPr")
        if ppr is not None:
            ind = ppr.find(W + "ind")
            if ind is not None:
                flc = ind.get(W + "firstLineChars")
                if flc is not None:
                    try:
                        info.first_line_chars = int(flc) / 100.0
                    except ValueError:
                        pass
                fl = ind.get(W + "firstLine")
                if fl is not None:
                    try:
                        info.first_line_twips = int(fl)
                    except ValueError:
                        pass
            info.outline_lvl = _int_or_none(ppr.find(W + "outlineLvl"))
        rpr = st.find(W + "rPr")
        if rpr is not None:
            info.sz = _int_or_none(rpr.find(W + "sz"))
            b = rpr.find(W + "b")
            if b is not None:
                info.bold = _parse_bool_val(b.get(W + "val"))
        out[sid] = info
    return out


def _style_chain(style_map: dict[str, StyleInfo], style_id: str | None) -> list[StyleInfo]:
    """样式继承链：[自身, 父, 祖]（basedOn，最多 3 级；环与缺失安全）。"""
    chain: list[StyleInfo] = []
    cur = style_id
    while cur and len(chain) < _MAX_STYLE_CHAIN:
        info = style_map.get(cur)
        if info is None:
            break
        chain.append(info)
        cur = info.based_on
    return chain


def _first_nonempty_run(p_el):
    for r in p_el.iter(W + "r"):
        if "".join(t.text or "" for t in r.findall(W + "t")).strip():
            return r
    return None


def _effective_indent(
    info: StyleInfo, font_size_halfpt: int | None
) -> float | None:
    """样式层缩进：优先 firstLineChars，否则 firstLine(twips) 按字号换算字符。"""
    if info.first_line_chars is not None:
        return info.first_line_chars
    if info.first_line_twips is not None and font_size_halfpt:
        return info.first_line_twips / (font_size_halfpt / 2.0 * 20.0)
    return None


def paragraph_features(
    p_el, para_index: int, style_map: dict[str, StyleInfo]
) -> ParagraphFeatures:
    """对单个 w:p 元素提取 ParagraphFeatures。"""
    text = "".join(t.text or "" for t in p_el.iter(W + "t"))
    ppr = p_el.find(W + "pPr")
    style_id = None
    if ppr is not None:
        ps = ppr.find(W + "pStyle")
        if ps is not None:
            style_id = ps.get(W + "val")
    chain = _style_chain(style_map, style_id)
    style_name = chain[0].name if chain else None

    # 字号（半点）：样式链(自身→父→祖，自身覆盖 basedOn) → 段落 pPr/rPr → run rPr
    font_size = None
    for info in chain:
        if info.sz is not None:
            font_size = info.sz
            break
    p_rpr = ppr.find(W + "rPr") if ppr is not None else None
    if p_rpr is not None:
        sz = _int_or_none(p_rpr.find(W + "sz"))
        if sz is not None:
            font_size = sz
    run = _first_nonempty_run(p_el)
    run_rpr = run.find(W + "rPr") if run is not None else None
    if run_rpr is not None:
        sz = _int_or_none(run_rpr.find(W + "sz"))
        if sz is not None:
            font_size = sz

    # 加粗（三态继承，缺省 False）
    bold_val: bool | None = None
    for info in chain:
        if info.bold is not None:
            bold_val = info.bold
            break
    if p_rpr is not None:
        b = p_rpr.find(W + "b")
        if b is not None:
            bold_val = _parse_bool_val(b.get(W + "val"))
    if run_rpr is not None:
        b = run_rpr.find(W + "b")
        if b is not None:
            bold_val = _parse_bool_val(b.get(W + "val"))
    bold = bool(bold_val)

    # 段内 run 有效格式一致性：run 未显式设置的属性回落段落/样式有效值，避免
    # 「显式 vs 继承到同值」的假阳性；仅统计含文本 run。
    sz_set: set[int | None] = set()
    b_set: set[bool] = set()
    for r in p_el.iter(W + "r"):
        if not "".join(t.text or "" for t in r.findall(W + "t")).strip():
            continue
        r_rpr = r.find(W + "rPr")
        rsz = _int_or_none(r_rpr.find(W + "sz")) if r_rpr is not None else None
        rb = None
        if r_rpr is not None:
            be = r_rpr.find(W + "b")
            if be is not None:
                rb = _parse_bool_val(be.get(W + "val"))
        sz_set.add(rsz if rsz is not None else font_size)
        b_set.add(rb if rb is not None else bold)
    mixed_run_fmt = len(sz_set) > 1 or len(b_set) > 1

    # 首行缩进（字符）：样式链 → 段落直接 pPr/w:ind
    indent = None
    for info in chain:
        v = _effective_indent(info, font_size)
        if v is not None:
            indent = v
            break
    if ppr is not None:
        ind = ppr.find(W + "ind")
        if ind is not None:
            flc = ind.get(W + "firstLineChars")
            if flc is not None:
                try:
                    indent = int(flc) / 100.0
                except ValueError:
                    indent = None
            else:
                fl = ind.get(W + "firstLine")
                if fl is not None and font_size:
                    try:
                        indent = int(fl) / (font_size / 2.0 * 20.0)
                    except ValueError:
                        indent = None

    # outlineLvl：段落直接 pPr → 样式定义 → basedOn 链（有界，0 起始）
    outline = _int_or_none(ppr.find(W + "outlineLvl")) if ppr is not None else None
    if outline is None:
        for info in chain:  # 自身优先于 basedOn
            if info.outline_lvl is not None:
                outline = info.outline_lvl
                break
    if outline is not None and outline >= 9:
        outline = None  # OOXML 约定：val 9 表示正文段落，非标题层级

    # numPr：仅读段落直接 pPr；numId=0 视为无
    has_numpr, num_id, ilvl = False, None, None
    if ppr is not None:
        npr = ppr.find(W + "numPr")
        if npr is not None:
            nid = _int_or_none(npr.find(W + "numId"))
            if nid:  # 非 0 且非 None
                has_numpr = True
                num_id = nid
                ilvl = _int_or_none(npr.find(W + "ilvl"))
                if ilvl is None:
                    ilvl = 0

    return ParagraphFeatures(
        para_index=para_index,
        text=text,
        style_id=style_id,
        style_name=style_name,
        font_size_halfpt=font_size,
        bold=bold,
        first_line_indent_chars=indent,
        outline_lvl=outline,
        has_numpr=has_numpr,
        num_id=num_id,
        ilvl=ilvl,
        mixed_run_fmt=mixed_run_fmt,
    )


def compute_baselines(
    features_list: list[ParagraphFeatures],
) -> tuple[int | None, float | None]:
    """全文基准：len(text)>=10 段落的字号半点众数、首行缩进众数。"""
    fonts = Counter(
        f.font_size_halfpt
        for f in features_list
        if len(f.text) >= 10 and f.font_size_halfpt is not None
    )
    inds = Counter(
        round(f.first_line_indent_chars, 2)
        for f in features_list
        if len(f.text) >= 10 and f.first_line_indent_chars is not None
    )
    baseline_font = fonts.most_common(1)[0][0] if fonts else None
    baseline_indent = inds.most_common(1)[0][0] if inds else None
    return baseline_font, baseline_indent


def style_heading_level(features: ParagraphFeatures) -> int | None:
    """样式名/ID 尾数字（heading/标题 + 数字）；不匹配返回 None。"""
    for candidate in (features.style_name, features.style_id):
        if candidate:
            m = HEADING_STYLE_RE.match(candidate.strip())
            if m:
                return int(m.group(2))
    return None


def suggest_level(features: ParagraphFeatures) -> int | None:
    """建议层级：outline_lvl+1 与样式尾数字；两者冲突 → None（待定）。"""
    lvl_outline = features.outline_lvl + 1 if features.outline_lvl is not None else None
    lvl_style = style_heading_level(features)
    if lvl_outline is not None and lvl_style is not None:
        return lvl_outline if lvl_outline == lvl_style else None
    return lvl_outline if lvl_outline is not None else lvl_style


def _diverges_from_body(
    features: ParagraphFeatures,
    baseline_font: int | None,
    baseline_indent: float | None,
) -> bool:
    """加粗候选须至少一项与正文差异化：字号 ≠ 基准，或缩进 ≠ 基准（顶格/未设置视为差异）。

    两个基准都缺失（退化文档）时返回 True，保持召回导向。
    """
    if baseline_font is None and baseline_indent is None:
        return True
    font_diff = (
        baseline_font is not None
        and features.font_size_halfpt is not None
        and features.font_size_halfpt != baseline_font
    )
    indent_diff = features.first_line_indent_chars is None or (
        baseline_indent is not None
        and features.first_line_indent_chars != baseline_indent
    )
    return font_diff or indent_diff


def is_candidate(
    features: ParagraphFeatures,
    baseline_font: int | None,
    baseline_indent: float | None,
) -> bool:
    """候选标题粗筛（design 4.2，满足任一即候选；键值行负规则除外）。"""
    text = features.text
    if not text or not text.strip():
        return False
    # ① outlineLvl 存在
    if features.outline_lvl is not None:
        return True
    # ② 样式名/ID 匹配 Heading/标题类
    if style_heading_level(features) is not None:
        return True
    # 负规则：键值行（冒号后跟内容），仅约束以下无属性路径
    if KEY_VALUE_RE.search(text):
        return False
    # 负规则：段内 run 有效格式不一（标题为单一字号/加粗；混杂是键值行等复合信息行特征）
    if features.mixed_run_fmt:
        return False
    # ③ 有效加粗且段长 <= 50 字，且字号/缩进至少一项与正文差异化
    #    （联动收紧：纯加粗、字号缩进均同正文的信息行如「项目编号：…」不再入候选）
    if (
        features.bold
        and len(text) <= 50
        and _diverges_from_body(features, baseline_font, baseline_indent)
    ):
        return True
    # ④ 字号 > 全文众数字号
    if (
        baseline_font is not None
        and features.font_size_halfpt is not None
        and features.font_size_halfpt > baseline_font
    ):
        return True
    # ⑤ 编号 + 短行组合。缩进与正文基准相同（首行缩进 2 字符）时该维度无判别力，
    #    且正文列表条目（如「(五) 禁止参加本次采购活动的供应商」）正是此形态——
    #    直接排除；仅保留顶格/更小缩进的编号行（标题相更足，余下交 LLM 裁决）。
    if (features.has_numpr or MANUAL_NUM_RE.match(text)) and len(text) <= 50:
        if (
            baseline_indent is not None
            and features.first_line_indent_chars == baseline_indent
        ):
            return False
        last = text.rstrip()[-1:]
        if last not in TRAILING_PUNCT:
            return True
        if (
            baseline_indent is not None
            and features.first_line_indent_chars is not None
            and features.first_line_indent_chars < baseline_indent
        ):
            return True
    return False


def split_manual_prefix(text: str) -> tuple[str, str] | None:
    """段首手动编号 → (编号前缀, 去除前缀后的剩余文本)；无编号返回 None。"""
    m = MANUAL_NUM_RE.match(text)
    if not m:
        return None
    return m.group(1).strip(), text[m.end():].strip()
