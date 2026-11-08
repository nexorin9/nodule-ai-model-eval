"""出站错误体的统一摘取：厂商错误码 + 出错字段名 + 一句说明。

**为什么独立成模块**：本产品有**四个**出站面 —— 院内 RIS 读、厂商推理服务写、
院内科研平台写、模型网关写。四张错误体的形状大同小异（都是「谁错了、错在哪、
怎么说」），但只有两处真正需要把**厂商自己的错误码与字段名**摘出来：

- **推理服务**（4xx）：「厂商返回 HTTP 400」没人能据此去问厂商；
  「厂商错误码 INVALID_IMAGE_REF，字段 image_ref」才行。
- **模型网关**（4xx）：OpenAI-compatible 端点的错误体形状是
  ``{"error": {"code": ..., "param": ..., "message": ...}}`` ——
  与推理服务那一半**是同一个形状**。

两份各写一遍，改了一边忘了另一边，就会出现「同一个厂商错误码在推理失败原因里
带字段名、在模型失败原因里不带」的怪事。故收成**一份**实现，两处 import。

**这一层不掺业务语义**：只负责把一段错误响应体读成结构化字段；
「这个错误该不该重试、算不算失败」由各自的失败分类表决定。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = ["BODY_SNIPPET_LIMIT", "VendorError", "extract_vendor_error"]

#: 错误响应体的摘录长度（避免把整段 HTML 写进日志与失败原因）。
BODY_SNIPPET_LIMIT = 500


@dataclass(frozen=True)
class VendorError:
    """厂商在错误响应里给出的信息。

    ``code``
        厂商自己的错误码（如 ``INVALID_IMAGE_REF``）—— 4xx 失败原因里必须带上它，
        信息科拿着它才能去问厂商「这个码是什么意思」。
    ``field``
        出错字段名（如 ``image_ref``）—— 参数错误最常见的成因。
    ``message``
        厂商给的中文/英文说明。
    """

    code: str = ""
    field: str = ""
    message: str = ""

    def describe(self) -> str:
        """一行说明，供失败原因拼装。"""

        parts: list[str] = []
        if self.code:
            parts.append(f"厂商错误码 {self.code}")
        if self.field:
            parts.append(f"字段 {self.field}")
        if self.message:
            parts.append(self.message)
        return "，".join(parts)


def extract_vendor_error(body: str) -> VendorError:
    """从错误响应体里抽厂商的错误码、字段名与说明。

    厂商的错误体形状不统一，本函数认下面几种（都见过）：

    - ``{"error": {"code": "...", "param": "...", "message": "..."}}``（OpenAI 风格）
    - ``{"code": "...", "message": "...", "field": "..."}``（扁平风格）
    - ``{"error": "..."}``（只有一句话）
    - ``{"errors": [{"code": "...", "field": "..."}]}``（列表风格）

    **认不出来就原样保留摘要** —— 失败原因里宁可多一句原文，
    也不能把厂商说的话丢掉。
    """

    text = (body or "").strip()
    if not text:
        return VendorError()

    try:
        payload = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return VendorError(message=text[:BODY_SNIPPET_LIMIT])

    if not isinstance(payload, Mapping):
        return VendorError(message=text[:BODY_SNIPPET_LIMIT])

    def _pick(source: Mapping[str, Any]) -> tuple[str, str, str]:
        code = str(source.get("code") or source.get("error_code") or "").strip()
        field = str(
            source.get("param")
            or source.get("field")
            or source.get("parameter")
            or source.get("name")
            or ""
        ).strip()
        message = str(source.get("message") or source.get("detail") or "").strip()
        return code, field, message

    error_value = payload.get("error")
    if isinstance(error_value, Mapping):
        code, field, message = _pick(error_value)
        return VendorError(code=code, field=field, message=message)

    if isinstance(error_value, str) and error_value.strip():
        code, field, message = _pick(payload)
        return VendorError(code=code, field=field, message=message or error_value.strip())

    errors_value = payload.get("errors")
    if isinstance(errors_value, Sequence) and not isinstance(errors_value, (str, bytes)):
        first = next((item for item in errors_value if isinstance(item, Mapping)), None)
        if first is not None:
            code, field, message = _pick(first)
            return VendorError(code=code, field=field, message=message)

    code, field, message = _pick(payload)
    if code or field or message:
        return VendorError(code=code, field=field, message=message)
    return VendorError(message=text[:BODY_SNIPPET_LIMIT])
