"""科研平台写回适配器：URL / 鉴权头 / 幂等键 / 请求体字段 / 失败分类。

这是本产品**真正改变业务状态**的那条出站面 —— 前两条（RIS 读、推理服务写）
只读院内与厂商的状态，只有这一条把评估结论**写进**院内系统。

这一组测试盯住五件事：

1. **实际发出去的请求** —— URL 路径、``Authorization`` 头、``Idempotency-Key`` 头、
   请求体字段集与顺序，全部对着替身（真 socket、真 ``requests.Session``）断言，
   不 mock 掉 HTTP 层；
2. **幂等键** —— 同一轮 + 同一数据集 → 同键；换一轮 → 换键；分隔符不会撞键；
3. **重复提交只登记一条** —— 信息科在表格里连点两下「确认提交」，
   科研平台侧只应有一条登记；
4. **失败分类** —— 4xx **不重试**（参数错重试必然再错）、5xx 指数退避最多三次、
   连不上时**没有** HTTP 状态码（不写假的 0 或 500）；
5. **「已发出」不等于「已生效」** —— 回执缺登记号、或回执登记的是另一轮，
   都判失败。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from conftest import make_env
from stub_research_platform import ResearchPlatformStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.adapters.research_platform import (
    DEFAULT_RESEARCH_PATH,
    IDEMPOTENCY_HEADER,
    OPERATOR_ENV,
    RECEIPT_FIELDS,
    SUBMISSION_FIELDS,
    EvaluationSubmission,
    ResearchPlatformAdapter,
    compute_idempotency_key,
    resolve_operator,
)
from nodule_eval.config import load_config
from nodule_eval.errors import ErrorCode, NoduleEvalError


def _submission(**overrides: Any) -> EvaluationSubmission:
    """一份齐备的登记提交体（可逐项覆写）。"""

    payload: dict[str, Any] = {
        "run_id": "run-2026-03-001",
        "model_id": "demo-model",
        "model_version": "v1.0.0",
        "dataset_ref": "eval-table-2026-03.xlsx",
        "metrics": {
            "tp": 120,
            "fp": 8,
            "fn": 3,
            "tn": 69,
            "sensitivity": 0.9756,
            "failed_count": 2,
            "rule_degraded_count": 1,
        },
        "case_rows": [
            {
                "accession_no": "ACC-001",
                "verdict": "hit",
                "model_findings": "右下肺 | 8.0mm | 磨玻璃 | 0.92",
                "failure_reason": "",
                "tool_call_id": "tc_0123456789abcdef0123456789abcdef",
            },
            {
                "accession_no": "ACC-002",
                "verdict": "miss",
                "model_findings": "未检出候选病灶",
                "failure_reason": "",
                "tool_call_id": "tc_abcdef0123456789abcdef0123456789",
            },
        ],
        "submitted_by": "信息科-王工",
        "submitted_at": "2026-03-02T09:30:00",
    }
    payload.update(overrides)
    return EvaluationSubmission(**payload)


def _adapter(stub: ResearchPlatformStub, sleeps: list[float] | None = None) -> ResearchPlatformAdapter:
    """对着替身造一个写回适配器（退避不真睡，测试要快）。"""

    env = make_env(RESEARCH_PLATFORM_BASE_URL=stub.base_url)
    config = load_config(env)
    client = HttpClient(sleep=(sleeps.append if sleeps is not None else (lambda _s: None)))
    return ResearchPlatformAdapter(config, client=client)


# --------------------------------------------------------------------------
# 1. 实际发出去的请求
# --------------------------------------------------------------------------


def test_request_url_auth_header_and_idempotency_key_are_the_spec_ones():
    """URL 路径、鉴权头、幂等键必须与 ``spec.md`` 的字段规范一致。"""

    stub = ResearchPlatformStub()
    stub.start()
    try:
        adapter = _adapter(stub)
        adapter.submit(_submission())
    finally:
        stub.stop()

    assert len(stub.requests) == 1
    request = stub.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == DEFAULT_RESEARCH_PATH == "/api/v1/model-evaluation-runs"
    assert request["headers"]["Authorization"] == "Bearer test-research-token"
    assert request["headers"]["Content-Type"] == "application/json"

    expected = hashlib.sha256(
        b"run-2026-03-001|demo-model|eval-table-2026-03.xlsx"
    ).hexdigest()
    assert request["headers"][IDEMPOTENCY_HEADER] == expected
    # 凭据只在头里 —— 请求体不夹带任何密钥字段。
    assert "token" not in request["body"]
    assert "Authorization" not in request["body"]


def test_request_body_has_exactly_the_spec_fields_in_order():
    """请求体字段集与顺序固定 —— 对端按这份规范收，多一个少一个都要被拒。"""

    stub = ResearchPlatformStub()
    stub.start()
    try:
        adapter = _adapter(stub)
        adapter.submit(_submission())
    finally:
        stub.stop()

    body = stub.requests[0]["body"]
    assert list(body.keys()) == list(SUBMISSION_FIELDS)
    assert body["run_id"] == "run-2026-03-001"
    assert body["model_id"] == "demo-model"
    assert body["model_version"] == "v1.0.0"
    assert body["dataset_ref"] == "eval-table-2026-03.xlsx"
    assert body["submitted_by"] == "信息科-王工"
    assert body["submitted_at"] == "2026-03-02T09:30:00"
    assert body["metrics"]["tp"] == 120
    assert body["metrics"]["sensitivity"] == 0.9756


def test_case_rows_are_passed_through_verbatim():
    """逐例判定行逐字送出去 —— 对端拿它做逐例复核，不许我们这边改写。"""

    stub = ResearchPlatformStub()
    stub.start()
    try:
        adapter = _adapter(stub)
        submission = _submission()
        adapter.submit(submission)
    finally:
        stub.stop()

    body = stub.requests[0]["body"]
    assert body["case_rows"] == [dict(row) for row in submission.case_rows]
    assert body["case_rows"][0]["accession_no"] == "ACC-001"
    assert body["case_rows"][0]["tool_call_id"].startswith("tc_")


# --------------------------------------------------------------------------
# 2. 幂等键
# --------------------------------------------------------------------------


def test_same_run_and_dataset_produce_the_same_key():
    """同一轮 + 同一数据集 → 键值相同（重复提交因此只登记一条）。"""

    first = compute_idempotency_key("run-1", "demo-model", "table-a")
    second = compute_idempotency_key("run-1", "demo-model", "table-a")
    assert first == second
    assert len(first) == 64, "sha256 十六进制串长 64"


def test_different_run_or_dataset_produce_different_keys():
    """换一轮、换一款模型、换一批数据 → 键必须变（否则两轮撞成一条登记）。"""

    base = compute_idempotency_key("run-1", "demo-model", "table-a")
    assert compute_idempotency_key("run-2", "demo-model", "table-a") != base
    assert compute_idempotency_key("run-1", "other-model", "table-a") != base
    assert compute_idempotency_key("run-1", "demo-model", "table-b") != base


def test_key_segments_cannot_collide():
    """分隔符竖线不许出现在段内 —— 否则两轮评估会撞成同一把钥匙。

    撞键的后果是「两轮不同的评估在科研平台侧只登记一条」，
    比报一个契约错严重得多，所以这里**指名报错**而不是悄悄换分隔符
    （换分隔符会与科研平台侧算出来的键对不上，幂等直接失效）。
    """

    for run_id, model_id, dataset_ref, offender in (
        ("a|b", "c", "d", "run_id"),
        ("a", "b|c", "d", "model_id"),
        ("a", "b", "c|d", "dataset_ref"),
    ):
        with pytest.raises(NoduleEvalError) as excinfo:
            compute_idempotency_key(run_id, model_id, dataset_ref)

        err = excinfo.value
        assert err.code is ErrorCode.CONTRACT_INVALID
        assert offender in err.message


def test_repeated_submit_creates_only_one_registration():
    """重复提交只登记一条 —— 信息科连点两下「确认提交」不会多出一条登记。"""

    stub = ResearchPlatformStub()
    stub.start()
    try:
        adapter = _adapter(stub)
        submission = _submission()
        first = adapter.submit(submission)
        second = adapter.submit(submission)
    finally:
        stub.stop()

    assert len(stub.requests) == 2, "两次提交都真的发了出去（幂等由对端保证）"
    assert len(stub.registrations) == 1, "同幂等键只登记一条"
    assert first.registration_id == second.registration_id
    assert first.registration_id.startswith("REG-")
    assert stub.requests[0]["headers"][IDEMPOTENCY_HEADER] == (
        stub.requests[1]["headers"][IDEMPOTENCY_HEADER]
    )


def test_receipt_carries_the_idempotency_key():
    """回执带幂等键 —— 事后能证明「重复提交没有产生第二条登记」。"""

    stub = ResearchPlatformStub()
    stub.start()
    try:
        adapter = _adapter(stub)
        receipt = adapter.submit(_submission())
    finally:
        stub.stop()

    assert receipt.idempotency_key == _submission().idempotency_key()
    assert receipt.run_id == "run-2026-03-001"
    assert receipt.status == "registered"
    assert receipt.received_at
    assert set(RECEIPT_FIELDS) == {
        "registration_id",
        "run_id",
        "status",
        "received_at",
    }


def test_attempts_are_reported_on_the_receipt():
    """回执带实际请求次数 —— 飞检问「这轮写回调了几次」时拿得到真数。"""

    stub = ResearchPlatformStub(status_sequence=[500, 500, 200])
    stub.start()
    try:
        sleeps: list[float] = []
        adapter = _adapter(stub, sleeps)
        receipt = adapter.submit(_submission())
    finally:
        stub.stop()

    assert len(stub.requests) == 3
    assert sleeps == [4.0, 8.0], "指数退避：最小 4 秒起"
    assert receipt.attempts == 3
    assert receipt.http_status == 200
    assert receipt.registration_id.startswith("REG-")


# --------------------------------------------------------------------------
# 3. 失败分类
# --------------------------------------------------------------------------


def test_client_error_is_not_retried():
    """4xx —— 不重试（参数或鉴权错，重试必然再错一遍），一次即失败。"""

    stub = ResearchPlatformStub(status_sequence=[400])
    stub.start()
    try:
        sleeps: list[float] = []
        adapter = _adapter(stub, sleeps)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.submit(_submission())
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.RESEARCH_WRITE_FAILED
    assert err.http_status == 400
    assert "HTTP 400" in err.message
    assert len(stub.requests) == 1, "4xx 不得触发重试"
    assert sleeps == [], "4xx 不得消耗退避预算"


def test_client_error_carries_the_platform_error_detail():
    """4xx 的失败原因要带科研平台自己的话 —— 「HTTP 400」没法据此去改。"""

    stub = ResearchPlatformStub(
        status_sequence=[400],
        reject_body={"error": {"code": "DUPLICATE_DATASET", "param": "dataset_ref"}},
    )
    stub.start()
    try:
        adapter = _adapter(stub)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.submit(_submission())
    finally:
        stub.stop()

    err = excinfo.value
    assert "DUPLICATE_DATASET" in err.message
    assert "dataset_ref" in err.message


def test_server_error_retries_then_fails_with_status_code():
    """连续三次 5xx —— 退避重试耗尽后判失败，并保留原始状态码。"""

    stub = ResearchPlatformStub(status_sequence=[500, 500, 500])
    stub.start()
    try:
        sleeps: list[float] = []
        adapter = _adapter(stub, sleeps)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.submit(_submission())
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.RESEARCH_WRITE_FAILED
    assert err.http_status == 500
    assert "HTTP 500" in err.message
    assert len(stub.requests) == 3, "最多尝试三次，不得无限重试"
    assert sleeps == [4.0, 8.0], "只在两次失败后各退避一次"


def test_transport_failure_reports_that_no_http_response_arrived():
    """连不上 —— 没有 HTTP 状态码，如实写「未收到 HTTP 响应」，不写假的 0 或 500。"""

    env = make_env(RESEARCH_PLATFORM_BASE_URL="http://127.0.0.1:1")
    config = load_config(env)
    client = HttpClient(sleep=lambda _s: None, max_attempts=2)
    adapter = ResearchPlatformAdapter(config, client=client)

    with pytest.raises(NoduleEvalError) as excinfo:
        adapter.submit(_submission())

    err = excinfo.value
    assert err.code is ErrorCode.RESEARCH_WRITE_FAILED
    assert err.http_status is None
    assert "未收到 HTTP 响应" in err.message
    assert "无法连接" in err.message


# --------------------------------------------------------------------------
# 4. 「已发出」不等于「已生效」
# --------------------------------------------------------------------------


class _ReceiptWithoutIdStub(ResearchPlatformStub):
    """回执缺 ``registration_id`` 的替身（拿不到登记号 = 不能算已登记）。"""

    def build_payload(self, request: Any = None) -> dict[str, Any]:
        return {"run_id": "run-2026-03-001", "status": "registered"}


class _MismatchedRunStub(ResearchPlatformStub):
    """回执登记的是另一轮的替身（对端登记的压根不是这一轮）。"""

    def build_payload(self, request: Any = None) -> dict[str, Any]:
        return {
            "registration_id": "REG-9999",
            "run_id": "run-other",
            "status": "registered",
            "received_at": "2026-03-02T09:31:00+08:00",
        }


def test_receipt_without_registration_id_is_treated_as_failure():
    """回执缺登记号 —— 判失败，不报「已登记」。"""

    stub = _ReceiptWithoutIdStub()
    stub.start()
    try:
        adapter = _adapter(stub)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.submit(_submission())
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.RESEARCH_WRITE_FAILED
    assert "registration_id" in err.message


def test_receipt_for_another_run_is_treated_as_failure():
    """回执登记的是另一轮 —— 判失败（比「没登记」更难查）。"""

    stub = _MismatchedRunStub()
    stub.start()
    try:
        adapter = _adapter(stub)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.submit(_submission())
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.RESEARCH_WRITE_FAILED
    assert "run-other" in err.message


# --------------------------------------------------------------------------
# 5. 提交体的字段校验
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    ["run_id", "model_id", "model_version", "dataset_ref", "submitted_by", "submitted_at"],
)
def test_missing_required_field_is_named(field_name: str):
    """必填字段为空 —— 指名报错，不构造「半个提交体」发出去。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        _submission(**{field_name: ""})

    err = excinfo.value
    assert err.code is ErrorCode.CONTRACT_INVALID
    assert field_name in err.message


