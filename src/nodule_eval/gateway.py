"""ModelGateway：OpenAI-compatible 单一模型网关。

**出站面（第四条出站链路的起点）**::

    POST {OPENAI_BASE_URL}/chat/completions
    Authorization: Bearer $OPENAI_API_KEY
    Content-Type: application/json

    请求体  {"model": ..., "messages": [{"role": "system", ...}, {"role": "user", ...}],
             "temperature": ...}
    响应体  {"choices": [{"index": 0, "message": {"role": "assistant", "content": "..."}}],
             "usage": {"prompt_tokens": ..., "completion_tokens": ...}}

**为什么不用厂商 SDK**：源产品用 ``langchain_openai.ChatOpenAI`` 把端点、密钥、
模型名、温度一股脑塞进一个客户端对象（``github_ref/MedRAX/main.py:8, 76``；
配置面在 ``main.py:113-117`` 从环境变量收）—— 换一家厂商就得改代码，
院内自建端点与云服务也没法共用同一套配置面。
本模块改用 **OpenAI-compatible 原始 HTTP**：协议面只有四个字段
（``model`` / ``messages`` / ``temperature`` / 可选 ``response_format``），
院内自建端点、云服务、本地部署走**同一份代码**，
「换一个端点」退化成「改一个环境变量」。
业务层（:mod:`nodule_eval.goldstd.merge`）只认 :class:`ModelGatewayPort`，
**不 import 任何厂商 SDK** —— 这一点由 ``tests/test_gateway_protocol.py``
静态扫描钉住。

**失败分类是本模块的第一职责**：模型端点挂了，科室要分清三件事：

1. **端点连不上**（DNS 解析失败 / 连接被拒 / 超时）→ 找信息科看网络与端点进程；
2. **我们的请求被拒了**（4xx，提示词或参数不对）→ 改提示词，重试没用；
3. **服务端报错了**（5xx / 429）→ 端点自己有问题，退避后重试。

混成一句「模型不可用」，值班的人只能挨个试。
因此本模块把失败**显式列成一张表**（:data:`MODEL_FAILURE_CLASSES`），
每类落哪个错误码、能不能重试、失败原因怎么写，全部写在表里。

**重试语义与另外三条出站链路完全一致**（见 :mod:`nodule_eval.adapters.http`）：
连接 / 读取超时**分开计数**，5xx 与 429 指数退避（min 4s），
4xx 参数错误**不重试**（重试必然再错一遍，只会把退避预算浪费掉）。

**任务边界**：本任务落「协议面 + 配置面 + 失败分类 + 结构化输出 + 可审计元数据」。
不可达降级与人工确认点落在任务 24。

**结构化输出（任务 23）**：给了 ``json_schema`` 就按 OpenAI-compatible 的标准形状
附上 ``response_format``；端点**不认**这个参数时（4xx 且响应里点名
``response_format`` / ``json_schema``）自动**回退**成「strict JSON 提示词」再问一次；
回退后模型仍回散文，**再做 1 次修复重试**（把上一轮原文与解析错误回给它，请它只吐
JSON）；仍不可解析即判解析失败 —— **绝不猜、不填默认值**。

**可审计调用元数据（任务 23）**：每次调用落一份 :class:`ModelCallMeta`
（``tool_call_id`` / ``model`` / ``base_url_host`` / ``started_at`` / ``latency_ms`` /
``prompt_tokens`` / ``completion_tokens`` / ``prompt_sha256`` / ``raw_response_sha256`` /
``fallback_used``）。**只落哈希、不落原文** —— 提示词里可能带金标准原文
（可能含患者可识别信息），落盘只留哈希，事后照样能对得上是哪一版提示词。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .adapters.http import (
    KIND_CONNECT_TIMEOUT,
    KIND_CONNECTION_ERROR,
    KIND_READ_TIMEOUT,
    KIND_REQUEST_ERROR,
    HttpClient,
    HttpFailure,
)
from .adapters.vendor_error import VendorError, extract_vendor_error
from .audit.toolcall import new_tool_call_id
from .config import Config, join_url
from .errors import ErrorCode, NoduleEvalError
from .structured import (
    STRICT_JSON_INSTRUCTION,
    build_repair_instruction,
    build_response_format,
    extract_json_payload,
    is_response_format_unsupported,
    required_fields_from_schema,
    validate_required_fields,
)

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "MODEL_FAILURE_CLASSES",
    "FailureClass",
    "ModelCallMeta",
    "ModelCallStats",
    "ModelGateway",
    "StructuredResponse",
    "classify_model_failure",
    "detect_connection_cause",
]


# --------------------------------------------------------------------------
# 失败分类表：八类失败 → 错误码 → 可重试 → 中文标签
# --------------------------------------------------------------------------

#: 连接阶段超时（TCP 握手没在期限内完成）。
FAILURE_CONNECT_TIMEOUT = "connect_timeout"
#: 响应阶段超时（连上了，但端点迟迟不返回）。
FAILURE_READ_TIMEOUT = "read_timeout"
#: 连接被拒：对端端口没人监听（端点进程没起来）。
FAILURE_CONNECTION_REFUSED = "connection_refused"
#: 域名解析失败：主机名解析不出来（DNS 配错 / 域名过期）。
FAILURE_DNS_FAILED = "dns_failed"
#: 连不上，但原因没归类（保留原文，不硬套一个猜测）。
FAILURE_ENDPOINT_UNREACHABLE = "endpoint_unreachable"
#: 请求层异常（TLS 握手失败、代理拦截）。
FAILURE_REQUEST_ERROR = "request_error"
#: 4xx：请求本身不对（提示词太长、参数名不对、鉴权错）。
FAILURE_HTTP_CLIENT_ERROR = "http_client_error"
#: 5xx / 429：端点自身报错或限流。
FAILURE_HTTP_SERVER_ERROR = "http_server_error"


@dataclass(frozen=True)
class FailureClass:
    """一个失败类别的声明。

    ``error_code``
        该类失败落到哪个产品错误码 —— 与 :mod:`nodule_eval.errors` 的错误码表一致。
    ``retryable``
        该类失败在**传输层**值不值得重试。4xx 参数类一律不可重试。
        （注意与错误码表里的 ``retryable`` 分工：那里说的是**案例层**要不要重跑，
        这里是**传输层**要不要再打一次同一个请求。）
    ``label``
        中文短标签，进失败原因文案。
    """

    name: str
    error_code: ErrorCode
    retryable: bool
    label: str


#: **失败分类的唯一真源**（传输层）。任务 24 的降级路径按这张表分流：
#: 「端点连不上」与「请求被拒」要走**不同的降级理由**。
MODEL_FAILURE_CLASSES: dict[str, FailureClass] = {
    FAILURE_CONNECT_TIMEOUT: FailureClass(
        FAILURE_CONNECT_TIMEOUT, ErrorCode.MODEL_UNAVAILABLE, True, "连接阶段超时"
    ),
    FAILURE_READ_TIMEOUT: FailureClass(
        FAILURE_READ_TIMEOUT, ErrorCode.MODEL_UNAVAILABLE, True, "响应阶段超时"
    ),
    FAILURE_CONNECTION_REFUSED: FailureClass(
        FAILURE_CONNECTION_REFUSED, ErrorCode.MODEL_UNAVAILABLE, True, "连接被拒"
    ),
    FAILURE_DNS_FAILED: FailureClass(
        FAILURE_DNS_FAILED, ErrorCode.MODEL_UNAVAILABLE, True, "域名解析失败"
    ),
    FAILURE_ENDPOINT_UNREACHABLE: FailureClass(
        FAILURE_ENDPOINT_UNREACHABLE, ErrorCode.MODEL_UNAVAILABLE, True, "端点不可达"
    ),
    FAILURE_REQUEST_ERROR: FailureClass(
        FAILURE_REQUEST_ERROR, ErrorCode.MODEL_UNAVAILABLE, False, "请求层异常"
    ),
    FAILURE_HTTP_CLIENT_ERROR: FailureClass(
        FAILURE_HTTP_CLIENT_ERROR,
        ErrorCode.MODEL_REQUEST_REJECTED,
        False,
        "请求被端点拒绝",
    ),
    FAILURE_HTTP_SERVER_ERROR: FailureClass(
        FAILURE_HTTP_SERVER_ERROR, ErrorCode.MODEL_UNAVAILABLE, True, "端点服务端错误"
    ),
}


#: 连接被拒的特征串（``requests`` 把 urllib3 的异常包了一层，文字要看全部）。
_CONNECTION_REFUSED_MARKERS: tuple[str, ...] = (
    "connection refused",
    "econnrefused",
    "拒绝连接",
    "actively refused",
)

#: 域名解析失败的特征串（Linux / macOS / urllib3 新版的措辞各不相同）。
_DNS_FAILURE_MARKERS: tuple[str, ...] = (
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "failed to resolve",
    "name resolution",
    "getaddrinfo",
    "unknown host",
    "unknown domain",
    "no address associated with hostname",
    "无法解析",
    "未知主机",
)


def _as_int(value: Any) -> int | None:
    """把 token 计数读成整数；读不出来就 ``None``（**不猜 0**）。

    ``usage.prompt_tokens`` 缺失或不是数字时，**不能填 0** ——
    「这次调用没用 token」与「端点没报 token」是两回事，填 0 就把前者说成了后者。
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def detect_connection_cause(body: str) -> str:
    """连接失败时，分清是「连接被拒」还是「域名解析失败」。

    两者在 ``requests`` 里都是 :class:`requests.exceptions.ConnectionError`，
    但**处置完全不同**：

    - **连接被拒** → 端点进程没起来 / 端口没监听 → 找厂商或院内运维看端点；
    - **域名解析失败** → DNS 配错、域名过期、 hosts 没配 → 找信息科看域名。

    认不出来就落 :data:`FAILURE_ENDPOINT_UNREACHABLE`（原因未归类），
    **不硬套一个猜测** —— 猜错了会把人引到错误的排查方向上。
    """

    text = (body or "").lower()
    if any(marker in text for marker in _DNS_FAILURE_MARKERS):
        return FAILURE_DNS_FAILED
    if any(marker in text for marker in _CONNECTION_REFUSED_MARKERS):
        return FAILURE_CONNECTION_REFUSED
    return FAILURE_ENDPOINT_UNREACHABLE


