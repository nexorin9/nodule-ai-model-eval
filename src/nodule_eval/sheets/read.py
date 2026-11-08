"""评估表读取：xlsm / CSV → 病例输入契约（``CaseInput``）。

**这一层做什么**：放射科交来一张表（既有列由科室维护），把它读成引擎能吃的
``CaseInput`` 序列。三入口里的 xlsm 宏与 WPS JSA 宏都要过这一层，
CSV 直跑也过 —— 差异只在「怎么把表里的行变成 cases」，不在契约本身。

**这一层守住的三条线**（对应任务的四条边界）：

1. **不静默丢行**：既有列全空的行是模板预留的空白行，跳过但**计数**；
   有内容却没有检查号的行，要么并入上一行（跨行叙述），要么跳过并**记行号**。
2. **不静默改原文**：``gold_standard_text`` 逐字保留（含换行与全角标点）；
   归一后的文本另存一份（``gold_standard_normalized``）—— **分析用那份、
   留痕用原文**。金标准原文永不被改写，这条在代码层成立，不靠约定。
3. **不猜**：既有列缺了、金标准是空白、编码解不出来 —— 全部**显式报错并点名**
   （错误码 ``CONTRACT_INVALID``），不填默认值蒙混过关。

**跨行叙述拼接**：放射科把一段叙述分两行写（**同一个检查号**连着两行）时，
两行并成一例，段与段之间用**换行**隔开 —— 换行是硬的分句边界，
「未见异常」在第一段就不会把第二段的「见结节」一起否定掉。拼接**不静默**：
每次拼接都记下行号，落进结果里的 ``joins`` 与逐行 ``flags``。

**没有检查号的行永远不被并进别的例**：它无法评估，跳过并记行号 ——
把一段不相干的叙述并进上一行，会污染那一例的金标准，比少读一行更糟。

**源产品证据**：``github_ref/MedRAX/data/get_cases.py:33`` 的
``span.text.strip().replace("#", "").strip()`` —— 标识符入库前的归一
（去空白、去前缀符号）；同文件 37-38 行「本页条数与预期不符就告警」——
数量对不上要**说出来**，不能悄悄吞掉。两处分别落为
:func:`normalize_identifier` 与本模块的计数与行号报告。
"""

from __future__ import annotations

import argparse
import codecs
import csv
import io
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Sequence

from ..contracts import CaseInput
from ..errors import ErrorCode, NoduleEvalError
from .columns import COLUMN_LABELS_ZH, EXISTING_COLUMNS
from .sections import is_section_marker
from .template import DETAIL_SHEET_NAMES

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 金标准原文的长度上限（字符）。超过则**截断并标记**，不静默丢弃。
MAX_GOLD_TEXT_CHARS = 4000

#: 截断后追加的标记 —— 复核人一看就知道原文被切过，不会当成完整原文。
TRUNCATION_MARKER = (
    f"…（原文超长，已截断至 {MAX_GOLD_TEXT_CHARS} 字；完整原文以评估表为准）"
)

#: 整文件严格解码时依次尝试的编码。
WHOLE_FILE_ENCODINGS: tuple[str, ...] = ("utf-8",)

#: 整文件解不出时**逐行**回退的编码 —— 处理 UTF-8 与 GBK 混编。
LINE_FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8", "gb18030", "gbk")

#: 表头行最多往下找多少行（表头不在第一行的情况）。
HEADER_SEARCH_ROWS = 20

#: 支持的评估表格式。
SUPPORTED_SUFFIXES: tuple[str, ...] = (".xlsm", ".xlsx", ".csv", ".tsv", ".txt")

#: 表头归一时剔除的字符：空白（含全角空格）、下划线、连字符、括号、冒号、
#: 点、斜杠、逗号、方括号、书名号与引号。
#: 「Accession No」「ACCESSION_NO」「accession-no」归一后是同一个键。
_HEADER_NOISE = " \t\r\n　_-—–/\\.,;:()[]【】、，\"'“”‘’《》"

#: 中文标点 → ASCII 的归一表（分析用文本）。原文不受影响。
_CN_PUNCT_MAP: dict[int, str] = str.maketrans(
    {
        "。": ".",
        "、": ",",
        "；": ";",
        "：": ":",
        "！": "!",
        "？": "?",
        "《": "<",
        "》": ">",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "—": "-",
        "–": "-",
        "～": "~",
        "·": ".",
    }
)

