#!/usr/bin/env python3
"""确认闸门的**端到端实测**（任务 36）。

这个脚本跑的是**真实协议面**：真的 HTTP 服务、真的 ``requests.Session``、
真的 ``Authorization: Bearer ...`` 头、真的 ``Idempotency-Key`` 头。
它对着**协议替身**（``tests/stub_inference.py`` / ``tests/stub_research_platform.py``）
走完产品里写回结论的整条路：

1. **跑一轮评估** → run 状态停在「已判定」，科研平台侧**一条记录都没有**，
   产物目录里**没有**任何写回痕迹；
2. **没复核就点「确认提交」** → 被闸门拒掉，**一次出站调用都没发**
   （客户端计数 0 次 + 对端 0 条记录）；
3. **人工复核** → run 推进到「已复核」；
4. **再点「确认提交」** → 出站一次，科研平台侧**一条登记**，
   run 推进到「已提交」，写回底账里留下登记号与幂等键。

每一步都打印**可核对的数**（发了几次、对端记了几条、状态是什么），
用来回答飞检问询里的那句话：「这一轮到底写回过没有？」

**替身纪律**：替身只存在于 ``tests/`` 下，本脚本**只在测试层**用它 ——
产品代码（``src/``）不 import 它，默认出站路径也永远不会命中它。

用法::

    python scripts/verify_writeback_gate.py
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

RUN_ID = "verify-gate-001"


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


def main() -> int:
    for key, value in ENV.items():
        os.environ[key] = value

    inference = InferenceStub()
    research = ResearchPlatformStub()
    inference.start()
    research.start()

    workdir = Path(tempfile.mkdtemp(prefix="verify-writeback-gate-"))
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
        response = engine.run(
            EvalRequest(
                run_id=RUN_ID,
                model_id="demo-model",
                model_version="v1.0.0",
                dataset_ref="评估表-2026-03（胸片肺结节）",
                cases=_cases(3),
            )
        )

        print("=== 场景 1：跑完一轮评估（还没复核） ===")
        print(f"run 状态          : {store.get_run(RUN_ID).status}")
        print(f"进分母例数        : {response.summary.evaluated_cases}")
        print(f"科研平台收到请求  : {len(research.requests)} 条（应为 0）")
        print(f"科研平台登记条数  : {len(research.registrations)} 条（应为 0）")
        audit_path = workdir / "runs" / RUN_ID / WRITEBACK_AUDIT_FILENAME
        print(f"写回底账存在      : {audit_path.exists()}（应为 False）")

        print()
        print("=== 场景 2：没复核就点「确认提交」 ===")
        config = load_config({**ENV, "RESEARCH_PLATFORM_BASE_URL": research.base_url})
        counter = CountingClient(sleep=lambda _s: None)
        service = WritebackService(config, store, client=counter)
        try:
            service.submit(RUN_ID, actor=ACTOR_HUMAN_SUBMIT)
            print("!! 不该走到这里 —— 闸门没拦住")
            return 1
        except NoduleEvalError as exc:
            print(f"错误码            : {exc.code.value}")
            print(f"中文文案          : {exc.message}")
        print(f"出站调用次数      : {counter.calls} 次（应为 0）")
        print(f"科研平台收到请求  : {len(research.requests)} 条（应为 0）")
        print(f"run 状态          : {store.get_run(RUN_ID).status}（不该被推进）")

        print()
        print("=== 场景 3：人工复核 ===")
        store.review_run(RUN_ID, actor=ACTOR_HUMAN_REVIEW)
        print(f"run 状态          : {store.get_run(RUN_ID).status}")

        print()
        print("=== 场景 4：人工确认提交 ===")
        outcome = service.submit(RUN_ID, actor=ACTOR_HUMAN_SUBMIT)
        print(f"出站调用次数      : {counter.calls} 次（应为 1）")
        print(f"科研平台收到请求  : {len(research.requests)} 条（应为 1）")
        print(f"科研平台登记条数  : {len(research.registrations)} 条（应为 1）")
        print(f"登记号            : {outcome.receipt.registration_id}")
        print(f"幂等键            : {outcome.receipt.idempotency_key}")
        print(f"run 状态          : {store.get_run(RUN_ID).status}")
        print(f"写回底账存在      : {audit_path.exists()}（应为 True）")
        record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
        print(f"底账登记号        : {record['registration_id']}")
        print(f"底账提交体字段数  : {len(outcome.submission.to_dict())}")
        print(f"提交体降级例数    : {outcome.submission.metrics.get('rule_degraded_count')}")
        print(f"请求体不含金标准原文: {'gold_standard_text' not in json.dumps(research.requests[0]['body'])}")

        ok = (
            len(research.requests) == 1
            and len(research.registrations) == 1
            and store.get_run(RUN_ID).status == "submitted"
            and audit_path.exists()
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
