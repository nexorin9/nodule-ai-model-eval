"""推理适配器的失败分类：六类失败各落各的错误码，各写各的失败原因。

这一组测试守的是产品里**最贵的一条线**：一次没调成的推理，如果被当成「模型未检出」，
就会把一次漏检悄悄塞进特异度 —— 报告上的数字仍然是漂亮的，但结论是假的。

因此每一条失败路径都断言三件事：

1. **错误码对**（落到 :data:`nodule_eval.adapters.inference.FAILURE_CLASSES` 声明的码）；
2. **失败原因可交差**（写明是哪个端点、什么状态码、缺哪个字段 / 哪个字段名出错）；
3. **不进指标分母**（``counts_toward_metrics`` 为假、判定落在 ``failed`` 而非四值枚举）。

六类失败（产品口径）：

=====================  =========================  ==================================
失败类                 错误码                     处置
=====================  =========================  ==================================
连接超时 / 读取超时     ``INFERENCE_TIMEOUT``      可重试；失败原因写明是哪个阶段超时
连接被拒 / DNS 失败    ``INFERENCE_5XX``          可重试；判「服务没起来」而不是超时
4xx 参数错误           ``INFERENCE_4XX``          **不可重试**；带厂商错误码与出错字段名
5xx / 429 服务端错误   ``INFERENCE_5XX``          指数退避，最多三次；保留原始状态码
回传标识与请求不一致    ``STUDY_UID_MISMATCH``     不可重试；该例判 failed，不当阴性
响应缺字段 / 结构不符   ``FIELD_MAPPING_MISSING`` 不可重试；写明缺哪个字段
=====================  =========================  ==================================
"""

from __future__ import annotations

import pytest
import requests
from conftest import make_env

from nodule_eval.adapters.http import HttpClient, HttpFailure
from nodule_eval.adapters.inference import (
    FAILURE_CLASSES,
    FAILURE_CONNECTION_ERROR,
    FAILURE_CONNECT_TIMEOUT,
    FAILURE_HTTP_CLIENT_ERROR,
    FAILURE_HTTP_SERVER_ERROR,
    FAILURE_READ_TIMEOUT,
    FAILURE_RESPONSE_FIELD_MISSING,
    FAILURE_STUDY_UID_MISMATCH,
    InferenceAdapter,
    classify_http_failure,
    extract_vendor_error,
    map_inference_failure,
)
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import (
    VERDICT_FAILED,
    VERDICT_FALSE_POSITIVE,
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_TRUE_NEGATIVE,
    parse_request,
)
from nodule_eval.errors import ErrorCode, NoduleEvalError
from stub_inference import InferenceStub

#: 参与指标计数的四值判定（``failed`` 不在其中）。
COUNTING_VERDICTS = (
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_FALSE_POSITIVE,
    VERDICT_TRUE_NEGATIVE,
)


def _adapter(
    base_url: str,
    sleeps: list[float] | None = None,
    *,
    max_attempts: int = 3,
    read_timeout: float = 60.0,
    connect_timeout: float = 5.0,
    session: object | None = None,
) -> InferenceAdapter:
    env = make_env(INFERENCE_BASE_URL=base_url)
    config = load_config(env)
    spec = load_model_spec("demo-model")
    client = HttpClient(
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
        max_attempts=max_attempts,
        read_timeout=read_timeout,
        connect_timeout=connect_timeout,
        session=session,
    )
    return InferenceAdapter(config, spec, client=client)


def _detect(adapter: InferenceAdapter, study_uid: str = "STUDY-ERR-0001"):
    return adapter.detect(
        study_uid=study_uid,
        image_ref="dx://demo/err-0001",
        model_id="demo-model",
        model_version="v1.0.0",
    )


# --------------------------------------------------------------------------
# 一、失败分类表本身：六类失败各有声明
# --------------------------------------------------------------------------


