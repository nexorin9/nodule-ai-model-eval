"""表格内宏 ↔ 引擎之间的**落盘 JSON 交接协议** —— 唯一真源。

三入口（xlsm 宏 / WPS JSA 宏 / CSV 直跑）写出的请求 JSON **字段完全一致**，
引擎读的是同一份契约（:mod:`nodule_eval.contracts`）。本模块把**宏这一侧**
用到的文件名、命令行、字段名、回填列集中在这里：

- ``xlsm_vba/nodule_eval.bas`` 与 ``wps_jsa/nodule_eval.js`` 两份宏源码
  **逐字引用**这里的常量（文件名字面量、字段名字面量、汇总区地址）；
- ``tests/test_sheet_contract.py`` 把两份宏源码与本模块**逐项对照** ——
  宏里少一个字段名、把「失败原因」写进「模型返回」、路径写错一个文件名，
  测试就红。

**为什么要有这一层**：宏是**手写**的，改一次字段名常常只改了一边 ——
Excel 里能跑、WPS 里字段空着，或者两份宏一个写对列一个写错列。
两份宏各写一份字面量，就是同一个静默事故的两个入口。

**交接协议一览**::

    <工作区>/req.json                请求 JSON（字段同 contracts.py）
    runs/<run_id>/result.json        引擎写出的结果 JSON
    runs/<run_id>/case_rows.csv      逐例判定明细（回填数据源）
    runs/<run_id>/tool_calls.jsonl   调用底账（可反查入参与返回）
    <工作区>/engine.log              引擎的标准输出与错误输出
    <工作区>/last_run.txt            最近一轮的 run_id（供「确认提交」用）

**凭据不在这里**：宏不碰任何凭据。RIS / 推理 / 科研平台 / 模型四个出站面的
凭据一律从环境变量读（见 :mod:`nodule_eval.config`），宏只负责**把表里的行
变成请求、把引擎的结果变成表里的判定**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import CASE_ROW_COLUMNS, REQUIRED_REQUEST_FIELDS, parse_request
from .columns import NEW_COLUMNS
from .template import SUMMARY_CELLS

# --------------------------------------------------------------------------
# 环境变量（宏与引擎之间只靠这几个变量说话）
# --------------------------------------------------------------------------

#: 工作区目录 —— 请求 JSON、引擎日志、最近一轮 run_id 都落在这里。
ENV_WORKSPACE = "NODULE_EVAL_WORKSPACE"

#: 项目根目录（含 ``src/`` 与 ``models/``）—— 引擎从这里启动。
ENV_HOME = "NODULE_EVAL_HOME"

#: Python 解释器（缺省 ``python``）。
ENV_PYTHON = "NODULE_EVAL_PYTHON"

#: 产物根目录（缺省 ``<工作区>/runs``）。
ENV_RUNS_DIR = "NODULE_EVAL_RUNS_DIR"

#: 被评模型标识（对应 ``models/<model_id>.yaml``）—— **必填**，缺了点按钮会指名报错。
ENV_MODEL_ID = "NODULE_EVAL_MODEL_ID"

#: 被评模型版本（缺省取模型目录里的声明）。
ENV_MODEL_VERSION = "NODULE_EVAL_MODEL_VERSION"

#: 数据集引用（写进 summary，便于事后核对这批结论是哪批数据跑的）。
ENV_DATASET_REF = "NODULE_EVAL_DATASET_REF"

#: 直接指定要提交/导出的那一轮（缺省取工作区里的 ``last_run.txt``）。
ENV_RUN_ID = "NODULE_EVAL_RUN_ID"

#: 尺寸容差（毫米，缺省 2.0）。
ENV_SIZE_TOLERANCE = "NODULE_EVAL_SIZE_TOLERANCE_MM"

#: 并发数（缺省 4）。
ENV_CONCURRENCY = "NODULE_EVAL_CONCURRENCY"

# --------------------------------------------------------------------------
# 文件名（宏与引擎对这几个名字的理解必须一字不差）
# --------------------------------------------------------------------------

REQUEST_FILENAME = "req.json"
RESULT_FILENAME = "result.json"
CASE_ROWS_FILENAME = "case_rows.csv"
SUMMARY_FILENAME = "summary.json"
TOOL_CALLS_FILENAME = "tool_calls.jsonl"
MISSED_LIST_FILENAME = "missed_list.csv"
ENGINE_LOG_FILENAME = "engine.log"
LAST_RUN_FILENAME = "last_run.txt"

#: 本机配置文件（键值对，一行一个）—— 宏这一侧的 python 路径、项目根、
#: 被评模型等放在这里。**不含任何凭据**：RIS / 推理 / 科研平台 / 模型四个
#: 出站面的凭据一律走环境变量（见 :mod:`nodule_eval.config`）。
CONFIG_FILENAME = "nodule_eval.conf"

#: **运行状态文件**（引擎写、宏轮询）—— 加载态的来源：
#: 宏每半秒读一次，把「已处理 N/总数 · 当前例号 · 心跳」刷到状态栏。
#: 落 ``runs/<run_id>/engine_status.json``。文件名与
#: :data:`nodule_eval.sheets.status.STATUS_FILENAME` 逐字一致（两份宏也照这个名）。
STATUS_FILENAME = "engine_status.json"

#: 状态文件里**渲染好的状态栏文本**这个键 —— 宏直接把这一行写进状态栏，
#: 不在宏里再拼一份中文说法（两边各写一份早晚漂）。
STATUS_KEY_TEXT = "status_text"

#: 引擎的命令行模块（``python -m nodule_eval.cli <子命令>``）。
CLI_MODULE = "nodule_eval.cli"

#: 三个按钮分别调的子命令。
SUBCOMMAND_RUN = "run"
SUBCOMMAND_SUBMIT = "submit"
SUBCOMMAND_EXPORT = "export"

# --------------------------------------------------------------------------
# 请求契约（宏**写**出去的字段）
# --------------------------------------------------------------------------

#: 请求体顶层字段 —— 必填四项 + 病例数组 + 选项。
REQUEST_TOP_FIELDS: tuple[str, ...] = tuple(REQUIRED_REQUEST_FIELDS) + ("cases", "options")

#: 单条病例的字段。
REQUEST_CASE_FIELDS: tuple[str, ...] = (
    "accession_no",
    "gold_standard_text",
    "image_ref",
    "row_no",
)

#: ``options`` 的字段。
REQUEST_OPTION_FIELDS: tuple[str, ...] = (
    "size_tolerance_mm",
    "concurrency",
    "resume",
)

# --------------------------------------------------------------------------
# 响应契约（宏**读**回来的字段）
# --------------------------------------------------------------------------

#: ``result.json`` 的顶层字段。
RESPONSE_TOP_FIELDS: tuple[str, ...] = (
    "run_id",
    "status",
    "case_rows",
    "metrics",
    "missed_list",
    "summary",
)

#: ``case_rows`` 里每一行的字段（与 ``case_rows.csv`` 的列序逐字相同）。
RESPONSE_CASE_ROW_FIELDS: tuple[str, ...] = tuple(CASE_ROW_COLUMNS)

#: 宏回填时**只写**的列 —— 产品新增六列，既有三列一个字都不动。
BACKFILL_COLUMNS: tuple[str, ...] = tuple(NEW_COLUMNS)

#: 汇总区：键 → (来源段, 来源字段)。``summary`` 段取自 ``result.json`` 的
#: ``summary``，``metrics`` 段取自 ``metrics``。
SUMMARY_CELL_SOURCES: Mapping[str, tuple[str, str]] = {
    "sample_size": ("summary", "total_cases"),
    "tp": ("metrics", "tp"),
    "fp": ("metrics", "fp"),
    "fn": ("metrics", "fn"),
    "tn": ("metrics", "tn"),
    "sensitivity": ("metrics", "sensitivity"),
    "specificity": ("metrics", "specificity"),
    "ppv": ("metrics", "ppv"),
    "npv": ("metrics", "npv"),
    "failed_count": ("metrics", "failed_count"),
    "rule_degraded_count": ("metrics", "rule_degraded_count"),
    "needs_review_count": ("metrics", "needs_review_count"),
    "incomparable_count": ("metrics", "incomparable_count"),
    "model_normalized_count": ("metrics", "model_normalized_count"),
    "degrade_note": ("summary", "degrade_note"),
}

#: 汇总区各键的**固定单元格地址**（与模板同一份，两端不各写一份）。
SUMMARY_CELL_ADDRESSES: Mapping[str, str] = dict(SUMMARY_CELLS)

#: 汇总区指标格为空时的占位符 —— 留空会让人以为漏算了。
SUMMARY_PLACEHOLDER = "—"

#: ``needs_review`` 列在表里的写法（布尔 → 是 / 否）。
NEEDS_REVIEW_YES = "是"
NEEDS_REVIEW_NO = "否"

#: 明细区读到哪一行为止：区块标记的**首字符**（U+3010 「【」）。
#:
#: 用码位判断而不是字面量 —— 宏文件若被以错误编码导入，字面量会变成乱码，
#: 而「这一行是区块标记」这件事不能因此失效（否则漏诊区会被当成新病例读进来）。
SECTION_MARKER_FIRST_CHAR = 0x3010


# --------------------------------------------------------------------------
# 宏源码位置（测试与安装脚本都要用）
# --------------------------------------------------------------------------


def macro_source_paths() -> dict[str, Path]:
    """两份宏源码的路径。

    - ``xlsm_vba``            —— Excel VBA 主模块（三按钮）
    - ``xlsm_vba_support``    —— Excel VBA 支撑模块（UTF-8 / JSON / CSV / 起进程）
    - ``wps_jsa``             —— WPS JSA 版（三按钮，同契约）
    """

    base = Path(__file__).resolve().parent
    return {
        "xlsm_vba": base / "xlsm_vba" / "nodule_eval.bas",
        "xlsm_vba_support": base / "xlsm_vba" / "nodule_eval_support.bas",
        "wps_jsa": base / "wps_jsa" / "nodule_eval.js",
    }


def read_macro_source(name: str) -> str:
    """读一份宏源码（按 UTF-8 读，``gb18030`` 兜底）。"""

    path = macro_source_paths()[name]
    raw = path.read_bytes()
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# 请求体组装（宏写出的那份 JSON 的**可执行副本**）
# --------------------------------------------------------------------------


def build_request_payload(
    cases: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    model_id: str,
    model_version: str = "",
    dataset_ref: str = "",
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按**宏写出的那份 JSON** 的形状组装请求体。

    宏（VBA / JSA）逐字照这个形状写；本函数是它的**可执行副本** ——
    测试用它构造的 JSON 必须能被 :func:`nodule_eval.contracts.parse_request`
    接受，否则宏写出的请求引擎读不了（宏改字段名 → 引擎报 CONTRACT_INVALID）。
    """

    payload: dict[str, Any] = {
        "run_id": run_id,
        "model_id": model_id,
        "model_version": model_version,
        "dataset_ref": dataset_ref,
        "cases": [{key: case.get(key) for key in REQUEST_CASE_FIELDS} for case in cases],
        "options": dict(options) if options is not None else build_options(),
    }
    return payload


