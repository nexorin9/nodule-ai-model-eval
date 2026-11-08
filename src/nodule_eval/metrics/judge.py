"""判定引擎（任务 26）：**5 值互斥完备枚举**。

**这个模块解决什么**：把「金标准说了什么」与「模型检出了什么」比对成一句
**可以拿去复核的判定** —— 五个互斥的值之一，外加逐层判定依据。

**5 个值（取值真源在 :mod:`nodule_eval.contracts`）**：

===============  ==========================================================
判定             含义
===============  ==========================================================
``hit``          金标准有结节，模型也检出（存在性一致）
``miss``         金标准有结节，模型**未**检出 —— **漏诊**
``false_positive`` 金标准无结节，模型却检出 —— **误报**
``true_negative`` 金标准无结节，模型也未检出（存在性一致）
``failed``       这一例**没拿到结论**（调用失败 / 超时 / 标识不回显 / 响应缺字段）
===============  ==========================================================

**两条硬性质**（本模块的任务就是把���们做成机器可验的，而不是写在注释里）：

1. **互斥** —— 任一输入组合只落**一个**判定，不可能同时是 ``hit`` 又是 ``miss``；
2. **完备** —— 四值在**已评估样本**上构成完整的 2×2（四个格子各落一个四值，
   不重不漏），``failed`` 单独占一个出口，与四值**不相交**。

``failed`` 之所以要单列，是因为**没调成的调用没有结论**：
把它算成「未检出」，等于把一次漏检悄悄塞进特异度里。

**失败优先（红线）**：调用失败 / 超时 / ``study_uid`` 不回显 / 响应缺字段，
**一律先判 ``failed``**，不与四值混判 —— 哪怕这次调用恰好也检出了东西。

**源产品对照（反面样本）**：源产品的胸片分类工具
（``github_ref/MedRAX/medrax/tools/classification.py``）输出的是 18 类病理的
**概率字典** + 一个 ``analysis_status`` 字符串；它**没有金标准可比**，
而且失败时把 ``{"error": ...}`` 塞进**同一个返回形状**里 ——
调用方若不逐个查 ``analysis_status``，就会把「工具跑挂了」当成
「概率为 0 即没有病灶」。本模块把这件事显式化：
**失败是第五个判定值，不是第四个值的一个特例**。

**任务 26 的范围**：只落**存在性这一层**。
属性三层（尺寸容差 / 部位等价 / 密度等价）在任务 27 落，
多标签最严口径在任务 28 落 —— 两者都在**存在性层之下**：
存在性不一致时直接定案，不再细看属性。

**任务 27 已把属性三层接进来**（:mod:`nodule_eval.metrics.attributes`）：
存在性**一致**时（两边都说「有病灶」）才逐层比尺寸 / 部位 / 密度，
每层给出四种结论之一 —— 一致 / 不一致 / **不可比** / 未比对。
「不可比」（有一侧写法不在等价表里）**单独计数**，不当一致也不当不一致 ——
把它当成一致是一次命中白送，当成不一致是一次命中被算成漏诊。
多候选时先按「部位 > 密度 > 尺寸」选一个最接近的参与比对，
**选择依据**落进判定依据字段。

**任务 28 已把多标签最严口径接进来**（:mod:`nodule_eval.metrics.strict`）：
金标准写了 **N 处**病灶，模型就得**盖住 N 处** —— 任一处没盖住即判 ``miss``，
没盖住的那几处逐条记进 ``uncovered_lesions``（任务 30 的漏诊原因文案直接引用）。
**部位是病灶的身份**：部位一致才算对应，部位不一致即不对应；
部位认不出来时密度 / 尺寸只作**佐证**（一致可支持对应，不一致**不**否对应）——
属性对不上不等于「不是那一处」，把 8mm 量成 9mm 判成漏诊是把测量偏差当成没看见。
「对应关系判不出来」单列**不可比**，既不当命中也不当漏诊。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from ..contracts import (
    DEFAULT_SIZE_TOLERANCE_MM,
    JUDGED_VERDICTS,
    VERDICT_FALSE_POSITIVE,
    VERDICT_FAILED,
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_TRUE_NEGATIVE,
    VERDICTS,
)
from ..errors import ErrorCode, NoduleEvalError, error_text
from ..goldstd import GoldLabels
from .attributes import (
    LAYER_INCOMPARABLE,
    LAYER_MATCH,
    LAYER_MISMATCH,
    LAYER_NOT_COMPARED,
    AttributeReport,
    candidate_summary,
    compare_attributes,
)
from .strict import (
    COVERAGE_COVERED,
    COVERAGE_INCOMPARABLE,
    COVERAGE_UNCOVERED,
    CoverageReport,
    LesionCoverage,
    compare_coverage,
)

__all__ = [
    "COVERAGE_COVERED",
    "COVERAGE_INCOMPARABLE",
    "COVERAGE_UNCOVERED",
    "CompletenessReport",
    "FAILURE_ERROR_CODES",
    "Judgement",
    "LAYER_INCOMPARABLE",
    "LAYER_MATCH",
    "LAYER_MISMATCH",
    "LAYER_NOT_COMPARED",
    "PRESENCE_MATRIX",
    "VERDICT_DEFINITIONS",
    "VerdictDefinition",
    "VerdictOutcome",
    "assert_verdict_completeness",
    "check_verdict_completeness",
    "enumerate_verdict_outcomes",
    "judge_case",
]


# --------------------------------------------------------------------------
# 5 值枚举的声明
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class VerdictDefinition:
    """一个判定值的完整声明。

    声明写成数据而不是注释 —— 枚举的**完备性要能被程序核对**
    （见 :func:`check_verdict_completeness`），注释只能给人看。
    """

    code: str
    name_zh: str
    condition: str
    counts_toward_metrics: bool


#: 五个判定值的声明（顺序即「先四值、后 failed」）。
VERDICT_DEFINITIONS: Mapping[str, VerdictDefinition] = {
    VERDICT_HIT: VerdictDefinition(
        code=VERDICT_HIT,
        name_zh="命中",
        condition="金标准存在性=有病灶，模型检出候选",
        counts_toward_metrics=True,
    ),
    VERDICT_MISS: VerdictDefinition(
        code=VERDICT_MISS,
        name_zh="漏诊",
        condition="金标准存在性=有病灶，模型未检出候选",
        counts_toward_metrics=True,
    ),
    VERDICT_FALSE_POSITIVE: VerdictDefinition(
        code=VERDICT_FALSE_POSITIVE,
        name_zh="误报",
        condition="金标准存在性=无病灶，模型检出候选",
        counts_toward_metrics=True,
    ),
    VERDICT_TRUE_NEGATIVE: VerdictDefinition(
        code=VERDICT_TRUE_NEGATIVE,
        name_zh="真阴性",
        condition="金标准存在性=无病灶，模型未检出候选",
        counts_toward_metrics=True,
    ),
    VERDICT_FAILED: VerdictDefinition(
        code=VERDICT_FAILED,
        name_zh="失败",
        condition=(
            "调用失败 / 超时 / study_uid 不回显 / 响应缺字段 / 金标准存在性读不出来"
            "——这一例没拿到结论，不进指标分母"
        ),
        counts_toward_metrics=False,
    ),
}


#: **存在性 2×2 真值表** —— 四值判定的唯一真源。
#:
#: 键是 ``(金标准存在性, 模型是否检出)``，值是对应的判定。
#: 判定逻辑**直接查这张表** —— 表与行为不会各写一份、不会漂移。
PRESENCE_MATRIX: Mapping[tuple[bool, bool], str] = {
    (True, True): VERDICT_HIT,
    (True, False): VERDICT_MISS,
    (False, True): VERDICT_FALSE_POSITIVE,
    (False, False): VERDICT_TRUE_NEGATIVE,
}


#: 判 ``failed`` 的错误码 —— 这些码出现即「这一例没拿到结论」。
#:
#: **收窄到「这一例的调用本身没成」**：模型归并降级（``MODEL_UNAVAILABLE`` /
#: ``MODEL_REQUEST_REJECTED`` / ``RULE_DEGRADED``）**不在此列** ——
#: 降级例的结论由规则基线给出，是**有结论的**，仍进指标分母
#: （口径见 :func:`nodule_eval.contracts.counts_toward_denominator`）。
FAILURE_ERROR_CODES: frozenset[str] = frozenset(
    {
        ErrorCode.INFERENCE_4XX.value,
        ErrorCode.INFERENCE_5XX.value,
        ErrorCode.INFERENCE_TIMEOUT.value,
        ErrorCode.STUDY_UID_MISMATCH.value,
        ErrorCode.FIELD_MAPPING_MISSING.value,
    }
)


# --------------------------------------------------------------------------
# 判定结果
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Judgement:
    """一例的判定结果与判定依据。

    ``verdict``
        五值判定之一（``hit`` / ``miss`` / ``false_positive`` /
        ``true_negative`` / ``failed``）。
    ``reason``
        中文判定依据 —— 放射科直接看这一句就知道「凭什么这么判」。
    ``layers``
        逐层结论（存在性 / 尺寸 / 部位 / 密度）。存在性层由 2×2 真值表给出；
        属性三层在存在性一致时才有结论（任务 27）。
    ``error_code``
        判 ``failed`` 时的错误码；判出结论时为空。
    ``detected``
        模型这一侧的存在性（是否检出候选）。与 ``gold_presence`` 并排就是
        2×2 真值表的输入 —— 复核时一眼能对上落在哪一格。
    ``incomparable_layers``
        判成「不可比」的属性层（任务 27）。**单独计数** ——
        有一侧写法不在等价表里时没有可比的对象，既不当一致也不当不一致。
    ``incomparable_reason``
        不可比的说明（中文），写明是哪一侧、哪个写法认不出来。
    ``selected_candidate``
        参与比对的候选（可读描述）—— 多候选时凭什么挑它。
    ``selection_basis``
        候选选择的依据（中文）。
    ``attribute_notes``
        属性三层的逐层说明（中文），复核时一眼看到比到哪一层、比出了什么。
    ``lesion_total`` / ``lesion_covered``
        **多标签最严口径**（任务 28）：金标准写了几处病灶、模型盖住了几处。
    ``coverage_state``
        覆盖结论：已覆盖（全覆盖）/ 未覆盖（部分覆盖）/ 不可比 / 未比对。
    ``uncovered_lesions``
        **没被盖住的病灶**（逐处，带原因）—— 任务 30 的漏诊原因文案用它。
    ``incomparable_lesions``
        对应关系**判不出来**的病灶（写法认不出）—— 单列不可比，
        既不当命中也不当漏诊。
    ``coverage_notes``
        逐处覆盖的中文说明。
    """

    verdict: str
    reason: str
    presence_layer: str = LAYER_NOT_COMPARED
    size_layer: str = LAYER_NOT_COMPARED
    location_layer: str = LAYER_NOT_COMPARED
    density_layer: str = LAYER_NOT_COMPARED
    gold_presence: bool | None = None
    detected: bool | None = None
    candidate_count: int = 0
    error_code: str = ""
    incomparable_layers: tuple[str, ...] = ()
    incomparable_reason: str = ""
    selected_candidate: str = ""
    selection_basis: str = ""
    attribute_notes: tuple[str, ...] = ()
    lesion_total: int = 0
    lesion_covered: int = 0
    coverage_state: str = ""
    uncovered_lesions: tuple[LesionCoverage, ...] = ()
    incomparable_lesions: tuple[LesionCoverage, ...] = ()
    coverage_notes: tuple[str, ...] = ()

    def layers(self) -> dict[str, str]:
        """逐层结论（落进逐例产物，复核时一眼看到比到哪一层）。"""

        return {
            "presence": self.presence_layer,
            "size": self.size_layer,
            "location": self.location_layer,
            "density": self.density_layer,
        }

    def has_incomparable(self) -> bool:
        """这一例有没有「不可比」。

        两种来源：

        - **层不可比**：某一层的写法认不出来（任务 27）；
        - **覆盖不可比**：有一处病灶的对应关系判不出来（任务 28）。

        两者的共同后果：**既不当命中也不当漏诊** —— 单独计数并标注，
        不进 TP / FP / FN / TN。
        """

        return bool(self.incomparable_layers) or bool(self.incomparable_lesions)

    def basis_text(self) -> str:
        """判定依据的一行文本 —— 落进逐例行的 ``judge_basis`` 字段。

        四层结论（存在性 / 尺寸 / 部位 / 密度）+ 覆盖结论，
        一行说完「这一例凭什么这么判」。
        """

        parts = [
            f"存在性：{self.presence_layer}",
            f"部位：{self.location_layer}",
            f"尺寸：{self.size_layer}",
            f"密度：{self.density_layer}",
        ]
        if self.coverage_state:
            parts.append(
                f"覆盖：{self.coverage_state}"
                f"（金标准 {self.lesion_total} 处病灶，模型盖住 {self.lesion_covered} 处）"
            )
        return "；".join(parts)

    def uncovered_text(self) -> str:
        """没被盖住的病灶的一行中文说明（任务 30 的漏诊原因文案直接引用）。"""

        if not self.uncovered_lesions:
            return ""
        return "；".join(
            f"{entry.label_text}（{entry.reason}）" for entry in self.uncovered_lesions
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "layers": self.layers(),
            "gold_presence": self.gold_presence,
            "detected": self.detected,
            "candidate_count": self.candidate_count,
            "error_code": self.error_code,
            "incomparable_layers": list(self.incomparable_layers),
            "incomparable_reason": self.incomparable_reason,
            "selected_candidate": self.selected_candidate,
            "selection_basis": self.selection_basis,
            "attribute_notes": list(self.attribute_notes),
            "lesion_total": self.lesion_total,
            "lesion_covered": self.lesion_covered,
            "coverage_state": self.coverage_state,
            "uncovered_lesions": [entry.to_dict() for entry in self.uncovered_lesions],
            "incomparable_lesions": [entry.to_dict() for entry in self.incomparable_lesions],
            "coverage_notes": list(self.coverage_notes),
            "basis": self.basis_text(),
        }


# --------------------------------------------------------------------------
# 判定入口
# --------------------------------------------------------------------------
def judge_case(
    gold: GoldLabels,
    candidates: Sequence[Any] = (),
    *,
    error: str = "",
    error_code: str = "",
    size_tolerance_mm: float | None = DEFAULT_SIZE_TOLERANCE_MM,
    table: Any | None = None,
) -> Judgement:
    """比对一例的金标准标签与模型候选，给出判定与判定依据。

    参数
    ----
    gold
        金标准归并结果（:class:`~nodule_eval.goldstd.GoldLabels`）。
    candidates
        模型检出的候选病灶（``adapters.inference.Candidate`` 序列）。
    error
        调用失败时的失败原因。为空表示调用成功。
    error_code
        调用失败时的错误码。
    size_tolerance_mm
        尺寸容差（毫米，默认 2.0，可配 ``options.size_tolerance_mm``）。
        闭区间 —— 恰好等于阈值算一致。
    table
        部位 / 密度等价表（缺省用内置的那一张）。

    **失败优先**：调用失败一律判 ``failed``，不与四值判定混判 ——
    「没调成」和「调成了但没检出」是两回事，混在一起敏感度就废了。

    **存在性第一层**：存在性不一致时直接定案，不再细看属性 ——
    属性层（任务 27）与多标签最严口径（任务 28）都在存在性层之下。

    **属性三层（任务 27）**：存在性**一致**且两边都说「有病灶」时，
    才逐层比尺寸 / 部位 / 密度，并把每层结论（一致 / 不一致 / **不可比** /
    未比对）与候选选择依据一并带出。

    **多标签最严口径（任务 28）**：存在性一致之后，再逐处比「金标准那几处
    病灶，模型盖住了几处」—— **任一处没盖住即判 ``miss``**，并记下
    没盖住的是哪一处（部位 / 尺寸 / 密度）与为什么。
    """

    count = len(candidates)

    # ---- 失败优先（红线）：任何失败信息先于四值判定 ----
    failure = _failure_judgement(gold, count, error=error, error_code=error_code)
    if failure is not None:
        return failure

    if gold.presence is None:
        # 金标准的存在性**读不出来** —— 这一例没拿到判定。
        # 判 failed 而不是猜一个：猜出来的存在性会直接改写敏感度。
        return Judgement(
            verdict=VERDICT_FAILED,
            reason=(
                "金标准原文未能判定病灶存在性（既无肯定表述也无否定表述），"
                "已挂人工复核，不计入指标"
            ),
            presence_layer=LAYER_NOT_COMPARED,
            gold_presence=None,
            detected=count > 0,
            candidate_count=count,
            error_code=ErrorCode.RULE_DEGRADED.value,
        )

    detected = count > 0
    # ---- 存在性 2×2：查表定案（表是唯一真源，逻辑与表不会漂移）----
    verdict = PRESENCE_MATRIX[(gold.presence, detected)]
    reason = _presence_reason(verdict, gold.presence, count)

    judgement = Judgement(
        verdict=verdict,
        reason=reason,
        presence_layer=LAYER_MATCH if gold.presence == detected else LAYER_MISMATCH,
        gold_presence=gold.presence,
        detected=detected,
        candidate_count=count,
    )

    # ---- 属性三层（任务 27）：只在「两边都说有病灶」时才有得比 ----
    if verdict != VERDICT_HIT:
        return judgement

    report = compare_attributes(
        gold, candidates, tolerance_mm=size_tolerance_mm, table=table
    )
    judgement = _with_attributes(judgement, report)

    # ---- 多标签最严口径（任务 28）：金标准写了 N 处，模型就得盖住 N 处 ----
    return _with_strict_coverage(
        judgement,
        gold,
        candidates,
        tolerance_mm=size_tolerance_mm,
        table=table,
    )


def _with_strict_coverage(
    judgement: Judgement,
    gold: GoldLabels,
    candidates: Sequence[Any],
    *,
    tolerance_mm: float | None,
    table: Any | None,
) -> Judgement:
    """把**多标签最严口径**的覆盖结论并进判定结果。

    三种结局（互斥）：

    - **全覆盖** → 维持 ``hit``；
    - **部分覆盖**（有病灶没被盖住）→ 改判 ``miss``，并把**没盖住的那几处**
      逐条记进 ``uncovered_lesions``（任务 30 的漏诊原因文案直接引用）；
    - **有病灶对应关系判不出来** → 维持判定值，但该例记**不可比**，
      既不当命中也不当漏诊（单列计数，不进 TP / FP / FN / TN）。

    **失败优先不受影响**：失败例在上一层就返回了，走不到这里。
    """

    coverage = compare_coverage(
        gold, candidates, tolerance_mm=tolerance_mm, table=table
    )
    uncovered = coverage.uncovered
    incomparable = coverage.incomparable

    verdict = judgement.verdict
    if uncovered:
        state = COVERAGE_UNCOVERED
        # **最严口径**：金标准写了 N 处，模型没盖住其中一处 → 判漏诊。
        verdict = VERDICT_MISS
    elif incomparable:
        state = COVERAGE_INCOMPARABLE
    else:
        state = COVERAGE_COVERED

    reason = judgement.reason
    if uncovered:
        labels = "、".join(entry.label_text for entry in uncovered)
        reason = (
            f"{reason}；金标准 {coverage.total} 处病灶，模型只盖住 "
            f"{coverage.covered} 处，按最严口径判漏诊"
            f"（未覆盖：{labels}）"
        )
    elif incomparable:
        reason = (
            f"{reason}；{coverage.total} 处病灶中有 {len(incomparable)} 处"
            "对应关系判不出来（写法认不出），本例不计入 TP/FP/FN/TN"
        )

    return Judgement(
        verdict=verdict,
        reason=reason,
        presence_layer=judgement.presence_layer,
        size_layer=judgement.size_layer,
        location_layer=judgement.location_layer,
        density_layer=judgement.density_layer,
        gold_presence=judgement.gold_presence,
        detected=judgement.detected,
        candidate_count=judgement.candidate_count,
        error_code=judgement.error_code,
        incomparable_layers=judgement.incomparable_layers,
        incomparable_reason=judgement.incomparable_reason,
        selected_candidate=judgement.selected_candidate,
        selection_basis=judgement.selection_basis,
        attribute_notes=judgement.attribute_notes,
        lesion_total=coverage.total,
        lesion_covered=coverage.covered,
        coverage_state=state,
        uncovered_lesions=uncovered,
        incomparable_lesions=incomparable,
        coverage_notes=coverage.notes(),
    )


def _with_attributes(judgement: Judgement, report: AttributeReport) -> Judgement:
    """把属性三层的结论与候选选择依据并进判定结果。"""

    notes = report.notes()
    incomparable = report.incomparable_layers
    incomparable_reason = "；".join(
        outcome.reason
        for outcome in report.outcomes().values()
        if outcome.incomparable and outcome.reason
    )

    reason = judgement.reason
    if notes:
        reason = f"{reason}（{'；'.join(notes)}）"

    return Judgement(
        verdict=judgement.verdict,
        reason=reason,
        presence_layer=judgement.presence_layer,
        size_layer=report.size.status,
        location_layer=report.location.status,
        density_layer=report.density.status,
        gold_presence=judgement.gold_presence,
        detected=judgement.detected,
        candidate_count=judgement.candidate_count,
        error_code=judgement.error_code,
        incomparable_layers=incomparable,
        incomparable_reason=incomparable_reason,
        selected_candidate=_candidate_text(report),
        selection_basis=report.selection.basis if report.selection is not None else "",
        attribute_notes=notes,
    )


def _candidate_text(report: AttributeReport) -> str:
    """选中候选的可读描述（部位 | 尺寸 | 密度）。"""

    if report.selection is None:
        return ""
    return candidate_summary(report.selection.candidate)


def _failure_judgement(
    gold: GoldLabels,
    count: int,
    *,
    error: str,
    error_code: str,
) -> Judgement | None:
    """失败优先判定：这一例没拿到结论就返回 ``failed`` 判定，否则返回 ``None``。

    「没拿到结论」有两种形态：

    - 带了失败原因（``error``）—— 引擎侧的失败收口已经把话编好了；
    - 带了失败错误码（``error_code``）但没带原因 —— 原因**从错误码表取**
      （同一张表被终端输出、表格 ``failure_reason`` 列与产物共用，
      业务代码不硬编码文案）。

    降级码（``MODEL_UNAVAILABLE`` / ``MODEL_REQUEST_REJECTED`` /
    ``RULE_DEGRADED``）**不算失败** —— 那一层没跑成，结论由规则基线给出，
    仍然是有结论的一例。
    """

    code = str(error_code or "")
    if code and code not in FAILURE_ERROR_CODES:
        # 降级码或非调用类错误码：不是「这一例没拿到结论」，继续往下判。
        if not error:
            return None

    if not error and not code:
        return None

    reason = error or error_text(code)
    return Judgement(
        verdict=VERDICT_FAILED,
        reason=reason,
        presence_layer=LAYER_NOT_COMPARED,
        gold_presence=gold.presence,
        detected=count > 0,
        candidate_count=count,
        error_code=code,
    )


def _presence_reason(verdict: str, gold_presence: bool, count: int) -> str:
    """四值判定的中文依据 —— 一句话说清「凭什么这么判」。"""

    if verdict == VERDICT_HIT:
        return f"金标准存在性=有病灶，模型检出 {count} 个候选，存在性一致"
    if verdict == VERDICT_MISS:
        return "金标准存在性=有病灶，模型未检出候选，存在性不一致（漏诊）"
    if verdict == VERDICT_FALSE_POSITIVE:
        return f"金标准存在性=无病灶，模型检出 {count} 个候选，存在性不一致（误报）"
    if verdict == VERDICT_TRUE_NEGATIVE:
        return "金标准存在性=无病灶，模型未检出候选，存在性一致"
    raise ValueError(f"未知判定：{verdict}（金标准存在性={gold_presence}）")


# --------------------------------------------------------------------------
# 完备性与互斥性的机器可验部分（任务 26 的验收物）
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class VerdictOutcome:
    """输入空间某一格的判定结果。

    ``failed``
        这一格是不是「调用失败」。
    ``gold_presence``
        金标准存在性（``None`` = 读不出来）。
    ``detected``
        模型这一侧是否检出候选。
    ``verdict``
        这一格落到的判定。
    """

    failed: bool
    gold_presence: bool | None
    detected: bool
    verdict: str

    @property
    def combo_key(self) -> str:
        """四值格的标识（``金标准存在性=T/F`` × ``模型检出=T/F``）。"""

        presence = "T" if self.gold_presence else ("F" if self.gold_presence is False else "?")
        return f"gold={presence},detected={'T' if self.detected else 'F'}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "failed": self.failed,
            "gold_presence": self.gold_presence,
            "detected": self.detected,
            "verdict": self.verdict,
            "combo_key": self.combo_key,
        }


def _sample_candidate() -> Any:
    """枚举时用的候选占位（本层只看「有没有检出」，不看候选属性）。"""

    return SimpleNamespace(position="右肺下叶", size_mm=8.0, density="磨玻璃", confidence=0.9)


def enumerate_verdict_outcomes() -> tuple[VerdictOutcome, ...]:
    """遍历判定输入空间的**全部组合**，返回每一格落到的判定。

    输入空间 = {调用失败: 是/否} × {金标准存在性: 有/无/读不出} × {模型检出: 是/否}
    = 2 × 3 × 2 = **12 格**。

    这 12 格是「完备性」与「互斥性」的**判定依据**：

    - **完备** —— 四值格（未失败 × 存在性可读）四个格子**不重不漏**；
    - **互斥** —— 每格只落一个值，``failed`` 与四值**不相交**；
    - **失败优先** —— 每个四值格都有对应的失败格，失败格一律落 ``failed``。

    这三条不是靠注释保证的，是靠**真的跑一遍判定**核对出来的。
    """

    outcomes: list[VerdictOutcome] = []
    for failed in (False, True):
        for presence in (True, False, None):
            for detected in (False, True):
                gold = GoldLabels(presence=presence)
                candidates: tuple[Any, ...] = (_sample_candidate(),) if detected else ()
                judgement = judge_case(
                    gold,
                    candidates,
                    error="调用失败（完备性枚举用）" if failed else "",
                    error_code=ErrorCode.INFERENCE_5XX.value if failed else "",
                )
                outcomes.append(
                    VerdictOutcome(
                        failed=failed,
                        gold_presence=presence,
                        detected=detected,
                        verdict=judgement.verdict,
                    )
                )
    return tuple(outcomes)


@dataclass(frozen=True)
class CompletenessReport:
    """5 值枚举的完备性核对结果。

    ``four_value_matrix``
        四值格上的判定（键是 ``gold=T/F,detected=T/F``）。
    ``failed_combinations``
        落 ``failed`` 的格数。
    ``verdicts_covered``
        全部 12 格覆盖到的判定集合。
    ``mutually_exclusive``
        四值格上四个判定互不重复，且 ``failed`` 与四值不相交。
    ``four_value_bijective``
        四值格与四值判定之间是**一一对应**（不重不漏）。
    ``failed_priority``
        每个四值格加上失败信息后都改判 ``failed``。
    ``complete``
        以上全绿 —— 5 个判定值都可达，四值在已评估样本上构成完备 2×2。
    """

    total_combinations: int
    four_value_matrix: Mapping[str, str]
    failed_combinations: int
    verdicts_covered: tuple[str, ...]
    mutually_exclusive: bool
    four_value_bijective: bool
    failed_priority: bool
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_combinations": self.total_combinations,
            "four_value_matrix": dict(self.four_value_matrix),
            "failed_combinations": self.failed_combinations,
            "verdicts_covered": list(self.verdicts_covered),
            "mutually_exclusive": self.mutually_exclusive,
            "four_value_bijective": self.four_value_bijective,
            "failed_priority": self.failed_priority,
            "complete": self.complete,
        }


def check_verdict_completeness() -> CompletenessReport:
    """真的把判定跑一遍，核对 5 值枚举的互斥性与完备性。

    这不是「检查一份声明」，而是**检查行为** —— 声明改了但行为没改，
    这里会立刻报出来。
    """

    outcomes = enumerate_verdict_outcomes()

    four_value: dict[str, str] = {}
    for outcome in outcomes:
        if outcome.failed or outcome.gold_presence is None:
            continue
        four_value[outcome.combo_key] = outcome.verdict

    covered = tuple(sorted({outcome.verdict for outcome in outcomes}))

    # 四值格 ↔ 四值判定 一一对应
    four_value_bijective = (
        len(four_value) == 4 and set(four_value.values()) == set(JUDGED_VERDICTS)
    )

    # 互斥：四值格上的判定互不重复；failed 与四值不相交
    mutually_exclusive = (
        len(set(four_value.values())) == len(four_value)
        and VERDICT_FAILED not in set(four_value.values())
    )

    # 失败优先：每个四值格加上失败信息后都改判 failed
    failed_priority = True
    for outcome in outcomes:
        if outcome.failed or outcome.gold_presence is None:
            continue
        failed_twin = judge_case(
            GoldLabels(presence=outcome.gold_presence),
            (_sample_candidate(),) if outcome.detected else (),
            error="调用失败（完备性枚举用）",
            error_code=ErrorCode.INFERENCE_5XX.value,
        )
        if failed_twin.verdict != VERDICT_FAILED:
            failed_priority = False
            break

    failed_combinations = sum(1 for outcome in outcomes if outcome.verdict == VERDICT_FAILED)

    complete = (
        four_value_bijective
        and mutually_exclusive
        and failed_priority
        and set(covered) == set(VERDICTS)
    )

    return CompletenessReport(
        total_combinations=len(outcomes),
        four_value_matrix=four_value,
        failed_combinations=failed_combinations,
        verdicts_covered=covered,
        mutually_exclusive=mutually_exclusive,
        four_value_bijective=four_value_bijective,
        failed_priority=failed_priority,
        complete=complete,
    )


def assert_verdict_completeness() -> CompletenessReport:
    """核对 5 值枚举的完备性；不满足就抛 ``CONTRACT_INVALID``。

    这条自检挂在判定引擎的门口 —— 枚举一旦被改坏（漏一格、两个格子撞成
    同一个值、失败不再优先），立刻当场报错，而不是等到汇总行才发现
    敏感度算错了。
    """

    report = check_verdict_completeness()
    if not report.complete:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            (
                "判定枚举不满足互斥完备："
                f"四值格={report.four_value_matrix}，"
                f"覆盖={report.verdicts_covered}，"
                f"互斥={report.mutually_exclusive}，"
                f"一一对应={report.four_value_bijective}，"
                f"失败优先={report.failed_priority}"
            ),
        )
    return report
