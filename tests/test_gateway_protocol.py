"""模型网关（OpenAI-compatible）的协议面、配置面与失败分类。

这些断言对应院内跑批时模型端点会出的四类问题：

1. **协议对不上** —— 端点路径、鉴权头、请求体形状错一个，模型就回不来；
2. **端点连不上** —— DNS 解析失败 / 连接被拒 / 超时，三者的排查方向完全不同；
3. **请求被拒**（4xx）—— 我们的提示词或参数不对，**重试没有意义**；
4. **端点自己坏了**（5xx / 429）—— 退避后重试。

另有一条硬约束：**业务层不得 import 任何厂商 SDK**。院内自建端点与云服务
必须走同一份代码，换端点只改环境变量 —— 这条由静态扫描钉住。
"""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
from conftest import make_env

from nodule_eval.adapters.http import (
    KIND_CONNECT_TIMEOUT,
    KIND_CONNECTION_ERROR,
    KIND_READ_TIMEOUT,
    KIND_REQUEST_ERROR,
    HttpClient,
    HttpFailure,
)
from nodule_eval.config import load_config
from nodule_eval.errors import ERROR_TABLE, ErrorCode, NoduleEvalError
from nodule_eval.gateway import (
    FAILURE_CONNECTION_REFUSED,
    FAILURE_DNS_FAILED,
    FAILURE_ENDPOINT_UNREACHABLE,
    FAILURE_HTTP_CLIENT_ERROR,
    FAILURE_HTTP_SERVER_ERROR,
    MODEL_FAILURE_CLASSES,
    ModelGateway,
    classify_model_failure,
    detect_connection_cause,
)
from nodule_eval.goldstd.merge import ModelMergeNormalizer, merge_by_model
from stub_model import ModelStub

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _config(**overrides: str):
    """取一份配置（环境变量全部为占位值，无真实凭据）。"""

    return load_config(make_env(**overrides))


def _gateway(
    base_url: str | None = None,
    *,
    sleeps: list[float] | None = None,
    client: HttpClient | None = None,
    **env: str,
) -> ModelGateway:
    """造一个网关。

    ``sleeps`` 用来把退避秒数记下来（不真睡）；``client`` 用来注入自定义会话
    （超时分别计数那条用例需要）。
    """

    config = _config(**env)
    if client is None:
        client = HttpClient(
            sleep=(sleeps.append if sleeps is not None else (lambda _s: None))
        )
    return ModelGateway(config, client=client, base_url=base_url)


def _closed_port() -> int:
    """拿一个**确定没人监听**的端口（绑定后立刻关掉）。"""

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _direct_session():
    """一个**不走系统代理**的会话。

    院内机器可能配了系统代理，代理会把「域名解析失败」截胡成它自己的一律 502 ——
    那样测的就不是「DNS 解析失败」这条路径了。端点不可达的两条用例必须直连，
    才测得到真正的失败形态。
    """

    import requests

    session = requests.Session()
    session.trust_env = False
    return session


class _ScriptedSession:
    """按脚本抛异常的假会话（只在本单测里用来逼出**超时分别计数**）。"""

    def __init__(self, actions: Sequence[str]) -> None:
        self.actions = list(actions)
        self.calls = 0

    def request(self, *_args: Any, **_kwargs: Any) -> Any:
        import requests

        action = self.actions[min(self.calls, len(self.actions) - 1)]
        self.calls += 1
        if action == "connect_timeout":
            raise requests.exceptions.ConnectTimeout("连接阶段超时")
        if action == "read_timeout":
            raise requests.exceptions.ReadTimeout("响应阶段超时")
        raise AssertionError(f"未预期的动作：{action}")


def _wrap(failure: HttpFailure) -> NoduleEvalError:
    """把 HttpFailure 翻成带计数的错误（网关那条路径的等价物）。"""

    return NoduleEvalError(
        ErrorCode.MODEL_UNAVAILABLE,
        detail=failure.detail(),
        attempts=failure.attempts,
        connect_timeouts=failure.connect_timeouts,
        read_timeouts=failure.read_timeouts,
    )


