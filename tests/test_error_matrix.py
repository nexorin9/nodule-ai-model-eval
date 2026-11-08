"""错误码落地矩阵：同一个错误码，在**三个面**上必须是同一句话。

这一组断言回答科室与信息科最常问的三个问题：

1. **同一个错误，为什么表格里写「推理服务暂时不可用」、终端写「推理服务异常」？**
   —— 不允许。文案只有一个来源（``errors.py``），表格 ``failure_reason`` 列、
   终端输出、``summary.json`` 三处读的是**同一个函数**的返回值。
2. **这个错误码到底在哪儿会触发？** —— 落地矩阵逐码登记触发点，
   并且**在源码里真的找得到** ``ErrorCode.X``（登记了却没人用 = 假条款）。
3. **4xx 会不会白白消耗退避预算？** —— 不会：参数错误重试必然还是被拒，
   4xx 立刻判该例失败，只发一次请求。

**断言走的是真实出站**：替身是真监听 socket 的 HTTP 服务，
产品的 ``requests.Session`` / URL 拼接 / Bearer 头一行都没改。
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

import pytest
from stub_inference import InferenceStub

from nodule_eval.adapters.http import RETRYABLE_STATUSES, HttpClient
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import VERDICT_FAILED, CaseInput, EvalRequest
from nodule_eval.engine import Engine
from nodule_eval.error_matrix import (
    build_matrix,
    failure_breakdown,
    render_failure_lines,
    render_matrix_markdown,
    retry_policy_by_status,
    sample_matches_template,
    scan_hardcoded_error_text,
    visible_surface_matrix,
)
from nodule_eval.errors import (
    ERROR_CODES,
    ERROR_TABLE,
    SURFACE_SUMMARY,
    SURFACE_TABLE,
    SURFACE_TERMINAL,
    USER_VISIBLE_SURFACES,
    ErrorCode,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TESTS_ROOT = PROJECT_ROOT / "tests"

#: 阳性金标准（放射科写法：部位 + 密度 + 尺寸）。
POSITIVE_GOLD = "右下肺见一磨玻璃结节，大小约 8mm。"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
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


def _engine(tmp_path: Path, *, sleeps: list[float] | None = None) -> Engine:
    """造一个挂上 run store 的引擎（退避不真睡，但**把要睡的秒数记下来**）。"""

    recorded: list[float] = sleeps if sleeps is not None else []
    config = load_config()
    spec = load_model_spec("demo-model")
    from nodule_eval.store import RunStore

    store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
    engine = Engine(
        config,
        spec,
        client=HttpClient(sleep=recorded.append),
        runs_dir=tmp_path / "runs",
        store=store,
    )
    engine.test_store = store  # type: ignore[attr-defined]
    engine.test_sleeps = recorded  # type: ignore[attr-defined]
    return engine


def _request(cases: Sequence[CaseInput], *, run_id: str) -> EvalRequest:
    return EvalRequest(
        run_id=run_id,
        model_id="demo-model",
        model_version="v1.0.0",
        dataset_ref="错误码落地矩阵验收样例",
        cases=tuple(cases),
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


# --------------------------------------------------------------------------
# 1. 落地矩阵本身：每码都有触发点、都在源码里真的用着
# --------------------------------------------------------------------------
def test_every_error_code_declares_its_trigger_points():
    """每个错误码都要登记「在哪儿触发」—— 没有登记的等于没人管。"""

    rows = build_matrix()
    assert len(rows) == len(ERROR_CODES)
    for row in rows:
        assert row.trigger_points, f"{row.error_code} 没登记触发点"
        assert all(point.strip() for point in row.trigger_points)
        assert row.message_template.strip(), f"{row.error_code} 没有中文文案模板"
        assert row.visible_in, f"{row.error_code} 没登记用户可见位置"


def test_every_error_code_is_actually_raised_somewhere_in_the_source():
    """登记了的错误码，源码里必须真的写着 ``ErrorCode.X``。

    登记了却没人用 = 假条款；用了却没登记 = 漏条款。两侧都在这里兜住。
    """

    unreachable = [row.error_code for row in build_matrix() if not row.reachable]
    assert unreachable == [], f"这些错误码在源码里找不到触发点：{unreachable}"


def test_every_error_code_has_a_reachable_test():
    """每个错误码都要有**可触发路径的测试** —— 否则它只是一行声明。"""

    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(TESTS_ROOT.glob("test_*.py"))
    )
    missing = [code for code in ERROR_CODES if code not in sources]
    assert missing == [], f"这些错误码没有任何测试覆盖：{missing}"


def test_matrix_document_is_up_to_date():
    """``docs/error_matrix.md`` 必须与当前代码一致（改表忘了重生成，当场发现）。"""

    target = PROJECT_ROOT / "docs" / "error_matrix.md"
    assert target.is_file(), "错误码落地矩阵文档缺失"
    assert target.read_text(encoding="utf-8") == render_matrix_markdown()

    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "gen_error_matrix.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------
# 2. 文案只从 errors.py 取：业务代码不许再抄一份
# --------------------------------------------------------------------------
def test_no_hardcoded_error_text_in_business_code():
    """业务代码里不许把错误文案硬编码一份。

    抄一份就意味着改模板时会漏改 —— 同一错误码在两层显示两种说法，
    正是「同一个错误三种说法」的根源。
    """

    violations = scan_hardcoded_error_text()
    assert violations == [], violations


def test_error_text_comes_from_the_table_only():
    """错误文案模板的唯一来源是错误码表。"""

    for code in ERROR_CODES:
        spec = ERROR_TABLE[code]
        assert spec.message.strip()
        assert "{" in spec.message or spec.message.endswith("："), (
            f"{code} 的模板看起来不像可填充的文案模板"
        )


# --------------------------------------------------------------------------
# 3. 三处文案一致：表格 failure_reason 列 / 终端输出 / summary.json
# --------------------------------------------------------------------------
def test_three_surfaces_say_the_same_thing(tmp_path, full_env, monkeypatch):
    """同一轮里两种失败：表格列、终端输出、``summary.json`` 逐字一致。"""

    stub = InferenceStub(
        fail_status_by_study_uid={"STUDY-0000": 400, "STUDY-0001": 500},
        error_body={
            "error": {
                "code": "INVALID_IMAGE_REF",
                "param": "image_ref",
                "message": "图像引用无法定位",
            }
        },
    )
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(3), run_id="matrix-三处"))
        artifacts = engine.last_artifacts
    finally:
        stub.stop()

    assert artifacts is not None
    failed = [row for row in response.case_rows if row.verdict == VERDICT_FAILED]
    assert len(failed) == 2, "两例应当分别落 4xx 与 5xx"

    # ---- 表格那一列 ----
    table_rows = _read_csv(artifacts.case_rows_csv)
    table_reasons = {row["accession_no"]: row["failure_reason"] for row in table_rows}
    assert len(table_reasons) == 3

    # ---- summary.json 那一段 ----
    summary = json.loads(artifacts.summary_json.read_text(encoding="utf-8"))
    failures = summary["failures"]
    assert failures["total"] == 2
    codes = {entry["error_code"] for entry in failures["by_code"]}
    assert codes == {ErrorCode.INFERENCE_4XX.value, ErrorCode.INFERENCE_5XX.value}

    # ---- 终端那几行 ----
    lines = render_failure_lines(failure_breakdown(response.case_rows))
    assert len(lines) == 2

    # **逐字比对**：三个面说的必须是同一句话。
    for entry in failures["by_code"]:
        code = entry["error_code"]
        message = entry["message"]
        # 1) summary 的这句话就是表格某一格里的原话
        assert message in table_reasons.values(), f"{code} 的 summary 文案在表格里找不到"
        # 2) 终端那一行里含同一句话
        matching = [line for line in lines if code in line]
        assert len(matching) == 1, f"终端输出里 {code} 的行数不对"
        assert message in matching[0], f"{code} 的终端文案与 summary 不一致"
        # 3) 该码的全部样本文案都在表格格里
        for sample in entry["samples"]:
            assert sample in table_reasons.values()
            assert sample_matches_template(code, sample), (
                f"{code} 的文案与错误码表的模板对不上：{sample}"
            )
        # 4) 模板就是错误码表里的那份
        assert entry["template"] == ERROR_TABLE[code].message


def test_failure_breakdown_counts_match_the_rows(tmp_path, full_env, monkeypatch):
    """失败汇总的例数与逐例行对得上（按码分组，不重不漏）。"""

    stub = InferenceStub(fail_study_uids=["STUDY-0001", "STUDY-0003"])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(5), run_id="matrix-counts"))
        artifacts = engine.last_artifacts
    finally:
        stub.stop()

    payload = failure_breakdown(response.case_rows)
    assert payload["total"] == 2
    entry = payload["by_code"][0]
    assert entry["error_code"] == ErrorCode.INFERENCE_5XX.value
    assert entry["count"] == 2
    assert entry["retryable"] is True
    assert entry["counts_toward_metrics"] is False

    summary = json.loads(artifacts.summary_json.read_text(encoding="utf-8"))
    assert summary["failures"] == payload


def test_no_failures_means_no_breakdown_line(tmp_path, full_env, monkeypatch):
    """一轮全跑通时，终端**不该**多出一行「失败分布」的空话。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(2), run_id="matrix-clean"))
        artifacts = engine.last_artifacts
    finally:
        stub.stop()

    assert render_failure_lines(failure_breakdown(response.case_rows)) == []
    summary = json.loads(artifacts.summary_json.read_text(encoding="utf-8"))
    assert summary["failures"]["total"] == 0
    assert summary["failures"]["by_code"] == []


