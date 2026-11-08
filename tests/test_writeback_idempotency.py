"""回执状态机、幂等重放与写回失败重试（任务 37）。

写回科研平台评估登记是本产品**唯一真正改变院内业务状态**的动作。
这一组断言回答四个问题 —— 全是「出了事事后要解释」的那类问题：

1. **重复点「确认提交」会不会多出一条登记？** —— 不会。对端**只收到 1 次
   POST**、只产生 1 条登记；第二次是**幂等回放**（一次出站调用都不发），
   拿回的是同一张回执。
2. **写回失败会不会被当成「已提交」？** —— 不会。run 停在 ``reviewed``，
   错误码与状态码落进回执台账与写回底账；5xx 退避重试 3 次仍失败就如实失败。
3. **「请求发出去了」是不是就等于「登记生效了」？** —— 不是。超时 / 断连时
   对端**可能已经登记了**，本地回执记 ``pending``；下一次提交**先问对端**
   （按幂等键 GET 核对），查到了就收敛成「已登记」，查不到才重发。
4. **登记号能不能事后查回来？** —— 能。回执落 SQLite + 产物目录，
   迁移底账里留 ``scope=receipt`` 的痕迹；``verify()`` 把本地登记号与对端
   登记号摆在一起核对。

**断言走的是真实出站**：科研平台是真监听 socket 的 HTTP 服务，
产品的 ``requests.Session`` / URL 拼接 / Bearer 头 / ``Idempotency-Key``
与回执核对用的 GET 路径一行都没改。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest
from conftest import make_env
from stub_inference import InferenceStub
from stub_research_platform import ResearchPlatformStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import VERDICT_HIT, CaseInput, CaseRow, EvalRequest
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.store import (
    ACTOR_ENGINE,
    ACTOR_HUMAN_REVIEW,
    ACTOR_HUMAN_SUBMIT,
    RUN_STATUS_REVIEWED,
    RUN_STATUS_SUBMITTED,
    RunStore,
)
from nodule_eval.writeback import (
    WRITEBACK_AUDIT_FILENAME,
    WritebackService,
)
from nodule_eval.writeback_receipt import (
    RECEIPT_STATUS_FAILED,
    RECEIPT_STATUS_PENDING,
    RECEIPT_STATUS_REGISTERED,
    WritebackReceipt,
    write_receipt_file,
)

#: 阳性金标准（放射科写法：部位 + 密度 + 尺寸）。
POSITIVE_GOLD = "右下肺见一磨玻璃结节，大小约 8mm。"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


class _CountingClient(HttpClient):
    """记「发了几次出站请求」的客户端。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls = 0

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls += 1
        return super().request(method, url, **kwargs)


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


def _engine(tmp_path: Path, store: RunStore) -> Engine:
    """造一个挂上 run store 的引擎（配置读当前进程的环境变量）。"""

    config = load_config()
    spec = load_model_spec("demo-model")
    return Engine(
        config,
        spec,
        client=HttpClient(session=_direct_session(), sleep=lambda _s: None),
        runs_dir=tmp_path / "runs",
        store=store,
    )


def _service(
    store: RunStore,
    stub: ResearchPlatformStub,
    *,
    client: HttpClient | None = None,
    read_timeout: float = 60.0,
) -> WritebackService:
    """对着科研平台替身造一个写回服务（退避不真睡，测试才跑得快）。"""

    config = load_config(make_env(RESEARCH_PLATFORM_BASE_URL=stub.base_url))
    return WritebackService(
        config,
        store,
        client=client
        if client is not None
        else HttpClient(
            session=_direct_session(),
            sleep=lambda _s: None,
            read_timeout=read_timeout,
        ),
    )


def _run_dir(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / "runs" / run_id


def _audit_rows(tmp_path: Path, run_id: str) -> list[dict[str, Any]]:
    """读写回底账（``runs/<run_id>/writeback_audit.jsonl``）。"""

    path = _run_dir(tmp_path, run_id) / WRITEBACK_AUDIT_FILENAME
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _post_count(stub: ResearchPlatformStub) -> int:
    """科研平台侧收到的 **POST** 次数（回执核对的 GET 不算）。"""

    return len([r for r in stub.requests if r["method"] == "POST"])


def _reviewed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_env: dict[str, str],
    *,
    run_id: str,
    count: int = 2,
) -> RunStore:
    """跑一轮评估并推到「已人工复核」，返回 run store。"""

    inference = InferenceStub()
    inference.start()
    try:
        monkeypatch.setenv("INFERENCE_BASE_URL", inference.base_url)
        store = _store(tmp_path)
        engine = _engine(tmp_path, store)
        engine.run(_request(_cases(count), run_id=run_id))
        store.review_run(run_id, actor=ACTOR_HUMAN_REVIEW)
    finally:
        inference.stop()
    return store


