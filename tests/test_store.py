"""run store 的落盘纪律。

这一组断言回答四个问题：

1. **状态跨进程还在吗** —— 引擎进程退出后，另起一个进程能不能读回这一轮；
2. **重复写会不会写出两行** —— 同一 ``run_id`` 的同一检查号只能占一行，
   同一个 ``tool_call_id`` 只能占一条底账；
3. **并发写会不会写出半行** —— 事务边界外的读者读不到还没提交的行；
4. **产物目录是不是有固定约定** —— 六件套各归各位，别到时候找不到漏诊清单。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from stub_inference import InferenceStub

from nodule_eval import store as store_module
from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import (
    CASE_ROW_COLUMNS,
    VERDICT_FAILED,
    VERDICT_HIT,
    VERDICT_PENDING,
    CaseInput,
    CaseRow,
    EvalRequest,
)
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.store import (
    CASE_ROWS_FILENAME,
    DEFAULT_DB_FILENAME,
    RUN_ARTIFACT_FILENAMES,
    RunStore,
    case_status_for_row,
    default_store_path,
    resolve_store_path,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"


def _row(accession_no: str, *, verdict: str = VERDICT_PENDING, row_no: int = 1) -> CaseRow:
    return CaseRow(
        accession_no=accession_no,
        verdict=verdict,
        model_findings="右下肺 | 8.0mm | 磨玻璃 | 0.92",
        failure_reason="",
        tool_call_id=f"tc_{accession_no}",
        needs_review=False,
        normalize_source="model",
        row_no=row_no,
    )


def _store(tmp_path: Path, name: str = "store.sqlite3") -> RunStore:
    return RunStore.open(tmp_path / name, runs_dir=tmp_path / "runs")


# --------------------------------------------------------------------------
# 建表与基本读写
# --------------------------------------------------------------------------


def test_schema_has_run_and_case_tables(tmp_path: Path) -> None:
    """run / case / tool_call 三张表都在，列名与口径一致。"""

    store = _store(tmp_path)
    try:
        tables = {
            row["name"]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"run", "case_row", "tool_call"} <= tables

        run_columns = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(run)").fetchall()
        }
        assert {
            "run_id",
            "dataset_ref",
            "model_id",
            "model_version",
            "status",
            "created_at",
            "updated_at",
        } <= run_columns

        case_columns = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(case_row)").fetchall()
        }
        assert {
            "run_id",
            "accession_no",
            "status",
            "verdict",
            "tool_call_id",
            "error_code",
        } <= case_columns
    finally:
        store.close()


def test_upsert_run_preserves_created_at_and_status(tmp_path: Path) -> None:
    """重复写同一 run：行数不变、created_at 不变、status 不被普通写入偷改。"""

    store = _store(tmp_path)
    try:
        first = store.upsert_run(
            run_id="run-001",
            dataset_ref="本地样例",
            model_id="demo-model",
            model_version="v1.0.0",
            status="evaluated",
        )
        time.sleep(1.1)  # 让 updated_at 有机会与 created_at 不同
        second = store.upsert_run(
            run_id="run-001",
            dataset_ref="本地样例",
            model_id="demo-model",
            model_version="v1.0.0",
        )

        assert second.created_at == first.created_at, "首次落库时间不得被覆盖"
        assert second.status == "evaluated", "没传状态时不得把状态改回草稿"
        assert second.updated_at >= first.updated_at
        assert len(store.list_runs()) == 1
    finally:
        store.close()


def test_upsert_case_overwrites_instead_of_appending(tmp_path: Path) -> None:
    """同一 run 的同一检查号重复写 —— 只占一行，字段按主键覆盖。"""

    store = _store(tmp_path)
    try:
        store.upsert_case(
            run_id="run-001",
            accession_no="ACC-001",
            status="called",
            verdict=VERDICT_PENDING,
            tool_call_id="tc_1",
            row_no=1,
            row=_row("ACC-001"),
        )
        store.upsert_case(
            run_id="run-001",
            accession_no="ACC-001",
            status="failed",
            verdict=VERDICT_FAILED,
            tool_call_id="tc_2",
            error_code="INFERENCE_5XX",
            row_no=1,
            row=_row("ACC-001", verdict=VERDICT_FAILED),
        )

        assert store.count_cases("run-001") == 1, "重复写不得追加第二行"
        stored = store.get_case("run-001", "ACC-001")
        assert stored is not None
        assert stored.status == "failed"
        assert stored.verdict == VERDICT_FAILED
        assert stored.tool_call_id == "tc_2"
        assert stored.error_code == "INFERENCE_5XX"
    finally:
        store.close()


def test_case_rows_keep_table_row_order(tmp_path: Path) -> None:
    """逐例行按表格行号排序 —— 回填与复核都按这个顺序看。"""

    store = _store(tmp_path)
    try:
        for index, acc in enumerate(["ACC-C", "ACC-A", "ACC-B"], start=1):
            store.upsert_case(
                run_id="run-001",
                accession_no=acc,
                row_no=index,
                row=_row(acc, row_no=index),
            )
        assert [c.accession_no for c in store.list_cases("run-001")] == [
            "ACC-C",
            "ACC-A",
            "ACC-B",
        ]
    finally:
        store.close()


# --------------------------------------------------------------------------
# 跨进程可读 + 按 run_id 复算
# --------------------------------------------------------------------------


def test_state_is_readable_from_another_process(tmp_path: Path) -> None:
    """**跨进程**：引擎进程退出后，另一个 Python 进程能读回这一轮。"""

    store = _store(tmp_path)
    try:
        store.upsert_run(
            run_id="run-x",
            dataset_ref="本地样例",
            model_id="demo-model",
            model_version="v1.0.0",
            status="called",
        )
        store.upsert_case(
            run_id="run-x",
            accession_no="ACC-001",
            status="called",
            verdict=VERDICT_PENDING,
            tool_call_id="tc_x",
            row_no=1,
            row=_row("ACC-001"),
        )
        db_path = store.db_path
    finally:
        store.close()

    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(SRC_DIR)!r})\n"
        "from nodule_eval.store import RunStore\n"
        f"store = RunStore.open({str(db_path)!r}, runs_dir={str(tmp_path / 'runs')!r})\n"
        "run = store.get_run('run-x')\n"
        "cases = store.list_cases('run-x')\n"
        "print(json.dumps({\n"
        "    'status': run.status,\n"
        "    'model_id': run.model_id,\n"
        "    'accession': [c.accession_no for c in cases],\n"
        "    'verdict': [c.verdict for c in cases],\n"
        "}, ensure_ascii=False))\n"
        "store.close()\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["status"] == "called"
    assert payload["model_id"] == "demo-model"
    assert payload["accession"] == ["ACC-001"]
    assert payload["verdict"] == [VERDICT_PENDING]


def test_recompute_matches_artifact_rows(tmp_path: Path) -> None:
    """按 run_id 复算：库里取回的逐例行与产物 CSV 逐字一致。"""

    store = _store(tmp_path)
    try:
        rows = [_row("ACC-001", row_no=1), _row("ACC-002", row_no=2)]
        store.write_case_rows("run-001", rows)
        for row in rows:
            store.upsert_case(
                run_id="run-001",
                accession_no=row.accession_no,
                status=case_status_for_row(row),
                verdict=row.verdict,
                tool_call_id=row.tool_call_id,
                row_no=row.row_no,
                row=row,
            )

        recomputed = store.recompute("run-001")
        assert len(recomputed) == 2
        assert [r.accession_no for r in recomputed] == ["ACC-001", "ACC-002"]
        assert recomputed[0] == rows[0]
        assert recomputed[1] == rows[1]

        artifact = store.read_case_rows("run-001")
        assert len(artifact) == 2
        assert artifact[0]["accession_no"] == "ACC-001"
        assert list(artifact[0].keys()) == list(CASE_ROW_COLUMNS)
    finally:
        store.close()


def test_recompute_restores_the_runtime_marks(tmp_path: Path) -> None:
    """复算出来的行必须**带着运行时标记** —— 少一个就不是原来那一行。

    ``held`` / ``rule_degraded`` / ``judge_basis`` 这些标记决定「这一例算不算数、
    结论是谁给的、凭什么这么判」。快照少还原一个，复算出来的行就跟当初判定的那一行
    不是同一回事：降级例看不出是降级例，写回科研平台的逐例行也跟着失真。
    """

    store = _store(tmp_path)
    try:
        row = CaseRow(
            accession_no="ACC-DEGRADED",
            verdict=VERDICT_HIT,
            model_findings="右下肺 | 8.0mm | 磨玻璃",
            tool_call_id="tc_acc_degraded",
            needs_review=True,
            normalize_source="rule",
            row_no=1,
            error_code=ErrorCode.RULE_DEGRADED.value,
            rule_degraded=True,
            degrade_reason="模型归并未成功，已降级为规则归并",
            judge_basis="存在性一致",
            uncovered_labels="密度",
            incomparable_reason="",
            gold_summary="右肺下叶 | 8.0mm | 磨玻璃",
        )
        store.upsert_case(
            run_id="run-marks",
            accession_no=row.accession_no,
            status=case_status_for_row(row),
            verdict=row.verdict,
            tool_call_id=row.tool_call_id,
            error_code=row.error_code,
            row_no=row.row_no,
            row=row,
        )

        recomputed = store.recompute("run-marks")
        assert len(recomputed) == 1
        assert recomputed[0] == row, "复算出来的行必须与落库时那一行逐字段相同"
        assert recomputed[0].rule_degraded is True
        assert recomputed[0].degrade_reason
        assert recomputed[0].judge_basis == "存在性一致"
        assert recomputed[0].uncovered_labels == "密度"
        assert recomputed[0].gold_summary
    finally:
        store.close()


# --------------------------------------------------------------------------
# 幂等：同一 tool_call_id 不重复追加
# --------------------------------------------------------------------------


def test_same_tool_call_id_is_not_appended_twice(tmp_path: Path) -> None:
    """同一 tool_call_id 只落一条底账 —— 重复的底账比没有底账更难核对。"""

    store = _store(tmp_path)
    try:
        first = store.append_tool_call(
            "run-001",
            tool_call_id="tc_001",
            name="demo-model.detect",
            args={"study_uid": "S-1"},
            content="{}",
        )
        second = store.append_tool_call(
            "run-001",
            tool_call_id="tc_001",
            name="demo-model.detect",
            args={"study_uid": "S-1"},
            content="{}",
        )

        assert first is True
        assert second is False, "已登记的调用不得再追加一行"
        assert store.count_tool_calls("run-001") == 1
        assert store.has_tool_call("run-001", "tc_001") is True

        ledger = (tmp_path / "runs" / "run-001" / "tool_calls.jsonl").read_text(
            encoding="utf-8"
        )
        lines = [line for line in ledger.splitlines() if line.strip()]
        assert len(lines) == 1, "底账里只能有一行"
        assert json.loads(lines[0])["tool_call_id"] == "tc_001"
    finally:
        store.close()


# --------------------------------------------------------------------------
# 并发安全
# --------------------------------------------------------------------------


def test_wal_mode_is_enabled(tmp_path: Path) -> None:
    """开 WAL —— 读写不互相阻塞，跑批时另开一个终端查状态不会把库锁死。"""

    store = _store(tmp_path)
    try:
        assert store.journal_mode() == "wal"
        assert store.busy_timeout() > 0
    finally:
        store.close()


def test_uncommitted_row_is_not_visible_outside_the_transaction(tmp_path: Path) -> None:
    """事务边界外读不到还没提交的行 —— 不出现「半行状态」。"""

    db_path = tmp_path / "shared.sqlite3"
    writer = RunStore.open(db_path, runs_dir=tmp_path / "runs")
    reader = RunStore.open(db_path, runs_dir=tmp_path / "runs")
    try:
        with writer.write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO case_row (run_id, accession_no, status, verdict,
                                      tool_call_id, error_code, row_no, row_json, updated_at)
                VALUES ('run-001', 'ACC-001', 'called', 'pending', '', '', 1, '', 'now')
                """
            )
            # 另一个连接此刻读不到这一行
            assert reader.get_case("run-001", "ACC-001") is None
        # 提交后立刻可见
        assert reader.get_case("run-001", "ACC-001") is not None
    finally:
        writer.close()
        reader.close()