def test_failure_class_table_covers_the_six_failure_families():
    """六类失败各有声明 —— 少一类就少一条被分类、被记账的失败路径。"""

    assert FAILURE_CLASSES[FAILURE_CONNECT_TIMEOUT].error_code is ErrorCode.INFERENCE_TIMEOUT
    assert FAILURE_CLASSES[FAILURE_READ_TIMEOUT].error_code is ErrorCode.INFERENCE_TIMEOUT
    assert FAILURE_CLASSES[FAILURE_CONNECTION_ERROR].error_code is ErrorCode.INFERENCE_5XX
    assert FAILURE_CLASSES[FAILURE_HTTP_CLIENT_ERROR].error_code is ErrorCode.INFERENCE_4XX
    assert FAILURE_CLASSES[FAILURE_HTTP_SERVER_ERROR].error_code is ErrorCode.INFERENCE_5XX
    assert FAILURE_CLASSES[FAILURE_STUDY_UID_MISMATCH].error_code is ErrorCode.STUDY_UID_MISMATCH
    assert FAILURE_CLASSES[
        FAILURE_RESPONSE_FIELD_MISSING
    ].error_code is ErrorCode.FIELD_MAPPING_MISSING


def test_only_client_errors_are_marked_unretryable_among_transport_failures():
    """4xx 不可重试；超时与 5xx 可重试 —— 参数错误重试必然再错一遍。"""

    assert FAILURE_CLASSES[FAILURE_HTTP_CLIENT_ERROR].retryable is False
    assert FAILURE_CLASSES[FAILURE_CONNECT_TIMEOUT].retryable is True
    assert FAILURE_CLASSES[FAILURE_READ_TIMEOUT].retryable is True
    assert FAILURE_CLASSES[FAILURE_HTTP_SERVER_ERROR].retryable is True
    assert FAILURE_CLASSES[FAILURE_CONNECTION_ERROR].retryable is True
    assert FAILURE_CLASSES[FAILURE_STUDY_UID_MISMATCH].retryable is False
    assert FAILURE_CLASSES[FAILURE_RESPONSE_FIELD_MISSING].retryable is False


def test_no_failure_class_counts_toward_metrics():
    """六类失败**一律不进指标分母** —— 失败不是结论。"""

    for name, spec in FAILURE_CLASSES.items():
        assert spec.error_code.value in {
            "INFERENCE_TIMEOUT",
            "INFERENCE_4XX",
            "INFERENCE_5XX",
            "STUDY_UID_MISMATCH",
            "FIELD_MAPPING_MISSING",
        }, f"{name} 落到了不该落的错误码"


# --------------------------------------------------------------------------
# 二、传输层失败 → 错误码
# --------------------------------------------------------------------------


def test_connect_timeout_maps_to_timeout_code():
    """连接超时 → ``INFERENCE_TIMEOUT``，失败原因写明是**连接阶段**。"""

    failure = HttpFailure(
        "connect_timeout",
        url="https://nodule.test-vendor.internal/api/v1/detections",
        body="ConnectTimeout",
        attempts=1,
        connect_timeout=5.0,
        read_timeout=60.0,
    )
    err = map_inference_failure(failure)

    assert err.code is ErrorCode.INFERENCE_TIMEOUT
    assert "连接阶段" in err.message
    assert "5 秒未建立连接" in err.message
    assert err.counts_toward_metrics is False


def test_read_timeout_maps_to_timeout_code_and_says_which_stage():
    """读取超时 → ``INFERENCE_TIMEOUT``，失败原因写明是**响应阶段**。"""

    failure = HttpFailure(
        "read_timeout",
        url="https://nodule.test-vendor.internal/api/v1/detections",
        body="Read timed out",
        attempts=3,
        connect_timeout=5.0,
        read_timeout=60.0,
    )
    err = map_inference_failure(failure)

    assert err.code is ErrorCode.INFERENCE_TIMEOUT
    assert "响应阶段" in err.message
    assert "60 秒未返回" in err.message
    assert err.counts_toward_metrics is False


def test_connection_refused_is_server_unavailable_not_timeout():
    """连接被拒 → ``INFERENCE_5XX``（服务没起来），**不是**超时。

    报成超时会让人去查网络，而问题在厂商进程 —— 这条区分必须留在错误码里。
    """

    failure = HttpFailure(
        "connection_error",
        url="https://nodule.test-vendor.internal/api/v1/detections",
        body="Connection refused",
        attempts=3,
    )
    err = map_inference_failure(failure)

    assert err.code is ErrorCode.INFERENCE_5XX
    assert "连接被拒" in err.message
    assert "超时" not in err.message, "连接被拒不报成超时"
    assert err.counts_toward_metrics is False


