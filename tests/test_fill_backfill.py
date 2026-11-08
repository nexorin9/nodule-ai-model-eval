"""逐例回填与**界面三态**的测试（任务 46）。

回填是本产品**唯一一次「程序往人写的表里写东西」**的动作 ——
所以这里断言的不是「文件能打开」，而是三件要紧的事：

1. **既有列与金标准原文零改动** —— 逐格比对，不是「我们没写那儿」；
   金标准原文一旦被改，事后没法证明当初是按什么判的。
2. **中断不留半截表** —— 回填中途崩了，原表**一个字节都没变**，
   不会出现「前 80 例填好了、第 81 例填到一半」的半截状态。
3. **三态都在** —— 加载态（进度 + 当前例号 + 心跳）/ 空态（本表未圈定病例、
   本次未发现漏判）/ 错误态（错误码 + 处置提示 + 失败例底色）。

另外还断言了**回填只写新增六列**：除新增列、汇总区、漏诊区、状态栏之外，
表里任何一个格子都不许被动过。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from nodule_eval.errors import ErrorCode, NoduleEvalError  # noqa: E402
from nodule_eval.sheets import fill as fill_module  # noqa: E402
from nodule_eval.sheets import status as status_module  # noqa: E402
from nodule_eval.sheets import tokens  # noqa: E402
from nodule_eval.sheets.columns import EXISTING_COLUMNS, NEW_COLUMNS  # noqa: E402
from nodule_eval.sheets.fill import (  # noqa: E402
    EMPTY_STATE_NO_CASES,
    EMPTY_STATE_NO_MISSES,
    assert_existing_columns_unchanged,
    backfill_workbook,
    read_missed_area_texts,
    read_status_bar,
)
from nodule_eval.sheets.read import read_sheet  # noqa: E402
from nodule_eval.sheets.status import RunStatus  # noqa: E402
from nodule_eval.sheets.template import FIRST_DATA_ROW, write_template  # noqa: E402

#: 科室填的既有列内容 —— **金标准原文用多行 + 全角标点**，
#: 回填最怕的就是把这种内容改掉（换行、全角标点一改就是「篡改原文」）。
DEPARTMENT_ROWS: list[tuple[str, str, str]] = [
    ("DEMO-0001", "右下肺见一磨玻璃结节，最大径约8mm", "dx://demo/study-001"),
    ("DEMO-0002", "双肺未见明显结节影", ""),
    ("DEMO-0003", "左肺上叶见实性结节，\n最大径约12mm", "dx://demo/study-003"),
]

#: 一轮评估的逐例判定行（命中 / 漏判 / 失败各一）。
CASE_ROWS: list[dict[str, object]] = [
    {
        "accession_no": "DEMO-0001",
        "verdict": "hit",
        "model_findings": "右肺下叶 | 8.0mm | 磨玻璃",
        "failure_reason": "",
        "tool_call_id": "tc_" + "a" * 32,
        "needs_review": False,
        "normalize_source": "model",
        "row_no": FIRST_DATA_ROW,
    },
    {
        "accession_no": "DEMO-0002",
        "verdict": "false_positive",
        "model_findings": "右肺下叶 | 6.0mm | 实性",
        "failure_reason": "",
        "tool_call_id": "tc_" + "b" * 32,
        "needs_review": False,
        "normalize_source": "model",
        "row_no": FIRST_DATA_ROW + 1,
    },
    {
        "accession_no": "DEMO-0003",
        "verdict": "failed",
        "model_findings": "",
        "failure_reason": "推理服务暂时不可用（HTTP 500）：服务端错误",
        "tool_call_id": "tc_" + "c" * 32,
        "needs_review": False,
        "normalize_source": "rule",
        "row_no": FIRST_DATA_ROW + 2,
    },
]

#: 汇总区数据（写到表头汇总区的固定地址）。
SUMMARY_MAP = {
    "total_cases": 3,
    "degrade_note": "含 1 例规则降级",
}

METRICS_MAP = {
    "tp": 1,
    "fp": 1,
    "fn": 0,
    "tn": 0,
    "sensitivity": 1.0,
    "specificity": 0.0,
    "ppv": 0.5,
    "npv": None,
    "failed_count": 1,
    "rule_degraded_count": 1,
    "needs_review_count": 0,
    "incomparable_count": 0,
    "model_normalized_count": 2,
}

MISSED_LIST: list[dict[str, object]] = [
    {
        "accession_no": "DEMO-0003",
        "gold_standard_summary": "左肺上叶 12.0mm 实性",
        "model_findings": "",
        "missed_reason": "金标准写了 1 处病灶，模型一处都没盖住",
        "tool_call_id": "tc_" + "c" * 32,
        "rule_degraded": True,
    }
]


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _make_sheet(tmp_path: Path, *, rows: int = 5, name: str = "评估表.xlsx") -> Path:
    """造一张填好既有列的评估表（科室填的那一半）。"""

    path = write_template(tmp_path / name, prefilled_rows=rows)
    workbook = load_workbook(path)
    worksheet = workbook["评估表"]
    for offset, (accession_no, gold_text, image_ref) in enumerate(DEPARTMENT_ROWS):
        row = FIRST_DATA_ROW + offset
        worksheet.cell(row=row, column=1, value=accession_no)
        worksheet.cell(row=row, column=2, value=gold_text)
        worksheet.cell(row=row, column=3, value=image_ref)
    workbook.save(path)
    workbook.close()
    return path


def _grid(path: Path) -> dict[tuple[int, int], object]:
    """整张表的逐格快照（``(行, 列) → 值``）。"""

    workbook = load_workbook(path)
    try:
        worksheet = workbook["评估表"]
        grid: dict[tuple[int, int], object] = {}
        for row in range(1, worksheet.max_row + 1):
            for column in range(1, 12):
                value = worksheet.cell(row=row, column=column).value
                if value is not None:
                    grid[(row, column)] = value
        return grid
    finally:
        workbook.close()


def _section_row(path: Path, marker: str) -> int:
    """找区块标记所在行（漏诊区 / 状态栏）。"""

    workbook = load_workbook(path)
    try:
        worksheet = workbook["评估表"]
        for row in range(1, worksheet.max_row + 1):
            value = worksheet.cell(row=row, column=1).value
            if value and str(value).strip().startswith(marker):
                return row
        return worksheet.max_row + 1
    finally:
        workbook.close()


def _existing_snapshot(path: Path) -> dict[tuple[int, int], object]:
    """既有三列的逐格快照（**只到明细区末行** —— 漏诊区与状态栏不算既有列）。"""

    workbook = load_workbook(path)
    try:
        worksheet = workbook["评估表"]
        limit = _section_row(path, "【漏诊明细】") - 1
        snapshot: dict[tuple[int, int], object] = {}
        for row in range(FIRST_DATA_ROW, limit + 1):
            for column in range(1, 4):
                snapshot[(row, column)] = worksheet.cell(row=row, column=column).value
        return snapshot
    finally:
        workbook.close()


# --------------------------------------------------------------------------
# 1. 既有列与金标准原文零改动
# --------------------------------------------------------------------------


def test_backfill_leaves_existing_columns_untouched(tmp_path: Path) -> None:
    """既有三列逐格未变 —— 金标准原文（含换行与全角标点）一个字都没动。"""

    path = _make_sheet(tmp_path)
    before = _existing_snapshot(path)

    result = backfill_workbook(
        path,
        CASE_ROWS,
        summary=SUMMARY_MAP,
        metrics=METRICS_MAP,
        missed_list=MISSED_LIST,
    )

    after = _existing_snapshot(path)
    assert result.existing_columns_unchanged is True
    assert after == before, "既有列被改动了"
    # 金标准原文逐字保留：换行与中文标点都在
    assert after[(FIRST_DATA_ROW + 2, 2)] == DEPARTMENT_ROWS[2][1]


def test_backfill_writes_only_the_six_new_columns(tmp_path: Path) -> None:
    """除新增六列（+ 汇总区 / 漏诊区 / 状态栏）外，别的格子一个都不动。"""

    path = _make_sheet(tmp_path)
    before = _grid(path)
    missed_section_row = _section_row(path, "【漏诊明细】")

    backfill_workbook(
        path,
        CASE_ROWS,
        summary=SUMMARY_MAP,
        metrics=METRICS_MAP,
        missed_list=MISSED_LIST,
    )

    after = _grid(path)
    changed = {
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    }
    allowed_columns = {4, 5, 6, 7, 8, 9}  # 新增六列 D..I
    summary_addresses = {
        "A6", "B6", "C6", "D6", "E6", "F6", "G6", "H6", "I6",
        "A8", "B8", "C8", "D8", "E8", "F8",
    }
    for row, column in sorted(changed):
        # 漏诊明细区与底部状态栏：引擎回填的地盘，随它变。
        if row >= missed_section_row:
            continue
        # 明细区的新增六列。
        if column in allowed_columns and row >= FIRST_DATA_ROW:
            continue
        cell = f"{chr(ord('A') + column - 1)}{row}"
        if cell in summary_addresses:
            continue
        raise AssertionError(
            f"不该被改动的格子：{cell}"
            f"（{before.get((row, column))!r} → {after.get((row, column))!r}）"
        )


def test_new_columns_carry_the_judgement(tmp_path: Path) -> None:
    """新增六列按契约列序落值（``needs_review`` 写「是 / 否」）。"""

    path = _make_sheet(tmp_path)
    backfill_workbook(path, CASE_ROWS, summary=SUMMARY_MAP, metrics=METRICS_MAP)

    workbook = load_workbook(path)
    try:
        worksheet = workbook["评估表"]
        row = FIRST_DATA_ROW
        assert worksheet.cell(row=row, column=4).value == "hit"
        assert worksheet.cell(row=row, column=5).value == "右肺下叶 | 8.0mm | 磨玻璃"
        assert worksheet.cell(row=row, column=6).value in ("", None)
        assert worksheet.cell(row=row, column=7).value == "tc_" + "a" * 32
        assert worksheet.cell(row=row, column=8).value == "否"
        assert worksheet.cell(row=row, column=9).value == "model"
        # 失败例：失败原因与错误码都落表
        failed_row = FIRST_DATA_ROW + 2
        assert worksheet.cell(row=failed_row, column=4).value == "failed"
        assert "HTTP 500" in str(worksheet.cell(row=failed_row, column=6).value)
    finally:
        workbook.close()


def test_existing_column_change_is_detected_and_blocks_save(tmp_path: Path) -> None:
    """回填写到既有列 → 报错并**放弃落盘**（原表保持原样）。"""

    path = _make_sheet(tmp_path)
    before_bytes = path.read_bytes()

    original = fill_module._write_case_rows

    def sabotage(worksheet, mapping, case_rows, header_row, warnings, **kwargs):  # noqa: ANN001
        # 模拟「回填写错了列」：往既有列（检查号）里写判定值。
        result = original(worksheet, mapping, case_rows, header_row, warnings, **kwargs)
        worksheet.cell(row=FIRST_DATA_ROW, column=1, value="被改写了的检查号")
        return result

    fill_module._write_case_rows = sabotage
    try:
        with pytest.raises(NoduleEvalError) as excinfo:
            backfill_workbook(path, CASE_ROWS, summary=SUMMARY_MAP, metrics=METRICS_MAP)
    finally:
        fill_module._write_case_rows = original

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "既有列" in excinfo.value.detail
    # 关键：原表一个字节都没变 —— 没落盘
    assert path.read_bytes() == before_bytes


def test_assert_existing_columns_unchanged_names_the_cell() -> None:
    """比对不过时**点名是第几行第几列**。"""

    before = {(12, 1): "DEMO-0001", (12, 2): "原文"}
    after = {(12, 1): "DEMO-9999", (12, 2): "原文"}
    with pytest.raises(NoduleEvalError) as excinfo:
        assert_existing_columns_unchanged(before, after)
    assert "第 12 行第 1 列" in excinfo.value.detail


def test_sheet_can_still_be_read_after_backfill(tmp_path: Path) -> None:
    """回填后**读层照旧只读到明细区的病例**（状态栏标记没被抹掉）。"""

    path = _make_sheet(tmp_path)
    backfill_workbook(path, CASE_ROWS, summary=SUMMARY_MAP, metrics=METRICS_MAP)

    result = read_sheet(path)
    assert [case.accession_no for case in result.cases] == [
        row[0] for row in DEPARTMENT_ROWS
    ]
    # 金标准原文读回来一字不差
    assert result.cases[2].gold_standard_text == DEPARTMENT_ROWS[2][1]


# --------------------------------------------------------------------------
# 2. 中断不留半截表
# --------------------------------------------------------------------------


def test_interrupted_backfill_leaves_the_table_untouched(tmp_path: Path) -> None:
    """回填中途失败 → 原表**一个字节都没变**（不留半截表）。"""

    path = _make_sheet(tmp_path)
    before_bytes = path.read_bytes()

    original = fill_module._write_case_rows

    def boom(worksheet, mapping, case_rows, header_row, warnings, **kwargs):  # noqa: ANN001
        original(worksheet, mapping, case_rows[:1], header_row, warnings, **kwargs)
        raise RuntimeError("模拟回填中途断电")

    fill_module._write_case_rows = boom
    try:
        with pytest.raises(RuntimeError):
            backfill_workbook(path, CASE_ROWS, summary=SUMMARY_MAP, metrics=METRICS_MAP)
    finally:
        fill_module._write_case_rows = original

    assert path.read_bytes() == before_bytes, "中断后表被改了 —— 半截回填"
    # 临时文件也不能留下
    assert not list(path.parent.glob("*.filling.tmp"))


def test_save_is_atomic_replace(tmp_path: Path) -> None:
    """落盘走**原子替换**：临时文件写全了才换上去。"""

    path = _make_sheet(tmp_path)
    backfill_workbook(path, CASE_ROWS, summary=SUMMARY_MAP, metrics=METRICS_MAP)
    assert path.is_file()
    assert not list(path.parent.glob("*.filling.tmp"))


# --------------------------------------------------------------------------
# 3. 三态
# --------------------------------------------------------------------------


def test_empty_state_when_no_case_is_selected(tmp_path: Path) -> None:
    """空态 · 本表未圈定病例：状态栏给一句人话，不是空白。"""

    path = _make_sheet(tmp_path)
    result = backfill_workbook(path, [], summary=None, metrics=None)

    assert result.empty_state == EMPTY_STATE_NO_CASES
    assert result.written_rows == 0
    status_text = read_status_bar(path)
    assert "本表未圈定病例" in status_text
    # 状态栏的区块标记必须还在（抹掉的话读层会把状态栏当数据行）
    assert status_text.startswith("【运行状态】")


def test_empty_state_when_no_case_is_missed(tmp_path: Path) -> None:
    """空态 · 本次未发现漏判：漏诊区写一句话，不是一片空白。"""

    path = _make_sheet(tmp_path)
    result = backfill_workbook(
        path,
        CASE_ROWS,
        summary=SUMMARY_MAP,
        metrics=METRICS_MAP,
        missed_list=[],
    )

    assert result.empty_state == EMPTY_STATE_NO_MISSES
    texts = read_missed_area_texts(path)
    assert any("本次未发现漏判" in text for text in texts)


def test_loading_state_shows_progress_and_current_case(tmp_path: Path) -> None:
    """加载态：状态栏显示已处理 / 总数 / 当前例号 / 心跳。"""

    status = RunStatus(
        run_id="sheet-20261108-101500",
        state=status_module.STATE_RUNNING,
        processed=37,
        total=200,
        current_accession_no="DEMO-0038",
        heartbeat_at="2026-11-08 10:16:20",
    )
    text = status_module.status_bar_text(status)
    assert "评估中" in text
    assert "37/200" in text
    assert "DEMO-0038" in text
    assert "2026-11-08 10:16:20" in text


def test_error_state_shows_error_code_and_dispose_hint(tmp_path: Path) -> None:
    """错误态：状态栏给**错误码 + 处置提示**，不只写「失败了」。"""

    status = RunStatus(
        run_id="sheet-20261108-101500",
        state=status_module.STATE_FAILED,
        processed=12,
        total=200,
        error_code="INFERENCE_5XX",
        error_detail="推理服务暂时不可用（HTTP 500）",
    )
    text = status_module.status_bar_text(status)
    assert "INFERENCE_5XX" in text
    assert "推理服务返回 5xx" in text  # 处置提示
    assert "HTTP 500" in text


def test_completed_with_failures_still_shows_the_error_code(tmp_path: Path) -> None:
    """这一轮有失败例时，状态栏给**错误码 + 处置提示**（不只写「失败 N 例」）。"""

    status = RunStatus(
        run_id="sheet-20261108-101500",
        state=status_module.STATE_COMPLETED,
        processed=200,
        total=200,
        failed_cases=2,
        error_code="INFERENCE_5XX",
    )
    text = status_module.status_bar_text(status)
    assert "失败 2 例" in text
    assert "INFERENCE_5XX" in text
    assert "推理服务返回 5xx" in text  # 处置提示


def test_failed_row_is_marked_with_the_failure_fill(tmp_path: Path) -> None:
    """错误态：失败例在明细区按失败底色标出（打开表一眼看见）。"""

    path = _make_sheet(tmp_path)
    backfill_workbook(path, CASE_ROWS, summary=SUMMARY_MAP, metrics=METRICS_MAP)

    workbook = load_workbook(path)
    try:
        worksheet = workbook["评估表"]
        failed_cell = worksheet.cell(row=FIRST_DATA_ROW + 2, column=6)
        assert failed_cell.fill is not None
        rgb = failed_cell.fill.fgColor.rgb
        assert rgb and rgb.endswith(tokens.COLOR_FAILED_FILL)
        # 正常例不带失败底色
        hit_cell = worksheet.cell(row=FIRST_DATA_ROW, column=6)
        assert hit_cell.fill is None or hit_cell.fill.fgColor.rgb in (None, "00000000")
    finally:
        workbook.close()


def test_status_bar_keeps_the_section_marker(tmp_path: Path) -> None:
    """状态栏的区块标记必须留着 —— 读层见到标记即停。"""

    path = _make_sheet(tmp_path)
    backfill_workbook(path, CASE_ROWS, summary=SUMMARY_MAP, metrics=METRICS_MAP)
    status_text = read_status_bar(path)
    assert status_text.startswith("【运行状态】")


def test_summary_area_is_filled_at_fixed_addresses(tmp_path: Path) -> None:
    """汇总区按**固定地址**落值（取不到值的写占位符，不留空）。"""

    path = _make_sheet(tmp_path)
    backfill_workbook(path, CASE_ROWS, summary=SUMMARY_MAP, metrics=METRICS_MAP)

    workbook = load_workbook(path)
    try:
        worksheet = workbook["评估表"]
        assert worksheet["A6"].value == 3       # 样本量
        assert worksheet["B6"].value == 1       # TP
        assert worksheet["C6"].value == 1       # FP
        assert worksheet["D6"].value == 0       # FN
        assert worksheet["F6"].value == 1.0     # 敏感度
        assert worksheet["I6"].value == "—"     # NPV 分母为 0 → 占位符
        assert worksheet["A8"].value == 1       # 失败数
        assert worksheet["F8"].value == "含 1 例规则降级"
    finally:
        workbook.close()


def test_missed_area_rows_match_the_missed_count(tmp_path: Path) -> None:
    """漏诊区行数与漏诊清单条数一致（复核的人数一遍就能手算敏感度）。"""

    path = _make_sheet(tmp_path)
    result = backfill_workbook(
        path,
        CASE_ROWS,
        summary=SUMMARY_MAP,
        metrics=METRICS_MAP,
        missed_list=MISSED_LIST,
    )
    assert result.missed_rows == len(MISSED_LIST) == 1

    workbook = load_workbook(path)
    try:
        worksheet = workbook["评估表"]
        found = [
            row
            for row in range(1, worksheet.max_row + 1)
            if str(worksheet.cell(row=row, column=1).value or "").strip() == "DEMO-0003"
        ]
        assert found, "漏诊区里没有那一行"
    finally:
        workbook.close()


# --------------------------------------------------------------------------
# 4. 状态文件（加载态的来源）
# --------------------------------------------------------------------------


def test_status_file_is_written_atomically_and_readable(tmp_path: Path) -> None:
    """状态文件写得出来、读得回来；写的是临时文件 + 原子替换。"""

    run_dir = tmp_path / "runs" / "sheet-20261108-101500"
    path = status_module.write_status(
        run_dir,
        RunStatus(
            run_id="sheet-20261108-101500",
            state=status_module.STATE_RUNNING,
            processed=5,
            total=200,
            current_accession_no="DEMO-0006",
        ),
    )
    assert path.is_file()
    assert not list(run_dir.glob("*.tmp"))

    loaded = status_module.read_status(path)
    assert loaded is not None
    assert loaded.processed == 5
    assert loaded.total == 200
    assert loaded.current_accession_no == "DEMO-0006"
    assert loaded.state == status_module.STATE_RUNNING


def test_read_status_returns_none_when_file_missing(tmp_path: Path) -> None:
    """读不到状态文件时返回 ``None``（不是抛错）—— 宏轮询到空文件是正常的。"""

    assert status_module.read_status(tmp_path / "engine_status.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{不是 JSON", encoding="utf-8")
    assert status_module.read_status(broken) is None


def test_run_progress_writes_progress_to_the_status_file(tmp_path: Path) -> None:
    """运行进度（已处理 / 总数 / 当前例号）随例推进落进状态文件。"""

    from nodule_eval.engine import RunProgress

    run_dir = tmp_path / "runs" / "demo"
    progress = RunProgress(run_dir, run_id="demo", total=3)
    progress.start()
    progress.advance("DEMO-0001")
    progress.advance("DEMO-0002", failed=True)

    loaded = status_module.read_status(status_module.status_path(run_dir))
    assert loaded is not None
    assert loaded.processed == 2
    assert loaded.total == 3
    assert loaded.current_accession_no == "DEMO-0002"
    assert loaded.failed_cases == 1
    assert loaded.heartbeat_at


def test_status_text_contract_keeps_the_marker_prefix() -> None:
    """状态栏文本一律带 ``【运行状态】`` 标记（读层靠它停）。"""

    for status in (
        RunStatus(state=status_module.STATE_RUNNING, processed=1, total=2),
        RunStatus(state=status_module.STATE_COMPLETED, total=2),
        RunStatus(state=status_module.STATE_FAILED, error_code="INFERENCE_5XX"),
        RunStatus(state=status_module.STATE_INTERRUPTED, error_detail="断了"),
        RunStatus(state=status_module.STATE_IDLE, total=0),
    ):
        assert status_module.status_bar_text(status).startswith("【运行状态】")


def test_dispose_hint_covers_the_common_error_codes() -> None:
    """处置提示覆盖常用错误码；没登记的码走默认提示（不空着）。"""

    for code in ("INFERENCE_5XX", "INFERENCE_TIMEOUT", "CREDENTIAL_MISSING",
                 "RESEARCH_WRITE_FAILED", "MODEL_UNAVAILABLE"):
        assert status_module.dispose_hint(code)
    assert status_module.dispose_hint("SOME_UNKNOWN_CODE") == status_module.DEFAULT_DISPOSE_HINT


# --------------------------------------------------------------------------
# 5. 端到端：跑一轮 → fill 子命令回填 → 表里既有列没动、新增列有值
# --------------------------------------------------------------------------


def test_cli_fill_end_to_end(tmp_path: Path, full_env, monkeypatch, capsys) -> None:
    """真跑一轮评估，再用 ``fill`` 子命令回填进评估表。

    这一条走的是**产品主路径**：引擎真调推理端点 → 产物落库 →
    回填把判定写进新增六列，既有列一个字都没动。
    """

    from nodule_eval import cli
    from tests.stub_inference import InferenceStub

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        cases_file = PROJECT_ROOT / "tests" / "fixtures" / "cases" / "single_case.json"
        runs_dir = tmp_path / "runs"
        store_path = tmp_path / "store" / "nodule_eval.sqlite3"

        exit_code = cli.main(
            [
                "run",
                "--cases",
                str(cases_file),
                "--model-id",
                "demo-model",
                "--run-id",
                "fill-e2e-001",
                "--dataset-ref",
                "本地样例",
                "--runs-dir",
                str(runs_dir),
                "--store",
                str(store_path),
            ]
        )
        assert exit_code == 0

        # 评估表：科室那一半填好，既有列一字不动
        sheet = write_template(tmp_path / "评估表.xlsx", prefilled_rows=3)
        workbook = load_workbook(sheet)
        worksheet = workbook["评估表"]
        worksheet.cell(row=FIRST_DATA_ROW, column=1, value="DEMO-0001")
        worksheet.cell(
            row=FIRST_DATA_ROW,
            column=2,
            value="右下肺见一磨玻璃结节，大小约 8mm。",
        )
        worksheet.cell(row=FIRST_DATA_ROW, column=3, value="dx://demo/study-001")
        workbook.save(sheet)
        workbook.close()

        before = _existing_snapshot(sheet)

        exit_code = cli.main(
            [
                "fill",
                "--sheet",
                str(sheet),
                "--run-id",
                "fill-e2e-001",
                "--runs-dir",
                str(runs_dir),
                "--store",
                str(store_path),
            ]
        )
        assert exit_code == 0

        # 既有列（检查号 / 金标准原文 / 影像引用）逐格未变
        assert _existing_snapshot(sheet) == before

        # 新增六列有判定
        workbook = load_workbook(sheet)
        try:
            worksheet = workbook["评估表"]
            assert worksheet.cell(row=FIRST_DATA_ROW, column=4).value in (
                "hit",
                "miss",
                "false_positive",
                "true_negative",
                "failed",
            )
            assert worksheet.cell(row=FIRST_DATA_ROW, column=7).value  # 调用记录ID
        finally:
            workbook.close()

        # 汇总区有样本量
        workbook = load_workbook(sheet)
        try:
            worksheet = workbook["评估表"]
            assert worksheet["A6"].value == 1
        finally:
            workbook.close()

        printed = capsys.readouterr().out
        assert "回填行数" in printed
        assert "既有列核对" in printed
    finally:
        stub.stop()


def test_engine_writes_the_status_file_during_a_run(tmp_path: Path, full_env, monkeypatch) -> None:
    """引擎跑的过程中写状态文件（加载态的来源），跑完落终态。"""

    from nodule_eval import cli
    from tests.stub_inference import InferenceStub

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        cases_file = PROJECT_ROOT / "tests" / "fixtures" / "cases" / "single_case.json"
        runs_dir = tmp_path / "runs"
        exit_code = cli.main(
            [
                "run",
                "--cases",
                str(cases_file),
                "--model-id",
                "demo-model",
                "--run-id",
                "status-e2e-001",
                "--runs-dir",
                str(runs_dir),
            ]
        )
        assert exit_code == 0

        status_file = runs_dir / "status-e2e-001" / "engine_status.json"
        assert status_file.is_file(), "引擎没有写出状态文件"
        loaded = status_module.read_status(status_file)
        assert loaded is not None
        assert loaded.state == status_module.STATE_COMPLETED
        assert loaded.processed == 1
        assert loaded.total == 1
        assert loaded.status_text.startswith("【运行状态】")
    finally:
        stub.stop()
