#!/usr/bin/env python3
"""采集**真实运行截图**（任务 48）。

这个脚本做三件事：

1. **真跑一轮 200 例评估** —— 造一张 200 例的评估表（科室那一半：检查号 /
   金标准原文 / 影像引用），起三个本地协议替身（RIS 清单 / 推理服务 / 科研平台
   登记，真 socket、真 HTTP、真 Bearer 头），用**真 CLI**
   （``python -m nodule_eval.cli run``）逐例调推理端点，回填原表新增六列，
   导出结论表，最后人工确认提交、幂等写回科研平台登记。
2. **采终端截图** —— 用 macOS Terminal.app 的**真窗口**跑上面那条命令链，
   ``screencapture`` 把窗口原样截下来（截的是真终端会话，不是排版稿）。
3. **渲染表格界面** —— 本环境没有 Excel / WPS，宏没法点；所以界面三态
   （正常 / 加载 / 失败）是**读真实产物**渲染的：真实回填后的工作簿、
   真实运行状态文件、真实导出的结论工作簿。图上写明产物路径与出处，
   不拿设计稿冒充实测截图。

产物落在 ``docs/screenshots/``；每张图对应的命令与出处记在
``docs/screenshots/README.md``。

用法::

    python scripts/capture_screenshots.py                 # 全流程
    python scripts/capture_screenshots.py --skip-terminal # 只渲染表格界面
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from openpyxl import load_workbook  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from nodule_eval.export import EXPORT_COLUMN_LABELS_ZH  # noqa: E402
from nodule_eval.sheets.columns import COLUMN_LABELS_ZH  # noqa: E402
from nodule_eval.sheets.fill import read_missed_area_texts, read_status_bar  # noqa: E402
from nodule_eval.sheets.read import read_sheet  # noqa: E402
from nodule_eval.sheets.status import status_path  # noqa: E402
from nodule_eval.sheets.template import (  # noqa: E402
    FIRST_DATA_ROW,
    HEADER_ROW,
    SUMMARY_CELLS,
    write_template,
)

# --------------------------------------------------------------------------
# 演示数据（全部为脱敏占位：检查号 / 影像引用 / 站点地址一律 DEMO 前缀）
# --------------------------------------------------------------------------

#: 病例数 —— 演示跑一轮 200 例。
DEMO_CASE_COUNT = 200

#: 三种病灶写法（科室那一半的金标准原文 + 模型侧应当检出的候选）。
LESION_PATTERNS: tuple[dict[str, Any], ...] = (
    {
        "gold": "右下肺见一磨玻璃结节，最大径约8mm",
        "candidate": {
            "position": "右下肺",
            "size_mm": 8.0,
            "density": "磨玻璃",
            "confidence": 0.92,
        },
    },
    {
        "gold": "左肺上叶见实性结节，最大径约12mm",
        "candidate": {
            "position": "左肺上叶",
            "size_mm": 12.0,
            "density": "实性",
            "confidence": 0.88,
        },
    },
    {
        "gold": "右肺中叶见部分实性结节，最大径约6mm",
        "candidate": {
            "position": "右肺中叶",
            "size_mm": 6.0,
            "density": "部分实性",
            "confidence": 0.79,
        },
    },
)

#: 阴性写法（金标准说没有结节）。
NEGATIVE_GOLD = "双肺未见明显结节影"

#: 一轮 200 例的判定分布（与 specs 里的期望一致，逐一可核）。
#: 命中 120 / 漏判 20 / 误报 40 / 真阴性 15 / 失败 5。
DEMO_MIX: tuple[tuple[str, int], ...] = (
    ("hit", 120),
    ("miss", 20),
    ("false_positive", 40),
    ("true_negative", 15),
    ("failed", 5),
)

#: 本轮 run_id 与数据集引用。
RUN_ID = "demo-200-dx-001"
DATASET_REF = "demo-chest-xray-200"

# --------------------------------------------------------------------------
# 配色（与 docs/concepts/visual-target.md 的视觉目标卡一致；
# 表格底色一律**从真实工作簿里读**，这里的值只作兜底）
# --------------------------------------------------------------------------

INK = (31, 59, 99)
BAND = (247, 248, 250)
PAPER = (255, 255, 255)
TEXT = (33, 38, 45)
MUTED = (108, 117, 129)
GRID = (214, 219, 226)
ACCENT = (15, 111, 75)
HIT_BG = (227, 244, 232)
MISS_BG = (251, 227, 227)
FAILED_BG = (245, 239, 224)

VERDICT_BG: dict[str, tuple[int, int, int]] = {
    "hit": HIT_BG,
    "miss": MISS_BG,
    "false_positive": MISS_BG,
    "true_negative": BAND,
    "failed": FAILED_BG,
}

VERDICT_ZH: dict[str, str] = {
    "hit": "命中",
    "miss": "漏判",
    "false_positive": "误报",
    "true_negative": "真阴性",
    "failed": "失败",
}

FONT_CJK = "/System/Library/Fonts/Hiragino Sans GB.ttc"
FONT_CJK_BOLD = "/System/Library/Fonts/STHeiti Medium.ttc"
FONT_MONO = "/System/Library/Fonts/Menlo.ttc"


def font(size: int, *, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    """取一款中文 / 等宽字体（本机 macOS 自带，渲染器不依赖额外字体包）。"""

    if mono:
        return ImageFont.truetype(FONT_MONO, size)
    return ImageFont.truetype(FONT_CJK_BOLD if bold else FONT_CJK, size)


# --------------------------------------------------------------------------
# 演示数据构造
# --------------------------------------------------------------------------


def build_demo_cases(count: int = DEMO_CASE_COUNT) -> list[dict[str, Any]]:
    """按 :data:`DEMO_MIX` 造这一轮的 200 例（检查号 / 金标准 / 影像引用 / 替身编排）。

    ``count`` 小于 200 时按同样比例缩放，五种判定各至少留一例。
    """

    total = sum(size for _, size in DEMO_MIX)
    counts = [max(1, round(size * count / total)) for _, size in DEMO_MIX]
    while sum(counts) > count:
        counts[max(range(len(counts)), key=lambda index: counts[index])] -= 1
    while sum(counts) < count:
        counts[max(range(len(counts)), key=lambda index: counts[index])] += 1

    plan: list[str] = []
    for (verdict, _), size in zip(DEMO_MIX, counts):
        plan.extend([verdict] * size)

    cases: list[dict[str, Any]] = []
    for index, verdict in enumerate(plan):
        number = index + 1
        accession_no = f"DEMO-{number:04d}"
        study_uid = f"STUDY-DEMO-{number:04d}"
        image_ref = f"dx://demo/study-{number:04d}"
        pattern = LESION_PATTERNS[index % len(LESION_PATTERNS)]
        positive = verdict in {"hit", "miss"}
        gold = pattern["gold"] if positive else NEGATIVE_GOLD
        behavior: dict[str, Any]
        if verdict == "failed":
            behavior = {"mode": "fail500"}
        elif verdict == "miss" or verdict == "true_negative":
            behavior = {"mode": "nodetect"}
        elif verdict == "hit":
            behavior = {"mode": "detect", "candidates": [dict(pattern["candidate"])]}
        else:  # false_positive：金标准说没有，模型却检出
            behavior = {"mode": "detect", "candidates": [dict(pattern["candidate"])]}
        cases.append(
            {
                "accession_no": accession_no,
                "study_uid": study_uid,
                "image_ref": image_ref,
                "gold_standard_text": gold,
                "expected_verdict": verdict,
                "behavior": behavior,
            }
        )
    return cases


def write_demo_sheet(path: Path, cases: Sequence[dict[str, Any]]) -> Path:
    """把 200 例写进评估表的**既有三列**（科室那一半）。"""

    write_template(path, prefilled_rows=len(cases))
    workbook = load_workbook(path)
    try:
        worksheet = workbook["评估表"]
        for offset, case in enumerate(cases):
            row = FIRST_DATA_ROW + offset
            worksheet.cell(row=row, column=1, value=case["accession_no"])
            worksheet.cell(row=row, column=2, value=case["gold_standard_text"])
            worksheet.cell(row=row, column=3, value=case["image_ref"])
        workbook.save(path)
    finally:
        workbook.close()
    return path


def write_demo_plan(path: Path, cases: Sequence[dict[str, Any]], *, delay: float) -> Path:
    """写替身用的 plan（RIS 清单 + 逐例响应编排）。"""

    plan = {
        "ris_items": [
            {
                "accession_no": case["accession_no"],
                "study_uid": case["study_uid"],
                "modality": "DX",
                "study_date": "2026-03-01",
                "report_status": "final",
                "image_ref": case["image_ref"],
            }
            for case in cases
        ],
        "inference": {case["study_uid"]: case["behavior"] for case in cases},
        "inference_delay_seconds": delay,
    }
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_request_json(path: Path, sheet_path: Path, cases: Sequence[dict[str, Any]]) -> Path:
    """**从评估表读出**病例输入，组装成引擎请求（产品主路径：表 → 请求 → 引擎）。"""

    result = read_sheet(sheet_path)
    payload = {
        "run_id": RUN_ID,
        "model_id": "demo-model",
        "model_version": "v1.0.0",
        "dataset_ref": DATASET_REF,
        "cases": [
            {
                "accession_no": case.accession_no,
                "gold_standard_text": case.gold_standard_text,
                "image_ref": case.image_ref,
            }
            for case in result.cases
        ],
        "options": {"size_tolerance_mm": 2.0, "concurrency": 4, "resume": False},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"从评估表读出病例 {len(payload['cases'])} 例 → {path}")
    return path


# --------------------------------------------------------------------------
# 终端会话（真 Terminal.app 窗口 + screencapture）
# --------------------------------------------------------------------------

#: 终端窗口的位置与大小（屏幕 1920×1080）。
TERM_BOUNDS = (60, 60, 1260, 1000)


def rel(path: Path) -> str:
    """项目内的路径写成**相对路径** —— 截图里的命令短一截，也更好读。"""

    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _osascript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def terminal_run(
    command: str,
    *,
    tag: str,
    title: str,
    workdir: Path,
    out_png: Path,
    timeout: float = 600.0,
    on_poll: Any = None,
) -> dict[str, Any]:
    """在 Terminal.app 的**真窗口**里跑一条命令，并把窗口原样截图。

    **不清屏**：Terminal 会把敲进去的命令原样回显在窗口里 —— 截图里能直接
    看到这条命令是什么（截图与命令一一对应，不用另找记录）。

    ``on_poll`` 在等待期间被反复调用（用来抓运行中的状态文件）。
    返回 ``{"window_id": ..., "elapsed": ...}``。
    """

    done_flag = workdir / f".done-{tag}"
    if done_flag.exists():
        done_flag.unlink()

    full = (
        f"cd {shlex.quote(str(ROOT))}; "
        f"printf '\\033[1m── {title} ──\\033[0m\\n'; "
        f"{command}; printf '\\n__退出码: %s__\\n' $?; "
        f"touch {shlex.quote(str(done_flag))}"
    )
    script = f"""
    tell application "Terminal"
      activate
      set newTab to do script {_osascript_quote(full)}
      delay 1
      set theWindow to first window whose selected tab is newTab
      set bounds of theWindow to {{{TERM_BOUNDS[0]}, {TERM_BOUNDS[1]}, {TERM_BOUNDS[2]}, {TERM_BOUNDS[3]}}}
      return id of theWindow
    end tell
    """
    started = time.time()
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, check=True
    )
    window_id = result.stdout.strip().splitlines()[-1].strip()
    print(f"[终端] 已开窗口 {window_id}（{tag}），等命令跑完 …")

    deadline = started + timeout
    while time.time() < deadline:
        if done_flag.exists():
            break
        if on_poll is not None:
            try:
                on_poll()
            except Exception:  # 抓状态失败不该影响截图
                pass
        time.sleep(0.05)
    else:
        raise TimeoutError(f"终端命令超时：{tag}")
    time.sleep(1.2)  # 让终端把最后几行渲染完

    region = ",".join(str(v) for v in TERM_BOUNDS)
    subprocess.run(
        ["screencapture", "-x", "-o", "-R", region, str(out_png)], check=True
    )
    print(f"[终端] 截图已存：{out_png}（耗时 {time.time() - started:.1f}s）")
    return {"window_id": window_id, "elapsed": time.time() - started}


def terminal_close(window_id: str) -> None:
    """关掉本脚本开的窗口（不碰用户自己的窗口）。"""

    script = f"""
    tell application "Terminal"
      try
        close (every window whose id is {window_id})
      end try
    end tell
    """
    subprocess.run(["osascript", "-e", script], capture_output=True, text=True)


# --------------------------------------------------------------------------
# 渲染：从**真实产物**画界面
# --------------------------------------------------------------------------


def _rgb(value: Any, default: tuple[int, int, int]) -> tuple[int, int, int]:
    """从 openpyxl 的颜色值取 RGB（读不到就用兜底色）。"""

    if not value:
        return default
    raw = str(value)
    if len(raw) == 8:  # AARRGGBB
        raw = raw[2:]
    if len(raw) != 6:
        return default
    try:
        return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    except ValueError:
        return default


def _cell_bg(cell: Any, default: tuple[int, int, int]) -> tuple[int, int, int]:
    fill = getattr(cell, "fill", None)
    if fill is None or fill.patternType is None:
        return default
    return _rgb(fill.fgColor.rgb, default)


def _text_width(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont) -> int:
    if not text:
        return 0
    box = draw.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0]


def _ellipsize(
    draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, width: int
) -> str:
    """超长文本截断（表格里一列只放一个信息维度）。"""

    if _text_width(draw, text, fnt) <= width:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed and _text_width(draw, trimmed + ellipsis, fnt) > width:
        trimmed = trimmed[:-1]
    return trimmed + ellipsis


def _draw_grid(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    headers: Sequence[Any],
    rows: Sequence[Sequence[str]],
    *,
    widths: Sequence[int],
    header_bg: tuple[int, int, int] = INK,
    row_bg: Sequence[tuple[int, int, int]] | None = None,
    body_font: ImageFont.FreeTypeFont | None = None,
    header_font: ImageFont.FreeTypeFont | None = None,
    row_height: int = 30,
    header_height: int | None = None,
    max_rows: int | None = None,
) -> int:
    """画一张带表头的表格，返回结束位置的 y 坐标。

    ``headers`` 每项可以是字符串，也可以是 ``(中文名, 契约列名)`` 两行元组。
    """

    body_font = body_font or font(15)
    header_font = header_font or font(15, bold=True)
    x0, y0 = origin
    total_width = sum(widths)
    header_height = header_height or row_height

    draw.rectangle([x0, y0, x0 + total_width, y0 + header_height], fill=header_bg)
    cursor = x0
    for index, title in enumerate(headers):
        if isinstance(title, tuple):
            draw.text((cursor + 10, y0 + 5), title[0], font=header_font, fill=(255, 255, 255))
            draw.text(
                (cursor + 10, y0 + 5 + header_font.size + 4),
                title[1],
                font=font(12),
                fill=(186, 202, 222),
            )
        else:
            draw.text((cursor + 10, y0 + 6), title, font=header_font, fill=(255, 255, 255))
        cursor += widths[index]
        if index < len(widths) - 1:
            draw.line(
                [cursor, y0, cursor, y0 + header_height], fill=(70, 96, 130), width=1
            )

    y = y0 + header_height
    shown = rows if max_rows is None else rows[:max_rows]
    for row_index, row in enumerate(shown):
        background = BAND if row_index % 2 == 0 else PAPER
        if row_bg is not None and row_index < len(row_bg):
            background = row_bg[row_index]
        draw.rectangle([x0, y, x0 + total_width, y + row_height], fill=background)
        cursor = x0
        for column_index, width in enumerate(widths):
            value = row[column_index] if column_index < len(row) else ""
            text = _ellipsize(draw, str(value), body_font, width - 16)
            draw.text((cursor + 10, y + 6), text, font=body_font, fill=TEXT)
            cursor += width
        draw.line([x0, y + row_height, x0 + total_width, y + row_height], fill=GRID, width=1)
        y += row_height
    draw.rectangle([x0, y0, x0 + total_width, y], outline=(150, 160, 172), width=1)
    return y


def _draw_panel(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    width: int,
    title: str,
    subtitle: str = "",
) -> int:
    """区块标题（深蓝条）。"""

    x0, y0 = origin
    draw.rectangle([x0, y0, x0 + width, y0 + 34], fill=INK)
    draw.text((x0 + 14, y0 + 8), title, font=font(17, bold=True), fill=(255, 255, 255))
    y = y0 + 34
    if subtitle:
        draw.text((x0 + 14, y + 8), subtitle, font=font(14), fill=MUTED)
        y += 30
    return y


def _wrap_text(
    draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, width: int
) -> list[str]:
    """按像素宽度折行（中文按字断，英文按词断）。"""

    lines: list[str] = []
    current = ""
    for chunk in re.findall(r"[A-Za-z0-9_\-./:]+|\s+|.", text):
        if current and _text_width(draw, current + chunk, fnt) > width:
            lines.append(current.rstrip())
            current = chunk.lstrip()
        else:
            current += chunk
    if current.strip():
        lines.append(current.rstrip())
    return lines or [""]


def _draw_caption(draw: ImageDraw.ImageDraw, origin: tuple[int, int], width: int, text: str) -> int:
    """图底部的出处说明（**必写**：这张图是从哪个真实产物渲染的）。"""

    x0, y0 = origin
    caption_font = font(13)
    lines = _wrap_text(draw, text, caption_font, width - 40)
    height = 16 + len(lines) * 20
    draw.rectangle([x0, y0, x0 + width, y0 + height], fill=(246, 247, 249))
    draw.rectangle([x0, y0, x0 + 4, y0 + height], fill=ACCENT)
    for index, line in enumerate(lines):
        draw.text((x0 + 16, y0 + 8 + index * 20), line, font=caption_font, fill=MUTED)
    return y0 + height


def _detail_rows(
    worksheet: Any, wanted: Sequence[str], limit: int = 6
) -> list[tuple[int, list[str]]]:
    """从真实工作簿里挑出**每种判定各一行**（按需要）。"""

    found: list[tuple[int, list[str]]] = []
    seen: set[str] = set()
    row = FIRST_DATA_ROW
    while row <= worksheet.max_row:
        verdict = worksheet.cell(row=row, column=4).value
        label = str(verdict or "").strip()
        if label in wanted and label not in seen:
            seen.add(label)
            cells = [
                worksheet.cell(row=row, column=column).value
                for column in range(1, 10)
            ]
            values = ["" if c is None else str(c) for c in cells]
            # 判定那一格在表里存的是契约取值（hit/miss/…）；图上并排写上中文，
            # 两个都留着 —— 截图与表里的取值一一对得上。
            values[3] = f"{VERDICT_ZH.get(label, label)} {label}"
            found.append((row, values))
            if len(found) >= limit:
                break
        row += 1
    return found


def _missed_rows(worksheet: Any, limit: int = 4) -> tuple[list[str], list[list[str]]]:
    """读漏诊明细区的**真实**表头与前几行（按区块的合并跨度取列）。"""

    from nodule_eval.sheets.sections import SECTION_MARKER_MISSED, SECTION_MARKER_STATUS
    from nodule_eval.sheets.template import MISSED_SPANS

    section_row = 0
    for row in range(1, worksheet.max_row + 1):
        value = worksheet.cell(row=row, column=1).value
        if value and str(value).strip().startswith(SECTION_MARKER_MISSED):
            section_row = row
            break
    if not section_row:
        return [], []
    columns = [start for _label, start, _end in MISSED_SPANS]
    headers = [
        str(worksheet.cell(row=section_row + 1, column=column).value or "")
        for column in columns
    ]
    rows: list[list[str]] = []
    row = section_row + 2
    while row <= worksheet.max_row and len(rows) < limit:
        value = worksheet.cell(row=row, column=1).value
        if value and str(value).strip().startswith(SECTION_MARKER_STATUS):
            break
        cells = [worksheet.cell(row=row, column=column).value for column in columns]
        if any(c not in (None, "") for c in cells):
            rows.append(["" if c is None else str(c) for c in cells])
        row += 1
    return headers, rows


def render_sheet_png(
    sheet_path: Path,
    out_png: Path,
    *,
    title: str,
    caption: str,
    picked_verdicts: Sequence[str] = ("hit", "miss", "false_positive", "true_negative", "failed"),
) -> Path:
    """把**真实回填后**的评估表渲染成一张图（汇总区 + 逐例明细 + 漏诊区 + 状态栏）。"""

    workbook = load_workbook(sheet_path)
    try:
        worksheet = workbook["评估表"]
        headers = [
            str(worksheet.cell(row=HEADER_ROW, column=column).value or "")
            for column in range(1, 10)
        ]
        labels = [
            COLUMN_LABELS_ZH.get(key, key)
            for key in (
                "accession_no",
                "gold_standard_text",
                "image_ref",
                "verdict",
                "model_findings",
                "failure_reason",
                "tool_call_id",
                "needs_review",
                "normalize_source",
            )
        ]
        summary = {
            key: worksheet[address].value for key, address in SUMMARY_CELLS.items()
        }
        rows = _detail_rows(worksheet, picked_verdicts)
        # 每行的底色按该行自己的判定给（读的是真实单元格的取值）
        backgrounds = [
            VERDICT_BG.get(values[3].strip(), BAND) for _, values in rows
        ]
        missed = read_missed_area_texts(sheet_path)
        missed_headers, missed_rows = _missed_rows(worksheet)
        status_bar = read_status_bar(sheet_path)
    finally:
        workbook.close()

    width = 1500
    padding = 40
    height = 1180
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)

    y = _draw_panel(
        draw,
        (padding, padding),
        width - 2 * padding,
        title,
        f"工作簿：{sheet_path.name}",
    )
    y += 14

    # ---- 汇总区（真实单元格的取值）----
    metric_items = [
        ("样本量", summary.get("sample_size")),
        ("命中 TP", summary.get("tp")),
        ("误报 FP", summary.get("fp")),
        ("漏判 FN", summary.get("fn")),
        ("真阴性 TN", summary.get("tn")),
        ("敏感度", summary.get("sensitivity")),
        ("特异度", summary.get("specificity")),
        ("PPV", summary.get("ppv")),
        ("NPV", summary.get("npv")),
        ("失败数", summary.get("failed_count")),
        ("挂起数", summary.get("needs_review_count")),
        ("不可比数", summary.get("incomparable_count")),
    ]
    cell_w = (width - 2 * padding) // 6
    for index, (label, value) in enumerate(metric_items):
        column = index % 6
        row_index = index // 6
        x = padding + column * cell_w
        y0 = y + row_index * 78
        draw.rectangle([x, y0, x + cell_w - 8, y0 + 68], fill=BAND, outline=GRID)
        draw.text((x + 14, y0 + 10), label, font=font(14), fill=MUTED)
        draw.text(
            (x + 14, y0 + 30),
            "—" if value in (None, "") else str(value),
            font=font(24, bold=True),
            fill=INK,
        )
    y += 2 * 78 + 18

    # ---- 逐例明细（真实行）----
    y = _draw_panel(
        draw,
        (padding, y),
        width - 2 * padding,
        "逐例明细（表中真实行，含四种判定与失败例各一行）",
        "既有三列由科室填写，产品只追加右边六列；金标准原文一个字符都没改。",
    )
    y += 8
    widths = [88, 112, 175, 140, 165, 160, 160, 130, 98, 115]
    display_headers: list[tuple[str, str]] = [("表内行号", "row")]
    for index in range(9):
        display_headers.append((labels[index], headers[index] if index < len(headers) else ""))
    body_rows = []
    for row_number, values in rows:
        body_rows.append([f"第 {row_number} 行"] + values)
    y = _draw_grid(
        draw,
        (padding, y),
        display_headers,
        body_rows,
        widths=widths,
        row_bg=backgrounds,
        row_height=30,
        header_height=50,
    )
    y += 20

    # ---- 漏诊区 + 状态栏（真实文本）----
    y = _draw_panel(
        draw,
        (padding, y),
        width - 2 * padding,
        "漏诊明细区（引擎回填，条数与漏判数相等）",
        "",
    )
    y += 8
    if missed_rows:
        missed_widths = [110, 190, 170, 320, 190, 110]
        y = _draw_grid(
            draw,
            (padding, y),
            [title for title in (missed_headers + [""] * 6)[:6]],
            [row[:6] for row in missed_rows],
            widths=missed_widths,
            header_bg=(70, 96, 130),
            row_height=28,
        )
    else:
        y = _draw_grid(
            draw,
            (padding, y),
            ["漏诊明细区"],
            [[missed[0] if missed else "（本次未发现漏判）"]],
            widths=[width - 2 * padding],
            header_bg=(70, 96, 130),
            row_height=28,
        )
    y += 16
    draw.rectangle(
        [padding, y, width - padding, y + 40], fill=(238, 240, 243), outline=GRID
    )
    draw.text((padding + 14, y + 10), status_bar, font=font(14), fill=TEXT)
    y += 56

    y = _draw_caption(
        draw,
        (padding, y),
        width - 2 * padding,
        caption,
    )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    # 裁到内容高度（不留大片空白）
    image.crop((0, 0, width, min(height, y + 24))).save(out_png)
    return out_png


def render_export_png(
    export_xlsx: Path,
    out_png: Path,
    *,
    caption: str,
) -> Path:
    """把**真实导出**的结论工作簿渲染成一张图（评估结论 + 逐例明细 + 漏诊清单）。"""

    workbook = load_workbook(export_xlsx, data_only=True)
    try:
        conclusion = workbook["评估结论"]
        conclusion_rows = [
            [str(conclusion.cell(row=row, column=1).value or ""),
             str(conclusion.cell(row=row, column=2).value or "")]
            for row in range(2, min(conclusion.max_row + 1, 26))
        ]
        cases = workbook["逐例明细"]
        case_headers = [str(cases.cell(row=1, column=column).value or "")
                        for column in range(1, cases.max_column + 1)]
        verdict_index = (
            case_headers.index("verdict") if "verdict" in case_headers else 1
        )
        case_rows: list[list[str]] = []
        case_backgrounds: list[tuple[int, int, int]] = []
        wanted = {"hit", "miss", "false_positive", "true_negative", "failed"}
        seen: set[str] = set()
        for row in range(2, cases.max_row + 1):
            values = [str(cases.cell(row=row, column=column).value or "")
                      for column in range(1, cases.max_column + 1)]
            verdict = values[verdict_index].strip()
            if verdict in wanted and verdict not in seen:
                seen.add(verdict)
                values[verdict_index] = f"{VERDICT_ZH.get(verdict, verdict)} {verdict}"
                case_rows.append(values)
                case_backgrounds.append(VERDICT_BG.get(verdict, BAND))
        missed = workbook["漏诊清单"]
        missed_headers = [str(missed.cell(row=1, column=column).value or "")
                          for column in range(1, missed.max_column + 1)]
        missed_rows = [
            [str(missed.cell(row=row, column=column).value or "")
             for column in range(1, missed.max_column + 1)]
            for row in range(2, min(missed.max_row + 1, 5))
        ]
    finally:
        workbook.close()

    width = 1500
    padding = 40
    height = 1240
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)

    y = _draw_panel(
        draw,
        (padding, padding),
        width - 2 * padding,
        "导出的结论表（导出结论表按钮的产物）",
        f"工作簿：{export_xlsx.name}",
    )
    y += 14

    y = _draw_panel(
        draw,
        (padding, y),
        width - 2 * padding,
        "评估结论（表头汇总区 + 手算算式）",
        "",
    )
    y += 8
    y = _draw_grid(
        draw,
        (padding, y),
        ["项目", "值"],
        conclusion_rows,
        widths=[240, width - 2 * padding - 240],
        row_height=26,
    )
    y += 20

    y = _draw_panel(
        draw,
        (padding, y),
        width - 2 * padding,
        "逐例明细（节选：四种判定与失败例各一行）",
        "",
    )
    y += 8
    widths = [130, 110, 200, 200, 260, 220, 160, 110]
    headers = [
        (EXPORT_COLUMN_LABELS_ZH.get(name, name), name)
        for name in (case_headers + [""] * 8)[:8]
    ]
    y = _draw_grid(
        draw,
        (padding, y),
        headers,
        [row[:8] for row in case_rows],
        widths=widths,
        row_bg=case_backgrounds,
        row_height=28,
        header_height=46,
    )
    y += 20

    y = _draw_panel(
        draw,
        (padding, y),
        width - 2 * padding,
        "漏诊清单（节选前 4 条）",
        "",
    )
    y += 8
    y = _draw_grid(
        draw,
        (padding, y),
        missed_headers[:6] if missed_headers else ["漏诊清单"],
        [row[:6] for row in missed_rows],
        widths=[130, 210, 190, 330, 190, 110],
        header_bg=(70, 96, 130),
        row_height=28,
    )
    y += 16

    y = _draw_caption(draw, (padding, y), width - 2 * padding, caption)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    # 裁到内容高度（不留大片空白）
    image.crop((0, 0, width, min(height, y + 24))).save(out_png)
    return out_png


def render_states_png(
    out_png: Path,
    *,
    normal: dict[str, Any],
    loading: dict[str, Any],
    failed: dict[str, Any],
    buttons: Sequence[str],
    caption: str,
) -> Path:
    """表格界面三态（正常 / 加载 / 失败）—— 全部由**真实状态文件**渲染。"""

    width = 1500
    padding = 40
    height = 1080
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)

    y = _draw_panel(
        draw,
        (padding, padding),
        width - 2 * padding,
        "表格内宏界面 · 三个状态",
        "按钮取自 src/nodule_eval/sheets/xlsm_vba/nodule_eval.bas；"
        "状态栏文字取自引擎写的 runs/<run_id>/engine_status.json。",
    )
    y += 14

    # ---- 按钮条（真实宏源码里的按钮名）----
    button_w = 190
    for index, label in enumerate(buttons):
        x = padding + index * (button_w + 16)
        primary = index == 0
        draw.rounded_rectangle(
            [x, y, x + button_w, y + 44],
            radius=6,
            fill=ACCENT if primary else (238, 240, 243),
            outline=ACCENT if primary else GRID,
        )
        draw.text(
            (x + button_w // 2 - _text_width(draw, label, font(16, bold=True)) // 2, y + 12),
            label,
            font=font(16, bold=True),
            fill=(255, 255, 255) if primary else TEXT,
        )
    y += 60

    panels = [
        ("正常态（这一轮跑完，表已回填）", normal, HIT_BG),
        ("加载态（引擎跑着，宏每半秒刷一次状态栏）", loading, (232, 238, 248)),
        ("失败态（推理端点报 5xx，状态栏给错误码与处置提示）", failed, FAILED_BG),
    ]
    for title, state, tint in panels:
        y = _draw_panel(
            draw,
            (padding, y),
            width - 2 * padding,
            title,
            "",
        )
        y += 8
        lines = [
            f"状态        ：{state.get('state', '—')}",
            f"进度        ：{state.get('processed', '—')} / {state.get('total', '—')}"
            f"　当前例号：{state.get('current_accession_no') or '—'}",
            f"心跳        ：{state.get('heartbeat_at', '—')}"
            f"　失败例：{state.get('failed_cases', 0)}",
            f"状态栏文本  ：{state.get('status_text', '—')}",
        ]
        if state.get("error_code"):
            lines.append(f"错误码      ：{state['error_code']}")
        panel_height = 24 + len(lines) * 21
        draw.rectangle(
            [padding, y, width - padding, y + panel_height], fill=tint, outline=GRID
        )
        for index, line in enumerate(lines):
            draw.text((padding + 16, y + 12 + index * 21), line, font=font(14), fill=TEXT)
        y += panel_height + 16

    y = _draw_caption(draw, (padding, y), width - 2 * padding, caption)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    # 裁到内容高度（不留大片空白）
    image.crop((0, 0, width, min(height, y + 24))).save(out_png)
    return out_png


def macro_button_labels() -> list[str]:
    """从**真实宏源码**里读出三个按钮的名字。"""

    source = (ROOT / "src" / "nodule_eval" / "sheets" / "xlsm_vba" / "nodule_eval.bas").read_text(
        encoding="utf-8", errors="replace"
    )
    labels: list[str] = []
    for name in ("StartEvaluation", "ConfirmSubmit", "ExportConclusion"):
        match = re.search(rf"'{name}\s+(\S+)", source)
        if match:
            labels.append(match.group(1))
    return labels or ["开始评估", "确认提交", "导出结论表"]


def read_status_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def start_stub_process(plan_path: Path, ports_file: Path) -> subprocess.Popen[bytes]:
    """起三端点协议替身（真 socket / 真 HTTP）。"""

    if ports_file.exists():
        ports_file.unlink()
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tests" / "stub_demo_endpoints.py"),
            "--plan",
            str(plan_path),
            "--ports-file",
            str(ports_file),
        ],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if ports_file.exists():
            try:
                json.loads(ports_file.read_text(encoding="utf-8"))
                return process
            except json.JSONDecodeError:
                pass
        time.sleep(0.1)
    process.terminate()
    raise TimeoutError("替身没能在 30 秒内起来")


def env_exports(bases: dict[str, str]) -> str:
    """出站凭据与端点的环境变量（**全部从环境变量读**，产品代码不落任何密钥）。"""

    exports = [
        "# 引擎从源码目录跑（装成包之后就是 python -m nodule_eval.cli …，命令不变）",
        "export PYTHONPATH=src",
        "",
        "# 院内端点与凭据（产品只从环境变量读；下面这些是演示用的占位值）",
        "export RIS_BASE_URL=https://ris.demo-hospital.internal",
        "export RIS_API_TOKEN=demo-ris-token",
        "export INFERENCE_BASE_URL=https://inference.demo-hospital.internal",
        "export INFERENCE_API_TOKEN=demo-inference-token",
        "export RESEARCH_PLATFORM_BASE_URL=https://research.demo-hospital.internal",
        "export RESEARCH_PLATFORM_TOKEN=demo-research-token",
        "export OPENAI_BASE_URL=https://llm.demo-hospital.internal/v1",
        "export OPENAI_API_KEY=demo-llm-token",
        "export OPENAI_MODEL=nodule-goldstd-merge",
        "export MODEL_TIMEOUT=20",
        "export MODEL_MAX_RETRIES=2",
        "export MODEL_TEMPERATURE=0",
        "",
        "# 替身门：显式开启，出站目标改走本地协议替身（真实 socket / 真实 HTTP）",
        "export IMAGING_EVAL_FIXTURE=1",
        f"export IMAGING_EVAL_FIXTURE_RIS_BASE_URL={bases['ris']}",
        f"export IMAGING_EVAL_FIXTURE_INFERENCE_BASE_URL={bases['inference']}",
        f"export IMAGING_EVAL_FIXTURE_RESEARCH_BASE_URL={bases['research_platform']}",
        f"export IMAGING_EVAL_FIXTURE_MODEL_BASE_URL={bases['model']}",
        "",
    ]
    return "\n".join(exports)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集真实运行截图")
    parser.add_argument(
        "--workdir",
        default=str(ROOT / "runs" / "_screenshots"),
        help="工作目录（产物、评估表、替身 plan 都落这里）",
    )
    parser.add_argument(
        "--shots-dir",
        default=str(ROOT / "docs" / "screenshots"),
        help="截图输出目录",
    )
    parser.add_argument("--skip-terminal", action="store_true", help="跳过终端截图")
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="只重新渲染表格界面（复用工作目录里已有的运行产物，不重跑）",
    )
    parser.add_argument("--cases", type=int, default=DEMO_CASE_COUNT, help="演示病例数")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir)
    shots_dir = Path(args.shots_dir)
    workdir.mkdir(parents=True, exist_ok=True)
    shots_dir.mkdir(parents=True, exist_ok=True)

    cases = build_demo_cases(args.cases)
    sheet_path = workdir / "肺结节AI评估表-200例.xlsx"
    request_path = workdir / "req.json"
    if not args.render_only:
        # --render-only 时**不重写评估表**：那会把上一轮回填好的表覆盖成空模板。
        write_demo_sheet(sheet_path, cases)
        print(f"评估表已生成（科室那一半 {len(cases)} 例）：{sheet_path}")
        build_request_json(request_path, sheet_path, cases)

    plan_path = workdir / "plan.json"
    write_demo_plan(plan_path, cases, delay=0.05)
    ports_file = workdir / "ports.json"
    stub = start_stub_process(plan_path, ports_file)
    bases = json.loads(ports_file.read_text(encoding="utf-8"))
    print(f"三端点协议替身已起：{bases}")

    exports = env_exports(bases)
    env_file = workdir / "env.sh"
    env_file.write_text(exports, encoding="utf-8")
    runs_dir = workdir / "runs"
    store_path = workdir / "store" / "nodule_eval.sqlite3"
    windows: list[str] = []
    loading_snapshot = workdir / "loading_state.json"
    source_env = f"source {shlex.quote(rel(env_file))}"

    def poll_loading_state() -> None:
        """跑的过程中抓一份**真实**状态文件（加载态那一格）。"""

        state = read_status_json(status_path(runs_dir / RUN_ID))
        if not state:
            return
        processed = int(state.get("processed") or 0)
        total = int(state.get("total") or 0)
        if 0 < processed < total and str(state.get("state")) == "running":
            loading_snapshot.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    try:
        if not args.skip_terminal and not args.render_only:
            # ---- 1. 真跑一轮 200 例 ----
            command = (
                f"{source_env} && "
                f"python3 -m nodule_eval.cli run --request {shlex.quote(rel(request_path))} "
                f"--runs-dir {shlex.quote(rel(runs_dir))} "
                f"--store {shlex.quote(rel(store_path))} "
                f"--concurrency 4 --size-tolerance-mm 2.0"
            )
            info = terminal_run(
                command, tag="run", workdir=workdir,
                title="1/4 真跑一轮 200 例（逐例真调推理端点）",
                out_png=shots_dir / "01-terminal-run-200-cases.png",
                on_poll=poll_loading_state,
            )
            windows.append(info["window_id"])

            # ---- 2. 回填 + 导出 ----
            command = (
                f"{source_env} && "
                f"python3 -m nodule_eval.cli fill --run-id {RUN_ID} "
                f"--sheet {shlex.quote(rel(sheet_path))} "
                f"--runs-dir {shlex.quote(rel(runs_dir))} "
                f"--store {shlex.quote(rel(store_path))}\n"
                f"{source_env} && "
                f"python3 -m nodule_eval.cli export --run-id {RUN_ID} "
                f"--runs-dir {shlex.quote(rel(runs_dir))} "
                f"--store {shlex.quote(rel(store_path))} --format both"
            )
            info = terminal_run(
                command, tag="fill-export", workdir=workdir,
                title="2/4 回填原表新增六列 + 导出结论表",
                out_png=shots_dir / "02-terminal-fill-and-export.png",
            )
            windows.append(info["window_id"])

            # ---- 3. 人工确认提交 → 幂等写回科研平台 ----
            command = (
                f"{source_env} && "
                f"python3 -m nodule_eval.cli submit --run-id {RUN_ID} --review "
                f"--submitted-by demo-radiology "
                f"--runs-dir {shlex.quote(rel(runs_dir))} "
                f"--store {shlex.quote(rel(store_path))}\n"
                f"{source_env} && "
                f"python3 -m nodule_eval.cli replay --run-id {RUN_ID} "
                f"--runs-dir {shlex.quote(rel(runs_dir))} "
                f"--store {shlex.quote(rel(store_path))}"
            )
            info = terminal_run(
                command, tag="submit", workdir=workdir,
                title="3/4 人工确认提交 → 幂等写回科研平台评估登记",
                out_png=shots_dir / "03-terminal-submit-writeback.png",
            )
            windows.append(info["window_id"])

        # ---- 4. 失败态：推理端点报 5xx ----
        broken_cases = cases[:6]
        for case in broken_cases:
            case["behavior"] = {"mode": "fail500"}
        broken_plan = workdir / "plan-broken.json"
        write_demo_plan(broken_plan, broken_cases, delay=0.0)
        broken_ports = workdir / "ports-broken.json"
        broken_stub = start_stub_process(broken_plan, broken_ports)
        broken_bases = json.loads(broken_ports.read_text(encoding="utf-8"))
        try:
            broken_request = workdir / "req-broken.json"
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            payload["run_id"] = "demo-broken-001"
            payload["cases"] = payload["cases"][:6]
            broken_request.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            broken_exports = env_exports(broken_bases)
            broken_env_file = workdir / "env-broken.sh"
            broken_env_file.write_text(broken_exports, encoding="utf-8")
            broken_command = (
                f"source {shlex.quote(rel(broken_env_file))} && "
                f"python3 -m nodule_eval.cli run --request {shlex.quote(rel(broken_request))} "
                f"--runs-dir {shlex.quote(rel(runs_dir))} "
                f"--store {shlex.quote(rel(store_path))} --concurrency 1"
            )
            if not args.skip_terminal and not args.render_only:
                info = terminal_run(
                    broken_command, tag="broken", workdir=workdir,
                    title="4/4 失败态：推理端点返回 5xx（该例判失败，不当阴性）",
                    out_png=shots_dir / "04-terminal-failed-state.png",
                )
                windows.append(info["window_id"])
        finally:
            broken_stub.terminate()

        # ---- 5. 加载态：跑的过程中抓一份真实状态文件 ----
        loading_state = load_loading_state(runs_dir, workdir)
    finally:
        stub.terminate()
        for window_id in windows:
            terminal_close(window_id)

    # ---- 6. 渲染表格界面（真实产物）----
    normal_state = read_status_json(status_path(runs_dir / RUN_ID))
    failed_state = read_status_json(status_path(runs_dir / "demo-broken-001"))
    export_dir = runs_dir / RUN_ID / "export"
    candidates = sorted(export_dir.glob("*.xlsx")) if export_dir.exists() else []
    export_xlsx = candidates[0] if candidates else export_dir / "评估结论.xlsx"

    render_sheet_png(
        sheet_path,
        shots_dir / "05-sheet-backfill-after-run.png",
        title="评估表（回填后）· 表头汇总区 + 逐例明细 + 漏诊区 + 状态栏",
        caption=(
            f"本图由真实产物渲染：{sheet_path.name}（既有三列由科室填写，"
            "产品只追加右边六列；金标准原文逐格未改）。本环境无 Excel/WPS，"
            "宏未在真表格里点过，故界面视图由回填后的真实工作簿渲染，非设计稿。"
        ),
    )
    if export_xlsx.exists():
        render_export_png(
            export_xlsx,
            shots_dir / "06-export-conclusion-table.png",
            caption=(
                f"本图由真实产物渲染：{export_xlsx.name}（导出结论表按钮的产物："
                "评估结论 / 逐例明细 / 漏诊清单 / 字段说明 四页）。"
                "本环境无 Excel/WPS，故由导出的真实工作簿渲染，非设计稿。"
            ),
        )
    render_states_png(
        shots_dir / "07-macro-ui-three-states.png",
        normal=normal_state,
        loading=loading_state,
        failed=failed_state,
        buttons=macro_button_labels(),
        caption=(
            "三态文字全部取自真实运行状态文件 runs/<run_id>/engine_status.json"
            "（正常态取自 200 例那一轮，失败态取自推理端点报 5xx 那一轮）；"
            "按钮名取自 src/nodule_eval/sheets/xlsm_vba/nodule_eval.bas。"
            "本环境无 Excel/WPS，宏未在真表格里点过，故为真实状态文件的渲染。"
        ),
    )

    print("截图已采集：")
    for path in sorted(shots_dir.glob("*.png")):
        print(f"  - {path}")
    return 0


def load_loading_state(runs_dir: Path, workdir: Path) -> dict[str, Any]:
    """跑的过程中抓一份**真实**状态文件（加载态）。"""

    snapshot = workdir / "loading_state.json"
    if snapshot.exists():
        try:
            return json.loads(snapshot.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    status = read_status_json(runs_dir / RUN_ID / "engine_status.json")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