def _normalize_key(value: str) -> str:
    """表头 / 列名归一：全角转半角、大小写归一、剔除分隔符与空白。"""

    text = unicodedata.normalize("NFKC", value).lower()
    return "".join(ch for ch in text if ch not in _HEADER_NOISE)


#: 契约列名 → 归一后的键（供前缀匹配用）。
COLUMN_KEYS: dict[str, str] = {name: _normalize_key(name) for name in EXISTING_COLUMNS}

#: 归一后的键 → 契约列名（供表头逐字命中用）。
COLUMN_BY_KEY: dict[str, str] = {key: name for name, key in COLUMN_KEYS.items()}


#: 中文列名（「字段说明」页那一套）归一后的键 → 契约列名。
ALIAS_KEYS: dict[str, str] = {
    _normalize_key(label): name
    for name, label in COLUMN_LABELS_ZH.items()
    if name in EXISTING_COLUMNS
}


# --------------------------------------------------------------------------
# 文本归一
# --------------------------------------------------------------------------


def normalize_identifier(value: str) -> str:
    """标识符归一：检查号与影像引用用。

    - 全角转半角（``１２３`` → ``123``）—— RIS 里的检查号是半角的；
    - 去掉首尾与中间的多余空白；
    - 去掉前缀符号（``#``）—— 源产品的检查号归一同样去前缀。

    **大小写不动**：检查号大小写敏感，改了就对不上 RIS。
    """

    text = unicodedata.normalize("NFKC", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    while text.startswith("#"):
        text = text[1:].strip()
    return text


def normalize_gold_standard_text(text: str) -> str:
    """金标准原文的**分析用**归一：全角数字、中文标点、行内空白。

    **原文不受影响** —— 这个函数只产出分析用的那一份副本，
    ``gold_standard_text`` 里存的是逐字原文。

    **换行保留**：换行是分句边界（「未见异常」不该把下一行的「见结节」一起
    否定掉），归一时只清行内空白与行尾空白，不动换行的位置。
    """

    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.translate(_CN_PUNCT_MAP)
    lines = [re.sub(r"[ \t　]+", " ", line).strip() for line in normalized.split("\n")]
    return "\n".join(lines)


def fragment_spans(text: str) -> tuple[tuple[int, int], ...]:
    """给出文本里每个**片段**（换行分隔）的字符区间。

    拼接后的金标准靠它定位：某一行的「未见」落在哪个片段、会不会跨片段
    否定后面的内容，一目了然。下游的否定作用域判定（任务 20）用它给
    否定词定界。
    """

    spans: list[tuple[int, int]] = []
    offset = 0
    for fragment in text.split("\n"):
        spans.append((offset, offset + len(fragment)))
        offset += len(fragment) + 1
    return tuple(spans)


def cell_text(value: Any) -> str:
    """把单元格值转成文本，**不做任何内容改写**。

    数字型检查号（Excel 把 ``20240115001`` 存成整数）转成字符串而不是
    ``20240115001.0``；日期时间用 ISO 形态；其余按原样取字符串。
    """

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value)


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SheetRow:
    """评估表里的一行 —— 读出来之后的形态。

    ``row_no`` 是**工作表里的行号**（1 基，含表头行）—— 回填（任务 46）
    按它定位要写哪一行，所以它必须是人在 Excel 里看到的那一行。
    """

    row_no: int
    accession_no: str | None
    gold_standard_text: str
    gold_standard_normalized: str
    image_ref: str | None
    source_rows: tuple[int, ...] = ()
    truncated: bool = False
    original_length: int = 0
    flags: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_no": self.row_no,
            "accession_no": self.accession_no,
            "gold_standard_text": self.gold_standard_text,
            "gold_standard_normalized": self.gold_standard_normalized,
            "image_ref": self.image_ref,
            "source_rows": list(self.source_rows),
            "truncated": self.truncated,
            "original_length": self.original_length,
            "flags": list(self.flags),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ContinuationJoin:
    """一次跨行叙述拼接：第 ``source_row_no`` 行并入了第 ``target_row_no`` 行。"""

    target_row_no: int
    source_row_no: int
    fragments: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_row_no": self.target_row_no,
            "source_row_no": self.source_row_no,
            "fragments": list(self.fragments),
        }


@dataclass(frozen=True)
class DecodeResult:
    """CSV 解码结果 —— 混编要被**说出来**，不能悄悄按一种编码解。"""

    text: str
    encodings: tuple[str, ...] = ()
    mixed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "encodings": list(self.encodings),
            "mixed": self.mixed,
        }


