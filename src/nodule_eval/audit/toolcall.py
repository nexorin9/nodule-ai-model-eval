"""调用底账：逐次出站调用落 JSONL 一行。

**源产品证据**：参考实现的 ``Agent._save_tool_calls``
（``github_ref/MedRAX/medrax/agent/agent.py:168-193``）把每次工具调用的
``{tool_call_id, name, args, content, timestamp}`` 写进**一个** JSON 文件，
文件名带时间戳（``tool_calls_{timestamp}.json``），整个文件在一次运行结束时落盘。

一次跑两百例、且要按 id 反查单例（「这例凭什么算漏判」）时，「一次运行一个 JSON 文件」
做不到 —— 只能整体读、无法按行定位、并发写还会互相覆盖。

所以这里**复用字段语义、改写落盘形态**：改为 ``runs/<run_id>/tool_calls.jsonl``
逐行追加，每行一次调用，行级可反查、行级损坏可跳过。

字段语义沿用源产品：``tool_call_id`` 对应源实现的 ``tool_call_id``（本次由本产品自己生成，
因为本产品的调用不是模型发起的），``name`` 是被调用的动作名，``args`` 是调用入参，
``content`` 是调用返回，``timestamp`` 是发生时刻。

**任务 31 补的三件事**（源产品都没有）：

1. **并发写不交错** —— 逐例 fan-out 是多线程跑的（任务 15 的并发闸门），
   多个线程同时往同一个底账追加，行与行不能互相咬合。这里用
   **文件锁 + 单次原子写**（锁内一次 ``write`` 写完整行）解决，
   Windows 用 ``msvcrt.locking``、POSIX 用 ``fcntl.flock``，院内 PC 两边都能用。
2. **单行损坏可跳过** —— 底账是唯一能说清「这例凭什么算漏判」的东西，
   一行坏数据不能带塌整份底账；读回时坏行跳过并**计数上报**（不静默丢）。
3. **id 规则写死** —— ``tool_call_id`` 全局唯一、**同一例重跑生成新 id**，
   旧 id 不复用、旧产物不被新调用覆盖。
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..contracts import CaseRow
from ..errors import ErrorCode, NoduleEvalError

#: 底账文件名单。
TOOL_CALLS_FILENAME = "tool_calls.jsonl"

#: 底账一行的**必填字段**（源产品 ``ToolCallLog`` 的同一组）。
TOOL_CALL_FIELDS = ("tool_call_id", "name", "args", "content", "timestamp")

#: 调用入参的**必填字段**：缺任何一项，这行底账就没法复现那次调用。
CALL_ARG_FIELDS = ("accession_no", "study_uid", "image_ref", "model_id", "model_version")


def new_tool_call_id() -> str:
    """生成一次调用的唯一标识。

    全局唯一；**同一例重跑生成新 id**，不复用旧 id ——
    旧产物不会被新调用覆盖，反查时也能分清是哪一轮的调用。

    规则（写死在这里，别处不另起一套）：

    * 形如 ``tc_<32 位十六进制>``，前固定，便于在表格里肉眼分辨；
    * **不编码业务信息** —— 检查号、模型名这些都在 ``args`` 里，
      id 只做唯一键，免得改口径时 id 跟着变、底账对不上；
    * **不复用**：重跑、续跑、换版本目录，各生成各的 id。
    """

    return f"tc_{uuid.uuid4().hex}"


def now_iso() -> str:
    """统一的时刻格式（本地时区，秒级）。"""

    return datetime.now().isoformat(timespec="seconds")


def build_call_name(model_id: str, action: str = "detect") -> str:
    """底账的 ``name``：**被评模型标识 + 动作名**。

    例：``demo-model.detect``。看一行底账就知道「哪款模型、干的哪件事」，
    不必再翻别处的对照表。模型标识或动作名为空时报契约错，
    不拼出一个 ``.detect`` 这种半截名字。
    """

    model = (model_id or "").strip()
    verb = (action or "").strip()
    if not model:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID, detail="调用底账的 name 缺少模型标识（model_id 为空）"
        )
    if not verb:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID, detail="调用底账的 name 缺少动作名（action 为空）"
        )
    return f"{model}.{verb}"


def build_call_args(
    *,
    accession_no: str,
    study_uid: str,
    image_ref: str | None,
    model_id: str,
    model_version: str,
) -> dict[str, Any]:
    """底账的 ``args``：一次调用真正发出去的入参。

    **固定五项、顺序固定**：``accession_no``（哪一例）、``study_uid`` /
    ``image_ref``（推的是哪张片子）、``model_id`` / ``model_version``
    （哪款模型、哪个版本）。字段集合写死在这里 —— 别处不再各拼各的，
    免得同一款模型在不同路径下落出两种入参形状，反查时对不上。

    取不到的值记成**空串**而不是 ``null``：底账是给人看的一行字，
    「这次调用没带图像引用」写成空字段，比 ``null`` 少一层解读。
    """

    return {
        "accession_no": (accession_no or "").strip(),
        "study_uid": (study_uid or "").strip(),
        "image_ref": (image_ref or "").strip(),
        "model_id": (model_id or "").strip(),
        "model_version": (model_version or "").strip(),
    }


def missing_call_arg_fields(args: Mapping[str, Any]) -> list[str]:
    """列出底账入参里没填上的字段（体检用；缺项不拦写入，只让人看得见）。"""

    missing: list[str] = []
    for key in CALL_ARG_FIELDS:
        value = args.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(key)
    return missing


# --------------------------------------------------------------------------
# 并发写：文件锁 + 单次原子写
# --------------------------------------------------------------------------


def _lock_backend() -> str:
    """探测本机可用的文件锁实现。"""

    if os.name == "nt":  # pragma: no cover - 院内 Windows PC 走这一支
        try:
            import msvcrt  # noqa: F401

            return "msvcrt"
        except ImportError:  # pragma: no cover
            return "none"
    try:
        import fcntl  # noqa: F401

        return "fcntl"
    except ImportError:  # pragma: no cover - 非常规平台
        return "none"


@contextmanager
def _exclusive_lock(handle: Any) -> Iterator[None]:
    """在 ``handle`` 上取一把跨进程 / 跨线程的排他锁。

    * POSIX：``fcntl.flock(fd, LOCK_EX)``；
    * Windows：``msvcrt.locking(fd, LK_LOCK, 1)``，锁文件首字节 ——
      所有写者都用同一约定，锁区间不与追加位置重叠，互斥成立；
    * 两边都拿不到时退化为「不加锁 + 单次原子写」：
      ``O_APPEND`` 下单次 ``write`` 不会互相覆盖，最多行数不齐，
      但**不会出现半行咬合**。
    """

    backend = _lock_backend()
    fd = handle.fileno()
    if backend == "fcntl":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if backend == "msvcrt":  # pragma: no cover - 院内 Windows PC 走这一支
        import msvcrt

        handle.seek(0)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    yield  # pragma: no cover - 非常规平台


@dataclass(frozen=True)
class ToolCallRecord:
    """底账里的一行。"""

    tool_call_id: str
    name: str
    args: Mapping[str, Any]
    content: str
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "args": dict(self.args),
            "content": self.content,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ToolCallRecord":
        """从一行 JSON 还原成记录对象（缺必填字段的行视为损坏）。"""

        missing = [key for key in TOOL_CALL_FIELDS if key not in payload]
        if missing:
            raise KeyError(f"底账行缺字段：{'、'.join(missing)}")
        return cls(
            tool_call_id=str(payload["tool_call_id"]),
            name=str(payload["name"]),
            args=dict(payload["args"] or {}),
            content=str(payload["content"]),
            timestamp=str(payload["timestamp"]),
        )


@dataclass(frozen=True)
class ToolCallScan:
    """读回底账的体检结果。

    ``corrupt_lines`` 单列出来 —— 底账是唯一能说清「这例凭什么算漏判」的东西，
    坏行必须**看得见**，不能读完悄悄少几行还让人以为账是全的。
    """

    records: list[dict[str, Any]] = field(default_factory=list)
    total_lines: int = 0
    corrupt_lines: int = 0
    blank_lines: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": list(self.records),
            "total_lines": self.total_lines,
            "corrupt_lines": self.corrupt_lines,
            "blank_lines": self.blank_lines,
        }


class ToolCallRecorder:
    """把出站调用追加到 ``runs/<run_id>/tool_calls.jsonl``。"""

    def __init__(self, run_dir: Path | str) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / TOOL_CALLS_FILENAME

    def record(
        self,
        *,
        name: str,
        args: Mapping[str, Any],
        content: str,
        tool_call_id: str | None = None,
        timestamp: str | None = None,
    ) -> ToolCallRecord:
        """追加一行并立即落盘（flush + fsync），保证中断后已写的行不丢。

        **并发安全**：持文件锁、锁内**一次** ``write`` 写完整行 ——
        多个线程同时追加时，行与行不会互相咬合（不会出现半行 + 半行拼成一行）。
        """

        record = ToolCallRecord(
            tool_call_id=tool_call_id or new_tool_call_id(),
            name=name,
            args=dict(args),
            content=content,
            timestamp=timestamp or now_iso(),
        )
        payload = json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
        with open(self.path, "ab") as handle:
            with _exclusive_lock(handle):
                handle.write(payload.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        return record

    def scan(self) -> ToolCallScan:
        """读回底账并体检（坏行跳过但计数上报）。"""

        if not self.path.is_file():
            return ToolCallScan()
        records: list[dict[str, Any]] = []
        total = 0
        corrupt = 0
        blank = 0
        with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                total += 1
                stripped = raw.strip()
                if not stripped:
                    blank += 1
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    corrupt += 1
                    continue
                if not isinstance(payload, dict):
                    corrupt += 1
                    continue
                try:
                    ToolCallRecord.from_dict(payload)
                except KeyError:
                    corrupt += 1
                    continue
                records.append(payload)
        return ToolCallScan(
            records=records,
            total_lines=total,
            corrupt_lines=corrupt,
            blank_lines=blank,
        )

    def read_all(self) -> list[dict[str, Any]]:
        """读回全部底账行（损坏行跳过，不让一行坏数据毁掉整份底账）。"""

        return self.scan().records

    def find(self, tool_call_id: str) -> dict[str, Any] | None:
        """按 id 找一条底账；找不到返回 ``None``（由调用方决定怎么报错）。"""

        wanted = (tool_call_id or "").strip()
        if not wanted:
            return None
        for record in self.read_all():
            if str(record.get("tool_call_id") or "") == wanted:
                return record
        return None


# --------------------------------------------------------------------------
# 按 id 反查（逐例行 ↔ 调用底账）
# --------------------------------------------------------------------------


def index_tool_calls(records: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 ``tool_call_id`` 给底账建索引。

    ``tool_call_id`` 全局唯一（``new_tool_call_id`` 生成），
    索引里的每一条都能原样取回那次调用的入参与返回。
    """

    index: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        call_id = str(record.get("tool_call_id") or "").strip()
        if not call_id:
            continue
        index[call_id] = dict(record)
    return index


