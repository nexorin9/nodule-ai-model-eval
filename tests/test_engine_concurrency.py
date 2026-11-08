"""并发限流与规模压测：快不快、准不准、失败看不看得见。

这一组断言回答五个问题：

1. **并发有没有跑漏例** —— 总调用次数 = 病例数、每例恰一次调用、无重复调用；
2. **限流是不是真限住了** —— 逐端点独立上限，超限的请求**排队**（不丢弃）；
3. **并发有没有把结论跑歪** —— 同一批输入，串行跑与并发跑**逐例判定一致**
   （失败例与挂起例也一一对应）；
4. **失败能不能被看见** —— 每类错误码都要能定位到具体是哪一例
   （逐例明细里有、调用底账里也能按 id 反查）；
5. **慢的时候能不能说清慢在哪** —— 逐例耗时台账把「算」与「排队」分开记。

**断言走的是真实出站**：替身是真监听 socket 的 HTTP 服务，
产品的 ``requests.Session`` / URL 拼接 / Bearer 头 / 请求体一行都没改。
**服务端自己记的同时在途请求数**是并发上限的独立证据 ——
客户端说自己限了几个，只能算它的说法。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import pytest
from stub_inference import InferenceStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import (
    VERDICT_FAILED,
    VERDICT_HIT,
    CaseInput,
    EvalRequest,
    RunOptions,
)
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode
from nodule_eval.goldstd import GoldLabels, normalize_gold_standard
from nodule_eval.store import (
    CASE_TIMINGS_FILENAME,
    RUN_STATUS_EVALUATED,
    RunStore,
)

#: 阳性金标准（放射科写法：部位 + 密度 + 尺寸）。
POSITIVE_GOLD = "右下肺见一磨玻璃结节，大小约 8mm。"
#: 阴性金标准。
NEGATIVE_GOLD = "双肺未见明显结节影。"

#: 并发验收的规模（两千例规模压测在 ``scripts/bench_scale.py`` 里做）。
SCALE = 120


class ModelBackedNormalizer:
    """声明自己**走模型端点**的归并器（测试替身）。

    语义归并（任务 21 / 24）会走院内模型服务 —— 那条出口的在途上限
    比推理端点低，引擎据此给**模型端点**单独一个闸门。
    这条声明就是那个闸门的开关：归并器不声明，就不占那条出口的额度
    （当前这一轮的规则基线纯本地不出网，本来也没有请求）。
    """

    uses_model_endpoint = True

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay

    def __call__(self, case: CaseInput) -> GoldLabels:
        time.sleep(self.delay)
        return normalize_gold_standard(case.gold_standard_text)


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
    concurrency: int = 4,
    endpoint_limits: dict[str, int] | None = None,
) -> EvalRequest:
    return EvalRequest(
        run_id=run_id,
        model_id="demo-model",
        model_version="v1.0.0",
        dataset_ref="并发验收样例",
        cases=tuple(cases),
        options=RunOptions(
            concurrency=concurrency,
            endpoint_limits=dict(endpoint_limits or {}),
        ),
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


# --------------------------------------------------------------------------
# 1. 并发有没有跑漏例
# --------------------------------------------------------------------------
def test_concurrent_fan_out_calls_each_case_exactly_once(tmp_path, full_env, monkeypatch):
    """并发跑：总调用次数 = 病例数，每例恰一次调用，无重复调用。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(
            _request(_cases(SCALE), run_id="conc-call-once", concurrency=8)
        )
    finally:
        stub.stop()

    # ---- 出站请求数 = 病例数（没有漏调，也没有重调）----
    assert len(stub.requests) == SCALE
    assert len(response.case_rows) == SCALE

    # ---- 每个检查号恰好被调一次（无重复调用）----
    requested = [req["body"]["study_uid"] for req in stub.requests]
    assert len(set(requested)) == SCALE, "同一个检查号被调了不止一次"

    # ---- 逐例行按输入顺序返回（表格回填按行号写回，顺序不能乱）----
    assert [row.accession_no for row in response.case_rows] == [
        f"ACC-{index:04d}" for index in range(SCALE)
    ]
    assert {row.verdict for row in response.case_rows} == {VERDICT_HIT}

    # ---- 底账逐例一条，id 唯一 ----
    ledger = _ledger(tmp_path / "runs" / "conc-call-once")
    assert len(ledger) == SCALE
    assert len({record["tool_call_id"] for record in ledger}) == SCALE


