"""判定层：把金标准标签与模型候选比对成判定。

**实现落在 :mod:`nodule_eval.metrics.judge`**（任务 26）——
本模块只是门面，判定引擎、5 值枚举声明与完备性核对都在那边。
**属性三层的比对规则落在 :mod:`nodule_eval.metrics.attributes`**（任务 27）——
尺寸容差、部位等价、密度等价与候选选择都在那边。
引擎侧照旧 ``from .metrics import Judgement, judge_case``，用法不变。

**这一层管什么**：一例该怎么判 —— 命中 / 漏诊 / 误报 / 真阴性 / 失败。

**一条红线**：任何一类失败都判 ``failed``，**绝不落成「未检出」**。
把一次没调成的调用算成阴性，等于把一次漏检悄悄塞进特异度里。

**任务 26 落「存在性」一层**：金标准说有病灶、模型也检出 → 命中；
金标准说有、模型没检出 → 漏诊；金标准说没有、模型检出 → 误报；
两边都没有 → 真阴性；任何失败 → 失败。

**任务 27 落「属性」三层**（存在性一致时才比）：尺寸按容差比较（闭区间）、
部位与密度按等价表归一后比较；某一侧写法认不出来 → **不可比**
（单独计数，既不当一致也不当不一致）；模型回多个候选时先选一个最接近的，
**选择依据**落进判定依据。

**任务 28 落「多标签最严口径」**（属性层之下）：金标准写了 N 处病灶，
模型就得盖住 N 处 —— 任一处没盖住即判 ``miss``，没盖住的那几处逐条记录；
「对应关系判不出来」单列**不可比**，既不当命中也不当漏诊。
本模块把**每一层的结论都记下来** —— 判定依据要能一眼看出
「这一例是存在性不一致，还是属性层没比，还是有一处病灶没盖住」。

**任务 29 落「指标计算」**（实现见 :mod:`nodule_eval.metrics.compute`）：
敏感度 / 特异度 / 阳性预测值 / 阴性预测值 + 原始计数 + **null 语义**
（分母为 0 时输出 ``null`` 并写明原因，**绝不输出 0**），
外加**手算复核入口** —— 给定逐例行与汇总指标可反算回 TP/FP/FN/TN 并逐项核对。
"""

from __future__ import annotations

from .attributes import (
    ATTRIBUTE_WEIGHTS,
    AttributeReport,
    CandidateSelection,
    LAYER_INCOMPARABLE,
    LAYER_MATCH,
    LAYER_MISMATCH,
    LAYER_NOT_COMPARED,
    LayerOutcome,
    SIZE_EPSILON_MM,
    candidate_summary,
    compare_attributes,
    compare_density,
    compare_location,
    compare_size,
    normalize_tolerance,
    select_candidate,
)
from .compute import (
    METRIC_DECIMALS,
    MetricValue,
    MetricsAudit,
    RawCounts,
    assert_metrics_consistent,
    audit_metrics,
    compute_metrics,
    compute_metrics_from_rows,
    count_model_normalized,
    count_raw_counts,
    count_rule_degraded,
    format_metric_value,
    metric_values,
    npv_value,
    ppv_value,
    sensitivity_value,
    specificity_value,
    summary_row_dict,
    summary_row_text,
)
from .judge import (
    COVERAGE_COVERED,
    COVERAGE_INCOMPARABLE,
    COVERAGE_UNCOVERED,
    CompletenessReport,
    FAILURE_ERROR_CODES,
    Judgement,
    PRESENCE_MATRIX,
    VERDICT_DEFINITIONS,
    VerdictDefinition,
    VerdictOutcome,
    assert_verdict_completeness,
    check_verdict_completeness,
    enumerate_verdict_outcomes,
    judge_case,
)
from .report import (
    MISSED_LIST_COLUMNS,
    assert_summary_consistent,
    build_missed_list,
    build_summary_payload,
    gold_summary_text,
    lesion_summary_text,
    layer_conclusions,
    manual_check_block,
    missed_not_counted,
    missed_reason_text,
    read_missed_list_csv,
    verdict_distribution,
    write_missed_list_csv,
)
from .strict import (
    CORRESPONDENCE_CONTRADICTED,
    CORRESPONDENCE_MATCHED,
    CORRESPONDENCE_UNDECIDED,
    CoverageReport,
    LesionCandidateOutcome,
    LesionCoverage,
    compare_coverage,
    gold_lesions,
    lesion_label_text,
)

__all__ = [
    "ATTRIBUTE_WEIGHTS",
    "AttributeReport",
    "COVERAGE_COVERED",
    "COVERAGE_INCOMPARABLE",
    "COVERAGE_UNCOVERED",
    "CORRESPONDENCE_CONTRADICTED",
    "CORRESPONDENCE_MATCHED",
    "CORRESPONDENCE_UNDECIDED",
    "CandidateSelection",
    "CompletenessReport",
    "CoverageReport",
    "FAILURE_ERROR_CODES",
    "Judgement",
    "LAYER_INCOMPARABLE",
    "LAYER_MATCH",
    "LAYER_MISMATCH",
    "LAYER_NOT_COMPARED",
    "LayerOutcome",
    "LesionCandidateOutcome",
    "LesionCoverage",
    "METRIC_DECIMALS",
    "MISSED_LIST_COLUMNS",
    "MetricValue",
    "MetricsAudit",
    "PRESENCE_MATRIX",
    "RawCounts",
    "SIZE_EPSILON_MM",
    "VERDICT_DEFINITIONS",
    "VerdictDefinition",
    "VerdictOutcome",
    "assert_metrics_consistent",
    "assert_summary_consistent",
    "assert_verdict_completeness",
    "audit_metrics",
    "build_missed_list",
    "build_summary_payload",
    "candidate_summary",
    "check_verdict_completeness",
    "compare_attributes",
    "compare_coverage",
    "compare_density",
    "compare_location",
    "compare_size",
    "compute_metrics",
    "compute_metrics_from_rows",
    "count_model_normalized",
    "count_raw_counts",
    "count_rule_degraded",
    "enumerate_verdict_outcomes",
    "format_metric_value",
    "gold_lesions",
    "gold_summary_text",
    "judge_case",
    "layer_conclusions",
    "lesion_label_text",
    "lesion_summary_text",
    "manual_check_block",
    "metric_values",
    "missed_not_counted",
    "missed_reason_text",
    "normalize_tolerance",
    "npv_value",
    "ppv_value",
    "read_missed_list_csv",
    "select_candidate",
    "sensitivity_value",
    "specificity_value",
    "summary_row_dict",
    "summary_row_text",
    "verdict_distribution",
    "write_missed_list_csv",
]
