"""推理端点适配器：厂商肺结节 AI 的逐例检出与测量。

**出站面（本产品真正改变业务状态的那条链路的起点）**::

    POST {INFERENCE_BASE_URL}{endpoint_path}
    Authorization: Bearer $INFERENCE_API_TOKEN
    Content-Type: application/json

    请求体  {"study_uid": ..., "image_ref": ..., "model_id": ..., "model_version": ...}
    响应体  {"study_uid": ..., "model_id": ..., "model_version": ...,
             "candidates": [{"position": ..., "size_mm": ..., "density": ..., "confidence": ...}]}

**为什么字段名不写死**：不同厂商对结节尺寸、候选列表、置信度各有各的叫法，
有的还把整段结果包在一层对象里。
本适配器按**模型目录的响应字段映射**取字段（``models/<model_id>.yaml``，
见 :mod:`nodule_eval.catalog`），**业务代码里不出现厂商字段名**；
映射缺字段即报 ``FIELD_MAPPING_MISSING`` 并写明**缺哪个字段、来自哪个模型**。

**失败分类是本模块的第一职责**：一次推理调用失败，科室要问的是三件事 ——
「是这家模型没检出，还是压根没调成？」。
把「连不上 / 读超时 / 参数被拒 / 服务端报错 / 回传标识对不上 / 响应缺字段」
混成一句话，会让每一次失败都变成一次人工排查。因此本模块把六类失败
**显式列成一张表**（:data:`FAILURE_CLASSES`），每类落到哪个错误码、能不能重试、
失败原因怎么写，全部写在表里 —— 任务 42 的落地矩阵直接引用这张表。

**一条不可让步的红线**：任何一类失败都判 ``failed``，
**绝不落到「模型未检出」**。把一次没调成的调用算成阴性，
等于把一次漏检悄悄塞进特异度里。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# 缺省字段命名与缺省端点路径统一放在 :mod:`nodule_eval.catalog`：
# 厂商的字段名只出现在**模型目录的映射表**里，业务代码不认厂商字段名。
from ..catalog import DEFAULT_ENDPOINT_PATH, DEFAULT_RESPONSE_MAPPING, resolve_path
from ..config import Config, ModelSpec, join_url
from ..errors import ErrorCode, NoduleEvalError
from .http import (
    KIND_CONNECT_TIMEOUT,
    KIND_CONNECTION_ERROR,
    KIND_READ_TIMEOUT,
    KIND_REQUEST_ERROR,
    HttpClient,
    HttpFailure,
)

#: 厂商错误体的摘取实现已收进 :mod:`nodule_eval.adapters.vendor_error`
#: （模型网关与推理服务共用同一份 —— 见该模块说明）。
#: 这里**原样再导出**，保持既有的 ``from ...inference import extract_vendor_error`` 用法不破。
from .vendor_error import BODY_SNIPPET_LIMIT, VendorError, extract_vendor_error

__all__ = [
    "Candidate",
    "DetectionResult",
    "DEFAULT_ENDPOINT_PATH",
    "FAILURE_CLASSES",
    "FailureClass",
    "InferenceAdapter",
    "VendorError",
    "classify_http_failure",
    "extract_vendor_error",
    "map_inference_failure",
]


# --------------------------------------------------------------------------
# 失败分类表：六类失败 → 错误码 → 可重试 → 中文标签
# --------------------------------------------------------------------------

#: 连接阶段超时（TCP 握手没在期限内完成）。
FAILURE_CONNECT_TIMEOUT = "connect_timeout"
#: 响应阶段超时（连上了，但厂商迟迟不返回）。
FAILURE_READ_TIMEOUT = "read_timeout"
#: 连接被拒 / 域名解析失败（厂商服务没起来）。
FAILURE_CONNECTION_ERROR = "connection_error"
#: 请求层异常（TLS 握手、代理拦截等）。
FAILURE_REQUEST_ERROR = "request_error"
#: 4xx：请求本身不对（参数错、鉴权错、图像引用错）。
FAILURE_HTTP_CLIENT_ERROR = "http_client_error"
#: 5xx / 429：厂商服务端出错或限流。
FAILURE_HTTP_SERVER_ERROR = "http_server_error"
#: 回传标识与请求不一致（含「压根没回传」）。
FAILURE_STUDY_UID_MISMATCH = "study_uid_mismatch"
#: 响应缺字段或结构不符。
FAILURE_RESPONSE_FIELD_MISSING = "response_field_missing"


@dataclass(frozen=True)
class FailureClass:
    """一个失败类别的声明。

    ``error_code``
        该类失败落到哪个产品错误码 —— 与 :mod:`nodule_eval.errors` 的错误码表一致。
    ``retryable``
        该类失败值不值得重试。4xx 参数类一律不可重试。
    ``label``
        中文短标签，进失败原因文案。
    """

    name: str
    error_code: ErrorCode
    retryable: bool
    label: str


#: **失败分类的唯一真源**。六类失败各占一行，任务 42 的错误码落地矩阵引用本表。
#:
#: 口径说明：
#:
#: - 连接超时与读取超时都归 ``INFERENCE_TIMEOUT``，但失败原因里写清是**哪个阶段**
#:   超时（连不上 ≠ 模型慢）；
#: - 连接被拒 / DNS 失败归 ``INFERENCE_5XX``（服务没起来 = 服务端不可用），
#:   **不是**超时 —— 报成超时会让人去查网络，而问题在厂商进程；
#: - 4xx 归 ``INFERENCE_4XX`` 且**不可重试**：参数错误重试必然再错一遍。
FAILURE_CLASSES: dict[str, FailureClass] = {
    FAILURE_CONNECT_TIMEOUT: FailureClass(
        FAILURE_CONNECT_TIMEOUT, ErrorCode.INFERENCE_TIMEOUT, True, "连接超时"
    ),
    FAILURE_READ_TIMEOUT: FailureClass(
        FAILURE_READ_TIMEOUT, ErrorCode.INFERENCE_TIMEOUT, True, "读取响应超时"
    ),
    FAILURE_CONNECTION_ERROR: FailureClass(
        FAILURE_CONNECTION_ERROR, ErrorCode.INFERENCE_5XX, True, "连接被拒或域名解析失败"
    ),
    FAILURE_REQUEST_ERROR: FailureClass(
        FAILURE_REQUEST_ERROR, ErrorCode.INFERENCE_5XX, False, "请求层异常"
    ),
    FAILURE_HTTP_CLIENT_ERROR: FailureClass(
        FAILURE_HTTP_CLIENT_ERROR, ErrorCode.INFERENCE_4XX, False, "参数错误被拒"
    ),
    FAILURE_HTTP_SERVER_ERROR: FailureClass(
        FAILURE_HTTP_SERVER_ERROR, ErrorCode.INFERENCE_5XX, True, "服务端错误"
    ),
    FAILURE_STUDY_UID_MISMATCH: FailureClass(
        FAILURE_STUDY_UID_MISMATCH, ErrorCode.STUDY_UID_MISMATCH, False, "回传标识与请求不一致"
    ),
    FAILURE_RESPONSE_FIELD_MISSING: FailureClass(
        FAILURE_RESPONSE_FIELD_MISSING,
        ErrorCode.FIELD_MAPPING_MISSING,
        False,
        "响应缺字段或结构不符",
    ),
}


def classify_http_failure(failure: HttpFailure) -> str:
    """把一次出站失败归到 :data:`FAILURE_CLASSES` 的哪一类。

    传输层的 ``kind`` 是中立的（不掺业务语义），这里做**唯一一处**的业务归类：
    RIS 读失败与推理写失败共用同一个 :class:`HttpFailure`，
    差别只在**调用方给的映射函数**（:func:`map_inference_failure`）。
    """

    if failure.kind == KIND_CONNECT_TIMEOUT:
        return FAILURE_CONNECT_TIMEOUT
    if failure.kind == KIND_READ_TIMEOUT:
        return FAILURE_READ_TIMEOUT
    if failure.kind == KIND_CONNECTION_ERROR:
        return FAILURE_CONNECTION_ERROR
    if failure.kind == KIND_REQUEST_ERROR:
        return FAILURE_REQUEST_ERROR
    if failure.kind == "http_status" and failure.status is not None:
        # 429 是**限流**：服务端让我们慢一点，不是「请求写错了」——
        # 它归到可重试的服务端错误那一类（与 http.py 的 RETRYABLE_STATUSES 对齐）。
        if 400 <= failure.status < 500 and failure.status != 429:
            return FAILURE_HTTP_CLIENT_ERROR
    return FAILURE_HTTP_SERVER_ERROR


# --------------------------------------------------------------------------
# 厂商错误体：把「错误码 + 字段名」从 4xx 响应体里取出来
# --------------------------------------------------------------------------
#
# 实现已收进 :mod:`nodule_eval.adapters.vendor_error`（模型网关共用同一份），
# 上面已原样再导出 ``VendorError`` / ``extract_vendor_error`` / ``BODY_SNIPPET_LIMIT``。


def map_inference_failure(failure: HttpFailure) -> NoduleEvalError:
    """把出站失败翻成推理端点的产品错误码。

    走 :data:`FAILURE_CLASSES` 这张表，六类失败各落各的码：

    ==========================  =====================  ================
    失败类                      错误码                  可重试
    ==========================  =====================  ================
    连接超时 / 读取超时          ``INFERENCE_TIMEOUT``   是
    连接被拒 / DNS 失败         ``INFERENCE_5XX``       是
    4xx（参数错）               ``INFERENCE_4XX``       **否**
    5xx / 429（重试耗尽）       ``INFERENCE_5XX``       是
    回传标识与请求不一致        ``STUDY_UID_MISMATCH``  否
    响应缺字段 / 结构不符       ``FIELD_MAPPING_MISSING`` 否
    ==========================  =====================  ================

    4xx 的失败原因里**带上厂商自己的错误码与出错字段名** ——
    「厂商返回 HTTP 400」没人能据此去问厂商；
    「厂商错误码 INVALID_IMAGE_REF，字段 image_ref」才行。
    """

    class_name = classify_http_failure(failure)
    spec = FAILURE_CLASSES[class_name]

    if class_name == FAILURE_HTTP_CLIENT_ERROR:
        vendor = extract_vendor_error(failure.body)
        detail = vendor.describe() or f"服务端返回 HTTP {failure.status}"
    elif class_name == FAILURE_HTTP_SERVER_ERROR:
        vendor = extract_vendor_error(failure.body)
        described = vendor.describe()
        # 状态码已经写在错误码模板里（HTTP {status}），这里不再重复一遍。
        detail = described or failure.detail()
    else:
        detail = failure.detail()

    timeout = ""
    if class_name == FAILURE_CONNECT_TIMEOUT:
        timeout = f"连接阶段 {failure.connect_timeout:g} 秒未建立连接"
    elif class_name == FAILURE_READ_TIMEOUT:
        timeout = f"响应阶段 {failure.read_timeout:g} 秒未返回"

    return NoduleEvalError(
        spec.error_code,
        detail=detail,
        http_status=failure.status,
        failure_class=class_name,
        timeout=timeout,
    )


# --------------------------------------------------------------------------
# 响应归一
# --------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _text_or_none(value: Any) -> str | None:
    """把厂商给的文本字段归一成非空字符串；空/None 一律返回 ``None``。"""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class Candidate:
    """一个候选病灶（已按映射表归一为内部字段名）。"""

    position: str
    size_mm: float | None
    density: str
    confidence: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "size_mm": self.size_mm,
            "density": self.density,
            "confidence": self.confidence,
        }

    def summary(self) -> str:
        """给评估表 ``model_findings`` 列用的一行摘要。"""

        size = "-" if self.size_mm is None else f"{self.size_mm:.1f}mm"
        conf = "-" if self.confidence is None else f"{self.confidence:.2f}"
        return f"{self.position} | {size} | {self.density} | {conf}"


@dataclass(frozen=True)
class DetectionResult:
    """一次检出调用的结果。

    ``echoed_model_id`` / ``echoed_model_version``
        厂商**回传**的模型标识与版本。原样带出、不做替换 ——
        「我们请求 v1.0.0、厂商回了什么」要能在底账里对上，
        换版评估时这就是最要紧的一条核对项。
    """

    study_uid: str
    model_id: str
    model_version: str
    candidates: tuple[Candidate, ...]
    raw: Mapping[str, Any]
    echoed_model_id: str = ""
    echoed_model_version: str = ""

    def summary(self) -> str:
        if not self.candidates:
            return "未检出候选病灶"
        return "；".join(item.summary() for item in self.candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "study_uid": self.study_uid,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "echoed_model_id": self.echoed_model_id,
            "echoed_model_version": self.echoed_model_version,
            "candidates": [item.to_dict() for item in self.candidates],
        }


class InferenceAdapter:
    """厂商肺结节 AI 推理服务的出站客户端。"""

    def __init__(
        self,
        config: Config,
        spec: ModelSpec | None = None,
        *,
        client: HttpClient | None = None,
        base_url: str | None = None,
    ) -> None:
        self._config = config
        self._spec = spec
        self._client = client if client is not None else HttpClient()
        endpoint_path = spec.endpoint_path if spec is not None else DEFAULT_ENDPOINT_PATH
        base = base_url if base_url is not None else config.inference_base_url
        self.url = join_url(base, endpoint_path)
        self.model_id = spec.model_id if spec is not None else ""
        self.model_version = spec.model_version if spec is not None else ""

    # ---- 字段名 ----
    def _field_name(self, key: str) -> str:
        """取响应字段名（或点号路径）：**一律以模型目录的映射表为准**。

        没有目录声明时才用缺省命名 —— 缺省表与目录表都在
        :mod:`nodule_eval.catalog` 里，业务代码里不出现厂商字段名。
        """

        if self._spec is not None:
            return self._spec.mapped(key)
        return DEFAULT_RESPONSE_MAPPING[key]

    # ---- 调用 ----
    def detect(
        self,
        *,
        study_uid: str,
        image_ref: str | None,
        model_id: str,
        model_version: str,
    ) -> DetectionResult:
        """对一例检查发起检出调用并解析结果。

        请求体字段固定为 ``{study_uid, image_ref, model_id, model_version}``；
        响应缺字段即报 ``FIELD_MAPPING_MISSING`` 并写明缺哪个字段 ——
        **不猜、不填 0、不用空候选冒充阴性**。
        """

        body = {
            "study_uid": study_uid,
            "image_ref": image_ref,
            "model_id": model_id,
            "model_version": model_version,
        }
        headers = dict(self._config.inference_headers)
        headers["Content-Type"] = "application/json"

        response = self._client.request(
            "POST",
            self.url,
            error_mapper=map_inference_failure,
            headers=headers,
            json_body=body,
        )

        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail=f"推理服务响应不是合法 JSON：{exc}",
                failure_class=FAILURE_RESPONSE_FIELD_MISSING,
            ) from exc

        return self.parse_response(payload, study_uid, model_id)

    # ---- 解析（出站与契约回放共用同一段代码）----
    def parse_response(
        self,
        payload: Any,
        requested_study_uid: str,
        requested_model_id: str = "",
    ) -> DetectionResult:
        """按**模型目录的字段映射**把厂商响应归一成产品内部的一套字段。

        归一之后业务代码只认 ``position`` / ``size_mm`` / ``density`` / ``confidence``，
        厂商叫什么都无所谓 —— 差异全在 ``models/<model_id>.yaml`` 的映射表里。

        缺字段即报 ``FIELD_MAPPING_MISSING`` 并写明**缺哪个字段、来自哪个模型** ——
        **不猜、不填 0、不用空候选冒充阴性**。

        这段代码**出站调用与契约回放共用同一处**（``spec.md`` 的响应字段规范
        写在哪、解析器就守在哪）：契约回放把录下来的响应样本喂进来，
        走的是与真实调用**一模一样**的判定逻辑，不会出现「测的是一条路、
        跑的是另一条路」。
        """

        model_label = self.model_id or requested_model_id or "（未指定模型）"

        if not isinstance(payload, Mapping):
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail=f"推理服务响应（模型 {model_label}）顶层不是对象",
                failure_class=FAILURE_RESPONSE_FIELD_MISSING,
            )

        # 回显校验：响应里的 study_uid 必须与请求一致。
        # 不一致（含「压根没回传」）说明这份响应根本不是本次检查的结果 ——
        # 拿它当阴性会把一次漏检掩盖成一次正确排除，敏感度直接被抬高。
        echoed = payload.get("study_uid")
        echoed_text = str(echoed).strip() if echoed is not None else ""
        if not echoed_text or echoed_text != requested_study_uid:
            raise NoduleEvalError(
                ErrorCode.STUDY_UID_MISMATCH,
                expected=requested_study_uid,
                actual=echoed_text or "（未回传）",
                failure_class=FAILURE_STUDY_UID_MISMATCH,
            )

        # 候选列表：映射值可以是字段名，也可以是点号路径（形如 a.b）。
        candidates_path = self._field_name("candidates")
        hit, raw_candidates = resolve_path(payload, candidates_path)
        if not hit:
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail=f"推理服务响应（模型 {model_label}）缺少字段 {candidates_path}",
                failure_class=FAILURE_RESPONSE_FIELD_MISSING,
            )
        if raw_candidates is None:
            # 字段在、值为 null：厂商**没给出候选列表**。
            # 这是「拿不到结果」，不是「没有病灶」—— 当成空列表就等于凭空造出一个阴性。
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail=(
                    f"推理服务响应（模型 {model_label}）的字段 {candidates_path} 为 null"
                    f"（厂商未返回候选列表），不能按未检出处理"
                ),
                failure_class=FAILURE_RESPONSE_FIELD_MISSING,
            )
        if isinstance(raw_candidates, (str, bytes)) or not isinstance(raw_candidates, Sequence):
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail=(
                    f"推理服务响应（模型 {model_label}）的字段 {candidates_path} 不是候选列表"
                ),
                failure_class=FAILURE_RESPONSE_FIELD_MISSING,
            )

        position_path = self._field_name("position")
        size_path = self._field_name("size")
        density_path = self._field_name("density")
        confidence_path = self._field_name("confidence")

        candidates: list[Candidate] = []
        for index, item in enumerate(raw_candidates):
            if not isinstance(item, Mapping):
                raise NoduleEvalError(
                    ErrorCode.FIELD_MAPPING_MISSING,
                    detail=f"推理服务响应（模型 {model_label}）的第 {index + 1} 个候选不是对象",
                    failure_class=FAILURE_RESPONSE_FIELD_MISSING,
                )
            values: dict[str, Any] = {}
            for key, path in (
                ("position", position_path),
                ("size", size_path),
                ("density", density_path),
                ("confidence", confidence_path),
            ):
                found, value = resolve_path(item, path)
                if not found:
                    raise NoduleEvalError(
                        ErrorCode.FIELD_MAPPING_MISSING,
                        detail=(
                            f"推理服务响应（模型 {model_label}）的第 {index + 1} 个候选"
                            f"缺少字段 {path}"
                        ),
                        failure_class=FAILURE_RESPONSE_FIELD_MISSING,
                    )
                values[key] = value

            # 部位与密度是**判定要用**的字段：空值等于「厂商没给」，
            # 不能拿空串或 "None" 字面量凑一个部位（否则后面按部位比对时
            # 会出现一个根本不存在的病灶位置）。
            position = _text_or_none(values["position"])
            if position is None:
                raise NoduleEvalError(
                    ErrorCode.FIELD_MAPPING_MISSING,
                    detail=(
                        f"推理服务响应（模型 {model_label}）的第 {index + 1} 个候选"
                        f"字段 {position_path} 为空（厂商未给出病灶部位）"
                    ),
                    failure_class=FAILURE_RESPONSE_FIELD_MISSING,
                )
            density = _text_or_none(values["density"])
            if density is None:
                raise NoduleEvalError(
                    ErrorCode.FIELD_MAPPING_MISSING,
                    detail=(
                        f"推理服务响应（模型 {model_label}）的第 {index + 1} 个候选"
                        f"字段 {density_path} 为空（厂商未给出病灶密度）"
                    ),
                    failure_class=FAILURE_RESPONSE_FIELD_MISSING,
                )

            candidates.append(
                Candidate(
                    position=position,
                    size_mm=self._size_value(
                        values["size"], model_label=model_label, index=index, path=size_path
                    ),
                    density=density,
                    confidence=_as_float(values["confidence"]),
                )
            )

        # **回显字段不伪造**：厂商没回传 model_id / model_version 时，
        # 回显留空（``echoed_model_*``），产品内部用的版本仍取本次请求 ——
        # 但底账里留的是「厂商说了什么」，不是「我们以为厂商说了什么」。
        echoed_model_id = _text_or_none(payload.get("model_id")) or ""
        echoed_model_version = _text_or_none(payload.get("model_version")) or ""
        return DetectionResult(
            study_uid=echoed_text,
            model_id=echoed_model_id or self.model_id,
            model_version=echoed_model_version or self.model_version,
            candidates=tuple(candidates),
            raw=dict(payload),
            echoed_model_id=echoed_model_id,
            echoed_model_version=echoed_model_version,
        )

    # ---- 解析（旧入口，转发到公开的 parse_response）----
    def _parse(
        self,
        payload: Any,
        requested_study_uid: str,
        requested_model_id: str = "",
    ) -> DetectionResult:
        """兼容旧调用点 —— 解析规则只有一份，就在 :meth:`parse_response`。"""

        return self.parse_response(payload, requested_study_uid, requested_model_id)

    # ---- 尺寸 ----
    @staticmethod
    def _size_value(
        value: Any,
        *,
        model_label: str,
        index: int,
        path: str,
    ) -> float | None:
        """把厂商给的尺寸归一成毫米数值。

        - ``None``（厂商没给尺寸）→ ``None``，后续按「不可比」处理，**不填 0**；
        - 非数值（``"约 8mm"`` / ``"未知"``）→ 报 ``FIELD_MAPPING_MISSING``
          并写明是哪个候选的哪个字段 —— 把 ``"未知"`` 当成 0 会在尺寸容差比对里
          凭空造出一个 8mm 的病灶。
        """

        if value is None:
            return None
        if isinstance(value, bool):
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail=(
                    f"推理服务响应（模型 {model_label}）的第 {index + 1} 个候选"
                    f"字段 {path} 不是数值（实得布尔值）"
                ),
                failure_class=FAILURE_RESPONSE_FIELD_MISSING,
            )
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                raise NoduleEvalError(
                    ErrorCode.FIELD_MAPPING_MISSING,
                    detail=(
                        f"推理服务响应（模型 {model_label}）的第 {index + 1} 个候选"
                        f"字段 {path} 不是数值（实得 {value.strip()[:40]!r}）"
                    ),
                    failure_class=FAILURE_RESPONSE_FIELD_MISSING,
                ) from None
        raise NoduleEvalError(
            ErrorCode.FIELD_MAPPING_MISSING,
            detail=(
                f"推理服务响应（模型 {model_label}）的第 {index + 1} 个候选"
                f"字段 {path} 不是数值"
            ),
            failure_class=FAILURE_RESPONSE_FIELD_MISSING,
        )
