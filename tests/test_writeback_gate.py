"""确认闸门：未经人工复核，绝不出一站调用（任务 36）。

写回科研平台评估登记是本产品**唯一真正改变院内业务状态**的动作。
这条动作的前置条件只有两条，**都要满足**：

1. 这一轮 run 已经被**人工复核**过（状态 ``reviewed``）；
2. 触发这次写回的**是人**（表格里那一下「确认提交」），不是引擎。

这一组断言回答四个问题：

1. **闸门不过，会不会偷偷发出去？** —— 不会。**一次出站调用都不发**
   （客户端计数替身记 0 次 + 科研平台替身侧 0 条记录），不是「发了但被拒」：
   「发了但被拒」已经在对端留下了痕迹，事后还得去解释；
2. **``reviewed`` 是谁能产生的？** —— 只有人工。引擎自推一律被状态机拒，
   闸门也只认人工触发方（``actor=engine`` 连闸门都进不去）；
3. **降级例能不能提交？** —— 能（结论是确定的），但提交体与汇总行都
   **带降级计数**，两边数字对不上就**不许写回**（对端拿到的数与总结论
   写的数不一样，采购会上就解释不清了）；
4. **跑完一轮，产物目录里有没有出站痕迹？** —— 没有。写回记录只在
   真的写回时才产生，跑完一轮评估不该留下任何「已写回」的影子。

**断言走的是真实出站**：科研平台是真监听 socket 的 HTTP 服务，
产品的 ``requests.Session`` / URL 拼接 / Bearer 头 / ``Idempotency-Key`` 一行都没改。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, Sequence

import pytest
from conftest import make_env
from stub_inference import InferenceStub
from stub_research_platform import ResearchPlatformStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import (
    VERDICT_HIT,
    CaseInput,
    CaseRow,
    EvalRequest,
)
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.gateway import ModelGateway
from nodule_eval.goldstd.merge import ModelMergeNormalizer
from nodule_eval.store import (
    ACTOR_ENGINE,
    ACTOR_HUMAN_REVIEW,
    ACTOR_HUMAN_SUBMIT,
    CASE_STATUS_JUDGED,
    RUN_STATUS_CALLED,
    RUN_STATUS_DRAFT,
    RUN_STATUS_EVALUATED,
    RUN_STATUS_REVIEWED,
    RUN_STATUS_SUBMITTED,
    RunStore,
)
from nodule_eval.writeback import (
    GATE_REQUIRED_STATUS,
    WRITEBACK_AUDIT_FILENAME,
    WritebackService,
    build_submission,
    evaluate_gate,
)

#: 阳性金标准（放射科写法：部位 + 密度 + 尺寸）。
POSITIVE_GOLD = "右下肺见一磨玻璃结节，大小约 8mm。"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


class _CountingClient(HttpClient):
    """记「发了几次出站请求」的客户端 —— 用来证明**压根没发**。

    只数 ``request`` 的调用次数：闸门拒绝时这个数必须是 **0**。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls = 0

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls += 1
        return super().request(method, url, **kwargs)


def _closed_port() -> int:
    """拿一个**确定没人监听**的端口（绑定后立刻关掉）。"""

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _direct_session():
    """不走系统代理的会话（院内机器常配代理，代理会把失败截胡成它的一律 502）。"""

    import requests

    session = requests.Session()
    session.trust_env = False
    return session


def _cases(count: int, *, prefix: str = "ACC") -> tuple[CaseInput, ...]:
    """造一批病例（每例自带影像标识，不必回读 RIS）。"""

    return tuple(
        CaseInput(
            accession_no=f"{prefix}-{index:04d}",
            gold_standard_text=POSITIVE_GOLD,
            image_ref=f"dx://local/{index:04d}",
            study_uid=f"STUDY-{index:04d}",
            row_no=index + 1,
        )
        for index in range(count)
    )


def _request(
    cases: Sequence[CaseInput],
    *,
    run_id: str,
    dataset_ref: str = "评估表-2026-03（胸片肺结节）",
) -> EvalRequest:
    return EvalRequest(
        run_id=run_id,
        model_id="demo-model",
        model_version="v1.0.0",
        dataset_ref=dataset_ref,
        cases=tuple(cases),
    )


