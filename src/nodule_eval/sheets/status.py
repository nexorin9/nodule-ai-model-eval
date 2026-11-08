"""运行状态契约 —— **引擎写、宏读**的唯一真源（任务 46）。

**这张评估表就是产品的界面本体**：放射科填金标准，信息科点「开始评估」，
两百例要跑好几分钟。这几分钟里，界面必须**说话** ——
不能是 Excel 冻结着的窗口，也不能是「跑完了才蹦出来一行字」。

本模块定义那份「说话」的内容与格式：

- **状态文件** ``runs/<run_id>/engine_status.json`` —— 引擎在跑的过程中持续写，
  宏每隔半秒读一次，把进度刷新到**状态栏**（表底部那行）。
- **三态文案** —— 加载态 / 空态 / 错误态各一套固定说法，
  在本模块里写死（模板、宏、引擎三处共用一份）。
- **错误码 → 处置提示** —— 状态栏不能只写「失败了」，得告诉人**下一步怎么办**。

**为什么用文件而不是管道**：宏（Excel VBA / WPS JSA）起的是独立进程，
拿不到子进程的标准输出；文件是两边唯一都能碰的东西，
而落盘**必须原子** —— 宏读到写了一半的 JSON 会解析失败、状态栏闪成乱码。
本模块的写入走「临时文件 + 原子替换」，读者永远读到完整的一份。

**这一层是新建的**：源产品（Gradio 聊天界面）只有控制台上的
``print(f"Progress: {processed}/{total_examples}")``（quickstart.py:274），
界面侧**没有任何加载态**；本任务把进度搬进表格界面，
并补上源产品没有的「空态」与「错误态」。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .sections import SECTION_MARKER_STATUS

# --------------------------------------------------------------------------
# 文件与状态取值
# --------------------------------------------------------------------------

#: 状态文件名 —— 落在 ``runs/<run_id>/`` 下（引擎写、宏读，两端一个字不差）。
STATUS_FILENAME = "engine_status.json"

#: 状态取值。
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_INTERRUPTED = "interrupted"

#: 全部状态取值。
STATES: tuple[str, ...] = (
    STATE_IDLE,
    STATE_RUNNING,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_INTERRUPTED,
)

#: 终态（宏轮询到这些就停）。
TERMINAL_STATES: tuple[str, ...] = (STATE_COMPLETED, STATE_FAILED, STATE_INTERRUPTED)


# --------------------------------------------------------------------------
# 三态文案（模板 / 宏 / 引擎共用一份）
# --------------------------------------------------------------------------

#: 状态栏文本的前缀 —— **必须保留区块标记**：
#: 状态栏首格带 ``【运行状态】`` 标记，读取层见到标记即停（不再往下的内容当数据读）。
#: 回填时把这个标记抹掉，读取层就会把状态栏当成普通数据行。
STATUS_PREFIX = SECTION_MARKER_STATUS

#: **加载态**文案模板：进度（已处理 / 总数 / 当前例号）+ 心跳时间。
LOADING_TEXT = (
    "{prefix}评估中：已处理 {processed}/{total} 例，当前例号 {current}"
    "（心跳 {heartbeat}）"
)

#: **加载态 · 尚未开始处理任何一例**的文案。
LOADING_TEXT_STARTING = "{prefix}评估中：共 {total} 例，正在准备调用推理端点……"

#: **空态 · 本表未圈定病例**（明细区没有可评估的检查号）。
EMPTY_CASES_TEXT = "{prefix}本表未圈定病例：明细区没有可评估的检查号"

#: **空态 · 本次未发现漏判**（漏诊明细区给的是一句人话，不是空白）。
EMPTY_MISSED_TEXT = "本次未发现漏判"

#: **完成态**文案模板。
COMPLETED_TEXT = "{prefix}评估完成：run_id={run_id}，共 {total} 例"

#: **完成态 · 有失败例**时的补充说明（错误码 + 处置提示）。
COMPLETED_FAILED_TEXT = "（失败 {failed} 例：错误码 {code} —— {hint}）"

#: **错误态**文案模板 —— 错误码 + 处置提示。
ERROR_TEXT = "{prefix}评估失败：错误码 {code} —— {hint}"

#: **中断态**文案模板。
INTERRUPTED_TEXT = "{prefix}评估中断：{detail} —— {hint}"


# --------------------------------------------------------------------------
# 错误码 → 处置提示（状态栏要告诉人「下一步怎么办」）
# --------------------------------------------------------------------------

#: 各错误码的处置提示。**只写与本产品相关的处置**，
#: 不写「请联系管理员」这种谁都能说的空话。
DISPOSE_HINTS: Mapping[str, str] = {
    "CREDENTIAL_MISSING": "按 doctor 的逐项提示补齐环境变量后重跑（不会自动回落到本地替身）。",
    "RIS_READ_FAILED": "确认院内 RIS 接口可用、令牌未过期后重跑本轮。",
    "AMBIGUOUS_BINDING": "同一检查号命中多条影像：人工在评估表里指定唯一影像后再重跑。",
    "INFERENCE_4XX": "按失败原因里的厂商错误码与出错字段修正后重跑（本次请求未重试）。",
    "INFERENCE_5XX": "推理服务返回 5xx：联系厂商查服务状态，重跑本轮即可（已跑完的例不会重复调用）。",
    "INFERENCE_TIMEOUT": "推理服务响应超时：确认端点负载，或调大 MODEL_TIMEOUT 后重跑。",
    "STUDY_UID_MISMATCH": "推理服务回传的检查标识与请求不一致：这一列结果不可采信，请厂商核对影像对应关系。",
    "FIELD_MAPPING_MISSING": "厂商响应字段与模型目录对不上：补齐 models/<model_id>.yaml 的字段映射后重跑。",
    "RESEARCH_WRITE_FAILED": "写回科研平台失败：本轮结论未登记，确认对端状态后重试（幂等键相同，不会重复登记）。",
    "RESEARCH_PAYLOAD_TOO_LARGE": "提交体超出对端上限：按提示分批提交（本轮不会截断逐例明细）。",
    "WRITEBACK_NOT_REVIEWED": "这一轮还没人工复核：先在表里逐行看过判定与漏诊区，再点「确认提交」。",
    "MODEL_UNAVAILABLE": "模型端点不可达：确认模型服务地址与凭据后重跑（已降级的例已单独标出）。",
    "MODEL_REQUEST_REJECTED": "模型端点拒收请求：按失败原因里的字段名修正提示词或参数后重跑。",
    "RULE_DEGRADED": "金标准归并走了规则降级：结论由确定性基线给出，请人工复核这一列。",
    "INVALID_STATE_TRANSITION": "换一个新的 run_id 重跑（已复核 / 已提交的轮次不许直接重跑）。",
    "CONTRACT_INVALID": "输入或产物不合契约：按提示修正后重跑。",
}

#: 默认处置提示（表里没有登记这个错误码时用）。
DEFAULT_DISPOSE_HINT = (
    "按失败原因里的提示处置后重跑；已跑完的例不会重复调用推理端点。"
)


def dispose_hint(code: str) -> str:
    """错误码 → 处置提示（没登记的码走默认提示）。"""

    return DISPOSE_HINTS.get(str(code or "").strip(), DEFAULT_DISPOSE_HINT)


# --------------------------------------------------------------------------
# 状态对象
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunStatus:
    """一次运行的状态快照 —— 引擎写进状态文件，宏读出来显示。"""

    run_id: str = ""
    state: str = STATE_IDLE
    processed: int = 0
    total: int = 0
    current_accession_no: str = ""
    heartbeat_at: str = ""
    updated_at: str = ""
    error_code: str = ""
    error_detail: str = ""
    message: str = ""
    failed_cases: int = 0
    #: **渲染好的状态栏文本**（状态文件里带着，宏直接搬这一行进状态栏）。
    status_text: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """落进状态文件的字典（**字段名是契约**，宏按这些名字取值）。

        ``status_text`` 是**渲染好的状态栏文本** —— 宏不自己拼文案，
        直接把这一行写进状态栏。文案的唯一真源是本模块的
        :func:`status_bar_text`；宏里再写一份中文说法，两边就会漂。
        """

        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "state": self.state,
            "processed": int(self.processed),
            "total": int(self.total),
            "current_accession_no": self.current_accession_no,
            "heartbeat_at": self.heartbeat_at,
            "updated_at": self.updated_at,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "message": self.message,
            "failed_cases": int(self.failed_cases),
            "status_text": self.status_text or status_bar_text(self),
        }
        payload.update(dict(self.extra))
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunStatus":
        """从状态文件读回来（**缺字段不报错**，按缺省补齐）。"""

        known = {
            "run_id",
            "state",
            "processed",
            "total",
            "current_accession_no",
            "heartbeat_at",
            "updated_at",
            "error_code",
            "error_detail",
            "message",
            "failed_cases",
            "status_text",
        }
        extra = {k: v for k, v in payload.items() if k not in known}
        return cls(
            run_id=str(payload.get("run_id") or ""),
            state=str(payload.get("state") or STATE_IDLE),
            processed=_as_int(payload.get("processed")),
            total=_as_int(payload.get("total")),
            current_accession_no=str(payload.get("current_accession_no") or ""),
            heartbeat_at=str(payload.get("heartbeat_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            error_code=str(payload.get("error_code") or ""),
            error_detail=str(payload.get("error_detail") or ""),
            message=str(payload.get("message") or ""),
            failed_cases=_as_int(payload.get("failed_cases")),
            status_text=str(payload.get("status_text") or ""),
            extra=extra,
        )


def _as_int(value: Any) -> int:
    """宽松地取整数（状态文件被手改过也不该让宏崩掉）。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------