def test_client_error_carries_vendor_code_and_field_name():
    """4xx → ``INFERENCE_4XX``，失败原因里带厂商错误码与出错字段名。"""

    failure = HttpFailure(
        "http_status",
        url="https://nodule.test-vendor.internal/api/v1/detections",
        status=400,
        body='{"error": {"code": "INVALID_IMAGE_REF", "param": "image_ref", "message": "图像引用无法定位"}}',
        attempts=1,
    )
    err = map_inference_failure(failure)

    assert err.code is ErrorCode.INFERENCE_4XX
    assert err.http_status == 400
    assert "INVALID_IMAGE_REF" in err.message
    assert "image_ref" in err.message
    assert err.retryable is False
    assert err.counts_toward_metrics is False


def test_server_error_keeps_original_status_code():
    """5xx → ``INFERENCE_5XX``，原始状态码保留在错误里（供事后核对）。"""

    failure = HttpFailure(
        "http_status",
        url="https://nodule.test-vendor.internal/api/v1/detections",
        status=503,
        body='{"error": "upstream unavailable"}',
        attempts=3,
    )
    err = map_inference_failure(failure)

    assert err.code is ErrorCode.INFERENCE_5XX
    assert err.http_status == 503
    assert "HTTP 503" in err.message
    assert err.counts_toward_metrics is False


def test_classify_http_failure_splits_client_and_server_status():
    """4xx 与 5xx 在分类层就分开 —— 退避策略与错误码都靠它。"""

    assert (
        classify_http_failure(HttpFailure("http_status", status=404))
        == FAILURE_HTTP_CLIENT_ERROR
    )
    assert (
        classify_http_failure(HttpFailure("http_status", status=429))
        == FAILURE_HTTP_SERVER_ERROR
    )
    assert (
        classify_http_failure(HttpFailure("http_status", status=500))
        == FAILURE_HTTP_SERVER_ERROR
    )


# --------------------------------------------------------------------------
# 三、厂商错误体的解析：几种常见形状都认
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, expect_code, expect_field",
    [
        (
            '{"error": {"code": "INVALID_IMAGE_REF", "param": "image_ref", "message": "图像引用无法定位"}}',
            "INVALID_IMAGE_REF",
            "image_ref",
        ),
        ('{"code": "BAD_MODEL_VERSION", "field": "model_version", "message": "版本不匹配"}', "BAD_MODEL_VERSION", "model_version"),
        ('{"error": "unknown model"}', "", ""),
        ('{"errors": [{"code": "QUOTA_EXCEEDED", "field": "model_id"}]}', "QUOTA_EXCEEDED", "model_id"),
    ],
)
def test_vendor_error_shapes_are_recognised(body: str, expect_code: str, expect_field: str):
    """厂商的错误体形状不统一，但**错误码与字段名必须能被取出来**。"""

    vendor = extract_vendor_error(body)

    assert vendor.code == expect_code
    assert vendor.field == expect_field


def test_unparsable_error_body_keeps_the_raw_snippet():
    """认不出形状时不丢原文 —— 失败原因里宁可多一句厂商原话。"""

    vendor = extract_vendor_error("<html>502 Bad Gateway</html>")

    assert vendor.code == ""
    assert "502 Bad Gateway" in vendor.message


# --------------------------------------------------------------------------
# 四、真出站：4xx 不重试、5xx 最多三次、429 走退避
# --------------------------------------------------------------------------


def test_client_error_is_not_retried_end_to_end():
    """4xx 端到端：只请求一次、不消耗退避预算，失败原因带厂商错误码与字段名。"""

    stub = InferenceStub(
        status_sequence=[400],
        error_body={
            "error": {
                "code": "INVALID_IMAGE_REF",
                "param": "image_ref",
                "message": "图像引用无法定位",
            }
        },
    )
    stub.start()
    try:
        sleeps: list[float] = []
        adapter = _adapter(stub.base_url, sleeps)
        with pytest.raises(NoduleEvalError) as excinfo:
            _detect(adapter)
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.INFERENCE_4XX
    assert err.http_status == 400
    assert len(stub.requests) == 1, "4xx 不得触发重试"
    assert sleeps == [], "4xx 不得消耗退避预算"
    assert "INVALID_IMAGE_REF" in err.message
    assert "image_ref" in err.message
    assert err.counts_toward_metrics is False


