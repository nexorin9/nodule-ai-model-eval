"""对照实验（规则基线 vs 模型归并）的自检（任务 25）。

**这个测试守住三件事**：

1. **裁判集够大、口径没漂** —— 条数不少于 60 条；数据文件坏了要**报错**，
   不许回落内置兜底集（兜底集会让对照表看起来是绿的）；
2. **对照表的结构稳定** —— CSV 的列、JSON 的键是**固定**的（下游脚本按列读）；
3. **结果可复现** —— 同一条命令跑两遍，CSV 与汇总 JSON **逐字节一致**。
   这一条是「可复现」三个字的落点：标注集版本号、内容哈希、随机源与模型参数
   都写进文件头，跑第二遍时对不上就查得出来。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _path in (
    str(PROJECT_ROOT / "src"),
    str(PROJECT_ROOT / "tests"),
    str(PROJECT_ROOT / "scripts"),
):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from nodule_eval.errors import ErrorCode, NoduleEvalError  # noqa: E402
from nodule_eval.goldstd.benchmark import (  # noqa: E402
    ERROR_TYPE_LABELS,
    OVERALL_FIELDS,
    compare_labels,
    load_benchmark_set,
    score_normalizer,
)
from nodule_eval.goldstd.schema import GoldLabels, LesionLabels  # noqa: E402

import compare_normalizers as cmp_script  # noqa: E402  —— scripts/ 下的命令行入口


#: 裁判集最少条数 —— 少于这个数，对照表说明不了问题。
MIN_BENCHMARK_CASES = 60


# --------------------------------------------------------------------------
# 裁判集
# --------------------------------------------------------------------------


def test_benchmark_has_at_least_sixty_cases() -> None:
    """裁判集条数不少于 60 条（任务硬条件）。"""

    benchmark = load_benchmark_set()
    assert len(benchmark.cases) >= MIN_BENCHMARK_CASES, (
        f"裁判集只有 {len(benchmark.cases)} 条，少于 {MIN_BENCHMARK_CASES} 条"
    )


def test_benchmark_covers_all_categories() -> None:
    """六类真实写法都要有 —— 少了哪类，那一类的失效就看不见。"""

    categories = set(load_benchmark_set().by_category())
    assert categories == {
        "基础阳性",
        "基础阴性",
        "否定作用域",
        "多病灶",
        "跨行拼接",
        "量纲混写",
        "别名与书写变体",
        "术语外写法",
    }


def test_benchmark_ids_unique_and_labels_complete() -> None:
    """id 不重复；每条的标注四元组齐全（缺字段就是编造，必须当场报错）。"""

    benchmark = load_benchmark_set()
    ids = [case.id for case in benchmark.cases]
    assert len(set(ids)) == len(ids)
    for case in benchmark.cases:
        assert set(case.expect) >= {"presence", "location", "size_mm", "density", "lesions"}
        assert case.text.strip()


def test_benchmark_sha256_pinned() -> None:
    """数据文件的内容哈希写进对照表文件头 —— 裁判集改了，对照表的数字就得重算。"""

    benchmark = load_benchmark_set()
    assert len(benchmark.sha256) == 64
    assert benchmark.sha256 == load_benchmark_set().sha256


def test_benchmark_loader_rejects_broken_data(tmp_path: Path) -> None:
    """裁判集坏了要**报错**，不许回落内置兜底集。"""

    missing = tmp_path / "not-there.yaml"
    with pytest.raises(NoduleEvalError) as excinfo:
        load_benchmark_set(missing)
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "not-there.yaml" in excinfo.value.detail

    broken = tmp_path / "broken.yaml"
    broken.write_text(
        "version: 1\ncases:\n  - id: x\n    text: 'abc'\n",
        encoding="utf-8",
    )
    with pytest.raises(NoduleEvalError) as excinfo:
        load_benchmark_set(broken)
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "expect" in excinfo.value.detail

    duplicated = tmp_path / "dup.yaml"
    duplicated.write_text(
        "version: 1\n"
        "cases:\n"
        "  - id: same\n"
        "    text: 'a'\n"
        "    expect: {presence: true, location: null, size_mm: null, density: null,"
        " lesions: 1, needs_human_review: false}\n"
        "  - id: same\n"
        "    text: 'b'\n"
        "    expect: {presence: true, location: null, size_mm: null, density: null,"
        " lesions: 1, needs_human_review: false}\n",
        encoding="utf-8",
    )
    with pytest.raises(NoduleEvalError) as excinfo:
        load_benchmark_set(duplicated)
    assert "重复" in excinfo.value.detail


# --------------------------------------------------------------------------
# 打分口径
# --------------------------------------------------------------------------


def test_scoring_classifies_four_outcomes() -> None:
    """四类结局分得清：该读出来的没读出来 / 不该给的给了 / 值不相等 / 判对。"""

    benchmark = load_benchmark_set()
    case = next(item for item in benchmark.cases if item.id == "bm-001")

    # 全对
    good = GoldLabels(
        presence=True,
        location="右肺下叶",
        size_mm=8.0,
        density="磨玻璃",
        lesions=(
            LesionLabels(location="右肺下叶", size_mm=8.0, density="磨玻璃"),
        ),
    )
    outcomes = compare_labels(case, good)
    assert all(item.status == "correct" for item in outcomes)

    # 漏召：该读出来的没读出来
    missed = GoldLabels(presence=True, location=None, size_mm=None, density=None, lesions=())
    statuses = {item.field: item.status for item in compare_labels(case, missed)}
    assert statuses["location"] == "missed"
    assert statuses["size_mm"] == "missed"
    assert statuses["density"] == "missed"

    # 误召：原文没写却给了值（bm-004 的密度标注是 null —— 原文没写密度）
    no_density = next(item for item in benchmark.cases if item.id == "bm-004")
    assert no_density.expect["density"] is None
    over = GoldLabels(
        presence=True,
        location="右肺中叶",
        size_mm=5.0,
        density="实性",
        lesions=(),
    )
    statuses = {item.field: item.status for item in compare_labels(no_density, over)}
    assert statuses["density"] == "over_call"

    # 错值
    wrong = GoldLabels(
        presence=True,
        location="右肺下叶",
        size_mm=9.0,
        density="磨玻璃",
        lesions=(),
    )
    statuses = {item.field: item.status for item in compare_labels(case, wrong)}
    assert statuses["size_mm"] == "wrong"


def test_presence_polarity_errors_are_separated() -> None:
    """存在性的两类错误必须分开：**阳性漏召**与**阴性误召**方向相反。"""

    benchmark = load_benchmark_set()
    positive = next(item for item in benchmark.cases if item.id == "bm-001")
    negative = next(item for item in benchmark.cases if item.id == "bm-011")

    flipped = GoldLabels(presence=False, lesions=())
    outcome = next(
        item
        for item in compare_labels(positive, flipped)
        if item.field == "presence"
    )
    assert outcome.status == "missed"
    assert outcome.error_type == "presence_false_negative"

    flipped = GoldLabels(presence=True, lesions=())
    outcome = next(
        item for item in compare_labels(negative, flipped) if item.field == "presence"
    )
    assert outcome.status == "over_call"
    assert outcome.error_type == "presence_false_positive"


def test_recall_never_exceeds_one_hundred_percent() -> None:
    """召回率是比例 —— 「两边都没读出来」不算一次「读对了」。

    没有这一条，分母与分子混在一起算，召回率会超过 100%。
    """

    benchmark = load_benchmark_set()
    empty = {case.id: GoldLabels() for case in benchmark.cases}
    score = score_normalizer("empty", benchmark.cases, empty)
    for field in OVERALL_FIELDS:
        field_score = score.fields[field]
        assert 0.0 <= field_score.recall <= 1.0
        assert 0.0 <= field_score.accuracy <= 1.0
        assert 0.0 <= field_score.false_call_rate <= 1.0


def test_error_type_codes_all_have_labels() -> None:
    """错误码都有中文标签 —— 输出表里不留裸码。"""

    benchmark = load_benchmark_set()
    score = score_normalizer("rule", benchmark.cases, {})
    for code in score.error_types:
        assert code in ERROR_TYPE_LABELS


def test_missing_case_counts_as_missed_not_skipped() -> None:
    """归并器没跑出来的例按「读不出来」记 —— 跳过会让它在表上消失。"""

    benchmark = load_benchmark_set()
    score = score_normalizer("empty", benchmark.cases, {})
    assert score.total_cases == len(benchmark.cases)
    assert score.fields["presence"].missed == len(benchmark.cases)


# --------------------------------------------------------------------------
# 端到端：跑脚本
# --------------------------------------------------------------------------


def _run(out_dir: Path, *extra: str) -> int:
    return cmp_script.main(
        ["--model-mode", "double", "--out-dir", str(out_dir), *extra]
    )


def test_compare_table_output_structure_is_stable(tmp_path: Path) -> None:
    """对照表的**结构**固定：CSV 列固定、JSON 键固定。"""

    assert _run(tmp_path) == 0

    csv_path = tmp_path / "compare-normalizers.csv"
    with csv_path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        rows = list(reader)
    assert header == list(cmp_script.CSV_COLUMNS)
    assert rows, "对照表明细不该是空的"
    # 每一行都得有这五列的内容。
    assert all(len(row) == len(cmp_script.CSV_COLUMNS) for row in rows)

    summary = json.loads((tmp_path / "compare-summary.json").read_text(encoding="utf-8"))
    assert set(summary) == {"benchmark", "run", "normalizers", "delta", "conclusion"}
    assert set(summary["normalizers"]) == {"rule", "model"}
    assert set(summary["run"]) >= {
        "seed",
        "model_mode",
        "model_endpoint_kind",
        "model_temperature",
        "source_sha256",
    }
    for name in ("rule", "model"):
        payload = summary["normalizers"][name]
        assert set(payload) >= {
            "name",
            "total_cases",
            "overall_accuracy",
            "fields",
            "error_types",
            "categories",
        }
        assert set(payload["fields"]) == set(OVERALL_FIELDS) | {"lesions"}


def test_two_runs_produce_identical_output(tmp_path: Path) -> None:
    """**重复运行两次结果一致** —— 这条是「可复现」的落点。"""

    first = tmp_path / "run-a"
    second = tmp_path / "run-b"
    assert _run(first) == 0
    assert _run(second) == 0

    for name in ("compare-normalizers.csv", "compare-summary.json"):
        left = (first / name).read_bytes()
        right = (second / name).read_bytes()
        assert left == right, f"{name} 两次运行结果不一致 —— 可复现性被破坏了"


def test_summary_header_records_reproducibility_conditions(tmp_path: Path) -> None:
    """文件头记下：标注集版本号 + 内容哈希 + 随机源 + 模型参数。"""

    assert _run(tmp_path) == 0
    summary = json.loads((tmp_path / "compare-summary.json").read_text(encoding="utf-8"))

    benchmark = load_benchmark_set()
    assert summary["benchmark"]["version"] == benchmark.version
    assert summary["benchmark"]["sha256"] == benchmark.sha256
    assert summary["benchmark"]["case_count"] >= MIN_BENCHMARK_CASES

    assert summary["run"]["seed"] == cmp_script.DEFAULT_SEED
    assert summary["run"]["model_temperature"] == 0.0
    # 出处可追：提示词 / 等价表 / 规则 / 契约 / 语义归并五个源文件的哈希都在。
    for name in ("prompt", "labels", "rules", "schema", "merge"):
        assert len(summary["run"]["source_sha256"][name]) == 64


def test_sampled_run_is_reproducible(tmp_path: Path) -> None:
    """按随机源抽样也要可复现 —— 同 seed 抽到同一批。"""

    first = tmp_path / "sample-a"
    second = tmp_path / "sample-b"
    assert _run(first, "--sample", "12") == 0
    assert _run(second, "--sample", "12") == 0
    assert (first / "compare-summary.json").read_bytes() == (
        second / "compare-summary.json"
    ).read_bytes()

    summary = json.loads((first / "compare-summary.json").read_text(encoding="utf-8"))
    assert summary["benchmark"]["case_count"] == 12
    assert summary["run"]["sample"] == 12


def test_replay_mode_reproduces_recorded_run(tmp_path: Path) -> None:
    """录下来的模型输出可以**回放**，回放结果与录制时一致。"""

    recorded = tmp_path / "model-log.jsonl"
    first = tmp_path / "record-run"
    assert _run(first, "--record", str(recorded)) == 0

    replay_dir = tmp_path / "replay-run"
    exit_code = cmp_script.main(
        [
            "--model-mode",
            "replay",
            "--model-log",
            str(recorded),
            "--out-dir",
            str(replay_dir),
        ]
    )
    assert exit_code == 0

    # 明细逐字节一致（汇总 JSON 只有「模型侧怎么服务的」这一处该不同）。
    assert (first / "compare-normalizers.csv").read_bytes() == (
        replay_dir / "compare-normalizers.csv"
    ).read_bytes()

    left = json.loads((first / "compare-summary.json").read_text(encoding="utf-8"))
    right = json.loads((replay_dir / "compare-summary.json").read_text(encoding="utf-8"))
    assert left["normalizers"] == right["normalizers"]
    assert left["delta"] == right["delta"]
    assert right["run"]["model_endpoint_kind"] == "recorded_model_log"


def test_gateway_mode_demands_credentials_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """默认模式是**真端点**；缺凭据要指名报错，不许悄悄改打本地替身。"""

    from nodule_eval.config import REQUIRED_ENV_VARS

    for name in REQUIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(NoduleEvalError) as excinfo:
        cmp_script.main(["--model-mode", "gateway", "--out-dir", str(tmp_path)])
    assert excinfo.value.code == ErrorCode.CREDENTIAL_MISSING
    assert "OPENAI_BASE_URL" in excinfo.value.detail


def test_double_mode_is_labelled_as_not_a_real_model(tmp_path: Path) -> None:
    """本地协议替身那一侧必须**写明它不是真实大模型** —— 数字不能被误读。"""

    assert _run(tmp_path) == 0
    summary = json.loads((tmp_path / "compare-summary.json").read_text(encoding="utf-8"))
    assert summary["run"]["model_endpoint_kind"] == "local_protocol_double"

    meta = json.loads((tmp_path / "compare-run-meta.json").read_text(encoding="utf-8"))
    assert meta["model_endpoint_kind"] == "local_protocol_double"


def test_model_side_runs_through_real_http_gateway(tmp_path: Path) -> None:
    """模型侧**真走 HTTP**：真 socket、真请求体、真响应解析。"""

    assert _run(tmp_path) == 0
    summary = json.loads((tmp_path / "compare-summary.json").read_text(encoding="utf-8"))
    model_score = summary["normalizers"]["model"]
    assert model_score["total_cases"] >= MIN_BENCHMARK_CASES
    # 走完一遍之后**一例都没降级** —— 回指校验与标签复核全部放行。
    assert model_score["degraded_cases"] == 0


def test_rule_failures_land_in_documented_error_types(tmp_path: Path) -> None:
    """规则基线的失效必须落进**已知的那几类** —— 出现新的一类，说明口径漂了。"""

    assert _run(tmp_path) == 0
    summary = json.loads((tmp_path / "compare-summary.json").read_text(encoding="utf-8"))
    rule_errors = set(summary["normalizers"]["rule"]["error_types"])

    assert rule_errors <= set(ERROR_TYPE_LABELS)
    # 「规则读不出存在性」与「否定误召」这两类必须出现 —— 它们正是
    # 项目最在意的那两类（一次假阴性就是一次假漏诊）。
    assert "presence_unreadable" in rule_errors
    assert "presence_false_positive" in rule_errors