class _RawPayloadStub(ModelStub):
    """能回任意响应体的替身（用来造「响应形状不对」的场景）。"""

    def __init__(self, payload: Any, *, status: int = 200) -> None:
        super().__init__()
        self._payload = payload
        self._status = status

    def next_response(self, request: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        return self._status, self._payload


# --------------------------------------------------------------------------
# 1. 协议面：路径、鉴权头、请求体形状
# --------------------------------------------------------------------------


def test_request_goes_to_chat_completions_path():
    """请求打在 ``{base}/chat/completions`` 上（base 尾斜杠不产生双斜杠）。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url + "/")
        gateway.complete(system="你是归并器", user="右下肺见一磨玻璃结节")
    finally:
        stub.stop()

    assert len(stub.requests) == 1
    assert stub.requests[0]["method"] == "POST"
    assert stub.requests[0]["path"] == "/chat/completions"
    assert "//chat" not in stub.requests[0]["path"], "base 尾斜杠不得拼出双斜杠"


def test_authorization_header_and_content_type():
    """鉴权头是 ``Bearer $OPENAI_API_KEY``，内容类型是 JSON。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    headers = stub.requests[0]["headers"]
    assert headers["Authorization"] == "Bearer test-model-token"
    assert headers["Content-Type"] == "application/json"


def test_request_body_shape():
    """请求体形状固定为 ``{model, messages, temperature}``。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        gateway.complete(system="系统口径", user="金标准原文")
    finally:
        stub.stop()

    body = stub.requests[0]["body"]
    assert set(body.keys()) == {"model", "messages", "temperature"}
    assert body["model"] == "nodule-goldstd-merge"
    assert body["temperature"] == 0.0
    assert body["messages"] == [
        {"role": "system", "content": "系统口径"},
        {"role": "user", "content": "金标准原文"},
    ]


def test_temperature_comes_from_env():
    """``temperature`` 取自 ``MODEL_TEMPERATURE``（不写死）。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url, MODEL_TEMPERATURE="0.3")
        gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert stub.requests[0]["body"]["temperature"] == 0.3


def test_json_schema_adds_response_format():
    """给了 JSON Schema 就按 OpenAI-compatible 标准形状附上 ``response_format``。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        gateway.complete(
            system="s",
            user="u",
            json_schema={
                "type": "object",
                "properties": {"presence": {"type": "boolean"}},
                "required": ["presence"],
            },
        )
    finally:
        stub.stop()

    response_format = stub.requests[0]["body"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"]["required"] == ["presence"]
    assert response_format["json_schema"]["strict"] is True


def test_no_response_format_without_schema():
    """不给 JSON Schema 时请求体里**没有** ``response_format``。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert "response_format" not in stub.requests[0]["body"]


def test_content_is_extracted_from_choices():
    """正常响应：取 ``choices[0].message.content``。"""

    stub = ModelStub(content='{"presence": true}')
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        text = gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert text == '{"presence": true}'


def test_content_parts_are_joined():
    """``content`` 是分片数组时按顺序拼起来。"""

    stub = ModelStub(
        content_parts=[
            {"type": "text", "text": '{"presence": '},
            {"type": "text", "text": "true}"},
        ]
    )
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        text = gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert text == '{"presence": true}'


# --------------------------------------------------------------------------
# 2. 响应缺字段：不猜、不用空串冒充
# --------------------------------------------------------------------------


def test_missing_choices_is_reported_with_field_name():
    """响应缺 ``choices`` → ``FIELD_MAPPING_MISSING`` 并写明缺哪个字段。"""

    stub = ModelStub(omit_fields=("choices",))
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "choices" in excinfo.value.message


def test_missing_message_is_reported_with_field_name():
    """响应缺 ``choices[0].message`` → 点名到那一段路径。"""

    stub = _RawPayloadStub({"choices": [{"index": 0, "finish_reason": "stop"}]})
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "choices[0].message" in excinfo.value.message


