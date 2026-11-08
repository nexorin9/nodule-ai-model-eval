"""多标签最严口径（任务 28）：**金标准写了 N 处病灶，模型就得盖住 N 处**。

**这个模块解决什么**：放射科写的金标准常常不止一处 ——
「右肺上叶见 5mm 实性结节，左下肺见 12mm 磨玻璃结节」是**两处**。
模型只报出右肺上叶那一处，按「有没有检出」算它是**命中**；
按「金标准说���几处、模型盖住几处」算，它是**漏了一处**。

两种口径都有人用，但**混着用会把敏感度算虚高**：厂商只要把最显眼的那处
报出来就算过，而漏掉的那处恰恰是最难的那处。评审要的是后一种口径 ——
**金标准标签集合中任一标签未被覆盖即判 ``miss``（最严口径，避免高估）**。

**对应（correspondence）怎么判**：一处金标准病灶与一个模型候选是不是
「同一处」，本模块按**部位层定**：

===================  ==========================================================
部位层结论            这一对（金标准病灶 × 模型候选）的对应关系
===================  ==========================================================
一致                  **对应** —— 同一个肺叶的同一个病灶
不一致                **不对应** —— 部位都对不上，不是同一处
不可比 / 未比对        部位**没有证据**：转看密度 / 尺寸作**佐证**
===================  ==========================================================

**为什么只有部位能否对应决**：部位是**病灶的身份**（同一个肺叶 = 同一处病灶）；
尺寸与密度是**属性**（量得准不准、认得对不对）。属性对不上不等于「不是那一处」——
把 8mm 量成 9mm 判成漏诊，是把「测量偏差」当成「没看见」，敏感度会被算歪。
所以密度 / 尺寸只**支持**对应（一致 → 对应），**不否**对应
（不一致 → 仍不可比，不判不对应）。**部位不一致才否对应**。

**不可比不是不一致**：两侧写法都认不出来时，**没有可比的对象** ——
既不当对应（白送一次命中），也不当不对应（把一次命中算成漏诊）。
这一类单独记为**不可比**，由判定层原样带出去。

**一个候选只能盖一处病灶**：金标准写了两处、模型只报了一处 ——
那一处算覆盖，另一处算**未覆盖**（本模块的核心结论）。

**源产品对照（反面样本）**：源产品的胸片分类工具
（``github_ref/MedRAX/medrax/tools/classification.py`` 的 ``ChestXRayClassifierTool._run``）
输出的是 **18 类病理的存在性概率字典**（``dict(zip(xrv.datasets.default_pathologies, preds.numpy()))``）——
**没有病灶级标签、没有金标准可比、更没有「盖住了几处」这件事**：
它回答的是「这张片子上有没有结节」，答不出「金标准那三处，你盖住了几处」。
本模块这一整套（病灶级对应、覆盖计数、未覆盖标签）是**新建**的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..contracts import DEFAULT_SIZE_TOLERANCE_MM
from ..goldstd.labels import LabelTable
from ..goldstd.schema import GoldLabels, LesionLabels
from .attributes import (
    LAYER_INCOMPARABLE,
    LAYER_MATCH,
    LAYER_MISMATCH,
    LAYER_NOT_COMPARED,
    LayerOutcome,
    compare_density,
    compare_location,
    compare_size,
    normalize_tolerance,
)

__all__ = [
    "COVERAGE_COVERED",
    "COVERAGE_INCOMPARABLE",
    "COVERAGE_UNCOVERED",
    "CORRESPONDENCE_CONTRADICTED",
    "CORRESPONDENCE_MATCHED",
    "CORRESPONDENCE_UNDECIDED",
    "CoverageReport",
    "LesionCandidateOutcome",
    "LesionCoverage",
    "compare_coverage",
    "gold_lesions",
    "lesion_label_text",
]


# --------------------------------------------------------------------------
# 三种对应关系
# --------------------------------------------------------------------------
#: 这一对是**同一处**病灶。
CORRESPONDENCE_MATCHED = "对应"
#: 这一对**不是**同一处（部位对不上）。
CORRESPONDENCE_CONTRADICTED = "不对应"
#: 没有可比的对象 —— 写法认不出来，**既不当对应也不当不对应**。
CORRESPONDENCE_UNDECIDED = "无法判定"


# --------------------------------------------------------------------------
# 三种覆盖状态
# --------------------------------------------------------------------------
#: 这处金标准病灶被某个模型候选盖住了。
COVERAGE_COVERED = "已覆盖"
#: 这处金标准病灶**没被盖住** —— 最严口径下即判漏诊。
COVERAGE_UNCOVERED = "未覆盖"
#: 这处病灶的对应关系**判不出来**（写法认不出）—— 不当漏诊，单列不可比。
COVERAGE_INCOMPARABLE = "不可比"


#: **部位**是唯一能否对应决的层（病灶的身份）。
IDENTITY_LAYER = "location"

#: 部位没有证据时，依次作**佐证**的层（只支持对应，不否对应）。
SUPPORT_LAYERS: tuple[str, ...] = ("density", "size")

#: 层名 → 中文名（判定依据里用）。
_LAYER_NAMES: Mapping[str, str] = {
    "size": "尺寸",
    "location": "部位",
    "density": "密度",
}


# --------------------------------------------------------------------------
# 金标准病灶集合
# --------------------------------------------------------------------------
def gold_lesions(gold: GoldLabels) -> tuple[LesionLabels, ...]:
    """金标准的**病灶集合** —— 顶层属性的投影不够用时要逐处比。

    优先用 :attr:`GoldLabels.lesions`（归并层逐处落的标签）；
    它为空时（手写构造的标签、语义归并只落一条的情况），
    用**顶层四元组**补一处 —— 顶层属性本来就是首个病灶的投影，
    补出来的那一处与归并层给出的口径一致。
    """

    if gold.lesions:
        return tuple(gold.lesions)
    return (
        LesionLabels(
            location=gold.location,
            size_mm=gold.size_mm,
            density=gold.density,
            finding="",
            evidence_span=gold.evidence_span,
        ),
    )


def lesion_label_text(lesion: LesionLabels) -> str:
    """一处病灶的可读标签（「左肺下叶 12.0mm 磨玻璃」）。"""

    parts: list[str] = []
    if lesion.location:
        parts.append(str(lesion.location))
    if lesion.size_mm is not None:
        parts.append(f"{float(lesion.size_mm):.1f}mm")
    if lesion.density:
        parts.append(str(lesion.density))
    if not parts:
        return "（金标准未写部位/尺寸/密度）"
    return " ".join(parts)


def _has_attributes(lesion: LesionLabels) -> bool:
    """这处病灶**有没有**可比属性。"""

    return (
        lesion.location is not None
        or lesion.size_mm is not None
        or lesion.density is not None
    )


# --------------------------------------------------------------------------
# 逐对结论
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class LesionCandidateOutcome:
    """一处金标准病灶与一个模型候选之间的比对结论。

    ``correspondence``
        对应 / 不对应 / 无法判定。
    ``anchor_layer``
        判出这个结论所依据的层（部位优先；无依据时为空）。
    ``outcomes``
        逐层结论（尺寸 / 部位 / 密度）—— 与属性三层同一份口径。
    """

    lesion_index: int
    candidate_index: int
    correspondence: str
    anchor_layer: str
    outcomes: Mapping[str, LayerOutcome]
    reason: str

    @property
    def matched(self) -> bool:
        return self.correspondence == CORRESPONDENCE_MATCHED

    @property
    def contradicted(self) -> bool:
        return self.correspondence == CORRESPONDENCE_CONTRADICTED

    @property
    def undecided(self) -> bool:
        return self.correspondence == CORRESPONDENCE_UNDECIDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "lesion_index": self.lesion_index,
            "candidate_index": self.candidate_index,
            "correspondence": self.correspondence,
            "anchor_layer": self.anchor_layer,
            "reason": self.reason,
            "outcomes": {name: outcome.to_dict() for name, outcome in self.outcomes.items()},
        }


# --------------------------------------------------------------------------
# 逐处覆盖结论
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class LesionCoverage:
    """一处金标准病灶的**覆盖**结论。

    ``state``
        已覆盖 / 未覆盖 / 不可比。
    ``candidate_index``
        盖住它的那个候选的下标（未覆盖时为空）。
    ``anchor_layer``
        判出对应的层。
    ``reason``
        一句中文说明 —— 复核的人靠它知道「这一处为什么算没盖住」。
    """

    index: int
    lesion: LesionLabels
    state: str
    candidate_index: int | None = None
    anchor_layer: str = ""
    reason: str = ""

    @property
    def covered(self) -> bool:
        return self.state == COVERAGE_COVERED

    @property
    def uncovered(self) -> bool:
        return self.state == COVERAGE_UNCOVERED

    @property
    def incomparable(self) -> bool:
        return self.state == COVERAGE_INCOMPARABLE

    @property
    def label_text(self) -> str:
        """这处病灶的可读标签。"""

        return lesion_label_text(self.lesion)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "state": self.state,
            "candidate_index": self.candidate_index,
            "anchor_layer": self.anchor_layer,
            "reason": self.reason,
            "label": self.label_text,
            "lesion": self.lesion.to_dict(),
        }


# --------------------------------------------------------------------------
# 覆盖报告
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CoverageReport:
    """一例的**多标签覆盖**结论。

    ``total``
        金标准写了几处病灶。
    ``covered``
        模型盖住了几处。
    ``entries``
        逐处的覆盖结论（顺序与金标准一致）。
    ``pairs``
        逐对（金标准病灶 × 模型候选）的比对结论 —— 复核时可逐对核对。
    """

    total: int
    covered: int
    entries: tuple[LesionCoverage, ...] = ()
    pairs: tuple[LesionCandidateOutcome, ...] = ()

    @property
    def uncovered(self) -> tuple[LesionCoverage, ...]:
        """没被盖住的病灶（最严口径下即判漏诊）。"""

        return tuple(entry for entry in self.entries if entry.uncovered)

    @property
    def incomparable(self) -> tuple[LesionCoverage, ...]:
        """对应关系判不出来的病灶（单列不可比，不当漏诊）。"""

        return tuple(entry for entry in self.entries if entry.incomparable)

    @property
    def fully_covered(self) -> bool:
        """是否**全覆盖**（一处不落）。"""

        return self.total > 0 and not self.uncovered and not self.incomparable

    @property
    def partially_covered(self) -> bool:
        """是否**部分覆盖**（盖住了几处、漏了几处）。"""

        return bool(self.uncovered)

    def notes(self) -> tuple[str, ...]:
        """逐处覆盖的中文说明（判定依据里引用）。"""

        return tuple(
            f"第 {entry.index + 1} 处病灶 {entry.label_text}：{entry.reason}"
            for entry in self.entries
            if entry.reason
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "covered": self.covered,
            "fully_covered": self.fully_covered,
            "partially_covered": self.partially_covered,
            "entries": [entry.to_dict() for entry in self.entries],
            "pairs": [pair.to_dict() for pair in self.pairs],
        }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _pair_outcomes(
    lesion: LesionLabels,
    candidate: Any,
    *,
    tolerance_mm: float,
    table: LabelTable | None,
) -> dict[str, LayerOutcome]:
    """一处金标准病灶与一个候选的逐层结论。"""

    return {
        "size": compare_size(
            lesion.size_mm,
            _candidate_size(candidate),
            tolerance_mm=tolerance_mm,
        ),
        "location": compare_location(
            lesion.location,
            _candidate_text(candidate, "position"),
            table=table,
        ),
        "density": compare_density(
            lesion.density,
            _candidate_text(candidate, "density"),
            table=table,
        ),
    }


def _correspondence(
    outcomes: Mapping[str, LayerOutcome],
) -> tuple[str, str, str]:
    """判一对的对应关系 —— 返回 ``(对应关系, 依据层, 中文说明)``。

    口径（**只有部位能否对应决**）：

    1. 部位**一致** → 对应；
    2. 部位**不一致** → 不对应（部位对不上，不是同一处病灶）；
    3. 部位**不可比 / 未比对** → 没有身份证据：密度、尺寸**一致**可作佐证
       判为对应；它们**不一致不算数**（属性对不上不等于不是那一处），
       仍然没有一致证据时 → **无法判定**（不可比）。
    """

    location = outcomes["location"]
    if location.status == LAYER_MATCH:
        return CORRESPONDENCE_MATCHED, IDENTITY_LAYER, location.reason
    if location.status == LAYER_MISMATCH:
        return CORRESPONDENCE_CONTRADICTED, IDENTITY_LAYER, location.reason

    for name in SUPPORT_LAYERS:
        outcome = outcomes[name]
        if outcome.status == LAYER_MATCH:
            return (
                CORRESPONDENCE_MATCHED,
                name,
                f"部位无证据，{_LAYER_NAMES[name]}一致可作佐证（{outcome.reason}）",
            )

    return (
        CORRESPONDENCE_UNDECIDED,
        "",
        "部位写法认不出，密度与尺寸也没有一致证据，无法判定是不是同一处病灶",
    )


def compare_coverage(
    gold: GoldLabels,
    candidates: Sequence[Any] = (),
    *,
    tolerance_mm: float | None = DEFAULT_SIZE_TOLERANCE_MM,
    table: LabelTable | None = None,
) -> CoverageReport:
    """逐处比对金标准病灶与模型候选，给出**多标签最严口径**的覆盖结论。

    参数
    ----
    gold
        金标准归并结果（含 ``lesions`` 多病灶标签集合）。
    candidates
        模型检出的候选病灶序列。
    tolerance_mm
        尺寸容差（毫米）—— 与属性三层同一口径。
    table
        部位 / 密度等价表（缺省用内置的那一张）。

    返回
    ----
    :class:`CoverageReport`：

    - **全覆盖**（``fully_covered``）→ 判 ``hit``；
    - **部分覆盖**（``partially_covered``）→ 判 ``miss``，
      未覆盖的那几处逐条记在 ``uncovered`` 里（任务 30 的漏诊原因文案用它）；
    - 有病灶**对应关系判不出来** → 该处记 ``incomparable``，
      单独计数并标注，**既不当命中也不当漏诊**。
    """

    lesions = gold_lesions(gold)
    if not lesions:
        return CoverageReport(total=0, covered=0)

    if not candidates:
        entries = tuple(
            LesionCoverage(
                index=index,
                lesion=lesion,
                state=COVERAGE_UNCOVERED,
                reason="模型未检出候选，这一处病灶没有任何候选可以对应",
            )
            for index, lesion in enumerate(lesions)
        )
        return CoverageReport(total=len(lesions), covered=0, entries=entries)

    pairs: list[LesionCandidateOutcome] = []
    # 容差走**同一份**规整口径（``None`` → 缺省 ±2mm；负数 / 非有限值当场报错）。
    tolerance = normalize_tolerance(tolerance_mm)
    for lesion_index, lesion in enumerate(lesions):
        for candidate_index, candidate in enumerate(candidates):
            outcomes = _pair_outcomes(
                lesion, candidate, tolerance_mm=tolerance, table=table
            )
            correspondence, anchor, reason = _correspondence(outcomes)
            if not _has_attributes(lesion):
                # 金标准这一处**没写任何属性** —— 没有可比的对象，
                # 也无从否定；按存在性对应（不猜、不当成属性一致）。
                correspondence = CORRESPONDENCE_MATCHED
                anchor = ""
                reason = "金标准这一处未写部位/尺寸/密度，无可比属性，按存在性对应"
            pairs.append(
                LesionCandidateOutcome(
                    lesion_index=lesion_index,
                    candidate_index=candidate_index,
                    correspondence=correspondence,
                    anchor_layer=anchor,
                    outcomes=outcomes,
                    reason=reason,
                )
            )

    entries = _assign(pairs, lesions)
    covered = sum(1 for entry in entries if entry.covered)
    return CoverageReport(
        total=len(lesions),
        covered=covered,
        entries=entries,
        pairs=tuple(pairs),
    )


def _assign(
    pairs: Sequence[LesionCandidateOutcome],
    lesions: Sequence[LesionLabels],
) -> tuple[LesionCoverage, ...]:
    """把候选**一对一**分给病灶 —— 一个候选只能盖一处病灶。

    分配顺序（三步，缺一不可）：

    1. **先分能对上的**：逐处病灶取**尚未被占用**且判为「对应」的候选；
    2. **再看判不出来的**：没分到候选、且存在「无法判定」的候选 →
       这处病灶记**不可比**（不当漏诊，也不当命中）；
    3. **剩下的是没盖住的**：既没有对应候选、也没有无法判定的候选 →
       记**未覆盖**（最严口径下即判漏诊）。
    """

    used: set[int] = set()
    matched: dict[int, LesionCandidateOutcome] = {}
    undecided: dict[int, list[LesionCandidateOutcome]] = {}

    # ---- 第 1 步：能对上的先分 ----
    for pair in pairs:
        if not pair.matched:
            continue
        lesion_index = pair.lesion_index
        if lesion_index in matched:
            continue
        if pair.candidate_index in used:
            continue
        matched[lesion_index] = pair
        used.add(pair.candidate_index)

    # ---- 第 2 步：判不出来���，留作不可比 ----
    for pair in pairs:
        if pair.matched or not pair.undecided:
            continue
        undecided.setdefault(pair.lesion_index, []).append(pair)

    entries: list[LesionCoverage] = []
    for index, lesion in enumerate(lesions):
        pair = matched.get(index)
        if pair is not None:
            entries.append(
                LesionCoverage(
                    index=index,
                    lesion=lesion,
                    state=COVERAGE_COVERED,
                    candidate_index=pair.candidate_index,
                    anchor_layer=pair.anchor_layer,
                    reason=f"已由第 {pair.candidate_index + 1} 个候选覆盖（{pair.reason}）",
                )
            )
            continue

        pending = undecided.get(index)
        if pending:
            entries.append(
                LesionCoverage(
                    index=index,
                    lesion=lesion,
                    state=COVERAGE_INCOMPARABLE,
                    reason=(
                        "对应关系判不出来（"
                        + "；".join(item.reason for item in pending)
                        + "）"
                    ),
                )
            )
            continue

        entries.append(
            LesionCoverage(
                index=index,
                lesion=lesion,
                state=COVERAGE_UNCOVERED,
                reason=_uncovered_reason(index, pairs),
            )
        )
    return tuple(entries)


def _uncovered_reason(
    lesion_index: int,
    pairs: Sequence[LesionCandidateOutcome],
) -> str:
    """一处病灶没被盖住的中文说明 —— 说清「模型检出的部位是什么」。"""

    contradicted = [
        pair
        for pair in pairs
        if pair.lesion_index == lesion_index and pair.contradicted
    ]
    if contradicted:
        places = "、".join(
            _candidate_place(pair) for pair in contradicted if _candidate_place(pair)
        )
        if places:
            return f"模型检出的部位（{places}）与金标准这一处对不上，没有对应上的候选"
        return "模型检出的候选与金标准这一处对不上，没有对应上的候选"
    return "模型未在金标准这一处的部位检出候选"


def _candidate_place(pair: LesionCandidateOutcome) -> str:
    """一对里候选的部位（标准标签）—— 写进未覆盖说明里。"""

    outcome = pair.outcomes.get("location")
    if outcome is None:
        return ""
    return outcome.candidate_value or outcome.gold_value


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------
def _candidate_size(candidate: Any) -> float | None:
    """取候选的尺寸（认不出来就是 ``None``，不当 0）。"""

    value = getattr(candidate, "size_mm", None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _candidate_text(candidate: Any, field: str) -> str:
    """取候选的文本字段（部位 / 密度）。"""

    value = getattr(candidate, field, None)
    if value is None:
        return ""
    return str(value)
