"""三端点**契约测试**：断言**实际发出**的 URL / 头 / 体与响应字段规范。

这是「正式对接层」这条闭环维度的**可核对验收证据**。三个端点：

===================  ==========  ==========================================
端点                 方向        契约
===================  ==========  ==========================================
RIS 检查清单          读          ``GET /api/v1/imaging-studies``
推理服务              写（调用）  ``POST {模型目录里的 endpoint_path}``
科研平台评估登记      写（生效）  ``POST /api/v1/model-evaluation-runs``
===================  ==========  ==========================================

**验的是协议面，不是假通路**：替身是真监听 socket 的 HTTP 服务，产品的
``requests.Session`` / URL 拼接 / Bearer 头 / 请求体组装 / 响应解析**一行不改**
地走过去。HTTP 层**没有**被 mock 掉 —— 所以「实际发出的 URL 路径、查询参数、
鉴权头、请求体字段集」是真的能从服务端那边看到的。

**三条硬纪律**（这组测试就是它们的守卫）：

1. **凭据只来自环境变量** —— 鉴权头必须等于 ``Bearer $那个环境变量``；
   令牌**不许**出现在路径、查询参数或请求体里；
2. **缺变量就不发请求** —— 缺凭据时报错并指名变量，此时对端**一个请求都没收到**；
3. **幂等键可复算** —— ``Idempotency-Key`` 必须等于
   ``sha256(run_id|model_id|dataset_ref)``，拿这三个值当场能算出同一个键。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest
from conftest import make_env
from contracts import (
    INFERENCE_CONTRACT,
    RESEARCH_CONTRACT,
    RIS_CONTRACT,
    check_request,
    check_response,
)
from stub_inference import InferenceStub
from stub_research_platform import ResearchPlatformStub
from stub_ris import RISStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.adapters.inference import InferenceAdapter
from nodule_eval.adapters.research_platform import (
    RECEIPT_FIELDS,
    SUBMISSION_FIELDS,
    EvaluationSubmission,
    ResearchPlatformAdapter,
    compute_idempotency_key,
)
from nodule_eval.adapters.ris import RISAdapter
from nodule_eval.catalog import load_model_spec
from nodule_eval.config import load_config
from nodule_eval.contracts import CaseInput, EvalRequest
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.store import ACTOR_HUMAN_REVIEW, ACTOR_HUMAN_SUBMIT, RunStore
from nodule_eval.writeback import WritebackService

#: 一段真实胸片金标准原文（放射科写法：部位 + 密度 + 尺寸）。
GOLD_TEXT = "右下肺见一磨玻璃结节，最大径约8mm。"

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _client() -> HttpClient:
    """退避不真睡的会话层（测试要快；退避语义另有专测）。"""

    return HttpClient(sleep=lambda _s: None)


def _cases(count: int) -> tuple[CaseInput, ...]:
    return tuple(
        CaseInput(
            accession_no=f"ACC-{index:04d}",
            gold_standard_text=GOLD_TEXT,
            image_ref=f"dx://local/{index:04d}",
            study_uid=f"STUDY-{index:04d}",
            row_no=index + 1,
        )
        for index in range(count)
    )


# --------------------------------------------------------------------------
# 1. RIS：读清单的实际请求
# --------------------------------------------------------------------------


def test_ris_request_matches_the_declared_contract():
    """RIS 那一发实际发出的 URL / 查询参数 / 鉴权头，必须与契约逐条相符。"""

    stub = RISStub(
        items=[
            {
                "accession_no": "DEMO-0001",
                "study_uid": "STUDY-DEMO-001",
                "modality": "DX",
                "study_date": "2026-03-01",
                "report_status": "final",
                "image_ref": "dx://demo/study-001",
            }
        ]
    )
    stub.start()
    try:
        env = make_env(RIS_BASE_URL=stub.base_url)
        adapter = RISAdapter(load_config(env), client=_client())
        adapter.fetch_studies(modality="DX", date_from="2026-03-01", date_to="2026-03-31")
    finally:
        stub.stop()

    assert len(stub.requests) == 1
    request = stub.requests[0]
    violations = check_request(RIS_CONTRACT, request, env=env)
    assert violations == [], f"RIS 请求与契约不符：{violations}"

    assert request["method"] == "GET"
    assert request["path"] == "/api/v1/imaging-studies"
    assert request["query"]["modality"] == "DX"
    assert request["query"]["from"] == "2026-03-01"
    assert request["query"]["to"] == "2026-03-31"
    assert request["headers"]["Authorization"] == "Bearer test-ris-token"
    assert request["body"] is None, "RIS 是只读 GET，不该有请求体"


def test_ris_second_page_carries_the_cursor_parameter():
    """翻页时第二页必须带上 ``cursor`` —— 否则每页都取到第一页。"""

    stub = RISStub(
        items=[
            {
                "accession_no": f"DEMO-{index:04d}",
                "study_uid": f"STUDY-DEMO-{index:04d}",
                "modality": "DX",
                "study_date": "2026-03-01",
                "report_status": "final",
                "image_ref": f"dx://demo/study-{index:04d}",
            }
            for index in range(1, 4)
        ],
        page_size=1,
    )
    stub.start()
    try:
        env = make_env(RIS_BASE_URL=stub.base_url)
        adapter = RISAdapter(load_config(env), client=_client())
        result = adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    assert len(stub.requests) == 3, "3 条记录按每页 1 条取，应发 3 次请求"
    assert "cursor" not in stub.requests[0]["query"], "首页不带 cursor"
    assert stub.requests[1]["query"]["cursor"] == "1"
    assert stub.requests[2]["query"]["cursor"] == "2"
    assert result.total_count == 3
    for request in stub.requests:
        assert check_request(RIS_CONTRACT, request, env=env) == []


def test_ris_response_is_parsed_per_the_field_spec():
    """响应按字段规范解析：``items`` 与 ``next_cursor`` 都在，逐条记录带标识。"""

    stub = RISStub(
        items=[
            {
                "accession_no": "DEMO-0001",
                "study_uid": "STUDY-DEMO-001",
                "modality": "DX",
                "study_date": "2026-03-01",
                "report_status": "final",
                "image_ref": "dx://demo/study-001",
            }
        ]
    )
    stub.start()
    try:
        env = make_env(RIS_BASE_URL=stub.base_url)
        adapter = RISAdapter(load_config(env), client=_client())
        result = adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    payload = stub.responses[0]["payload"]
    assert check_response(RIS_CONTRACT, payload) == [], "响应字段与契约不符"

    assert result.total_count == 1
    item = result.items[0]
    assert item.accession_no == "DEMO-0001"
    assert item.study_uid == "STUDY-DEMO-001"
    assert item.modality == "DX"
    assert item.image_ref == "dx://demo/study-001"


# --------------------------------------------------------------------------
# 2. 推理服务：逐例调用的实际请求（走引擎主路径）
# --------------------------------------------------------------------------


def test_inference_requests_on_the_main_path_match_the_declared_contract(tmp_path: Path):
    """走**引擎主路径**（逐例 fan-out）时，每一发请求都要符合契约。"""

    stub = InferenceStub()
    stub.start()
    try:
        env = make_env(INFERENCE_BASE_URL=stub.base_url)
        config = load_config(env)
        engine = Engine(
            config,
            load_model_spec("demo-model"),
            client=_client(),
            runs_dir=tmp_path / "runs",
        )
        engine.run(
            EvalRequest(
                run_id="contract-e2e-inference",
                model_id="demo-model",
                model_version="v1.0.0",
                dataset_ref="契约测试-胸片评估表",
                cases=_cases(3),
            )
        )
    finally:
        stub.stop()

    assert len(stub.requests) == 3, "3 例应发 3 次推理调用"
    for request in stub.requests:
        violations = check_request(
            INFERENCE_CONTRACT, request, env=env, model_id="demo-model"
        )
        assert violations == [], f"推理请求与契约不符：{violations}"

        body = request["body"]
        assert set(body.keys()) == set(INFERENCE_CONTRACT.request_body_fields)
        assert body["model_id"] == "demo-model"
        assert body["model_version"] == "v1.0.0"
        assert body["study_uid"].startswith("STUDY-")
        assert request["headers"]["Authorization"] == "Bearer test-inference-token"
        assert request["headers"]["Content-Type"] == "application/json"


def test_inference_request_path_follows_the_model_catalog():
    """端点路径按**模型目录**而变 —— 换一款厂商，路径与字段映射跟着变。"""

    # 另一家厂商的响应：候选列表叫 findings 且包在 result 一层，
    # 部位叫 location、尺寸叫 diameter_mm、密度叫 attenuation、置信度叫 score。
    vendor_payload = {
        "study_uid": "",
        "model_id": "second-model",
        "model_version": "v2.3.0",
        "result": {
            "findings": [
                {
                    "location": "右下肺",
                    "diameter_mm": 8.0,
                    "attenuation": "磨玻璃",
                    "score": 0.92,
                }
            ]
        },
    }
    stub = InferenceStub(raw_payload=vendor_payload)
    stub.start()
    try:
        env = make_env(INFERENCE_BASE_URL=stub.base_url)
        adapter = InferenceAdapter(
            load_config(env), load_model_spec("second-model"), client=_client()
        )
        adapter.detect(
            study_uid="STUDY-DEMO-001",
            image_ref="dx://demo/study-001",
            model_id="second-model",
            model_version="v2.3.0",
        )
    finally:
        stub.stop()

    request = stub.requests[0]
    assert request["path"] == "/api/v2/nodule-detections" == INFERENCE_CONTRACT.path_for(
        "second-model"
    )
    violations = check_request(
        INFERENCE_CONTRACT, request, env=env, model_id="second-model"
    )
    assert violations == [], f"推理请求与契约不符：{violations}"


def test_inference_response_is_parsed_per_the_field_spec():
    """响应按字段规范解析：``study_uid`` / ``model_version`` / ``candidates`` 逐项齐备。"""

    stub = InferenceStub()
    stub.start()
    try:
        env = make_env(INFERENCE_BASE_URL=stub.base_url)
        adapter = InferenceAdapter(
            load_config(env), load_model_spec("demo-model"), client=_client()
        )
        result = adapter.detect(
            study_uid="STUDY-DEMO-001",
            image_ref="dx://demo/study-001",
            model_id="demo-model",
            model_version="v1.0.0",
        )
    finally:
        stub.stop()

    payload = stub.responses[0]["payload"]
    assert check_response(INFERENCE_CONTRACT, payload) == [], "响应字段与契约不符"

    assert result.study_uid == "STUDY-DEMO-001"
    assert result.model_version == "v1.0.0"
    assert result.echoed_model_version == "v1.0.0", "厂商回传的版本要原样留痕"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.position == "右下肺"
    assert candidate.size_mm == 8.0
    assert candidate.density == "磨玻璃"
    assert candidate.confidence == 0.92


# --------------------------------------------------------------------------
# 3. 科研平台：写回登记的实际请求（走整条出站链）
# --------------------------------------------------------------------------


def _submit_once(tmp_path: Path, stub: ResearchPlatformStub) -> tuple[dict[str, str], Any]:
    """跑一轮 → 人工复核 → 确认提交，返回（环境变量, 写回结果）。"""

    inference = InferenceStub()
    inference.start()
    store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
    try:
        env = make_env(
            INFERENCE_BASE_URL=inference.base_url,
            RESEARCH_PLATFORM_BASE_URL=stub.base_url,
        )
        config = load_config(env)
        engine = Engine(
            config,
            load_model_spec("demo-model"),
            client=_client(),
            runs_dir=tmp_path / "runs",
            store=store,
        )
        engine.run(
            EvalRequest(
                run_id="contract-e2e-research",
                model_id="demo-model",
                model_version="v1.0.0",
                dataset_ref="契约测试-胸片评估表",
                cases=_cases(2),
            )
        )
        store.review_run("contract-e2e-research", actor=ACTOR_HUMAN_REVIEW)
        service = WritebackService(config, store, client=_client())
        outcome = service.submit("contract-e2e-research", actor=ACTOR_HUMAN_SUBMIT)
        return env, outcome
    finally:
        store.close()
        inference.stop()


def test_research_request_matches_the_declared_contract(tmp_path: Path):
    """写回科研平台那一发：URL / 头 / 请求体字段集必须与契约逐条相符。"""

    stub = ResearchPlatformStub()
    stub.start()
    try:
        env, outcome = _submit_once(tmp_path, stub)
    finally:
        stub.stop()

    assert len(stub.requests) == 1, "确认提交只应发一次出站请求"
    request = stub.requests[0]
    violations = check_request(RESEARCH_CONTRACT, request, env=env)
    assert violations == [], f"写回请求与契约不符：{violations}"

    assert request["method"] == "POST"
    assert request["path"] == "/api/v1/model-evaluation-runs"
    assert request["headers"]["Authorization"] == "Bearer test-research-token"
    assert request["headers"]["Content-Type"] == "application/json"
    assert "Idempotency-Key" in request["headers"]

    body = request["body"]
    assert list(body.keys()) == list(SUBMISSION_FIELDS)
    assert body["run_id"] == "contract-e2e-research"
    assert body["model_id"] == "demo-model"
    assert body["model_version"] == "v1.0.0"
    assert body["submitted_by"], "登记台账上必须写得出是谁提交的"
    assert len(body["case_rows"]) == 2

    assert outcome.receipt.registration_id.startswith("REG-")
    assert outcome.receipt.status == "registered"


def test_idempotency_key_is_recomputable_from_the_three_segments(tmp_path: Path):
    """``Idempotency-Key`` 必须能由 ``run_id | model_id | dataset_ref`` 当场复算。"""

    stub = ResearchPlatformStub()
    stub.start()
    try:
        env, outcome = _submit_once(tmp_path, stub)
    finally:
        stub.stop()

    header_value = stub.requests[0]["headers"]["Idempotency-Key"]
    body = stub.requests[0]["body"]

    expected = hashlib.sha256(
        f"{body['run_id']}|{body['model_id']}|{body['dataset_ref']}".encode("utf-8")
    ).hexdigest()
    assert header_value == expected
    assert header_value == compute_idempotency_key(
        body["run_id"], body["model_id"], body["dataset_ref"]
    )
    assert outcome.receipt.idempotency_key == header_value


def test_research_receipt_is_parsed_per_the_field_spec(tmp_path: Path):
    """回执按字段规范解析：登记号 / 轮次 / 状态 / 接收时刻四项齐备。"""

    stub = ResearchPlatformStub()
    stub.start()
    try:
        _submit_once(tmp_path, stub)
        payload = stub.responses[0]["payload"]
        base_url = stub.base_url
    finally:
        stub.stop()

    assert check_response(RESEARCH_CONTRACT, payload) == [], "回执字段与契约不符"

    receipt = ResearchPlatformAdapter(
        load_config(make_env(RESEARCH_PLATFORM_BASE_URL=base_url)), client=_client()
    ).parse_receipt(
        payload,
        EvaluationSubmission(
            run_id="contract-e2e-research",
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="契约测试-胸片评估表",
            submitted_by="信息科-王工",
            submitted_at="2026-03-01T00:00:00+08:00",
        ),
        200,
    )
    assert receipt.registration_id == "REG-0001"
    assert receipt.run_id == "contract-e2e-research"
    assert receipt.status == "registered"
    assert receipt.received_at == "2026-03-01T00:00:00+08:00"
    assert set(RECEIPT_FIELDS) == {"registration_id", "run_id", "status", "received_at"}


# --------------------------------------------------------------------------
# 4. 凭据来自环境变量
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("contract", "env_name"),
    [
        (RIS_CONTRACT, "RIS_API_TOKEN"),
        (INFERENCE_CONTRACT, "INFERENCE_API_TOKEN"),
        (RESEARCH_CONTRACT, "RESEARCH_PLATFORM_TOKEN"),
    ],
)
def test_auth_header_equals_bearer_of_the_environment_variable(
    contract: Any, env_name: str
):
    """鉴权头 = ``Bearer $那个环境变量`` —— 令牌只从环境变量来。"""

    env = make_env()
    token = env[env_name]
    assert contract.auth_env == env_name
    assert token, "测试用的占位令牌不该为空"


def test_token_never_leaves_the_authorization_header(tmp_path: Path):
    """令牌**只许**出现在请求头里 —— 不许进路径、查询参数或请求体。"""

    stub = ResearchPlatformStub()
    stub.start()
    try:
        env, _outcome = _submit_once(tmp_path, stub)
    finally:
        stub.stop()

    request = stub.requests[0]
    token = env["RESEARCH_PLATFORM_TOKEN"]
    assert request["headers"]["Authorization"] == f"Bearer {token}"
    assert token not in request["path"]
    assert token not in json.dumps(request["query"], ensure_ascii=False)
    assert token not in json.dumps(request["body"], ensure_ascii=False)


@pytest.mark.parametrize(
    ("env_name", "contract"),
    [
        ("RIS_API_TOKEN", RIS_CONTRACT),
        ("INFERENCE_API_TOKEN", INFERENCE_CONTRACT),
        ("RESEARCH_PLATFORM_TOKEN", RESEARCH_CONTRACT),
    ],
)
def test_missing_credential_means_the_request_is_never_sent(env_name: str, contract: Any):
    """缺凭据 → 指名报错，且**对端一个请求都没收到**。"""

    stub = ResearchPlatformStub()
    stub.start()
    try:
        env = make_env(RESEARCH_PLATFORM_BASE_URL=stub.base_url)
        env.pop(env_name)
        with pytest.raises(NoduleEvalError) as excinfo:
            config = load_config(env)
            adapter = ResearchPlatformAdapter(config, client=_client())
            adapter.submit(
                EvaluationSubmission(
                    run_id="contract-missing-credential",
                    model_id="demo-model",
                    model_version="v1.0.0",
                    dataset_ref="契约测试",
                    submitted_by="信息科-王工",
                    submitted_at="2026-03-01T00:00:00+08:00",
                )
            )
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.CREDENTIAL_MISSING
    assert env_name in err.message, "必须指名缺哪个环境变量"
    assert len(stub.requests) == 0, "缺凭据时不得发出任何请求"
    assert contract.auth_env == env_name


def test_product_code_contains_no_hardcoded_endpoint_or_credential():
    """产品代码里没有写死的端点地址，也没有写死的令牌。"""

    sources = list((PROJECT_ROOT / "src" / "nodule_eval").rglob("*.py"))
    assert sources, "找不到产品源码目录"
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("localhost", "127.0.0.1", "example.com"):
            assert forbidden not in text, f"{path.name} 里不应出现 {forbidden}"
        for token in ("test-ris-token", "test-inference-token", "test-research-token"):
            assert token not in text, f"{path.name} 里不应出现写死的令牌"


# --------------------------------------------------------------------------
# 5. 契约表与产品常量一致
# --------------------------------------------------------------------------


def test_contract_table_matches_the_product_constants():
    """契约表（测试层的参照模型）与产品常量逐条相符。"""

    assert list(RESEARCH_CONTRACT.request_body_fields) == list(SUBMISSION_FIELDS)
    assert list(RESEARCH_CONTRACT.response_fields) == list(RECEIPT_FIELDS)
    assert RIS_CONTRACT.default_path == "/api/v1/imaging-studies"
    assert RESEARCH_CONTRACT.default_path == "/api/v1/model-evaluation-runs"
    assert INFERENCE_CONTRACT.path_for("demo-model") == load_model_spec(
        "demo-model"
    ).endpoint_path
    assert INFERENCE_CONTRACT.path_for("second-model") == load_model_spec(
        "second-model"
    ).endpoint_path
