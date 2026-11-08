"""模型调用元数据底账 ``model_calls.jsonl``（任务 33）。

工具调用与模型调用是**两个端点、两个问题**：

* ``tool_calls.jsonl`` 答「**推理服务**被调了没有、回了什么」；
* ``model_calls.jsonl`` 答「**语义归并**调了哪个模型、哪个端点、耗时多久、
  用了多少 token、提示词是第几版、这一轮是不是走了降级路径」。

飞检问「这例凭什么算漏判」时，这两本账要能**接上**：
结论行 → ``tool_call_id`` → 推理调用底账 → 同一 id 下的模型调用元数据。

这一组断言覆盖五件事：

1. **字段齐备** —— 一行必填字段齐全（调了哪个模型、哪个端点、什么时候、
   多久、多少 token、提示词与返回各是哪一版、有没有走降级）；
2. **能关联** —— 模型调用的 ``tool_call_id`` 在 ``tool_calls.jsonl`` 里命中；
3. **缺关联看得见** —— 命不中的记成**异常**，不静默丢；
4. **只落哈希、不落原文** —— 提示词与模型返回里带金标准原文（可能含患者
   可识别信息），底账里只留哈希；
5. **提示词版本可反查** —— 底账里的哈希能对回 ``prompts/`` 下的具体文件版本。

外加两条工程红线：并发追加不咬合、坏行跳过但计数上报。
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from conftest import make_env
from stub_inference import InferenceStub
from stub_model import ModelStub

from nodule_eval.audit.modelcall import (
    MODEL_CALL_FIELDS,
    MODEL_CALLS_FILENAME,
    ModelCallRecorder,
    PromptRef,
    build_prompt_index,
    hash_prompt_file,
    index_model_calls,
    link_model_calls,
    missing_model_call_fields,
    new_model_call_id,
    resolve_prompt_version,
)
from nodule_eval.audit.toolcall import ToolCallRecorder
from nodule_eval.config import load_config
from nodule_eval.gateway import ModelGateway
from nodule_eval.goldstd.merge import DEFAULT_PROMPT_PATH

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = PROJECT_ROOT / "prompts"

#: 一段真实胸片金标准写法（含可能含患者可识别信息的正文）。
GOLD_TEXT = "右下肺见一磨玻璃结节，最大径约8mm。"

#: 语义归并的合法输出（模型端点回的那份 JSON）。
GOOD_PAYLOAD: dict[str, Any] = {
    "presence": True,
    "location": "右肺下叶",
    "size_mm": 8.0,
    "density": "磨玻璃",
    "evidence_span": "右下肺见一磨玻璃结节",
    "confidence": 0.9,
    "needs_human_review": False,
}


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _meta(**overrides: Any) -> dict[str, Any]:
    """造一份**网关口径**的调用元数据（字段形状与 ``ModelCallMeta`` 一致）。"""

    payload: dict[str, Any] = {
        "tool_call_id": "tc_" + "a" * 32,
        "model": "nodule-goldstd-merge",
        "base_url_host": "llm.test-hospital.internal",
        "started_at": "2026-11-08T09:30:00",
        "latency_ms": 812,
        "prompt_tokens": 128,
        "completion_tokens": 64,
        "prompt_sha256": hashlib.sha256(b"prompt").hexdigest(),
        "raw_response_sha256": hashlib.sha256(b"response").hexdigest(),
        "fallback_used": False,
        "status": "ok",
        "error_code": "",
        "attempts": 1,
        "connect_timeouts": 0,
        "read_timeouts": 0,
    }
    payload.update(overrides)
    return payload


def _record(recorder: ModelCallRecorder, **overrides: Any):
    """往底账里记一条模型调用。"""

    fields = _meta(**overrides)
    return recorder.record(
        meta=fields,
        tool_call_id=str(fields["tool_call_id"]),
        accession_no=str(overrides.get("accession_no") or "ACC-0001"),
    )


def _read_lines(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --------------------------------------------------------------------------
# 1. 字段齐备
# --------------------------------------------------------------------------


def test_model_call_row_carries_every_required_field(tmp_path):
    """一行的**必填字段**齐全 —— 少一项，事后这条账就答不出问题。"""

    recorder = ModelCallRecorder(tmp_path)
    record = _record(recorder)

    payload = record.to_dict()
    for name in MODEL_CALL_FIELDS:
        assert name in payload, f"缺字段 {name}"
        assert payload[name] not in (None, ""), f"字段 {name} 是空的"
    assert payload["model_call_id"].startswith("mc_")
    assert missing_model_call_fields(payload) == []

    # 落盘的行与返回的记录逐字一致（读回即可用）。
    rows = _read_lines(recorder.path)
    assert len(rows) == 1
    assert rows[0] == payload


def test_model_call_id_is_unique_per_call(tmp_path):
    """每次模型调用**自己的** id 唯一，且与工具调用的 id 分得开。"""

    recorder = ModelCallRecorder(tmp_path)
    first = _record(recorder)
    second = _record(recorder)

    assert first.model_call_id != second.model_call_id
    assert first.model_call_id.startswith("mc_")
    assert new_model_call_id() != new_model_call_id()


def test_missing_required_field_is_rejected(tmp_path):
    """必填字段缺任一 → **直接报错**，不写半行账。"""

    recorder = ModelCallRecorder(tmp_path)
    fields = _meta()
    del fields["model"]

    with pytest.raises(Exception) as excinfo:
        recorder.record(meta=fields, tool_call_id="tc_x")
    assert "model" in str(excinfo.value)


# --------------------------------------------------------------------------
# 2. 与工具调用底账的关联
# --------------------------------------------------------------------------


def test_model_call_links_to_the_tool_call_ledger(tmp_path):
    """模型调用的 ``tool_call_id`` **能在工具调用底账里命中** —— 三级链路一次走通。"""

    tool_recorder = ToolCallRecorder(tmp_path)
    tool_recorder.record(
        name="demo-model.detect",
        args={"accession_no": "ACC-0001"},
        content="{}",
        tool_call_id="tc_shared_0001",
    )

    model_recorder = ModelCallRecorder(tmp_path)
    _record(model_recorder, tool_call_id="tc_shared_0001")

    report = link_model_calls(
        tool_recorder.read_all(),
        model_recorder.read_all(),
    )

    assert report.linked == 1, "这一条挂上了工具调用底账"
    assert report.unlinked == [], "没有关联不上的账"
    assert report.tool_calls_without_model == []


def test_missing_link_is_recorded_as_an_anomaly(tmp_path):
    """**挂不上就是异常**：记下来、查得出来，不静默丢。"""

    model_recorder = ModelCallRecorder(tmp_path)
    _record(model_recorder, tool_call_id="tc_not_in_ledger")
    _record(model_recorder, tool_call_id="tc_also_missing")

    report = link_model_calls([], model_recorder.read_all())

    assert report.linked == 0
    assert len(report.unlinked) == 2, "两条孤儿账都进异常清单"
    reasons = {item["reason"] for item in report.unlinked}
    assert all("无处关联" in reason for reason in reasons)
    assert {item["tool_call_id"] for item in report.unlinked} == {
        "tc_not_in_ledger",
        "tc_also_missing",
    }


def test_tool_call_without_model_call_is_listed(tmp_path):
    """工具调用**没有**对应模型调用时，单列出来（正常情况：没走语义归并）。"""

    tool_recorder = ToolCallRecorder(tmp_path)
    tool_recorder.record(
        name="demo-model.detect",
        args={"accession_no": "ACC-0001"},
        content="{}",
        tool_call_id="tc_only_tool",
    )

    report = link_model_calls(tool_recorder.read_all(), [])

    assert report.linked == 0
    assert report.tool_calls_without_model == ["tc_only_tool"]


def test_index_model_calls_keeps_every_row_of_one_tool_call(tmp_path):
    """同一个 ``tool_call_id`` 下的多次模型调用**都留着**（压成一条会抹账）。"""

    recorder = ModelCallRecorder(tmp_path)
    _record(recorder, tool_call_id="tc_multi")
    _record(recorder, tool_call_id="tc_multi")

    index = index_model_calls(recorder.read_all())

    assert len(index["tc_multi"]) == 2
    assert len({item["model_call_id"] for item in index["tc_multi"]}) == 2


# --------------------------------------------------------------------------
# 3. 只落哈希、不落原文
# --------------------------------------------------------------------------


def test_ledger_keeps_hashes_not_the_raw_response(tmp_path):
    """**只落哈希、不落原文** —— 提示词与返回里带金标准原文（可能含患者信息）。"""

    raw_response = "右下肺见一磨玻璃结节，患者张某某，住院号 0012345"
    recorder = ModelCallRecorder(tmp_path)
    _record(
        recorder,
        prompt_sha256=hashlib.sha256(GOLD_TEXT.encode("utf-8")).hexdigest(),
        raw_response_sha256=hashlib.sha256(raw_response.encode("utf-8")).hexdigest(),
    )

    text = recorder.path.read_text(encoding="utf-8")

    assert "张某某" not in text, "账本里不得出现患者可识别信息"
    assert "住院号" not in text
    assert raw_response not in text
    assert GOLD_TEXT not in text, "金标准原文不落账"
    # 哈希在，够复现用。
    assert hashlib.sha256(GOLD_TEXT.encode("utf-8")).hexdigest() in text
    assert hashlib.sha256(raw_response.encode("utf-8")).hexdigest() in text


def test_failed_model_call_is_recorded_with_its_reason(tmp_path):
    """**失败的模型调用也记账** —— 「这轮为什么降级」要从账上查得出来。"""

    recorder = ModelCallRecorder(tmp_path)
    _record(
        recorder,
        status="failed",
        error_code="MODEL_UNAVAILABLE",
        raw_response_sha256="",
        attempts=3,
    )

    row = _read_lines(recorder.path)[0]
    assert row["status"] == "failed"
    assert row["error_code"] == "MODEL_UNAVAILABLE"
    assert row["attempts"] == 3
    assert row["raw_response_sha256"] == "", "失败的调用没有返回体可哈希"


# --------------------------------------------------------------------------
# 4. 提示词版本可反查
# --------------------------------------------------------------------------


def test_prompt_version_is_traceable_back_to_the_file(tmp_path):
    """底账里的提示词哈希**能反查回** ``prompts/`` 下的具体文件版本。"""

    ref = PromptRef.from_file(DEFAULT_PROMPT_PATH)
    index = build_prompt_index(PROMPTS_DIR)

    assert ref.path == "prompts/goldstd_merge.v1.md"
    assert ref.sha256 == hash_prompt_file(DEFAULT_PROMPT_PATH)
    assert resolve_prompt_version(ref.sha256, index) == "prompts/goldstd_merge.v1.md"
    assert resolve_prompt_version("0" * 64, index) == "", "查不到不猜是哪一版"
    assert resolve_prompt_version("", index) == ""


def test_prompt_ref_of_a_missing_file_is_rejected(tmp_path):
    """提示词文件读不出来 → 报错，不静默用空哈希（空哈希查不回版本）。"""

    with pytest.raises(Exception) as excinfo:
        PromptRef.from_file(tmp_path / "no-such-prompt.md")
    assert "no-such-prompt.md" in str(excinfo.value)


def test_prompt_index_requires_an_existing_directory(tmp_path):
    """提示词目录不存在 → 报错（空索引会让「反查不到」看起来像「没记录」）。"""

    with pytest.raises(Exception) as excinfo:
        build_prompt_index(tmp_path / "no-such-dir")
    assert "no-such-dir" in str(excinfo.value)


# --------------------------------------------------------------------------
# 5. 并发与坏行
# --------------------------------------------------------------------------


def test_concurrent_appends_do_not_interleave(tmp_path):
    """并发追加：行与行**不咬合**，一条不少，id 不重复。"""

    recorder = ModelCallRecorder(tmp_path)
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            for step in range(25):
                _record(
                    recorder,
                    tool_call_id=f"tc_{index}_{step}",
                    accession_no=f"ACC-{index}-{step}",
                )
        except Exception as exc:  # pragma: no cover - 失败要报出来
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    rows = _read_lines(recorder.path)
    assert len(rows) == 200, "一条不少"
    assert len({row["model_call_id"] for row in rows}) == 200, "id 不重复"
    assert len({row["tool_call_id"] for row in rows}) == 200


def test_corrupt_lines_are_skipped_and_counted(tmp_path):
    """坏行跳过但**计数上报** —— 读完悄悄少几行比坏行本身更麻烦。"""

    recorder = ModelCallRecorder(tmp_path)
    _record(recorder)
    with open(recorder.path, "a", encoding="utf-8") as handle:
        handle.write("这不是 JSON\n")
        handle.write("\n")
        handle.write(json.dumps({"model_call_id": "mc_x"}) + "\n")  # 缺必填字段

    scan = recorder.scan()

    assert len(scan.records) == 1, "好行一条不少"
    assert scan.total_lines == 4
    assert scan.corrupt_lines == 2, "坏 JSON 与缺字段各算一条"
    assert scan.blank_lines == 1


# --------------------------------------------------------------------------
# 6. 端到端：真 CLI 进程 + 真 socket 替身
# --------------------------------------------------------------------------


def test_cli_merge_model_writes_the_model_call_ledger(tmp_path, monkeypatch, capsys):
    """产品入口走一遍：``--merge model`` → 账本落盘、字段齐备、**挂得上工具底账**。"""

    from nodule_eval import cli

    inference_stub = InferenceStub()
    model_stub = ModelStub(content=json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    inference_stub.start()
    model_stub.start()
    try:
        env = make_env(
            INFERENCE_BASE_URL=inference_stub.base_url,
            OPENAI_BASE_URL=model_stub.base_url,
        )
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        cases_path = tmp_path / "cases.json"
        cases_path.write_text(
            json.dumps(
                {
                    "run_id": "modelcall-e2e",
                    "model_id": "demo-model",
                    "model_version": "v1.0.0",
                    "dataset_ref": "模型调用底账验收",
                    "cases": [
                        {
                            "accession_no": f"ACC-MC-{index:04d}",
                            "gold_standard_text": GOLD_TEXT,
                            "image_ref": f"dx://local/{index:04d}",
                            "study_uid": f"STUDY-MC-{index:04d}",
                        }
                        for index in range(3)
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        exit_code = cli.main(
            [
                "run",
                "--cases",
                str(cases_path),
                "--model-id",
                "demo-model",
                "--merge",
                "model",
                "--run-id",
                "modelcall-e2e",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--store",
                str(tmp_path / "store.sqlite3"),
            ]
        )
    finally:
        inference_stub.stop()
        model_stub.stop()

    out = capsys.readouterr().out
    assert exit_code == 0, out

    run_dir = tmp_path / "runs" / "modelcall-e2e"
    ledger_path = run_dir / MODEL_CALLS_FILENAME
    assert ledger_path.is_file(), "模型调用底账要落盘"

    rows = _read_lines(ledger_path)
    assert len(rows) == 3, "三例各一条模型调用"

    for row in rows:
        for name in MODEL_CALL_FIELDS:
            assert name in row, f"缺字段 {name}"
        assert row["model"].startswith("nodule") or row["model"]
        assert row["base_url_host"]
        assert row["latency_ms"] >= 0
        assert row["prompt_tokens"] == 128, "端点报的 token 计数原样留痕"
        assert row["completion_tokens"] == 64
        assert len(row["prompt_sha256"]) == 64
        assert len(row["raw_response_sha256"]) == 64
        assert row["fallback_used"] is False
        assert row["status"] == "ok"
        # 提示词版本可反查。
        assert row["prompt_template"] == "prompts/goldstd_merge.v1.md"
        index = build_prompt_index(PROMPTS_DIR)
        assert resolve_prompt_version(row["prompt_template_sha256"], index) == (
            "prompts/goldstd_merge.v1.md"
        )

    # **挂得上工具调用底账**：三级链路一次走通。
    tool_rows = _read_lines(run_dir / "tool_calls.jsonl")
    report = link_model_calls(tool_rows, rows)
    assert report.linked == 3
    assert report.unlinked == [], f"不该有挂不上的账：{report.unlinked}"

    # 账本里**没有**金标准原文（只落哈希）。
    text = ledger_path.read_text(encoding="utf-8")
    assert GOLD_TEXT not in text
    assert "磨玻璃结节，最大径约" not in text


def test_cli_merge_model_records_the_failed_call_too(tmp_path, monkeypatch):
    """模型端点**拒绝请求** → 归并降级，但**失败的调用照样记账**。

    只记成功的调用，账就是半本的 —— 「这轮为什么降级」查不出来。
    """

    from nodule_eval import cli

    inference_stub = InferenceStub()
    model_stub = ModelStub(status_sequence=[400])
    inference_stub.start()
    model_stub.start()
    try:
        env = make_env(
            INFERENCE_BASE_URL=inference_stub.base_url,
            OPENAI_BASE_URL=model_stub.base_url,
        )
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        cases_path = tmp_path / "cases.json"
        cases_path.write_text(
            json.dumps(
                {
                    "run_id": "modelcall-failed",
                    "model_id": "demo-model",
                    "model_version": "v1.0.0",
                    "dataset_ref": "失败的模型调用也要记账",
                    "cases": [
                        {
                            "accession_no": "ACC-FAIL-0001",
                            "gold_standard_text": GOLD_TEXT,
                            "image_ref": "dx://local/fail",
                            "study_uid": "STUDY-FAIL-0001",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        exit_code = cli.main(
            [
                "run",
                "--cases",
                str(cases_path),
                "--model-id",
                "demo-model",
                "--merge",
                "model",
                "--run-id",
                "modelcall-failed",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--store",
                str(tmp_path / "store.sqlite3"),
            ]
        )
    finally:
        inference_stub.stop()
        model_stub.stop()

    assert exit_code == 0, "降级不是失败，退出码仍是 0"

    run_dir = tmp_path / "runs" / "modelcall-failed"
    rows = _read_lines(run_dir / MODEL_CALLS_FILENAME)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "failed", "失败的调用也要记账"
    assert row["error_code"] == "MODEL_REQUEST_REJECTED"
    assert row["attempts"] == 1, "4xx 不重试"
    assert row["raw_response_sha256"] == "", "失败的调用没有返回体可哈希"
    assert row["prompt_sha256"], "提示词确实发出去了，哈希要留着"

    # 挂得上工具调用底账 —— 失败的那次也一样。
    tool_rows = _read_lines(run_dir / "tool_calls.jsonl")
    report = link_model_calls(tool_rows, rows)
    assert report.linked == 1
    assert report.unlinked == []


def test_rule_baseline_writes_no_model_call_ledger(tmp_path, monkeypatch):
    """走**规则基线**时压根没调模型 → **不记账本**（空账本比没有账本更误导）。"""

    from nodule_eval import cli

    inference_stub = InferenceStub()
    inference_stub.start()
    try:
        env = make_env(INFERENCE_BASE_URL=inference_stub.base_url)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        cases_path = tmp_path / "cases.json"
        cases_path.write_text(
            json.dumps(
                {
                    "run_id": "modelcall-none",
                    "model_id": "demo-model",
                    "model_version": "v1.0.0",
                    "dataset_ref": "无模型调用验收",
                    "cases": [
                        {
                            "accession_no": "ACC-NO-MC-0001",
                            "gold_standard_text": GOLD_TEXT,
                            "image_ref": "dx://local/none",
                            "study_uid": "STUDY-NO-MC-0001",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        exit_code = cli.main(
            [
                "run",
                "--cases",
                str(cases_path),
                "--model-id",
                "demo-model",
                "--run-id",
                "modelcall-none",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--store",
                str(tmp_path / "store.sqlite3"),
            ]
        )
    finally:
        inference_stub.stop()

    assert exit_code == 0
    assert not (tmp_path / "runs" / "modelcall-none" / MODEL_CALLS_FILENAME).exists()


def test_gateway_exposes_thread_local_meta():
    """网关的 ``last_meta`` **按线程隔离** —— 并发时不会串例。"""

    gateway = ModelGateway(load_config(make_env()))
    assert gateway.last_meta is None

    gateway.last_meta = None  # type: ignore[assignment]
    other: dict[str, Any] = {}

    def worker() -> None:
        other["seen"] = gateway.last_meta

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert other["seen"] is None, "别的线程看不到本线程的元数据"