def classify_model_failure(failure: HttpFailure) -> str:
    """把一次出站失败归到 :data:`MODEL_FAILURE_CLASSES` 的哪一类。

    与 :func:`nodule_eval.adapters.inference.classify_http_failure` 同一套路：
    传输层的 ``kind`` 是中立的，**唯一一处**的业务归类在这里做。
    差别只在「连不上」这一支 —— 模型端点的连接失败要再分
    「连接被拒」与「域名解析失败」。
    """

    if failure.kind == KIND_CONNECT_TIMEOUT:
        return FAILURE_CONNECT_TIMEOUT
    if failure.kind == KIND_READ_TIMEOUT:
        return FAILURE_READ_TIMEOUT
    if failure.kind == KIND_CONNECTION_ERROR:
        return detect_connection_cause(failure.body)
    if failure.kind == KIND_REQUEST_ERROR:
        return FAILURE_REQUEST_ERROR
    if failure.kind == "http_status" and failure.status is not None:
        # 429 是**限流**：端点让我们慢一点，不是「请求写错了」——
        # 它归到可重试的服务端错误那一类（与 http.py 的 RETRYABLE_STATUSES 对齐）。
        if 400 <= failure.status < 500 and failure.status != 429:
            return FAILURE_HTTP_CLIENT_ERROR
    return FAILURE_HTTP_SERVER_ERROR