def test_concurrent_run_reaches_the_same_states_as_a_serial_run(
    tmp_path, full_env, monkeypatch
):
    """并发跑完，run 与逐例状态与串行跑一样（状态机不被并发跑歪）。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        engine.run(_request(_cases(20), run_id="conc-state", concurrency=8))
        store = engine.test_store  # type: ignore[attr-defined]
    finally:
        stub.stop()

    assert store.get_run("conc-state").status == RUN_STATUS_EVALUATED
    assert store.count_cases("conc-state") == 20
    store.close()


# --------------------------------------------------------------------------
# 2. 限流是不是真限住了
# --------------------------------------------------------------------------
def test_endpoint_limit_caps_in_flight_requests_without_dropping_any(
    tmp_path, full_env, monkeypatch
):
    """推理端点上限 = 2：同时在途不超过 2，超限的**排队**（一个都不丢）。"""

    # 替身给 10 毫秒响应延迟：本地替身太快的话，8 个线程 rarely 同时挤在
    # 闸门上，排队数就会随调度抖动 —— 断言要稳，就得让并发真的撞上闸门。
    stub = InferenceStub(delay_seconds=0.01)
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(
            _request(
                _cases(SCALE),
                run_id="conc-limit",
                concurrency=8,
                endpoint_limits={"inference": 2},
            )
        )
        stats = engine.limiters.stats()  # type: ignore[union-attr]
    finally:
        stub.stop()

    # ---- 服务端观察到的同时在途请求数 ≤ 上限 ----
    assert stub.peak_in_flight <= 2, "端点上限没限住"
    assert stub.peak_in_flight >= 1

    # ---- 引擎侧的限流统计与之一致 ----
    inference_stats = stats["inference"]
    assert inference_stats["max_concurrency"] == 2
    assert inference_stats["peak_in_flight"] <= 2
    assert inference_stats["acquisitions"] == SCALE

    # ---- 超限的请求排队了，但**一个都没丢** ----
    assert inference_stats["queued"] > 0, "并发 8 打上限 2，应当有请求排队"
    assert len(stub.requests) == SCALE, "排队不能变成丢弃"
    assert len(response.case_rows) == SCALE


def test_each_endpoint_has_its_own_limit(tmp_path, full_env, monkeypatch):
    """**逐端点独立上限**：模型端点卡到 1 时，推理端点不受它的约束。

    这是「一个总闸门管两个端点」不行的原因 —— 模型端点打满时，
    推理端点的请求不该跟着排队等一个轮不到的额度。

    模型端点限到 1 之后**峰值必然是 1**（只有一条额度），
    推理端点该跑的调用一次不少 —— 两条闸门各算各的。
    """

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path, normalizer=ModelBackedNormalizer(delay=0.005))
        response = engine.run(
            _request(
                _cases(40),
                run_id="conc-two-endpoints",
                concurrency=8,
                endpoint_limits={"inference": 8, "model": 1},
            )
        )
        stats = engine.limiters.stats()  # type: ignore[union-attr]
    finally:
        stub.stop()

    # ---- 模型端点：上限 1，在途峰值就是 1，其余全部排队 ----
    model_stats = stats["model"]
    assert model_stats["max_concurrency"] == 1
    assert model_stats["peak_in_flight"] == 1
    assert model_stats["acquisitions"] == 40
    assert model_stats["queued"] == 39

    # ---- 推理端点：自己的上限仍是 8，一个调用都没少 ----
    inference_stats = stats["inference"]
    assert inference_stats["max_concurrency"] == 8
    assert inference_stats["acquisitions"] == 40
    assert len(stub.requests) == 40, "模型端点限流不该连累推理端点的调用"
    assert len(response.case_rows) == 40


def test_unknown_endpoint_limit_name_is_rejected(tmp_path, full_env, monkeypatch):
    """端点名写错当场报错（限流写错名字等于限流没开）。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        with pytest.raises(Exception) as excinfo:
            engine.run(
                _request(
                    _cases(2),
                    run_id="conc-bad-limit",
                    concurrency=2,
                    endpoint_limits={"inferencee": 2},
                )
            )
    finally:
        stub.stop()

    assert getattr(excinfo.value, "code", None) == ErrorCode.CONTRACT_INVALID
    assert "inferencee" in str(excinfo.value)