def test_concurrent_writes_keep_every_row(tmp_path: Path) -> None:
    """多进程式并发写：每个连接各写各的，一行都不丢、一行都不残。"""

    db_path = tmp_path / "concurrent.sqlite3"
    runs_dir = tmp_path / "runs"
    workers = 4
    per_worker = 25

    def worker(index: int) -> None:
        store = RunStore.open(db_path, runs_dir=runs_dir)
        try:
            for n in range(per_worker):
                acc = f"ACC-{index}-{n}"
                store.upsert_case(
                    run_id="run-concurrent",
                    accession_no=acc,
                    status="called",
                    verdict=VERDICT_PENDING,
                    tool_call_id=f"tc_{acc}",
                    row_no=index * per_worker + n,
                    row=_row(acc, row_no=index * per_worker + n),
                )
        finally:
            store.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    store = RunStore.open(db_path, runs_dir=runs_dir)
    try:
        assert store.count_cases("run-concurrent") == workers * per_worker
        cases = store.list_cases("run-concurrent")
        assert len(cases) == workers * per_worker
        # 每一行都是完整的一行（有检查号、有调用 id、有判定），不是半行
        for case in cases:
            assert case.accession_no.startswith("ACC-")
            assert case.tool_call_id.startswith("tc_")
            assert case.verdict == VERDICT_PENDING
            assert case.row is not None
            assert case.row.accession_no == case.accession_no
    finally:
        store.close()


