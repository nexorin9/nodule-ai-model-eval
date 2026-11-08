"""产物脱敏扫描：产物里不该有患者可识别明细（任务 34）。

这一组断言回答五个问题 —— 也就是信息科做等保自查、飞检翻产物时，
真会翻的那五个问题：

1. **正常产物是不是真的干净** —— 真跑一轮（真 socket 替身 + 真
   ``requests.Session``），扫一遍产物目录，**零命中**。扫描器要是不敢扫
   自己写的产物，这道闸门就没有意义；
2. **塞了明细能不能查出来** —— 姓名 / 身份证 / 住院号 / 手机号，
   四类各塞一处，**一处都不能漏**，且要报出**文件名与命中位置**；
3. **会不会误伤** —— 业务检查号与内容哈希**不许**被当成明细
   （院内检查号常是纯数字，撞上手机号形状）；
4. **只报不改** —— 扫完一遍，产物**一个字都不能变**（底账必须原样）；
5. **后来加的字段登记了吗** —— 产物里出现**未登记**的字段即报错，
   这是「加字段忘了登记，明细跟着出去」的闸门。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from stub_inference import InferenceStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.audit.redact import (
    ARTIFACT_FIELDS_FILENAME,
    KIND_ADMISSION_NO,
    KIND_ID_CARD,
    KIND_PATIENT_NAME,
    KIND_PHONE,
    KIND_UNREGISTERED_FIELD,
    assert_artifacts_clean,
    detect_pii,
    load_artifact_fields,
    mask_payload,
    mask_pii,
    register_artifact_fields,
    render_scan_report,
    scan_run_artifacts,
)
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import CaseInput, EvalRequest
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.goldstd.schema import GoldLabels
from nodule_eval.store import RunStore

#: 金标准样例（不含明细 —— 干净产物该长这样）。
GOLD_POSITIVE = "右下肺见一磨玻璃结节，大小约 8mm。"
GOLD_NEGATIVE = "双肺未见明显结节影。"

#: 各类可识别明细的样例（**脱敏用**：这些是形状样例，不是真实数据）。
SAMPLE_ID_CARD = "110101199003071234"
SAMPLE_ADMISSION_NO = "ZY2026001234"
SAMPLE_PHONE = "13812345678"
SAMPLE_NAME = "张三"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _engine(tmp_path: Path) -> Engine:
    config = load_config()
    spec = load_model_spec("demo-model")
    store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
    engine = Engine(
        config,
        spec,
        client=HttpClient(sleep=lambda _seconds: None),
        runs_dir=tmp_path / "runs",
        store=store,
    )
    engine.test_store = store  # type: ignore[attr-defined]
    return engine


def _run_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str = "redact-e2e",
    gold: str = GOLD_POSITIVE,
) -> Path:
    """真跑一轮，返回产物目录。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        engine.run(
            EvalRequest(
                run_id=run_id,
                model_id="demo-model",
                model_version="v1.0.0",
                dataset_ref="脱敏扫描验收样例",
                cases=(
                    CaseInput(
                        accession_no="ACC-0001",
                        gold_standard_text=gold,
                        image_ref="dx://local/0001",
                        study_uid="STUDY-0001",
                        row_no=1,
                    ),
                    CaseInput(
                        accession_no="ACC-0002",
                        gold_standard_text=GOLD_NEGATIVE,
                        image_ref="dx://local/0002",
                        study_uid="STUDY-0002",
                        row_no=2,
                    ),
                ),
            )
        )
        store = engine.test_store  # type: ignore[attr-defined]
    finally:
        stub.stop()
    store.close()
    return tmp_path / "runs" / run_id


def _write(run_dir: Path, name: str, body: str) -> Path:
    path = run_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 1. 正常产物：零命中
# --------------------------------------------------------------------------


def test_real_run_artifacts_are_clean(tmp_path, full_env, monkeypatch):
    """真跑一轮，扫一遍：零命中。"""

    run_dir = _run_once(tmp_path, monkeypatch)
    report = scan_run_artifacts(run_dir)

    assert report.scanned_files, "产物目录里一份产物都没有，扫描就是空的"
    assert report.findings == (), f"正常产物不该有命中：{report.to_dict()}"
    assert report.ok
    assert report.counts() == {}


