"""写回回执的状态机：这一轮**到底有没有真的登记成功**。

**为什么单一层**：写回科研平台是本产品唯一会**改变院内业务状态**的动作，
而「请求发出去了」与「登记生效了」是**两件事**。中间那几种情形最容易出事：

- 超时 / 断连 —— 对端**可能已经登记了**，只是回执没回来。此时若当成
  「没登记」，再点一次「确认提交」就可能多出一条登记；
- 5xx —— 对端明确报错，但**也可能已经写了一半**；
- 回执缺登记号 —— 压根不能算「已生效」。

所以本地要有一张**回执台账**，把「本地知道的状态」与「对端的状态」分开记：

    none ──→ pending ──→ registered
      │          │            │
      └──────────┴──→ failed  │（registered 是对端的事实，本地改不动它）

- ``none``：还没提交过；
- ``pending``：**已发出、结果未知**（超时 / 断连）—— 这是最要紧的一格，
  它意味着「不能当成没登记」，下一步该做的是**问对端**，不是再发一次；
- ``registered``：对端已登记，拿到了登记号；
- ``failed``：对端明确没登记（4xx / 5xx 重试耗尽），可以重试。

**合法迁移边写死在 :data:`RECEIPT_TRANSITIONS`** —— 与 run / case 状态机
同一套做法（边先声明，不在边上的迁移一律拒）。其中一条最要紧：

    registered ──→ registered（只允许重放同一张回执）

已登记**不能退回**成 failed / pending —— 登记是**对端的事实**，
把本地状态改回去并不会把对端那条记录删掉，只会让两边对不上。

**落盘形态**：SQLite 一张 ``writeback_receipt`` 表（真源）+ 产物目录里一份
``runs/<run_id>/writeback_receipt.json``（人可读）。回执状态机不走 run / case
那套状态机，但**迁移留痕共用同一张 ``state_transition`` 表**（``scope=receipt``），
所以「这一轮什么时候变成已登记的、是谁推的」在 ``transitions.jsonl`` 里查得到。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .errors import ErrorCode, NoduleEvalError

__all__ = [
    "RECEIPT_FILENAME",
    "RECEIPT_STATUS_FAILED",
    "RECEIPT_STATUS_NONE",
    "RECEIPT_STATUS_PENDING",
    "RECEIPT_STATUS_REGISTERED",
    "RECEIPT_TRANSITIONS",
    "WritebackReceipt",
    "is_legal_receipt_transition",
    "receipt_transition_targets",
    "summarize_response",
    "write_receipt_file",
]


#: 回执文件名（落在 ``runs/<run_id>/`` 下）。
RECEIPT_FILENAME = "writeback_receipt.json"

#: 回执状态：本地没有回执（还没提交过）。
RECEIPT_STATUS_NONE = "none"
#: 回执状态：已发出、**结果未知**（超时 / 断连）—— 不能当成没登记。
RECEIPT_STATUS_PENDING = "pending"
#: 回执状态：对端已登记（拿到了登记号）。
RECEIPT_STATUS_REGISTERED = "registered"
#: 回执状态：写回失败，对端明确没登记，可以重试。
RECEIPT_STATUS_FAILED = "failed"

#: 回执状态机的合法去向。
RECEIPT_TRANSITIONS: dict[str, tuple[str, ...]] = {
    RECEIPT_STATUS_NONE: (
        RECEIPT_STATUS_PENDING,
        RECEIPT_STATUS_REGISTERED,
        RECEIPT_STATUS_FAILED,
    ),
    RECEIPT_STATUS_PENDING: (
        RECEIPT_STATUS_PENDING,
        RECEIPT_STATUS_REGISTERED,
        RECEIPT_STATUS_FAILED,
    ),
    RECEIPT_STATUS_FAILED: (
        RECEIPT_STATUS_PENDING,
        RECEIPT_STATUS_REGISTERED,
        RECEIPT_STATUS_FAILED,
    ),
    # 已登记是**对端的事实**：本地状态改不动它 —— 只允许重放同一张回执。
    RECEIPT_STATUS_REGISTERED: (RECEIPT_STATUS_REGISTERED,),
}


def receipt_transition_targets(status: str) -> tuple[str, ...]:
    """某个回执状态的合法去向；未知状态返回空元组。"""

    return RECEIPT_TRANSITIONS.get(status, ())


def is_legal_receipt_transition(current: str, target: str) -> bool:
    """这条回执迁移在不在表上。"""

    return target in RECEIPT_TRANSITIONS.get(current, ())


def assert_receipt_transition(current: str, target: str, *, run_id: str = "") -> None:
    """不在合法边上的回执迁移直接拒（与 run / case 状态机同一口径）。"""

    if is_legal_receipt_transition(current, target):
        return
    targets = receipt_transition_targets(current)
    where = f"（run {run_id}）" if run_id else ""
    raise NoduleEvalError(
        ErrorCode.INVALID_STATE_TRANSITION,
        detail=(
            f"写回回执不能从「{current or '未知'}」跳到「{target}」{where}"
            f"（{current} 的合法去向：{'、'.join(targets) or '无'}）"
        ),
        current_state=current,
        target_state=target,
    )


def summarize_response(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """把对端回执**摘成一份摘要**。

    只留科研平台回执规范里声明的那几项（登记号 / 状态 / 收到时刻）——
    对端多给什么字段都不往台账里抄：这份摘要是要进产物目录给人看的，
    里面不该出现任何患者可识别明细。
    """

    if not isinstance(payload, Mapping):
        return {}
    summary: dict[str, Any] = {}
    for key in ("registration_id", "run_id", "status", "received_at"):
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            summary[key] = value
    return summary


@dataclass(frozen=True)
class WritebackReceipt:
    """一条写回回执的**本地台账**。

    ``status``
        本地状态（见 :data:`RECEIPT_STATUS_*`）。
    ``registration_id``
        对端登记号 —— 「已生效事实」的凭据。
    ``idempotency_key``
        本次提交用的幂等键；回执与键一起留痕，事后能证明
        「重复提交没有产生第二条登记」。
    ``submitted_at`` / ``received_at``
        本地提交时刻 / 对端收到时刻。
    ``http_status`` / ``attempts``
        最后一次出站的 HTTP 状态码与**实际尝试次数**（含退避重试）——
        「这轮写回调了几次」是飞检常问的一句。
    ``error_code`` / ``error_detail``
        失败时的错误码与中文说明（与错误码表同一套文案）。
    ``response_summary``
        对端响应的**摘要**（只含回执规范里声明的那几项）。
    ``payload_sha256``
        本次提交体的**内容摘要** —— 与写回审计底账里留的是同一个值。
        幂等回放时把它一并回放出去，回执台账与审计底账两处对得上，
        「发出去的那一版到底是什么」这件事只有一个答案。
    """

    run_id: str
    idempotency_key: str
    status: str = RECEIPT_STATUS_NONE
    registration_id: str = ""
    submitted_at: str = ""
    received_at: str = ""
    http_status: int | None = None
    attempts: int = 0
    error_code: str = ""
    error_detail: str = ""
    actor: str = ""
    response_summary: Mapping[str, Any] = field(default_factory=dict)
    payload_sha256: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "registration_id": self.registration_id,
            "submitted_at": self.submitted_at,
            "received_at": self.received_at,
            "http_status": self.http_status,
            "attempts": self.attempts,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "actor": self.actor,
            "response_summary": dict(self.response_summary),
            "payload_sha256": self.payload_sha256,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WritebackReceipt":
        """从台账行还原一条回执（缺字段按缺省填，不报错）。"""

        summary = payload.get("response_summary")
        http_status = payload.get("http_status")
        attempts = payload.get("attempts")
        return cls(
            run_id=str(payload.get("run_id") or ""),
            idempotency_key=str(payload.get("idempotency_key") or ""),
            status=str(payload.get("status") or RECEIPT_STATUS_NONE),
            registration_id=str(payload.get("registration_id") or ""),
            submitted_at=str(payload.get("submitted_at") or ""),
            received_at=str(payload.get("received_at") or ""),
            http_status=int(http_status) if isinstance(http_status, int) else None,
            attempts=int(attempts) if isinstance(attempts, int) else 0,
            error_code=str(payload.get("error_code") or ""),
            error_detail=str(payload.get("error_detail") or ""),
            actor=str(payload.get("actor") or ""),
            response_summary=dict(summary) if isinstance(summary, Mapping) else {},
            payload_sha256=str(payload.get("payload_sha256") or ""),
            updated_at=str(payload.get("updated_at") or ""),
        )


def write_receipt_file(path: Path | str, receipt: WritebackReceipt) -> Path:
    """把回执写进产物目录（人可读的那一份）。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(receipt.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target