def test_null_content_is_not_treated_as_empty_answer():
    """``content`` 为 null → 报缺字段，**不**返回空串（空串会被当成「模型什么都没说」）。"""

    stub = _RawPayloadStub(
        {"choices": [{"index": 0, "message": {"role": "assistant", "content": None}}]}
    )
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "content" in excinfo.value.message


def test_non_object_response_is_reported():
    """响应顶层不是对象 → ``FIELD_MAPPING_MISSING``（不猜、不当空回复）。"""

    stub = _RawPayloadStub(["不是对象"])
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "顶层不是对象" in excinfo.value.message


# --------------------------------------------------------------------------
# 3. 重试语义：5xx 退避、4xx 不重试、429 可重试
# --------------------------------------------------------------------------


def test_server_error_retries_with_exponential_backoff():
    """前两次 5xx、第三次成功 —— 退避秒数为指数退避（4s、8s）。"""

    stub = ModelStub(status_sequence=[500, 500, 200])
    stub.start()
    try:
        sleeps: list[float] = []
        gateway = _gateway(stub.base_url, sleeps=sleeps)
        text = gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert len(stub.requests) == 3, "应恰好试了三次（前两次服务端报错）"
    assert sleeps == [4.0, 8.0], "退避应为指数退避：最小 4 秒起"
    assert json.loads(text)["presence"] is True


def test_three_consecutive_server_errors_fail():
    """连续三次 5xx → ``MODEL_UNAVAILABLE``，且**不再重试**。"""

    stub = ModelStub(status_sequence=[500, 500, 500])
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert excinfo.value.http_status == 500
    assert "HTTP 500" in excinfo.value.message, "失败原因要带上原始 HTTP 状态码"
    assert len(stub.requests) == 3, "最多尝试三次，不得无限重试"
    assert gateway.last_stats.attempts == 3


def test_client_error_is_not_retried_and_keeps_vendor_code():
    """4xx → 不重试，落 ``MODEL_REQUEST_REJECTED``，并带上端点自己的错误码与字段名。"""

    stub = ModelStub(
        status_sequence=[400],
        error_body={
            "error": {
                "code": "invalid_request_error",
                "param": "messages",
                "message": "system prompt exceeds context window",
            }
        },
    )
    stub.start()
    try:
        sleeps: list[float] = []
        gateway = _gateway(stub.base_url, sleeps=sleeps)
        with pytest.raises(NoduleEvalError) as excinfo:
            gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.MODEL_REQUEST_REJECTED
    assert excinfo.value.http_status == 400
    assert len(stub.requests) == 1, "4xx 不重试"
    assert sleeps == [], "4xx 不消耗退避预算"
    assert "invalid_request_error" in excinfo.value.message
    assert "messages" in excinfo.value.message


def test_rate_limit_is_retried():
    """429 是限流（不是请求写错）→ 退避后重试。"""

    stub = ModelStub(status_sequence=[429, 200])
    stub.start()
    try:
        sleeps: list[float] = []
        gateway = _gateway(stub.base_url, sleeps=sleeps)
        text = gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert len(stub.requests) == 2
    assert sleeps == [4.0]
    assert json.loads(text)["presence"] is True


# --------------------------------------------------------------------------
# 4. 端点不可达分类：连接被拒 / DNS 失败 / 超时 各自可区分
# --------------------------------------------------------------------------


def test_connection_refused_is_classified():
    """连接被拒（端点进程没起来）→ ``connection_refused``，失败原因点明排查方向。"""

    port = _closed_port()
    client = HttpClient(session=_direct_session(), sleep=lambda _s: None)
    gateway = _gateway(f"http://127.0.0.1:{port}", client=client)
    with pytest.raises(NoduleEvalError) as excinfo:
        gateway.complete(system="s", user="u")

    assert excinfo.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert excinfo.value.fields["failure_class"] == FAILURE_CONNECTION_REFUSED
    assert "连接被拒" in excinfo.value.message


