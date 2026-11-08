"""模型调用元数据底账：逐次模型调用落 JSONL 一行（任务 33）。

**为什么要单独记一本账**：工具调用与模型调用是**两个端点、两个问题**。

* ``tool_calls.jsonl`` 答的是「**推理服务**被调了没有、回了什么」；
* ``model_calls.jsonl`` 答的是「**语义归并**调了哪个模型、哪个端点、
  耗时多久、用了多少 token、提示词是第几版、这一轮是不是走了降级路径」。

飞检问「这例凭什么算漏诊」时，这两本账要能**接上**：
结论行 → ``tool_call_id`` → 推理调用底账 → 同一 id 下的模型调用元数据。
所以本账每行的 ``tool_call_id`` **必须能在 ``tool_calls.jsonl`` 里命中** ——
命不中就是异常，记下来、查得出来（见 :func:`link_model_calls`）。

**源产品证据**：参考实现的 ``quickstart.py:152-171`` 已经**逐次模型调用记一行**
（``question_id`` / ``timestamp`` / ``model`` / ``temperature`` / ``duration`` /
``usage.prompt_tokens`` / ``usage.completion_tokens`` / ``model_answer``），
字段形状是本产品这本账的来源。但源产品那行有两处**不能照抄**：

1. **它把 ``input.messages`` 与 ``model_answer`` 整段落盘** ——
   本产品的提示词与模型返回里带**金标准原文**（可能含患者可识别信息），
   所以这里**只落哈希**（``prompt_sha256`` / ``raw_response_sha256``），
   事后凭哈希对得上是哪一版提示词、哪一次返回，够复现用；
2. **它是一条混在一起的日志流**（``logger.info``），不落 run 目录、
   也**关联不到工具调用底账** —— 本产品改成每轮一本
   ``runs/<run_id>/model_calls.jsonl``，并与工具调用底账**按 id 关联**。

**源产品没有对应物的三件**（本轮新建）：按 run 分账、与工具底账的关联校验、
提示词版本可反查（:func:`build_prompt_index` / :func:`resolve_prompt_version`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..errors import ErrorCode, NoduleEvalError
from .toolcall import _exclusive_lock

__all__ = [
    "MODEL_CALLS_FILENAME",
    "MODEL_CALL_FIELDS",
    "PROMPT_VERSION_FIELD",
    "ModelCallLinkReport",
    "ModelCallRecord",
    "ModelCallRecorder",
    "ModelCallScan",
    "PromptRef",
    "build_prompt_index",
    "hash_prompt_file",
    "index_model_calls",
    "link_model_calls",
    "missing_model_call_fields",
    "new_model_call_id",
    "parse_prompt_version",
    "resolve_prompt_version",
]


#: 模型调用底账文件名单（与工具调用底账 ``tool_calls.jsonl`` 同级）。
MODEL_CALLS_FILENAME = "model_calls.jsonl"

#: 一行的**必填字段** —— 少任何一项，事后这条账就答不出问题。
#:
#: 前十项是任务 33 定的字段契约（调了哪个模型、哪个端点、什么时候、
#: 多久、多少 token、提示词与返回各是哪一版、有没有走降级）；
#: ``model_call_id`` 是本次模型调用**自己的**唯一标识 ——
#: ``tool_call_id`` 是「它挂在哪次工具调用下面」，
#: 两者不是一回事（一次工具调用下面理论上可以有多次模型调用）。
MODEL_CALL_FIELDS: tuple[str, ...] = (
    "tool_call_id",
    "model",
    "base_url_host",
    "started_at",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "prompt_sha256",
    "raw_response_sha256",
    "fallback_used",
)

#: **提示词版本号**（任务 47）不在上面的必填集里 ——
#: 用内联提示词（没有提示词文件）的调用**本来就没有版本号**，
#: 硬要求非空会让那类调用记不成账。
#:
#: 用外置提示词文件时（语义归并的常态）它一定有值，
#: 由 :func:`parse_prompt_version` 从文件名解析。
PROMPT_VERSION_FIELD = "prompt_version"

#: 一行必须齐备的字段（必填契约 + 本次调用的唯一标识）。
REQUIRED_MODEL_CALL_FIELDS: tuple[str, ...] = ("model_call_id",) + MODEL_CALL_FIELDS

#: 记录对象**显式持有**的字段（其余字段原样进 ``extra``）。
_EXPLICIT_RECORD_FIELDS: frozenset[str] = frozenset(
    {
        "model_call_id",
        "tool_call_id",
        "accession_no",
        "model",
        "base_url_host",
        "started_at",
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
        "prompt_sha256",
        "raw_response_sha256",
        "fallback_used",
        "prompt_template",
        "prompt_template_sha256",
        "prompt_version",
        "prompt_rendered_sha256",
    }
)

#: 这些字段**不能是空值** —— 空着的账看着齐全，其实答不出问题。
_MUST_BE_NON_EMPTY: tuple[str, ...] = (
    "tool_call_id",
    "model",
    "base_url_host",
    "started_at",
    "prompt_sha256",
)

#: 项目根（``src/nodule_eval/audit/modelcall.py`` → 上溯三级）。
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def new_model_call_id() -> str:
    """生成一次模型调用的唯一标识。

    形如 ``mc_<32 位十六进制>``，与工具调用的 ``tc_`` 前缀区分开 ——
    看一眼前缀就知道这条是「调模型」还是「调推理服务」。

    **不复用**：重跑、续跑各生成各的 id，旧产物不被新调用覆盖。
    """

    return f"mc_{uuid.uuid4().hex}"


def hash_prompt_file(path: Path | str) -> str:
    """提示词文件的**内容哈希**（sha256）。

    落进底账的是**文件内容**的哈希，不是渲染后的提示词 ——
    渲染后的提示词里塞着金标准原文，不能落盘；
    文件哈希既能对回 ``prompts/`` 下的具体版本，又不带任何原文。
    """

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: 提示词文件名的**版本号**位置 —— ``goldstd_merge.v1.md`` 里的 ``v1``。
#:
#: **版本号住在文件名里，不住在内容里** —— 改了口径就另存一版
#: （``v2``），老版留在仓库里，旧结论仍能对回它当时用的那一版。
#: 文件名认不出版本号 → 返回**空串**（不猜是哪一版）。
PROMPT_VERSION_PATTERN = re.compile(r"\.v(?P<version>\d+)\.md$", re.IGNORECASE)


def parse_prompt_version(path: Path | str) -> str:
    """从提示词文件名里取**版本号**（``goldstd_merge.v1.md`` → ``v1``）。

    认不出（文件名里没有 ``.v<N>.md``）→ 返回**空串** ——
    「没有版本号」与「版本号是 v1」是两回事，不猜。
    """

    match = PROMPT_VERSION_PATTERN.search(Path(path).name)
    if match is None:
        return ""
    return f"v{match.group('version')}"


@dataclass(frozen=True)
class PromptRef:
    """提示词文件的**引用**（路径 + 版本号 + 内容哈希）。

    ``path``
        相对项目根的路径（如 ``prompts/goldstd_merge.v1.md``）——
        底账里落它，复核的人才知道去哪儿找这一版。
    ``version``
        **版本号**（从文件名解析，如 ``v1``）。改口径就另存一版，
        文件名里的版本号就是「这批结论是哪版口径跑的」的答案。
        文件名里没有版本号 → 空串（不猜）。
    ``sha256``
        文件内容哈希 —— :func:`resolve_prompt_version` 靠它反查版本。
    """

    path: str
    sha256: str
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "version": self.version}

    @classmethod
    def from_file(cls, path: Path | str) -> "PromptRef":
        """从提示词文件造引用（文件读不出来直接报错，不静默用空哈希）。"""

        target = Path(path)
        if not target.is_file():
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"提示词文件不存在：{target}",
            )
        return cls(
            path=_relative_to_project(target),
            sha256=hash_prompt_file(target),
            version=parse_prompt_version(target),
        )


def _relative_to_project(path: Path) -> str:
    """把绝对路径压成**相对项目根**的路径（压不动就只留文件名）。"""

    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT))
    except ValueError:
        return resolved.name


def build_prompt_index(prompts_dir: Path | str) -> dict[str, str]:
    """给 ``prompts/`` 下的提示词建**内容哈希索引**：``{sha256: 相对路径}``。

    有了它，底账里一个 ``prompt_template_sha256`` 就能反查回
    「这一轮用的是哪一版提示词文件」—— 提示词改过了没有、这一轮跑的是哪版，
    事后查得清。

    目录不存在 → 报错（不静默返回空索引：空索引会让「反查不到」看起来像
    「提示词没记录」）。
    """

    directory = Path(prompts_dir)
    if not directory.is_dir():
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"提示词目录不存在：{directory}",
        )
    index: dict[str, str] = {}
    for item in sorted(directory.rglob("*")):
        if not item.is_file():
            continue
        index[hash_prompt_file(item)] = _relative_to_project(item)
    return index


def resolve_prompt_version(prompt_sha256: str, index: Mapping[str, str]) -> str:
    """按内容哈希反查提示词版本；查不到返回**空串**（不猜是哪一版）。"""

    wanted = (prompt_sha256 or "").strip()
    if not wanted:
        return ""
    return index.get(wanted, "")


@dataclass(frozen=True)
class ModelCallRecord:
    """模型调用底账里的一行。

    ``tool_call_id``
        这次模型调用**挂在哪次工具调用下面** —— 必须能在
        ``tool_calls.jsonl`` 里命中（命不中记异常，见 :func:`link_model_calls`）。
    ``model`` / ``base_url_host``
        调的是哪个模型、哪个端点主机（**只落主机名，不落完整 URL** ——
        URL 里可能带路径上的租户标识）。
    ``prompt_tokens`` / ``completion_tokens``
        端点报上来的 token 计数。端点没报就是 ``None``（**不填 0** ——
        「没用 token」与「端点没报」是两回事）。
    ``prompt_sha256`` / ``raw_response_sha256``
        **只落哈希，不落原文** —— 提示词与返回里带金标准原文，
        可能含患者可识别信息。
    ``fallback_used``
        这一轮的结构化输出是**降级拿到的**（端点不认结构化输出参数，
        或模型回的不是 JSON 做了修复重试）—— 复核的人看到它就该知道
        这一例的归并可信度与直出不同。
    ``status``
        ``ok`` / ``failed`` —— **失败的模型调用也记账**：
        「这轮归并为什么降级」要能从账上查出来。
    ``extra``
        元数据里**其余**字段（协议层的降级标记、失败类别、尝试次数…）
        **整体搬过来**。审计层**不认协议细节** —— 字段名由协议层定，
        审计层只负责原样留痕（业务层同理：引擎不拼协议参数）。
    ``prompt_version``
        **提示词版本号**（从文件名解析，如 ``v1``）。改口径就另存一版，
        这一列直接回答「这批结论是哪版口径跑的」。
    ``prompt_rendered_sha256``
        **实际发出去的那份提示词**（渲染后、填了标签表）的哈希 ——
        与 ``prompt_sha256``（提示词**文件**的哈希）分工：
        前者认「哪一版口径」，后者认「这一例具体发了什么」。
    """

    model_call_id: str
    tool_call_id: str
    model: str
    base_url_host: str
    started_at: str
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    prompt_sha256: str
    raw_response_sha256: str
    fallback_used: bool
    accession_no: str = ""
    prompt_template: str = ""
    prompt_template_sha256: str = ""
    prompt_version: str = ""
    prompt_rendered_sha256: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model_call_id": self.model_call_id,
            "tool_call_id": self.tool_call_id,
            "accession_no": self.accession_no,
            "model": self.model,
            "base_url_host": self.base_url_host,
            "started_at": self.started_at,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "prompt_sha256": self.prompt_sha256,
            "raw_response_sha256": self.raw_response_sha256,
            "fallback_used": self.fallback_used,
            "prompt_template": self.prompt_template,
            "prompt_template_sha256": self.prompt_template_sha256,
            "prompt_version": self.prompt_version,
            "prompt_rendered_sha256": self.prompt_rendered_sha256,
        }
        payload.update(dict(self.extra))
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ModelCallRecord":
        """从一行 JSON 还原成记录对象（缺必填字段的行视为损坏）。"""

        missing = missing_model_call_fields(payload)
        if missing:
            raise KeyError(f"模型调用底账行缺字段：{'、'.join(missing)}")
        extra = {
            key: value
            for key, value in payload.items()
            if key not in _EXPLICIT_RECORD_FIELDS
        }
        return cls(
            model_call_id=str(payload["model_call_id"]),
            tool_call_id=str(payload["tool_call_id"]),
            model=str(payload["model"]),
            base_url_host=str(payload["base_url_host"]),
            started_at=str(payload["started_at"]),
            latency_ms=int(payload["latency_ms"] or 0),
            prompt_tokens=(
                None if payload.get("prompt_tokens") is None else int(payload["prompt_tokens"])
            ),
            completion_tokens=(
                None
                if payload.get("completion_tokens") is None
                else int(payload["completion_tokens"])
            ),
            prompt_sha256=str(payload["prompt_sha256"]),
            raw_response_sha256=str(payload["raw_response_sha256"]),
            fallback_used=bool(payload["fallback_used"]),
            accession_no=str(payload.get("accession_no") or ""),
            prompt_template=str(payload.get("prompt_template") or ""),
            prompt_template_sha256=str(payload.get("prompt_template_sha256") or ""),
            prompt_version=str(payload.get("prompt_version") or ""),
            prompt_rendered_sha256=str(payload.get("prompt_rendered_sha256") or ""),
            extra=extra,
        )


def missing_model_call_fields(payload: Mapping[str, Any]) -> list[str]:
    """列出这行缺哪些必填字段（体检用）。

    **「缺」有两种**：字段**没有**，与字段**在但是空的**。后一种更要紧 ——
    一行写着 ``"model": ""`` 的账，看着齐全，其实答不出「调了哪个模型」。
    空值算缺。

    两处例外：

    * ``raw_response_sha256`` 在**失败**的调用上可以为空 —— 压根没有返回体
      可哈希（连不上时什么都没回来）；
    * ``prompt_tokens`` / ``completion_tokens`` 可以是 ``None`` ——
      端点没报 token 计数时**不填 0**（「没用 token」与「端点没报」是两回事）。
    """

    missing: list[str] = []
    for key in REQUIRED_MODEL_CALL_FIELDS:
        if key not in payload:
            missing.append(key)
    for key in _MUST_BE_NON_EMPTY:
        if key in payload and (
            payload[key] is None or str(payload[key]).strip() == ""
        ):
            missing.append(key)
    if (
        str(payload.get("status") or "ok") == "ok"
        and "raw_response_sha256" in payload
        and not str(payload.get("raw_response_sha256") or "").strip()
    ):
        missing.append("raw_response_sha256")
    return missing


@dataclass(frozen=True)
class ModelCallScan:
    """读回模型调用底账的体检结果。

    坏行**跳过但计数上报** —— 底账是唯一能说清「这轮归并调了哪个模型」的
    东西，读完悄悄少几行、还让人以为账是全的，比坏行本身更麻烦。
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


