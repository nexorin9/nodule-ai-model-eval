"""断点续跑、幂等重放与产物不覆盖。

这一组断言回答四个问题：

1. **中断后重跑会不会白花钱** —— 已跑完的例不再调用推理端点，
   续跑这一版的调用次数 = **未完成例数**；
2. **中断点的那一例怎么办** —— 状态标回「待处理」（**不标 judged**：
   中断时它只走到「已调用 / 已归并」，没有结论），续跑时重走一遍；
3. **幂等重放会不会写出两份登记** —— 同一个 ``run_id`` 重跑，
   库里仍是一行 run、每个检查号仍是一行 case，调用底账里 id 不重复；
4. **上一版产物会不会被抹掉** —— 同一 ``run_id`` 的每一版各存一份
   （``runs/<run_id>/versions/version-XXXX/``），第一版一字未动。

**断言走的是真实出站**：替身是真监听 socket 的 HTTP 服务，
产品的 ``requests.Session`` / URL 拼接 / Bearer 头 / 请求体一行都没改。
「中断」是用真实的异常打断主路径（归并阶段抛错 / 推理调用还没返回就抛错），
不是把引擎内部状态改掉 —— 改状态的话，测的就不是「真断一次会怎样」。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest
from stub_inference import InferenceStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import (
    VERDICT_HIT,
    CaseInput,
    EvalRequest,
    RunOptions,
)
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.goldstd import GoldLabels, normalize_gold_standard
from nodule_eval.store import (
    CASE_STATUS_CALLED,
    CASE_STATUS_JUDGED,
    CASE_STATUS_PENDING,
    RUN_STATUS_EVALUATED,
    RunStore,
)

#: 阳性金标准（放射科写法：部位 + 密度 + 尺寸）。
POSITIVE_GOLD = "右下肺见一磨玻璃结节，大小约 8mm。"

#: 续跑场景的规模（小规模才看得清「哪几例被跳过了」）。
SCALE = 6

#: 模拟中断的那一例（跑批到一半断掉）。
INTERRUPTED = "ACC-0003"


def _cases(count: int = SCALE, gold: str = POSITIVE_GOLD) -> tuple[CaseInput, ...]:
    """造一批病例（每例自带影像标识，不必回读 RIS）。"""

    return tuple(
        CaseInput(
            accession_no=f"ACC-{index:04d}",
            gold_standard_text=gold,
            image_ref=f"dx://local/{index:04d}",
            study_uid=f"STUDY-{index:04d}",
            row_no=index + 1,
        )
        for index in range(count)
    )


def _engine(tmp_path: Path, *, normalizer: Any = None) -> Engine:
    """造一个挂上 run store 的引擎（退避不真睡，测试才跑得快）。"""

    config = load_config()
    spec = load_model_spec("demo-model")
    store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
    engine = Engine(
        config,
        spec,
        client=HttpClient(sleep=lambda _seconds: None),
        runs_dir=tmp_path / "runs",
        store=store,
        normalizer=normalizer,
    )
    engine.test_store = store  # type: ignore[attr-defined]
    return engine


def _request(
    cases: Sequence[CaseInput],
    *,
    run_id: str,
    resume: bool = False,
) -> EvalRequest:
    """串行跑（并发数 1）：中断落在哪一例上才说得清。"""

    return EvalRequest(
        run_id=run_id,
        model_id="demo-model",
        model_version="v1.0.0",
        dataset_ref="断点续跑验收样例",
        cases=tuple(cases),
        options=RunOptions(concurrency=1, resume=resume),
    )


def _ledger(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "tool_calls.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _interrupting_normalizer(target: str):
    """归并到指定那一例时抛错 —— 模拟「跑到一半断电」。

    抛错点在**推理调用之后**：这一例已经调过厂商端点、底账已记，
    状态停在「已调用」—— 这是最容易被误当成「跑完了」的那种中断。
    """

    def _normalize(case: CaseInput) -> GoldLabels:
        if case.accession_no == target:
            raise RuntimeError("模拟断电：归并阶段崩了")
        return normalize_gold_standard(case.gold_standard_text)

    return _normalize


# --------------------------------------------------------------------------
# 断点续跑
# --------------------------------------------------------------------------


def test_resume_only_processes_the_unfinished_cases(tmp_path, full_env, monkeypatch):
    """中断后续跑：总调用次数 = 未完成例数，已跑完的例不再打厂商端点。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path, normalizer=_interrupting_normalizer(INTERRUPTED))
        cases = _cases()
        with pytest.raises(RuntimeError):
            engine.run(_request(cases, run_id="resume-001"))
        first_calls = len(stub.requests)
        store = engine.test_store  # type: ignore[attr-defined]

        # 断点之前：前三例判完（终态），中断那一例停在「已调用」
        before = {c.accession_no: c for c in store.list_cases("resume-001")}
        assert set(before) == {"ACC-0000", "ACC-0001", "ACC-0002", INTERRUPTED}
        assert before[INTERRUPTED].status == CASE_STATUS_CALLED
        judged_ids = {
            acc: c.tool_call_id
            for acc, c in before.items()
            if c.status == CASE_STATUS_JUDGED
        }
        assert set(judged_ids) == {"ACC-0000", "ACC-0001", "ACC-0002"}
        # 中断那一例的推理调用**真的发生过**，底账里留着
        assert before[INTERRUPTED].tool_call_id

        # 换成正常归并器再续跑
        engine.normalizer = lambda case: normalize_gold_standard(case.gold_standard_text)
        response = engine.run(_request(cases, run_id="resume-001", resume=True))
        resume_calls = len(stub.requests) - first_calls
    finally:
        stub.stop()

    # ---- 续跑这一版的调用次数 = 未完成例数（3 例）----
    assert first_calls == 4, "前三例 + 中断那一例各调一次推理端点"
    assert resume_calls == 3, "续跑只该处理没跑完的三例"
    assert len(response.case_rows) == SCALE
    assert response.summary.skipped_count == 3
    assert response.summary.evaluated_cases == SCALE

    # ---- 已跑完的例：判定行与调用 id 一字未改（没重调、没重记底账）----
    after = {c.accession_no: c for c in store.list_cases("resume-001")}
    assert len(after) == SCALE, "每个检查号仍只有一行 case（不追加重复行）"
    rows = {row.accession_no: row for row in response.case_rows}
    for acc, call_id in judged_ids.items():
        assert after[acc].tool_call_id == call_id, "已跑完的例不再产生新的调用"
        assert rows[acc].tool_call_id == call_id
        assert rows[acc].verdict == VERDICT_HIT

    # ---- 中断那一例：被复位到「待处理」后重走一遍，判完 ----
    assert after[INTERRUPTED].status == CASE_STATUS_JUDGED
    assert rows[INTERRUPTED].tool_call_id != before[INTERRUPTED].tool_call_id

    store.close()