def test_lock_conflict_is_retried_then_reported(tmp_path: Path, monkeypatch) -> None:
    """库被占着时按退避重试；重试用尽仍拿不到锁，**指名报错**而不是静默丢写。"""

    store = RunStore.open(
        tmp_path / "locked.sqlite3", runs_dir=tmp_path / "runs", busy_timeout_ms=50, lock_retries=2
    )
    blocker = RunStore.open(
        tmp_path / "locked.sqlite3", runs_dir=tmp_path / "runs", busy_timeout_ms=50
    )
    sleeps: list[float] = []
    monkeypatch.setattr(store_module.time, "sleep", lambda s: sleeps.append(s))

    try:
        blocker._conn.execute("BEGIN EXCLUSIVE")
        with pytest.raises(NoduleEvalError) as excinfo:
            store.upsert_case(run_id="run-001", accession_no="ACC-001")
        blocker._conn.execute("ROLLBACK")

        assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
        assert "存储库忙" in excinfo.value.message
        assert len(sleeps) >= 1, "锁冲突必须先退避重试，不能一次就放弃"
    finally:
        store.close()
        blocker.close()


def test_lock_released_mid_retry_lets_the_write_through(tmp_path: Path) -> None:
    """锁很快被放开时，写入应当重试成功，而不是整轮失败。"""

    db_path = tmp_path / "transient.sqlite3"
    store = RunStore.open(db_path, runs_dir=tmp_path / "runs", busy_timeout_ms=30, lock_retries=6)
    # 阻塞连接要能被另一个线程回滚（释放锁），所以关掉 sqlite3 的线程检查。
    blocker = sqlite3.connect(str(db_path), timeout=5, check_same_thread=False)

    def release_later() -> None:
        time.sleep(0.25)
        blocker.execute("ROLLBACK")

    try:
        blocker.execute("BEGIN EXCLUSIVE")
        releaser = threading.Thread(target=release_later)
        releaser.start()
        store.upsert_case(run_id="run-001", accession_no="ACC-001", status="called")
        releaser.join()
        assert store.count_cases("run-001") == 1
    finally:
        store.close()
        blocker.close()