def test_dns_failure_is_classified():
    """域名解析失败 → ``dns_failed``，失败原因点明是 DNS 与 base_url。"""

    client = HttpClient(session=_direct_session(), sleep=lambda _s: None)
    gateway = _gateway("http://nodule-eval-nonexistent.invalid", client=client)
    with pytest.raises(NoduleEvalError) as excinfo:
        gateway.complete(system="s", user="u")

    assert excinfo.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert excinfo.value.fields["failure_class"] == FAILURE_DNS_FAILED
    assert "域名解析失败" in excinfo.value.message


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("[Errno 61] Connection refused", FAILURE_CONNECTION_REFUSED),
        ("Max retries exceeded ... Connection refused", FAILURE_CONNECTION_REFUSED),
        ("[Errno 8] nodename nor servname provided, or not known", FAILURE_DNS_FAILED),
        ("Name or service not known", FAILURE_DNS_FAILED),
        ("Failed to resolve: x.invalid", FAILURE_DNS_FAILED),
        ("Temporary failure in name resolution", FAILURE_DNS_FAILED),
        ("something else entirely", FAILURE_ENDPOINT_UNREACHABLE),
        ("", FAILURE_ENDPOINT_UNREACHABLE),
    ],
)
def test_connection_cause_detection(body: str, expected: str):
    """「连接被拒」与「域名解析失败」的判定表（认不出就归「原因未归类」，不硬套猜测）。"""

    assert detect_connection_cause(body) == expected


def test_failure_class_table_covers_every_kind():
    """失败分类表：八类失败各落各的错误码，4xx 不可重试。"""

    assert MODEL_FAILURE_CLASSES[FAILURE_HTTP_CLIENT_ERROR].error_code is (
        ErrorCode.MODEL_REQUEST_REJECTED
    )
    assert MODEL_FAILURE_CLASSES[FAILURE_HTTP_CLIENT_ERROR].retryable is False
    assert MODEL_FAILURE_CLASSES[FAILURE_HTTP_SERVER_ERROR].error_code is (
        ErrorCode.MODEL_UNAVAILABLE
    )
    assert MODEL_FAILURE_CLASSES[FAILURE_HTTP_SERVER_ERROR].retryable is True
    assert MODEL_FAILURE_CLASSES[FAILURE_CONNECTION_REFUSED].retryable is True
    assert MODEL_FAILURE_CLASSES[FAILURE_DNS_FAILED].retryable is True


def test_classify_model_failure_by_kind():
    """传输层 kind → 失败类别（4xx 与 429 分开）。"""

    assert (
        classify_model_failure(HttpFailure(kind=KIND_CONNECT_TIMEOUT))
        == "connect_timeout"
    )
    assert classify_model_failure(HttpFailure(kind=KIND_READ_TIMEOUT)) == "read_timeout"
    assert (
        classify_model_failure(HttpFailure(kind=KIND_REQUEST_ERROR)) == "request_error"
    )
    assert (
        classify_model_failure(
            HttpFailure(kind=KIND_CONNECTION_ERROR, body="Connection refused")
        )
        == FAILURE_CONNECTION_REFUSED
    )
    assert (
        classify_model_failure(HttpFailure(kind="http_status", status=400))
        == FAILURE_HTTP_CLIENT_ERROR
    )
    assert (
        classify_model_failure(HttpFailure(kind="http_status", status=429))
        == FAILURE_HTTP_SERVER_ERROR
    )
    assert (
        classify_model_failure(HttpFailure(kind="http_status", status=503))
        == FAILURE_HTTP_SERVER_ERROR
    )


# --------------------------------------------------------------------------
# 5. 连接超时与读取超时**分别计数**
# --------------------------------------------------------------------------