def status_path(run_dir: Path | str) -> Path:
    """状态文件路径（``run_dir`` 一般是 ``runs/<run_id>``）。"""

    return Path(run_dir) / STATUS_FILENAME


def now_stamp() -> str:
    """统一的时间戳格式（``YYYY-MM-DD HH:mm:ss``）—— 表里各处都用这一个。"""

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_status(run_dir: Path | str, status: RunStatus) -> Path:
    """把状态写进 ``run_dir/engine_status.json`` —— **原子写**。

    **原子是硬要求**：宏每隔半秒读一次这个文件；写到一半被读到，
    宏就会解析失败、状态栏闪成乱码。这里走「临时文件写全 → 原子替换」，
    读者要么读到上一份完整的，要么读到这一份完整的，读不到半截。

    **临时文件名带进程号与线程号**：并发跑时（``--concurrency 4``）几个线程
    会同时写这份状态 —— 共用一个固定临时文件名时，先替换掉的那个线程会让
    后一个的 ``os.replace`` 找不到源文件（``FileNotFoundError``），
    状态栏就停在半截、进度看不见了。临时名唯一 + 原子替换，两者都成立。
    """

    path = status_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = status.to_dict()
    payload.setdefault("updated_at", now_stamp())
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_status(path: Path | str) -> RunStatus | None:
    """读状态文件。

    **读不到就返回 ``None``**（不是抛错）—— 宏在引擎还没写第一份状态时
    轮询到空文件是正常情况，不该因此报错退出。
    """

    file_path = Path(path)
    if not file_path.is_file():
        return None
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return RunStatus.from_dict(payload)


