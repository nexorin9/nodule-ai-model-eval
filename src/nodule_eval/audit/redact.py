"""产物脱敏扫描器：产物里不该有患者可识别明细（任务 34）。

**红线**（``spec.md`` →「安全与合规边界」）：产物只落 ``accession_no`` 与内容
哈希，**不含姓名 / 身份证 / 住院号**。

两处会漏明细：

* **放射科写的金标准原文**里带姓名、住院号 —— 原文本身在院内评估表里（那是
  医院自己的单据），但产品**派生**出来的片段（``evidence_span``：判出存在性
  的那一句）不能跟着落进产物；
* **厂商推理服务把患者信息回显在返回里** —— 那是别人的返回，我们不替它改
  （底账必须原样留），但要**扫出来**。

本模块是这条红线的**验收闸门**，做三件事：

1. **扫内容** —— 遍历 ``runs/<run_id>/`` 下的每一份产物，按身份证号、住院号、
   手机号与中文姓名四种模式检出可识别明细；**命中即失败**，报出文件名与
   命中位置，**不做静默替换**（悄悄改掉 = 底账对不上原始调用，飞检一问就穿）；
2. **扫字段** —— 每类产物有一份**字段白名单**（:data:`ARTIFACT_SPECS`）。
   产物里出现**未登记**的字段即报错：后来有人给产物加了一列装明细，
   没登记就等于把明细带出去了；
3. **登记** —— 写产物时同步登记这一类产物的字段白名单
   （:func:`register_artifact_fields`），落 ``runs/<run_id>/artifact_fields.json``；
   事后扫描拿它对账，**手改过的产物也看得出来**。

**源产品证据**：参考实现把 ``input.messages``（含临床问题原文）与
``model_answer`` **整段落盘**进一条日志流
（``github_ref/MedRAX/quickstart.py:152-171``），全链路**没有任何脱敏**。
本产品把这条「原文不落盘」的做法反过来用成**闸门**：先不落原文
（只落哈希），再用本扫描器**验证**这条约束真的成立 —— 源产品无对应物，
为新建。

**这套闸门认得出什么、认不出什么**（说清楚，才敢让人信它）：

* **认得出**：身份证号（18/15 位）、带标签的住院号 / 病案号、手机号、
  **姓名字段**（``姓名`` / ``patient_name`` 这类键）、带 ``姓名：`` 标签的
  自由文本；
* **认不出**：**不带标签、也不在姓名字段里**的中文姓名 —— 中文姓名没有
  固定形状（「张三」和「肺门」长得一模一样），谁也认不出。所以产品侧的
  做法是**根本不落原文**（只落哈希与结构化标签），而不是指望扫描器认出来；
  扫描器守的是「**没登记的字段**」与「**有形状的明细**」这两类。

**为什么扫描器只报不改**：底账（``tool_calls.jsonl``）必须原样留 ——
它记的是「厂商到底回了什么」，悄悄改一个字，飞检一问「这例凭什么算漏判」
就答不上来了。要改的是**我们自己派生的片段**（金标准原文摘句），
那部分走 :func:`mask_pii` 落盘前脱敏，脱敏了几处记进 ``redaction`` 块。
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..contracts import CASE_ROW_COLUMNS, MISSED_LIST_COLUMNS
from ..errors import ErrorCode, NoduleEvalError

__all__ = [
    "ARTIFACT_FIELDS_FILENAME",
    "ARTIFACT_SPECS",
    "ArtifactSpec",
    "PiiHit",
    "ScanFinding",
    "ScanReport",
    "assert_artifacts_clean",
    "detect_pii",
    "load_artifact_fields",
    "mask_pii",
    "mask_payload",
    "register_artifact_fields",
    "render_scan_report",
    "scan_run_artifacts",
    "scan_text",
]


# --------------------------------------------------------------------------
# 检出种类
# --------------------------------------------------------------------------

#: 身份证号（18 位新证 / 15 位旧证）。
KIND_ID_CARD = "id_card"
#: 住院号 / 病案号 / 就诊号。
KIND_ADMISSION_NO = "admission_no"
#: 手机号。
KIND_PHONE = "phone"
#: 中文姓名（字段名或带标签的自由文本）。
KIND_PATIENT_NAME = "patient_name"
#: 产物里出现了**未登记**的字段。
KIND_UNREGISTERED_FIELD = "unregistered_field"

#: 全部检出种类的中文名（报错文案用）。
PII_KIND_LABELS: dict[str, str] = {
    KIND_ID_CARD: "身份证号",
    KIND_ADMISSION_NO: "住院号",
    KIND_PHONE: "手机号",
    KIND_PATIENT_NAME: "患者姓名",
    KIND_UNREGISTERED_FIELD: "未登记字段",
}


# --------------------------------------------------------------------------
# 模式表
# --------------------------------------------------------------------------
#
# **宁可漏报也不误报**：医院里到处是数字（检查号、时间戳、毫秒数、哈希），
# 一条宽泛的正则能把整本账扫成一片红，红了就没人看 —— 闸门一旦被习惯性
# 忽略，就等于没有。所以每条模式都尽量「带标签」或「带结构」。

#: 身份证号（裸数字）：6 位地址码 + 4 位年份（19xx/20xx）+ 2 位月 + 2 位日
#: + 3 位顺序码 + 1 位校验位。**年份段必须是 19xx/20xx** —— 纯 18 位数字
#: （比如某个纯数字检查号）不会撞上。
_ID_CARD_BARE_RE = re.compile(
    r"(?<![0-9A-Za-z])"
    r"(\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])"
    r"(?![0-9A-Za-z])"
)
#: 身份证号（带标签）：「身份证号：11010119900307xxxx」这种写法。
#: **18 位在前、15 位在后** —— 反过来会把 18 位证件号截成前 15 位。
_ID_CARD_LABELED_RE = re.compile(
    r"(?:身份证(?:号码?|件号)?|证件号码?)\s*[:：]?\s*([0-9Xx]{18}|[0-9Xx]{15})"
)
#: 住院号 / 病案号 / 就诊号：**只认带标签的写法**。裸的住院号（6~10 位数字）
#: 与检查号、科室编码长得一模一样，认不得。
_ADMISSION_NO_RE = re.compile(
    r"(?:住院号|住院流水号|病案号|病案首页号|就诊号|住院证号)"
    r"\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9\-]{2,})"
)
#: 手机号（裸）：11 位、以 13~19 开头。
_PHONE_BARE_RE = re.compile(r"(?<![0-9])1[3-9]\d{9}(?![0-9])")
#: 手机号（带标签）。
_PHONE_LABELED_RE = re.compile(
    r"(?:手机号码?|手机号|联系电话?|电话)\s*[:：]?\s*(1[3-9]\d{9})"
)
#: 中文姓名（带标签的自由文本）：「姓名：张三」「患者姓名：李四」。
_NAME_LABELED_RE = re.compile(
    r"(?:患者|病人|受检者)?姓名\s*[:：]\s*([㐀-䶿一-鿿·]{2,6})"
)

#: **病历抬头里的姓名**：「张三，男，58岁」—— 放射科写金标准时最常见的开头。
#:
#: 只在**脱敏**时用（:func:`mask_pii`），**不进扫描器**：它的判据是
#: 「姓名的形状 + 后面跟性别 + 后面跟逗号」，自由文本里这个形状偶尔会撞上
#: 别的词（患者、复查……）。扫描器宁可漏报也不能误报 —— 一条会误报的闸门，
#: 用不了两周就被人关掉了。
_NAME_PREAMBLE_RE = re.compile(
    r"([㐀-䶿一-鿿·]{2,4})(?=\s*[，,]\s*(?:男|女)(?:性)?\s*[，,、。；;\s])"
)

#: 抬头里常见的**非姓名**词（脱敏时放过，别把「患者」当成姓名抹掉）。
_NAME_PREAMBLE_STOPWORDS: frozenset[str] = frozenset(
    {
        "患者",
        "病人",
        "受检者",
        "本人",
        "该患者",
        "本患者",
        "复查",
        "门诊",
        "住院",
        "出院",
        "入院",
        "手术",
        "胸片",
        "胸部",
        "正位",
        "侧位",
        "平片",
        "片子",
        "影像",
        "报告",
        "检查",
        "临床",
        "病史",
        "主诉",
        "现病史",
    }
)

#: **字段名**上带「姓名」含义的键（精确匹配，不做子串匹配 ——
#: ``model_name`` / ``file_name`` 里也有 name，不能误伤）。
NAME_FIELD_KEYS: frozenset[str] = frozenset(
    {
        "姓名",
        "患者姓名",
        "病人姓名",
        "受检者姓名",
        "姓名全称",
        "患者名",
        "病人名",
        "name",
        "patient_name",
        "patientname",
        "full_name",
        "real_name",
    }
)

#: 值长得像中文姓名（2~6 个汉字，可含间隔号「·」用于复姓）。
_NAME_VALUE_RE = re.compile(r"^[㐀-䶿一-鿿·]{2,6}$")

#: 模式表（顺序即优先级：带标签的先扫，裸的后面补位；重叠时先到先得）。
_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (KIND_ID_CARD, _ID_CARD_LABELED_RE),
    (KIND_ADMISSION_NO, _ADMISSION_NO_RE),
    (KIND_PHONE, _PHONE_LABELED_RE),
    (KIND_PATIENT_NAME, _NAME_LABELED_RE),
    (KIND_ID_CARD, _ID_CARD_BARE_RE),
    (KIND_PHONE, _PHONE_BARE_RE),
)

#: **白名单键**：这些键的值是业务标识或内容哈希，允许出现数字，扫描器不误报。
#:
#: - ``accession_no`` —— 业务检查号，产物里**必须**有它（这是与院内对得上的
#:   唯一钥匙），它本身不是患者明细；
#: - ``*_sha256`` —— 内容哈希；
#: - ``tool_call_id`` / ``model_call_id`` / ``study_uid`` / ``image_ref``
#:   —— 本系统与 RIS 的标识；
#: - ``prompt_version`` —— 提示词版本号（如 ``v1``），不是患者明细。
ALLOWED_VALUE_KEYS: frozenset[str] = frozenset(
    {
        "accession_no",
        "tool_call_id",
        "model_call_id",
        "study_uid",
        "image_ref",
        "row_no",
        "case_id",
        "archived_files",
        "list_path",
        "prompt_template",
        "prompt_version",
    }
)

#: 递归扫描的最大深度（防御畸形产物导致的无限递归）。
_MAX_DEPTH = 24


# --------------------------------------------------------------------------
# 产物字段白名单
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactSpec:
    """一类产物的字段白名单。

    ``allowed_fields``
        这一类产物里**允许出现**的字段名（扁平一份，任意层级通用）。
        产物里出现不在册的字段 → 扫描器报「未登记字段」。
    ``opaque_fields``
        这些键的值是**外部内容**（厂商返回的原始 JSON 等）——
        里面的字段名不是我们写的，管不着；但里面的**值**照样要扫
        （厂商回显了患者姓名，不能因为是别人的返回就放过）。
    ``path_hint``
        同名产物靠它区分（``cases/<检查号>/case.json`` 与
        ``cases/<检查号>/attempt-0001/case.json`` 是两种产物）。
    """

    key: str
    filename: str
    allowed_fields: tuple[str, ...]
    opaque_fields: tuple[str, ...] = ()
    path_hint: str = ""


#: 计数类字段（多个产物共用）。
_COUNT_FIELDS: tuple[str, ...] = (
    "tp",
    "fp",
    "fn",
    "tn",
    "counted",
    "failed",
    "needs_review",
    "incomparable",
    "rule_degraded",
    "model_normalized",
)
#: 指标类字段。
_METRIC_FIELDS: tuple[str, ...] = (
    "sensitivity",
    "specificity",
    "ppv",
    "npv",
    "failed_count",
    "rule_degraded_count",
    "needs_review_count",
    "incomparable_count",
    "model_normalized_count",
    "sensitivity_reason",
    "specificity_reason",
    "ppv_reason",
    "npv_reason",
)
#: 判定分布字段。
_VERDICT_FIELDS: tuple[str, ...] = ("hit", "miss", "false_positive", "true_negative")
#: 逐例行的字段（``CaseRow.to_dict`` 的全部键）。
_CASE_ROW_FIELDS: tuple[str, ...] = (
    "accession_no",
    "verdict",
    "model_findings",
    "failure_reason",
    "tool_call_id",
    "needs_review",
    "normalize_source",
    "row_no",
    "error_code",
    "held",
    "rule_degraded",
    "degrade_reason",
    "judge_basis",
    "uncovered_labels",
    "incomparable",
    "incomparable_reason",
    "gold_summary",
)
#: 金标准标签字段（``GoldLabels.to_dict`` + ``LesionLabels.to_dict``）。
_GOLD_LABEL_FIELDS: tuple[str, ...] = (
    "presence",
    "location",
    "size_mm",
    "density",
    "lesions",
    "evidence_span",
    "confidence",
    "needs_human_review",
    "source",
    "degraded",
    "degrade_reason",
    "degrade_error_code",
    "finding",
)
#: 判定字段（``Judgement.to_dict``）。
_JUDGEMENT_FIELDS: tuple[str, ...] = (
    "verdict",
    "reason",
    "layers",
    "gold_presence",
    "detected",
    "candidate_count",
    "error_code",
    "incomparable_layers",
    "incomparable_reason",
    "selected_candidate",
    "selection_basis",
    "attribute_notes",
    "lesion_total",
    "lesion_covered",
    "coverage_state",
    "uncovered_lesions",
    "incomparable_lesions",
    "coverage_notes",
    "basis",
)
#: 覆盖条目字段（``LesionCoverage.to_dict``）。
_COVERAGE_FIELDS: tuple[str, ...] = (
    "index",
    "state",
    "candidate_index",
    "anchor_layer",
    "reason",
    "label",
    "lesion",
)
#: 失败记录字段（``CaseFailure.to_dict``）。
_FAILURE_FIELDS: tuple[str, ...] = (
    "error_code",
    "failure_reason",
    "http_status",
    "failure_class",
    "endpoint",
    "endpoint_label",
    "retryable",
    "counts_toward_metrics",
    "message",
)


def _spec(
    key: str,
    filename: str,
    allowed: Iterable[str],
    *,
    opaque: Iterable[str] = (),
    path_hint: str = "",
) -> ArtifactSpec:
    return ArtifactSpec(
        key=key,
        filename=filename,
        allowed_fields=tuple(dict.fromkeys(allowed)),
        opaque_fields=tuple(opaque),
        path_hint=path_hint,
    )


def _model_call_meta_fields() -> tuple[str, ...]:
    """模型调用元数据的字段名 —— **取自协议层自己的声明**。

    协议细节（降级标记 / 失败类别 / 尝试次数那些）**不在审计层硬写一份**：
    协议层加字段，审计层自动跟上；字段名也只出现在协议层
    （守住「``response_format`` 只在协议层出现」的静态扫描红线）。
    """

    from ..gateway import ModelCallMeta

    return tuple(ModelCallMeta.__dataclass_fields__)


#: **全部产物的字段白名单** —— 单一真源。
#:
#: 新增字段时**必须**在这里登记，否则扫描器会报「未登记字段」——
#: 这正是这道闸门要拦的事：加字段的人忘了登记，字段里带的明细就出去了。
ARTIFACT_SPECS: tuple[ArtifactSpec, ...] = (
    _spec(
        "case_rows.csv",
        "case_rows.csv",
        CASE_ROW_COLUMNS,
    ),
    _spec(
        "missed_list.csv",
        "missed_list.csv",
        MISSED_LIST_COLUMNS,
    ),
    _spec(
        "summary.json",
        "summary.json",
        (
            "run_id",
            "model_id",
            "model_version",
            "dataset_ref",
            "generated_at",
            "version_no",
            "skipped_count",
            "total_cases",
            "evaluated_cases",
            "counts",
            "metrics",
            "summary_text",
            "verdict_distribution",
            "missed",
            "degrade_note",
            "manual_check",
            "count",
            "fn",
            "list_path",
            "not_counted",
            "consistent",
            "declared",
            "recomputed",
            "formulas",
            "differences",
            # **失败分布段**（任务 42）：按错误码汇总失败例 ——
            # 与终端输出、表格 failure_reason 列同源（文案只从 errors.py 取）。
            "failures",
            "total",
            "by_code",
            "message",
            "samples",
            "retryable",
            "counts_toward_metrics",
            "trigger_points",
        )
        + _COUNT_FIELDS
        + _METRIC_FIELDS
        + _VERDICT_FIELDS,
    ),
    _spec(
        "result.json",
        "result.json",
        (
            "run_id",
            "status",
            "case_rows",
            "metrics",
            "missed_list",
            "summary",
            "gold_standard_summary",
            "missed_reason",
            "failed_count",
            "rule_degraded_count",
            "needs_review_count",
            "incomparable_count",
            "model_normalized_count",
            "total_cases",
            "evaluated_cases",
            "generated_at",
            "skipped_count",
            "version_no",
            "degrade_note",
            "model_id",
            "model_version",
            "dataset_ref",
            "tp",
            "fp",
            "fn",
            "tn",
        )
        + _CASE_ROW_FIELDS
        + _METRIC_FIELDS,
    ),
    _spec(
        "case_timings.json",
        "case_timings.json",
        (
            "run_id",
            "concurrency",
            "endpoints",
            "case_count",
            "cases",
            "accession_no",
            "elapsed_ms",
            "queued_ms",
            "verdict",
            "error_code",
            "needs_review",
            "skipped",
            "endpoint",
            "max_concurrency",
            "acquisitions",
            "queued",
            "waited_seconds",
            "peak_in_flight",
            "in_flight",
            "inference",
            "model",
            "ris",
            "research",
        ),
    ),
    _spec(
        "tool_calls.jsonl",
        "tool_calls.jsonl",
        (
            "tool_call_id",
            "name",
            "args",
            "content",
            "timestamp",
            "accession_no",
            "study_uid",
            "image_ref",
            "model_id",
            "model_version",
        ),
        # ``content`` 是**厂商返回的原始 JSON** —— 里面的字段名不是我们写的，
        # 管不着；但里面的值照样要扫（厂商回显了患者姓名不能放过）。
        opaque=("content",),
    ),
    _spec(
        "model_calls.jsonl",
        "model_calls.jsonl",
        (
            # 账本**自己**的六项（审计层自己定的）。
            "model_call_id",
            "accession_no",
            "prompt_template",
            "prompt_template_sha256",
            "prompt_version",
            "prompt_rendered_sha256",
        )
        # 其余是协议层的元数据字段 —— 取自协议层自己的声明。
        + _model_call_meta_fields(),
    ),
    _spec(
        "transitions.jsonl",
        "transitions.jsonl",
        ("scope", "run_id", "subject", "from", "to", "actor", "reason", "at"),
    ),
    _spec(
        "binding.json",
        "binding.json",
        (
            "run_id",
            "authority",
            "generated_at",
            "counts",
            "entries",
            "note",
            "accession_no",
            "row_no",
            "status",
            "study_uid",
            "image_ref",
            "study_uid_source",
            "image_ref_source",
            "needs_review",
            "error_code",
            "message",
            "sheet_image_ref",
            "sheet_study_uid",
            "candidates",
            "differences",
            "study_date",
            "modality",
            "report_status",
            "field",
            "sheet_value",
            "authority_value",
            "total",
            "bound",
            "ambiguous",
            "unresolved",
            "duplicate",
            "with_difference",
        ),
    ),
    _spec(
        "caseset_manifest.json",
        "caseset_manifest.json",
        (
            "run_id",
            "cases_root",
            "source",
            "generated_at",
            "requested",
            "assembled",
            "skipped",
            "unresolved",
            "ambiguous",
            "damaged_dirs",
            "counts",
            "ris_counts",
            "note",
        ),
    ),
    _spec(
        "case_attempt.json",
        "case.json",
        (
            "accession_no",
            "row_no",
            "attempted_at",
            "attempt",
            "tool_call_id",
            "call_name",
            "call_args",
            "model_findings",
            "gold_labels",
            "judgement",
            "verdict",
            "failure_reason",
            "needs_review",
            "normalize_source",
            "error_code",
            "failure",
            "degradation",
            "held",
            "gold_standard_sha256",
            "redaction",
            "degraded",
            "reason",
            "counts_toward_metrics",
            # ``call_args`` 里的四项与 ``judgement.layers`` 的四个层名。
            "study_uid",
            "image_ref",
            "model_id",
            "model_version",
            "presence",
            "size",
            "location",
            "density",
        )
        + _CASE_ROW_FIELDS
        + _GOLD_LABEL_FIELDS
        + _JUDGEMENT_FIELDS
        + _COVERAGE_FIELDS
        + _FAILURE_FIELDS,
        path_hint="attempt-",
    ),
    _spec(
        "caseset_case.json",
        "case.json",
        (
            "accession_no",
            "study_uid",
            "image_ref",
            "modality",
            "study_date",
            "report_status",
            "assembled_at",
            "source",
        ),
    ),
    _spec(
        "version.json",
        "version.json",
        (
            "version_no",
            "mode",
            "status",
            "started_at",
            "finished_at",
            "detail",
            "archived_files",
            "processed_cases",
            "skipped_cases",
            "total_cases",
            "run_id",
        ),
    ),
    _spec(
        "writeback_receipt.json",
        "writeback_receipt.json",
        (
            "run_id",
            "idempotency_key",
            "status",
            "registration_id",
            "submitted_at",
            "received_at",
            "http_status",
            "attempts",
            "error_code",
            "error_detail",
            "actor",
            "response_summary",
            "payload_sha256",
            "updated_at",
            # ``response_summary`` 里是科研平台回执规范声明的那几项
            "received_at",
        ),
    ),
    _spec(
        "writeback_audit.jsonl",
        "writeback_audit.jsonl",
        (
            "at",
            "run_id",
            "actor",
            "action",
            "gate",
            "registration_id",
            "idempotency_key",
            "http_status",
            "attempts",
            "submitted_by",
            "rule_degraded_count",
            "model_normalized_count",
            "total_cases",
            # 写回审计要能回答「发出去的那一版到底是什么」——
            # 模型与数据集标识 + 请求体内容摘要 + 字节数 + 对端回执状态。
            "model_id",
            "model_version",
            "dataset_ref",
            "payload_sha256",
            "payload_bytes",
            "response_status",
            "note",
            "converged",
            "error_code",
            "error_detail",
            "receipt_status",
            # ``gate`` 是闸门判定结果，字段与 ``GateDecision`` 一致
            "allowed",
            "current_status",
            "required_status",
            "reason",
        ),
    ),
)

#: **登记台账自己**也是产物 —— 它归谁登记？归它自己。
#: 台账里 ``artifacts`` 的键就是各产物的类型名，所以这些键也要在白名单里。
ARTIFACT_SPECS = ARTIFACT_SPECS + (
    _spec(
        "artifact_fields.json",
        "artifact_fields.json",
        ("run_id", "artifacts", "registered_at", "fields")
        + tuple(spec.key for spec in ARTIFACT_SPECS),
    ),
)


#: 字段白名单台账的文件名（落在 ``runs/<run_id>/`` 下）。
ARTIFACT_FIELDS_FILENAME = "artifact_fields.json"


def _spec_for(rel_path: Path) -> ArtifactSpec | None:
    """按（文件名 + 路径特征）找这份产物对应的字段白名单。"""

    text = rel_path.as_posix()
    for spec in ARTIFACT_SPECS:
        if spec.filename != rel_path.name:
            continue
        if spec.path_hint and spec.path_hint not in text:
            continue
        return spec
    return None


# --------------------------------------------------------------------------
# 检出
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PiiHit:
    """文本里的一处可识别明细。"""

    kind: str
    start: int
    end: int
    text: str

    @property
    def label(self) -> str:
        return PII_KIND_LABELS.get(self.kind, self.kind)


def detect_pii(text: str) -> list[PiiHit]:
    """在一段文本里检出可识别明细。

    重叠的命中**只留一处**（先到先得，带标签的优先）——
    「身份证号：110101199003071234」不该既算带标签的又算裸的。
    """

    if not text:
        return []

    hits: list[PiiHit] = []
    for kind, pattern in _TEXT_PATTERNS:
        for match in pattern.finditer(text):
            hits.append(
                PiiHit(
                    kind=kind,
                    start=match.start(),
                    end=match.end(),
                    text=match.group(0),
                )
            )
    hits.sort(key=lambda hit: (hit.start, -(hit.end - hit.start)))

    kept: list[PiiHit] = []
    cursor = -1
    for hit in hits:
        if hit.start < cursor:
            continue
        kept.append(hit)
        cursor = hit.end
    return kept


def scan_text(text: str) -> list[PiiHit]:
    """扫一段自由文本（公开别名，便于外部调用）。"""

    return detect_pii(text)


def mask_pii(text: str) -> tuple[str, list[str]]:
    """把一段文本里的可识别明细**替换掉**，返回（脱敏后的文本, 脱敏的种类）。

    这是**产品侧**的兜底：金标准原文派生的片段（``evidence_span``）在落产物
    前先过这一道，产物里就不留明细。

    与扫描器的规矩**相反**且不冲突：扫描器**只报不改**（底账必须原样），
    这里改的是**我们自己派生的片段**（原文仍在院内评估表里，改动不影响
    回指核对）。
    """

    if not text:
        return text, []

    hits = list(detect_pii(text))
    kinds: list[str] = [hit.kind for hit in hits]

    # **病历抬头里的姓名**（「张三，男，58岁」）—— 只在脱敏时认。
    for match in _NAME_PREAMBLE_RE.finditer(text):
        if match.group(1) in _NAME_PREAMBLE_STOPWORDS:
            continue
        # 与已命中的区域重叠就跳过 —— 同一段文本不替换两次。
        if any(
            match.start(1) < hit.end and hit.start < match.end(1) for hit in hits
        ):
            continue
        hits.append(
            PiiHit(
                kind=KIND_PATIENT_NAME,
                start=match.start(1),
                end=match.end(1),
                text=match.group(1),
            )
        )
        kinds.append(KIND_PATIENT_NAME)

    if not hits:
        return text, []

    # 从后往前替换，位置才不会错位。
    hits.sort(key=lambda hit: hit.start)
    masked = text
    for hit in reversed(hits):
        masked = masked[: hit.start] + f"【已脱敏·{hit.label}】" + masked[hit.end :]
    return masked, sorted(set(kinds))


def mask_payload(value: Any, *, key: str = "") -> tuple[Any, list[str]]:
    """把产物载荷里的可识别明细脱敏掉，返回（脱敏后的载荷, 脱敏的种类）。

    **白名单键**（检查号、哈希、调用 id）原样保留 —— 它们不是患者明细，
    抹掉反而对不上账。
    """

    kinds: list[str] = []

    def _mask_text(text: str) -> str:
        nonlocal kinds
        masked, hit_kinds = mask_pii(text)
        kinds.extend(hit_kinds)
        return masked

    def _walk(node: Any, node_key: str, depth: int) -> Any:
        if depth > _MAX_DEPTH:
            return node
        if isinstance(node, Mapping):
            out: dict[str, Any] = {}
            for child_key, child in node.items():
                if _is_name_key(child_key) and _is_chinese_name(child):
                    out[child_key] = "【已脱敏·患者姓名】"
                    kinds.append(KIND_PATIENT_NAME)
                    continue
                out[child_key] = _walk(child, child_key, depth + 1)
            return out
        if isinstance(node, list):
            return [_walk(item, node_key, depth + 1) for item in node]
        if isinstance(node, str):
            if _is_allowed_key(node_key):
                return node
            return _mask_text(node)
        return node

    return _walk(value, key, 0), sorted(set(kinds))


def _is_allowed_key(key: str) -> bool:
    """这个键的值是不是「业务标识 / 内容哈希」（不参与脱敏）。"""

    if not key:
        return False
    if key in ALLOWED_VALUE_KEYS:
        return True
    return key.endswith("_sha256")


def _is_name_key(key: str) -> bool:
    """这个键是不是「姓名」字段（精确匹配，不做子串匹配）。"""

    normalized = key.strip().lower().replace(" ", "_")
    return normalized in NAME_FIELD_KEYS


def _is_chinese_name(value: Any) -> bool:
    """这个值长得像中文姓名吗。"""

    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    return bool(_NAME_VALUE_RE.match(text))


# --------------------------------------------------------------------------
# 扫描结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanFinding:
    """一处扫描命中。

    ``excerpt`` 是**脱敏后**的片段 —— 扫描报告本身也是要往外发的东西，
    报告里不能再带一遍明细。
    """

    file: str
    kind: str
    where: str
    excerpt: str
    detail: str = ""

    @property
    def label(self) -> str:
        return PII_KIND_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "kind": self.kind,
            "kind_label": self.label,
            "where": self.where,
            "excerpt": self.excerpt,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ScanReport:
    """一轮扫描的结果。"""

    run_id: str
    scanned_files: tuple[str, ...] = ()
    findings: tuple[ScanFinding, ...] = ()
    registered: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """没有命中才算干净。"""

        return not self.findings

    def counts(self) -> dict[str, int]:
        """按种类数命中。"""

        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.kind] = counts.get(finding.kind, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ok": self.ok,
            "scanned_files": list(self.scanned_files),
            "scanned_count": len(self.scanned_files),
            "findings": [finding.to_dict() for finding in self.findings],
            "counts": self.counts(),
        }


# --------------------------------------------------------------------------
# 字段白名单台账
# --------------------------------------------------------------------------


def _ledger_path(run_dir: Path | str) -> Path:
    return Path(run_dir) / ARTIFACT_FIELDS_FILENAME


def register_artifact_fields(
    run_dir: Path | str,
    artifact_key: str,
) -> Path:
    """写产物时同步登记这一类产物的字段白名单。

    登记的是**声明的白名单**（:data:`ARTIFACT_SPECS`），不是「实际写了哪些
    字段」—— 否则新加的字段会自己登记自己，闸门形同虚设。

    登记台账落 ``runs/<run_id>/artifact_fields.json``；事后扫描拿它对账：
    产物里出现了台账上没有的字段 → 有人加了字段没登记。
    """

    spec = _spec_by_key(artifact_key)
    if spec is None:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"没有登记在册的产物类型：{artifact_key}",
        )

    path = _ledger_path(run_dir)
    payload = _read_ledger(path)
    payload.setdefault("run_id", Path(run_dir).name)
    artifacts = payload.setdefault("artifacts", {})
    artifacts[spec.key] = {
        "fields": list(spec.allowed_fields),
        "registered_at": _now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_artifact_fields(run_dir: Path | str) -> dict[str, tuple[str, ...]]:
    """读回字段白名单台账（没有台账就返回空表）。"""

    payload = _read_ledger(_ledger_path(run_dir))
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return {}
    registered: dict[str, tuple[str, ...]] = {}
    for key, entry in artifacts.items():
        if not isinstance(entry, Mapping):
            continue
        fields = entry.get("fields")
        if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
            continue
        registered[str(key)] = tuple(str(name) for name in fields)
    return registered


def _read_ledger(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _spec_by_key(key: str) -> ArtifactSpec | None:
    for spec in ARTIFACT_SPECS:
        if spec.key == key:
            return spec
    return None


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------


def scan_run_artifacts(run_dir: Path | str) -> ScanReport:
    """扫一遍 ``runs/<run_id>/`` 下的全部产物。

    返回一份 :class:`ScanReport`：**有命中就是不合格**（调用方决定要不要
    报错退出；:func:`assert_artifacts_clean` 就是那个「报错退出」的口子）。
    """

    root = Path(run_dir)
    if not root.is_dir():
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"产物目录不存在：{root}",
        )

    registered = load_artifact_fields(root)
    findings: list[ScanFinding] = []
    scanned: list[str] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        scanned.append(rel.as_posix())
        findings.extend(_scan_file(path, rel, registered))

    return ScanReport(
        run_id=root.name,
        scanned_files=tuple(scanned),
        findings=tuple(findings),
        registered=registered,
    )


def _scan_file(
    path: Path,
    rel: Path,
    registered: Mapping[str, tuple[str, ...]],
) -> list[ScanFinding]:
    """扫一份产物文件。"""

    spec = _spec_for(rel)
    suffix = path.suffix.lower()
    findings: list[ScanFinding] = []

    if suffix == ".json":
        payload = _read_json(path)
        if payload is None:
            return findings
        _walk_payload(
            payload,
            file=rel.as_posix(),
            where="",
            spec=spec,
            allowed=registered.get(spec.key, spec.allowed_fields) if spec else (),
            check_fields=spec is not None,
            findings=findings,
            depth=0,
        )
        return findings

    if suffix == ".jsonl":
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            _walk_payload(
                payload,
                file=rel.as_posix(),
                where=f"第 {line_no} 行",
                spec=spec,
                allowed=registered.get(spec.key, spec.allowed_fields) if spec else (),
                check_fields=spec is not None,
                findings=findings,
                depth=0,
            )
        return findings

    if suffix == ".csv":
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                headers = list(reader.fieldnames or [])
                rows = list(reader)
        except (OSError, UnicodeDecodeError):
            return findings
        if spec is not None:
            allowed = registered.get(spec.key, spec.allowed_fields)
            for header in headers:
                if header not in allowed:
                    findings.append(
                        ScanFinding(
                            file=rel.as_posix(),
                            kind=KIND_UNREGISTERED_FIELD,
                            where=f"表头「{header}」",
                            excerpt=header,
                            detail=(
                                f"字段「{header}」不在 {spec.key} 的字段白名单里；"
                                "新增字段必须先登记，否则明细可能跟着出去"
                            ),
                        )
                    )
        for row_no, row in enumerate(rows, start=2):
            for column, value in row.items():
                if column is None or not isinstance(value, str):
                    continue
                _scan_text_value(
                    value,
                    file=rel.as_posix(),
                    where=f"第 {row_no} 行 · {column} 列",
                    findings=findings,
                    key=str(column),
                )
        return findings

    # 其余文件（.txt / .log 等）：只扫内容，不查字段。
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return findings
    _scan_text_value(
        text,
        file=rel.as_posix(),
        where="全文",
        findings=findings,
    )
    return findings


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _walk_payload(
    value: Any,
    *,
    file: str,
    where: str,
    spec: ArtifactSpec | None,
    allowed: tuple[str, ...],
    check_fields: bool,
    findings: list[ScanFinding],
    depth: int,
    key: str = "",
) -> list[ScanFinding]:
    """走一遍产物载荷：既查字段是否登记，也查值里有没有明细。"""

    if depth > _MAX_DEPTH:
        return findings

    if isinstance(value, Mapping):
        for child_key, child in value.items():
            child_where = f"{where} · {child_key}" if where else str(child_key)
            if check_fields and child_key not in allowed:
                findings.append(
                    ScanFinding(
                        file=file,
                        kind=KIND_UNREGISTERED_FIELD,
                        where=child_where,
                        excerpt=str(child_key),
                        detail=(
                            f"字段「{child_key}」不在 {spec.key if spec else ''} "
                            "的字段白名单里；新增字段必须先登记再落盘"
                        ),
                    )
                )
            if _is_name_key(child_key) and _is_chinese_name(child):
                findings.append(
                    ScanFinding(
                        file=file,
                        kind=KIND_PATIENT_NAME,
                        where=child_where,
                        excerpt="【已脱敏·患者姓名】",
                        detail=(
                            f"字段「{child_key}」里是中文姓名 —— 产物只落检查号与内容哈希"
                        ),
                    )
                )
                continue
            opaque = spec is not None and child_key in spec.opaque_fields
            _walk_payload(
                child,
                file=file,
                where=child_where,
                spec=spec,
                allowed=allowed,
                check_fields=check_fields and not opaque,
                findings=findings,
                depth=depth + 1,
                key=str(child_key),
            )
        return findings

    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk_payload(
                item,
                file=file,
                where=f"{where}[{index}]",
                spec=spec,
                allowed=allowed,
                check_fields=check_fields,
                findings=findings,
                depth=depth + 1,
                key=key,
            )
        return findings

    if isinstance(value, str):
        if _is_allowed_key(key):
            return findings
        # **嵌在字符串里的 JSON**（厂商返回的原始报文就在 ``content`` 里）：
        # 按结构再走一遍 —— 里面的姓名字段才认得出来。
        nested = _parse_json_text(value)
        if nested is not None:
            _walk_payload(
                nested,
                file=file,
                where=where,
                spec=spec,
                allowed=allowed,
                check_fields=check_fields,
                findings=findings,
                depth=depth + 1,
                key=key,
            )
            return findings
        _scan_text_value(
            value,
            file=file,
            where=where,
            findings=findings,
            key=key,
        )
        return findings

    return findings


def _parse_json_text(value: str) -> Any:
    """这段文本是不是一段 JSON？是就解析出来（不是就返回 ``None``）。"""

    text = value.strip()
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _scan_text_value(
    text: str,
    *,
    file: str,
    where: str,
    findings: list[ScanFinding],
    key: str = "",
) -> list[ScanFinding]:
    """扫一段文本，把命中记进 ``findings``（顺带返回，方便链式调用）。

    ``key`` 是这段文字所在的**字段名** —— 业务标识与内容哈希走白名单，
    不参与扫描（检查号撞上手机号形状不算明细）。
    """

    if _is_allowed_key(key):
        return findings

    hits = detect_pii(text)
    for hit in hits:
        findings.append(
            ScanFinding(
                file=file,
                kind=hit.kind,
                where=where,
                excerpt=_masked_excerpt(text, hit),
                detail=f"命中{hit.label}",
            )
        )
    return findings


def _masked_excerpt(text: str, hit: PiiHit) -> str:
    """取命中位置周围的一小段，**并且把明细本身抹掉**。"""

    start = max(0, hit.start - 12)
    end = min(len(text), hit.end + 12)
    excerpt = text[start:hit.start] + f"【已脱敏·{hit.label}】" + text[hit.end : end]
    return excerpt.replace("\n", " ")


# --------------------------------------------------------------------------
# 闸门
# --------------------------------------------------------------------------


def assert_artifacts_clean(run_dir: Path | str) -> ScanReport:
    """扫一遍产物，**有命中就报错**（不静默通过）。

    两种命中分两种错误码：

    * 患者可识别明细 → ``ARTIFACT_PII_DETECTED``；
    * 未登记字段 → ``ARTIFACT_FIELD_UNREGISTERED``。
    """

    report = scan_run_artifacts(run_dir)
    if report.ok:
        return report

    pii = [finding for finding in report.findings if finding.kind != KIND_UNREGISTERED_FIELD]
    unregistered = [
        finding for finding in report.findings if finding.kind == KIND_UNREGISTERED_FIELD
    ]

    if pii:
        first = pii[0]
        detail = (
            f"{len(pii)} 处可识别明细；例如 {first.file}（{first.where}）："
            f"{first.detail}。产物只应落检查号与内容哈希，请清掉后重跑"
            "（扫描器不做静默替换）"
        )
        raise NoduleEvalError(ErrorCode.ARTIFACT_PII_DETECTED, detail=detail)

    first = unregistered[0]
    detail = (
        f"{len(unregistered)} 个未登记字段；例如 {first.file}（{first.where}）："
        f"{first.detail}"
    )
    raise NoduleEvalError(ErrorCode.ARTIFACT_FIELD_UNREGISTERED, detail=detail)


def render_scan_report(report: ScanReport) -> str:
    """把扫描报告渲染成终端可读的中文文本。"""

    lines: list[str] = []
    lines.append(f"产物脱敏扫描：{report.run_id}")
    lines.append(f"已扫描产物：{len(report.scanned_files)} 份")
    if report.ok:
        lines.append("结论：干净 —— 未检出患者可识别明细，字段登记一致。")
        return "\n".join(lines)

    counts = report.counts()
    summary = "、".join(
        f"{PII_KIND_LABELS.get(kind, kind)} {count} 处" for kind, count in counts.items()
    )
    lines.append(f"结论：不干净 —— 命中 {len(report.findings)} 处（{summary}）")
    lines.append("")
    for finding in report.findings:
        lines.append(
            f"  [{finding.label}] {finding.file} · {finding.where}：{finding.excerpt}"
        )
    lines.append("")
    lines.append("扫描器只报不改 —— 请确认这些明细的来源后处理，再重跑这一轮。")
    return "\n".join(lines)
