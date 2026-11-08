"""院内科研平台评估登记端点的**协议替身**（只在测试层）。

模拟的是**本产品真正改变业务状态**的那条出站面::

    POST {RESEARCH_PLATFORM_BASE_URL}/api/v1/model-evaluation-runs
    Authorization: Bearer $RESEARCH_PLATFORM_TOKEN
    Idempotency-Key: sha256(run_id|model_id|dataset_ref)
    -> {"registration_id": "...", "run_id": "...", "status": "registered",
        "received_at": "..."}

    GET  {RESEARCH_PLATFORM_BASE_URL}/api/v1/model-evaluation-runs
         ?idempotency_key=<key>
    -> 200 {"registration_id": "...", ...}   # 登记在
    -> 404 {"error": "not found"}            # 登记不在

**幂等语义**：替身按 ``Idempotency-Key`` 去重 —— 同键重复提交**只登记一条**，
第二次直接回放第一次的回执。这让「重复点确认提交会不会多出一条登记」能被真断言，
而不是靠嘴说。

**回执核对**：``GET`` 按幂等键查登记，查不到回 **404**（404 是「没登记」这个
答案，不是一次失败）。这条让「已发出 ≠ 已生效」能被真断言。

**支持注入的场景**：

- ``status_sequence``：4xx（不重试）/ 5xx（退避重试）注入
- ``reject_body``：4xx 时的错误体
- ``lookup_status_sequence``：回执核对查询的失败注入（默认不注入）
- ``hang_after_commit_seconds``：**先登记再不回响应** —— 模拟
  「对端已登记，但响应在路上丢了」。这是最难处理的一格：
  客户端只知道「超时」，压根不知道对端到底记没记上。

**纪律**：本模块只允许出现在 ``tests/`` 下。产品代码不 import 它。
"""

from __future__ import annotations

import argparse
import time
from typing import Any, Mapping, Sequence

from stub_base import BaseStub

#: 回执字段规范（科研平台侧）。
REGISTRATION_FIELDS: tuple[str, ...] = (
    "registration_id",
    "run_id",
    "status",
    "received_at",
)

#: 回执核对查询的查询参数名（与产品侧常量一致）。
LOOKUP_PARAM = "idempotency_key"


class ResearchPlatformStub(BaseStub):
    """科研平台评估登记端点替身（带幂等去重与回执核对）。"""

    endpoint_path = "/api/v1/model-evaluation-runs"

    def __init__(
        self,
        *,
        status_sequence: Sequence[int] | None = None,
        reject_body: Mapping[str, Any] | None = None,
        lookup_status_sequence: Sequence[int] | None = None,
        hang_after_commit_seconds: float = 0.0,
    ) -> None:
        super().__init__(status_sequence=status_sequence)
        self.reject_body = dict(reject_body or {"error": "stubbed rejection", "status": 400})
        #: 幂等键 → 回执。**同键只登记一条**。
        self.registrations: dict[str, dict[str, Any]] = {}
        #: 回执核对查询的失败注入（默认不注入）。
        self.lookup_status_sequence = list(lookup_status_sequence or [])
        #: 先登记再不回响应的秒数（模拟「对端已登记、响应丢了」）。
        self.hang_after_commit_seconds = float(hang_after_commit_seconds)
        #: 回执核对查询的请求条数。
        self.lookup_requests: list[dict[str, Any]] = []
        #: POST 请求计数（``status_sequence`` 按它取号 —— 核对查询是 GET，
        #: 不该把 POST 的序号往后挤）。
        self._post_count = 0

    # ---- 幂等登记 ----

    def build_payload(self, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """按字段规范组装登记回执；同幂等键回放同一条登记。"""

        current = request if request is not None else (self.requests[-1] if self.requests else None)
        if not current:
            return {}

        headers = {key.lower(): value for key, value in current.get("headers", {}).items()}
        key = headers.get("idempotency-key", "")
        body = current.get("body")
        run_id = body.get("run_id", "") if isinstance(body, Mapping) else ""

        if key and key in self.registrations:
            return dict(self.registrations[key])

        receipt: dict[str, Any] = {
            "registration_id": f"REG-{len(self.registrations) + 1:04d}",
            "run_id": run_id,
            "status": "registered",
            "received_at": "2026-03-01T00:00:00+08:00",
        }
        if key:
            self.registrations[key] = dict(receipt)
        return receipt

    # ---- 回执核对 ----

    def lookup_registration(self, idempotency_key: str) -> tuple[int, Any]:
        """按幂等键查登记：查得到回 200，查不到回 **404**。"""

        if self.lookup_status_sequence:
            index = min(len(self.lookup_requests) - 1, len(self.lookup_status_sequence) - 1)
            status = int(self.lookup_status_sequence[index])
            if status >= 400:
                return status, {"error": "stubbed lookup failure", "status": status}
            return status, {}

        receipt = self.registrations.get(idempotency_key)
        if receipt is None:
            return 404, {"error": "registration not found", "status": 404}
        return 200, dict(receipt)

    # ---- 响应编排 ----

    def next_response(self, request: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        record = request if request is not None else (self.requests[-1] if self.requests else None)
        method = str((record or {}).get("method", "POST")).upper()

        if method == "GET":
            query = (record or {}).get("query") or {}
            key = str(query.get(LOOKUP_PARAM, ""))
            self.lookup_requests.append(dict(record or {}))
            return self.lookup_registration(key)

        index = min(self._post_count, len(self.status_sequence) - 1)
        self._post_count += 1
        status = self.status_sequence[index]
        if status >= 400:
            body = dict(self.reject_body)
            body["status"] = status
            return status, body

        payload = self.build_payload(record)
        if self.hang_after_commit_seconds > 0:
            # **先登记再卡住**：对端已经记上了，只是响应没回去 ——
            # 客户端看到的是「超时」，压根不知道对端记没记上。
            time.sleep(self.hang_after_commit_seconds)
        return status, payload


def main() -> None:
    """命令行启动替身：``python tests/stub_research_platform.py --port 8790``。"""

    parser = argparse.ArgumentParser(description="科研平台评估登记协议替身（仅测试层）")
    parser.add_argument("--port", type=int, default=8790, help="监听端口（0 = 随机可用端口）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    args = parser.parse_args()

    stub = ResearchPlatformStub()
    stub.start(args.port)
    print(f"科研平台评估登记协议替身已启动：{stub.base_url}{ResearchPlatformStub.endpoint_path}")
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
