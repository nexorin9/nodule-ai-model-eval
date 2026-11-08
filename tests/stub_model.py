"""模型端点（OpenAI-compatible）的**协议替身**（只在测试层）。

它是一个**真的 HTTP 服务**：真的监听 socket、真的收请求体、真的按
OpenAI-compatible 的 ``/chat/completions`` 形状回响应。产品的出站代码
（``requests.Session``、URL 拼接、Bearer 鉴权头、请求体字段、响应解析）
**一行都不改**就走完一遍 —— 所以它验证的是**协议面**，
不是「把 HTTP 层 mock 掉」的假通路。

**支持注入的场景**：

- ``status_sequence``：依次返回的 HTTP 状态码（连续 5xx、429 等）
- ``content``：模型回复的正文（默认给一份合法的归并 JSON）
- ``content_parts``：把 ``content`` 拆成分片数组（``[{"type": "text", ...}]``）
- ``omit_fields``：从响应里删掉字段（模拟端点返回缺字段 → ``FIELD_MAPPING_MISSING``）
- ``error_body``：4xx / 5xx 时回**端点自己的错误体**（错误码 + 出错字段名）
- ``delay_seconds``：回响应前先睡一会儿（逼出**读取超时**这条路径）
- ``reject_response_format``：请求里带 ``response_format`` 就回 400
  「不支持这个参数」—— 逼出网关的**兼容回退**路径（任务 23）
- ``reject_response_format_times``：只拒前 N 次（默认一直拒）

**纪律**：本模块只允许出现在 ``tests/`` 下。产品代码（``src/``）不 import 它，
默认出站路径也永远不会命中它 —— 除非显式开启替身门（``--fixture`` /
``IMAGING_EVAL_FIXTURE=1`` + 替身地址变量）。
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Mapping, Sequence

from stub_base import BaseStub

#: 默认模型回复：一份**合法的**语义归并输出（任务 21 的七字段契约）。
DEFAULT_CONTENT: str = json.dumps(
    {
        "presence": True,
        "location": "右肺下叶",
        "size_mm": 8.0,
        "density": "磨玻璃",
        "evidence_span": "右下肺见一磨玻璃结节",
        "confidence": 0.9,
        "needs_human_review": False,
    },
    ensure_ascii=False,
)


class ModelStub(BaseStub):
    """可编排响应的模型端点替身。

    参数
    ----
    content
        模型回复正文（字符串）。
    content_parts
        给了就把 ``content`` 拆成分片数组（部分端点的分片返回形态）。
    status_sequence
        依次返回的 HTTP 状态码；用尽后重复最后一个。
    omit_fields
        从响应**顶层**删掉的字段（模拟端点返回缺字段）。
    error_body
        4xx / 5xx 时回端点自己的错误体。
    delay_seconds
        回响应前先睡一会儿（逼出读取超时这条路径）。
    reject_response_format
        请求里带 ``response_format`` 就回 400「不支持这个参数」。
    reject_response_format_times
        只拒前 N 次（``None`` = 一直拒）。
    responder
        **按这一次请求作答**的回调：入参是请求记录，返回模型回复正文。
        对照实验（任务 25）用它让替身「读懂」金标准原文再作答 ——
        没有它，替身只会对所有输入回同一句固定 JSON。
    """

    endpoint_path = "/chat/completions"

    def __init__(
        self,
        *,
        content: str = DEFAULT_CONTENT,
        content_parts: Sequence[Mapping[str, Any]] | None = None,
        status_sequence: Sequence[int] | None = None,
        omit_fields: Sequence[str] = (),
        error_body: Mapping[str, Any] | None = None,
        delay_seconds: float = 0.0,
        reject_response_format: bool = False,
        reject_response_format_times: int | None = None,
        content_sequence: Sequence[str] | None = None,
        responder: Any = None,
    ) -> None:
        super().__init__(status_sequence=status_sequence)
        self.content = content
        self.content_parts = (
            [dict(item) for item in content_parts] if content_parts is not None else None
        )
        self.omit_fields = list(omit_fields)
        self.error_body = dict(error_body) if error_body else None
        self.delay_seconds = float(delay_seconds)
        self.reject_response_format = bool(reject_response_format)
        self.reject_response_format_times = reject_response_format_times
        #: 依次返回的正文（用尽后重复最后一个）—— 用来编排
        #: 「第一轮回散文、修复重试回 JSON」这种多轮场景。
        self.content_sequence = (
            [str(item) for item in content_sequence] if content_sequence is not None else None
        )
        #: 按请求作答的回调（见类文档）。
        self.responder = responder
        #: 已经被拒掉的 ``response_format`` 请求数（替身自记，供断言回退次数）。
        self.rejected_response_format = 0

    def _should_reject_response_format(self, request: Mapping[str, Any] | None) -> bool:
        """这次请求要不要按「端点不认 response_format」处理。"""

        if not self.reject_response_format:
            return False
        if self.reject_response_format_times is not None:
            if self.rejected_response_format >= self.reject_response_format_times:
                return False
        body = request.get("body") if isinstance(request, Mapping) else None
        if not isinstance(body, Mapping):
            return False
        if "response_format" not in body:
            return False
        self.rejected_response_format += 1
        return True

    def next_response(self, request: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        """给出下一次请求的（状态码, 响应体）。

        与底座的三点差别：

        1. ``error_body``：4xx / 5xx 时回**端点自己的错误体**
           （含错误码与出错字段名）—— 失败原因里要能带上它；
        2. ``delay_seconds``：回响应前先睡一会儿 —— 用来逼出**读取超时**这条路径；
        3. ``reject_response_format``：请求带 ``response_format`` 就回 400
           「这个参数不支持」—— 用来逼出网关的**兼容回退**。
        """

        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

        if self._should_reject_response_format(request):
            return 400, {
                "error": {
                    "code": "unsupported_value",
                    "param": "response_format",
                    "message": "response_format is not supported by this endpoint",
                }
            }

        index = min(len(self.requests) - 1, len(self.status_sequence) - 1)
        status = self.status_sequence[index]
        if status >= 400:
            if self.error_body is not None:
                return status, dict(self.error_body)
            return status, {"error": "stubbed failure", "status": status}
        return status, self.build_payload(request)

    def build_payload(self, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """按 OpenAI-compatible 形状组装正常响应。"""

        current = request if request is not None else (self.requests[-1] if self.requests else {})
        body = current.get("body") if isinstance(current, Mapping) else None

        model = ""
        if isinstance(body, Mapping):
            model = str(body.get("model") or "")

        content: Any = self.content
        if self.content_sequence is not None:
            index = min(len(self.requests) - 1, len(self.content_sequence) - 1)
            content = self.content_sequence[index]
        if self.responder is not None:
            # 按这一次请求作答 —— 对照实验的模型侧就是这么跑的。
            content = self.responder(current)
        if self.content_parts is not None:
            content = [dict(item) for item in self.content_parts]

        payload: dict[str, Any] = {
            "id": "stub-chatcmpl-0001",
            "object": "chat.completion",
            "created": 1767225600,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 128,
                "completion_tokens": 64,
                "total_tokens": 192,
            },
        }
        for field in self.omit_fields:
            payload.pop(field, None)
        return payload


def main() -> None:
    """命令行启动替身：``python tests/stub_model.py --port 8790``。"""

    parser = argparse.ArgumentParser(description="模型端点协议替身（仅测试层）")
    parser.add_argument("--port", type=int, default=8790, help="监听端口（0 = 随机可用端口）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    args = parser.parse_args()

    stub = ModelStub()
    stub.start(args.port)
    print(f"模型端点协议替身已启动：{stub.base_url}{ModelStub.endpoint_path}")
    print("按 Ctrl-C 结束。")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stub.stop()


if __name__ == "__main__":
    main()