# --------------------------------------------------------------------------
# 产物目录约定
# --------------------------------------------------------------------------


def test_artifact_directory_convention(tmp_path: Path) -> None:
    """一轮的产物落在 ``runs/<run_id>/`` 下，文件名是固定那六件套。"""

    store = _store(tmp_path)
    try:
        assert store.run_dir("run-001") == tmp_path / "runs" / "run-001"
        for filename in RUN_ARTIFACT_FILENAMES:
            assert store.artifact_path("run-001", filename) == (
                tmp_path / "runs" / "run-001" / filename
            )
        store.write_case_rows("run-001", [_row("ACC-001")])
        store.write_json_artifact("run-001", "summary.json", {"total_cases": 1})
        assert "case_rows.csv" in store.list_artifacts("run-001")
        assert "summary.json" in store.list_artifacts("run-001")
        assert store.read_json_artifact("run-001", "summary.json") == {"total_cases": 1}
    finally:
        store.close()


def test_store_path_resolution(tmp_path: Path) -> None:
    """``--store`` 给目录就补文件名，给文件就用文件；不猜、不回落。"""

    assert resolve_store_path(tmp_path / "x.sqlite3") == tmp_path / "x.sqlite3"
    assert resolve_store_path(tmp_path) == tmp_path / DEFAULT_DB_FILENAME
    assert resolve_store_path(None) == default_store_path()
    assert default_store_path().name == DEFAULT_DB_FILENAME


