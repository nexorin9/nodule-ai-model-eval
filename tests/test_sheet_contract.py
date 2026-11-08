"""三入口（xlsm 宏 / WPS JSA 宏 / CSV 直跑）的**契约一致性**检查。

**这个测试防的是什么**：宏是手写的，改一次字段名常常只改了一边 ——

- Excel 版能跑，WPS 版字段空着；
- 汇总区地址两份宏各写一份，一份写 A6、一份写 A7；
- 回填列写错一列，把「失败原因」写进了「模型返回」。

这些错误**在 Excel 里看不出来**（宏一改完就"跑通了"），只有把两份宏源码与
契约（:mod:`nodule_eval.sheets.macro_contract`）逐项对照才能拦住。

**测法**：把两份宏源码当文本读出来，用正则把里面的**字面量**抠出来，
与契约里的真源逐项比对。抠不出来（宏改了写法）也判失败 —— 抠不出来本身就是
「这份宏不再与契约对照」。

跑法::

    pytest tests/test_sheet_contract.py -q
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nodule_eval.cli import _load_cases_file, build_parser
from nodule_eval.contracts import CASE_ROW_COLUMNS, EvalResponse, parse_request
from nodule_eval.errors import NoduleEvalError
from nodule_eval.sheets import macro_contract as mc
from nodule_eval.sheets.columns import ALL_COLUMNS, EXISTING_COLUMNS, NEW_COLUMNS
from nodule_eval.sheets.template import SUMMARY_CELLS

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 两份宏源码（VBA 主模块 + VBA 支撑模块合起来算一份）。
VBA_MAIN = mc.read_macro_source("xlsm_vba")
VBA_SUPPORT = mc.read_macro_source("xlsm_vba_support")
VBA_ALL = VBA_MAIN + "\n" + VBA_SUPPORT
JSA_ALL = mc.read_macro_source("wps_jsa")


# --------------------------------------------------------------------------
# 抠字面量的工具
# --------------------------------------------------------------------------


def _quoted(text: str) -> list[str]:
    """抠出源码里**像标识符**的双引号字符串字面量。

    只取 ``[A-Za-z_][A-Za-z0-9_]*`` 这种形状 —— 契约字段名、列名、文件名都是
    这个形状。而源码里还有 ``"\\""``、``'"'`` 这类**不是标识符**的引号片段，
    逐个配对会把后面的引号配错对（抠出来的列表整个错位，测试会以
    「字段没出现」的形式报出来，看着像宏写错了，其实是抠取方法错了）。
    """

    return re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', text)


def _vba_consts(text: str) -> dict[str, str]:
    """抠出 VBA 的 ``Const NAME As String = "值"``。"""

    return {
        name: value
        for name, value in re.findall(r'Const\s+(\w+)\s+As\s+String\s*=\s*"([^"]*)"', text)
    }


def _vba_backfill_columns(text: str) -> list[str]:
    """抠出 VBA 的回填列清单。"""

    match = re.search(r"BackfillColumns\s*=\s*Array\(([^)]*)\)", text)
    assert match, "VBA 里找不到 BackfillColumns = Array(...) —— 回填列清单被挪走了"
    return re.findall(r'"([^"]+)"', match.group(1))


def _vba_column_index_order(text: str) -> list[str]:
    """抠出 VBA 回填时用的列顺序（``cols.Verdict`` 之类），还原成契约列名。"""

    consts = _vba_consts(text)
    # 常量名里的下划线不影响对应关系（COL_TOOL_CALL ↔ cols.ToolCall）
    by_normalized = {
        name[len("COL_"):].upper().replace("_", ""): value
        for name, value in consts.items()
        if name.startswith("COL_")
    }
    match = re.search(r"ColumnIndexArray\s*=\s*Array\(([^)]*)\)", text)
    assert match, "VBA 里找不到 ColumnIndexArray = Array(...)"
    order: list[str] = []
    for item in match.group(1).split(","):
        item = item.strip()
        assert item.startswith("cols."), f"回填列顺序里出现了未知写法：{item}"
        attr = item.split(".", 1)[1]
        key = attr.upper().replace("_", "")
        assert key in by_normalized, f"VBA 里没有对应的列常量：{attr}"
        order.append(by_normalized[key])
    return order


def _vba_summary_addresses(text: str) -> dict[str, str]:
    """抠出 VBA 汇总区的「键 → 单元格地址」。"""

    consts = _vba_consts(text)
    pairs = re.findall(
        r'Case\s+"(\w+)"\s*:\s*SummaryCellAddress\s*=\s*(\w+)', text
    )
    result: dict[str, str] = {}
    for key, const_name in pairs:
        assert const_name in consts, f"VBA 里没有常量 {const_name}"
        result[key] = consts[const_name]
    return result


def _vba_summary_sources(text: str) -> dict[str, str]:
    """抠出 VBA 汇总区的「键 → 取自哪个字段」。"""

    return dict(
        re.findall(r'Case\s+"(\w+)"\s*:\s*SummarySourceField\s*=\s*"([^"]+)"', text)
    )


def _js_backfill_columns(text: str) -> list[str]:
    match = re.search(r"var\s+BACKFILL_COLUMNS\s*=\s*\[([^\]]*)\]", text)
    assert match, "JSA 里找不到 BACKFILL_COLUMNS 数组 —— 回填列清单被挪走了"
    return re.findall(r'"([^"]+)"', match.group(1))


def _js_block(text: str, name: str) -> str:
    match = re.search(r"var\s+" + name + r"\s*=\s*\{([^}]*)\}", text)
    assert match, f"JSA 里找不到 {name} 字面量表"
    return match.group(1)


def _js_summary_addresses(text: str) -> dict[str, str]:
    return dict(
        re.findall(r'(\w+)\s*:\s*"([A-I]\d+)"', _js_block(text, "SUMMARY_CELL_ADDRESSES"))
    )


def _js_summary_sources(text: str) -> dict[str, str]:
    return dict(
        re.findall(r'(\w+)\s*:\s*"(\w+)"', _js_block(text, "SUMMARY_SOURCE_FIELD"))
    )


# --------------------------------------------------------------------------
# 1. 契约本身：宏写出的请求，引擎真的读得懂
# --------------------------------------------------------------------------


def test_request_payload_is_accepted_by_the_engine():
    """宏写出的请求 JSON 必须能被引擎的解析器接受。"""

    payload = mc.build_request_payload(
        [
            {
                "accession_no": "DEMO-0001",
                "gold_standard_text": "右下肺见一磨玻璃结节，最大径约8mm。",
                "image_ref": "img-1",
                "row_no": 12,
            }
        ],
        run_id="sheet-20260101-120000",
        model_id="demo-model",
        model_version="1.0.0",
        dataset_ref="评估表",
        options=mc.build_options(),
    )
    request = parse_request(payload)
    assert request.run_id == "sheet-20260101-120000"
    assert request.cases[0].accession_no == "DEMO-0001"
    assert request.cases[0].row_no == 12
    assert request.options.size_tolerance_mm == 2.0
    assert request.options.concurrency == 4
    assert request.options.resume is False


def test_request_payload_top_level_fields_match_the_contract():
    payload = mc.build_request_payload(
        [{"accession_no": "A-1", "gold_standard_text": "右肺上叶结节", "image_ref": "", "row_no": 12}],
        run_id="r-1",
        model_id="demo-model",
    )
    assert tuple(payload.keys()) == mc.REQUEST_TOP_FIELDS
    assert tuple(payload["cases"][0].keys()) == mc.REQUEST_CASE_FIELDS
    assert tuple(payload["options"].keys()) == mc.REQUEST_OPTION_FIELDS


def test_required_request_fields_come_from_the_engine_contract():
    """请求的必填四项直接取自引擎契约 —— 两边不各写一份。"""

    assert mc.REQUEST_TOP_FIELDS[:4] == (
        "run_id",
        "model_id",
        "model_version",
        "dataset_ref",
    )


def test_response_fields_match_the_engine_contract():
    payload = EvalResponse(run_id="r-1", status="evaluated").to_dict()
    for field in mc.RESPONSE_TOP_FIELDS:
        assert field in payload, f"响应契约里写了 {field}，引擎的响应里却没有"
    assert mc.RESPONSE_CASE_ROW_FIELDS == tuple(CASE_ROW_COLUMNS)


def test_backfill_columns_are_exactly_the_six_new_columns():
    assert mc.BACKFILL_COLUMNS == tuple(NEW_COLUMNS)
    assert len(mc.BACKFILL_COLUMNS) == 6
    for existing in EXISTING_COLUMNS:
        assert existing not in mc.BACKFILL_COLUMNS


def test_summary_cells_match_the_template():
    assert dict(mc.SUMMARY_CELL_ADDRESSES) == dict(SUMMARY_CELLS)
    assert set(mc.SUMMARY_CELL_SOURCES) == set(SUMMARY_CELLS)


def test_backfill_values_write_is_needs_review():
    values = mc.backfill_values(
        {
            "verdict": "miss",
            "model_findings": "右下肺 | 8.0mm | 磨玻璃 | 0.92",
            "failure_reason": "",
            "tool_call_id": "call-1",
            "needs_review": True,
            "normalize_source": "rule",
        }
    )
    assert tuple(values.keys()) == tuple(NEW_COLUMNS)
    assert values["needs_review"] == "是"
    assert values["verdict"] == "miss"
    assert mc.backfill_values({"needs_review": False})["needs_review"] == "否"


# --------------------------------------------------------------------------
# 2. 两个宏版本：按钮、字段、路径、回填列、汇总区地址
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["StartEvaluation", "ConfirmSubmit", "ExportConclusion"],
)
def test_both_macros_define_the_same_three_buttons(name):
    assert f"Public Sub {name}" in VBA_MAIN, f"VBA 版没有 {name} 这个按钮"
    assert re.search(rf"function\s+{name}\s*\(", JSA_ALL), f"JSA 版没有 {name} 这个按钮"


@pytest.mark.parametrize(
    "field",
    list(mc.REQUEST_TOP_FIELDS)
    + list(mc.REQUEST_CASE_FIELDS)
    + list(mc.REQUEST_OPTION_FIELDS),
)
def test_both_macros_write_the_same_request_fields(field):
    assert field in _quoted(VBA_ALL), f"VBA 版没写出请求字段 {field}"
    assert field in _quoted(JSA_ALL), f"JSA 版没写出请求字段 {field}"


@pytest.mark.parametrize(
    "field",
    sorted({field for _section, field in mc.SUMMARY_CELL_SOURCES.values()}),
)
def test_both_macros_read_the_same_summary_fields(field):
    """汇总区每个指标取自哪个字段 —— 两份宏必须读同一个。"""

    assert field in _quoted(VBA_ALL), f"VBA 版没读汇总字段 {field}"
    assert field in _quoted(JSA_ALL), f"JSA 版没读汇总字段 {field}"


@pytest.mark.parametrize("field", list(NEW_COLUMNS))
def test_both_macros_read_the_same_backfill_fields(field):
    """回填的六个列名 —— 两份宏必须写同一个（写错就是整列错位）。"""

    assert field in _quoted(VBA_ALL), f"VBA 版没回填列 {field}"
    assert field in _quoted(JSA_ALL), f"JSA 版没回填列 {field}"


@pytest.mark.parametrize(
    "filename",
    [
        mc.REQUEST_FILENAME,
        mc.RESULT_FILENAME,
        mc.CASE_ROWS_FILENAME,
        mc.TOOL_CALLS_FILENAME,
        mc.ENGINE_LOG_FILENAME,
        mc.LAST_RUN_FILENAME,
        mc.CONFIG_FILENAME,
    ],
)
def test_both_macros_agree_on_the_handoff_file_names(filename):
    assert filename in VBA_ALL, f"VBA 版没用约定的文件名 {filename}"
    assert filename in JSA_ALL, f"JSA 版没用约定的文件名 {filename}"


def test_both_macros_agree_on_the_completion_marker():
    """引擎跑完的完成标记：两份宏必须用同一个（否则一边等不到）。"""

    markers = {m for m in _quoted(VBA_ALL) if m.startswith("__ENGINE")}
    assert markers, "VBA 版找不到引擎完成标记"
    assert markers == {m for m in _quoted(JSA_ALL) if m.startswith("__ENGINE")}


def test_both_macros_write_only_the_six_new_columns():
    assert _vba_backfill_columns(VBA_ALL) == list(NEW_COLUMNS)
    assert _js_backfill_columns(JSA_ALL) == list(NEW_COLUMNS)
    # 回填时用的列号顺序也要与列名顺序一致（写错顺序 = 整列错位）
    assert _vba_column_index_order(VBA_ALL) == list(NEW_COLUMNS)


def test_both_macros_use_the_same_summary_cell_addresses():
    assert _vba_summary_addresses(VBA_ALL) == dict(SUMMARY_CELLS)
    assert _js_summary_addresses(JSA_ALL) == dict(SUMMARY_CELLS)


def test_both_macros_read_the_same_summary_source_fields():
    expected = {key: field for key, (_section, field) in mc.SUMMARY_CELL_SOURCES.items()}
    assert _vba_summary_sources(VBA_ALL) == expected
    assert _js_summary_sources(JSA_ALL) == expected


def test_macros_reference_the_same_cli_module_and_subcommands():
    for source in (VBA_ALL, JSA_ALL):
        assert mc.CLI_MODULE in source
        for flag in ("--request", "--runs-dir", "--run-id", "--review"):
            assert flag in source, f"宏里少了命令行参数 {flag}"


def test_macros_do_not_embed_credentials_or_point_at_stub_servers():
    """宏里**不许**出现凭据，也不许指向本地替身。"""

    for name, source in (("VBA", VBA_ALL), ("JSA", JSA_ALL)):
        lowered = source.lower()
        for forbidden in ("bearer ", "api_key", "localhost", "127.0.0.1", "example.com"):
            assert forbidden not in lowered, f"{name} 版里出现了不该有的内容：{forbidden}"


def test_macros_do_not_write_the_existing_columns():
    """既有三列是科室维护的 —— 宏里只能读，不能写。"""

    for name, source in (("VBA", VBA_ALL), ("JSA", JSA_ALL)):
        backfill = (
            _vba_backfill_columns(source) if name == "VBA" else _js_backfill_columns(source)
        )
        for existing in EXISTING_COLUMNS:
            assert existing not in backfill, f"{name} 版把既有列 {existing} 当成回填列了"


# --------------------------------------------------------------------------
# 3. 宏发的命令行，CLI 真的认
# --------------------------------------------------------------------------


def test_cli_accepts_the_macro_run_command():
    args = build_parser().parse_args(mc.run_args("C:/ws/req.json", "C:/ws/runs"))
    assert args.command == "run"
    assert args.request == "C:/ws/req.json"
    assert args.runs_dir == "C:/ws/runs"


def test_cli_accepts_the_macro_submit_command():
    args = build_parser().parse_args(
        mc.submit_args("sheet-20260101-120000", "C:/ws/runs", submitted_by="信息科")
    )
    assert args.command == "submit"
    assert args.run_id == "sheet-20260101-120000"
    assert args.submitted_by == "信息科"
    assert args.review is True


def test_cli_accepts_the_macro_export_command():
    args = build_parser().parse_args(mc.export_args("sheet-20260101-120000", "C:/ws/runs"))
    assert args.command == "export"
    assert args.run_id == "sheet-20260101-120000"


def test_full_commands_carry_the_module_prefix():
    """宏起的是 ``python -m nodule_eval.cli <子命令>`` —— 前缀不能少。"""

    for command in (
        mc.run_command("C:/ws/req.json", "C:/ws/runs"),
        mc.submit_command("r-1", "C:/ws/runs"),
        mc.export_command("r-1", "C:/ws/runs"),
    ):
        assert command[:2] == ["-m", mc.CLI_MODULE]


def test_engine_reads_a_request_json_written_with_a_bom():
    """Excel / WPS 写出的 JSON 可能带 BOM —— 引擎要读得对。"""

    payload = mc.build_request_payload(
        [{"accession_no": "A-1", "gold_standard_text": "右肺上叶结节", "image_ref": "", "row_no": 12}],
        run_id="r-bom",
        model_id="demo-model",
    )
    path = PROJECT_ROOT / "tests" / "fixtures" / "cases" / "_bom_request.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    try:
        loaded = _load_cases_file(str(path))
        assert loaded["run_id"] == "r-bom"
    finally:
        path.unlink(missing_ok=True)


def test_engine_rejects_a_request_json_that_is_not_json():
    path = PROJECT_ROOT / "tests" / "fixtures" / "cases" / "_broken_request.json"
    path.write_text("{不是 JSON", encoding="utf-8")
    try:
        with pytest.raises(NoduleEvalError) as excinfo:
            _load_cases_file(str(path))
        assert "不是合法 JSON" in excinfo.value.message
    finally:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# 4. 安装说明
# --------------------------------------------------------------------------


def test_macro_install_doc_exists_and_covers_the_required_topics():
    doc = PROJECT_ROOT / "docs" / "macro_install.md"
    assert doc.is_file(), "缺少宏安装说明 docs/macro_install.md"
    text = doc.read_text(encoding="utf-8")
    for topic in (
        "信任",           # Excel 信任中心
        "开发工具",       # 建按钮
        "JSA",            # WPS 加载方式
        "CSV",            # 无宏退路
        "fill",           # 无宏退路（openpyxl 回填）
        "环境变量",       # 凭据来源
        "convert_bas_to_ansi",  # .bas 转码
    ):
        assert topic in text, f"宏安装说明里没有写到「{topic}」"


def test_convert_script_exists():
    assert (PROJECT_ROOT / "scripts" / "convert_bas_to_ansi.py").is_file()


def test_macro_sources_ship_with_the_package():
    paths = mc.macro_source_paths()
    for name, path in paths.items():
        assert path.is_file(), f"宏源码缺失：{name}（{path}）"


# --------------------------------------------------------------------------
# 5. 加载态：宏轮询引擎的状态文件
# --------------------------------------------------------------------------


def test_status_filename_matches_the_status_module():
    """宏契约里的状态文件名与状态模块**逐字一致**（两边不各写一份）。"""

    from nodule_eval.sheets import status as status_module

    assert mc.STATUS_FILENAME == status_module.STATUS_FILENAME
    assert mc.STATUS_KEY_TEXT == "status_text"


def test_both_macros_agree_on_the_status_file_name():
    """两份宏都用约定的状态文件名（一边写、一边读，名字必须一样）。"""

    assert mc.STATUS_FILENAME in VBA_ALL, "VBA 版没用约定的状态文件名"
    assert mc.STATUS_FILENAME in JSA_ALL, "JSA 版没用约定的状态文件名"


def test_both_macros_poll_the_status_file_and_refresh_the_status_bar():
    """两份宏都要**轮询状态文件刷新状态栏**（加载态），不能只等结果。"""

    assert "RefreshProgress" in VBA_ALL
    assert "refreshProgress" in JSA_ALL
    # 状态栏文本由引擎渲染，宏只搬运那个键
    assert mc.STATUS_KEY_TEXT in VBA_ALL
    assert mc.STATUS_KEY_TEXT in JSA_ALL
    # 轮询循环里要有 DoEvents / pause —— 表格不冻结
    assert "DoEvents" in VBA_ALL
    assert "pause(" in JSA_ALL


def test_macros_do_not_render_the_progress_text_themselves():
    """进度文案**不在宏里拼** —— 宏只搬引擎渲染好的 status_text。

    两边各拼一份中文说法，改了引擎那版就会漂。
    """

    for name, source in (("VBA", VBA_ALL), ("JSA", JSA_ALL)):
        literals = re.findall(r'"([^"\n]*)"', source)
        for literal in literals:
            assert "已处理" not in literal, f"{name} 版自己拼了进度文案"
            assert "当前例号" not in literal, f"{name} 版自己拼了进度文案"