# --------------------------------------------------------------------------
# 文案渲染
# --------------------------------------------------------------------------


def loading_text(status: RunStatus) -> str:
    """加载态文案（进度 + 当前例号 + 心跳）。"""

    if status.processed <= 0:
        return LOADING_TEXT_STARTING.format(prefix=STATUS_PREFIX, total=status.total)
    current = status.current_accession_no or "—"
    return LOADING_TEXT.format(
        prefix=STATUS_PREFIX,
        processed=status.processed,
        total=status.total,
        current=current,
        heartbeat=status.heartbeat_at or status.updated_at or "—",
    )


def completed_text(status: RunStatus) -> str:
    """完成态文案。

    **这一轮有失败例时，状态栏要给出错误码与处置提示** ——
    只写「失败 2 例」，看的人还得自己去翻底账才知道出了什么事；
    写清「错误码 + 下一步怎么办」，才是给人用的提示。
    """

    text = COMPLETED_TEXT.format(
        prefix=STATUS_PREFIX,
        run_id=status.run_id or "—",
        total=status.total,
    )
    if status.failed_cases > 0 and status.error_code:
        text += COMPLETED_FAILED_TEXT.format(
            failed=status.failed_cases,
            code=status.error_code,
            hint=dispose_hint(status.error_code),
        )
    elif status.failed_cases > 0:
        text += f"（失败 {status.failed_cases} 例）"
    return text


def error_text(status: RunStatus) -> str:
    """错误态文案 —— **错误码 + 处置提示**，不只写「失败了」。"""

    code = status.error_code or "CONTRACT_INVALID"
    detail = status.error_detail or status.message or ""
    hint = dispose_hint(code)
    text = ERROR_TEXT.format(prefix=STATUS_PREFIX, code=code, hint=hint)
    if detail:
        text = f"{text}（{detail}）"
    return text


def interrupted_text(status: RunStatus) -> str:
    """中断态文案。"""

    code = status.error_code or "CONTRACT_INVALID"
    detail = status.error_detail or status.message or "运行被中断"
    return INTERRUPTED_TEXT.format(
        prefix=STATUS_PREFIX,
        detail=detail,
        hint=dispose_hint(code),
    )


def status_bar_text(status: RunStatus) -> str:
    """按状态取值渲染**状态栏那一行**的文本。

    空态（``total <= 0``）单独走一条 —— 「本表未圈定病例」与
    「评估失败」是两回事，前者根本没开始跑，后者的处置完全不同。
    """

    if status.total <= 0 and status.state in (STATE_IDLE, STATE_RUNNING):
        return EMPTY_CASES_TEXT.format(prefix=STATUS_PREFIX)
    if status.state == STATE_RUNNING:
        return loading_text(status)
    if status.state == STATE_COMPLETED:
        return completed_text(status)
    if status.state == STATE_INTERRUPTED:
        return interrupted_text(status)
    if status.state == STATE_FAILED:
        return error_text(status)
    return EMPTY_CASES_TEXT.format(prefix=STATUS_PREFIX)
