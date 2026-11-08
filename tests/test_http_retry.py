"""出站会话层：退避重试、超时分离与错误码映射。

这些断言直接对应院内跑批时最常见的三种故障：厂商服务端 500、请求参数写错被 4xx 拒、
连接根本建不起来。三者的处置必须**不同**：前两者与连接失败可重试，4xx 不可重试。
"""

from __future__ import annotations

import pytest
from conftest import make_env

from nodule_eval.adapters.http import HttpClient
from nodule_eval.adapters.inference import InferenceAdapter
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.errors import ErrorCode, NoduleEvalError
from stub_inference import InferenceStub


def _adapter(base_url: str, sleeps: list[float] | None = None) -> InferenceAdapter:
    env = make_env(INFERENCE_BASE_URL=base_url)
    config = load_config(env)
    spec = load_model_spec("demo-model")
    client = HttpClient(sleep=(sleeps.append if sleeps is not None else (lambda _s: None)))
    return InferenceAdapter(config, spec, client=client)


def test_third_attempt_succeeds_after_two_server_errors():
    """前两次 5xx、第三次成功 —— 客户端最终拿到结果，且确实试了三次。"""

    stub = InferenceStub(status_sequence=[500, 500, 200])
    stub.start()
    try:
        sleeps: list[float] = []
        adapter = _adapter(stub.base_url, sleeps)
        result = adapter.detect(
            study_uid="STUDY-DEMO-001",
            image_ref="dx://demo/study-001",
            model_id="demo-model",
            model_version="v1.0.0",
        )
    finally:
        stub.stop()

    assert len(stub.requests) == 3, "应恰好试了三次（前两次服务端报错）"
    assert sleeps == [4.0, 8.0], "退避应为指数退避：最小 4 秒起"
    assert result.study_uid == "STUDY-DEMO-001"
    assert len(result.candidates) == 1
    assert result.candidates[0].size_mm == 8.0


def test_three_consecutive_server_errors_fail_with_error_code():
    """连续三次 5xx —— 按错误码失败（INFERENCE_5XX），不静默返回空结果。"""

    stub = InferenceStub(status_sequence=[500, 500, 500])
    stub.start()
    try:
        sleeps: list[float] = []
        adapter = _adapter(stub.base_url, sleeps)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.detect(
                study_uid="STUDY-DEMO-001",
                image_ref=None,
                model_id="demo-model",
                model_version="v1.0.0",
            )
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.INFERENCE_5XX
    assert excinfo.value.http_status == 500
    assert "HTTP 500" in excinfo.value.message
    assert len(stub.requests) == 3, "最多尝试三次，不得无限重试"
    assert sleeps == [4.0, 8.0], "只在两次失败后各退避一次"


def test_client_error_is_not_retried():
    """4xx 参数错误 —— 不重试，一次即失败（重试没有意义，只浪费退避预算）。"""

    stub = InferenceStub(status_sequence=[400])
    stub.start()
    try:
        sleeps: list[float] = []
        adapter = _adapter(stub.base_url, sleeps)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.detect(
                study_uid="STUDY-DEMO-001",
                image_ref=None,
                model_id="demo-model",
                model_version="v1.0.0",
            )
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.INFERENCE_4XX
    assert excinfo.value.http_status == 400
    assert len(stub.requests) == 1, "4xx 不得触发重试"
    assert sleeps == [], "4xx 不得消耗退避预算"


class _FakeResponse:
    """最小响应替身（只用于断言超时参数的传递）。"""

    status_code = 200
    text = "{}"

    def json(self):  # noqa: ANN201 - 最小替身
        return {"study_uid": "S", "candidates": []}


class _RecordingSession:
    """记录出站调用的 session 替身。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeResponse()


def test_connect_timeout_and_read_timeout_are_separated():
    """连接超时与读取超时必须分开设置：模型慢 ≠ 连不上。"""

    session = _RecordingSession()
    client = HttpClient(connect_timeout=5.0, read_timeout=60.0, session=session)
    client.request(
        "POST",
        "https://nodule.test-vendor.internal/api/v1/detections",
        error_mapper=lambda failure: NoduleEvalError(ErrorCode.INFERENCE_5XX, detail=failure.detail()),
        headers={"Authorization": "Bearer test-inference-token"},
        json_body={"study_uid": "S"},
    )

    assert len(session.calls) == 1
    timeout = session.calls[0]["timeout"]
    assert isinstance(timeout, tuple) and len(timeout) == 2
    assert timeout[0] == 5.0, "连接超时"
    assert timeout[1] == 60.0, "读取超时"


def test_connection_failure_maps_to_server_unavailable():
    """连不上（连接被拒）—— 归为服务端不可用，不是超时、也不是 4xx。"""

    env = make_env(INFERENCE_BASE_URL="http://127.0.0.1:1")
    config = load_config(env)
    spec = load_model_spec("demo-model")
    client = HttpClient(sleep=lambda _s: None, max_attempts=2)
    adapter = InferenceAdapter(config, spec, client=client)

    with pytest.raises(NoduleEvalError) as excinfo:
        adapter.detect(
            study_uid="STUDY-DEMO-001",
            image_ref=None,
            model_id="demo-model",
            model_version="v1.0.0",
        )

    assert excinfo.value.code is ErrorCode.INFERENCE_5XX


def test_missing_response_field_fails_with_mapping_error():
    """响应缺字段 —— 报 FIELD_MAPPING_MISSING 并写明缺哪个，不用空候选冒充阴性。"""

    stub = InferenceStub(omit_fields=["candidates"])
    stub.start()
    try:
        adapter = _adapter(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.detect(
                study_uid="STUDY-DEMO-001",
                image_ref=None,
                model_id="demo-model",
                model_version="v1.0.0",
            )
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "candidates" in excinfo.value.message