# --------------------------------------------------------------------------
# 3. 并发有没有把结论跑歪
# --------------------------------------------------------------------------
def test_concurrent_and_serial_runs_produce_identical_verdicts(
    tmp_path, full_env, monkeypatch
):
    """同一批输入，串行跑与并发跑**逐例判定一致**。

    失败例与挂起例也必须一一对应 —— 「跑得更快」不能以「判得不一样」为代价。
    """

    stub = InferenceStub(fail_study_uids=["STUDY-0003", "STUDY-0007"])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        serial_engine = _engine(tmp_path / "serial")
        serial = serial_engine.run(
            _request(_cases(60), run_id="parity-serial", concurrency=1)
        )
        parallel_engine = _engine(tmp_path / "parallel")
        parallel = parallel_engine.run(
            _request(_cases(60), run_id="parity-parallel", concurrency=8)
        )
    finally:
        stub.stop()

    assert [row.verdict for row in serial.case_rows] == [
        row.verdict for row in parallel.case_rows
    ]
    assert [row.error_code for row in serial.case_rows] == [
        row.error_code for row in parallel.case_rows
    ]
    assert [row.needs_review for row in serial.case_rows] == [
        row.needs_review for row in parallel.case_rows
    ]
    # 两边都该有同样的失败例（证明这次跑法确实踩到了失败路径）
    assert serial.summary.failed_count == parallel.summary.failed_count == 2


def test_concurrent_run_keeps_failed_cases_out_of_the_metric_denominator(
    tmp_path, full_env, monkeypatch
):
    """并发跑时失败例照样不进指标分母（并发不能改口径）。"""

    stub = InferenceStub(fail_study_uids=["STUDY-0000"])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(
            _request(_cases(30), run_id="conc-denominator", concurrency=6)
        )
    finally:
        stub.stop()

    assert response.summary.failed_count == 1
    assert response.summary.evaluated_cases == 29
    failed_rows = [row for row in response.case_rows if row.verdict == VERDICT_FAILED]
    assert len(failed_rows) == 1
    assert failed_rows[0].error_code == ErrorCode.INFERENCE_5XX.value


# --------------------------------------------------------------------------
# 4. 失败能不能被看见
# --------------------------------------------------------------------------
def test_every_failure_can_be_traced_back_to_its_call(tmp_path, full_env, monkeypatch):
    """失败可观测：每类错误码都能定位到具体是哪一例，且能反查回那次调用。"""

    stub = InferenceStub(fail_study_uids=["STUDY-0002", "STUDY-0005"])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(
            _request(_cases(40), run_id="conc-observable", concurrency=6)
        )
    finally:
        stub.stop()

    ledger = _ledger(tmp_path / "runs" / "conc-observable")
    index = {record["tool_call_id"]: record for record in ledger}

    failed = [row for row in response.case_rows if row.verdict == VERDICT_FAILED]
    assert len(failed) == 2
    for row in failed:
        assert row.error_code == ErrorCode.INFERENCE_5XX.value
        assert row.failure_reason, "失败例必须写明失败原因"
        # ---- 反查：逐例行里的调用 id 能在底账里命中，且指向同一例 ----
        assert row.tool_call_id in index, "失败例在调用底账里查不到"
        record = index[row.tool_call_id]
        assert record["args"]["accession_no"] == row.accession_no
        assert ErrorCode.INFERENCE_5XX.value in record["content"]

    # ---- 反过来也成立：底账里没有「失败但无记录」的例 ----
    failed_ids = {row.tool_call_id for row in failed}
    for record in ledger:
        content = record["content"]
        if ErrorCode.INFERENCE_5XX.value in content:
            assert record["tool_call_id"] in failed_ids