def test_resume_resets_the_interrupted_case_to_pending(tmp_path, full_env, monkeypatch):
    """中断点的例状态标回 **pending**（不标 judged），且留迁移痕。

    「不标 judged」是关键：中断时它只走到「已调用」，
    判都没判 —— 标成 judged 等于凭空造了个结论。
    """

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path, normalizer=_interrupting_normalizer(INTERRUPTED))
        cases = _cases()
        with pytest.raises(RuntimeError):
            engine.run(_request(cases, run_id="resume-reset"))
        store = engine.test_store  # type: ignore[attr-defined]

        engine.normalizer = lambda case: normalize_gold_standard(case.gold_standard_text)
        engine.run(_request(cases, run_id="resume-reset", resume=True))
    finally:
        stub.stop()

    moves = [
        (m.from_state, m.to_state)
        for m in store.list_transitions("resume-reset", scope="case", subject=INTERRUPTED)
    ]
    assert moves == [
        ("pending", "called"),       # 第一版：调了推理端点
        ("called", "pending"),       # 续跑：复位到待处理（不是 judged）
        ("pending", "called"),       # 续跑：重新调推理端点
        ("called", "normalized"),
        ("normalized", "judged"),
    ]
    reasons = [
        m.reason for m in store.list_transitions("resume-reset", scope="case", subject=INTERRUPTED)
    ]
    assert any("复位" in reason for reason in reasons)
    store.close()


