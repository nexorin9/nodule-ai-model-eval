"""命令行入口。

**产品首要入口是表格内插件**（xlsm 宏 / WPS JSA 宏 / CSV 直跑）；CLI 是**运维与排障通道**：
后台跑批、CI 自检、**没有宏可用时的退路**时用。

**子命令族**（任务 39 补齐）：

============  ==========================================================
子命令        干什么
============  ==========================================================
``run``       跑一轮评估：逐例调推理端点 → 回填判定与指标 → 落产物
``assemble``  装配本院病例集：RIS 取清单 → 按检查号幂等落盘
``submit``    把评估结论写回科研平台评估登记（**须已人工复核**）
``inspect``   按调用记录 ID 反查那次调用的入参与返回
``replay``    回放写回回执：这一轮到底写上去了没有
``doctor``    自检：配置、依赖、模型目录、存储目录
``export``    导出结论表（xlsx）与漏诊清单（csv），**不依赖宏**
``redact-scan`` 产物脱敏扫描
============  ==========================================================

退出码约定：``0`` 成功；``2`` 可预期的产品错误（错误码 + 中文文案打在标准错误上）；
``1`` 未预期异常。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapters.ris import RISAdapter
from .audit.inspect import inspect_tool_call, render_inspect, render_snippet
from .audit.redact import render_scan_report, scan_run_artifacts
from .caseset import (
    AUTHORITY_RIS,
    DEFAULT_CASES_ROOT,
    assemble_caseset,
    bind_cases,
    resolve_case_inputs,
)
from .config import Config, load_config, load_model_spec
from .contracts import (
    CaseInput,
    EvalRequest,
    parse_case,
    parse_endpoint_limits,
    parse_request,
)
from .doctor import render_report, run_doctor
from .engine import DEFAULT_RUNS_DIR, Engine, uses_model_endpoint
from .errors import ErrorCode, NoduleEvalError
from .error_matrix import failure_breakdown, render_failure_lines
from .export import EXPORT_FORMATS, export_run
from .fixture_gate import fixture_enabled, routed_base_urls
from .metrics import format_metric_value
from .metrics import (
    build_missed_list,
    compute_metrics_from_rows,
    count_model_normalized,
    count_raw_counts,
    count_rule_degraded,
)
from .sheets.fill import backfill_workbook
from .sheets.status import read_status, status_path
from .store import ACTOR_HUMAN_REVIEW, ACTOR_HUMAN_SUBMIT, RUN_STATUS_SUBMITTED, RunStore

#: 退出码：可预期的产品错误。
EXIT_PRODUCT_ERROR = 2
#: 退出码：未预期异常。
EXIT_UNEXPECTED = 1

#: ``--store`` 的统一说明（多个子命令共用同一处文案，不各写一份）。
_STORE_HELP = (
    "run store 数据库路径（SQLite 文件或目录；缺省用平台默认目录："
    "Windows %%LOCALAPPDATA%%\\nodule-ai-model-eval\\，"
    "其余平台 ~/.local/share/nodule-ai-model-eval/）"
)

#: ``--runs-dir`` 的统一说明。
_RUNS_DIR_HELP = f"产物根目录（缺省 {DEFAULT_RUNS_DIR}/）"

#: ``--fixture`` 的统一说明 —— 三处出站面共用一个开关，不允许半开。
_FIXTURE_HELP = (
    "显式启用替身门：出站目标改用替身地址（仅测试用；缺地址即报错，不回落真实端点）"
)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""

    parser = argparse.ArgumentParser(
        prog="nodule-eval",
        description="肺结节AI模型评估系统 · 引擎命令行入口（运维与排障通道）",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="子命令")

    run = sub.add_parser("run", help="跑一轮评估：逐例调用推理端点并落产物")
    run.add_argument(
        "--cases",
        default="",
        metavar="cases.json",
        help=(
            "病例输入文件：完整请求 JSON，或只含病例数组的 JSON"
            "（与 --request 二选一）"
        ),
    )
    run.add_argument(
        "--request",
        default="",
        metavar="request.json",
        help="完整请求 JSON（须为对象且含 cases 字段；与 --cases 二选一）",
    )
    run.add_argument(
        "--model-id",
        default="",
        help="被评模型标识（对应 models/<model_id>.yaml）",
    )
    run.add_argument("--run-id", default="", help="本次 run 的标识（缺省按时刻生成）")
    run.add_argument("--dataset-ref", default="", help="数据集引用（写进 summary）")
    run.add_argument("--model-version", default="", help="被评模型版本（缺省取模型目录里的声明）")
    run.add_argument(
        "--runs-dir",
        default=DEFAULT_RUNS_DIR,
        metavar="DIR",
        help=_RUNS_DIR_HELP,
    )
    run.add_argument(
        "--fixture",
        action="store_true",
        help=_FIXTURE_HELP,
    )
    run.add_argument("--connect-timeout", type=float, default=None, help="连接超时（秒）")
    run.add_argument("--read-timeout", type=float, default=None, help="读取超时（秒）")
    run.add_argument(
        "--modality",
        default="DX",
        help="按检查号绑定影像时向 RIS 请求的模态（缺省 DX）",
    )
    run.add_argument(
        "--from",
        dest="date_from",
        default="",
        help="绑定时的检查日期下界 YYYY-MM-DD（缺省不限）",
    )
    run.add_argument(
        "--to",
        dest="date_to",
        default="",
        help="绑定时的检查日期上界 YYYY-MM-DD（缺省不限）",
    )
    run.add_argument(
        "--store",
        default="",
        metavar="PATH",
        help=_STORE_HELP,
    )
    run.add_argument(
        "--concurrency",
        type=int,
        default=None,
        metavar="N",
        help="工作线程数：同时处理几例（1 = 串行；缺省取请求里的 options.concurrency）",
    )
    run.add_argument(
        "--endpoint-limits",
        default="",
        metavar="inference=8,model=2",
        help=(
            "逐端点的在途上限（超限排队不丢弃）。缺省每个端点都取并发数。"
            "端点名写错当场报错 —— 限流写错名字等于限流没开"
        ),
    )
    run.add_argument(
        "--resume",
        action="store_true",
        help=(
            "断点续跑：跳过运行库里已跑完的例（不重复调用推理端点），"
            "只处理上次没跑完的例；同一 run_id 的上一版产物保留在 versions/ 下"
        ),
    )
    run.add_argument(
        "--size-tolerance-mm",
        type=float,
        default=None,
        metavar="MM",
        help=(
            "尺寸容差（毫米，缺省 2.0）：模型尺寸与金标准尺寸的差在这个范围内"
            "算一致（闭区间，恰好等于阈值算一致）"
        ),
    )
    run.add_argument(
        "--merge",
        choices=("rule", "model"),
        default="rule",
        help=(
            "金标准归并口径：rule = 规则基线（纯本地，不调模型端点）；"
            "model = 语义归并（调院内模型服务）。"
            "选 model 时模型端点不可达会自动降级为规则基线，"
            "该例仍计入指标但单列「规则降级」并挂人工复核"
        ),
    )
    run.add_argument(
        "--no-model-gateway",
        action="store_true",
        help=(
            "强制走纯规则基线：不建模型网关、不调模型端点。"
            "与 --merge model 同时给会当场报错（口径打架，不猜哪个优先）"
        ),
    )

    assemble = sub.add_parser(
        "assemble", help="装配本院病例集：RIS 取检查清单 → 按检查号幂等落盘"
    )
    assemble.add_argument(
        "--cases-root",
        default=DEFAULT_CASES_ROOT,
        metavar="DIR",
        help=f"病例集根目录（缺省 {DEFAULT_CASES_ROOT}/）",
    )
    assemble.add_argument(
        "--runs-dir",
        default=DEFAULT_RUNS_DIR,
        metavar="DIR",
        help=f"产物根目录，装配清单落 runs/<run_id>/（缺省 {DEFAULT_RUNS_DIR}/）",
    )
    assemble.add_argument("--run-id", default="", help="本次装配的标识（缺省按时刻生成）")
    assemble.add_argument("--modality", default="DX", help="按模态取清单（缺省 DX）")
    assemble.add_argument("--from", dest="date_from", default="", help="检查日期下界 YYYY-MM-DD")
    assemble.add_argument("--to", dest="date_to", default="", help="检查日期上界 YYYY-MM-DD")
    assemble.add_argument(
        "--accession",
        action="append",
        default=None,
        metavar="ACC",
        help="只装配指定检查号（可重复）；与 --cases 同时给时以本项为准",
    )
    assemble.add_argument(
        "--cases",
        default="",
        metavar="cases.json",
        help=(
            "可选：评估表病例 JSON。给了就用表里圈定的检查号作为装配集合，"
            "并按检查号兜底解析 image_ref"
        ),
    )
    assemble.add_argument(
        "--fixture",
        action="store_true",
        help="显式启用替身门：出站目标改用替身地址（仅测试用）",
    )

    inspect = sub.add_parser(
        "inspect",
        help="按调用记录ID反查：那次调用的入参与返回，并 join 回这一例的判定",
    )
    inspect.add_argument(
        "--tool-call-id",
        required=True,
        metavar="ID",
        help="调用记录 ID（表格「调用记录ID」列填的就是它）",
    )
    inspect.add_argument(
        "--runs-dir",
        default=DEFAULT_RUNS_DIR,
        metavar="DIR",
        help=f"产物根目录（缺省 {DEFAULT_RUNS_DIR}/）",
    )
    inspect.add_argument(
        "--run-id",
        default="",
        help="限定在某一轮里查（缺省在所有 run 里找；调用记录 ID 全局唯一）",
    )
    inspect.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出反查结果（给表格插件 / 脚本读；缺省输出中文文本）",
    )
    inspect.add_argument(
        "--snippet",
        action="store_true",
        help="只输出可直接粘进工单 / 飞检说明的核查片段",
    )

    redact = sub.add_parser(
        "redact-scan",
        help="产物脱敏扫描：查这一轮的产物里有没有患者可识别明细",
    )
    redact.add_argument(
        "--run-id",
        required=True,
        metavar="ID",
        help="要扫的那一轮（runs/<run_id>/）",
    )
    redact.add_argument(
        "--runs-dir",
        default=DEFAULT_RUNS_DIR,
        metavar="DIR",
        help=f"产物根目录（缺省 {DEFAULT_RUNS_DIR}/）",
    )
    redact.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出扫描结果（给表格插件 / CI 读；缺省输出中文文本）",
    )

    submit = sub.add_parser(
        "submit",
        help="把评估结论写回科研平台评估登记（须已人工复核）",
    )
    submit.add_argument("--run-id", default="", help="要写回的那一轮（缺省报错）")
    submit.add_argument("--store", default="", metavar="PATH", help=_STORE_HELP)
    submit.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, metavar="DIR", help=_RUNS_DIR_HELP)
    submit.add_argument("--fixture", action="store_true", help=_FIXTURE_HELP)
    submit.add_argument(
        "--submitted-by",
        default="",
        help="提交人（缺省取当前系统账号，写进登记的 submitted_by）",
    )
    submit.add_argument(
        "--review",
        action="store_true",
        help=(
            "先把这一轮推进到「已人工复核」再写回"
            "（不加则只写回已复核的轮；未复核直接报错，一次出站调用都不发）"
        ),
    )
    submit.add_argument(
        "--dry-run",
        action="store_true",
        help="只组装提交体并打印口径，**不出站**（用于核对写回前的内容）",
    )

    replay = sub.add_parser(
        "replay",
        help="回放写回回执：这一轮到底写上去了没有（本地回执台账）",
    )
    replay.add_argument("--run-id", default="", help="要查的那一轮（缺省报错）")
    replay.add_argument("--store", default="", metavar="PATH", help=_STORE_HELP)
    replay.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, metavar="DIR", help=_RUNS_DIR_HELP)
    replay.add_argument("--fixture", action="store_true", help=_FIXTURE_HELP)
    replay.add_argument(
        "--verify",
        action="store_true",
        help=(
            "去科研平台核对登记号（一次 GET 查询，不发 POST）；"
            "核对失败不算「没登记」，只报「查不到」"
        ),
    )
    replay.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出回执（给脚本读；缺省输出中文文本）",
    )

    doctor = sub.add_parser(
        "doctor",
        help="自检：环境变量、依赖、被评模型目录、存储目录（不发任何出站调用）",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出自检结果（给院内安装脚本读；缺省输出中文文本）",
    )
    doctor.add_argument("--store", default="", metavar="PATH", help="要检查的运行库路径（缺省用平台默认目录）")
    doctor.add_argument("--models-dir", default="", metavar="DIR", help="被评模型目录（缺省用项目内 models/）")
    doctor.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, metavar="DIR", help="产物根目录")
    doctor.add_argument(
        "--dataset-dir",
        default="",
        metavar="DIR",
        help="公开数据集（JSRT）缓存目录（缺省 data/jsrt；只用于对照实验，不影响评估主路径）",
    )
    doctor.add_argument(
        "--verify-dataset",
        action="store_true",
        help="对 JSRT 缓存做逐文件 SHA256 深度校验（耗时较久；缺省只查数量与元数据）",
    )
    doctor.add_argument(
        "--fixture",
        action="store_true",
        help=(
            "声明本次自检处于替身模式（只影响说明文字；doctor 本身不发起任何出站调用，"
            "也不打开替身门）"
        ),
    )

    export = sub.add_parser(
        "export",
        help="导出结论表（xlsx）与漏诊清单（csv），不依赖宏",
    )
    export.add_argument("--run-id", default="", help="要导出哪一轮（缺省报错）")
    export.add_argument("--store", default="", metavar="PATH", help=_STORE_HELP)
    export.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, metavar="DIR", help=_RUNS_DIR_HELP)
    export.add_argument(
        "--out-dir",
        default="",
        metavar="DIR",
        help="导出目录（缺省 runs/<run_id>/export/）",
    )
    export.add_argument(
        "--format",
        choices=(*EXPORT_FORMATS, "both"),
        default="both",
        help="导出格式：xlsx（结论工作簿）/ csv（逐例明细 + 漏诊清单）/ both（缺省）",
    )

    fill = sub.add_parser(
        "fill",
        help="把一轮评估的判定回填进评估表（只写新增六列，既有列零改动）",
    )
    fill.add_argument("--run-id", default="", help="要回填哪一轮（缺省报错）")
    fill.add_argument(
        "--sheet",
        required=True,
        metavar="PATH",
        help="评估表路径（.xlsm / .xlsx）",
    )
    fill.add_argument(
        "--sheet-name",
        default="",
        metavar="NAME",
        help="明细页名（缺省自动找「评估表」或「逐例明细」）",
    )
    fill.add_argument("--store", default="", metavar="PATH", help=_STORE_HELP)
    fill.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, metavar="DIR", help=_RUNS_DIR_HELP)

    return parser


def _load_cases_file(path: str) -> Any:
    """读病例输入文件。"""

    file_path = Path(path)
    if not file_path.is_file():
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"病例输入文件不存在：{file_path}",
        )
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"病例输入文件读不出来：{file_path}（{exc}）",
        ) from exc
    try:
        # **utf-8-sig**：Excel / WPS 写出的 JSON 可能带 BOM（ADODB.Stream 会加），
        # 带 BOM 与不带 BOM 都要读得对 —— 带 BOM 时 json.loads 会直接报错。
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"病例输入文件不是 UTF-8 编码：{file_path}（{exc}）",
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"病例输入文件不是合法 JSON：{file_path}（{exc}）",
        ) from exc


def _build_request(
    payload: Any,
    args: argparse.Namespace,
    *,
    require_object: bool = False,
) -> dict[str, Any]:
    """把病例输入文件 + 命令行参数组装成请求体。

    文件是**完整请求**时直接用它的字段（命令行参数可覆盖）；
    文件只是**病例数组**时，run_id / model_id / dataset_ref 由命令行给。

    ``require_object``
        ``--request`` 用它：这个入口只收**完整请求**（对象 + cases 字段）。
        给成病例数组时报错并说清「这个入口要完整请求」——
        把数组当请求跑，缺的那几个字段会被命令行默认值悄悄补上，
        跑出来的结论挂在一个来路不明的 run_id 上。
    """

    if isinstance(payload, Mapping):
        if "cases" not in payload:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail="病例输入文件是 JSON 对象，但缺少 cases 字段",
            )
        request = dict(payload)
    elif isinstance(payload, list):
        if require_object:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=(
                    "--request 只收完整请求 JSON（对象，含 run_id / model_id / "
                    "model_version / dataset_ref / cases）；"
                    "只含病例数组的文件请改用 --cases"
                ),
            )
        request = {"cases": payload}
    else:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail="病例输入文件须为 JSON 对象（含 cases）或 JSON 数组",
        )

    if args.model_id:
        request["model_id"] = args.model_id
    if args.model_version:
        request["model_version"] = args.model_version
    if args.dataset_ref:
        request["dataset_ref"] = args.dataset_ref
    if args.run_id:
        request["run_id"] = args.run_id
    if not request.get("run_id"):
        request["run_id"] = f"run-{_timestamp()}"
    if not request.get("dataset_ref"):
        request["dataset_ref"] = "未标注"
    options = dict(request.get("options") or {})
    if getattr(args, "concurrency", None) is not None:
        options["concurrency"] = int(args.concurrency)
    if getattr(args, "resume", False):
        options["resume"] = True
    if getattr(args, "size_tolerance_mm", None) is not None:
        options["size_tolerance_mm"] = float(args.size_tolerance_mm)
    endpoint_limits = _parse_endpoint_limits(getattr(args, "endpoint_limits", "") or "")
    if endpoint_limits:
        options["endpoint_limits"] = endpoint_limits
    if options:
        request["options"] = options
    return request


def _parse_endpoint_limits(text: str) -> dict[str, int]:
    """把 ``inference=8,model=2`` 这样的命令行写法解析成逐端点上限。

    **端点名写错当场报错**（并列出可用的端点名）—— 限流写错名字等于限流没开，
    而跑完之后从报告里看不出「我限的那个端点根本不存在」。
    """

    if not text.strip():
        return {}
    limits: dict[str, int] = {}
    for chunk in text.split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" not in item:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"端点上限的写法应为「端点名=数字」（实得 {item!r}）",
            )
        name, _, value = item.partition("=")
        name = name.strip()
        value = value.strip()
        try:
            limits[name] = int(value)
        except ValueError as exc:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"端点 {name} 的并发上限须为整数（实得 {value!r}）",
            ) from exc
    return parse_endpoint_limits(limits)


def _timestamp() -> str:
    from .audit.toolcall import now_iso

    return now_iso().replace(":", "").replace("-", "").replace("T", "-")


def _bind_request(
    request: EvalRequest,
    config: Config,
    routed: Mapping[str, str],
    args: argparse.Namespace,
) -> tuple[EvalRequest, Any]:
    """把表格里的检查号绑到 RIS 真源的影像标识上（三键绑定）。

    **只有当有例没带影像标识时才去读 RIS** —— 已经带好的（JSON 直跑路径）
    不多打一次 RIS。绑定结果落 ``runs/<run_id>/binding.json``，
    一行表格对应哪张影像、凭什么这么绑、有没有留差异，都在里面。

    一对多歧义、RIS 里找不到、检查号在表里重复 —— 这三类**挂起**：该例不带
    影像标识，引擎不会发起推理调用，它进汇总行的「挂起数」，不进指标分母。
    """

    if all(case.study_uid for case in request.cases):
        return request, None

    adapter = RISAdapter(config, base_url=routed["ris"])
    result = adapter.fetch_studies(
        modality=args.modality, date_from=args.date_from, date_to=args.date_to
    )
    binding = bind_cases(
        request.cases,
        result.items,
        run_id=request.run_id,
        runs_dir=args.runs_dir,
        modality=args.modality,
        authority=AUTHORITY_RIS,
        write=True,
    )
    bound = EvalRequest(
        run_id=request.run_id,
        model_id=request.model_id,
        model_version=request.model_version,
        dataset_ref=request.dataset_ref,
        cases=binding.cases,
        options=request.options,
    )
    return bound, binding


def _open_store(args: argparse.Namespace) -> RunStore:
    """打开 run store（SQLite）。

    路径给不出 / 打不开时**指名报错**（错误码 + 中文文案），
    绝不静默换成本地替身或干脆不落库 —— 「跑完了但状态没落盘」最难查。
    """

    db_path = args.store.strip() or None
    return RunStore.open(db_path, runs_dir=args.runs_dir)


def _build_normalizer(
    config: Config,
    routed: Mapping[str, str],
    args: argparse.Namespace,
) -> Any:
    """按 ``--merge`` 选金标准归并口径。

    ``rule``（缺省）
        规则基线 —— 纯本地，不调模型端点。**确定性基线必须能独立跑完整条主干**：
        模型端点挂了，评估照样出得来（结论口径退到规则基线）。
    ``model``
        语义归并 —— 调院内模型服务。模型端点不可达时**自动降级**为规则基线：
        该例仍计入指标，但单列「规则降级」并挂人工复核（任务 24）。

    归并器不直接 import 厂商 SDK：模型端点走
    :class:`nodule_eval.gateway.ModelGateway`（OpenAI-compatible 原始 HTTP）。
    """

    merge = getattr(args, "merge", "rule")
    if bool(getattr(args, "no_model_gateway", False)) and merge == "model":
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                "--no-model-gateway 与 --merge model 打架："
                "前者强制走纯规则基线，后者要走模型归并 —— "
                "两个都给了不猜哪个优先，请去掉其一"
            ),
        )
    if merge != "model":
        return None
    from .gateway import ModelGateway
    from .goldstd.merge import ModelMergeNormalizer

    gateway = ModelGateway(config, base_url=routed.get("model"))
    return ModelMergeNormalizer(gateway)


def _merge_mode_text(args: argparse.Namespace) -> str:
    """这一轮走的是哪条归并口径（打给用户看，别让人猜）。"""

    if bool(getattr(args, "no_model_gateway", False)):
        return "规则基线（--no-model-gateway：不建模型网关、不调模型端点）"
    if getattr(args, "merge", "rule") == "model":
        return "模型归并（ModelGateway · OpenAI-compatible；不可达时降级为规则基线）"
    return "规则基线（纯本地，不调模型端点）"


def _prompt_version_line(engine: Any) -> str:
    """这一轮的**归并口径版本**（任务 47）—— 打给用户看的一行。

    走语义归并时，归并口径住在 ``prompts/goldstd_merge.v1.md`` 里，
    **版本号在文件名里**。这一行直接说清「这批结论是哪版口径跑的」：
    版本号 + 文件相对路径 + 内容哈希前 12 位（凭哈希可反查回那一版文件）。

    走规则基线、或归并器没带提示词文件（内联提示词）时 → 返回空串
    （**不打印这一行**，别塞一句「无」占位）。

    读不到提示词文件**不报错** —— 这一行是给人看的补充信息，
    不该把一整轮跑完的结论拖下水；真正的哈希留痕在
    ``model_calls.jsonl`` 里，那才是可核对的凭据。
    """

    normalizer = getattr(engine, "normalizer", None)
    if normalizer is None:
        return ""
    if not uses_model_endpoint(normalizer):
        return ""
    ref = getattr(normalizer, "prompt_ref", None)
    if ref is None:
        return ""
    version = str(getattr(ref, "version", "") or "").strip()
    path = str(getattr(ref, "path", "") or "").strip()
    sha = str(getattr(ref, "sha256", "") or "").strip()
    if not path:
        return ""
    tail = f"，sha256 {sha[:12]}" if sha else ""
    head = f"归并口径版本: {version}" if version else "归并口径版本: 未标版本号"
    return f"{head}（{path}{tail}）"


def _resolve_cases_path(args: argparse.Namespace) -> tuple[str, bool]:
    """定下这一轮读哪个文件 —— **缺参数当场报错并指名**。

    返回 ``(路径, 是否要求完整请求)``。

    缺参报错走 ``CONTRACT_INVALID`` 而不是 argparse 自己的
    ``the following arguments are required`` —— 后者是英文，
    院里信息科的人看不懂到底该补哪个。
    """

    cases = (getattr(args, "cases", "") or "").strip()
    request = (getattr(args, "request", "") or "").strip()
    if cases and request:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail="--cases 与 --request 只能给一个（前者可给病例数组，后者只收完整请求）",
        )
    if not cases and not request:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                "run 缺少必填参数 --cases（或 --request）："
                "病例输入文件（完整请求 JSON，或只含病例数组的 JSON）"
            ),
        )
    return (request, True) if request else (cases, False)


def _run(args: argparse.Namespace) -> int:
    path, require_object = _resolve_cases_path(args)
    payload = _load_cases_file(path)
    request_payload = _build_request(payload, args, require_object=require_object)

    if not (args.model_id or str(request_payload.get("model_id") or "").strip()):
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                "run 缺少必填参数 --model-id：被评模型标识"
                "（对应 models/<model_id>.yaml；命令行不给时须由请求文件里的 model_id 提供）"
            ),
        )
    request = parse_request(request_payload)

    config = load_config()
    spec = load_model_spec(request.model_id)

    fixture = fixture_enabled(args.fixture)
    routed = routed_base_urls(config, fixture=fixture)

    request, binding = _bind_request(request, config, routed, args)
    if binding is not None:
        counts = binding.counts()
        print(f"绑定清单      : {binding.binding_path}")
        print(f"唯一绑定      : {counts['bound']}")
        print(f"一对多挂起    : {counts['ambiguous']}")
        print(f"未找到/未解析 : {counts['unresolved']}")
        print(f"检查号重复    : {counts['duplicate']}")
        if counts["with_difference"]:
            print(f"与 RIS 不一致 : {counts['with_difference']}（以 RIS 为准，已挂起复核）")

    store = _open_store(args)
    engine = Engine(
        config,
        spec,
        runs_dir=args.runs_dir,
        store=store,
        fixture=fixture,
        normalizer=_build_normalizer(config, routed, args),
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
    )
    response = engine.run(request)

    artifacts = engine.last_artifacts
    print(f"run_id        : {response.run_id}")
    print(f"被评模型      : {response.summary.model_id} {response.summary.model_version}")
    print(f"归并口径      : {_merge_mode_text(args)}")
    # **口径版本**（任务 47）：走语义归并时，终端直接把「这批结论是哪版口径
    # 跑的」说清楚 —— 提示词版本号 + 文件相对路径 + 内容哈希前 12 位。
    # 复核的人不用去翻底账才知道该对哪一版。
    prompt_line = _prompt_version_line(engine)
    if prompt_line:
        print(prompt_line)
    print(f"病例数        : {response.summary.total_cases}")
    metrics = response.metrics
    print(
        "原始计数      : "
        f"TP {metrics.tp} / FP {metrics.fp} / FN {metrics.fn} / TN {metrics.tn}"
        f"（进分母 {metrics.tp + metrics.fp + metrics.fn + metrics.tn} 例）"
    )
    print(
        "敏感度        : "
        f"{format_metric_value(metrics.sensitivity, metrics.sensitivity_reason)}"
        "（TP/(TP+FN)）"
    )
    print(
        "特异度        : "
        f"{format_metric_value(metrics.specificity, metrics.specificity_reason)}"
        "（TN/(TN+FP)）"
    )
    print(
        "阳性预测值    : "
        f"{format_metric_value(metrics.ppv, metrics.ppv_reason)}（TP/(TP+FP)）"
    )
    print(
        "阴性预测值    : "
        f"{format_metric_value(metrics.npv, metrics.npv_reason)}（TN/(TN+FN)）"
    )
    print(f"失败数        : {response.summary.failed_count}")
    # **失败分布**（任务 42）：按错误码说清「哪一类失败、几例、为什么」。
    # 文案与表格 ``failure_reason`` 列、``summary.json`` 的 ``failures`` 段
    # 同源（``error_matrix.failure_breakdown``）—— 三处必然是同一句话。
    for line in render_failure_lines(failure_breakdown(response.case_rows)):
        print(line)
    print(f"挂起数        : {response.summary.needs_review_count}")
    print(f"不可比数      : {response.summary.incomparable_count}")
    print(f"模型归并      : {response.summary.model_normalized_count} 例")
    print(f"规则降级      : {response.summary.rule_degraded_count} 例")
    if response.summary.degrade_note:
        print(f"降级说明      : {response.summary.degrade_note}")
    if response.summary.skipped_count:
        print(
            f"跳过（已跑完）: {response.summary.skipped_count}"
            f"（断点续跑：这些例本版没重跑）"
        )
    print(f"本轮版本      : 第 {response.summary.version_no} 版")
    print(f"run 状态      : {response.status}")
    print(f"运行库        : {store.db_path}")
    if engine.limiters is not None:
        stats = engine.limiters.stats()
        summary = "、".join(
            f"{name} 峰值 {item['peak_in_flight']}/{item['max_concurrency']}"
            f"（排队 {item['queued']}）"
            for name, item in stats.items()
            if item["acquisitions"] or item["max_concurrency"] != engine.concurrency
        )
        print(f"并发数        : {engine.concurrency}")
        print(f"端点限流      : {summary or '无'}")
    if artifacts is not None:
        print(f"逐例明细      : {artifacts.case_rows_csv}")
        print(f"漏诊清单      : {artifacts.missed_list_csv}")
        print(f"评估结论      : {artifacts.summary_json}")
        print(f"调用底账      : {artifacts.tool_calls_jsonl}")
        # **模型调用底账**只在真的调过模型时才有（走规则基线时压根没调，
        # 压根就没有账）—— 没有就不印，免得人以为「有但丢了」。
        if artifacts.model_calls_jsonl is not None:
            print(f"模型调用底账  : {artifacts.model_calls_jsonl}")
        print(f"耗时台账      : {artifacts.case_timings_json}")
        print(f"结果文件      : {artifacts.result_json}")
    print(f"漏诊例数      : {len(response.missed_list)}（与漏诊数 FN 相同）")
    return 0


def _load_case_inputs(path: str) -> tuple[CaseInput, ...]:
    """读评估表病例文件（完整请求 JSON 或只含病例数组的 JSON）。"""

    payload = _load_cases_file(path)
    if isinstance(payload, Mapping):
        raw_cases = payload.get("cases")
        if raw_cases is None:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail="病例输入文件是 JSON 对象，但缺少 cases 字段",
            )
    elif isinstance(payload, list):
        raw_cases = payload
    else:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail="病例输入文件须为 JSON 对象（含 cases）或 JSON 数组",
        )
    if not isinstance(raw_cases, list):
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail="病例输入文件的 cases 须为数组",
        )
    return tuple(parse_case(item, index) for index, item in enumerate(raw_cases))


def _assemble(args: argparse.Namespace) -> int:
    """装配本院病例集：RIS 取清单 → 按检查号幂等落盘 → 落装配清单。

    **请求集合从哪来**（决定幂等能不能做到「二次装配不打 RIS」）：

    - ``--accession`` 给了 → 用它；
    - ``--cases <评估表病例 JSON>`` 给了 → 用表里圈定的检查号（**产品主路径**：
      表格圈定 → 装配这几例 → 二次装配一次 RIS 都不打）；
    - 两者都没给 → 装配 RIS 清单里的全部（清单本身会变，所以清单读一次）。
    """

    config = load_config()
    fixture = fixture_enabled(args.fixture)
    routed = routed_base_urls(config, fixture=fixture)
    adapter = RISAdapter(config, base_url=routed["ris"])

    sheet_cases: tuple[CaseInput, ...] = ()
    if args.cases:
        sheet_cases = _load_case_inputs(args.cases)

    requested: list[str] | None = args.accession
    if requested is None and sheet_cases:
        requested = [case.accession_no for case in sheet_cases]

    run_id = args.run_id or f"assemble-{_timestamp()}"
    assembly = assemble_caseset(
        adapter,
        args.cases_root,
        run_id=run_id,
        requested=requested,
        modality=args.modality,
        date_from=args.date_from,
        date_to=args.date_to,
        runs_dir=args.runs_dir,
    )

    counts = assembly.counts()
    print(f"run_id        : {assembly.run_id}")
    print(f"病例集根目录  : {assembly.cases_root}")
    print(f"装配清单      : {assembly.manifest_path}")
    print(f"新增装配      : {counts['assembled']}")
    print(f"跳过（已存在）: {counts['skipped']}")
    print(f"RIS 里没有    : {counts['unresolved']}")
    print(f"一对多待判定  : {counts['ambiguous']}")
    if counts["damaged_dirs"]:
        print(f"损坏待补齐    : {counts['damaged_dirs']}")

    if sheet_cases:
        resolution = resolve_case_inputs(sheet_cases, args.cases_root)
        resolved_counts = resolution.counts()
        print(f"评估表病例数  : {resolved_counts['total']}")
        print(
            f"image_ref 兜底: {resolved_counts['resolved_from_caseset']}"
            f"（表格未给、从已装配病例集取）"
        )
        print(f"引用未解析    : {resolved_counts['unresolved']}（已标 needs_review，须人工复核）")

    return 0


def _inspect(args: argparse.Namespace) -> int:
    """按调用记录 ID 反查：从结论回到原始那一次调用。

    输出三样东西：那次调用的**入参与返回**、这一例的**判定与判定依据**、
    以及一段**可直接粘进工单 / 飞检说明**的核查片段。
    """

    result = inspect_tool_call(args.runs_dir, args.tool_call_id, run_id=args.run_id)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.snippet:
        print(render_snippet(result))
        return 0
    print(render_inspect(result))
    print()
    print("── 可直接粘进工单 / 飞检说明的核查片段 ─────")
    print(render_snippet(result))
    return 0


def _redact_scan(args: argparse.Namespace) -> int:
    """产物脱敏扫描：扫一遍这一轮的产物，看有没有患者可识别明细。

    **命中即失败** —— 退出码非零，错误打在标准错误上；
    扫描器**只报不改**（底账必须原样留，改了就对不上原始调用）。
    """

    run_dir = Path(args.runs_dir) / args.run_id
    report = scan_run_artifacts(run_dir)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_scan_report(report))
    if report.ok:
        return 0
    raise NoduleEvalError(
        ErrorCode.ARTIFACT_PII_DETECTED
        if any(finding.kind != "unregistered_field" for finding in report.findings)
        else ErrorCode.ARTIFACT_FIELD_UNREGISTERED,
        detail=(
            f"{len(report.findings)} 处命中（{report.counts()}）；"
            f"详见 {report.run_id} 的产物目录"
        ),
    )


def _require_run_id(args: argparse.Namespace, command: str) -> str:
    """取 ``--run-id`` —— 缺了就指名报错。"""

    run_id = (getattr(args, "run_id", "") or "").strip()
    if not run_id:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"{command} 缺少必填参数 --run-id：要处理的那一轮",
        )
    return run_id


def _submit(args: argparse.Namespace) -> int:
    """把评估结论写回科研平台评估登记。

    **闸门在出站之前**：这一轮没到「已人工复核」，或者触发方不是人 ——
    **一次出站调用都不发**，报错说清是哪一条没过。

    ``--review`` 是「无宏退路」下的人工复核动作：命令行上补一次复核，
    再走写回。不加它就跑，闸门照常拦。

    ``--dry-run`` 只组装提交体并打印口径，**不出站** —— 写回前想核对
    「对端会收到什么」时用它。
    """

    from .adapters.research_platform import ResearchPlatformAdapter
    from .store import RUN_STATUS_EVALUATED, SUMMARY_FILENAME
    from .writeback import (
        WRITEBACK_AUDIT_FILENAME,
        WritebackService,
        build_submission,
    )

    run_id = _require_run_id(args, "submit")
    config = load_config()
    store = _open_store(args)
    fixture = fixture_enabled(args.fixture)
    routed = routed_base_urls(config, fixture=fixture)
    adapter = ResearchPlatformAdapter(config, base_url=routed["research_platform"])
    service = WritebackService(config, store, adapter=adapter)

    run = store.get_run(run_id)
    if run is None:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"运行库里没有这一轮：{run_id}",
        )

    if args.review and run.status == RUN_STATUS_EVALUATED:
        store.review_run(
            run_id,
            actor=ACTOR_HUMAN_REVIEW,
            reason="命令行人工复核（submit --review）",
        )
        run = store.get_run(run_id)

    decision = service.evaluate(run_id, actor=ACTOR_HUMAN_SUBMIT)
    print(f"run_id        : {run_id}")
    print(f"当前 run 状态 : {run.status}")
    if run.status == RUN_STATUS_SUBMITTED:
        # 这一轮**已经写回过** —— 再点一次「确认提交」是幂等回放，
        # 不是「闸门拒绝」。这两种情况在终端上必须分得开：
        # 前者是正常流程走完了，后者是没复核就想写出去。
        print("写回闸门      : 本轮已写回（状态 submitted）—— 下面按幂等键回放本地回执，不发出站请求")
    else:
        print(f"写回闸门      : {'放行' if decision.allowed else '拦下'} —— {decision.reason}")

    if args.dry_run:
        if run.status == RUN_STATUS_SUBMITTED:
            receipt = store.get_receipt_by_run(run_id)
            print("干跑模式      : 只核对，不发出站调用")
            print(
                "本轮已写回    : 幂等键 "
                + (receipt.idempotency_key if receipt is not None else "（无回执）")
            )
            print("再次提交      : 走幂等回放，对端不会多出一条登记")
            return 0
        if not decision.allowed:
            raise NoduleEvalError(
                ErrorCode.WRITEBACK_NOT_REVIEWED,
                detail=decision.reason,
                current_status=decision.current_status,
                required_status=decision.required_status,
                actor=decision.actor,
            )
        rows = store.recompute(run_id)
        summary_payload = store.read_json_artifact(run_id, SUMMARY_FILENAME)
        if not isinstance(summary_payload, Mapping):
            summary_payload = None
        submission = build_submission(
            run,
            rows,
            submitted_by=args.submitted_by,
            summary_payload=summary_payload,
        )
        body = submission.to_dict()
        print("干跑模式      : 只核对，不发出站调用")
        print(f"幂等键        : {submission.idempotency_key()}")
        print(f"逐例条数      : {len(submission.case_rows)}")
        print(f"请求体大小    : {len(json.dumps(body, ensure_ascii=False).encode('utf-8'))} 字节")
        print(f"提交人        : {submission.submitted_by}")
        print(
            "原始计数      : "
            f"TP {submission.metrics.get('tp')} / FP {submission.metrics.get('fp')} / "
            f"FN {submission.metrics.get('fn')} / TN {submission.metrics.get('tn')}"
        )
        return 0

    outcome = service.submit(
        run_id,
        actor=ACTOR_HUMAN_SUBMIT,
        submitted_by=args.submitted_by,
    )
    receipt = outcome.receipt
    print(f"写回结果      : {'幂等回放（未发出站请求）' if outcome.replayed else '已写回'}")
    if receipt is not None:
        print(f"登记号        : {receipt.registration_id}")
        print(f"幂等键        : {receipt.idempotency_key}")
        print(f"回执状态      : {receipt.status}（HTTP {receipt.http_status}）")
        print(f"尝试次数      : {receipt.attempts}")
    if outcome.replayed:
        # 回放交出去的是**当初那一版**的记录 —— 「提交人」那一栏里存的是
        # 触发方（human-submit），不是操作者账号，标签要说准。
        print(f"原提交触发方  : {outcome.submitted_by}")
    else:
        print(f"提交人        : {outcome.submitted_by}")
    print(f"提交时间      : {outcome.submitted_at}")
    print(f"写回底账      : {store.artifact_path(run_id, WRITEBACK_AUDIT_FILENAME)}")
    return 0


def _replay(args: argparse.Namespace) -> int:
    """回放写回回执：这一轮到底写上去了没有。

    **默认零出站**：回放的是**本地回执台账**里的那张回执 ——
    「请求发出去了」不等于「登记生效了」，这一条把本地记的说清楚。

    ``--verify`` 才去科研平台核对登记号（**一次 GET 查询，不发 POST**）。
    核对失败（连不上 / 5xx）**不算「没登记」**，只报「查不到」。
    """

    from .adapters.research_platform import compute_idempotency_key
    from .writeback import WritebackService

    run_id = _require_run_id(args, "replay")
    store = _open_store(args)
    run = store.get_run(run_id)
    if run is None:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"运行库里没有这一轮：{run_id}",
        )

    key = compute_idempotency_key(run.run_id, run.model_id, run.dataset_ref)
    receipt = store.get_receipt_by_run(run_id) or store.get_receipt(key)
    if receipt is None:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"这一轮还没有写回回执：{run_id}（说明这一轮压根没提交过）",
        )

    payload: dict[str, Any] = {
        "run_id": run_id,
        "run_status": run.status,
        "local_status": receipt.status,
        "registration_id": receipt.registration_id,
        "idempotency_key": receipt.idempotency_key,
        "submitted_at": receipt.submitted_at,
        "received_at": receipt.received_at,
        "http_status": receipt.http_status,
        "attempts": receipt.attempts,
        "actor": receipt.actor,
        "payload_sha256": receipt.payload_sha256,
        "error_code": receipt.error_code,
        "error_detail": receipt.error_detail,
    }

    if args.verify:
        config = load_config()
        fixture = fixture_enabled(args.fixture)
        routed = routed_base_urls(config, fixture=fixture)
        from .adapters.research_platform import ResearchPlatformAdapter

        adapter = ResearchPlatformAdapter(config, base_url=routed["research_platform"])
        service = WritebackService(config, store, adapter=adapter)
        try:
            payload["verification"] = service.verify(run_id, remote=True).to_dict()
        except NoduleEvalError as exc:
            # **核对失败不等于「没登记」** —— 连不上对端时只能说「查不到」。
            payload["verification"] = {
                "matched": False,
                "remote_found": False,
                "reason": f"对端核对未完成：{exc.message}（查不到 ≠ 没登记）",
            }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"run_id        : {run_id}")
    print(f"run 状态      : {run.status}")
    print(f"本地回执状态  : {receipt.status}")
    print(f"登记号        : {receipt.registration_id or '（无）'}")
    print(f"幂等键        : {receipt.idempotency_key}")
    print(f"提交时间      : {receipt.submitted_at or '（无）'}")
    print(f"尝试次数      : {receipt.attempts}")
    if receipt.error_code:
        print(f"错误码        : {receipt.error_code}")
        print(f"错误详情      : {receipt.error_detail}")
    verification = payload.get("verification")
    if isinstance(verification, Mapping):
        print(f"对端核对      : {verification.get('reason', '')}")
        if verification.get("matched"):
            print("核对结论      : 一致 —— 登记确实生效了")
        else:
            print("核对结论      : 未确认（须人工核实）")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    """自检：环境变量、依赖、出站目标、模型目录、运行库、产物目录、公开数据集缓存。

    **不发任何出站调用** —— 自检程序去打真实端点会消耗额度，
    也会把对端日志搞脏。阻断项不通过时退出码非零，
    院内安装脚本据此判断「装好了没有」。

    **doctor 同时是安装验证脚本**：它会真的打开一次运行库并做一次写入探针
    （写完立刻回滚），把「目录能写但文件写不进去」这类问题挡在安装现场。
    """

    report = run_doctor(
        store_path=(args.store or "").strip() or None,
        models_dir=(args.models_dir or "").strip() or None,
        runs_dir=args.runs_dir,
        fixture_explicit=bool(getattr(args, "fixture", False)),
        dataset_dir=(args.dataset_dir or "").strip() or None,
        verify_dataset=bool(getattr(args, "verify_dataset", False)),
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_report(report))
    return 0 if report.ok else EXIT_PRODUCT_ERROR


def _export(args: argparse.Namespace) -> int:
    """导出结论表（xlsx）与漏诊清单（csv）—— **不依赖宏**。

    内容全部取自 run store：逐例行是库里的真源，不从 CSV 再解析一遍
    （二次解析会把「产物被改过」藏起来）。导出件里**不落金标准原文**。
    """

    run_id = _require_run_id(args, "export")
    store = _open_store(args)
    out_dir = (args.out_dir or "").strip() or str(Path(args.runs_dir) / run_id / "export")
    formats = EXPORT_FORMATS if args.format == "both" else (args.format,)
    result = export_run(store, run_id, out_dir, formats=formats)

    print(f"run_id        : {result.run_id}")
    print(f"导出目录      : {result.out_dir}")
    print(f"逐例条数      : {result.case_count}")
    print(f"漏诊条数      : {result.missed_count}")
    if result.workbook_path:
        print(f"结论工作簿    : {result.workbook_path}")
    if result.missed_list_path:
        print(f"漏诊清单      : {result.missed_list_path}")
    if result.case_rows_path:
        print(f"逐例明细      : {result.case_rows_path}")
    return 0


def _fill(args: argparse.Namespace) -> int:
    """把一轮评估的判定**回填进评估表** —— 只写新增六列。

    **既有三列一个字都不动**（金标准原文是科室写的原始件）——
    回填前后逐单元格比对，对不上就放弃落盘（表保持原样）。
    落盘走原子替换：中断不会留下半截回填的表。
    """

    run_id = _require_run_id(args, "fill")
    store = _open_store(args)
    rows = store.recompute(run_id)
    if not rows:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"运行库里没有这一轮的逐例行：{run_id}",
        )
    metrics = compute_metrics_from_rows(rows)
    missed = build_missed_list(rows)
    counts = count_raw_counts(rows)

    summary_payload: Mapping[str, Any] = {}
    try:
        loaded = store.read_json_artifact(run_id, "summary.json")
    except (OSError, NoduleEvalError):
        loaded = None
    if isinstance(loaded, Mapping):
        summary_payload = loaded

    summary_map = {
        "total_cases": summary_payload.get("total_cases") or len(rows),
        "degrade_note": summary_payload.get("degrade_note") or "",
    }
    metrics_map = {
        "tp": counts.tp,
        "fp": counts.fp,
        "fn": counts.fn,
        "tn": counts.tn,
        "sensitivity": metrics.sensitivity,
        "specificity": metrics.specificity,
        "ppv": metrics.ppv,
        "npv": metrics.npv,
        "failed_count": counts.failed,
        "rule_degraded_count": count_rule_degraded(rows),
        "needs_review_count": counts.needs_review,
        "incomparable_count": counts.incomparable,
        "model_normalized_count": count_model_normalized(rows),
    }

    status_obj = read_status(status_path(Path(args.runs_dir) / run_id))
    result = backfill_workbook(
        args.sheet,
        rows,
        summary=summary_map,
        metrics=metrics_map,
        missed_list=missed,
        status=status_obj,
        sheet_name=(args.sheet_name or "").strip() or None,
    )

    print(f"run_id        : {run_id}")
    print(f"评估表        : {result.path}（明细页：{result.sheet_name}）")
    print(f"回填行数      : {result.written_rows}（其中失败例 {result.failed_rows}）")
    print(f"汇总区格数    : {result.summary_cells}")
    print(f"漏诊清单行数  : {result.missed_rows}")
    print(f"既有列核对    : {result.checked_cells} 格逐格比对，未改动")
    if result.empty_state:
        print(f"空态          : {result.empty_state}")
    print(f"状态栏        : {result.status_text}")
    for warning in result.warnings:
        print(f"[提示] {warning}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """命令行主入口。"""

    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.command == "run":
            return _run(args)
        if args.command == "assemble":
            return _assemble(args)
        if args.command == "submit":
            return _submit(args)
        if args.command == "inspect":
            return _inspect(args)
        if args.command == "replay":
            return _replay(args)
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "export":
            return _export(args)
        if args.command == "redact-scan":
            return _redact_scan(args)
        if args.command == "fill":
            return _fill(args)
    except NoduleEvalError as exc:
        print(f"[{exc.code.value}] {exc.message}", file=sys.stderr)
        return EXIT_PRODUCT_ERROR
    except Exception as exc:  # pragma: no cover - 未预期异常兜底
        print(f"[UNEXPECTED] {exc}", file=sys.stderr)
        return EXIT_UNEXPECTED

    parser.print_help()
    return EXIT_UNEXPECTED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
