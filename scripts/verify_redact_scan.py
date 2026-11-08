"""产物脱敏扫描的端到端验收（任务 34）：真 CLI 进程 + 真 socket 替身。

跑法（项目根目录下）::

    python3 scripts/verify_redact_scan.py

做三件事：

1. 真跑一轮（推理替身是真 HTTP 服务、真 ``requests.Session``、真 Bearer 头）；
2. 用真 CLI 进程 ``redact-scan`` 扫这一轮 —— 干净则退出码 0；
3. 往产物里塞一处姓名明细，再扫一次 —— 退出码非零、错误打在 stderr、
   **明细本身不出现在任何输出里**。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from stub_inference import InferenceStub  # noqa: E402

from nodule_eval.adapters.http import HttpClient  # noqa: E402
from nodule_eval.config import load_config, load_model_spec  # noqa: E402
from nodule_eval.contracts import CaseInput, EvalRequest  # noqa: E402
from nodule_eval.engine import Engine  # noqa: E402
from nodule_eval.store import RunStore  # noqa: E402

ENV = {
    "RIS_BASE_URL": "https://ris.test-hospital.internal",
    "RIS_API_TOKEN": "test-ris-token",
    "INFERENCE_BASE_URL": "https://nodule.test-vendor.internal",
    "INFERENCE_API_TOKEN": "test-inference-token",
    "RESEARCH_PLATFORM_BASE_URL": "https://research.test-hospital.internal",
    "RESEARCH_PLATFORM_TOKEN": "test-research-token",
    "OPENAI_BASE_URL": "https://llm.test-hospital.internal/v1",
    "OPENAI_API_KEY": "test-model-token",
    "OPENAI_MODEL": "nodule-goldstd-merge",
    "MODEL_TIMEOUT": "20",
    "MODEL_MAX_RETRIES": "2",
    "MODEL_TEMPERATURE": "0",
}

GOLD_POSITIVE = "右下肺见一磨玻璃结节，大小约 8mm。"
GOLD_NEGATIVE = "双肺未见明显结节影。"


def _run_once(runs_dir: Path, run_id: str) -> None:
    """真跑一轮。"""

    stub = InferenceStub()
    stub.start()
    os.environ["INFERENCE_BASE_URL"] = stub.base_url
    try:
        config = load_config()
        spec = load_model_spec("demo-model")
        store = RunStore.open(runs_dir.parent / "store.sqlite3", runs_dir=runs_dir)
        engine = Engine(
            config,
            spec,
            client=HttpClient(sleep=lambda _seconds: None),
            runs_dir=runs_dir,
            store=store,
        )
        engine.run(
            EvalRequest(
                run_id=run_id,
                model_id="demo-model",
                model_version="v1.0.0",
                dataset_ref="脱敏扫描端到端验收",
                cases=(
                    CaseInput(
                        accession_no="ACC-0001",
                        gold_standard_text=GOLD_POSITIVE,
                        image_ref="dx://local/0001",
                        study_uid="STUDY-0001",
                        row_no=1,
                    ),
                    CaseInput(
                        accession_no="ACC-0002",
                        gold_standard_text=GOLD_NEGATIVE,
                        image_ref="dx://local/0002",
                        study_uid="STUDY-0002",
                        row_no=2,
                    ),
                ),
            )
        )
        store.close()
    finally:
        stub.stop()


def _scan(runs_dir: Path, run_id: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "nodule_eval.cli",
            "redact-scan",
            "--run-id",
            run_id,
            "--runs-dir",
            str(runs_dir),
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def main() -> int:
    os.environ.update(ENV)
    workdir = Path(tempfile.mkdtemp(prefix="redact-verify-"))
    runs_dir = workdir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    print("── 1. 真跑一轮（真 socket 替身 + 真 requests.Session + 真 Bearer 头）")
    _run_once(runs_dir, "redact-verify")
    run_dir = runs_dir / "redact-verify"
    artifacts = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file())
    print(f"   产物 {len(artifacts)} 份：")
    for name in artifacts:
        print(f"     - {name}")

    print()
    print("── 2. 扫一遍（干净则退出码 0）")
    clean = _scan(runs_dir, "redact-verify", workdir)
    print(clean.stdout.rstrip())
    print(f"   退出码：{clean.returncode}")
    if clean.returncode != 0:
        print(clean.stderr)
        return 1

    print()
    print("── 3. 往产物里塞一处姓名明细，再扫一次")
    target = run_dir / "summary.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["summary_text"] = "复核人：姓名：李四"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    dirty = _scan(runs_dir, "redact-verify", workdir)
    print(dirty.stdout.rstrip())
    print(f"   退出码：{dirty.returncode}")
    print(f"   stderr：{dirty.stderr.strip()}")
    if dirty.returncode == 0:
        print("   ✗ 塞了明细却没报错 —— 闸门失灵")
        return 1
    if "李四" in dirty.stdout or "李四" in dirty.stderr:
        print("   ✗ 报告里夹带了明细本身")
        return 1
    if "ARTIFACT_PII_DETECTED" not in dirty.stderr:
        print("   ✗ 错误码不对")
        return 1

    print()
    print("✓ 脱敏扫描端到端验收通过：干净轮零命中，塞了明细即失败且不夹带明细")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