def test_scan_covers_all_artifact_kinds(tmp_path, full_env, monkeypatch):
    """逐例明细、结论、底账、模型底账、逐例产物都在扫描范围内。"""

    run_dir = _run_once(tmp_path, monkeypatch)
    names = {Path(name).name for name in report_names(run_dir)}

    assert "case_rows.csv" in names
    assert "summary.json" in names
    assert "tool_calls.jsonl" in names
    assert "case.json" in names


def report_names(run_dir: Path) -> list[str]:
    return [path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file()]


# --------------------------------------------------------------------------
# 2. 塞了明细能查出来
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (f"结论：姓名：{SAMPLE_NAME}，右下肺见结节。", KIND_PATIENT_NAME),
        (f"结论：身份证号：{SAMPLE_ID_CARD}，右下肺见结节。", KIND_ID_CARD),
        (f"结论：住院号：{SAMPLE_ADMISSION_NO}，右下肺见结节。", KIND_ADMISSION_NO),
        (f"结论：联系电话 {SAMPLE_PHONE}，右下肺见结节。", KIND_PHONE),
    ],
)
def test_detects_pii_in_text_artifact(tmp_path, body, kind):
    """四类明细各塞一处：一处都不能漏。"""

    run_dir = tmp_path / "runs" / "dirty"
    run_dir.mkdir(parents=True)
    _write(run_dir, "summary.json", json.dumps({"run_id": "dirty", "note": body}))

    report = scan_run_artifacts(run_dir)
    kinds = {finding.kind for finding in report.findings}

    assert kind in kinds, f"没检出 {kind}：{report.to_dict()}"
    assert not report.ok


def test_detects_patient_name_field(tmp_path):
    """结构化产物里的姓名字段（``patient_name``）也要检出。"""

    run_dir = tmp_path / "runs" / "dirty"
    run_dir.mkdir(parents=True)
    _write(
        run_dir,
        "case.json",
        json.dumps({"accession_no": "ACC-0001", "patient_name": SAMPLE_NAME}),
    )

    report = scan_run_artifacts(run_dir)
    findings = [f for f in report.findings if f.kind == KIND_PATIENT_NAME]

    assert findings, f"没检出姓名字段：{report.to_dict()}"
    assert "patient_name" in findings[0].where


def test_detects_name_inside_vendor_content(tmp_path):
    """厂商返回里回显了姓名 —— 那是别人的返回，但照样要扫出来。"""

    run_dir = tmp_path / "runs" / "dirty"
    run_dir.mkdir(parents=True)
    content = json.dumps({"patient_name": SAMPLE_NAME}, ensure_ascii=False)
    _write(
        run_dir,
        "tool_calls.jsonl",
        json.dumps(
            {
                "tool_call_id": "tc_00000000000000000000000000000000",
                "name": "demo-model.detect",
                "args": {"accession_no": "ACC-0001"},
                "content": content,
                "timestamp": "2026-11-08T10:00:00",
            },
            ensure_ascii=False,
        )
        + "\n",
    )

    report = scan_run_artifacts(run_dir)
    kinds = {finding.kind for finding in report.findings}

    assert KIND_PATIENT_NAME in kinds, f"厂商回显的姓名没扫出来：{report.to_dict()}"


def test_finding_reports_file_and_location(tmp_path):
    """命中要报出**文件名与命中位置** —— 不知道在哪儿就没法处理。"""

    run_dir = tmp_path / "runs" / "dirty"
    run_dir.mkdir(parents=True)
    _write(run_dir, "missed_list.csv", "accession_no,missed_reason\nACC-0001,身份证号：" + SAMPLE_ID_CARD + "\n")

    report = scan_run_artifacts(run_dir)
    findings = [f for f in report.findings if f.kind == KIND_ID_CARD]

    assert findings
    finding = findings[0]
    assert finding.file == "missed_list.csv"
    assert "第 2 行" in finding.where
    assert "missed_reason" in finding.where


