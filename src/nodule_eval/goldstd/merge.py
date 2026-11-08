"""语义归并（任务 21）：金标准自由文本 → 固定标签集（走模型端点）。

规则基线（:mod:`nodule_eval.goldstd.rules`）在**词形**上认得准，但认不出的
东西一律留 ``None``。语义归并用**模型**补这一层：同一件事有很多种写法
（"约 8mm 磨玻璃" / "8mm GGN" / "右肺上叶见一磨玻璃小结节"），模型负责把它们
收敛到**同一张标签表**上。

**这一层的价值不在"模型更聪明"，在"它必须能被程序复核"**：

- **回指校验（核心）**：模型给每个标签都必须附一段**原文里连续存在的子串**。
  程序拿这段子串回原文里找 —— 找不到就整份输出作废（标签可能是编的）；
  找到就说明模型确实是从这段原文读出来的。
- **标签复核**：部位与密度只能在**等价表**
  （:mod:`nodule_eval.goldstd.labels`，金标准侧与模型侧共用的唯一真源）里取值；
  尺寸必须与原文里**确实写着**的某个尺寸对得上（``8mm`` / ``0.8cm`` 都算 ``8.0``）。
- **原文不可变**：归并结果只允许写进展示列（``model_findings`` /
  ``normalize_source``），**金标准原文一个字都不许动** —— 由
  :func:`apply_merge_updates` 的断言保证，不是靠约定。

**为什么解析失败就降级、不硬扛**：模型这一层是**可选增益**，不是主干。
模型挂了、输出不合契约、回指对不上 —— 这三种情况下规则基线仍然能给出
一份**确定的**结论（读不出就留 ``None`` 挂人工复核）。所以这里一律
**降级到规则基线**（``normalize_source=rule``），而不是把这一例判失败：
判失败等于把"没跑成"算成"模型没看见"，敏感度会被凭空拉低。

**端口（Port）设计**：本模块只依赖 :class:`ModelGatewayPort` 这一个最小接口
——「发一段对话，拿回一段原文」。任务 22 的 ``ModelGateway``（OpenAI-compatible
原始 HTTP）实现它；业务层不 import 任何厂商 SDK。

**源产品对照**：参考实现把整段自由文本交给 LLM 综合成**一句结论**就往上送
（``github_ref/MedRAX/medrax/agent/agent.py:104-131`` 的 ``process_request``；
``github_ref/MedRAX/interface.py:142`` 只把临时路径从回答里剔掉就往上送）——
**没有任何字段级的结构化约束**。本模块的输出契约、标签表约束与回指校验
都是**新建**的；唯一从源产品搬过来的是「提示词外置成可版本化文件」这一条做法
（``github_ref/MedRAX/main.py:47-49`` 的 ``load_prompts_from_file``）。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..audit.modelcall import PromptRef
from ..contracts import (
    MERGE_FORBIDDEN_OUTPUT_FIELDS,
    MERGE_LESION_FIELDS,
    MERGE_OUTPUT_FIELDS as _CONTRACT_MERGE_OUTPUT_FIELDS,
    MERGE_REQUIRED_FIELDS,
)
from ..errors import ErrorCode, NoduleEvalError, format_message
from ..sheets.columns import NEW_COLUMNS
from ..structured import strip_json_fence
from .labels import (
    canonical_densities,
    canonical_locations,
    find_densities,
    find_locations,
    resolve_density,
    resolve_location,
)
from .prompt import (
    DEFAULT_PROMPT_PATH,
    DENSITIES_TOKEN,
    LOCATIONS_TOKEN,
    load_prompt_bundle,
)
from .rules import find_sizes, merge_by_rules
from .schema import (
    NORMALIZE_SOURCE_MODEL,
    GoldLabels,
    LesionLabels,
)

__all__ = [
    "DEFAULT_PROMPT_PATH",
    "MERGE_JSON_SCHEMA",
    "MERGE_OUTPUT_FIELDS",
    "MERGE_WRITABLE_COLUMNS",
    "ModelGatewayPort",
    "ModelMergeNormalizer",
    "ModelMergeResult",
    "ParsedMergeOutput",
    "apply_merge_updates",
    "build_merge_prompt",
    "merge_by_model",
    "merge_updates_for_labels",
    "parse_merge_output",
    "validate_evidence_span",
]


#: 语义归并的**输出契约** —— 七个字段，少一个都算解析失败。
#:
#: **这份清单的真源在** :mod:`nodule_eval.contracts`（任务 47）——
#: 提示词加载、解析层、提示词示例校验三处共用同一份，避免各写一份漂移。
#:
#: ``presence`` / ``location`` / ``size_mm`` / ``density``
#:     标签四元组（取值见 :class:`~nodule_eval.goldstd.schema.GoldLabels`）。
#:     这四项**缺任一即判解析失败** —— 模型少给一项，这一例就走规则降级。
#:     （模型归并的价值恰恰在属性层：它能读出"约 8mm GGN"这种规则读不全的写法。）
#: ``evidence_span``
#:     **回指锚点**：原文里一段连续子串，支撑上面四项的结论。
#:     程序拿它回原文里找（见 :func:`validate_evidence_span`），找不到就作废。
#: ``confidence``
#:     模型自评置信度，缺省填 0.0 并**记一次解析降级**（不因此作废）。
#: ``needs_human_review``
#:     模型自认「这一例我拿不准」，缺省填 False 并**记一次解析降级**。
MERGE_OUTPUT_FIELDS: tuple[str, ...] = _CONTRACT_MERGE_OUTPUT_FIELDS

#: 缺任一即判**解析失败**（整份输出作废，走规则降级）的字段。
REQUIRED_MERGE_FIELDS: tuple[str, ...] = ("presence", "location", "size_mm", "density")

#: 契约里的必填字段（含 ``evidence_span``）—— 与 :data:`MERGE_REQUIRED_FIELDS`
#: 同一个清单，这里只是把「回指锚点」也显式列出来（解析层单独查它，
#: 报错文案要更具体：没有它就无法复核）。
if set(REQUIRED_MERGE_FIELDS) | {"evidence_span"} != set(MERGE_REQUIRED_FIELDS):
    raise RuntimeError("语义归并的必填字段与输出契约不一致（实现层自检）")

#: 缺省填默认值并**记一次解析降级**（不因此作废）的字段。
OPTIONAL_MERGE_FIELDS: tuple[str, ...] = ("confidence", "needs_human_review")

#: 语义归并的 **JSON Schema**（任务 23）—— 请求时按 OpenAI-compatible 的标准形状
#: 附上 ``response_format``，端点不认就回退成 strict JSON 提示词。
#:
#: **必填字段（5 个）** = 标签四项 + 回指锚点：缺任一即判解析失败。
#: ``confidence`` 与 ``needs_human_review`` 是**模型自评字段**，缺省时填默认值并
#: **记一次解析降级**（任务 21 已定的口径）—— 它们是模型对「自己拿不拿得准」的
#: 评价，不是临床结论，缺一个不至于把整例作废。
#:
#: **网关层只按 ``required`` 校验，绝不替模型补默认值** ——
#: 补了就等于替模型编结论，这条结论的出处就查不清了。
MERGE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "presence": {
            "type": ["boolean", "null"],
            "description": "原文有没有写明存在肺结节：true 有 / false 没有 / null 读不出",
        },
        "location": {
            "type": ["string", "null"],
            "description": "病灶部位标准标签（取值见部位等价表），原文没写就 null",
        },
        "size_mm": {
            "type": ["number", "null"],
            "description": "病灶最大径（毫米），原文没写就 null",
        },
        "density": {
            "type": ["string", "null"],
            "description": "密度标准标签（取值见密度等价表），原文没写就 null",
        },
        "evidence_span": {
            "type": "string",
            "description": "支撑上述结论的原文连续片段，必须逐字出现在原文里",
        },
        "confidence": {
            "type": ["number", "null"],
            "description": "模型自评置信度，0 到 1 之间",
        },
        "needs_human_review": {
            "type": "boolean",
            "description": "模型是否自认这一例拿不准",
        },
        "lesions": {
            "type": "array",
            "description": "可选：原文写了几处病灶就写几条",
            "items": {
                "type": "object",
                "properties": {
                    "location": {"type": ["string", "null"]},
                    "size_mm": {"type": ["number", "null"]},
                    "density": {"type": ["string", "null"]},
                    "evidence_span": {"type": "string"},
                },
                "required": ["location", "size_mm", "density", "evidence_span"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["presence", "location", "size_mm", "density", "evidence_span"],
    "additionalProperties": False,
}

#: 语义归并的 system prompt 文件（外置、可版本化 —— 沿用源产品
#: ``main.py:47-49`` 的 ``load_prompts_from_file`` 做法）。
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "goldstd_merge.v1.md"

#: 提示词里的标签表占位符 —— 由等价表在运行时填进去，
#: **标签表换了不用改提示词**（标签表是唯一真源）。
#:
#: 真源在 :mod:`nodule_eval.goldstd.prompt`（任务 47 拆出），
#: 这里只做转出，老代码按 ``merge._LOCATIONS_TOKEN`` 取值的用法不破。
_LOCATIONS_TOKEN = LOCATIONS_TOKEN
_DENSITIES_TOKEN = DENSITIES_TOKEN

#: 尺寸复核的容差（毫米）：原文写 ``8mm``、模型答 ``8.0``，差值 0；
#: 原文写 ``0.8cm``、模型答 ``8``，换算后也是 0。这里留一点余量给
#: 「约 8mm」这类四舍五入的读法。
_SIZE_TOLERANCE_MM = 0.05

#: 归并结果**允许写入**的列 —— 既有三列（尤其 ``gold_standard_text``）永不写。
#:
#: 这不是约定，是 :func:`apply_merge_updates` 里的断言：写别的列直接抛错。
MERGE_WRITABLE_COLUMNS: tuple[str, ...] = ("model_findings", "normalize_source")

#: 原文里已有的列 —— 归并结果碰它们就是改写金标准原文。
_READONLY_COLUMNS: tuple[str, ...] = ("accession_no", "gold_standard_text", "image_ref")

#: **触发降级的错误码**（任务 24）。
#:
#: 这些码的共同点是「**模型这一层没成功**」，而不是「这一例没结论」：
#:
#: - ``MODEL_UNAVAILABLE`` —— 端点连不上 / 超时 / 服务端报错（网关侧分类）；
#: - ``MODEL_REQUEST_REJECTED`` —— 端点拒绝了请求（提示词或参数不对）；
#: - ``FIELD_MAPPING_MISSING`` —— 端点回了，但响应形状不对（缺字段）；
#: - ``CONTRACT_INVALID`` —— 回来了，但**不合输出契约**（解析失败 / 必填字段缺失）。
#:
#: 命中即降级到规则基线，**不判这一例失败** —— 判失败等于把
#: 「模型这一层没跑成」算成「模型没看见」，敏感度会被凭空拉低。
#:
#: 其它错误码（契约层自己算错、存储写不动这类）不在此列：它们不是
#: 「模型不可用」，而是本产品自己的问题，应该原样往上抛。
DEGRADE_CAUSE_CODES: tuple[ErrorCode, ...] = (
    ErrorCode.MODEL_UNAVAILABLE,
    ErrorCode.MODEL_REQUEST_REJECTED,
    ErrorCode.FIELD_MAPPING_MISSING,
    ErrorCode.CONTRACT_INVALID,
)


# --------------------------------------------------------------------------
# 端口：模型端点的最小接口
# --------------------------------------------------------------------------


@runtime_checkable
class ModelGatewayPort(Protocol):
    """语义归并要用的**最小**模型接口。

    任务 22 的 ``ModelGateway``（OpenAI-compatible 原始 HTTP）实现它。
    这里只声明**发什么、拿回什么**，不关心传输细节 ——
    业务层不 import 任何厂商 SDK（换院内自建端点或云端点只改网关实现）。
    """

    def complete(
        self,
        *,
        system: str,
        user: str,
        json_schema: Mapping[str, Any] | None = None,
    ) -> str:
        """发一段对话，拿回模型的**原始文本**。

        参数
        ----
        system
            system prompt（语义归并的口径说明）。
        user
            用户消息（金标准原文）。
        json_schema
            结构化输出的 JSON Schema。端点不支持时网关自己降级
            （任务 23 的 ``strict JSON 解析 + 1 次修复重试``），
            本模块只管拿回文本后**自己解析并校验**。

        返回
        ----
        模型回复的原始文本（可能是 JSON，也可能是带围栏的 JSON）。
        """


# --------------------------------------------------------------------------
# 结果类型
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelMergeResult:
    """一次语义归并的结果。

    ``labels``
        与规则基线**同一份**契约（:class:`~nodule_eval.goldstd.schema.GoldLabels`），
        任务 25 的「规则 vs 模型」对照表才能逐字段直接比。
    ``degraded``
        是否**降级**了：模型不可达 / 输出不合契约 / 回指对不上，
        三种情况都退回规则基线，这一列标 ``normalize_source=rule``。
    ``reason``
        降级原因（中文，给复核的人看）。
    ``raw``
        模型返回的**原始文本**（截断到 2000 字）—— 事后能查模型当时说了什么。
    """

    labels: GoldLabels
    degraded: bool = False
    reason: str = ""
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "labels": self.labels.to_dict(),
            "degraded": self.degraded,
            "reason": self.reason,
            "raw": self.raw[:2000],
        }


@dataclass(frozen=True)
class ParsedMergeOutput:
    """解析后的模型输出（还没复核）。"""

    presence: bool | None
    location: str | None
    size_mm: float | None
    density: str | None
    evidence_span: str
    confidence: float
    needs_human_review: bool
    degraded_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "presence": self.presence,
            "location": self.location,
            "size_mm": self.size_mm,
            "density": self.density,
            "evidence_span": self.evidence_span,
            "confidence": self.confidence,
            "needs_human_review": self.needs_human_review,
            "degraded_fields": list(self.degraded_fields),
        }


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------


def build_merge_prompt(
    prompt_path: Path | str | None = None,
    *,
    locations: Sequence[str] | None = None,
    densities: Sequence[str] | None = None,
) -> str:
    """读语义归并的 system prompt，并把**标签表**填进去。

    提示词外置成可版本化文件（沿用源产品 ``main.py:47-48`` 的做法）：
    改归并口径只改 ``prompts/goldstd_merge.v1.md``，不动判定逻辑代码。

    标签表在运行时从等价表填进 ``{{LOCATIONS}}`` / ``{{DENSITIES}}`` 占位符 ——
    **标签表换了不用改提示词**（等价表是唯一真源，提示词里不许手写标签）。

    **口径校验也在这里做**（任务 47）：提示词声明的字段要与输出契约一致、
    安全条款（「不改写原文」「不输出诊断」）要写明 —— 不满足就抛
    ``CONTRACT_INVALID``。文件不存在 / 占位符缺失同样报错，
    **不回落内置兜底提示词**（兜底提示词会让「这轮用的哪版口径」查不出来）。

    要拿**版本号与文件哈希**（落模型调用底账用），
    用 :func:`nodule_eval.goldstd.prompt.load_prompt_bundle`。
    """

    bundle = load_prompt_bundle(
        prompt_path,
        locations=locations,
        densities=densities,
        # 自定义路径（试写、临时对照）不强求文件名带版本号；
        # 缺省提示词必须带（``goldstd_merge.v1.md``）。
        require_version=prompt_path is None,
    )
    return bundle.rendered_text


# --------------------------------------------------------------------------
# 解析与校验
# --------------------------------------------------------------------------


def _strip_json_fence(raw: str) -> str:
    """剥掉 ```json ... ``` 围栏。

    模型很爱加围栏 —— 剥掉是**格式**层面的规整，不改内容。
    围栏外还有别的话（散文、解释）→ 解析失败（走降级），不猜。

    实现复用 :mod:`nodule_eval.structured` 的同一份（协议层与业务层不各剥一次）。
    """

    return strip_json_fence(raw)


def _coerce_float(value: Any) -> float | None:
    """把尺寸取值读成毫米数值。

    模型可能回 ``"8"`` / ``"8mm"`` / ``8.0`` —— 三种都读成 ``8.0``。
    读不出来（``"约八毫米"``）返回 ``None``，**不猜**。
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"(\d+(?:\.\d+)?)", value)
        if match is None:
            return None
        text = value.lower()
        if "cm" in text or "厘米" in text or "公分" in text:
            return round(float(match.group(1)) * 10.0, 1)
        return round(float(match.group(1)), 1)
    return None


