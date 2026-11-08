"""契约**回放**：拿录下来的请求体与响应样本，验解析器对字段缺失的反应。

黄金样本在 ``tests/fixtures/contracts/*.json``，由 ``tests/record_contracts.py``
**真跑一遍协议面**录下来（不是手写的）。这里拿它们做两件事：

1. **漂移检测** —— 录下来的请求是否仍与契约相符。适配器改了 URL、换了头、
   改了请求体字段集，这儿立刻红。
2. **缺字段的显式性** —— 把样本里的**必填字段逐个抠掉**，再喂回解析器：
   必须抛**指名错误**（说清缺哪个字段），**不许**静默补一个默认值。

第 2 条是这一组测试最要紧的地方。**静默默认**在评估场景里会直接扭曲指标：

- ``next_cursor`` 缺 → 当成「没有下一页」→ 后面几页的病例**静默丢掉**；
- ``candidates`` 缺 → 当成「未检出」→ 一次失败被记成一次**正确排除**；
- ``registration_id`` 缺 → 当成「已登记」→ 登记台账上多出一条查无此轮的记录。

「能跑通」不算数，**缺了字段要说得出来**才算数。
"""

from __future__ import annotations

import copy
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

from nodule_eval.adapters.http import HttpClient
from nodule_eval.adapters.inference import InferenceAdapter
from nodule_eval.adapters.research_platform import (
    EvaluationSubmission,
    ResearchPlatformAdapter,
)
from nodule_eval.adapters.ris import RISAdapter
from nodule_eval.catalog import load_model_spec
from nodule_eval.config import load_config
from nodule_eval.errors import ErrorCode, NoduleEvalError

#: 黄金样本目录。
CONTRACTS_DIR = Path(__file__).resolve().parent / "fixtures" / "contracts"

#: 录制时用的轮次号（回放时提交体要与之对齐）。
RECORDED_RUN_ID = "contract-sample-001"


def _load(name: str) -> dict[str, Any]:
    """读一份黄金样本。"""

    path = CONTRACTS_DIR / f"{name}.json"
    assert path.is_file(), f"黄金样本不存在：{path}（跑 python tests/record_contracts.py 录制）"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, Mapping)
    return dict(payload)


def _subst_auth(request: Mapping[str, Any], contract: Any, env: Mapping[str, str]) -> dict:
    """把黄金样本里的鉴权占位换成环境变量的实际值，再拿去核契约。"""

    out = dict(request)
    headers = dict(out.get("headers") or {})
    headers["Authorization"] = f"Bearer {env[contract.auth_env]}"
    out["headers"] = headers
    return out


def _env() -> dict[str, str]:
    """一份齐备的环境变量（占位值，无真实凭据）。"""

    return make_env()


# --------------------------------------------------------------------------
# 1. 漂移检测：录下来的请求仍与契约相符
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "contract"),
    [
        ("ris", RIS_CONTRACT),
        ("inference", INFERENCE_CONTRACT),
        ("research_platform", RESEARCH_CONTRACT),
    ],
)
def test_recorded_request_still_matches_the_contract(name: str, contract: Any):
    """录下来的请求与契约逐条相符 —— 适配器改了协议，这儿就红。"""

    sample = _load(name)
    env = _env()
    assert sample["requests"], f"{name} 的黄金样本里没有请求"

    for request in sample["requests"]:
        probe = _subst_auth(request, contract, env)
        violations = check_request(
            contract, probe, env=env, model_id=str(sample.get("model_id") or "")
        )
        assert violations == [], f"{name} 的录样本与契约不符：{violations}"


@pytest.mark.parametrize(
    ("name", "contract"),
    [
        ("ris", RIS_CONTRACT),
        ("inference", INFERENCE_CONTRACT),
        ("research_platform", RESEARCH_CONTRACT),
    ],
)
def test_recorded_response_carries_every_contract_field(name: str, contract: Any):
    """录下来的响应字段齐备（这是回放的前提）。"""

    sample = _load(name)
    assert sample["responses"], f"{name} 的黄金样本里没有响应"
    for response in sample["responses"]:
        assert response["status"] == 200
        assert check_response(contract, response["payload"]) == []


def test_recorded_authorization_is_stored_as_a_placeholder():
    """录下来的鉴权头是占位写法 —— **真实令牌不落盘**。"""

    for name, contract in (
        ("ris", RIS_CONTRACT),
        ("inference", INFERENCE_CONTRACT),
        ("research_platform", RESEARCH_CONTRACT),
    ):
        sample = _load(name)
        for request in sample["requests"]:
            assert request["headers"]["Authorization"] == contract.auth_marker()


