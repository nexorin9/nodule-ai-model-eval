"""三入口共用的 JSON 契约。

**请求**（xlsm 宏 / WPS JSA 宏 / CSV 直跑写出的 ``req.json``）::

    {
      "run_id": "...",
      "model_id": "...",
      "model_version": "...",
      "dataset_ref": "...",
      "cases": [{"accession_no": "...", "gold_standard_text": "...", "image_ref": "..."}],
      "options": {"size_tolerance_mm": 2.0, "concurrency": 4, "resume": false,
                  "endpoint_limits": {"inference": 8, "model": 2}}
    }

**响应**（引擎写出的 ``result.json``）::

    {
      "run_id": "...",
      "status": "evaluated",
      "case_rows": [...],
      "metrics": {...},
      "missed_list": [...],
      "summary": {...}
    }

两个宏版本与 CSV 直跑都走这一份契约 —— 差异只在“怎么把表里的行变成 cases”，
不在契约本身。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .errors import ErrorCode, NoduleEvalError
from .limits import KNOWN_ENDPOINTS

# ---- 判定枚举（任务 26 的判定引擎实现取值，任务 46 的表格回填复用） ----
VERDICT_HIT = "hit"
VERDICT_MISS = "miss"
VERDICT_FALSE_POSITIVE = "false_positive"
VERDICT_TRUE_NEGATIVE = "true_negative"
VERDICT_FAILED = "failed"

#: 五值互斥完备枚举。
VERDICTS: tuple[str, ...] = (
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_FALSE_POSITIVE,
    VERDICT_TRUE_NEGATIVE,
    VERDICT_FAILED,
)

#: **判定尚未执行**的过渡标记。
#:
#: 它**不属于**上面的五值判定枚举 —— 五值枚举描述的是判定引擎的取值空间，
#: 完备性与互斥性不受此标记影响。任务 3 的调用层最小切片只跑到「调用 + 落底账」，
#: 判定由任务 26 的判定引擎填；任务 26 起引擎不再产出此值。
VERDICT_PENDING = "pending"

#: run 状态：已完成逐例调用但尚未判定（任务 3 最小切片）。
#: 判定引擎落地后状态推进为 ``evaluated``。
RUN_STATUS_CALLED = "called"

#: 金标准归并来源：``model``（模型归并）/ ``rule``（规则降级）。
NORMALIZE_SOURCE_MODEL = "model"
NORMALIZE_SOURCE_RULE = "rule"
NORMALIZE_SOURCES: tuple[str, ...] = (NORMALIZE_SOURCE_MODEL, NORMALIZE_SOURCE_RULE)

#: 语义归并的**输出契约**（任务 47）—— 七个字段。
#:
#: 这份清单是**唯一真源**，三处共用：
#:
#: 1. **提示词加载时**校验提示词文件里声明的字段（提示词要哪个字段，
#:    清单里就得有；清单里的必填字段，提示词也得声明）——
#:    口径写在提示词里、契约写在代码里，两边各写一份就会漂移；
#: 2. **解析层**拿它判「模型少给了哪个字段」（缺任一必填字段即解析失败，
#:    走规则降级，见任务 21）；
#: 3. **提示词示例**的 JSON 块按它校验（多出来的字段一律拒收）。
#:
#: ``presence`` / ``location`` / ``size_mm`` / ``density``
#:     标签四元组。原文没写的属性填 ``None``，**不许猜**。
#: ``evidence_span``
#:     **回指锚点**：原文里一段连续子串，程序拿它回原文里找。
#: ``confidence`` / ``needs_human_review``
#:     模型自评字段（缺省填默认值并记一次解析降级，不因此作废）。
MERGE_OUTPUT_FIELDS: tuple[str, ...] = (
    "presence",
    "location",
    "size_mm",
    "density",
    "evidence_span",
    "confidence",
    "needs_human_review",
)

#: 缺任一即判**解析失败**（整份输出作废，走规则降级）的字段。
MERGE_REQUIRED_FIELDS: tuple[str, ...] = (
    "presence",
    "location",
    "size_mm",
    "density",
    "evidence_span",
)

#: 多病灶数组（``lesions``）里**每一条**允许的字段。
MERGE_LESION_FIELDS: tuple[str, ...] = (
    "location",
    "size_mm",
    "density",
    "evidence_span",
)

#: **禁止出现**的输出字段 —— 诊断结论类字段。
#:
#: 本产品只做**结构化标注**（把放射科写的原文读成标签），不出诊断、
#: 不出结论、不给治疗建议。模型输出里冒出这类字段，一律判**解析失败**
#: → 走规则降级（任务 24）：宁可退回确定性口径，也不把一句诊断结论
#: 带进评估结论里 —— 那一栏不是给临床决策用的。
MERGE_FORBIDDEN_OUTPUT_FIELDS: tuple[str, ...] = (
    "diagnosis",
    "diagnosis_text",
    "impression",
    "impression_text",
    "conclusion",
    "recommendation",
    "treatment",
    "advice",
    "clinical_advice",
    "诊断",
    "诊断结论",
    "结论",
    "建议",
    "治疗",
    "处置",
)

#: **四值判定** —— 真正给出结论的判定。只有它们可能进指标分母。
JUDGED_VERDICTS: tuple[str, ...] = (
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_FALSE_POSITIVE,
    VERDICT_TRUE_NEGATIVE,
)

#: **没给出结论**的判定 —— 一律不进指标分母。
#:
#: ``failed`` 的含义是「这一例没拿到判定」：调用失败、超时、标识不回显、
#: 响应缺字段，或绑定歧义被挂起。把它当阴性（未检出）计进分母，
#: 等于把「没跑成」算成「模型没看见」—— 敏感度直接被拉低。
NON_METRIC_VERDICTS: tuple[str, ...] = (VERDICT_FAILED,)

# ---- 契约默认值 ----
DEFAULT_SIZE_TOLERANCE_MM = 2.0
DEFAULT_CONCURRENCY = 4
MAX_SIZE_TOLERANCE_MM = 50.0
MAX_CONCURRENCY = 64

#: 请求体顶层必填字段。
REQUIRED_REQUEST_FIELDS: tuple[str, ...] = (
    "run_id",
    "model_id",
    "model_version",
    "dataset_ref",
)

#: 模型目录里响应字段映射必须声明的键（任务 6 的目录 schema 依此校验）。
REQUIRED_MAPPING_KEYS: tuple[str, ...] = (
    "candidates",
    "position",
    "size",
    "density",
    "confidence",
)


def contract_error(detail: str) -> NoduleEvalError:
    """契约类失败统一走 ``CONTRACT_INVALID``。"""

    return NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail=detail)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_nonempty_str(payload: Mapping[str, Any], key: str, where: str) -> str:
    value = payload.get(key)
    if value is None:
        raise contract_error(f"{where}的字段 {key} 必填且不得为空")
    if not isinstance(value, str):
        raise contract_error(f"{where}的字段 {key} 须为字符串（实得 {type(value).__name__}）")
    if not value.strip():
        raise contract_error(f"{where}的字段 {key} 必填且不得为空")
    return value


@dataclass(frozen=True)
class CaseInput:
    """评估表里的一行 —— 输入侧的最小单元。

    ``gold_standard_text`` 是放射科写的金标准原文，**任何环节都不得改写**；
    校验只判断它非空，存回时保持原样（含换行与全角标点）。

    ``study_uid`` 是**运行时解析字段**，不是评估表的列：生产路径上它由 RIS 按
    ``accession_no`` 绑定得出（任务 9 / 10）。任务 3 的调用层最小切片允许输入文件
    直接带上它，好让「检查号 → 影像标识」的绑定先跑通一遍。

    ``needs_review`` 同样是**运行时解析字段**：评估表没有这一列，它由装配层
    标出「这一例的影像引用没解析出来」（表格没给 ``image_ref``、已装配的病例集里
    也取不到），该例挂人工复核，**不静默用空值继续**。

    ``hold_reason`` 是**挂起原因**：绑定歧义、引用与 RIS 不一致、检查号在表里
    出现多次时，绑定层把原因写在这里，引擎原样落到表格的 ``failure_reason`` 列。
    有了它，表格上就能看出「这一例为什么没被评估」，而不是只有一句「未绑定」。

    ``gold_standard_normalized`` 是**分析用副本**（全角数字与中文标点归一、
    行内空白收敛），由读取层（任务 11）产出。留痕用原文、解析用归一版 ——
    ``gold_standard_text`` **逐字不动**（含换行与全角标点）。

    ``tool_call_ref`` 是**运行时引用**（评估表没有这一列）：引擎跑完推理调用后
    把那一例的调用记录 id 带上，语义归并把它透传给模型端点 —— 于是模型调用
    底账（``model_calls.jsonl``）里那一行的 ``tool_call_id`` 能在工具调用底账
    （``tool_calls.jsonl``）里命中，「结论 → 工具调用 → 模型调用」三级链路
    一次走通（任务 33）。

    **它刻意不叫 ``tool_call_id``**：评估表里那一列是**输出**列（产品写回去的
    调用记录 id），读回来就成自证 —— 所以读取层只认既有三列 + 三个运行时字段，
    输出列一律不读（见 ``sheets/read.py``）。
    """

    accession_no: str
    gold_standard_text: str
    image_ref: str | None = None
    row_no: int | None = None
    study_uid: str | None = None
    needs_review: bool = False
    hold_reason: str = ""
    gold_standard_normalized: str = ""
    tool_call_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession_no": self.accession_no,
            "gold_standard_text": self.gold_standard_text,
            "image_ref": self.image_ref,
            "row_no": self.row_no,
            "study_uid": self.study_uid,
            "needs_review": self.needs_review,
            "hold_reason": self.hold_reason,
            "gold_standard_normalized": self.gold_standard_normalized,
            "tool_call_ref": self.tool_call_ref,
        }


@dataclass(frozen=True)
class RunOptions:
    """本轮评估的可调参数。

    ``concurrency`` 是**工作线程数**（逐例 fan-out 同时处理几例）。

    ``endpoint_limits`` 是**逐端点的在途上限**（``{"inference": 8, "model": 2}``）——
    推理端点与模型端点**各自**限流：模型端点通常更慢更贵，
    用一个总闸门管两个端点会出现「模型端点打满了，推理请求还在排队等额度」。
    缺省时每个端点都取 ``concurrency``。
    """

    size_tolerance_mm: float = DEFAULT_SIZE_TOLERANCE_MM
    concurrency: int = DEFAULT_CONCURRENCY
    resume: bool = False
    endpoint_limits: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "size_tolerance_mm": self.size_tolerance_mm,
            "concurrency": self.concurrency,
            "resume": self.resume,
            "endpoint_limits": dict(self.endpoint_limits),
        }


@dataclass(frozen=True)
class EvalRequest:
    """一次评估请求（表内插件点「开始评估」后写出的 ``req.json``）。"""

    run_id: str
    model_id: str
    model_version: str
    dataset_ref: str
    cases: tuple[CaseInput, ...]
    options: RunOptions = field(default_factory=RunOptions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "dataset_ref": self.dataset_ref,
            "cases": [case.to_dict() for case in self.cases],
            "options": self.options.to_dict(),
        }


@dataclass(frozen=True)
class CaseRow:
    """逐例判定行 —— 回填到评估表新增列的一行。

    ``error_code`` 是**运行时字段**，不进评估表新增列（那六列由科室定好了）。
    它记的是这一例失败时的错误码（``INFERENCE_5XX`` / ``STUDY_UID_MISMATCH`` 等），
    让 run store 能按错误码把失败例筛出来 —— 只留一句中文失败原因，
    事后没法按码统计。

    ``held`` / ``rule_degraded`` / ``degrade_reason`` 同样是运行时字段
    （任务 24），它们回答三个**互相独立**的问题：

    - ``held``
        这一例因**绑定或引用存疑**被挂起（一对多歧义 / 引用与 RIS 不一致 /
        引用未解析）→ **不进指标分母**。影像本身存疑时，用什么口径判都没用。
    - ``rule_degraded``
        金标准归并走了**规则降级路径**（模型不可达 / 输出不合契约 / 回指对不上）→
        结论由确定性基线给出，**仍进指标分母**，但单列「降级数」并挂人工复核。
    - ``degrade_reason``
        降级原因（中文）—— 模型为什么没成功。

    **``needs_review`` 是「要人看一眼」的总闸**（挂起 + 降级 + 归并读不出），
    它**不直接**决定进不进分母 —— 决定分母的是上面两个标记。

    **任务 28 的四个字段**（同样是运行时字段，不进评估表新增六列）：

    - ``judge_basis``
        **判定依据**的一行文本：存在性 / 部位 / 尺寸 / 密度四层结论 + 覆盖结论。
        「这一例凭什么这么判」必须一眼看得见 —— 复核的人不翻产物就能看懂。
    - ``uncovered_labels``
        **没被盖住的病灶**（多标签最严口径）：金标准写了几处、模型盖住了几处，
        漏掉的是哪一处（部位 / 尺寸 / 密度）。任务 30 的漏诊原因文案直接引用。
    - ``incomparable``
        这一例的**属性不可比**（写法认不出来，或有一处病灶的对应关系判不出来）→
        **既不当命中也不当漏诊**，不进指标分母，单列「不可比数」。
    - ``incomparable_reason``
        不可比的说明（中文）—— 写明是哪一侧、哪个写法认不出来。

    **``gold_summary`` 是任务 30 新增的运行时字段**（同样不进评估表新增六列）：
    **金标准摘要** —— 由归并后的结构化标签（部位 / 尺寸 / 密度）拼成的一句文本。
    漏诊清单要带「金标准说了什么」，但**原文不落产物**（可能带患者可识别信息），
    所以摘要走这条字段。
    """

    accession_no: str
    verdict: str
    model_findings: str = ""
    failure_reason: str = ""
    tool_call_id: str = ""
    needs_review: bool = False
    normalize_source: str = ""
    row_no: int | None = None
    error_code: str = ""
    held: bool = False
    rule_degraded: bool = False
    degrade_reason: str = ""
    judge_basis: str = ""
    uncovered_labels: str = ""
    incomparable: bool = False
    incomparable_reason: str = ""
    gold_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession_no": self.accession_no,
            "verdict": self.verdict,
            "model_findings": self.model_findings,
            "failure_reason": self.failure_reason,
            "tool_call_id": self.tool_call_id,
            "needs_review": self.needs_review,
            "normalize_source": self.normalize_source,
            "row_no": self.row_no,
            "error_code": self.error_code,
            "held": self.held,
            "rule_degraded": self.rule_degraded,
            "degrade_reason": self.degrade_reason,
            "judge_basis": self.judge_basis,
            "uncovered_labels": self.uncovered_labels,
            "incomparable": self.incomparable,
            "incomparable_reason": self.incomparable_reason,
            "gold_summary": self.gold_summary,
        }


#: 逐例明细的列顺序（既有键 + 新增六列 + 行号）。
#: 与评估表新增列一一对应，回填（任务 46）与导出（任务 39）共用这一份顺序。
CASE_ROW_COLUMNS: tuple[str, ...] = (
    "accession_no",
    "verdict",
    "model_findings",
    "failure_reason",
    "tool_call_id",
    "needs_review",
    "normalize_source",
    "row_no",
)

#: 漏诊清单的列顺序（任务 30 的结论产物，**不是**评估表的回填列）。
#:
#: 「金标准摘要」列给的是**归并后的结构化标签**（部位 / 尺寸 / 密度），
#: 不是金标准原文 —— 原文可能带姓名 / 年龄 / 住院号，产物里不落。
MISSED_LIST_COLUMNS: tuple[str, ...] = (
    "accession_no",
    "gold_standard_summary",
    "model_findings",
    "missed_reason",
    "tool_call_id",
    "rule_degraded",
)


# --------------------------------------------------------------------------
# 指标隔离（任务 10）
# --------------------------------------------------------------------------
#
# **谁不进指标分母**，一句话说清：
#
# - **没给出结论的例**（``failed`` / 尚未判定）—— 没跑成就没有结论，
#   把它算成阴性等于说「模型没看见」，敏感度被凭空拉低；
# - **挂起待复核的例**（``needs_review``）—— 这一例绑到的影像本身存疑
#   （一对多歧义、引用与 RIS 不一致、检查号在表里重复），它要等人看过才算数。
#
# 两类都在汇总行**单列**（失败数 / 挂起数），绝不混进 TP / FP / FN / TN。
# 这份口径是**唯一真源**：判定引擎（任务 26）、指标计算（任务 29）、
# 汇总行与写回体（任务 30 / 38）都从这里取，不各写一份。


def counts_toward_denominator(row: CaseRow) -> bool:
    """这一例是否进入指标分母。

    四条判据（按序）：

    1. **必须给出四值判定** —— ``failed`` / 尚未判定的一律不进；
    2. **绑定或引用存疑挂起的例不进**（``held``）—— 影像本身存疑时，
       用什么口径判都没用；
    3. **规则降级的例仍进**（``rule_degraded``）—— 结论是确定的，
       只是归并口径退到了规则基线。把降级例排除出分母，
       等于把「模型这一层没跑成」算成「模型没看见」，敏感度会被凭空拉低；
    4. **属性不可比的例不进**（``incomparable``，任务 28）—— 写法认不出来时
       **没有可比的对象**：当成一致是一次命中白送，当成不一致是一次命中
       被算成漏诊。两头都不能算，就不进分母，单列「不可比数」。
    """

    if row.verdict not in JUDGED_VERDICTS:
        return False
    if row.held:
        return False
    if row.incomparable:
        return False
    if row.needs_review and not row.rule_degraded:
        return False
    return True


def is_hold(row: CaseRow) -> bool:
    """这一例是否**挂起等人处理**（而不是「需要看一眼」）。

    挂起与降级是两回事：

    - **挂起**（``held``）：这一例的影像绑定存疑，**没有结论**，不进分母；
    - **降级**（``rule_degraded``）：这一例**有结论**，只是归并口径退到了
      规则基线 —— 仍进分母，单列降级数，优先复核。

    汇总行的「挂起数」只数挂起例：把降级例混进去，复核的人就分不清
    该去改表（挂起）还是该去找信息科看模型端点（降级）。

    **挂起优先于降级**：一例外挂起又被降级（影像存疑 + 模型不可达），
    它仍算挂起 —— 影像本身存疑时，用什么口径判都没用。

    **不可比单列**（任务 28）：``incomparable`` 的例既不是挂起也不是降级，
    它单列在「不可比数」里 —— 挂起数的含义是「影像存疑」，
    把写法认不出的例混进去，复核的人就分不清该去改表还是该去补等价表。
    """

    if row.held:
        return True
    if row.incomparable:
        return False
    return bool(row.needs_review and not row.rule_degraded)


@dataclass(frozen=True)
class MetricPartition:
    """逐例行按「进不进指标分母」切开的计数。

    ``failed`` 与 ``needs_review`` **互斥**：挂了起的例不再计进失败数 ——
    「这一例因为绑不准而挂起」和「这一例调用失败了」是两回事，
    混在一列里，复核的人就不知道该去改表还是该找厂商。

    ``incomparable``（任务 28）**同样单列**：写法认不出的例既不是失败、
    也不是挂起 —— 它单列一列，汇总行不会把它混进任何一栏。
    """

    total: int = 0
    counted: int = 0
    judged: int = 0
    failed: int = 0
    needs_review: int = 0
    incomparable: int = 0
    excluded: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "counted": self.counted,
            "judged": self.judged,
            "failed": self.failed,
            "needs_review": self.needs_review,
            "incomparable": self.incomparable,
            "excluded": self.excluded,
        }


def partition_rows(rows: Sequence[CaseRow]) -> MetricPartition:
    """把逐例行切开：进分母的、失败的、挂起的、不可比的各有多少。"""

    total = len(rows)
    counted = 0
    judged = 0
    failed = 0
    needs_review = 0
    incomparable = 0

    for row in rows:
        if row.verdict in JUDGED_VERDICTS:
            judged += 1
        if is_hold(row):
            needs_review += 1
        elif row.verdict == VERDICT_FAILED:
            failed += 1
        elif row.incomparable:
            incomparable += 1
        if counts_toward_denominator(row):
            counted += 1

    return MetricPartition(
        total=total,
        counted=counted,
        judged=judged,
        failed=failed,
        needs_review=needs_review,
        incomparable=incomparable,
        excluded=total - counted,
    )


@dataclass(frozen=True)
class Metrics:
    """四项指标 + 原始计数。

    指标分母为 0 时取值为 ``None``（不是 0），原因写在 ``*_reason`` 字段里；
    原始计数保持整数，供人工手算复核。
    """

    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    sensitivity: float | None = None
    specificity: float | None = None
    ppv: float | None = None
    npv: float | None = None
    failed_count: int = 0
    rule_degraded_count: int = 0
    needs_review_count: int = 0
    incomparable_count: int = 0
    #: 用**模型**归并的例数（与 ``rule_degraded_count`` 一起看，才知道
    #: 这一轮有多少例的结论出自模型、多少例退回了规则基线）。
    model_normalized_count: int = 0
    sensitivity_reason: str = ""
    specificity_reason: str = ""
    ppv_reason: str = ""
    npv_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "sensitivity": self.sensitivity,
            "specificity": self.specificity,
            "ppv": self.ppv,
            "npv": self.npv,
            "failed_count": self.failed_count,
            "rule_degraded_count": self.rule_degraded_count,
            "needs_review_count": self.needs_review_count,
            "incomparable_count": self.incomparable_count,
            "model_normalized_count": self.model_normalized_count,
            "sensitivity_reason": self.sensitivity_reason,
            "specificity_reason": self.specificity_reason,
            "ppv_reason": self.ppv_reason,
            "npv_reason": self.npv_reason,
        }


@dataclass(frozen=True)
class MissedItem:
    """漏诊清单的一行 —— 放射科逐例回看原调用用。

    ``gold_standard_summary``
        **金标准摘要** —— 由归并后的结构化标签（部位 / 尺寸 / 密度）拼成。
        **不是金标准原文**：原文可能带姓名 / 年龄 / 住院号，产物里不落。

    ``rule_degraded``
        这一条漏诊是**规则降级**判出来的（模型不可达，退回规则基线）——
        复核时要区别对待：它说明的是「金标准说有、模型没检出」，
        但归并口径与其它例不同。
    """

    accession_no: str
    gold_standard_summary: str = ""
    model_findings: str = ""
    missed_reason: str = ""
    tool_call_id: str = ""
    rule_degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession_no": self.accession_no,
            "gold_standard_summary": self.gold_standard_summary,
            "model_findings": self.model_findings,
            "missed_reason": self.missed_reason,
            "tool_call_id": self.tool_call_id,
            "rule_degraded": self.rule_degraded,
        }


@dataclass(frozen=True)
class Summary:
    """run 级结论摘要。"""

    model_id: str = ""
    model_version: str = ""
    dataset_ref: str = ""
    total_cases: int = 0
    evaluated_cases: int = 0
    failed_count: int = 0
    rule_degraded_count: int = 0
    needs_review_count: int = 0
    #: **属性不可比**的例数（任务 28）—— 写法认不出来，没法核对，
    #: 既不当命中也不当漏诊，单列一栏。
    incomparable_count: int = 0
    generated_at: str = ""
    #: 断点续跑时**跳过**的例数（上一版已跑完、本版没重跑）。
    skipped_count: int = 0
    #: 本次是同一轮的第几版（1 = 第一版；重跑 / 续跑依次递增）。
    version_no: int = 1
    #: 用**模型**归并的例数 —— 与 ``rule_degraded_count`` 并列看，
    #: 才分得清「这一轮有多少例的结论出自模型、多少例退回了规则基线」。
    model_normalized_count: int = 0
    #: 汇总行的**降级说明**（中文）—— 有降级时写明降级例数与降级口径。
    degrade_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "dataset_ref": self.dataset_ref,
            "total_cases": self.total_cases,
            "evaluated_cases": self.evaluated_cases,
            "failed_count": self.failed_count,
            "rule_degraded_count": self.rule_degraded_count,
            "needs_review_count": self.needs_review_count,
            "incomparable_count": self.incomparable_count,
            "generated_at": self.generated_at,
            "skipped_count": self.skipped_count,
            "version_no": self.version_no,
            "model_normalized_count": self.model_normalized_count,
            "degrade_note": self.degrade_note,
        }


@dataclass(frozen=True)
class EvalResponse:
    """引擎写出的结果（``result.json``），表内插件据此回填新增列。"""

    run_id: str
    status: str
    case_rows: tuple[CaseRow, ...] = ()
    metrics: Metrics = field(default_factory=Metrics)
    missed_list: tuple[MissedItem, ...] = ()
    summary: Summary = field(default_factory=Summary)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "case_rows": [row.to_dict() for row in self.case_rows],
            "metrics": self.metrics.to_dict(),
            "missed_list": [item.to_dict() for item in self.missed_list],
            "summary": self.summary.to_dict(),
        }


# --------------------------------------------------------------------------
# 解析与校验
# --------------------------------------------------------------------------


def parse_case(payload: Any, index: int) -> CaseInput:
    """解析一行病例输入。

    ``accession_no`` 必填非空；``gold_standard_text`` 必填非空（**原文保持不变**）；
    ``image_ref`` 选填，缺省由引擎按检查号从 RIS 取。
    """

    where = f"第 {index + 1} 行"
    if not isinstance(payload, Mapping):
        raise contract_error(f"{where}须为 JSON 对象（实得 {type(payload).__name__}）")

    accession_no = _require_nonempty_str(payload, "accession_no", where)

    raw_text = payload.get("gold_standard_text")
    if raw_text is None:
        raise contract_error(f"{where}的字段 gold_standard_text 必填且不得为空")
    if not isinstance(raw_text, str):
        raise contract_error(
            f"{where}的字段 gold_standard_text 须为字符串（实得 {type(raw_text).__name__}）"
        )
    if not raw_text.strip():
        raise contract_error(f"{where}的字段 gold_standard_text 必填且不得为空")

    image_ref_raw = payload.get("image_ref")
    image_ref: str | None = None
    if image_ref_raw is not None:
        if not isinstance(image_ref_raw, str):
            raise contract_error(
                f"{where}的字段 image_ref 须为字符串（实得 {type(image_ref_raw).__name__}）"
            )
        image_ref = image_ref_raw.strip() or None

    row_no_raw = payload.get("row_no")
    row_no: int | None
    if row_no_raw is None:
        row_no = index + 1
    elif isinstance(row_no_raw, bool) or not isinstance(row_no_raw, int):
        raise contract_error(f"{where}的字段 row_no 须为整数")
    else:
        row_no = row_no_raw

    study_uid_raw = payload.get("study_uid")
    study_uid: str | None = None
    if study_uid_raw is not None:
        if not isinstance(study_uid_raw, str):
            raise contract_error(f"{where}的字段 study_uid 须为字符串")
        study_uid = study_uid_raw.strip() or None

    needs_review = payload.get("needs_review", False)
    if not isinstance(needs_review, bool):
        raise contract_error(f"{where}的字段 needs_review 须为布尔值")

    hold_reason_raw = payload.get("hold_reason", "")
    if not isinstance(hold_reason_raw, str):
        raise contract_error(f"{where}的字段 hold_reason 须为字符串")
    hold_reason = hold_reason_raw.strip()

    normalized_raw = payload.get("gold_standard_normalized")
    if normalized_raw is None:
        gold_standard_normalized = ""
    elif not isinstance(normalized_raw, str):
        raise contract_error(f"{where}的字段 gold_standard_normalized 须为字符串")
    else:
        gold_standard_normalized = normalized_raw

    tool_call_ref_raw = payload.get("tool_call_ref")
    if tool_call_ref_raw is None:
        tool_call_ref = ""
    elif not isinstance(tool_call_ref_raw, str):
        raise contract_error(f"{where}的字段 tool_call_ref 须为字符串")
    else:
        tool_call_ref = tool_call_ref_raw.strip()

    return CaseInput(
        accession_no=accession_no.strip(),
        gold_standard_text=raw_text,
        image_ref=image_ref,
        row_no=row_no,
        study_uid=study_uid,
        needs_review=needs_review,
        hold_reason=hold_reason,
        gold_standard_normalized=gold_standard_normalized,
        tool_call_ref=tool_call_ref,
    )


def parse_endpoint_limits(payload: Any) -> dict[str, int]:
    """解析 ``options.endpoint_limits``（逐端点的在途上限）。

    形如 ``{"inference": 8, "model": 2}``。写错的端点名、写成 0 或负数的上限，
    都指名报错 —— 限流写错名字等于限流没开，默默生效比报错更危险。
    """

    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise contract_error(
            f"字段 options.endpoint_limits 须为 JSON 对象（实得 {type(payload).__name__}）"
        )

    limits: dict[str, int] = {}
    for name, value in payload.items():
        if name not in KNOWN_ENDPOINTS:
            raise contract_error(
                f"字段 options.endpoint_limits 的端点 {name} 不存在"
                f"（可用：{'/'.join(KNOWN_ENDPOINTS)}）"
            )
        if isinstance(value, bool) or not isinstance(value, int):
            raise contract_error(f"字段 options.endpoint_limits.{name} 须为整数")
        if value < 1:
            raise contract_error(f"字段 options.endpoint_limits.{name} 不得小于 1")
        limits[str(name)] = int(value)
    return limits


def parse_options(payload: Any) -> RunOptions:
    """解析 ``options``；缺省时用契约默认值（容差 ±2mm、并发 4、不续跑）。"""

    if payload is None:
        return RunOptions()
    if not isinstance(payload, Mapping):
        raise contract_error(f"字段 options 须为 JSON 对象（实得 {type(payload).__name__}）")

    size_tolerance = payload.get("size_tolerance_mm", DEFAULT_SIZE_TOLERANCE_MM)
    if not _is_number(size_tolerance):
        raise contract_error("字段 options.size_tolerance_mm 须为数字")
    size_tolerance = float(size_tolerance)
    if size_tolerance < 0:
        raise contract_error("字段 options.size_tolerance_mm 不得为负数")
    if size_tolerance > MAX_SIZE_TOLERANCE_MM:
        raise contract_error(
            f"字段 options.size_tolerance_mm 不得超过 {MAX_SIZE_TOLERANCE_MM} 毫米"
        )

    concurrency = payload.get("concurrency", DEFAULT_CONCURRENCY)
    if isinstance(concurrency, bool) or not isinstance(concurrency, int):
        raise contract_error("字段 options.concurrency 须为整数")
    if concurrency < 1:
        raise contract_error("字段 options.concurrency 不得小于 1")
    if concurrency > MAX_CONCURRENCY:
        raise contract_error(f"字段 options.concurrency 不得超过 {MAX_CONCURRENCY}")

    resume = payload.get("resume", False)
    if not isinstance(resume, bool):
        raise contract_error("字段 options.resume 须为布尔值")

    endpoint_limits = parse_endpoint_limits(payload.get("endpoint_limits"))

    return RunOptions(
        size_tolerance_mm=size_tolerance,
        concurrency=concurrency,
        resume=resume,
        endpoint_limits=endpoint_limits,
    )


def parse_request(payload: Any) -> EvalRequest:
    """把三入口写出的 JSON 请求解析成 :class:`EvalRequest`。

    任何不符合契约的输入都抛 ``CONTRACT_INVALID``，并**指名字段**，
    不静默丢行、不填默认值蒙混。
    """

    if not isinstance(payload, Mapping):
        raise contract_error(f"请求体须为 JSON 对象（实得 {type(payload).__name__}）")

    values = {key: _require_nonempty_str(payload, key, "请求体") for key in REQUIRED_REQUEST_FIELDS}

    cases_raw = payload.get("cases")
    if cases_raw is None:
        raise contract_error("请求体的字段 cases 必填且不得为空")
    if isinstance(cases_raw, (str, bytes)) or not isinstance(cases_raw, Sequence):
        raise contract_error("请求体的字段 cases 须为数组")
    if len(cases_raw) == 0:
        raise contract_error("请求体的字段 cases 不得为空数组")

    cases = tuple(parse_case(item, index) for index, item in enumerate(cases_raw))

    return EvalRequest(
        run_id=values["run_id"].strip(),
        model_id=values["model_id"].strip(),
        model_version=values["model_version"].strip(),
        dataset_ref=values["dataset_ref"].strip(),
        cases=cases,
        options=parse_options(payload.get("options")),
    )


def parse_case_row(payload: Any, index: int) -> CaseRow:
    """解析结果里的一行逐例判定。"""

    where = f"结果第 {index + 1} 行"
    if not isinstance(payload, Mapping):
        raise contract_error(f"{where}须为 JSON 对象（实得 {type(payload).__name__}）")

    accession_no = _require_nonempty_str(payload, "accession_no", where)
    verdict = _require_nonempty_str(payload, "verdict", where)
    if verdict not in VERDICTS and verdict != VERDICT_PENDING:
        raise contract_error(
            f"{where}的字段 verdict 取值非法（{verdict}），"
            f"合法取值为 {'/'.join(VERDICTS)}"
        )

    normalize_source = payload.get("normalize_source") or ""
    if normalize_source and normalize_source not in NORMALIZE_SOURCES:
        raise contract_error(
            f"{where}的字段 normalize_source 取值非法（{normalize_source}），"
            f"合法取值为 {'/'.join(NORMALIZE_SOURCES)}"
        )

    needs_review = payload.get("needs_review", False)
    if not isinstance(needs_review, bool):
        raise contract_error(f"{where}的字段 needs_review 须为布尔值")

    row_no_raw = payload.get("row_no")
    if row_no_raw is not None and (isinstance(row_no_raw, bool) or not isinstance(row_no_raw, int)):
        raise contract_error(f"{where}的字段 row_no 须为整数")

    error_code_raw = payload.get("error_code") or ""
    if not isinstance(error_code_raw, str):
        raise contract_error(f"{where}的字段 error_code 须为字符串")

    held = payload.get("held", False)
    if not isinstance(held, bool):
        raise contract_error(f"{where}的字段 held 须为布尔值")

    rule_degraded = payload.get("rule_degraded", False)
    if not isinstance(rule_degraded, bool):
        raise contract_error(f"{where}的字段 rule_degraded 须为布尔值")

    degrade_reason = payload.get("degrade_reason") or ""
    if not isinstance(degrade_reason, str):
        raise contract_error(f"{where}的字段 degrade_reason 须为字符串")

    incomparable = payload.get("incomparable", False)
    if not isinstance(incomparable, bool):
        raise contract_error(f"{where}的字段 incomparable 须为布尔值")

    judge_basis = payload.get("judge_basis") or ""
    if not isinstance(judge_basis, str):
        raise contract_error(f"{where}的字段 judge_basis 须为字符串")

    uncovered_labels = payload.get("uncovered_labels") or ""
    if not isinstance(uncovered_labels, str):
        raise contract_error(f"{where}的字段 uncovered_labels 须为字符串")

    incomparable_reason = payload.get("incomparable_reason") or ""
    if not isinstance(incomparable_reason, str):
        raise contract_error(f"{where}的字段 incomparable_reason 须为字符串")

    gold_summary = payload.get("gold_summary") or ""
    if not isinstance(gold_summary, str):
        raise contract_error(f"{where}的字段 gold_summary 须为字符串")

    return CaseRow(
        accession_no=accession_no.strip(),
        verdict=verdict,
        model_findings=payload.get("model_findings") or "",
        failure_reason=payload.get("failure_reason") or "",
        tool_call_id=payload.get("tool_call_id") or "",
        needs_review=needs_review,
        normalize_source=normalize_source,
        row_no=row_no_raw,
        error_code=error_code_raw,
        held=held,
        rule_degraded=rule_degraded,
        degrade_reason=degrade_reason,
        judge_basis=judge_basis,
        uncovered_labels=uncovered_labels,
        incomparable=incomparable,
        incomparable_reason=incomparable_reason,
        gold_summary=gold_summary,
    )


def _parse_metrics(payload: Any) -> Metrics:
    if payload is None:
        return Metrics()
    if not isinstance(payload, Mapping):
        raise contract_error("字段 metrics 须为 JSON 对象")

    def _int_or_zero(key: str) -> int:
        value = payload.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            raise contract_error(f"字段 metrics.{key} 须为整数")
        return value

    def _num_or_none(key: str) -> float | None:
        value = payload.get(key)
        if value is None:
            return None
        if not _is_number(value):
            raise contract_error(f"字段 metrics.{key} 须为数字或 null")
        return float(value)

    return Metrics(
        tp=_int_or_zero("tp"),
        fp=_int_or_zero("fp"),
        fn=_int_or_zero("fn"),
        tn=_int_or_zero("tn"),
        sensitivity=_num_or_none("sensitivity"),
        specificity=_num_or_none("specificity"),
        ppv=_num_or_none("ppv"),
        npv=_num_or_none("npv"),
        failed_count=_int_or_zero("failed_count"),
        rule_degraded_count=_int_or_zero("rule_degraded_count"),
        needs_review_count=_int_or_zero("needs_review_count"),
        incomparable_count=_int_or_zero("incomparable_count"),
        model_normalized_count=_int_or_zero("model_normalized_count"),
        sensitivity_reason=payload.get("sensitivity_reason") or "",
        specificity_reason=payload.get("specificity_reason") or "",
        ppv_reason=payload.get("ppv_reason") or "",
        npv_reason=payload.get("npv_reason") or "",
    )


def _parse_summary(payload: Any) -> Summary:
    if payload is None:
        return Summary()
    if not isinstance(payload, Mapping):
        raise contract_error("字段 summary 须为 JSON 对象")

    def _int_or_zero(key: str) -> int:
        value = payload.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            raise contract_error(f"字段 summary.{key} 须为整数")
        return value

    return Summary(
        model_id=payload.get("model_id") or "",
        model_version=payload.get("model_version") or "",
        dataset_ref=payload.get("dataset_ref") or "",
        total_cases=_int_or_zero("total_cases"),
        evaluated_cases=_int_or_zero("evaluated_cases"),
        failed_count=_int_or_zero("failed_count"),
        rule_degraded_count=_int_or_zero("rule_degraded_count"),
        needs_review_count=_int_or_zero("needs_review_count"),
        incomparable_count=_int_or_zero("incomparable_count"),
        generated_at=payload.get("generated_at") or "",
        skipped_count=_int_or_zero("skipped_count"),
        version_no=_int_or_zero("version_no") or 1,
        model_normalized_count=_int_or_zero("model_normalized_count"),
        degrade_note=payload.get("degrade_note") or "",
    )


def parse_response(payload: Any) -> EvalResponse:
    """解析引擎写出的 ``result.json``（表内插件读回后据此回填新增列）。"""

    if not isinstance(payload, Mapping):
        raise contract_error(f"响应体须为 JSON 对象（实得 {type(payload).__name__}）")

    run_id = _require_nonempty_str(payload, "run_id", "响应体")
    status = _require_nonempty_str(payload, "status", "响应体")

    rows_raw = payload.get("case_rows") or []
    if not isinstance(rows_raw, Sequence) or isinstance(rows_raw, (str, bytes)):
        raise contract_error("字段 case_rows 须为数组")
    case_rows = tuple(parse_case_row(item, index) for index, item in enumerate(rows_raw))

    missed_raw = payload.get("missed_list") or []
    if not isinstance(missed_raw, Sequence) or isinstance(missed_raw, (str, bytes)):
        raise contract_error("字段 missed_list 须为数组")
    missed_list = tuple(
        MissedItem(
            accession_no=_require_nonempty_str(item, "accession_no", f"漏诊清单第 {index + 1} 行"),
            gold_standard_summary=item.get("gold_standard_summary") or "",
            model_findings=item.get("model_findings") or "",
            missed_reason=item.get("missed_reason") or "",
            tool_call_id=item.get("tool_call_id") or "",
        )
        for index, item in enumerate(missed_raw)
    )

    return EvalResponse(
        run_id=run_id.strip(),
        status=status.strip(),
        case_rows=case_rows,
        metrics=_parse_metrics(payload.get("metrics")),
        missed_list=missed_list,
        summary=_parse_summary(payload.get("summary")),
    )