def parse_merge_output(raw: str) -> ParsedMergeOutput:
    """把模型返回的文本解析成结构化输出。

    **解析失败 = 整份输出作废**（走规则降级）。判失败的条件：

    - 不是合法 JSON；
    - 顶层不是对象；
    - **带了诊断结论类字段**（``diagnosis`` / ``impression`` / ``诊断`` …）——
      提示词里写了「不输出诊断」，但**光靠提示词拦不住**；
      本产品只做结构化标注，输出里冒出诊断结论就判作废
      （任务 47 的安全边界落到代码）；
    - ``presence`` / ``location`` / ``size_mm`` / ``density`` **缺任一**；
    - 字段类型不对（``presence`` 必须是布尔或 null，``size_mm`` 必须是数值）。

    ``confidence`` 与 ``needs_human_review`` 缺省时**填默认值并记一次解析降级**
    —— 模型少给一个自评字段，不值得把整例丢掉。
    """

    text = _strip_json_fence(raw)
    if not text:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail="模型返回为空，无法解析成结构化归并结果",
        )
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"模型返回不是合法 JSON：{exc}",
        ) from exc
    if not isinstance(payload, Mapping):
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"模型返回的顶层须是对象（实得 {type(payload).__name__}）",
        )

    forbidden = sorted(
        name
        for name in payload
        if str(name).strip().lower() in {item.lower() for item in MERGE_FORBIDDEN_OUTPUT_FIELDS}
    )
    if forbidden:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                f"模型归并结果带了诊断结论类字段：{'、'.join(forbidden)}"
                "（本产品只做结构化标注，不接受诊断 / 结论 / 建议类输出）"
            ),
        )

    missing = [name for name in REQUIRED_MERGE_FIELDS if name not in payload]
    if missing:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"模型归并结果缺字段：{'、'.join(missing)}",
        )

    presence = payload.get("presence")
    if presence is not None and not isinstance(presence, bool):
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"模型归并结果的 presence 须是布尔或 null（实得 {type(presence).__name__}）",
        )

    location = payload.get("location")
    if location is not None and not isinstance(location, str):
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"模型归并结果的 location 须是字符串或 null（实得 {type(location).__name__}）",
        )

    density = payload.get("density")
    if density is not None and not isinstance(density, str):
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"模型归并结果的 density 须是字符串或 null（实得 {type(density).__name__}）",
        )

    raw_size = payload.get("size_mm")
    size_mm = _coerce_float(raw_size)
    if raw_size is not None and size_mm is None:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"模型归并结果的 size_mm 读不出数值（实得 {raw_size!r}）",
        )

    evidence_span = payload.get("evidence_span")
    if not isinstance(evidence_span, str) or not evidence_span.strip():
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail="模型归并结果缺 evidence_span（回指锚点，没有它无法复核）",
        )

    degraded_fields: list[str] = []

    confidence = payload.get("confidence")
    if confidence is None:
        confidence = 0.0
        degraded_fields.append("confidence")
    else:
        parsed_confidence = _coerce_float(confidence)
        if parsed_confidence is None:
            parsed_confidence = 0.0
            degraded_fields.append("confidence")
        else:
            confidence = min(1.0, max(0.0, parsed_confidence))

    needs_review = payload.get("needs_human_review")
    if not isinstance(needs_review, bool):
        needs_review = False
        degraded_fields.append("needs_human_review")

    return ParsedMergeOutput(
        presence=presence,
        location=location,
        size_mm=size_mm,
        density=density,
        evidence_span=evidence_span.strip(),
        confidence=confidence,
        needs_human_review=needs_review,
        degraded_fields=tuple(degraded_fields),
    )