def test_finding_excerpt_does_not_leak_the_detail(tmp_path):
    """扫描报告本身也是要往外发的东西 —— 报告里不能再带一遍明细。"""

    run_dir = tmp_path / "runs" / "dirty"
    run_dir.mkdir(parents=True)
    _write(run_dir, "summary.json", json.dumps({"note": f"身份证号：{SAMPLE_ID_CARD}"}))

    report = scan_run_artifacts(run_dir)
    blob = json.dumps(report.to_dict(), ensure_ascii=False)

    assert SAMPLE_ID_CARD not in blob
    assert "已脱敏" in blob


# --------------------------------------------------------------------------
# 3. 不误伤：检查号与哈希
# --------------------------------------------------------------------------


def test_accession_no_is_not_flagged_even_when_numeric(tmp_path):
    """院内检查号常是纯数字 —— 撞上手机号形状也不许误报。"""

    run_dir = tmp_path / "runs" / "clean"
    run_dir.mkdir(parents=True)
    _write(
        run_dir,
        "case_rows.csv",
        "accession_no,verdict\n" + SAMPLE_PHONE + ",hit\n",
    )

    report = scan_run_artifacts(run_dir)

    assert report.findings == (), f"检查号被误报了：{report.to_dict()}"


def test_content_hash_is_not_flagged(tmp_path):
    """内容哈希不是明细。"""

    digest = "a" * 64
    run_dir = tmp_path / "runs" / "clean"
    run_dir.mkdir(parents=True)
    _write(
        run_dir,
        "cases/ACC-0001/attempt-0001/case.json",
        json.dumps({"accession_no": "ACC-0001", "gold_standard_sha256": digest}),
    )

    report = scan_run_artifacts(run_dir)

    assert report.findings == (), f"哈希被误报了：{report.to_dict()}"


def test_numeric_accession_no_in_structured_payload_is_not_flagged(tmp_path):
    """结构化载荷里的检查号同样走白名单。"""

    run_dir = tmp_path / "runs" / "clean"
    run_dir.mkdir(parents=True)
    _write(
        run_dir,
        "cases/ACC-0001/attempt-0001/case.json",
        json.dumps({"accession_no": SAMPLE_PHONE, "verdict": "hit"}),
    )

    report = scan_run_artifacts(run_dir)

    assert report.findings == (), f"检查号被误报了：{report.to_dict()}"


# --------------------------------------------------------------------------
# 4. 只报不改
# --------------------------------------------------------------------------


def test_scan_does_not_modify_artifacts(tmp_path):
    """扫完一遍，产物一个字都不能变（底账必须原样）。"""

    run_dir = tmp_path / "runs" / "dirty"
    run_dir.mkdir(parents=True)
    body = f"结论：姓名：{SAMPLE_NAME}，身份证号：{SAMPLE_ID_CARD}"
    path = _write(run_dir, "summary.json", body)

    scan_run_artifacts(run_dir)

    assert path.read_text(encoding="utf-8") == body


# --------------------------------------------------------------------------
# 5. 字段白名单与登记台账
# --------------------------------------------------------------------------


def test_unregistered_field_is_reported(tmp_path):
    """产物里出现没登记的字段 —— 后来加字段忘了登记，明细就出去了。"""

    run_dir = tmp_path / "runs" / "dirty"
    run_dir.mkdir(parents=True)
    _write(
        run_dir,
        "case_rows.csv",
        "accession_no,verdict,patient_name\nACC-0001,hit,张三\n",
    )

    report = scan_run_artifacts(run_dir)
    findings = [f for f in report.findings if f.kind == KIND_UNREGISTERED_FIELD]

    assert findings, f"未登记字段没报出来：{report.to_dict()}"
    assert "patient_name" in findings[0].excerpt


