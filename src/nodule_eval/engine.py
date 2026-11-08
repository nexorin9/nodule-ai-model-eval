"""引擎：逐例 fan-out —— 选定模型 → 调用 → 归并 → 判定 → 下一例。

**源产品证据**：参考实现用 LangGraph 的「process → execute → process」两节点循环
驱动多工具调用（``github_ref/MedRAX/medrax/agent/agent.py:91-98``）：
``process`` 让模型决定下一步调哪个工具，``execute`` 把这一批工具调用跑完
（``agent.py:133-166``），``has_tool_calls`` 决定**循环什么时候结束**
（``agent.py:120-131``：模型不再要工具就退出）。

本产品把推进条件换成**确定性的配置选型**：被评模型在模型目录
（``models/<model_id>.yaml``）里定死，循环的终止条件是「病例集跑完」。
引擎**不猜、不问模型下一步做什么** —— 被评模型的输出是评估对象，
不是评估流程的调度者。

**任务 3 → 14 → 15 → 16 → 17 的推进**：

- 任务 3：单例循环（调用 + 落底账 + 落逐例行），判定列写过渡标记 ``pending``；
- **任务 14**：加深为**逐例 fan-out** —— 每一例走完
  「调用 → 取回归一化结果 → 归并金标准 → 判定 → 落行与底账 → 逐例产物归档」，
  循环有**显式终止条件**（病例集跑完 / run 级致命错误），
  退出后 run 状态推进到 ``evaluated``（判定已出，仍是草稿层）；
- **任务 15（本轮）**：加**并发限流** —— ``concurrency`` 个工作线程并行处理，
  推理端点与模型端点**各自**限流（超限排队，不丢弃）；
- **任务 16**：加断点续跑（逐例判定行判完即落库当检查点）；
- **任务 17（本轮）**：把失败收口成**显式的隔离层** —— 一次失败收成一条
  统一口径的失败记录（:class:`CaseFailure`：错误码 + 固定失败原因 +
  原始 HTTP 状态 + 失败类别 + 端点），失败例**永不**进指标分母，
  单例失败**不阻断整批**。

**本轮的判定深度**：判定落到**存在性**一层（属性三层在任务 27 落）。
判定依据逐层记下来 —— 复核的人一眼能看出「这一例是存在性不一致，还是属性层没比」。

**并发下的两条纪律**：

1. **逐例行的顺序 = 输入顺序**。并发跑的完成顺序是乱的，
   但 ``case_rows`` 必须与评估表的行序一一对应 —— 表格回填按行号写回，
   顺序乱了就写错行。
2. **并发不改变判定**。同一批输入，串行跑与并发跑**逐例判定必须一致**；
   压测脚本与单测都断言这一点（任务 15 的验收项）。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .adapters.http import HttpClient
from .adapters.inference import DetectionResult, InferenceAdapter
from .audit.case_artifact import CaseArtifact, write_case_artifact
from .audit.modelcall import MODEL_CALLS_FILENAME, ModelCallRecorder
from .audit.toolcall import (
    ToolCallRecorder,
    build_call_args,
    build_call_name,
    new_tool_call_id,
    now_iso,
)
from .audit.version import (
    ATTEMPT_STATUS_COMPLETED,
    ATTEMPT_STATUS_INTERRUPTED,
    RunVersion,
    begin_version,
    finish_version,
)
from .config import Config, ModelSpec
from .contracts import (
    DEFAULT_SIZE_TOLERANCE_MM,
    VERDICT_FAILED,
    CaseInput,
    CaseRow,
    EvalRequest,
    EvalResponse,
    Summary,
    counts_toward_denominator,
    partition_rows,
)
from .errors import ErrorCode, NoduleEvalError, error_text, flatten
from .fixture_gate import routed_base_urls
from .goldstd import GoldLabels, normalize_gold_standard
from .limits import (
    ENDPOINT_INFERENCE,
    ENDPOINT_MODEL,
    ENDPOINT_RESEARCH,
    ENDPOINT_RIS,
    LimiterSet,
    Slot,
)
from .metrics import (
    Judgement,
    RawCounts,
    assert_metrics_consistent,
    build_missed_list,
    build_summary_payload,
    compute_metrics,
    count_model_normalized,
    count_raw_counts,
    count_rule_degraded,
    gold_summary_text,
    judge_case,
    write_missed_list_csv,
)
from .metrics.report import assert_summary_consistent
from .audit.redact import mask_payload, register_artifact_fields
from .sheets.status import (
    RunStatus,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_INTERRUPTED,
    STATE_RUNNING,
    now_stamp,
    status_path,
    write_status,
)
from .store import (
    ACTOR_ASSEMBLE,
    ACTOR_ENGINE,
    CASE_ROWS_FILENAME,
    CASE_STATUS_CALLED,
    CASE_STATUS_NORMALIZED,
    CASE_STATUS_PENDING,
    CASE_TIMINGS_FILENAME,
    CASE_TRANSITIONS,
    DEFAULT_RUNS_DIR,
    MISSED_LIST_FILENAME,
    RESULT_FILENAME,
    RUN_STATUS_ASSEMBLED,
    RUN_STATUS_CALLED,
    RUN_STATUS_DRAFT,
    RUN_STATUS_EVALUATED,
    RUN_STATUS_FAILED,
    RUN_STATUS_REVIEWED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUBMITTED,
    SUMMARY_FILENAME,
    RunStore,
    case_status_for_row,
    case_status_path,
    is_case_terminal,
    write_case_rows_csv,
    write_json_file,
)

#: 金标准归并函数签名：吃一例，吐一组标签。
Normalizer = Callable[[CaseInput], GoldLabels]

#: 判定函数签名：吃标签与候选，吐判定。
#:
#: 缺省实现额外接一个 ``size_tolerance_mm`` 关键字（任务 27）——
#: 引擎按本轮 ``options.size_tolerance_mm`` 把它传下去；
#: 自定义实现不接这个关键字也不受影响（见 :func:`_accepts_keyword`）。
Judger = Callable[..., Judgement]


def uses_model_endpoint(normalizer: Any) -> bool:
    """这个归并器走不走**模型端点**。

    语义归并（任务 21 / 24）要调院内模型服务 —— 那条出口比推理端点更贵更慢，
    在途上限更低，所以**单独一个闸门**。归并器在实例上声明
    ``uses_model_endpoint = True`` 即表示「我这条出口要占模型端点的额度」。

    当前这一轮的规则基线（纯本地、不出网）不声明它，因此**不占额度** ——
    不占额度不是漏限流，是那条出口上本来就没有请求。
    """

    return bool(getattr(normalizer, "uses_model_endpoint", False))


def _default_normalizer(case: CaseInput) -> GoldLabels:
    """缺省归并：**存在性一层**的规则基线。

    优先读读取层产出的分析用副本（``gold_standard_normalized``）——
    原文里写「，」还是「,」不该影响判读；原文本身一个字都不动。
    """

    return normalize_gold_standard(
        case.gold_standard_text,
        normalized_text=case.gold_standard_normalized or None,
    )


def _default_judger(
    gold: GoldLabels,
    candidates: Sequence[Any],
    *,
    size_tolerance_mm: float = DEFAULT_SIZE_TOLERANCE_MM,
) -> Judgement:
    """缺省判定：存在性 + 属性三层的比对（容差由引擎按本轮选项传下来）。"""

    return judge_case(gold, candidates, size_tolerance_mm=size_tolerance_mm)


def _accepts_keyword(func: Any, name: str) -> bool:
    """这个可调用对象接不接某个关键字参数。

    判定函数是**开口子**（``judger=``）：外部可以塞自己的实现进来。
    自定义实现不一定知道 ``size_tolerance_mm`` —— 硬塞给它会当场 TypeError，
    所以这里先问一句「你收不收这个参数」，收才传（任务 27）。
    """

    import inspect

    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover —— 签名取不到的极端情况
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is parameter.VAR_KEYWORD:
            return True
        if parameter.name == name and parameter.kind in (
            parameter.KEYWORD_ONLY,
            parameter.POSITIONAL_OR_KEYWORD,
        ):
            return True
    return False


def _sha256_text(text: str) -> str:
    """文本的 sha256（十六进制）。"""

    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 失败收口层（任务 17）
# --------------------------------------------------------------------------
#
# 一次评估几百例，任何一类失败都可能发生：连接被拒、读超时、参数被拒、
# 服务端报错、回传标识对不上、响应缺字段。这些失败若由各层各写一句文案，
# 就会出现「同一类失败在终端、表格、产物里三种说法」—— 复核的人得自己
# 判断说的是不是同一件事。
#
# 本层把一次失败收成**一条记录**（:class:`CaseFailure`），里面只有四样东西：
#
# 1. **错误码** —— 机器口径，可按码统计（``INFERENCE_5XX`` / ``STUDY_UID_MISMATCH`` …）；
# 2. **固定失败原因** —— 一句可交差的中文；**只从** :mod:`nodule_eval.errors`
#    的错误码表取，业务代码不散落硬编码文案（任务 42 据此扫描断言）；
# 3. **原始 HTTP 状态** —— 「厂商回了 503」与「厂商回了 400」处置完全不同；
# 4. **失败类别与出问题的端点** —— 六类失败与「哪个端点出的事」。
#
# **一条红线写死在这一层**：失败例**永不**进指标分母。
# 把「没跑成」算成「模型没看见」，等于把一次漏检悄悄塞进特异度里。

#: 本地归并（不出网）：失败原因里的端点标识。
ENDPOINT_LOCAL = "local"

#: 出站端点的中文标签 —— 失败记录里要能说清「哪个端点出的事」。
ENDPOINT_LABELS: dict[str, str] = {
    ENDPOINT_INFERENCE: "推理服务",
    ENDPOINT_MODEL: "模型服务",
    ENDPOINT_RIS: "院内 RIS",
    ENDPOINT_RESEARCH: "科研平台",
    ENDPOINT_LOCAL: "本地规则归并",
}

#: 失败原因的长度上限（表格一格放得下的长度；超长截断）。
FAILURE_REASON_LIMIT = 500


def one_line(text: str, *, limit: int = FAILURE_REASON_LIMIT) -> str:
    """把一段文字压成**一行**（表格一格一行；超长截断）。

    失败原因要写进评估表的 ``failure_reason`` 列 —— 里面有换行，
    表格里就是一格多行，打印出来会串行；整段厂商返回更是没法看。

    实现委托给 :func:`nodule_eval.errors.flatten` —— **压成一行**这件事
    只有一份实现：表格列、终端输出、``summary.json`` 三处都调它，
    同一错误码三处必然是同一句话（任务 42）。
    """

    return flatten(text, limit=limit)


@dataclass(frozen=True)
class CaseFailure:
    """一条逐例失败记录 —— 失败例的统一口径。

    ``failure_reason``
        **固定文案**，取自错误码表（:func:`nodule_eval.errors.format_message`）。
        同一句文案在终端输出、表格 ``failure_reason`` 列与逐例产物里一字不差。
    ``http_status``
        原始 HTTP 状态码；传输层失败（连不上 / 超时）没有状态码，取 ``None``。
    ``failure_class``
        六类失败之一（连接超时 / 读取超时 / 连接被拒 / 4xx / 5xx /
        标识不回显 / 响应缺字段），由适配器在抛出时带上。
    """

    error_code: str
    failure_reason: str
    http_status: int | None = None
    failure_class: str = ""
    endpoint: str = ""
    retryable: bool = False

    @property
    def counts_toward_metrics(self) -> bool:
        """失败例**永不**进指标分母（红线）。"""

        return False

    @property
    def endpoint_label(self) -> str:
        """端点的中文说法（「推理服务」而不是 ``inference``）。"""

        return ENDPOINT_LABELS.get(self.endpoint, self.endpoint)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "failure_reason": self.failure_reason,
            "http_status": self.http_status,
            "failure_class": self.failure_class,
            "endpoint": self.endpoint,
            "endpoint_label": self.endpoint_label,
            "retryable": self.retryable,
            "counts_toward_metrics": False,
        }


def build_case_failure(exc: NoduleEvalError, *, endpoint: str) -> CaseFailure:
    """把一次失败收成逐例失败记录。

    **失败原因取自错误码表**（``exc.message``）—— 业务代码不自己写失败文案，
    免得同一类失败在终端、表格、产物里出现三种说法。
    """

    return CaseFailure(
        error_code=exc.code.value,
        failure_reason=exc.user_text,
        http_status=exc.http_status,
        failure_class=str(exc.fields.get("failure_class") or ""),
        endpoint=endpoint,
        retryable=exc.retryable,
    )


def _failure_content(failure: CaseFailure) -> str:
    """失败调用的底账内容（``tool_calls.jsonl`` 的 ``content``）。

    落**结构化**的失败记录而不是一句中文 —— 事后按错误码统计失败分布
    要能直接筛；厂商原话仍在 ``failure_reason`` 里，不丢。
    """

    payload = failure.to_dict()
    payload["message"] = failure.failure_reason
    return json.dumps(payload, ensure_ascii=False)


@dataclass
class MetricCounts:
    """按「进不进指标分母」的口径数出来的原始计数。

    ``counted`` 是真正进了指标分母的例数；``failed`` / ``needs_review``
    / ``incomparable`` 是被隔离在外、各单列的例数 —— 三类互不吞并
    （口径与 :func:`nodule_eval.contracts.partition_rows` 一致）。

    这是一个**累加器**（就地累加后交出去），所以不冻结。
    """

    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    failed: int = 0
    needs_review: int = 0
    incomparable: int = 0
    counted: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "failed": self.failed,
            "needs_review": self.needs_review,
            "incomparable": self.incomparable,
            "counted": self.counted,
        }


def count_metric_rows(rows: Sequence[CaseRow]) -> MetricCounts:
    """数原始计数：**只有进了指标分母的例才进 TP / FP / FN / TN**。

    口径真源仍是 :func:`nodule_eval.contracts.counts_toward_denominator` ——
    失败例（``failed``）、挂起例（``needs_review``）与**不可比例**
    （``incomparable``，任务 28）一律不进分母，各自单列计数。

    实现落在 :func:`nodule_eval.metrics.compute.count_raw_counts`
    （任务 29 把它和指标计算放到同一处，好让人手算复核时
    引擎数出来的数与复核脚本数出来的数**必然一致**）；
    本函数只做一层换壳，给引擎内既有调用方用。
    """

    raw = count_raw_counts(rows)
    return MetricCounts(
        tp=raw.tp,
        fp=raw.fp,
        fn=raw.fn,
        tn=raw.tn,
        failed=raw.failed,
        needs_review=raw.needs_review,
        incomparable=raw.incomparable,
        counted=raw.counted,
    )


def assert_failure_isolated(rows: Sequence[CaseRow]) -> None:
    """红线自检：失败例与挂起例一律不进指标分母。

    这条断言是**给未来的改动设的绊子** —— 哪天有人为了「让样本数好看一点」
    把 ``failed`` 也算进分母（敏感度立刻被拉低），这里当场炸掉。
    """

    for row in rows:
        if row.verdict == VERDICT_FAILED and counts_toward_denominator(row):
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=(
                    f"检查号 {row.accession_no} 判失败却进了指标分母 —— "
                    f"失败例必须隔离在分母之外"
                ),
            )


@dataclass(frozen=True)
class RunArtifacts:
    """一次 run 落下的产物路径。

    ``model_calls_jsonl``
        **模型调用底账**（任务 33）。只在**真的调过模型**时才有文件 ——
        走规则基线（``--merge rule``）时压根没有模型调用，
        这里给 ``None`` 而不是给一个不存在的路径（免得人以为「有但丢了」）。
    """

    run_dir: Path
    case_rows_csv: Path
    tool_calls_jsonl: Path
    result_json: Path
    case_timings_json: Path
    missed_list_csv: Path | None = None
    summary_json: Path | None = None
    model_calls_jsonl: Path | None = None


@dataclass(frozen=True)
class CaseTiming:
    """一例的耗时台账（进压测报告）。

    ``elapsed_ms`` 是这一例从开跑到判定的总耗时；``queued_ms`` 是其中
    **排队等限流额度**的时间 —— 两者分开，才能说清「这轮慢在模型算得慢」
    还是「这轮慢在排队」。

    ``skipped`` 标出「这一例本版没跑」—— 断点续跑时上一版已跑完的例
    直接跳过，不占端点额度，耗时台账里也不该把它算成「这一版跑了 0 毫秒」。
    """

    accession_no: str
    elapsed_ms: float
    queued_ms: float
    verdict: str
    error_code: str = ""
    needs_review: bool = False
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession_no": self.accession_no,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "queued_ms": round(self.queued_ms, 3),
            "verdict": self.verdict,
            "error_code": self.error_code,
            "needs_review": self.needs_review,
            "skipped": self.skipped,
        }


class RunProgress:
    """运行进度 —— 写状态文件，给表格宏轮询（任务 46）。

    **为什么引擎要写这个文件**：评估表是本产品的**界面本体**，
    两百例要跑好几分钟。这几分钟里界面必须说话 ——
    宏每隔半秒读一次 ``runs/<run_id>/engine_status.json``，
    把「已处理 N/总数 · 当前例号 · 心跳」刷到状态栏。

    **源产品对照**：参考实现只有控制台上的
    ``print(f"Progress: {processed}/{total_examples}")``
    （``github_ref/MedRAX/quickstart.py:274``），界面侧（Gradio）**没有任何加载态** ——
    这一层是新建的。

    **写不动状态文件不该毁掉这一轮评估**：界面看不到进度，
    但结论照样算得出来。写失败会记在 :attr:`write_errors` 里并在终端提示一次，
    **不静默吞掉**。
    """

    def __init__(self, run_dir: Path | str, *, run_id: str, total: int) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.total = int(total)
        self._lock = threading.Lock()
        self._processed = 0
        self._current = ""
        self._failed = 0
        self._state = STATE_RUNNING
        #: 状态文件写不动的次数（不为 0 就说明界面看不到进度，终端会提示）。
        self.write_errors = 0

    # ---- 状态迁移 ----
    def start(self) -> None:
        """开跑（写第一份状态）。"""

        self._flush()

    def advance(self, accession_no: str, *, failed: bool = False) -> None:
        """一例跑完，进度 +1。"""

        with self._lock:
            self._processed += 1
            self._current = accession_no
            if failed:
                self._failed += 1
        self._flush()

    def finish(
        self,
        *,
        state: str,
        error_code: str = "",
        error_detail: str = "",
        failed_cases: int | None = None,
    ) -> None:
        """收尾（写终态）。"""

        with self._lock:
            self._state = state
            if failed_cases is not None:
                self._failed = int(failed_cases)
        self._flush(error_code=error_code, error_detail=error_detail)

    # ---- 落盘 ----
    def _flush(self, *, error_code: str = "", error_detail: str = "") -> None:
        try:
            # **写也在锁里**：并发跑时几个线程会同时刷进度，
            # 出了锁就可能出现「旧状态后写、覆盖掉新状态」——
            # 状态栏上的进度会往回跳。锁内写完，落到盘上的永远是最新的那份。
            with self._lock:
                status = RunStatus(
                    run_id=self.run_id,
                    state=self._state,
                    processed=self._processed,
                    total=self.total,
                    current_accession_no=self._current,
                    heartbeat_at=now_stamp(),
                    updated_at=now_stamp(),
                    error_code=error_code,
                    error_detail=error_detail,
                    failed_cases=self._failed,
                )
                write_status(self.run_dir, status)
        except OSError as exc:
            self.write_errors += 1
            if self.write_errors == 1:
                print(
                    f"[WARN] 运行状态文件写不进去（{exc}）—— 表格界面看不到进度，"
                    f"但这一轮评估照常跑：{status_path(self.run_dir)}",
                    flush=True,
                )

    def to_dict(self) -> dict[str, Any]:
        """当前进度的快照（测试与终端汇报用）。"""

        with self._lock:
            return {
                "run_id": self.run_id,
                "state": self._state,
                "processed": self._processed,
                "total": self.total,
                "current_accession_no": self._current,
                "failed_cases": self._failed,
            }


def _dominant_failure_code(rows: Sequence[CaseRow]) -> str:
    """这一轮**出现最多的失败错误码**（状态栏给处置提示时用）。

    没有失败例就返回空串 —— 状态栏不必提「失败」这两个字。
    同一错误码的失败原因在表格、终端、``summary.json`` 三处是同一句，
    这里只取**码**（状态栏要说的是「下一步怎么办」）。
    """

    counts: dict[str, int] = {}
    for row in rows:
        code = str(getattr(row, "error_code", "") or "")
        if not code:
            continue
        counts[code] = counts.get(code, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


class Engine:
    """评估引擎：逐例 fan-out → 落底账 → 落逐例行 → 落逐例产物。

    参数
    ----
    config
        环境变量读出来的配置（凭据只在这里面）。
    spec
        被评模型的目录声明（端点路径、版本、响应字段映射）。缺省时按标准端点与字段名走。
    fixture
        是否走替身门。**默认 False** —— 此时出站目标只来自环境变量里的真实端点。
    normalizer
        金标准归并函数。缺省走 :func:`_default_normalizer`（存在性一层的规则基线）；
        任务 21 的语义归并从这一个口子接进来，业务代码不改。
    judger
        判定函数。缺省走 :func:`_default_judger`；任务 26 / 27 的判定引擎
        从这一个口子接进来。
    """

    def __init__(
        self,
        config: Config,
        spec: ModelSpec | None = None,
        *,
        client: HttpClient | None = None,
        runs_dir: Path | str | None = None,
        store: RunStore | None = None,
        fixture: bool = False,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        normalizer: Normalizer | None = None,
        judger: Judger | None = None,
    ) -> None:
        self.config = config
        self.spec = spec
        self.fixture = fixture
        #: run store（可选）。给了就把 run / case 状态落进 SQLite ——
        #: 跨进程可读、可断点续跑、可按 run_id 复算（任务 12）。
        self.store = store
        if store is not None and runs_dir is not None:
            if Path(runs_dir) != Path(store.runs_dir):
                # 两处产物目录不一致 = 明细在一个目录、底账在另一个目录，
                # 事后按 id 反查会找不到 —— 这种事不能靠人记得对齐。
                raise NoduleEvalError(
                    ErrorCode.CONTRACT_INVALID,
                    detail=(
                        f"运行库的产物目录与引擎的产物目录不一致："
                        f"{store.runs_dir} 与 {runs_dir}"
                    ),
                )
        if runs_dir is not None:
            self.runs_dir = Path(runs_dir)
        elif store is not None:
            self.runs_dir = Path(store.runs_dir)
        else:
            self.runs_dir = Path(DEFAULT_RUNS_DIR)
        if client is None:
            client = HttpClient(
                **(
                    {"connect_timeout": connect_timeout}
                    if connect_timeout is not None
                    else {}
                ),
                **(
                    {"read_timeout": read_timeout}
                    if read_timeout is not None
                    else {}
                ),
            )
        self.client = client
        self.last_artifacts: RunArtifacts | None = None
        #: 金标准归并与判定的**两个口子**。业务代码只认这两个函数，
        #: 不直接 import 具体的归并实现 —— 换实现不改主路径。
        self.normalizer: Normalizer = normalizer or _default_normalizer
        self.judger: Judger = judger or _default_judger
        #: 自定义判定函数接不接 ``size_tolerance_mm``（任务 27）——
        #: 接才把本轮的尺寸容差传下去，不接就按它的原样调用。
        self._judger_accepts_tolerance = _accepts_keyword(
            self.judger, "size_tolerance_mm"
        )
        #: **调用底账的写锁**：并发 fan-out 下多线程会同时追加 JSONL，
        #: 同一行被写花比少一行更难查。
        self._record_lock = threading.Lock()
        #: **模型调用底账的记录器**（任务 33）：按 run_id 缓存一个 ——
        #: 同一轮的多线程共用一个记录器（内部有文件锁，写入互斥）。
        self._model_call_recorders: dict[str, ModelCallRecorder] = {}
        self._model_call_lock = threading.Lock()
        #: 本轮逐例耗时台账（按输入顺序）。
        self.case_timings: list[CaseTiming] = []
        #: 本轮运行进度（写状态文件给表格宏轮询）—— 跑起来之后才有。
        self.progress: RunProgress | None = None
        #: 本轮用的限流闸门（每端点一个独立上限）。
        self.limiters: LimiterSet | None = None
        #: 本轮的工作线程数（1 = 串行）。
        self.concurrency = 1
        # 出站基址统一过替身门：默认只认环境变量里的真实端点；替身开启时全部改走替身。
        routed = routed_base_urls(config, fixture=fixture)
        self.adapter = InferenceAdapter(
            config, spec, client=self.client, base_url=routed["inference"]
        )
        self.model_id = spec.model_id if spec is not None else ""
        self.model_version = spec.model_version if spec is not None else ""

    # ---- 主路径 ----
    def run(self, request: EvalRequest) -> EvalResponse:
        """跑完一次评估：逐例 fan-out（选定模型 → 调用 → 归并 → 判定 → 下一例）。

        **循环终止条件只有两条**：

        1. **病例集跑完** —— 正常结束；
        2. **run 级致命错误** —— 存储库写不动、产物目录写不了这类
           「再跑一遍也是同样结果」的问题（整轮标 ``failed`` 并抛出）。

        **单例失败不终止循环**：一行有问题不该毁掉两百行的工作。
        任务 17 把这条收口成显式的失败隔离层：失败例判 ``failed``、
        落失败记录（错误码 + 固定原因 + 原始 HTTP 状态）、
        **不进指标分母**，整批照跑。

        **状态推进走状态机**（任务 13）：草稿 → 已装配 → 运行中 → 已调用 → 已判定，
        每一步都留迁移记录。引擎**只**能推到 ``evaluated`` ——
        复核与提交是人工确认层，引擎推不进去（store 会拒）。

        **并发（任务 15）**：``options.concurrency`` 个工作线程并行处理，
        推理端点与模型端点各自限流（超限排队，不丢弃）。
        逐例行仍按**输入顺序**返回 —— 表格回填按行号写回，顺序不能乱。

        **断点续跑与版本留存（任务 16）**：

        - ``options.resume`` 为真时，先从 run store 读出上一版**已跑完**的例
          （终态），本版不再调用推理端点；跑到一半的例（非终态）复位回
          「待处理」重走一遍 —— 中断时停在 ``called`` 的例**没有结论**，
          跳过它这一例就永远缺一段；
        - 同一 ``run_id`` 的每一版产物各存一份（``runs/<run_id>/versions/``），
          顶层 ``case_rows.csv`` 始终是当前这一版（表内插件按固定路径回填）；
        - **跑崩了也留版本台账**：状态写 ``interrupted`` —— 半截产物不能
          装作完整产物。
        """

        run_dir = self.runs_dir / request.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        recorder = ToolCallRecorder(run_dir)

        # **运行进度**（任务 46）：写状态文件，表格宏轮询它刷新状态栏。
        progress = RunProgress(run_dir, run_id=request.run_id, total=len(request.cases))
        self.progress = progress
        progress.start()

        resume = bool(request.options.resume)
        # 续跑的准备放在最前面：这一轮在库里不存在就直接报错，
        # 别先把目录建出来再告诉用户「没得续」。
        completed = self._prepare_resume(request) if resume else {}

        version = begin_version(run_dir, "resume" if resume else "run")
        try:
            response, artifacts = self._execute(request, recorder, completed, version)
        except BaseException as exc:
            if isinstance(exc, NoduleEvalError):
                progress.finish(
                    state=STATE_FAILED,
                    error_code=exc.code.value,
                    error_detail=flatten(exc.detail),
                )
            else:
                progress.finish(
                    state=STATE_INTERRUPTED,
                    error_detail=f"{type(exc).__name__}: {exc}",
                )
            finish_version(
                version,
                status=ATTEMPT_STATUS_INTERRUPTED,
                detail=f"{type(exc).__name__}: {exc}",
                summary={"total_cases": len(request.cases)},
            )
            raise

        finish_version(
            version,
            status=ATTEMPT_STATUS_COMPLETED,
            detail="",
            files=[
                artifacts.case_rows_csv,
                artifacts.result_json,
                artifacts.case_timings_json,
            ],
            summary={
                "total_cases": len(request.cases),
                "processed_cases": len(request.cases) - len(completed),
                "skipped_cases": len(completed),
            },
        )
        progress.finish(
            state=STATE_COMPLETED,
            failed_cases=sum(
                1 for row in response.case_rows if row.verdict == VERDICT_FAILED
            ),
            error_code=_dominant_failure_code(response.case_rows),
        )
        self.last_artifacts = artifacts
        return response

    def _execute(
        self,
        request: EvalRequest,
        recorder: ToolCallRecorder,
        completed: Mapping[str, CaseRow],
        version: RunVersion,
    ) -> tuple[EvalResponse, RunArtifacts]:
        """跑一轮的主干。

        ``completed`` 是「本版要跳过的例」（检查号 → 上一版的判定行），
        续跑时来自 run store，正常跑时为空。
        """

        run_dir = self.runs_dir / request.run_id

        self._prepare_run(request)

        concurrency = self._resolve_concurrency(request)
        limiters = self._build_limiters(request, concurrency)
        self.limiters = limiters
        self.concurrency = concurrency
        self._resize_connection_pool(concurrency)

        rows: list[CaseRow] = self._fan_out(
            request, recorder, limiters, concurrency, completed
        )

        # **红线自检**：失败例与挂起例一律不进指标分母。
        # 这条断言放在汇总之前 —— 口径一旦被改坏，这里当场炸，
        # 不会带着一份「看起来正常」的汇总流到表格里。
        assert_failure_isolated(rows)

        # 指标隔离的**唯一口径**（contracts.partition_rows）：
        # 失败数与挂起数互斥单列，挂起的例不进指标分母。
        partition = partition_rows(rows)
        # 原始计数按同一口径数（只有进分母的例进 TP / FP / FN / TN）。
        counts = count_metric_rows(rows)
        # **两处口径不许漂移**：``partition_rows``（contracts，汇总行的真源）
        # 与 ``count_metric_rows``（本层，原始计数的真源）必须数出同一个数 ——
        # 漂移过一次，汇总行就会和逐例行对不上，人算的敏感度跟表里的对不上。
        if (partition.failed, partition.needs_review, partition.incomparable) != (
            counts.failed,
            counts.needs_review,
            counts.incomparable,
        ):
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=(
                    f"指标隔离口径漂移：汇总口径数出失败 {partition.failed} 例 / "
                    f"挂起 {partition.needs_review} 例 / 不可比 "
                    f"{partition.incomparable} 例，计数口径数出失败 "
                    f"{counts.failed} 例 / 挂起 {counts.needs_review} 例 / 不可比 "
                    f"{counts.incomparable} 例"
                ),
            )
        # 降级数：**归并走了降级路径**的例数。
        #
        # 两种来源都在这一列里（口径写在 ``degrade_note`` 里）：
        # 「模型不可达 → 退回规则基线」（任务 24）与「规则基线读不出存在性」。
        # 两者都是同一件事：**这一例的结论不是模型给的**。
        rule_degraded = count_rule_degraded(rows)
        # 用模型归并的例数 —— 与降级数**分列**，不混为一谈：
        # 复核的人一眼能看出「这一轮有多少例的结论出自模型」。
        model_normalized = count_model_normalized(rows)
        degrade_note = ""
        if rule_degraded:
            degrade_note = (
                f"含 {rule_degraded} 例规则降级"
                "（模型归并未成功或金标准存在性读不出，已按规则基线处理，请优先复核）"
            )

        # **不可比**（任务 28）：属性写法认不出来、没法核对的例 ——
        # 既不当命中也不当漏诊，单列一栏（不混进失败数 / 挂起数）。
        incomparable = counts.incomparable
        if incomparable:
            degrade_note = (
                f"{degrade_note}；" if degrade_note else ""
            ) + (
                f"含 {incomparable} 例属性不可比"
                "（部位/密度写法不在等价表里，没法核对，未计入 TP/FP/FN/TN）"
            )

        # **四项指标 + 原始计数**（任务 29）：指标值与原始计数同出一处，
        # 分母为 0 时指标为 ``null`` 并写明原因（绝不输出 0）。
        metrics = compute_metrics(
            RawCounts(
                tp=counts.tp,
                fp=counts.fp,
                fn=counts.fn,
                tn=counts.tn,
                counted=counts.counted,
                failed=counts.failed,
                needs_review=counts.needs_review,
                incomparable=counts.incomparable,
            ),
            rule_degraded=rule_degraded,
            model_normalized=model_normalized,
        )
        # **手算复核自检**：汇总行必须与逐例行逐项对得上。
        # 对不上就当场报错 —— 不给一份看着正常、其实和明细对不上的汇总。
        assert_metrics_consistent(rows, metrics)

        # **漏诊清单（任务 30）**：只收**计入指标分母**的漏诊例 ——
        # 清单条数恒等于汇总行里的 FN，复核的人数一遍清单就能对上敏感度。
        missed = build_missed_list(rows)

        summary = Summary(
            model_id=self.model_id or request.model_id,
            model_version=self.model_version or request.model_version,
            dataset_ref=request.dataset_ref,
            total_cases=len(request.cases),
            evaluated_cases=counts.counted,
            failed_count=counts.failed,
            rule_degraded_count=rule_degraded,
            needs_review_count=counts.needs_review,
            incomparable_count=incomparable,
            generated_at=now_iso(),
            skipped_count=len(completed),
            version_no=version.version_no,
            model_normalized_count=model_normalized,
            degrade_note=degrade_note,
        )

        # **结论一致性自检**：指标 ↔ 逐例明细、单列栏 ↔ 逐例明细、
        # 漏诊清单条数 ↔ FN、样本量 ↔ 逐例行条数。
        # 任何一项对不上就当场报错并点名差在哪一项 ——
        # 结论已经贴进招采说明时才发现对不上，就晚了。
        assert_summary_consistent(rows, metrics, summary, missed)

        response = EvalResponse(
            run_id=request.run_id,
            status=RUN_STATUS_EVALUATED,
            case_rows=tuple(rows),
            metrics=metrics,
            missed_list=missed,
            summary=summary,
        )

        self._persist_cases(request, rows)
        self._advance_run(
            request,
            RUN_STATUS_CALLED,
            actor=ACTOR_ENGINE,
            reason="逐例调用完成",
        )
        self._advance_run(
            request,
            RUN_STATUS_EVALUATED,
            actor=ACTOR_ENGINE,
            reason="逐例判定完成",
        )
        # **降级不推进状态**：有降级例时，run 也只能停在「已判定」。
        self._assert_degradation_did_not_advance_state(request, rows)

        artifacts = self._write_artifacts(run_dir, response)
        return response, artifacts

    def _prepare_resume(self, request: EvalRequest) -> dict[str, CaseRow]:
        """断点续跑的准备：确认这一轮在库里、复位跑到一半的例、取出已跑完的例。

        **返回的是「本版要跳过的例」**（检查号 → 上一版的判定行）。
        跳过的例不再调用推理端点、不再产生新的 ``tool_call`` ——
        重复调一次既浪费厂商额度，又让底账里出现两条一模一样的调用记录，
        复核的人得先猜哪条是真的。
        """

        if self.store is None:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=(
                    "断点续跑需要挂上 run store（--store）："
                    "没有库就没有「上次跑到哪儿」的凭据"
                ),
            )
        existing = self.store.get_run(request.run_id)
        if existing is None:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=(
                    f"运行库里没有 run {request.run_id} 的记录，无法断点续跑"
                    f"（先跑一轮，或去掉 --resume 重头跑）"
                ),
            )

        # 跑到一半的例复位回「待处理」：中断时停在 called / normalized 的例
        # 没有结论 —— 跳过它，这一例就永远缺一段判定。
        self.store.reset_incomplete_cases(
            request.run_id, reason="断点续跑：复位到待处理"
        )

        completed: dict[str, CaseRow] = {}
        for stored in self.store.list_completed_cases(request.run_id):
            if stored.row is None:
                # 有终态却没有判定行快照 —— 当作没跑完，这一例重跑。
                continue
            completed[stored.accession_no] = stored.row
        return completed

    # ---- 并发调度 ----

    def _resolve_concurrency(self, request: EvalRequest) -> int:
        """本轮用几个工作线程。

        取 ``options.concurrency``（契约默认 4）。**1 = 串行** ——
        串行是压测的对照组，也让排障时能一条一条复现。
        """

        concurrency = int(request.options.concurrency)
        if concurrency < 1:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"并发数不得小于 1（实得 {concurrency}）",
            )
        return concurrency

    def _build_limiters(self, request: EvalRequest, concurrency: int) -> LimiterSet:
        """按并发数建限流闸门（逐端点独立上限）。"""

        try:
            return LimiterSet.build(
                concurrency=concurrency,
                endpoint_limits=request.options.endpoint_limits,
            )
        except ValueError as exc:
            raise NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail=str(exc)) from exc

    def _resize_connection_pool(self, concurrency: int) -> None:
        """把出站连接池调大到并发数。

        ``requests`` 的默认连接池只有 10 条连接，并发更高时挤不下的连接
        会被 urllib3 **丢掉重建** —— 一次评估几百上千例，每例重握手一次，
        压测报告里看到的「慢」大半不是推理服务慢，是连接池在漏。
        """

        resize = getattr(self.client, "resize_pool", None)
        if resize is None:  # pragma: no cover - 自定义客户端可不含该方法
            return
        resize(concurrency)

    @contextmanager
    def _merge_slot(self, limiters: LimiterSet) -> Iterator[Slot]:
        """金标准归并这一步占的**模型端点**额度。

        语义归并（任务 21 / 24）走院内模型服务 —— 那条出口更贵更慢，
        在途上限比推理端点低，所以单独一个闸门。归并器没声明走模型端点时
        （当前的规则基线，纯本地不出网）不占额度：那条出口上本来就没有请求。
        """

        if not uses_model_endpoint(self.normalizer):
            yield Slot(endpoint=ENDPOINT_MODEL, waited_seconds=0.0)
            return
        with limiters.limiter(ENDPOINT_MODEL).slot() as slot:
            yield slot

    def _fan_out(
        self,
        request: EvalRequest,
        recorder: ToolCallRecorder,
        limiters: LimiterSet,
        concurrency: int,
        completed: Mapping[str, CaseRow] | None = None,
    ) -> list[CaseRow]:
        """逐例 fan-out：串行或并发。

        **两种跑法给出同一份结果** —— 逐例判定逐例一致（任务 15 的验收项）。
        差别只在快慢，不在结论。

        ``completed`` 是断点续跑时要**跳过**的例（检查号 → 上一版的判定行）：
        跳过的例不占端点额度、不产生新的调用底账，判定行沿用上一版。
        """

        self.case_timings = []
        skip = completed or {}
        if concurrency <= 1:
            return self._fan_out_serial(request, recorder, limiters, skip)
        return self._fan_out_parallel(request, recorder, limiters, concurrency, skip)

    @staticmethod
    def _skipped_timing(row: CaseRow) -> CaseTiming:
        """跳过例的耗时台账：没跑就没有耗时。"""

        return CaseTiming(
            accession_no=row.accession_no,
            elapsed_ms=0.0,
            queued_ms=0.0,
            verdict=row.verdict,
            error_code=row.error_code,
            needs_review=row.needs_review,
            skipped=True,
        )

    def _fan_out_serial(
        self,
        request: EvalRequest,
        recorder: ToolCallRecorder,
        limiters: LimiterSet,
        skip: Mapping[str, CaseRow] | None = None,
    ) -> list[CaseRow]:
        """串行：一例跑完再跑下一例。"""

        skipped = skip or {}
        rows: list[CaseRow] = []
        for case in request.cases:
            stored = skipped.get(case.accession_no)
            if stored is not None:
                rows.append(stored)
                self.case_timings.append(self._skipped_timing(stored))
                self._advance_progress(stored)
                continue
            row, timing = self._process_case_guarded(case, recorder, request, limiters)
            rows.append(row)
            self.case_timings.append(timing)
            self._advance_progress(row)
        return rows

    def _fan_out_parallel(
        self,
        request: EvalRequest,
        recorder: ToolCallRecorder,
        limiters: LimiterSet,
        concurrency: int,
        skip: Mapping[str, CaseRow] | None = None,
    ) -> list[CaseRow]:
        """并发：``concurrency`` 个工作线程各处理一批病例。

        **逐例行按输入顺序返回**：完成顺序是乱的，但表格回填按行号写回，
        顺序乱了就会写错行 —— 这是并发最容易踩的坑。

        **run 级致命错误**（库写不动这类）仍按串行版的口径处理：整轮标
        ``failed`` 并抛出，不留半截「跑完了」的产物。
        """

        cases = request.cases
        skipped = skip or {}
        slots: list[CaseRow | None] = [None] * len(cases)
        timings: list[CaseTiming | None] = [None] * len(cases)

        def worker(index: int, case: CaseInput) -> None:
            stored = skipped.get(case.accession_no)
            if stored is not None:
                slots[index] = stored
                timings[index] = self._skipped_timing(stored)
                self._advance_progress(stored)
                return
            row, timing = self._process_case_guarded(case, recorder, request, limiters)
            slots[index] = row
            timings[index] = timing
            self._advance_progress(row)

        pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="nodule-eval")
        try:
            futures = [
                pool.submit(worker, index, case) for index, case in enumerate(cases)
            ]
            for future in futures:
                future.result()
        except BaseException as exc:
            # run 级致命错误：取消还没开始的，等在跑的收尾，整轮标 failed。
            pool.shutdown(wait=True, cancel_futures=True)
            self._fail_run(request, detail=str(exc))
            raise
        else:
            pool.shutdown(wait=True)

        rows: list[CaseRow] = []
        for index, row in enumerate(slots):
            if row is None:  # pragma: no cover - 提交过就不会缺
                raise NoduleEvalError(
                    ErrorCode.CONTRACT_INVALID,
                    detail=f"检查号 {cases[index].accession_no} 这一例没有跑出结果",
                )
            rows.append(row)
        self.case_timings = [timing for timing in timings if timing is not None]
        return rows

    def _advance_progress(self, row: CaseRow) -> None:
        """一例跑完 → 推进运行进度（写状态文件）。

        **进度写不动不该中断这一轮** —— 界面看不到进度是体验问题，
        结论算不出来是事故，两件事不能混。
        """

        progress = self.progress
        if progress is None:
            return
        progress.advance(
            row.accession_no,
            failed=row.verdict == VERDICT_FAILED,
        )

    def _process_case_guarded(
        self,
        case: CaseInput,
        recorder: ToolCallRecorder,
        request: EvalRequest,
        limiters: LimiterSet,
    ) -> tuple[CaseRow, CaseTiming]:
        """跑一例并兜住单例层面的失败，返回（逐例行, 耗时台账）。

        单例层面的契约问题（归并结果不合契约等）记一行失败、整批继续；
        **run 级致命错误**原样抛出 —— 那一轮的问题不能算成这一例的问题。
        """

        started = time.perf_counter()
        queued_seconds = 0.0
        try:
            row, queued_seconds = self._process_case(case, recorder, request, limiters)
        except NoduleEvalError as exc:
            # **单例层面的失败**：收成失败行，**整批继续**，绝不中断。
            # 一行有问题不该毁掉两百行的工作 —— 这是「部分失败不阻断整批」
            # 这条纪律的落点。
            failure = build_case_failure(exc, endpoint=ENDPOINT_LOCAL)
            row = self._failure_row(case, failure)
            # 失败例的逐例状态也要落到位（failed），并留迁移痕 ——
            # 事后看状态就知道这一例是「跑过了但失败」，不是「没跑过」。
            self._settle_case(
                request,
                row.accession_no,
                case_status_for_row(row),
                reason=f"单例处理失败：{failure.error_code}",
            )
            # 失败行也要**立刻**落库（断点续跑的检查点）：
            # 这一例跑过了、结论是「失败」—— 不落库，续跑时会把它当成
            # 「没跑过」再调一次厂商端点，白花一次额度。
            self._persist_case_row(request, row)
            # 逐例产物照样归档：失败原因、错误码与状态码都要能被反查。
            self._archive_case(
                request,
                case,
                row,
                gold=None,
                judgement=None,
                args={},
                call_name="",
                failure=failure,
            )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        timing = CaseTiming(
            accession_no=row.accession_no,
            elapsed_ms=elapsed_ms,
            queued_ms=queued_seconds * 1000.0,
            verdict=row.verdict,
            error_code=row.error_code,
            needs_review=row.needs_review,
        )
        return row, timing

    # ---- run 状态推进 ----
    def _ensure_run(self, request: EvalRequest) -> str:
        """确保 run 行存在，返回当前状态。

        新建的 run 落在 ``draft``（草稿）—— 还没开始跑。
        """

        if self.store is None:
            return ""
        existing = self.store.get_run(request.run_id)
        if existing is not None:
            return existing.status
        self.store.upsert_run(
            run_id=request.run_id,
            dataset_ref=request.dataset_ref,
            model_id=self.model_id or request.model_id,
            model_version=self.model_version or request.model_version,
        )
        stored = self.store.get_run(request.run_id)
        return stored.status if stored is not None else RUN_STATUS_DRAFT

    def _advance_run(
        self,
        request: EvalRequest,
        to_state: str,
        *,
        actor: str,
        reason: str,
    ) -> None:
        """把 run 推进到目标状态。

        非法迁移**不在这里兜底** —— 由状态机抛错（引擎不该有能力绕开状态机）。
        """

        if self.store is None:
            return
        self._assert_engine_may_advance(to_state)
        current = self._ensure_run(request)
        if current == to_state:
            return
        self.store.transition_run(request.run_id, to_state, actor=actor, reason=reason)

    @staticmethod
    def _assert_engine_may_advance(to_state: str) -> None:
        """引擎能推进到的 run 状态**上限**（任务 24）。

        「已复核」与「已提交」是**人工确认层**的状态：引擎不许推过去 ——
        **降级也不构成例外**。模型不可达时结论退到了规则基线，
        这只影响「结论有多可信」，不影响「有没有人看过」；
        拿一批降级例去写回科研平台，等于让机器替人签字。
        """

        if to_state in (RUN_STATUS_REVIEWED, RUN_STATUS_SUBMITTED):
            raise NoduleEvalError(
                ErrorCode.INVALID_STATE_TRANSITION,
                detail=(
                    f"引擎不能把 run 推进到 {to_state} —— "
                    "复核与提交只能由人工确认触发"
                ),
            )

    def _assert_degradation_did_not_advance_state(
        self,
        request: EvalRequest,
        rows: Sequence[CaseRow],
    ) -> None:
        """**降级不推进状态**（任务 24 的红线）。

        这一轮有降级例时，run 的状态**必须**停在「已判定」——
        与没有降级例时一模一样。降级只改结论口径，不改流程进度：
        引擎最高只到 ``evaluated``，往后（已复核 / 已提交）是人工确认层。
        """

        if self.store is None:
            return
        if not any(row.rule_degraded for row in rows):
            return
        stored = self.store.get_run(request.run_id)
        if stored is None:
            return
        if stored.status != RUN_STATUS_EVALUATED:
            raise NoduleEvalError(
                ErrorCode.INVALID_STATE_TRANSITION,
                detail=(
                    f"run {request.run_id} 有降级例，状态却不在 {RUN_STATUS_EVALUATED}"
                    f"（实得 {stored.status}）—— 降级不许推进状态"
                ),
            )

    def _fail_run(self, request: EvalRequest, *, detail: str) -> None:
        """整轮失败：run 标 ``failed`` 并留原因。

        **半截产物比没有产物更危险** —— 这一轮的状态必须说清楚是「跑完了」
        还是「跑崩了」。
        """

        if self.store is None:
            return
        try:
            self._advance_run(
                request,
                RUN_STATUS_FAILED,
                actor=ACTOR_ENGINE,
                reason=f"整轮失败：{detail}",
            )
        except NoduleEvalError:
            # 状态机都推不动了（库坏了）—— 原样抛出，别在这里吞。
            raise

    def _prepare_run(self, request: EvalRequest) -> None:
        """把 run 推进到「运行中」。

        - 草稿 → 已装配 → 运行中：第一次跑；
        - 已调用 / 已判定 / 整轮失败 → 运行中：**同一轮重跑**（复位重跑）；
        - 已复核 / 已提交：**拒绝重跑** —— 人工确认过的东西不该被机器推翻，
          要重评请换一个新的 ``run_id``。
        """

        status = self._ensure_run(request)
        if status in (RUN_STATUS_REVIEWED, RUN_STATUS_SUBMITTED):
            raise NoduleEvalError(
                ErrorCode.INVALID_STATE_TRANSITION,
                detail=(
                    f"run {request.run_id} 已处于 {status} 状态，不能直接重跑；"
                    f"要重评请换一个 run_id"
                ),
            )
        if status == RUN_STATUS_DRAFT:
            self._advance_run(
                request,
                RUN_STATUS_ASSEMBLED,
                actor=ACTOR_ASSEMBLE,
                reason="病例集已就绪",
            )
            status = RUN_STATUS_ASSEMBLED
        if status in (
            RUN_STATUS_ASSEMBLED,
            RUN_STATUS_CALLED,
            RUN_STATUS_EVALUATED,
            RUN_STATUS_FAILED,
        ):
            self._advance_run(
                request,
                RUN_STATUS_RUNNING,
                actor=ACTOR_ENGINE,
                reason="开始逐例评估",
            )

    # ---- 落库 ----
    def _persist_cases(self, request: EvalRequest, rows: Sequence[CaseRow]) -> None:
        """把逐例行写进 run store（主键覆盖，不追加重复行）。

        **状态不在这儿改**：状态由状态机推进并记账，这一步只补内容
        （判定 / 调用 id / 错误码 / 行快照）。状态字段沿用库里当前的值 ——
        两处各写一份状态，迟早写出两种状态。
        """

        if self.store is None:
            return
        for row in rows:
            existing = self.store.get_case(request.run_id, row.accession_no)
            status = (
                existing.status if existing is not None else case_status_for_row(row)
            )
            self.store.upsert_case(
                run_id=request.run_id,
                accession_no=row.accession_no,
                status=status,
                verdict=row.verdict,
                tool_call_id=row.tool_call_id,
                error_code=row.error_code,
                row_no=row.row_no,
                row=row,
            )

    def _persist_case_row(self, request: EvalRequest, row: CaseRow) -> None:
        """把这一例的判定行**立刻**落进 run store（断点续跑的检查点）。

        **为什么不是等整轮跑完再落**：跑两百例时中途断电、关窗、厂商端点挂掉，
        落完的行是这轮**唯一**的进度凭据 —— 库里有行且是终态就说明这一例
        跑完了，续跑时跳过它；库里没有就说明它没跑完，重跑。

        **状态不在这儿改**：沿用库里当前的状态（刚由状态机推进过），
        两处各写一份状态迟早写出两种状态。
        """

        if self.store is None:
            return
        existing = self.store.get_case(request.run_id, row.accession_no)
        status = (
            existing.status if existing is not None else case_status_for_row(row)
        )
        self.store.upsert_case(
            run_id=request.run_id,
            accession_no=row.accession_no,
            status=status,
            verdict=row.verdict,
            tool_call_id=row.tool_call_id,
            error_code=row.error_code,
            row_no=row.row_no,
            row=row,
        )

    def _checkpoint_call_id(
        self,
        request: EvalRequest,
        accession_no: str,
        tool_call_id: str,
    ) -> None:
        """把这一例的**调用 id** 记进库（断点续跑的检查点，中间态）。

        推理调用已经发出、底账已记，但判定还没出 —— 这一例**跑过但没跑完**。
        把调用 id 记下来，事后能说清「这一例中断前调过哪一次」；
        不记的话，库里只留一个空的调用 id，事后无从查起。

        **判定行不在这里写**：判定行是「这一例的结论」，
        没结论的时候不写 —— 库里的行一律是结论。
        """

        if self.store is None:
            return
        existing = self.store.get_case(request.run_id, accession_no)
        if existing is None:
            return
        self.store.upsert_case(
            run_id=request.run_id,
            accession_no=accession_no,
            status=existing.status,
            verdict=existing.verdict,
            tool_call_id=tool_call_id,
            error_code=existing.error_code,
            row_no=existing.row_no,
            row=existing.row,
        )

    def _ensure_case_row(self, request: EvalRequest, case: CaseInput) -> None:
        """第一次见到这一例时落一行「待处理」。

        之后每一次状态变化都走状态机并留痕 —— 不在这儿偷偷改状态。
        """

        if self.store is None:
            return
        if self.store.get_case(request.run_id, case.accession_no) is not None:
            return
        self.store.upsert_case(
            run_id=request.run_id,
            accession_no=case.accession_no,
            status=CASE_STATUS_PENDING,
            row_no=case.row_no,
        )

    def _settle_case(
        self,
        request: EvalRequest,
        accession_no: str,
        target: str,
        *,
        reason: str,
    ) -> None:
        """把这一例的状态推进到 ``target``，**沿合法边走**。

        状态机的边是写死的（``CASE_TRANSITIONS``），
        ``pending → called → normalized → judged`` 每一步都要走 ——
        没调推理端点就出判定，等于凭空造结论。

        重跑场景（这一例上次已经跑过）先复位到「待处理」再重新走一遍，
        每一步都记账 —— 事后能看出「这一例重跑过」。
        """

        if self.store is None:
            return
        current = self.store.get_case(request.run_id, accession_no)
        if current is None:
            return
        status = current.status
        if status == target:
            return

        path = case_status_path(status, target)
        if not path:
            raise NoduleEvalError(
                ErrorCode.INVALID_STATE_TRANSITION,
                detail=(
                    f"检查号 {accession_no} 的状态无法从 {status} 走到 {target}"
                    f"（{status} 的合法去向：{'/'.join(CASE_TRANSITIONS.get(status, ())) or '无'}）"
                ),
            )
        for step in path:
            self.store.transition_case(
                request.run_id,
                accession_no,
                step,
                actor=ACTOR_ENGINE,
                reason=reason,
            )

    # ---- 单例 ----
    def _judge(
        self,
        gold: GoldLabels,
        candidates: Sequence[Any],
        tolerance_mm: float,
    ) -> Judgement:
        """判定一例：把本轮的尺寸容差带给判定引擎（任务 27）。

        容差是**本轮的可调参数**（``options.size_tolerance_mm``），
        不是写死的常量 —— 同一款模型在不同院区的测量口径不一样，
        评委会按本院的口径给容差。

        自定义判定函数（``judger=`` 那个口子）不一定收这个参数：
        先问一句再传，别硬塞给人家（见 :func:`_accepts_keyword`）。
        """

        if self._judger_accepts_tolerance:
            return self.judger(gold, candidates, size_tolerance_mm=tolerance_mm)
        return self.judger(gold, candidates)

    def _process_case(
        self,
        case: CaseInput,
        recorder: ToolCallRecorder,
        request: EvalRequest,
        limiters: LimiterSet | None = None,
    ) -> tuple[CaseRow, float]:
        """处理一例：调用 → 取回 → 归并金标准 → 判定 → 落行、底账与逐例产物。

        这一段就是 fan-out 循环的**循环体**：每一例走完全程，下一例才开始。

        返回 ``(逐例行, 排队秒数)`` —— 排队秒数是这一例为了等端点额度
        额外花掉的时间，压测报告据此把「慢」拆成「等」与「算」。
        """

        if limiters is None:
            limiters = LimiterSet.build(concurrency=1)
        queued_seconds = 0.0

        self._ensure_case_row(request, case)

        study_uid = case.study_uid
        if not study_uid:
            # 绑定层没能给出**可信的影像标识**（一对多歧义 / RIS 里找不到 /
            # 检查号在表里重复）—— 这一例**挂起**，不发起推理调用。
            #
            # 为什么不能照常调用：拿一条存疑的影像去问模型，「模型判漏了」
            # 会被记到一条根本不是本次检查的影像上 —— 直接把敏感度算歪。
            # 为什么不让整批崩掉：一行有问题不该毁掉两百行的工作。
            message = case.hold_reason or error_text(
                ErrorCode.CONTRACT_INVALID,
                detail=(
                    f"检查号 {case.accession_no} 未绑定到唯一影像，"
                    f"生产路径须先经 RIS 按检查号绑定影像标识"
                ),
            )
            row = CaseRow(
                accession_no=case.accession_no,
                verdict=VERDICT_FAILED,
                model_findings="",
                failure_reason=message,
                tool_call_id="",
                needs_review=True,
                normalize_source="",
                row_no=case.row_no,
                held=True,
            )
            self._settle_case(
                request,
                row.accession_no,
                case_status_for_row(row),
                reason=message,
            )
            self._persist_case_row(request, row)
            self._archive_case(request, case, row, gold=None, judgement=None, args={}, call_name="")
            return row, queued_seconds

        model_id = self.model_id or request.model_id
        model_version = self.model_version or request.model_version
        # 入参与动作名的形状**只在 audit/toolcall 里定一次**（任务 31）：
        # 别处再各拼各的，同一款模型会在不同路径下落出两种底账形状。
        args = build_call_args(
            accession_no=case.accession_no,
            study_uid=study_uid,
            image_ref=case.image_ref,
            model_id=model_id,
            model_version=model_version,
        )
        call_name = build_call_name(model_id, "detect")

        try:
            # **推理端点单独限流**：这一段的并发上限由 ``endpoint_limits``
            # 里的 ``inference`` 定，与模型端点各算各的。
            with limiters.limiter(ENDPOINT_INFERENCE).slot() as inference_slot:
                queued_seconds += inference_slot.waited_seconds
                result = self.adapter.detect(
                    study_uid=study_uid,
                    image_ref=case.image_ref,
                    model_id=model_id,
                    model_version=model_version,
                )
        except NoduleEvalError as exc:
            # **失败收口**：一次失败收成一条统一口径的失败记录 ——
            # 错误码 + 固定失败原因 + 原始 HTTP 状态 + 失败类别 + 端点。
            failure = build_case_failure(exc, endpoint=ENDPOINT_INFERENCE)
            # 失败也要留底账：这一例「凭什么算失败」同样要能反查。
            tool_call_id = self._record_call(
                recorder,
                request.run_id,
                name=call_name,
                args=args,
                content=_failure_content(failure),
            )
            # 调用失败归「失败数」一列；绑定层已挂起的例仍算「挂起」——
            # 两列互斥，挂起优先（这一例本来就还没判完）。
            row = CaseRow(
                accession_no=case.accession_no,
                verdict=VERDICT_FAILED,
                model_findings="",
                failure_reason=failure.failure_reason,
                tool_call_id=tool_call_id,
                needs_review=case.needs_review,
                normalize_source="",
                row_no=case.row_no,
                error_code=failure.error_code,
                held=bool(case.needs_review),
            )
            self._settle_case(
                request,
                row.accession_no,
                case_status_for_row(row),
                reason=f"调用失败：{failure.error_code}",
            )
            self._persist_case_row(request, row)
            self._archive_case(
                request,
                case,
                row,
                gold=None,
                judgement=None,
                args=args,
                call_name=call_name,
                failure=failure,
            )
            return row, queued_seconds

        record_tool_call_id = self._record_call(
            recorder,
            request.run_id,
            name=call_name,
            args=args,
            content=json.dumps(result.raw, ensure_ascii=False),
        )
        # 调用成功 → 这一例进入「已调用」。
        self._settle_case(
            request,
            case.accession_no,
            CASE_STATUS_CALLED,
            reason="推理端点已返回",
        )
        # 调用 id 先落库：这一刻这一例**跑过但没跑完**，
        # 中断的话事后能查清它调过哪一次。
        self._checkpoint_call_id(request, case.accession_no, record_tool_call_id)

        # ---- 归并金标准 ----
        # 归并时把**这一例的调用记录 id** 带上：语义归并把它透传给模型端点，
        # 于是模型调用底账（``model_calls.jsonl``）里那一行的 ``tool_call_id``
        # 能在工具调用底账（``tool_calls.jsonl``）里命中 ——
        # 「结论 → 工具调用 → 模型调用」三级链路一次走通（任务 33）。
        merge_case = replace(case, tool_call_ref=record_tool_call_id)
        try:
            # 语义归并走**模型端点**时，这一步占模型端点的额度 ——
            # 与推理端点各算各的（任务 21 / 24 接进来就自动生效）。
            with self._merge_slot(limiters) as merge_slot:
                queued_seconds += merge_slot.waited_seconds
                gold = self.normalizer(merge_case)
        except NoduleEvalError as exc:
            # **失败的模型调用也要记账**（任务 33）：归并失败会被降级成规则基线，
            # 「这一轮为什么降级」得能从账上查出来。
            self._record_model_call(
                request, case=case, tool_call_id=record_tool_call_id
            )
            # 归并失败：推理调用**已经成功**，所以这一例有调用底账可反查。
            # 端点看归并器走哪条出口 —— 走模型端点的记模型服务，
            # 走本地规则基线的记本地归并。
            failure = build_case_failure(
                exc,
                endpoint=(
                    ENDPOINT_MODEL if uses_model_endpoint(self.normalizer) else ENDPOINT_LOCAL
                ),
            )
            row = CaseRow(
                accession_no=case.accession_no,
                verdict=VERDICT_FAILED,
                model_findings=result.summary(),
                failure_reason=failure.failure_reason,
                tool_call_id=record_tool_call_id,
                needs_review=case.needs_review,
                normalize_source="",
                row_no=case.row_no,
                error_code=failure.error_code,
                held=bool(case.needs_review),
            )
            self._settle_case(
                request,
                row.accession_no,
                case_status_for_row(row),
                reason=f"金标准归并失败：{failure.error_code}",
            )
            self._persist_case_row(request, row)
            self._archive_case(
                request,
                case,
                row,
                gold=None,
                judgement=None,
                args=args,
                call_name=call_name,
                failure=failure,
            )
            return row, queued_seconds

        # 归并走通了：把这次模型调用的元数据记进模型调用底账（任务 33）。
        self._record_model_call(request, case=case, tool_call_id=record_tool_call_id)

        self._settle_case(
            request,
            case.accession_no,
            CASE_STATUS_NORMALIZED,
            reason="金标准已归并",
        )

        # ---- 判定 ----
        judgement = self._judge(gold, result.candidates, request.options.size_tolerance_mm)
        # ``needs_review`` 的口径是「这一例要人看过才算数」（绑定歧义挂起 /
        # 引用与 RIS 不一致 / 归并降级），**不是**「还没判定」。
        # 尚未判定由 ``verdict=pending`` 自己说明 —— 两者混用会让汇总行的
        # 「挂起数」等于病例总数，等于没有这一列。
        #
        # **降级例（任务 24）**：模型这一层没成功，结论由规则基线给出 ——
        # 标记 ``rule_degraded`` + 降级原因，**仍进指标分母**（结论是确定的），
        # 但单列降级数、优先复核。降级**不推进 run 状态**：引擎最高只到
        # ``evaluated``，写回仍要人工确认。
        degraded = bool(getattr(gold, "degraded", False))
        # **判定依据与多标签口径**（任务 28）：四层结论 + 覆盖结论落进逐例行，
        # 没盖住的病灶逐条记下（任务 30 的漏诊原因文案直接引用），
        # 属性不可比的例单列 —— 既不当命中也不当漏诊。
        incomparable = bool(judgement.has_incomparable())
        incomparable_reason = judgement.incomparable_reason
        if incomparable and judgement.incomparable_lesions:
            incomparable_reason = "；".join(
                part
                for part in (
                    judgement.incomparable_reason,
                    "；".join(entry.reason for entry in judgement.incomparable_lesions),
                )
                if part
            )
        row = CaseRow(
            accession_no=case.accession_no,
            verdict=judgement.verdict,
            model_findings=result.summary(),
            failure_reason=judgement.reason if judgement.verdict == VERDICT_FAILED else "",
            tool_call_id=record_tool_call_id,
            needs_review=bool(
                case.needs_review
                or gold.needs_human_review
                or degraded
                or incomparable
            ),
            normalize_source=gold.source,
            row_no=case.row_no,
            error_code=(
                ErrorCode.RULE_DEGRADED.value if degraded else judgement.error_code
            ),
            held=bool(case.needs_review),
            rule_degraded=degraded,
            degrade_reason=str(getattr(gold, "degrade_reason", "") or ""),
            judge_basis=judgement.basis_text(),
            uncovered_labels=judgement.uncovered_text(),
            incomparable=incomparable,
            incomparable_reason=incomparable_reason,
            # **金标准摘要**（任务 30）：由归并后的结构化标签拼成 ——
            # 漏诊清单要带「金标准说了什么」，但**原文不落产物**。
            gold_summary=gold_summary_text(gold),
        )
        self._settle_case(
            request,
            row.accession_no,
            case_status_for_row(row),
            reason=judgement.reason,
        )
        self._persist_case_row(request, row)
        self._archive_case(
            request, case, row, gold=gold, judgement=judgement, args=args, call_name=call_name
        )
        return row, queued_seconds

    # ---- 单例失败行 ----
    def _failure_row(self, case: CaseInput, failure: CaseFailure) -> CaseRow:
        """单例层面的契约失败：记一行失败，不中断整批。

        **失败原因取自失败记录**（错误码表的固定文案）——
        与终端输出、调用底账同一句话。
        """

        row = CaseRow(
            accession_no=case.accession_no,
            verdict=VERDICT_FAILED,
            model_findings="",
            failure_reason=failure.failure_reason,
            tool_call_id="",
            needs_review=case.needs_review,
            normalize_source="",
            row_no=case.row_no,
            error_code=failure.error_code,
            held=bool(case.needs_review),
        )
        return row

    # ---- 逐例产物归档 ----
    def _archive_case(
        self,
        request: EvalRequest,
        case: CaseInput,
        row: CaseRow,
        *,
        gold: GoldLabels | None,
        judgement: Judgement | None,
        args: dict[str, Any],
        call_name: str,
        failure: CaseFailure | None = None,
    ) -> CaseArtifact:
        """落一例的产物（``runs/<run_id>/cases/<检查号>/attempt-XXXX/case.json``）。

        **重跑不覆盖历史**：序号自增，两版都在，可以逐例比对。
        产物里**不落金标准原文**（可能带患者可识别信息），只落它的 sha256
        与归并出的标签。

        **失败例也归档**：失败记录（错误码 / 原始 HTTP 状态 / 失败类别 /
        端点）一并落盘 —— 事后问「这一例为什么算失败」，看这一份就够，
        不用去翻日志。
        """

        payload: dict[str, Any] = {
            "accession_no": row.accession_no,
            "row_no": row.row_no,
            "attempted_at": now_iso(),
            "tool_call_id": row.tool_call_id,
            "call_name": call_name,
            "call_args": dict(args),
            "model_findings": row.model_findings,
            "gold_labels": gold.to_dict() if gold is not None else None,
            "judgement": judgement.to_dict() if judgement is not None else None,
            "verdict": row.verdict,
            "failure_reason": row.failure_reason,
            "needs_review": row.needs_review,
            "normalize_source": row.normalize_source,
            "error_code": row.error_code,
            "failure": failure.to_dict() if failure is not None else None,
            # **降级留痕**（任务 24）：这一例的结论不是模型给的 ——
            # 原因、原因错误码都落盘，事后问「这例凭什么这么判」有据可查。
            "degradation": (
                {
                    "degraded": True,
                    "reason": row.degrade_reason,
                    "error_code": row.error_code,
                    "counts_toward_metrics": counts_toward_denominator(row),
                }
                if row.rule_degraded
                else None
            ),
            "held": row.held,
            "gold_standard_sha256": _sha256_text(case.gold_standard_text),
        }

        # **产物脱敏**（任务 34）：金标准原文派生的片段（``evidence_span``）
        # 可能带姓名 / 住院号 —— 原文在院内评估表里（那是医院自己的单据），
        # 但**产物**只该留检查号与内容哈希。落盘前先脱敏，脱敏了几处也记进
        # ``redaction`` 块，事后审计看得见。
        payload, masked_kinds = mask_payload(payload)
        if masked_kinds:
            payload["redaction"] = {"masked": True, "kinds": masked_kinds}

        return write_case_artifact(
            self.runs_dir / request.run_id, row.accession_no, payload
        )

    # ---- 调用底账 ----
    def _record_call(
        self,
        recorder: ToolCallRecorder,
        run_id: str,
        *,
        name: str,
        args: dict[str, Any],
        content: str,
    ) -> str:
        """记一条调用底账，返回它的 ``tool_call_id``。

        挂了 run store 时走 store 的登记口 —— 那边带**去重索引**，
        同一个 ``tool_call_id`` 不会落两遍；没挂 store 时直接追加 JSONL。
        两条路径的落盘内容完全一致（同一份 JSONL、同一套字段）。
        """

        timestamp = now_iso()
        call_id = new_tool_call_id()
        if self.store is not None:
            self.store.append_tool_call(
                run_id,
                tool_call_id=call_id,
                name=name,
                args=args,
                content=content,
                timestamp=timestamp,
            )
            return call_id
        record = recorder.record(
            name=name,
            args=args,
            content=content,
            tool_call_id=call_id,
            timestamp=timestamp,
        )
        return record.tool_call_id

    # ---- 模型调用底账 ----
    def _model_call_recorder(self, run_id: str) -> ModelCallRecorder:
        """取本轮的**模型调用底账**记录器（按 run_id 缓存一个）。

        同一轮的多线程共用同一个记录器 —— 写入互斥由记录器内部的文件锁保证，
        这里只防「两个线程各造一个记录器、抢同一个文件」。
        """

        with self._model_call_lock:
            recorder = self._model_call_recorders.get(run_id)
            if recorder is None:
                recorder = ModelCallRecorder(self.runs_dir / run_id)
                self._model_call_recorders[run_id] = recorder
            return recorder

    def _record_model_call(
        self,
        request: EvalRequest,
        *,
        case: CaseInput,
        tool_call_id: str,
    ) -> None:
        """把这一例的**模型调用元数据**记进 ``runs/<run_id>/model_calls.jsonl``。

        **没调模型就不记**：走规则基线（``--merge rule``）时压根没有模型调用，
        账本就不该有行 —— 一本空账本比没有账本更让人误会（「调过了但没记」）。

        **只落哈希、不落原文**：提示词与模型返回里带金标准原文（可能含患者
        可识别信息），底账里只留 ``prompt_sha256`` / ``raw_response_sha256``。

        **失败的那次也记**：归并层会把失败降级成规则基线，
        「这一轮为什么降级」要能从账上查出来（``status=failed`` + 原因码）。
        """

        meta = getattr(self.normalizer, "last_meta", None)
        if meta is None:
            return
        prompt_ref = getattr(self.normalizer, "prompt_ref", None)
        recorder = self._model_call_recorder(request.run_id)
        # 元数据**整体**交给审计层落盘 —— 引擎不逐个字段拼协议细节
        # （协议层的字段名由协议层定，审计层原样留痕）。
        recorder.record(
            meta=meta,
            tool_call_id=tool_call_id or str(getattr(meta, "tool_call_id", "") or ""),
            accession_no=case.accession_no,
            prompt_ref=prompt_ref,
        )

    # ---- 产物 ----
    def _timings_payload(self, run_id: str) -> dict[str, Any]:
        """逐例耗时台账（进 ``runs/<run_id>/case_timings.json``）。

        **两段时间分开记**：``elapsed_ms`` 是这一例从开跑到出判定的总耗时，
        ``queued_ms`` 是其中**排队等端点额度**的时间。压测报告据此回答
        「这轮慢在模型算得慢，还是慢在排队」—— 这两件事的处置完全不同：
        前者要降批规模，后者要抬端点上限。
        """

        return {
            "run_id": run_id,
            "concurrency": self.concurrency,
            "case_count": len(self.case_timings),
            "endpoints": self.limiters.stats() if self.limiters is not None else {},
            "cases": [timing.to_dict() for timing in self.case_timings],
        }

    def _write_artifacts(self, run_dir: Path, response: EvalResponse) -> RunArtifacts:
        """落本轮产物。

        写法走 :mod:`nodule_eval.store` 的**唯一实现** —— 引擎与 run store 共用同一份
        CSV / JSON 写出逻辑，两处各写一遍迟早写出两种格式。

        **结论产物（任务 30）**也在这一段落盘：
        ``missed_list.csv``（漏诊清单：放射科逐例回看原调用）与
        ``summary.json``（run 级结论：招采 / 上线 / 换版说明直接引用）。
        """

        case_rows_path = write_case_rows_csv(
            run_dir / CASE_ROWS_FILENAME, response.case_rows
        )
        result_path = write_json_file(
            run_dir / RESULT_FILENAME, response.to_dict()
        )
        timings_path = write_json_file(
            run_dir / CASE_TIMINGS_FILENAME, self._timings_payload(response.run_id)
        )
        missed_path = write_missed_list_csv(
            run_dir / MISSED_LIST_FILENAME, response.missed_list
        )
        summary_payload = build_summary_payload(
            run_id=response.run_id,
            summary=response.summary,
            metrics=response.metrics,
            rows=response.case_rows,
            missed=response.missed_list,
            missed_list_path=MISSED_LIST_FILENAME,
        )
        summary_path = write_json_file(run_dir / SUMMARY_FILENAME, summary_payload)

        # **模型调用底账**（任务 33）：调过模型才有这个文件。
        model_calls_path = run_dir / MODEL_CALLS_FILENAME

        # **字段白名单登记**（任务 34）：写产物时同步登记这一类产物的字段，
        # 落 ``runs/<run_id>/artifact_fields.json``。事后扫描拿它对账 ——
        # 产物里出现没登记的字段就报错（后来加字段忘了登记，明细就出去了）。
        for artifact_key in (
            "case_rows.csv",
            "result.json",
            "case_timings.json",
            "missed_list.csv",
            "summary.json",
            "case_attempt.json",
        ):
            register_artifact_fields(run_dir, artifact_key)

        return RunArtifacts(
            run_dir=run_dir,
            case_rows_csv=case_rows_path,
            tool_calls_jsonl=run_dir / "tool_calls.jsonl",
            result_json=result_path,
            case_timings_json=timings_path,
            missed_list_csv=missed_path,
            summary_json=summary_path,
            model_calls_jsonl=model_calls_path if model_calls_path.is_file() else None,
        )

    # ---- 便捷入口 ----
    def run_cases(
        self,
        cases: Iterable[CaseInput],
        *,
        run_id: str,
        dataset_ref: str,
    ) -> EvalResponse:
        """直接给一组病例跑一遍（CLI 与测试用）。"""

        request = EvalRequest(
            run_id=run_id,
            model_id=self.model_id,
            model_version=self.model_version,
            dataset_ref=dataset_ref,
            cases=tuple(cases),
        )
        return self.run(request)


def case_rows_to_dicts(rows: Sequence[CaseRow]) -> list[dict[str, Any]]:
    """逐例行转字典（导出与测试用）。"""

    return [row.to_dict() for row in rows]


def detection_summary(result: DetectionResult) -> str:
    """检出结果摘要（独立成函数便于测试）。"""

    return result.summary()
