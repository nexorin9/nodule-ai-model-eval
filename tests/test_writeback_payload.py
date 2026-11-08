"""提交体组装与写回审计留痕（任务 38）。

写回科研平台是本产品**唯一真正改变院内业务状态**的动作，所以这一层
回答三个问题，缺一个都不行：

1. **发出去的是什么？** —— 提交体必须**按字段规范齐备**（八项，顺序固定），
   逐例行必须带**判定依据摘要**与**调用记录 ID**，让对端能反查
   「这例凭什么算漏诊」；而**金标准原文一个字都不许带出去**
   （原文可能带姓名 / 年龄 / 住院号 —— 带出去等于把患者明细写进院内系统）。
2. **事后能证明发出去的是哪一版吗？** —— 写回审计底账
   （``runs/<run_id>/writeback_audit.jsonl``）要记下**操作者、时间、
   幂等键、请求体内容摘要、对端回执 ID 与响应状态**；请求体摘要可以
   **当场复算**：把科研平台侧收到的请求体算一次摘要，与底账里的对得上。
3. **太大怎么办？** —— 超出对端上限时**明确报错并提示分批**，
   **绝不静默截断**逐例明细（截断 = 对端登记的例数与本轮结论对不上），
   而且**一次出站调用都不发**。

**断言走的是真实出站**：科研平台是真监听 socket 的 HTTP 服务，
产品的 ``requests.Session`` / URL 拼接 / Bearer 头 / ``Idempotency-Key``
一行都没改 —— 底账里记的摘要，是从**对端真正收到的那个请求体**算出来的。
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
from nodule_eval.adapters.research_platform import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    SUBMISSION_FIELDS,
    assert_payload_within_limit,
    canonical_json,
    payload_bytes,
    payload_digest,
)
from nodule_eval.config import load_config
from nodule_eval.contracts import VERDICT_HIT, VERDICT_MISS, CaseInput, CaseRow, EvalRequest
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.store import (
    ACTOR_HUMAN_REVIEW,
    ACTOR_HUMAN_SUBMIT,
    RUN_STATUS_EVALUATED,
    RUN_STATUS_REVIEWED,
    RUN_STATUS_SUBMITTED,
    RunStore,
)
from nodule_eval.writeback import (
    SUBMITTED_CASE_FIELDS,
    WRITEBACK_AUDIT_FILENAME,
    WritebackService,
    build_submission,
)

#: 阳性金标准（放射科写法：部位 + 密度 + 尺寸）。
POSITIVE_GOLD = "右下肺见一磨玻璃结节，大小约 8mm。"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _direct_session():
    """不走系统代理的会话（院内机器常配代理，代理会把失败截胡成它的一律 502）。"""

    import requests

    session = requests.Session()
    session.trust_env = False
    return session


def _cases(count: int, *, prefix: str = "ACC") -> tuple[CaseInput, ...]:
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


def _request(cases: Sequence[CaseInput], *, run_id: str) -> EvalRequest:
    return EvalRequest(
        run_id=run_id,
        model_id="demo-model",
        model_version="v1.0.0",
        dataset_ref="评估表-2026-03（胸片肺结节）",
        cases=tuple(cases),
    )


def _store(tmp_path: Path) -> RunStore:
    return RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")


def _engine(tmp_path: Path, store: RunStore) -> Engine:
    config = load_config()
    from nodule_eval.config import load_model_spec

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
    **env_overrides: str,
) -> WritebackService:
    config = load_config(
        make_env(RESEARCH_PLATFORM_BASE_URL=stub.base_url, **env_overrides)
    )
    return WritebackService(
        config,
        store,
        client=HttpClient(session=_direct_session(), sleep=lambda _s: None),
    )


def _audit_lines(tmp_path: Path, run_id: str) -> list[dict[str, Any]]:
    path = tmp_path / "runs" / run_id / WRITEBACK_AUDIT_FILENAME
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _stored_run(store: RunStore, run_id: str) -> Any:
    return store.get_run(run_id)


# --------------------------------------------------------------------------
# 1. 提交体：八项字段齐备、顺序固定
# --------------------------------------------------------------------------


def test_submission_carries_the_eight_declared_fields(tmp_path):
    """提交体的字段规范是八项，顺序固定 —— 对端按这个顺序解析。"""

    store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
    run = store.upsert_run(
        run_id="payload-001",
        dataset_ref="评估表-2026-03",
        model_id="demo-model",
        model_version="v1.0.0",
        status=RUN_STATUS_REVIEWED,
    )
    rows = [
        CaseRow(
            accession_no="ACC-0001",
            verdict=VERDICT_HIT,
            model_findings="右下肺 | 8.0mm | 磨玻璃",
            tool_call_id="call-0001",
            judge_basis="存在性一致；部位一致；尺寸一致；密度一致",
            normalize_source="model",
            row_no=1,
        )
    ]

    submission = build_submission(run, rows, submitted_by="信息科-张三", submitted_at="2026-03-01T10:00:00")
    body = submission.to_dict()

    assert list(body.keys()) == list(SUBMISSION_FIELDS), "提交体字段与顺序必须与规范一致"
    assert body["run_id"] == "payload-001"
    assert body["model_id"] == "demo-model"
    assert body["model_version"] == "v1.0.0"
    assert body["dataset_ref"] == "评估表-2026-03"
    assert body["submitted_by"] == "信息科-张三"
    assert body["submitted_at"] == "2026-03-01T10:00:00"
    assert isinstance(body["metrics"], dict) and body["metrics"], "metrics 不得为空"
    assert isinstance(body["case_rows"], list) and len(body["case_rows"]) == 1


# --------------------------------------------------------------------------
# 2. 逐例字段规范：带判定依据摘要与调用记录 ID，不带金标准原文
# --------------------------------------------------------------------------


def test_each_case_row_carries_judge_basis_and_tool_call_id(tmp_path):
    """逐例行要能支撑对端反查 —— 且一个字的金标准原文都不许带出去。"""

    store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
    run = store.upsert_run(
        run_id="payload-002",
        dataset_ref="评估表-2026-03",
        model_id="demo-model",
        model_version="v1.0.0",
        status=RUN_STATUS_REVIEWED,
    )
    rows = [
        CaseRow(
            accession_no="ACC-0001",
            verdict=VERDICT_MISS,
            model_findings="右下肺 | 9.0mm | 实性",
            failure_reason="",
            tool_call_id="call-0001",
            judge_basis="存在性一致；部位一致；尺寸不一致（9.0mm 超出容差 ±2mm）；密度不一致",
            normalize_source="model",
            row_no=1,
        )
    ]

    body = build_submission(run, rows).to_dict()
    row = body["case_rows"][0]

    for field in (
        "accession_no",
        "verdict",
        "judge_basis",
        "tool_call_id",
        "normalize_source",
        "needs_review",
    ):
        assert field in row, f"逐例行缺字段 {field}"

    assert set(SUBMITTED_CASE_FIELDS) == set(row.keys()), "逐例行字段集必须与声明一致"
    assert row["tool_call_id"] == "call-0001", "逐例行要带调用记录 ID（对端反查靠它）"
    assert "尺寸不一致" in row["judge_basis"], "逐例行要带判定依据摘要"

    # 金标准原文**一个字都不许出现**（原文可能带姓名 / 年龄 / 住院号）。
    text = canonical_json(body)
    assert "金标准原文" not in text
    assert "gold_standard" not in text
    assert "右下肺见一磨玻璃结节" not in text


# --------------------------------------------------------------------------
# 3. 写回审计底账：操作者 / 时间 / 幂等键 / 请求体摘要 / 回执 ID / 响应状态
# --------------------------------------------------------------------------


def test_writeback_audit_records_who_what_and_which_payload(
    tmp_path, monkeypatch, full_env
):
    """真跑一轮、真写回一次 —— 底账要把「谁、什么时候、发了哪一版」记全。"""

    inference = InferenceStub()
    inference.start()
    research = ResearchPlatformStub()
    research.start()
    try:
        monkeypatch.setenv("INFERENCE_BASE_URL", inference.base_url)
        store = _store(tmp_path)
        engine = _engine(tmp_path, store)
        engine.run(_request(_cases(2), run_id="payload-003"))
        assert store.get_run("payload-003").status == RUN_STATUS_EVALUATED

        store.review_run("payload-003", actor=ACTOR_HUMAN_REVIEW)
        service = _service(store, research)
        outcome = service.submit("payload-003", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        inference.stop()
        research.stop()

    assert outcome.receipt is not None
    assert store.get_run("payload-003").status == RUN_STATUS_SUBMITTED

    lines = _audit_lines(tmp_path, "payload-003")
    assert len(lines) == 1, "写回一次就该有一条审计记录"
    entry = lines[0]

    # 谁、什么时候。
    assert entry["actor"] == ACTOR_HUMAN_SUBMIT
    assert entry["submitted_by"]
    assert entry["at"]
    # 写了哪一轮、哪款模型、哪批数据。
    assert entry["run_id"] == "payload-003"
    assert entry["model_id"] == "demo-model"
    assert entry["model_version"] == "v1.0.0"
    assert entry["dataset_ref"]
    # 幂等键、对端回执 ID 与响应状态。
    assert entry["idempotency_key"] == outcome.receipt.idempotency_key
    assert entry["registration_id"] == outcome.receipt.registration_id
    assert entry["response_status"], "对端回执状态要记下来"
    # 请求体摘要与字节数。
    assert len(entry["payload_sha256"]) == 64
    assert entry["payload_bytes"] > 0


def test_audit_payload_digest_matches_the_body_the_peer_received(
    tmp_path, monkeypatch, full_env
):
    """底账里的摘要 == 对端**真正收到**的那个请求体的摘要 —— 事后可复算。"""

    inference = InferenceStub()
    inference.start()
    research = ResearchPlatformStub()
    research.start()
    try:
        monkeypatch.setenv("INFERENCE_BASE_URL", inference.base_url)
        store = _store(tmp_path)
        engine = _engine(tmp_path, store)
        engine.run(_request(_cases(2), run_id="payload-004"))
        store.review_run("payload-004", actor=ACTOR_HUMAN_REVIEW)
        service = _service(store, research)
        service.submit("payload-004", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        inference.stop()
        research.stop()

    posts = [r for r in research.requests if r.get("method") == "POST"]
    assert len(posts) == 1, "写回一次只该发一个 POST"
    received = posts[0]["body"]

    entry = _audit_lines(tmp_path, "payload-004")[0]
    assert entry["payload_sha256"] == payload_digest(received), "摘要必须与对端收到的请求体一致"
    assert entry["payload_bytes"] == payload_bytes(received)


def test_audit_records_the_payload_of_a_failed_writeback(
    tmp_path, monkeypatch, full_env
):
    """写回失败也要留痕 —— 且留的是**发出去的那一版**的摘要。"""

    inference = InferenceStub()
    inference.start()
    research = ResearchPlatformStub(status_sequence=[500, 500, 500])
    research.start()
    try:
        monkeypatch.setenv("INFERENCE_BASE_URL", inference.base_url)
        store = _store(tmp_path)
        engine = _engine(tmp_path, store)
        engine.run(_request(_cases(1), run_id="payload-005"))
        store.review_run("payload-005", actor=ACTOR_HUMAN_REVIEW)
        service = _service(store, research)
        with pytest.raises(NoduleEvalError):
            service.submit("payload-005", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        inference.stop()
        research.stop()

    entries = _audit_lines(tmp_path, "payload-005")
    assert entries, "写回失败也要留痕"
    failed = [e for e in entries if e["action"] == "submit_failed"]
    assert len(failed) == 1
    assert failed[0]["error_code"] == ErrorCode.RESEARCH_WRITE_FAILED.value
    assert failed[0]["payload_sha256"], "失败那次也要记下发出去的请求体摘要"
    assert failed[0]["attempts"] == 3


# --------------------------------------------------------------------------
# 4. 提交体大小保护：超限明确报错、不截断、零出站
# --------------------------------------------------------------------------


def test_payload_over_the_limit_is_rejected_without_truncating(
    tmp_path, monkeypatch, full_env
):
    """超出对端上限 —— 明确报错、**不截断**、**一次出站都不发**。"""

    inference = InferenceStub()
    inference.start()
    research = ResearchPlatformStub()
    research.start()
    try:
        monkeypatch.setenv("INFERENCE_BASE_URL", inference.base_url)
        store = _store(tmp_path)
        engine = _engine(tmp_path, store)
        engine.run(_request(_cases(3), run_id="payload-006"))
        store.review_run("payload-006", actor=ACTOR_HUMAN_REVIEW)

        # 把上限压到极小 —— 这一轮必然超限。
        service = _service(
            store,
            research,
            RESEARCH_PLATFORM_MAX_PAYLOAD_BYTES="256",
        )
        with pytest.raises(NoduleEvalError) as excinfo:
            service.submit("payload-006", actor=ACTOR_HUMAN_SUBMIT)
    finally:
        inference.stop()
        research.stop()

    err = excinfo.value
    assert err.code is ErrorCode.RESEARCH_PAYLOAD_TOO_LARGE
    assert "256" in err.message, "报错要写明上限是多少"
    assert "分批" in err.message, "报错要提示改用分批"
    assert "不会截断" in err.message, "报错要说明不截断逐例明细"

    # **零出站**：超限这一轮压根没发请求（对端不知道有这么一轮）。
    assert research.requests == [], "超限时一次出站调用都不该发"

    # 逐例明细**一条都没少** —— 库里那三行原样还在。
    rows = store.recompute("payload-006")
    assert len(rows) == 3, "不能因为超限就把逐例明细截掉"
    assert all(row.verdict for row in rows)

    # 状态不被推进（没登记成功就不许说「已提交」）。
    assert store.get_run("payload-006").status == RUN_STATUS_REVIEWED


def test_payload_limit_error_names_the_byte_counts():
    """报错要带三个数：实际字节、上限、批大小 —— 照着就能分批。"""

    payload = {"run_id": "r", "case_rows": [{"accession_no": f"A-{i}"} for i in range(50)]}
    with pytest.raises(NoduleEvalError) as excinfo:
        assert_payload_within_limit(payload, max_bytes=200, run_id="r1", case_count=50)

    err = excinfo.value
    assert err.code is ErrorCode.RESEARCH_PAYLOAD_TOO_LARGE
    assert str(payload_bytes(payload)) in err.message
    assert "200" in err.message
    assert "50 例" in err.message
    assert "每批约" in err.message, "报错要给出每批大约几例"


def test_payload_within_the_limit_passes_untouched():
    """没超限就**原样放行** —— 大小保护不是拦路虎。"""

    payload = {"run_id": "r", "case_rows": [{"accession_no": "A-1"}]}
    size = assert_payload_within_limit(payload, max_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
    assert size == payload_bytes(payload)