def validate_evidence_span(
    span: str,
    original: str,
    *,
    normalized: str | None = None,
) -> tuple[bool, str]:
    """**回指校验**：这段证据是不是原文里连续存在的一段。

    返回 ``(是否命中, 中文说明)``。

    判据（两条都过才算命中）：

    1. 证据段是**原文的连续子串**（空白差异不算数 —— 归一后逐字比对）；
    2. 证据段在**归一副本**里也命中（读取层产出的分析用副本）。

    为什么要求「连续」：模型如果把两段不相邻的话拼成一句当证据，
    那句话在原文里**不存在**，回指不上 —— 说明它不是在读原文，是在编。

    原文与归一副本都不命中 → 判解析失败（走规则降级）。
    """

    if not span or not span.strip():
        return False, "证据片段为空"

    probe = span.strip()
    haystack = original or ""
    if probe not in haystack:
        squashed = re.sub(r"\s+", "", haystack)
        if re.sub(r"\s+", "", probe) not in squashed:
            return False, "证据片段在金标准原文中不存在（模型可能编了一句）"

    if normalized:
        normalized_probe = re.sub(r"\s+", "", normalized)
        if re.sub(r"\s+", "", probe) not in normalized_probe:
            return False, "证据片段在金标准归一副本中不存在"

    return True, "证据片段已回指到金标准原文"


