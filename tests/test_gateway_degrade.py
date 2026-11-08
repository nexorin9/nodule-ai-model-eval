"""模型不可达降级：规则回落、rule_degraded 计数与状态不推进（任务 24）。

**模型这一层是可选增益，不是主干。** 端点连不上、请求被拒、返回的形状不对 ——
这三种情况下评估**照样要出得来**：结论改由**规则基线**（确定性口径）给，
这一例标「降级」，单独计数、挂人工复核。

这一组断言回答四个问题：

1. **降级会发生吗** —— 模型端点不可达 / 请求被拒时，归并层接住错误、
   退回规则基线，**不把异常往上抛**（模型挂了不等于这一例失败）；
2. **降级例还算不算数** —— 算：它**仍进指标分母**（结论是确定的），
   但单列「降级数」，不与「模型归并」混为一谈；
3. **降级会不会偷偷推进流程** —— 不会：run 状态停在「已判定」，
   引擎**无权**把它推到「已复核 / 已提交」（那是人工确认层）；
4. **降级看不看得见** —— 逐例行、逐例产物、汇总行三处都看得见，
   放射科能优先复核这一例，信息科能查清「模型为什么没成功」。

**断言走的是真实出站**：模型端点是真监听 socket 的 HTTP 服务（或真关闭的端口），
产品的 ``requests.Session`` / URL 拼接 / Bearer 头一行都没改。
"""

from __future__ import annotations

import csv
import json
import socket
from pathlib import Path
from typing import Any, Sequence

import pytest
from conftest import make_env
from stub_inference import InferenceStub
from stub_model import ModelStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import (
    VERDICT_HIT,
    CaseInput,
    CaseRow,
    EvalRequest,
    counts_toward_denominator,
    partition_rows,
)
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError, format_message
from nodule_eval.gateway import ModelGateway
from nodule_eval.goldstd.merge import ModelMergeNormalizer, merge_by_model
from nodule_eval.store import (
    RUN_STATUS_EVALUATED,
    RUN_STATUS_REVIEWED,
    RUN_STATUS_SUBMITTED,
    RunStore,
)

#: 阳性金标准（放射科写法：部位 + 密度 + 尺寸）。
POSITIVE_GOLD = "右下肺见一磨玻璃结节，大小约 8mm。"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _closed_port() -> int:
    """拿一个**确定没人监听**的端口（绑定后立刻关掉）。"""

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _direct_session():
    """一个**不走系统代理**的会话。

    院内机器常配了系统代理；代理会把「连不上」截胡成它自己的一律 502 ——
    那样测的就不是「端点连不上」这条路径了。
    """

    import requests

    session = requests.Session()
    session.trust_env = False
    return session


def _fast_client() -> HttpClient:
    """退避不真睡的客户端（否则「连接被拒」要等 12 秒）。"""

    return HttpClient(session=_direct_session(), sleep=lambda _s: None)