# --------------------------------------------------------------------------
# 1. 幂等重放：对端只收到 1 次 POST
# --------------------------------------------------------------------------


def test_repeated_submit_sends_only_one_post(
    tmp_path, monkeypatch, full_env
):
    """连点两下「确认提交」—— 对端**只收到 1 次 POST**、只产生 1 条登记。"""

    store = _reviewed_run(
        tmp_path, monkeypatch, full_env, run_id="idem-001", count=2
    )
    research = ResearchPlatformStub()
    research.start()
    counter = _CountingClient(sleep=lambda _s: None)
    try:
        service = _service(store, research, client=counter)
        first = service.submit("idem-001", actor=ACTOR_HUMAN_SUBMIT)
        posts_after_first = _post_count(research)
        second = service.submit("idem-001", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        research.stop()

    # 对端侧：只有一次 POST、一条登记。
    assert posts_after_first == 1, "第一次提交应当发一次 POST"
    assert _post_count(research) == 1, "重复提交不得再发 POST"
    assert len(research.registrations) == 1, "重复提交不得在对端产生第二条登记"
    assert counter.calls == 1, "重复提交一次出站调用都不该发"

    # 两次拿回的是**同一张回执**。
    assert first.replayed is False
    assert second.replayed is True, "第二次是幂等回放"
    assert second.submission is None, "回放没有发过请求，不该有提交体"
    assert first.receipt is not None and second.receipt is not None
    assert first.receipt.registration_id == second.receipt.registration_id
    assert first.receipt.idempotency_key == second.receipt.idempotency_key

    # 状态：已提交，且回放不改变它。
    assert store.get_run("idem-001").status == RUN_STATUS_SUBMITTED

    # 底账两条：一次真提交 + 一次回放，写得清清楚楚。
    rows = _audit_rows(tmp_path, "idem-001")
    assert [row["action"] for row in rows] == ["submit", "replay"]
    assert rows[1]["registration_id"] == first.receipt.registration_id
    assert rows[1]["converged"] is False


def test_replay_of_a_registered_run_sends_nothing_at_all(
    tmp_path, monkeypatch, full_env
):
    """已登记的那一轮再点「确认提交」—— 出站调用**零次**。"""

    store = _reviewed_run(
        tmp_path, monkeypatch, full_env, run_id="idem-002", count=1
    )
    research = ResearchPlatformStub()
    research.start()
    counter = _CountingClient(sleep=lambda _s: None)
    try:
        service = _service(store, research, client=counter)
        service.submit("idem-002", actor=ACTOR_HUMAN_SUBMIT)
        assert counter.calls == 1
        service.submit("idem-002", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        research.stop()

    assert counter.calls == 1, "幂等回放一次出站调用都不该发"
    assert _post_count(research) == 1


def test_engine_cannot_replay_a_registered_writeback(tmp_path, monkeypatch, full_env):
    """引擎想「回放」一次已登记的写回 —— 被拒，且零出站。"""

    store = _reviewed_run(
        tmp_path, monkeypatch, full_env, run_id="idem-003", count=1
    )
    research = ResearchPlatformStub()
    research.start()
    counter = _CountingClient(sleep=lambda _s: None)
    try:
        service = _service(store, research, client=counter)
        service.submit("idem-003", actor=ACTOR_HUMAN_SUBMIT)
        with pytest.raises(NoduleEvalError) as excinfo:
            service.submit("idem-003", actor=ACTOR_ENGINE)
    finally:
        research.stop()

    err = excinfo.value
    assert err.code is ErrorCode.WRITEBACK_NOT_REVIEWED
    assert "引擎" in err.message
    assert counter.calls == 1, "被拒的那次不许再出站"
    assert _post_count(research) == 1


# --------------------------------------------------------------------------
# 2. 写回失败：不推进状态、留痕
# --------------------------------------------------------------------------


def test_failed_writeback_keeps_the_run_reviewed(tmp_path, monkeypatch, full_env):
    """科研平台一直 5xx —— run 停在「已复核」，**不标成已提交**。"""

    store = _reviewed_run(
        tmp_path, monkeypatch, full_env, run_id="fail-001", count=2
    )
    research = ResearchPlatformStub(status_sequence=[500])
    research.start()
    counter = _CountingClient(sleep=lambda _s: None)
    try:
        service = _service(store, research, client=counter)
        with pytest.raises(NoduleEvalError) as excinfo:
            service.submit("fail-001", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        research.stop()

    err = excinfo.value
    assert err.code is ErrorCode.RESEARCH_WRITE_FAILED
    assert err.http_status == 500, "原始状态码要带出来"

    # **状态不推进**：没登记成功就不许说「已提交」。
    assert store.get_run("fail-001").status == RUN_STATUS_REVIEWED

    # 5xx 退避重试 3 次（会话层统一量级），仍失败才报。
    assert _post_count(research) == 3, "5xx 应退避重试到 3 次"

    # 回执台账：状态 failed、错误码与状态码留痕。
    receipt = store.get_receipt_by_run("fail-001")
    assert receipt is not None
    assert receipt.status == RECEIPT_STATUS_FAILED
    assert receipt.error_code == ErrorCode.RESEARCH_WRITE_FAILED.value
    assert receipt.http_status == 500
    assert receipt.attempts == 3, "试了几次要留痕"
    assert receipt.registration_id == "", "没登记成功就不该有登记号"

    # 写回底账：一条失败记录，**没有**成功记录。
    rows = _audit_rows(tmp_path, "fail-001")
    assert [row["action"] for row in rows] == ["submit_failed"]
    assert rows[0]["error_code"] == ErrorCode.RESEARCH_WRITE_FAILED.value
    assert rows[0]["http_status"] == 500
    assert rows[0]["receipt_status"] == RECEIPT_STATUS_FAILED


def test_client_error_is_not_retried_and_does_not_advance(tmp_path, monkeypatch, full_env):
    """4xx（参数 / 鉴权错）—— 不重试，只打一次，状态照旧不动。"""

    store = _reviewed_run(
        tmp_path, monkeypatch, full_env, run_id="fail-002", count=1
    )
    research = ResearchPlatformStub(
        status_sequence=[400], reject_body={"error": "DUPLICATE_DATASET", "status": 400}
    )
    research.start()
    try:
        service = _service(store, research)
        with pytest.raises(NoduleEvalError) as excinfo:
            service.submit("fail-002", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        research.stop()

    assert excinfo.value.code is ErrorCode.RESEARCH_WRITE_FAILED
    assert excinfo.value.http_status == 400
    assert _post_count(research) == 1, "4xx 重试没有意义"
    assert store.get_run("fail-002").status == RUN_STATUS_REVIEWED
    receipt = store.get_receipt_by_run("fail-002")
    assert receipt is not None and receipt.status == RECEIPT_STATUS_FAILED


def test_a_failed_writeback_can_be_retried_successfully(
    tmp_path, monkeypatch, full_env
):
    """失败之后还能重试 —— 状态没被推进过，所以重试合法。"""

    store = _reviewed_run(
        tmp_path, monkeypatch, full_env, run_id="fail-003", count=1
    )
    # 前三次 5xx（一次提交打满退避预算），第四次成功。
    research = ResearchPlatformStub(status_sequence=[500, 500, 500, 200])
    research.start()
    try:
        service = _service(store, research)
        with pytest.raises(NoduleEvalError):
            service.submit("fail-003", actor=ACTOR_HUMAN_SUBMIT)
        assert store.get_run("fail-003").status == RUN_STATUS_REVIEWED

        outcome = service.submit("fail-003", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        research.stop()

    assert outcome.replayed is False
    assert store.get_run("fail-003").status == RUN_STATUS_SUBMITTED
    assert len(research.registrations) == 1, "对端只有一条登记"
    receipt = store.get_receipt_by_run("fail-003")
    assert receipt is not None and receipt.status == RECEIPT_STATUS_REGISTERED
    assert receipt.registration_id.startswith("REG-")


# --------------------------------------------------------------------------
# 3. 结果未知 → 先核对，别急着再发
# --------------------------------------------------------------------------


def test_unknown_outcome_is_verified_before_sending_again(
    tmp_path, monkeypatch, full_env
):
    """「对端已登记、响应丢了」—— 重试前先核对，**不再发第二次 POST**。"""

    store = _reviewed_run(
        tmp_path, monkeypatch, full_env, run_id="pending-001", count=1
    )
    # 替身先登记、再卡住不回响应 —— 客户端只看到「超时」。
    research = ResearchPlatformStub(hang_after_commit_seconds=30.0)
    research.start()
    try:
        service = _service(store, research, read_timeout=0.5)
        with pytest.raises(NoduleEvalError) as excinfo:
            service.submit("pending-001", actor=ACTOR_HUMAN_SUBMIT)

        err = excinfo.value
        assert err.code is ErrorCode.RESEARCH_WRITE_FAILED
        assert err.http_status is None, "传输层失败没有 HTTP 状态码"

        # 本地记的是「结果未知」—— 不是「没登记」。
        receipt = store.get_receipt_by_run("pending-001")
        assert receipt is not None
        assert receipt.status == RECEIPT_STATUS_PENDING
        assert store.get_run("pending-001").status == RUN_STATUS_REVIEWED

        # 对端其实已经登记了一条。
        assert len(research.registrations) == 1
        posts_before = _post_count(research)

        # 第二次提交：先 GET 核对 → 查到 → 收敛成「已登记」，**不再 POST**。
        service = _service(store, research, read_timeout=0.5)
        outcome = service.submit("pending-001", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        research.stop()

    assert outcome.replayed is True, "核对到登记后按回放处理"
    assert _post_count(research) == posts_before, "核对之后不该再发 POST"
    assert len(research.lookup_requests) == 1, "应当发过一次回执核对查询"
    assert store.get_run("pending-001").status == RUN_STATUS_SUBMITTED

    converged = store.get_receipt_by_run("pending-001")
    assert converged is not None and converged.status == RECEIPT_STATUS_REGISTERED
    assert converged.registration_id == next(iter(research.registrations.values()))["registration_id"]


def test_receipt_verification_compares_local_and_remote(tmp_path, monkeypatch, full_env):
    """回执核对：本地登记号与对端登记号摆在一起 —— 一致才算真的生效。"""

    store = _reviewed_run(
        tmp_path, monkeypatch, full_env, run_id="verify-001", count=2
    )
    research = ResearchPlatformStub()
    research.start()
    try:
        service = _service(store, research)
        outcome = service.submit("verify-001", actor=ACTOR_HUMAN_SUBMIT)
        check = service.verify("verify-001")
    finally:
        research.stop()

    assert check.matched is True
    assert check.remote_found is True
    assert check.local_status == RECEIPT_STATUS_REGISTERED
    assert check.registration_id == outcome.receipt.registration_id
    assert check.remote_registration_id == outcome.receipt.registration_id
    assert "一致" in check.reason


def test_verification_of_a_run_that_was_never_submitted_is_rejected(
    tmp_path, monkeypatch, full_env
):
    """压根没提交过的一轮 —— 指名报错，不谎称「已登记」。"""

    store = _reviewed_run(
        tmp_path, monkeypatch, full_env, run_id="verify-002", count=1
    )
    research = ResearchPlatformStub()
    research.start()
    try:
        service = _service(store, research)
        with pytest.raises(NoduleEvalError) as excinfo:
            service.verify("verify-002")
    finally:
        research.stop()

    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert "verify-002" in excinfo.value.message


def test_lookup_failure_is_not_reported_as_registered(tmp_path, monkeypatch, full_env):
    """核对查询本身失败 —— 报失败，**不许**说「已登记」。"""

    store = _reviewed_run(
        tmp_path, monkeypatch, full_env, run_id="verify-003", count=1
    )
    research = ResearchPlatformStub()
    research.start()
    try:
        service = _service(store, research)
        service.submit("verify-003", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        research.stop()

    # 换一个「核对查询一直 500」的替身。
    broken = ResearchPlatformStub(lookup_status_sequence=[500])
    broken.start()
    try:
        service = _service(store, broken)
        with pytest.raises(NoduleEvalError) as excinfo:
            service.verify("verify-003")
    finally:
        broken.stop()

    assert excinfo.value.code is ErrorCode.RESEARCH_WRITE_FAILED
    assert excinfo.value.http_status == 500


# --------------------------------------------------------------------------
# 4. 回执状态机
# --------------------------------------------------------------------------


def test_registered_receipt_cannot_regress(tmp_path):
    """已登记是**对端的事实** —— 本地状态不许退回 failed / pending。"""

    store = _store(tmp_path)
    try:
        key = "abc123"
        store.record_receipt(
            WritebackReceipt(
                run_id="reg-001",
                idempotency_key=key,
                status=RECEIPT_STATUS_REGISTERED,
                registration_id="REG-0001",
            )
        )
        with pytest.raises(NoduleEvalError) as excinfo:
            store.record_receipt(
                WritebackReceipt(
                    run_id="reg-001",
                    idempotency_key=key,
                    status=RECEIPT_STATUS_FAILED,
                    error_code="RESEARCH_WRITE_FAILED",
                )
            )
    finally:
        store.close()

    err = excinfo.value
    assert err.code is ErrorCode.INVALID_STATE_TRANSITION
    assert "registered" in err.message


def test_receipt_ledger_is_persisted_and_traced(tmp_path):
    """回执落 SQLite + 产物目录，迁移底账里留 ``scope=receipt`` 的痕迹。"""

    store = _store(tmp_path)
    try:
        store.record_receipt(
            WritebackReceipt(
                run_id="reg-002",
                idempotency_key="key-002",
                status=RECEIPT_STATUS_REGISTERED,
                registration_id="REG-0002",
                submitted_at="2026-03-01T10:00:00+08:00",
                response_summary={"registration_id": "REG-0002", "status": "registered"},
            ),
            actor=ACTOR_HUMAN_SUBMIT,
            reason="写回成功",
        )

        stored = store.get_receipt("key-002")
        assert stored is not None
        assert stored.status == RECEIPT_STATUS_REGISTERED
        assert stored.registration_id == "REG-0002"
        assert store.get_receipt_by_run("reg-002") is not None

        # 产物目录里那份（人可读）。
        artifact = tmp_path / "runs" / "reg-002" / "writeback_receipt.json"
        assert artifact.is_file()
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        assert payload["registration_id"] == "REG-0002"

        # 迁移底账：回执的每一次状态变化都留痕。
        transitions = store.list_transitions("reg-002", scope="receipt")
        assert len(transitions) == 1
        assert transitions[0].from_state == "none"
        assert transitions[0].to_state == RECEIPT_STATUS_REGISTERED
        assert transitions[0].actor == ACTOR_HUMAN_SUBMIT
    finally:
        store.close()


def test_receipt_file_helper_writes_a_readable_copy(tmp_path):
    """回执文件助手：写出来的那份人能直接看懂。"""

    path = write_receipt_file(
        tmp_path / "receipt.json",
        WritebackReceipt(
            run_id="reg-003",
            idempotency_key="key-003",
            status=RECEIPT_STATUS_FAILED,
            error_code="RESEARCH_WRITE_FAILED",
            error_detail="写回院内科研平台失败（HTTP 500）：服务端内部错误",
            http_status=500,
            attempts=3,
        ),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == RECEIPT_STATUS_FAILED
    assert payload["attempts"] == 3
    assert "HTTP 500" in payload["error_detail"]


# --------------------------------------------------------------------------
# 5. 回执产物也要过脱敏扫描
# --------------------------------------------------------------------------


def test_receipt_artifact_is_whitelisted_and_clean(tmp_path, monkeypatch, full_env):
    """回执与写回底账也在脱敏扫描范围内 —— 字段有登记、扫不出明细。"""

    from nodule_eval.audit.redact import scan_run_artifacts

    store = _reviewed_run(
        tmp_path, monkeypatch, full_env, run_id="scan-001", count=2
    )
    research = ResearchPlatformStub()
    research.start()
    try:
        service = _service(store, research)
        service.submit("scan-001", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        research.stop()

    run_dir = _run_dir(tmp_path, "scan-001")
    assert (run_dir / "writeback_receipt.json").is_file()
    assert (run_dir / "writeback_audit.jsonl").is_file()

    report = scan_run_artifacts(run_dir)
    scanned = {Path(name).name for name in report.scanned_files}
    assert "writeback_receipt.json" in scanned
    assert "writeback_audit.jsonl" in scanned
    assert report.findings == (), f"扫描发现未登记字段或明细：{report.findings}"


# --------------------------------------------------------------------------
# 6. 替身纪律
# --------------------------------------------------------------------------


def test_writeback_modules_never_fall_back_to_a_local_endpoint():
    """写回相关代码里没有本地回环兜底、没有替身 import。"""

    root = Path(__file__).resolve().parents[1] / "src" / "nodule_eval"
    for name in ("writeback.py", "writeback_receipt.py", "adapters/research_platform.py"):
        source = (root / name).read_text(encoding="utf-8")
        for forbidden in ("localhost", "127.0.0.1", "example.com"):
            assert forbidden not in source, f"{name} 里不应出现 {forbidden}"
        for forbidden in ("stub_research_platform", "from stub", "import stub"):
            assert forbidden not in source, f"{name} 里不应出现 {forbidden}"
