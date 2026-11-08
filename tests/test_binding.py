"""三键实体绑定：``accession_no`` ↔ ``study_uid`` ↔ 表格行。

这一组测试盯住四件事：

1. **一一对应关系可核对** —— 绑定结果落 ``runs/<run_id>/binding.json``，
   每一行表格都能回答「绑到哪张影像、凭什么这么绑、有没有留差异」；
2. **一对多绝不自动取第一条** —— 命中多条影像的例挂起（``needs_review``），
   不带影像标识，**引擎不发起推理调用**，不进指标分母；
3. **表格与 RIS 不一致以 RIS 为准** —— 记差异、挂起复核，**不静默采用表格值**；
4. **指标隔离** —— ``needs_review`` 与 ``failed`` 的例一律不进分母，
   汇总行**单列**「挂起数」与「失败数」。

**为什么值得单独测**：绑定错一行，「模型判漏了」就会被记到一条根本不是
本次检查的影像上 —— 敏感度直接算歪，而且从结果表上看不出来。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import make_env
from stub_inference import InferenceStub
from stub_ris import DEFAULT_ITEM, RISStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.adapters.ris import RISAdapter
from nodule_eval.caseset import (
    AUTHORITY_RIS,
    AUTHORITY_SNAPSHOT,
    BINDING_AMBIGUOUS,
    BINDING_BOUND,
    BINDING_DUPLICATE,
    BINDING_FILENAME,
    BINDING_UNRESOLVED,
    RISItem,
    bind_cases,
)
from nodule_eval.config import load_config
from nodule_eval.contracts import (
    VERDICT_FAILED,
    VERDICT_HIT,
    VERDICT_PENDING,
    CaseInput,
    CaseRow,
    partition_rows,
)
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode

GOLD_TEXT = "右下肺见一磨玻璃结节，大小约 8mm。"


def _item(accession_no: Any = "ACC-001", **overrides: Any) -> dict[str, Any]:
    """一条 DX 检查记录（可覆写任一字段）。"""

    payload = dict(DEFAULT_ITEM)
    payload.update(overrides)
    payload["accession_no"] = accession_no
    return payload


def _ris_item(accession_no: str = "ACC-001", **overrides: Any) -> RISItem:
    """把替身用的记录转成绑定层吃的 :class:`RISItem`。"""

    payload = _item(accession_no, **overrides)
    return RISItem(
        accession_no=payload["accession_no"],
        study_uid=payload["study_uid"],
        modality=payload["modality"],
        study_date=payload["study_date"],
        report_status=payload["report_status"],
        image_ref=payload["image_ref"],
    )


def _case(accession_no: str = "ACC-001", **overrides: Any) -> CaseInput:
    """评估表里的一行（既有列）。"""

    payload: dict[str, Any] = {
        "accession_no": accession_no,
        "gold_standard_text": GOLD_TEXT,
        "row_no": 2,
    }
    payload.update(overrides)
    return CaseInput(**payload)


def _binding_manifest(runs_dir: Path, run_id: str) -> dict[str, Any]:
    """读绑定清单。"""

    path = runs_dir / run_id / BINDING_FILENAME
    assert path.is_file(), f"绑定清单必须落盘：{path}"
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 1. 一一对应关系落盘
# --------------------------------------------------------------------------


def test_binding_manifest_records_the_three_key_mapping(tmp_path: Path):
    """绑定清单落 ``runs/<run_id>/binding.json`` —— 三键一一对应可核对。"""

    result = bind_cases(
        [_case("ACC-001", row_no=2), _case("ACC-002", row_no=3)],
        [_ris_item("ACC-001", study_uid="STUDY-A"), _ris_item("ACC-002", study_uid="STUDY-B")],
        run_id="run-001",
        runs_dir=tmp_path / "runs",
    )

    manifest = _binding_manifest(tmp_path / "runs", "run-001")
    assert result.binding_path == str(tmp_path / "runs" / "run-001" / BINDING_FILENAME)
    assert manifest["run_id"] == "run-001"
    assert manifest["authority"] == AUTHORITY_RIS
    assert manifest["counts"]["bound"] == 2
    assert manifest["counts"]["ambiguous"] == 0

    entries = {entry["accession_no"]: entry for entry in manifest["entries"]}
    assert set(entries) == {"ACC-001", "ACC-002"}
    for accession_no, entry in entries.items():
        # 三键：表格行号 ↔ 业务检查号 ↔ 影像标识
        assert entry["accession_no"] == accession_no
        assert entry["row_no"] in (2, 3)
        assert entry["study_uid"].startswith("STUDY-")
        assert entry["status"] == BINDING_BOUND
        assert entry["needs_review"] is False


def test_binding_keeps_the_sheet_row_number_as_the_anchor(tmp_path: Path):
    """表格行号是回填与人工复核的定位锚点 —— 绑定结果里必须留着。"""

    result = bind_cases(
        [_case("ACC-001", row_no=17)],
        [_ris_item("ACC-001")],
        run_id="run-001",
        runs_dir=tmp_path / "runs",
    )

    entry = result.entry_for("ACC-001")
    assert entry is not None
    assert entry.row_no == 17
    assert result.cases[0].row_no == 17


# --------------------------------------------------------------------------
# 2. 一对多：挂起，绝不自动取第一条
# --------------------------------------------------------------------------


def test_one_to_many_is_held_and_never_auto_picks_the_first():
    """同一检查号命中多条影像 —— 挂起，不取第一条。"""

    items = [
        _ris_item("ACC-001", study_uid="STUDY-A", study_date="2026-03-01"),
        _ris_item("ACC-001", study_uid="STUDY-B", study_date="2026-03-05"),
    ]

    result = bind_cases([_case("ACC-001")], items, authority=AUTHORITY_RIS, write=False)

    entry = result.entry_for("ACC-001")
    assert entry is not None
    assert entry.status == BINDING_AMBIGUOUS
    assert entry.error_code == ErrorCode.AMBIGUOUS_BINDING.value
    assert entry.needs_review is True
    assert entry.study_uid is None, "绝不自动取第一条"
    assert entry.image_ref is None, "没有可信引用就不发起推理调用"

    case = result.cases[0]
    assert case.study_uid is None
    assert case.image_ref is None
    assert case.needs_review is True
    assert "ACC-001" in case.hold_reason
    assert "命中多条影像" in case.hold_reason


def test_one_to_many_records_every_candidate_for_human_judgement():
    """候选影像原样记下来 —— 人工判定时要能看清有哪几张可选。"""

    items = [
        _ris_item("ACC-001", study_uid="STUDY-A", study_date="2026-03-01"),
        _ris_item("ACC-001", study_uid="STUDY-B", study_date="2026-03-05"),
    ]

    result = bind_cases([_case("ACC-001")], items, authority=AUTHORITY_RIS, write=False)

    entry = result.entry_for("ACC-001")
    assert entry is not None
    candidates = {candidate.study_uid: candidate for candidate in entry.candidates}
    assert set(candidates) == {"STUDY-A", "STUDY-B"}
    assert candidates["STUDY-A"].study_date == "2026-03-01"
    assert candidates["STUDY-B"].study_date == "2026-03-05"


def test_one_to_many_is_not_counted_in_the_metric_denominator():
    """歧义例 —— 不进指标分母，汇总行单列挂起数。"""

    items = [
        _ris_item("ACC-001", study_uid="STUDY-A"),
        _ris_item("ACC-001", study_uid="STUDY-B"),
    ]

    result = bind_cases([_case("ACC-001")], items, authority=AUTHORITY_RIS, write=False)
    held = result.cases[0]

    row = CaseRow(
        accession_no=held.accession_no,
        verdict=VERDICT_FAILED,
        failure_reason=held.hold_reason,
        needs_review=held.needs_review,
    )
    partition = partition_rows([row])

    assert partition.counted == 0, "挂起的例不进指标分母"
    assert partition.needs_review == 1
    assert partition.failed == 0, "挂起与失败是两类，不混在一列里"
    assert partition.excluded == 1


def test_held_case_is_never_called_by_the_engine(tmp_path: Path):
    """挂起的例 —— 引擎不发起推理调用，整批不中断。"""

    stub = InferenceStub()
    stub.start()
    try:
        env = make_env(INFERENCE_BASE_URL=stub.base_url)
        config = load_config(env)
        from nodule_eval.config import load_model_spec

        spec = load_model_spec("demo-model")
        engine = Engine(
            config,
            spec,
            client=HttpClient(sleep=lambda _s: None),
            runs_dir=tmp_path / "runs",
        )

        items = [
            _ris_item("ACC-001", study_uid="STUDY-A"),
            _ris_item("ACC-001", study_uid="STUDY-B"),
        ]
        binding = bind_cases([_case("ACC-001")], items, authority=AUTHORITY_RIS, write=False)

        from nodule_eval.contracts import EvalRequest

        request = EvalRequest(
            run_id="held-001",
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="绑定样例",
            cases=binding.cases,
        )
        response = engine.run(request)
    finally:
        stub.stop()

    assert len(stub.requests) == 0, "挂起的例不该发起推理调用"
    assert len(response.case_rows) == 1
    row = response.case_rows[0]
    assert row.verdict == VERDICT_FAILED
    assert row.needs_review is True
    assert row.tool_call_id == "", "没调用就没有调用记录 ID"
    assert "命中多条影像" in row.failure_reason
    assert response.summary.needs_review_count == 1
    assert response.summary.failed_count == 0, "挂起不记成调用失败"


# --------------------------------------------------------------------------
# 3. 表格与 RIS 不一致：以 RIS 为准
# --------------------------------------------------------------------------


def test_sheet_image_ref_conflict_falls_back_to_ris_and_records_the_difference():
    """表格给的引用与 RIS 不一致 —— 以 RIS 为准，记差异并挂起复核。"""

    result = bind_cases(
        [_case("ACC-001", image_ref="dx://from-sheet/001")],
        [_ris_item("ACC-001", image_ref="dx://from-ris/001")],
        authority=AUTHORITY_RIS,
        write=False,
    )

    entry = result.entry_for("ACC-001")
    assert entry is not None
    assert entry.image_ref == "dx://from-ris/001", "以 RIS 为真源，不静默采用表格值"
    assert entry.image_ref_source == "ris"
    assert entry.needs_review is True, "不一致要挂起复核"

    difference = next(d for d in entry.differences if d.field == "image_ref")
    assert difference.sheet_value == "dx://from-sheet/001"
    assert difference.authority_value == "dx://from-ris/001"
    assert "以 RIS 为准" in difference.note


def test_sheet_study_uid_conflict_falls_back_to_ris():
    """表格里的影像标识与 RIS 不一致 —— 同样以 RIS 为准。"""

    result = bind_cases(
        [_case("ACC-001", study_uid="STUDY-WRONG")],
        [_ris_item("ACC-001", study_uid="STUDY-RIGHT")],
        authority=AUTHORITY_RIS,
        write=False,
    )

    entry = result.entry_for("ACC-001")
    assert entry is not None
    assert entry.study_uid == "STUDY-RIGHT"
    assert entry.needs_review is True
    difference = next(d for d in entry.differences if d.field == "study_uid")
    assert difference.sheet_value == "STUDY-WRONG"
    assert difference.authority_value == "STUDY-RIGHT"


def test_ris_without_image_ref_keeps_the_sheet_reference():
    """RIS 没给引用 —— 沿用表格的，但记一笔（不是冲突，是缺字段）。"""

    result = bind_cases(
        [_case("ACC-001", image_ref="dx://from-sheet/001")],
        [_ris_item("ACC-001", image_ref="")],
        authority=AUTHORITY_RIS,
        write=False,
    )

    entry = result.entry_for("ACC-001")
    assert entry is not None
    assert entry.image_ref == "dx://from-sheet/001"
    assert entry.needs_review is False, "只是 RIS 缺字段，不必挂起"
    assert entry.differences[0].authority_value == ""


def test_snapshot_authority_keeps_the_sheet_value_and_only_records_the_difference():
    """快照口径 —— 快照可能过期，表格优先，但差异照样留痕。"""

    result = bind_cases(
        [_case("ACC-001", image_ref="dx://from-sheet/001")],
        [_ris_item("ACC-001", image_ref="dx://from-snapshot/001")],
        authority=AUTHORITY_SNAPSHOT,
        write=False,
    )

    entry = result.entry_for("ACC-001")
    assert entry is not None
    assert entry.image_ref == "dx://from-sheet/001"
    assert entry.image_ref_source == "sheet"
    assert entry.needs_review is False, "快照口径不因差异挂起"
    assert entry.differences[0].field == "image_ref"


def test_snapshot_authority_also_keeps_the_sheet_study_uid():
    """快照口径下，表格带的影像标识同样优先 —— 与影像引用一个口径。"""

    result = bind_cases(
        [_case("ACC-001", study_uid="STUDY-FROM-SHEET")],
        [_ris_item("ACC-001", study_uid="STUDY-FROM-SNAPSHOT")],
        authority=AUTHORITY_SNAPSHOT,
        write=False,
    )

    entry = result.entry_for("ACC-001")
    assert entry is not None
    assert entry.study_uid == "STUDY-FROM-SHEET"
    assert entry.study_uid_source == "sheet"
    assert entry.needs_review is False
    difference = next(d for d in entry.differences if d.field == "study_uid")
    assert "快照可能过期" in difference.note


# --------------------------------------------------------------------------
# 4. 找不到 / 重复：同样挂起
# --------------------------------------------------------------------------


def test_accession_missing_from_ris_is_held_not_dropped():
    """RIS 里没有的检查号 —— 挂起，不静默丢行。"""

    result = bind_cases(
        [_case("ACC-404")],
        [_ris_item("ACC-001")],
        authority=AUTHORITY_RIS,
        write=False,
    )

    entry = result.entry_for("ACC-404")
    assert entry is not None
    assert entry.status == BINDING_UNRESOLVED
    assert entry.needs_review is True
    assert result.cases[0].needs_review is True
    assert "ACC-404" in result.cases[0].hold_reason


def test_duplicate_accession_in_the_sheet_is_held():
    """同一个检查号在表里出现两次 —— 一一对应关系不成立，挂起。"""

    result = bind_cases(
        [_case("ACC-001", row_no=2), _case("ACC-001", row_no=9)],
        [_ris_item("ACC-001")],
        authority=AUTHORITY_RIS,
        write=False,
    )

    for entry in result.entries:
        assert entry.status == BINDING_DUPLICATE
        assert entry.needs_review is True
        assert entry.study_uid is None
    assert result.cases[0].needs_review is True
    assert result.cases[1].needs_review is True


# --------------------------------------------------------------------------
# 5. 指标隔离：挂起与失败各归各
# --------------------------------------------------------------------------


def test_needs_review_and_failed_rows_never_enter_the_denominator():
    """三类行：已判定 / 挂起 / 失败 —— 只有第一类进分母。"""

    rows = [
        CaseRow(accession_no="A", verdict=VERDICT_HIT),
        CaseRow(accession_no="B", verdict=VERDICT_HIT, needs_review=True),
        CaseRow(accession_no="C", verdict=VERDICT_FAILED),
        CaseRow(accession_no="D", verdict=VERDICT_FAILED, needs_review=True),
        CaseRow(accession_no="E", verdict=VERDICT_PENDING),
    ]

    partition = partition_rows(rows)

    assert partition.total == 5
    assert partition.counted == 1, "只有未挂起的已判定例进分母"
    assert partition.judged == 2
    assert partition.failed == 1, "失败与挂起互斥单列"
    assert partition.needs_review == 2
    assert partition.excluded == 4


def test_summary_lists_held_and_failed_counts_separately(tmp_path: Path):
    """汇总行单列「挂起数」与「失败数」—— 两个数互不吞并。"""

    stub = InferenceStub(status_sequence=[400])
    stub.start()
    try:
        env = make_env(INFERENCE_BASE_URL=stub.base_url)
        config = load_config(env)
        from nodule_eval.config import load_model_spec

        spec = load_model_spec("demo-model")
        engine = Engine(
            config,
            spec,
            client=HttpClient(sleep=lambda _s: None),
            runs_dir=tmp_path / "runs",
        )

        from nodule_eval.contracts import EvalRequest

        request = EvalRequest(
            run_id="mixed-001",
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="混合样例",
            cases=(
                _case("ACC-001", study_uid="STUDY-A", row_no=2),
                _case("ACC-002", needs_review=True, hold_reason="检查号 ACC-002 命中多条影像，无法自动绑定，已挂起人工判定", row_no=3),
            ),
        )
        response = engine.run(request)
    finally:
        stub.stop()

    assert response.summary.total_cases == 2
    assert response.summary.failed_count == 1, "调用失败一例"
    assert response.summary.needs_review_count == 1, "挂起一例"
    assert response.summary.evaluated_cases == 0


# --------------------------------------------------------------------------
# 6. 金标准原文零改动
# --------------------------------------------------------------------------


def test_gold_standard_text_is_never_rewritten_by_the_binding_layer():
    """绑定层只碰引用，**一个字都不改金标准原文**。"""

    text = "双肺未见明显结节影。\n但右下肺见磨玻璃结节，约 8mm，大小 0.8cm。"

    result = bind_cases(
        [_case("ACC-001", gold_standard_text=text)],
        [_ris_item("ACC-001")],
        authority=AUTHORITY_RIS,
        write=False,
    )

    assert result.cases[0].gold_standard_text == text


# --------------------------------------------------------------------------
# 7. 真实出站：RIS 替身 → 绑定清单 → 推理调用
# --------------------------------------------------------------------------


def test_cli_run_binds_against_ris_and_writes_the_binding_manifest(tmp_path, monkeypatch):
    """``cli run`` 真打 RIS 拿清单 → 绑定 → 落绑定清单 → 再打推理端点。"""

    from nodule_eval import cli

    ris_stub = RISStub(
        items=[
            _item("ACC-001", study_uid="STUDY-A"),
            _item("ACC-002", study_uid="STUDY-A"),
            _item("ACC-002", study_uid="STUDY-B"),
            _item("ACC-003", study_uid="STUDY-D", image_ref="dx://ris/STUDY-D"),
        ]
    )
    inference_stub = InferenceStub()
    ris_stub.start()
    inference_stub.start()
    try:
        env = make_env(
            RIS_BASE_URL=ris_stub.base_url,
            INFERENCE_BASE_URL=inference_stub.base_url,
            IMAGING_EVAL_FIXTURE="1",
            IMAGING_EVAL_FIXTURE_RIS_BASE_URL=ris_stub.base_url,
            IMAGING_EVAL_FIXTURE_INFERENCE_BASE_URL=inference_stub.base_url,
            # 替身开关是「全开或全关」：另两个端点本轮不调用，也要把地址给齐，
            # 否则替身门会指名报错（这正是它该有的行为）。
            IMAGING_EVAL_FIXTURE_RESEARCH_BASE_URL=inference_stub.base_url,
            IMAGING_EVAL_FIXTURE_MODEL_BASE_URL=inference_stub.base_url,
        )
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        cases_file = tmp_path / "cases.json"
        cases_file.write_text(
            json.dumps(
                {
                    "run_id": "bind-001",
                    "model_id": "demo-model",
                    "model_version": "v1.0.0",
                    "dataset_ref": "绑定样例",
                    "cases": [
                        {
                            "accession_no": "ACC-001",
                            "gold_standard_text": GOLD_TEXT,
                            "row_no": 2,
                        },
                        {
                            "accession_no": "ACC-002",
                            "gold_standard_text": GOLD_TEXT,
                            "row_no": 3,
                        },
                        {
                            "accession_no": "ACC-003",
                            "gold_standard_text": GOLD_TEXT,
                            "image_ref": "dx://from-sheet/003",
                            "row_no": 4,
                        },
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
                str(cases_file),
                "--model-id",
                "demo-model",
                "--run-id",
                "bind-001",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--store",
                str(tmp_path / "store" / "nodule_eval.sqlite3"),
                "--fixture",
            ]
        )
    finally:
        ris_stub.stop()
        inference_stub.stop()

    assert exit_code == 0
    assert len(ris_stub.requests) == 1, "绑定读一次 RIS 清单"
    assert ris_stub.requests[0]["path"] == "/api/v1/imaging-studies"

    manifest = _binding_manifest(tmp_path / "runs", "bind-001")
    assert manifest["counts"]["bound"] == 2
    assert manifest["counts"]["ambiguous"] == 1
    assert manifest["counts"]["with_difference"] == 1
    assert manifest["counts"]["needs_review"] == 2

    entries = {entry["accession_no"]: entry for entry in manifest["entries"]}
    assert entries["ACC-001"]["study_uid"] == "STUDY-A"
    assert entries["ACC-002"]["status"] == BINDING_AMBIGUOUS
    assert entries["ACC-002"]["study_uid"] is None
    # 表格给了与 RIS 不一致的引用 —— 以 RIS 为准，并挂起复核
    assert entries["ACC-003"]["image_ref"] == "dx://ris/STUDY-D"
    assert entries["ACC-003"]["needs_review"] is True
    assert entries["ACC-003"]["differences"][0]["field"] == "image_ref"

    # 歧义例不被调用；绑上的两例正常调用，且用的是 RIS 的引用。
    assert len(inference_stub.requests) == 2
    bodies = {request["body"]["study_uid"]: request["body"] for request in inference_stub.requests}
    assert set(bodies) == {"STUDY-A", "STUDY-D"}
    assert bodies["STUDY-D"]["image_ref"] == "dx://ris/STUDY-D", (
        "表格给的引用与 RIS 不一致时，发出去的是 RIS 的引用"
    )

    result = json.loads(
        (tmp_path / "runs" / "bind-001" / "result.json").read_text(encoding="utf-8")
    )
    assert result["summary"]["needs_review_count"] == 2, "歧义例 + 引用不一致例都挂起"
    assert result["summary"]["failed_count"] == 0

    rows = {row["accession_no"]: row for row in result["case_rows"]}
    assert rows["ACC-002"]["needs_review"] is True
    assert rows["ACC-002"]["tool_call_id"] == "", "挂起的例没有调用记录"
    assert rows["ACC-003"]["needs_review"] is True, "绑定层的挂起标记不能被丢掉"


def test_cli_run_does_not_read_ris_when_every_case_already_carries_a_study_uid(
    tmp_path, monkeypatch
):
    """已经带好影像标识的请求 —— 一次 RIS 都不打。"""

    from nodule_eval import cli

    inference_stub = InferenceStub()
    inference_stub.start()
    try:
        env = make_env(INFERENCE_BASE_URL=inference_stub.base_url)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        cases_file = tmp_path / "cases.json"
        cases_file.write_text(
            json.dumps(
                {
                    "run_id": "bind-002",
                    "model_id": "demo-model",
                    "model_version": "v1.0.0",
                    "dataset_ref": "绑定样例",
                    "cases": [
                        {
                            "accession_no": "ACC-001",
                            "gold_standard_text": GOLD_TEXT,
                            "image_ref": "dx://demo/study-001",
                            "study_uid": "STUDY-DEMO-001",
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
                str(cases_file),
                "--model-id",
                "demo-model",
                "--run-id",
                "bind-002",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--store",
                str(tmp_path / "store" / "nodule_eval.sqlite3"),
            ]
        )
    finally:
        inference_stub.stop()

    assert exit_code == 0
    assert len(inference_stub.requests) == 1
    assert not (tmp_path / "runs" / "bind-002" / BINDING_FILENAME).exists(), (
        "不需要绑定时不多读 RIS、不多写绑定清单"
    )


# --------------------------------------------------------------------------
# 8. 替身纪律
# --------------------------------------------------------------------------


def test_product_code_never_imports_the_stubs():
    """替身只在 tests/ 层 —— 产品代码不 import 任何替身模块。"""

    src_root = Path(__file__).resolve().parents[1] / "src"
    for path in src_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("import stub_", "from stub_", "import scenarios"):
            assert forbidden not in source, f"{path.name} 里不应出现 {forbidden}"


def test_binding_layer_never_points_at_a_local_endpoint():
    """绑定层的出站地址只来自环境变量 —— 代码里没有本地回环兜底。"""

    source = (
        Path(__file__).resolve().parents[1] / "src" / "nodule_eval" / "caseset.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("localhost", "127.0.0.1", "example.com"):
        assert forbidden not in source, f"绑定层里不应出现 {forbidden}"