def _validate_labels(
    parsed: ParsedMergeOutput,
    original: str,
    *,
    normalized: str | None = None,
) -> tuple[bool, str, dict[str, str]]:
    """**标签复核**：模型给的每个标签都能在原文里找到出处。

    四条：

    - **存在性**：模型引用的那段证据**本身就得能支撑它的结论** ——
      判「有病灶」那段话里必须**真的有病灶词**且不被否定词压住，
      判「没病灶」那段话里必须**真的有否定词**。
      这条挡住的是「模型编一个结论再配一段真的原文」：
      拿「双肺未见明显结节影」当证据说「有病灶」，一眼就假。
    - **部位**：取值须能在等价表里归一（``右下肺`` → ``右肺下叶``），
      且**原文里确实写着**这个部位的某个别名；写不出来就是模型编的。
    - **密度**：同上。
    - **尺寸**：原文里确实写着某个尺寸，且换算成毫米后与模型给的对得上
      （容差 :data:`_SIZE_TOLERANCE_MM` 毫米）。

    返回 ``(是否通过, 说明, 各标签在原文里的出处)``。出处落进逐例产物，
    复核的人一眼看到「这个部位是从原话哪一句读出来的」。

    任何一条不成立 → 判解析失败（走规则降级）。
    """

    haystack = normalized if normalized else original
    evidence: dict[str, str] = {}

    if parsed.presence is not None:
        rule_presence = merge_by_rules(parsed.evidence_span).labels.presence
        if rule_presence is not None and rule_presence != parsed.presence:
            return (
                False,
                (
                    f"模型判存在性={parsed.presence}，但它引用的那段证据"
                    f"（{parsed.evidence_span[:30]!r}）本身读出的是 "
                    f"{rule_presence}（结论与证据矛盾）"
                ),
                evidence,
            )
        if rule_presence is None:
            # 确定性读法读不出存在性，模型却给了个结论 ——
            # **接受**（这正是模型比规则强的地方：有的写法规则读不出），
            # 但挂人工复核，让人看一眼。
            evidence["presence"] = parsed.evidence_span
            evidence["presence_uncertain"] = "确定性读法读不出存在性，模型给了结论"
        else:
            evidence["presence"] = parsed.evidence_span

    if parsed.location:
        resolved = resolve_location(parsed.location)
        if resolved is None:
            return False, f"部位取值 {parsed.location!r} 不在部位等价表里", evidence
        canonical = resolved.canonical
        found = find_locations(haystack)
        matched = next((item for item in found if item.canonical == canonical), None)
        if matched is None:
            return False, f"原文里找不到部位 {canonical!r}（模型可能编了部位）", evidence
        evidence["location"] = matched.matched_text or canonical

    if parsed.density:
        resolved = resolve_density(parsed.density)
        if resolved is None:
            return False, f"密度取值 {parsed.density!r} 不在密度等价表里", evidence
        canonical = resolved.canonical
        found = find_densities(haystack)
        matched = next((item for item in found if item.canonical == canonical), None)
        if matched is None:
            return False, f"原文里找不到密度 {canonical!r}（模型可能编了密度）", evidence
        evidence["density"] = matched.matched_text or canonical

    if parsed.size_mm is not None:
        sizes = find_sizes(haystack)
        if not sizes:
            return False, "原文里没有尺寸，但模型给了一个尺寸", evidence
        matched = next(
            (item for item in sizes if abs(item.value_mm - parsed.size_mm) <= _SIZE_TOLERANCE_MM),
            None,
        )
        if matched is None:
            return False, (
                f"原文里的尺寸（{'、'.join(f'{item.value_mm}mm' for item in sizes)}）"
                f"与模型给的 {parsed.size_mm}mm 对不上"
            ), evidence
        evidence["size_mm"] = matched.matched_text

    return True, "标签已复核", evidence


