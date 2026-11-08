#!/usr/bin/env python3
"""把三条出站面的**真实请求与响应录下来**，存成契约黄金样本。

这个脚本是「录制器」：它对着协议替身（真 socket、真 ``requests.Session``、
真 ``Authorization: Bearer ...`` 头）把三个端点各调一遍，把**实际发出去的
请求**与**对端真回过来的响应**原样存成 JSON::

    tests/fixtures/contracts/ris.json
    tests/fixtures/contracts/inference.json
    tests/fixtures/contracts/research_platform.json

录完之后，``tests/test_contract_replay.py`` 就拿这些样本做**契约回放**：
把响应样本喂回产品的解析器，验「字段缺了会不会被静默默认」。

**为什么要有这一步**：响应解析器如果只被「当场造出来的响应」测过，
就测不出「对端少给一个字段」这种情形 —— 那正是**静默默认**的温床。
录下来的样本是真的走完一遍协议面之后存下来的，不是手写的。

**令牌不落盘**：录到文件里的鉴权头一律写成 ``Bearer {环境变量名}``，
真实令牌不写进黄金文件。

重新录制::

    python tests/record_contracts.py

**纪律**：本模块只存在于 ``tests/`` 下；产品代码不 import 它。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
for _path in (str(SRC_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from conftest import make_env  # noqa: E402
from contracts import (  # noqa: E402
    INFERENCE_CONTRACT,
    RESEARCH_CONTRACT,
    RIS_CONTRACT,
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
from nodule_eval.config import load_config, load_model_spec  # noqa: E402

#: 黄金样本目录。
CONTRACTS_DIR = TESTS_DIR / "fixtures" / "contracts"

#: 录制时固定不变的入参 —— 黄金文件要能**逐字复现**，不能带当前时刻。
RIS_ITEMS: list[dict[str, Any]] = [
    {
        "accession_no": "DEMO-0001",
        "study_uid": "STUDY-DEMO-001",
        "modality": "DX",
        "study_date": "2026-03-01",
        "report_status": "final",
        "image_ref": "dx://demo/study-001",
    },
    {
        "accession_no": "DEMO-0002",
        "study_uid": "STUDY-DEMO-002",
        "modality": "DX",
        "study_date": "2026-03-02",
        "report_status": "final",
        "image_ref": "dx://demo/study-002",
    },
]

RECORDED_AT = "2026-03-01T00:00:00+08:00"


#: **每次录制都会变**的请求头 —— 不录进黄金文件（否则文件一变就看不出契约变了）。
VOLATILE_HEADERS = frozenset(
    {"host", "user-agent", "accept-encoding", "connection", "date", "content-length"}
)


def _redact_headers(contract_key: str, headers: Mapping[str, Any]) -> dict[str, str]:
    """把鉴权头换成占位写法（真实令牌不落盘），并剔掉每次都变的请求头。"""

    marker = {
        "ris": RIS_CONTRACT.auth_marker(),
        "inference": INFERENCE_CONTRACT.auth_marker(),
        "research_platform": RESEARCH_CONTRACT.auth_marker(),
    }[contract_key]
    out: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in VOLATILE_HEADERS:
            continue
        if name.lower() == "authorization":
            out[name] = marker
        else:
            out[name] = str(value)
    return out


def _record(
    contract_key: str,
    *,
    model_id: str,
    requests: list[Mapping[str, Any]],
    responses: list[Mapping[str, Any]],
    note: str,
) -> dict[str, Any]:
    """把一个端点的请求 / 响应整理成黄金样本。"""

    return {
        "endpoint": contract_key,
        "note": note,
        "model_id": model_id,
        "recorded_at": RECORDED_AT,
        "requests": [
            {
                "method": str(item.get("method") or ""),
                "path": str(item.get("path") or ""),
                "query": {str(k): str(v) for k, v in (item.get("query") or {}).items()},
                "headers": _redact_headers(contract_key, item.get("headers") or {}),
                "body": item.get("body"),
            }
            for item in requests
        ],
        "responses": [
            {"status": int(item.get("status") or 0), "payload": item.get("payload")}
            for item in responses
        ],
    }


def record_ris() -> dict[str, Any]:
    """录 RIS 检查清单（两页游标翻页）。"""

    stub = RISStub(items=RIS_ITEMS, page_size=1)
    stub.start()
    try:
        env = make_env(RIS_BASE_URL=stub.base_url)
        adapter = RISAdapter(
            load_config(env),
            client=HttpClient(sleep=lambda _s: None),
        )
        adapter.fetch_studies(modality="DX", date_from="2026-03-01", date_to="2026-03-31")
    finally:
        stub.stop()

    return _record(
        "ris",
        model_id="",
        requests=stub.requests,
        responses=stub.responses,
        note="RIS 检查清单：首页不带 cursor，第二页带 cursor —— 两页都录",
    )


def record_inference() -> dict[str, Any]:
    """录推理服务的一次检出调用。"""

    stub = InferenceStub()
    stub.start()
    try:
        env = make_env(INFERENCE_BASE_URL=stub.base_url)
        adapter = InferenceAdapter(
            load_config(env),
            load_model_spec("demo-model"),
            client=HttpClient(sleep=lambda _s: None),
        )
        adapter.detect(
            study_uid="STUDY-DEMO-001",
            image_ref="dx://demo/study-001",
            model_id="demo-model",
            model_version="v1.0.0",
        )
    finally:
        stub.stop()

    return _record(
        "inference",
        model_id="demo-model",
        requests=stub.requests,
        responses=stub.responses,
        note="推理服务：一次检出调用（含回显的 study_uid / model_id / model_version）",
    )


def record_research_platform() -> dict[str, Any]:
    """录科研平台评估登记的一次提交。"""

    stub = ResearchPlatformStub()
    stub.start()
    try:
        env = make_env(RESEARCH_PLATFORM_BASE_URL=stub.base_url)
        adapter = ResearchPlatformAdapter(
            load_config(env),
            client=HttpClient(sleep=lambda _s: None),
        )
        adapter.submit(
            EvaluationSubmission(
                run_id="contract-sample-001",
                model_id="demo-model",
                model_version="v1.0.0",
                dataset_ref="contract-sample-table.xlsx",
                metrics={"tp": 1, "fp": 0, "fn": 0, "tn": 0, "sensitivity": 1.0},
                case_rows=[
                    {
                        "accession_no": "DEMO-0001",
                        "verdict": "hit",
                        "model_findings": "右下肺 | 8.0mm | 磨玻璃 | 0.92",
                        "failure_reason": "",
                        "tool_call_id": "tc_0123456789abcdef0123456789abcdef",
                    }
                ],
                submitted_by="信息科-王工",
                submitted_at=RECORDED_AT,
            )
        )
    finally:
        stub.stop()

    return _record(
        "research_platform",
        model_id="",
        requests=stub.requests,
        responses=stub.responses,
        note="科研平台评估登记：一次提交（含 Idempotency-Key 头与登记回执）",
    )


def main() -> int:
    """把三个端点的黄金样本写盘。"""

    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, builder in (
        ("ris", record_ris),
        ("inference", record_inference),
        ("research_platform", record_research_platform),
    ):
        sample = builder()
        path = CONTRACTS_DIR / f"{name}.json"
        path.write_text(
            # **不排序键**：请求体的字段**顺序**本身就是契约的一部分
            # （对端按这份规范收），排序会把顺序抹平、回放就验不到了。
            json.dumps(sample, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"{name}: 录到 {len(sample['requests'])} 次请求 / "
            f"{len(sample['responses'])} 次响应 → {path.relative_to(PROJECT_ROOT)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