@dataclass(frozen=True)
class SheetReadResult:
    """一次评估表读取的完整结果。"""

    source: str
    sheet_name: str = ""
    header_row: int = 0
    cases: tuple[CaseInput, ...] = ()
    rows: tuple[SheetRow, ...] = ()
    blank_rows: int = 0
    skipped_rows: tuple[int, ...] = ()
    joins: tuple[ContinuationJoin, ...] = ()
    encodings: tuple[str, ...] = ()
    mixed_encoding: bool = False
    warnings: tuple[str, ...] = ()

    def counts(self) -> dict[str, int]:
        """行数台账 —— 每一类行都有数，加起来等于表里的数据行数。"""

        return {
            "cases": len(self.cases),
            "blank_rows": self.blank_rows,
            "skipped_rows": len(self.skipped_rows),
            "joined_rows": len(self.joins),
            "truncated_rows": sum(1 for row in self.rows if row.truncated),
        }

    def row_for(self, accession_no: str) -> SheetRow | None:
        """按检查号取回那一行（复核与排障用）。"""

        for row in self.rows:
            if row.accession_no == accession_no:
                return row
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "sheet_name": self.sheet_name,
            "header_row": self.header_row,
            "counts": self.counts(),
            "cases": [case.to_dict() for case in self.cases],
            "rows": [row.to_dict() for row in self.rows],
            "skipped_rows": list(self.skipped_rows),
            "joins": [join.to_dict() for join in self.joins],
            "encodings": list(self.encodings),
            "mixed_encoding": self.mixed_encoding,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------
# 解码
# --------------------------------------------------------------------------


def decode_bytes(raw: bytes) -> DecodeResult:
    """解一段字节，**处理 UTF-8 与 GBK 混编**。

    整文件能解就整文件解（快，且不会误判）。解不出时**逐行**回退：
    一行一行试 UTF-8 → GB18030 → GBK。混编文件里两种编码的行各按各的
    解，**不把整份文件按一种编码硬解** —— 那样 UTF-8 的行会变成乱码，
    而乱码照样能通过解码，看不出错。

    混编要被**报出来**：``mixed=True`` 且 ``encodings`` 列出用过的编码。
    """

    if raw.startswith(codecs.BOM_UTF8):
        try:
            return DecodeResult(raw.decode("utf-8-sig"), ("utf-8-sig",), False)
        except UnicodeDecodeError:  # pragma: no cover - BOM 存在却解不出，罕见
            pass

    for encoding in WHOLE_FILE_ENCODINGS:
        try:
            return DecodeResult(raw.decode(encoding), (encoding,), False)
        except UnicodeDecodeError:
            continue

    # 整文件解不出 → 多半是 UTF-8 与 GBK 混编：逐行回退。
    pieces: list[str] = []
    used: list[str] = []
    non_ascii_used: list[str] = []
    for line in raw.split(b"\n"):
        decoded: str | None = None
        has_non_ascii = any(byte >= 0x80 for byte in line)
        for encoding in LINE_FALLBACK_ENCODINGS:
            try:
                decoded = line.decode(encoding)
            except UnicodeDecodeError:
                continue
            used.append(encoding)
            if has_non_ascii:
                non_ascii_used.append(encoding)
            break
        if decoded is None:
            # latin-1 不会失败：字节全部保下来，宁可乱码也不丢内容。
            decoded = line.decode("latin-1")
            used.append("latin-1")
            non_ascii_used.append("latin-1")
        pieces.append(decoded)

    distinct = tuple(dict.fromkeys(non_ascii_used))
    return DecodeResult(
        text="\n".join(pieces),
        encodings=distinct or ("ascii",),
        mixed=len(distinct) > 1,
    )


# --------------------------------------------------------------------------
# 表头定位
# --------------------------------------------------------------------------


def _map_columns(row: Sequence[Any]) -> dict[str, int]:
    """把一行表头映射成「契约列名 → 列号（0 基）」。

    容错三档：契约列名逐字命中 → 中文列名命中 → 前缀命中
    （``金标准原文（必填）``、``Accession No``、``ACCESSION_NO`` 都能对上）。

    契约列名优先于中文别名：一张表里同时出现两套写法时，按契约名定位。
    """

    exact: dict[str, int] = {}
    alias: dict[str, int] = {}

    for index, value in enumerate(row):
        if value is None:
            continue
        key = _normalize_key(cell_text(value))
        if not key:
            continue

        name = COLUMN_BY_KEY.get(key)
        if name is not None:
            exact.setdefault(name, index)
            continue

        name = ALIAS_KEYS.get(key)
        if name is not None:
            alias.setdefault(name, index)
            continue

        for candidate in EXISTING_COLUMNS:
            if key.startswith(COLUMN_KEYS[candidate]):
                alias.setdefault(candidate, index)
                break
        else:
            for label, candidate in ALIAS_KEYS.items():
                if key.startswith(label):
                    alias.setdefault(candidate, index)
                    break

    merged = dict(alias)
    merged.update(exact)
    return merged


