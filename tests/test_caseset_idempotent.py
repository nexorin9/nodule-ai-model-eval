"""病例集装配：按检查号幂等落盘 + image_ref 兜底 + 装配清单。

这一组测试盯住三件事：

1. **幂等** —— 二次装配同一批检查号：一次 RIS 都不打、不产生新目录、
   已装配的内容（连装配时间）一字不改。这是「重复装配会把已经评审过的影像
   引用改掉」的防线；
2. **兜底解析** —— 表格没给 ``image_ref`` 时按检查号从**已装配的病例集**取；
   两处都取不到就挂 ``needs_review``，**不静默用空值继续**；
3. **不静默丢例** —— RIS 里没有的检查号、同一检查号命中多条影像，
   都进装配清单，不被悄悄丢掉或「自动取第一条」。

**源产品证据**：参考实现按 ``case_id`` 建目录落图、**已存在则跳过**
（``github_ref/MedRAX/data/figures.py:49-57`` 的 ``save_figure``：
``if output_path.exists(): return``），病例号来自翻页抓取
（``github_ref/MedRAX/data/get_cases.py:29-41``）。本产品把同一机制改写到
院内 RIS 上：来源从 Eurorad 换成 RIS，落盘单位从「一张图」换成「一例」。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import make_env
from stub_ris import DEFAULT_ITEM, RISStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.adapters.ris import RISAdapter
from nodule_eval.caseset import (
    CASE_FILE_FILENAME,
    CASESET_MANIFEST_FILENAME,
    assemble_caseset,
    resolve_case_inputs,
    safe_accession_component,
)
from nodule_eval.config import load_config
from nodule_eval.contracts import CaseInput
from nodule_eval.errors import ErrorCode, NoduleEvalError


def _adapter(stub: RISStub) -> RISAdapter:
    """对着替身造一个 RIS 适配器（退避不真睡，测试要快）。"""

    env = make_env(RIS_BASE_URL=stub.base_url)
    config = load_config(env)
    client = HttpClient(sleep=lambda _s: None)
    return RISAdapter(config, client=client)


def _item(accession_no: Any = "ACC-001", **overrides: Any) -> dict[str, Any]:
    """一条 DX 检查记录（可覆写任一字段）。"""

    payload = dict(DEFAULT_ITEM)
    payload.update(overrides)
    payload["accession_no"] = accession_no
    return payload


def _case_json(cases_root: Path, accession_no: str) -> dict[str, Any]:
    """读某一例的装配文件。"""

    path = cases_root / accession_no / CASE_FILE_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 1. 首次装配
# --------------------------------------------------------------------------


def test_first_assembly_writes_one_directory_per_accession(tmp_path: Path):
    """首次装配 —— 每例一个目录，里面放 study_uid / image_ref / 装配时间。"""

    items = [_item("ACC-001"), _item("ACC-002"), _item("ACC-003")]
    stub = RISStub(items=items)
    stub.start()
    try:
        assembly = assemble_caseset(
            _adapter(stub),
            tmp_path / "cases",
            run_id="run-001",
            runs_dir=tmp_path / "runs",
        )
    finally:
        stub.stop()

    assert assembly.counts()["assembled"] == 3
    assert assembly.counts()["skipped"] == 0
    for accession_no in ("ACC-001", "ACC-002", "ACC-003"):
        payload = _case_json(tmp_path / "cases", accession_no)
        assert payload["accession_no"] == accession_no
        assert payload["study_uid"], "装配目录里必须有影像标识"
        assert payload["image_ref"], "装配目录里必须有图像引用"
        assert payload["assembled_at"], "装配目录里必须有装配时间"
        assert payload["source"] == "ris"


def test_assembly_manifest_records_source_skipped_and_added(tmp_path: Path):
    """装配清单落 ``runs/<run_id>/caseset_manifest.json``，供事后核对。"""

    stub = RISStub(items=[_item("ACC-001"), _item("ACC-002")])
    stub.start()
    try:
        assembly = assemble_caseset(
            _adapter(stub),
            tmp_path / "cases",
            run_id="run-001",
            runs_dir=tmp_path / "runs",
        )
    finally:
        stub.stop()

    manifest_path = tmp_path / "runs" / "run-001" / CASESET_MANIFEST_FILENAME
    assert manifest_path.is_file(), "装配清单必须落盘"
    assert assembly.manifest_path == str(manifest_path)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["run_id"] == "run-001"
    assert manifest["source"] == "ris"
    assert manifest["assembled"] == ["ACC-001", "ACC-002"]
    assert manifest["counts"]["assembled"] == 2
    assert manifest["counts"]["skipped"] == 0
    assert manifest["ris_counts"]["total_count"] == 2


# --------------------------------------------------------------------------
# 2. 幂等
# --------------------------------------------------------------------------


def test_second_assembly_hits_ris_zero_times_and_changes_nothing(tmp_path: Path):
    """二次装配同一批检查号 —— 一次 RIS 都不打，目录与内容一字不改。"""

    items = [_item("ACC-001"), _item("ACC-002")]
    stub = RISStub(items=items)
    stub.start()
    try:
        adapter = _adapter(stub)
        assemble_caseset(
            adapter,
            tmp_path / "cases",
            run_id="run-001",
            requested=["ACC-001", "ACC-002"],
            runs_dir=tmp_path / "runs",
        )
        assert len(stub.requests) == 1, "首次装配读一次 RIS"

        before = {
            name: _case_json(tmp_path / "cases", name)
            for name in ("ACC-001", "ACC-002")
        }
        dirs_before = sorted(p.name for p in (tmp_path / "cases").iterdir())

        second = assemble_caseset(
            adapter,
            tmp_path / "cases",
            run_id="run-002",
            requested=["ACC-001", "ACC-002"],
            runs_dir=tmp_path / "runs",
        )
    finally:
        stub.stop()

    assert len(stub.requests) == 1, "二次装配不该再打 RIS"
    assert second.counts()["assembled"] == 0
    assert second.counts()["skipped"] == 2

    dirs_after = sorted(p.name for p in (tmp_path / "cases").iterdir())
    assert dirs_after == dirs_before, "二次装配不该产生新目录"
    for name, payload in before.items():
        assert _case_json(tmp_path / "cases", name) == payload, "已装配的内容不该被改写"


def test_unresolved_accession_is_asked_again_but_assembled_one_is_not(tmp_path: Path):
    """「已装配」与「未解析」的区别 —— 前者不再问 RIS，后者每次都再问一次。

    RIS 里没有的检查号可能只是还没进 RIS（比如报告还没出），悄悄跳过等于
    让这一例永远装不上。
    """

    stub = RISStub(items=[_item("ACC-001")])
    stub.start()
    try:
        adapter = _adapter(stub)
        first = assemble_caseset(
            adapter,
            tmp_path / "cases",
            run_id="run-001",
            requested=["ACC-001", "ACC-404"],
            runs_dir=tmp_path / "runs",
        )
        second = assemble_caseset(
            adapter,
            tmp_path / "cases",
            run_id="run-002",
            requested=["ACC-001", "ACC-404"],
            runs_dir=tmp_path / "runs",
        )
    finally:
        stub.stop()

    assert len(stub.requests) == 2, "未解析的检查号每次都要再问一次 RIS"
    assert first.counts()["assembled"] == 1
    assert first.counts()["unresolved"] == 1
    assert second.counts()["assembled"] == 0, "已装配的例不重复装配"
    assert second.counts()["skipped"] == 1
    assert second.counts()["unresolved"] == 1, "未解析的例仍留在清单里，不被悄悄丢掉"
    assert (tmp_path / "cases" / "ACC-001" / CASE_FILE_FILENAME).is_file()
    assert not (tmp_path / "cases" / "ACC-404").exists()


def test_assemble_all_mode_skips_assembled_cases_without_rewriting(tmp_path: Path):
    """全量装配模式 —— 清单仍读一次（清单本身会变），但已装配的例一律跳过。

    全量模式下「有哪些检查」只能问 RIS，所以清单读一次是必要的；
    **不重复装配**的部分仍然成立：不产生新目录、不改写已装配的内容。
    """

    items = [_item("ACC-001"), _item("ACC-002")]
    stub = RISStub(items=items)
    stub.start()
    try:
        adapter = _adapter(stub)
        assemble_caseset(
            adapter, tmp_path / "cases", run_id="run-001", runs_dir=tmp_path / "runs"
        )
        before = _case_json(tmp_path / "cases", "ACC-001")
        dirs_before = sorted(p.name for p in (tmp_path / "cases").iterdir())

        second = assemble_caseset(
            adapter, tmp_path / "cases", run_id="run-002", runs_dir=tmp_path / "runs"
        )
    finally:
        stub.stop()

    assert second.counts()["assembled"] == 0
    assert second.counts()["skipped"] == 2
    assert sorted(p.name for p in (tmp_path / "cases").iterdir()) == dirs_before
    assert _case_json(tmp_path / "cases", "ACC-001") == before, "已装配的内容不该被改写"


def test_second_assembly_with_explicit_accessions_also_skips_ris(tmp_path: Path):
    """指定检查号重复装配 —— 同样一次 RIS 都不打。"""

    stub = RISStub(items=[_item("ACC-001"), _item("ACC-002")])
    stub.start()
    try:
        adapter = _adapter(stub)
        assemble_caseset(
            adapter,
            tmp_path / "cases",
            run_id="run-001",
            requested=["ACC-001"],
            runs_dir=tmp_path / "runs",
        )
        assert len(stub.requests) == 1

        second = assemble_caseset(
            adapter,
            tmp_path / "cases",
            run_id="run-002",
            requested=["ACC-001"],
            runs_dir=tmp_path / "runs",
        )
    finally:
        stub.stop()

    assert len(stub.requests) == 1, "已装配的检查号不该再打 RIS"
    assert second.counts()["skipped"] == 1
    assert second.counts()["assembled"] == 0


def test_new_accession_is_assembled_while_existing_one_is_skipped(tmp_path: Path):
    """增量装配 —— 只有新检查号去读 RIS，已装配的照旧跳过。"""

    stub = RISStub(items=[_item("ACC-001"), _item("ACC-002")])
    stub.start()
    try:
        adapter = _adapter(stub)
        assemble_caseset(
            adapter,
            tmp_path / "cases",
            run_id="run-001",
            requested=["ACC-001"],
            runs_dir=tmp_path / "runs",
        )
        second = assemble_caseset(
            adapter,
            tmp_path / "cases",
            run_id="run-002",
            requested=["ACC-001", "ACC-002"],
            runs_dir=tmp_path / "runs",
        )
    finally:
        stub.stop()

    assert len(stub.requests) == 2, "有新检查号才打第二次 RIS"
    assert second.counts()["assembled"] == 1
    assert second.counts()["skipped"] == 1
    assert second.assembled == ("ACC-002",)
    assert second.skipped == ("ACC-001",)


def test_damaged_case_directory_is_refilled_not_skipped(tmp_path: Path):
    """目录在但内容坏了 —— 算未装配，下次补齐，不留空壳。"""

    cases_root = tmp_path / "cases"
    (cases_root / "ACC-001").mkdir(parents=True)
    (cases_root / "ACC-001" / CASE_FILE_FILENAME).write_text("{ 坏掉的 json", encoding="utf-8")

    stub = RISStub(items=[_item("ACC-001")])
    stub.start()
    try:
        assembly = assemble_caseset(
            _adapter(stub),
            cases_root,
            run_id="run-001",
            runs_dir=tmp_path / "runs",
        )
    finally:
        stub.stop()

    assert assembly.counts()["assembled"] == 1, "坏掉的目录要被补齐"
    assert assembly.counts()["damaged_dirs"] == 1, "损坏目录数要看得见"
    assert _case_json(cases_root, "ACC-001")["study_uid"]


# --------------------------------------------------------------------------
# 3. image_ref 兜底
# --------------------------------------------------------------------------


def test_image_ref_from_sheet_is_kept(tmp_path: Path):
    """表格给了 image_ref —— 以表格为准。"""

    cases_root = tmp_path / "cases"
    stub = RISStub(items=[_item("ACC-001")])
    stub.start()
    try:
        assemble_caseset(
            _adapter(stub), cases_root, run_id="run-001", runs_dir=tmp_path / "runs"
        )
    finally:
        stub.stop()

    resolution = resolve_case_inputs(
        [
            CaseInput(
                accession_no="ACC-001",
                gold_standard_text="右下肺见磨玻璃结节",
                image_ref="dx://from-sheet/001",
            )
        ],
        cases_root,
    )

    case = resolution.cases[0]
    assert case.image_ref == "dx://from-sheet/001", "表格给的引用不能被病例集覆盖"
    assert resolution.sources["ACC-001"] == "sheet"
    assert case.needs_review is False


def test_image_ref_falls_back_to_assembled_caseset(tmp_path: Path):
    """表格没给 image_ref —— 按检查号从已装配的病例集取。"""

    cases_root = tmp_path / "cases"
    stub = RISStub(items=[_item("ACC-001")])
    stub.start()
    try:
        assemble_caseset(
            _adapter(stub), cases_root, run_id="run-001", runs_dir=tmp_path / "runs"
        )
    finally:
        stub.stop()

    resolution = resolve_case_inputs(
        [CaseInput(accession_no="ACC-001", gold_standard_text="右下肺见磨玻璃结节")],
        cases_root,
    )

    case = resolution.cases[0]
    assert case.image_ref == DEFAULT_ITEM["image_ref"]
    assert case.study_uid == DEFAULT_ITEM["study_uid"]
    assert resolution.sources["ACC-001"] == "caseset"
    assert resolution.resolved_from_caseset == ("ACC-001",)
    assert case.needs_review is False


def test_unresolvable_reference_is_flagged_for_review_not_silently_continued(
    tmp_path: Path,
):
    """两处都取不到 —— 标 needs_review，不静默用空值继续。"""

    cases_root = tmp_path / "cases"
    cases_root.mkdir(parents=True)

    resolution = resolve_case_inputs(
        [CaseInput(accession_no="ACC-999", gold_standard_text="右下肺见磨玻璃结节")],
        cases_root,
    )

    case = resolution.cases[0]
    assert case.image_ref is None
    assert case.study_uid is None
    assert case.needs_review is True, "取不到引用必须挂人工复核"
    assert resolution.unresolved == ("ACC-999",)
    assert resolution.sources["ACC-999"] == "unresolved"


def test_assembled_case_without_image_ref_is_flagged(tmp_path: Path):
    """RIS 里那条没有 image_ref —— 兜底取不到，同样挂 needs_review。"""

    cases_root = tmp_path / "cases"
    stub = RISStub(items=[_item("ACC-001", image_ref="")])
    stub.start()
    try:
        assemble_caseset(
            _adapter(stub), cases_root, run_id="run-001", runs_dir=tmp_path / "runs"
        )
    finally:
        stub.stop()

    resolution = resolve_case_inputs(
        [CaseInput(accession_no="ACC-001", gold_standard_text="右下肺见磨玻璃结节")],
        cases_root,
    )

    assert resolution.cases[0].image_ref is None
    assert resolution.cases[0].needs_review is True


# --------------------------------------------------------------------------
# 4. 不静默丢例
# --------------------------------------------------------------------------


def test_accession_missing_from_ris_is_recorded_not_dropped(tmp_path: Path):
    """RIS 里没有的检查号 —— 进 unresolved，不静默丢弃。"""

    stub = RISStub(items=[_item("ACC-001")])
    stub.start()
    try:
        assembly = assemble_caseset(
            _adapter(stub),
            tmp_path / "cases",
            run_id="run-001",
            requested=["ACC-001", "ACC-404"],
            runs_dir=tmp_path / "runs",
        )
    finally:
        stub.stop()

    assert assembly.counts()["unresolved"] == 1
    assert assembly.unresolved == ("ACC-404",)
    assert not (tmp_path / "cases" / "ACC-404").exists()


def test_one_to_many_accession_is_not_auto_picked(tmp_path: Path):
    """同一检查号命中多条影像 —— 不自动取第一条，进 ambiguous。"""

    items = [
        _item("ACC-001", study_uid="STUDY-A", study_date="2026-03-01"),
        _item("ACC-001", study_uid="STUDY-B", study_date="2026-03-05"),
    ]
    stub = RISStub(items=items)
    stub.start()
    try:
        assembly = assemble_caseset(
            _adapter(stub), tmp_path / "cases", run_id="run-001", runs_dir=tmp_path / "runs"
        )
    finally:
        stub.stop()

    assert assembly.counts()["ambiguous"] == 1
    assert assembly.ambiguous == ("ACC-001",)
    assert assembly.counts()["assembled"] == 0
    assert not (tmp_path / "cases" / "ACC-001").exists(), "歧义例不自动取第一条落盘"


def test_same_accession_same_study_uid_is_one_case(tmp_path: Path):
    """同一影像的重复行 —— 算唯一绑定，正常装配。"""

    items = [
        _item("ACC-001", study_uid="STUDY-A"),
        _item("ACC-001", study_uid="STUDY-A"),
    ]
    stub = RISStub(items=items)
    stub.start()
    try:
        assembly = assemble_caseset(
            _adapter(stub), tmp_path / "cases", run_id="run-001", runs_dir=tmp_path / "runs"
        )
    finally:
        stub.stop()

    assert assembly.counts()["assembled"] == 1
    assert assembly.counts()["ambiguous"] == 0


# --------------------------------------------------------------------------
# 5. 路径安全
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_accession",
    ["../escape", "a/b", "..", ".", "with\\backslash"],
)
def test_illegal_accession_cannot_escape_the_cases_root(bad_accession: str, tmp_path: Path):
    """检查号是外部输入 —— 含路径分隔符就报错，不许写到病例集根之外。"""

    cases_root = tmp_path / "cases"
    with pytest.raises(NoduleEvalError) as excinfo:
        safe_accession_component(bad_accession)

    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert not (tmp_path / "escape").exists()
    assert not cases_root.exists() or list(cases_root.iterdir()) == []


def test_illegal_accession_in_request_is_rejected_before_any_request(tmp_path: Path):
    """非法检查号 —— 报错且不打 RIS。"""

    stub = RISStub(items=[_item("ACC-001")])
    stub.start()
    try:
        with pytest.raises(NoduleEvalError) as excinfo:
            assemble_caseset(
                _adapter(stub),
                tmp_path / "cases",
                run_id="run-001",
                requested=["../escape"],
                runs_dir=tmp_path / "runs",
            )
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert len(stub.requests) == 0


# --------------------------------------------------------------------------
# 6. 替身纪律与静态扫描
# --------------------------------------------------------------------------


def test_product_code_never_imports_the_stubs():
    """替身只在 tests/ 层 —— 产品代码不 import 任何替身模块。"""

    src_root = Path(__file__).resolve().parents[1] / "src"
    for path in src_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("import stub_", "from stub_", "import scenarios"):
            assert forbidden not in source, f"{path.name} 里不应出现 {forbidden}"


def test_caseset_module_never_points_at_a_local_endpoint():
    """装配层的出站地址只来自环境变量 —— 代码里没有本地回环兜底。"""

    source = (
        Path(__file__).resolve().parents[1] / "src" / "nodule_eval" / "caseset.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("localhost", "127.0.0.1", "example.com"):
        assert forbidden not in source, f"装配层里不应出现 {forbidden}"


# --------------------------------------------------------------------------
# 7. 命令行端到端
# --------------------------------------------------------------------------


def test_cli_assemble_writes_the_caseset_and_the_manifest(tmp_path, monkeypatch):
    """``cli assemble`` 真跑一遍：目录与装配清单都落盘。"""

    from nodule_eval.cli import main as cli_main

    stub = RISStub(items=[_item("ACC-001"), _item("ACC-002")])
    stub.start()
    try:
        env = make_env(
            RIS_BASE_URL=stub.base_url,
            IMAGING_EVAL_FIXTURE="1",
            IMAGING_EVAL_FIXTURE_BASE_URL=stub.base_url,
        )
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        code = cli_main(
            [
                "assemble",
                "--cases-root",
                str(tmp_path / "cases"),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--run-id",
                "cli-001",
            ]
        )
    finally:
        stub.stop()

    assert code == 0
    assert (tmp_path / "cases" / "ACC-001" / CASE_FILE_FILENAME).is_file()
    assert (tmp_path / "cases" / "ACC-002" / CASE_FILE_FILENAME).is_file()
    manifest = tmp_path / "runs" / "cli-001" / CASESET_MANIFEST_FILENAME
    assert manifest.is_file()
    assert json.loads(manifest.read_text(encoding="utf-8"))["counts"]["assembled"] == 2