# --------------------------------------------------------------------------
# 2. 回放：把录下来的响应喂回解析器
# --------------------------------------------------------------------------


def _ris_adapter() -> RISAdapter:
    return RISAdapter(
        load_config(make_env(RESEARCH_PLATFORM_BASE_URL="https://research.invalid")),
        client=HttpClient(sleep=lambda _s: None),
    )


def _inference_adapter() -> InferenceAdapter:
    return InferenceAdapter(
        load_config(make_env()),
        load_model_spec("demo-model"),
        client=HttpClient(sleep=lambda _s: None),
    )


def _research_adapter() -> ResearchPlatformAdapter:
    return ResearchPlatformAdapter(
        load_config(make_env()),
        client=HttpClient(sleep=lambda _s: None),
    )


def _submission() -> EvaluationSubmission:
    return EvaluationSubmission(
        run_id=RECORDED_RUN_ID,
        model_id="demo-model",
        model_version="v1.0.0",
        dataset_ref="contract-sample-table.xlsx",
        submitted_by="信息科-王工",
        submitted_at="2026-03-01T00:00:00+08:00",
    )


def test_replaying_the_recorded_ris_response_yields_the_parsed_items():
    """回放 RIS 响应 → 逐条记录与游标按字段规范解析出来。"""

    payload = _load("ris")["responses"][0]["payload"]
    page = _ris_adapter().parse_response(payload)

    assert page.next_cursor == "1"
    assert len(page.parsed) == 1
    assert page.parsed[0].accession_no == "DEMO-0001"
    assert page.parsed[0].study_uid == "STUDY-DEMO-001"
    assert page.parsed[0].modality == "DX"


def test_replaying_the_recorded_inference_response_yields_the_candidates():
    """回放推理响应 → 候选逐字段解析出来（部位 / 尺寸 / 密度 / 置信度）。"""

    payload = _load("inference")["responses"][0]["payload"]
    result = _inference_adapter().parse_response(payload, "STUDY-DEMO-001")

    assert result.study_uid == "STUDY-DEMO-001"
    assert result.echoed_model_id == "demo-model"
    assert result.echoed_model_version == "v1.0.0"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.position == "右下肺"
    assert candidate.size_mm == 8.0
    assert candidate.density == "磨玻璃"
    assert candidate.confidence == 0.92


def test_replaying_the_recorded_receipt_yields_the_registration():
    """回放登记回执 → 登记号 / 轮次 / 状态 / 接收时刻齐备。"""

    payload = _load("research_platform")["responses"][0]["payload"]
    receipt = _research_adapter().parse_receipt(payload, _submission(), 200)

    assert receipt.registration_id == "REG-0001"
    assert receipt.run_id == RECORDED_RUN_ID
    assert receipt.status == "registered"
    assert receipt.received_at == "2026-03-01T00:00:00+08:00"
    assert receipt.idempotency_key == _submission().idempotency_key()


# --------------------------------------------------------------------------
# 3. 缺字段必须给出**指名**错误，而不是静默默认
# --------------------------------------------------------------------------


