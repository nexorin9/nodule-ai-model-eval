"""主干最小切片：从输入文件到一行逐例结果 + 一条调用底账。

这一组断言回答三个问题：

1. **主干真的通了吗** —— 一条命令能拿到 1 行 ``case_rows`` 与 1 条 ``tool_call``；
2. **出站真的是出站** —— 断言**实际发出**的 URL、鉴权头与请求体字段，
   而不是断言「某个 mock 被调用了」；
3. **默认路径会不会命中替身** —— 替身门必须显式开启才生效，
   产品代码里不允许出现任何 localhost 兜底。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_env
from stub_inference import InferenceStub

from nodule_eval import cli
from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import join_url, load_config, load_model_spec
from nodule_eval.contracts import CASE_ROW_COLUMNS, parse_request, parse_response
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.fixture_gate import FIXTURE_BASE_URL_ENV, resolve_base_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_FILE = PROJECT_ROOT / "tests" / "fixtures" / "cases" / "single_case.json"
SRC_DIR = PROJECT_ROOT / "src" / "nodule_eval"


def _run_cli(runs_dir: Path, cases_file: Path = CASES_FILE, extra: list[str] | None = None) -> int:
    argv = [
        "run",
        "--cases",
        str(cases_file),
        "--model-id",
        "demo-model",
        "--run-id",
        "smoke-001",
        "--dataset-ref",
        "本地样例",
        "--runs-dir",
        str(runs_dir),
        # 运行库落在临时目录里：测试**不碰**用户主目录下的默认存储位置。
        "--store",
        str(runs_dir.parent / "store" / "nodule_eval.sqlite3"),
    ]
    argv.extend(extra or [])
    return cli.main(argv)


def test_main_path_produces_one_case_row_and_one_tool_call(tmp_path, full_env, monkeypatch):
    """一条命令跑通：真外发推理端点 → 1 行 case_rows + 1 条 tool_call。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        exit_code = _run_cli(tmp_path / "runs")
    finally:
        stub.stop()

    assert exit_code == 0

    run_dir = tmp_path / "runs" / "smoke-001"
    case_rows = run_dir / "case_rows.csv"
    tool_calls = run_dir / "tool_calls.jsonl"
    result_json = run_dir / "result.json"
    assert case_rows.is_file()
    assert tool_calls.is_file()
    assert result_json.is_file()

    # ---- 逐例明细：1 行，表头与契约一致 ----
    text = case_rows.read_text(encoding="utf-8-sig")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 2, "表头 + 1 行数据"
    header = lines[0].split(",")
    assert header == list(CASE_ROW_COLUMNS)
    assert "DEMO-0001" in lines[1]

    # ---- 调用底账：1 行，字段语义与源产品一致 ----
    records = [json.loads(line) for line in tool_calls.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(records) == 1
    record = records[0]
    assert set(record) == {"tool_call_id", "name", "args", "content", "timestamp"}
    assert record["tool_call_id"].startswith("tc_")
    assert record["name"] == "demo-model.detect"
    assert record["args"]["study_uid"] == "STUDY-DEMO-001"
    assert record["args"]["model_id"] == "demo-model"
    assert record["timestamp"]

    # ---- 结果文件能被契约解析 ----
    response = parse_response(json.loads(result_json.read_text(encoding="utf-8")))
    assert response.run_id == "smoke-001"
    assert len(response.case_rows) == 1
    assert response.case_rows[0].tool_call_id == record["tool_call_id"]


def test_outbound_request_carries_real_url_auth_and_body(tmp_path, full_env, monkeypatch):
    """断言**实际发出**的 URL、鉴权头与请求体字段规范。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        _run_cli(tmp_path / "runs")
    finally:
        stub.stop()

    assert len(stub.requests) == 1
    request = stub.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/api/v1/detections"
    assert request["headers"]["Authorization"] == "Bearer test-inference-token"
    assert request["headers"]["Content-Type"] == "application/json"
    assert set(request["body"]) == {"study_uid", "image_ref", "model_id", "model_version"}
    assert request["body"]["study_uid"] == "STUDY-DEMO-001"
    assert request["body"]["model_id"] == "demo-model"
    assert request["body"]["model_version"] == "v1.0.0"


def test_failed_call_is_recorded_and_does_not_crash_the_run(tmp_path, full_env, monkeypatch):
    """调用失败 —— 该例判 failed、失败原因写明、底账照样留一行，整批不中断。"""

    stub = InferenceStub(status_sequence=[400])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        exit_code = _run_cli(tmp_path / "runs")
    finally:
        stub.stop()

    assert exit_code == 0, "单例失败不得让整批 run 崩掉"

    run_dir = tmp_path / "runs" / "smoke-001"
    text = (run_dir / "case_rows.csv").read_text(encoding="utf-8-sig")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 2
    row = lines[1].split(",")
    assert row[1] == "failed", "失败例判 failed，绝不当阴性"
    assert row[3], "失败原因列必须有内容"

    records = [
        json.loads(line)
        for line in (run_dir / "tool_calls.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1, "失败的调用同样要留底账，便于反查"
    assert "INFERENCE_4XX" in records[0]["content"]


def test_engine_survives_repeated_server_errors(tmp_path, full_env, monkeypatch):
    """连续 5xx 退避三次仍失败 —— 引擎判 failed、不中断整批、失败可反查。"""

    stub = InferenceStub(status_sequence=[500, 500, 500])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        config = load_config()
        spec = load_model_spec("demo-model")
        sleeps: list[float] = []
        engine = Engine(
            config,
            spec,
            client=HttpClient(sleep=sleeps.append),
            runs_dir=tmp_path / "runs",
        )
        request = parse_request(json.loads(CASES_FILE.read_text(encoding="utf-8")))
        response = engine.run(request)
    finally:
        stub.stop()

    assert len(stub.requests) == 3, "退避三次仍失败后停手"
    assert sleeps == [4.0, 8.0]
    assert response.case_rows[0].verdict == "failed"
    assert response.case_rows[0].failure_reason
    assert response.summary.failed_count == 1

    records = [
        json.loads(line)
        for line in (tmp_path / "runs" / "smoke-001" / "tool_calls.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert "INFERENCE_5XX" in records[0]["content"]


def test_default_path_uses_env_endpoint_and_never_a_substitute(full_env, monkeypatch):
    """默认路径（未开 --fixture）：出站目标只来自环境变量里的真实端点。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    monkeypatch.delenv("IMAGING_EVAL_FIXTURE", raising=False)
    monkeypatch.delenv(FIXTURE_BASE_URL_ENV, raising=False)
    try:
        config = load_config()
        spec = load_model_spec("demo-model")
        engine = Engine(config, spec, fixture=False)
        assert engine.adapter.url == join_url(config.inference_base_url, spec.endpoint_path)
        assert engine.adapter.url.startswith(stub.base_url)
    finally:
        stub.stop()


def test_fixture_gate_requires_explicit_base_url(monkeypatch):
    """替身门：开了开关却没给替身地址 —— 指名报错，不猜地址、不回落真实端点。"""

    monkeypatch.delenv(FIXTURE_BASE_URL_ENV, raising=False)
    with pytest.raises(NoduleEvalError) as excinfo:
        resolve_base_url("https://nodule.test-vendor.internal", fixture=True, env={})

    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert FIXTURE_BASE_URL_ENV in excinfo.value.message


def test_product_code_has_no_local_substitute():
    """静态扫描：产品代码里不得出现替身模块、localhost 或示例地址。"""

    forbidden = ("localhost", "127.0.0.1", "0.0.0.0", "example.com", "stub_inference", "mock_server")
    offenders: list[str] = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.name}:{token}")
    assert not offenders, f"产品代码里出现替身痕迹：{offenders}"


def test_product_code_never_imports_test_layer():
    """静态扫描：``src/`` 不得 import 任何测试层模块。"""

    offenders: list[str] = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import stub_", "from stub_", "import tests", "from tests")):
                offenders.append(f"{path.name}: {stripped}")
    assert not offenders, f"产品代码 import 了测试层模块：{offenders}"