# --------------------------------------------------------------------------
# 调用统计
# --------------------------------------------------------------------------


@dataclass
class ModelCallStats:
    """一次模型调用的统计（连接阶段与响应阶段的超时**分开计数**）。

    ``attempts``
        这次**逻辑调用**实际发出的请求次数。传输层退避重试算多次，
        结构化路径的「正常 / 回退 / 修复」三次请求也算多次 ——
        审计问「这例调了模型几次」时，这里是**唯一的答案**，
        所以它必须**如实数**，不能只报最后一次请求。
    ``connect_timeouts`` / ``read_timeouts``
        两个阶段各自的超时次数。分开计数是为了回答一个具体问题：
        「这轮是**连不上**，还是**连上了但模型算得慢**？」——
        前者找网络，后者找端点与算力。
    ``elapsed_ms``
        本次调用耗时（毫秒）。
    ``failure_class``
        失败时归到 :data:`MODEL_FAILURE_CLASSES` 的哪一类。
    """

    attempts: int = 0
    connect_timeouts: int = 0
    read_timeouts: int = 0
    elapsed_ms: int = 0
    failure_class: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "connect_timeouts": self.connect_timeouts,
            "read_timeouts": self.read_timeouts,
            "elapsed_ms": self.elapsed_ms,
            "failure_class": self.failure_class,
        }


# --------------------------------------------------------------------------
# 网关
# --------------------------------------------------------------------------

#: 连接阶段超时上限（秒）。模型端点在院内网络里通常秒级可达，
#: 连接给 5 秒足够；再长只会让「端点没起来」拖成一次长等待。
DEFAULT_CONNECT_TIMEOUT = 5.0

#: 响应阶段超时的缺省上限（秒）—— 真正的取值来自 ``MODEL_TIMEOUT``。
DEFAULT_READ_TIMEOUT = 60.0

#: 退避基数与上限（与推理端点、RIS、科研平台**同一套量级**）。
DEFAULT_BACKOFF_BASE = 4.0
DEFAULT_BACKOFF_MAX = 10.0

#: ``response_format.json_schema.name`` 的缺省名（沿用任务 22 的形状）。
DEFAULT_SCHEMA_NAME = "goldstd_merge_output"