def build_options(
    *,
    size_tolerance_mm: float = 2.0,
    concurrency: int = 4,
    resume: bool = False,
) -> dict[str, Any]:
    """``options`` 的默认形状（宏不传时引擎用同一组缺省值）。"""

    return {
        "size_tolerance_mm": float(size_tolerance_mm),
        "concurrency": int(concurrency),
        "resume": bool(resume),
    }


# --------------------------------------------------------------------------
# 三个按钮的命令行
# --------------------------------------------------------------------------


def run_args(
    request_path: str | Path,
    runs_dir: str | Path,
) -> list[str]:
    """「开始评估」按钮的**子命令参数**（不含 ``-m <模块>``）。"""

    return [
        SUBCOMMAND_RUN,
        "--request",
        str(request_path),
        "--runs-dir",
        str(runs_dir),
    ]


def submit_args(
    run_id: str,
    runs_dir: str | Path,
    *,
    submitted_by: str = "",
    review: bool = True,
) -> list[str]:
    """「确认提交」按钮的**子命令参数**。

    ``review=True`` 表示**按钮本身即人工确认动作** —— 点下去之前人已经在表里
    逐行看过判定与漏判区了，引擎据此把这一轮推进到「已人工复核」再写回。
    不带这个动作就想写回，闸门照常拦（一次出站调用都不发）。
    """

    args = [
        SUBCOMMAND_SUBMIT,
        "--run-id",
        str(run_id),
        "--runs-dir",
        str(runs_dir),
    ]
    if submitted_by:
        args += ["--submitted-by", str(submitted_by)]
    if review:
        args.append("--review")
    return args