def test_register_artifact_fields_writes_ledger(tmp_path):
    """写产物时同步登记字段白名单。"""

    run_dir = tmp_path / "runs" / "ledger"
    run_dir.mkdir(parents=True)

    path = register_artifact_fields(run_dir, "case_rows.csv")
    registered = load_artifact_fields(run_dir)

    assert path.is_file()
    assert path.name == ARTIFACT_FIELDS_FILENAME
    assert "accession_no" in registered["case_rows.csv"]
    assert "verdict" in registered["case_rows.csv"]


def test_register_unknown_artifact_raises(tmp_path):
    """登记一个没在册的产物类型 —— 报错，不静默跳过。"""

    run_dir = tmp_path / "runs" / "ledger"
    run_dir.mkdir(parents=True)

    with pytest.raises(NoduleEvalError) as excinfo:
        register_artifact_fields(run_dir, "not-a-real-artifact")

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "not-a-real-artifact" in excinfo.value.detail


def test_run_registers_artifact_fields(tmp_path, full_env, monkeypatch):
    """跑完一轮，产物目录里有字段登记台账。"""

    run_dir = _run_once(tmp_path, monkeypatch)
    registered = load_artifact_fields(run_dir)

    assert "case_rows.csv" in registered
    assert "summary.json" in registered
    assert "case_attempt.json" in registered


def test_ledger_is_itself_scanned(tmp_path, full_env, monkeypatch):
    """台账自己也归扫描管 —— 改台账改不掉明细。"""

    run_dir = _run_once(tmp_path, monkeypatch)
    report = scan_run_artifacts(run_dir)

    assert not any(
        finding.file == ARTIFACT_FIELDS_FILENAME for finding in report.findings
    )