# --------------------------------------------------------------------------
# 可审计调用元数据
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCallMeta:
    """一次模型调用的**可审计元数据**。

    这是飞检问询「这例凭什么算漏判」的落点之一：结论行 → 归并标签 →
    **这一次模型调用**（调了哪个模型、哪个端点、耗时多久、用了多少 token、
    提示词是第几版、原始返回长什么样）。

    **只落哈希，不落原文**：``prompt_sha256`` 是提示词的哈希、
    ``raw_response_sha256`` 是原始返回的哈希 —— 提示词与返回里可能带
    金标准原文（可能含患者可识别信息），**不落盘**；要复现时凭哈希对得上
    提示词版本与端点返回，够用了。

    ``fallback_used``
        这一轮**没有**用上 schema 约束（端点不认 ``response_format``，
        或模型回的不是 JSON、做了 1 次修复重试）。复核的人看到它就该知道：
        这一例的结构化输出是**降级拿到的**，可信度与直出不同。
    ``response_format_fallback`` / ``repair_used``
        两条回退路径各自留痕（``fallback_used`` 是它们的并集）。
    ``status`` / ``error_code``
        这一轮是**成了**还是**没成**。**失败的模型调用也出元数据** ——
        「这轮归并为什么降级」要能从账上查出来（任务 33）。
    ``attempts`` / ``connect_timeouts`` / ``read_timeouts``
        这次逻辑调用实际打出去几次、两个阶段的超时各几次 ——
        「连不上」与「连上了但算得慢」是两回事，处置完全不同。
    """

    tool_call_id: str
    model: str
    base_url_host: str
    started_at: str
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    prompt_sha256: str
    raw_response_sha256: str
    fallback_used: bool
    response_format_fallback: bool = False
    repair_used: bool = False
    status: str = "ok"
    error_code: str = ""
    attempts: int = 1
    connect_timeouts: int = 0
    read_timeouts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "model": self.model,
            "base_url_host": self.base_url_host,
            "started_at": self.started_at,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "prompt_sha256": self.prompt_sha256,
            "raw_response_sha256": self.raw_response_sha256,
            "fallback_used": self.fallback_used,
            "response_format_fallback": self.response_format_fallback,
            "repair_used": self.repair_used,
            "status": self.status,
            "error_code": self.error_code,
            "attempts": self.attempts,
            "connect_timeouts": self.connect_timeouts,
            "read_timeouts": self.read_timeouts,
        }


@dataclass(frozen=True)
class StructuredResponse:
    """一次结构化调用的结果。

    ``payload``
        解析并校验过的 JSON 对象（**不是**模型的原始文本）。
    ``raw``
        模型这一轮返回的**原始文本**（修复重试后就是修复那一轮的原文）。
    ``meta``
        这一次的可审计元数据（见 :class:`ModelCallMeta`）。
    ``fallback_used``
        是否走过回退路径（与 ``meta.fallback_used`` 一致，便于调用方直接读）。
    """

    payload: Mapping[str, Any]
    raw: str
    meta: ModelCallMeta
    fallback_used: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "payload": dict(self.payload),
            "raw": self.raw[:2000],
            "meta": self.meta.to_dict(),
            "fallback_used": self.fallback_used,
        }