def test_connect_and_read_timeouts_are_counted_separately():
    """一次连接超时、一次读超时 → 两个计数各记 1，不混成一个。"""

    session = _ScriptedSession(["connect_timeout", "read_timeout"])
    client = HttpClient(session=session, max_attempts=2, sleep=lambda _s: None)
    with pytest.raises(NoduleEvalError) as excinfo:
        client.request(
            "POST",
            "https://llm.test-hospital.internal/v1/chat/completions",
            error_mapper=_wrap,
        )
    failure = excinfo.value

    assert failure.fields["connect_timeouts"] == 1
    assert failure.fields["read_timeouts"] == 1
    assert failure.fields["attempts"] == 2


def test_gateway_reports_per_stage_timeout_counts():
    """网关把两个阶段的超时计数带进调用统计。"""

    client = HttpClient(
        session=_ScriptedSession(["connect_timeout", "read_timeout"]),
        max_attempts=2,
        sleep=lambda _s: None,
    )
    gateway = _gateway("https://llm.test-hospital.internal/v1", client=client)

    with pytest.raises(NoduleEvalError):
        gateway.complete(system="s", user="u")

    assert gateway.last_stats.connect_timeouts == 1
    assert gateway.last_stats.read_timeouts == 1
    assert gateway.last_stats.attempts == 2
    assert gateway.last_stats.failure_class == "read_timeout"


def test_read_timeout_is_distinguished_from_connect_timeout():
    """读取超时的失败原因**说得清是哪一段**（不是笼统的「超时」）。"""

    client = HttpClient(
        session=_ScriptedSession(["read_timeout"]), max_attempts=1, sleep=lambda _s: None
    )
    gateway = _gateway("https://llm.test-hospital.internal/v1", client=client)
    with pytest.raises(NoduleEvalError) as excinfo:
        gateway.complete(system="s", user="u")

    assert excinfo.value.fields["failure_class"] == "read_timeout"
    assert "响应阶段" in excinfo.value.message


def test_connect_timeout_is_distinguished_from_read_timeout():
    """连接超时同样说得清是哪一段。"""

    client = HttpClient(
        session=_ScriptedSession(["connect_timeout"]), max_attempts=1, sleep=lambda _s: None
    )
    gateway = _gateway("https://llm.test-hospital.internal/v1", client=client)
    with pytest.raises(NoduleEvalError) as excinfo:
        gateway.complete(system="s", user="u")

    assert excinfo.value.fields["failure_class"] == "connect_timeout"
    assert "连接阶段" in excinfo.value.message


# --------------------------------------------------------------------------
# 6. 配置面：缺凭据指名报错、绝不回落本地替身
# --------------------------------------------------------------------------


def test_missing_credential_is_named():
    """缺 ``OPENAI_API_KEY`` → ``CREDENTIAL_MISSING`` 并**指名变量**。"""

    env = make_env()
    env.pop("OPENAI_API_KEY")
    with pytest.raises(NoduleEvalError) as excinfo:
        load_config(env)

    assert excinfo.value.code is ErrorCode.CREDENTIAL_MISSING
    assert "OPENAI_API_KEY" in excinfo.value.message


def test_gateway_url_comes_from_config_only():
    """网关的端点只来自配置（环境变量）—— 代码里没有硬编码地址。"""

    gateway = ModelGateway(_config(OPENAI_BASE_URL="https://llm.internal/v1"))
    assert gateway.url == "https://llm.internal/v1/chat/completions"
    assert gateway.model == "nodule-goldstd-merge"


def test_no_local_fallback_address_in_source():
    """产品代码里没有本地回环地址（默认路径绝不命中替身）。"""

    offenders: list[str] = []
    for path in SRC_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in ("localhost", "127.0.0.1", "example.com"):
            if marker in text:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{marker}")
    assert offenders == [], f"src/ 里不得出现本地回环地址：{offenders}"


# --------------------------------------------------------------------------
# 7. 业务层不依赖厂商 SDK
# --------------------------------------------------------------------------