def _find_header_row(rows: Sequence[Sequence[Any]]) -> tuple[int, dict[str, int]]:
    """在前若干行里找表头行，返回（表头行号（1 基）、列号映射）。"""

    best_row = 0
    best_map: dict[str, int] = {}
    for index, row in enumerate(rows[:HEADER_SEARCH_ROWS], start=1):
        mapping = _map_columns(row)
        if len(mapping) > len(best_map):
            best_row = index
            best_map = mapping
        if len(best_map) >= len(EXISTING_COLUMNS):
            break
    return best_row, best_map


def _require_columns(mapping: dict[str, int], header_row: int, source: str) -> None:
    """既有列缺了就报错并**点名缺哪一列**。"""

    missing = [name for name in ("accession_no", "gold_standard_text") if name not in mapping]
    if missing:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                f"评估表 {source} 的表头行（第 {header_row} 行）缺少既有列："
                f"{'、'.join(missing)}；既有列须为 accession_no / gold_standard_text / image_ref"
            ),
        )


# --------------------------------------------------------------------------
# 行装配
# --------------------------------------------------------------------------


def _truncate(text: str) -> tuple[str, bool]:
    """超长金标准文本：截断**并标记**，不静默丢弃。

    尽量切在句子边界上 —— 从中间切断有可能把「未见」切在「见」的前面，
    把一个阳性读成阴性，那比截断本身严重得多。找不到合适的边界才硬切。
    """

    if len(text) <= MAX_GOLD_TEXT_CHARS:
        return text, False

    cut = text[:MAX_GOLD_TEXT_CHARS]
    floor = int(MAX_GOLD_TEXT_CHARS * 0.6)
    for mark in ("。", ".", "；", ";", "！", "!", "？", "?", "\n"):
        position = cut.rfind(mark)
        if position >= floor:
            cut = cut[: position + 1]
            break
    return cut + TRUNCATION_MARKER, True