def test_model_call_ledger_is_scanned_too(tmp_path, full_env, monkeypatch):
    """走语义归并那一路，模型调用底账也在扫描范围内（协议字段从协议层取）。"""

    from stub_model import ModelStub

    from nodule_eval import cli

    inference_stub = InferenceStub()
    model_stub = ModelStub(
        content=json.dumps(
            {
                "presence": True,
                "location": "右肺下叶",
                "size_mm": 8.0,
                "density": "磨玻璃",
                "evidence_span": "右下肺见一磨玻璃结节",
                "confidence": 0.9,
                "needs_human_review": False,
            },
            ensure_ascii=False,
        )
    )
    inference_stub.start()
    model_stub.start()
    try:
        monkeypatch.setenv("INFERENCE_BASE_URL", inference_stub.base_url)
        monkeypatch.setenv("OPENAI_BASE_URL", model_stub.base_url)
        cases_path = tmp_path / "cases.json"
        cases_path.write_text(
            json.dumps(
                {
                    "run_id": "redact-merge-model",
                    "model_id": "demo-model",
                    "model_version": "v1.0.0",
                    "dataset_ref": "脱敏扫描 · 语义归并那一路",
                    "cases": [
                        {
                            "accession_no": "ACC-0001",
                            "gold_standard_text": GOLD_POSITIVE,
                            "image_ref": "dx://local/0001",
                            "study_uid": "STUDY-0001",
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
                "redact-merge-model",
                "--runs-dir",
                str(tmp_path / "runs"),
            ]
        )
    finally:
        inference_stub.stop()
        model_stub.stop()

    assert exit_code == 0
    run_dir = tmp_path / "runs" / "redact-merge-model"
    assert (run_dir / "model_calls.jsonl").is_file(), "模型调用底账没落盘"

    report = scan_run_artifacts(run_dir)

    assert report.findings == (), f"模型那一路的产物没扫干净：{report.to_dict()}"


# --------------------------------------------------------------------------
# 6. 命中即失败
# --------------------------------------------------------------------------


def test_assert_artifacts_clean_raises_on_pii(tmp_path):
    """命中即失败 —— 报错要指名文件与位置，退出码非零由调用方决定。"""

    run_dir = tmp_path / "runs" / "dirty"
    run_dir.mkdir(parents=True)
    _write(run_dir, "summary.json", json.dumps({"note": f"身份证号：{SAMPLE_ID_CARD}"}))

    with pytest.raises(NoduleEvalError) as excinfo:
        assert_artifacts_clean(run_dir)

    assert excinfo.value.code == ErrorCode.ARTIFACT_PII_DETECTED
    assert "summary.json" in excinfo.value.detail
    assert SAMPLE_ID_CARD not in excinfo.value.detail


def test_assert_artifacts_clean_passes_on_clean_run(tmp_path, full_env, monkeypatch):
    """干净产物放行。"""

    run_dir = _run_once(tmp_path, monkeypatch)

    report = assert_artifacts_clean(run_dir)

    assert report.ok


def test_assert_artifacts_clean_raises_on_unregistered_field(tmp_path):
    """未登记字段单独一种错误码 —— 处理办法不一样。"""

    run_dir = tmp_path / "runs" / "dirty"
    run_dir.mkdir(parents=True)
    _write(run_dir, "case_rows.csv", "accession_no,verdict,备注\nACC-0001,hit,x\n")

    with pytest.raises(NoduleEvalError) as excinfo:
        assert_artifacts_clean(run_dir)

    assert excinfo.value.code == ErrorCode.ARTIFACT_FIELD_UNREGISTERED
    assert "备注" in excinfo.value.detail


def test_scan_missing_run_dir_raises(tmp_path):
    """产物目录不存在 —— 指名报错，不静默返回「干净」。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        scan_run_artifacts(tmp_path / "runs" / "nope")

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "nope" in excinfo.value.detail


# --------------------------------------------------------------------------
# 7. 产品侧脱敏：落盘前把派生的原文摘句抹掉
# --------------------------------------------------------------------------


def test_mask_pii_replaces_detail():
    """脱敏：抹掉明细本身。"""

    masked, kinds = mask_pii(f"身份证号：{SAMPLE_ID_CARD}")

    assert SAMPLE_ID_CARD not in masked
    assert "已脱敏" in masked
    assert KIND_ID_CARD in kinds


def test_mask_pii_leaves_clean_text_untouched():
    """干净文本一个字都不动。"""

    text = GOLD_POSITIVE

    masked, kinds = mask_pii(text)

    assert masked == text
    assert kinds == []


def test_mask_pii_masks_record_preamble_name():
    """病历抬头里的姓名（「张三，男，58岁」）也抹。"""

    masked, kinds = mask_pii("张三，男，58岁。右下肺见一磨玻璃结节。")

    assert SAMPLE_NAME not in masked
    assert KIND_PATIENT_NAME in kinds


def test_mask_pii_keeps_common_words():
    """「患者男，58岁」里的「患者」不是姓名，别抹。"""

    masked, kinds = mask_pii("患者男，58岁")

    assert masked == "患者男，58岁"
    assert kinds == []


def test_mask_payload_masks_nested_name_field():
    """结构化载荷里的姓名字段也要抹。"""

    masked, kinds = mask_payload(
        {"accession_no": "ACC-0001", "patient_name": SAMPLE_NAME}
    )

    assert masked["patient_name"] == "【已脱敏·患者姓名】"
    assert masked["accession_no"] == "ACC-0001"
    assert KIND_PATIENT_NAME in kinds


def test_engine_masks_gold_derived_excerpt_before_writing(tmp_path, full_env, monkeypatch):
    """金标准原文派生的摘句落盘前要脱敏（原文在院内表里，产物不留明细）。

    规则归并只取「判出存在性的那一句」，一般带不上抬头里的姓名；
    但**语义归并**（模型回指）可能把整句带回来 —— 这一条走模型那条路。
    """

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        engine.normalizer = _named_evidence_normalizer  # type: ignore[attr-defined]
        engine.run(
            EvalRequest(
                run_id="redact-mask",
                model_id="demo-model",
                model_version="v1.0.0",
                dataset_ref="脱敏边界验收样例",
                cases=(
                    CaseInput(
                        accession_no="ACC-0001",
                        gold_standard_text="张三，男，58岁，右下肺见一磨玻璃结节，大小约 8mm。",
                        image_ref="dx://local/0001",
                        study_uid="STUDY-0001",
                        row_no=1,
                    ),
                ),
            )
        )
        store = engine.test_store  # type: ignore[attr-defined]
    finally:
        stub.stop()
    store.close()

    run_dir = tmp_path / "runs" / "redact-mask"
    artifacts = sorted(run_dir.rglob("case.json"))
    assert artifacts, "逐例产物没落盘"
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    blob = json.dumps(payload, ensure_ascii=False)

    assert SAMPLE_NAME not in blob
    assert payload.get("redaction", {}).get("masked") is True
    assert KIND_PATIENT_NAME in payload["redaction"]["kinds"]


def _named_evidence_normalizer(case: CaseInput):
    """一个「把整句带回来」的归并器 —— 模拟语义归并回指到含姓名的整句。"""

    return GoldLabels(
        presence=True,
        location="右肺下叶",
        size_mm=8.0,
        density="磨玻璃",
        evidence_span="张三，男，58岁，右下肺见一磨玻璃结节",
        confidence=0.9,
        source="model",
    )


def test_engine_artifact_still_has_gold_hash(tmp_path, full_env, monkeypatch):
    """脱敏不能把原文哈希也抹掉 —— 核对原文还要用它。"""

    run_dir = _run_once(tmp_path, monkeypatch)
    artifacts = sorted(run_dir.rglob("case.json"))
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))

    assert len(payload["gold_standard_sha256"]) == 64


# --------------------------------------------------------------------------
# 8. CLI：redact-scan
# --------------------------------------------------------------------------


def _cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "nodule_eval.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_redact_scan_clean_exits_zero(tmp_path):
    """干净产物：退出码 0。"""

    run_dir = tmp_path / "runs" / "clean"
    run_dir.mkdir(parents=True)
    _write(run_dir, "case_rows.csv", "accession_no,verdict\nACC-0001,hit\n")

    result = _cli(["redact-scan", "--run-id", "clean"], tmp_path)

    assert result.returncode == 0, result.stderr
    assert "干净" in result.stdout


def test_cli_redact_scan_dirty_exits_nonzero(tmp_path):
    """有明细：退出码非零，错误打在标准错误上。"""

    run_dir = tmp_path / "runs" / "dirty"
    run_dir.mkdir(parents=True)
    _write(
        run_dir,
        "summary.json",
        json.dumps({"note": f"身份证号：{SAMPLE_ID_CARD}"}, ensure_ascii=False),
    )

    result = _cli(["redact-scan", "--run-id", "dirty"], tmp_path)

    assert result.returncode != 0
    assert "ARTIFACT_PII_DETECTED" in result.stderr
    assert SAMPLE_ID_CARD not in result.stdout
    assert SAMPLE_ID_CARD not in result.stderr


def test_cli_redact_scan_json_output(tmp_path):
    """--json 可解析。"""

    run_dir = tmp_path / "runs" / "dirty"
    run_dir.mkdir(parents=True)
    _write(run_dir, "summary.json", json.dumps({"summary_text": f"姓名：{SAMPLE_NAME}"}))

    result = _cli(["redact-scan", "--run-id", "dirty", "--json"], tmp_path)

    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["findings"]
    assert payload["findings"][0]["kind"] == KIND_PATIENT_NAME
    assert payload["counts"][KIND_PATIENT_NAME] == 1


# --------------------------------------------------------------------------
# 9. 报告渲染
# --------------------------------------------------------------------------


def test_render_scan_report_is_readable(tmp_path):
    """报告要能直接给人看：说清扫了什么、命中在哪。"""

    run_dir = tmp_path / "runs" / "dirty"
    run_dir.mkdir(parents=True)
    _write(run_dir, "summary.json", json.dumps({"note": f"姓名：{SAMPLE_NAME}"}))

    text = render_scan_report(scan_run_artifacts(run_dir))

    assert "产物脱敏扫描" in text
    assert "不干净" in text
    assert "summary.json" in text
    assert SAMPLE_NAME not in text


def test_detect_pii_finds_nothing_in_ordinary_text():
    """普通中文不误报。"""

    assert detect_pii("右下肺见一磨玻璃结节，大小约 8mm。") == []
    assert detect_pii("双肺未见明显结节影。") == []
    assert detect_pii("ACC-2026-0001") == []