# --------------------------------------------------------------------------
# 归并主流程
# --------------------------------------------------------------------------


def _supports_kwarg(func: Any, name: str) -> bool:
    """这个可调用对象接不接某个关键字参数（签名里查，不靠试）。"""

    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - 内建对象取不到签名
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == name and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def _call_model(
    gateway: ModelGatewayPort,
    *,
    system: str,
    user: str,
    tool_call_id: str = "",
) -> str:
    """向模型端点要一次归并结果，返回**原始文本**。

    **优先走结构化通道**（网关实现了 ``complete_structured`` 时）：
    按 :data:`MERGE_JSON_SCHEMA` 请求 → 端点不认 ``response_format`` 就回退成
    strict JSON 提示词 → 模型回的不是 JSON 就做 1 次修复重试 →
    必填字段缺任一即判解析失败。**这条通道的元数据**（提示词哈希、token 计数、
    延迟、是否走过回退）由网关采集，事后可审计。

    ``tool_call_id``
        本次归并**挂在哪次工具调用下面**（任务 33）。传下去，模型调用底账
        里那一行的 ``tool_call_id`` 就能在 ``tool_calls.jsonl`` 里命中 ——
        结论 → 工具调用 → 模型调用，三级链路一次走通。
        网关不接这个参数（只实现了端口的最小形态）时不传 —— **业务层不因此改口径**。

    网关只实现了端口（``complete``）时走裸文本路径 —— 业务层不因此改口径：
    解析与回指校验在下面照做一遍。
    """

    structured = getattr(gateway, "complete_structured", None)
    if callable(structured):
        kwargs: dict[str, Any] = {
            "system": system,
            "user": user,
            "json_schema": MERGE_JSON_SCHEMA,
        }
        if tool_call_id and _supports_kwarg(structured, "tool_call_id"):
            kwargs["tool_call_id"] = tool_call_id
        result = structured(**kwargs)
        return str(result.raw)
    kwargs = {"system": system, "user": user, "json_schema": None}
    if tool_call_id and _supports_kwarg(gateway.complete, "tool_call_id"):
        kwargs["tool_call_id"] = tool_call_id
    return gateway.complete(**kwargs)