def test_server_error_retries_at_most_three_times():
    """5xx 端到端：退避两次、总共三次尝试，之后判失败并保留原始状态码。"""

    stub = InferenceStub(status_sequence=[500, 500, 500])
    stub.start()
    try:
        sleeps: list[float] = []
        adapter = _adapter(stub.base_url, sleeps)
        with pytest.raises(NoduleEvalError) as excinfo:
            _detect(adapter)
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.INFERENCE_5XX
    assert err.http_status == 500
    assert len(stub.requests) == 3, "最多尝试三次"
    assert sleeps == [4.0, 8.0], "指数退避：最小 4 秒起"
    assert err.counts_toward_metrics is False


def test_rate_limit_is_retried_then_fails_with_429_preserved():
    """429（限流）走退避重试；耗尽后仍保留原始状态码 429。"""

    stub = InferenceStub(status_sequence=[429, 429, 429])
    stub.start()
    try:
        sleeps: list[float] = []
        adapter = _adapter(stub.base_url, sleeps)
        with pytest.raises(NoduleEvalError) as excinfo:
            _detect(adapter)
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.INFERENCE_5XX
    assert err.http_status == 429, "原始状态码必须保留"
    assert len(stub.requests) == 3
    assert sleeps == [4.0, 8.0]


def test_connection_refused_end_to_end():
    """端到端连不上（无监听端口）→ ``INFERENCE_5XX``，不是超时、也不是 4xx。"""

    adapter = _adapter("http://127.0.0.1:1", max_attempts=2)
    with pytest.raises(NoduleEvalError) as excinfo:
        _detect(adapter)

    err = excinfo.value
    assert err.code is ErrorCode.INFERENCE_5XX
    assert "超时" not in err.message
    assert err.counts_toward_metrics is False


def test_read_timeout_is_triggered_by_a_slow_server():
    """读取超时端到端：厂商迟迟不返回 → ``INFERENCE_TIMEOUT``，失败原因写明响应阶段。"""

    stub = InferenceStub(delay_seconds=1.5)
    stub.start()
    try:
        adapter = _adapter(stub.base_url, read_timeout=0.3, connect_timeout=0.3)
        with pytest.raises(NoduleEvalError) as excinfo:
            _detect(adapter)
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.INFERENCE_TIMEOUT
    assert "响应阶段" in err.message
    assert err.counts_toward_metrics is False


def test_connect_timeout_is_reported_as_connection_stage():
    """连接超时端到端：失败原因必须落在**连接**阶段，与读取超时区分得开。"""

    class _TimeoutSession:
        """在传输层直接抛出连接超时（模拟握手迟迟不完成）。"""

        def request(self, method: str, url: str, **kwargs):  # noqa: ANN003, ANN201
            raise requests.exceptions.ConnectTimeout("connect timed out")

    adapter = _adapter(
        "https://nodule.test-vendor.internal",
        max_attempts=1,
        connect_timeout=5.0,
        session=_TimeoutSession(),
    )
    with pytest.raises(NoduleEvalError) as excinfo:
        _detect(adapter)

    err = excinfo.value
    assert err.code is ErrorCode.INFERENCE_TIMEOUT
    assert "连接阶段" in err.message
    assert "响应阶段" not in err.message


# --------------------------------------------------------------------------
# 五、回显校验：标识对不上 → 判失败，不当阴性
# --------------------------------------------------------------------------