class ModelGateway:
    """OpenAI-compatible 单一模型网关。

    **两个出口**：

    - :meth:`complete` —— 把「一段 system + 一段 user」变成「模型的原始回复文本」。
      业务层的**端口契约**（:class:`nodule_eval.goldstd.merge.ModelGatewayPort`）
      就是它；解析与回指校验在业务层做。
    - :meth:`complete_structured` —— 同一次调用，但**要结构化结果**：
      按 JSON Schema 请求、必要时回退 + 修复重试、校验必填字段，
      并带回一份 :class:`ModelCallMeta`。

    两条路都**只负责「把请求按协议发出去，把失败按分类表说清楚」**，
    **不决定**失败后怎么办（降级路径在任务 24）。
    """

    def __init__(
        self,
        config: Config,
        *,
        client: HttpClient | None = None,
        base_url: str | None = None,
    ) -> None:
        self._config = config
        # 连接超时取「MODEL_TIMEOUT 与 5 秒的较小者」：
        # 端点连不上时不该等满整个读超时；读超时才用 MODEL_TIMEOUT。
        connect_timeout = min(DEFAULT_CONNECT_TIMEOUT, config.model_timeout)
        self._client = (
            client
            if client is not None
            else HttpClient(
                connect_timeout=connect_timeout,
                read_timeout=config.model_timeout,
                # MODEL_MAX_RETRIES 是「额外重试次数」：
                # 默认 2 → 最多 3 次尝试（与另外三条出站链路的退避量级一致）。
                max_attempts=config.model_max_retries + 1,
                backoff_base=DEFAULT_BACKOFF_BASE,
                backoff_max=DEFAULT_BACKOFF_MAX,
            )
        )
        base = base_url if base_url is not None else config.openai_base_url
        self.url = join_url(base, "/chat/completions")
        self.model = config.openai_model
        #: 最近一次调用的统计（供调用底账与耗时台账取用）。
        self.last_stats = ModelCallStats()
        #: 最近一次调用的可审计元数据（任务 33 落 ``model_calls.jsonl`` 时取它）。
        #:
        #: **按线程隔离**：一个网关实例会被并发 fan-out 的多个工作线程共用，
        #: 共享一个可变属性会互相覆盖 —— 复核的人看到的「最近一次」
        #: 可能根本不是自己那一例的（与 ``ModelMergeNormalizer.last_result``
        #: 同一个道理）。所以这里用 :class:`threading.local` 存。
        self._meta_local = threading.local()
        #: 最近一次**实际发出去**的请求体 —— 失败时用它算提示词哈希
        #: （回退 / 修复重试会换请求体，哈希得对得上真正发出去的那一份）。
        self._last_request_body: dict[str, Any] | None = None
        #: 最近一次调用的起始时刻（ISO，本地时区，秒级）。
        self._started_at = datetime.now().isoformat(timespec="seconds")

    # ---- 最近一次调用的元数据（按线程隔离） ----

    @property
    def last_meta(self) -> ModelCallMeta | None:
        """最近一次调用的可审计元数据（**当前线程**的那一次）。"""

        return getattr(self._meta_local, "meta", None)

    @last_meta.setter
    def last_meta(self, value: ModelCallMeta | None) -> None:
        self._meta_local.meta = value

    # ---- 请求体 ----
    def build_body(
        self,
        *,
        system: str,
        user: str,
        json_schema: Mapping[str, Any] | None = None,
        extra_messages: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """按 OpenAI-compatible 协议组装请求体。

        形状固定为 ``{model, messages, temperature}``（``temperature`` 取
        ``MODEL_TEMPERATURE``）。``json_schema`` 给了就按标准形状附上
        ``response_format``（``{"type": "json_schema", "json_schema": {...}}``）。

        ``extra_messages``
            追加在 system / user 之后的对话消息 —— **修复重试**用它把上一轮
            模型原文与解析错误一起回给模型（让它知道自己错在哪）。
        """

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if extra_messages:
            messages.extend(dict(item) for item in extra_messages)

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self._config.model_temperature,
        }
        if json_schema is not None:
            body["response_format"] = build_response_format(
                json_schema, name=DEFAULT_SCHEMA_NAME
            )
        return body

    # ---- 失败映射 ----
    def map_failure(self, failure: HttpFailure) -> NoduleEvalError:
        """把一次出站失败翻成模型网关的产品错误码。

        走 :data:`MODEL_FAILURE_CLASSES` 这张表：

        ==========================  =========================  ==============
        失败类                      错误码                    可重试（传输层）
        ==========================  =========================  ==============
        连接阶段超时                ``MODEL_UNAVAILABLE``     是
        响应阶段超时                ``MODEL_UNAVAILABLE``     是
        连接被拒                    ``MODEL_UNAVAILABLE``     是
        域名解析失败                ``MODEL_UNAVAILABLE``     是
        端点不可达（原因未归类）    ``MODEL_UNAVAILABLE``     是
        请求层异常（TLS / 代理）    ``MODEL_UNAVAILABLE``     否
        4xx（请求被拒）             ``MODEL_REQUEST_REJECTED`` **否**
        5xx / 429（重试耗尽）       ``MODEL_UNAVAILABLE``     是
        ==========================  =========================  ==============

        4xx 的失败原因里**带上端点自己的错误码与出错字段名** ——
        「模型端点返回 HTTP 400」没法据此去改提示词；
        「厂商错误码 invalid_request，字段 messages」才行。
        """

        class_name = classify_model_failure(failure)
        spec = MODEL_FAILURE_CLASSES[class_name]

        if class_name == FAILURE_HTTP_CLIENT_ERROR:
            vendor = extract_vendor_error(failure.body)
            detail = vendor.describe() or f"端点返回 HTTP {failure.status}"
        elif class_name == FAILURE_HTTP_SERVER_ERROR:
            vendor = extract_vendor_error(failure.body)
            described = vendor.describe()
            # 错误码模板里没有 {status} 位，HTTP 状态码得自己带上 ——
            # 「模型不可用：stubbed failure」没人知道是 500 还是 503。
            detail = (
                f"端点返回 HTTP {failure.status}：{described}"
                if described
                else failure.detail()
            )
        else:
            detail = failure.detail()

        timeout = ""
        if class_name == FAILURE_CONNECT_TIMEOUT:
            timeout = f"连接阶段 {failure.connect_timeout:g} 秒未建立连接"
            detail = f"{timeout}：{failure.body}"
        elif class_name == FAILURE_READ_TIMEOUT:
            timeout = f"响应阶段 {failure.read_timeout:g} 秒未返回"
            detail = f"{timeout}：{failure.body}"

        # 端点连不上时，把**排查方向**写进失败原因：
        # 连接被拒 = 端点进程没起来；DNS 失败 = 域名解析不出来。
        if class_name == FAILURE_CONNECTION_REFUSED:
            detail = f"连接被拒（对端端口未监听，端点进程多半没起来）：{failure.body}"
        elif class_name == FAILURE_DNS_FAILED:
            detail = f"域名解析失败（主机名解析不到，检查 DNS 与 base_url）：{failure.body}"
        elif class_name == FAILURE_ENDPOINT_UNREACHABLE:
            detail = (
                f"连接不上（原因未归类，请核对 base_url 与端点进程）：{failure.body}"
            )

        return NoduleEvalError(
            spec.error_code,
            detail=detail,
            http_status=failure.status,
            failure_class=class_name,
            timeout=timeout,
            attempts=failure.attempts,
            connect_timeouts=failure.connect_timeouts,
            read_timeouts=failure.read_timeouts,
        )

    # ---- 调用 ----
    def complete(
        self,
        *,
        system: str,
        user: str,
        json_schema: Mapping[str, Any] | None = None,
        tool_call_id: str | None = None,
    ) -> str:
        """发一次对话，拿回模型的**原始文本**。

        实现 :class:`nodule_eval.goldstd.merge.ModelGatewayPort`：
        业务层只关心「拿回一段文本」，解析与校验在业务层做。

        失败一律抛 :class:`NoduleEvalError`（带错误码、状态码与失败类别）——
        **不静默返回空串**：空串会被下游当成「模型什么都没说」，
        归并层就会拿一份空的结论去比对，敏感度会被凭空抬高。
        """

        body = self.build_body(system=system, user=user, json_schema=json_schema)
        started = time.monotonic()
        self._started_at = datetime.now().isoformat(timespec="seconds")
        try:
            payload = self._post(body, started=started)
            text = self._content_from_payload(payload)
        except NoduleEvalError as exc:
            # **失败的模型调用也出元数据**（任务 33）：只记成功的，账就是半本的 ——
            # 「这轮归并为什么降级」得从账上查得出来。
            self.last_meta = self._build_failed_meta(
                body=body,
                started_at=self._started_at,
                started=started,
                exc=exc,
                tool_call_id=tool_call_id,
            )
            raise
        self.last_meta = self._build_meta(
            body=body,
            payload=payload,
            raw=text,
            started_at=self._started_at,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            tool_call_id=tool_call_id,
            fallback_used=False,
            response_format_fallback=False,
            repair_used=False,
        )
        return text

    # ---- 结构化调用 ----
    def complete_structured(
        self,
        *,
        system: str,
        user: str,
        json_schema: Mapping[str, Any],
        required_fields: Sequence[str] | None = None,
        tool_call_id: str | None = None,
    ) -> StructuredResponse:
        """要一份**结构化**结果（JSON Schema 约束 + 回退 + 修复重试 + 契约校验）。

        三段式：

        1. **按 schema 请求**：附上 ``response_format``；
        2. **兼容回退**：端点不认 ``response_format``（4xx 且响应点名这个参数）
           → 去掉它，改用「strict JSON 提示词」再问一次
           （``fallback_used`` 记 ``True``）；
        3. **1 次修复重试**：模型回的不是 JSON → 把上一轮原文与解析错误回给它，
           请它只吐 JSON；仍不可解析即判解析失败（抛 ``CONTRACT_INVALID``）。

        必填字段缺任一即判解析失败并**写明缺哪个字段** —— **不猜默认值补齐**。

        失败时的错误码：
        - 传输层失败 → 沿用 :data:`MODEL_FAILURE_CLASSES`（任务 22 的口径）；
        - 输出不合契约 → ``CONTRACT_INVALID``（业务层据此走规则降级）。
        """

        required = (
            tuple(required_fields)
            if required_fields is not None
            else required_fields_from_schema(json_schema)
        )

        started = time.monotonic()
        self._started_at = datetime.now().isoformat(timespec="seconds")
        call_id = tool_call_id or new_tool_call_id()
        body = self.build_body(system=system, user=user, json_schema=json_schema)

        try:
            return self._complete_structured(
                system=system,
                user=user,
                json_schema=json_schema,
                required=required,
                call_id=call_id,
                body=body,
                started=started,
            )
        except NoduleEvalError as exc:
            # **失败的模型调用也出元数据**（任务 33）：端点连不上、请求被拒、
            # 返回缺字段、输出不合契约 —— 这四种都会让归并层降级，
            # 降级的原因得能从账上查出来。
            self.last_meta = self._build_failed_meta(
                body=body,
                started_at=self._started_at,
                started=started,
                exc=exc,
                tool_call_id=call_id,
            )
            raise

    def _complete_structured(
        self,
        *,
        system: str,
        user: str,
        json_schema: Mapping[str, Any],
        required: tuple[str, ...],
        call_id: str,
        body: dict[str, Any],
        started: float,
    ) -> StructuredResponse:
        """结构化调用的**内层**（正常 / 回退 / 修复三段式）。

        拆成内层是为了让外层能统一兜住**失败**：任何一段抛错，
        外层都会补一份失败元数据再往上抛（任务 33 的账要记失败的调用）。
        """

        response_format_fallback = False
        repair_used = False

        # 三次请求（正常 / 回退 / 修复）的计数**累加到同一份** ——
        # 审计时看到的 attempts 就是这次逻辑调用实际打出去几次。
        tally = ModelCallStats()

        try:
            payload = self._post(body, started=started, tally=tally)
        except NoduleEvalError as exc:
            if not is_response_format_unsupported(exc.http_status, exc.detail):
                raise
            # 端点不认 response_format —— 换个问法（strict JSON 提示词）再问一次。
            response_format_fallback = True
            body = self.build_body(
                system=f"{system}\n\n{STRICT_JSON_INSTRUCTION}",
                user=user,
                json_schema=None,
            )
            payload = self._post(body, started=started, tally=tally)

        raw = self._content_from_payload(payload)

        try:
            data = extract_json_payload(raw)
        except NoduleEvalError as exc:
            # 1 次修复重试：把上一轮原文与解析错误回给模型。
            repair_used = True
            body = self.build_body(
                system=system,
                user=user,
                json_schema=None,
                extra_messages=[
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": build_repair_instruction(exc.detail)},
                ],
            )
            payload = self._post(body, started=started, tally=tally)
            raw = self._content_from_payload(payload)
            try:
                data = extract_json_payload(raw)
            except NoduleEvalError as retry_exc:
                raise NoduleEvalError(
                    ErrorCode.CONTRACT_INVALID,
                    detail=(
                        f"模型返回经 1 次修复重试后仍不可解析：{retry_exc.detail}"
                    ),
                    failure_class=FAILURE_HTTP_SERVER_ERROR,
                ) from retry_exc

        validate_required_fields(data, required, source="模型结构化输出")

        meta = self._build_meta(
            body=body,
            payload=payload,
            raw=raw,
            started_at=self._started_at,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            tool_call_id=call_id,
            fallback_used=response_format_fallback or repair_used,
            response_format_fallback=response_format_fallback,
            repair_used=repair_used,
        )
        self.last_meta = meta
        return StructuredResponse(
            payload=data,
            raw=raw,
            meta=meta,
            fallback_used=meta.fallback_used,
        )

    # ---- 出站 ----
    def _post(
        self,
        body: Mapping[str, Any],
        *,
        started: float,
        tally: ModelCallStats | None = None,
    ) -> dict[str, Any]:
        """发一次出站请求，返回**已解析的响应体**（失败按分类表抛）。

        统计（尝试次数、两个阶段的超时计数、耗时）在这里统一更新 ——
        结构化路径最多发三次请求（正常 / 回退 / 修复），统计必须覆盖全部：
        ``tally`` 由调用方创建并一路传下来，三次请求的计数**累加**到同一份，
        审计时看到的 ``attempts`` 就是**这次逻辑调用实际打出去几次**。
        """

        headers = dict(self._config.model_headers)
        stats = tally if tally is not None else ModelCallStats()
        http_stats: dict[str, int] = {}
        # 记住**真正发出去的那一份**请求体 —— 失败时提示词哈希要按它算
        # （回退 / 修复重试会换请求体）。
        self._last_request_body = dict(body)
        try:
            response = self._client.request(
                "POST",
                self.url,
                error_mapper=self.map_failure,
                headers=headers,
                json_body=dict(body),
                stats=http_stats,
            )
        except NoduleEvalError as exc:
            stats.attempts += int(exc.fields.get("attempts", 1) or 1)
            stats.connect_timeouts += int(exc.fields.get("connect_timeouts", 0) or 0)
            stats.read_timeouts += int(exc.fields.get("read_timeouts", 0) or 0)
            stats.failure_class = str(exc.fields.get("failure_class", "") or "")
            stats.elapsed_ms = int((time.monotonic() - started) * 1000)
            self.last_stats = stats
            raise

        stats.attempts += int(http_stats.get("attempts", 1) or 1)
        stats.connect_timeouts += int(http_stats.get("connect_timeouts", 0) or 0)
        stats.read_timeouts += int(http_stats.get("read_timeouts", 0) or 0)
        stats.elapsed_ms = int((time.monotonic() - started) * 1000)
        self.last_stats = stats
        return self._payload_from_response(response)

    # ---- 元数据 ----
    def _build_failed_meta(
        self,
        *,
        body: Mapping[str, Any],
        started_at: str,
        started: float,
        exc: NoduleEvalError,
        tool_call_id: str | None,
    ) -> ModelCallMeta:
        """组装一次**失败**调用的可审计元数据。

        失败的调用也要记账 —— 归并层会把它降级成规则基线，
        事后问「这轮为什么降级」得从账上查得出原因（端点连不上？
        请求被拒？返回缺字段？输出不合契约？）。

        ``raw_response_sha256`` 留**空串**：失败的调用**没有**可哈希的返回体
        （连不上时什么都没回来；输出不合契约时返回的是散文，混在哈希里
        反而说不清是哪一轮）。提示词哈希照算 —— 提示词确实发出去了。
        """

        sent = self._last_request_body or dict(body)
        messages = sent.get("messages") if isinstance(sent, Mapping) else None
        prompt_text = json.dumps(messages or [], ensure_ascii=False, sort_keys=True)

        return ModelCallMeta(
            tool_call_id=tool_call_id or new_tool_call_id(),
            model=self.model,
            base_url_host=urlsplit(self.url).hostname or "",
            started_at=started_at,
            latency_ms=int((time.monotonic() - started) * 1000),
            prompt_tokens=None,
            completion_tokens=None,
            prompt_sha256=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
            raw_response_sha256="",
            fallback_used=False,
            response_format_fallback=False,
            repair_used=False,
            status="failed",
            error_code=exc.code.value,
            attempts=int(exc.fields.get("attempts", 1) or 1),
            connect_timeouts=int(exc.fields.get("connect_timeouts", 0) or 0),
            read_timeouts=int(exc.fields.get("read_timeouts", 0) or 0),
        )

    def _build_meta(
        self,
        *,
        body: Mapping[str, Any],
        payload: Mapping[str, Any],
        raw: str,
        started_at: str,
        elapsed_ms: int,
        tool_call_id: str | None,
        fallback_used: bool,
        response_format_fallback: bool,
        repair_used: bool,
    ) -> ModelCallMeta:
        """组装这次调用的可审计元数据（**只落哈希，不落原文**）。"""

        usage = payload.get("usage") if isinstance(payload, Mapping) else None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        if isinstance(usage, Mapping):
            prompt_tokens = _as_int(usage.get("prompt_tokens"))
            completion_tokens = _as_int(usage.get("completion_tokens"))

        messages = body.get("messages") if isinstance(body, Mapping) else None
        prompt_text = json.dumps(messages or [], ensure_ascii=False, sort_keys=True)

        return ModelCallMeta(
            tool_call_id=tool_call_id or new_tool_call_id(),
            model=self.model,
            base_url_host=urlsplit(self.url).hostname or "",
            started_at=started_at,
            latency_ms=elapsed_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_sha256=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
            raw_response_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            fallback_used=fallback_used,
            response_format_fallback=response_format_fallback,
            repair_used=repair_used,
        )

    # ---- 响应解析 ----
    @staticmethod
    def _payload_from_response(response: Any) -> dict[str, Any]:
        """把 HTTP 响应解析成 JSON 对象（顶层不是对象即报缺字段）。"""

        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail=f"模型端点响应不是合法 JSON：{exc}",
                failure_class=FAILURE_HTTP_SERVER_ERROR,
            ) from exc

        if not isinstance(payload, Mapping):
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail=f"模型端点响应顶层不是对象（实得 {type(payload).__name__}）",
                failure_class=FAILURE_HTTP_SERVER_ERROR,
            )
        return dict(payload)

    @classmethod
    def _content_from_payload(cls, payload: Mapping[str, Any]) -> str:
        """从响应体里取模型的原始文本。

        OpenAI-compatible 的形状是
        ``choices[0].message.content``；部分端点把 ``content`` 拆成分片数组
        （``[{"type": "text", "text": "..."}]``），这里**拼起来**再返回。

        缺字段即报 ``FIELD_MAPPING_MISSING`` 并写明**缺哪一段路径** ——
        不猜、不用空串冒充「模型没说话」。
        """

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail="模型端点响应缺少字段 choices（或 choices 为空）",
                failure_class=FAILURE_HTTP_SERVER_ERROR,
            )

        first = choices[0]
        if not isinstance(first, Mapping):
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail="模型端点响应的 choices[0] 不是对象",
                failure_class=FAILURE_HTTP_SERVER_ERROR,
            )

        message = first.get("message")
        if not isinstance(message, Mapping):
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail="模型端点响应缺少字段 choices[0].message",
                failure_class=FAILURE_HTTP_SERVER_ERROR,
            )

        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, Mapping):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            if parts:
                return "".join(parts)
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail="模型端点响应的 choices[0].message.content 分片里没有文本",
                failure_class=FAILURE_HTTP_SERVER_ERROR,
            )
        if content is None:
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail="模型端点响应的 choices[0].message.content 为空（端点没返回内容）",
                failure_class=FAILURE_HTTP_SERVER_ERROR,
            )
        raise NoduleEvalError(
            ErrorCode.FIELD_MAPPING_MISSING,
            detail=(
                "模型端点响应的 choices[0].message.content 类型不对"
                f"（实得 {type(content).__name__}）"
            ),
            failure_class=FAILURE_HTTP_SERVER_ERROR,
        )