def test_case_rows_must_be_objects():
    """逐例行不是对象 —— 指名报错（对端拒收只是浪费一次登记）。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        _submission(case_rows=["ACC-001"])

    err = excinfo.value
    assert err.code is ErrorCode.CONTRACT_INVALID
    assert "case_rows" in err.message


def test_resolve_operator_prefers_the_environment_variable():
    """操作者标识优先取环境变量 —— 登记台账上写得出是谁提交的。"""

    assert resolve_operator({OPERATOR_ENV: "信息科-王工"}) == "信息科-王工"


def test_resolve_operator_names_the_variable_when_it_cannot_be_resolved(monkeypatch):
    """取不到操作者标识 —— 指名报错，不让登记台账上写「未知提交人」。"""

    monkeypatch.setattr("getpass.getuser", lambda: "")
    with pytest.raises(NoduleEvalError) as excinfo:
        resolve_operator({})

    err = excinfo.value
    assert err.code is ErrorCode.CONTRACT_INVALID
    assert OPERATOR_ENV in err.message


# --------------------------------------------------------------------------
# 6. 替身纪律
# --------------------------------------------------------------------------


def test_adapter_never_falls_back_to_a_local_endpoint():
    """默认出站地址只来自 ``RESEARCH_PLATFORM_BASE_URL`` —— 代码里没有本地回环兜底。"""

    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "nodule_eval"
        / "adapters"
        / "research_platform.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("localhost", "127.0.0.1", "example.com", "http://"):
        assert forbidden not in source, f"写回适配器里不应出现 {forbidden}"


def test_adapter_does_not_import_test_stubs():
    """产品代码不 import 任何替身模块 —— 替身只存在于 ``tests/``。"""

    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "nodule_eval"
        / "adapters"
        / "research_platform.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("stub_research_platform", "from stub", "import stub"):
        assert forbidden not in source, f"写回适配器里不应出现 {forbidden}"