class ModelCallRecorder:
    """把模型调用追加到 ``runs/<run_id>/model_calls.jsonl``。"""

    def __init__(self, run_dir: Path | str) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / MODEL_CALLS_FILENAME

    def record(
        self,
        *,
        meta: Any,
        tool_call_id: str,
        accession_no: str = "",
        prompt_ref: PromptRef | None = None,
        model_call_id: str | None = None,
    ) -> ModelCallRecord:
        """追加一行并立即落盘（flush + fsync），保证中断后已写的行不丢。

        ``meta``
            网关采集的**可审计元数据**（有 ``to_dict()`` 的对象，或普通字典）。
            审计层**不逐个字段改名** —— 协议层定的字段名原样留痕，
            业务层与审计层都不认协议细节（与「业务层不拼协议参数」同一条纪律）。

        **并发安全**：持文件锁、锁内**一次** ``write`` 写完整行 ——
        多线程同时追加时行与行不会互相咬合（与工具调用底账同一套实现）。

        **必填字段缺任一 → 直接报错**：账本写半行等于没写，
        事后查「这轮调了哪个模型」会得到一个看似完整的假象。

        **两个提示词哈希各管一件事**（任务 47）：

        - ``prompt_sha256`` —— **提示词文件的内容哈希**。有外置提示词文件时
          （本产品的常态）它就是文件哈希，能直接反查回
          ``prompts/goldstd_merge.v1.md`` 那一版 —— 「这批结论是哪版口径
          跑的」由此可追。没有文件（内联提示词）时退回协议层给的渲染哈希。
        - ``prompt_rendered_sha256`` —— **实际发出去的那份提示词**的哈希
          （渲染后、填了标签表）。它认的是「这一例具体发了什么」。

        两个都是哈希，**不落原文** —— 提示词里可能带金标准原文
        （可能含患者可识别信息）。
        """

        fields = dict(meta.to_dict()) if hasattr(meta, "to_dict") else dict(meta)
        extra = {
            key: value
            for key, value in fields.items()
            if key not in _EXPLICIT_RECORD_FIELDS
        }
        rendered_sha = str(fields.get("prompt_sha256") or "").strip()
        template_sha = (getattr(prompt_ref, "sha256", "") or "").strip()
        record = ModelCallRecord(
            model_call_id=model_call_id or new_model_call_id(),
            tool_call_id=(tool_call_id or "").strip(),
            model=str(fields.get("model") or "").strip(),
            base_url_host=str(fields.get("base_url_host") or "").strip(),
            started_at=str(fields.get("started_at") or ""),
            latency_ms=int(fields.get("latency_ms") or 0),
            prompt_tokens=fields.get("prompt_tokens"),
            completion_tokens=fields.get("completion_tokens"),
            # 提示词**文件**的哈希优先（能反查版本）；没有文件才用渲染哈希。
            prompt_sha256=template_sha or rendered_sha,
            raw_response_sha256=str(fields.get("raw_response_sha256") or "").strip(),
            fallback_used=bool(fields.get("fallback_used")),
            accession_no=(accession_no or "").strip(),
            prompt_template=(getattr(prompt_ref, "path", "") or "").strip(),
            prompt_template_sha256=template_sha,
            prompt_version=(getattr(prompt_ref, "version", "") or "").strip(),
            prompt_rendered_sha256=rendered_sha,
            extra=extra,
        )
        missing = missing_model_call_fields(record.to_dict())
        if missing:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"模型调用底账缺字段：{'、'.join(missing)}",
            )
        payload = json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
        with open(self.path, "ab") as handle:
            with _exclusive_lock(handle):
                handle.write(payload.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        return record

    def scan(self) -> ModelCallScan:
        """读回底账并体检（坏行跳过但计数上报）。"""

        if not self.path.is_file():
            return ModelCallScan()
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
                    ModelCallRecord.from_dict(payload)
                except (KeyError, TypeError, ValueError):
                    corrupt += 1
                    continue
                records.append(payload)
        return ModelCallScan(
            records=records,
            total_lines=total,
            corrupt_lines=corrupt,
            blank_lines=blank,
        )

    def read_all(self) -> list[dict[str, Any]]:
        """读回全部底账行（损坏行跳过，不让一行坏数据毁掉整本账）。"""

        return self.scan().records

    def find_by_tool_call_id(self, tool_call_id: str) -> list[dict[str, Any]]:
        """按 ``tool_call_id`` 取出这次工具调用下面的**全部**模型调用。"""

        wanted = (tool_call_id or "").strip()
        if not wanted:
            return []
        return [
            record
            for record in self.read_all()
            if str(record.get("tool_call_id") or "") == wanted
        ]


# --------------------------------------------------------------------------
# 与工具调用底账的关联
# --------------------------------------------------------------------------


def index_model_calls(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """按 ``tool_call_id`` 给模型调用底账建索引。

    值是**列表** —— 同一个 ``tool_call_id`` 下面理论上可以有多次模型调用
    （例如重跑后又在同一轮里重试），压成一条会把前面的账抹掉。
    """

    index: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        call_id = str(record.get("tool_call_id") or "").strip()
        if not call_id:
            continue
        index.setdefault(call_id, []).append(dict(record))
    return index


@dataclass(frozen=True)
class ModelCallLinkReport:
    """模型调用底账与工具调用底账的**关联体检**。

    ``linked``
        能挂上工具调用底账的模型调用条数。
    ``unlinked``
        **异常清单**：挂不上的模型调用（``tool_call_id`` 在
        ``tool_calls.jsonl`` 里找不到）。这类账是「孤儿」——
        事后按 id 反查会查不到，必须看得见。
    ``tool_calls_without_model``
        工具调用底账里**没有对应模型调用**的 id（正常情况：这一例没走
        语义归并，或归并之前就失败了）。单列出来，便于判断是「本来就没有」
        还是「应该有却没有」。
    """

    linked: int = 0
    unlinked: list[dict[str, Any]] = field(default_factory=list)
    tool_calls_without_model: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "linked": self.linked,
            "unlinked": list(self.unlinked),
            "tool_calls_without_model": list(self.tool_calls_without_model),
        }


def link_model_calls(
    tool_records: Iterable[Mapping[str, Any]],
    model_records: Iterable[Mapping[str, Any]],
) -> ModelCallLinkReport:
    """把模型调用底账与工具调用底账**按 id 关联**，并把挂不上的记成异常。

    * 模型调用的 ``tool_call_id`` 能在工具底账里命中 → 关联成立（``linked``）；
    * 命不中 → 进 ``unlinked`` 异常清单（带原因），**不静默丢**；
    * 工具调用没有对应模型调用 → 进 ``tool_calls_without_model``。
    """

    tool_ids = {
        str(record.get("tool_call_id") or "").strip()
        for record in tool_records
        if isinstance(record, Mapping)
    }
    tool_ids.discard("")

    linked = 0
    unlinked: list[dict[str, Any]] = []
    used: set[str] = set()
    for record in model_records:
        if not isinstance(record, Mapping):
            continue
        call_id = str(record.get("tool_call_id") or "").strip()
        if call_id and call_id in tool_ids:
            linked += 1
            used.add(call_id)
            continue
        unlinked.append(
            {
                "model_call_id": str(record.get("model_call_id") or ""),
                "tool_call_id": call_id,
                "accession_no": str(record.get("accession_no") or ""),
                "model": str(record.get("model") or ""),
                "reason": (
                    "工具调用底账里没有这个 tool_call_id，模型调用无处关联"
                    if call_id
                    else "模型调用没带 tool_call_id，无法与工具调用底账关联"
                ),
            }
        )

    return ModelCallLinkReport(
        linked=linked,
        unlinked=unlinked,
        tool_calls_without_model=sorted(tool_ids - used),
    )


def summarize_link_report(report: ModelCallLinkReport) -> str:
    """把关联体检结果压成一句中文（给终端与产物用）。"""

    total = report.linked + len(report.unlinked)
    if total == 0:
        return "本轮没有模型调用（未启用语义归并）"
    text = f"模型调用 {total} 条：关联成立 {report.linked} 条"
    if report.unlinked:
        text += f"，关联不上 {len(report.unlinked)} 条"
    if report.tool_calls_without_model:
        text += f"，无对应模型调用的工具调用 {len(report.tool_calls_without_model)} 条"
    return text


def find_model_calls(
    model_records: Sequence[Mapping[str, Any]],
    tool_call_id: str,
) -> list[dict[str, Any]]:
    """按 ``tool_call_id`` 取出模型调用行（反查链路的入口）。"""

    wanted = (tool_call_id or "").strip()
    if not wanted:
        return []
    return [
        dict(record)
        for record in model_records
        if str(record.get("tool_call_id") or "") == wanted
    ]