def test_resume_after_a_crash_before_the_call_runs_that_case_exactly_once(
    tmp_path, full_env, monkeypatch
):
    """推理调用还没返回就崩了：那一例**没留下调用**，续跑时恰好调一次。

    与上一条的区别：崩溃点更靠前（调用发出、响应没回来），
    底账里没有这一例的记录 —— 续跑不能凭空多出一条调用，
    也不能因为「以为它跑过」就跳过它。
    """

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        real_detect = engine.adapter.detect

        def _detect(**kwargs: Any):
            if kwargs.get("study_uid") == "STUDY-0002":
                raise RuntimeError("模拟断电：推理调用还没返回")
            return real_detect(**kwargs)

        engine.adapter.detect = _detect  # type: ignore[method-assign]
        cases = _cases()
        with pytest.raises(RuntimeError):
            engine.run(_request(cases, run_id="resume-precall"))
        store = engine.test_store  # type: ignore[attr-defined]

        before = {c.accession_no: c for c in store.list_cases("resume-precall")}
        assert "ACC-0002" in before
        assert before["ACC-0002"].status == CASE_STATUS_PENDING
        assert before["ACC-0002"].tool_call_id == "", "调用没返回，底账里不该有它"

        engine.adapter.detect = real_detect  # type: ignore[method-assign]
        response = engine.run(_request(cases, run_id="resume-precall", resume=True))
    finally:
        stub.stop()

    ledger = _ledger(tmp_path / "runs" / "resume-precall")
    calls_for_interrupted = [
        record
        for record in ledger
        if record["args"].get("accession_no") == "ACC-0002"
    ]
    assert len(calls_for_interrupted) == 1, "这一例全程只该有一次调用底账"
    rows = {row.accession_no: row for row in response.case_rows}
    assert rows["ACC-0002"].verdict == VERDICT_HIT
    assert rows["ACC-0002"].tool_call_id == calls_for_interrupted[0]["tool_call_id"]
    store.close()


def test_resume_keeps_both_versions_of_the_artifacts(tmp_path, full_env, monkeypatch):
    """同一 run_id 的每一版产物各存一份，第一版一字未动。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        cases = _cases(3)
        first = engine.run(_request(cases, run_id="replay-versions"))
        first_csv = (
            tmp_path / "runs" / "replay-versions" / "case_rows.csv"
        ).read_text(encoding="utf-8-sig")
        second = engine.run(_request(cases, run_id="replay-versions"))
    finally:
        stub.stop()

    versions = tmp_path / "runs" / "replay-versions" / "versions"
    assert sorted(p.name for p in versions.iterdir()) == ["version-0001", "version-0002"]

    v1 = versions / "version-0001"
    v2 = versions / "version-0002"
    # 第一版一字未动
    assert (v1 / "case_rows.csv").read_text(encoding="utf-8-sig") == first_csv
    # 第二版 = 当前这一版（顶层始终是最新版，表内插件按固定路径回填）
    assert (v2 / "case_rows.csv").read_text(encoding="utf-8-sig") == (
        tmp_path / "runs" / "replay-versions" / "case_rows.csv"
    ).read_text(encoding="utf-8-sig")

    v1_meta = json.loads((v1 / "version.json").read_text(encoding="utf-8"))
    assert v1_meta["version_no"] == 1
    assert v1_meta["status"] == "completed"
    assert v1_meta["processed_cases"] == 3
    assert v1_meta["skipped_cases"] == 0
    v2_meta = json.loads((v2 / "version.json").read_text(encoding="utf-8"))
    assert v2_meta["version_no"] == 2
    assert v2_meta["mode"] == "run"

    assert first.summary.version_no == 1
    assert second.summary.version_no == 2


def test_interrupted_version_is_marked_interrupted(tmp_path, full_env, monkeypatch):
    """跑崩的那一版照样留版本台账，状态写 ``interrupted``。

    半截产物不能装作完整产物 —— 「这一版跑完了」和「这一版崩在第几例」
    必须分得清。
    """

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path, normalizer=_interrupting_normalizer(INTERRUPTED))
        with pytest.raises(RuntimeError):
            engine.run(_request(_cases(), run_id="replay-crash"))
    finally:
        stub.stop()

    versions = tmp_path / "runs" / "replay-crash" / "versions"
    meta = json.loads((versions / "version-0001" / "version.json").read_text(encoding="utf-8"))
    assert meta["status"] == "interrupted"
    assert "模拟断电" in meta["detail"]
    assert meta["total_cases"] == SCALE


# --------------------------------------------------------------------------
# 幂等重放
# --------------------------------------------------------------------------


def test_replaying_the_same_run_id_does_not_duplicate_registrations(
    tmp_path, full_env, monkeypatch
):
    """同一个 run_id 重跑：库里仍是一行 run、每个检查号仍是一行 case。

    **幂等键与主键覆盖**：run 按 ``run_id`` 主键覆盖、case 按
    ``(run_id, accession_no)`` 主键覆盖、调用底账按 ``tool_call_id``
    去重 —— 三处都不追加第二条登记。
    """

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        cases = _cases(4)
        engine.run(_request(cases, run_id="replay-001"))
        second = engine.run(_request(cases, run_id="replay-001"))
        store = engine.test_store  # type: ignore[attr-defined]
    finally:
        stub.stop()

    # ---- 库里没有第二条登记 ----
    assert len(store.list_runs()) == 1
    assert store.count_cases("replay-001") == 4, "重复跑不追加 case 行"
    assert len(second.case_rows) == 4

    # ---- 调用底账：id 全局唯一，没有同一条记两遍 ----
    ledger = _ledger(tmp_path / "runs" / "replay-001")
    assert len(ledger) == 8, "两轮各 4 次调用"
    assert len({record["tool_call_id"] for record in ledger}) == 8
    assert store.count_tool_calls("replay-001") == 8

    # ---- 逐例产物按 attempt 另起一份，历史留着 ----
    case_dir = tmp_path / "runs" / "replay-001" / "cases" / "ACC-0000"
    assert sorted(p.name for p in case_dir.iterdir()) == ["attempt-0001", "attempt-0002"]
    store.close()


def test_resume_of_a_finished_run_skips_everything(tmp_path, full_env, monkeypatch):
    """已经跑完的一轮再点「继续评估」：一例都不重跑，判定行一字未改。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        cases = _cases(4)
        first = engine.run(_request(cases, run_id="resume-done"))
        calls_before = len(stub.requests)
        second = engine.run(_request(cases, run_id="resume-done", resume=True))
        calls_after = len(stub.requests)
    finally:
        stub.stop()

    assert calls_after == calls_before, "已跑完的一轮，续跑一次都不调"
    assert second.summary.skipped_count == 4
    assert [r.accession_no for r in second.case_rows] == [r.accession_no for r in first.case_rows]
    assert [r.verdict for r in second.case_rows] == [r.verdict for r in first.case_rows]
    assert [r.tool_call_id for r in second.case_rows] == [
        r.tool_call_id for r in first.case_rows
    ]