def test_unopenable_store_path_is_reported(tmp_path: Path) -> None:
    """路径打不开时指名报错，不静默换地方。"""

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(NoduleEvalError) as excinfo:
        RunStore.open(blocker / "sub" / "store.sqlite3")
    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert "存储库打不开" in excinfo.value.message


# --------------------------------------------------------------------------
# 引擎挂上 run store 之后
# --------------------------------------------------------------------------


def test_engine_persists_run_and_cases_into_the_store(tmp_path: Path, full_env, monkeypatch) -> None:
    """引擎挂了 run store 之后，这一轮的 run 与逐例行都落进 SQLite。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        config = load_config()
        spec = load_model_spec("demo-model")
        store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
        engine = Engine(
            config,
            spec,
            client=HttpClient(sleep=lambda _s: None),
            runs_dir=tmp_path / "runs",
            store=store,
        )
        request = EvalRequest(
            run_id="engine-001",
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="本地样例",
            cases=(
                CaseInput(
                    accession_no="ACC-001",
                    gold_standard_text="右下肺见磨玻璃结节，约8mm",
                    image_ref="dx://local/001",
                    study_uid="STUDY-001",
                    row_no=1,
                ),
            ),
        )
        response = engine.run(request)

        assert response.status == "evaluated"
        run_row = store.get_run("engine-001")
        assert run_row is not None
        assert run_row.model_id == "demo-model"
        assert run_row.dataset_ref == "本地样例"
        assert run_row.status == "evaluated"

        assert store.count_cases("engine-001") == 1
        case_row = store.get_case("engine-001", "ACC-001")
        assert case_row is not None
        assert case_row.tool_call_id.startswith("tc_")
        # 金标准说「右下肺见磨玻璃结节」，替身回一个候选 → 存在性一致 → 命中
        assert case_row.verdict == VERDICT_HIT

        # 库里的逐例行与产物 CSV 对得上
        recomputed = store.recompute("engine-001")
        artifact = store.read_case_rows("engine-001")
        assert [r.accession_no for r in recomputed] == [r["accession_no"] for r in artifact]
        store.close()
    finally:
        stub.stop()


def test_engine_rerun_keeps_one_row_per_case(tmp_path: Path, full_env, monkeypatch) -> None:
    """同一 run_id 重跑：库里仍是每例一行（幂等落盘）。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        config = load_config()
        spec = load_model_spec("demo-model")
        store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
        engine = Engine(
            config,
            spec,
            client=HttpClient(sleep=lambda _s: None),
            runs_dir=tmp_path / "runs",
            store=store,
        )
        request = EvalRequest(
            run_id="engine-002",
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="本地样例",
            cases=(
                CaseInput(
                    accession_no="ACC-001",
                    gold_standard_text="右下肺见磨玻璃结节，约8mm",
                    image_ref="dx://local/001",
                    study_uid="STUDY-001",
                    row_no=1,
                ),
            ),
        )
        engine.run(request)
        engine.run(request)
        assert store.count_cases("engine-002") == 1
        assert len(store.list_runs()) == 1
        store.close()
    finally:
        stub.stop()