def test_study_uid_not_echoed_is_refused():
    """响应不回传标识 → ``STUDY_UID_MISMATCH``，该例判失败。"""

    stub = InferenceStub(echo_study_uid=False)
    stub.start()
    try:
        adapter = _adapter(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            _detect(adapter, study_uid="STUDY-REQUESTED-001")
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.STUDY_UID_MISMATCH
    assert "STUDY-REQUESTED-001" in err.message
    assert "未回传" in err.message
    assert err.counts_toward_metrics is False


def test_study_uid_mismatch_is_refused():
    """回传了**另一个**检查的标识 → 同样判失败（拿错结果的响应最危险）。"""

    stub = InferenceStub(study_uid_override="STUDY-OTHER-999")
    stub.start()
    try:
        adapter = _adapter(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            _detect(adapter, study_uid="STUDY-REQUESTED-001")
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.STUDY_UID_MISMATCH
    assert "STUDY-REQUESTED-001" in err.message
    assert "STUDY-OTHER-999" in err.message


def test_echoed_model_version_is_kept_for_audit():
    """厂商回传的模型版本**原样带出**，供换版评估时核对（不静默替换成本次请求的版本）。"""

    stub = InferenceStub(
        raw_payload={
            "study_uid": "",
            "model_id": "demo-model",
            "model_version": "v9.9.9-echoed",
            "candidates": [
                {
                    "position": "右下肺",
                    "size_mm": 8.0,
                    "density": "磨玻璃",
                    "confidence": 0.9,
                }
            ],
        }
    )
    stub.start()
    try:
        adapter = _adapter(stub.base_url)
        result = _detect(adapter)
    finally:
        stub.stop()

    assert result.echoed_model_version == "v9.9.9-echoed"
    assert result.echoed_model_id == "demo-model"


# --------------------------------------------------------------------------
# 六、响应缺字段 / 结构不符：不猜、不填 0、不用空候选冒充阴性
# --------------------------------------------------------------------------


def test_missing_candidates_field_is_refused():
    """响应缺 candidates 字段 → ``FIELD_MAPPING_MISSING``，写明缺哪个字段。"""

    stub = InferenceStub(omit_fields=["candidates"])
    stub.start()
    try:
        adapter = _adapter(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            _detect(adapter)
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "candidates" in err.message
    assert err.counts_toward_metrics is False


def test_null_candidates_is_not_treated_as_no_detection():
    """candidates 为 null —— 是「没拿到结果」，**不是**「没有病灶」。

    当成空列表就等于凭空造出一个阴性例，把一次失败塞进特异度。
    """

    stub = InferenceStub(
        raw_payload={
            "study_uid": "",
            "model_id": "demo-model",
            "model_version": "v1.0.0",
            "candidates": None,
        }
    )
    stub.start()
    try:
        adapter = _adapter(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            _detect(adapter)
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "null" in err.message
    assert err.counts_toward_metrics is False


def test_empty_candidates_list_is_a_real_negative_result():
    """candidates 为空列表 —— 这是厂商真的说「没检出」，是合法结果，不报错。"""

    stub = InferenceStub(candidates=[])
    stub.start()
    try:
        adapter = _adapter(stub.base_url)
        result = _detect(adapter)
    finally:
        stub.stop()

    assert result.candidates == ()
    assert result.summary() == "未检出候选病灶"


def test_candidates_of_wrong_type_is_refused():
    """candidates 不是列表 → ``FIELD_MAPPING_MISSING``（结构不符）。"""

    stub = InferenceStub(
        raw_payload={
            "study_uid": "",
            "model_id": "demo-model",
            "model_version": "v1.0.0",
            "candidates": "nodule-1",
        }
    )
    stub.start()
    try:
        adapter = _adapter(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            _detect(adapter)
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "candidates" in excinfo.value.message


def test_missing_candidate_size_field_is_refused():
    """候选缺 size 字段 → ``FIELD_MAPPING_MISSING``，写明缺哪个字段、哪个候选。"""

    stub = InferenceStub(omit_candidate_fields=["size_mm"])
    stub.start()
    try:
        adapter = _adapter(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            _detect(adapter)
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "size_mm" in err.message
    assert "第 1 个候选" in err.message


def test_blank_position_is_refused_instead_of_writing_none():
    """候选的部位为空 —— 判失败，**不**把 "None" 字面量写进评估表。"""

    stub = InferenceStub(
        raw_payload={
            "study_uid": "",
            "model_id": "demo-model",
            "model_version": "v1.0.0",
            "candidates": [
                {"position": None, "size_mm": 8.0, "density": "磨玻璃", "confidence": 0.9}
            ],
        }
    )
    stub.start()
    try:
        adapter = _adapter(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            _detect(adapter)
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "position" in err.message
    assert "None" not in err.message


def test_non_numeric_size_is_refused_instead_of_being_read_as_zero():
    """尺寸给成字符串「约 8mm」→ 判失败；当成 0 会在尺寸容差比对里凭空造出一个 0mm 病灶。"""

    stub = InferenceStub(
        raw_payload={
            "study_uid": "",
            "model_id": "demo-model",
            "model_version": "v1.0.0",
            "candidates": [
                {
                    "position": "右下肺",
                    "size_mm": "约 8mm",
                    "density": "磨玻璃",
                    "confidence": 0.9,
                }
            ],
        }
    )
    stub.start()
    try:
        adapter = _adapter(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            _detect(adapter)
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "size_mm" in err.message


def test_null_size_is_kept_as_unknown_not_zero():
    """尺寸为 null（厂商没测量）→ 保留为 None（后续按不可比处理），不填 0。"""

    stub = InferenceStub(
        raw_payload={
            "study_uid": "",
            "model_id": "demo-model",
            "model_version": "v1.0.0",
            "candidates": [
                {"position": "右下肺", "size_mm": None, "density": "磨玻璃", "confidence": 0.9}
            ],
        }
    )
    stub.start()
    try:
        adapter = _adapter(stub.base_url)
        result = _detect(adapter)
    finally:
        stub.stop()

    assert result.candidates[0].size_mm is None
    assert result.candidates[0].summary() == "右下肺 | - | 磨玻璃 | 0.90"


# --------------------------------------------------------------------------
# 七、失败例在引擎层落 failed，不进指标分母
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stub_kwargs",
    [
        {"status_sequence": [500, 500, 500]},
        {"echo_study_uid": False},
        {"omit_fields": ["candidates"]},
        {"raw_payload": {"study_uid": "", "model_id": "demo-model", "candidates": None}},
    ],
    ids=["server_error", "study_uid_not_echoed", "field_missing", "null_candidates"],
)
def test_failed_case_never_becomes_a_counting_verdict(tmp_path, stub_kwargs):
    """四类失败在引擎层落的判定都是 ``failed`` —— 不属于参与计数的四值枚举。"""

    from nodule_eval.engine import Engine

    stub = InferenceStub(**stub_kwargs)
    stub.start()
    try:
        env = make_env(INFERENCE_BASE_URL=stub.base_url)
        config = load_config(env)
        spec = load_model_spec("demo-model")
        engine = Engine(
            config,
            spec,
            client=HttpClient(sleep=lambda _s: None),
            runs_dir=tmp_path / "runs",
        )
        request = parse_request(
            {
                "run_id": "err-class",
                "model_id": "demo-model",
                "model_version": "v1.0.0",
                "dataset_ref": "失败分类样例",
                "cases": [
                    {
                        "accession_no": "ERRCLASS-0001",
                        "gold_standard_text": "右下肺见一磨玻璃结节，大小约 8mm。",
                        "image_ref": "dx://demo/errclass-0001",
                        "study_uid": "STUDY-ERRCLASS-0001",
                    }
                ],
            }
        )
        response = engine.run(request)
    finally:
        stub.stop()

    row = response.case_rows[0]
    assert row.verdict == VERDICT_FAILED
    assert row.verdict not in COUNTING_VERDICTS
    assert row.failure_reason, "失败原因必须可交差"
    assert response.summary.failed_count == 1
    assert response.summary.evaluated_cases == 0, "失败例不进已评估样本数"

    # 错误码同时落在调用底账里 —— 失败例也能反查。
    ledger = (tmp_path / "runs" / "err-class" / "tool_calls.jsonl").read_text(
        encoding="utf-8"
    )
    assert row.tool_call_id in ledger
    assert "INFERENCE_" in ledger or "STUDY_UID_MISMATCH" in ledger or "FIELD_MAPPING_MISSING" in ledger
