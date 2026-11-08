"""规模压测的**负载发生器**（只在测试层）。

它按 ``spec.md`` 的推理端点字段规范回响应，并在**按检查号确定**的样本上
注入失败，让压测能同时量出「快不快」与「失败时能不能被看见」。

**为什么失败要按检查号确定**：压测要拿**并发跑**与**串行跑**的结果逐例比对，
证明「并发不改变判定」。若失败是按「第几次请求」注入的，两次跑的失败落在
不同的例上，比对立刻失去意义 —— 比不出差异不等于判定一致。
按检查号确定则两次跑命中同一批例，判定逐例可比。

**注入的三种失败**（各自对应产品的一类错误码）：

===================  ==========================  ======================
注入                 端点表现                     产品错误码
===================  ==========================  ======================
``server_error``     HTTP 500（退避三次仍失败）   ``INFERENCE_5XX``
``uid_mismatch``     回传**别的**检查的标识       ``STUDY_UID_MISMATCH``
``missing_field``   响应里没有 ``candidates``    ``FIELD_MAPPING_MISSING``
===================  ==========================  ======================

**纪律**：本模块只允许出现在 ``tests/`` 下。产品代码（``src/``）不 import 它，
默认出站路径也永远不会命中它 —— 它是**压测负载发生器**，不是产品对接面。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Mapping, Sequence

from stub_base import BaseStub

#: 失败注入的三种模式（按序循环）。
FAILURE_MODES: tuple[str, ...] = ("server_error", "uid_mismatch", "missing_field")

#: 默认候选病灶（与推理端点字段规范一致）。
DEFAULT_CANDIDATES: list[dict[str, Any]] = [
    {
        "position": "右下肺",
        "size_mm": 8.0,
        "density": "磨玻璃",
        "confidence": 0.92,
    }
]


def _bucket(seed: int, key: str, salt: str = "") -> float:
    """把 ``key`` 映射到 ``[0, 1)`` 的确定性桶位。

    用 sha256 而不是内置 ``hash()``：内置哈希**每个进程都不同**（PYTHONHASHSEED），
    跨进程就不可复现了 —— 压测报告要能重复跑出同一份结果。
    """

    digest = hashlib.sha256(f"{seed}:{salt}:{key}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / float(1 << 32)


class LoadGenerator(BaseStub):
    """按厂商字段规范回响应、并按检查号确定地注入失败的负载发生器。

    参数
    ----
    failure_rate
        多少比例的**检查号**会被注入失败（默认 2%）。按检查号而非按请求，
        所以并发跑与串行跑命中同一批例。
    seed
        注入的随机源（固定写进压测报告，便于复现）。
    candidates
        正常响应里返回的候选病灶。
    delay_seconds
        每次响应前睡多久（模拟推理耗时）。
    """

    endpoint_path = "/api/v1/detections"

    def __init__(
        self,
        *,
        failure_rate: float = 0.02,
        seed: int = 20261107,
        candidates: Sequence[Mapping[str, Any]] | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        super().__init__()
        self.failure_rate = float(failure_rate)
        self.seed = int(seed)
        self.candidates = [
            dict(item) for item in (DEFAULT_CANDIDATES if candidates is None else candidates)
        ]
        self.delay_seconds = float(delay_seconds)
        #: 注入统计（报告要能说清「注了多少、命中多少」）。
        self.injected: dict[str, int] = {mode: 0 for mode in FAILURE_MODES}
        self.requests_by_study: dict[str, int] = {}

    # ---- 注入判定 ----
    def failure_mode(self, study_uid: str) -> str | None:
        """这个检查号该注入哪种失败（不注入则返回 ``None``）。

        **纯函数**：同一个检查号在任何一次调用里答案都一样。
        """

        if not study_uid:
            return None
        if _bucket(self.seed, study_uid, "fail") >= self.failure_rate:
            return None
        index = int(_bucket(self.seed, study_uid, "mode") * len(FAILURE_MODES))
        return FAILURE_MODES[min(index, len(FAILURE_MODES) - 1)]

    # ---- 响应 ----
    def next_response(self, request: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

        current = request if request is not None else (self.requests[-1] if self.requests else {})
        body = current.get("body") if isinstance(current, Mapping) else None
        body = body if isinstance(body, Mapping) else {}
        study_uid = str(body.get("study_uid") or "")
        self.requests_by_study[study_uid] = self.requests_by_study.get(study_uid, 0) + 1

        mode = self.failure_mode(study_uid)
        if mode == "server_error":
            self.injected[mode] += 1
            return 500, {"error": "load generator injected server error", "status": 500}
        if mode == "uid_mismatch":
            self.injected[mode] += 1
            payload = self._payload(study_uid, body)
            payload["study_uid"] = f"OTHER-{study_uid}"
            return 200, payload
        if mode == "missing_field":
            self.injected[mode] += 1
            payload = self._payload(study_uid, body)
            payload.pop("candidates", None)
            return 200, payload
        return 200, self._payload(study_uid, body)

    def _payload(
        self, study_uid: str, body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            "study_uid": study_uid,
            "model_id": str((body or {}).get("model_id") or ""),
            "model_version": str((body or {}).get("model_version") or ""),
            "candidates": [dict(item) for item in self.candidates],
        }
