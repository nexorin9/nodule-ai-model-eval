"""run / case 双状态机。

这一组断言回答四个问题：

1. **合法迁移都放行吗** —— 表上写着的边，一条都不该被误拒；
2. **非法迁移都拦得住吗** —— 跳步的、往回退的、**绕过人工复核的**，
   一个都不该放过去；
3. **人工确认层拦得住吗** —— 引擎把 run 往 ``reviewed`` / ``submitted`` 推，
   必须被拒（这条写在代码里，不是口头约定）；
4. **每次迁移留痕吗** —— 从哪到哪、谁推的、为什么推、什么时候推的，
   事后能对着 ``transitions.jsonl`` 逐条回放。

**为什么这一层要单独做**：状态机是「这一轮到底算不算数」的边界。
漏放一条边 = 未复核的结论可能被当成已生效事实；错拦一条边 = 一轮两百例
白跑。宁可写死，也不临场判断。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from stub_inference import InferenceStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import (
    VERDICT_FAILED,
    VERDICT_HIT,
    VERDICT_PENDING,
    CaseInput,
    CaseRow,
    EvalRequest,
    partition_rows,
)
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.store import (
    ACTOR_ENGINE,
    ACTOR_HUMAN_REVIEW,
    ACTOR_HUMAN_SUBMIT,
    CASE_STATUS_CALLED,
    CASE_STATUS_FAILED,
    CASE_STATUS_JUDGED,
    CASE_STATUS_NEEDS_REVIEW,
    CASE_STATUS_NORMALIZED,
    CASE_STATUS_PENDING,
    CASE_TRANSITIONS,
    HUMAN_ACTORS,
    RUN_STATUS_ASSEMBLED,
    RUN_STATUS_CALLED,
    RUN_STATUS_DRAFT,
    RUN_STATUS_EVALUATED,
    RUN_STATUS_FAILED,
    RUN_STATUS_REVIEWED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUBMITTED,
    RUN_TRANSITIONS,
    RunStore,
    case_status_for_row,
    is_legal_case_transition,
    is_legal_run_transition,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

#: run 状态机的 6 条推进边（草稿 → 装配 → 运行 → 调用 → 判定 → 复核 → 提交）。
RUN_LEGAL_EDGES: tuple[tuple[str, str], ...] = (
    (RUN_STATUS_DRAFT, RUN_STATUS_ASSEMBLED),
    (RUN_STATUS_ASSEMBLED, RUN_STATUS_RUNNING),
    (RUN_STATUS_RUNNING, RUN_STATUS_CALLED),
    (RUN_STATUS_CALLED, RUN_STATUS_EVALUATED),
    (RUN_STATUS_EVALUATED, RUN_STATUS_REVIEWED),
    (RUN_STATUS_REVIEWED, RUN_STATUS_SUBMITTED),
)

#: case 状态机的 4 条推进边。
CASE_LEGAL_EDGES: tuple[tuple[str, str], ...] = (
    (CASE_STATUS_PENDING, CASE_STATUS_CALLED),
    (CASE_STATUS_CALLED, CASE_STATUS_NORMALIZED),
    (CASE_STATUS_NORMALIZED, CASE_STATUS_JUDGED),
    (CASE_STATUS_NEEDS_REVIEW, CASE_STATUS_CALLED),
)


def _store(tmp_path: Path) -> RunStore:
    return RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")


def _seed_run(store: RunStore, run_id: str = "run-001", status: str = RUN_STATUS_DRAFT) -> None:
    """先落一行 run（状态由调用方指定）。"""

    store.upsert_run(
        run_id=run_id,
        dataset_ref="本地样例",
        model_id="demo-model",
        model_version="v1.0.0",
        status=status,
    )


def _seed_case(
    store: RunStore,
    run_id: str = "run-001",
    accession_no: str = "ACC-001",
    status: str = CASE_STATUS_PENDING,
) -> None:
    store.upsert_case(
        run_id=run_id,
        accession_no=accession_no,
        status=status,
        verdict=VERDICT_PENDING,
        tool_call_id="tc_001",
        row_no=1,
    )


# --------------------------------------------------------------------------
# 合法迁移：表上的边一条都不该被误拒
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("current", "target"), RUN_LEGAL_EDGES)
def test_every_legal_run_edge_is_allowed(
    tmp_path: Path, current: str, target: str
) -> None:
    """run 的 6 条推进边逐条走一遍 —— 都放行，状态确实落到目标上。"""

    store = _store(tmp_path)
    try:
        _seed_run(store, status=current)
        # ``reviewed`` / ``submitted`` 是人工确认层，必须由人工触发方推。
        actor = ACTOR_HUMAN_SUBMIT if target == RUN_STATUS_SUBMITTED else ACTOR_HUMAN_REVIEW
        updated = store.transition_run(
            "run-001", target, actor=actor, reason="单测推进"
        )
        assert updated.status == target
        assert is_legal_run_transition(current, target) is True
    finally:
        store.close()


@pytest.mark.parametrize(("current", "target"), CASE_LEGAL_EDGES)
def test_every_legal_case_edge_is_allowed(
    tmp_path: Path, current: str, target: str
) -> None:
    """case 的 4 条推进边逐条走一遍 —— 都放行。"""

    store = _store(tmp_path)
    try:
        _seed_run(store)
        _seed_case(store, status=current)
        updated = store.transition_case(
            "run-001", "ACC-001", target, actor=ACTOR_ENGINE, reason="单测推进"
        )
        assert updated.status == target
        assert is_legal_case_transition(current, target) is True
    finally:
        store.close()


def test_failed_edge_is_allowed_from_every_non_terminal_run_state(
    tmp_path: Path,
) -> None:
    """整轮失败：从任何一个未终结的状态都能落到 ``failed``。"""

    store = _store(tmp_path)
    try:
        for index, status in enumerate(
            (
                RUN_STATUS_DRAFT,
                RUN_STATUS_ASSEMBLED,
                RUN_STATUS_RUNNING,
                RUN_STATUS_CALLED,
                RUN_STATUS_EVALUATED,
            )
        ):
            run_id = f"run-{index}"
            _seed_run(store, run_id=run_id, status=status)
            updated = store.transition_run(
                run_id, RUN_STATUS_FAILED, actor=ACTOR_ENGINE, reason="整轮失败"
            )
            assert updated.status == RUN_STATUS_FAILED
        # 已提交的不再能落 failed —— 已生效事实不能被机器推翻
        _seed_run(store, run_id="run-submitted", status=RUN_STATUS_SUBMITTED)
        with pytest.raises(NoduleEvalError) as excinfo:
            store.transition_run(
                "run-submitted",
                RUN_STATUS_FAILED,
                actor=ACTOR_ENGINE,
                reason="试图推翻已提交的结论",
            )
        assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION
    finally:
        store.close()


# --------------------------------------------------------------------------
# 非法迁移：跳步的、往回退的、绕过人工复核的，一律拒
# --------------------------------------------------------------------------


def test_evaluated_cannot_jump_straight_to_submitted(tmp_path: Path) -> None:
    """**未经人工复核直跳已提交** —— 必须被拒。

    这是本状态机最要紧的一条边：``submitted`` 是「已写回科研平台、
    已生效事实」的层，没���人点过「确认提交」就跳进去，等于把没看过的
    结论当成定论发出去。
    """

    store = _store(tmp_path)
    try:
        _seed_run(store, status=RUN_STATUS_EVALUATED)
        with pytest.raises(NoduleEvalError) as excinfo:
            store.transition_run(
                "run-001",
                RUN_STATUS_SUBMITTED,
                actor=ACTOR_HUMAN_SUBMIT,
                reason="未经复核直跳已提交",
            )
        assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION
        assert "不能从 evaluated 跳到 submitted" in excinfo.value.message
        assert "reviewed" in excinfo.value.message, "文案要点出必须先经哪一格"

        # 状态没有被改坏
        assert store.get_run("run-001").status == RUN_STATUS_EVALUATED
        # 也没留下迁移记录 —— 被拒的迁移不留痕
        assert store.list_transitions("run-001") == ()
    finally:
        store.close()


def test_submitted_run_cannot_be_changed_again(tmp_path: Path) -> None:
    """已提交的 run 不许再改判 —— 终态就是终态。"""

    store = _store(tmp_path)
    try:
        _seed_run(store, status=RUN_STATUS_SUBMITTED)
        for target in (RUN_STATUS_REVIEWED, RUN_STATUS_EVALUATED, RUN_STATUS_RUNNING):
            with pytest.raises(NoduleEvalError) as excinfo:
                store.transition_run(
                    "run-001",
                    target,
                    actor=ACTOR_HUMAN_REVIEW,
                    reason="试图改判已提交的结论",
                )
            assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION
        assert store.get_run("run-001").status == RUN_STATUS_SUBMITTED
    finally:
        store.close()


def test_case_cannot_jump_from_pending_to_judged(tmp_path: Path) -> None:
    """**case 从待处理直跳已判定** —— 必须被拒。

    没调推理端点、没归并金标准就出判定，等于凭空造结论。
    """

    store = _store(tmp_path)
    try:
        _seed_run(store)
        _seed_case(store, status=CASE_STATUS_PENDING)
        with pytest.raises(NoduleEvalError) as excinfo:
            store.transition_case(
                "run-001",
                "ACC-001",
                CASE_STATUS_JUDGED,
                actor=ACTOR_ENGINE,
                reason="跳步判定",
            )
        assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION
        assert "不能从 pending 跳到 judged" in excinfo.value.message
        assert store.get_case("run-001", "ACC-001").status == CASE_STATUS_PENDING
    finally:
        store.close()


def test_draft_run_cannot_skip_assembly(tmp_path: Path) -> None:
    """草稿不能直接开跑 —— 必须先过「已装配」这一格。"""

    store = _store(tmp_path)
    try:
        _seed_run(store, status=RUN_STATUS_DRAFT)
        with pytest.raises(NoduleEvalError) as excinfo:
            store.transition_run(
                "run-001", RUN_STATUS_RUNNING, actor=ACTOR_ENGINE, reason="跳步开跑"
            )
        assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION
        assert "不能从 draft 跳到 running" in excinfo.value.message
    finally:
        store.close()


def test_same_state_transition_is_rejected(tmp_path: Path) -> None:
    """「已是该状态」不是一次迁移 —— 拒掉，别把幂等写进状态机。"""

    store = _store(tmp_path)
    try:
        _seed_run(store, status=RUN_STATUS_EVALUATED)
        with pytest.raises(NoduleEvalError) as excinfo:
            store.transition_run(
                "run-001",
                RUN_STATUS_EVALUATED,
                actor=ACTOR_ENGINE,
                reason="重复推进",
            )
        assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION
        assert "已是 evaluated 状态" in excinfo.value.message
    finally:
        store.close()


def test_transition_on_missing_subject_is_rejected(tmp_path: Path) -> None:
    """run 或 case 压根不在库里时，迁移要指名报错，不能凭空造出一行。"""

    store = _store(tmp_path)
    try:
        with pytest.raises(NoduleEvalError) as excinfo:
            store.transition_run(
                "run-nope", RUN_STATUS_ASSEMBLED, actor=ACTOR_ENGINE, reason="推进不存在的 run"
            )
        assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION
        assert "尚未落库" in excinfo.value.message

        _seed_run(store, run_id="run-001")
        with pytest.raises(NoduleEvalError) as excinfo:
            store.transition_case(
                "run-001",
                "ACC-NOPE",
                CASE_STATUS_CALLED,
                actor=ACTOR_ENGINE,
                reason="推进不存在的例",
            )
        assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION
        assert "ACC-NOPE" in excinfo.value.message
    finally:
        store.close()


# --------------------------------------------------------------------------
# 人工确认层：引擎推不进去
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "target"),
    [
        (RUN_STATUS_EVALUATED, RUN_STATUS_REVIEWED),
        (RUN_STATUS_REVIEWED, RUN_STATUS_SUBMITTED),
    ],
)
def test_engine_cannot_advance_into_the_human_confirmation_layer(
    tmp_path: Path, status: str, target: str
) -> None:
    """**只有显式的人工复核 / 确认提交能推进人工确认层**。

    引擎自己跑完一轮，状态最高只到 ``evaluated``；往 ``reviewed`` /
    ``submitted`` 推必须带人工触发方 —— 这条拦在 store 里，引擎绕不开。
    """

    store = _store(tmp_path)
    try:
        _seed_run(store, status=status)
        with pytest.raises(NoduleEvalError) as excinfo:
            store.transition_run(
                "run-001", target, actor=ACTOR_ENGINE, reason="引擎自推"
            )
        assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION
        assert "只能由人工复核" in excinfo.value.message
        assert "engine" in excinfo.value.message, "文案要点出是谁在推"
        assert store.get_run("run-001").status == status
    finally:
        store.close()


def test_review_and_submit_require_a_human_actor(tmp_path: Path) -> None:
    """``review_run`` / ``submit_run`` 这两个口子只认人工触发方。"""

    store = _store(tmp_path)
    try:
        _seed_run(store, status=RUN_STATUS_EVALUATED)
        with pytest.raises(NoduleEvalError):
            store.review_run("run-001", actor=ACTOR_ENGINE, reason="引擎冒充人工")

        reviewed = store.review_run("run-001", reason="放射科复核完毕")
        assert reviewed.status == RUN_STATUS_REVIEWED

        with pytest.raises(NoduleEvalError):
            store.submit_run("run-001", actor=ACTOR_ENGINE, reason="引擎冒充人工")

        submitted = store.submit_run("run-001", reason="信息科确认提交")
        assert submitted.status == RUN_STATUS_SUBMITTED
        assert ACTOR_HUMAN_REVIEW in HUMAN_ACTORS
        assert ACTOR_HUMAN_SUBMIT in HUMAN_ACTORS
    finally:
        store.close()


# --------------------------------------------------------------------------
# 迁移留痕
# --------------------------------------------------------------------------


def test_every_transition_is_recorded_with_from_to_actor_reason(tmp_path: Path) -> None:
    """每次迁移都留痕：从哪到哪、谁推的、为什么推、什么时候推的。"""

    store = _store(tmp_path)
    try:
        _seed_run(store)
        _seed_case(store)
        store.transition_run(
            "run-001", RUN_STATUS_ASSEMBLED, actor="assemble", reason="病例集已就绪"
        )
        store.transition_case(
            "run-001",
            "ACC-001",
            CASE_STATUS_CALLED,
            actor=ACTOR_ENGINE,
            reason="推理端点已返回",
        )

        transitions = store.list_transitions("run-001")
        assert len(transitions) == 2
        run_move, case_move = transitions

        assert run_move.scope == "run"
        assert run_move.subject == "run-001"
        assert run_move.from_state == RUN_STATUS_DRAFT
        assert run_move.to_state == RUN_STATUS_ASSEMBLED
        assert run_move.actor == "assemble"
        assert run_move.reason == "病例集已就绪"
        assert run_move.at

        assert case_move.scope == "case"
        assert case_move.subject == "ACC-001"
        assert case_move.from_state == CASE_STATUS_PENDING
        assert case_move.to_state == CASE_STATUS_CALLED
        assert case_move.actor == ACTOR_ENGINE

        # 按作用域 / 对象筛
        assert len(store.list_transitions("run-001", scope="run")) == 1
        assert len(store.list_transitions("run-001", scope="case")) == 1
        assert store.list_transitions("run-001", subject="ACC-001") == (case_move,)

        # 同一份底账也落在产物目录里（人看得见的那一份）
        ledger = tmp_path / "runs" / "run-001" / "transitions.jsonl"
        rows = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == 2
        assert rows[0]["from"] == RUN_STATUS_DRAFT
        assert rows[0]["to"] == RUN_STATUS_ASSEMBLED
        assert rows[1]["subject"] == "ACC-001"
    finally:
        store.close()


def test_rejected_transition_leaves_no_trace(tmp_path: Path) -> None:
    """被拒的迁移**不留痕** —— 底账里出现的每一条都是真发生过的。"""

    store = _store(tmp_path)
    try:
        _seed_run(store, status=RUN_STATUS_EVALUATED)
        with pytest.raises(NoduleEvalError):
            store.transition_run(
                "run-001", RUN_STATUS_SUBMITTED, actor=ACTOR_HUMAN_SUBMIT, reason="越权"
            )
        assert store.list_transitions("run-001") == ()
        ledger = tmp_path / "runs" / "run-001" / "transitions.jsonl"
        assert not ledger.exists() or ledger.read_text(encoding="utf-8").strip() == ""
    finally:
        store.close()


def test_transition_log_is_readable_from_another_process(tmp_path: Path) -> None:
    """**跨进程**：另一个 Python 进程能读回这一轮的迁移记录。"""

    store = _store(tmp_path)
    try:
        _seed_run(store)
        store.transition_run(
            "run-001", RUN_STATUS_ASSEMBLED, actor="assemble", reason="病例集已就绪"
        )
        db_path = store.db_path
        runs_dir = tmp_path / "runs"
    finally:
        store.close()

    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(SRC_DIR)!r})\n"
        "from nodule_eval.store import RunStore\n"
        f"store = RunStore.open({str(db_path)!r}, runs_dir={str(runs_dir)!r})\n"
        "rows = store.list_transitions('run-001')\n"
        "print(json.dumps([{'from': r.from_state, 'to': r.to_state} for r in rows], ensure_ascii=False))\n"
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
    assert payload == [{"from": RUN_STATUS_DRAFT, "to": RUN_STATUS_ASSEMBLED}]


# --------------------------------------------------------------------------
# 引擎接入：状态推进走状态机，且降级路径不推进 run 状态
# --------------------------------------------------------------------------


def test_engine_run_advances_the_run_state_legally(
    tmp_path: Path, full_env, monkeypatch
) -> None:
    """引擎跑一轮：run 状态按 草稿 → 装配 → 运行 → 调用 → 判定 走完，每步都留痕。

    **任务 14 起**：fan-out 循环里每一例都走完「调用 → 归并 → 判定」，
    循环退出后 run 推进到 ``evaluated``（判定已出，仍是草稿层）。
    """

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
            run_id="engine-sm",
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

        assert store.get_run("engine-sm").status == RUN_STATUS_EVALUATED

        run_moves = store.list_transitions("engine-sm", scope="run")
        assert [(m.from_state, m.to_state) for m in run_moves] == [
            (RUN_STATUS_DRAFT, RUN_STATUS_ASSEMBLED),
            (RUN_STATUS_ASSEMBLED, RUN_STATUS_RUNNING),
            (RUN_STATUS_RUNNING, RUN_STATUS_CALLED),
            (RUN_STATUS_CALLED, RUN_STATUS_EVALUATED),
        ]
        assert [m.actor for m in run_moves] == [
            "assemble",
            ACTOR_ENGINE,
            ACTOR_ENGINE,
            ACTOR_ENGINE,
        ]

        # 每一例在 fan-out 里走完 调用 → 归并 → 判定 三步，每步都留痕
        case_moves = store.list_transitions("engine-sm", scope="case")
        assert [(m.from_state, m.to_state) for m in case_moves] == [
            (CASE_STATUS_PENDING, CASE_STATUS_CALLED),
            (CASE_STATUS_CALLED, CASE_STATUS_NORMALIZED),
            (CASE_STATUS_NORMALIZED, CASE_STATUS_JUDGED),
        ]
        assert store.get_case("engine-sm", "ACC-001").status == CASE_STATUS_JUDGED

        # 引擎跑到这一步为止 —— 人工确认层没被碰过
        assert all(
            m.to_state not in (RUN_STATUS_REVIEWED, RUN_STATUS_SUBMITTED)
            for m in store.list_transitions("engine-sm")
        )
        store.close()
    finally:
        stub.stop()


def test_engine_cannot_rerun_an_already_reviewed_run(
    tmp_path: Path, full_env, monkeypatch
) -> None:
    """已复核的 run 不能被引擎直接重跑 —— 要重评请换 run_id。"""

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
            run_id="engine-locked",
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
        # 引擎自己把 run 推到 evaluated（判定已出）；再由人工复核往下推
        assert store.get_run("engine-locked").status == RUN_STATUS_EVALUATED
        store.review_run("engine-locked", reason="放射科复核完毕")

        with pytest.raises(NoduleEvalError) as excinfo:
            engine.run(request)
        assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION
        assert "不能直接重跑" in excinfo.value.message
        assert store.get_run("engine-locked").status == RUN_STATUS_REVIEWED
        store.close()
    finally:
        stub.stop()


def test_rule_degraded_case_does_not_advance_the_run_state(tmp_path: Path) -> None:
    """**模型降级路径不推进 run 状态**。

    模型不可达 → 规则归并接管，这一例标 ``normalize_source=rule`` +
    ``needs_review``：它进「挂起数」，不进指标分母，run 也**不许**因为
    它而往前推一格 —— 降级结果得等人看过。
    """

    store = _store(tmp_path)
    try:
        _seed_run(store, status=RUN_STATUS_RUNNING)
        _seed_case(store)

        # 规则降级：这一例挂起，等人复核
        store.transition_case(
            "run-001",
            "ACC-001",
            CASE_STATUS_NEEDS_REVIEW,
            actor=ACTOR_ENGINE,
            reason="模型不可达，已降级为规则归并",
        )
        assert store.get_case("run-001", "ACC-001").status == CASE_STATUS_NEEDS_REVIEW

        # run 状态没被降级例带着往前推
        assert store.get_run("run-001").status == RUN_STATUS_RUNNING
        targets = {m.to_state for m in store.list_transitions("run-001")}
        assert targets == {CASE_STATUS_NEEDS_REVIEW}

        # 降级例不计入指标分母
        degraded = CaseRow(
            accession_no="ACC-001",
            verdict="hit",
            normalize_source="rule",
            needs_review=True,
        )
        partition = partition_rows([degraded])
        assert partition.counted == 0
        assert partition.needs_review == 1

        # 引擎把 run 往人工确认层推，统统被拒
        for target in (RUN_STATUS_REVIEWED, RUN_STATUS_SUBMITTED):
            with pytest.raises(NoduleEvalError):
                store.transition_run(
                    "run-001", target, actor=ACTOR_ENGINE, reason="降级后自推"
                )
    finally:
        store.close()


def test_transition_tables_cover_every_declared_state() -> None:
    """状态机表与状态取值表对得上 —— 不留「没写边的状态」。"""

    from nodule_eval.store import CASE_STATUSES, RUN_STATUSES

    assert set(RUN_TRANSITIONS) == set(RUN_STATUSES)
    assert set(CASE_TRANSITIONS) == set(CASE_STATUSES)
    # 表上出现的目标状态也都得是合法状态
    for targets in RUN_TRANSITIONS.values():
        assert set(targets) <= set(RUN_STATUSES)
    for targets in CASE_TRANSITIONS.values():
        assert set(targets) <= set(CASE_STATUSES)


def test_case_status_for_row_stays_the_single_source_of_truth() -> None:
    """逐例状态的口径只有一个出处 —— 判定行推状态，状态机只认它。"""

    assert (
        case_status_for_row(
            CaseRow(accession_no="ACC-001", verdict=VERDICT_HIT)
        )
        == CASE_STATUS_JUDGED
    )
    assert (
        case_status_for_row(
            CaseRow(accession_no="ACC-001", verdict=VERDICT_FAILED)
        )
        == CASE_STATUS_FAILED
    )
    assert (
        case_status_for_row(
            CaseRow(
                accession_no="ACC-001",
                verdict=VERDICT_FAILED,
                needs_review=True,
            )
        )
        == CASE_STATUS_NEEDS_REVIEW
    )