def export_args(
    run_id: str,
    runs_dir: str | Path,
    *,
    out_dir: str | Path = "",
) -> list[str]:
    """「导出结论表」按钮的**子命令参数**。"""

    args = [
        SUBCOMMAND_EXPORT,
        "--run-id",
        str(run_id),
        "--runs-dir",
        str(runs_dir),
    ]
    if out_dir:
        args += ["--out-dir", str(out_dir)]
    return args


def run_command(
    request_path: str | Path,
    runs_dir: str | Path,
    *,
    python_executable: str = "python",
) -> list[str]:
    """「开始评估」按钮的**完整命令行**（含 ``-m <模块>``）。"""

    return ["-m", CLI_MODULE] + run_args(request_path, runs_dir)


def submit_command(
    run_id: str,
    runs_dir: str | Path,
    *,
    submitted_by: str = "",
    review: bool = True,
) -> list[str]:
    """「确认提交」按钮的**完整命令行**。"""

    return ["-m", CLI_MODULE] + submit_args(
        run_id, runs_dir, submitted_by=submitted_by, review=review
    )


def export_command(
    run_id: str,
    runs_dir: str | Path,
    *,
    out_dir: str | Path = "",
) -> list[str]:
    """「导出结论表」按钮的**完整命令行**。"""

    return ["-m", CLI_MODULE] + export_args(run_id, runs_dir, out_dir=out_dir)