# --------------------------------------------------------------------------
# 5. 慢的时候能不能说清慢在哪
# --------------------------------------------------------------------------
def test_case_timing_ledger_separates_compute_from_queueing(
    tmp_path, full_env, monkeypatch
):
    """逐例耗时台账落盘：算的时长与排队的时长分开记，端点统计一并落。"""

    # 同样给一点响应延迟，确保「排队」这件事真的发生过（见上一条注释）。
    stub = InferenceStub(delay_seconds=0.01)
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        engine.run(
            _request(
                _cases(40),
                run_id="conc-timings",
                concurrency=8,
                endpoint_limits={"inference": 2},
            )
        )
    finally:
        stub.stop()

    payload = json.loads(
        (tmp_path / "runs" / "conc-timings" / CASE_TIMINGS_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert payload["concurrency"] == 8
    assert payload["case_count"] == 40
    assert len(payload["cases"]) == 40
    # ---- 逐例台账逐字逐项都有 ----
    first = payload["cases"][0]
    for key in ("accession_no", "elapsed_ms", "queued_ms", "verdict", "error_code"):
        assert key in first
    # ---- 排队时长确实被记下来了（端点卡到 2，必然有排队）----
    queued_total = sum(item["queued_ms"] for item in payload["cases"])
    assert queued_total > 0
    assert payload["endpoints"]["inference"]["max_concurrency"] == 2
    assert payload["endpoints"]["inference"]["peak_in_flight"] <= 2


def test_connection_pool_is_sized_to_the_concurrency(tmp_path, full_env, monkeypatch):
    """连接池按并发数扩容 —— 否则并发一高就在反复重握手。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        engine.run(_request(_cases(10), run_id="conc-pool", concurrency=16))
    finally:
        stub.stop()

    adapter = engine.client.session.get_adapter("https://example.invalid")
    assert adapter._pool_maxsize >= 16  # noqa: SLF001 - 断言连接池尺寸


# --------------------------------------------------------------------------
# 6. CLI 能不能把并发交到运维手上
# --------------------------------------------------------------------------
def test_cli_accepts_concurrency_and_endpoint_limits(tmp_path, full_env, monkeypatch):
    """CLI 的 ``--concurrency`` / ``--endpoint-limits`` 真的生效。"""

    from nodule_eval import cli

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(
        json.dumps(
            {
                "run_id": "cli-conc",
                "model_id": "demo-model",
                "model_version": "v1.0.0",
                "dataset_ref": "CLI 并发样例",
                "cases": [
                    {
                        "accession_no": f"CLI-{index:04d}",
                        "gold_standard_text": POSITIVE_GOLD,
                        "image_ref": f"dx://local/{index:04d}",
                        "study_uid": f"STUDY-{index:04d}",
                    }
                    for index in range(20)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        exit_code = cli.main(
            [
                "run",
                "--cases",
                str(cases_file),
                "--model-id",
                "demo-model",
                "--run-id",
                "cli-conc",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--store",
                str(tmp_path / "store" / "nodule_eval.sqlite3"),
                "--concurrency",
                "8",
                "--endpoint-limits",
                "inference=4",
            ]
        )
    finally:
        stub.stop()

    assert exit_code == 0
    payload = json.loads(
        (tmp_path / "runs" / "cli-conc" / CASE_TIMINGS_FILENAME).read_text(encoding="utf-8")
    )
    assert payload["concurrency"] == 8
    assert payload["endpoints"]["inference"]["max_concurrency"] == 4


def test_cli_rejects_a_malformed_endpoint_limit(tmp_path, full_env, monkeypatch, capsys):
    """端点上限写法不对，当场报错并说清正确写法（退出码非零）。"""

    from nodule_eval import cli

    cases_file = tmp_path / "cases.json"
    cases_file.write_text(
        json.dumps(
            {
                "run_id": "cli-bad",
                "model_id": "demo-model",
                "model_version": "v1.0.0",
                "dataset_ref": "CLI 端点上限报错样例",
                "cases": [
                    {
                        "accession_no": "CLI-0000",
                        "gold_standard_text": POSITIVE_GOLD,
                        "image_ref": "dx://local/0000",
                        "study_uid": "STUDY-0000",
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
            "cli-bad",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--store",
            str(tmp_path / "store" / "nodule_eval.sqlite3"),
            "--endpoint-limits",
            "inference",
        ]
    )

    assert exit_code == 2, "可预期的产品错误要走退出码 2，不是抛栈"
    captured = capsys.readouterr()
    assert ErrorCode.CONTRACT_INVALID.value in captured.err
    assert "端点名=数字" in captured.err


# --------------------------------------------------------------------------
# 7. 压测脚本本身的契约
# --------------------------------------------------------------------------
def test_bench_report_contract_is_stable(tmp_path, full_env, monkeypatch):
    """压测脚本的报告结构稳定，且**逐例判定一致**这条断言真的会跑。

    报告是运维看「这轮快不快、准不准」的凭据 —— 字段名变了，
    拿报告做对比的人就接不上了。
    """

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import bench_scale

    from bench_load_server import LoadGenerator

    generator = LoadGenerator(failure_rate=0.2, seed=7)
    generator.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", generator.base_url)
    try:
        cases, counts = bench_scale.build_cases(40, ambiguity_rate=0.05, seed=7)
        assert counts["total"] == 40
        serial = bench_scale.run_once(
            cases=cases,
            run_id="bench-test-serial",
            model_id="demo-model",
            model_version="v1.0.0",
            concurrency=1,
            endpoint_limits={},
            runs_dir=tmp_path / "runs",
            dataset_ref="压测契约样例",
            backoff_sleep="off",
        )
        parallel = bench_scale.run_once(
            cases=cases,
            run_id="bench-test-parallel",
            model_id="demo-model",
            model_version="v1.0.0",
            concurrency=6,
            endpoint_limits={},
            runs_dir=tmp_path / "runs",
            dataset_ref="压测契约样例",
            backoff_sleep="off",
        )
    finally:
        generator.stop()

    # ---- 报告字段齐备 ----
    for key in (
        "total_seconds",
        "cases_per_second",
        "elapsed_ms",
        "queued_ms",
        "verdict_distribution",
        "error_code_distribution",
        "endpoint_stats",
        "failure_observability",
    ):
        assert key in serial
    assert set(serial["elapsed_ms"]) >= {"p50", "p95", "p99", "max", "mean"}

    # ---- 注入的失败真的落到了错误码上 ----
    assert serial["error_code_distribution"], "注入了失败却没有错误码分布"
    assert parallel["error_code_distribution"] == serial["error_code_distribution"]

    # ---- 失败可观测：每一类错误码都定位到了具体例，没有「失败但无记录」 ----
    observability = serial["failure_observability"]
    assert observability["unlocated_count"] == 0
    assert sum(observability["located_by_error_code"].values()) == sum(
        serial["error_code_distribution"].values()
    )

    # ---- 并发与串行逐例判定一致 ----
    comparison = bench_scale.compare_runs(serial, parallel)
    assert comparison["identical"] is True
    assert comparison["compared_cases"] == 40
    assert comparison["verdict_difference_count"] == 0
    assert comparison["error_code_difference_count"] == 0


def test_bench_refuses_to_run_without_credentials(tmp_path, monkeypatch, capsys):
    """压测缺凭据时**指名报错**退出，不悄悄改打本地替身。"""

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import bench_scale

    for name in bench_scale.REQUIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    exit_code = bench_scale.main(["--target", "real", "--cases", "10"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "INFERENCE_BASE_URL" in captured.err
    assert "不会改用本地替身" in captured.err
