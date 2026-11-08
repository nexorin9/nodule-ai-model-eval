"""按调用记录 ID 反查：从结论回到原始那一次调用。

放射科与信息科最常问的一句话是「**这一例凭什么算漏判**」。
回答它需要一条**从结论回到原始调用**的链路：

    表格「调用记录ID」列  →  runs/<run_id>/tool_calls.jsonl  →  那次调用的入参与返回
                        ↘  runs/<run_id>/case_rows.csv       →  这一例的判定
                        ↘  runs/<run_id>/cases/<检查号>/…    →  判定依据与金标准摘要

**源产品证据**：参考实现把每次工具调用写进 ``logs/tool_calls_{timestamp}.json``
（``github_ref/MedRAX/medrax/agent/agent.py:168-193`` 的 ``_save_tool_calls``），
字段是 ``{tool_call_id, name, args, content, timestamp}``。它**能**回答
「这次调用发了什么、回了什么」，但那是**一次运行一整份文件**的形态：

* 一次跑两百例 → 一个文件里堆着两百条，只能整体读；
* 想问「某某例那一次」→ 得把整个文件读进内存再挑；
* 而且**结论侧没有对应的行** —— 源产品的「结论」是一句自然语言回答，
  没有「这一例判成什么、按哪一条判的」这种可 join 的东西。

所以本模块做三件源产品没做的事：

1. **跨产物 join** —— 逐例判定行（``case_rows.csv``）与调用底账
   （``tool_calls.jsonl``）按 ``tool_call_id`` 关联，**两侧缺一侧就报错**
   （不返回一份看着齐全、其实缺了半边的结果）；逐例产物
   （``cases/<检查号>/attempt-XXXX/case.json``）补上**判定依据**与
   **金标准摘要**；
2. **找不到就说找不到** —— id 不存在时报「未找到该调用记录」并**列出可用的
   run_id**（让人知道自己查错了哪一轮），**不返回空结果**；
3. **可直接粘进工单的核查片段** —— 结论 + 入参 + 返回 + 依据排成一段中文，
   信息科复制就能贴进工单或飞检说明。

**金标准原文不落产物**，本模块给的「金标准」是**归并后的结构化标签摘要**
与**原文哈希**（``gold_standard_sha256``）—— 原文可能带姓名 / 年龄 / 住院号，
反查片段是要往外发的东西，不能带患者可识别明细。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import ErrorCode, NoduleEvalError
from .case_artifact import CASES_DIRNAME, case_attempts_dir
from .toolcall import TOOL_CALLS_FILENAME

__all__ = [
    "CASE_ARTIFACT_FILENAME",
    "CASE_ROWS_FILENAME",
    "InspectResult",
    "available_run_ids",
    "inspect_tool_call",
    "render_inspect",
    "render_snippet",
]

#: 逐例明细 CSV 的文件名（与产物目录约定一致）。
CASE_ROWS_FILENAME = "case_rows.csv"

#: 逐例产物文件名（与 :mod:`nodule_eval.audit.case_artifact` 一致）。
CASE_ARTIFACT_FILENAME = "case.json"

#: run 级结论产物的文件名（用于带出模型与版本、数据集引用）。
SUMMARY_FILENAME = "summary.json"

#: 判定值的中文说法（给复核的人看，不是内部字段名）。
VERDICT_LABELS: dict[str, str] = {
    "hit": "命中",
    "miss": "漏诊",
    "false_positive": "误报",
    "true_negative": "真阴性",
    "failed": "失败",
}


# --------------------------------------------------------------------------
# 找 run 目录
# --------------------------------------------------------------------------


#: 认定一个目录是「一轮 run」的标志物（任一在即为是）。
_RUN_MARKERS: tuple[str, ...] = (
    TOOL_CALLS_FILENAME,
    CASE_ROWS_FILENAME,
    SUMMARY_FILENAME,
    "cases",
    "versions",
)


def _is_run_dir(path: Path) -> bool:
    """这个目录是不是一轮 run 的产物目录。"""

    if not path.is_dir():
        return False
    return any((path / marker).exists() for marker in _RUN_MARKERS)


def available_run_ids(runs_dir: Path | str) -> tuple[str, ...]:
    """列出产物根目录下的 run_id（按名称排序）。

    认的是「**有一轮产物**的目录」而不是「有底账的目录」——
    底账恰好丢了的那一轮**也要列出来**：它正是「产物不完整」要指认的那一轮，
    不列出来，复核的人根本不知道该去看哪一轮。
    """

    root = Path(runs_dir)
    if not root.is_dir():
        return ()
    return tuple(
        child.name for child in sorted(root.iterdir()) if _is_run_dir(child)
    )


def _run_dirs(runs_dir: Path | str, run_id: str, tool_call_id: str) -> tuple[Path, ...]:
    """确定要查哪几个 run 目录。

    * 给了 ``run_id`` → 就查这一个（它不存在 → 直接报错并列出可用的）；
    * 没给 → 查**所有** run 目录（调用记录 ID 全局唯一，至多命中一个）。
    """

    root = Path(runs_dir)
    if run_id:
        target = root / run_id
        if not target.is_dir():
            known = available_run_ids(root)
            raise NoduleEvalError(
                ErrorCode.TOOL_CALL_NOT_FOUND,
                detail=(
                    f"run 目录不存在：{target}。"
                    f"可用 run_id：{'、'.join(known) if known else '（产物目录下没有任何 run）'}"
                ),
                tool_call_id=tool_call_id,
            )
        return (target,)
    return tuple(root / name for name in available_run_ids(root))


# --------------------------------------------------------------------------
# 读三份产物
# --------------------------------------------------------------------------


def _read_tool_calls(run_dir: Path) -> list[dict[str, Any]]:
    """读一个 run 的调用底账（坏行跳过 —— 底账一行坏了不能查不成别的例）。"""

    path = run_dir / TOOL_CALLS_FILENAME
    if not path.is_file():
        raise NoduleEvalError(
            ErrorCode.INSPECT_ARTIFACT_INCOMPLETE,
            detail=(
                f"缺少调用底账 {TOOL_CALLS_FILENAME}（目录 {run_dir}）—— "
                "这一轮的出站调用没有留痕，无法按 id 反查"
            ),
        )
    records: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def _read_case_rows(run_dir: Path) -> list[dict[str, Any]]:
    """读一个 run 的逐例判定行（表头缺列即报产物不完整）。"""

    path = run_dir / CASE_ROWS_FILENAME
    if not path.is_file():
        raise NoduleEvalError(
            ErrorCode.INSPECT_ARTIFACT_INCOMPLETE,
            detail=(
                f"缺少逐例明细 {CASE_ROWS_FILENAME}（目录 {run_dir}）—— "
                "没有结论行可与调用底账对账，反查链路是断的"
            ),
        )
    from ..store import read_case_rows_csv  # 局部导入：避免 store ↔ audit 循环

    return [dict(row) for row in read_case_rows_csv(path)]


def _latest_case_artifact(run_dir: Path, accession_no: str) -> tuple[dict[str, Any], str]:
    """取某一例**最新一次尝试**的逐例产物（判定依据与金标准摘要在里面）。

    重跑会产生 ``attempt-0001`` / ``attempt-0002`` … 多个版本；
    反查要看的是**当前这一版** —— 取序号最大的那个。
    """

    case_dir = case_attempts_dir(run_dir, accession_no)
    if not case_dir.is_dir():
        return {}, ""
    attempts: list[tuple[int, Path]] = []
    for child in case_dir.iterdir():
        if not child.is_dir() or not child.name.startswith("attempt-"):
            continue
        try:
            index = int(child.name.split("-", 1)[1])
        except ValueError:
            continue
        attempts.append((index, child))
    if not attempts:
        return {}, ""
    attempts.sort()
    path = attempts[-1][1] / CASE_ARTIFACT_FILENAME
    if not path.is_file():
        return {}, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}, ""
    if not isinstance(payload, dict):
        return {}, ""
    return payload, str(path)


# --------------------------------------------------------------------------
# 金标准摘要（从归并后的结构化标签拼，**不是原文**）
# --------------------------------------------------------------------------


def _lesion_summary(lesion: Mapping[str, Any]) -> str:
    """一处病灶的摘要（部位 / 尺寸 / 密度；认不出的属性不写）。"""

    bits: list[str] = []
    location = lesion.get("location")
    size_mm = lesion.get("size_mm")
    density = lesion.get("density")
    if location:
        bits.append(str(location))
    if isinstance(size_mm, (int, float)) and not isinstance(size_mm, bool):
        bits.append(f"{float(size_mm):.1f}mm")
    if density:
        bits.append(str(density))
    if bits:
        return " ".join(bits)
    finding = str(lesion.get("finding") or "")
    return finding or "金标准写明有病灶（未写部位 / 尺寸 / 密度）"


def gold_summary_from_labels(gold: Mapping[str, Any] | None) -> str:
    """金标准摘要 —— 由归并后的结构化标签拼成的一句可读文本。

    与 :func:`nodule_eval.metrics.report.gold_summary_text` 同一口径，
    只是这里拿的是**从 JSON 读回的字典**（产物里的形态），不是对象。
    """

    if not gold:
        return ""
    lesions = gold.get("lesions") or []
    parts = [_lesion_summary(item) for item in lesions if isinstance(item, Mapping)]
    parts = [part for part in parts if part]
    if parts:
        return "；".join(parts)
    presence = gold.get("presence")
    if presence is True:
        return "金标准写明有病灶（未写部位 / 尺寸 / 密度）"
    if presence is False:
        return "金标准写明未见结节"
    return "金标准的存在性读不出来（需人工复核）"


# --------------------------------------------------------------------------
# 反查结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InspectResult:
    """一次反查的完整结果。

    ``call``
        调用底账里那一行（入参 + 返回）。
    ``case_row``
        逐例判定行（判定 / 失败原因 / 是否挂起 …）。
    ``judge_basis``
        判定依据（存在性 / 部位 / 尺寸 / 密度 / 覆盖五层结论）——
        来自逐例产物；产物不在时为空串。
    ``gold_summary``
        金标准摘要（**归并后的结构化标签**，不是原文）。
    ``gold_standard_sha256``
        金标准原文的哈希 —— 要核对原文时用它对表里的那一行，
        不必把原文复制进核查片段。
    """

    run_id: str
    run_dir: str
    tool_call_id: str
    call: Mapping[str, Any]
    case_row: Mapping[str, Any]
    accession_no: str
    verdict: str
    judge_basis: str = ""
    gold_summary: str = ""
    gold_standard_sha256: str = ""
    model_findings: str = ""
    failure_reason: str = ""
    needs_review: bool = False
    normalize_source: str = ""
    error_code: str = ""
    uncovered_labels: str = ""
    rule_degraded: bool = False
    degrade_reason: str = ""
    case_artifact_found: bool = False
    case_artifact_path: str = ""
    run_summary: Mapping[str, Any] = field(default_factory=dict)

    @property
    def verdict_label(self) -> str:
        """判定的中文说法。"""

        return VERDICT_LABELS.get(self.verdict, self.verdict or "（无判定）")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "tool_call_id": self.tool_call_id,
            "accession_no": self.accession_no,
            "verdict": self.verdict,
            "verdict_label": self.verdict_label,
            "call": dict(self.call),
            "case_row": dict(self.case_row),
            "judge_basis": self.judge_basis,
            "gold_summary": self.gold_summary,
            "gold_standard_sha256": self.gold_standard_sha256,
            "model_findings": self.model_findings,
            "failure_reason": self.failure_reason,
            "needs_review": self.needs_review,
            "normalize_source": self.normalize_source,
            "error_code": self.error_code,
            "uncovered_labels": self.uncovered_labels,
            "rule_degraded": self.rule_degraded,
            "degrade_reason": self.degrade_reason,
            "case_artifact_found": self.case_artifact_found,
            "case_artifact_path": self.case_artifact_path,
            "run_summary": dict(self.run_summary),
        }


def _truthy(value: Any) -> bool:
    """CSV 里读回来的布尔值（``"True"`` / ``"true"`` / ``"1"`` 都算真）。"""

    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"true", "1", "yes", "y"}


def inspect_tool_call(
    runs_dir: Path | str,
    tool_call_id: str,
    run_id: str = "",
) -> InspectResult:
    """按 ``tool_call_id`` 反查那一次调用的全貌。

    **找不到就报错**：id 不存在时抛 ``TOOL_CALL_NOT_FOUND`` 并列出可用的
    ``run_id`` —— 不返回一份空结果让人以为「查过了，没有问题」。

    **缺一侧就报错**：调用底账与逐例明细必须**都在**；id 只在底账里
    （有调用没结论）或只在明细里（有结论没底账），都属**产物不完整**，
    抛 ``INSPECT_ARTIFACT_INCOMPLETE`` 并写明缺哪一侧。

    逐例产物（``cases/<检查号>/attempt-XXXX/case.json``）用于补
    **判定依据**与**金标准摘要**；它不在时**不报错**（判定与调用仍在），
    但结果里 ``case_artifact_found=False``，渲染文本会明说依据不可得。
    """

    wanted = (tool_call_id or "").strip()
    if not wanted:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail="反查需要 --tool-call-id（调用记录 ID），不能为空",
        )

    root = Path(runs_dir)
    known = available_run_ids(root)
    hit_dir: Path | None = None
    hit_call: dict[str, Any] | None = None
    incomplete_runs: list[str] = []
    for candidate in _run_dirs(root, run_id, wanted):
        if not (candidate / TOOL_CALLS_FILENAME).is_file():
            # 这一轮没有底账：**显式点名要查它**时报产物不完整；
            # 顺带搜别轮时先记下来 —— 查不到 id 时得说清
            #「有一轮的底账丢了，所以不能断言这个 id 不存在」。
            if run_id:
                raise NoduleEvalError(
                    ErrorCode.INSPECT_ARTIFACT_INCOMPLETE,
                    detail=(
                        f"run {candidate.name} 缺少调用底账 {TOOL_CALLS_FILENAME}"
                        f"（目录 {candidate}）—— 这一轮的出站调用没有留痕，"
                        "无法按 id 反查"
                    ),
                )
            if (candidate / CASE_ROWS_FILENAME).is_file():
                incomplete_runs.append(candidate.name)
            continue
        for record in _read_tool_calls(candidate):
            if str(record.get("tool_call_id") or "").strip() == wanted:
                hit_dir = candidate
                hit_call = record
                break
        if hit_call is not None:
            break

    if hit_dir is None or hit_call is None:
        if incomplete_runs:
            # 有一轮的底账丢了 → **不能断言**这个 id 不存在
            # （它可能就在那一轮里）。说「产物不完整」比说「找不到」诚实。
            raise NoduleEvalError(
                ErrorCode.INSPECT_ARTIFACT_INCOMPLETE,
                detail=(
                    f"run {'、'.join(incomplete_runs)} 缺少调用底账 "
                    f"{TOOL_CALLS_FILENAME}，无法排除调用记录就在其中；"
                    "补齐该轮产物后重查"
                ),
            )
        raise NoduleEvalError(
            ErrorCode.TOOL_CALL_NOT_FOUND,
            detail=(
                f"可用 run_id：{'、'.join(known) if known else '（产物目录下没有任何 run）'}"
            ),
            tool_call_id=wanted,
        )

    rows = _read_case_rows(hit_dir)
    matched = [
        row
        for row in rows
        if str(row.get("tool_call_id") or "").strip() == wanted
    ]
    if not matched:
        raise NoduleEvalError(
            ErrorCode.INSPECT_ARTIFACT_INCOMPLETE,
            detail=(
                f"调用底账里有 {wanted}，但逐例明细 {CASE_ROWS_FILENAME} 里没有对应行"
                f"（目录 {hit_dir}）—— 这一轮的结论与调用对不上账，产物不完整"
            ),
        )
    if len(matched) > 1:
        raise NoduleEvalError(
            ErrorCode.INSPECT_ARTIFACT_INCOMPLETE,
            detail=(
                f"逐例明细里有 {len(matched)} 行挂着同一个调用记录 ID {wanted}"
                f"（目录 {hit_dir}）—— 结论与调用对不上账，产物不完整"
            ),
        )

    row = matched[0]
    accession_no = str(row.get("accession_no") or "")
    artifact, artifact_path = _latest_case_artifact(hit_dir, accession_no)
    judgement = artifact.get("judgement") if isinstance(artifact, Mapping) else None
    judgement = judgement if isinstance(judgement, Mapping) else {}
    gold = artifact.get("gold_labels") if isinstance(artifact, Mapping) else None
    gold = gold if isinstance(gold, Mapping) else {}

    judge_basis = str(row.get("judge_basis") or "") or str(judgement.get("reason") or "")
    if not judge_basis:
        judge_basis = str(judgement.get("basis_text") or "")

    summary: dict[str, Any] = {}
    summary_path = hit_dir / SUMMARY_FILENAME
    if summary_path.is_file():
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = None
        if isinstance(payload, dict):
            summary = payload

    return InspectResult(
        run_id=hit_dir.name,
        run_dir=str(hit_dir),
        tool_call_id=wanted,
        call=dict(hit_call),
        case_row=dict(row),
        accession_no=accession_no,
        verdict=str(row.get("verdict") or ""),
        judge_basis=judge_basis,
        gold_summary=gold_summary_from_labels(gold),
        gold_standard_sha256=str(artifact.get("gold_standard_sha256") or ""),
        model_findings=str(row.get("model_findings") or ""),
        failure_reason=str(row.get("failure_reason") or ""),
        needs_review=_truthy(row.get("needs_review")),
        normalize_source=str(row.get("normalize_source") or ""),
        error_code=str(row.get("error_code") or ""),
        uncovered_labels=str(row.get("uncovered_labels") or ""),
        rule_degraded=_truthy(row.get("rule_degraded")),
        degrade_reason=str(row.get("degrade_reason") or ""),
        case_artifact_found=bool(artifact),
        case_artifact_path=artifact_path,
        run_summary=summary,
    )


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def _call_args_text(call: Mapping[str, Any]) -> str:
    """把入参排成一行人看得懂的文本。"""

    args = call.get("args")
    if not isinstance(args, Mapping) or not args:
        return "（底账这一行没有入参）"
    parts: list[str] = []
    for key in ("accession_no", "study_uid", "image_ref", "model_id", "model_version"):
        if key in args:
            parts.append(f"{key}={args[key]}")
    for key, value in args.items():
        if key not in {"accession_no", "study_uid", "image_ref", "model_id", "model_version"}:
            parts.append(f"{key}={value}")
    return "；".join(parts) if parts else "（底账这一行没有入参）"


def _call_content_text(call: Mapping[str, Any]) -> str:
    """把返回排成一行（原文照抄，只把换行压掉）。"""

    content = call.get("content")
    if content is None:
        return "（底账这一行没有返回）"
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    text = " ".join(text.split())
    return text or "（底账这一行没有返回）"


def render_inspect(result: InspectResult) -> str:
    """反查结果的完整文字输出（终端给人看的那一份）。"""

    call = result.call
    args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
    lines: list[str] = [
        f"run_id          : {result.run_id}",
        f"产物目录        : {result.run_dir}",
        f"调用记录ID      : {result.tool_call_id}",
        f"检查号          : {result.accession_no or '（未绑定到检查号）'}",
        f"调用时刻        : {call.get('timestamp') or '（底账未记时刻）'}",
        f"被调动作        : {call.get('name') or '（底账未记动作名）'}",
        "",
        "── 调用入参 ─────────────────────────────────",
        _call_args_text(call),
        "",
        "── 调用返回 ─────────────────────────────────",
        _call_content_text(call),
        "",
        "── 这一例的判定 ─────────────────────────────",
        f"判定            : {result.verdict or '（无判定）'}（{result.verdict_label}）",
    ]
    if result.needs_review:
        lines.append("需人工复核      : 是")
    if result.rule_degraded:
        lines.append(f"规则降级        : 是（{result.degrade_reason or '原因见逐例产物'}）")
    if result.error_code:
        lines.append(f"错误码          : {result.error_code}")
    if result.failure_reason:
        lines.append(f"失败原因        : {result.failure_reason}")
    if result.normalize_source:
        lines.append(f"金标准归并来源  : {result.normalize_source}")
    if result.uncovered_labels:
        lines.append(f"未盖住的病灶    : {result.uncovered_labels}")
    lines.extend(
        [
            "",
            "── 判定依据 ─────────────────────────────────",
            result.judge_basis or "（逐例产物缺失：判定依据不可得）",
            "",
            "── 金标准（归并摘要，原文不在产物中）──────",
            result.gold_summary or "（逐例产物缺失：金标准摘要不可得）",
        ]
    )
    if result.gold_standard_sha256:
        lines.append(f"原文哈希        : sha256:{result.gold_standard_sha256}")
    if result.case_artifact_path:
        lines.append(f"逐例产物        : {result.case_artifact_path}")
    return "\n".join(lines)


def render_snippet(result: InspectResult) -> str:
    """**可直接粘进工单 / 飞检说明**的核查片段。

    排版按「结论先行 → 依据 → 原始调用」的顺序：看的人第一眼拿到结论，
    想较真再往下看是哪一次调用、发了什么、回了什么。
    """

    call = result.call
    lines = [
        "【肺结节AI模型评估 · 调用核查片段】",
        f"run_id：{result.run_id}",
        f"检查号：{result.accession_no or '（未绑定）'}",
        f"调用记录ID：{result.tool_call_id}",
        f"被调动作：{call.get('name') or '（未记）'}",
        f"调用时刻：{call.get('timestamp') or '（未记）'}",
        f"判定：{result.verdict or '（无判定）'}（{result.verdict_label}）",
        f"判定依据：{result.judge_basis or '（逐例产物缺失，依据不可得）'}",
    ]
    if result.uncovered_labels:
        lines.append(f"未盖住的病灶：{result.uncovered_labels}")
    if result.failure_reason:
        lines.append(f"失败原因：{result.failure_reason}")
    if result.error_code:
        lines.append(f"错误码：{result.error_code}")
    if result.gold_summary:
        lines.append(f"金标准（归并摘要）：{result.gold_summary}")
    if result.gold_standard_sha256:
        lines.append(f"金标准原文哈希：sha256:{result.gold_standard_sha256}")
    lines.append(f"调用入参：{_call_args_text(call)}")
    lines.append(f"调用返回：{_call_content_text(call)}")
    if result.case_artifact_path:
        lines.append(f"逐例产物：{result.case_artifact_path}")
    return "\n".join(lines)


def snippet_lines(result: InspectResult) -> Sequence[str]:
    """核查片段按行切开（写进文件 / 拼进别的文本时用）。"""

    return tuple(render_snippet(result).splitlines())
