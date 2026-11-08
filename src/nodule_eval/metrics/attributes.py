"""属性三层的比对规则（任务 27）：**尺寸容差 · 部位等价 · 密度等价**。

**这个模块解决什么**：存在性判完之后，「命中」这两个字还得经得起追问 ——
**同一处**结节，模型是不是把它**量对了、认对地方、认对密度**了？
这一层给出三条规则与它们各自的边界：

============  ==========================================  ==========================
属性          比较口径                                      边界
============  ==========================================  ==========================
尺寸          ``|模型尺寸 − 金标准尺寸| ≤ 容差``（默认 2mm）  **闭区间** —— 恰好等于
                                                            阈值算一致；浮点误差不翻脸
部位          两侧都经 ``labels.yaml`` 归一到标准标签再比      任一侧写法认不出来 →
                                                            **不可比**，不当一致也不当不一致
密度          同上                                            同上
============  ==========================================  ==========================

**为什么「不可比」必须单独一种结论**：厂商回「肺门区」、放射科写「右下肺野」，
两边写法都不在等价表里 —— 这时候**没有可比的对象**，不是「一致」也不是「不一致」。
把它悄悄当成一致，一次命中就白送；悄悄当成不一致，一次命中就被算成漏诊。
本模块把它落成第三种结论（``不可比``），**单独计数**，由判定层原样带出去
（任务 28 起把它与「多标签未覆盖」分开处理）。

**候选选择**：模型一回好几个候选时，拿哪个去比？
本模块按**可比的属性**给每个候选打分（部位 > 密度 > 尺寸，权重是经验值），
取分最高的那一个参与比对，并把**选择依据**写进判定依据字段 ——
「为什么用这个候选去比金标准」必须能一句话说清，否则复核的人无从下手。

**源产品对照（反面样本）**：源产品的胸片分类工具
（``github_ref/MedRAX/medrax/tools/classification.py`` 的 ``ChestXRayClassifierTool._run``）
返回的是 **18 类病理的概率字典** + 一个 ``analysis_status`` 字符串 ——
**没有部位、没有尺寸、没有密度**，也**没有金标准可比**。
属性层这一整套（容差比较、等价表归一、候选选择）在源产品里**没有对应物**，
是本产品为「可复算的模型评估」新建的一层。

**口径来源标注**：±2 mm 容差、部位/密度等价表的词条，都是**经验推断** ——
项目根 ``domain/`` 资产层四份文件（医保违规负面清单 / 处方审核规则体系 /
CHS-DRG 分组逻辑 / 医保结算清单字段规范）**均不覆盖医学影像 AI 模型评估口径**。
院内来源是放射科评估表模板与厂商说明书；**严禁虚构标准号与指南名**。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..contracts import DEFAULT_SIZE_TOLERANCE_MM
from ..errors import ErrorCode, NoduleEvalError
from ..goldstd import GoldLabels
from ..goldstd.labels import (
    LabelTable,
    find_density,
    find_location,
    normalize_density,
    normalize_location,
    resolve_density,
    resolve_location,
)

__all__ = [
    "ATTRIBUTE_WEIGHTS",
    "AttributeReport",
    "CandidateSelection",
    "LAYER_INCOMPARABLE",
    "LAYER_MATCH",
    "LAYER_MISMATCH",
    "LAYER_NOT_COMPARED",
    "LayerOutcome",
    "SIZE_EPSILON_MM",
    "candidate_summary",
    "compare_attributes",
    "compare_density",
    "compare_location",
    "compare_size",
    "normalize_tolerance",
    "select_candidate",
]


# --------------------------------------------------------------------------
# 逐层结论的四种取值
# --------------------------------------------------------------------------
#: 两侧一致。
LAYER_MATCH = "一致"
#: 两侧不一致（可比，但结论相反）。
LAYER_MISMATCH = "不一致"
#: **不可比** —— 有一侧写法认不出来，没有可比的对象。
#:
#: 与「不一致」的区别：不一致是**比过了、结论相反**；不可比是**没比成**。
#: 把它混进不一致，一次命中会被算成漏诊；混进一致，一次命中会白送。
LAYER_INCOMPARABLE = "不可比"
#: 没得比（金标准这一层没写 / 模型没给候选）。
LAYER_NOT_COMPARED = "未比对"


#: 尺寸容差比较的**浮点余量**（毫米）。
#:
#: 为什么需要它：``8.1 - 10.1`` 在 IEEE 双精度下是 ``-2.0000000000000018``，
#: 拿它跟 ``2.0`` 比会判成「超出容差」—— 数学上恰好等于阈值的尺寸被判不一致，
#: 边界上反复摇摆。阈值比较一律加这个余量。
SIZE_EPSILON_MM = 1e-9

#: 属性权重（**经验推断**）：部位 > 密度 > 尺寸。
#:
#: 用于**候选选择**打分：部位写错是最严重的错（认错地方），
#: 密度写错次之，尺寸差一点点最轻。权重写在代码里，改口径只改这一处。
ATTRIBUTE_WEIGHTS: Mapping[str, float] = {
    "location": 4.0,
    "density": 2.0,
    "size": 1.0,
}

#: 属性名 → 中文名（判定依据里用）。
_LAYER_NAMES: Mapping[str, str] = {
    "size": "尺寸",
    "location": "部位",
    "density": "密度",
}


def _contract_invalid(detail: str) -> NoduleEvalError:
    return NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail=detail)


def normalize_tolerance(value: float | None) -> float:
    """把容差规整成合法值（缺省 2mm）。

    容差是**本轮评估的可调参数**（``options.size_tolerance_mm``），
    负数或非有限值当场报错 —— 容差配错等于判定口径被悄悄改了。
    """

    if value is None:
        return DEFAULT_SIZE_TOLERANCE_MM
    try:
        tolerance = float(value)
    except (TypeError, ValueError) as exc:
        raise _contract_invalid(f"尺寸容差 size_tolerance_mm 须为数字：{value!r}") from exc
    if math.isnan(tolerance) or math.isinf(tolerance):
        raise _contract_invalid(f"尺寸容差 size_tolerance_mm 不得为无穷或空值：{value!r}")
    if tolerance < 0:
        raise _contract_invalid(f"尺寸容差 size_tolerance_mm 不得为负数：{tolerance}")
    return tolerance


# --------------------------------------------------------------------------
# 单层比对结论
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class LayerOutcome:
    """属性某一层的比对结论。

    ``layer``
        属性名（``size`` / ``location`` / ``density``）。
    ``status``
        四种结论之一：一致 / 不一致 / 不可比 / 未比对。
    ``gold_value`` / ``candidate_value``
        两侧的**归一后**取值（部位与密度是标准标签，尺寸是毫米）。
        不可比时保留**原文**，便于一眼看出「是哪句话认不出来」。
    ``delta_mm``
        尺寸差（模型 − 金标准）；只有尺寸层有。
    ``reason``
        一句中文说明 —— 判定依据里直接引用。
    """

    layer: str
    status: str
    gold_value: str = ""
    candidate_value: str = ""
    delta_mm: float | None = None
    reason: str = ""

    @property
    def layer_name_zh(self) -> str:
        return _LAYER_NAMES.get(self.layer, self.layer)

    @property
    def matched(self) -> bool:
        """这一层是否**一致**。"""

        return self.status == LAYER_MATCH

    @property
    def incomparable(self) -> bool:
        """这一层是否**不可比**。"""

        return self.status == LAYER_INCOMPARABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "status": self.status,
            "gold_value": self.gold_value,
            "candidate_value": self.candidate_value,
            "delta_mm": self.delta_mm,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------
# 三条比对规则
# --------------------------------------------------------------------------
def compare_size(
    gold_mm: float | None,
    candidate_mm: float | None,
    *,
    tolerance_mm: float | None = DEFAULT_SIZE_TOLERANCE_MM,
) -> LayerOutcome:
    """尺寸容差比较：**闭区间** —— 恰好等于阈值算一致。

    - 金标准没写尺寸 → **未比对**（没有可比的对象，不是「一致」）；
    - 模型没给尺寸 → **不可比**（模型这一侧缺东西）；
    - ``|模型 − 金标准| ≤ 容差`` → 一致；否则不一致。

    阈值比较带 :data:`SIZE_EPSILON_MM` 余量 —— ``8.1`` 与 ``10.1`` 在双精度下
    差出来是 ``2.0000000000000018``，不加余量会把「恰好等于阈值」判成超出。
    """

    tolerance = normalize_tolerance(tolerance_mm)

    if gold_mm is None:
        return LayerOutcome(
            layer="size",
            status=LAYER_NOT_COMPARED,
            reason="金标准未写尺寸，无从比起",
        )

    if candidate_mm is None:
        return LayerOutcome(
            layer="size",
            status=LAYER_INCOMPARABLE,
            gold_value=_fmt_mm(gold_mm),
            reason="模型未给出尺寸，尺寸这一层不可比",
        )

    delta = float(candidate_mm) - float(gold_mm)
    if abs(delta) <= tolerance + SIZE_EPSILON_MM:
        return LayerOutcome(
            layer="size",
            status=LAYER_MATCH,
            gold_value=_fmt_mm(gold_mm),
            candidate_value=_fmt_mm(candidate_mm),
            delta_mm=round(delta, 3),
            reason=(
                f"尺寸差 {abs(delta):.1f}mm，在容差 {tolerance:g}mm 内"
                f"（金标准 {_fmt_mm(gold_mm)} / 模型 {_fmt_mm(candidate_mm)}）"
            ),
        )

    return LayerOutcome(
        layer="size",
        status=LAYER_MISMATCH,
        gold_value=_fmt_mm(gold_mm),
        candidate_value=_fmt_mm(candidate_mm),
        delta_mm=round(delta, 3),
        reason=(
            f"尺寸差 {abs(delta):.1f}mm，超出容差 {tolerance:g}mm"
            f"（金标准 {_fmt_mm(gold_mm)} / 模型 {_fmt_mm(candidate_mm)}）"
        ),
    )


def compare_location(
    gold_location: str | None,
    candidate_position: str | None,
    *,
    table: LabelTable | None = None,
) -> LayerOutcome:
    """部位等价比较：两侧都归一到标准标签再比。

    - 金标准没写部位 → **未比对**；
    - 任一侧写法不在等价表里 → **不可比**（保留原文，写明哪一侧认不出来）；
    - 归一后相同 → 一致；否则不一致（两侧标准标签都留出来，便于核对）。

    模型侧的写法先按**术语**精确查表，查不到再**扫文本** ——
    有些厂商回的是「右下肺见一结节」这样带句子的话，扫文本能把部位捞出来。
    """

    if not gold_location or not str(gold_location).strip():
        return LayerOutcome(
            layer="location",
            status=LAYER_NOT_COMPARED,
            reason="金标准未写部位，无从比起",
        )

    gold_label = normalize_location(str(gold_location), table=table)
    if gold_label is None:
        return LayerOutcome(
            layer="location",
            status=LAYER_INCOMPARABLE,
            gold_value=str(gold_location),
            reason=f"金标准部位写法「{gold_location}」不在部位等价表里，不可比",
        )

    if not candidate_position or not str(candidate_position).strip():
        return LayerOutcome(
            layer="location",
            status=LAYER_INCOMPARABLE,
            gold_value=gold_label,
            reason="模型未给出部位，部位这一层不可比",
        )

    candidate_label = _normalize_candidate_location(str(candidate_position), table=table)
    if candidate_label is None:
        return LayerOutcome(
            layer="location",
            status=LAYER_INCOMPARABLE,
            gold_value=gold_label,
            candidate_value=str(candidate_position),
            reason=f"模型部位写法「{candidate_position}」不在部位等价表里，不可比",
        )

    if candidate_label == gold_label:
        return LayerOutcome(
            layer="location",
            status=LAYER_MATCH,
            gold_value=gold_label,
            candidate_value=candidate_label,
            reason=f"部位一致（{gold_label}）",
        )

    return LayerOutcome(
        layer="location",
        status=LAYER_MISMATCH,
        gold_value=gold_label,
        candidate_value=candidate_label,
        reason=f"部位不一致（金标准 {gold_label} / 模型 {candidate_label}）",
    )


def compare_density(
    gold_density: str | None,
    candidate_density: str | None,
    *,
    table: LabelTable | None = None,
) -> LayerOutcome:
    """密度等价比较：与部位同一套口径（归一到标准标签再比）。

    归不出来的写法 → **不可比**；金标准没写密度 → **未比对**。
    """

    if not gold_density or not str(gold_density).strip():
        return LayerOutcome(
            layer="density",
            status=LAYER_NOT_COMPARED,
            reason="金标准未写密度，无从比起",
        )

    gold_label = normalize_density(str(gold_density), table=table)
    if gold_label is None:
        return LayerOutcome(
            layer="density",
            status=LAYER_INCOMPARABLE,
            gold_value=str(gold_density),
            reason=f"金标准密度写法「{gold_density}」不在密度等价表里，不可比",
        )

    if not candidate_density or not str(candidate_density).strip():
        return LayerOutcome(
            layer="density",
            status=LAYER_INCOMPARABLE,
            gold_value=gold_label,
            reason="模型未给出密度，密度这一层不可比",
        )

    candidate_label = _normalize_candidate_density(str(candidate_density), table=table)
    if candidate_label is None:
        return LayerOutcome(
            layer="density",
            status=LAYER_INCOMPARABLE,
            gold_value=gold_label,
            candidate_value=str(candidate_density),
            reason=f"模型密度写法「{candidate_density}」不在密度等价表里，不可比",
        )

    if candidate_label == gold_label:
        return LayerOutcome(
            layer="density",
            status=LAYER_MATCH,
            gold_value=gold_label,
            candidate_value=candidate_label,
            reason=f"密度一致（{gold_label}）",
        )

    return LayerOutcome(
        layer="density",
        status=LAYER_MISMATCH,
        gold_value=gold_label,
        candidate_value=candidate_label,
        reason=f"密度不一致（金标准 {gold_label} / 模型 {candidate_label}）",
    )


# --------------------------------------------------------------------------
# 候选选择
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CandidateSelection:
    """选中的候选与选择依据。

    ``index``
        在候选序列里的下标（从 0 起）。
    ``candidate``
        选中的候选对象本身。
    ``score``
        打分（越大越接近金标准）。
    ``outcomes``
        选中候选的逐层结论。
    ``basis``
        一句中文依据 —— 「为什么用这个候选去比金标准」。
    ``tie_count``
        与它同分的候选个数（>1 说明选的是「同分里第一个」）。
    """

    index: int
    candidate: Any
    score: float
    outcomes: Mapping[str, LayerOutcome]
    basis: str
    tie_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "score": self.score,
            "basis": self.basis,
            "tie_count": self.tie_count,
            "outcomes": {name: outcome.to_dict() for name, outcome in self.outcomes.items()},
        }


def select_candidate(
    candidates: Sequence[Any],
    gold: GoldLabels,
    *,
    tolerance_mm: float | None = DEFAULT_SIZE_TOLERANCE_MM,
    table: LabelTable | None = None,
) -> CandidateSelection | None:
    """从多个候选里挑一个与金标准**最接近**的参与比对。

    打分口径（**经验推断**）：每一层按「一致 +权重 / 不一致 −权重 / 不可比或
    未比对 0」计分，权重见 :data:`ATTRIBUTE_WEIGHTS`（部位 > 密度 > 尺寸）。
    不可比不计分也不扣分 —— 它是「没比成」，不是「比不过」。

    同分时取**厂商返回顺序里的第一个**，并在依据里写明同分个数 ——
    「为什么偏偏挑了这个」必须说得清。
    """

    tolerance = normalize_tolerance(tolerance_mm)
    if not candidates:
        return None

    scored: list[tuple[int, Any, float, dict[str, LayerOutcome]]] = []
    for index, candidate in enumerate(candidates):
        outcomes = _candidate_outcomes(candidate, gold, tolerance_mm=tolerance, table=table)
        score = _score(outcomes)
        scored.append((index, candidate, score, outcomes))

    best_score = max(item[2] for item in scored)
    winners = [item for item in scored if item[2] == best_score]
    index, candidate, score, outcomes = winners[0]

    basis = _selection_basis(candidate, outcomes, len(candidates), len(winners))
    return CandidateSelection(
        index=index,
        candidate=candidate,
        score=score,
        outcomes=outcomes,
        basis=basis,
        tie_count=len(winners),
    )


def compare_attributes(
    gold: GoldLabels,
    candidates: Sequence[Any],
    *,
    tolerance_mm: float | None = DEFAULT_SIZE_TOLERANCE_MM,
    table: LabelTable | None = None,
) -> AttributeReport:
    """属性三层的比对总入口（供判定层调用）。

    候选为空 → 三层都是「未比对」（模型没检出东西，没有可比的对象）。
    """

    tolerance = normalize_tolerance(tolerance_mm)

    if not candidates:
        empty = LayerOutcome(layer=layer, status=LAYER_NOT_COMPARED, reason="模型未检出候选")
        return AttributeReport(
            size=empty,
            location=LayerOutcome(
                layer="location", status=LAYER_NOT_COMPARED, reason="模型未检出候选"
            ),
            density=LayerOutcome(
                layer="density", status=LAYER_NOT_COMPARED, reason="模型未检出候选"
            ),
            selection=None,
        )

    selection = select_candidate(
        candidates, gold, tolerance_mm=tolerance, table=table
    )
    assert selection is not None  # noqa: S101 —— 上面的分支已保证有候选
    outcomes = selection.outcomes
    return AttributeReport(
        size=outcomes["size"],
        location=outcomes["location"],
        density=outcomes["density"],
        selection=selection,
    )


@dataclass(frozen=True)
class AttributeReport:
    """属性三层的比对结果（逐层结论 + 候选选择）。

    ``incomparable_layers``
        判成「不可比」的层 —— **单独计数**，不混进一致/不一致。
    ``mismatched_layers``
        判成「不一致」的层。
    """

    size: LayerOutcome
    location: LayerOutcome
    density: LayerOutcome
    selection: CandidateSelection | None = None

    def outcomes(self) -> Mapping[str, LayerOutcome]:
        return {"size": self.size, "location": self.location, "density": self.density}

    @property
    def incomparable_layers(self) -> tuple[str, ...]:
        return tuple(
            name for name, outcome in self.outcomes().items() if outcome.incomparable
        )

    @property
    def mismatched_layers(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, outcome in self.outcomes().items()
            if outcome.status == LAYER_MISMATCH
        )

    @property
    def matched_layers(self) -> tuple[str, ...]:
        return tuple(
            name for name, outcome in self.outcomes().items() if outcome.matched
        )

    def notes(self) -> tuple[str, ...]:
        """逐层的中文说明（判定依据里直接引用）。"""

        return tuple(
            f"{outcome.layer_name_zh}：{outcome.reason}"
            for outcome in self.outcomes().values()
            if outcome.reason
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size.to_dict(),
            "location": self.location.to_dict(),
            "density": self.density.to_dict(),
            "incomparable_layers": list(self.incomparable_layers),
            "mismatched_layers": list(self.mismatched_layers),
            "selection": self.selection.to_dict() if self.selection is not None else None,
        }


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------
def _fmt_mm(value: float | None) -> str:
    """毫米值的统一写法（一位小数）。"""

    if value is None:
        return ""
    return f"{float(value):.1f}mm"


def _normalize_candidate_location(value: str, *, table: LabelTable | None = None) -> str | None:
    """厂商回的部位 → 标准标签：先按术语精确查表，再退回扫文本。

    精确命中不扫文本，是为了避免把「右肺下叶」这类词从一句话里捞出来时
    恰好捞成「右肺」—— 精确表里有就认精确的。
    """

    match = resolve_location(value, table=table)
    if match is not None:
        return match.canonical
    scanned = find_location(value, table=table)
    return scanned.canonical if scanned is not None else None


def _normalize_candidate_density(value: str, *, table: LabelTable | None = None) -> str | None:
    """厂商回的密度 → 标准标签（精确查表 → 扫文本）。"""

    match = resolve_density(value, table=table)
    if match is not None:
        return match.canonical
    scanned = find_density(value, table=table)
    return scanned.canonical if scanned is not None else None


def _candidate_outcomes(
    candidate: Any,
    gold: GoldLabels,
    *,
    tolerance_mm: float,
    table: LabelTable | None = None,
) -> dict[str, LayerOutcome]:
    """一个候选与金标准的逐层结论。"""

    return {
        "size": compare_size(
            gold.size_mm,
            _candidate_size(candidate),
            tolerance_mm=tolerance_mm,
        ),
        "location": compare_location(
            gold.location,
            _candidate_text(candidate, "position"),
            table=table,
        ),
        "density": compare_density(
            gold.density,
            _candidate_text(candidate, "density"),
            table=table,
        ),
    }


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


def _score(outcomes: Mapping[str, LayerOutcome]) -> float:
    """候选打分：一致 +权重，不一致 −权重，不可比 / 未比对 0。"""

    total = 0.0
    for name, outcome in outcomes.items():
        weight = ATTRIBUTE_WEIGHTS.get(name, 0.0)
        if outcome.status == LAYER_MATCH:
            total += weight
        elif outcome.status == LAYER_MISMATCH:
            total -= weight
    return total


def _selection_basis(
    candidate: Any,
    outcomes: Mapping[str, LayerOutcome],
    total: int,
    tie_count: int,
) -> str:
    """一句中文的选择依据。"""

    if total == 1:
        head = "模型只返回 1 个候选，直接与金标准比对"
    else:
        head = f"模型返回 {total} 个候选，按部位 > 密度 > 尺寸取最接近的一个"
        if tie_count > 1:
            head += f"（{tie_count} 个同分，取厂商返回顺序里的第一个）"

    parts = [outcome.reason for outcome in outcomes.values() if outcome.reason]
    detail = "；".join(parts) if parts else "无可比属性"
    return f"{head}：{detail}"


def _candidate_summary(candidate: Any) -> str:
    """候选的可读描述（落进判定依据）。"""

    position = _candidate_text(candidate, "position")
    size = _candidate_size(candidate)
    density = _candidate_text(candidate, "density")
    size_text = "-" if size is None else f"{size:.1f}mm"
    return f"{position} | {size_text} | {density}"


#: 候选的可读描述（公开给判定层用 —— 判定依据里要写「选了哪个候选」）。
candidate_summary = _candidate_summary