def _drop(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    """深拷贝一份响应并抠掉一个字段。"""

    clone = copy.deepcopy(dict(payload))
    clone.pop(field, None)
    return clone


def test_missing_next_cursor_is_refused_instead_of_ending_the_paging():
    """缺 ``next_cursor`` → 判失败，**不当「没有下一页」**。

    当成没有下一页，后面几页的病例会被静默丢掉 ——
    清单少几条 nobody 会察觉，尤其当少的恰好是漏诊例时。
    """

    payload = _load("ris")["responses"][0]["payload"]
    with pytest.raises(NoduleEvalError) as excinfo:
        _ris_adapter().parse_response(_drop(payload, "next_cursor"))

    err = excinfo.value
    assert err.code is ErrorCode.RIS_READ_FAILED
    assert "next_cursor" in err.message


def test_missing_items_is_refused_instead_of_returning_an_empty_list():
    """缺 ``items`` → 判失败，**不当「这一批没有病例」**。"""

    payload = _load("ris")["responses"][0]["payload"]
    with pytest.raises(NoduleEvalError) as excinfo:
        _ris_adapter().parse_response(_drop(payload, "items"))

    err = excinfo.value
    assert err.code is ErrorCode.RIS_READ_FAILED
    assert "items" in err.message


@pytest.mark.parametrize("field", ["accession_no", "study_uid"])
def test_missing_required_item_field_is_named(field: str):
    """逐条记录缺必填字段 → 指名报错（丢一条 = 凭空少一例样本）。"""

    payload = copy.deepcopy(_load("ris")["responses"][0]["payload"])
    payload["items"][0].pop(field, None)
    with pytest.raises(NoduleEvalError) as excinfo:
        _ris_adapter().parse_response(payload)

    err = excinfo.value
    assert err.code is ErrorCode.RIS_READ_FAILED
    assert field in err.message


def test_missing_study_uid_in_the_inference_response_is_refused():
    """缺 ``study_uid`` → 判失败（这份响应根本不是本次检查的结果）。"""

    payload = _load("inference")["responses"][0]["payload"]
    with pytest.raises(NoduleEvalError) as excinfo:
        _inference_adapter().parse_response(_drop(payload, "study_uid"), "STUDY-DEMO-001")

    err = excinfo.value
    assert err.code is ErrorCode.STUDY_UID_MISMATCH


def test_missing_candidates_is_refused_instead_of_counting_as_no_detection():
    """缺 ``candidates`` → 判失败，**不当「未检出」**。

    当成未检出，一次失败就被记成一次正确排除，特异度直接被抬高。
    """

    payload = _load("inference")["responses"][0]["payload"]
    with pytest.raises(NoduleEvalError) as excinfo:
        _inference_adapter().parse_response(_drop(payload, "candidates"), "STUDY-DEMO-001")

    err = excinfo.value
    assert err.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "candidates" in err.message


def test_null_candidates_is_refused_instead_of_counting_as_no_detection():
    """``candidates`` 为 null → 判失败，**不当「未检出」**。"""

    payload = copy.deepcopy(_load("inference")["responses"][0]["payload"])
    payload["candidates"] = None
    with pytest.raises(NoduleEvalError) as excinfo:
        _inference_adapter().parse_response(payload, "STUDY-DEMO-001")

    err = excinfo.value
    assert err.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "candidates" in err.message


@pytest.mark.parametrize("field", ["position", "size_mm", "density", "confidence"])
def test_missing_candidate_field_is_named(field: str):
    """候选缺字段 → 指名报错（缺哪个字段、第几个候选）。"""

    payload = copy.deepcopy(_load("inference")["responses"][0]["payload"])
    payload["candidates"][0].pop(field, None)
    with pytest.raises(NoduleEvalError) as excinfo:
        _inference_adapter().parse_response(payload, "STUDY-DEMO-001")

    err = excinfo.value
    assert err.code is ErrorCode.FIELD_MAPPING_MISSING
    assert field in err.message
    assert "第 1 个候选" in err.message


def test_missing_echoed_model_version_is_not_fabricated():
    """厂商没回传 ``model_version`` → 回显留空，**不伪造**厂商没说的话。"""

    payload = _drop(_load("inference")["responses"][0]["payload"], "model_version")
    result = _inference_adapter().parse_response(payload, "STUDY-DEMO-001")

    assert result.echoed_model_version == "", "厂商没回传的版本不许补一个"
    assert "model_version" not in result.raw, "原始响应照原样留着，可回查"


@pytest.mark.parametrize("field", ["registration_id", "run_id", "status", "received_at"])
def test_missing_receipt_field_is_named(field: str):
    """回执缺字段 → 指名报错（拿不到登记号就不算已登记）。"""

    payload = _load("research_platform")["responses"][0]["payload"]
    with pytest.raises(NoduleEvalError) as excinfo:
        _research_adapter().parse_receipt(_drop(payload, field), _submission(), 200)

    err = excinfo.value
    assert err.code is ErrorCode.RESEARCH_WRITE_FAILED
    assert field in err.message


def test_receipt_for_another_run_is_refused():
    """回执登记的是另一轮 → 判失败（比「没登记」更难查）。"""

    payload = copy.deepcopy(_load("research_platform")["responses"][0]["payload"])
    payload["run_id"] = "contract-sample-999"
    with pytest.raises(NoduleEvalError) as excinfo:
        _research_adapter().parse_receipt(payload, _submission(), 200)

    err = excinfo.value
    assert err.code is ErrorCode.RESEARCH_WRITE_FAILED
    assert "contract-sample-999" in err.message
