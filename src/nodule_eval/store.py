"""run store：SQLite 落 run / case 双表，并统一约定产物目录。

**源产品证据**：参考实现用 LangGraph 的 ``Annotated[List[AnyMessage], operator.add]``
做状态归约（``github_ref/MedRAX/medrax/agent/agent.py:35-45``），配合入口侧的
``MemorySaver``（``github_ref/MedRAX/main.py:75``）做检查点 —— 状态活在**进程内存**里：
进程一退就没了，一次跑两百例中途断掉，前面的进度全丢，只能从头再来。

本产品把「检查点」改写成**落盘的 SQLite run store**：

- 状态**跨进程可读** —— 引擎跑完退出，信息科换个终端还能查这一轮跑到哪了；
- 状态**可断点续跑** —— 已完成的例不再重复调用推理端点（任务 16 消费）；
- 状态**可按 run_id 复算** —— 从库里取回逐例判定行，与产物 CSV 逐字对齐。

**幂等落盘**是这一层的第一条纪律：同一个 ``run_id`` 重复写，**按主键覆盖**，
不追加重复行；同一个 ``tool_call_id`` 重复登记，**不重复追加**底账。
底账一旦重复，反查「这例凭什么算漏判」时就会出现两条一模一样的记录，
复核的人得先猜哪条是真的 —— 这比少一条更麻烦。

**并发安全**：SQLite 开 WAL，写操作用 ``BEGIN IMMEDIATE`` 划事务边界，
锁冲突按退避重试。半行状态（读到了还没提交的行）在事务边界外**读不到**。

**多线程下的连接策略（任务 15）**：逐例 fan-out 是多线程跑的
（``concurrency`` 个工作线程各处理一批病例），而 SQLite 的连接**默认不能跨线程用**。
所以本模块给**每个线程一条自己的连接**（``threading.local``），
指向同一个库文件：WAL 允许一写多读，写与写之间靠 ``BEGIN IMMEDIATE`` 排队。

一条连接一个线程，而不是一把大锁串行所有写 —— 串行化会把并发评估
重新压回单线程；分连接则让「读」与「写」真正并行，写与写之间由 SQLite 自己排队。
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .audit.state_transition import (
    TRANSITIONS_FILENAME,
    StateTransition,
    TransitionRecorder,
)
from .audit.toolcall import TOOL_CALLS_FILENAME, ToolCallRecorder, now_iso
from .caseset import BINDING_FILENAME
from .contracts import (
    CASE_ROW_COLUMNS,
    RUN_STATUS_CALLED,
    VERDICT_FAILED,
    VERDICT_PENDING,
    CaseRow,
)
from .errors import ErrorCode, NoduleEvalError
from .writeback_receipt import (
    RECEIPT_FILENAME,
    RECEIPT_STATUS_NONE,
    WritebackReceipt,
    assert_receipt_transition,
    write_receipt_file,
)

# --------------------------------------------------------------------------
# 目录与文件名约定
# --------------------------------------------------------------------------

#: 产物根目录的缺省名（相对当前工作目录）。
DEFAULT_RUNS_DIR = "runs"

#: run store 数据库文件名。
DEFAULT_DB_FILENAME = "nodule_eval.sqlite3"

#: 平台默认存储目录名（``%LOCALAPPDATA%`` / ``XDG_DATA_HOME`` 之下）。
STORE_DIR_NAME = "nodule-ai-model-eval"

#: 逐例明细 CSV。
CASE_ROWS_FILENAME = "case_rows.csv"
#: run 级结论摘要 JSON。
SUMMARY_FILENAME = "summary.json"
#: 漏诊清单 CSV。
MISSED_LIST_FILENAME = "missed_list.csv"
#: 模型调用元数据底账 JSONL。
MODEL_CALLS_FILENAME = "model_calls.jsonl"
#: 引擎写出的结果 JSON（表内插件读回它回填新增列）。
RESULT_FILENAME = "result.json"
#: 逐例耗时台账 JSON（并发跑完后的「慢在哪」台账，压测报告据此算 P50 / P95）。
CASE_TIMINGS_FILENAME = "case_timings.json"
#: 写回回执 JSON（这一轮到底有没有真的登记成功）。
RECEIPT_FILENAME = RECEIPT_FILENAME

#: 一次 run 的**产物目录约定**：``runs/<run_id>/`` 下固定这九个文件。
#:
#: 九件套各司其职 —— 逐例明细给放射科看、摘要给信息科看、漏诊清单给换版论证用、
#: 两份底账给事后反查用、绑定清单给「这例凭什么绑到这张影像」看、
#: 状态迁移日志给「这一轮什么时候变成已复核的、是谁推的」看、
#: 耗时台账给「这轮慢在算还是慢在排队」看、
#: 写回回执给「这一轮到底有没有真的登记成功」看。
RUN_ARTIFACT_FILENAMES: tuple[str, ...] = (
    CASE_ROWS_FILENAME,
    SUMMARY_FILENAME,
    MISSED_LIST_FILENAME,
    TOOL_CALLS_FILENAME,
    MODEL_CALLS_FILENAME,
    BINDING_FILENAME,
    TRANSITIONS_FILENAME,
    CASE_TIMINGS_FILENAME,
    RECEIPT_FILENAME,
)

# --------------------------------------------------------------------------
# 状态名（**取值**在此声明；**合法迁移边**由任务 13 的状态机表管）
# --------------------------------------------------------------------------

#: run 状态：草稿（还没开始跑）。
RUN_STATUS_DRAFT = "draft"
#: run 状态：病例集已装配。
RUN_STATUS_ASSEMBLED = "assembled"
#: run 状态：正在逐例跑。
RUN_STATUS_RUNNING = "running"
#: run 状态：已判定（**草稿层**，尚未经人工复核）。
RUN_STATUS_EVALUATED = "evaluated"
#: run 状态：人工已复核。
RUN_STATUS_REVIEWED = "reviewed"
#: run 状态：已写回科研平台（已生效事实）。
RUN_STATUS_SUBMITTED = "submitted"
#: run 状态：整轮失败。
RUN_STATUS_FAILED = "failed"
#: run 状态：已逐例调完推理端点但**尚未判定**（判定引擎落地前的过渡态，
#: 取值与 :data:`nodule_eval.contracts.RUN_STATUS_CALLED` 一致）。
RUN_STATUS_CALLED = RUN_STATUS_CALLED

#: 全部 run 状态取值。
RUN_STATUSES: tuple[str, ...] = (
    RUN_STATUS_DRAFT,
    RUN_STATUS_ASSEMBLED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_CALLED,
    RUN_STATUS_EVALUATED,
    RUN_STATUS_REVIEWED,
    RUN_STATUS_SUBMITTED,
    RUN_STATUS_FAILED,
)

#: case 状态：待处理。
CASE_STATUS_PENDING = "pending"
#: case 状态：已调推理端点。
CASE_STATUS_CALLED = "called"
#: case 状态：金标准已归并。
CASE_STATUS_NORMALIZED = "normalized"
#: case 状态：已判定。
CASE_STATUS_JUDGED = "judged"
#: case 状态：这一例失败。
CASE_STATUS_FAILED = "failed"
#: case 状态：这一例挂起待人工判定。
CASE_STATUS_NEEDS_REVIEW = "needs_review"

#: 全部 case 状态取值。
CASE_STATUSES: tuple[str, ...] = (
    CASE_STATUS_PENDING,
    CASE_STATUS_CALLED,
    CASE_STATUS_NORMALIZED,
    CASE_STATUS_JUDGED,
    CASE_STATUS_FAILED,
    CASE_STATUS_NEEDS_REVIEW,
)

#: case 的**终态** —— 已经有结论、不会被重跑的例。
#:
#: ``judged``（判完了）/ ``failed``（跑挂了，结论就是失败）/ ``needs_review``
#: （挂起等人看，结论就是挂起）—— 三种都是**这一例有话说**。
#:
#: 剩下的 ``pending`` / ``called`` / ``normalized`` 是**跑到一半**的状态：
#: 中途断掉时例就停在这儿，续跑时这些例要重新走一遍
#: （``reset_incomplete_cases`` 把它们复位回 ``pending``）。
CASE_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        CASE_STATUS_JUDGED,
        CASE_STATUS_FAILED,
        CASE_STATUS_NEEDS_REVIEW,
    }
)


def is_case_terminal(status: str) -> bool:
    """这个 case 状态是不是**终态**（有结论、不会被重跑）。"""

    return status in CASE_TERMINAL_STATUSES


# --------------------------------------------------------------------------
# 状态机（任务 13）：合法迁移边写死在这里
# --------------------------------------------------------------------------
#
# **为什么要一张写死的迁移表**：状态推进必须**可预期** —— 谁在什么条件下
# 能把这一轮从哪一格推到哪一格，写死在代码里，出问题时能对着表逐条核对。
# 靠 if/else 临场判断，早晚会多出一条「本来不该有的路」。
#
# **run 状态机（6 条推进边 + 失败边 + 重跑复位边）**：
#
#     draft ──→ assembled ──→ running ──→ called ──→ evaluated ──→ reviewed ──→ submitted
#
# - ``evaluated`` 是**草稿层**：判定完了，但还没人看过；
# - ``reviewed`` 与 ``submitted`` 是**人工确认层**，只有人工能推（见
#   :data:`HUMAN_GATED_RUN_STATES`）—— 引擎再怎么跑都推不进去；
# - ``submitted`` 是终态：已写回科研平台，**已生效事实**，不许再改判。
#
# **case 状态机（4 条推进边 + 失败 / 挂起出口边 + 重跑复位边）**：
#
#     pending ──→ called ──→ normalized ──→ judged
#
# - ``pending → called``：已调推理端点并取回；
# - ``called → normalized``：金标准已归并；
# - ``normalized → judged``：判定已出；
# - ``needs_review → called``：人工把绑定歧义澄清后，这一例重新调推理端点；
# - ``failed`` / ``needs_review`` 是**出口**—— 这两类的例不进指标分母，
#   等人处理完才能回到 ``pending`` 重跑。

#: run 状态机的合法去向。
RUN_TRANSITIONS: dict[str, tuple[str, ...]] = {
    RUN_STATUS_DRAFT: (RUN_STATUS_ASSEMBLED, RUN_STATUS_FAILED),
    RUN_STATUS_ASSEMBLED: (RUN_STATUS_RUNNING, RUN_STATUS_FAILED),
    RUN_STATUS_RUNNING: (RUN_STATUS_CALLED, RUN_STATUS_FAILED),
    # ``called → running`` / ``evaluated → running``：同一轮重跑（复位重跑）
    RUN_STATUS_CALLED: (RUN_STATUS_EVALUATED, RUN_STATUS_RUNNING, RUN_STATUS_FAILED),
    RUN_STATUS_EVALUATED: (RUN_STATUS_REVIEWED, RUN_STATUS_RUNNING, RUN_STATUS_FAILED),
    # ``reviewed`` 之后只能提交，不能再退回 —— 人工确认过的东西不该被机器推翻
    RUN_STATUS_REVIEWED: (RUN_STATUS_SUBMITTED,),
    RUN_STATUS_SUBMITTED: (),
    # 整轮失败后可重来
    RUN_STATUS_FAILED: (RUN_STATUS_RUNNING,),
}

#: case 状态机的合法去向。
CASE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    CASE_STATUS_PENDING: (
        CASE_STATUS_CALLED,
        CASE_STATUS_FAILED,
        CASE_STATUS_NEEDS_REVIEW,
    ),
    CASE_STATUS_CALLED: (
        CASE_STATUS_NORMALIZED,
        CASE_STATUS_FAILED,
        CASE_STATUS_NEEDS_REVIEW,
        CASE_STATUS_PENDING,
    ),
    CASE_STATUS_NORMALIZED: (
        CASE_STATUS_JUDGED,
        CASE_STATUS_FAILED,
        CASE_STATUS_NEEDS_REVIEW,
        CASE_STATUS_PENDING,
    ),
    # 已判定的例要重判，必须先复位到 pending 重新走一遍
    CASE_STATUS_JUDGED: (CASE_STATUS_PENDING,),
    CASE_STATUS_FAILED: (CASE_STATUS_PENDING,),
    # 人工澄清绑定歧义后，这一例重新调推理端点
    CASE_STATUS_NEEDS_REVIEW: (CASE_STATUS_PENDING, CASE_STATUS_CALLED),
}

#: 触发方：引擎（自动推进）。
ACTOR_ENGINE = "engine"
#: 触发方：病例集装配。
ACTOR_ASSEMBLE = "assemble"
#: 触发方：人工复核（表格里那一下「确认复核」）。
ACTOR_HUMAN_REVIEW = "human-review"
#: 触发方：人工确认提交（表格里那一下「确认提交」）。
ACTOR_HUMAN_SUBMIT = "human-submit"

#: 允许推进到**人工确认层**的触发方。
#:
#: 只有这两个能推 ``reviewed`` / ``submitted`` —— 引擎、装配、批量任务都不行。
#: 这条约束写在状态机里（不是写在口头约定里）：引擎就算想推也会被拒。
HUMAN_ACTORS: frozenset[str] = frozenset({ACTOR_HUMAN_REVIEW, ACTOR_HUMAN_SUBMIT})

#: 必须由人工触发方推进的 run 状态。
HUMAN_GATED_RUN_STATES: frozenset[str] = frozenset(
    {RUN_STATUS_REVIEWED, RUN_STATUS_SUBMITTED}
)


def run_transition_targets(status: str) -> tuple[str, ...]:
    """某个 run 状态的合法去向；未知状态返回空元组。"""

    return RUN_TRANSITIONS.get(status, ())


def case_transition_targets(status: str) -> tuple[str, ...]:
    """某个 case 状态的合法去向；未知状态返回空元组。"""

    return CASE_TRANSITIONS.get(status, ())


def is_legal_run_transition(current: str, target: str) -> bool:
    """这条 run 迁移在不在表上。"""

    return target in RUN_TRANSITIONS.get(current, ())


def is_legal_case_transition(current: str, target: str) -> bool:
    """这条 case 迁移在不在表上。"""

    return target in CASE_TRANSITIONS.get(current, ())


def case_status_path(current: str, target: str) -> tuple[str, ...]:
    """在合法边上找出从 ``current`` 走到 ``target`` 的**最短路径**（不含起点）。

    引擎要推进一个 case 的状态时，不能一步跳过去 ——
    ``pending → judged`` 不在表上（没调推理端点、没归并金标准就出判定，
    等于凭空造结论）。这条函数把 ``pending → called → normalized → judged``
    这条路找出来，让引擎**一步一步走**，每一步都留迁移痕。

    走不通返回空元组（起止相同也返回空元组 —— 没有要走的边）。
    """

    if current == target:
        return ()
    if current not in CASE_TRANSITIONS:
        return ()

    # 广度优先：状态机很小，但「最短路径」比「随便找一条路」更好解释 ——
    # 复核的人看迁移底账时，路径是唯一的。
    seen = {current}
    queue: list[tuple[str, tuple[str, ...]]] = [(current, ())]
    while queue:
        node, path = queue.pop(0)
        for nxt in CASE_TRANSITIONS.get(node, ()):
            if nxt in seen:
                continue
            new_path = path + (nxt,)
            if nxt == target:
                return new_path
            seen.add(nxt)
            queue.append((nxt, new_path))
    return ()


# --------------------------------------------------------------------------
# 平台默认存储位置
# --------------------------------------------------------------------------


def default_store_dir() -> Path:
    """平台默认的存储目录。

    Windows 上落 ``%LOCALAPPDATA%\\nodule-ai-model-eval\\``（院内 PC 的惯例），
    其余平台落 ``$XDG_DATA_HOME/nodule-ai-model-eval/``（缺省 ``~/.local/share/``）。
    """

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / STORE_DIR_NAME


def default_store_path() -> Path:
    """平台默认的数据库文件路径。"""

    return default_store_dir() / DEFAULT_DB_FILENAME


def resolve_store_path(db_path: Path | str | None) -> Path:
    """把 ``--store`` 的取值解析成数据库文件路径。

    给目录就补上默认文件名；给文件就直接用。**不猜、不回落**：
    调用方拿到的路径就是它指定的路径。
    """

    if db_path is None:
        return default_store_path()
    path = Path(db_path)
    if path.is_dir() or str(path).endswith(("/", os.sep)):
        return path / DEFAULT_DB_FILENAME
    return path


# --------------------------------------------------------------------------
# 产物读写（**唯一实现**）：引擎与 run store 共用，避免两处写出两种格式
# --------------------------------------------------------------------------


def write_case_rows_csv(path: Path | str, rows: Sequence[CaseRow]) -> Path:
    """写逐例明细 CSV。

    列顺序取 :data:`~nodule_eval.contracts.CASE_ROW_COLUMNS`（与评估表新增列一致），
    编码用 ``utf-8-sig`` —— 科室用 Excel 直接双击打开不乱码。
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CASE_ROW_COLUMNS))
        writer.writeheader()
        for row in rows:
            payload = row.to_dict()
            writer.writerow({key: payload.get(key, "") for key in CASE_ROW_COLUMNS})
    return target


