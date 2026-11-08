#!/usr/bin/env python3
"""提示词版本化验收（任务 47）：真 CLI 跑一轮 ``--merge model``，
逐条核对「这批结论是哪版口径跑的」在底账里查得到。

**验的是什么**

1. 模型调用底账 ``runs/<run_id>/model_calls.jsonl`` 里每一行的
   ``prompt_sha256`` **等于** ``prompts/goldstd_merge.v1.md`` 的文件内容哈希
   （``sha256sum`` 逐字比）—— 不是「差不多」，是**同一个值**；
2. ``prompt_version`` 是文件名里的版本号（``v1``），
   ``prompt_template`` 是那个文件的相对路径 —— 三者凑一起能反查回是哪一版；
3. 提示词里的**标签表占位符**在渲染后已被填满（渲染后正文不再有
   ``{{LOCATIONS}}`` / ``{{DENSITIES}}``），而**文件里**占位符仍在
   —— 标签表换了不用改提示词；
4. 提示词文件缺安全条款 / 字段与输出契约对不上时，**载入即报错**
   （不回落内置兜底提示词）—— 这一条用临时改坏的副本验证。

**怎么跑**

    python3 scripts/verify_prompt_version.py

退出码 ``0`` 表示全部核对通过；任一条不通过即非零退出并打印差在哪。
本脚本只打**本地协议替身**（真 socket + 真 requests.Session + 真 Bearer 头），
**不打院内端点**；它验的是「提示词这一层的口径与留痕」，不是「模型准不准」。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
TESTS = PROJECT_ROOT / "tests"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from stub_inference import InferenceStub  # noqa: E402
from stub_model import ModelStub  # noqa: E402

from nodule_eval.contracts import MERGE_OUTPUT_FIELDS  # noqa: E402
from nodule_eval.errors import ErrorCode, NoduleEvalError  # noqa: E402
from nodule_eval.goldstd.prompt import (  # noqa: E402
    DEFAULT_PROMPT_PATH,
    load_prompt_bundle,
)

#: 金标准原文（属性齐全，模型归并的回指锚点就在这段里）。
GOLD_TEXT = "右下肺见一磨玻璃结节，最大径约8mm，边界欠清。"

#: 模型端的固定回复（字段齐全且合契约）。
MODEL_CONTENT = json.dumps(
    {
        "presence": True,
        "location": "右肺下叶",
        "size_mm": 8.0,
        "density": "磨玻璃",
        "evidence_span": "右下肺见一磨玻璃结节，最大径约8mm",
        "confidence": 0.92,
        "needs_human_review": False,
    },
    ensure_ascii=False,
)

_FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """记一条核对结果（不抛异常 —— 一次跑完所有核对再汇总）。"""

    mark = "通过" if ok else "不通过"
    print(f"[{mark}] {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        _FAILURES.append(label)


def _base_env(inference_url: str, model_url: str) -> dict[str, str]:
    """一套齐备的环境变量（全部为占位值，无任何真实凭据）。"""

    return {
        "RIS_BASE_URL": "https://ris.test-hospital.internal",
        "RIS_API_TOKEN": "test-ris-token",
        "INFERENCE_BASE_URL": inference_url,
        "INFERENCE_API_TOKEN": "test-inference-token",
        "RESEARCH_PLATFORM_BASE_URL": "https://research.test-hospital.internal",
        "RESEARCH_PLATFORM_TOKEN": "test-research-token",
        "OPENAI_BASE_URL": model_url,
        "OPENAI_API_KEY": "test-model-token",
        "OPENAI_MODEL": "nodule-goldstd-merge",
        "MODEL_TIMEOUT": "20",
        "MODEL_MAX_RETRIES": "2",
        "MODEL_TEMPERATURE": "0",
    }


def _run_cli(tmp: Path, env: dict[str, str]) -> tuple[int, str]:
    """真 CLI **子进程**跑一轮 ``--merge model``。"""

    cases_path = tmp / "cases.json"
    cases_path.write_text(
        json.dumps(
            {
                "run_id": "prompt-version-verify",
                "model_id": "demo-model",
                "model_version": "v1.0.0",
                "dataset_ref": "提示词版本化验收",
                "cases": [
                    {
                        "accession_no": "ACC-PV-0001",
                        "gold_standard_text": GOLD_TEXT,
                        "image_ref": "dx://local/0001",
                        "study_uid": "STUDY-PV-0001",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    child_env = dict(os.environ)
    child_env.update(env)
    child_env["PYTHONPATH"] = str(SRC) + os.pathsep + child_env.get("PYTHONPATH", "")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "nodule_eval.cli",
            "run",
            "--cases",
            str(cases_path),
            "--model-id",
            "demo-model",
            "--merge",
            "model",
            "--run-id",
            "prompt-version-verify",
            "--runs-dir",
            str(tmp / "runs"),
            "--store",
            str(tmp / "store.sqlite3"),
        ],
        cwd=str(PROJECT_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )
    print("--- CLI stdout ---")
    print(completed.stdout.strip() or "(空)")
    if completed.stderr.strip():
        print("--- CLI stderr ---")
        print(completed.stderr.strip())
    return completed.returncode, completed.stdout + completed.stderr


def main() -> int:
    """跑完全部核对，返回退出码。"""

    file_hash = hashlib.sha256(DEFAULT_PROMPT_PATH.read_bytes()).hexdigest()
    print(f"提示词文件      : {DEFAULT_PROMPT_PATH.relative_to(PROJECT_ROOT)}")
    print(f"文件内容 sha256 : {file_hash}")
    print()

    inference_stub = InferenceStub()
    model_stub = ModelStub(content=MODEL_CONTENT)
    inference_stub.start()
    model_stub.start()
    try:
        with tempfile.TemporaryDirectory(prefix="prompt-version-verify-") as raw_tmp:
            tmp = Path(raw_tmp)
            env = _base_env(inference_stub.base_url, model_stub.base_url)
            code, _ = _run_cli(tmp, env)

            check("CLI 退出码为 0", code == 0, f"实得 {code}")

            ledger = tmp / "runs" / "prompt-version-verify" / "model_calls.jsonl"
            check("模型调用底账已落盘", ledger.is_file(), str(ledger))
            if not ledger.is_file():
                return 1

            rows = [
                json.loads(line)
                for line in ledger.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            check("底账有 1 行模型调用", len(rows) == 1, f"实得 {len(rows)}")
            if not rows:
                return 1
            row = rows[0]

            check(
                "底账 prompt_sha256 == 提示词文件实际哈希",
                row.get("prompt_sha256") == file_hash,
                f"账上 {row.get('prompt_sha256')} / 文件 {file_hash}",
            )
            check(
                "底账 prompt_template 指向 prompts/ 下的那一版",
                row.get("prompt_template") == "prompts/goldstd_merge.v1.md",
                f"实得 {row.get('prompt_template')}",
            )
            check(
                "底账 prompt_version 是文件名里的版本号",
                row.get("prompt_version") == "v1",
                f"实得 {row.get('prompt_version')}",
            )
            check(
                "实际发出去的提示词另有渲染哈希（两者分工不同）",
                bool(row.get("prompt_rendered_sha256"))
                and row.get("prompt_rendered_sha256") != row.get("prompt_sha256"),
                f"渲染哈希 {row.get('prompt_rendered_sha256')}",
            )
            check(
                "账上不含金标准原文（只落哈希不落原文）",
                GOLD_TEXT not in ledger.read_text(encoding="utf-8"),
                "底账里没出现金标准原文",
            )

            from nodule_eval.audit.modelcall import build_prompt_index, resolve_prompt_version

            index = build_prompt_index(PROJECT_ROOT / "prompts")
            resolved = resolve_prompt_version(row.get("prompt_sha256", ""), index)
            check(
                "凭哈希能反查回 prompts/ 下的具体文件",
                resolved == "prompts/goldstd_merge.v1.md",
                f"反查得 {resolved or '(空)'}",
            )
    finally:
        inference_stub.stop()
        model_stub.stop()

    print()
    bundle = load_prompt_bundle()
    check(
        "渲染后提示词已填满标签表占位符",
        "{{LOCATIONS}}" not in bundle.rendered_text
        and "{{DENSITIES}}" not in bundle.rendered_text,
        "渲染后无占位符残留",
    )
    check(
        "提示词文件里占位符仍在（标签表换了不用改提示词）",
        "{{LOCATIONS}}" in bundle.template_text
        and "{{DENSITIES}}" in bundle.template_text,
        "模板里保留占位符",
    )
    check(
        "提示词声明的字段与输出契约一致",
        set(bundle.example_fields) <= set(MERGE_OUTPUT_FIELDS) | {"lesions"},
        f"示例字段 {list(bundle.example_fields)}",
    )

    # 提示词被改坏（缺字段 / 缺安全条款）时**载入即报错**，不回落兜底提示词。
    with tempfile.TemporaryDirectory(prefix="prompt-broken-") as raw_tmp:
        broken = Path(raw_tmp) / "goldstd_merge.v9.md"
        broken.write_text(
            DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").replace(
                '"evidence_span": "右下肺见一磨玻璃结节，最大径约8mm",', "", 1
            ),
            encoding="utf-8",
        )
        try:
            load_prompt_bundle(broken)
            check("提示词缺必填字段时载入即报错", False, "居然载入了")
        except NoduleEvalError as exc:
            check(
                "提示词缺必填字段时载入即报错",
                exc.code == ErrorCode.CONTRACT_INVALID and "evidence_span" in exc.detail,
                exc.detail[:80],
            )

    print()
    if _FAILURES:
        print(f"核对不通过 {len(_FAILURES)} 条：")
        for item in _FAILURES:
            print(f"  - {item}")
        return 1
    print("全部核对通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
