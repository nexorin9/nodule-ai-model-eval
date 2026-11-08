"""替身场景断言：四类边界异常各自落到**声明好的**错误码上。

这一组测试的价值在于「**替身注入的坏情况 == 产品报出的错误码**」，
两边共用同一份场景 JSON（``tests/fixtures/scenarios/*.json``），
所以样例与期望不会各写一份、走偏。

四类边界异常与它们必须落到的错误码：

| 场景 | 注入方式 | 期望错误码 | 为什么不许更软 |
|---|---|---|---|
| 一对多绑定 | RIS 清单同一检查号两条影像 | ``AMBIGUOUS_BINDING`` | 自动取第一条 = 把别的检查算成这次 |
| 字段映射缺字段 | 候选缺 ``size_mm`` / 目录缺 ``size`` | ``FIELD_MAPPING_MISSING`` | 填 0 = 凭空造出一个尺寸 |
| study_uid 不回显 | 响应不带回请求的标识 | ``STUDY_UID_MISMATCH`` | 当阴性 = 把一次漏检藏进指标 |
| 连续 5xx | 推理端点连续 500 | ``INFERENCE_5XX`` | 当阴性 = 同上 |

**每一条都断言「该例判 failed / 挂起，不进指标分母」**——
边界异常最怕的不是报错，而是报错之后仍被算成一个正经结论。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_env

from nodule_eval.adapters.http import HttpClient
from nodule_eval.adapters.inference import InferenceAdapter
from nodule_eval.adapters.ris import RISAdapter
from nodule_eval.caseset import bind_accession, parse_ris_items
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import VERDICT_FAILED
from nodule_eval.errors import ERROR_TABLE, ErrorCode, NoduleEvalError
from scenarios import build_stub, load_scenario, scenario_names, stub_spec

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_DIR = PROJECT_ROOT / "tests" / "fixtures" / "scenarios"


def _inference_adapter(base_url: str, sleeps: list[float] | None = None) -> InferenceAdapter:
    env = make_env(INFERENCE_BASE_URL=base_url)
    config = load_config(env)
    spec = load_model_spec("demo-model")
    client = HttpClient(sleep=(sleeps.append if sleeps is not None else (lambda _s: None)))
    return InferenceAdapter(config, spec, client=client)


# --------------------------------------------------------------------------
# 场景样例本身
# --------------------------------------------------------------------------


def test_all_boundary_scenarios_have_fixtures():
    """五类边界场景各有一份样例 —— 少一份就少一条被覆盖的失败路径。"""

    names = scenario_names()
    for required in (
        "ambiguous_binding",
        "field_mapping_missing",
        "study_uid_not_echoed",
        "consecutive_5xx",
        "model_unreachable",
    ):
        assert required in names, f"缺少场景样例：{required}"


@pytest.mark.parametrize("name", sorted(p.name for p in SCENARIOS_DIR.glob("*.json")))
def test_scenario_fixture_declares_a_known_error_code(name):
    """场景样例声明的错误码必须在错误码表里，且口径标记齐全。"""

    payload = json.loads((SCENARIOS_DIR / name).read_text(encoding="utf-8"))

    assert payload.get("scenario"), "样例须声明 scenario 名"
    code = payload["expected_error_code"]
    assert code in ERROR_TABLE, f"未登记的错误码：{code}"
    assert isinstance(payload["counts_toward_metrics"], bool), "须声明是否进指标分母"
    assert payload["counts_toward_metrics"] is ERROR_TABLE[code].counts_toward_metrics, (
        f"{name} 的 counts_toward_metrics 与错误码表不一致"
    )


# --------------------------------------------------------------------------
# 场景一：一对多绑定 → AMBIGUOUS_BINDING
# --------------------------------------------------------------------------


def test_ambiguous_binding_is_refused_and_never_auto_picks_the_first():
    """同一检查号命中两条影像 —— 拒绝自动绑定、挂起人工判定。"""

    scenario = load_scenario("ambiguous_binding")
    stub = build_stub(scenario)
    stub.start()
    try:
        env = make_env(RIS_BASE_URL=stub.base_url)
        config = load_config(env)
        adapter = RISAdapter(config)
        page = adapter.fetch_page(modality="DX")
    finally:
        stub.stop()

    items = parse_ris_items({"items": list(page.items), "next_cursor": page.next_cursor})
    accession_no = scenario["accession_no"]
    assert len(items) == 2, "样例须提供两条同检查号的影像"
    assert len({item.study_uid for item in items}) == 2

    with pytest.raises(NoduleEvalError) as excinfo:
        bind_accession(items, accession_no)

    err = excinfo.value
    assert err.code is ErrorCode.AMBIGUOUS_BINDING
    assert accession_no in err.message
    assert err.counts_toward_metrics is False, "歧义例绝不能进指标分母"


def test_ris_adapter_reads_real_fields_and_paginates():
    """RIS 替身走的是真协议：URL、鉴权头、查询参数、翻页游标都要对。"""

    scenario = load_scenario("ambiguous_binding")
    spec = stub_spec(scenario)
    items = spec["items"]
    stub = build_stub({**scenario, "stub": {**spec, "page_size": 1}})
    stub.start()
    try:
        env = make_env(RIS_BASE_URL=stub.base_url)
        config = load_config(env)
        adapter = RISAdapter(config)

        first = adapter.fetch_page(modality="DX", date_from="2026-03-01", date_to="2026-03-31")
        assert first.has_more
        second = adapter.fetch_page(modality="DX", cursor=first.next_cursor)
        assert not second.has_more
    finally:
        stub.stop()

    request = stub.requests[0]
    assert request["method"] == "GET"
    assert request["path"] == "/api/v1/imaging-studies"
    assert request["headers"]["Authorization"] == "Bearer test-ris-token"
    assert request["query"]["modality"] == "DX"
    assert request["query"]["from"] == "2026-03-01"
    assert request["query"]["to"] == "2026-03-31"
    assert len(stub.requests) == 2
    assert stub.requests[1]["query"]["cursor"] == first.next_cursor


def test_ris_read_failure_is_reported_not_swallowed():
    """RIS 拉不到 —— 报 ``RIS_READ_FAILED``，绝不返回空清单当作「没有病例」。"""

    from stub_ris import RISStub

    stub = RISStub(items=[], status_sequence=[500, 500, 500])
    stub.start()
    try:
        env = make_env(RIS_BASE_URL=stub.base_url)
        config = load_config(env)
        adapter = RISAdapter(config)
        client = HttpClient(sleep=lambda _s: None)
        adapter._client = client

        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.fetch_page(modality="DX")
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.RIS_READ_FAILED
    assert excinfo.value.counts_toward_metrics is False


# --------------------------------------------------------------------------
# 场景二：字段映射缺字段 → FIELD_MAPPING_MISSING
# --------------------------------------------------------------------------


def test_missing_candidate_field_fails_with_mapping_error():
    """候选缺 ``size_mm`` —— 报 ``FIELD_MAPPING_MISSING`` 并写明缺哪个字段。"""

    scenario = load_scenario("field_mapping_missing")
    stub = build_stub(scenario)
    stub.start()
    try:
        adapter = _inference_adapter(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.detect(
                study_uid=scenario["request"]["study_uid"],
                image_ref=scenario["request"]["image_ref"],
                model_id=scenario["request"]["model_id"],
                model_version=scenario["request"]["model_version"],
            )
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "size_mm" in err.message
    assert err.counts_toward_metrics is False


def test_model_catalog_missing_mapping_also_fails_with_mapping_error():
    """模型目录缺 ``size`` 映射 —— 同样落 ``FIELD_MAPPING_MISSING``，写明缺哪个字段。"""

    scenario = load_scenario("field_mapping_missing")
    import yaml

    models_dir = PROJECT_ROOT / "tests" / "fixtures" / "models"
    assert models_dir.is_dir()

    with pytest.raises(NoduleEvalError) as excinfo:
        load_model_spec("missing-size-model", models_dir=models_dir)

    err = excinfo.value
    assert err.code is ErrorCode.FIELD_MAPPING_MISSING
    assert scenario["expected_missing_mapping_key"] in err.message
    assert err.counts_toward_metrics is False


# --------------------------------------------------------------------------
# 场景三：study_uid 不回显 → STUDY_UID_MISMATCH
# --------------------------------------------------------------------------


def test_study_uid_not_echoed_fails_and_is_not_counted_as_negative():
    """响应不回传请求的标识 —— 判失败，绝不当阴性混进指标分母。"""

    scenario = load_scenario("study_uid_not_echoed")
    stub = build_stub(scenario)
    stub.start()
    try:
        adapter = _inference_adapter(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.detect(
                study_uid=scenario["request"]["study_uid"],
                image_ref=scenario["request"]["image_ref"],
                model_id=scenario["request"]["model_id"],
                model_version=scenario["request"]["model_version"],
            )
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.STUDY_UID_MISMATCH
    assert scenario["request"]["study_uid"] in err.message
    assert err.counts_toward_metrics is False
    assert err.retryable is False, "标识对不上不是重试能解决的"


def test_mismatched_study_uid_is_also_refused():
    """回传了**另一个**检查的标识 —— 同样判失败。"""

    from stub_inference import InferenceStub

    stub = InferenceStub(study_uid_override="STUDY-OTHER-999")
    stub.start()
    try:
        adapter = _inference_adapter(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.detect(
                study_uid="STUDY-REQUESTED-001",
                image_ref=None,
                model_id="demo-model",
                model_version="v1.0.0",
            )
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.STUDY_UID_MISMATCH
    assert "STUDY-REQUESTED-001" in excinfo.value.message
    assert "STUDY-OTHER-999" in excinfo.value.message


# --------------------------------------------------------------------------
# 场景四：连续 5xx → INFERENCE_5XX
# --------------------------------------------------------------------------


def test_consecutive_5xx_fails_after_three_attempts():
    """连续 5xx —— 退避重试三次后判失败，绝不当阴性。"""

    scenario = load_scenario("consecutive_5xx")
    stub = build_stub(scenario)
    stub.start()
    try:
        sleeps: list[float] = []
        adapter = _inference_adapter(stub.base_url, sleeps)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.detect(
                study_uid=scenario["request"]["study_uid"],
                image_ref=scenario["request"]["image_ref"],
                model_id=scenario["request"]["model_id"],
                model_version=scenario["request"]["model_version"],
            )
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.INFERENCE_5XX
    assert err.http_status == 500
    assert err.counts_toward_metrics is False
    assert len(stub.requests) == scenario["expected_attempts"]
    assert sleeps == [4.0, 8.0]


def test_failed_case_is_recorded_as_failed_not_as_a_verdict(tmp_path):
    """边界异常在引擎层落的判定是 ``failed`` —— 不是四值枚举里的任何一个。"""

    from nodule_eval.contracts import (
        VERDICT_FALSE_POSITIVE,
        VERDICT_HIT,
        VERDICT_MISS,
        VERDICT_TRUE_NEGATIVE,
        parse_request,
    )
    from nodule_eval.engine import Engine

    #: 参与指标计数的四值判定（``failed`` 不在其中）。
    COUNTING_VERDICTS = (
        VERDICT_HIT,
        VERDICT_MISS,
        VERDICT_FALSE_POSITIVE,
        VERDICT_TRUE_NEGATIVE,
    )

    scenario = load_scenario("consecutive_5xx")
    stub = build_stub(scenario)
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
                "run_id": "scenario-5xx",
                "model_id": "demo-model",
                "model_version": "v1.0.0",
                "dataset_ref": "场景样例",
                "cases": [
                    {
                        "accession_no": scenario["accession_no"],
                        "gold_standard_text": "右下肺见一磨玻璃结节，大小约 8mm。",
                        "image_ref": scenario["request"]["image_ref"],
                        "study_uid": scenario["request"]["study_uid"],
                    }
                ],
            }
        )
        response = engine.run(request)
    finally:
        stub.stop()

    row = response.case_rows[0]
    assert row.verdict == VERDICT_FAILED
    assert row.verdict not in COUNTING_VERDICTS, "failed 不属于参与计数的四值判定"
    assert row.failure_reason, "失败原因列必须有可交差的中文说明"
    assert "HTTP 500" in row.failure_reason
    assert response.summary.failed_count == 1
    assert row.tool_call_id, "失败例同样要留调用记录 ID 以便反查"

    # 错误码落在调用底账里 —— 失败例同样可反查。
    ledger = (tmp_path / "runs" / "scenario-5xx" / "tool_calls.jsonl").read_text(
        encoding="utf-8"
    )
    assert "INFERENCE_5XX" in ledger
    assert row.tool_call_id in ledger


# --------------------------------------------------------------------------
# 场景五：模型不可达 → 降级路径的登记状态
# --------------------------------------------------------------------------


def test_model_unreachable_scenario_declares_the_degradation_contract():
    """模型不可达 —— 场景声明的降级口径与错误码表一致。

    落到该例行上的错误码是 ``RULE_DEGRADED``（降级仍参与指标），
    网关侧抛出的原因码是 ``MODEL_UNAVAILABLE``。
    """

    scenario = load_scenario("model_unreachable")

    assert scenario["expected_error_code"] == ErrorCode.RULE_DEGRADED.value
    assert scenario["expected_cause_error_code"] == ErrorCode.MODEL_UNAVAILABLE.value
    assert scenario["expected_fallback"] == "rule"
    assert scenario["expected_needs_review"] is True
    assert ERROR_TABLE[ErrorCode.RULE_DEGRADED.value].counts_toward_metrics is True
    assert ERROR_TABLE[ErrorCode.RULE_DEGRADED.value].retryable is False


# --------------------------------------------------------------------------
# 科研平台替身：幂等键去重
# --------------------------------------------------------------------------


def test_research_platform_stub_deduplicates_by_idempotency_key():
    """同一幂等键重复提交 —— 对端只登记一条。

    幂等键必须是**十六进制摘要**：HTTP 头只允许 ASCII，
    ``sha256(run_id|model_id|dataset_ref)`` 算出来就是 64 位十六进制串，
    可直接放进头里（任务 35 的实现照此取值）。
    """

    import hashlib

    from stub_research_platform import ResearchPlatformStub

    dataset_ref = "本地样例"
    idempotency_key = hashlib.sha256(
        f"run-1|demo-model|{dataset_ref}".encode("utf-8")
    ).hexdigest()
    assert idempotency_key.isascii()

    stub = ResearchPlatformStub()
    stub.start()
    try:
        import requests

        headers = {
            "Authorization": "Bearer test-research-token",
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
        }
        body = {"run_id": "run-1", "model_id": "demo-model", "dataset_ref": dataset_ref}
        first = requests.post(
            f"{stub.base_url}/api/v1/model-evaluation-runs", headers=headers, json=body, timeout=5
        )
        second = requests.post(
            f"{stub.base_url}/api/v1/model-evaluation-runs", headers=headers, json=body, timeout=5
        )
    finally:
        stub.stop()

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["registration_id"] == second.json()["registration_id"]
    assert len(stub.registrations) == 1, "同幂等键只登记一条"


def test_research_platform_write_failure_is_reported():
    """科研平台 5xx —— 落 ``RESEARCH_WRITE_FAILED``，不把「请求发出」当「已生效」。"""

    from stub_research_platform import ResearchPlatformStub

    scenario = load_scenario("research_write_5xx")
    stub = build_stub(scenario)
    stub.start()
    try:
        env = make_env(RESEARCH_PLATFORM_BASE_URL=stub.base_url)
        config = load_config(env)
        client = HttpClient(sleep=lambda _s: None)

        from nodule_eval.errors import NoduleEvalError

        with pytest.raises(NoduleEvalError) as excinfo:
            client.request(
                "POST",
                config.research_platform_runs_url(),
                error_mapper=lambda failure: NoduleEvalError(
                    ErrorCode.RESEARCH_WRITE_FAILED,
                    detail=failure.detail(),
                    http_status=failure.status,
                ),
                headers=config.research_platform_headers,
                json_body={"run_id": "smoke-001"},
            )
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.RESEARCH_WRITE_FAILED
    assert err.http_status == 500
    assert len(stub.requests) == 3