def read_case_rows_csv(path: Path | str) -> tuple[dict[str, Any], ...]:
    """读回逐例明细 CSV（表头缺列即报契约错，不静默补空）。"""

    source = Path(path)
    if not source.is_file():
        return ()
    with open(source, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return ()
        missing = [name for name in CASE_ROW_COLUMNS if name not in reader.fieldnames]
        if missing:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"逐例明细 CSV 缺列：{'、'.join(missing)}（文件 {source}）",
            )
        return tuple(dict(row) for row in reader)


def write_json_file(path: Path | str, payload: Any) -> Path:
    """写 JSON 文件（``ensure_ascii=False``，中文可读）。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def read_json_file(path: Path | str) -> Any:
    """读 JSON 文件；不存在返回 ``None``，解析失败报契约错。"""

    source = Path(path)
    if not source.is_file():
        return None
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"JSON 产物不是合法 JSON：{source}（{exc}）",
        ) from exc


# --------------------------------------------------------------------------
# 记录形态
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredRun:
    """库里的一行 run。"""

    run_id: str
    dataset_ref: str = ""
    model_id: str = ""
    model_version: str = ""
    status: str = RUN_STATUS_DRAFT
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset_ref": self.dataset_ref,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class StoredCase:
    """库里的一行 case。

    ``row`` 是逐例明细的**快照**（含判定依据与金标准归并结果），
    使「按 run_id 复算」不必回读 CSV —— 跨进程、跨机器都读得到。
    """

    run_id: str
    accession_no: str
    status: str = CASE_STATUS_PENDING
    verdict: str = ""
    tool_call_id: str = ""
    error_code: str = ""
    row_no: int | None = None
    updated_at: str = ""
    row: CaseRow | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "accession_no": self.accession_no,
            "status": self.status,
            "verdict": self.verdict,
            "tool_call_id": self.tool_call_id,
            "error_code": self.error_code,
            "row_no": self.row_no,
            "updated_at": self.updated_at,
        }
        if self.row is not None:
            payload["row"] = self.row.to_dict()
        return payload


def _row_from_json(payload: str) -> CaseRow | None:
    """从 ``row_json`` 还原逐例行；坏数据返回 ``None``（不猜默认值）。"""

    if not payload:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, Mapping):
        return None
    row_no = data.get("row_no")
    if row_no is not None and (isinstance(row_no, bool) or not isinstance(row_no, int)):
        row_no = None
    return CaseRow(
        accession_no=str(data.get("accession_no") or ""),
        verdict=str(data.get("verdict") or VERDICT_PENDING),
        model_findings=str(data.get("model_findings") or ""),
        failure_reason=str(data.get("failure_reason") or ""),
        tool_call_id=str(data.get("tool_call_id") or ""),
        needs_review=bool(data.get("needs_review")),
        normalize_source=str(data.get("normalize_source") or ""),
        row_no=row_no,
        error_code=str(data.get("error_code") or ""),
        # ↓ 下面这八个是**运行时标记**。快照必须**逐字还原**它们 ——
        #   少还原一个，「按 run_id 复算」出来的行就跟当初判定的那一行
        #   不是同一回事了（降级例看不出是降级例、挂起例看不出是挂起例），
        #   写回科研平台的逐例行也会跟着失真。
        held=bool(data.get("held")),
        rule_degraded=bool(data.get("rule_degraded")),
        degrade_reason=str(data.get("degrade_reason") or ""),
        judge_basis=str(data.get("judge_basis") or ""),
        uncovered_labels=str(data.get("uncovered_labels") or ""),
        incomparable=bool(data.get("incomparable")),
        incomparable_reason=str(data.get("incomparable_reason") or ""),
        gold_summary=str(data.get("gold_summary") or ""),
    )


def _is_lock_error(exc: BaseException) -> bool:
    """判断是不是「库被别的进程占着」这类可重试的锁错误。"""

    text = str(exc).lower()
    return "locked" in text or "busy" in text


def case_status_for_row(row: CaseRow) -> str:
    """由逐例判定行推出该例的 case 状态。

    口径与 run store 的状态词表一致：**挂起优先于失败** ——
    「这一例绑不准等人复核」和「这一例调用失败」是两回事，
    混成一个状态，复核的人就不知道该去改表还是该找厂商。
    """

    if row.needs_review:
        return CASE_STATUS_NEEDS_REVIEW
    if row.verdict == VERDICT_FAILED:
        return CASE_STATUS_FAILED
    if row.verdict == VERDICT_PENDING:
        return CASE_STATUS_CALLED
    return CASE_STATUS_JUDGED


# --------------------------------------------------------------------------
# run store
# --------------------------------------------------------------------------


class RunStore:
    """SQLite run store：run / case 双表 + 产物目录。

    参数
    ----
    db_path
        数据库文件路径。给目录时补上默认文件名。
    runs_dir
        产物根目录，run 的产物落 ``<runs_dir>/<run_id>/``。
    busy_timeout_ms
        SQLite 锁等待上限（毫秒）。
    lock_retries
        锁冲突时的重试次数（每次按指数退避）。
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        runs_dir: Path | str | None = None,
        busy_timeout_ms: int = 5000,
        lock_retries: int = 3,
        lock_backoff_seconds: float = 0.2,
    ) -> None:
        self.db_path = resolve_store_path(db_path)
        self.runs_dir = Path(runs_dir) if runs_dir is not None else Path(DEFAULT_RUNS_DIR)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.lock_retries = int(lock_retries)
        self.lock_backoff_seconds = float(lock_backoff_seconds)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        #: **每线程一条连接**（见模块说明）：逐例 fan-out 是多线程跑的，
        #: SQLite 的连接不能跨线程共享。
        self._local = threading.local()
        self._conn_lock = threading.Lock()
        self._all_conns: list[sqlite3.Connection] = []
        self._ensure_wal()
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    # ---- 连接 ----

    @property
    def _conn(self) -> sqlite3.Connection:
        """当前线程的连接（没有就建一条）。

        每个线程一条连接指向同一个库文件：WAL 允许一写多读，
        写与写之间由 ``BEGIN IMMEDIATE`` 排队 —— 比一把大锁串行所有写更划算。
        """

        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open_connection()
        return conn

    def _open_connection(self) -> sqlite3.Connection:
        """开一条连接并登记（关库时一并关掉）。"""

        conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,  # 事务边界由本模块显式控制
            timeout=self.busy_timeout_ms / 1000.0,
            check_same_thread=False,  # 连接归创建它的线程用；close() 由主线程统一收尾
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        self._local.conn = conn
        with self._conn_lock:
            self._all_conns.append(conn)
        return conn

    def _ensure_wal(self) -> None:
        """确保日志模式是 WAL。

        已经在 WAL 上时**不再设置** —— ``PRAGMA journal_mode=WAL`` 会去要写锁，
        别的进程正写着就报「database is locked」，而它本来就是 WAL 了，没必要要这把锁。
        """

        rows = self._conn.execute("PRAGMA journal_mode").fetchall()
        if rows and str(rows[0][0]).lower() == "wal":
            return
        self._retry_on_lock(lambda: self._conn.execute("PRAGMA journal_mode = WAL"))

    def _retry_on_lock(self, action: Any) -> Any:
        """执行一个可能撞锁的动作，撞了就退避重试。"""

        last_error: sqlite3.Error | None = None
        for attempt in range(self.lock_retries + 1):
            try:
                return action()
            except sqlite3.OperationalError as exc:
                if not _is_lock_error(exc):
                    raise
                last_error = exc
                if attempt >= self.lock_retries:
                    break
                time.sleep(self.lock_backoff_seconds * (2**attempt))
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"存储库忙，操作未完成：{self.db_path}（{last_error}）",
        )

    # ---- 生命周期 ----

    @classmethod
    def open(
        cls,
        db_path: Path | str | None = None,
        *,
        runs_dir: Path | str | None = None,
        **kwargs: Any,
    ) -> "RunStore":
        """打开（必要时新建）一个 run store。

        路径建不出来时抛 ``CONTRACT_INVALID`` 并写明是哪个路径 ——
        「存储库打不开」不能只留一句「未知错误」。
        """

        try:
            return cls(db_path, runs_dir=runs_dir, **kwargs)
        except (OSError, sqlite3.Error) as exc:
            target = resolve_store_path(db_path)
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"存储库打不开：{target}（{exc}）",
            ) from exc

    def close(self) -> None:
        """关连接（**所有线程**建的那几条一并关）。"""

        with self._conn_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - 关一条已关的连接
                pass
        self._local = threading.local()

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ---- 建表 ----

    def _init_schema(self) -> None:
        """建表（幂等）。

        表名 ``case_row`` 而不是 ``case`` —— ``CASE`` 是 SQLite 的保留字，
        直接用会报语法错。逻辑上它就是「case 表」。

        建表也要走退避重试：多个进程同时第一次打开同一个库时，
        建表语句会撞锁。
        """

        self._retry_on_lock(
            lambda: self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS run (
                    run_id        TEXT PRIMARY KEY,
                    dataset_ref   TEXT NOT NULL DEFAULT '',
                    model_id      TEXT NOT NULL DEFAULT '',
                    model_version TEXT NOT NULL DEFAULT '',
                    status        TEXT NOT NULL DEFAULT 'draft',
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS case_row (
                    run_id        TEXT NOT NULL,
                    accession_no  TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'pending',
                    verdict       TEXT NOT NULL DEFAULT '',
                    tool_call_id  TEXT NOT NULL DEFAULT '',
                    error_code    TEXT NOT NULL DEFAULT '',
                    row_no        INTEGER,
                    row_json      TEXT NOT NULL DEFAULT '',
                    updated_at    TEXT NOT NULL,
                    PRIMARY KEY (run_id, accession_no)
                );

                CREATE TABLE IF NOT EXISTS tool_call (
                    run_id       TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    name         TEXT NOT NULL DEFAULT '',
                    recorded_at  TEXT NOT NULL,
                    PRIMARY KEY (run_id, tool_call_id)
                );

                CREATE TABLE IF NOT EXISTS state_transition (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id      TEXT NOT NULL,
                    scope       TEXT NOT NULL,
                    subject     TEXT NOT NULL,
                    from_state  TEXT NOT NULL,
                    to_state    TEXT NOT NULL,
                    actor       TEXT NOT NULL DEFAULT '',
                    reason      TEXT NOT NULL DEFAULT '',
                    at          TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS writeback_receipt (
                    idempotency_key TEXT PRIMARY KEY,
                    run_id       TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'none',
                    registration_id TEXT NOT NULL DEFAULT '',
                    submitted_at TEXT NOT NULL DEFAULT '',
                    received_at  TEXT NOT NULL DEFAULT '',
                    http_status  INTEGER,
                    attempts     INTEGER NOT NULL DEFAULT 0,
                    error_code   TEXT NOT NULL DEFAULT '',
                    error_detail TEXT NOT NULL DEFAULT '',
                    actor        TEXT NOT NULL DEFAULT '',
                    summary_json TEXT NOT NULL DEFAULT '',
                    payload_sha256 TEXT NOT NULL DEFAULT '',
                    updated_at   TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_case_row_run ON case_row (run_id);
                CREATE INDEX IF NOT EXISTS idx_tool_call_run ON tool_call (run_id);
                CREATE INDEX IF NOT EXISTS idx_transition_run ON state_transition (run_id);
                CREATE INDEX IF NOT EXISTS idx_receipt_run ON writeback_receipt (run_id);
                """
            )
        )
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """给**已有的库**补上后加的列（幂等）。

        ``CREATE TABLE IF NOT EXISTS`` 只管「新库」—— 老库里那张表早建好了，
        不会因为改了建表语句就多出一列。所以这里显式补列：查
        ``PRAGMA table_info`` 看列在不在，不在就 ``ALTER TABLE ADD COLUMN``。

        **为什么值得单独写一层**：院内机器上的库往往跨版本用着，
        升级一次程序就报「no such column」的话，整轮评估直接跑不起来。
        """

        def _has_column(table: str, column: str) -> bool:
            rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            return any(str(row[1]) == column for row in rows)

        try:
            if not _has_column("writeback_receipt", "payload_sha256"):
                self._conn.execute(
                    "ALTER TABLE writeback_receipt "
                    "ADD COLUMN payload_sha256 TEXT NOT NULL DEFAULT ''"
                )
                self._conn.commit()
        except sqlite3.Error:  # pragma: no cover - 防御性分支：只读库等极端情况跳过
            pass

    # ---- 事务 ----

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：``BEGIN IMMEDIATE`` → 提交 / 回滚。

        锁冲突（另一个进程正在写）按指数退避重试；重试用尽仍拿不到锁时
        抛 ``CONTRACT_INVALID`` 并写明原因 —— **不静默丢写**。
        """

        last_error: sqlite3.Error | None = None
        for attempt in range(self.lock_retries + 1):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                last_error = None
                break
            except sqlite3.OperationalError as exc:
                if not _is_lock_error(exc):
                    raise
                last_error = exc
                if attempt >= self.lock_retries:
                    break
                time.sleep(self.lock_backoff_seconds * (2**attempt))
        if last_error is not None:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"存储库忙，写入未完成：{self.db_path}（{last_error}）",
            )

        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ---- run 表 ----

    def upsert_run(
        self,
        *,
        run_id: str,
        dataset_ref: str = "",
        model_id: str = "",
        model_version: str = "",
        status: str | None = None,
    ) -> StoredRun:
        """写入（或覆盖）一行 run。

        ``created_at`` 首次落库后**不再变**；``status`` 传 ``None`` 表示
        「这次不改状态」—— 状态推进由任务 13 的状态机负责，不在普通写入里偷偷改。
        """

        key = run_id.strip()
        if not key:
            raise NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail="run_id 不得为空")
        now = now_iso()
        with self.write_transaction() as conn:
            # ``status`` 传 None 表示「这次不改状态」：新建行落草稿，
            # 覆盖已有行时保留原状态 —— 状态推进由状态机负责，不在普通写入里偷改。
            conn.execute(
                """
                INSERT INTO run (run_id, dataset_ref, model_id, model_version,
                                 status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    dataset_ref   = excluded.dataset_ref,
                    model_id      = excluded.model_id,
                    model_version = excluded.model_version,
                    status        = COALESCE(?, run.status),
                    updated_at    = excluded.updated_at
                """,
                (
                    key,
                    dataset_ref,
                    model_id,
                    model_version,
                    status if status is not None else RUN_STATUS_DRAFT,
                    now,
                    now,
                    status,
                ),
            )
        return self.get_run(key)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> StoredRun | None:
        """取一行 run；没有则返回 ``None``。"""

        # 用 fetchall 而不是 fetchone：fetchone 会把语句留在「未结束」状态，
        # 连接上就挂着一个旧的读快照 —— 提交后的新行读不到，像库没写进去。
        rows = self._conn.execute(
            "SELECT * FROM run WHERE run_id = ?", (run_id,)
        ).fetchall()
        if not rows:
            return None
        row = rows[0]
        return StoredRun(
            run_id=row["run_id"],
            dataset_ref=row["dataset_ref"],
            model_id=row["model_id"],
            model_version=row["model_version"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_runs(self) -> tuple[StoredRun, ...]:
        """列出全部 run（按创建时间）。"""

        rows = self._conn.execute(
            "SELECT * FROM run ORDER BY created_at, run_id"
        ).fetchall()
        return tuple(
            StoredRun(
                run_id=row["run_id"],
                dataset_ref=row["dataset_ref"],
                model_id=row["model_id"],
                model_version=row["model_version"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        )

    # ---- case 表 ----

    def upsert_case(
        self,
        *,
        run_id: str,
        accession_no: str,
        status: str = CASE_STATUS_PENDING,
        verdict: str = "",
        tool_call_id: str = "",
        error_code: str = "",
        row_no: int | None = None,
        row: CaseRow | None = None,
    ) -> StoredCase:
        """写入（或覆盖）一行 case。

        主键是 ``(run_id, accession_no)`` —— 同一轮里同一个检查号**只占一行**，
        重复写按主键覆盖，不追加重复行。
        """

        run_key = run_id.strip()
        acc = accession_no.strip()
        if not run_key:
            raise NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail="run_id 不得为空")
        if not acc:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID, detail="accession_no 不得为空"
            )
        now = now_iso()
        payload = json.dumps(row.to_dict(), ensure_ascii=False) if row is not None else ""
        with self.write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO case_row (run_id, accession_no, status, verdict, tool_call_id,
                                  error_code, row_no, row_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, accession_no) DO UPDATE SET
                    status       = excluded.status,
                    verdict      = excluded.verdict,
                    tool_call_id = excluded.tool_call_id,
                    error_code   = excluded.error_code,
                    row_no       = excluded.row_no,
                    row_json     = excluded.row_json,
                    updated_at   = excluded.updated_at
                """,
                (
                    run_key,
                    acc,
                    status,
                    verdict,
                    tool_call_id,
                    error_code,
                    row_no,
                    payload,
                    now,
                ),
            )
        return self.get_case(run_key, acc)  # type: ignore[return-value]

    def get_case(self, run_id: str, accession_no: str) -> StoredCase | None:
        """取一行 case；没有则返回 ``None``。"""

        rows = self._conn.execute(
            "SELECT * FROM case_row WHERE run_id = ? AND accession_no = ?",
            (run_id, accession_no),
        ).fetchall()
        if not rows:
            return None
        return self._to_stored_case(rows[0])

    def list_cases(self, run_id: str) -> tuple[StoredCase, ...]:
        """列出某轮的逐例行（按表格行号排序，没有行号的按写入顺序）。"""

        rows = self._conn.execute(
            """
            SELECT * FROM case_row
            WHERE run_id = ?
            ORDER BY (row_no IS NULL), row_no, rowid
            """,
            (run_id,),
        ).fetchall()
        return tuple(self._to_stored_case(row) for row in rows)

    def count_cases(self, run_id: str) -> int:
        """某轮落了多少行 case。"""

        rows = self._conn.execute(
            "SELECT COUNT(*) AS n FROM case_row WHERE run_id = ?", (run_id,)
        ).fetchall()
        return int(rows[0]["n"])

    @staticmethod
    def _to_stored_case(row: sqlite3.Row) -> StoredCase:
        row_no = row["row_no"]
        if row_no is not None and not isinstance(row_no, int):
            row_no = None
        return StoredCase(
            run_id=row["run_id"],
            accession_no=row["accession_no"],
            status=row["status"],
            verdict=row["verdict"],
            tool_call_id=row["tool_call_id"],
            error_code=row["error_code"],
            row_no=row_no,
            updated_at=row["updated_at"],
            row=_row_from_json(row["row_json"]),
        )

    def recompute(self, run_id: str) -> tuple[CaseRow, ...]:
        """按 run_id **复算**逐例判定行。

        取的是库里落下的快照，不回读 CSV —— 引擎进程退出后仍能复算，
        「这轮到底判了什么」有唯一答案。
        """

        rows: list[CaseRow] = []
        for stored in self.list_cases(run_id):
            if stored.row is not None:
                rows.append(stored.row)
                continue
            rows.append(
                CaseRow(
                    accession_no=stored.accession_no,
                    verdict=stored.verdict or VERDICT_PENDING,
                    model_findings="",
                    failure_reason="",
                    tool_call_id=stored.tool_call_id,
                    needs_review=stored.status == CASE_STATUS_NEEDS_REVIEW,
                    normalize_source="",
                    row_no=stored.row_no,
                )
            )
        return tuple(rows)

    # ---- 断点续跑（任务 16） ----
    #
    # 「跑完了没」不看进程还在不在，只看**这一例在库里是什么状态**：
    # 终态（judged / failed / needs_review）= 有结论、续跑时跳过；
    # 非终态（pending / called / normalized）= 跑到一半、续跑时重来一遍。

    def list_completed_cases(self, run_id: str) -> tuple[StoredCase, ...]:
        """这一轮**已跑完**的例（终态）。"""

        return tuple(
            stored
            for stored in self.list_cases(run_id)
            if is_case_terminal(stored.status)
        )

    def list_incomplete_cases(self, run_id: str) -> tuple[StoredCase, ...]:
        """这一轮**跑到一半**的例（非终态）。"""

        return tuple(
            stored
            for stored in self.list_cases(run_id)
            if not is_case_terminal(stored.status)
        )

    def reset_incomplete_cases(
        self,
        run_id: str,
        *,
        reason: str = "断点续跑：复位到待处理",
        actor: str = ACTOR_ENGINE,
    ) -> tuple[str, ...]:
        """把跑到一半的例**复位**回「待处理」，返回被复位的检查号。

        **为什么复位而不是直接跳过**：中断时停在 ``called`` / ``normalized``
        的例**没有结论** —— 调了推理端点不等于判完了，归并了也不等于定了。
        把它当「跑完了」跳过，这一例就永远缺一段；复位回 ``pending``
        重走一遍，结论才完整。

        复位走状态机的**合法边**（``called → pending`` / ``normalized → pending``），
        并留迁移痕 —— 事后能看出「这一例中断时停在哪儿、什么时候复位的」。
        """

        reset: list[str] = []
        for stored in self.list_incomplete_cases(run_id):
            if stored.status == CASE_STATUS_PENDING:
                # 本来就在待处理，不用复位（``pending → pending`` 不是一次迁移）。
                continue
            self.transition_case(
                run_id,
                stored.accession_no,
                CASE_STATUS_PENDING,
                actor=actor,
                reason=reason,
            )
            reset.append(stored.accession_no)
        return tuple(reset)

    # ---- 调用底账索引 ----

    def has_tool_call(self, run_id: str, tool_call_id: str) -> bool:
        """这条调用是否已登记过。"""

        rows = self._conn.execute(
            "SELECT 1 FROM tool_call WHERE run_id = ? AND tool_call_id = ?",
            (run_id, tool_call_id),
        ).fetchall()
        return bool(rows)

    def count_tool_calls(self, run_id: str) -> int:
        """某轮登记了多少条调用。"""

        rows = self._conn.execute(
            "SELECT COUNT(*) AS n FROM tool_call WHERE run_id = ?", (run_id,)
        ).fetchall()
        return int(rows[0]["n"])

    def append_tool_call(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        name: str,
        args: Mapping[str, Any],
        content: str,
        timestamp: str | None = None,
    ) -> bool:
        """登记并追加一条调用底账。

        **同一个 ``tool_call_id`` 只落一次** —— 已登记过的直接返回 ``False``，
        不往 JSONL 里再追加一行（重复的底账比没有底账更难核对）。

        返回 ``True`` 表示这次是新记录。
        """

        if not tool_call_id.strip():
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID, detail="tool_call_id 不得为空"
            )
        recorder = ToolCallRecorder(self.run_dir(run_id))
        with self.write_transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO tool_call (run_id, tool_call_id, name, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, tool_call_id, name, timestamp or now_iso()),
            )
            if cursor.rowcount == 0:
                return False
            recorder.record(
                name=name,
                args=args,
                content=content,
                tool_call_id=tool_call_id,
                timestamp=timestamp,
            )
        return True

    # ---- 写回回执台账 ----

    def get_receipt(self, idempotency_key: str) -> WritebackReceipt | None:
        """按**幂等键**取这一轮的回执；没有就返回 ``None``。

        幂等键就是回执的天然主键：``sha256(run_id|model_id|dataset_ref)`` ——
        同一轮、同一款模型、同一批数据，永远是同一条回执。
        """

        key = (idempotency_key or "").strip()
        if not key:
            return None
        row = self._conn.execute(
            "SELECT * FROM writeback_receipt WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        return None if row is None else self._receipt_from_row(row)

    def get_receipt_by_run(self, run_id: str) -> WritebackReceipt | None:
        """按 ``run_id`` 取这一轮回执（最新一条）。

        同一轮理论上只有一条回执（幂等键里含 ``run_id``），
        但重试会覆盖同一行 —— 这里按更新时间取最后一条，语义上就是「当前状态」。
        """

        key = (run_id or "").strip()
        if not key:
            return None
        row = self._conn.execute(
            """
            SELECT * FROM writeback_receipt
            WHERE run_id = ?
            ORDER BY updated_at DESC, rowid DESC
            LIMIT 1
            """,
            (key,),
        ).fetchone()
        return None if row is None else self._receipt_from_row(row)

    def record_receipt(
        self,
        receipt: WritebackReceipt,
        *,
        actor: str = "",
        reason: str = "",
    ) -> WritebackReceipt:
        """按回执状态机落一条回执（**非法迁移直接拒**）。

        流程：读当前状态 → 校验这条边在不在 :data:`RECEIPT_TRANSITIONS` 表上 →
        覆盖写库 → 记一条迁移痕迹（``scope=receipt``）→ 写产物目录那份 JSON。

        **覆盖而不是追加**：回执记的是「当前状态」，同一把幂等键只有一条。
        追加的话，失败一次留一条、成功一次再留一条，两条都写着「这一轮的状态」，
        事后根本分不清哪条是真的 —— 与调用底账「同一 id 不重复登记」同一口径。
        """

        key = receipt.idempotency_key.strip()
        if not key:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID, detail="回执的幂等键不得为空"
            )
        current = self.get_receipt(key)
        current_status = current.status if current is not None else RECEIPT_STATUS_NONE
        assert_receipt_transition(current_status, receipt.status, run_id=receipt.run_id)

        now = now_iso()
        with self.write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO writeback_receipt
                    (idempotency_key, run_id, status, registration_id, submitted_at,
                     received_at, http_status, attempts, error_code, error_detail,
                     actor, summary_json, payload_sha256, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    run_id = excluded.run_id,
                    status = excluded.status,
                    registration_id = excluded.registration_id,
                    submitted_at = excluded.submitted_at,
                    received_at = excluded.received_at,
                    http_status = excluded.http_status,
                    attempts = excluded.attempts,
                    error_code = excluded.error_code,
                    error_detail = excluded.error_detail,
                    actor = excluded.actor,
                    summary_json = excluded.summary_json,
                    payload_sha256 = excluded.payload_sha256,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    receipt.run_id,
                    receipt.status,
                    receipt.registration_id,
                    receipt.submitted_at,
                    receipt.received_at,
                    receipt.http_status,
                    receipt.attempts,
                    receipt.error_code,
                    receipt.error_detail,
                    receipt.actor or actor,
                    json.dumps(dict(receipt.response_summary), ensure_ascii=False),
                    receipt.payload_sha256,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO state_transition
                    (run_id, scope, subject, from_state, to_state, actor, reason, at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.run_id,
                    "receipt",
                    receipt.run_id,
                    current_status,
                    receipt.status,
                    receipt.actor or actor,
                    reason,
                    now,
                ),
            )
            TransitionRecorder(self.run_dir(receipt.run_id)).record(
                StateTransition(
                    scope="receipt",
                    run_id=receipt.run_id,
                    subject=receipt.run_id,
                    from_state=current_status,
                    to_state=receipt.status,
                    actor=receipt.actor or actor,
                    reason=reason,
                    at=now,
                )
            )
        write_receipt_file(self.artifact_path(receipt.run_id, RECEIPT_FILENAME), receipt)
        return receipt

    def _receipt_from_row(self, row: sqlite3.Row) -> WritebackReceipt:
        """把库里的回执行还原成 :class:`WritebackReceipt`。"""

        try:
            summary = json.loads(row["summary_json"] or "{}")
        except json.JSONDecodeError:  # pragma: no cover - 防御性分支
            summary = {}
        http_status = row["http_status"]
        try:
            payload_sha256 = str(row["payload_sha256"] or "")
        except (IndexError, KeyError):  # pragma: no cover - 老库补列失败时的兜底
            payload_sha256 = ""
        return WritebackReceipt(
            run_id=row["run_id"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            registration_id=row["registration_id"],
            submitted_at=row["submitted_at"],
            received_at=row["received_at"],
            http_status=int(http_status) if http_status is not None else None,
            attempts=int(row["attempts"]),
            error_code=row["error_code"],
            error_detail=row["error_detail"],
            actor=row["actor"],
            response_summary=summary if isinstance(summary, dict) else {},
            payload_sha256=payload_sha256,
            updated_at=row["updated_at"],
        )

    # ---- 状态机 ----
    #
    # 状态推进**只有这一条路**：走表上的边、留迁移痕迹、被拒时说清为什么。
    # 引擎、装配、批量任务都调这里 —— 想要绕开状态机改状态，代码里没有口子。

    def transition_run(
        self,
        run_id: str,
        to_state: str,
        *,
        actor: str,
        reason: str = "",
    ) -> StoredRun:
        """把 run 推进到下一个状态。

        **非法迁移一律拒绝**，并说清「从哪到哪不行、合法去向是什么」：

        - 不在 :data:`RUN_TRANSITIONS` 表上的边（比如 ``evaluated`` 直跳
          ``submitted`` 跳过人工复核）→ 拒；
        - 推进到 ``reviewed`` / ``submitted`` 而触发方不是人工 → 拒
          （**引擎推不进去人工确认层**，这条写在代码里而不是口头约定里）；
        - 推进到当前已处的状态 → 拒（「已是该状态」不是一次迁移）。

        每次成功推进都留一条迁移记录（SQLite 一行 + ``transitions.jsonl`` 一行）：
        从哪到哪、谁推的、为什么推、什么时候推的。
        """

        key = run_id.strip()
        if not key:
            raise NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail="run_id 不得为空")
        current = self.get_run(key)
        if current is None:
            raise NoduleEvalError(
                ErrorCode.INVALID_STATE_TRANSITION,
                detail=f"run {key} 尚未落库，无法推进状态",
            )
        self._assert_transition_allowed(
            scope="run",
            subject=key,
            current=current.status,
            target=to_state,
            actor=actor,
            legal=RUN_TRANSITIONS,
            gated=HUMAN_GATED_RUN_STATES,
        )

        now = now_iso()
        transition = StateTransition(
            scope="run",
            run_id=key,
            subject=key,
            from_state=current.status,
            to_state=to_state,
            actor=actor,
            reason=reason,
            at=now,
        )
        with self.write_transaction() as conn:
            conn.execute(
                "UPDATE run SET status = ?, updated_at = ? WHERE run_id = ?",
                (to_state, now, key),
            )
            conn.execute(
                """
                INSERT INTO state_transition
                    (run_id, scope, subject, from_state, to_state, actor, reason, at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    "run",
                    key,
                    current.status,
                    to_state,
                    actor,
                    reason,
                    now,
                ),
            )
            TransitionRecorder(self.run_dir(key)).record(transition)
        return self.get_run(key)  # type: ignore[return-value]

    def transition_case(
        self,
        run_id: str,
        accession_no: str,
        to_state: str,
        *,
        actor: str,
        reason: str = "",
    ) -> StoredCase:
        """把逐例的状态推进到下一个状态。

        规则与 :meth:`transition_run` 同源：不在 :data:`CASE_TRANSITIONS`
        表上的边一律拒（如 ``pending`` 直跳 ``judged`` —— 没调推理端点、
        没归并金标准就出判定，等于凭空造结论）。
        """

        run_key = run_id.strip()
        acc = accession_no.strip()
        if not run_key:
            raise NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail="run_id 不得为空")
        if not acc:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID, detail="accession_no 不得为空"
            )
        current = self.get_case(run_key, acc)
        if current is None:
            raise NoduleEvalError(
                ErrorCode.INVALID_STATE_TRANSITION,
                detail=f"run {run_key} 里没有检查号 {acc} 这一例，无法推进状态",
            )
        self._assert_transition_allowed(
            scope="case",
            subject=acc,
            current=current.status,
            target=to_state,
            actor=actor,
            legal=CASE_TRANSITIONS,
            gated=frozenset(),
        )

        now = now_iso()
        transition = StateTransition(
            scope="case",
            run_id=run_key,
            subject=acc,
            from_state=current.status,
            to_state=to_state,
            actor=actor,
            reason=reason,
            at=now,
        )
        with self.write_transaction() as conn:
            conn.execute(
                """
                UPDATE case_row SET status = ?, updated_at = ?
                WHERE run_id = ? AND accession_no = ?
                """,
                (to_state, now, run_key, acc),
            )
            conn.execute(
                """
                INSERT INTO state_transition
                    (run_id, scope, subject, from_state, to_state, actor, reason, at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_key,
                    "case",
                    acc,
                    current.status,
                    to_state,
                    actor,
                    reason,
                    now,
                ),
            )
            TransitionRecorder(self.run_dir(run_key)).record(transition)
        return self.get_case(run_key, acc)  # type: ignore[return-value]

    def review_run(
        self,
        run_id: str,
        *,
        actor: str = ACTOR_HUMAN_REVIEW,
        reason: str = "人工复核完成",
    ) -> StoredRun:
        """人工复核：``evaluated → reviewed``。

        这是**唯一**能把 run 推进到「已复核」的口子，且只有人工触发方才算数。
        """

        return self.transition_run(
            run_id, RUN_STATUS_REVIEWED, actor=actor, reason=reason
        )

    def submit_run(
        self,
        run_id: str,
        *,
        actor: str = ACTOR_HUMAN_SUBMIT,
        reason: str = "人工确认提交",
    ) -> StoredRun:
        """人工确认提交：``reviewed → submitted``。

        只有人工点「确认提交」才走这条路 —— 写回科研平台前的最后一道闸。
        """

        return self.transition_run(
            run_id, RUN_STATUS_SUBMITTED, actor=actor, reason=reason
        )

    def list_transitions(
        self,
        run_id: str,
        *,
        scope: str | None = None,
        subject: str | None = None,
    ) -> tuple[StateTransition, ...]:
        """列出某一轮的迁移记录（按发生顺序）。

        ``scope`` 过滤 ``run`` / ``case``；``subject`` 过滤具体对象
        （run 作用域下是 ``run_id``，case 作用域下是 ``accession_no``）。
        """

        clauses = ["run_id = ?"]
        params: list[Any] = [run_id.strip()]
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        if subject:
            clauses.append("subject = ?")
            params.append(subject)
        rows = self._conn.execute(
            f"""
            SELECT * FROM state_transition
            WHERE {' AND '.join(clauses)}
            ORDER BY id
            """,
            tuple(params),
        ).fetchall()
        return tuple(
            StateTransition(
                scope=row["scope"],
                run_id=row["run_id"],
                subject=row["subject"],
                from_state=row["from_state"],
                to_state=row["to_state"],
                actor=row["actor"],
                reason=row["reason"],
                at=row["at"],
            )
            for row in rows
        )

    @staticmethod
    def _assert_transition_allowed(
        *,
        scope: str,
        subject: str,
        current: str,
        target: str,
        actor: str,
        legal: Mapping[str, tuple[str, ...]],
        gated: frozenset[str],
    ) -> None:
        """校验一条迁移是否合法；不合法就抛错并说清为什么。"""

        if current == target:
            raise NoduleEvalError(
                ErrorCode.INVALID_STATE_TRANSITION,
                detail=f"{scope} {subject} 已是 {current} 状态，不是一次迁移",
            )
        allowed = legal.get(current, ())
        if target not in allowed:
            hint = "、".join(allowed) if allowed else "无（已是终态）"
            raise NoduleEvalError(
                ErrorCode.INVALID_STATE_TRANSITION,
                detail=(
                    f"{scope} {subject} 不能从 {current} 跳到 {target}"
                    f"（{current} 的合法去向：{hint}）"
                ),
            )
        if target in gated and actor not in HUMAN_ACTORS:
            raise NoduleEvalError(
                ErrorCode.INVALID_STATE_TRANSITION,
                detail=(
                    f"{scope} {subject} 推进到 {target} 只能由人工复核 / 确认提交触发"
                    f"（当前触发方：{actor}）"
                ),
            )

    # ---- 产物目录 ----

    def run_dir(self, run_id: str) -> Path:
        """某一轮的产物目录（不存在就建）。"""

        target = self.runs_dir / run_id
        target.mkdir(parents=True, exist_ok=True)
        return target

    def artifact_path(self, run_id: str, filename: str) -> Path:
        """某一轮某个产物的路径（**不**创建目录）。"""

        return self.runs_dir / run_id / filename

    def write_case_rows(self, run_id: str, rows: Sequence[CaseRow]) -> Path:
        """写逐例明细 CSV 到本轮产物目录。"""

        return write_case_rows_csv(
            self.artifact_path(run_id, CASE_ROWS_FILENAME), rows
        )

    def read_case_rows(self, run_id: str) -> tuple[dict[str, Any], ...]:
        """读回本轮逐例明细 CSV。"""

        return read_case_rows_csv(self.artifact_path(run_id, CASE_ROWS_FILENAME))

    def write_json_artifact(self, run_id: str, filename: str, payload: Any) -> Path:
        """写一个 JSON 产物到本轮产物目录。"""

        return write_json_file(self.artifact_path(run_id, filename), payload)

    def read_json_artifact(self, run_id: str, filename: str) -> Any:
        """读一个 JSON 产物。"""

        return read_json_file(self.artifact_path(run_id, filename))

    def list_artifacts(self, run_id: str) -> tuple[str, ...]:
        """本轮产物目录里实际落了的文件。"""

        target = self.runs_dir / run_id
        if not target.is_dir():
            return ()
        return tuple(sorted(p.name for p in target.iterdir() if p.is_file()))

    # ---- 诊断 ----

    def journal_mode(self) -> str:
        """当前的日志模式（应为 ``wal``）。"""

        rows = self._conn.execute("PRAGMA journal_mode").fetchall()
        return str(rows[0][0]).lower()

    def busy_timeout(self) -> int:
        """当前的锁等待上限（毫秒）。"""

        rows = self._conn.execute("PRAGMA busy_timeout").fetchall()
        return int(rows[0][0])
