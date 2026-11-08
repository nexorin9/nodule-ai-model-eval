#!/usr/bin/env python3
"""归并对照实验：**规则基线 vs 模型归并**（同一标注集上的准确率 / 召回 / 误召率）。

**这个脚本回答一个具体问题**：模型这一层到底值不值得加？

回答它要三样东西，缺一样都不算数：

1. **同一批写法** —— 规则与模型读的是同一段金标准原文；
2. **人工标注的裁判** —— ``src/nodule_eval/goldstd/benchmark.yaml``（66 条，
   覆盖否定作用域 / 多病灶 / 跨行 / 量纲混写 / 别名变体 / 术语外写法）；
3. **逐字段的可复现对照表** —— 准确率 / 召回 / 误召率 / 各错误类型分布，
   写进 CSV 与 JSON，**文件头记下标注集版本号、内容哈希、随机源与模型参数**。

**模型侧的三种跑法**（``--model-mode``）：

============  ======================================================  ================
模式          跑法                                                      用途
============  ======================================================  ================
``gateway``   **默认**。真 OpenAI-compatible 端点（``OPENAI_BASE_URL``）  **生产路径** ——
              —— 真 socket、真 ``requests.Session``、真 Bearer 头              院内模型端点
``replay``    回放一份**录下来的**模型输出（JSONL），不发任何请求        复盘 / 复核
``double``    本地协议替身（**只在测试层**）按请求作答                   **离线核验**
============  ======================================================  ================

**关于 ``double`` 模式的重要声明**：那一侧的应答逻辑是 ``tests/semantic_reader.py``
里的一段**确定性 Python 代码**，**不是大模型**。所以 ``double`` 模式给出的
「模型侧」数字**不代表真实模型的能力** —— 它代表「一个比规则基线宽一点的确定性
读法，在同一套回指校验与标签复核约束下能拿到什么」。文件头与控制台都会写明这一点。
院内模型端点可用后，用同一条命令（``--model-mode gateway``）重跑，
拿到的才是可对外汇报的数字。

用法::

    # 默认：打院内真实模型端点（凭据从环境变量读）
    OPENAI_BASE_URL=... OPENAI_API_KEY=... python scripts/compare_normalizers.py

    # 回放一份录下来的模型输出（逐字节可复现）
    python scripts/compare_normalizers.py --model-mode replay --model-log model-log.jsonl

    # 离线核验（本地协议替身；替身只在 tests/ 层）
    python scripts/compare_normalizers.py --model-mode double

产物（默认落在 ``reports/normalizer-compare/``）：

- ``compare-normalizers.csv`` —— 逐例逐字段的对照明细（复核的人一眼看到差在哪）；
- ``compare-summary.json`` —— 汇总数字 + 文件头（**不含时间戳**，重复跑逐字节一致）；
- ``compare-run-meta.json`` —— 运行元信息（命令行、时间戳、端点地址）。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
TESTS_DIR = PROJECT_ROOT / "tests"
for _path in (str(SRC_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from nodule_eval.config import load_config  # noqa: E402
from nodule_eval.errors import ErrorCode, NoduleEvalError  # noqa: E402
from nodule_eval.gateway import ModelGateway  # noqa: E402
from nodule_eval.goldstd.benchmark import (  # noqa: E402
    ERROR_TYPE_LABELS,
    BenchmarkCase,
    BenchmarkSet,
    compare_labels,
    load_benchmark_set,
    score_normalizer,
)
from nodule_eval.goldstd.merge import build_merge_prompt, merge_by_model  # noqa: E402
from nodule_eval.goldstd.rules import merge_by_rules  # noqa: E402
from nodule_eval.goldstd.schema import GoldLabels  # noqa: E402

#: 默认随机源 —— 抽样与任何随机成分都用它，**写进文件头**。
DEFAULT_SEED = 20260907

#: 默认产物目录。
DEFAULT_OUT_DIR = PROJECT_ROOT / "reports" / "normalizer-compare"

#: 本实验**用不到**的三个端点（RIS / 推理服务 / 科研平台）的占位值。
#:
#: 配置层要求 12 项环境变量一个不少，缺一项就指名报错。占位值让
#: 「这一项本实验一次都不会被打到」写在明处，而不是让它看起来像真配置。
_UNUSED_ENDPOINT_PLACEHOLDER = "https://unused-in-this-experiment.invalid"

#: 模型端点的占位值 —— 只在 ``double`` / ``replay`` 模式下使用
#: （那两种模式要么指向本地替身，要么根本不发请求）。
_MODEL_ENDPOINT_PLACEHOLDER: dict[str, str] = {
    "OPENAI_BASE_URL": "https://model-placeholder.invalid",
    "OPENAI_API_KEY": "placeholder-model-token",
    "OPENAI_MODEL": "nodule-goldstd-merge",
    "MODEL_TIMEOUT": "60",
    "MODEL_MAX_RETRIES": "2",
    "MODEL_TEMPERATURE": "0",
}

#: 参与哈希的源文件 —— 结论出处可追（改哪一处，摘要里看得到）。
_SOURCE_FILES: tuple[tuple[str, str], ...] = (
    ("prompt", "prompts/goldstd_merge.v1.md"),
    ("labels", "src/nodule_eval/goldstd/labels.yaml"),
    ("rules", "src/nodule_eval/goldstd/rules.py"),
    ("schema", "src/nodule_eval/goldstd/schema.py"),
    ("merge", "src/nodule_eval/goldstd/merge.py"),
)

#: 对照明细表的列（**顺序固定** —— 复核的人按列看）。
CSV_COLUMNS: tuple[str, ...] = (
    "case_id",
    "category",
    "normalizer",
    "field",
    "expected",
    "actual",
    "status",
    "error_type",
    "error_label",
    "text",
)


# --------------------------------------------------------------------------
# 模型侧：三种跑法
# --------------------------------------------------------------------------


class ReplayGateway:
    """回放**录下来的**模型输出（不发任何请求）。

    回放日志是 JSONL，每行 ``{"case_id": ..., "text": ..., "raw": ...}``；
    按**原文**精确匹配。日志里没有的原文 → 抛 ``CONTRACT_INVALID``
    并指名缺哪一条 —— 那一例会走规则降级，**不会**悄悄用别的答案顶上。
    """

    def __init__(self, path: Path | str) -> None:
        source = Path(path)
        if not source.is_file():
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"模型回放日志不存在：{source}",
            )
        self.source_path = str(source)
        self._by_text: dict[str, str] = {}
        self._by_id: dict[str, str] = {}
        for line_no, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise NoduleEvalError(
                    ErrorCode.CONTRACT_INVALID,
                    detail=f"模型回放日志 {source} 第 {line_no} 行不是合法 JSON：{exc}",
                ) from exc
            text = str(record.get("text", ""))
            raw = str(record.get("raw", ""))
            if not text or not raw:
                raise NoduleEvalError(
                    ErrorCode.CONTRACT_INVALID,
                    detail=f"模型回放日志 {source} 第 {line_no} 行缺 text 或 raw",
                )
            self._by_text[text] = raw
            case_id = str(record.get("case_id", ""))
            if case_id:
                self._by_id[case_id] = raw

    def complete(self, *, system: str, user: str, json_schema: Mapping[str, Any] | None = None) -> str:
        raw = self._by_text.get(user)
        if raw is None:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=(
                    "模型回放日志里没有这一条原文"
                    f"（sha256={hashlib.sha256(user.encode('utf-8')).hexdigest()[:16]}）"
                ),
            )
        return raw

    def complete_structured(
        self,
        *,
        system: str,
        user: str,
        json_schema: Mapping[str, Any],
        required_fields: Sequence[str] | None = None,
        tool_call_id: str | None = None,
    ) -> Any:
        """结构化路径 —— 直接把录下来的原文交给业务层解析（与真网关同一条校验链）。"""

        from nodule_eval.gateway import StructuredResponse
        from nodule_eval.structured import extract_json_payload

        raw = self.complete(system=system, user=user, json_schema=json_schema)
        payload = extract_json_payload(raw)
        return StructuredResponse(
            payload=payload,
            raw=raw,
            meta=None,  # type: ignore[arg-type]  —— 回放没有真实调用元数据
            fallback_used=False,
        )


def _build_double_gateway(env: Mapping[str, str]) -> tuple[ModelGateway, Any]:
    """起一个**本地协议替身**，把真网关指过去。

    替身只在 ``tests/`` 层；产品代码（``src/``）不 import 它。
    """

    from stub_model import ModelStub
    from semantic_reader import answer_for_text, extract_user_message

    def responder(request: Mapping[str, Any]) -> str:
        text = extract_user_message(request.get("body"))
        return json.dumps(answer_for_text(text), ensure_ascii=False)

    stub = ModelStub(responder=responder)
    base_url = stub.start()
    gateway = ModelGateway(load_config(env), base_url=base_url)
    return gateway, stub


# --------------------------------------------------------------------------
# 跑两侧
# --------------------------------------------------------------------------


def _run_rule_side(cases: Sequence[BenchmarkCase]) -> dict[str, GoldLabels]:
    """规则基线侧 —— 纯本地、无网络、无随机。"""

    results: dict[str, GoldLabels] = {}
    for case in cases:
        results[case.id] = merge_by_rules(case.text).labels
    return results


def _run_model_side(
    cases: Sequence[BenchmarkCase],
    *,
    mode: str,
    env: Mapping[str, str],
    model_log: Path | str | None,
) -> tuple[dict[str, GoldLabels], list[dict[str, str]], str]:
    """模型侧 —— 返回（标签, 录下来的原文, 端点类型说明）。"""

    labels: dict[str, GoldLabels] = {}
    recorded: list[dict[str, str]] = []
    stub = None

    try:
        if mode == "replay":
            gateway: Any = ReplayGateway(model_log)  # type: ignore[assignment]
            endpoint_kind = "recorded_model_log"
        elif mode == "double":
            gateway, stub = _build_double_gateway(env)
            endpoint_kind = "local_protocol_double"
        else:
            gateway = ModelGateway(load_config(env))
            endpoint_kind = "openai_compatible_endpoint"

        prompt = build_merge_prompt()
        for case in cases:
            result = merge_by_model(case.text, gateway=gateway, prompt=prompt)
            labels[case.id] = result.labels
            recorded.append({"case_id": case.id, "text": case.text, "raw": result.raw})
    finally:
        if stub is not None:
            stub.stop()

    return labels, recorded, endpoint_kind


# --------------------------------------------------------------------------
# 产物
# --------------------------------------------------------------------------


def _sha256_of(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _source_hashes() -> dict[str, str]:
    return {name: _sha256_of(PROJECT_ROOT / rel) for name, rel in _SOURCE_FILES}


def _write_csv(
    path: Path,
    cases: Sequence[BenchmarkCase],
    rule_labels: Mapping[str, GoldLabels],
    model_labels: Mapping[str, GoldLabels],
) -> None:
    """逐例逐字段的对照明细 —— 复核的人按 ``case_id`` 一行行看差在哪。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for case in cases:
            for name, source in (("rule", rule_labels), ("model", model_labels)):
                labels = source.get(case.id) or GoldLabels()
                for outcome in compare_labels(case, labels):
                    writer.writerow(
                        {
                            "case_id": outcome.case_id,
                            "category": outcome.category,
                            "normalizer": name,
                            "field": outcome.field,
                            "expected": _fmt(outcome.expected),
                            "actual": _fmt(outcome.actual),
                            "status": outcome.status,
                            "error_type": outcome.error_type,
                            "error_label": ERROR_TYPE_LABELS.get(outcome.error_type, ""),
                            "text": case.text.replace("\n", "⏎"),
                        }
                    )


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _write_record_log(path: Path, recorded: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(dict(item), ensure_ascii=False) for item in recorded]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# --------------------------------------------------------------------------
# 结论
# --------------------------------------------------------------------------


