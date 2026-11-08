"""规则基线归并（任务 19）：金标准自由文本 → 结构化标签。

**这一层是整条主干的确定性地基**，两条用途：

1. **模型不可达时接管归并**（任务 24：``normalize_source=rule`` + ``needs_review``）；
2. **与语义归并做对照**（任务 25：规则基线 vs 模型归并在同一批标注集上的
   归并准确率 / 召回对照表）—— 没有这条基线，「模型比规则强」这句话没有证据。

所以规则基线**不是临时替身**：它必须自己跑出**和语义归并同一份**结论
（:class:`~nodule_eval.goldstd.schema.GoldLabels`），两边才能直接比对。

====================  ==========================================================
层                    落点
====================  ==========================================================
存在性                分句 + **否定作用域**（任务 20）：否定只管它自己那句
部位 / 密度           走 :mod:`nodule_eval.goldstd.labels` 的等价表（唯一真源）
尺寸                  尺寸正则 + 量纲换算，统一成毫米
多病灶                逐处病灶标签集合 ``GoldLabels.lesions``（任务 20）
====================  ==========================================================

**三条红线**（都写在代码里，不靠约定）：

- **读不出来就说读不出来**：部位、密度、尺寸认不出来一律留 ``None``，
  绝不猜一个近似标签；认不出的**原词**记进
  :class:`RuleDiagnostics`（``unknown_*_terms``），让人看得见「这里没读出来」；
- **原文零改写**：本模块只读原文（优先读读取层产出的**分析用副本**
  ``gold_standard_normalized``），一个字都不往回写；
- **失败不当阴性**：判不出存在性就交回 ``presence=None``，由判定层按
  「没拿到判定」挂人工复核 —— 宁可少一例进统计，也不用一个猜出来的
  存在性去污染敏感度。

**本层不做什么**（留给后续任务，别在这里抢）：

- 语义归并与 ``evidence_span`` 回指校验 —— 任务 21；
- 与语义归并的对照实验 —— 任务 25。

**任务 20 已落**：否定**作用域**（「未见异常，但见结节」该判阳性，
「未见明显结节及钙化灶」的密度不能算成钙化）、**多病灶**的多标签集合
（``GoldLabels.lesions``）、**跨行拼接**（上一行的「未见」不外溢到下一行）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Sequence

from .labels import (
    DensityMatch,
    LabelTable,
    LocationMatch,
    find_densities,
    find_density,
    find_location,
    find_locations,
)
from .schema import (
    AFFIRMATIVE_MARKERS,
    NEGATION_MARKERS,
    NORMALIZE_SOURCE_RULE,
    GoldLabels,
    LesionLabels,
)

__all__ = [
    "ClauseSpan",
    "FindingHit",
    "MarkerHit",
    "RuleDiagnostics",
    "RuleMerge",
    "SizeMatch",
    "convert_to_mm",
    "find_affirmatives",
    "find_negations",
    "find_sizes",
    "merge_by_rules",
    "normalize_by_rules",
    "parse_size",
    "split_clauses",
]


# --------------------------------------------------------------------------
# 尺寸：正则 + 量纲换算
# --------------------------------------------------------------------------

#: 单位别名表 —— 中文写法与英文写法指向**同一个量纲**。
#:
#: 「公分」「厘米」是国内报告里常见的 cm 写法，「毫米」是 mm 的中文写法；
#: 认得出来就换算，认不出来就记成未知（不猜）。
UNIT_ALIASES: dict[str, str] = {
    "mm": "mm",
    "毫米": "mm",
    "cm": "cm",
    "厘米": "cm",
    "公分": "cm",
}

#: cm → mm 的换算系数。
CM_TO_MM = Decimal("10")

#: 尺寸写法：**数字 + 单位**。
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|公分)", re.IGNORECASE)

#: 认不出的长度单位 —— 数字后面跟着它，但换算不了。
#: 「0.5 吋」「0.8 公尺」这类写法：规则层不猜，记成未知交给人看。
_UNKNOWN_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(公尺|吋|inches|inch)", re.IGNORECASE)

#: 尺寸保留的小数位 —— 「8.33mm」记成 8.3mm。
#:
#: 保留两位会让「8.55mm」与「8.6mm」在容差比对里分道扬镳；保留一位
#: 足够放射科核对（报告本身也只写到 0.1mm 精度），且与任务 27 的
#: 尺寸容差口径对得上。
SIZE_DECIMALS = 1


@dataclass(frozen=True)
class SizeMatch:
    """一处尺寸的解析结果。

    ``value_mm``
        换算成毫米后的数值（保留一位小数）。
    ``raw_value`` / ``unit``
        原文里的数字与单位写法（「0.8」/「cm」）。
    ``matched_text`` / ``start`` / ``end``
        命中的原文片段与位置（证据要指回原文）。
    """

    value_mm: float
    raw_value: str
    unit: str
    matched_text: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "value_mm": self.value_mm,
            "raw_value": self.raw_value,
            "unit": self.unit,
            "matched_text": self.matched_text,
            "start": self.start,
            "end": self.end,
        }


def convert_to_mm(value: str, unit: str) -> float | None:
    """把「数值 + 单位」换算成毫米。

    - ``cm`` 乘 10（``0.8cm`` → ``8.0mm``）；
    - 换算不了（单位不认识 / 数值不是数）→ 返回 ``None``，**不猜**。
    """

    canonical = UNIT_ALIASES.get((unit or "").lower())
    if canonical is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if canonical == "cm":
        amount = amount * CM_TO_MM
    try:
        rounded = amount.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return float(rounded)


def find_sizes(text: str) -> list[SizeMatch]:
    """在一段文本里找出所有尺寸，按出现顺序返回。"""

    if not text:
        return []

    found: list[SizeMatch] = []
    for match in _SIZE_RE.finditer(text):
        raw_value, unit = match.group(1), match.group(2)
        # 「5×3mm」里的 3 是**短径**不是最大径；认成 3mm 比认不出来更糟
        # （拿 3mm 去比模型的 5mm，一次命中会被算成漏诊）。后一个数直接跳过。
        if match.start() > 0 and text[match.start() - 1] in "×xX*·":
            continue
        value_mm = convert_to_mm(raw_value, unit)
        if value_mm is None:
            continue
        found.append(
            SizeMatch(
                value_mm=value_mm,
                raw_value=raw_value,
                unit=unit.lower(),
                matched_text=match.group(0),
                start=match.start(),
                end=match.end(),
            )
        )
    return found


def parse_size(text: str) -> SizeMatch | None:
    """取一处尺寸 —— **第一条**。

    报告通常先写最大径（「最大径约 8mm，横径 5mm」）→ 第一条就是最大径。
    多病灶 / 多径向的最严口径留给任务 28。
    """

    sizes = find_sizes(text)
    return sizes[0] if sizes else None


# --------------------------------------------------------------------------
# 分句与标记命中位置
# --------------------------------------------------------------------------

#: 分句边界（标点 + 换行）。换行是硬边界 —— 上一行的「未见」不外溢到下一行。
_CLAUSE_SPLIT_RE = re.compile(r"[。；;！!？?，,、\n\r]+")

#: 转折词：前后两句的结论可能相反，必须切开各判各的。
_CONTRAST_MARKERS: tuple[str, ...] = ("但是", "但", "不过", "然而", "除外")

#: 证据片段最长保留长度（避免把整段报告抄进产物）。
_EVIDENCE_LIMIT = 120


# --------------------------------------------------------------------------
# 否定作用域（任务 20）
# --------------------------------------------------------------------------
#
# **为什么要有作用域**：放射科写「双肺未见异常，但右下肺见磨玻璃结节」，
# 「未见」只管前半句 —— 整句判阴性会把一次真实的阳性算成漏诊，敏感度直接被拉低。
# 反过来，「双肺未见明显结节及钙化灶」里的「钙化」也在否定范围内 ——
# 把它当成阳性属性，比对时一次命中会被算成部位/密度不一致，同样是漏诊。
#
# 所以本层把否定拆成三件事：
#
# 1. **否定词管哪一句** —— 分句 / 分段，否定不跨硬边界；
# 2. **否定词管哪一个病灶词** —— 否定词与它管的那个病灶词之间只允许夹修饰字；
# 3. **被否定的病灶词不算数** —— 部位 / 密度 / 尺寸只从**未被否定**的病灶词上取。

#: **硬边界**：句子结束。跨过硬边界的属性**不许**互相借用
#: （上一句的「右下肺」不许落到下一句的病灶上）。
#:
#: 逗号**不是**硬边界 —— 「右下肺见一磨玻璃结节，大小约 8mm」是一句话，
#: 尺寸就是这句病灶的尺寸。
_SENTENCE_END_RE = re.compile(r"[。！？；!?;]+")


def _is_hard_boundary(gap: str) -> bool:
    """两句之间是不是**硬边界**（属性不许跨过去借）。

    - 句末标点（。！？；）→ 硬边界；
    - 换行 → 硬边界，**除非**上一行以逗号收尾 —— 那是同一句话折行
      （「右下肺见一磨玻璃结节，\\n大小约 8mm」），尺寸还是这句病灶的。
    """

    if _SENTENCE_END_RE.search(gap):
        return True
    if "\n" in gap or "\r" in gap:
        return not gap.rstrip().endswith((",", "，", "、"))
    return False

#: 否定词与它管的那个病灶词之间**允许夹的修饰字**。
#:
#: 「未见**明显**结节」管得到；「未见异常**可见**磨玻璃影」管不到 ——
#: 「可见」是新的一句陈述的引导词，不是修饰字。
_NEGATION_GAP_ALT: str = "|".join(
    ("明显", "新发", "典型", "确切", "重要", "较大", "较小", "多发", "散在", "的", "等")
)
_NEGATION_GAP_RE = re.compile(
    rf"^(?:[\s]*(?:{_NEGATION_GAP_ALT}))*$"
)

#: 病灶词之间**允许夹的并列连接字** —— 夹着它们的病灶词是**同一处**病灶的
#: 并列描述（「未见明显结节**及**钙化灶」），不是第二处病灶。
_COORDINATION_GAP_RE = re.compile(r"^(?:[\s]*(?:及|和|与|或|以及))+$")

#: 病灶词（肯定标记）的正则分支 —— 按长度倒序，保证「磨玻璃结节」先匹到长的。
_FINDING_ALT: str = "|".join(
    re.escape(marker)
    for marker in sorted(AFFIRMATIVE_MARKERS, key=len, reverse=True)
)

#: 「无」+ 病灶词 —— 放射科写「无钙化灶」「无结节影」的固定说法。
#:
#: 刻意不收单字「无」（「无心影」「无特殊病史」会假阴性），
#: 但「无」**直接管着**一个病灶词时，它就是否定 —— 这一条由本正则兜住。
_PATTERN_NEGATION_RE = re.compile(
    rf"无((?:[\s]*(?:{_NEGATION_GAP_ALT}))*[\s]*(?:{_FINDING_ALT}))"
)


@dataclass(frozen=True)
class ClauseSpan:
    """一句原文（带位置）。

    ``text``
        这一句的内容（首尾空白已去掉）。
    ``start`` / ``end``
        在**原文**里的起止下标 —— 标记位置与证据片段都指回原文。
    """

    text: str
    start: int
    end: int


@dataclass(frozen=True)
class MarkerHit:
    """一个否定 / 肯定标记的命中位置。

    ``clause_index``
        落在第几句里（-1 = 不在任何一句内）。任务 20 做否定作用域时，
        靠它判断「这个否定词管的是哪一句」。
    """

    marker: str
    start: int
    end: int
    clause_index: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "start": self.start,
            "end": self.end,
            "clause_index": self.clause_index,
        }


@dataclass(frozen=True)
class FindingHit:
    """一处**病灶词**的命中。

    ``marker``
        病灶词原文（「磨玻璃结节」「结节影」…）。
    ``start`` / ``end``
        在原文里的起止下标 —— 作用域判定与证据片段都指回原文。
    """

    marker: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "start": self.start,
            "end": self.end,
        }


def _make_span(raw: str, start: int) -> ClauseSpan | None:
    """把一段原文切成一个带位置的句子（首尾空白去掉，位置相应前移）。"""

    stripped = raw.strip()
    if not stripped:
        return None
    offset = len(raw) - len(raw.lstrip())
    return ClauseSpan(
        text=stripped,
        start=start + offset,
        end=start + offset + len(stripped),
    )


def _split_by_marker(span: ClauseSpan, marker: str) -> list[ClauseSpan]:
    """按一个转折词把一句切成几段（转折词本身单独成句）。"""

    pieces: list[ClauseSpan] = []
    cursor = 0
    while True:
        index = span.text.find(marker, cursor)
        if index < 0:
            tail = _make_span(span.text[cursor:], span.start + cursor)
            if tail is not None:
                pieces.append(tail)
            break
        head = _make_span(span.text[cursor:index], span.start + cursor)
        if head is not None:
            pieces.append(head)
        # 转折词本身也是一句：它不表态，但「除外」这类词同时是否定标记，
        # 单独成句才不会被前一句的结论吃掉。
        marker_span = _make_span(marker, span.start + index)
        if marker_span is not None:
            pieces.append(marker_span)
        cursor = index + len(marker)
    return pieces


def split_clauses(text: str) -> list[ClauseSpan]:
    """把原文切成一句一句（标点、换行、转折词都是边界）。

    与任务 14 的存在性层**同一套切法** —— 切法变了，存在性判定就会变，
    前后两轮的结论就没法互相印证了。
    """

    if not text:
        return []

    spans: list[ClauseSpan] = []
    cursor = 0
    for match in _CLAUSE_SPLIT_RE.finditer(text):
        piece = _make_span(text[cursor : match.start()], cursor)
        if piece is not None:
            spans.append(piece)
        cursor = match.end()
    tail = _make_span(text[cursor:], cursor)
    if tail is not None:
        spans.append(tail)

    for marker in _CONTRAST_MARKERS:
        expanded: list[ClauseSpan] = []
        for span in spans:
            expanded.extend(_split_by_marker(span, marker))
        spans = expanded
    return spans


def _first_hit(text: str, markers: Sequence[str]) -> tuple[int, str]:
    """返回（首次出现的位置, 命中的词）；没命中返回 ``(-1, "")``。"""

    best_pos = -1
    best_word = ""
    lowered = text.lower()
    for marker in markers:
        pos = lowered.find(marker.lower())
        if pos < 0:
            continue
        if best_pos < 0 or pos < best_pos:
            best_pos = pos
            best_word = marker
    return best_pos, best_word


def _marker_hits(text: str, markers: Sequence[str]) -> list[MarkerHit]:
    """找出所有标记命中（**最靠左最长**，重叠的只留最长的那个）。"""

    if not text:
        return []

    lowered = text.lower()
    raw: list[MarkerHit] = []
    for marker in markers:
        lowered_marker = marker.lower()
        start = 0
        while True:
            index = lowered.find(lowered_marker, start)
            if index < 0:
                break
            raw.append(
                MarkerHit(
                    marker=marker,
                    start=index,
                    end=index + len(marker),
                )
            )
            start = index + 1

    raw.sort(key=lambda hit: (hit.start, -(hit.end - hit.start)))
    kept: list[MarkerHit] = []
    for hit in raw:
        if kept and hit.start < kept[-1].end:
            continue  # 与上一个命中重叠：保留更长的那个
        kept.append(hit)
    return kept


def _with_clause_index(hits: Sequence[MarkerHit], clauses: Sequence[ClauseSpan]) -> list[MarkerHit]:
    """给每个命中标上它落在第几句里（任务 20 的否定作用域要用）。"""

    marked: list[MarkerHit] = []
    for hit in hits:
        index = -1
        for position, clause in enumerate(clauses):
            if clause.start <= hit.start < clause.end:
                index = position
                break
        marked.append(
            MarkerHit(
                marker=hit.marker,
                start=hit.start,
                end=hit.end,
                clause_index=index,
            )
        )
    return marked


def find_negations(text: str) -> list[MarkerHit]:
    """找出所有否定标记及其位置。

    两类都收：词表里的否定标记（「未见」「无明显」…）与「无」+ 病灶词的
    写法（「无钙化灶」）。位置用来判**否定作用域**：「双肺未见异常，
    但右下肺见结节」里的「未见」管不到后半句。
    """

    clauses = split_clauses(text)
    hits = _dedupe_hits(
        list(_marker_hits(text, NEGATION_MARKERS)) + list(_pattern_negations(text))
    )
    return _with_clause_index(hits, clauses)


def _dedupe_hits(hits: Sequence[MarkerHit]) -> list[MarkerHit]:
    """去掉重叠的命中（保留最靠左、最长的那个）。"""

    ordered = sorted(hits, key=lambda hit: (hit.start, -(hit.end - hit.start)))
    kept: list[MarkerHit] = []
    for hit in ordered:
        if kept and hit.start < kept[-1].end:
            continue
        kept.append(hit)
    return kept


def _pattern_negations(text: str) -> list[MarkerHit]:
    """「无」+ 病灶词 —— 放射科写「无钙化灶」「无结节影」的固定说法。

    刻意**不收单字「无」**：「无心影」「无特殊病史」这类句子里它先出现，
    收进来会把一句有病灶的话判成阴性。只有「无」**直接管着一个病灶词**时，
    它才是否定 —— 这一条由本函数兜住。
    """

    if not text:
        return []

    hits: list[MarkerHit] = []
    for match in _PATTERN_NEGATION_RE.finditer(text):
        matched = match.group(0)
        hits.append(
            MarkerHit(
                marker=matched,
                start=match.start(),
                end=match.end(),
            )
        )
    return hits


def _finding_hits(text: str) -> list[FindingHit]:
    """找出所有**病灶词**命中，**相邻的合并成一处**。

    「磨玻璃结节」是**一处**病灶（磨玻璃 + 结节），不是两处 ——
    分成两处会让多病灶口径数出两处病灶，比对时按最严口径就全乱了。

    而「结节可见磨玻璃影」里的「可见」隔开了两处 —— 那**是**两处。
    """

    if not text:
        return []

    merged: list[FindingHit] = []
    for hit in _marker_hits(text, AFFIRMATIVE_MARKERS):
        if merged:
            last = merged[-1]
            gap = text[last.end : hit.start]
            if not gap.strip():
                # 相邻（中间只隔空白）→ 同一处病灶的复合说法
                merged[-1] = FindingHit(
                    marker=text[last.start : hit.end],
                    start=last.start,
                    end=hit.end,
                )
                continue
        merged.append(FindingHit(marker=hit.marker, start=hit.start, end=hit.end))
    return merged


def _extend_findings_with_density(
    findings: Sequence[FindingHit],
    densities: Sequence[DensityMatch],
) -> list[FindingHit]:
    """把压在病灶词左边的密度词并进病灶词（「部分实性结节」别只读到「实性结节」）。"""

    extended: list[FindingHit] = []
    for finding in findings:
        start, end = finding.start, finding.end
        for density in densities:
            # 密度词与病灶词重叠且贴着左边 → 往前并
            if density.start <= start and density.end > start and density.end <= end:
                start = density.start
        extended.append(
            FindingHit(marker=finding.marker, start=start, end=end)
        )
    return extended


def find_affirmatives(text: str) -> list[MarkerHit]:
    """找出所有肯定标记及其位置（与否定标记对称，供任务 20 比对作用域）。"""

    clauses = split_clauses(text)
    return _with_clause_index(_marker_hits(text, AFFIRMATIVE_MARKERS), clauses)


# --------------------------------------------------------------------------
# 认不出来的原词（记成未知，不猜）
# --------------------------------------------------------------------------

#: 部位类提示词 —— 句子里出现这些字，多半在描述部位。
_LOCATION_HINT_RE = re.compile(r"[肺叶段]")

#: 密度类提示词 —— 句子里出现这些字，多半在描述密度。
_DENSITY_HINT_RE = re.compile(r"(密度|实性|磨玻|钙化|空洞|囊性|实变|亚实性)", re.IGNORECASE)

#: 连接词 —— 「密度不均的结节」里的「密度不均」才是术语本身，
#: 断句时按这些字把候选切小。
_CONNECTIVE_CHARS = "见有呈为示及和与并可其该等的"

#: 中文词片段（不含数字、拉丁字母与标点）。
_CJK_RUN_RE = re.compile(r"[一-鿿]+")


def _segments_by_connective(text: str) -> list[tuple[str, int]]:
    """按连接词把一段中文切成若干候选术语（返回术语与其起始下标）。"""

    segments: list[tuple[str, int]] = []
    cursor = 0
    for index, char in enumerate(text):
        if char in _CONNECTIVE_CHARS:
            piece = text[cursor:index]
            if piece:
                segments.append((piece, cursor))
            cursor = index + 1
    tail = text[cursor:]
    if tail:
        segments.append((tail, cursor))
    return segments


def _candidate_term(run_text: str, hint_offset: int, other_hint: re.Pattern[str]) -> str:
    """从一段中文里取出「最像术语」的那一段。

    - 先按连接词切成候选术语，取**含提示词**的那一段；
    - 再在下一个「另一类提示词」处截断（「肺门区钙化」→ 部位候选取「肺门区」）。
    """

    for segment, offset in _segments_by_connective(run_text):
        if not (offset <= hint_offset < offset + len(segment)):
            continue
        cut = len(segment)
        for match in other_hint.finditer(segment):
            if match.start() > hint_offset:
                cut = match.start()
                break
        return segment[:cut]
    return run_text


def _covered(span_start: int, span_end: int, hits: Sequence[int], run_start: int) -> bool:
    """提示词位置是否落在某个已命中区间内（落在里面 = 已被解释）。"""

    return any(span_start <= run_start + position < span_end for position in hits)


def _unknown_terms(
    text: str,
    location_matches: Sequence[LocationMatch],
    density_matches: Sequence[DensityMatch],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """找出**认不出来**的部位词与密度词（原词记下来，不猜标签）。

    判据：句子里出现了部位 / 密度提示词，但**本句里没有任何等价表命中覆盖它** ——
    说明这里有属性信息，只是这张表还认不出来。记下来，人可以据此补别名。

    **按句算**：上一句的部位命中解释不了这一句的部位词 ——
    「右下肺见结节，肺门区见淋巴结肿大」里的「肺门区」照样得报出来。
    """

    unknown_locations: list[str] = []
    unknown_densities: list[str] = []

    for run in _CJK_RUN_RE.finditer(text or ""):
        run_text = run.group(0)
        run_start = run.start()

        location_hints = [m.start() for m in _LOCATION_HINT_RE.finditer(run_text)]
        if location_hints:
            explained = any(
                _covered(match.start, match.end, location_hints, run_start)
                for match in location_matches
            )
            if not explained:
                term = _candidate_term(run_text, location_hints[0], _DENSITY_HINT_RE)
                if term:
                    unknown_locations.append(term)

        density_hints = [m.start() for m in _DENSITY_HINT_RE.finditer(run_text)]
        if density_hints:
            explained = any(
                _covered(match.start, match.end, density_hints, run_start)
                for match in density_matches
            )
            if not explained:
                term = _candidate_term(run_text, density_hints[0], _LOCATION_HINT_RE)
                if term:
                    unknown_densities.append(term)

    return tuple(unknown_locations), tuple(unknown_densities)


def _unknown_size_terms(text: str) -> tuple[str, ...]:
    """认不出来的长度单位（「0.5 吋」）—— 换算不了就不给尺寸。"""

    found: list[str] = []
    for match in _UNKNOWN_UNIT_RE.finditer(text or ""):
        found.append(match.group(0).strip())
    return tuple(found)


# --------------------------------------------------------------------------
# 归并结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleDiagnostics:
    """规则归并的**过程台账** —— 复核的人靠它看清「这一例读出来什么、没读出什么」。

    ``negations`` / ``affirmatives``
        否定 / 肯定标记的命中位置（任务 20 的否定作用域判定靠它们）。
    ``unknown_location_terms`` / ``unknown_density_terms`` / ``unknown_size_terms``
        **认不出来的原词**。属性留空不是「没问题」，是「这里没读出来」——
        记下来，人可以据此补别名或补单位。
    ``clause_presence``
        **逐句**的存在性结论（``True`` / ``False`` / ``None`` = 该句不表态）。
        复核的人靠它看清「这一例是哪一句定的」。
    ``fragments``
        按换行切开的**片段**（跨行叙述拼接后的原文行）。
        否定词的位置落在哪一片里，一眼可见 —— 上一行的「未见」不会外溢到下一行。
    """

    clauses: tuple[str, ...] = ()
    negations: tuple[MarkerHit, ...] = ()
    affirmatives: tuple[MarkerHit, ...] = ()
    unknown_location_terms: tuple[str, ...] = ()
    unknown_density_terms: tuple[str, ...] = ()
    unknown_size_terms: tuple[str, ...] = ()
    location_match: LocationMatch | None = None
    density_match: DensityMatch | None = None
    size_match: SizeMatch | None = None
    clause_presence: tuple[bool | None, ...] = ()
    fragments: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "clauses": list(self.clauses),
            "negations": [hit.to_dict() for hit in self.negations],
            "affirmatives": [hit.to_dict() for hit in self.affirmatives],
            "unknown_location_terms": list(self.unknown_location_terms),
            "unknown_density_terms": list(self.unknown_density_terms),
            "unknown_size_terms": list(self.unknown_size_terms),
            "location_match": self.location_match.to_dict() if self.location_match else None,
            "density_match": self.density_match.to_dict() if self.density_match else None,
            "size_match": self.size_match.to_dict() if self.size_match else None,
            "clause_presence": list(self.clause_presence),
            "fragments": list(self.fragments),
        }


@dataclass(frozen=True)
class RuleMerge:
    """一次规则归并的结果：结论 + 台账。

    ``labels``
        与语义归并**同一份**契约（:class:`~nodule_eval.goldstd.schema.GoldLabels`），
        两边可直接逐字段比对。
    ``diagnostics``
        过程台账（分句、标记位置、认不出来的原词）。
    """

    labels: GoldLabels
    diagnostics: RuleDiagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "labels": self.labels.to_dict(),
            "diagnostics": self.diagnostics.to_dict(),
        }


@dataclass(frozen=True)
class _ClauseFacts:
    """一句里的全部命中（位置都是**原文**里的绝对下标）。

    ``segment``
        这一句属于第几段 —— 属性只在**同一段内**互相借用。
    """

    clause: ClauseSpan
    segment: int
    negations: tuple[MarkerHit, ...]
    findings: tuple[FindingHit, ...]
    locations: tuple[LocationMatch, ...]
    sizes: tuple[SizeMatch, ...]
    densities: tuple[DensityMatch, ...]


def _clause_index_of(position: int, clauses: Sequence[ClauseSpan]) -> int:
    """一个下标落在第几句里；不在任何一句内返回 ``-1``。"""

    for index, clause in enumerate(clauses):
        if clause.start <= position < clause.end:
            return index
    return -1


def _assign_segments(clauses: Sequence[ClauseSpan], text: str) -> list[int]:
    """给每一句标上它属于第几**段**。

    **段** = 硬边界（句末标点、换行、转折词）之间的一串句子。
    属性（部位 / 尺寸 / 密度）只在**同一段内**互相借用 ——
    上一句的「右下肺」不许落到下一句的病灶上。

    逗号**不是**硬边界：「右下肺见一磨玻璃结节，大小约 8mm」是一句话，
    尺寸就是这句病灶的尺寸。
    """

    segments: list[int] = []
    index = 0
    for position, clause in enumerate(clauses):
        if position > 0:
            previous = clauses[position - 1]
            gap = text[previous.end : clause.start]
            if _is_hard_boundary(gap) or previous.text in _CONTRAST_MARKERS:
                index += 1
        segments.append(index)
    return segments


def _build_clause_facts(
    text: str,
    clauses: Sequence[ClauseSpan],
    segments: Sequence[int],
    table: LabelTable | None,
) -> list[_ClauseFacts]:
    """把整段原文的命中按句分桶。"""

    densities = find_densities(text, table=table)
    findings = _extend_findings_with_density(_finding_hits(text), densities)
    negations = find_negations(text)
    locations = find_locations(text, table=table)
    sizes = find_sizes(text)

    def _bucket(items: Sequence[Any], start_of: Any) -> list[list[Any]]:
        buckets: list[list[Any]] = [[] for _ in clauses]
        for item in items:
            index = _clause_index_of(start_of(item), clauses)
            if index >= 0:
                buckets[index].append(item)
        return buckets

    neg_buckets = _bucket(negations, lambda hit: hit.start)
    find_buckets = _bucket(findings, lambda hit: hit.start)
    loc_buckets = _bucket(locations, lambda match: match.start)
    size_buckets = _bucket(sizes, lambda match: match.start)
    dens_buckets = _bucket(densities, lambda match: match.start)

    return [
        _ClauseFacts(
            clause=clause,
            segment=segments[index],
            negations=tuple(neg_buckets[index]),
            findings=tuple(find_buckets[index]),
            locations=tuple(loc_buckets[index]),
            sizes=tuple(size_buckets[index]),
            densities=tuple(dens_buckets[index]),
        )
        for index, clause in enumerate(clauses)
    ]


def _cover_coordinated(
    text: str,
    findings: Sequence[FindingHit],
    covered: set[int],
    index: int,
) -> None:
    """并列描述**同一处**病灶的病灶词（「及」「和」连接）一并被否定。"""

    previous = findings[index]
    for next_index in range(index + 1, len(findings)):
        candidate = findings[next_index]
        gap = text[previous.end : candidate.start]
        if _COORDINATION_GAP_RE.match(gap) is None:
            return
        covered.add(next_index)
        previous = candidate


def _governed_indices(
    text: str,
    negations: Sequence[MarkerHit],
    findings: Sequence[FindingHit],
) -> set[int]:
    """算哪些病灶词落在否定词的**作用域**内（被否定掉了）。

    **否定词管得到哪个病灶词**：否定词之后**最近**的那个病灶词，
    且两者之间只夹着修饰字（「未见**明显**结节」）；
    中间隔着「可见」这类新陈述的引导词 —— 否定管不到（那是一句新的话）。
    """

    covered: set[int] = set()
    for negation in negations:
        for index, finding in enumerate(findings):
            if index in covered:
                continue
            if finding.start < negation.start:
                continue  # 病灶词在否定词之前 → 不受它管（「除…外未见异常」）
            if finding.start < negation.end:
                # 病灶词落在否定标记**里面**（「无钙化」这种写法）→ 直接被否定
                covered.add(index)
                break
            gap = text[negation.end : finding.start]
            if _NEGATION_GAP_RE.match(gap) is None:
                continue  # 中间隔着新陈述的引导词 → 这处病灶不在否定范围内
            covered.add(index)
            _cover_coordinated(text, findings, covered, index)
            break
    return covered


def _clause_presence(
    text: str,
    negations: Sequence[MarkerHit],
    findings: Sequence[FindingHit],
) -> tuple[bool | None, set[int]]:
    """判一句的存在性，并给出**未被否定**的病灶词下标。

    - 有未被否定的病灶词 → 有病灶（``True``）；
    - 没有病灶词但有否定词 → 没病灶（``False``）；
    - 两者都没有 → 这句**不表态**（``None``）。
    """

    covered = _governed_indices(text, negations, findings)
    asserted = {index for index in range(len(findings)) if index not in covered}
    if asserted:
        return True, asserted
    if negations:
        return False, set()
    return None, set()


def _pick_nearest(
    anchors: Sequence[tuple[int, int, Any]],
    position: int,
) -> Any | None:
    """取**最近的前一个**锚点；没有就取最近的后一个（后一个不能被后面的病灶词占着）。"""

    before = [item for item in anchors if item[0] <= position]
    if before:
        return max(before, key=lambda item: item[0])[2]
    after = [item for item in anchors if item[0] > position]
    if after:
        return min(after, key=lambda item: item[0])[2]
    return None


def _pick_density(
    densities: Sequence[DensityMatch],
    start: int,
    end: int,
) -> str | None:
    """取一处病灶的密度：**压住它的**优先，其次就近。"""

    overlapping = [item for item in densities if item.start < end and item.end > start]
    if overlapping:
        return overlapping[0].canonical
    before = [item for item in densities if item.start <= start]
    if before:
        return max(before, key=lambda item: item.start).canonical
    after = [item for item in densities if item.start > start]
    if after:
        return min(after, key=lambda item: item.start).canonical
    return None


def _separated_by_negation(
    anchor_start: int,
    finding_start: int,
    negations: Sequence[MarkerHit],
) -> bool:
    """属性与病灶词之间是否隔着一个否定词。

    隔开了就说明两者**分属不同的表述** —— 不许互相认领：

    - 「双肺**未见**明显结节，可见磨玻璃影」：部位「双肺」是那句否定的话的
      主语，不是那处磨玻璃影的部位；
    - 「除右下肺小结节**外**未见异常」：部位「右下肺」与病灶词「小结节」
      都在否定词之前，同属一处 → 部位照常认领。
    """

    low, high = (
        (anchor_start, finding_start)
        if anchor_start <= finding_start
        else (finding_start, anchor_start)
    )
    return any(low < negation.start <= high for negation in negations)


def _build_lesions(
    facts: Sequence[_ClauseFacts],
    asserted_by_clause: Sequence[Sequence[int]],
) -> list[LesionLabels]:
    """按**未被否定**的病灶词，一处一处建标签。

    属性只在**同一段内**取：上一句的部位不许落到下一句的病灶上
    （「双肺未见异常，但右下肺见磨玻璃结节」的部位是**右下肺**，不是双肺）。

    **被否定词隔开的属性不许认领病灶**（见 :func:`_separated_by_negation`）。

    **后面的病灶词占着的属性不许倒着认领**：「双肺见多发结节及钙化灶」里，
    「钙化」是第二处病灶的密度，不是第一处的。
    """

    locations_by_segment: dict[int, list[tuple[int, int, str]]] = {}
    sizes_by_segment: dict[int, list[tuple[int, int, float]]] = {}
    densities_by_segment: dict[int, list[DensityMatch]] = {}
    findings_by_segment: dict[int, list[FindingHit]] = {}
    negations_by_segment: dict[int, list[MarkerHit]] = {}

    for fact in facts:
        locations_by_segment.setdefault(fact.segment, []).extend(
            (match.start, match.end, match.canonical) for match in fact.locations
        )
        sizes_by_segment.setdefault(fact.segment, []).extend(
            (match.start, match.end, match.value_mm) for match in fact.sizes
        )
        densities_by_segment.setdefault(fact.segment, []).extend(fact.densities)
        findings_by_segment.setdefault(fact.segment, []).extend(fact.findings)
        negations_by_segment.setdefault(fact.segment, []).extend(fact.negations)

    def _claimed_by_later(anchor: tuple[int, int, Any], later: Sequence[FindingHit]) -> bool:
        return any(
            other.start < anchor[1] and other.end > anchor[0] for other in later
        )

    lesions: list[LesionLabels] = []
    for position, fact in enumerate(facts):
        segment = fact.segment
        segment_findings = findings_by_segment.get(segment, [])
        negations = negations_by_segment.get(segment, [])
        for index in asserted_by_clause[position]:
            finding = fact.findings[index]
            later = [
                other
                for other in segment_findings
                if other.start > finding.start
            ]

            def _usable(anchor: tuple[int, int, Any]) -> bool:
                if _claimed_by_later(anchor, later):
                    return False
                return not _separated_by_negation(
                    anchor[0], finding.start, negations
                )

            locations = locations_by_segment.get(segment, [])
            location = _pick_nearest(
                [item for item in locations if _usable(item)],
                finding.start,
            )
            sizes = sizes_by_segment.get(segment, [])
            size = _pick_nearest(
                [item for item in sizes if _usable(item)],
                finding.start,
            )
            density = _pick_density(
                [
                    item
                    for item in densities_by_segment.get(segment, [])
                    if _usable((item.start, item.end, item.canonical))
                ],
                finding.start,
                finding.end,
            )
            lesions.append(
                LesionLabels(
                    location=location,
                    size_mm=size,
                    density=density,
                    finding=finding.marker,
                    evidence_span=fact.clause.text[:_EVIDENCE_LIMIT],
                )
            )
    return lesions


def _first_present(values: Sequence[Any]) -> Any | None:
    """取第一个**有值**的（辅助用：顶层属性取「首个病灶」，缺失时往下顺延）。"""

    for value in values:
        if value is not None:
            return value
    return None


def merge_by_rules(
    text: str,
    *,
    normalized_text: str | None = None,
    table: LabelTable | None = None,
) -> RuleMerge:
    """把一段金标准原文按规则归并成标签。

    参数
    ----
    text
        金标准**原文**。本函数只读它，一个字都不改。
    normalized_text
        读取层产出的**分析用副本**（全角数字与中文标点已归一）。
        给了就用它匹配 —— 原文里写「，」还是「,」不该影响判读。
    table
        部位 / 密度等价表（默认用内置表）。金标准侧与模型侧共用同一张表。

    **归并口径**（否定作用域见任务 20）：

    - 存在性：**按句判，否定只管它自己那句**。一句里有未被否定的病灶词 →
      这句说有病灶；没有病灶词但有否定词 → 这句说没病灶；两者都没有 →
      这句不表态（``None``）。句与句之间取并存事实：**任一句说有 → 整体说有**；
      否则任一句说没有 → 整体没有；都没有 → ``presence=None``
      （读不出来，挂人工复核）。
    - 部位 / 密度 / 尺寸：只从**未被否定**的病灶词上取，且**不跨否定词**认领
      （「双肺未见明显结节，可见磨玻璃影」的部位不是「双肺」）；
      认不出来留 ``None``；
    - 尺寸：``mm`` / ``毫米`` 原样、``cm`` / ``厘米`` / ``公分`` 乘 10，
      统一成毫米，保留一位小数。
    - 多病灶：逐处病灶落一条 ``LesionLabels``，顶层三个属性是**首个病灶**的投影。
    """

    source_text = (normalized_text if normalized_text else text) or ""

    clauses = split_clauses(source_text)
    segments = _assign_segments(clauses, source_text)
    facts = _build_clause_facts(source_text, clauses, segments, table)

    presence: bool | None = None
    evidence = ""
    clause_presences: list[bool | None] = []
    asserted_by_clause: list[list[int]] = []

    for fact in facts:
        clause_presence, asserted = _clause_presence(
            source_text, fact.negations, fact.findings
        )
        clause_presences.append(clause_presence)
        asserted_by_clause.append(sorted(asserted))

        if clause_presence is None:
            continue  # 这一句不表态
        if presence is None:
            presence = clause_presence
            evidence = fact.clause.text[:_EVIDENCE_LIMIT]
        elif clause_presence and not presence:
            # 并存事实：任一句说有 → 整体说有（「未见异常，但见结节」）。
            presence = True
            evidence = fact.clause.text[:_EVIDENCE_LIMIT]
        # 前一句已经说有 → 保持（并存事实取阳性那一侧）

    lesions = _build_lesions(facts, asserted_by_clause)

    unknown_locations: list[str] = []
    unknown_densities: list[str] = []
    for fact in facts:
        loc_terms, dens_terms = _unknown_terms(
            fact.clause.text, fact.locations, fact.densities
        )
        unknown_locations.extend(loc_terms)
        unknown_densities.extend(dens_terms)
    unknown_sizes = _unknown_size_terms(source_text)

    # 顶层属性 = **首个病灶**的投影（多病灶的逐处标签在 ``lesions`` 里）。
    primary = lesions[0] if lesions else None
    labels = GoldLabels(
        presence=presence,
        location=primary.location if primary is not None else None,
        size_mm=primary.size_mm if primary is not None else None,
        density=primary.density if primary is not None else None,
        lesions=tuple(lesions),
        evidence_span=evidence,
        confidence=None,  # 规则基线没有置信度可言
        needs_human_review=presence is None,
        source=NORMALIZE_SOURCE_RULE,
    )

    diagnostics = RuleDiagnostics(
        clauses=tuple(clause.text for clause in clauses),
        negations=tuple(find_negations(source_text)),
        affirmatives=tuple(find_affirmatives(source_text)),
        unknown_location_terms=tuple(unknown_locations),
        unknown_density_terms=tuple(unknown_densities),
        unknown_size_terms=unknown_sizes,
        location_match=find_location(source_text, table=table),
        density_match=find_density(source_text, table=table),
        size_match=parse_size(source_text),
        clause_presence=tuple(clause_presences),
        fragments=tuple(source_text.split("\n")),
    )
    return RuleMerge(labels=labels, diagnostics=diagnostics)


def normalize_by_rules(
    text: str,
    *,
    normalized_text: str | None = None,
    table: LabelTable | None = None,
) -> GoldLabels:
    """只要结论、不要台账 —— 与语义归并（任务 21）**同一个返回类型**。

    两边返回同一种 :class:`~nodule_eval.goldstd.schema.GoldLabels`，
    任务 25 的「规则基线 vs 语义归并」对照表才能逐字段直接比。
    """

    return merge_by_rules(
        text,
        normalized_text=normalized_text,
        table=table,
    ).labels
