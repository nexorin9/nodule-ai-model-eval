#!/usr/bin/env python3
"""提交体组装与写回审计留痕的**端到端实测**（任务 38）。

这个脚本跑的是**真实协议面**：真的 HTTP 服务、真的 ``requests.Session``、
真的 ``Authorization: Bearer ...`` 头、真的 ``Idempotency-Key`` 头。
它对着**协议替身**（``tests/stub_inference.py`` / ``tests/stub_research_platform.py``）
走完产品里写回结论的整条路，回答三个问题：

1. **发出去的是什么？** —— 提交体八项字段齐备（顺序固定），逐例行带
   **判定依据摘要**与**调用记录 ID**，而**金标准原文一个字都不带出去**；
2. **事后能证明发出去的是哪一版吗？** —— 写回底账里记下操作者、时间、
   幂等键、**请求体内容摘要**、对端回执 ID 与响应状态；脚本把
   **科研平台侧真正收到的那个请求体**再算一次摘要，与底账里的对得上；
3. **太大怎么办？** —— 把上限压到极小再写一次：明确报错并提示分批，
   **一次出站调用都不发**，逐例明细**一条都没少**。

每一步都打印**可核对的数**（字段数、字节数、摘要、出站次数），
用来回答飞检问询里的那句话：「这一轮到底把什么写进院内登记了？」

**替身纪律**：替身只存在于 ``tests/`` 下，本脚本**只在测试层**用它 ——
产品代码（``src/``）不 import 它，默认出站路径也永远不会命中它。

用法::

    python scripts/verify_writeback_payload.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
TESTS_DIR = PROJECT_ROOT / "tests"

for _path in (str(SRC_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from nodule_eval.adapters.http import HttpClient  # noqa: E402
from nodule_eval.adapters.research_platform import (  # noqa: E402
    SUBMISSION_FIELDS,
    payload_bytes,
    payload_digest,
)
from nodule_eval.config import load_config, load_model_spec  # noqa: E402
from nodule_eval.contracts import CaseInput, EvalRequest  # noqa: E402
from nodule_eval.engine import Engine  # noqa: E402
from nodule_eval.errors import NoduleEvalError  # noqa: E402
from nodule_eval.store import (  # noqa: E402
    ACTOR_HUMAN_REVIEW,
    ACTOR_HUMAN_SUBMIT,
    RunStore,
)
from nodule_eval.writeback import (  # noqa: E402
    WRITEBACK_AUDIT_FILENAME,
    WritebackService,
)
from stub_inference import InferenceStub  # noqa: E402
from stub_research_platform import ResearchPlatformStub  # noqa: E402

#: 环境变量全是**占位值**，无真实凭据（真连院内端点时换成真值）。
ENV: dict[str, str] = {
    "RIS_BASE_URL": "https://ris.example.invalid",
    "RIS_API_TOKEN": "placeholder-ris-token",
    "INFERENCE_BASE_URL": "https://inference.example.invalid",
    "INFERENCE_API_TOKEN": "placeholder-inference-token",
    "RESEARCH_PLATFORM_BASE_URL": "https://research.example.invalid",
    "RESEARCH_PLATFORM_TOKEN": "placeholder-research-token",
    "OPENAI_BASE_URL": "https://model.example.invalid",
    "OPENAI_API_KEY": "placeholder-model-token",
    "OPENAI_MODEL": "nodule-goldstd-merge",
    "MODEL_TIMEOUT": "60",
    "MODEL_MAX_RETRIES": "2",
    "MODEL_TEMPERATURE": "0",
}

#: 一段真实胸片金标准原文（放射科写法：部位 + 密度 + 尺寸）。
GOLD_TEXT = "右下肺见一磨玻璃结节，最大径约8mm。"

RUN_ID = "verify-payload-001"
TINY_RUN_ID = "verify-payload-002"


class CountingClient(HttpClient):
    """记「发了几次出站请求」的客户端 —— 用来证明**压根没发**。"""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.calls = 0

    def request(self, method: str, url: str, **kwargs: object):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().request(method, url, **kwargs)


def _cases(count: int) -> tuple[CaseInput, ...]:
    return tuple(
        CaseInput(
            accession_no=f"ACC-{index:04d}",
            gold_standard_text=GOLD_TEXT,
            image_ref=f"dx://local/{index:04d}",
            study_uid=f"STUDY-{index:04d}",
            row_no=index + 1,
        )
        for index in range(count)
    )


def _run(engine: Engine, store: RunStore, run_id: str, count: int) -> None:
    engine.run(
        EvalRequest(
            run_id=run_id,
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="评估表-2026-03（胸片肺结节）",
            cases=_cases(count),
        )
    )
    store.review_run(run_id, actor=ACTOR_HUMAN_REVIEW)


def main() -> int:
    for key, value in ENV.items():
        os.environ[key] = value

    inference = InferenceStub()
    research = ResearchPlatformStub()
    inference.start()
    research.start()

    workdir = Path(tempfile.mkdtemp(prefix="verify-writeback-payload-"))
    os.environ["INFERENCE_BASE_URL"] = inference.base_url

    try:
        store = RunStore.open(workdir / "store.sqlite3", runs_dir=workdir / "runs")
        engine = Engine(
            load_config(),
            load_model_spec("demo-model"),
            client=HttpClient(sleep=lambda _s: None),
            runs_dir=workdir / "runs",
            store=store,
        )

        print("=== 场景 1：跑一轮 → 人工复核 → 确认提交 ===")
        _run(engine, store, RUN_ID, 3)
        config = load_config({**ENV, "RESEARCH_PLATFORM_BASE_URL": research.base_url})
        counter = CountingClient(sleep=lambda _s: None)
        service = WritebackService(config, store, client=counter)
        outcome = service.submit(RUN_ID, actor=ACTOR_HUMAN_SUBMIT)

        body = outcome.submission.to_dict()
        print(f"提交体字段        : {list(body.keys())}")
        print(f"字段与规范一致    : {list(body.keys()) == list(SUBMISSION_FIELDS)}")
        print(f"逐例行字段        : {list(body['case_rows'][0].keys())}")
        print(f"逐例行带判定依据  : {bool(body['case_rows'][0].get('judge_basis'))}")
        print(f"逐例行带调用记录ID: {bool(body['case_rows'][0].get('tool_call_id'))}")
        print(
            "请求体不含金标准原文: "
            f"{'gold_standard_text' not in json.dumps(body, ensure_ascii=False)}"
        )
        print(f"提交体字节数      : {payload_bytes(body)}")

        audit_path = workdir / "runs" / RUN_ID / WRITEBACK_AUDIT_FILENAME
        record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
        print(f"底账操作者        : {record['actor']}")
        print(f"底账时间          : {record['at']}")
        print(f"底账幂等键        : {record['idempotency_key']}")
        print(f"底账请求体摘要    : {record['payload_sha256']}")
        print(f"底账请求体字节    : {record['payload_bytes']}")
        print(f"底账回执ID        : {record['registration_id']}")
        print(f"底账对端响应状态  : {record['response_status']}")

        received = research.requests[0]["body"]
        print(f"对端收到的请求体摘要: {payload_digest(received)}")
        print(f"摘要与底账一致    : {payload_digest(received) == record['payload_sha256']}")
        print(f"出站调用次数      : {counter.calls} 次")
        print(f"科研平台登记条数  : {len(research.registrations)} 条")
        print(f"run 状态          : {store.get_run(RUN_ID).status}")

        print()
        print("=== 场景 2：把上限压到极小再写一次 ===")
        _run(engine, store, TINY_RUN_ID, 3)
        tiny_config = load_config(
            {
                **ENV,
                "RESEARCH_PLATFORM_BASE_URL": research.base_url,
                "RESEARCH_PLATFORM_MAX_PAYLOAD_BYTES": "256",
            }
        )
        tiny_counter = CountingClient(sleep=lambda _s: None)
        tiny_service = WritebackService(tiny_config, store, client=tiny_counter)
        try:
            tiny_service.submit(TINY_RUN_ID, actor=ACTOR_HUMAN_SUBMIT)
            print("!! 不该走到这里 —— 超限没拦住")
            return 1
        except NoduleEvalError as exc:
            print(f"错误码            : {exc.code.value}")
            print(f"中文文案          : {exc.message}")
        print(f"出站调用次数      : {tiny_counter.calls} 次（应为 0）")
        print(f"逐例明细条数      : {len(store.recompute(TINY_RUN_ID))} 条（不该被截断）")
        print(f"run 状态          : {store.get_run(TINY_RUN_ID).status}（不该被推进）")

        ok = (
            list(body.keys()) == list(SUBMISSION_FIELDS)
            and payload_digest(received) == record["payload_sha256"]
            and counter.calls == 1
            and len(research.registrations) == 1
            and tiny_counter.calls == 0
            and len(store.recompute(TINY_RUN_ID)) == 3
        )
        print()
        print(f"总判定            : {'通过' if ok else '未通过'}")
        return 0 if ok else 1
    finally:
        inference.stop()
        research.stop()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
