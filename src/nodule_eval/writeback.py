"""确认闸门：未经人工复核，绝不出一站调用。

产品里唯一会**改变院内业务状态**的动作，是把评估结论写回科研平台的评估登记。
这条动作只在两个条件**同时**成立时才允许发生：

1. 这一轮 run 已经被**人工复核**过（状态 ``reviewed``）；
2. 触发这次写回的**是人**（表格里那一下「确认提交」），不是引擎。

两个条件都写在闸门里 —— **闸门不过，一次出站调用都不发**。

**「压根没发」与「发了但被拒」是两回事**：后者已经在对端留下了痕迹
（哪怕被拒，也是对端日志里的一条记录，事后还得去解释）。所以闸门的位置
必须在出站客户端**之前**，而不是让对端来拒。

**闸门拒绝时不落任何写回产物**：这一轮没有发生过写回，产物目录里一眼就该看得出来
（没有 ``writeback_audit.jsonl``）。把「被拦下」也写成一条记录是审计上的另一个话题，
留给任务 38 的写回审计层统一处理。

**降级例照常能提交**（模型不可达退回规则基线的例，结论是确定的），
但提交体与汇总行都**带降级计数** —— 对端拿到的数字必须和总结论里写的那个一样，
否则两边各说各话，采购会上就解释不清了。

**幂等与回执（任务 37）**：闸门放行之后还有三件事，缺一件就会出事：

1. **重复提交只产生一条登记** —— 幂等键相同，本地已有「已登记」回执时
   **一次出站调用都不发**，直接把本地回执回放出去（表格里连点两下
   「确认提交」不会多出一条登记）；
2. **结果未知要去核对，不能当没发生** —— 超时 / 断连时对端**可能已经登记了**，
   本地回执记 ``pending``；下一次提交先**问对端**（GET 按幂等键查登记），
   查到了就收敛成「已登记」，查不到才重发；
3. **写回失败不推进状态** —— run 停在 ``reviewed``，错误码与状态码落进
   回执台账与写回底账，绝不标成「已提交」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapters.research_platform import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    EvaluationSubmission,
    RegistrationLookup,
    RegistrationReceipt,
    ResearchPlatformAdapter,
    assert_payload_within_limit,
    compute_idempotency_key,
    now_iso,
    payload_bytes,
    payload_digest,
    resolve_operator,
)
from .contracts import CaseRow
from .errors import ErrorCode, NoduleEvalError
from .metrics.compute import compute_metrics_from_rows
from .store import (
    ACTOR_HUMAN_SUBMIT,
    HUMAN_ACTORS,
    RUN_STATUS_REVIEWED,
    SUMMARY_FILENAME,
    RunStore,
    StoredRun,
)
from .writeback_receipt import (
    RECEIPT_STATUS_FAILED,
    RECEIPT_STATUS_PENDING,
    RECEIPT_STATUS_REGISTERED,
    WritebackReceipt,
    summarize_response,
)

#: 闸门要求的 run 状态 —— 只有「已人工复核」这一层才允许出站写回。
GATE_REQUIRED_STATUS = RUN_STATUS_REVIEWED

#: 写回底账文件名（落在 ``runs/<run_id>/`` 下）。
WRITEBACK_AUDIT_FILENAME = "writeback_audit.jsonl"

#: 写回体里**逐例行**带出去的字段。
#:
#: **不含金标准原文** —— 原文可能带姓名 / 年龄 / 住院号，
#: 写出去等于把患者明细带出评估表（任务 34 的脱敏红线）。
#:
#: ``judge_basis`` 是**判定依据摘要**（存在性 / 部位 / 尺寸 / 密度四层结论
#: 的一行文本）—— 对端复核「这例凭什么算漏判」时不必再翻底稿。
SUBMITTED_CASE_FIELDS: tuple[str, ...] = (
    "accession_no",
    "verdict",
    "model_findings",
    "failure_reason",
    "judge_basis",
    "tool_call_id",
    "normalize_source",
    "rule_degraded",
    "needs_review",
)


@dataclass(frozen=True)
class GateDecision:
    """闸门判定结果。

    ``allowed``
        能不能写回。``False`` 时**一次出站调用都不该发生**。
    ``current_status``
        这一轮当时的 run 状态 —— 报错时指名给人看，别让人猜是哪一步没过。
    ``reason``
        中文说明（**为什么**没放行）。
    """

    allowed: bool
    current_status: str
    required_status: str = GATE_REQUIRED_STATUS
    actor: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "current_status": self.current_status,
            "required_status": self.required_status,
            "actor": self.actor,
            "reason": self.reason,
        }


def evaluate_gate(
    run_status: str,
    *,
    actor: str = ACTOR_HUMAN_SUBMIT,
) -> GateDecision:
    """判断这一轮能不能写回（**只判，不出站**）。

    两条硬条件，**都要满足**：

    1. run 状态已是 ``reviewed`` —— 人工复核过；
    2. 触发方是人 —— 表格里那一下「确认提交」。

    引擎（``actor=engine``）连闸门都进不去 —— 不是「进去了被拒」，
    是压根不许触发这件事。模型输出与规则降级结果都无权写回院内登记。
    """

    if not actor_allowed(actor):
        return actor_rejection(run_status, actor)
    if run_status != GATE_REQUIRED_STATUS:
        return GateDecision(
            allowed=False,
            current_status=run_status,
            actor=actor,
            reason=(
                f"本轮 run 状态是「{run_status or '未知'}」，"
                f"须先人工复核到「{GATE_REQUIRED_STATUS}」才能写回"
            ),
        )
    return GateDecision(
        allowed=True,
        current_status=run_status,
        actor=actor,
        reason=f"已人工复核（{GATE_REQUIRED_STATUS}），允许写回",
    )


def actor_allowed(actor: str) -> bool:
    """这个触发方能不能触发写回相关动作（只有人工能）。"""

    return actor in HUMAN_ACTORS


def actor_rejection(run_status: str, actor: str) -> GateDecision:
    """触发方不合法时的判定结果（引擎不得自行写回院内登记）。"""

    return GateDecision(
        allowed=False,
        current_status=run_status,
        actor=actor,
        reason=(
            f"写回只能由人工触发（当前触发方：{actor or '未标明'}）；"
            "引擎不得自行把评估结论写进院内登记"
        ),
    )


def assert_gate_open(
    run_status: str,
    *,
    actor: str = ACTOR_HUMAN_SUBMIT,
) -> GateDecision:
    """闸门不通过就抛错 —— **抛错点在出站之前**。

    抛的是 :class:`NoduleEvalError`（错误码 ``WRITEBACK_NOT_REVIEWED``），
    文案只从 :mod:`nodule_eval.errors` 的错误码表取。
    """

    decision = evaluate_gate(run_status, actor=actor)
    if not decision.allowed:
        raise NoduleEvalError(
            ErrorCode.WRITEBACK_NOT_REVIEWED,
            detail=decision.reason,
            current_status=decision.current_status,
            required_status=decision.required_status,
            actor=decision.actor,
        )
    return decision


def build_submission(
    run: StoredRun,
    rows: Sequence[CaseRow],
    *,
    submitted_by: str = "",
    submitted_at: str = "",
    summary_payload: Mapping[str, Any] | None = None,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> EvaluationSubmission:
    """组装登记提交体 —— **口径真源在库里，不在文档里**。

    ``summary_payload``
        本轮的 ``summary.json`` 内容。给了就**对一次账**：它写的降级例数
        必须与逐例明细数出来的降级例数一致 —— 对端拿到的数字与总结论里写的
        那个不一样，两边各说各话，采购会上就解释不清了。对不上直接报
        ``CONTRACT_INVALID``，**不写回**。
    ``max_payload_bytes``
        对端能接受的请求体上限。**超限明确报错，不截断** ——
        截断掉的那几条等于「这一轮评估了 N 例、只登记了 M 例」。
        校验点在**出站客户端之前**：超限这一轮一次请求都不发。
    """

    metrics = compute_metrics_from_rows(rows)
    metrics_payload: dict[str, Any] = dict(metrics.to_dict())
    degrade_note = ""

    if not rows:
        # 空评估写回去是一条**没有意义的登记** —— 对端收了它，事后看就是
        # 「这轮评估了 0 例」。宁可在这里拦下，也不往院内系统里写垃圾。
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail="这一轮没有任何逐例判定行 —— 空评估不写回",
        )

    if summary_payload:
        counts = summary_payload.get("counts")
        if isinstance(counts, Mapping):
            declared = counts.get("rule_degraded")
            if isinstance(declared, int) and declared != metrics.rule_degraded_count:
                raise NoduleEvalError(
                    ErrorCode.CONTRACT_INVALID,
                    detail=(
                        f"规则降级数对不上：summary.json 写 {declared} 例，"
                        f"逐例明细数出 {metrics.rule_degraded_count} 例"
                    ),
                )
        note = summary_payload.get("degrade_note")
        if isinstance(note, str):
            degrade_note = note
    if degrade_note:
        metrics_payload["degrade_note"] = degrade_note

    dataset_ref = (run.dataset_ref or "").strip()
    if not dataset_ref:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                "这一轮没有数据集引用（dataset_ref）——幂等键要靠它，"
                "缺了写回去对端就没法去重"
            ),
        )

    case_rows: list[dict[str, Any]] = []
    for row in rows:
        case_rows.append(
            {
                "accession_no": row.accession_no,
                "verdict": row.verdict,
                "model_findings": row.model_findings,
                "failure_reason": row.failure_reason,
                "judge_basis": row.judge_basis,
                "tool_call_id": row.tool_call_id,
                "normalize_source": row.normalize_source,
                "rule_degraded": bool(row.rule_degraded),
                "needs_review": bool(row.needs_review),
            }
        )

    submission = EvaluationSubmission(
        run_id=run.run_id,
        model_id=run.model_id,
        model_version=run.model_version,
        dataset_ref=dataset_ref,
        metrics=metrics_payload,
        case_rows=case_rows,
        submitted_by=submitted_by or resolve_operator(),
        submitted_at=submitted_at or now_iso(),
    )

    # 大小保护 —— **在出站之前**。超限就报错，不截断、不发请求。
    assert_payload_within_limit(
        submission.to_dict(),
        max_bytes=max_payload_bytes,
        run_id=run.run_id,
        case_count=len(case_rows),
    )

    return submission


@dataclass(frozen=True)
class WritebackOutcome:
    """一次写回的结果（闸门判定 + 提交体 + 回执）。

    ``submission``
        这次发出去的提交体。**幂等回放时为 ``None``** —— 回放根本没发过
        请求，没有什么「发出去的提交体」。
    ``replayed``
        这次是不是**幂等回放**（本地已有已登记回执，一次出站调用都没发）。
        调用方要靠它区分「这轮真的写上去了」与「这轮本来就已经写上去了」。
    """

    run_id: str
    decision: GateDecision
    submission: EvaluationSubmission | None = None
    receipt: RegistrationReceipt | None = None
    submitted_by: str = ""
    submitted_at: str = ""
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "gate": self.decision.to_dict(),
            "submission": self.submission.to_dict() if self.submission else None,
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "submitted_by": self.submitted_by,
            "submitted_at": self.submitted_at,
            "replayed": self.replayed,
        }


@dataclass(frozen=True)
class ReceiptVerification:
    """回执核对结果：本地记的与对端实际的**对不对得上**。

    ``matched``
        两边的登记号一致 —— 登记**确实生效**了。
    ``reason``
        中文说明（为什么对上 / 为什么对不上）。
    """

    run_id: str
    local_status: str
    registration_id: str
    idempotency_key: str
    remote_found: bool = False
    remote_registration_id: str = ""
    remote_status: str = ""
    http_status: int | None = None
    matched: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "local_status": self.local_status,
            "registration_id": self.registration_id,
            "idempotency_key": self.idempotency_key,
            "remote_found": self.remote_found,
            "remote_registration_id": self.remote_registration_id,
            "remote_status": self.remote_status,
            "http_status": self.http_status,
            "matched": self.matched,
            "reason": self.reason,
        }


def append_writeback_audit(path: Path | str, payload: Mapping[str, Any]) -> Path:
    """往写回底账里追加一行（JSONL）。

    底账记的是「**确实发生过**的写回」：出站成功、回执到手。
    闸门拒绝走的是另一条路 —— 压根没出站，也就不落这一行。
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(payload), ensure_ascii=False) + "\n")
    return target