def _build_conclusion(
    benchmark: BenchmarkSet,
    rule_score,
    model_score,
) -> dict[str, Any]:
    """从数字里**算出**结论（不写死话）—— 口径变了结论跟着变。"""

    rule_fields = rule_score.fields
    model_fields = model_score.fields

    field_delta = {
        name: round(model_fields[name].accuracy - rule_fields[name].accuracy, 4)
        for name in sorted(rule_fields)
        if name in model_fields
    }

    rule_failures = sorted(
        (
            {"error_type": key, "label": ERROR_TYPE_LABELS.get(key, key), "count": value}
            for key, value in rule_score.error_types.items()
        ),
        key=lambda item: (-item["count"], item["error_type"]),
    )

    category_delta = {}
    for name, rule_cat in rule_score.categories.items():
        model_cat = model_score.categories.get(name)
        if model_cat is None:
            continue
        category_delta[name] = {
            "rule_accuracy": round(rule_cat.accuracy, 4),
            "model_accuracy": round(model_cat.accuracy, 4),
            "delta": round(model_cat.accuracy - rule_cat.accuracy, 4),
            "cases": rule_cat.cases,
        }

    improved = [name for name, value in field_delta.items() if value > 0]
    regressed = [name for name, value in field_delta.items() if value < 0]

    return {
        "headline": (
            f"规则基线整体准确率 {rule_score.overall_accuracy:.1%} → "
            f"模型侧 {model_score.overall_accuracy:.1%}"
        ),
        "field_accuracy_delta": field_delta,
        "improved_fields": improved,
        "regressed_fields": regressed,
        "rule_failure_distribution": rule_failures,
        "category_delta": category_delta,
        "rule_baseline_still_needed": [
            "模型端点不可达时由规则基线兜底，整条主干仍能跑完（降级例单独计数）。",
            "回指校验与标签复核以规则读法为基准 —— 没有它，模型给的标签无从复核。",
            "模型输出不合契约时降级到规则基线，而不是把这一例判失败。",
        ],
    }