def _assemble_rows(
    rows: Sequence[Sequence[Any]],
    header_row: int,
    mapping: dict[str, int],
) -> tuple[
    tuple[CaseInput, ...],
    tuple[SheetRow, ...],
    int,
    tuple[int, ...],
    tuple[ContinuationJoin, ...],
    tuple[str, ...],
]:
    """把数据行装配成病例输入。

    返回（病例、逐行记录、空行数、跳过的行号、拼接记录、警告）。
    """

    def cell(row: Sequence[Any], name: str) -> str:
        index = mapping.get(name)
        if index is None or index >= len(row):
            return ""
        return cell_text(row[index])

    pending: list[dict[str, Any]] = []
    skipped: list[int] = []
    seen_accessions: dict[str, int] = {}
    joins: list[ContinuationJoin] = []
    warnings: list[str] = []
    blank_rows = 0
    last_was_case = False

    for index in range(header_row, len(rows)):
        row_no = index + 1
        row = rows[index]

        # **明细区到此为止**：评估表模板在明细区之下还有漏诊明细区与底部状态栏，
        # 两者的第一格都带区块标记（见 :mod:`nodule_eval.sheets.sections`）。
        # 漏诊区里也有检查号 —— 那是引擎回填的**结论**，不是输入；
        # 一路读到表尾会把同一例读两遍，还会拿空的金标准原文报错。
        if row and is_section_marker(cell_text(row[0])):
            break

        accession_raw = cell(row, "accession_no")
        gold_raw = cell(row, "gold_standard_text")
        image_raw = cell(row, "image_ref")

        accession_no = normalize_identifier(accession_raw)
        image_ref = normalize_identifier(image_raw) or None
        has_gold = bool(gold_raw.strip())

        if not accession_raw.strip() and not has_gold and image_ref is None:
            # 模板预留的空白行：跳过但计数，不静默丢。
            blank_rows += 1
            continue

        if not accession_no:
            # 没有检查号的行**整行跳过并计数** —— 它无法评估。
            # 「跨行叙述」指同一个检查号写在相邻两行，不是「这一行没写检查号」：
            # 后者并进上一行会把不相干的叙述混进别的例，污染金标准。
            skipped.append(row_no)
            reason = "有金标准原文但没有检查号" if has_gold else "有内容但没有检查号"
            warnings.append(f"第 {row_no} 行{reason}，已跳过")
            last_was_case = False
            continue

        if not has_gold:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=(
                    f"评估表第 {row_no} 行的金标准原文（gold_standard_text）为空；"
                    f"该例无法评估，请补写后重跑"
                ),
            )

        if pending and pending[-1]["accession_no"] == accession_no:
            # 跨行叙述：**同一个检查号**写在相邻两行 —— 并成一例。
            # 段与段之间用换行隔开（硬的分句边界），「未见异常」不会把
            # 下一段的「见结节」一起否定掉。
            target = pending[-1]
            target["fragments"].append(gold_raw)
            target["source_rows"].append(row_no)
            if target["image_ref"] is None:
                target["image_ref"] = image_ref
            joins.append(
                ContinuationJoin(
                    target_row_no=target["source_rows"][0],
                    source_row_no=row_no,
                    fragments=tuple(target["fragments"]),
                )
            )
            warnings.append(
                f"第 {row_no} 行与第 {target['source_rows'][0]} 行是同一个检查号，"
                f"已并作一例"
            )
            last_was_case = True
            continue

        pending.append(
            {
                "row_no": row_no,
                "accession_no": accession_no,
                "fragments": [gold_raw],
                "source_rows": [row_no],
                "image_ref": image_ref,
            }
        )
        if accession_no in seen_accessions:
            # 同一个检查号**隔行**重复 —— 不并（并了会把两处叙述混成一段），
            # 也不静默：留给绑定层按「检查号重复」挂起，这里先把行号说出来。
            first_row = seen_accessions[accession_no]
            warnings.append(
                f"检查号 {accession_no} 在第 {first_row} 行与第 {row_no} 行重复出现，"
                f"未合并；请核对后由绑定层挂起"
            )
        else:
            seen_accessions[accession_no] = row_no
        last_was_case = True

    cases: list[CaseInput] = []
    sheet_rows: list[SheetRow] = []

    for item in pending:
        text = "\n".join(item["fragments"])
        truncated_text, truncated = _truncate(text)
        normalized = normalize_gold_standard_text(truncated_text)

        flags: list[str] = []
        if len(item["source_rows"]) > 1:
            flags.append("joined")
        if truncated:
            flags.append("truncated")
        if normalized != truncated_text:
            flags.append("normalized")

        row_warnings: list[str] = []
        if truncated:
            row_warnings.append(
                f"第 {item['row_no']} 行金标准原文超长，已截断至 {MAX_GOLD_TEXT_CHARS} 字"
            )

        sheet_row = SheetRow(
            row_no=item["row_no"],
            accession_no=item["accession_no"],
            gold_standard_text=truncated_text,
            gold_standard_normalized=normalized,
            image_ref=item["image_ref"],
            source_rows=tuple(item["source_rows"]),
            truncated=truncated,
            original_length=len(text),
            flags=tuple(flags),
            warnings=tuple(row_warnings),
        )
        sheet_rows.append(sheet_row)

        cases.append(
            CaseInput(
                accession_no=item["accession_no"],
                gold_standard_text=truncated_text,
                image_ref=item["image_ref"],
                row_no=item["row_no"],
                gold_standard_normalized=normalized,
            )
        )

    return (
        tuple(cases),
        tuple(sheet_rows),
        blank_rows,
        tuple(skipped),
        tuple(joins),
        tuple(warnings),
    )


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def _read_workbook_rows(
    path: Path, sheet_name: str | None
) -> tuple[list[list[Any]], str]:
    """读工作簿的逐行值（``.xlsm`` / ``.xlsx``）。

    工作表缺省取明细页（**「评估表」或「逐例明细」都认** —— 前者是我们
    生成的模板页，后者是科室自己的表常见的页名）；两页都没有就取第一页。
    """

    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet_name:
            if sheet_name not in workbook.sheetnames:
                raise NoduleEvalError(
                    ErrorCode.CONTRACT_INVALID,
                    detail=f"评估表 {path} 里没有工作表「{sheet_name}」",
                )
            worksheet = workbook[sheet_name]
        else:
            worksheet = workbook[workbook.sheetnames[0]]
            for candidate in DETAIL_SHEET_NAMES:
                if candidate in workbook.sheetnames:
                    worksheet = workbook[candidate]
                    break
        title = worksheet.title
        rows = [list(row) for row in worksheet.iter_rows(values_only=True)]
        return rows, title
    finally:
        workbook.close()