def _store(tmp_path: Path) -> RunStore:
    return RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")


def _engine(
    tmp_path: Path,
    store: RunStore,
    normalizer: Any = None,
) -> Engine:
    """造一个挂上 run store 的引擎（配置读当前进程的环境变量）。"""

    config = load_config()
    spec = load_model_spec("demo-model")
    return Engine(
        config,
        spec,
        client=HttpClient(session=_direct_session(), sleep=lambda _s: None),
        runs_dir=tmp_path / "runs",
        store=store,
        normalizer=normalizer,
    )


def _degrading_normalizer() -> ModelMergeNormalizer:
    """指向**连不上**的模型端点 —— 归并会降级到规则基线。"""

    gateway = ModelGateway(
        load_config(make_env()),
        client=HttpClient(session=_direct_session(), sleep=lambda _s: None),
        base_url=f"http://127.0.0.1:{_closed_port()}",
    )
    return ModelMergeNormalizer(gateway)


def _service(
    store: RunStore,
    stub: ResearchPlatformStub,
    *,
    client: HttpClient | None = None,
) -> WritebackService:
    """对着科研平台替身造一个写回服务。"""

    config = load_config(make_env(RESEARCH_PLATFORM_BASE_URL=stub.base_url))
    return WritebackService(
        config,
        store,
        client=client if client is not None else HttpClient(sleep=lambda _s: None),
    )