class WritebackService:
    """写回科研平台评估登记的**闸门 + 提交**。

    参数
    ----
    config
        环境变量读出来的配置（凭据只在这里面）。
    store
        run store —— run 状态与逐例明细的权威来源。
    adapter
        科研平台出站客户端。缺省按配置造一个真客户端
        （**不回落本地替身**：地址只认 ``RESEARCH_PLATFORM_BASE_URL``）。

    **调用顺序是这一层全部意义所在**：

    1. **闸门** —— run 必须已 ``reviewed``，且触发方是人；
       不通过就在这里抛错，**一次出站调用都不发**；
    2. 组装提交体（口径取自库里，与 ``summary.json`` 对一次账）；
    3. 出站提交，拿回执；
    4. **先留痕再推进状态** —— 底账记下这次写回，然后 run 才推进到 ``submitted``；
       出站成功但状态推进失败时，底账里仍然看得见「确实发出去过」。
    """

    def __init__(
        self,
        config: Any,
        store: RunStore,
        *,
        adapter: ResearchPlatformAdapter | None = None,
        client: Any | None = None,
        base_url: str | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._adapter = (
            adapter
            if adapter is not None
            else ResearchPlatformAdapter(config, client=client, base_url=base_url)
        )

    # ---- 闸门 ----

    def evaluate(
        self,
        run_id: str,
        *,
        actor: str = ACTOR_HUMAN_SUBMIT,
    ) -> GateDecision:
        """只判闸门，**不出站**（给「检查一下能不能提交」这类用法）。"""

        run = self._require_run(run_id)
        return evaluate_gate(run.status, actor=actor)

    # ---- 提交 ----

    def submit(
        self,
        run_id: str,
        *,
        actor: str = ACTOR_HUMAN_SUBMIT,
        submitted_by: str = "",
        submitted_at: str = "",
    ) -> WritebackOutcome:
        """把这一轮的评估结论写回科研平台登记。

        三条不变量：

        1. **闸门不过 → 一次出站调用都不发**（抛 ``WRITEBACK_NOT_REVIEWED``）；
        2. **重复提交 → 对端只产生一条登记**（幂等回放，一次出站都不发）；
        3. **写回失败 → run 停在 ``reviewed``**，绝不标成「已提交」。
        """

        run = self._require_run(run_id)

        # 幂等键：三段（哪一轮 · 哪款模型 · 哪批数据）—— 对端按它去重，
        # 本地回执台账也按它当主键。
        key = compute_idempotency_key(run.run_id, run.model_id, run.dataset_ref)
        existing = self._store.get_receipt(key)

        # ① 幂等回放：本地台账里这一轮**已经登记过** —— 一次出站调用都不发。
        #    表格里连点两下「确认提交」到这儿就停了，对端不会多出一条登记。
        if existing is not None and existing.status == RECEIPT_STATUS_REGISTERED:
            self._assert_human_actor(actor, run.status)
            return self._replay(existing, run, actor=actor)

        # ② 结果未知（上次超时 / 断连）：**先去问对端**，别急着再发一次。
        #    对端可能已经登记了 —— 这正是「已发出 ≠ 已生效」那一步。
        if existing is not None and existing.status == RECEIPT_STATUS_PENDING:
            confirmed = self._confirm_remote(existing)
            if confirmed is not None:
                receipt = self._store.record_receipt(
                    WritebackReceipt(
                        run_id=run.run_id,
                        idempotency_key=key,
                        status=RECEIPT_STATUS_REGISTERED,
                        registration_id=confirmed.registration_id,
                        submitted_at=existing.submitted_at,
                        received_at=confirmed.received_at,
                        http_status=confirmed.http_status,
                        attempts=existing.attempts,
                        actor=actor,
                        response_summary=summarize_response(confirmed.raw),
                        # 收敛时**沿用当初那一版**的摘要（那次请求确实发出去过），
                        # 不重新算 —— 重新算的是当前内容，与发出去的那一版未必相同。
                        payload_sha256=existing.payload_sha256,
                    ),
                    actor=actor,
                    reason=(
                        "回执核对确认对端已登记"
                        f"（登记号 {confirmed.registration_id}），本地状态收敛为已登记"
                    ),
                )
                return self._replay(receipt, run, actor=actor, converged=True)
            # 对端查不到 → 继续走正常提交。幂等键保证这次提交
            # 在对端**不会产生第二条登记**。

        # ③ 闸门 —— **在出站之前**。不通过就抛错，这里一个请求都不发出去。
        decision = assert_gate_open(run.status, actor=actor)

        # ④ 组装提交体（口径取自库里，并与本轮 summary.json 对一次账）。
        #    超限就在这里报错 —— **一次出站调用都不发**。
        rows = self._store.recompute(run_id)
        submission = build_submission(
            run,
            rows,
            submitted_by=submitted_by,
            submitted_at=submitted_at,
            summary_payload=self._summary_payload(run_id),
            max_payload_bytes=self._max_payload_bytes(),
        )
        body = submission.to_dict()
        digest = payload_digest(body)
        size = payload_bytes(body)

        # ⑤ 出站。幂等键由 run_id / model_id / dataset_ref 三段算出，
        #    同键重复提交对端只登记一条。5xx 退避重试在会话层（最多 3 次），
        #    4xx 不重试。
        try:
            receipt = self._adapter.submit(submission)
        except NoduleEvalError as exc:
            # 失败也**要留痕**，但**不许推进状态**：没登记成功就不许说「已提交」。
            self._record_failure(run, key, submission, exc, actor=actor)
            raise

        # ⑥ 落回执（回执状态机：failed / pending → registered）。
        self._store.record_receipt(
            WritebackReceipt(
                run_id=run.run_id,
                idempotency_key=key,
                status=RECEIPT_STATUS_REGISTERED,
                registration_id=receipt.registration_id,
                submitted_at=submission.submitted_at,
                received_at=receipt.received_at,
                http_status=receipt.http_status,
                attempts=receipt.attempts,
                actor=actor,
                response_summary=summarize_response(receipt.raw),
                payload_sha256=digest,
            ),
            actor=actor,
            reason=f"写回成功，登记号 {receipt.registration_id}",
        )

        # ⑦ 先留痕，再推进状态 —— 出站成功但推进失败时，底账里仍看得见
        #    「确实发出去过、拿到了登记号」。
        #    底账记的是**内容**：请求体摘要（内容指纹）+ 大小 + 对端回执状态，
        #    事后把科研平台侧的登记导出来算一次摘要就能对上。
        audit_path = self._store.artifact_path(run_id, WRITEBACK_AUDIT_FILENAME)
        append_writeback_audit(
            audit_path,
            {
                "at": submission.submitted_at,
                "run_id": run_id,
                "actor": actor,
                "action": "submit",
                "gate": decision.to_dict(),
                "registration_id": receipt.registration_id,
                "idempotency_key": receipt.idempotency_key,
                "http_status": receipt.http_status,
                "attempts": receipt.attempts,
                "submitted_by": submission.submitted_by,
                "model_id": submission.model_id,
                "model_version": submission.model_version,
                "dataset_ref": submission.dataset_ref,
                "payload_sha256": digest,
                "payload_bytes": size,
                "response_status": receipt.status,
                "rule_degraded_count": submission.metrics.get("rule_degraded_count", 0),
                "model_normalized_count": submission.metrics.get("model_normalized_count", 0),
                "total_cases": len(rows),
            },
        )

        # ⑧ 登记确实生效了，才把 run 推进到「已提交」。
        self._store.submit_run(
            run_id,
            actor=actor,
            reason=f"写回科研平台评估登记，登记号 {receipt.registration_id}",
        )

        return WritebackOutcome(
            run_id=run_id,
            decision=decision,
            submission=submission,
            receipt=receipt,
            submitted_by=submission.submitted_by,
            submitted_at=submission.submitted_at,
        )

    # ---- 回执核对 ----

    def verify(self, run_id: str, *, remote: bool = True) -> ReceiptVerification:
        """核对回执：本地记的与对端实际的**对不对得上**。

        「请求发出去了」不等于「登记生效了」。这一条把两边的登记号摆在一起：

        - 本地没有回执 → 这一轮压根没提交过；
        - 本地记着「结果未知」→ 该去问对端（这正是「已发出 ≠ 已生效」那一步）；
        - 两边都有登记号且一致 → 登记**确实生效**了；
        - 两边都有但不一致 → 对端记的不是这一轮，**必须报出来**。
        """

        run = self._require_run(run_id)
        receipt = self._store.get_receipt_by_run(run_id)
        if receipt is None:
            key = compute_idempotency_key(run.run_id, run.model_id, run.dataset_ref)
            receipt = self._store.get_receipt(key)
        if receipt is None:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"这一轮还没有写回回执：{run_id}",
            )

        if not remote:
            return ReceiptVerification(
                run_id=run.run_id,
                local_status=receipt.status,
                registration_id=receipt.registration_id,
                idempotency_key=receipt.idempotency_key,
                http_status=receipt.http_status,
                matched=receipt.status == RECEIPT_STATUS_REGISTERED,
                reason=(
                    "本地回执显示已登记（未做对端核对）"
                    if receipt.status == RECEIPT_STATUS_REGISTERED
                    else f"本地回执状态：{receipt.status}（未做对端核对）"
                ),
            )

        lookup = self._adapter.fetch_registration(receipt.idempotency_key)
        matched = bool(
            lookup.found and lookup.registration_id == receipt.registration_id
        )
        if matched:
            reason = f"对端已登记，登记号一致（{receipt.registration_id}）"
        elif lookup.found:
            reason = (
                "对端有登记，但登记号与本地不一致"
                f"（本地 {receipt.registration_id or '无'}，"
                f"对端 {lookup.registration_id}）"
            )
        else:
            reason = "对端查不到这条登记（本地记着已登记，需人工核实）"

        return ReceiptVerification(
            run_id=run.run_id,
            local_status=receipt.status,
            registration_id=receipt.registration_id,
            idempotency_key=receipt.idempotency_key,
            remote_found=lookup.found,
            remote_registration_id=lookup.registration_id,
            remote_status=lookup.status,
            http_status=lookup.http_status,
            matched=matched,
            reason=reason,
        )

    # ---- 内部 ----

    def _assert_human_actor(self, actor: str, run_status: str) -> None:
        """触发方不是人工 —— 直接拒（幂等回放也不例外）。

        回放虽然不出站，但它把「这一轮已经写进院内登记了」这个结论交出去，
        那也是**人工确认**的一部分 —— 引擎不该替人宣布「提交过了」。
        """

        if actor_allowed(actor):
            return
        decision = actor_rejection(run_status, actor)
        raise NoduleEvalError(
            ErrorCode.WRITEBACK_NOT_REVIEWED,
            detail=decision.reason,
            current_status=decision.current_status,
            required_status=GATE_REQUIRED_STATUS,
            actor=decision.actor,
        )

    def _confirm_remote(self, receipt: WritebackReceipt) -> RegistrationLookup | None:
        """问对端：这条登记到底在不在。

        查不到（404）返回 ``None``；**核对本身失败**（连不上 / 5xx）也返回
        ``None`` —— 那说明对端此刻不可问，不能据此断言「没登记」。
        真正连不上时，随后的提交会带着自己的错误信息出来，不必在这里
        再抛一次把真正的原因盖住。
        """

        try:
            lookup = self._adapter.fetch_registration(receipt.idempotency_key)
        except NoduleEvalError:
            return None
        return lookup if lookup.found else None

    def _replay(
        self,
        receipt: WritebackReceipt,
        run: StoredRun,
        *,
        actor: str,
        converged: bool = False,
    ) -> WritebackOutcome:
        """幂等回放：把**已经登记过**的结果原样交回去。

        **一次出站调用都不发** —— 对端已经有那一条登记了，再发一次只是让
        科研平台再回放一遍同样的回执（幂等键相同），没有任何新信息，
        却多一个「这轮写回调了几次」的疑问。

        顺带**收敛状态**：上一次出站成功但本地状态推进失败时（进程被杀、
        库被锁），这一轮把状态补到 ``submitted``，两边对得上。
        """

        append_writeback_audit(
            self._store.artifact_path(run.run_id, WRITEBACK_AUDIT_FILENAME),
            {
                "at": receipt.updated_at or now_iso(),
                "run_id": run.run_id,
                "actor": actor,
                "action": "replay",
                "registration_id": receipt.registration_id,
                "idempotency_key": receipt.idempotency_key,
                "http_status": receipt.http_status,
                "attempts": receipt.attempts,
                "converged": converged,
                "submitted_by": receipt.actor,
                # 回放**没有新的请求体** —— 这里回放的是**当初那一版**的摘要，
                # 从回执台账里取（台账里存着），不重新算（重算的是当前内容，
                # 与当初发出去的那一版未必相同，写进去就是假账）。
                "payload_sha256": receipt.payload_sha256,
                "payload_bytes": 0,
                "response_status": str(
                    receipt.response_summary.get("status") or ""
                ),
                "total_cases": 0,
                "note": (
                    "同一幂等键已登记过，本次按幂等回放本地回执"
                    "（未发出任何出站请求）"
                ),
            },
        )

        # 状态收敛：本地还没到「已提交」，且当前状态允许提交时补上。
        if run.status == RUN_STATUS_REVIEWED:
            self._store.submit_run(
                run.run_id,
                actor=actor,
                reason=f"幂等回放写回结果，登记号 {receipt.registration_id}",
            )

        return WritebackOutcome(
            run_id=run.run_id,
            decision=GateDecision(
                allowed=True,
                current_status=run.status,
                actor=actor,
                reason=(
                    "这一轮已登记过，按幂等键回放本地回执"
                    "（本次未发出任何出站请求）"
                ),
            ),
            submission=None,
            receipt=RegistrationReceipt(
                registration_id=receipt.registration_id,
                run_id=receipt.run_id,
                status=str(receipt.response_summary.get("status") or ""),
                received_at=receipt.received_at,
                idempotency_key=receipt.idempotency_key,
                http_status=receipt.http_status,
                attempts=receipt.attempts,
            ),
            submitted_by=receipt.actor,
            submitted_at=receipt.submitted_at,
            replayed=True,
        )

    def _record_failure(
        self,
        run: StoredRun,
        key: str,
        submission: EvaluationSubmission,
        exc: NoduleEvalError,
        *,
        actor: str,
    ) -> None:
        """写回失败：**留痕但不推进状态**。

        两件事必须分清：

        - **run 状态**：停在 ``reviewed``，绝不推进到 ``submitted`` ——
          没登记成功就不许说「已提交」；
        - **回执状态**：记 ``pending``（传输层失败，**结果未知**）或
          ``failed``（对端明确报错，可以重试）。

        ``pending`` 特别要紧：它意味着「对端可能已经登记了，只是回执没回来」。
        下一步该做的是**核对**，不是再发一次。
        """

        status = (
            RECEIPT_STATUS_PENDING if exc.http_status is None else RECEIPT_STATUS_FAILED
        )
        attempts = int(exc.fields.get("attempts") or 1)
        body = submission.to_dict()
        digest = payload_digest(body)
        size = payload_bytes(body)

        self._store.record_receipt(
            WritebackReceipt(
                run_id=run.run_id,
                idempotency_key=key,
                status=status,
                submitted_at=submission.submitted_at,
                http_status=exc.http_status,
                attempts=attempts,
                error_code=exc.code.value,
                error_detail=exc.message,
                actor=actor,
                payload_sha256=digest,
            ),
            actor=actor,
            reason=f"写回失败（{exc.code.value}）：{exc.detail}",
        )

        append_writeback_audit(
            self._store.artifact_path(run.run_id, WRITEBACK_AUDIT_FILENAME),
            {
                "at": submission.submitted_at,
                "run_id": run.run_id,
                "actor": actor,
                "action": "submit_failed",
                "idempotency_key": key,
                "http_status": exc.http_status,
                "attempts": attempts,
                "error_code": exc.code.value,
                "error_detail": exc.message,
                "receipt_status": status,
                "submitted_by": submission.submitted_by,
                "model_id": submission.model_id,
                "model_version": submission.model_version,
                "dataset_ref": submission.dataset_ref,
                "payload_sha256": digest,
                "payload_bytes": size,
                "response_status": "",
                "total_cases": len(submission.case_rows),
            },
        )

    # ---- 库读取 ----

    def _max_payload_bytes(self) -> int:
        """写回提交体的大小上限（配置里读；缺省 1 MiB）。"""

        value = getattr(self._config, "research_platform_max_payload_bytes", None)
        if isinstance(value, int) and value > 0:
            return value
        return DEFAULT_MAX_PAYLOAD_BYTES

    def _require_run(self, run_id: str) -> StoredRun:
        """取这一轮的 run 行；不在库里就指名报错。"""

        key = (run_id or "").strip()
        if not key:
            raise NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail="run_id 不得为空")
        run = self._store.get_run(key)
        if run is None:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"运行库里没有这一轮：{key}",
            )
        return run

    def _summary_payload(self, run_id: str) -> Mapping[str, Any] | None:
        """读本轮的 ``summary.json``（没有就当没有，不猜）。"""

        try:
            payload = self._store.read_json_artifact(run_id, SUMMARY_FILENAME)
        except (OSError, NoduleEvalError):
            return None
        return payload if isinstance(payload, Mapping) else None
