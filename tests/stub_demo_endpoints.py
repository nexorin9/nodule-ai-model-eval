"""演示用的三端点协议替身（**只在测试层**）。

按一份 plan JSON 同时起四个**真的 HTTP 服务**：

- **RIS 检查清单**（权威数据源）：``GET /api/v1/imaging-studies`` —— 给每一例的
  ``accession_no → study_uid`` 绑定；
- **推理服务**：``POST /api/v1/detections`` —— 按 plan 逐例编排响应
  （检出 / 未检出 / 报 5xx）；
- **科研平台评估登记**：``POST /api/v1/model-evaluation-runs`` —— 人工确认后的
  幂等写回；
- **模型端点**（OpenAI-compatible）：``POST /chat/completions`` —— 语义归并口径
  （本轮演示走规则基线，这个端点只占位，不被调用）。

plan 长这样::

    {
      "ris_items": [{"accession_no": "DEMO-0001", "study_uid": "STUDY-DEMO-0001",
                      "modality": "DX", "study_date": "2026-03-01",
                      "report_status": "final", "image_ref": "dx://demo/study-0001"}],
      "inference": {"STUDY-DEMO-0002": {"mode": "nodetect"},
                    "STUDY-DEMO-0003": {"mode": "fail500"},
                    "STUDY-DEMO-0001": {"mode": "detect",
                                        "candidates": [{"position": "右下肺",
                                                        "size_mm": 8.0,
                                                        "density": "磨玻璃",
                                                        "confidence": 0.92}]}},
      "inference_delay_seconds": 0.02
    }

用法（起服务并把三个基址写进文件，供 shell 脚本取）::

    python tests/stub_demo_endpoints.py --plan plan.json --ports-file ports.json

**纪律**：本模块只允许出现在 ``tests/`` 下。产品代码（``src/``）不 import 它，
默认出站路径也永远不会命中它 —— 除非显式开启替身门（``--fixture`` 或
``IMAGING_EVAL_FIXTURE=1`` + 替身地址变量）。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Mapping

from stub_base import BaseStub
from stub_inference import InferenceStub
from stub_model import ModelStub
from stub_research_platform import ResearchPlatformStub
from stub_ris import RISStub


class PlanInferenceStub(InferenceStub):
    """按 plan 逐例编排响应的推理端点替身。

    ``plan["inference"]`` 是一张 ``study_uid → 编排`` 的表：

    - ``{"mode": "detect"}`` —— 正常检出（用替身默认候选）；
    - ``{"mode": "detect", "candidates": [...]}`` —— 检出并给出指定候选；
    - ``{"mode": "nodetect"}`` —— 未检出（``candidates: []``，**合法**的阴性）；
    - ``{"mode": "fail500"}`` —— 服务端报错（该例判失败，绝不当阴性）。

    plan 里没写的 study_uid 一律走 ``detect``。
    """

    def __init__(self, plan: Mapping[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.plan: dict[str, Any] = dict((plan or {}).get("inference") or {})

    def _rule_for(self, study_uid: str | None) -> dict[str, Any]:
        if not study_uid:
            return {}
        value = self.plan.get(study_uid)
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, str):
            return {"mode": value}
        return {}

    def next_response(self, request: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        current = request if request is not None else (self.requests[-1] if self.requests else {})
        body = current.get("body") if isinstance(current, Mapping) else None
        study_uid = body.get("study_uid") if isinstance(body, Mapping) else None
        rule = self._rule_for(study_uid if isinstance(study_uid, str) else None)
        mode = str(rule.get("mode") or "detect")

        if mode == "fail500":
            return 500, {"error": "stubbed failure", "status": 500}
        if mode == "fail400":
            return 400, {
                "error": {
                    "code": "INVALID_IMAGE_REF",
                    "param": "image_ref",
                    "message": "图像引用无法定位",
                }
            }
        if mode == "nodetect":
            payload = self.build_payload(current)
            payload["candidates"] = []
            return 200, payload

        candidates = rule.get("candidates")
        if isinstance(candidates, list) and candidates:
            payload = self.build_payload(current)
            payload["candidates"] = [dict(item) for item in candidates]
            return 200, payload
        return super().next_response(current)


def start_all(plan: Mapping[str, Any]) -> dict[str, BaseStub]:
    """按 plan 起四个替身，返回 ``{kind: stub}``。"""

    items = [dict(item) for item in (plan.get("ris_items") or [])]
    ris = RISStub(items=items)
    inference = PlanInferenceStub(
        plan=plan,
        delay_seconds=float(plan.get("inference_delay_seconds") or 0.0),
    )
    research = ResearchPlatformStub()
    model = ModelStub()
    for stub in (ris, inference, research, model):
        stub.start(0)
    return {
        "ris": ris,
        "inference": inference,
        "research_platform": research,
        "model": model,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="演示用的三端点协议替身（只在测试层）")
    parser.add_argument("--plan", required=True, help="plan JSON 路径")
    parser.add_argument("--ports-file", default="", help="把三个基址写到这个文件")
    args = parser.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    stubs = start_all(plan)
    bases = {kind: stub.base_url for kind, stub in stubs.items()}
    if args.ports_file:
        Path(args.ports_file).write_text(
            json.dumps(bases, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(bases, ensure_ascii=False))
    print("三端点协议替身已启动（真 socket / 真 HTTP）。Ctrl-C 结束。", flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for stub in stubs.values():
            stub.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