def test_failed_case_carries_error_code_into_the_store(
    tmp_path: Path, full_env, monkeypatch
) -> None:
    """调用失败的例：错误码落库、状态落 failed，不与挂起混为一谈。"""

    stub = InferenceStub(status_sequence=[500, 500, 500])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        config = load_config()
        spec = load_model_spec("demo-model")
        store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
        engine = Engine(
            config,
            spec,
            client=HttpClient(sleep=lambda _s: None),
            runs_dir=tmp_path / "runs",
            store=store,
        )
        request = EvalRequest(
            run_id="engine-003",
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="本地样例",
            cases=(
                CaseInput(
                    accession_no="ACC-ERR",
                    gold_standard_text="右下肺见磨玻璃结节，约8mm",
                    image_ref="dx://local/err",
                    study_uid="STUDY-ERR",
                    row_no=1,
                ),
            ),
        )
        engine.run(request)
        case_row = store.get_case("engine-003", "ACC-ERR")
        assert case_row is not None
        assert case_row.verdict == VERDICT_FAILED
        assert case_row.status == "failed"
        assert case_row.error_code == "INFERENCE_5XX"
        store.close()
    finally:
        stub.stop()


def test_engine_ledger_and_store_index_stay_in_sync(
    tmp_path: Path, full_env, monkeypatch
) -> None:
    """引擎记的调用底账与库里的索引一一对应 —— 反查时两边都能找到。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        config = load_config()
        spec = load_model_spec("demo-model")
        store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
        engine = Engine(
            config,
            spec,
            client=HttpClient(sleep=lambda _s: None),
            runs_dir=tmp_path / "runs",
            store=store,
        )
        request = EvalRequest(
            run_id="engine-004",
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="本地样例",
            cases=(
                CaseInput(
                    accession_no="ACC-001",
                    gold_standard_text="右下肺见磨玻璃结节，约8mm",
                    image_ref="dx://local/001",
                    study_uid="STUDY-001",
                    row_no=1,
                ),
            ),
        )
        engine.run(request)

        ledger_lines = [
            json.loads(line)
            for line in (tmp_path / "runs" / "engine-004" / "tool_calls.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        assert len(ledger_lines) == 1
        call_id = ledger_lines[0]["tool_call_id"]
        assert store.has_tool_call("engine-004", call_id) is True
        assert store.count_tool_calls("engine-004") == 1
        assert store.get_case("engine-004", "ACC-001").tool_call_id == call_id
        store.close()
    finally:
        stub.stop()


def test_engine_rejects_mismatched_artifact_directory(tmp_path: Path, full_env) -> None:
    """引擎的产物目录与运行库的产物目录不一致时指名报错，不让两边分家。"""

    config = load_config()
    spec = load_model_spec("demo-model")
    store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
    try:
        with pytest.raises(NoduleEvalError) as excinfo:
            Engine(
                config,
                spec,
                runs_dir=tmp_path / "other-runs",
                store=store,
            )
        assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
        assert "产物目录" in excinfo.value.message
    finally:
        store.close()


def test_cli_accepts_store_option(tmp_path: Path, full_env, monkeypatch, capsys) -> None:
    """``--store`` 指定运行库时，引擎的 run 与逐例行落在那个库里。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        from nodule_eval import cli

        cases_file = PROJECT_ROOT / "tests" / "fixtures" / "cases" / "single_case.json"
        store_path = tmp_path / "cli-store" / "nodule_eval.sqlite3"
        exit_code = cli.main(
            [
                "run",
                "--cases",
                str(cases_file),
                "--model-id",
                "demo-model",
                "--run-id",
                "cli-store-001",
                "--dataset-ref",
                "本地样例",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--store",
                str(store_path),
            ]
        )
        assert exit_code == 0
        assert store_path.is_file(), "--store 指定的运行库应当被创建"

        store = RunStore.open(store_path, runs_dir=tmp_path / "runs")
        try:
            assert store.get_run("cli-store-001") is not None
            assert store.count_cases("cli-store-001") == 1
        finally:
            store.close()

        printed = capsys.readouterr().out
        assert "运行库" in printed
    finally:
        stub.stop()
