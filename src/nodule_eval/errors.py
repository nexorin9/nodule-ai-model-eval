"""统一错误码表。

同一张表被三处共用：终端输出、评估表新增的 ``failure_reason`` 列、``summary.json``。
**同一个错误码在三处必须是同一句话** —— 文案只从这里取，业务代码不得散落硬编码
（:mod:`nodule_eval.error_matrix` 扫描断言这一点）。

每个错误码声明五件事（任务 42 的落地矩阵）：

``message``
    中文文案模板，``{占位符}`` 由 :func:`format_message` 填充。
``trigger_points``
    **在哪儿触发** —— 这一句是人话说明「什么情况下会走到这个错误码」，
    落地矩阵的「触发点」一列直接取它。
``retryable``
    是否值得重试。4xx 参数类一律不可重试，避免把退避预算浪费在必然失败的请求上。
``counts_toward_metrics``
    该错误发生后，这一例**是否仍进入指标分母**。只有调用类失败（超时 / 5xx /
    study_uid 不回显 / 响应缺字段）为 ``False``；绑定歧义与人工挂起为 ``True``
    但单独计数，不混入 TP / FP / FN / TN。
``visible_in``
    用户能在哪儿看到它 —— 表格哪一列、终端哪一行、产物哪个文件。
    取值来自本模块的 ``SURFACE_*`` 常量（受控词表），便于机器核对
    「同一个错误码在三处是不是同一句话」。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ErrorCode(str, Enum):
    """全部错误码。取值即字符串，可直接写进 JSON 产物。"""

    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
    CONTRACT_INVALID = "CONTRACT_INVALID"
    RIS_READ_FAILED = "RIS_READ_FAILED"
    AMBIGUOUS_BINDING = "AMBIGUOUS_BINDING"
    INFERENCE_4XX = "INFERENCE_4XX"
    INFERENCE_5XX = "INFERENCE_5XX"
    INFERENCE_TIMEOUT = "INFERENCE_TIMEOUT"
    STUDY_UID_MISMATCH = "STUDY_UID_MISMATCH"
    FIELD_MAPPING_MISSING = "FIELD_MAPPING_MISSING"
    RESEARCH_WRITE_FAILED = "RESEARCH_WRITE_FAILED"
    RESEARCH_PAYLOAD_TOO_LARGE = "RESEARCH_PAYLOAD_TOO_LARGE"
    WRITEBACK_NOT_REVIEWED = "WRITEBACK_NOT_REVIEWED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_REQUEST_REJECTED = "MODEL_REQUEST_REJECTED"
    RULE_DEGRADED = "RULE_DEGRADED"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    TOOL_CALL_NOT_FOUND = "TOOL_CALL_NOT_FOUND"
    INSPECT_ARTIFACT_INCOMPLETE = "INSPECT_ARTIFACT_INCOMPLETE"
    ARTIFACT_PII_DETECTED = "ARTIFACT_PII_DETECTED"
    ARTIFACT_FIELD_UNREGISTERED = "ARTIFACT_FIELD_UNREGISTERED"


# --------------------------------------------------------------------------
# 用户可见位置的受控词表
# --------------------------------------------------------------------------
#: 评估表新增的失败原因列。
SURFACE_TABLE = "表格 failure_reason 列"
#: 命令行终端输出。
SURFACE_TERMINAL = "终端输出"
#: run 级结论 ``summary.json``。
SURFACE_SUMMARY = "summary.json"
#: 逐例调用底账。
SURFACE_TOOL_CALLS = "tool_calls.jsonl"
#: 模型调用底账。
SURFACE_MODEL_CALLS = "model_calls.jsonl"
#: 写回底账。
SURFACE_WRITEBACK_AUDIT = "writeback_audit.jsonl"
#: 状态迁移底账。
SURFACE_TRANSITIONS = "transitions.jsonl"
#: 本轮产物目录。
SURFACE_ARTIFACTS = "runs/<run_id>/ 产物目录"
#: ``doctor`` 子命令（启动即体检）。
SURFACE_DOCTOR = "终端输出（doctor）"
#: ``inspect`` 子命令（按调用 ID 反查）。
SURFACE_INSPECT = "终端输出（inspect）"
#: ``redact-scan`` 子命令（产物体检）。
SURFACE_REDACT_SCAN = "终端输出（redact-scan）"

#: **逐例失败必须出现在的三个面** —— 同一个错误码在这三处是同一句话。
#: 落地矩阵据此核对：表格 ``failure_reason`` 列、终端输出、``summary.json``。
USER_VISIBLE_SURFACES: tuple[str, ...] = (
    SURFACE_TABLE,
    SURFACE_TERMINAL,
    SURFACE_SUMMARY,
)

#: 用户可见文案的行宽上限（表格一格一行；超长截断）。
FAILURE_TEXT_LIMIT = 500


@dataclass(frozen=True)
class ErrorSpec:
    """一个错误码的完整声明。"""

    code: ErrorCode
    message: str
    retryable: bool
    counts_toward_metrics: bool
    visible_in: tuple[str, ...]
    trigger_points: tuple[str, ...]


def _spec(
    code: ErrorCode,
    message: str,
    retryable: bool,
    counts_toward_metrics: bool,
    visible_in: tuple[str, ...],
    trigger_points: tuple[str, ...],
) -> ErrorSpec:
    return ErrorSpec(
        code=code,
        message=message,
        retryable=retryable,
        counts_toward_metrics=counts_toward_metrics,
        visible_in=visible_in,
        trigger_points=trigger_points,
    )


ERROR_TABLE: dict[str, ErrorSpec] = {
    spec.code.value: spec
    for spec in (
        _spec(
            ErrorCode.CREDENTIAL_MISSING,
            "缺少必需的环境变量：{names}。请补齐后重试（不会自动回落到本地替身）",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_DOCTOR, SURFACE_TERMINAL),
            trigger_points=(
                "config.require_env() 读环境变量时发现缺项 —— 指名缺哪个变量",
                "CLI 启动即体检（doctor）时缺项",
            ),
        ),
        _spec(
            ErrorCode.CONTRACT_INVALID,
            "不符合数据契约：{detail}",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_TERMINAL, SURFACE_TABLE, SURFACE_SUMMARY),
            trigger_points=(
                "模型目录字段映射不合法（catalog 装载时）",
                "评估表既有列缺失 / 金标准为空 / 行号越界（sheets 读取时）",
                "CLI 参数互斥或缺必填项（命令行解析时）",
                "指标与结论自检对不上（落盘前一致性校验）",
                "run store 打开失败或写入越界（存储层）",
            ),
        ),
        _spec(
            ErrorCode.RIS_READ_FAILED,
            "读取院内 RIS 检查清单失败：{detail}",
            retryable=True,
            counts_toward_metrics=False,
            visible_in=(SURFACE_TERMINAL, SURFACE_TABLE, SURFACE_SUMMARY),
            trigger_points=(
                "adapters/ris.py 拉取检查清单时 HTTP 出错或分页中途失败",
                "RIS 返回的记录缺 accession_no / study_uid 必填字段",
            ),
        ),
        _spec(
            ErrorCode.AMBIGUOUS_BINDING,
            "检查号 {accession_no} 命中多条影像，无法自动绑定，已挂起人工判定",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_TABLE, SURFACE_SUMMARY, SURFACE_TERMINAL),
            trigger_points=(
                "caseset.bind_cases() 一对多命中（同一检查号多条影像 / 多个时间点）",
                "RIS 里查不到该检查号（unresolved 挂起）",
                "表内检查号重复（duplicate 挂起）",
            ),
        ),
        _spec(
            ErrorCode.INFERENCE_4XX,
            "推理服务拒绝了本次请求（HTTP {status}）：{detail}",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_TABLE, SURFACE_TERMINAL, SURFACE_TOOL_CALLS, SURFACE_SUMMARY),
            trigger_points=(
                "adapters/inference.py 收到 4xx（参数 / 鉴权 / 图像引用不合法）",
                "**立刻判该例失败，不消耗退避预算**（重试必然还是被拒）",
            ),
        ),
        _spec(
            ErrorCode.INFERENCE_5XX,
            "推理服务暂时不可用（HTTP {status}）：{detail}",
            retryable=True,
            counts_toward_metrics=False,
            visible_in=(SURFACE_TABLE, SURFACE_TERMINAL, SURFACE_TOOL_CALLS, SURFACE_SUMMARY),
            trigger_points=(
                "adapters/inference.py 收到 5xx / 429（退避重试后仍失败）",
                "连接被拒 / DNS 解析失败（adapters/inference.py）",
            ),
        ),
        _spec(
            ErrorCode.INFERENCE_TIMEOUT,
            "调用推理服务超时（{timeout}）：{detail}",
            retryable=True,
            counts_toward_metrics=False,
            visible_in=(SURFACE_TABLE, SURFACE_TERMINAL, SURFACE_TOOL_CALLS, SURFACE_SUMMARY),
            trigger_points=(
                "adapters/inference.py 连接阶段超时",
                "adapters/inference.py 响应阶段超时",
            ),
        ),
        _spec(
            ErrorCode.STUDY_UID_MISMATCH,
            "推理服务回传的检查标识与请求不一致（期望 {expected}，实得 {actual}），该例判失败",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_TABLE, SURFACE_TERMINAL, SURFACE_TOOL_CALLS, SURFACE_SUMMARY),
            trigger_points=(
                "adapters/inference.py 响应 study_uid 与请求不一致",
                "adapters/inference.py 响应未回传 study_uid",
            ),
        ),
        _spec(
            ErrorCode.FIELD_MAPPING_MISSING,
            "字段缺失：{detail}",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_DOCTOR, SURFACE_TABLE, SURFACE_TERMINAL, SURFACE_SUMMARY),
            trigger_points=(
                "模型目录缺字段映射（catalog 装载时）",
                "推理响应缺 candidates 或候选字段缺失（adapters/inference.py）",
                "模型响应缺结构化字段（gateway 结构化解析时）",
            ),
        ),
        _spec(
            ErrorCode.RESEARCH_WRITE_FAILED,
            # 状态位由调用方给全（"HTTP 500" / "未收到 HTTP 响应"）——
            # 写回失败可能是「对端拒收」也可能是「压根没连上」，
            # 后者没有 HTTP 状态码，模板里再写死一个 "HTTP" 就会变成
            # 「（HTTP 未收到 HTTP 响应）」这种读不通的话。
            "写回院内科研平台失败（{status}）：{detail}",
            retryable=True,
            counts_toward_metrics=False,
            visible_in=(SURFACE_TERMINAL, SURFACE_WRITEBACK_AUDIT),
            trigger_points=(
                "adapters/research_platform.py 写回登记时 HTTP 出错或连接失败",
                "adapters/research_platform.py 回执核对缺 registration_id / run_id / status / received_at",
            ),
        ),
        _spec(
            ErrorCode.RESEARCH_PAYLOAD_TOO_LARGE,
            # 提交体超出对端上限 —— **明确报错，不截断**。
            # 截断掉的那几条逐例明细等于「这一轮评估了 N 例但只登记了 M 例」，
            # 对端拿到的数字与本轮结论对不上，采购会上说不清。
            # detail 里带三个数：实际字节、上限、以及「分批」怎么分。
            "写回提交体超出对端上限：{detail}",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_TERMINAL, SURFACE_WRITEBACK_AUDIT),
            trigger_points=(
                "adapters/research_platform.py 提交体字节数超过对端上限",
            ),
        ),
        _spec(
            ErrorCode.WRITEBACK_NOT_REVIEWED,
            # 写回前确认闸门没通过 —— **一次出站调用都不发**。
            # 「压根没发」与「发了但被拒」是两回事：后者已经在对端留下了痕迹。
            # 状态与触发方写进 detail（**只有一处说这件事**，不重复两遍）。
            "确认闸门未通过：{detail}",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_TERMINAL,),
            trigger_points=(
                "writeback 闸门校验：run 未经人工复核就请求确认提交",
                "CLI submit 子命令直接跳过复核环节",
            ),
        ),
        _spec(
            ErrorCode.MODEL_UNAVAILABLE,
            "模型不可用：{detail}",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_DOCTOR,),
            trigger_points=(
                "模型目录未授权该模型（catalog 选型时）",
                "模型目录文件缺失 / 端点路径缺失",
                "gateway 连接阶段或响应阶段超时",
                "gateway 端点不可达（降级路径入口）",
            ),
        ),
        _spec(
            ErrorCode.MODEL_REQUEST_REJECTED,
            "模型端点拒绝了本次请求（HTTP {status}）：{detail}",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_TERMINAL, SURFACE_TABLE, SURFACE_SUMMARY, SURFACE_MODEL_CALLS),
            trigger_points=(
                "gateway 收到 4xx（请求被拒）",
                "goldstd/merge.py 模型归并输出不合契约（缺 evidence_span / 改写原文）",
            ),
        ),
        _spec(
            ErrorCode.RULE_DEGRADED,
            "模型归并未成功，已降级为规则归并：{detail}",
            retryable=False,
            counts_toward_metrics=True,
            visible_in=(SURFACE_TABLE, SURFACE_TERMINAL, SURFACE_SUMMARY, SURFACE_TOOL_CALLS),
            trigger_points=(
                "goldstd/merge.py 模型不可达 / 输出不可解析 → 走规则基线归并",
                "engine 逐例判定时标记该例为降级（仍进指标分母，单列降级数）",
            ),
        ),
        _spec(
            ErrorCode.INVALID_STATE_TRANSITION,
            "状态迁移被拒：{detail}",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_TERMINAL, SURFACE_TABLE, SURFACE_SUMMARY, SURFACE_TRANSITIONS),
            trigger_points=(
                "store.run/case 状态机遇到非法边（如 evaluated 未经 reviewed 直跳 submitted）",
                "引擎内部循环试图自行推进到 reviewed / submitted",
                "writeback_receipt 写回回执状态非法",
            ),
        ),
        _spec(
            ErrorCode.TOOL_CALL_NOT_FOUND,
            "未找到该调用记录：{tool_call_id}。{detail}",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_INSPECT,),
            trigger_points=(
                "audit/inspect.py 按 tool_call_id 反查未命中",
                "audit/inspect.py 底账里缺该次调用",
            ),
        ),
        _spec(
            ErrorCode.INSPECT_ARTIFACT_INCOMPLETE,
            "反查所需产物不完整：{detail}",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_INSPECT, SURFACE_ARTIFACTS),
            trigger_points=(
                "audit/inspect.py 找不到逐例产物目录",
                "audit/inspect.py 逐例产物缺 case.json / 底账缺行",
                "audit/inspect.py 逐例产物与调用底账对不上",
            ),
        ),
        _spec(
            ErrorCode.ARTIFACT_PII_DETECTED,
            "产物里出现患者可识别明细：{detail}",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_REDACT_SCAN, SURFACE_ARTIFACTS),
            trigger_points=(
                "audit/redact.py 扫产物命中身份证号 / 住院号 / 手机号 / 中文姓名",
            ),
        ),
        _spec(
            ErrorCode.ARTIFACT_FIELD_UNREGISTERED,
            "产物里出现未登记字段：{detail}",
            retryable=False,
            counts_toward_metrics=False,
            visible_in=(SURFACE_REDACT_SCAN, SURFACE_ARTIFACTS),
            trigger_points=(
                "audit/redact.py 扫产物命中字段白名单之外的字段名",
            ),
        ),
    )
}

#: 全部错误码，任务 42 据此断言「每个错误码都有可触发路径测试」。
ERROR_CODES: tuple[str, ...] = tuple(ERROR_TABLE.keys())


class _SafeFields(dict):
    """占位符缺失时填空串，避免 ``KeyError`` 把错误文案本身搞崩。"""

    def __missing__(self, key: str) -> str:  # pragma: no cover - 兜底分支
        return ""


def spec(code: "ErrorCode | str") -> ErrorSpec:
    """取错误码声明；未知错误码直接抛 ``ValueError``（表本身写错是编码事故）。"""

    key = code.value if isinstance(code, ErrorCode) else str(code)
    try:
        return ERROR_TABLE[key]
    except KeyError as exc:  # pragma: no cover - 防御性分支
        raise ValueError(f"未登记的错误码：{key}") from exc


def format_message(code: "ErrorCode | str", **fields: Any) -> str:
    """按错误码模板填充占位符，产出最终中文文案。"""

    return spec(code).message.format_map(_SafeFields(fields))


def flatten(text: str, *, limit: int = FAILURE_TEXT_LIMIT) -> str:
    """把一段文字压成**一行**（表格一格一行；超长截断）。

    失败原因要写进评估表的 ``failure_reason`` 列 —— 里面有换行，
    表格里就是一格多行，打印出来会串行；整段厂商返回更是没法看。
    """

    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


def user_text(code: "ErrorCode | str", **fields: Any) -> str:
    """**用户可见的失败文案**（一行）。

    表格 ``failure_reason`` 列、终端输出、``summary.json`` 三处都调它 ——
    同一错误码必然是同一句话，因为只有一个函数产出它。
    """

    return flatten(format_message(code, **fields))


def visible_surfaces(code: "ErrorCode | str") -> tuple[str, ...]:
    """错误码的用户可见位置（受控词表）。"""

    return spec(code).visible_in


def trigger_points(code: "ErrorCode | str") -> tuple[str, ...]:
    """错误码的触发点（人话说明「什么情况下会走到它」）。"""

    return spec(code).trigger_points


class NoduleEvalError(Exception):
    """本产品所有可预期失败的统一异常。

    携带错误码、原始状态码与补充字段；``message`` 永远由 :func:`format_message`
    产出，保证三处文案一致。
    """

    def __init__(
        self,
        code: "ErrorCode | str",
        detail: str = "",
        http_status: int | None = None,
        **fields: Any,
    ) -> None:
        self.code = ErrorCode(code) if not isinstance(code, ErrorCode) else code
        self.detail = detail
        self.http_status = http_status
        self.fields: dict[str, Any] = dict(fields)
        super().__init__(self.message)

    @property
    def message(self) -> str:
        payload: dict[str, Any] = dict(self.fields)
        payload["detail"] = self.detail
        payload.setdefault("status", self.http_status if self.http_status is not None else "")
        return format_message(self.code, **payload)

    @property
    def user_text(self) -> str:
        """一行版用户可见文案（表格一格一行）。"""

        return flatten(self.message)

    @property
    def retryable(self) -> bool:
        return spec(self.code).retryable

    @property
    def counts_toward_metrics(self) -> bool:
        return spec(self.code).counts_toward_metrics

    @property
    def visible_in(self) -> tuple[str, ...]:
        return spec(self.code).visible_in

    @property
    def trigger_points(self) -> tuple[str, ...]:
        return spec(self.code).trigger_points

    def to_dict(self) -> dict[str, Any]:
        """落进 JSON 产物时的形态（不含任何患者可识别明细）。"""

        return {
            "error_code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "http_status": self.http_status,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"NoduleEvalError({self.code.value}: {self.message})"


def error_text(code: "ErrorCode | str", **fields: Any) -> str:
    """便捷入口：直接取错误码文案。"""

    return format_message(code, **fields)


def describe(code: "ErrorCode | str") -> Mapping[str, Any]:
    """取错误码的完整声明（落地矩阵用）。"""

    s = spec(code)
    return {
        "error_code": s.code.value,
        "message": s.message,
        "retryable": s.retryable,
        "counts_toward_metrics": s.counts_toward_metrics,
        "visible_in": list(s.visible_in),
        "trigger_points": list(s.trigger_points),
    }