# --------------------------------------------------------------------------
# 回填与汇总区取值
# --------------------------------------------------------------------------


def backfill_values(case_row: Mapping[str, Any]) -> dict[str, str]:
    """一条逐例判定行 → **新增六列**的写法。

    ``needs_review`` 在表里写「是 / 否」（字段说明页上的口径），
    其余四列原样落值；空值落空串（不留 ``None`` 字面量）。
    """

    needs_review = bool(case_row.get("needs_review"))
    return {
        "verdict": str(case_row.get("verdict") or ""),
        "model_findings": str(case_row.get("model_findings") or ""),
        "failure_reason": str(case_row.get("failure_reason") or ""),
        "tool_call_id": str(case_row.get("tool_call_id") or ""),
        "needs_review": NEEDS_REVIEW_YES if needs_review else NEEDS_REVIEW_NO,
        "normalize_source": str(case_row.get("normalize_source") or ""),
    }


def summary_values(
    summary: Mapping[str, Any],
    metrics: Mapping[str, Any],
) -> dict[str, Any]:
    """汇总区各格该写的值。

    计数写整数、指标写浮点数（表里的数字格式是 ``0.0000``），
    取不到值（``null``）写占位符「—」——**留空会让人以为漏算了**。
    """

    values: dict[str, Any] = {}
    for key, (section, field) in SUMMARY_CELL_SOURCES.items():
        raw = (summary if section == "summary" else metrics).get(field)
        if raw is None:
            values[key] = SUMMARY_PLACEHOLDER
        elif section == "summary" and field == "degrade_note":
            # 降级说明为空时也写占位符 —— 与宏里的写法一致（留空会让人以为漏算了）
            values[key] = str(raw) if str(raw).strip() else SUMMARY_PLACEHOLDER
        elif key in ("sample_size", "tp", "fp", "fn", "tn", "failed_count",
                     "rule_degraded_count", "needs_review_count",
                     "incomparable_count", "model_normalized_count"):
            values[key] = int(raw)
        else:
            values[key] = float(raw)
    return values


def assert_request_payload_valid(payload: Mapping[str, Any]) -> None:
    """断言这份请求体**引擎真的读得懂**（宏写错字段名时立刻能看出来）。"""

    parse_request(payload)