def merge_by_model(
    original: str,
    *,
    gateway: ModelGatewayPort,
    normalized: str | None = None,
    prompt: str | None = None,
    tool_call_id: str = "",
) -> ModelMergeResult:
    """走模型做一次语义归并；**任何一步失败都降级到规则基线**。

    参数
    ----
    original
        金标准**原文**。模型读的就是它，回指校验比对的也是它。
    gateway
        模型端点（任务 22 的 ``ModelGateway`` 实现 :class:`ModelGatewayPort`）。
    normalized
        读取层产出的**分析用副本**（可选）。给了就用它做标签复核，
        原文一个字都不动。
    prompt
        system prompt（不给就现读 ``prompts/goldstd_merge.v1.md``）。
    tool_call_id
        本次归并挂在哪次工具调用下面（任务 33）。给了，模型调用底账里
        那一行就能在 ``tool_calls.jsonl`` 里命中。

    返回
    ----
    :class:`ModelMergeResult`。降级时 ``labels`` 是规则基线的结果、
    ``source=rule``、``degraded=True``、``reason`` 写明为什么降级。
    """

    system = prompt if prompt is not None else build_merge_prompt()

    try:
        raw = _call_model(
            gateway, system=system, user=original, tool_call_id=tool_call_id
        )
    except NoduleEvalError as exc:
        # **模型这一层没成功** → 这一例改用规则基线，不是「这一例失败」。
        #
        # 传输层失败（端点不可达 / 被拒 / 响应缺字段）与输出不合契约
        # 走**同一条**降级路径：结论照常出，只是改用确定性口径来判，
        # 并在例上留下降级标记（``degraded`` / 降级原因 / 原因错误码），
        # 由引擎层单独计「降级数」（任务 24）。
        #
        # 本产品自己的问题（契约算错、存储写不动）不在降级名单里 ——
        # 那些原样往上抛，别拿「降级」把真问题盖住。
        if exc.code in DEGRADE_CAUSE_CODES:
            return _degrade_to_rules(
                original,
                normalized=normalized,
                reason=_degrade_reason(exc),
                error_code=exc.code.value,
            )
        raise
    except Exception as exc:  # noqa: BLE001 - 网关抛什么都算「模型不可达」
        return _degrade_to_rules(
            original,
            normalized=normalized,
            reason=_degrade_reason(exc),
            error_code=ErrorCode.MODEL_UNAVAILABLE.value,
        )

    try:
        parsed = parse_merge_output(raw)
    except NoduleEvalError as exc:
        return _degrade_to_rules(
            original,
            normalized=normalized,
            reason=f"模型输出解析失败：{exc.detail}",
            error_code=exc.code.value,
            raw=raw,
        )

    ok, why = validate_evidence_span(parsed.evidence_span, original, normalized=normalized)
    if not ok:
        return _degrade_to_rules(
            original,
            normalized=normalized,
            reason=f"证据回指校验未通过：{why}",
            error_code=ErrorCode.CONTRACT_INVALID.value,
            raw=raw,
        )

    ok, why, evidence = _validate_labels(parsed, original, normalized=normalized)
    if not ok:
        return _degrade_to_rules(
            original,
            normalized=normalized,
            reason=f"标签复核未通过：{why}",
            error_code=ErrorCode.CONTRACT_INVALID.value,
            raw=raw,
        )

    labels = GoldLabels(
        presence=parsed.presence,
        location=(
            resolve_location(parsed.location).canonical if parsed.location else None
        ),
        size_mm=parsed.size_mm,
        density=(resolve_density(parsed.density).canonical if parsed.density else None),
        lesions=_build_lesions(parsed, evidence),
        evidence_span=parsed.evidence_span,
        confidence=parsed.confidence,
        needs_human_review=(
            parsed.needs_human_review
            or parsed.presence is None
            or "presence_uncertain" in evidence
        ),
        source=NORMALIZE_SOURCE_MODEL,
    )
    return ModelMergeResult(labels=labels, degraded=False, reason="", raw=raw)


