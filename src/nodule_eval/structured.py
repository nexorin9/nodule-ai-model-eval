"""结构化输出契约层：JSON Schema → ``response_format``、strict JSON 解析、必填字段校验。

**这一层解决的是一件很具体的事**：院内模型端点的能力参差不齐 ——
有的认 ``response_format``（OpenAI-compatible 的结构化输出），有的**只认 OpenAI
旧版协议**，回一句「``response_format`` is not supported」；有的认，但**不保证**按
schema 输出，回一段带围栏的散文。

本模块把三件事收成**协议无关**的一份实现（**不含任何 HTTP**），
网关层（:mod:`nodule_eval.gateway`）只管按它给出的结论决定「怎么再问一次」：

1. :func:`build_response_format` —— 把 JSON Schema 包成 OpenAI-compatible 的
   ``response_format`` 形状；
2. :func:`extract_json_payload` —— 从模型原文里**取**出 JSON 对象
   （容忍 ```` ```json ```` 围栏；围栏外还有别的话 → 解析失败，**不猜**）；
3. :func:`validate_required_fields` —— 按 schema 的 ``required`` 校验必填字段，
   缺任一即判解析失败并**写明缺哪个字段**。

**为什么「不猜默认值补齐」是硬规矩**：归并结果决定这一例算命中还是漏判。
模型少给一个字段，我们替它填一个默认值，等于**替模型编结论** ——
那这条评估结论的出处就查不清了。宁可这一例走规则基线（确定性口径），
也不要一个看着齐整、实则编出来的结论。

**兼容回退的判定**：:func:`is_response_format_unsupported` 只在
「4xx **且**响应里点名 ``response_format`` / ``json_schema``」时才判「端点不支持」——
否则那是一个普通的参数错误（提示词太长、鉴权错），**回退只会把问题掩盖掉**。
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from .errors import ErrorCode, NoduleEvalError

__all__ = [
    "DEFAULT_SCHEMA_NAME",
    "STRICT_JSON_INSTRUCTION",
    "build_repair_instruction",
    "build_response_format",
    "extract_json_payload",
    "is_response_format_unsupported",
    "required_fields_from_schema",
    "strip_json_fence",
    "validate_required_fields",
]


#: ``response_format.json_schema.name`` 的缺省名。
DEFAULT_SCHEMA_NAME = "structured_output"

#: 端点**不认** ``response_format`` 时追加给 system prompt 的口径。
#:
#: 这一句是「换个问法」的全部内容：不改提示词口径，只要求**输出形状**。
STRICT_JSON_INSTRUCTION = (
    "【输出格式】这一轮请**只输出一个 JSON 对象**，不要输出解释文字、"
    "不要用 Markdown 代码块包裹；字段名严格按约定，值读不出来就填 null，"
    "不要编造。"
)

#: 围栏的正则（模型很爱加 ```json … ```）。
_FENCE_PATTERN = re.compile(r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)

#: 响应里出现这些词，才可能是在说「这个端点不认 ``response_format``」。
_UNSUPPORTED_MARKERS: tuple[str, ...] = (
    "response_format",
    "json_schema",
    "structured output",
    "structured_output",
)

#: 响应里出现这些词，才可能是在说「这个参数不支持」。
_UNSUPPORTED_WORDS: tuple[str, ...] = (
    "not support",
    "unsupported",
    "unrecognized",
    "unknown",
    "invalid",
    "not implemented",
    "does not support",
    "不支持",
    "不允许",
)


# --------------------------------------------------------------------------
# 请求侧：JSON Schema → response_format
# --------------------------------------------------------------------------


def build_response_format(
    schema: Mapping[str, Any],
    *,
    name: str = DEFAULT_SCHEMA_NAME,
) -> dict[str, Any]:
    """把 JSON Schema 包成 OpenAI-compatible 的 ``response_format``。

    形状固定为
    ``{"type": "json_schema", "json_schema": {"name": ..., "schema": ..., "strict": True}}``
    —— 这是 OpenAI-compatible 的标准形状，院内自建端点与云服务共用同一份。
    """

    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "schema": dict(schema),
            "strict": True,
        },
    }


def required_fields_from_schema(schema: Mapping[str, Any] | None) -> tuple[str, ...]:
    """取 schema 的 ``required`` 列表（没有就当「没有必填字段」）。"""

    if not isinstance(schema, Mapping):
        return ()
    required = schema.get("required")
    if not isinstance(required, (list, tuple)):
        return ()
    return tuple(str(name) for name in required)


# --------------------------------------------------------------------------
# 响应侧：strict JSON 解析
# --------------------------------------------------------------------------


def strip_json_fence(raw: str) -> str:
    """剥掉 ```` ```json … ``` ```` 围栏。

    模型很爱加围栏 —— 剥掉是**格式**层面的规整，不改内容。
    围栏外还有别的话（散文、解释）→ 原样返回，交给 JSON 解析去判失败（**不猜**）。
    """

    text = (raw or "").strip()
    if not text.startswith("```"):
        return text
    match = _FENCE_PATTERN.match(text)
    if match is None:
        return text
    return match.group(1).strip()


def extract_json_payload(raw: str) -> dict[str, Any]:
    """从模型原文里取出 JSON 对象。

    判失败的三种情况（都抛 ``CONTRACT_INVALID``，**不返回半成品**）：

    - 原文为空；
    - 原文不是合法 JSON；
    - JSON 的顶层不是对象（数组、字符串、数字都不行 —— 归并结果必须是对象）。

    **不填默认值**：解析不出来就是解析不出来，由调用方决定走规则降级。
    """

    text = strip_json_fence(raw)
    if not text:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail="模型返回为空，无法解析成 JSON 对象",
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
    return dict(payload)


def validate_required_fields(
    payload: Mapping[str, Any],
    required: Sequence[str],
    *,
    source: str = "模型返回",
) -> None:
    """按必填字段清单校验，缺任一即判解析失败并**写明缺哪个字段**。

    **绝不替模型补默认值** —— 补了就等于替模型编结论。
    """

    missing = [name for name in required if name not in payload]
    if missing:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                f"{source}缺必填字段：{'、'.join(missing)}"
                "（不猜默认值补齐，缺字段的输出整份作废）"
            ),
        )


# --------------------------------------------------------------------------
# 兼容回退：端点认不认 response_format
# --------------------------------------------------------------------------


def is_response_format_unsupported(status: int | None, body: str) -> bool:
    """这次 4xx 是不是「端点不认 ``response_format``」。

    **判据两条都要满足**：

    1. 状态码是 4xx（**429 不算** —— 那是限流，退避后重试才对，不是能力问题）；
    2. 响应里**点名** ``response_format`` / ``json_schema``，且带「不支持 / 无效 /
       未知」这类措辞。

    只有响应点名了这个参数才回退 —— 否则那是一个普通的参数错误
    （提示词超长、鉴权错、模型名不存在），**回退只会把问题掩盖掉**，
    让值班的人去查错方向。
    """

    if status is None or not (400 <= status < 500) or status == 429:
        return False
    text = (body or "").lower()
    if not any(marker in text for marker in _UNSUPPORTED_MARKERS):
        return False
    return any(word in text for word in _UNSUPPORTED_WORDS)


def build_repair_instruction(detail: str) -> str:
    """构造「修复重试」时回给模型的那句话。

    **只说格式，不说业务**：把上一轮解析失败的原因原样告诉模型，
    请它**只**重出一份能解析的 JSON —— 不追问、不要求它改结论。
    """

    return (
        "【修复重试】你上一轮的输出无法解析成约定的 JSON 对象，原因是："
        f"{detail}\n"
        "这一轮请**只输出一个 JSON 对象**，不要解释、不要代码围栏；"
        "字段名严格按约定，读不出来的值填 null，不要编造。"
    )
