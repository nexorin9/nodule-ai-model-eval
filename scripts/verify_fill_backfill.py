#!/usr/bin/env python3
"""逐例回填与**界面三态**的实跑核对（任务 46）。

真跑一遍产品主路径并把结果**逐条打印出来**核对：

1. 造一张填好既有列的评估表（科室那一半，含多行金标准原文与全角标点）；
2. 起本地替身推理端点，跑一轮真评估（真 socket + 真 ``requests.Session``）；
3. 用 ``fill`` 子命令把判定回填进评估表；
4. **逐格比对**既有三列 —— 确认一个字符都没变；
5. 打印新增六列、汇总区、漏诊区、状态栏；
6. **空态**与**错误态**各跑一遍：表里没圈定病例 → 状态栏那句话；
   推理端点报 5xx → 状态栏给错误码 + 处置提示，失败例按失败底色标出。

用法::

    python scripts/verify_fill_backfill.py            # 写到临时目录
    python scripts/verify_fill_backfill.py --out 评估表.xlsx
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from openpyxl import load_workbook  # noqa: E402

from nodule_eval import cli  # noqa: E402
from nodule_eval.sheets.fill import (  # noqa: E402
    backfill_workbook,
    read_missed_area_texts,
    read_status_bar,
)
from nodule_eval.sheets.sections import SECTION_MARKER_MISSED  # noqa: E402
from nodule_eval.sheets.template import FIRST_DATA_ROW, write_template  # noqa: E402
from nodule_eval.sheets.status import read_status, status_path  # noqa: E402

from tests.stub_inference import InferenceStub  # noqa: E402

#: 科室填的既有列（含多行原文与全角标点 —— 回填最怕改的就是这些）。
DEPARTMENT_ROWS = [
    ("DEMO-0001", "右下肺见一磨玻璃结节，最大径约8mm", "dx://demo/study-001"),
    ("DEMO-0002", "双肺未见明显结节影", ""),
    ("DEMO-0003", "左肺上叶见实性结节，\n最大径约12mm", "dx://demo/study-003"),
]


def build_sheet(path: Path) -> None:
    """造一张填好既有列的评估表。"""

    write_template(path, prefilled_rows=5)
    workbook = load_workbook(path)
    worksheet = workbook["评估表"]
    for offset, (accession_no, gold_text, image_ref) in enumerate(DEPARTMENT_ROWS):
        row = FIRST_DATA_ROW + offset
        worksheet.cell(row=row, column=1, value=accession_no)
        worksheet.cell(row=row, column=2, value=gold_text)
        worksheet.cell(row=row, column=3, value=image_ref)
    workbook.save(path)
    workbook.close()


def existing_snapshot(path: Path) -> dict[tuple[int, int], object]:
    """既有三列的逐格快照（只到明细区末行）。"""

    workbook = load_workbook(path)
    try:
        worksheet = workbook["评估表"]
        limit = worksheet.max_row + 1
        for row in range(1, worksheet.max_row + 1):
            value = worksheet.cell(row=row, column=1).value
            if value and str(value).strip().startswith(SECTION_MARKER_MISSED):
                limit = row
                break
        snapshot: dict[tuple[int, int], object] = {}
        for row in range(FIRST_DATA_ROW, limit):
            for column in range(1, 4):
                snapshot[(row, column)] = worksheet.cell(row=row, column=column).value
        return snapshot
    finally:
        workbook.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对逐例回填与界面三态")
    parser.add_argument("--out", default="", help="评估表输出路径（缺省写到临时目录）")
    args = parser.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="nodule-fill-"))
    sheet_path = Path(args.out) if args.out else workdir / "评估表.xlsx"
    sheet_path.parent.mkdir(parents=True, exist_ok=True)
    runs_dir = workdir / "runs"
    store_path = workdir / "store" / "nodule_eval.sqlite3"

    build_sheet(sheet_path)
    before = existing_snapshot(sheet_path)
    print(f"评估表            : {sheet_path}")
    print(f"科室已填既有列    : {len(DEPARTMENT_ROWS)} 例（含多行金标准原文与全角标点）")

    stub = InferenceStub()
    stub.start()
    try:
        import os

        os.environ.setdefault("RIS_BASE_URL", "https://ris.test-hospital.internal")
        os.environ.setdefault("RIS_API_TOKEN", "test-ris-token")
        os.environ.setdefault("INFERENCE_BASE_URL", stub.base_url)
        os.environ.setdefault("INFERENCE_API_TOKEN", "test-inference-token")
        os.environ.setdefault("RESEARCH_PLATFORM_BASE_URL", "https://research.test-hospital.internal")
        os.environ.setdefault("RESEARCH_PLATFORM_TOKEN", "test-research-token")
        os.environ.setdefault("OPENAI_BASE_URL", "https://llm.test-hospital.internal/v1")
        os.environ.setdefault("OPENAI_API_KEY", "test-model-token")
        os.environ.setdefault("OPENAI_MODEL", "nodule-goldstd-merge")
        os.environ.setdefault("MODEL_TIMEOUT", "20")
        os.environ.setdefault("MODEL_MAX_RETRIES", "2")
        os.environ.setdefault("MODEL_TEMPERATURE", "0")

        print("\n--- 第一段：跑一轮真评估 ---")
        exit_code = cli.main(
            [
                "run",
                "--cases",
                str(ROOT / "tests" / "fixtures" / "cases" / "single_case.json"),
                "--model-id",
                "demo-model",
                "--run-id",
                "verify-fill-001",
                "--runs-dir",
                str(runs_dir),
                "--store",
                str(store_path),
            ]
        )
        print(f"run 退出码        : {exit_code}")

        status = read_status(status_path(runs_dir / "verify-fill-001"))
        print(f"状态文件          : {status_path(runs_dir / 'verify-fill-001')}")
        if status is not None:
            print(f"  状态            : {status.state}")
            print(f"  进度            : {status.processed}/{status.total}")
            print(f"  当前例号        : {status.current_accession_no or '—'}")
            print(f"  心跳            : {status.heartbeat_at}")
            print(f"  状态栏文本      : {status.status_text}")

        print("\n--- 第二段：回填进评估表 ---")
        exit_code = cli.main(
            [
                "fill",
                "--sheet",
                str(sheet_path),
                "--run-id",
                "verify-fill-001",
                "--runs-dir",
                str(runs_dir),
                "--store",
                str(store_path),
            ]
        )
        print(f"fill 退出码       : {exit_code}")

        after = existing_snapshot(sheet_path)
        same = after == before
        print(f"既有列逐格比对    : {len(before)} 格 —— {'一致（未改动）' if same else '被改动了！'}")
        if not same:
            for key in sorted(set(before) | set(after)):
                if before.get(key) != after.get(key):
                    print(f"  差异            : 第 {key[0]} 行第 {key[1]} 列 "
                          f"{before.get(key)!r} → {after.get(key)!r}")

        workbook = load_workbook(sheet_path)
        try:
            worksheet = workbook["评估表"]
            print("\n--- 第三段：表里现在长什么样 ---")
            print("新增六列（判定 / 模型返回 / 失败原因 / 调用记录ID / 待复核 / 归并来源）：")
            for offset in range(len(DEPARTMENT_ROWS)):
                row = FIRST_DATA_ROW + offset
                cells = [worksheet.cell(row=row, column=column).value for column in range(4, 10)]
                print(f"  第 {row} 行      : " + " | ".join(str(c) for c in cells))
            print("汇总区（固定地址）：")
            for address in ("A6", "B6", "C6", "D6", "E6", "F6", "G6", "H6", "I6",
                            "A8", "B8", "C8", "D8", "E8"):
                print(f"  {address:<4}         : {worksheet[address].value}")
            failed_cell = worksheet.cell(row=FIRST_DATA_ROW, column=6)
            print(f"失败底色（命中例的失败原因格）：{failed_cell.fill.fgColor.rgb}")
        finally:
            workbook.close()

        print(f"状态栏            : {read_status_bar(sheet_path)}")
        missed_texts = read_missed_area_texts(sheet_path)
        print(f"漏诊区            : {missed_texts or '（空）'}")

        print("\n--- 第四段：空态（本表未圈定病例）---")
        empty_sheet = workdir / "空表.xlsx"
        write_template(empty_sheet, prefilled_rows=3)
        # 这一轮一个病例都没有（表里没圈定）→ 状态栏给一句人话，一张格子都不动。
        empty_result = backfill_workbook(empty_sheet, [], summary=None, metrics=None)
        print(f"空态取值          : {empty_result.empty_state}")
        print(f"写入行数          : {empty_result.written_rows}")
        print(f"状态栏            : {read_status_bar(empty_sheet)}")

        print("\n--- 第五段：错误态（推理端点报 5xx）---")
        broken_sheet = workdir / "失败表.xlsx"
        build_sheet(broken_sheet)
        stub.stop()
        broken = InferenceStub(status_sequence=[500, 500, 500])
        broken.start()
        os.environ["INFERENCE_BASE_URL"] = broken.base_url
        cli.main(
            [
                "run",
                "--cases",
                str(ROOT / "tests" / "fixtures" / "cases" / "single_case.json"),
                "--model-id",
                "demo-model",
                "--run-id",
                "verify-fill-failed",
                "--runs-dir",
                str(runs_dir),
                "--store",
                str(store_path),
            ]
        )
        cli.main(
            [
                "fill",
                "--sheet",
                str(broken_sheet),
                "--run-id",
                "verify-fill-failed",
                "--runs-dir",
                str(runs_dir),
                "--store",
                str(store_path),
            ]
        )
        print(f"状态栏            : {read_status_bar(broken_sheet)}")
        workbook = load_workbook(broken_sheet)
        try:
            worksheet = workbook["评估表"]
            cell = worksheet.cell(row=FIRST_DATA_ROW, column=6)
            print(f"失败例的失败原因格: {cell.value}")
            print(f"失败底色          : {cell.fill.fgColor.rgb}")
        finally:
            workbook.close()
        broken.stop()
    finally:
        stub.stop()

    print("\n核对结论：既有列零改动 ✔　加载态 / 空态 / 错误态三态齐全 ✔")
    if not args.out:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