# --------------------------------------------------------------------------
# 4. 不可重试的错误立刻失败：不消耗退避预算
# --------------------------------------------------------------------------
def test_4xx_fails_immediately_without_spending_backoff_budget(
    tmp_path, full_env, monkeypatch
):
    """4xx 只发**一次**请求，且一次退避都不睡。

    参数错误重试必然还是被拒 —— 把三次退避预算花在这里，
    代价是这一轮两百例多等十几秒，换来的还是同一个 400。
    """

    sleeps: list[float] = []
    stub = InferenceStub(
        status_sequence=[400],
        error_body={
            "error": {
                "code": "INVALID_IMAGE_REF",
                "param": "image_ref",
                "message": "图像引用无法定位",
            }
        },
    )
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path, sleeps=sleeps)
        response = engine.run(_request(_cases(1), run_id="matrix-4xx"))
    finally:
        stub.stop()

    assert len(stub.requests) == 1, "4xx 不该重试"
    assert sleeps == [], f"4xx 不该消耗退避预算，实际睡了 {sleeps}"
    row = response.case_rows[0]
    assert row.error_code == ErrorCode.INFERENCE_4XX.value
    assert row.verdict == VERDICT_FAILED
    assert ERROR_TABLE[ErrorCode.INFERENCE_4XX.value].retryable is False