def _cases(count: int, gold: str = POSITIVE_GOLD, *, prefix: str = "ACC") -> tuple[CaseInput, ...]:
    """造一批病例（每例自带影像标识，不必回读 RIS）。"""

    return tuple(
        CaseInput(
            accession_no=f"{prefix}-{index:04d}",
            gold_standard_text=gold,
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
        dataset_ref="本轮降级验收样例",
        cases=tuple(cases),
    )


def _normalizer(base_url: str, *, client: HttpClient | None = None) -> ModelMergeNormalizer:
    """造一个指向给定端点的语义归并器（模型端点可达或不可达都由它决定）。"""

    gateway = ModelGateway(
        load_config(make_env()),
        client=client if client is not None else _fast_client(),
        base_url=base_url,
    )
    return ModelMergeNormalizer(gateway)


def _engine(tmp_path: Path, normalizer: Any = None) -> Engine:
    """造一个挂上 run store 的引擎（配置读当前进程的环境变量）。"""

    config = load_config()
    spec = load_model_spec("demo-model")
    store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
    engine = Engine(
        config,
        spec,
        client=HttpClient(sleep=lambda _s: None),
        runs_dir=tmp_path / "runs",
        store=store,
        normalizer=normalizer,
    )
    engine.test_store = store  # type: ignore[attr-defined]
    return engine


def _case_rows_csv(run_dir: Path) -> list[dict[str, str]]:
    path = run_dir / "case_rows.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _artifact(run_dir: Path, accession_no: str) -> dict[str, Any]:
    """读某一例的逐例产物（attempt-0001）。"""

    path = run_dir / "cases" / accession_no / "attempt-0001" / "case.json"
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 1. 归并层：模型不可达 → 规则回落
# --------------------------------------------------------------------------


def test_unreachable_model_endpoint_degrades_to_rule_baseline():
    """端点连不上 → 退回规则基线，**不抛异常**（模型挂了 ≠ 这一例失败）。"""

    normalizer = _normalizer(f"http://127.0.0.1:{_closed_port()}")
    result = merge_by_model(POSITIVE_GOLD, gateway=normalizer.gateway)

    assert result.degraded is True
    assert result.labels.source == "rule", "降级后归并来源是规则基线"
    assert result.labels.presence is True, "规则基线照常给出确定结论"
    assert result.labels.location == "右肺下叶"
    assert result.labels.degraded is True
    assert result.labels.degrade_error_code == ErrorCode.MODEL_UNAVAILABLE.value
    assert result.labels.needs_human_review is True, "降级例挂人工复核"


def test_model_request_rejected_also_degrades():
    """端点拒绝了请求（4xx）→ 同样降级，原因码是「请求被拒」。

    两种失败要走**不同的原因码**：连不上找运维，请求被拒改提示词。
    """

    stub = ModelStub(status_sequence=[400])
    stub.start()
    try:
        normalizer = _normalizer(stub.base_url)
        result = merge_by_model(POSITIVE_GOLD, gateway=normalizer.gateway)
    finally:
        stub.stop()

    assert result.degraded is True
    assert result.labels.degrade_error_code == ErrorCode.MODEL_REQUEST_REJECTED.value
    assert result.labels.presence is True


def test_degradation_never_touches_the_gold_standard_text():
    """降级只改**归并口径**，金标准原文一个字都不动。"""

    normalizer = _normalizer(f"http://127.0.0.1:{_closed_port()}")
    result = merge_by_model(POSITIVE_GOLD, gateway=normalizer.gateway)

    assert result.labels.evidence_span in POSITIVE_GOLD
    # 归并结果只写展示列 —— 既有三列（尤其原文）永不被改写。
    from nodule_eval.goldstd.merge import apply_merge_updates

    row = {
        "accession_no": "ACC-0001",
        "gold_standard_text": POSITIVE_GOLD,
        "image_ref": "dx://local/0001",
    }
    updated = apply_merge_updates(row, result.labels.to_dict() and {
        "model_findings": "右肺下叶 | 8.0mm | 磨玻璃",
        "normalize_source": result.labels.source,
    })
    assert updated["gold_standard_text"] == POSITIVE_GOLD


def test_degradation_reason_comes_from_the_error_table():
    """降级原因**只从错误码表取** —— 三处（终端 / 表格 / 产物）同一句话。"""

    normalizer = _normalizer(f"http://127.0.0.1:{_closed_port()}")
    result = merge_by_model(POSITIVE_GOLD, gateway=normalizer.gateway)

    assert result.reason.startswith(
        format_message(ErrorCode.RULE_DEGRADED, detail="").split("{")[0]
    )
    assert result.labels.degrade_reason == result.reason


# --------------------------------------------------------------------------
# 2. 引擎层：降级例仍参与指标
# --------------------------------------------------------------------------


def test_degraded_case_still_counts_toward_metrics(tmp_path, monkeypatch, full_env):
    """模型端点挂了 → 该例降级，但**仍参与指标**（结论是确定的）。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(
            tmp_path,
            normalizer=_normalizer(f"http://127.0.0.1:{_closed_port()}"),
        )
        response = engine.run(_request(_cases(3), run_id="degrade-001"))
        store = engine.test_store  # type: ignore[attr-defined]
    finally:
        stub.stop()

    # 逐例行：判定照常给出，降级标记齐备。
    for row in response.case_rows:
        assert row.verdict == VERDICT_HIT, "降级例照样给出判定"
        assert row.rule_degraded is True
        assert row.error_code == ErrorCode.RULE_DEGRADED.value
        assert row.normalize_source == "rule"
        assert row.needs_review is True, "降级例挂人工复核"
        assert row.degrade_reason, "降级原因随行带出"
        assert counts_toward_denominator(row) is True, "降级例仍进指标分母"

    # 汇总行：降级数单列，且**不与模型归并混为一谈**。
    assert response.summary.rule_degraded_count == 3
    assert response.summary.model_normalized_count == 0
    assert response.summary.evaluated_cases == 3, "三例都进了分母"
    assert response.summary.needs_review_count == 0, "降级例不占「挂起数」那一列"
    assert response.summary.failed_count == 0, "降级不是失败"
    assert "含 3 例规则降级" in response.summary.degrade_note
    assert response.metrics.rule_degraded_count == 3
    assert response.metrics.model_normalized_count == 0

    # 指标分母里三例都算上（存在性一致 → 全是命中）。
    assert response.metrics.tp == 3
    assert response.metrics.fn == 0

    # run 状态停在「已判定」—— 降级不推进流程。
    assert store.get_run("degrade-001").status == RUN_STATUS_EVALUATED


def test_degraded_rows_are_visible_and_filterable(tmp_path, monkeypatch, full_env):
    """降级例在**逐例明细**与**逐例产物**里都看得见、筛得出。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(
            tmp_path,
            normalizer=_normalizer(f"http://127.0.0.1:{_closed_port()}"),
        )
        response = engine.run(_request(_cases(2), run_id="degrade-002"))
    finally:
        stub.stop()

    rows = _case_rows_csv(tmp_path / "runs" / "degrade-002")
    degraded_rows = [
        row for row in rows if row["normalize_source"] == "rule" and row["needs_review"] == "True"
    ]
    assert len(degraded_rows) == 2, "按「规则归并 + 需复核」能筛出降级例"
    assert all(row["verdict"] == VERDICT_HIT for row in degraded_rows)

    artifact = _artifact(tmp_path / "runs" / "degrade-002", "ACC-0001")
    assert artifact["degradation"] is not None
    assert artifact["degradation"]["degraded"] is True
    assert artifact["degradation"]["counts_toward_metrics"] is True
    assert artifact["degradation"]["error_code"] == ErrorCode.RULE_DEGRADED.value
    assert "已降级为规则归并" in artifact["degradation"]["reason"]
    assert artifact["held"] is False
    # 产物里不落金标准原文（可能含患者可识别信息），只落哈希。
    assert "gold_standard_text" not in artifact
    assert artifact["gold_standard_sha256"]


def test_degradation_does_not_advance_run_state(tmp_path, monkeypatch, full_env):
    """**降级不推进状态**：run 停在「已判定」，人工确认层不许被跳过。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(
            tmp_path,
            normalizer=_normalizer(f"http://127.0.0.1:{_closed_port()}"),
        )
        engine.run(_request(_cases(1), run_id="degrade-003"))
        store = engine.test_store  # type: ignore[attr-defined]
    finally:
        stub.stop()

    assert store.get_run("degrade-003").status == RUN_STATUS_EVALUATED

    # 引擎无权把 run 推到「已复核 / 已提交」—— 哪怕这一轮全是降级例。
    with pytest.raises(NoduleEvalError) as excinfo:
        engine._advance_run(
            _request(_cases(1), run_id="degrade-003"),
            RUN_STATUS_REVIEWED,
            actor="engine",
            reason="引擎自推",
        )
    assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION

    with pytest.raises(NoduleEvalError):
        engine._advance_run(
            _request(_cases(1), run_id="degrade-003"),
            RUN_STATUS_SUBMITTED,
            actor="engine",
            reason="引擎自推",
        )

    # 状态机本身也只认人工触发方。
    with pytest.raises(NoduleEvalError):
        store.transition_run(
            "degrade-003", RUN_STATUS_REVIEWED, actor="engine", reason="引擎自推"
        )


def test_held_case_stays_out_of_the_denominator(tmp_path, monkeypatch, full_env):
    """挂起例（绑定存疑）**仍然不进分母** —— 降级的推广不放宽这条红线。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(
            tmp_path,
            normalizer=_normalizer(f"http://127.0.0.1:{_closed_port()}"),
        )
        cases = (
            CaseInput(
                accession_no="ACC-HELD",
                gold_standard_text=POSITIVE_GOLD,
                image_ref="dx://local/held",
                study_uid="STUDY-HELD",
                row_no=1,
                needs_review=True,
                hold_reason="检查号 ACC-HELD 的影像引用与 RIS 不一致，已挂起复核",
            ),
        )
        response = engine.run(_request(cases, run_id="degrade-004"))
    finally:
        stub.stop()

    row = response.case_rows[0]
    assert row.held is True
    assert row.rule_degraded is True, "这一例确实走了降级路径"
    assert counts_toward_denominator(row) is False, "但影像存疑 → 仍不进分母"
    assert response.summary.evaluated_cases == 0
    assert response.summary.needs_review_count == 1, "挂起例占「挂起数」那一列"
    assert response.summary.rule_degraded_count == 1, "降级数照实记"


def test_contract_isolation_counts_hold_and_degrade_separately():
    """契约层：挂起与降级**互不吞并**（口径只有一份，不各写一份）。"""

    rows = [
        CaseRow(accession_no="A", verdict=VERDICT_HIT),
        CaseRow(accession_no="B", verdict=VERDICT_HIT, needs_review=True, held=True),
        CaseRow(
            accession_no="C",
            verdict=VERDICT_HIT,
            needs_review=True,
            rule_degraded=True,
        ),
        CaseRow(accession_no="D", verdict="failed"),
    ]
    partition = partition_rows(rows)

    assert partition.counted == 2, "普通判定例 + 降级例都进分母"
    assert partition.needs_review == 1, "只有挂起例占「挂起数」"
    assert partition.failed == 1


# --------------------------------------------------------------------------
# 3. 确定性基线独立跑完整条主干
# --------------------------------------------------------------------------


def test_deterministic_baseline_runs_the_whole_path_without_a_model(tmp_path, monkeypatch, full_env):
    """**不挂模型归并器**（纯规则基线）也能跑完整条主干并出指标。

    这是「模型不可达也能出结论」的底线：模型端点整条链路都挂掉时，
    评估依然跑得完，只是口径退到规则基线。
    """

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)  # 不传 normalizer → 走规则基线
        response = engine.run(_request(_cases(3), run_id="baseline-001"))
    finally:
        stub.stop()

    assert response.summary.evaluated_cases == 3
    assert response.metrics.tp == 3
    assert response.summary.rule_degraded_count == 0, "规则基线是主路径，不是降级"
    assert response.summary.model_normalized_count == 0
    assert response.summary.degrade_note == ""


# --------------------------------------------------------------------------
# 4. 产品入口：CLI 的 --merge model
# --------------------------------------------------------------------------


def test_cli_merge_model_degrades_and_still_exits_zero(tmp_path, monkeypatch, capsys):
    """CLI 走语义归并、模型端点拒绝请求 → 降级、**退出码 0**、产物齐全。

    降级路径必须能从**产品入口**触达 —— 只在测试里存在的能力不算数。
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
                    "run_id": "cli-degrade",
                    "model_id": "demo-model",
                    "model_version": "v1.0.0",
                    "dataset_ref": "CLI 降级验收",
                    "cases": [
                        {
                            "accession_no": "ACC-CLI-0001",
                            "gold_standard_text": POSITIVE_GOLD,
                            "image_ref": "dx://local/cli-0001",
                            "study_uid": "STUDY-CLI-0001",
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
                "cli-degrade",
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
    assert "规则降级      : 1 例" in out
    assert "降级说明" in out

    rows = _case_rows_csv(tmp_path / "runs" / "cli-degrade")
    assert len(rows) == 1
    assert rows[0]["verdict"] == VERDICT_HIT
    assert rows[0]["normalize_source"] == "rule"
    assert rows[0]["needs_review"] == "True"
