"""部位与密度等价表 —— 金标准归并与模型输出比对**共用的唯一真源**。

**为什么必须只有一张表**：同一处结节，放射科在金标准里写「右下肺」，
厂商模型回「右肺下叶」，指的是同一处病灶。两边各写一张表，一旦两边漂移，
同一处病灶就会被判成「部位不一致」—— 一次命中被算成漏诊，敏感度直接被拉低，
而且从报告上看不出来是口径问题还是模型问题。

**这张表因此被两侧共用**：

- 金标准侧：把放射科写的自由文本归成标准标签（任务 19 的规则基线、
  任务 21 的语义归并都调这里）；
- 模型侧：把厂商返回的 ``position`` / ``density`` 归成**同一套**标准标签
  （任务 27 的部位 / 密度比对调这里）。

**口径三条**：

1. **肺段归入所属肺叶**：胸片报告写「右下肺S6」「右下叶背段」，
   判定时按**肺叶**比（右肺下叶），段号保留在归并结果里供事后核对。
2. **认不出来就是认不出来**：写法不在表里 → 返回 ``None``，
   **不猜一个近似标签**。猜测出来的标签会直接改写敏感度与特异度。
   （「左肺中叶」这种不存在的解剖结构同样返回 ``None``。）
3. **侧别未指定时不补侧别**：只写「上叶」「S6」时，标准标签也**不带侧别**
   （``上叶`` / ``下叶``）。侧别不一致怎么判，交给判定层的最严口径（任务 27），
   本表只负责把写法归一，不替医生做决定。

**表在数据文件里**（``labels.yaml``）：新增别名只改那一个文件，
判定逻辑代码一行不动 —— 加一条别名不该动判定逻辑。

**源产品对照**：参考实现的工具集把胸片压成 18 类病理的存在性概率
（Nodule / Lung Lesion / Lung Opacity …，``github_ref/MedRAX/medrax/tools/classification.py``），
**不带部位也不带密度**，答案还是一段自由文本
（``github_ref/MedRAX/interface.py:142`` 只把临时路径从回答里剔掉）。
本产品要在「命中 / 漏诊 / 误报」之间做**可复算**的比对，就得把标签固定成
一张可归一的表 —— 这一层是**新建**的，源产品没有对应物。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from ..errors import ErrorCode, NoduleEvalError

__all__ = [
    "DEFAULT_TABLE_PATH",
    "DensityMatch",
    "LabelTable",
    "LocationMatch",
    "aliases_for",
    "canonical_densities",
    "canonical_locations",
    "describe_tables",
    "find_densities",
    "find_density",
    "find_location",
    "find_locations",
    "load_label_table",
    "location_lobe",
    "location_side",
    "normalize_density",
    "normalize_densities",
    "normalize_key",
    "normalize_location",
    "normalize_locations",
    "resolve_density",
    "resolve_location",
]


#: 等价表数据文件（与本模块同目录 —— 表跟着代码走，不跟着工作目录走）。
DEFAULT_TABLE_PATH = Path(__file__).resolve().parent / "labels.yaml"

#: 肺叶后缀（按长度倒序不影响：三个都是两字）。
_LOBE_SUFFIXES: tuple[str, ...] = ("上叶", "中叶", "下叶")

#: 键归一时去掉的标点（术语内部不会用这些字符）。
_PUNCT_RE = re.compile(r"[·、，,。.；;：:！!？?（）()\[\]【】《》<>/\\|~*#\"'“”‘’]+")

#: 键归一时去掉的空白与连字符（「ground glass」与「ground-glass」认作同一个）。
_SPACE_RE = re.compile(r"[\s\-_]+")


def normalize_key(value: str) -> str:
    """把一个写法归一成**查表键**。

    - NFKC：全角转半角（``ＲＵＬ`` → ``RUL``，``Ｓ６`` → ``S6``）；
    - 去标点、去空白与连字符、字母小写。

    于是「右肺上叶」「右肺 上叶」「右肺上叶。」「RUL」落到同一个键上。
    **大小写影响不到中文术语，但影响英文缩写** —— 缩写一律小写化。
    """

    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value or "")
    text = text.strip().lower()
    text = _PUNCT_RE.sub("", text)
    text = _SPACE_RE.sub("", text)
    return text


def _squash(text: str) -> tuple[str, list[int]]:
    """把文本压成「无空白无连字符」的小写串，并保留每个字符在原文里的下标。

    压完的串与 :func:`normalize_key` 的产物同一形态，
    所以查表键可以直接在压后的串上做子串匹配；
    下标表用于把命中位置还原回**原文**（证据片段要指向原文）。
    """

    out: list[str] = []
    index_map: list[int] = []
    for index, char in enumerate(text):
        if char in " \t\r\n　-_":
            continue
        out.append(char.lower() if "A" <= char <= "Z" else char)
        index_map.append(index)
    return "".join(out), index_map


@dataclass(frozen=True)
class LocationMatch:
    """一次部位归一的结果。

    ``canonical``
        标准标签（判定层只认它）。
    ``matched_alias``
        命中的别名键（已归一）。
    ``matched_text``
        原文里命中的片段（文本扫描时给出；按术语查表时为空）。
    ``segment`` / ``segment_code``
        原文写的是哪个肺段（段号）。归入所属肺叶时**保留**，事后核对时
        说得清「右下肺S6」是怎么归成「右肺下叶」的。
    ``side`` / ``lobe``
        标准标签拆开的侧别与肺叶（``右肺下叶`` → ``右`` / ``下叶``）。
    """

    canonical: str
    matched_alias: str = ""
    matched_text: str = ""
    start: int = -1
    end: int = -1
    segment: str = ""
    segment_code: str = ""
    side: str = ""
    lobe: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical": self.canonical,
            "matched_alias": self.matched_alias,
            "matched_text": self.matched_text,
            "segment": self.segment,
            "segment_code": self.segment_code,
            "side": self.side,
            "lobe": self.lobe,
        }


@dataclass(frozen=True)
class DensityMatch:
    """一次密度归一的结果。"""

    canonical: str
    matched_alias: str = ""
    matched_text: str = ""
    start: int = -1
    end: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical": self.canonical,
            "matched_alias": self.matched_alias,
            "matched_text": self.matched_text,
        }


@dataclass(frozen=True)
class LabelTable:
    """等价表的载入结果（部位与密度各一套）。"""

    source_path: str
    canonical_locations: tuple[str, ...]
    location_aliases: Mapping[str, str]
    location_segments: Mapping[str, tuple[str, str]]
    canonical_densities: tuple[str, ...]
    density_aliases: Mapping[str, str]

    def describe(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "locations": {
                "canonical": list(self.canonical_locations),
                "alias_count": len(self.location_aliases),
                "segment_alias_count": len(self.location_segments),
            },
            "densities": {
                "canonical": list(self.canonical_densities),
                "alias_count": len(self.density_aliases),
            },
        }


def _contract_invalid(detail: str) -> NoduleEvalError:
    return NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail=detail)


def _build_alias_map(
    section: Any,
    canonical: Sequence[str],
    *,
    kind: str,
    source_path: str,
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """把 YAML 里的别名段解析成「归一键 → 标准标签」与「归一键 → 肺段」两张表。

    三条硬校验（载入即报错，不静默取一个）：

    - 两个别名归一后撞键 → 报错（写明是哪两个原始写法）；
    - 别名指向的标准标签没在 ``canonical`` 里登记 → 报错；
    - 别名的值既不是字符串也不是映射 → 报错。
    """

    if not isinstance(section, Mapping):
        raise _contract_invalid(f"等价表 {source_path} 的 {kind} 别名段须为映射结构")

    canonical_set = set(canonical)
    aliases: dict[str, str] = {}
    segments: dict[str, tuple[str, str]] = {}
    origin: dict[str, str] = {}

    for raw_key, raw_value in section.items():
        original = str(raw_key)
        key = normalize_key(original)
        if not key:
            raise _contract_invalid(f"等价表 {source_path} 的 {kind} 别名为空：{original!r}")

        if key in aliases:
            raise _contract_invalid(
                f"等价表 {source_path} 的 {kind} 别名归一后撞键："
                f"{origin[key]!r} 与 {original!r} 都归一到 {key!r}"
            )

        segment = ""
        segment_code = ""
        if isinstance(raw_value, str):
            label = raw_value.strip()
        elif isinstance(raw_value, Mapping):
            label = str(raw_value.get("canonical", "")).strip()
            segment = str(raw_value.get("segment", "")).strip()
            segment_code = str(raw_value.get("segment_code", "")).strip()
        else:
            raise _contract_invalid(
                f"等价表 {source_path} 的 {kind} 别名 {original!r} 的值须为字符串或映射"
            )

        if not label:
            raise _contract_invalid(f"等价表 {source_path} 的 {kind} 别名 {original!r} 缺标准标签")
        if label not in canonical_set:
            raise _contract_invalid(
                f"等价表 {source_path} 的 {kind} 别名 {original!r} 指向未登记的标准标签 {label!r}"
            )

        aliases[key] = label
        origin[key] = original
        if segment or segment_code:
            segments[key] = (segment, segment_code)

    return aliases, segments


def load_label_table(path: Path | str | None = None) -> LabelTable:
    """载入部位与密度等价表。

    - 文件不存在 / 解析失败 / 别名撞键 / 别名指向未登记标签
      → 抛 ``CONTRACT_INVALID``，**不回落到任何内置兜底表**
      （兜底表只会让口径悄悄漂移）。
    """

    table_path = Path(path) if path is not None else DEFAULT_TABLE_PATH
    if not table_path.is_file():
        raise _contract_invalid(f"部位/密度等价表文件不存在：{table_path}")

    try:
        raw = yaml.safe_load(table_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise _contract_invalid(f"部位/密度等价表 {table_path} 解析失败：{exc}") from exc
    except OSError as exc:
        raise _contract_invalid(f"部位/密度等价表 {table_path} 读取失败：{exc}") from exc

    if not isinstance(raw, Mapping):
        raise _contract_invalid(f"部位/密度等价表 {table_path} 顶层须为映射结构")

    locations = raw.get("locations")
    densities = raw.get("densities")
    if not isinstance(locations, Mapping) or not isinstance(densities, Mapping):
        raise _contract_invalid(f"部位/密度等价表 {table_path} 缺 locations 或 densities 段")

    location_canonical = locations.get("canonical")
    density_canonical = densities.get("canonical")
    if not isinstance(location_canonical, list) or not location_canonical:
        raise _contract_invalid(f"等价表 {table_path} 的 locations.canonical 须为非空列表")
    if not isinstance(density_canonical, list) or not density_canonical:
        raise _contract_invalid(f"等价表 {table_path} 的 densities.canonical 须为非空列表")

    location_aliases, location_segments = _build_alias_map(
        locations.get("aliases"),
        [str(item) for item in location_canonical],
        kind="部位",
        source_path=str(table_path),
    )
    density_aliases, _ = _build_alias_map(
        densities.get("aliases"),
        [str(item) for item in density_canonical],
        kind="密度",
        source_path=str(table_path),
    )

    return LabelTable(
        source_path=str(table_path),
        canonical_locations=tuple(str(item) for item in location_canonical),
        location_aliases=location_aliases,
        location_segments=location_segments,
        canonical_densities=tuple(str(item) for item in density_canonical),
        density_aliases=density_aliases,
    )


@lru_cache(maxsize=1)
def _default_table() -> LabelTable:
    return load_label_table()


def _table(table: LabelTable | None) -> LabelTable:
    return table if table is not None else _default_table()


def _split_canonical(canonical: str) -> tuple[str, str]:
    """把标准标签拆成（侧别, 肺叶）。

    ``右肺下叶`` → ``("右", "下叶")``；``下叶`` → ``("", "下叶")``；
    ``双肺`` → ``("双", "")``；``右肺`` → ``("右", "")``。
    """

    for lobe in _LOBE_SUFFIXES:
        if canonical.endswith(lobe):
            head = canonical[: -len(lobe)]
            if head == "":
                return "", lobe
            if head.endswith("肺"):
                return head[:-1], lobe
            return head, lobe
    if canonical.endswith("肺"):
        return canonical[:-1], ""
    return canonical, ""


def location_side(canonical: str) -> str:
    """标准标签的侧别（``右`` / ``左`` / ``双`` / ``""`` 表示未指定）。"""

    return _split_canonical(canonical)[0]


def location_lobe(canonical: str) -> str:
    """标准标签的肺叶（``""`` 表示整肺）。"""

    return _split_canonical(canonical)[1]


def resolve_location(value: str, *, table: LabelTable | None = None) -> LocationMatch | None:
    """把一个**部位术语**归一成标准标签。

    - 术语在表里 → 返回匹配结果（含肺段信息）；
    - 术语不在表里 → 返回 ``None``（**不猜一个近似标签**）。

    输入是**术语**（「右下肺」「右下肺S6」「RUL」），
    不是整句 —— 从整句里捞术语用 :func:`find_location`。
    """

    key = normalize_key(value)
    if not key:
        return None

    active = _table(table)
    label = active.location_aliases.get(key)
    if label is None:
        return None

    segment, segment_code = active.location_segments.get(key, ("", ""))
    side, lobe = _split_canonical(label)
    return LocationMatch(
        canonical=label,
        matched_alias=key,
        segment=segment,
        segment_code=segment_code,
        side=side,
        lobe=lobe,
    )


def normalize_location(value: str, *, table: LabelTable | None = None) -> str | None:
    """部位术语 → 标准标签；认不出来返回 ``None``。"""

    match = resolve_location(value, table=table)
    return match.canonical if match else None


def normalize_locations(
    values: Iterable[str],
    *,
    table: LabelTable | None = None,
) -> str | None:
    """多个写法 → 同一个标准标签。

    **全部写法都认得出来且归到同一个标签**时返回该标签；
    只要有一个认不出来、或归到不同标签，就返回 ``None``
    （**不挑一个当答案** —— 两个标签打架时最稳的做法是交人工复核）。
    """

    items = [item for item in values]
    if not items:
        return None

    labels: set[str] = set()
    for item in items:
        label = normalize_location(item, table=table)
        if label is None:
            return None
        labels.add(label)

    if len(labels) != 1:
        return None
    return labels.pop()


def _scan(
    text: str,
    alias_map: Mapping[str, str],
) -> tuple[str, str, int, int] | None:
    """在一段文本里找最靠左、且最长的那个命中。

    返回 ``(归一键, 标准标签, 起点, 终点)``；找不到返回 ``None``。

    **左most-longest**：同一个起点上先试长别名（「右肺下叶」先于「右肺」），
    免得「右下肺见结节」被判成整肺。
    """

    if not text:
        return None

    squashed, index_map = _squash(unicodedata.normalize("NFKC", text))
    if not squashed:
        return None

    buckets: dict[str, list[str]] = {}
    for alias in alias_map:
        buckets.setdefault(alias[0], []).append(alias)
    for bucket in buckets.values():
        bucket.sort(key=len, reverse=True)

    for start in range(len(squashed)):
        for alias in buckets.get(squashed[start], ()):
            if squashed.startswith(alias, start):
                end = start + len(alias)
                raw_start = index_map[start]
                raw_end = index_map[end - 1] + 1
                return alias, alias_map[alias], raw_start, raw_end
    return None


def find_location(text: str, *, table: LabelTable | None = None) -> LocationMatch | None:
    """在一段文本里找出部位术语并归一（给规则基线与语义归并用）。

    找不到 → ``None``（**不猜**）。
    """

    active = _table(table)
    hit = _scan(text, active.location_aliases)
    if hit is None:
        return None

    alias, label, start, end = hit
    segment, segment_code = active.location_segments.get(alias, ("", ""))
    side, lobe = _split_canonical(label)
    normalized_text = unicodedata.normalize("NFKC", text)
    return LocationMatch(
        canonical=label,
        matched_alias=alias,
        matched_text=normalized_text[start:end],
        start=start,
        end=end,
        segment=segment,
        segment_code=segment_code,
        side=side,
        lobe=lobe,
    )


def _scan_all(
    text: str,
    alias_map: Mapping[str, str],
) -> list[tuple[str, str, int, int]]:
    """在一段文本里找出**全部**互不重叠的命中。

    命中顺序与 :func:`_scan` 一致（**左most-longest**），只是不停在第一个 ——
    「右下肺见结节，左上肺见结节」一处一个部位，全扫出来才知道是几处病灶。

    返回 ``[(归一键, 标准标签, 起点, 终点), ...]``。
    """

    if not text:
        return []

    squashed, index_map = _squash(unicodedata.normalize("NFKC", text))
    if not squashed:
        return []

    buckets: dict[str, list[str]] = {}
    for alias in alias_map:
        buckets.setdefault(alias[0], []).append(alias)
    for bucket in buckets.values():
        bucket.sort(key=len, reverse=True)

    results: list[tuple[str, str, int, int]] = []
    start = 0
    length = len(squashed)
    while start < length:
        found: str | None = None
        for alias in buckets.get(squashed[start], ()):
            if squashed.startswith(alias, start):
                found = alias
                break
        if found is None:
            start += 1
            continue
        end = start + len(found)
        results.append(
            (
                found,
                alias_map[found],
                index_map[start],
                index_map[end - 1] + 1,
            )
        )
        # 跳到本次命中的末尾 —— 重叠的命中只算第一个（左most-longest）。
        start = end
    return results


def find_locations(text: str, *, table: LabelTable | None = None) -> list[LocationMatch]:
    """在一段文本里找出**全部**部位术语并归一（按出现顺序）。

    单数版 :func:`find_location` 只给第一处 —— 多病灶口径（任务 20）要的是
    「这一段里说了几个部位」，一处一个标签。
    """

    active = _table(table)
    normalized_text = unicodedata.normalize("NFKC", text)
    matches: list[LocationMatch] = []
    for alias, label, start, end in _scan_all(text, active.location_aliases):
        segment, segment_code = active.location_segments.get(alias, ("", ""))
        side, lobe = _split_canonical(label)
        matches.append(
            LocationMatch(
                canonical=label,
                matched_alias=alias,
                matched_text=normalized_text[start:end],
                start=start,
                end=end,
                segment=segment,
                segment_code=segment_code,
                side=side,
                lobe=lobe,
            )
        )
    return matches


def resolve_density(value: str, *, table: LabelTable | None = None) -> DensityMatch | None:
    """把一个**密度术语**归一成标准标签；认不出来返回 ``None``。"""

    key = normalize_key(value)
    if not key:
        return None

    label = _table(table).density_aliases.get(key)
    if label is None:
        return None
    return DensityMatch(canonical=label, matched_alias=key)


def normalize_density(value: str, *, table: LabelTable | None = None) -> str | None:
    """密度术语 → 标准标签；认不出来返回 ``None``。"""

    match = resolve_density(value, table=table)
    return match.canonical if match else None


def normalize_densities(
    values: Iterable[str],
    *,
    table: LabelTable | None = None,
) -> str | None:
    """多个写法 → 同一个密度标准标签（规则同 :func:`normalize_locations`）。"""

    items = [item for item in values]
    if not items:
        return None

    labels: set[str] = set()
    for item in items:
        label = normalize_density(item, table=table)
        if label is None:
            return None
        labels.add(label)

    if len(labels) != 1:
        return None
    return labels.pop()


def find_density(text: str, *, table: LabelTable | None = None) -> DensityMatch | None:
    """在一段文本里找出密度术语并归一；找不到返回 ``None``。"""

    active = _table(table)
    hit = _scan(text, active.density_aliases)
    if hit is None:
        return None

    alias, label, start, end = hit
    normalized_text = unicodedata.normalize("NFKC", text)
    return DensityMatch(
        canonical=label,
        matched_alias=alias,
        matched_text=normalized_text[start:end],
        start=start,
        end=end,
    )


def find_densities(text: str, *, table: LabelTable | None = None) -> list[DensityMatch]:
    """在一段文本里找出**全部**密度术语并归一（按出现顺序）。

    「双肺见多发结节及钙化灶」里有两处密度描述 —— 只给第一处的话，
    多病灶口径（任务 20）会把第二处的密度算丢。
    """

    active = _table(table)
    normalized_text = unicodedata.normalize("NFKC", text)
    matches: list[DensityMatch] = []
    for alias, label, start, end in _scan_all(text, active.density_aliases):
        matches.append(
            DensityMatch(
                canonical=label,
                matched_alias=alias,
                matched_text=normalized_text[start:end],
                start=start,
                end=end,
            )
        )
    return matches


def canonical_locations(*, table: LabelTable | None = None) -> tuple[str, ...]:
    """部位标准标签清单。"""

    return _table(table).canonical_locations


def canonical_densities(*, table: LabelTable | None = None) -> tuple[str, ...]:
    """密度标准标签清单。"""

    return _table(table).canonical_densities


def aliases_for(
    canonical: str,
    *,
    kind: str = "location",
    table: LabelTable | None = None,
) -> tuple[str, ...]:
    """列出归到某个标准标签的全部写法（给放射科看「我们认哪些写法」）。"""

    active = _table(table)
    if kind == "density":
        alias_map = active.density_aliases
    elif kind == "location":
        alias_map = active.location_aliases
    else:
        raise ValueError(f"未知的别名类别：{kind}")

    return tuple(
        sorted(alias for alias, label in alias_map.items() if label == canonical)
    )


def describe_tables(*, table: LabelTable | None = None) -> dict[str, Any]:
    """等价表的概览（标准标签清单、别名条数、数据文件路径）。"""

    return _table(table).describe()


def _main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：``python -m nodule_eval.goldstd.labels`` 看我们认哪些写法。"""

    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="python -m nodule_eval.goldstd.labels",
        description="部位与密度等价表：列出标准标签与本产品认得的写法",
    )
    parser.add_argument(
        "--kind",
        choices=("location", "density", "all"),
        default="all",
        help="看哪一类等价表（默认 all）",
    )
    parser.add_argument(
        "--canonical",
        default="",
        help="只看某个标准标签下认得的写法（如 右肺上叶）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="按 JSON 输出",
    )
    args = parser.parse_args(argv)

    overview = describe_tables()
    payload: dict[str, Any] = {"overview": overview, "items": []}

    kinds = ("location", "density") if args.kind == "all" else (args.kind,)
    for kind in kinds:
        canonicals = (
            canonical_locations() if kind == "location" else canonical_densities()
        )
        for label in canonicals:
            if args.canonical and label != args.canonical:
                continue
            payload["items"].append(
                {
                    "kind": kind,
                    "canonical": label,
                    "aliases": list(aliases_for(label, kind=kind)),
                }
            )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"等价表数据文件：{overview['source_path']}")
    print(
        "部位：标准标签 {n} 个，别名 {a} 条（其中肺段写法 {s} 条）".format(
            n=len(overview["locations"]["canonical"]),
            a=overview["locations"]["alias_count"],
            s=overview["locations"]["segment_alias_count"],
        )
    )
    print(
        "密度：标准标签 {n} 个，别名 {a} 条".format(
            n=len(overview["densities"]["canonical"]),
            a=overview["densities"]["alias_count"],
        )
    )
    for item in payload["items"]:
        print(f"\n[{item['kind']}] {item['canonical']}")
        for alias in item["aliases"]:
            print(f"  - {alias}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(_main())
