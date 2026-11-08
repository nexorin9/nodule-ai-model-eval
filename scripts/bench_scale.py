#!/usr/bin/env python3
"""规模压测：两千例逐例 fan-out 的耗时、判定分布与正确性核对。

**这个脚本回答四个问题**：

1. **快不快** —— 两千例跑完要多久，慢在「算」还是慢在「排队」
   （总耗时、P50 / P95 单例耗时、排队时长分开记）；
2. **并发有没有把结论跑歪** —— 同一批输入，串行跑一遍、并发跑一遍，
   **逐例判定必须一致**（失败例与挂起例也一一对应）；
3. **失败能不能被看见** —— 每类错误码都要能定位到具体是哪一例
   （逐例明细里有、调用底账里也能反查到）；
4. **限流是不是真限住了** —— 每个端点的在途峰值与排队数。

**压测目标（``--target``）**：

- ``real``（**默认**）：打**院内真实推理端点**（``INFERENCE_BASE_URL`` /
  ``INFERENCE_API_TOKEN``）。缺凭据就**指名报错**退出，绝不悄悄改打本地。
  这条路径测的是真实网络、真实排队、真实限流。
- ``local``：打**压测负载发生器**（``tests/bench_load_server.py``，住在测试层）。
  它按厂商字段规范回响应并按检查号确定地注入约 2% 的失败 ——
  **它是负载发生器，不是产品对接面**：产品主路径永远不命中它。

两种目标都跑同一套核对（并发 vs 串行逐例一致、失败可观测、限流峰值），
报告里写清本次打的是哪一种，别把压测结果当成「院内实测」。

用法::

    # 打院内真实端点
    INFERENCE_BASE_URL=... INFERENCE_API_TOKEN=... python scripts/bench_scale.py --target real

    # 打压测负载发生器（无院内环境时）
    python scripts/bench_scale.py --target local --cases 2000 --concurrency 16

产物：``bench-scale.json``（默认落在当前目录）。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
TESTS = PROJECT_ROOT / "tests"
for path in (SRC, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from nodule_eval.adapters.http import HttpClient  # noqa: E402
from nodule_eval.config import REQUIRED_ENV_VARS, load_config, load_model_spec  # noqa: E402
from nodule_eval.contracts import CaseInput, EvalRequest, RunOptions  # noqa: E402
from nodule_eval.engine import Engine  # noqa: E402
from nodule_eval.errors import NoduleEvalError  # noqa: E402
from nodule_eval.store import (  # noqa: E402
    CASE_ROWS_FILENAME,
    CASE_TIMINGS_FILENAME,
    RESULT_FILENAME,
    TOOL_CALLS_FILENAME,
    read_case_rows_csv,
    read_json_file,
)

#: 阳性金标准（放射科写法）。
POSITIVE_GOLD = "右下肺见一磨玻璃结节，大小约 8mm。"
#: 阴性金标准。
NEGATIVE_GOLD = "双肺未见明显结节影。"
#: 阳性占比。
POSITIVE_SHARE = 0.7


# --------------------------------------------------------------------------
# 输入构造
# --------------------------------------------------------------------------
def build_cases(
    count: int,
    *,
    ambiguity_rate: float,
    seed: int,
) -> tuple[list[CaseInput], dict[str, int]]:
    """造一批规模输入（**确定性**：同一 ``seed`` 造出同一批例）。

    注入两类东西：

    - **歧义**（输入层）：约 ``ambiguity_rate`` 的例**不带影像标识** ——
      模拟「检查号命中多条影像」，这类例会被绑定层挂起，**零推理调用**；
    - 其余例带影像标识，正常发起推理调用（失败与否取决于端点）。

    返回 ``(病例列表, 各类计数)``。
    """

    import hashlib

    cases: list[CaseInput] = []
    counts = {"total": count, "ambiguous": 0, "positive": 0, "negative": 0}
    for index in range(count):
        accession_no = f"BENCH-{index:05d}"
        digest = hashlib.sha256(f"{seed}:{accession_no}".encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) / float(1 << 32)
        positive = bucket < POSITIVE_SHARE
        counts["positive" if positive else "negative"] += 1
        ambiguous = bucket >= (1.0 - ambiguity_rate)
        if ambiguous:
            # 歧义例：没有影像标识 → 绑定层挂起，引擎不发起推理调用。
            counts["ambiguous"] += 1
            cases.append(
                CaseInput(
                    accession_no=accession_no,
                    gold_standard_text=POSITIVE_GOLD if positive else NEGATIVE_GOLD,
                    image_ref=None,
                    study_uid=None,
                    row_no=index + 1,
                )
            )
            continue
        cases.append(
            CaseInput(
                accession_no=accession_no,
                gold_standard_text=POSITIVE_GOLD if positive else NEGATIVE_GOLD,
                image_ref=f"dx://bench/{index:05d}",
                study_uid=f"STUDY-{index:05d}",
                row_no=index + 1,
            )
        )
    return cases, counts


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------
def percentile(values: Sequence[float], ratio: float) -> float:
    """取分位数（线性插值）。空序列返回 0。"""

    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = ratio * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _distribution(values: Iterable[str]) -> dict[str, int]:
    """计数分布（按值排序，便于报告逐行比对）。"""

    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def read_ledger(path: Path) -> list[dict[str, Any]]:
    """读调用底账 JSONL。"""

    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def locate_failures(
    rows: Sequence[Mapping[str, Any]],
    ledger: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """核对「失败可观测」：每类错误码都能定位到具体是哪一例。

    两种失败的去向不一样：

    - **调用失败**（发出去过、没成）：必须有 ``tool_call_id``，
      且能在调用底账里按 id 命中 —— 「这例凭什么算失败」要能反查；
    - **挂起**（压根没发调用）：``tool_call_id`` 为空是**对的**，
      它进「挂起数」而不是「失败数」，不能混进调用失败里。
    """

    index = {record.get("tool_call_id"): record for record in ledger}
    located: dict[str, int] = {}
    unlocated: list[dict[str, str]] = []
    for row in rows:
        code = (row.get("error_code") or "").strip()
        if not code:
            continue
        call_id = (row.get("tool_call_id") or "").strip()
        if not call_id:
            # 没发调用就没底账 —— 只能是被挂起的例（不是调用失败）。
            if str(row.get("verdict")) == "failed" and str(row.get("needs_review")).lower() not in {
                "true",
                "1",
            }:
                unlocated.append(
                    {
                        "accession_no": str(row.get("accession_no")),
                        "error_code": code,
                        "reason": "既无调用底账，也未标挂起 —— 失败无记录",
                    }
                )
            continue
        record = index.get(call_id)
        if record is None:
            unlocated.append(
                {
                    "accession_no": str(row.get("accession_no")),
                    "error_code": code,
                    "reason": "逐例行里的调用 id 在底账里查不到",
                }
            )
            continue
        located[code] = located.get(code, 0) + 1
    return {
        "located_by_error_code": {key: located[key] for key in sorted(located)},
        "unlocated": unlocated[:20],
        "unlocated_count": len(unlocated),
    }


def run_once(
    *,
    cases: Sequence[CaseInput],
    run_id: str,
    model_id: str,
    model_version: str,
    concurrency: int,
    endpoint_limits: Mapping[str, int],
    runs_dir: Path,
    dataset_ref: str,
    backoff_sleep: str = "real",
) -> dict[str, Any]:
    """跑一轮 fan-out，返回这一轮的可核对台账。

    ``backoff_sleep``
        ``real`` = 退避照真睡（打院内真实端点时用这个）；``off`` = 退避**不睡**
        （语义照旧：照退 3 次、照算指数退避量级，只是不真等）——
        打负载发生器时用这个，否则一个注入的 5xx 就会把整轮时间拖成 12 秒，
        测出来的是退避不是吞吐。退避语义本身由单测断言，不靠压测证明。
    """

    config = load_config()
    spec = load_model_spec(model_id)
    request = EvalRequest(
        run_id=run_id,
        model_id=model_id,
        model_version=model_version,
        dataset_ref=dataset_ref,
        cases=tuple(cases),
        options=RunOptions(
            concurrency=concurrency,
            endpoint_limits=dict(endpoint_limits),
        ),
    )
    client = HttpClient(sleep=(lambda _seconds: None) if backoff_sleep == "off" else time.sleep)
    engine = Engine(config, spec, runs_dir=runs_dir, client=client)
    started = time.perf_counter()
    response = engine.run(request)
    elapsed = time.perf_counter() - started

    run_dir = runs_dir / run_id
    timings = read_json_file(run_dir / CASE_TIMINGS_FILENAME) or {}
    result = read_json_file(run_dir / RESULT_FILENAME) or {}
    #: 逐例行取 ``result.json``（**带 error_code**）——
    #: ``case_rows.csv`` 是给人看的那六列 + 行号，错误码是运行时字段，不进表。
    rows = list(result.get("case_rows", []))
    #: 另读一遍 CSV：产物齐不齐、行数对不对，也要核。
    csv_rows = read_case_rows_csv(run_dir / CASE_ROWS_FILENAME)
    ledger = read_ledger(run_dir / TOOL_CALLS_FILENAME)

    elapsed_list = [float(item.get("elapsed_ms", 0.0)) for item in timings.get("cases", [])]
    queued_list = [float(item.get("queued_ms", 0.0)) for item in timings.get("cases", [])]

    return {
        "run_id": run_id,
        "concurrency": concurrency,
        "case_count": len(cases),
        "row_count": len(rows),
        "csv_row_count": len(csv_rows),
        "http_requests": len(ledger),
        "total_seconds": round(elapsed, 3),
        "cases_per_second": round(len(cases) / elapsed, 2) if elapsed > 0 else 0.0,
        "elapsed_ms": {
            "p50": round(percentile(elapsed_list, 0.50), 3),
            "p95": round(percentile(elapsed_list, 0.95), 3),
            "p99": round(percentile(elapsed_list, 0.99), 3),
            "max": round(max(elapsed_list), 3) if elapsed_list else 0.0,
            "mean": round(statistics.fmean(elapsed_list), 3) if elapsed_list else 0.0,
        },
        "queued_ms": {
            "p50": round(percentile(queued_list, 0.50), 3),
            "p95": round(percentile(queued_list, 0.95), 3),
            "total": round(sum(queued_list), 3),
        },
        "verdict_distribution": _distribution(str(row.get("verdict")) for row in rows),
        "error_code_distribution": _distribution(
            str(row.get("error_code")) for row in rows if str(row.get("error_code") or "").strip()
        ),
        "endpoint_stats": timings.get("endpoints", {}),
        "failure_observability": locate_failures(rows, ledger),
        "verdicts": {str(row.get("accession_no")): str(row.get("verdict")) for row in rows},
        "error_codes": {
            str(row.get("accession_no")): str(row.get("error_code") or "") for row in rows
        },
    }


def compare_runs(serial: Mapping[str, Any], parallel: Mapping[str, Any]) -> dict[str, Any]:
    """比对串行跑与并发跑的**逐例判定**（正确性不降的核心断言）。"""

    serial_verdicts = serial.get("verdicts", {})
    parallel_verdicts = parallel.get("verdicts", {})
    serial_codes = serial.get("error_codes", {})
    parallel_codes = parallel.get("error_codes", {})

    only_serial = sorted(set(serial_verdicts) - set(parallel_verdicts))
    only_parallel = sorted(set(parallel_verdicts) - set(serial_verdicts))
    verdict_diff = [
        {
            "accession_no": key,
            "serial": serial_verdicts.get(key),
            "parallel": parallel_verdicts.get(key),
        }
        for key in sorted(set(serial_verdicts) & set(parallel_verdicts))
        if serial_verdicts.get(key) != parallel_verdicts.get(key)
    ]
    code_diff = [
        {
            "accession_no": key,
            "serial": serial_codes.get(key),
            "parallel": parallel_codes.get(key),
        }
        for key in sorted(set(serial_codes) & set(parallel_codes))
        if serial_codes.get(key) != parallel_codes.get(key)
    ]
    identical = not (only_serial or only_parallel or verdict_diff or code_diff)
    return {
        "identical": identical,
        "compared_cases": len(set(serial_verdicts) & set(parallel_verdicts)),
        "only_in_serial": only_serial[:20],
        "only_in_parallel": only_parallel[:20],
        "verdict_differences": verdict_diff[:20],
        "verdict_difference_count": len(verdict_diff),
        "error_code_differences": code_diff[:20],
        "error_code_difference_count": len(code_diff),
    }


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def _missing_env() -> list[str]:
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name, "").strip()]


def _parse_endpoint_limits(text: str) -> dict[str, int]:
    limits: dict[str, int] = {}
    for chunk in text.split(","):
        item = chunk.strip()
        if not item:
            continue
        name, _, value = item.partition("=")
        limits[name.strip()] = int(value.strip())
    return limits


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="肺结节AI模型评估系统 · 规模压测（并发限流 + 正确性核对）",
    )
    parser.add_argument(
        "--target",
        choices=("real", "local"),
        default="real",
        help="压测目标：real = 院内真实推理端点（默认）；local = 压测负载发生器（非产品对接面）",
    )
    parser.add_argument("--cases", type=int, default=2000, help="病例数（默认 2000）")
    parser.add_argument("--concurrency", type=int, default=16, help="并发工作线程数（默认 16）")
    parser.add_argument(
        "--endpoint-limits",
        default="",
        metavar="inference=8,model=2",
        help="逐端点在途上限（缺省每个端点都取并发数）",
    )
    parser.add_argument("--model-id", default="demo-model", help="被评模型标识")
    parser.add_argument("--runs-dir", default="runs", help="产物目录（缺省 runs/）")
    parser.add_argument("--out", default="bench-scale.json", help="压测报告落点")
    parser.add_argument("--ambiguity-rate", type=float, default=0.02, help="歧义注入比例")
    parser.add_argument("--failure-rate", type=float, default=0.02, help="失败注入比例（仅 local 目标）")
    parser.add_argument("--seed", type=int, default=20261107, help="注入的随机源（写进报告便于复现）")
    parser.add_argument(
        "--latency-ms",
        type=float,
        default=0.0,
        metavar="MS",
        help="负载发生器每次响应的模拟推理耗时（毫秒；仅 local 目标有效）",
    )
    parser.add_argument(
        "--backoff-sleep",
        choices=("real", "off"),
        default="",
        help="5xx 退避是否真睡（缺省：local 目标不睡、real 目标真睡）",
    )
    parser.add_argument("--skip-serial", action="store_true", help="跳过串行对照跑（不核对正确性）")
    parser.add_argument(
        "--store",
        default="",
        metavar="PATH",
        help="run store 数据库路径（缺省用平台默认目录）",
    )
    args = parser.parse_args(argv)

    missing = _missing_env()
    if missing:
        print("缺少必填环境变量，压测不启动：", file=sys.stderr)
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        print(
            "（压测不会改用本地替身：缺凭据就指名报错）",
            file=sys.stderr,
        )
        return 2

    generator = None
    if args.target == "local":
        from bench_load_server import LoadGenerator  # 测试层的负载发生器

        generator = LoadGenerator(
            failure_rate=args.failure_rate,
            seed=args.seed,
            delay_seconds=args.latency_ms / 1000.0,
        )
        generator.start()
        os.environ["INFERENCE_BASE_URL"] = generator.base_url
        os.environ["INFERENCE_API_TOKEN"] = "load-generator-token"

    backoff_sleep = args.backoff_sleep or ("off" if args.target == "local" else "real")
    runs_dir = Path(args.runs_dir)
    endpoint_limits = _parse_endpoint_limits(args.endpoint_limits)
    cases, case_counts = build_cases(
        args.cases, ambiguity_rate=args.ambiguity_rate, seed=args.seed
    )

    model_spec = load_model_spec(args.model_id)
    model_version = model_spec.model_version
    dataset_ref = f"规模压测样例（{args.cases} 例，seed={args.seed}）"

    try:
        serial = None
        if not args.skip_serial:
            print(f"跑串行对照（并发 1，{args.cases} 例）……", flush=True)
            serial = run_once(
                cases=cases,
                run_id=f"bench-serial-{args.seed}",
                model_id=args.model_id,
                model_version=model_version,
                concurrency=1,
                endpoint_limits=endpoint_limits,
                runs_dir=runs_dir,
                dataset_ref=dataset_ref,
                backoff_sleep=backoff_sleep,
            )
            print(
                f"  串行完成：{serial['total_seconds']} 秒，"
                f"判定分布 {serial['verdict_distribution']}",
                flush=True,
            )

        print(f"跑并发轮（并发 {args.concurrency}，{args.cases} 例）……", flush=True)
        parallel = run_once(
            cases=cases,
            run_id=f"bench-parallel-{args.seed}",
            model_id=args.model_id,
            model_version=model_version,
            concurrency=args.concurrency,
            endpoint_limits=endpoint_limits,
            runs_dir=runs_dir,
            dataset_ref=dataset_ref,
            backoff_sleep=backoff_sleep,
        )
        print(
            f"  并发完成：{parallel['total_seconds']} 秒，"
            f"判定分布 {parallel['verdict_distribution']}",
            flush=True,
        )
    finally:
        if generator is not None:
            generator.stop()

    comparison = compare_runs(serial, parallel) if serial is not None else {"identical": None}

    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "target": args.target,
        "target_note": (
            "院内真实推理端点"
            if args.target == "real"
            else "压测负载发生器（测试层，非产品对接面）"
        ),
        "seed": args.seed,
        "model_id": args.model_id,
        "model_version": model_version,
        "concurrency": args.concurrency,
        "endpoint_limits": endpoint_limits,
        "backoff_sleep": backoff_sleep,
        "simulated_latency_ms": args.latency_ms,
        "injection": {
            "ambiguity_rate": args.ambiguity_rate,
            "failure_rate": args.failure_rate if args.target == "local" else 0.0,
            "case_counts": case_counts,
            "load_generator_injected": generator.injected if generator is not None else {},
        },
        "serial": serial,
        "parallel": parallel,
        "comparison": comparison,
        "speedup": (
            round(serial["total_seconds"] / parallel["total_seconds"], 2)
            if serial is not None and parallel["total_seconds"] > 0
            else None
        ),
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"压测报告      : {out_path}")
    print(f"压测目标      : {report['target']}（{report['target_note']}）")
    print(f"病例数        : {args.cases}（歧义 {case_counts['ambiguous']}）")
    if serial is not None:
        print(f"串行总耗时    : {serial['total_seconds']} 秒")
    print(f"并发总耗时    : {parallel['total_seconds']} 秒")
    print(f"单例耗时 P50  : {parallel['elapsed_ms']['p50']} 毫秒")
    print(f"单例耗时 P95  : {parallel['elapsed_ms']['p95']} 毫秒")
    print(f"排队耗时 P95  : {parallel['queued_ms']['p95']} 毫秒")
    print(f"判定分布      : {parallel['verdict_distribution']}")
    print(f"错误码分布    : {parallel['error_code_distribution']}")
    print(f"失败可观测    : 已定位 {parallel['failure_observability']['located_by_error_code']}")
    if serial is not None:
        print(
            "并发 vs 串行  : "
            + ("逐例判定一致" if comparison.get("identical") else f"**不一致** {comparison}")
        )
    return 0 if comparison.get("identical") in (True, None) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NoduleEvalError as exc:
        print(f"[{exc.code.value}] {exc.message}", file=sys.stderr)
        raise SystemExit(2) from exc
