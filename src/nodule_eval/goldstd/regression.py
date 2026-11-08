"""否定误召 / 漏召回归集（任务 20）。

**这一层是规则基线的「不许悄悄漂」的护栏**。

否定作用域是最容易在改动中悄悄漂掉的一层：改一个词表、改一处切句规则，
敏感度与特异度就跟着动 —— 而报告上看不出来是口径变了还是模型变了。
所以每条真实写法都把**期望标签钉死**在一份数据文件里
（``regression.yaml``），规则一改就跑一遍回归。

**两类错误，两边都直接扭曲指标**：

- **否定误召**：阴性被判成阳性 —— 拉低**特异度**（真阴性被算成误报）；
- **否定漏召**：阳性被判成阴性 —— 拉低**敏感度**（真阳性被算成漏诊）。

数据文件与代码分开（同 ``labels.yaml`` 的做法）：加一条真实写法只改数据，
不动判定逻辑 —— 加回归用例不该动判定逻辑。

**这份回归集也给任务 25 用**：规则基线 vs 语义归并的对照实验，
拿的就是这批标注（同一个 ``expect`` 结构，两边跑一遍比逐字段差异）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from ..errors import ErrorCode, NoduleEvalError

__all__ = [
    "DEFAULT_REGRESSION_PATH",
    "RegressionCase",
    "RegressionSet",
    "load_regression_set",
    "regression_cases",
]


#: 回归集数据文件（与本模块同目录 —— 数据跟着代码走）。
DEFAULT_REGRESSION_PATH = Path(__file__).resolve().parent / "regression.yaml"

#: 期望标签里允许出现的字段。
_EXPECT_KEYS: tuple[str, ...] = (
    "presence",
    "location",
    "size_mm",
    "density",
    "lesions",
    "needs_human_review",
)


def _contract_invalid(detail: str) -> NoduleEvalError:
    return NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail=detail)


@dataclass(frozen=True)
class RegressionCase:
    """一条回归用例：真实写法 + 期望标签。

    ``expect``
        期望的归并结论（``presence`` / ``location`` / ``size_mm`` /
        ``density`` / ``lesions`` / ``needs_human_review``）。
    ``lesion_expect``
        期望的**逐处病灶**标签（多病灶口径）；没写就不校验。
    ``unknown_density_terms`` / ``unknown_location_terms``
        期望记进台账的「认不出来的原词」。
    """

    id: str
    category: str
    note: str
    text: str
    expect: Mapping[str, Any]
    lesion_expect: tuple[Mapping[str, Any], ...] = ()
    unknown_density_terms: tuple[str, ...] = ()
    unknown_location_terms: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "note": self.note,
            "text": self.text,
            "expect": dict(self.expect),
            "lesion_expect": [dict(item) for item in self.lesion_expect],
            "unknown_density_terms": list(self.unknown_density_terms),
            "unknown_location_terms": list(self.unknown_location_terms),
        }


@dataclass(frozen=True)
class RegressionSet:
    """一整份回归集。"""

    version: int
    description: str
    cases: tuple[RegressionCase, ...]
    source_path: str

    def by_category(self) -> dict[str, tuple[RegressionCase, ...]]:
        """按类别分组（跑回归时按类看覆盖率）。"""

        grouped: dict[str, list[RegressionCase]] = {}
        for case in self.cases:
            grouped.setdefault(case.category, []).append(case)
        return {key: tuple(value) for key, value in grouped.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "description": self.description,
            "source_path": self.source_path,
            "cases": [case.to_dict() for case in self.cases],
        }


def _require_mapping(value: Any, detail: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _contract_invalid(detail)
    return value


def _parse_case(raw: Any, index: int, source_path: str) -> RegressionCase:
    """解析一条用例 —— 字段缺了就报错（不静默取默认值）。"""

    payload = _require_mapping(raw, f"回归集 {source_path} 第 {index} 条须为映射结构")

    case_id = str(payload.get("id", "")).strip()
    if not case_id:
        raise _contract_invalid(f"回归集 {source_path} 第 {index} 条缺 id")

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise _contract_invalid(f"回归集 {source_path} 的用例 {case_id} 缺 text")

    expect = _require_mapping(
        payload.get("expect"), f"回归集 {source_path} 的用例 {case_id} 缺 expect"
    )
    unknown_keys = [key for key in expect if key not in _EXPECT_KEYS]
    if unknown_keys:
        raise _contract_invalid(
            f"回归集 {source_path} 的用例 {case_id} 的 expect 含未知字段："
            f"{'、'.join(sorted(unknown_keys))}"
        )
    missing_keys = [key for key in _EXPECT_KEYS if key not in expect]
    if missing_keys:
        raise _contract_invalid(
            f"回归集 {source_path} 的用例 {case_id} 的 expect 缺字段："
            f"{'、'.join(missing_keys)}"
        )

    lesion_expect_raw = payload.get("lesion_expect") or []
    if not isinstance(lesion_expect_raw, Sequence) or isinstance(lesion_expect_raw, str):
        raise _contract_invalid(
            f"回归集 {source_path} 的用例 {case_id} 的 lesion_expect 须为列表"
        )

    return RegressionCase(
        id=case_id,
        category=str(payload.get("category", "")).strip(),
        note=str(payload.get("note", "")).strip(),
        text=text,
        expect=dict(expect),
        lesion_expect=tuple(
            _require_mapping(item, f"回归集 {source_path} 的用例 {case_id} 的 lesion_expect 项")
            for item in lesion_expect_raw
        ),
        unknown_density_terms=tuple(payload.get("unknown_density_terms") or ()),
        unknown_location_terms=tuple(payload.get("unknown_location_terms") or ()),
    )


def load_regression_set(path: Path | str | None = None) -> RegressionSet:
    """载入回归集。

    - 文件不存在 / 解析失败 / 字段缺失 → 抛 ``CONTRACT_INVALID``，
      **不回落到任何内置兜底集**（兜底集只会让回归看起来是绿的）。
    """

    source = Path(path) if path is not None else DEFAULT_REGRESSION_PATH
    if not source.is_file():
        raise _contract_invalid(f"否定回归集文件不存在：{source}")

    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise _contract_invalid(f"否定回归集 {source} 解析失败：{exc}") from exc
    except OSError as exc:
        raise _contract_invalid(f"否定回归集 {source} 读取失败：{exc}") from exc

    payload = _require_mapping(raw, f"否定回归集 {source} 顶层须为映射结构")

    cases_raw = payload.get("cases")
    if not isinstance(cases_raw, Sequence) or isinstance(cases_raw, str) or not cases_raw:
        raise _contract_invalid(f"否定回归集 {source} 缺 cases 或 cases 为空")

    cases = tuple(
        _parse_case(item, index, str(source)) for index, item in enumerate(cases_raw, start=1)
    )

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise _contract_invalid(f"否定回归集 {source} 里的用例 id 重复：{case.id}")
        seen.add(case.id)

    return RegressionSet(
        version=int(payload.get("version", 1)),
        description=str(payload.get("description", "")).strip(),
        cases=cases,
        source_path=str(source),
    )


def regression_cases(path: Path | str | None = None) -> tuple[RegressionCase, ...]:
    """便捷入口：只取用例列表。"""

    return load_regression_set(path).cases
