#!/usr/bin/env python3
"""三条出站面的**端点契约**端到端实测（任务 41）。

这个脚本跑的是**真实协议面**：真的 HTTP 服务、真的 ``requests.Session``、
真的 ``Authorization: Bearer ...`` 头、真的 ``Idempotency-Key`` 头。
它对着协议替身把三个端点各调一遍，逐条打印**对端真正收到的**：

- 请求方法与路径；
- 查询参数；
- 鉴权头（令牌按占位符打印，**不落真实值**）；
- 请求体字段集；
- 响应字段规范是否齐备。

然后回答三个问题：

1. **发出去的和 spec.md 写的一样吗？** —— 每条端点逐项核，违规项打印出来；
2. **凭据只从环境变量来吗？** —— 缺任一变量时对端**一个请求都收不到**；
3. **黄金样本还合得上吗？** —— 重新录一遍，与仓库里的黄金样本逐字比对
   （协议漂移检测）。

**替身纪律**：替身只存在于 ``tests/`` 下，本脚本**只在测试层**用它 ——
产品代码（``src/``）不 import 它，默认出站路径也永远不会命中它。

用法::

    python scripts/verify_contracts.py
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

from conftest import make_env  # noqa: E402
from contracts import (  # noqa: E402
    INFERENCE_CONTRACT,
    RESEARCH_CONTRACT,
    RIS_CONTRACT,
    check_request,
    check_response,
)
from stub_inference import InferenceStub  # noqa: E402
from stub_research_platform import ResearchPlatformStub  # noqa: E402
from stub_ris import RISStub  # noqa: E402

from nodule_eval.adapters.http import HttpClient  # noqa: E402
from nodule_eval.adapters.inference import InferenceAdapter  # noqa: E402
from nodule_eval.adapters.research_platform import (  # noqa: E402
    EvaluationSubmission,
    ResearchPlatformAdapter,
)
from nodule_eval.adapters.ris import RISAdapter  # noqa: E402
from nodule_eval.catalog import load_model_spec  # noqa: E402
from nodule_eval.config import load_config  # noqa: E402
from nodule_eval.contracts import CaseInput, EvalRequest  # noqa: E402
from nodule_eval.engine import Engine  # noqa: E402
from nodule_eval.errors import ErrorCode, NoduleEvalError  # noqa: E402
from nodule_eval.store import ACTOR_HUMAN_REVIEW, ACTOR_HUMAN_SUBMIT, RunStore  # noqa: E402
from nodule_eval.writeback import WritebackService  # noqa: E402

GOLD_TEXT = "右下肺见一磨玻璃结节，最大径约8mm。"


def _client() -> HttpClient:
    return HttpClient(sleep=lambda _s: None)


def _show(label: str, value: object) -> None:
    print(f"{label:<18}: {value}")


def _print_request(title: str, request: dict) -> None:
    """打印对端真正收到的那一条请求。"""

    print(f"--- {title} ---")
    _show("方法", request["method"])
    _show("路径", request["path"])
    _show("查询参数", json.dumps(request.get("query") or {}, ensure_ascii=False))
    auth = request["headers"].get("Authorization", "")
    _show("鉴权头", auth.split(" ")[0] + " <令牌按占位符隐去>" if auth else "（无）")
    if request["headers"].get("Idempotency-Key"):
        _show("幂等键", request["headers"]["Idempotency-Key"])
    body = request.get("body")
    if isinstance(body, dict):
        _show("请求体字段集", json.dumps(list(body.keys()), ensure_ascii=False))
    else:
        _show("请求体", body)


def main() -> int:
    failures: list[str] = []
    workdir = Path(tempfile.mkdtemp(prefix="verify-contracts-"))

    ris_stub = RISStub(
        items=[
            {
                "accession_no": "DEMO-0001",
                "study_uid": "STUDY-DEMO-001",
                "modality": "DX",
                "study_date": "2026-03-01",
                "report_status": "final",
                "image_ref": "dx://demo/study-001",
            }
        ]
    )
    inference_stub = InferenceStub()
    research_stub = ResearchPlatformStub()

    ris_stub.start()
    inference_stub.start()
    research_stub.start()
    store = RunStore.open(workdir / "store.sqlite3", runs_dir=workdir / "runs")

    try:
        # ---------------- RIS ----------------
        print("=== 端点 1：院内 RIS 检查清单（读）===")
        ris_env = make_env(RIS_BASE_URL=ris_stub.base_url)
        ris_adapter = RISAdapter(load_config(ris_env), client=_client())
        ris_result = ris_adapter.fetch_studies(modality="DX", date_from="2026-03-01")
        _print_request("RIS 请求", ris_stub.requests[0])
        ris_violations = check_request(RIS_CONTRACT, ris_stub.requests[0], env=ris_env)
        _show("契约违规项", ris_violations or "无")
        failures.extend(f"RIS：{item}" for item in ris_violations)
        _show("解析出的例数", ris_result.total_count)
        _show("响应字段齐备", not check_response(RIS_CONTRACT, ris_stub.responses[0]["payload"]))

        # ---------------- 推理服务 ----------------
        print()
        print("=== 端点 2：厂商肺结节 AI 推理服务（写：逐例调用）===")
        inference_env = make_env(INFERENCE_BASE_URL=inference_stub.base_url)
        config = load_config(inference_env)
        engine = Engine(
            config,
            load_model_spec("demo-model"),
            client=_client(),
            runs_dir=workdir / "runs",
            store=store,
        )
        engine.run(
            EvalRequest(
                run_id="verify-contracts-001",
                model_id="demo-model",
                model_version="v1.0.0",
                dataset_ref="契约实测-胸片评估表",
                cases=tuple(
                    CaseInput(
                        accession_no=f"ACC-{index:04d}",
                        gold_standard_text=GOLD_TEXT,
                        image_ref=f"dx://local/{index:04d}",
                        study_uid=f"STUDY-{index:04d}",
                        row_no=index + 1,
                    )
                    for index in range(2)
                ),
            )
        )
        _print_request("推理请求", inference_stub.requests[0])
        inference_violations = [
            item
            for request in inference_stub.requests
            for item in check_request(
                INFERENCE_CONTRACT, request, env=inference_env, model_id="demo-model"
            )
        ]
        _show("契约违规项", inference_violations or "无")
        failures.extend(f"推理：{item}" for item in inference_violations)
        _show("出站调用次数", len(inference_stub.requests))
        _show("响应字段齐备", not check_response(INFERENCE_CONTRACT, inference_stub.responses[0]["payload"]))

        # ---------------- 科研平台 ----------------
        print()
        print("=== 端点 3：院内科研平台评估登记（写：真正生效的那条面）===")
        research_env = make_env(
            INFERENCE_BASE_URL=inference_stub.base_url,
            RESEARCH_PLATFORM_BASE_URL=research_stub.base_url,
        )
        research_config = load_config(research_env)
        service = WritebackService(research_config, store, client=_client())
        store.review_run("verify-contracts-001", actor=ACTOR_HUMAN_REVIEW)
        outcome = service.submit("verify-contracts-001", actor=ACTOR_HUMAN_SUBMIT)
        _print_request("写回请求", research_stub.requests[0])
        research_violations = check_request(
            RESEARCH_CONTRACT, research_stub.requests[0], env=research_env
        )
        _show("契约违规项", research_violations or "无")
        failures.extend(f"写回：{item}" for item in research_violations)
        _show("登记号", outcome.receipt.registration_id)
        _show("登记状态", outcome.receipt.status)
        _show(
            "幂等键可复算",
            outcome.receipt.idempotency_key
            == research_stub.requests[0]["headers"]["Idempotency-Key"],
        )
        _show("响应字段齐备", not check_response(RESEARCH_CONTRACT, research_stub.responses[0]["payload"]))

        # ---------------- 缺凭据：一个请求都不发 ----------------
        print()
        print("=== 凭据纪律：缺变量时对端一个请求都收不到 ===")
        for env_name in ("RIS_API_TOKEN", "INFERENCE_API_TOKEN", "RESEARCH_PLATFORM_TOKEN"):
            broken = make_env()
            broken.pop(env_name)
            try:
                load_config(broken)
            except NoduleEvalError as exc:
                named = env_name in exc.message
                _show(
                    f"缺 {env_name}",
                    f"错误码 {exc.code.value}｜指名该变量：{'是' if named else '否'}",
                )
                if exc.code is not ErrorCode.CREDENTIAL_MISSING or not named:
                    failures.append(f"{env_name}：报错未指名变量或错误码不对")
            else:
                failures.append(f"{env_name}：缺变量竟然没报错")
        _show("RIS 收到的请求数", len(ris_stub.requests))
        _show("推理收到的请求数", len(inference_stub.requests))
        _show("科研平台收到的请求数", len(research_stub.requests))

        # ---------------- 黄金样本漂移检测 ----------------
        print()
        print("=== 黄金样本漂移检测：重新录一遍与仓库里的样本逐字比对 ===")
        sys.path.insert(0, str(TESTS_DIR))
        import record_contracts  # noqa: E402

        for name in ("ris", "inference", "research_platform"):
            committed = json.loads(
                (TESTS_DIR / "fixtures" / "contracts" / f"{name}.json").read_text(
                    encoding="utf-8"
                )
            )
            fresh = {
                "ris": record_contracts.record_ris,
                "inference": record_contracts.record_inference,
                "research_platform": record_contracts.record_research_platform,
            }[name]()
            same = json.dumps(committed, sort_keys=True) == json.dumps(fresh, sort_keys=True)
            _show(f"{name} 样本一致", same)
            if not same:
                failures.append(f"{name}：黄金样本与当前协议不一致（协议漂移了）")

        print()
        print(f"总判定            : {'通过' if not failures else '未通过'}")
        for item in failures:
            print(f"  - {item}")
        return 0 if not failures else 1
    finally:
        ris_stub.stop()
        inference_stub.stop()
        research_stub.stop()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
