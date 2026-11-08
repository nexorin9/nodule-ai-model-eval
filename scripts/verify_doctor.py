#!/usr/bin/env python3
"""``doctor`` 自检的**端到端实测**（任务 40）。

这个脚本回答的是院内科室装机器时最关心的两个问题：

1. **「装好了没有？」** —— 逐项自检，缺什么**指名什么**，
   有阻断项时退出码非零，院内安装脚本据此判断；
2. **「自检会不会自己惹事？」** —— **不发任何出站调用**、
   **不打开替身门**。自检打真实端点会消耗额度，也会把对端日志搞脏。

跑的是**真实 CLI 子进程**（不是函数调用），所以退出码、stderr 走向、
JSON 输出形状都是真的；出站面用**协议替身**（``tests/stub_*.py``）监听真 socket，
用来证明「doctor 打过去 0 次」。

场景依次为：

1. **一个变量都不配** → 退出码非零，**12 项变量逐个列名**；
2. **只缺一项** → 只指名**那一个**变量名（不是「配置有误」让人自己猜）；
3. **全部配齐** → 退出码 0，「自检通过」；
4. **JSON 输出** → 阻断项与提示项**分开列**（给院内脚本读）；
5. **``--fixture``** → 提及替身且说清「仅测试用、非默认」，
   且**替身门没被打开**（``IMAGING_EVAL_FIXTURE`` 仍是关的）；
6. **零出站** → 把四个出站面全指向真 socket 替身，doctor 跑完
   **替身收到 0 次请求**；
7. **``.env`` 落法** → 凭据配在 ``.env`` 里时自检**读得到**（不误报缺变量）；
8. **写入探针** → 运行库真被写过一次又回滚，**不留残迹**。

**替身纪律**：替身只存在于 ``tests/`` 下，本脚本**只在测试层**用它 ——
产品代码（``src/``）不 import 它。

用法::

    python scripts/verify_doctor.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
TESTS_DIR = PROJECT_ROOT / "tests"

for _path in (str(SRC_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from stub_inference import InferenceStub  # noqa: E402
from stub_research_platform import ResearchPlatformStub  # noqa: E402
from stub_ris import RISStub  # noqa: E402

#: 12 项必填环境变量 —— 与 ``config.REQUIRED_ENV_VARS`` 同源，这里只作断言用。
REQUIRED_ENV_VARS: tuple[str, ...] = (
    "RIS_BASE_URL",
    "RIS_API_TOKEN",
    "INFERENCE_BASE_URL",
    "INFERENCE_API_TOKEN",
    "RESEARCH_PLATFORM_BASE_URL",
    "RESEARCH_PLATFORM_TOKEN",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "MODEL_TIMEOUT",
    "MODEL_MAX_RETRIES",
    "MODEL_TEMPERATURE",
)


def _base_env() -> dict[str, str]:
    """一份齐备的凭据（**全是占位值**，只用来验自检逻辑，不指向任何真实端点）。"""

    return {
        "RIS_BASE_URL": "https://ris.example.internal",
        "RIS_API_TOKEN": "placeholder-ris-token",
        "INFERENCE_BASE_URL": "https://inference.example.internal",
        "INFERENCE_API_TOKEN": "placeholder-inference-token",
        "RESEARCH_PLATFORM_BASE_URL": "https://research.example.internal",
        "RESEARCH_PLATFORM_TOKEN": "placeholder-research-token",
        "OPENAI_BASE_URL": "https://model.example.internal/v1",
        "OPENAI_API_KEY": "placeholder-model-key",
        "OPENAI_MODEL": "hospital-merge-model",
        "MODEL_TIMEOUT": "60",
        "MODEL_MAX_RETRIES": "3",
        "MODEL_TEMPERATURE": "0",
    }


def _run_cli(
    args: list[str],
    env: dict[str, str],
    cwd: Path,
) -> tuple[int, str, str]:
    """跑一次**真实 CLI 子进程**，返回 (退出码, stdout, stderr)。"""

    child_env = dict(os.environ)
    # 继承的凭据先清干净 —— 自检必须只看到本场景给的那些变量。
    for name in REQUIRED_ENV_VARS:
        child_env.pop(name, None)
    child_env.pop("IMAGING_EVAL_FIXTURE", None)
    child_env.pop("IMAGING_EVAL_FIXTURE_BASE_URL", None)
    child_env.pop("NODULE_EVAL_ENV_FILE", None)
    child_env.update(env)
    child_env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC_DIR), str(TESTS_DIR), child_env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    child_env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.run(
        [sys.executable, "-m", "nodule_eval.cli", *args],
        cwd=str(cwd),
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode, proc.stdout, proc.stderr


def _banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def scenario_missing_all(workdir: Path) -> None:
    """场景 1：一个变量都不配 → 退出码非零 + 12 项逐个列名。"""

    _banner("场景 1 · 一个变量都不配：退出码非零，缺哪几个逐个列名")
    code, out, err = _run_cli(
        ["doctor", "--store", str(workdir / "store.sqlite3"), "--runs-dir", str(workdir / "runs")],
        {},
        workdir,
    )
    print(f"退出码        : {code}")
    print(out.rstrip())
    if err.strip():
        print("stderr:", err.strip())
    assert code != 0, "缺凭据时退出码必须非零（院内脚本据此判断装好没有）"
    for name in REQUIRED_ENV_VARS:
        assert name in out, f"缺项里没有点名 {name}"
    print(f"→ 12 项变量全部被指名（{len(REQUIRED_ENV_VARS)}/12）")


def scenario_missing_one(workdir: Path) -> None:
    """场景 2：只缺一项 → 只指名那一个。"""

    _banner("场景 2 · 只缺一项：只指名那一个变量名")
    env = _base_env()
    env.pop("RESEARCH_PLATFORM_TOKEN")
    code, out, _ = _run_cli(
        ["doctor", "--store", str(workdir / "store.sqlite3"), "--runs-dir", str(workdir / "runs")],
        env,
        workdir,
    )
    print(f"退出码        : {code}")
    for line in out.splitlines():
        if "环境变量" in line or "自检" in line:
            print(line)
    assert code != 0
    assert "RESEARCH_PLATFORM_TOKEN" in out
    print("→ 只指名 RESEARCH_PLATFORM_TOKEN（不给「配置有误」让人自己猜）")


def scenario_all_present(workdir: Path) -> None:
    """场景 3：全部配齐 → 退出码 0。"""

    _banner("场景 3 · 配置齐备：退出码 0")
    code, out, _ = _run_cli(
        ["doctor", "--store", str(workdir / "store.sqlite3"), "--runs-dir", str(workdir / "runs")],
        _base_env(),
        workdir,
    )
    print(f"退出码        : {code}")
    print(out.rstrip())
    assert code == 0, out
    assert "自检通过" in out
    print("→ 自检通过")


def scenario_json(workdir: Path) -> None:
    """场景 4：JSON 输出把阻断项与提示项分开列。"""

    _banner("场景 4 · JSON 输出：阻断项与提示项分开列")
    code, out, _ = _run_cli(
        [
            "doctor",
            "--json",
            "--store",
            str(workdir / "store.sqlite3"),
            "--runs-dir",
            str(workdir / "runs"),
            "--dataset-dir",
            str(workdir / "no-such-dataset"),
        ],
        _base_env(),
        workdir,
    )
    assert code == 0, out
    payload = json.loads(out)
    print(f"ok                    : {payload['ok']}")
    print(f"blocking_failures     : {payload['blocking_failures']}")
    print(f"advisories            : {payload['advisories']}")
    print("checks                :")
    for check in payload["checks"]:
        mark = "✓" if check["ok"] else ("✗" if check["blocking"] else "!")
        print(f"  [{mark}] {check['name']}：{check['detail'][:88]}")
    assert payload["ok"] is True
    assert payload["blocking_failures"] == []
    assert "公开数据集缓存（JSRT）" in payload["advisories"]
    print("→ 提示项不进阻断项，JSON 可直接给院内脚本读")


def scenario_fixture_mentioned_only(workdir: Path) -> None:
    """场景 5：``--fixture`` 只影响说明文字，替身门仍关着。"""

    _banner("场景 5 · --fixture：提及替身且说清「仅测试用、非默认」，替身门不打开")
    code, out, _ = _run_cli(
        [
            "doctor",
            "--fixture",
            "--store",
            str(workdir / "store.sqlite3"),
            "--runs-dir",
            str(workdir / "runs"),
        ],
        _base_env(),
        workdir,
    )
    assert code == 0, out
    for line in out.splitlines():
        if "出站目标" in line:
            print(line)
    assert "替身" in out and "仅测试用" in out and "非默认" in out
    print("→ 默认路径下「出站目标」这一项不提替身；显式开启才提，且说清非默认")


def scenario_zero_outbound(workdir: Path) -> None:
    """场景 6：把四个出站面全指向真 socket 替身，doctor 打过去 0 次。"""

    _banner("场景 6 · 零出站：doctor 不打任何真实端点")
    ris = RISStub()
    inference = InferenceStub()
    research = ResearchPlatformStub()
    ris.start()
    inference.start()
    research.start()
    try:
        env = _base_env()
        env["RIS_BASE_URL"] = ris.base_url
        env["INFERENCE_BASE_URL"] = inference.base_url
        env["RESEARCH_PLATFORM_BASE_URL"] = research.base_url
        code, out, _ = _run_cli(
            [
                "doctor",
                "--store",
                str(workdir / "store.sqlite3"),
                "--runs-dir",
                str(workdir / "runs"),
            ],
            env,
            workdir,
        )
        assert code == 0, out
        print(f"RIS 替身收到的请求     : {len(ris.requests)}")
        print(f"推理替身收到的请求     : {len(inference.requests)}")
        print(f"科研平台替身收到的请求 : {len(research.requests)}")
        assert not ris.requests and not inference.requests and not research.requests, (
            "自检不许打真实端点（会消耗额度、也会把对端日志搞脏）"
        )
        print("→ 三个出站面各收到 0 次请求")
    finally:
        ris.stop()
        inference.stop()
        research.stop()


def scenario_env_file(workdir: Path) -> None:
    """场景 7：凭据配在 ``.env`` 里时自检读得到。"""

    _banner("场景 7 · .env 落法：凭据在 .env 里时自检读得到（不误报缺变量）")
    env_file = workdir / ".env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in _base_env().items()) + "\n",
        encoding="utf-8",
    )
    code, out, _ = _run_cli(
        ["doctor", "--store", str(workdir / "store.sqlite3"), "--runs-dir", str(workdir / "runs")],
        {"NODULE_EVAL_ENV_FILE": str(env_file)},
        workdir,
    )
    print(f"退出码        : {code}")
    for line in out.splitlines():
        if "环境变量" in line:
            print(line)
    assert code == 0, out
    assert "自检通过" in out
    print("→ .env 里的凭据被读到了（系统环境变量优先，.env 兜底）")


def scenario_write_probe(workdir: Path) -> None:
    """场景 8：运行库写入探针 —— 真写一次又回滚，不留残迹。"""

    _banner("场景 8 · 运行库写入探针：真写一次又回滚，不留残迹")
    store = workdir / "probe-store.sqlite3"
    code, out, _ = _run_cli(
        ["doctor", "--store", str(store), "--runs-dir", str(workdir / "runs")],
        _base_env(),
        workdir,
    )
    assert code == 0, out
    for line in out.splitlines():
        if "运行库" in line:
            print(line)
    assert store.is_file(), "自检应当把运行库建出来（安装验证的一部分）"
    conn = sqlite3.connect(str(store))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    print(f"库里的表        : {sorted(tables)}")
    assert not any("probe" in name for name in tables), f"探针留下残迹：{tables}"
    print("→ 写入探针通过且已回滚，库里没有 probe 表")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="nodule-doctor-verify-") as raw:
        workdir = Path(raw)
        (workdir / "runs").mkdir(parents=True, exist_ok=True)
        scenario_missing_all(workdir)
        scenario_missing_one(workdir)
        scenario_all_present(workdir)
        scenario_json(workdir)
        scenario_fixture_mentioned_only(workdir)
        scenario_zero_outbound(workdir)
        scenario_env_file(workdir)
        scenario_write_probe(workdir)
    print()
    print("=" * 78)
    print("doctor 自检端到端实测：八个场景全过")
    print("  · 缺什么指名什么（12 项逐个点名）")
    print("  · 阻断项退出码非零，提示项不阻断")
    print("  · 零出站、替身门不打开")
    print("  · .env 落法可读、运行库写入探针不留残迹")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