def test_no_vendor_sdk_import_in_source():
    """``src/`` 里不得 import 任何厂商 SDK（换端点只改环境变量）。"""

    banned = (
        "openai",
        "langchain",
        "langchain_openai",
        "anthropic",
        "dashscope",
        "zhipuai",
        "ollama",
        "genai",
        "cohere",
        "mistralai",
        "litellm",
    )
    pattern = re.compile(
        r"^\s*(?:from|import)\s+(?:" + "|".join(banned) + r")\b", re.MULTILINE
    )
    offenders: list[str] = []
    for path in SRC_DIR.rglob("*.py"):
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert offenders == [], f"src/ 里不得依赖厂商 SDK：{offenders}"


def test_gateway_implements_the_merge_port():
    """网关满足任务 21 定的端口契约（业务层只认这一个接口）。"""

    gateway = ModelGateway(_config())
    assert hasattr(gateway, "complete")
    varnames = gateway.complete.__code__.co_varnames
    assert {"system", "user", "json_schema"} <= set(varnames)


# --------------------------------------------------------------------------
# 8. 与语义归并层端到端串起来（真 socket + 真 Bearer 头）
# --------------------------------------------------------------------------


def test_merge_through_gateway_end_to_end():
    """归并层 → 网关 → 替身：模型归并结果直接可用（标签四项齐全）。"""

    stub = ModelStub(
        content=json.dumps(
            {
                "presence": True,
                "location": "右肺下叶",
                "size_mm": 8.0,
                "density": "磨玻璃",
                "evidence_span": "右下肺见一磨玻璃结节",
                "confidence": 0.9,
                "needs_human_review": False,
            },
            ensure_ascii=False,
        )
    )
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        result = merge_by_model(
            "右下肺见一磨玻璃结节，最大径约8mm。",
            gateway=gateway,
        )
    finally:
        stub.stop()

    assert result.degraded is False
    assert result.labels.presence is True
    assert result.labels.location == "右肺下叶"
    assert result.labels.size_mm == 8.0
    assert result.labels.density == "磨玻璃"
    assert result.labels.source == "model"


def test_gateway_unreachable_error_carries_routing_info_for_degrade_path():
    """端点连不上时，网关抛**带分类的产品错误**（不静默返回空串）。

    网关这一侧的职责是：把失败**分类清楚**，让降级逻辑能区分
    「端点连不上」（该降级重试或降级到规则）与「请求被拒」（该改提示词）。

    **任务 24 起**：这条降级路径已经接上 ——
    归并层接住 ``MODEL_UNAVAILABLE``，退回规则基线并留下降级标记，
    **不再把异常往上抛**（模型这一层是可选增益，不是主干）。
    """

    port = _closed_port()
    client = HttpClient(session=_direct_session(), sleep=lambda _s: None)
    gateway = _gateway(f"http://127.0.0.1:{port}", client=client)
    normalizer = ModelMergeNormalizer(gateway)

    class _Case:
        gold_standard_text = "右下肺见一磨玻璃结节"
        gold_standard_normalized = None

    # 1. 网关侧：失败仍然按分类表抛，带上排查方向。
    with pytest.raises(NoduleEvalError) as excinfo:
        gateway.complete_structured(
            system="归并口径",
            user="右下肺见一磨玻璃结节",
            json_schema={"type": "object", "properties": {}, "required": []},
        )

    assert excinfo.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert excinfo.value.fields["failure_class"] == FAILURE_CONNECTION_REFUSED

    # 2. 归并侧：接住它 → 退回规则基线，标降级，**不抛异常**。
    labels = normalizer(_Case())
    assert labels.degraded is True
    assert labels.degrade_error_code == ErrorCode.MODEL_UNAVAILABLE.value
    assert labels.source == "rule"
    assert labels.presence is True, "规则基线照常给出确定结论"
    assert "已降级为规则归并" in labels.degrade_reason


# --------------------------------------------------------------------------
# 9. 错误码表登记
# --------------------------------------------------------------------------


def test_model_request_rejected_is_registered():
    """新增错误码进统一错误码表，带中文文案与「不可重试」标记。"""

    spec = ERROR_TABLE[ErrorCode.MODEL_REQUEST_REJECTED.value]
    assert spec.retryable is False
    assert "模型端点拒绝" in spec.message
    assert "HTTP {status}" in spec.message
