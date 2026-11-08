"""对照实验的**裁判集**与**打分口径**（任务 25）。

**这个模块回答一个问题**：规则基线归并与模型归并，**在同一批人工标注的写法上**
各能读对多少 —— 也就是「模型这一层到底值不值得加」。

**为什么要单独落一份标注集**：口头论证「模型更聪明」没人信。要让人信，
得有两样东西：

1. **同一批写法**（:mod:`nodule_eval.goldstd.benchmark` 的数据文件）——
   规则与模型读的是**同一段原文**，逐字段可比；
2. **人工标注的裁判**（每条写法的标准标签）—— 谁对谁错由**标注**说了算，
   不是由某个归并器说了算。

**打分口径（四类结局，不含糊）**：

===============  ==========================================  ==================
结局            含义                                        临床含义
===============  ==========================================  ==================
``correct``     读出来且与标注一致                          ——
``missed``      原文写了，归并器**没读出来**（留空）          **漏召** —— 拉低敏感度
``over_call``   原文没写，归并器**给了一个值**              **误召** —— 拉低特异度
``wrong``       两边都有值但**不相等**                      错值 —— 拉低两项
===============  ==========================================  ==================

存在性另有一档「**读不出**」（``unreadable``）：原文两句话都没表态，
或归并器交白卷。它**既不是阴性也不是阳性** —— 单列计数，
不进「阴性误召」也不进「阳性漏召」（任务 24 的降级口径同源：
「没跑成」不等于「没看见」）。

**尺寸容差**：``0.05`` 毫米（与 :mod:`nodule_eval.goldstd.merge` 的回指复核同值）。

**这份集是分层构建的，不是随机抽样** —— 每一类都是「这一类写法」，
所以准确率**不代表真实世界的写法分布**，只用于**层与层之间的对照**
（同一批写法上，规则比模型多读对 / 少读对多少）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from ..errors import ErrorCode, NoduleEvalError
from .schema import GoldLabels

__all__ = [
    "BENCHMARK_FIELDS",
    "BenchmarkCase",
    "BenchmarkSet",
    "CategoryScore",
    "ERROR_TYPE_LABELS",
    "FieldOutcome",
    "FieldScore",
    "LESION_FIELD",
    "NormalizerScore",
    "OVERALL_FIELDS",
    "SIZE_TOLERANCE_MM",
    "benchmark_cases",
    "compare_labels",
    "load_benchmark_set",
    "score_normalizer",
]


#: 裁判集数据文件（与本模块同目录 —— 数据跟着代码走）。
DEFAULT_BENCHMARK_PATH = Path(__file__).resolve().parent / "benchmark.yaml"

#: **逐字段比对**的字段。
BENCHMARK_FIELDS: tuple[str, ...] = ("presence", "location", "size_mm", "density")

#: 病灶处数也算一项 —— 多病灶口径（任务 20）单独看得见。
LESION_FIELD = "lesions"

#: **计入合计**的标签字段（病灶处数不算进合计，理由见
#: :attr:`NormalizerScore.overall_accuracy`）。
OVERALL_FIELDS: tuple[str, ...] = ("presence", "location", "size_mm", "density")

#: 尺寸比对容差（毫米）。
SIZE_TOLERANCE_MM = 0.05

#: 期望标签里允许出现的字段。
_EXPECT_KEYS: tuple[str, ...] = (
    "presence",
    "location",
    "size_mm",
    "density",
    "lesions",
    "needs_human_review",
)

#: 字段 → 错误码前缀（``size_mm`` 的错误码用 ``size``，短一点好读）。
_ERROR_PREFIX: dict[str, str] = {
    "presence": "presence",
    "location": "location",
    "size_mm": "size",
    "density": "density",
    "lesions": "lesion_count",
}

#: 错误类型 → 中文标签（输出表与结论摘要共用这一份，不各写一套）。
ERROR_TYPE_LABELS: dict[str, str] = {
    "presence_false_negative": "存在性 · 阳性被判成阴性（漏召）",
    "presence_unreadable": "存在性 · 读不出来（交白卷）",
    "presence_false_positive": "存在性 · 阴性被判成阳性（误召）",
    "location_missed": "部位 · 该读出来的没读出来（漏召）",
    "location_over_call": "部位 · 原文没写却给了值（误召）",
    "location_wrong": "部位 · 值不相等（错值）",
    "size_missed": "尺寸 · 该读出来的没读出来（漏召）",
    "size_over_call": "尺寸 · 原文没写却给了值（误召）",
    "size_wrong": "尺寸 · 值不相等（错值）",
    "density_missed": "密度 · 该读出来的没读出来（漏召）",
    "density_over_call": "密度 · 原文没写却给了值（误召）",
    "density_wrong": "密度 · 值不相等（错值）",
    "lesion_count_mismatch": "病灶处数 · 与标注不符",
}


def _contract_invalid(detail: str) -> NoduleEvalError:
    return NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail=detail)


@dataclass(frozen=True)
class BenchmarkCase:
    """一条裁判用例：真实写法 + 人工标注的标准标签。

    ``expect``
        人工标注的结论（``presence`` / ``location`` / ``size_mm`` /
        ``density`` / ``lesions`` / ``needs_human_review``）。
        属性为 ``None`` 表示**原文没写这一项** —— 与「归并器没读出来」
        是两件事，正是对照实验要分开看的。
    """

    id: str
    category: str
    note: str
    text: str
    expect: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "note": self.note,
            "text": self.text,
            "expect": dict(self.expect),
        }


@dataclass(frozen=True)
class BenchmarkSet:
    """一整份裁判集。

    ``sha256``
        数据文件的内容哈希 —— 写进对照表文件头，
        「这轮对照跑的是哪一版标注集」永远查得清。
    """

    version: int
    description: str
    cases: tuple[BenchmarkCase, ...]
    source_path: str
    sha256: str

    def by_category(self) -> dict[str, tuple[BenchmarkCase, ...]]:
        """按类别分组（按类别看准确率 —— 失效集中在哪一类）。"""

        grouped: dict[str, list[BenchmarkCase]] = {}
        for case in self.cases:
            grouped.setdefault(case.category, []).append(case)
        return {key: tuple(value) for key, value in grouped.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "description": self.description,
            "case_count": len(self.cases),
            "source_path": self.source_path,
            "sha256": self.sha256,
            "cases": [case.to_dict() for case in self.cases],
        }


@dataclass(frozen=True)
class FieldOutcome:
    """一个字段在一例上的比对结局。"""

    case_id: str
    category: str
    field: str
    expected: Any
    actual: Any
    status: str
    error_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
            "status": self.status,
            "error_type": self.error_type,
        }


@dataclass(frozen=True)
class FieldScore:
    """一个字段在整份裁判集上的成绩。

    ``scored``
        **期望有值**的例数（存在性字段是「标注为阳性」的例数）—— ``recall`` 的分母。
    ``unscored``
        **期望无值**（原文没写 / 标注为阴性）的例数 —— ``false_call_rate`` 的分母。
    ``correct_scored`` / ``correct_unscored``
        两个池子各自判对的例数。**必须分开记** —— 混在一起算，
        「两边都没读出来」会被算成一次「读对了」，召回率就会超过 100%。
    """

    field: str
    total: int = 0
    scored: int = 0
    unscored: int = 0
    correct_scored: int = 0
    correct_unscored: int = 0
    missed: int = 0
    over_call: int = 0
    wrong: int = 0
    unreadable: int = 0

    @property
    def correct(self) -> int:
        return self.correct_scored + self.correct_unscored

    @property
    def accuracy(self) -> float:
        return _ratio(self.correct, self.total)

    @property
    def recall(self) -> float:
        """期望有值的例里读对的比例（漏召直接扣在这里）。"""

        return _ratio(self.correct_scored, self.scored)

    @property
    def missed_rate(self) -> float:
        return _ratio(self.missed, self.scored)

    @property
    def false_call_rate(self) -> float:
        """期望无值的例里被错给了值的比例（误召）。"""

        return _ratio(self.over_call, self.unscored)

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "total": self.total,
            "scored": self.scored,
            "unscored": self.unscored,
            "correct": self.correct,
            "correct_scored": self.correct_scored,
            "correct_unscored": self.correct_unscored,
            "missed": self.missed,
            "over_call": self.over_call,
            "wrong": self.wrong,
            "unreadable": self.unreadable,
            "accuracy": round(self.accuracy, 4),
            "recall": round(self.recall, 4),
            "missed_rate": round(self.missed_rate, 4),
            "false_call_rate": round(self.false_call_rate, 4),
        }


@dataclass(frozen=True)
class CategoryScore:
    """一个**场景类别**上的成绩 —— 失效集中在哪一类，看这张。"""

    category: str
    cases: int = 0
    fields_scored: int = 0
    correct: int = 0

    @property
    def accuracy(self) -> float:
        return _ratio(self.correct, self.fields_scored)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "cases": self.cases,
            "fields_scored": self.fields_scored,
            "correct": self.correct,
            "accuracy": round(self.accuracy, 4),
        }


@dataclass(frozen=True)
class NormalizerScore:
    """一个归并器在整份裁判集上的总成绩。"""

    name: str
    total_cases: int = 0
    fields: Mapping[str, FieldScore] = ()
    error_types: Mapping[str, int] = ()
    categories: Mapping[str, CategoryScore] = ()
    degraded_cases: int = 0

    @property
    def overall_accuracy(self) -> float:
        """**逐字段**准确率的宏平均（每个标签字段等权）。

        **病灶处数不算进合计** —— 它不是标签字段，而是「一处病灶一条标签」
        这条口径的落地情况，单独一行看（模型归并当前只落一条，多病灶口径
        要靠任务 27 / 28 的最严比对补齐 —— 那是**契约**的缺口，不是模型的缺口）。
        """

        values = [self.fields[name].accuracy for name in OVERALL_FIELDS if name in self.fields]
        if not values:
            return 0.0
        return sum(values) / len(values)

    @property
    def overall_recall(self) -> float:
        values = [self.fields[name].recall for name in OVERALL_FIELDS if name in self.fields]
        if not values:
            return 0.0
        return sum(values) / len(values)

    @property
    def overall_false_call_rate(self) -> float:
        values = [
            self.fields[name].false_call_rate for name in OVERALL_FIELDS if name in self.fields
        ]
        if not values:
            return 0.0
        return sum(values) / len(values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total_cases": self.total_cases,
            "overall_accuracy": round(self.overall_accuracy, 4),
            "overall_recall": round(self.overall_recall, 4),
            "overall_false_call_rate": round(self.overall_false_call_rate, 4),
            "fields": {key: value.to_dict() for key, value in self.fields.items()},
            "error_types": {
                key: self.error_types[key] for key in sorted(self.error_types)
            },
            "categories": {key: self.categories[key].to_dict() for key in sorted(self.categories)},
            "degraded_cases": self.degraded_cases,
        }


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


# --------------------------------------------------------------------------
# 载入
# --------------------------------------------------------------------------


def _require_mapping(value: Any, detail: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _contract_invalid(detail)
    return value


def _parse_case(raw: Any, index: int, source_path: str) -> BenchmarkCase:
    payload = _require_mapping(raw, f"裁判集 {source_path} 第 {index} 条须为映射结构")

    case_id = str(payload.get("id", "")).strip()
    if not case_id:
        raise _contract_invalid(f"裁判集 {source_path} 第 {index} 条缺 id")

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise _contract_invalid(f"裁判集 {source_path} 的用例 {case_id} 缺 text")

    expect = _require_mapping(
        payload.get("expect"), f"裁判集 {source_path} 的用例 {case_id} 缺 expect"
    )
    unknown_keys = [key for key in expect if key not in _EXPECT_KEYS]
    if unknown_keys:
        raise _contract_invalid(
            f"裁判集 {source_path} 的用例 {case_id} 的 expect 含未知字段："
            f"{'、'.join(sorted(unknown_keys))}"
        )
    missing_keys = [key for key in _EXPECT_KEYS if key not in expect]
    if missing_keys:
        raise _contract_invalid(
            f"裁判集 {source_path} 的用例 {case_id} 的 expect 缺字段："
            f"{'、'.join(missing_keys)}"
        )

    presence = expect.get("presence")
    if presence is not None and not isinstance(presence, bool):
        raise _contract_invalid(
            f"裁判集 {source_path} 的用例 {case_id} 的 presence 须是布尔或 null"
        )
    lesions = expect.get("lesions")
    if not isinstance(lesions, int) or isinstance(lesions, bool) or lesions < 0:
        raise _contract_invalid(
            f"裁判集 {source_path} 的用例 {case_id} 的 lesions 须是非负整数"
        )
    size = expect.get("size_mm")
    if size is not None and not isinstance(size, (int, float)):
        raise _contract_invalid(
            f"裁判集 {source_path} 的用例 {case_id} 的 size_mm 须是数值或 null"
        )

    return BenchmarkCase(
        id=case_id,
        category=str(payload.get("category", "")).strip(),
        note=str(payload.get("note", "")).strip(),
        text=text,
        expect=dict(expect),
    )


def load_benchmark_set(path: Path | str | None = None) -> BenchmarkSet:
    """载入裁判集。

    - 文件不存在 / 解析失败 / 字段缺失 → 抛 ``CONTRACT_INVALID``，
      **不回落到任何内置兜底集**（兜底集会让对照表看起来是绿的）。
    """

    source = Path(path) if path is not None else DEFAULT_BENCHMARK_PATH
    if not source.is_file():
        raise _contract_invalid(f"归并对照裁判集文件不存在：{source}")

    try:
        raw_bytes = source.read_bytes()
    except OSError as exc:
        raise _contract_invalid(f"归并对照裁判集 {source} 读取失败：{exc}") from exc

    try:
        payload = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise _contract_invalid(f"归并对照裁判集 {source} 解析失败：{exc}") from exc

    payload = _require_mapping(payload, f"归并对照裁判集 {source} 顶层须为映射结构")

    cases_raw = payload.get("cases")
    if not isinstance(cases_raw, Sequence) or isinstance(cases_raw, str) or not cases_raw:
        raise _contract_invalid(f"归并对照裁判集 {source} 缺 cases 或 cases 为空")

    cases = tuple(
        _parse_case(item, index, str(source)) for index, item in enumerate(cases_raw, start=1)
    )

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise _contract_invalid(f"归并对照裁判集 {source} 里的用例 id 重复：{case.id}")
        seen.add(case.id)

    return BenchmarkSet(
        version=int(payload.get("version", 1)),
        description=str(payload.get("description", "")).strip(),
        cases=cases,
        source_path=str(source),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def benchmark_cases(path: Path | str | None = None) -> tuple[BenchmarkCase, ...]:
    """便捷入口：只取用例列表。"""

    return load_benchmark_set(path).cases


# --------------------------------------------------------------------------
# 逐例比对
# --------------------------------------------------------------------------


def _same_size(expected: Any, actual: Any) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    try:
        return abs(float(expected) - float(actual)) <= SIZE_TOLERANCE_MM
    except (TypeError, ValueError):
        return False


def compare_labels(case: BenchmarkCase, labels: GoldLabels) -> list[FieldOutcome]:
    """把一个归并器的结论与人工标注**逐字段**比对。

    返回**五个**结局（四个属性字段 + 病灶处数），顺序固定：
    ``presence`` → ``location`` → ``size_mm`` → ``density`` → ``lesions``。
    """

    expect = case.expect
    outcomes: list[FieldOutcome] = []

    # ---- 存在性 ----
    expected_presence = expect.get("presence")
    actual_presence = labels.presence
    if expected_presence is None:
        # 标注本身没表态（原文两句话都没说）：归并器给了结论就算「多给了」。
        status = "correct" if actual_presence is None else "over_call"
        error_type = "" if status == "correct" else "presence_false_positive"
    elif actual_presence is None:
        status = "missed"
        error_type = "presence_unreadable"
    elif bool(actual_presence) == bool(expected_presence):
        status = "correct"
        error_type = ""
    elif bool(expected_presence) and not bool(actual_presence):
        status = "missed"
        error_type = "presence_false_negative"
    else:
        status = "over_call"
        error_type = "presence_false_positive"
    outcomes.append(
        FieldOutcome(
            case_id=case.id,
            category=case.category,
            field="presence",
            expected=expected_presence,
            actual=actual_presence,
            status=status,
            error_type=error_type,
        )
    )

    # ---- 三个属性字段 ----
    for field, actual in (
        ("location", labels.location),
        ("size_mm", labels.size_mm),
        ("density", labels.density),
    ):
        prefix = _ERROR_PREFIX[field]
        expected = expect.get(field)
        if expected is None and actual is None:
            status, error_type = "correct", ""
        elif expected is None:
            status, error_type = "over_call", f"{prefix}_over_call"
        elif actual is None:
            status, error_type = "missed", f"{prefix}_missed"
        elif field == "size_mm":
            if _same_size(expected, actual):
                status, error_type = "correct", ""
            else:
                status, error_type = "wrong", f"{prefix}_wrong"
        elif expected == actual:
            status, error_type = "correct", ""
        else:
            status, error_type = "wrong", f"{prefix}_wrong"
        outcomes.append(
            FieldOutcome(
                case_id=case.id,
                category=case.category,
                field=field,
                expected=expected,
                actual=actual,
                status=status,
                error_type=error_type,
            )
        )

    # ---- 病灶处数 ----
    expected_lesions = int(expect.get("lesions", 0) or 0)
    actual_lesions = len(labels.lesions)
    if expected_lesions == actual_lesions:
        status, error_type = "correct", ""
    else:
        status, error_type = "wrong", "lesion_count_mismatch"
    outcomes.append(
        FieldOutcome(
            case_id=case.id,
            category=case.category,
            field=LESION_FIELD,
            expected=expected_lesions,
            actual=actual_lesions,
            status=status,
            error_type=error_type,
        )
    )

    return outcomes


# --------------------------------------------------------------------------
# 汇总打分
# --------------------------------------------------------------------------


def score_normalizer(
    name: str,
    cases: Sequence[BenchmarkCase],
    results: Mapping[str, GoldLabels],
) -> NormalizerScore:
    """给一个归并器在整份裁判集上打分。

    ``results``
        ``{case_id: 归并结论}``。缺一例就**当漏召**（不是跳过）——
        跳过会让「归并器没跑这一例」在表上消失。
    """

    field_scores: dict[str, FieldScore] = {}
    error_types: dict[str, int] = {}
    category_totals: dict[str, list[int]] = {}
    degraded = 0
    #: 每个字段的累加器（frozen dataclass 只在最后组装）。
    counters: dict[str, dict[str, int]] = {}

    for case in cases:
        labels = results.get(case.id)
        if labels is None:
            # 没跑出结论 —— 全部字段按「读不出来」记。
            labels = GoldLabels()
        if getattr(labels, "degraded", False):
            degraded += 1

        outcomes = compare_labels(case, labels)
        for outcome in outcomes:
            bucket = counters.setdefault(
                outcome.field,
                {
                    "total": 0,
                    "scored": 0,
                    "unscored": 0,
                    "correct_scored": 0,
                    "correct_unscored": 0,
                    "missed": 0,
                    "over_call": 0,
                    "wrong": 0,
                    "unreadable": 0,
                },
            )

            bucket["total"] += 1
            expected_value = outcome.expected
            if outcome.field == "presence":
                in_scored = expected_value is True
            else:
                in_scored = expected_value is not None
            if in_scored:
                bucket["scored"] += 1
            else:
                bucket["unscored"] += 1

            if outcome.status == "correct":
                if in_scored:
                    bucket["correct_scored"] += 1
                else:
                    bucket["correct_unscored"] += 1
            elif outcome.status == "missed":
                bucket["missed"] += 1
            elif outcome.status == "over_call":
                bucket["over_call"] += 1
            else:
                bucket["wrong"] += 1

            if outcome.actual is None and outcome.field == "presence":
                bucket["unreadable"] += 1

            if outcome.error_type:
                error_types[outcome.error_type] = error_types.get(outcome.error_type, 0) + 1

            totals = category_totals.setdefault(outcome.category, [0, 0])
            totals[1] += 1
            if outcome.status == "correct":
                totals[0] += 1

    for field_name, bucket in counters.items():
        field_scores[field_name] = FieldScore(field=field_name, **bucket)

    categories = {
        name_: CategoryScore(
            category=name_,
            cases=len({case.id for case in cases if case.category == name_}),
            fields_scored=totals[1],
            correct=totals[0],
        )
        for name_, totals in category_totals.items()
    }

    return NormalizerScore(
        name=name,
        total_cases=len(cases),
        fields=field_scores,
        error_types=error_types,
        categories=categories,
        degraded_cases=degraded,
    )
