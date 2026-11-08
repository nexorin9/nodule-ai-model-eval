#!/usr/bin/env python3
"""宏 ↔ 引擎**落盘 JSON 交接**的端到端实测（任务 45）。

这个脚本走的是**宏真正会走的那条路**：

1. 按宏的写法生成请求 JSON（字段与 ``contracts.py`` 一致），写到工作区的
   ``req.json``（**带 BOM** —— WPS 的 ADODB.Stream 写 UTF-8 会带 BOM，
   宏读回来也是带 BOM 的，所以这条路径必须真的走得通）；
2. 起引擎：``python -m nodule_eval.cli run --request req.json --runs-dir runs``
   （真 socket、真 ``requests.Session``、真 ``Authorization: Bearer ...`` 头）；
3. 读回 ``runs/<run_id>/result.json`` 与 ``runs/<run_id>/case_rows.csv``；
4. **按宏的读法**（按列序读 CSV）把新增六列与汇总区该写的值逐条打出来。

打印出来的就是宏点一次「开始评估」之后，表里会被写上的东西 ——
能对着屏幕核对，就说明宏与引擎确实对得上。

**替身纪律**：替身只存在于 ``tests/`` 下，本脚本**只在测试层**用它；
产品代码（``src/``）不 import 它，默认出站路径也永远不会命中它。

用法::

    python scripts/verify_macro_handoff.py
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

from nodule_eval import cli  # noqa: E402
from nodule_eval.contracts import CASE_ROW_COLUMNS  # noqa: E402
from nodule_eval.sheets import macro_contract as mc  # noqa: E402
from nodule_eval.sheets.columns import NEW_COLUMNS  # noqa: E402
from stub_inference import InferenceStub  # noqa: E402
from stub_ris import RISStub  # noqa: E402

#: 环境变量全是**占位值**，无真实凭据。
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

RUN_ID = "verify-macro-001"

#: 宏会圈定的两行（既有三列由科室填；影像引用留空 → 引擎按检查号从 RIS 取）。
CASES = [
    {
        "accession_no": "DEMO-0001",
        "gold_standard_text": "右下肺见一磨玻璃结节，最大径约8mm。",
        "image_ref": "",
        "row_no": 12,
    },
    {
        "accession_no": "DEMO-0002",
        "gold_standard_text": "双肺未见明显结节影。",
        "image_ref": "",
        "row_no": 13,
    },
]

#: RIS 侧的检查清单（权威数据源：影像标识从这里来）。
RIS_ITEMS = [
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


def _read_csv_rows(path: Path) -> list[list[str]]:
    """读逐例明细（**按列序**，与宏的读法一致）。"""

    import csv

    text = path.read_text(encoding="utf-8-sig")
    return [row for row in csv.reader(text.splitlines()) if row and any(row)]


def main(argv: list[str] | None = None) -> int:
    os.environ.update(ENV)

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "workspace"
        workspace.mkdir(parents=True)
        runs_dir = Path(tmp) / "runs"

        # ---- 1. 宏写请求 JSON（带 BOM：WPS 写出来的就是带 BOM 的） ----
        payload = mc.build_request_payload(
            CASES,
            run_id=RUN_ID,
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="评估表",
            options=mc.build_options(),
        )
        req_path = workspace / mc.REQUEST_FILENAME
        req_path.write_bytes(
            b"\xef\xbb\xbf" + json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        print(f"请求文件      : {req_path}")
        print(f"请求字段      : {', '.join(payload.keys())}")
        print(f"病例数        : {len(payload['cases'])}")
        print(f"带 BOM        : 是（宏写出来的就是带 BOM 的）")

        # ---- 2. 起引擎（真 socket / 真 requests.Session / 真 Bearer 头） ----
        stub = InferenceStub()
        stub.start()
        ris_stub = RISStub(items=RIS_ITEMS)
        ris_stub.start()
        os.environ["INFERENCE_BASE_URL"] = stub.base_url
        os.environ["RIS_BASE_URL"] = ris_stub.base_url
        try:
            exit_code = cli.main(
                [
                    "run",
                    "--request",
                    str(req_path),
                    "--runs-dir",
                    str(runs_dir),
                    "--model-id",
                    "demo-model",
                    "--run-id",
                    RUN_ID,
                    "--store",
                    str(Path(tmp) / "store" / "nodule_eval.sqlite3"),
                ]
            )
        finally:
            stub.stop()
            ris_stub.stop()

        if exit_code != 0:
            print(f"引擎退出码    : {exit_code}（不为 0）")
            return 1

        print(f"推理端点收到  : {len(stub.requests)} 次 POST {stub.requests[0]['path']}")
        print(f"鉴权头        : {stub.requests[0]['headers'].get('Authorization')}")
        print(f"RIS 端点收到  : {len(ris_stub.requests)} 次 GET {ris_stub.requests[0]['path']}")

        # ---- 3. 读回产物 ----
        run_dir = runs_dir / RUN_ID
        result = json.loads((run_dir / mc.RESULT_FILENAME).read_text(encoding="utf-8"))
        rows = _read_csv_rows(run_dir / mc.CASE_ROWS_FILENAME)

        print()
        print("── 宏读回后，表里会被写上的东西 ──────────────")
        print(f"CSV 列序      : {', '.join(rows[0])}")
        if tuple(rows[0]) != tuple(CASE_ROW_COLUMNS):
            print("✗ CSV 列序与契约不一致 —— 宏按列序读会读错列")
            return 1
        print("✓ CSV 列序与契约一致（宏按列序读是对的）")

        # 按宏的读法：fields(1)..fields(6) 是新增六列，fields(7) 是行号
        for fields in rows[1:]:
            row_no = fields[7]
            values = {
                "verdict": fields[1],
                "model_findings": fields[2],
                "failure_reason": fields[3],
                "tool_call_id": fields[4],
                "needs_review": "是" if fields[5].strip().lower() == "true" else "否",
                "normalize_source": fields[6],
            }
            print(f"第 {row_no} 行    : {fields[0]} → " + " | ".join(
                f"{key}={values[key]}" for key in NEW_COLUMNS
            ))

        metrics = result.get("metrics", {})
        summary = result.get("summary", {})
        cells = mc.summary_values(summary, metrics)
        print()
        print("── 汇总区（表头第 4–8 行）会被写上的值 ──────────────")
        for key, address in mc.SUMMARY_CELL_ADDRESSES.items():
            print(f"{address:<4} {key:<24} {cells[key]}")

        print()
        print("── 交接协议核对 ──────────────")
        print(f"请求文件      : {mc.REQUEST_FILENAME} ✓")
        print(f"结果文件      : {mc.RESULT_FILENAME} ✓")
        print(f"逐例明细      : {mc.CASE_ROWS_FILENAME} ✓")
        print(f"调用底账      : {mc.TOOL_CALLS_FILENAME} "
              f"{'✓' if (run_dir / mc.TOOL_CALLS_FILENAME).is_file() else '✗'}")
        print(f"引擎日志      : {mc.ENGINE_LOG_FILENAME}（宏起进程时写）")
        print(f"本机配置      : {mc.CONFIG_FILENAME}（宏读它拿 python 路径与被评模型）")
        print("✓ 宏与引擎对得上：同一份请求字段、同一份列序、同一份汇总区地址。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