# --------------------------------------------------------------------------
# 控制台摘要
# --------------------------------------------------------------------------


def _print_summary(
    benchmark: BenchmarkSet,
    rule_score,
    model_score,
    *,
    model_mode: str,
    endpoint_kind: str,
    seed: int,
) -> None:
    print("=== 归并对照实验 · 规则基线 vs 模型归并 ===")
    print(
        f"裁判集    : 版本 {benchmark.version} / {len(benchmark.cases)} 条 / "
        f"sha256 {benchmark.sha256[:16]}…"
    )
    print(f"模型侧    : {model_mode}（{endpoint_kind}）")
    print(f"随机源    : seed={seed}")
    if endpoint_kind == "local_protocol_double":
        print(
            "**注意**：本轮模型侧是本地协议替身（确定性回放），**不是真实大模型** —— "
            "它的数字不代表模型能力。"
        )
    print()

    header = (
        f"{'字段':<12}{'规则准确率':>10}{'模型准确率':>10}{'差值':>8}"
        f"{'规则召回':>10}{'模型召回':>10}{'规则误召率':>12}{'模型误召率':>12}"
    )
    print(header)
    print("-" * len(header))
    for name in sorted(rule_score.fields):
        rule_field = rule_score.fields[name]
        model_field = model_score.fields.get(name)
        if model_field is None:
            continue
        delta = model_field.accuracy - rule_field.accuracy
        print(
            f"{name:<12}{rule_field.accuracy:>10.1%}{model_field.accuracy:>10.1%}"
            f"{delta:>+8.1%}{rule_field.recall:>10.1%}{model_field.recall:>10.1%}"
            f"{rule_field.false_call_rate:>12.1%}{model_field.false_call_rate:>12.1%}"
        )
    print(
        f"{'【合计】':<12}{rule_score.overall_accuracy:>10.1%}"
        f"{model_score.overall_accuracy:>10.1%}"
        f"{model_score.overall_accuracy - rule_score.overall_accuracy:>+8.1%}"
        f"{rule_score.overall_recall:>10.1%}{model_score.overall_recall:>10.1%}"
        f"{rule_score.overall_false_call_rate:>12.1%}"
        f"{model_score.overall_false_call_rate:>12.1%}"
    )

    print()
    print("按场景类别的准确率（规则 → 模型）：")
    for name in sorted(rule_score.categories):
        rule_cat = rule_score.categories[name]
        model_cat = model_score.categories.get(name)
        model_acc = model_cat.accuracy if model_cat else 0.0
        print(
            f"  {name:<14}{rule_cat.accuracy:>7.1%} → {model_acc:>7.1%}"
            f"   （{rule_cat.cases} 例）"
        )

    print()
    print("规则基线的失效类型分布（漏召 / 误召 / 错值）：")
    if not rule_score.error_types:
        print("  （无）")
    for item in sorted(
        rule_score.error_types.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        key, count = item
        print(f"  {ERROR_TYPE_LABELS.get(key, key):<34}{count:>4} 例")

    print()
    print(f"模型侧降级例（回指校验 / 标签复核未通过）：{model_score.degraded_cases} 例")
    print("产物：compare-normalizers.csv（逐例明细）/ compare-summary.json（汇总）")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _default_env(mode: str) -> dict[str, str]:
    """组装本次对照实验要用的配置。

    **本实验只调用模型端点** —— RIS / 推理服务 / 科研平台这三个端点在这个脚本里
    一次都不会被打到，所以它们用**显眼的占位值**补齐（配置层要求 12 项一个不少，
    缺任何一项就指名报错 —— 占位值让「这一项本实验用不到」写在明处）。

    **模型那六项**：
    - ``gateway`` 模式 —— **必须**来自真实环境变量，缺一项就指名报错，
      绝不悄悄改打本地替身；
    - ``double`` / ``replay`` 模式 —— 端点不是本实验的对象（一个指向本地替身，
      一个根本不发请求），用占位值并在输出里写明 ``model_endpoint_kind``。
    """

    from nodule_eval.config import REQUIRED_ENV_VARS

    env = {name: str(os.environ.get(name, "")).strip() for name in REQUIRED_ENV_VARS}

    for name in (
        "RIS_BASE_URL",
        "RIS_API_TOKEN",
        "INFERENCE_BASE_URL",
        "INFERENCE_API_TOKEN",
        "RESEARCH_PLATFORM_BASE_URL",
        "RESEARCH_PLATFORM_TOKEN",
    ):
        if not env.get(name):
            env[name] = _UNUSED_ENDPOINT_PLACEHOLDER

    if mode == "gateway":
        missing = [
            name
            for name in (
                "OPENAI_BASE_URL",
                "OPENAI_API_KEY",
                "OPENAI_MODEL",
                "MODEL_TIMEOUT",
                "MODEL_MAX_RETRIES",
                "MODEL_TEMPERATURE",
            )
            if not env.get(name)
        ]
        if missing:
            raise NoduleEvalError(
                ErrorCode.CREDENTIAL_MISSING,
                detail=(
                    f"缺少环境变量：{'、'.join(missing)}"
                    "（模型端点凭据只从环境变量读，不回落本地替身；"
                    "离线核验请加 --model-mode double）"
                ),
            )
    else:
        for name, value in _MODEL_ENDPOINT_PLACEHOLDER.items():
            if not env.get(name):
                env[name] = value

    return env


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="归并对照实验：规则基线 vs 模型归并（同一标注集上的可复现对照表）"
    )
    parser.add_argument(
        "--benchmark",
        default=None,
        help="裁判集数据文件（缺省用 src/nodule_eval/goldstd/benchmark.yaml）",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help="产物目录（缺省 reports/normalizer-compare/）",
    )
    parser.add_argument(
        "--model-mode",
        choices=("gateway", "replay", "double"),
        default="gateway",
        help="模型侧跑法：gateway=真端点（默认）/ replay=回放录下来的输出 / double=本地协议替身",
    )
    parser.add_argument(
        "--model-log",
        default=None,
        help="回放日志（JSONL）；--model-mode replay 时必填",
    )
    parser.add_argument(
        "--record",
        default=None,
        help="把本轮模型侧的原始返回录到这个 JSONL（供后续 --model-mode replay 回放）",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机源（写进文件头）")
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help="按随机源抽 N 条（0 = 全量）；抽样结果由 seed 决定，可复现",
    )
    args = parser.parse_args(argv)

    if args.model_mode == "replay" and not args.model_log:
        print("错误：--model-mode replay 需要 --model-log <录下来的 JSONL>", file=sys.stderr)
        return 2

    # ---- 裁判集 ----
    benchmark = load_benchmark_set(args.benchmark)
    cases = list(benchmark.cases)
    if args.sample and args.sample < len(cases):
        sampler = random.Random(args.seed)
        cases = sorted(sampler.sample(cases, args.sample), key=lambda item: item.id)

    # ---- 两侧 ----
    env = _default_env(args.model_mode)
    rule_labels = _run_rule_side(cases)
    model_labels, recorded, endpoint_kind = _run_model_side(
        cases,
        mode=args.model_mode,
        env=env,
        model_log=args.model_log,
    )

    rule_score = score_normalizer("rule", cases, rule_labels)
    model_score = score_normalizer("model", cases, model_labels)

    # ---- 产物 ----
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "compare-normalizers.csv", cases, rule_labels, model_labels)

    summary = {
        "benchmark": {
            "version": benchmark.version,
            "description": benchmark.description,
            "case_count": len(cases),
            "source_path": benchmark.source_path,
            "sha256": benchmark.sha256,
        },
        "run": {
            "seed": args.seed,
            "sample": args.sample,
            "model_mode": args.model_mode,
            "model_endpoint_kind": endpoint_kind,
            "model_name": env.get("OPENAI_MODEL", ""),
            "model_temperature": float(env.get("MODEL_TEMPERATURE", "0") or 0),
            "model_timeout": float(env.get("MODEL_TIMEOUT", "0") or 0),
            "model_max_retries": int(env.get("MODEL_MAX_RETRIES", "0") or 0),
            "source_sha256": _source_hashes(),
        },
        "normalizers": {
            "rule": rule_score.to_dict(),
            "model": model_score.to_dict(),
        },
        "delta": {
            "overall_accuracy": round(
                model_score.overall_accuracy - rule_score.overall_accuracy, 4
            ),
            "overall_recall": round(model_score.overall_recall - rule_score.overall_recall, 4),
            "overall_false_call_rate": round(
                model_score.overall_false_call_rate - rule_score.overall_false_call_rate, 4
            ),
        },
        "conclusion": _build_conclusion(benchmark, rule_score, model_score),
    }
    _write_json(out_dir / "compare-summary.json", summary)

    _write_json(
        out_dir / "compare-run-meta.json",
        {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "argv": list(argv) if argv is not None else sys.argv[1:],
            "model_mode": args.model_mode,
            "model_endpoint_kind": endpoint_kind,
            "model_base_url": env.get("OPENAI_BASE_URL", ""),
            "out_dir": str(out_dir),
        },
    )

    if args.record:
        _write_record_log(Path(args.record), recorded)

    _print_summary(
        benchmark,
        rule_score,
        model_score,
        model_mode=args.model_mode,
        endpoint_kind=endpoint_kind,
        seed=args.seed,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NoduleEvalError as error:
        print(f"[{error.code.value}] {error.detail}", file=sys.stderr)
        raise SystemExit(2) from error