def _run_dir(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / "runs" / run_id


# --------------------------------------------------------------------------
# 1. 闸门不过 → 一次出站调用都不发
# --------------------------------------------------------------------------


def test_submit_before_human_review_is_rejected_and_sends_nothing(
    tmp_path, monkeypatch, full_env
):
    """跑完没复核就点「确认提交」—— 被拒，且**一个请求都没发出去**。"""

    inference = InferenceStub()
    inference.start()
    research = ResearchPlatformStub()
    research.start()
    counter = _CountingClient(sleep=lambda _s: None)
    try:
        monkeypatch.setenv("INFERENCE_BASE_URL", inference.base_url)
        store = _store(tmp_path)
        engine = _engine(tmp_path, store)
        engine.run(_request(_cases(2), run_id="gate-001"))
        assert store.get_run("gate-001").status == RUN_STATUS_EVALUATED

        service = _service(store, research, client=counter)
        with pytest.raises(NoduleEvalError) as excinfo:
            service.submit("gate-001")
    finally:
        inference.stop()
        research.stop()

    err = excinfo.value
    assert err.code is ErrorCode.WRITEBACK_NOT_REVIEWED
    assert RUN_STATUS_EVALUATED in err.message, "报错要指名当前状态"
    assert GATE_REQUIRED_STATUS in err.message, "报错要指名要先到哪一步"

    # **零出站**：客户端一次都没调、对端一条记录都没有。
    assert counter.calls == 0, "闸门拒绝时一次出站调用都不该发"
    assert research.requests == [], "科研平台侧不该有任何记录"

    # 状态不被推进，产物目录里也不留写回痕迹。
    assert store.get_run("gate-001").status == RUN_STATUS_EVALUATED
    assert not (_run_dir(tmp_path, "gate-001") / WRITEBACK_AUDIT_FILENAME).exists()


@pytest.mark.parametrize(
    "status",
    [
        RUN_STATUS_DRAFT,
        "assembled",
        "running",
        RUN_STATUS_CALLED,
        RUN_STATUS_EVALUATED,
        "failed",
    ],
)
def test_only_a_reviewed_run_may_leave_the_hospital(tmp_path, status):
    """除了 ``reviewed``，别的状态一律写不出去 —— 且一律零出站。"""

    research = ResearchPlatformStub()
    research.start()
    counter = _CountingClient(sleep=lambda _s: None)
    try:
        store = _store(tmp_path)
        store.upsert_run(
            run_id="gate-status",
            dataset_ref="评估表-2026-03",
            model_id="demo-model",
            model_version="v1.0.0",
            status=status,
        )
        service = _service(store, research, client=counter)
        with pytest.raises(NoduleEvalError) as excinfo:
            service.submit("gate-status")
    finally:
        research.stop()

    assert excinfo.value.code is ErrorCode.WRITEBACK_NOT_REVIEWED
    assert counter.calls == 0
    assert research.requests == []


def test_submit_of_an_unknown_run_is_rejected_without_outbound(tmp_path):
    """库里没这一轮 —— 指名报错，且零出站。"""

    research = ResearchPlatformStub()
    research.start()
    counter = _CountingClient(sleep=lambda _s: None)
    try:
        store = _store(tmp_path)
        service = _service(store, research, client=counter)
        with pytest.raises(NoduleEvalError) as excinfo:
            service.submit("never-existed")
    finally:
        research.stop()

    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert "never-existed" in excinfo.value.message
    assert counter.calls == 0
    assert research.requests == []


def test_engine_triggered_writeback_is_rejected(tmp_path):
    """引擎触发写回 —— 连闸门都进不去（哪怕这一轮已经复核过）。"""

    research = ResearchPlatformStub()
    research.start()
    counter = _CountingClient(sleep=lambda _s: None)
    try:
        store = _store(tmp_path)
        store.upsert_run(
            run_id="gate-actor",
            dataset_ref="评估表-2026-03",
            model_id="demo-model",
            model_version="v1.0.0",
            status=RUN_STATUS_REVIEWED,
        )
        service = _service(store, research, client=counter)
        with pytest.raises(NoduleEvalError) as excinfo:
            service.submit("gate-actor", actor=ACTOR_ENGINE)
    finally:
        research.stop()

    err = excinfo.value
    assert err.code is ErrorCode.WRITEBACK_NOT_REVIEWED
    assert "引擎" in err.message, "报错要说清是引擎在触发"
    assert counter.calls == 0
    assert research.requests == []
    assert store.get_run("gate-actor").status == RUN_STATUS_REVIEWED, "状态不许被推进"


# --------------------------------------------------------------------------
# 2. reviewed 只能由人工产生
# --------------------------------------------------------------------------


def test_engine_cannot_advance_run_to_reviewed(tmp_path, monkeypatch, full_env):
    """引擎自推「已复核」—— 状态机直接拒（这条写在代码里，不是口头约定）。"""

    inference = InferenceStub()
    inference.start()
    try:
        monkeypatch.setenv("INFERENCE_BASE_URL", inference.base_url)
        store = _store(tmp_path)
        engine = _engine(tmp_path, store)
        request = _request(_cases(1), run_id="gate-002")
        engine.run(request)

        assert store.get_run("gate-002").status == RUN_STATUS_EVALUATED

        with pytest.raises(NoduleEvalError) as excinfo:
            store.transition_run(
                "gate-002", RUN_STATUS_REVIEWED, actor=ACTOR_ENGINE, reason="引擎自推"
            )
        assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION

        with pytest.raises(NoduleEvalError):
            engine._advance_run(
                request, RUN_STATUS_REVIEWED, actor=ACTOR_ENGINE, reason="引擎自推"
            )
    finally:
        inference.stop()


def test_human_review_then_submit_writes_back_once(tmp_path, monkeypatch, full_env):
    """人工复核 → 人工确认提交 → 对端一条登记、run 推进到「已提交」。"""

    inference = InferenceStub()
    inference.start()
    research = ResearchPlatformStub()
    research.start()
    try:
        monkeypatch.setenv("INFERENCE_BASE_URL", inference.base_url)
        store = _store(tmp_path)
        engine = _engine(tmp_path, store)
        engine.run(_request(_cases(2), run_id="gate-003"))

        # 人工复核（表格里那一下「确认复核」）。
        store.review_run("gate-003", actor=ACTOR_HUMAN_REVIEW)
        assert store.get_run("gate-003").status == RUN_STATUS_REVIEWED

        service = _service(store, research)
        outcome = service.submit("gate-003", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        inference.stop()
        research.stop()

    # 对端侧：一条登记、一次出站。
    assert len(research.requests) == 1
    assert len(research.registrations) == 1
    assert outcome.receipt.registration_id.startswith("REG-")
    assert outcome.decision.allowed is True

    # 状态推进到「已提交」，写回底账留下登记号与幂等键。
    assert store.get_run("gate-003").status == RUN_STATUS_SUBMITTED
    audit = _run_dir(tmp_path, "gate-003") / WRITEBACK_AUDIT_FILENAME
    assert audit.is_file()
    record = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert record["registration_id"] == outcome.receipt.registration_id
    assert record["idempotency_key"]
    assert record["actor"] == ACTOR_HUMAN_SUBMIT


# --------------------------------------------------------------------------
# 3. 降级例：能提交，但数字要带着走
# --------------------------------------------------------------------------


def test_degraded_run_can_be_submitted_and_carries_the_degrade_count(
    tmp_path, monkeypatch, full_env
):
    """含规则降级例的 run **可以提交**，但提交体与汇总行都带降级计数。"""

    inference = InferenceStub()
    inference.start()
    research = ResearchPlatformStub()
    research.start()
    try:
        monkeypatch.setenv("INFERENCE_BASE_URL", inference.base_url)
        store = _store(tmp_path)
        engine = _engine(tmp_path, store, normalizer=_degrading_normalizer())
        response = engine.run(_request(_cases(3), run_id="gate-004"))
        assert response.summary.rule_degraded_count == 3

        store.review_run("gate-004", actor=ACTOR_HUMAN_REVIEW)
        service = _service(store, research)
        outcome = service.submit("gate-004", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        inference.stop()
        research.stop()

    # 汇总行（summary.json）里的降级数。
    summary = json.loads(
        (_run_dir(tmp_path, "gate-004") / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["counts"]["rule_degraded"] == 3

    # 提交体里的降级数 —— 与汇总行**必须是同一个数**。
    body = research.requests[0]["body"]
    assert body["metrics"]["rule_degraded_count"] == 3
    assert body["metrics"]["model_normalized_count"] == 0
    assert outcome.submission.metrics["rule_degraded_count"] == 3

    # 逐例行也带得上「哪一例是降级出来的」。
    degraded_rows = [row for row in body["case_rows"] if row["rule_degraded"]]
    assert len(degraded_rows) == 3
    assert all(row["normalize_source"] == "rule" for row in degraded_rows)


def test_summary_disagreeing_with_rows_blocks_the_writeback(tmp_path):
    """汇总行与逐例明细的降级数对不上 —— **不许写回**（对端会拿到另一个口径）。"""

    research = ResearchPlatformStub()
    research.start()
    counter = _CountingClient(sleep=lambda _s: None)
    try:
        store = _store(tmp_path)
        store.upsert_run(
            run_id="gate-005",
            dataset_ref="评估表-2026-03",
            model_id="demo-model",
            model_version="v1.0.0",
            status=RUN_STATUS_REVIEWED,
        )
        # 手写一份 summary.json：降级数写着 5 例，逐例明细里一例降级都没有。
        run_dir = _run_dir(tmp_path, "gate-005")
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "summary.json").write_text(
            json.dumps(
                {
                    "run_id": "gate-005",
                    "counts": {"rule_degraded": 5, "model_normalized": 0},
                    "degrade_note": "含 5 例规则降级",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # 逐例明细：一例正常判定（**没有降级例**）—— 与上面那份 summary 对不上。
        store.upsert_case(
            run_id="gate-005",
            accession_no="ACC-0001",
            status=CASE_STATUS_JUDGED,
            verdict=VERDICT_HIT,
            row=CaseRow(accession_no="ACC-0001", verdict=VERDICT_HIT, row_no=1),
        )

        service = _service(store, research, client=counter)
        with pytest.raises(NoduleEvalError) as excinfo:
            service.submit("gate-005", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        research.stop()

    err = excinfo.value
    assert err.code is ErrorCode.CONTRACT_INVALID
    assert "规则降级数对不上" in err.message
    assert counter.calls == 0, "口径对不上就不该出站"
    assert research.requests == []


# --------------------------------------------------------------------------
# 4. 跑完一轮，产物目录里没有出站痕迹
# --------------------------------------------------------------------------


def test_a_finished_run_leaves_no_writeback_record(tmp_path, monkeypatch, full_env):
    """跑完一轮评估 → 科研平台侧零请求、产物目录里零写回记录。"""

    inference = InferenceStub()
    inference.start()
    research = ResearchPlatformStub()
    research.start()
    research_base_url = research.base_url
    try:
        monkeypatch.setenv("INFERENCE_BASE_URL", inference.base_url)
        store = _store(tmp_path)
        engine = _engine(tmp_path, store)
        engine.run(_request(_cases(2), run_id="gate-006"))
    finally:
        inference.stop()
        research.stop()

    run_dir = _run_dir(tmp_path, "gate-006")
    assert research.requests == [], "跑评估不该碰到科研平台"
    assert not (run_dir / WRITEBACK_AUDIT_FILENAME).exists()

    # 产物里也不该出现任何「已写回」的痕迹（登记号 / 对端地址）。
    for path in run_dir.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "registration_id" not in text, f"{path.name} 里出现了登记号"
        assert research_base_url not in text, f"{path.name} 里出现了科研平台地址"


# --------------------------------------------------------------------------
# 5. 闸门判定本身
# --------------------------------------------------------------------------


def test_gate_decision_reports_why_it_is_closed():
    """闸门判定要说清「现在什么状态」「要去哪」「谁在触发」。"""

    closed = evaluate_gate(RUN_STATUS_EVALUATED, actor=ACTOR_HUMAN_SUBMIT)
    assert closed.allowed is False
    assert closed.current_status == RUN_STATUS_EVALUATED
    assert closed.required_status == GATE_REQUIRED_STATUS
    assert "reviewed" in closed.reason

    opened = evaluate_gate(RUN_STATUS_REVIEWED, actor=ACTOR_HUMAN_SUBMIT)
    assert opened.allowed is True

    by_engine = evaluate_gate(RUN_STATUS_REVIEWED, actor=ACTOR_ENGINE)
    assert by_engine.allowed is False
    assert "引擎" in by_engine.reason


def test_submission_without_a_dataset_ref_is_rejected():
    """没有数据集引用 —— 幂等键算不出来，不许写回。"""

    from nodule_eval.store import StoredRun

    run = StoredRun(
        run_id="gate-007",
        dataset_ref="",
        model_id="demo-model",
        model_version="v1.0.0",
        status=RUN_STATUS_REVIEWED,
    )
    rows = [CaseRow(accession_no="ACC-0001", verdict=VERDICT_HIT, row_no=1)]
    with pytest.raises(NoduleEvalError) as excinfo:
        build_submission(run, rows)

    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert "dataset_ref" in excinfo.value.message


def test_submission_without_a_single_case_row_is_rejected():
    """空评估 —— 一条没有意义的登记，不写回院内系统。"""

    from nodule_eval.store import StoredRun

    run = StoredRun(
        run_id="gate-008",
        dataset_ref="评估表-2026-03",
        model_id="demo-model",
        model_version="v1.0.0",
        status=RUN_STATUS_REVIEWED,
    )
    with pytest.raises(NoduleEvalError) as excinfo:
        build_submission(run, [])

    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert "空评估" in excinfo.value.message


# --------------------------------------------------------------------------
# 6. 替身纪律
# --------------------------------------------------------------------------


def test_writeback_module_never_falls_back_to_a_local_endpoint():
    """写回地址只来自环境变量 —— 代码里没有本地回环兜底、没有替身 import。"""

    source = (
        Path(__file__).resolve().parents[1] / "src" / "nodule_eval" / "writeback.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("localhost", "127.0.0.1", "example.com", "http://"):
        assert forbidden not in source, f"写回闸门里不应出现 {forbidden}"
    for forbidden in ("stub_research_platform", "from stub", "import stub"):
        assert forbidden not in source, f"写回闸门里不应出现 {forbidden}"