def _build_lesions(
    parsed: ParsedMergeOutput,
    evidence: Mapping[str, str],
) -> tuple[LesionLabels, ...]:
    """把归并结果落成**多病灶标签集合**（任务 20 的契约）。

    规则基线的多病灶口径是「逐处病灶一条」；语义归并这边模型只给一组标签，
    所以这里落**一条**（顶层四元组本身），并把各标签在原文里的出处记进去 ——
    判定层（任务 27 / 28）拿它逐处比对，**两侧口径同构**。
    """

    if parsed.location is None and parsed.size_mm is None and parsed.density is None:
        return ()
    return (
        LesionLabels(
            location=(
                resolve_location(parsed.location).canonical if parsed.location else None
            ),
            size_mm=parsed.size_mm,
            density=(resolve_density(parsed.density).canonical if parsed.density else None),
            finding=evidence.get("location", "") or parsed.evidence_span[:60],
            evidence_span=parsed.evidence_span,
        ),
    )


# 任务 21 里 ``_build_lesions`` 曾被写了两遍，**后一份把前一份盖掉了** ——
# 调用处 ``_build_lesions(parsed, evidence)`` 实际走的是那份，
# 于是标签复核辛苦攒下来的「部位在原文里的出处」（``evidence["location"]``）
# 被丢掉，``finding`` 永远退化成证据片段的前 60 字。重复的那份已删除，
# 只保留上面那一份（带 evidence 参数的）。


def _degrade_reason(exc: BaseException) -> str:
    """降级原因文案 —— **只从错误码表取**（业务代码不散落硬编码）。

    同一个错误码在终端输出、表格 ``failure_reason`` 列、逐例产物与
    ``summary.json`` 里是**同一句话**：复核的人不用猜这几处说的是不是一件事。

    网关抛的是 :class:`NoduleEvalError`（带 ``detail``）；别的异常
    （自定义网关抛的 ``RuntimeError`` 之类）取它的原文。
    """

    detail = getattr(exc, "detail", "") or str(exc)
    return format_message(ErrorCode.RULE_DEGRADED, detail=detail)


def _degrade_to_rules(
    original: str,
    *,
    normalized: str | None,
    reason: str,
    raw: str = "",
    error_code: str = "",
) -> ModelMergeResult:
    """退回规则基线。

    规则基线**永远能跑**（它不调模型），所以降级不是「这一例失败」，
    而是「这一例改用确定性口径来判」—— 结论照常出，``normalize_source`` 标 ``rule``。

    **降级标记写在标签上**（``degraded`` / 降级原因 / 原因错误码）：
    标签是**逐例的值**，引擎拿它直接读 —— 不依赖归并器实例上的可变状态，
    并发跑的时候也不会串行（任务 24）。
    """

    rule_result = merge_by_rules(original, normalized_text=normalized)
    labels = replace(
        rule_result.labels,
        degraded=True,
        degrade_reason=reason,
        degrade_error_code=error_code,
        # 降级例一律挂人工复核：结论是确定的，但归并口径退到了规则基线，
        # 放射科应当优先看一眼这一例。
        needs_human_review=True,
    )
    return ModelMergeResult(
        labels=labels,
        degraded=True,
        reason=reason,
        raw=raw,
    )


# --------------------------------------------------------------------------
# 原文不可变：归并结果只写展示列
# --------------------------------------------------------------------------


def merge_updates_for_labels(labels: GoldLabels) -> dict[str, str]:
    """归并结果要写进表格的**列值** —— 只有两列。

    ``model_findings``
        归并出来的标签摘要（部位 / 尺寸 / 密度），人一眼能看懂。
    ``normalize_source``
        ``model``（模型归并）或 ``rule``（规则降级）。

    **只写这两列**：既有三列（``accession_no`` / ``gold_standard_text`` /
    ``image_ref``）由科室维护，**金标准原文永不被改写**。
    """

    findings_parts = []
    if labels.location:
        findings_parts.append(labels.location)
    if labels.size_mm is not None:
        findings_parts.append(f"{labels.size_mm}mm")
    if labels.density:
        findings_parts.append(labels.density)
    findings = " | ".join(findings_parts)
    if labels.presence is None:
        findings = f"{findings}（存在性读不出，挂人工复核）" if findings else (
            "存在性读不出，挂人工复核"
        )
    return {
        "model_findings": findings,
        "normalize_source": labels.source,
    }


def apply_merge_updates(
    row: Mapping[str, Any],
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """把归并结果写回一行 —— **只写展示列，既有列一字不动**。

    这是「金标准原文永不被改写」的**实现层断言**，不是约定：

    - ``updates`` 里出现任何**既有列**（含 ``gold_standard_text``）→ 直接抛
      ``CONTRACT_INVALID``；
    - 写完之后**逐列比对**，既有列的值必须与写之前**逐字相同**；
    - 写完之后既有列的**内容哈希**不变（额外一道防呆）。

    返回写完的新行（不改传入的 ``row``）。
    """

    forbidden = [name for name in updates if name in _READONLY_COLUMNS]
    if forbidden:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                f"归并结果不得写入既有列：{'、'.join(forbidden)}"
                "（金标准原文由科室维护，永不被改写）"
            ),
        )
    unknown = [name for name in updates if name not in MERGE_WRITABLE_COLUMNS]
    if unknown:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                f"归并结果只允许写入 {'、'.join(MERGE_WRITABLE_COLUMNS)}"
                f"（实得 {'、'.join(unknown)}）"
            ),
        )

    before = {name: row.get(name) for name in _READONLY_COLUMNS}
    updated = dict(row)
    updated.update(updates)

    for name, old in before.items():
        if updated.get(name) != old:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"归并结果改写了既有列 {name}（金标准原文永不被改写）",
            )

    if before.get("gold_standard_text") is not None:
        digest_before = hashlib.sha256(
            str(before["gold_standard_text"]).encode("utf-8")
        ).hexdigest()
        digest_after = hashlib.sha256(
            str(updated.get("gold_standard_text", "")).encode("utf-8")
        ).hexdigest()
        if digest_before != digest_after:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail="归并结果改写了金标准原文（内容哈希不一致）",
            )

    return updated


