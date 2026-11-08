"""院内科研平台评估登记的出站适配器 —— **本产品真正改变业务状态的那条出站面**。

前面两条出站面（RIS 读、厂商推理服务写）都只**读**院内与厂商的状态；
只有这一条会把本产品的结论**写进**院内系统，成为「已生效事实」::

    POST {RESEARCH_PLATFORM_BASE_URL}/api/v1/model-evaluation-runs
    Authorization: Bearer $RESEARCH_PLATFORM_TOKEN
    Idempotency-Key: sha256(run_id|model_id|dataset_ref)
    Content-Type: application/json

    请求体  {"run_id", "model_id", "model_version", "dataset_ref",
             "metrics", "case_rows", "submitted_by", "submitted_at"}
    响应体  {"registration_id", "run_id", "status", "received_at"}

**幂等键**是 ``sha256(run_id|model_id|dataset_ref)``：同一轮、同一款模型、同一批数据
重复提交 → 键值相同 → 对端只登记一条并回放同一张回执。信息科在表格里
**连点两下「确认提交」不会多出一条登记** —— 这不是「小心一点」能解决的，
登记台账里两条一样的评估结论，采购评审时没人说得清该引用哪一条。

**键里为什么不含逐例内容**：一轮结论复核后可能被补判，但键只认
「哪一轮 · 哪款模型 · 哪批数据」—— 键一变就等于「另一次评估」，
登记台账里会出现同一批数据的两条记录、无法分辨。**补判后要另起 run_id 重跑**
（产物版本台账 ``versions/version-XXXX/`` 已经把两版都留着，可逐例比对）。

**失败一律落 ``RESEARCH_WRITE_FAILED`` 并保留原始 HTTP 状态码**：
4xx **不重试**（参数或鉴权错，重试必然再错一遍），5xx 指数退避重试（与另外
三条出站链路同一套量级：指数、最小 4 秒、最多 3 次）。**「请求发出去了」不等于
「登记成功了」** —— 拿不到回执就算失败，绝不把「已发出」当成「已生效」。

**源产品对照**：参考实现是**纯聊天**产品 —— 一次问答结束就结束，
既没有「把结论写进院内系统」这回事，也就没有幂等键、回执与写回闸门。
这一层是**新建**（见 ``spec.md`` → ``## 开源对照`` 的 ``writeback`` 行）。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..config import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    Config,
    join_url,
    resolve_max_payload_bytes,
)
from ..errors import ErrorCode, NoduleEvalError
from .http import HttpClient, HttpFailure
from .vendor_error import extract_vendor_error

__all__ = [
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "DEFAULT_RESEARCH_PATH",
    "EvaluationSubmission",
    "IDEMPOTENCY_HEADER",
    "OPERATOR_ENV",
    "RECEIPT_FIELDS",
    "REGISTRATION_LOOKUP_PARAM",
    "RegistrationLookup",
    "RegistrationReceipt",
    "ResearchPlatformAdapter",
    "SUBMISSION_FIELDS",
    "assert_payload_within_limit",
    "canonical_json",
    "compute_idempotency_key",
    "map_research_failure",
    "now_iso",
    "payload_bytes",
    "payload_digest",
    "resolve_operator",
]


#: 评估登记端点路径（相对 ``RESEARCH_PLATFORM_BASE_URL``）。
DEFAULT_RESEARCH_PATH = "/api/v1/model-evaluation-runs"

#: 幂等键的请求头名。
IDEMPOTENCY_HEADER = "Idempotency-Key"

#: 操作者标识的环境变量名（``submitted_by`` 的来源）。
OPERATOR_ENV = "NODULE_EVAL_OPERATOR"

#: 登记请求体的**字段规范**（顺序固定，便于逐字比对）。
SUBMISSION_FIELDS: tuple[str, ...] = (
    "run_id",
    "model_id",
    "model_version",
    "dataset_ref",
    "metrics",
    "case_rows",
    "submitted_by",
    "submitted_at",
)

#: 登记回执的**字段规范**（科研平台侧给出）。
RECEIPT_FIELDS: tuple[str, ...] = (
    "registration_id",
    "run_id",
    "status",
    "received_at",
)

#: 回执核对查询的**查询参数名**（科研平台侧按它回放登记）。
#:
#: 核对走的是**同一个资源**（``GET /api/v1/model-evaluation-runs``），
#: 只是把幂等键作为查询参数传过去 —— 幂等键是科研平台侧自己算出来的那把钥匙，
#: 用它查回来的就是「你那条登记到底在不在」。
REGISTRATION_LOOKUP_PARAM = "idempotency_key"

#: 提交体的**大小上限**（字节）。默认 1 MiB。
#:
#: 院内科研平台的登记接口通常有请求体上限（网关 / 平台侧都会拒）。
#: 200–500 例的逐例明细在 100–400 KB 量级，默认值留了一倍多的余量；
#: 真到超限时**明确报错并提示改用分批**，绝不静默截断 ``case_rows`` ——
#: 截断掉的那几条等于「这一轮评估了 N 例、只登记了 M 例」，
#: 对端拿到的数字与本轮结论对不上，采购会上说不清。
#:
#: 上限可由环境变量 ``RESEARCH_PLATFORM_MAX_PAYLOAD_BYTES`` 覆盖
#: （科研平台侧改了上限，本机跟着改，不用改代码）。
#: 常量与读取口径的真源在 :mod:`nodule_eval.config`（本模块只做转出）。
#: 提交体里必须非空的**字符串字段**。
_REQUIRED_TEXT_FIELDS: tuple[str, ...] = (
    "run_id",
    "model_id",
    "model_version",
    "dataset_ref",
    "submitted_by",
    "submitted_at",
)


def now_iso() -> str:
    """统一的时刻格式（本地时区，秒级）—— 与调用底账同一口径。"""

    return datetime.now().isoformat(timespec="seconds")


def compute_idempotency_key(run_id: str, model_id: str, dataset_ref: str) -> str:
    """幂等键：``sha256(run_id|model_id|dataset_ref)`` 的十六进制串。

    键**只认这三段**：换一轮、换一款模型、换一批数据 → 键变；
    其余一切（逐例结论、复核意见、提交时刻）变化 → 键不变。

    **三段里不许出现竖线**：分隔符一旦出现在段内，
    ``("a|b", "c", "d")`` 与 ``("a", "b|c", "d")`` 会拼成同一个字符串、
    撞成同一把钥匙 —— 两轮不同的评估在科研平台侧登记成一条。
    本产品的运行环境是院内 Windows PC，文件名本来就不许含竖线，
    所以这里**指名报错**而不是悄悄换个分隔符（换分隔符会与科研平台
    侧算出来的键对不上，幂等直接失效）。
    """

    for name, value in (
        ("run_id", run_id),
        ("model_id", model_id),
        ("dataset_ref", dataset_ref),
    ):
        if "|" in (value or ""):
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=(
                    f"幂等键的字段 {name} 不得包含分隔符 |"
                    "（否则两轮评估会撞成同一条登记）"
                ),
            )

    raw = f"{run_id}|{model_id}|{dataset_ref}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def resolve_operator(env: Mapping[str, str] | None = None) -> str:
    """取操作者标识（``submitted_by`` 的来源）。

    顺序：环境变量 ``NODULE_EVAL_OPERATOR`` → 操作系统登录名。
    两者都取不到时抛 ``CONTRACT_INVALID`` 并**指名变量** ——
    登记台账上不能写「未知提交人」：这条记录是要给采购评审引用的，
    「谁提交的」必须能回答。
    """

    source: Mapping[str, str] = os.environ if env is None else env
    raw = str(source.get(OPERATOR_ENV, "")).strip()
    if raw:
        return raw

    try:  # pragma: no cover - 依赖运行环境，交互式场景才走到
        import getpass

        user = getpass.getuser().strip()
    except Exception:  # pragma: no cover - 拿不到登录名时走下面的报错
        user = ""
    if user:
        return user

    raise NoduleEvalError(
        ErrorCode.CONTRACT_INVALID,
        detail=(
            f"取不到操作者标识：请设置环境变量 {OPERATOR_ENV}"
            "（登记台账上必须写得出是谁提交的这轮评估结论）"
        ),
    )


def canonical_json(payload: Any) -> str:
    """把提交体序列化成**规范形式**（键序无关的稳定字符串）。

    排序键 + 紧凑分隔符 + 不转义中文 —— 同一份内容永远得到同一个字符串，
    于是「内容摘要」可以复算：把留痕里的哈希拿回来对一遍，
    就知道「发出去的那一版」与「本地记的那一版」是不是同一版。

    **这不是线上字节**（HTTP 报文怎么排版是会话层的事），
    它是**内容指纹**：内容一样指纹一样，内容变了指纹就变。
    """

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_bytes(payload: Any) -> int:
    """提交体的字节数（按 :func:`canonical_json` 的规范形式算）。"""

    return len(canonical_json(payload).encode("utf-8"))


def payload_digest(payload: Any) -> str:
    """提交体的**内容摘要**（sha256 十六进制）。

    写回审计里留它，是为了让「这轮到底把什么写进院内登记了」有一把钥匙：
    事后从科研平台侧把登记内容导出来，算一次摘要就能与留痕对上。
    """

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def assert_payload_within_limit(
    payload: Any,
    *,
    max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    run_id: str = "",
    case_count: int = 0,
) -> int:
    """提交体超限就**明确报错**（不截断、不发出去）。

    报 ``RESEARCH_PAYLOAD_TOO_LARGE``，文案里带三个数：实际字节、上限、
    以及**怎么分批**（每批大致多少例）。调用点在出站客户端**之前** ——
    超限这一轮**一次出站调用都不发**（对端压根不知道有这么一轮）。

    ``case_count``
        逐例行数，用来把「分批」的批大小算出来：按当前每例的平均字节数
        估出「一批最多放几例」，让人照着分，不用自己算。
    """

    size = payload_bytes(payload)
    if size <= max_bytes:
        return size

    per_case = 0
    rows = payload.get("case_rows") if isinstance(payload, Mapping) else None
    if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)) and rows:
        per_case = max(1, size // len(rows))
    batch_hint = (
        f"每批约 {max(1, max_bytes // per_case)} 例"
        if per_case
        else "请按对端上限分批"
    )
    where = f"（{run_id}）" if run_id else ""
    raise NoduleEvalError(
        ErrorCode.RESEARCH_PAYLOAD_TOO_LARGE,
        detail=(
            f"提交体 {size} 字节 > 对端上限 {max_bytes} 字节{where}；"
            f"共 {case_count or (len(rows) if isinstance(rows, Sequence) else 0)} 例，"
            f"{batch_hint}—— 请另起 run_id 分批提交，"
            "本轮不会截断逐例明细，也不会发出请求"
        ),
        payload_bytes=size,
        max_payload_bytes=max_bytes,
        case_count=case_count,
    )


@dataclass(frozen=True)
class EvaluationSubmission:
    """一次评估登记的**提交体**（字段规范见 :data:`SUBMISSION_FIELDS`）。

    ``metrics``
        汇总指标（TP/FP/FN/TN + 四项指标 + 失败·降级·挂起·不可比各栏）——
        对端拿它做登记；**分母为 0 的指标是 ``null``**，不是 0。
    ``case_rows``
        逐例判定行（检查号 / 判定 / 模型结论 / 失败原因 / 调用记录 ID）。
        逐例内容**不含金标准原文** —— 原文可能带姓名 / 身份证 / 住院号，
        写出去就等于把患者明细带出评估表。
    ``submitted_by``
        操作者标识 —— 必填，不许写「未知」。
    ``submitted_at``
        提交时刻（ISO 串）。缺省由适配器补当前时刻。
    ``dataset_ref``
        **数据集的稳定引用**（表名 / 数据集编号），**不要指向本机路径** ——
        同一批数据换台机器重跑就该是同一把幂等键；指向本机路径的话，
        换台机器就换键，幂等直接失效。

    构造时即校验：任何必填字段为空都抛 ``CONTRACT_INVALID`` 并**指名字段**，
    不构造出一个「半个提交体」再发出去 —— 对端拒收只是浪费一次登记。
    """

    run_id: str
    model_id: str
    model_version: str
    dataset_ref: str
    metrics: Mapping[str, Any] = field(default_factory=dict)
    case_rows: Sequence[Mapping[str, Any]] = ()
    submitted_by: str = ""
    submitted_at: str = ""

    def __post_init__(self) -> None:
        for name in _REQUIRED_TEXT_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise NoduleEvalError(
                    ErrorCode.CONTRACT_INVALID,
                    detail=f"登记提交体的字段 {name} 必填且不得为空",
                )
        if not isinstance(self.metrics, Mapping):
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail="登记提交体的字段 metrics 须为 JSON 对象",
            )
        if isinstance(self.case_rows, (str, bytes)) or not isinstance(self.case_rows, Sequence):
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail="登记提交体的字段 case_rows 须为数组",
            )
        for index, row in enumerate(self.case_rows):
            if not isinstance(row, Mapping):
                raise NoduleEvalError(
                    ErrorCode.CONTRACT_INVALID,
                    detail=f"登记提交体的第 {index + 1} 条 case_rows 不是 JSON 对象",
                )

    def idempotency_key(self) -> str:
        """本次登记的幂等键（见 :func:`compute_idempotency_key`）。"""

        return compute_idempotency_key(self.run_id, self.model_id, self.dataset_ref)

    def to_dict(self) -> dict[str, Any]:
        """按 :data:`SUBMISSION_FIELDS` 的顺序组装请求体。"""

        return {
            "run_id": self.run_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "dataset_ref": self.dataset_ref,
            "metrics": dict(self.metrics),
            "case_rows": [dict(row) for row in self.case_rows],
            "submitted_by": self.submitted_by,
            "submitted_at": self.submitted_at or now_iso(),
        }


@dataclass(frozen=True)
class RegistrationReceipt:
    """科研平台给出的登记回执。

    ``registration_id``
        登记号 —— 这是「已生效事实」的凭据，出错时要拿它去问科研平台。
    ``idempotency_key``
        本次提交用的幂等键 —— 回执与键一起留痕，事后能证明
        「重复提交没有产生第二条登记」。
    ``attempts``
        实际发出的请求次数（含退避重试）—— 「这轮写回调了几次」是
        飞检常问的一句。
    """

    registration_id: str
    run_id: str
    status: str
    received_at: str
    idempotency_key: str = ""
    http_status: int | None = None
    attempts: int = 1
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "registration_id": self.registration_id,
            "run_id": self.run_id,
            "status": self.status,
            "received_at": self.received_at,
            "idempotency_key": self.idempotency_key,
            "http_status": self.http_status,
            "attempts": self.attempts,
        }


def map_research_failure(failure: HttpFailure) -> NoduleEvalError:
    """把出站失败翻成 ``RESEARCH_WRITE_FAILED``（保留原始 HTTP 状态码）。

    4xx 的失败原因要带**科研平台自己的错误码与出错字段名** ——
    「写回失败（HTTP 400）」没人能据此去改，「科研平台错误码 DUPLICATE_DATASET，
    字段 dataset_ref」才行（复取用 :func:`adapters.vendor_error.extract_vendor_error`）。

    **传输层失败（连不上 / 超时）没有 HTTP 状态码**，也没有「错误体」——
    此时用会话层给的阶段说明（「无法连接（连接被拒或域名解析失败）」），
    状态位写「未收到 HTTP 响应」，**不写一个假的 0 或 500**。

    **``attempts`` 一并带出去**：这一轮写回**实际打出去几次**（含退避重试）
    要落进回执台账与写回底账 —— 「失败时试了几次」是飞检常问的一句。
    """

    if failure.status is None:
        detail = failure.detail()
        status_text = "未收到 HTTP 响应"
    else:
        vendor = extract_vendor_error(failure.body)
        detail = vendor.describe() or failure.detail()
        status_text = f"HTTP {failure.status}"

    return NoduleEvalError(
        ErrorCode.RESEARCH_WRITE_FAILED,
        detail=detail,
        http_status=failure.status,
        status=status_text,
        attempts=failure.attempts,
    )


@dataclass(frozen=True)
class RegistrationLookup:
    """按幂等键查回来的**登记核对结果**。

    ``found``
        对端到底有没有这条登记。**404 不算错误** —— 「没登记」是一个答案，
        不是一次失败；把它当失败会让「核对」这件事没法做。
    ``registration_id``
        对端的登记号 —— 与本地回执里的那个一致，才算「两边对得上」。
    """

    found: bool
    idempotency_key: str = ""
    registration_id: str = ""
    status: str = ""
    received_at: str = ""
    http_status: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "idempotency_key": self.idempotency_key,
            "registration_id": self.registration_id,
            "status": self.status,
            "received_at": self.received_at,
            "http_status": self.http_status,
        }


class ResearchPlatformAdapter:
    """院内科研平台评估登记的出站客户端。"""

    def __init__(
        self,
        config: Config,
        *,
        client: HttpClient | None = None,
        base_url: str | None = None,
        endpoint_path: str = DEFAULT_RESEARCH_PATH,
    ) -> None:
        self._config = config
        self._client = client if client is not None else HttpClient()
        base = base_url if base_url is not None else config.research_platform_base_url
        self.url = join_url(base, endpoint_path)

    # ---- 幂等键 ----
    def idempotency_key(self, submission: EvaluationSubmission) -> str:
        """本次提交用的幂等键（与提交体里的三段一一对应）。"""

        return submission.idempotency_key()

    # ---- 提交 ----
    def submit(self, submission: EvaluationSubmission) -> RegistrationReceipt:
        """提交一次评估登记，返回科研平台给的回执。

        - 请求头：``Authorization: Bearer $RESEARCH_PLATFORM_TOKEN`` +
          ``Idempotency-Key: sha256(run_id|model_id|dataset_ref)``；
        - 请求体：:data:`SUBMISSION_FIELDS` 八项，顺序固定；
        - **4xx 不重试、5xx 指数退避**（会话层的统一语义）；
        - 回执缺 ``registration_id`` → 判失败（不能报「已登记」却没有登记号）。
        """

        body = submission.to_dict()
        headers = dict(self._config.research_platform_headers)
        headers["Content-Type"] = "application/json"
        headers[IDEMPOTENCY_HEADER] = submission.idempotency_key()

        stats: dict[str, int] = {}
        response = self._client.request(
            "POST",
            self.url,
            error_mapper=map_research_failure,
            headers=headers,
            json_body=body,
            stats=stats,
        )

        try:
            payload = response.json()
        except ValueError as exc:
            raise NoduleEvalError(
                ErrorCode.RESEARCH_WRITE_FAILED,
                detail=f"科研平台响应不是合法 JSON：{exc}",
                http_status=response.status_code,
                status=f"HTTP {response.status_code}",
            ) from exc

        return self.parse_receipt(payload, submission, response.status_code, stats)

    # ---- 回执核对 ----

    def fetch_registration(self, idempotency_key: str) -> RegistrationLookup:
        """按幂等键查对端到底有没有这条登记。

        ``GET {RESEARCH_PLATFORM_BASE_URL}/api/v1/model-evaluation-runs
        ?idempotency_key=<key>``，头 ``Authorization: Bearer $RESEARCH_PLATFORM_TOKEN``。

        **404 = 「没登记」**，不是失败 —— 它是一个答案。所以这里把 404
        收成 ``found=False`` 返回，而不是抛错：核对这件事必须能问出
        「不在」这个结论，否则「上一次到底写上去没有」永远查不清。

        其它 4xx / 5xx 照旧落 ``RESEARCH_WRITE_FAILED``（连不上、鉴权错、
        服务端报错 —— 那才是真的没核对成）。
        """

        key = (idempotency_key or "").strip()
        if not key:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID, detail="回执核对的幂等键不得为空"
            )

        headers = dict(self._config.research_platform_headers)
        headers["Accept"] = "application/json"
        try:
            response = self._client.request(
                "GET",
                self.url,
                error_mapper=map_research_failure,
                headers=headers,
                params={REGISTRATION_LOOKUP_PARAM: key},
            )
        except NoduleEvalError as exc:
            if exc.http_status == 404:
                return RegistrationLookup(
                    found=False,
                    idempotency_key=key,
                    http_status=404,
                )
            raise

        try:
            payload = response.json()
        except ValueError as exc:
            raise NoduleEvalError(
                ErrorCode.RESEARCH_WRITE_FAILED,
                detail=f"科研平台回执查询响应不是合法 JSON：{exc}",
                http_status=response.status_code,
                status=f"HTTP {response.status_code}",
            ) from exc

        if not isinstance(payload, Mapping):
            raise NoduleEvalError(
                ErrorCode.RESEARCH_WRITE_FAILED,
                detail=(
                    "科研平台回执查询响应顶层不是对象"
                    f"（实得 {type(payload).__name__}）"
                ),
                http_status=response.status_code,
                status=f"HTTP {response.status_code}",
            )

        registration_id = str(payload.get("registration_id") or "").strip()
        if not registration_id:
            # 查到了东西却没有登记号 —— 对端返回的不是一条登记。
            raise NoduleEvalError(
                ErrorCode.RESEARCH_WRITE_FAILED,
                detail="科研平台回执查询结果缺少字段 registration_id",
                http_status=response.status_code,
                status=f"HTTP {response.status_code}",
            )

        return RegistrationLookup(
            found=True,
            idempotency_key=key,
            registration_id=registration_id,
            status=str(payload.get("status") or "").strip(),
            received_at=str(payload.get("received_at") or "").strip(),
            http_status=response.status_code,
            raw=dict(payload),
        )

    # ---- 回执解析（出站与契约回放共用同一段代码）----
    def parse_receipt(
        self,
        payload: Any,
        submission: EvaluationSubmission,
        http_status: int,
        stats: Mapping[str, int] | None = None,
    ) -> RegistrationReceipt:
        """解析登记回执。

        ``spec.md`` 规定的回执字段是 ``registration_id`` / ``run_id`` /
        ``status`` / ``received_at`` 四项，**四项都必须在场**：

        - 缺 ``registration_id`` → 拿不到登记号，不能算已登记；
        - 缺 ``run_id`` → 对端没说它登记的是哪一轮，**不当「就是这一轮」**
          （登记台账里挂错轮次，比没登记更难查）；
        - ``run_id`` 与提交的不一致 → 对端登记的压根不是这一轮；
        - 缺 ``status`` → 本地回执台账会记下一个空状态，「这一轮到底登记成
          什么」永远答不上来；
        - 缺 ``received_at`` → 回执的接收时刻缺失，事后核对没有时间锚点。

        任一项缺失一律判 ``RESEARCH_WRITE_FAILED`` 并**指名缺哪个字段** ——
        不填默认值、不把「没登记」当成「登记了」。

        这段代码**出站提交与契约回放共用同一处**（``spec.md`` 的回执字段规范
        写在哪、解析器就守在哪）。
        """

        attempts = int((stats or {}).get("attempts", 1))

        if not isinstance(payload, Mapping):
            raise NoduleEvalError(
                ErrorCode.RESEARCH_WRITE_FAILED,
                detail=(
                    "科研平台响应顶层不是对象"
                    f"（实得 {type(payload).__name__}）"
                ),
                http_status=http_status,
                status=f"HTTP {http_status}",
            )

        registration_id = str(payload.get("registration_id") or "").strip()
        if not registration_id:
            raise NoduleEvalError(
                ErrorCode.RESEARCH_WRITE_FAILED,
                detail="科研平台回执缺少字段 registration_id（拿不到登记号，不能算已登记）",
                http_status=http_status,
                status=f"HTTP {http_status}",
            )

        if "run_id" not in payload:
            raise NoduleEvalError(
                ErrorCode.RESEARCH_WRITE_FAILED,
                detail=(
                    "科研平台回执缺少字段 run_id"
                    "（对端没说它登记的是哪一轮，不能默认当作本次提交的轮次）"
                ),
                http_status=http_status,
                status=f"HTTP {http_status}",
            )

        echoed_run_id = str(payload.get("run_id") or "").strip()
        if echoed_run_id != submission.run_id:
            raise NoduleEvalError(
                ErrorCode.RESEARCH_WRITE_FAILED,
                detail=(
                    "科研平台回执的 run_id 与提交的不一致"
                    f"（提交 {submission.run_id}，回执 {echoed_run_id}）"
                ),
                http_status=http_status,
                status=f"HTTP {http_status}",
            )

        if "status" not in payload:
            raise NoduleEvalError(
                ErrorCode.RESEARCH_WRITE_FAILED,
                detail=(
                    "科研平台回执缺少字段 status"
                    "（登记成什么状态必须写得出，否则本地回执台账记不下这轮的状态）"
                ),
                http_status=http_status,
                status=f"HTTP {http_status}",
            )

        if "received_at" not in payload:
            raise NoduleEvalError(
                ErrorCode.RESEARCH_WRITE_FAILED,
                detail=(
                    "科研平台回执缺少字段 received_at"
                    "（缺接收时刻，事后核对没有时间锚点）"
                ),
                http_status=http_status,
                status=f"HTTP {http_status}",
            )

        return RegistrationReceipt(
            registration_id=registration_id,
            run_id=echoed_run_id,
            status=str(payload.get("status") or "").strip(),
            received_at=str(payload.get("received_at") or "").strip(),
            idempotency_key=submission.idempotency_key(),
            http_status=http_status,
            attempts=attempts,
            raw=dict(payload),
        )
