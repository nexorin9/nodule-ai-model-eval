#!/usr/bin/env python3
"""回执状态机、幂等重放与写回失败重试的**端到端实测**（任务 37）。

这个脚本跑的是**真实协议面**：真的 HTTP 服务、真的 ``requests.Session``、
真的 ``Authorization: Bearer ...`` 头、真的 ``Idempotency-Key`` 头、
真的回执核对查询（``GET ?idempotency_key=``）。
它对着**协议替身**（``tests/stub_inference.py`` / ``tests/stub_research_platform.py``）
走完五个场景：

1. **人工确认提交** → 对端**一次 POST**、**一条登记**，回执落「已登记」；
2. **再点一次「确认提交」** → **幂等回放**：一次出站调用都不发，
   对端仍只有那一条登记，两次拿回同一个登记号；
3. **写回失败（科研平台 5xx）** → run 停在「已复核」（**不标成已提交**），
   错误码、原始状态码与尝试次数落进回执台账与写回底账；
4. **结果未知（对端已登记、响应丢了）** → 本地回执记「结果未知」，
   重试前**先去核对**，查到就收敛成「已登记」、**不再发 POST**；
5. **回执核对** → 本地登记号与对端登记号摆在一起，一致才算真的生效。

每一步都打印**可核对的数**（发了几次、对端记了几条、状态是什么、回执是什么），
用来回答飞检问询里的那句话：「这一轮到底写回过没有？重复点会不会多出一条？」

**替身纪律**：替身只存在于 ``tests/`` 下，本脚本**只在测试层**用它 ——
产品代码（``src/``）不 import 它，默认出站路径也永远不会命中它。

用法::

    python scripts/verify_writeback_idempotency.py
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

from nodule_eval.adapters.http import HttpClient  # noqa: E402
from nodule_eval.config import load_config, load_model_spec  # noqa: E402
from nodule_eval.contracts import CaseInput, EvalRequest  # noqa: E402
from nodule_eval.engine import Engine  # noqa: E402
from nodule_eval.errors import NoduleEvalError  # noqa: E402
from nodule_eval.store import (  # noqa: E402
    ACTOR_HUMAN_REVIEW,
    ACTOR_HUMAN_SUBMIT,
    RunStore,
)
from nodule_eval.writeback import WRITEBACK_AUDIT_FILENAME, WritebackService  # noqa: E402
from stub_inference import InferenceStub  # noqa: E402
from stub_research_platform import ResearchPlatformStub  # noqa: E402

#: 环境变量全是**占位值**，无真实凭据（真连院内端点时换成真值）。
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

#: 一段真实胸片金标准原文（放射科写法：部位 + 密度 + 尺寸）。
GOLD_TEXT = "右下肺见一磨玻璃结节，最大径约8mm。"


class CountingClient(HttpClient):
    """记「发了几次出站请求」的客户端。"""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.calls = 0

    def request(self, method: str, url: str, **kwargs: object):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().request(method, url, **kwargs)


def _cases(count: int, prefix: str = "ACC") -> tuple[CaseInput, ...]:
    return tuple(
        CaseInput(
            accession_no=f"{prefix}-{index:04d}",
            gold_standard_text=GOLD_TEXT,
            image_ref=f"dx://local/{index:04d}",
            study_uid=f"STUDY-{index:04d}",
            row_no=index + 1,
        )
        for index in range(count)
    )


def _posts(stub: ResearchPlatformStub) -> int:
    return len([r for r in stub.requests if r["method"] == "POST"])


def _audit_rows(workdir: Path, run_id: str) -> list[dict[str, object]]:
    path = workdir / "runs" / run_id / WRITEBACK_AUDIT_FILENAME
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run_one(
    workdir: Path,
    run_id: str,
    inference: InferenceStub,
    research: ResearchPlatformStub,
    *,
    read_timeout: float = 60.0,
) -> tuple[RunStore, WritebackService]:
    """跑一轮评估 → 人工复核 → 返回（run store，写回服务）。"""

    config = load_config({**ENV, "RESEARCH_PLATFORM_BASE_URL": research.base_url})
    store = RunStore.open(workdir / f"{run_id}.sqlite3", runs_dir=workdir / "runs")
    engine = Engine(
        config,
        load_model_spec("demo-model"),
        client=HttpClient(sleep=lambda _s: None),
        runs_dir=workdir / "runs",
        store=store,
    )
    engine.run(
        EvalRequest(
            run_id=run_id,
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="评估表-2026-03（胸片肺结节）",
            cases=_cases(3),
        )
    )
    store.review_run(run_id, actor=ACTOR_HUMAN_REVIEW)
    service = WritebackService(
        config,
        store,
        client=HttpClient(sleep=lambda _s: None, read_timeout=read_timeout),
    )
    return store, service


def main() -> int:
    for key, value in ENV.items():
        os.environ[key] = value

    inference = InferenceStub()
    inference.start()
    os.environ["INFERENCE_BASE_URL"] = inference.base_url

    workdir = Path(tempfile.mkdtemp(prefix="verify-writeback-idem-"))
    ok = True

    try:
        # ---------------------------------------------------------- 场景 1 + 2
        research = ResearchPlatformStub()
        research.start()
        counter = CountingClient(sleep=lambda _s: None)
        try:
            config = load_config({**ENV, "RESEARCH_PLATFORM_BASE_URL": research.base_url})
            store = RunStore.open(workdir / "s1.sqlite3", runs_dir=workdir / "runs")
            engine = Engine(
                config,
                load_model_spec("demo-model"),
                client=HttpClient(sleep=lambda _s: None),
                runs_dir=workdir / "runs",
                store=store,
            )
            engine.run(
                EvalRequest(
                    run_id="verify-idem-001",
                    model_id="demo-model",
                    model_version="v1.0.0",
                    dataset_ref="评估表-2026-03（胸片肺结节）",
                    cases=_cases(3),
                )
            )
            store.review_run("verify-idem-001", actor=ACTOR_HUMAN_REVIEW)
            service = WritebackService(config, store, client=counter)

            print("=== 场景 1：人工确认提交 ===")
            first = service.submit("verify-idem-001", actor=ACTOR_HUMAN_SUBMIT)
            print(f"出站调用次数      : {counter.calls} 次（应为 1）")
            print(f"科研平台 POST 数  : {_posts(research)} 次（应为 1）")
            print(f"科研平台登记条数  : {len(research.registrations)} 条（应为 1）")
            print(f"登记号            : {first.receipt.registration_id}")
            print(f"幂等键            : {first.receipt.idempotency_key[:16]}…")
            print(f"run 状态          : {store.get_run('verify-idem-001').status}")
            receipt = store.get_receipt_by_run("verify-idem-001")
            print(f"本地回执状态      : {receipt.status}")

            print()
            print("=== 场景 2：再点一次「确认提交」（幂等回放） ===")
            posts_before = _posts(research)
            second = service.submit("verify-idem-001", actor=ACTOR_HUMAN_SUBMIT)
            print(f"出站调用次数      : {counter.calls} 次（应仍为 1）")
            print(f"科研平台 POST 数  : {_posts(research)} 次（应仍为 1）")
            print(f"科研平台登记条数  : {len(research.registrations)} 条（应仍为 1）")
            print(f"这次是回放吗      : {second.replayed}（应为 True）")
            print(f"登记号与第一次一致: {second.receipt.registration_id == first.receipt.registration_id}")
            print(f"run 状态          : {store.get_run('verify-idem-001').status}")
            rows = _audit_rows(workdir, "verify-idem-001")
            print(f"写回底账动作      : {[r['action'] for r in rows]}（应为 ['submit', 'replay']）")
            ok = ok and _posts(research) == 1 and len(research.registrations) == 1
            ok = ok and second.replayed and rows[-1]["action"] == "replay"

            print()
            print("=== 场景 5：回执核对（本地 vs 对端） ===")
            check = service.verify("verify-idem-001")
            print(f"本地回执状态      : {check.local_status}")
            print(f"对端查到登记      : {check.remote_found}")
            print(f"本地登记号        : {check.registration_id}")
            print(f"对端登记号        : {check.remote_registration_id}")
            print(f"两边一致          : {check.matched}（应为 True）")
            print(f"核对查询次数      : {len(research.lookup_requests)} 次")
            ok = ok and check.matched
            store.close()
        finally:
            research.stop()

        # ---------------------------------------------------------- 场景 3
        print()
        print("=== 场景 3：写回失败（科研平台一直 5xx） ===")
        broken = ResearchPlatformStub(status_sequence=[500])
        broken.start()
        try:
            store3, service3 = _run_one(
                workdir, "verify-idem-003", inference, broken
            )
            try:
                service3.submit("verify-idem-003", actor=ACTOR_HUMAN_SUBMIT)
                print("!! 不该走到这里 —— 写回失败了却没报错")
                ok = False
            except NoduleEvalError as exc:
                print(f"错误码            : {exc.code.value}")
                print(f"原始状态码        : {exc.http_status}")
                print(f"中文文案          : {exc.message}")
            print(f"科研平台 POST 数  : {_posts(broken)} 次（5xx 退避重试 3 次）")
            print(f"run 状态          : {store3.get_run('verify-idem-003').status}（应仍为 reviewed）")
            receipt3 = store3.get_receipt_by_run("verify-idem-003")
            print(f"本地回执状态      : {receipt3.status}（应为 failed）")
            print(f"回执错误码        : {receipt3.error_code}")
            print(f"回执尝试次数      : {receipt3.attempts}")
            rows3 = _audit_rows(workdir, "verify-idem-003")
            print(f"写回底账动作      : {[r['action'] for r in rows3]}")
            ok = ok and store3.get_run("verify-idem-003").status == "reviewed"
            ok = ok and receipt3.status == "failed" and receipt3.attempts == 3
            ok = ok and [r["action"] for r in rows3] == ["submit_failed"]
            store3.close()
        finally:
            broken.stop()

        # ---------------------------------------------------------- 场景 4
        print()
        print("=== 场景 4：结果未知（对端已登记、响应丢了） ===")
        hanging = ResearchPlatformStub(hang_after_commit_seconds=30.0)
        hanging.start()
        try:
            store4, service4 = _run_one(
                workdir, "verify-idem-004", inference, hanging, read_timeout=0.5
            )
            try:
                service4.submit("verify-idem-004", actor=ACTOR_HUMAN_SUBMIT)
                print("!! 不该走到这里 —— 超时了却没报错")
                ok = False
            except NoduleEvalError as exc:
                print(f"错误码            : {exc.code.value}")
                print(f"有 HTTP 状态码吗  : {exc.http_status}（传输层失败没有）")
            receipt4 = store4.get_receipt_by_run("verify-idem-004")
            print(f"本地回执状态      : {receipt4.status}（应为 pending = 结果未知）")
            print(f"run 状态          : {store4.get_run('verify-idem-004').status}（应仍为 reviewed）")
            print(f"对端其实已登记    : {len(hanging.registrations)} 条")

            posts_before = _posts(hanging)
            replay = service4.submit("verify-idem-004", actor=ACTOR_HUMAN_SUBMIT)
            print(f"重试时 POST 数    : {_posts(hanging)} 次（应与重试前相同 = {posts_before}）")
            print(f"重试时核对查询数  : {len(hanging.lookup_requests)} 次（应为 1）")
            print(f"重试后回执状态    : {store4.get_receipt_by_run('verify-idem-004').status}")
            print(f"run 状态          : {store4.get_run('verify-idem-004').status}")
            ok = ok and _posts(hanging) == posts_before
            ok = ok and store4.get_receipt_by_run("verify-idem-004").status == "registered"
            ok = ok and store4.get_run("verify-idem-004").status == "submitted"
            ok = ok and replay.replayed
            store4.close()
        finally:
            hanging.stop()

        print()
        print(f"总判定            : {'通过' if ok else '未通过'}")
        return 0 if ok else 1
    finally:
        inference.stop()


if __name__ == "__main__":
    raise SystemExit(main())