# --------------------------------------------------------------------------
# 引擎接口：把语义归并接进逐例 fan-out
# --------------------------------------------------------------------------


class ModelMergeNormalizer:
    """把语义归并接进引擎的**归并口**（``Engine(normalizer=...)``）。

    引擎的归并口是一个 ``CaseInput -> GoldLabels`` 的函数；本类实现它，
    并声明 ``uses_model_endpoint = True`` —— 引擎据此把这一步算在
    **模型端点**的并发闸门里（模型端点比推理端点慢且贵，各算各的）。

    降级路径（模型不可达 / 输出不合契约 / 回指对不上）全部在这里收口：
    返回规则基线的标签，``source=rule``。**不抛异常** ——
    归并失败不是「这一例失败」，而是「这一例改用确定性口径来判」。
    """

    #: 走模型端点 → 引擎给它单独一个并发闸门。
    uses_model_endpoint = True

    def __init__(
        self,
        gateway: ModelGatewayPort,
        *,
        prompt: str | None = None,
        prompt_path: Path | str | None = None,
    ) -> None:
        self.gateway = gateway
        self._prompt = prompt
        #: 提示词**文件**（给了内联提示词文本时它就不作数）。
        self._prompt_path = Path(prompt_path) if prompt_path is not None else None
        #: 最近一次归并的结果 —— **按线程隔离**。
        #:
        #: 引擎并发跑（任务 15）时多个工作线程共用一个归并器实例，
        #: 共享一个可变属性会互相覆盖：复核的人看到的「最近一次」
        #: 可能根本不是自己那一例的。降级标记本身走**标签**
        #: （``GoldLabels.degraded``），不受这里影响。
        self._local = threading.local()
        #: 提示词文件引用（路径 + 内容哈希）—— 算一次就缓存，
        #: 提示词文件在一轮里不会变。
        self._prompt_ref: PromptRef | None = None
        self._prompt_ref_done = False

    @property
    def last_result(self) -> ModelMergeResult | None:
        """当前线程最近一次归并的结果。"""

        return getattr(self._local, "last_result", None)

    @property
    def last_meta(self) -> Any | None:
        """当前线程最近一次**模型调用**的可审计元数据（任务 33）。

        由网关采集（提示词哈希、token 计数、延迟、是否走过回退），
        引擎据此把这一条记进 ``runs/<run_id>/model_calls.jsonl``。

        **失败的那次也在这里** —— 网关在抛错前会先落一份失败元数据，
        归并层把它降级成规则基线，但「为什么降级」得留在账上。
        """

        return getattr(self._local, "last_meta", None)

    @property
    def prompt_ref(self) -> PromptRef | None:
        """本次归并用的**提示词文件引用**（路径 + 内容哈希）。

        给了内联提示词文本（``prompt`` 参数）时没有文件版本可指 → ``None``。
        引擎把它记进模型调用底账，事后凭哈希反查得到「这一轮用的是哪一版
        提示词」（见 :func:`nodule_eval.audit.modelcall.resolve_prompt_version`）。
        """

        if self._prompt is not None:
            return None
        if not self._prompt_ref_done:
            path = self._prompt_path if self._prompt_path is not None else DEFAULT_PROMPT_PATH
            try:
                self._prompt_ref = PromptRef.from_file(path)
            except NoduleEvalError:
                self._prompt_ref = None
            self._prompt_ref_done = True
        return self._prompt_ref

    def __call__(self, case: Any) -> GoldLabels:
        """归并一例 —— 返回与规则基线**同一份**契约的标签。

        **降级信息随标签一起走**（``degraded`` / ``degrade_reason`` /
        ``degrade_error_code``）：引擎据此标 ``normalize_source=rule``、
        计「降级数」并挂人工复核。
        """

        # 本例的模型调用元数据重新计 —— 上一例的不能带过来。
        # 网关那份也一并清零：提示词文件读不到这类「压根没调模型」的失败，
        # 不清的话会把上一例的元数据当成这一例的记进账里。
        self._local.last_meta = None
        if hasattr(self.gateway, "last_meta"):
            try:
                self.gateway.last_meta = None
            except AttributeError:  # pragma: no cover - 只读属性实现
                pass
        result = merge_by_model(
            case.gold_standard_text,
            gateway=self.gateway,
            normalized=case.gold_standard_normalized or None,
            prompt=self._prompt,
            tool_call_id=str(getattr(case, "tool_call_ref", "") or ""),
        )
        self._local.last_result = result
        self._local.last_meta = getattr(self.gateway, "last_meta", None)
        return result.labels