# --------------------------------------------------------------------------
# 前置校验
# --------------------------------------------------------------------------


def test_resume_without_a_previous_run_names_the_run_id(tmp_path, full_env, monkeypatch):
    """库里没这一轮 → 指名报错，不静默重头跑。"""

    engine = _engine(tmp_path)
    try:
        with pytest.raises(NoduleEvalError) as excinfo:
            engine.run(_request(_cases(2), run_id="resume-missing", resume=True))
    finally:
        engine.test_store.close()  # type: ignore[attr-defined]

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "resume-missing" in excinfo.value.message


def test_resume_without_a_store_reports_the_missing_store(tmp_path, full_env, monkeypatch):
    """没挂 run store → 续不了（没有「上次跑到哪儿」的凭据）。"""

    config = load_config()
    spec = load_model_spec("demo-model")
    engine = Engine(
        config,
        spec,
        client=HttpClient(sleep=lambda _seconds: None),
        runs_dir=tmp_path / "runs",
    )
    with pytest.raises(NoduleEvalError) as excinfo:
        engine.run(_request(_cases(2), run_id="resume-nostore", resume=True))

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "--store" in excinfo.value.message


def test_cli_resume_flag_maps_to_the_contract_option():
    """``run --resume`` 落到契约的 ``options.resume``。"""

    from nodule_eval.cli import _build_request, build_parser

    args = build_parser().parse_args(
        ["run", "--cases", "cases.json", "--model-id", "demo-model", "--resume"]
    )
    assert args.resume is True

    payload = _build_request(
        {"cases": [{"accession_no": "ACC-0001", "gold_standard_text": "右下肺见结节。"}]},
        args,
    )
    assert payload["options"]["resume"] is True


def test_resume_leaves_the_run_in_the_evaluated_state(tmp_path, full_env, monkeypatch):
    """续跑跑完，run 状态照旧推进到 evaluated（草稿层）。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path, normalizer=_interrupting_normalizer(INTERRUPTED))
        cases = _cases()
        with pytest.raises(RuntimeError):
            engine.run(_request(cases, run_id="resume-state"))
        store = engine.test_store  # type: ignore[attr-defined]
        engine.normalizer = lambda case: normalize_gold_standard(case.gold_standard_text)
        engine.run(_request(cases, run_id="resume-state", resume=True))
    finally:
        stub.stop()

    assert store.get_run("resume-state").status == RUN_STATUS_EVALUATED
    store.close()