def test_5xx_still_spends_the_backoff_budget(tmp_path, full_env, monkeypatch):
    """5xx 仍要退避重试 —— 与 4xx 相反，否则真故障就被当成参数错误。"""

    sleeps: list[float] = []
    stub = InferenceStub(status_sequence=[500, 500, 500])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path, sleeps=sleeps)
        response = engine.run(_request(_cases(1), run_id="matrix-5xx"))
    finally:
        stub.stop()

    assert len(stub.requests) == 3
    assert len(sleeps) == 2, f"5xx 应退避两次，实际 {sleeps}"
    assert response.case_rows[0].error_code == ErrorCode.INFERENCE_5XX.value


def test_retry_policy_declared_in_the_table_matches_the_http_layer():
    """错误码表声明的重试口径 == 出站会话层的实际行为。

    两边对不上就是有人只改了一边 —— 这条断言当场抓住。
    """

    policy = retry_policy_by_status()
    for status in range(400, 600):
        if status not in policy:
            continue
        assert policy[status] == (status in RETRYABLE_STATUSES), (
            f"HTTP {status} 的重试口径与 adapters/http.py 不一致"
        )
    # 4xx 一律不重试（429 与 5xx 除外）
    for status in (400, 401, 403, 404, 409, 422):
        assert policy[status] is False
    assert policy[429] is True
    assert policy[503] is True


# --------------------------------------------------------------------------
# 5. 用户可见位置：三个面都要落到位
# --------------------------------------------------------------------------
def test_visible_surface_matrix_covers_the_three_surfaces():
    """逐例失败必须落在表格 / 终端 / ``summary.json`` 三个面上。"""

    matrix = visible_surface_matrix()
    assert len(matrix) == len(ERROR_CODES)
    for item in matrix:
        code = item["error_code"]
        if code in (
            ErrorCode.INFERENCE_4XX.value,
            ErrorCode.INFERENCE_5XX.value,
            ErrorCode.INFERENCE_TIMEOUT.value,
            ErrorCode.STUDY_UID_MISMATCH.value,
            ErrorCode.AMBIGUOUS_BINDING.value,
        ):
            assert item["in_table"], f"{code} 没落在表格 failure_reason 列"
            assert item["in_terminal"], f"{code} 没落在终端输出"
            assert item["in_summary"], f"{code} 没落在 summary.json"


def test_user_visible_surfaces_are_the_declared_three():
    """三个可见面就是那三个 —— 换名字会让一致性断言失效。"""

    assert USER_VISIBLE_SURFACES == (SURFACE_TABLE, SURFACE_TERMINAL, SURFACE_SUMMARY)


def test_matrix_row_exposes_the_five_landing_dimensions():
    """落地矩阵的五个维度一个都不能少。"""

    for row in build_matrix():
        payload = row.to_dict()
        for key in (
            "error_code",
            "trigger_points",
            "message_template",
            "retryable",
            "counts_toward_metrics",
            "visible_in",
            "code_sites",
        ):
            assert key in payload, f"{row.error_code} 的落地矩阵缺 {key}"
        assert isinstance(payload["retryable"], bool)
        assert isinstance(payload["counts_toward_metrics"], bool)


@pytest.mark.parametrize("code", sorted(ERROR_CODES))
def test_each_error_code_has_a_stable_spec(code: str):
    """逐码核对：声明齐整、模板非空、可见位置非空。"""

    spec = ERROR_TABLE[code]
    assert spec.code.value == code
    assert spec.message.strip()
    assert spec.visible_in
    assert spec.trigger_points
    assert isinstance(spec.retryable, bool)
    assert isinstance(spec.counts_toward_metrics, bool)