def _read_csv_rows(path: Path) -> tuple[list[list[str]], DecodeResult]:
    """读 CSV 的逐行值（``utf-8`` / ``gbk`` / 两者混编）。"""

    decoded = decode_bytes(path.read_bytes())
    sample = decoded.text[:4096]
    delimiter = ","
    if sample.strip():
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.reader(io.StringIO(decoded.text), delimiter=delimiter)
    rows = [list(row) for row in reader]
    return rows, decoded


def read_sheet(
    path: Path | str,
    *,
    sheet_name: str | None = None,
) -> SheetReadResult:
    """读一张评估表，装配成病例输入契约。

    支持 ``.xlsm`` / ``.xlsx`` / ``.csv`` / ``.tsv`` / ``.txt``。
    """

    file_path = Path(path)
    if not file_path.is_file():
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"评估表文件不存在：{file_path}",
        )

    suffix = file_path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                f"评估表格式不支持：{suffix or '（无扩展名）'}；"
                f"支持 {'/'.join(SUPPORTED_SUFFIXES)}"
            ),
        )

    if suffix in (".csv", ".tsv", ".txt"):
        rows, decoded = _read_csv_rows(file_path)
        title = file_path.stem
        encodings = decoded.encodings
        mixed = decoded.mixed
    else:
        rows, title = _read_workbook_rows(file_path, sheet_name)
        encodings = ()
        mixed = False

    header_row, mapping = _find_header_row(rows)
    if not mapping:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                f"评估表 {file_path} 找不到表头行："
                f"前 {HEADER_SEARCH_ROWS} 行里没有既有列"
                f"（accession_no / gold_standard_text / image_ref）"
            ),
        )
    _require_columns(mapping, header_row, str(file_path))

    cases, sheet_rows, blank_rows, skipped, joins, warnings = _assemble_rows(
        rows, header_row, mapping
    )

    return SheetReadResult(
        source=str(file_path),
        sheet_name=title,
        header_row=header_row,
        cases=cases,
        rows=sheet_rows,
        blank_rows=blank_rows,
        skipped_rows=skipped,
        joins=joins,
        encodings=encodings,
        mixed_encoding=mixed,
        warnings=warnings,
    )


def read_sheet_cases(
    path: Path | str,
    *,
    sheet_name: str | None = None,
) -> tuple[CaseInput, ...]:
    """只要病例输入序列时的便捷入口。"""

    return read_sheet(path, sheet_name=sheet_name).cases


# --------------------------------------------------------------------------
# 命令行入口（无宏退路：直接看这张表读成了什么）
# --------------------------------------------------------------------------


def _summary_lines(result: SheetReadResult) -> list[str]:
    counts = result.counts()
    lines = [
        f"评估表      : {result.source}",
        f"工作表      : {result.sheet_name}（表头在第 {result.header_row} 行）",
        f"病例数      : {counts['cases']}",
        f"空白行      : {counts['blank_rows']}",
        f"跳过行      : {counts['skipped_rows']}"
        + (f"（行号 {', '.join(str(no) for no in result.skipped_rows)}）" if result.skipped_rows else ""),
        f"跨行拼接    : {counts['joined_rows']}",
        f"超长截断    : {counts['truncated_rows']}",
    ]
    if result.encodings:
        lines.append(
            "解码编码    : "
            + "/".join(result.encodings)
            + ("（检测到 UTF-8 与 GBK 混编，已逐行回退）" if result.mixed_encoding else "")
        )
    for warning in result.warnings:
        lines.append(f"提示        : {warning}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m nodule_eval.sheets.read",
        description="读取评估表（xlsm / CSV），装配成病例输入契约并打印摘要",
    )
    parser.add_argument("path", help="评估表路径（.xlsm / .xlsx / .csv）")
    parser.add_argument(
        "--sheet",
        default=None,
        help="工作表名（缺省用「评估表」页 / 「逐例明细」页 / 第一页）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出完整结果")
    args = parser.parse_args(argv)

    try:
        result = read_sheet(args.path, sheet_name=args.sheet)
    except NoduleEvalError as exc:
        print(f"[{exc.code.value}] {exc.message}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        for line in _summary_lines(result):
            print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
