"""推理端点的**协议替身**（只在测试层）。

它是一个**真的 HTTP 服务**：真的监听 socket、真的收请求体、真的按
``spec.md`` 的字段规范回响应。产品的出站代码（``requests.Session``、URL 拼接、
Bearer 鉴权头、请求体字段、响应解析）**一行都不改**就走完一遍 ——
所以它验证的是**协议面**，不是「把 HTTP 层 mock 掉」的假通路。

**支持注入的场景**：

- ``status_sequence``：依次返回的 HTTP 状态码（连续 5xx 等）
- ``omit_fields``：从响应里删掉字段（模拟厂商返回缺字段 → ``FIELD_MAPPING_MISSING``）
- ``echo_study_uid=False``：响应里不回传请求的 ``study_uid``
  （→ ``STUDY_UID_MISMATCH``，该例判失败，绝不当阴性）
- ``candidates=[]``：模型未检出候选（阴性例）
- ``raw_payload=...``：整段响应自己指定（**模拟某一家厂商自己的字段命名与嵌套层级**）
- ``error_body=...``：4xx / 5xx 时回**厂商自己的错误体**（错误码 + 出错字段名）
- ``delay_seconds=...``：回响应前先睡一会儿（逼出**读取超时**这条路径）

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

#: 默认候选病灶（厂商标准命名的响应）。
DEFAULT_CANDIDATES: list[dict[str, Any]] = [
    {
        "position": "右下肺",
        "size_mm": 8.0,
        "density": "磨玻璃",
        "confidence": 0.92,
    }
]


class InferenceStub(BaseStub):
    """可编排响应的推理端点替身。

    参数
    ----
    candidates
        正常响应里返回的候选病灶列表。
    status_sequence
        依次返回的 HTTP 状态码；用尽后重复最后一个。``[500, 500, 200]``
        表示「前两次服务端报错、第三次成功」。
    omit_fields
        从响应**顶层**删掉的字段（模拟厂商返回缺字段）。
    omit_candidate_fields
        从每个候选病灶里删掉的字段（模拟「候选缺 size_mm」这类缺字段）。
    echo_study_uid
        响应里是否回传请求的 ``study_uid``。关掉即模拟「回传标识与请求不一致」。
    study_uid_override
        强制回传这个 ``study_uid``（模拟「回传了另一个检查的标识」）。
    raw_payload
        直接指定整个响应体（**模拟某一家厂商自己的字段命名与嵌套层级**）。
        给了它就原样返回，只把 ``study_uid`` 为空串的占位替换成请求里的标识。
    fail_study_uids
        这些 ``study_uid`` 的请求**一律回 500**。

        **为什么要有按检查号注入失败的口子**（而不是只靠 ``status_sequence``）：
        ``status_sequence`` 按「第几次请求」发状态码 —— 那是**顺序**。
        引擎并发跑时第几次请求落在哪一例上是不确定的，失败会落到不同的例，
        「哪一例失败」这件事每次跑都不一样。按 ``study_uid`` 注入则与顺序无关：
        同一批输入，串行跑与并发跑命中的是同一批失败例 —— 这正是
        「并发不改变判定」这条断言能成立的前提。
    """

    endpoint_path = "/api/v1/detections"

    def __init__(
        self,
        *,
        candidates: Sequence[Mapping[str, Any]] | None = None,
        status_sequence: Sequence[int] | None = None,
        omit_fields: Sequence[str] = (),
        omit_candidate_fields: Sequence[str] = (),
        echo_study_uid: bool = True,
        study_uid_override: str | None = None,
        raw_payload: Mapping[str, Any] | None = None,
        error_body: Mapping[str, Any] | None = None,
        delay_seconds: float = 0.0,
        fail_study_uids: Sequence[str] = (),
        fail_status_by_study_uid: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(status_sequence=status_sequence)
        # 注意用 ``is None`` 判定：空列表是**合法的「未检出」**，
        # 不能被 ``or`` 当成「没传」而回落到默认候选。
        self.candidates = [
            dict(item) for item in (DEFAULT_CANDIDATES if candidates is None else candidates)
        ]
        self.omit_fields = list(omit_fields)
        self.omit_candidate_fields = list(omit_candidate_fields)
        self.echo_study_uid = bool(echo_study_uid)
        self.study_uid_override = study_uid_override
        self.raw_payload = json.loads(json.dumps(raw_payload, ensure_ascii=False)) if raw_payload else None
        self.error_body = dict(error_body) if error_body else None
        self.delay_seconds = float(delay_seconds)
        self.fail_study_uids = set(fail_study_uids)
        #: 按检查号指定状态码（``{"STUDY-0001": 400}``）——
        #: 让**同一轮里同时出现两种错误码**（4xx 与 5xx 各一例），
        #: 用来核对「同一个错误码在三处是同一句话」时不被顺序干扰。
        self.fail_status_by_study_uid = dict(fail_status_by_study_uid or {})

    def next_response(self, request: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        """给出下一次请求的（状态码, 响应体）。

        与底座的三点差别：

        1. ``error_body``：4xx / 5xx 时回**厂商自己的错误体**
           （含错误码与出错字段名）—— 失败原因里要能带上它；
        2. ``delay_seconds``：回响应前先睡一会儿 —— 用来逼出**读取超时**这条路径；
        3. ``fail_study_uids``：命中这些检查号的一律回 500（与请求顺序无关）。

        **响应按「这一次请求」组**（``request``）—— 并发时拿「最后一条请求」
        凑出来的响应会把 A 例的标识回给 B 例。
        """

        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

        current = request if request is not None else (self.requests[-1] if self.requests else {})
        body = current.get("body") if isinstance(current, Mapping) else None

        if self.fail_study_uids:
            study_uid = body.get("study_uid") if isinstance(body, Mapping) else None
            if study_uid in self.fail_study_uids:
                return 500, {"error": "stubbed failure", "status": 500}

        if self.fail_status_by_study_uid:
            study_uid = body.get("study_uid") if isinstance(body, Mapping) else None
            forced = self.fail_status_by_study_uid.get(study_uid) if study_uid else None
            if forced is not None:
                if forced >= 400 and self.error_body is not None:
                    return forced, dict(self.error_body)
                return forced, {"error": "stubbed failure", "status": forced}

        index = min(len(self.requests) - 1, len(self.status_sequence) - 1)
        status = self.status_sequence[index]
        if status >= 400:
            if self.error_body is not None:
                return status, dict(self.error_body)
            return status, {"error": "stubbed failure", "status": status}
        return status, self.build_payload(current)

    def build_payload(self, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """按字段规范组装正常响应（可删字段、可不回传 study_uid）。"""

        current = request if request is not None else (self.requests[-1] if self.requests else {})
        body = current.get("body") if isinstance(current, Mapping) else None

        if self.raw_payload is not None:
            # 厂商自有命名 / 嵌套层级的响应：原样返回，只回填 study_uid 占位。
            payload = dict(self.raw_payload)
            if payload.get("study_uid") == "" and isinstance(body, Mapping):
                payload["study_uid"] = body.get("study_uid", "")
            return payload

        candidates: list[dict[str, Any]] = []
        for item in self.candidates:
            candidate = dict(item)
            for field in self.omit_candidate_fields:
                candidate.pop(field, None)
            candidates.append(candidate)

        payload: dict[str, Any] = {
            "study_uid": "",
            "model_id": "",
            "model_version": "",
            "candidates": candidates,
        }
        if self.study_uid_override is not None:
            payload["study_uid"] = self.study_uid_override
        if isinstance(body, Mapping):
            if self.echo_study_uid and self.study_uid_override is None:
                payload["study_uid"] = body.get("study_uid", "")
            payload["model_id"] = body.get("model_id", "")
            payload["model_version"] = body.get("model_version", "")
        for field in self.omit_fields:
            payload.pop(field, None)
        return payload


def main() -> None:
    """命令行启动替身（供手动跑主干时用）：``python tests/stub_inference.py --port 8788``。"""

    parser = argparse.ArgumentParser(description="推理端点协议替身（仅测试层）")
    parser.add_argument("--port", type=int, default=8788, help="监听端口（0 = 随机可用端口）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    args = parser.parse_args()

    stub = InferenceStub()
    stub.start(args.port)
    print(f"推理端点协议替身已启动：{stub.base_url}{InferenceStub.endpoint_path}")
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