@dataclass(frozen=True)
class JoinedCase:
    """一行逐例判定 + 它对应的那次调用。"""

    row: CaseRow
    tool_call: dict[str, Any] | None

    @property
    def joined(self) -> bool:
        """这一行是否在底账里找到了对应的调用。"""

        return self.tool_call is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession_no": self.row.accession_no,
            "verdict": self.row.verdict,
            "tool_call_id": self.row.tool_call_id,
            "joined": self.joined,
            "tool_call": self.tool_call,
        }


def join_case_rows(
    rows: Sequence[CaseRow],
    records: Iterable[Mapping[str, Any]],
) -> list[JoinedCase]:
    """把逐例判定行与调用底账按 ``tool_call_id`` 关联起来。

    **对齐口径**：表格「调用记录ID」列填的就是这个 id。
    复核的人问「这一例凭什么算漏判」，靠的就是这条链路 ——
    从结论行跳回那次调用的入参与返回。关联不上就是产物不完整，
    ``joined`` 为 ``False``，由调用方决定怎么报。
    """

    index = index_tool_calls(records)
    joined: list[JoinedCase] = []
    for row in rows:
        call_id = (row.tool_call_id or "").strip()
        joined.append(JoinedCase(row=row, tool_call=index.get(call_id) if call_id else None))
    return joined
