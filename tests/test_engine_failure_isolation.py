"""失败分类与失败隔离：失败例**绝不**进指标分母，单例失败**不阻断**整批。

这一组断言回答科室最常问的三个问题：

1. **这一例是没调成，还是调成了但没检出？** —— 六类失败（连接被拒 /
   读取超时 / 4xx / 5xx / 标识不回显 / 响应缺字段）各落各的错误码，
   失败原因写明是哪个端点、什么状态码、缺哪个字段；
2. **失败例会不会把敏感度算歪？** —— 失败例判 ``failed``，
   **不进 TP / FP / FN / TN 任一计数**，汇总行单列「失败数」；
3. **一例失败会不会把两百例的工作毁掉？** —— 单例失败不终止整批，
   run 的最终状态不会被误判成 ``failed``。

**断言走的是真实出站**：替身是真监听 socket 的 HTTP 服务，
产品的 ``requests.Session`` / URL 拼接 / Bearer 头 / 请求体一行都没改。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, Sequence

import pytest
from stub_inference import InferenceStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import (
    VERDICT_FAILED,
    VERDICT_HIT,
    CaseInput,
    EvalRequest,
)
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode
from nodule_eval.store import CASE_STATUS_FAILED, RUN_STATUS_EVALUATED, RunStore

#: 阳性金标准（放射科写法：部位 + 密度 + 尺寸）。
POSITIVE_GOLD = "右下肺见一磨玻璃结节，大小约 8mm。"
#: 阴性金标准。
NEGATIVE_GOLD = "双肺未见明显结节影。"


def _cases(count: int, gold: str = POSITIVE_GOLD, *, prefix: str = "ACC") -> tuple[CaseInput, ...]:
    """造一批病例（每例自带影像标识，不必回读 RIS）。"""

    return tuple(
        CaseInput(
            accession_no=f"{prefix}-{index:04d}",
            gold_standard_text=gold,
            image_ref=f"dx://local/{index:04d}",
            study_uid=f"STUDY-{index:04d}",
            row_no=index + 1,
        )
        for index in range(count)
    )


def _engine(tmp_path: Path, **http_kwargs: Any) -> Engine:
    """造一个挂上 run store 的引擎（退避不真睡，测试才跑得快）。

    ``http_kwargs`` 透传给出站会话层（``read_timeout`` / ``connect_timeout`` …）。
    """

    config = load_config()
    spec = load_model_spec("demo-model")
    store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
    engine = Engine(
        config,
        spec,
        client=HttpClient(sleep=lambda _seconds: None, **http_kwargs),
        runs_dir=tmp_path / "runs",
        store=store,
    )
    engine.test_store = store  # type: ignore[attr-defined]
    return engine


def _request(cases: Sequence[CaseInput], *, run_id: str) -> EvalRequest:
    return EvalRequest(
        run_id=run_id,
        model_id="demo-model",
        model_version="v1.0.0",
        dataset_ref="失败隔离验收样例",
        cases=tuple(cases),
    )


def _ledger(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "tool_calls.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _closed_port() -> int:
    """找一个确定没人监听的端口（用来制造「连不上」）。"""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------
# 1. 六类失败：各落各的错误码，各写各的可交差原因
# --------------------------------------------------------------------------


def _assert_isolated_failure(
    response: Any,
    row: Any,
    *,
    expected_code: str,
    total: int,
) -> None:
    """一条失败例的共同断言：判 failed、不进分母、原因可交差。"""

    assert row.verdict == VERDICT_FAILED, "失败例必须判 failed，绝不落成未检出"
    assert row.error_code == expected_code
    assert row.failure_reason, "失败原因必须可交差"

    # ---- 不进指标分母：四项原始计数里没有它 ----
    metrics = response.metrics
    assert metrics.tp + metrics.fp + metrics.fn + metrics.tn == total - 1
    assert metrics.failed_count == 1
    assert response.summary.failed_count == 1
    assert response.summary.evaluated_cases == total - 1


def test_connection_refused_is_inference_5xx_and_stays_out_of_the_denominator(
    tmp_path, full_env, monkeypatch
):
    """连接被拒（厂商服务没起来）→ ``INFERENCE_5XX``，该例判失败、不进分母。"""

    monkeypatch.setenv("INFERENCE_BASE_URL", f"http://127.0.0.1:{_closed_port()}")
    engine = _engine(tmp_path)
    response = engine.run(_request(_cases(1), run_id="iso-connect"))
    store = engine.test_store  # type: ignore[attr-defined]

    row = response.case_rows[0]
    _assert_isolated_failure(
        response, row, expected_code=ErrorCode.INFERENCE_5XX.value, total=1
    )
    assert "无法连接" in row.failure_reason
    assert store.get_case("iso-connect", "ACC-0000").status == CASE_STATUS_FAILED
    store.close()


def test_read_timeout_is_inference_timeout_and_says_which_stage(
    tmp_path, full_env, monkeypatch
):
    """读取超时（连上了但厂商迟迟不返回）→ ``INFERENCE_TIMEOUT``。

    失败原因必须写明是**响应阶段**超时 —— 「模型慢」与「连不上」处置不同，
    混成一句会让信息科去查网络，而问题其实在厂商服务。
    """

    stub = InferenceStub(delay_seconds=0.4)
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path, read_timeout=0.05)
        response = engine.run(_request(_cases(1), run_id="iso-timeout"))
    finally:
        stub.stop()

    row = response.case_rows[0]
    _assert_isolated_failure(
        response, row, expected_code=ErrorCode.INFERENCE_TIMEOUT.value, total=1
    )
    assert "响应阶段" in row.failure_reason
    assert "超时" in row.failure_reason


def test_client_error_keeps_the_vendor_error_code_and_field(
    tmp_path, full_env, monkeypatch
):
    """4xx（参数错）→ ``INFERENCE_4XX``，失败原因带厂商错误码与出错字段名。

    「厂商返回 HTTP 400」没人能据此去问厂商；
    「厂商错误码 INVALID_IMAGE_REF，字段 image_ref」才行。
    """

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
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(1), run_id="iso-4xx"))
    finally:
        stub.stop()

    row = response.case_rows[0]
    _assert_isolated_failure(
        response, row, expected_code=ErrorCode.INFERENCE_4XX.value, total=1
    )
    assert "HTTP 400" in row.failure_reason
    assert "INVALID_IMAGE_REF" in row.failure_reason
    assert "image_ref" in row.failure_reason
    # 4xx 不重试：替身只收到一次请求
    assert len(stub.requests) == 1


def test_server_error_is_inference_5xx_with_the_status_code(
    tmp_path, full_env, monkeypatch
):
    """5xx（厂商服务端出错）→ ``INFERENCE_5XX``，失败原因写明状态码。"""

    stub = InferenceStub(status_sequence=[503, 503, 503])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(1), run_id="iso-5xx"))
    finally:
        stub.stop()

    row = response.case_rows[0]
    _assert_isolated_failure(
        response, row, expected_code=ErrorCode.INFERENCE_5XX.value, total=1
    )
    assert "HTTP 503" in row.failure_reason
    # 5xx 退避重试三次才判失败
    assert len(stub.requests) == 3


def test_study_uid_not_echoed_is_a_mismatch_not_a_negative(
    tmp_path, full_env, monkeypatch
):
    """厂商没回传检查标识 → ``STUDY_UID_MISMATCH``，该例判失败、绝不当阴性。

    拿一份「不是本次检查的响应」当阴性，等于把一次漏检掩盖成一次正确排除。
    """

    stub = InferenceStub(echo_study_uid=False)
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(1), run_id="iso-uid"))
    finally:
        stub.stop()

    row = response.case_rows[0]
    _assert_isolated_failure(
        response, row, expected_code=ErrorCode.STUDY_UID_MISMATCH.value, total=1
    )
    assert "未回传" in row.failure_reason
    assert response.metrics.tp == 0
    assert response.metrics.tn == 0


def test_missing_response_field_is_reported_with_the_field_name(
    tmp_path, full_env, monkeypatch
):
    """响应缺字段 → ``FIELD_MAPPING_MISSING``，失败原因写明缺哪个字段。"""

    stub = InferenceStub(omit_fields=["candidates"])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(1), run_id="iso-field"))
    finally:
        stub.stop()

    row = response.case_rows[0]
    _assert_isolated_failure(
        response, row, expected_code=ErrorCode.FIELD_MAPPING_MISSING.value, total=1
    )
    assert "candidates" in row.failure_reason


# --------------------------------------------------------------------------
# 2. 单例失败不阻断整批
# --------------------------------------------------------------------------


def test_one_failing_case_does_not_stop_the_batch_or_the_run(
    tmp_path, full_env, monkeypatch
):
    """十例里坏一例：其余九例照跑，run 的最终状态**不是** failed。"""

    stub = InferenceStub(fail_study_uids=["STUDY-0003"])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(10), run_id="iso-batch"))
        store = engine.test_store  # type: ignore[attr-defined]
    finally:
        stub.stop()

    assert len(response.case_rows) == 10
    assert sum(1 for row in response.case_rows if row.verdict == VERDICT_HIT) == 9
    assert sum(1 for row in response.case_rows if row.verdict == VERDICT_FAILED) == 1

    # run 的最终状态是「已判定」，**不是**「整轮失败」——
    # 一行有问题不是这一轮的问题。
    assert store.get_run("iso-batch").status == RUN_STATUS_EVALUATED
    assert response.status == RUN_STATUS_EVALUATED

    # 指标只数那九例
    assert response.metrics.tp == 9
    assert response.metrics.failed_count == 1
    assert response.summary.evaluated_cases == 9
    store.close()


def test_failure_record_keeps_the_original_http_status(
    tmp_path, full_env, monkeypatch
):
    """失败记录带**原始 HTTP 状态** —— 「回了 503」与「回了 400」处置不同。"""

    stub = InferenceStub(status_sequence=[503, 503, 503])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(1), run_id="iso-status"))
    finally:
        stub.stop()

    artifact = (
        tmp_path
        / "runs"
        / "iso-status"
        / "cases"
        / "ACC-0000"
        / "attempt-0001"
        / "case.json"
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    failure = payload["failure"]
    assert failure["error_code"] == ErrorCode.INFERENCE_5XX.value
    assert failure["http_status"] == 503
    assert failure["endpoint"] == "inference"
    assert failure["endpoint_label"] == "推理服务"
    assert failure["counts_toward_metrics"] is False
    assert failure["failure_class"], "失败类别要留着（哪一类失败）"


def test_failed_case_is_traced_back_to_its_call(
    tmp_path, full_env, monkeypatch
):
    """失败例可反查：逐例行的调用 id 在底账里命中，底账里带结构化失败记录。"""

    stub = InferenceStub(fail_study_uids=["STUDY-0001"])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(2), run_id="iso-trace"))
    finally:
        stub.stop()

    ledger = _ledger(tmp_path / "runs" / "iso-trace")
    index = {record["tool_call_id"]: record for record in ledger}
    failed = [row for row in response.case_rows if row.verdict == VERDICT_FAILED]
    assert len(failed) == 1
    row = failed[0]

    assert row.tool_call_id in index, "失败例在调用底账里查不到"
    record = index[row.tool_call_id]
    assert record["args"]["accession_no"] == row.accession_no
    content = json.loads(record["content"])
    assert content["error_code"] == ErrorCode.INFERENCE_5XX.value
    assert content["http_status"] == 500
    assert content["counts_toward_metrics"] is False


def test_failure_reason_is_readable_and_carries_no_patient_detail(
    tmp_path, full_env, monkeypatch
):
    """失败原因要**能交差**（哪个端点 / 什么状态码 / 缺哪个字段），
    且**不含患者可识别明细**（金标准原文一个字都不许漏进去）。"""

    gold = "患者张某，男，67岁。右下肺见一磨玻璃结节，大小约 8mm。"
    stub = InferenceStub(status_sequence=[500, 500, 500])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(1, gold), run_id="iso-redact"))
    finally:
        stub.stop()

    row = response.case_rows[0]
    assert "推理服务" in row.failure_reason, "要说清是哪个端点出的事"
    assert "HTTP 500" in row.failure_reason, "要说清是什么状态码"
    # 患者可识别明细一律不进失败原因
    assert gold not in row.failure_reason
    assert "张某" not in row.failure_reason
    assert "67岁" not in row.failure_reason
    # 失败原因必须是一行（表格一格一行）
    assert "\n" not in row.failure_reason
    assert "\r" not in row.failure_reason


def test_a_failure_never_becomes_a_negative(
    tmp_path, full_env, monkeypatch
):
    """红线：金标准说没有、调用全失败 → 判 failed，**不是**真阴性。

    把一次没调成的调用算成阴性，等于把一次漏检悄悄塞进特异度里。
    """

    stub = InferenceStub(status_sequence=[500, 500, 500])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(3, NEGATIVE_GOLD), run_id="iso-never-negative"))
    finally:
        stub.stop()

    assert {row.verdict for row in response.case_rows} == {VERDICT_FAILED}
    assert response.metrics.tn == 0, "失败绝不当真阴性"
    assert response.metrics.tp == 0
    assert response.metrics.fp == 0
    assert response.metrics.fn == 0
    assert response.metrics.tp + response.metrics.fp + response.metrics.fn + response.metrics.tn == 0
    assert response.summary.evaluated_cases == 0
