"""三入口共用 JSON 契约的校验测试。"""

import json

import pytest

from nodule_eval.contracts import (
    DEFAULT_CONCURRENCY,
    DEFAULT_SIZE_TOLERANCE_MM,
    VERDICTS,
    CaseInput,
    CaseRow,
    EvalRequest,
    EvalResponse,
    RunOptions,
    parse_request,
    parse_response,
)
from nodule_eval.errors import ErrorCode, NoduleEvalError


def _valid_payload():
    return {
        "run_id": "run-2026-1107-001",
        "model_id": "fixture-model",
        "model_version": "1.4.2",
        "dataset_ref": "jsrt-247",
        "cases": [
            {
                "accession_no": "ACC-0001",
                "gold_standard_text": "右下肺见磨玻璃结节，约 8mm。",
                "image_ref": "study://uid-0001",
            },
            {
                "accession_no": "ACC-0002",
                "gold_standard_text": "双肺未见明显结节影。",
            },
        ],
        "options": {"size_tolerance_mm": 2.0, "concurrency": 4, "resume": False},
    }


def test_parse_request_accepts_valid_payload():
    request = parse_request(_valid_payload())

    assert request.run_id == "run-2026-1107-001"
    assert request.model_id == "fixture-model"
    assert request.model_version == "1.4.2"
    assert request.dataset_ref == "jsrt-247"
    assert len(request.cases) == 2
    assert request.cases[0].accession_no == "ACC-0001"
    assert request.cases[0].image_ref == "study://uid-0001"
    assert request.cases[1].image_ref is None
    assert request.options.size_tolerance_mm == DEFAULT_SIZE_TOLERANCE_MM
    assert request.options.concurrency == DEFAULT_CONCURRENCY
    assert request.options.resume is False


def test_request_round_trips_through_json():
    """宏写出的 req.json → 引擎读入 → 再写出，内容必须一致（三入口共用同一契约）。"""

    original = parse_request(_valid_payload())
    again = parse_request(json.loads(json.dumps(original.to_dict())))

    assert again == original


@pytest.mark.parametrize("missing_key", ["run_id", "model_id", "model_version", "dataset_ref"])
def test_missing_top_level_field_names_the_field(missing_key):
    payload = _valid_payload()
    del payload[missing_key]

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_request(payload)

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert missing_key in excinfo.value.message


@pytest.mark.parametrize("blank_key", ["run_id", "model_id", "model_version", "dataset_ref"])
def test_blank_top_level_field_is_rejected(blank_key):
    payload = _valid_payload()
    payload[blank_key] = "   "

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_request(payload)

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert blank_key in excinfo.value.message


def test_missing_accession_no_names_the_field_and_row():
    payload = _valid_payload()
    del payload["cases"][1]["accession_no"]

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_request(payload)

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "accession_no" in excinfo.value.message
    assert "第 2 行" in excinfo.value.message


def test_empty_accession_no_is_rejected_not_silently_dropped():
    payload = _valid_payload()
    payload["cases"][0]["accession_no"] = ""

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_request(payload)

    assert "accession_no" in excinfo.value.message
    assert "不得为空" in excinfo.value.message


def test_missing_gold_standard_text_is_rejected():
    payload = _valid_payload()
    del payload["cases"][0]["gold_standard_text"]

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_request(payload)

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "gold_standard_text" in excinfo.value.message


def test_blank_gold_standard_text_is_rejected():
    payload = _valid_payload()
    payload["cases"][0]["gold_standard_text"] = "   \n  "

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_request(payload)

    assert "gold_standard_text" in excinfo.value.message


def test_gold_standard_text_is_stored_verbatim():
    """金标准原文永不被改写：换行、全角标点、多余空格都原样保留。"""

    raw = "双肺未见明显结节影。\n右下肺见磨玻璃结节，约 8mm，边界欠清。"
    payload = _valid_payload()
    payload["cases"][0]["gold_standard_text"] = raw

    request = parse_request(payload)

    assert request.cases[0].gold_standard_text == raw


def test_empty_cases_array_is_rejected():
    payload = _valid_payload()
    payload["cases"] = []

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_request(payload)

    assert "cases" in excinfo.value.message


def test_cases_must_be_array():
    payload = _valid_payload()
    payload["cases"] = {"accession_no": "ACC-0001"}

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_request(payload)

    assert "cases" in excinfo.value.message


def test_options_default_when_absent():
    payload = _valid_payload()
    del payload["options"]

    request = parse_request(payload)

    assert request.options == RunOptions(
        size_tolerance_mm=DEFAULT_SIZE_TOLERANCE_MM,
        concurrency=DEFAULT_CONCURRENCY,
        resume=False,
    )


@pytest.mark.parametrize(
    "options",
    [
        {"size_tolerance_mm": -1},
        {"size_tolerance_mm": "2mm"},
        {"concurrency": 0},
        {"concurrency": "4"},
        {"concurrency": True},
        {"resume": "yes"},
    ],
)
def test_invalid_options_are_rejected(options):
    payload = _valid_payload()
    payload["options"] = options

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_request(payload)

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "options" in excinfo.value.message


def test_row_no_defaults_to_one_based_row_index():
    request = parse_request(_valid_payload())

    assert request.cases[0].row_no == 1
    assert request.cases[1].row_no == 2


def test_parse_response_round_trip():
    response = EvalResponse(
        run_id="run-2026-1107-001",
        status="evaluated",
        case_rows=(
            CaseRow(
                accession_no="ACC-0001",
                verdict="hit",
                model_findings="右肺下叶磨玻璃结节 8mm",
                failure_reason="",
                tool_call_id="tc-0001",
                needs_review=False,
                normalize_source="model",
                row_no=1,
            ),
        ),
    )
    payload = json.loads(json.dumps(response.to_dict()))

    parsed = parse_response(payload)

    assert parsed.run_id == "run-2026-1107-001"
    assert parsed.status == "evaluated"
    assert parsed.case_rows[0].verdict == "hit"
    assert parsed.case_rows[0].tool_call_id == "tc-0001"


def test_parse_response_rejects_unknown_verdict():
    payload = {
        "run_id": "run-x",
        "status": "evaluated",
        "case_rows": [{"accession_no": "ACC-0001", "verdict": "maybe"}],
    }

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_response(payload)

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "verdict" in excinfo.value.message


def test_parse_response_rejects_unknown_normalize_source():
    payload = {
        "run_id": "run-x",
        "status": "evaluated",
        "case_rows": [
            {
                "accession_no": "ACC-0001",
                "verdict": "hit",
                "normalize_source": "magic",
            }
        ],
    }

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_response(payload)

    assert "normalize_source" in excinfo.value.message


def test_verdict_enum_is_five_mutually_exclusive_values():
    assert VERDICTS == ("hit", "miss", "false_positive", "true_negative", "failed")
    assert len(set(VERDICTS)) == 5


def test_case_input_is_immutable_snapshot():
    case = CaseInput(accession_no="ACC-0001", gold_standard_text="原文")
    with pytest.raises(Exception):
        case.accession_no = "ACC-9999"  # type: ignore[misc]


def test_eval_request_is_hashable_value_object():
    request = parse_request(_valid_payload())
    assert isinstance(request, EvalRequest)
    assert request == parse_request(_valid_payload())
