#!/usr/bin/env python3
"""结构化输出与可审计元数据的**端到端实测**（任务 23）。

这个脚本跑的是**真实协议面**：真的 HTTP 服务、真的 ``requests.Session``、
真的 ``Authorization: Bearer ...`` 头、真的 OpenAI-compatible 请求体与响应体。
它对着**协议替身**（``tests/stub_model.py``）跑三种现场会遇到的场景：

1. **端点认 ``response_format``** —— 按 JSON Schema 请求，直出结构化结果；
2. **端点不认 ``response_format``**（回一句「不支持这个参数」）
   —— 网关**自动回退**成 strict JSON 提示词再问一次；
3. **端点认参数但模型回散文** —— 网关**做 1 次修复重试**，请模型只吐 JSON。

每个场景都打印**可审计元数据**（调了哪个模型、哪个端点、耗时、token、
提示词哈希、返回哈希、走过哪条回退路径、**实际打出去几次请求**），
用来回答飞检问询里的那句话：「这一例凭什么算漏判？」

**替身纪律**：替身只存在于 ``tests/`` 下，本脚本**只在测试层**用它 ——
产品代码（``src/``）不 import 它，默认出站路径也永远不会命中它。

用法::

    python scripts/verify_gateway_structured.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
TESTS_DIR = PROJECT_ROOT / "tests"

for _path in (str(SRC_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from nodule_eval.config import load_config  # noqa: E402
from nodule_eval.gateway import ModelGateway  # noqa: E402
from nodule_eval.goldstd.merge import ModelMergeNormalizer  # noqa: E402
from stub_model import DEFAULT_CONTENT, ModelStub  # noqa: E402

#: 环境变量全是**占位值**，无真实凭据（真连院内端点时换成真值）。
ENV: dict[str, str] = {
    "RIS_BASE_URL": "https://ris.example.invalid",
    "RIS_API_TOKEN": "placeholder-ris-token",
    "INFERENCE_BASE_URL": "https://inference.example.invalid",
    "INFERENCE_API_TOKEN": "placeholder-inference-token",
    "RESEARCH_PLATFORM_BASE_URL": "https://research.example.invalid",
    "RESEARCH_PLATFORM_TOKEN": "placeholder-research-token",
    "OPENAI_BASE_URL": "https://model.example.invalid",
    "OPENAI_API_KEY": "test-model-token",
    "OPENAI_MODEL": "nodule-goldstd-merge",
    "MODEL_TIMEOUT": "60",
    "MODEL_MAX_RETRIES": "2",
    "MODEL_TEMPERATURE": "0",
}

#: 一段真实胸片金标准原文。
GOLD_TEXT = "右下肺见一磨玻璃结节，最大径约8mm。"


class _Case:
    """最小病例（归并口只需要金标准原文）。"""

    gold_standard_text = GOLD_TEXT
    gold_standard_normalized = None


def _gateway(base_url: str) -> ModelGateway:
    return ModelGateway(load_config(ENV), base_url=base_url)


def _print_requests(stub: ModelStub) -> None:
    print(f"请求次数: {len(stub.requests)}")
    for index, request in enumerate(stub.requests, start=1):
        body = request.get("body") or {}
        headers = request.get("headers") or {}
        print(
            f"  第 {index} 次: POST {request.get('path')} "
            f"| 鉴权头 {headers.get('Authorization')} "
            f"| 体字段 {sorted(body)} "
            f"| 有 response_format: {'response_format' in body}"
        )


def _print_meta(gateway: ModelGateway) -> None:
    meta = gateway.last_meta
    if meta is None:
        print("可审计元数据: （本次调用没有留下元数据）")
        return
    print("可审计元数据:")
    for key, value in meta.to_dict().items():
        print(f"  {key}: {value}")
    print(f"  attempts: {gateway.last_stats.attempts}")


def _run_scenario(title: str, stub: ModelStub) -> None:
    print()
    print(f"=== {title} ===")
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        normalizer = ModelMergeNormalizer(gateway)
        normalizer(_Case())
    finally:
        stub.stop()

    _print_requests(stub)
    result = normalizer.last_result
    print(
        "归并: "
        f"degraded={result.degraded} source={result.labels.source} "
        f"presence={result.labels.presence} location={result.labels.location} "
        f"size_mm={result.labels.size_mm} density={result.labels.density}"
    )
    _print_meta(gateway)


def main() -> int:
    # 场景 1 · 端点认 response_format：直出结构化结果。
    _run_scenario("场景 1 · 正常结构化调用（带 response_format）", ModelStub())

    # 场景 2 · 端点不认 response_format：网关自动回退成 strict JSON 提示词。
    _run_scenario(
        "场景 2 · 端点不认 response_format（自动回退）",
        ModelStub(reject_response_format=True),
    )

    # 场景 3 · 模型回散文：网关做 1 次修复重试。
    _run_scenario(
        "场景 3 · 模型回散文（1 次修复重试）",
        ModelStub(content_sequence=["这一轮模型回的是一段散文，不是 JSON。", DEFAULT_CONTENT]),
    )

    print()
    print("全部三个场景跑完。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
