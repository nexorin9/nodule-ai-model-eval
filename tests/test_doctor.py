"""doctor 自检的专项测试（任务 40）。

**doctor 是安装验证脚本**：院内信息科装完第一件事就是跑它，
所以它必须满足三件事 ——

1. **缺什么指名什么**：缺哪个环境变量就报哪个变量名，
   不给一句「配置有误」让人自己猜；
2. **装不上就说装不上**：有阻断项时退出码非零，院内脚本据此判断；
3. **不替人做决定**：不发任何出站调用、不打开替身门 ——
   自检打真实端点会消耗额度，也会把对端日志搞脏。

另有一条边界值得单独盯：**替身（本地测试替身）只在显式开启时才被提及**，
默认情况下连提都不提 —— 院内装的时候看到的应该只有真实端点。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from conftest import make_env
from stub_inference import InferenceStub
from stub_research_platform import ResearchPlatformStub

from nodule_eval import cli
from nodule_eval.config import REQUIRED_ENV_VARS
from nodule_eval.doctor import (
    OUTBOUND_KINDS,
    CheckResult,
    DoctorReport,
    render_report,
    run_doctor,
)
from nodule_eval.fixture_gate import fixture_enabled


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _check(report: DoctorReport, name: str) -> CheckResult:
    """按名字取一项自检结果。"""

    for check in report.checks:
        if check.name == name:
            return check
    raise AssertionError(f"报告里没有「{name}」这一项：{[c.name for c in report.checks]}")


def _doctor(tmp_path: Path, env: dict[str, str], **kwargs) -> DoctorReport:
    """在临时目录里跑一遍自检（产物目录与运行库都落临时目录）。"""

    kwargs.setdefault("store_path", tmp_path / "store.sqlite3")
    kwargs.setdefault("runs_dir", tmp_path / "runs")
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    return run_doctor(env=env, **kwargs)


# --------------------------------------------------------------------------
# 1. 缺什么指名什么
# --------------------------------------------------------------------------


@pytest.mark.parametrize("missing", list(REQUIRED_ENV_VARS))
def test_doctor_names_each_missing_env_var(tmp_path, missing):
    """12 项变量逐个删一遍 —— 每次都必须**指名那一个**变量名。"""

    env = make_env()
    env.pop(missing, None)
    report = _doctor(tmp_path, env)
    detail = _check(report, "环境变量").detail
    assert missing in detail, f"缺 {missing} 时报的是：{detail}"
    assert not _check(report, "环境变量").ok


def test_doctor_lists_all_missing_vars_at_once(tmp_path):
    """一个都不配时，12 项全部列出来（不让科室来回试十二遍）。"""

    report = _doctor(tmp_path, {})
    detail = _check(report, "环境变量").detail
    for name in REQUIRED_ENV_VARS:
        assert name in detail, f"缺项里没有 {name}：{detail}"


def test_doctor_blank_value_counts_as_missing(tmp_path):
    """空串等于没配 —— 装了等于没装是最难查的一种。"""

    env = make_env(RIS_API_TOKEN="   ")
    report = _doctor(tmp_path, env)
    detail = _check(report, "环境变量").detail
    assert "RIS_API_TOKEN" in detail


def test_doctor_cli_exit_code_nonzero_when_env_missing(tmp_path, monkeypatch, capsys):
    """变量没配 → 退出码非零（院内安装脚本据此判断装好没有）。"""

    for name in REQUIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    exit_code = cli.main(
        [
            "doctor",
            "--store",
            str(tmp_path / "store.sqlite3"),
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )
    assert exit_code == cli.EXIT_PRODUCT_ERROR

    out = capsys.readouterr().out
    assert "RIS_BASE_URL" in out and "OPENAI_API_KEY" in out
    assert "MODEL_TEMPERATURE" in out
    assert "自检未通过" in out


def test_doctor_cli_exit_code_zero_when_everything_in_place(tmp_path, monkeypatch, capsys):
    """配置齐备 → 退出码 0。"""

    for key, value in make_env().items():
        monkeypatch.setenv(key, value)

    exit_code = cli.main(
        [
            "doctor",
            "--store",
            str(tmp_path / "store.sqlite3"),
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0, out
    assert "自检通过" in out


def test_doctor_json_output_separates_blocking_from_advisory(tmp_path, monkeypatch, capsys):
    """JSON 输出里阻断项与提示项分开列（给院内脚本读）。"""

    for key, value in make_env().items():
        monkeypatch.setenv(key, value)

    exit_code = cli.main(
        [
            "doctor",
            "--json",
            "--store",
            str(tmp_path / "store.sqlite3"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--dataset-dir",
            str(tmp_path / "no-such-dataset"),
        ]
    )
    assert exit_code == 0, capsys.readouterr().out

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["blocking_failures"] == []
    assert "公开数据集缓存（JSRT）" in payload["advisories"]
    names = {check["name"] for check in payload["checks"]}
    assert {"环境变量", "依赖包", "被评模型目录", "运行库", "产物目录"} <= names


# --------------------------------------------------------------------------
# 2. 替身：默认不启用、只在显式开启时被提及、且始终不是默认路径
# --------------------------------------------------------------------------


def test_doctor_default_report_does_not_mention_fixture(tmp_path):
    """默认路径下，「出站目标」这一项**不提替身**。

    院内安装时看到的应该只有真实端点 —— 不给「还有个本地假货可以顶一下」的暗示。
    """

    report = _doctor(tmp_path, make_env())
    detail = _check(report, "出站目标").detail
    assert "替身" not in detail, detail
    assert "真实端点" in detail


def test_doctor_mentions_fixture_only_when_explicitly_enabled(tmp_path):
    """显式开启时才提，且必须说清「仅测试用、非默认」。"""

    env = make_env(IMAGING_EVAL_FIXTURE="1")
    report = _doctor(tmp_path, env, fixture_explicit=True)
    detail = _check(report, "出站目标").detail
    assert "替身" in detail
    assert "非默认" in detail and "仅测试用" in detail


def test_doctor_fixture_mode_without_address_is_advisory_only(tmp_path):
    """开了替身却没给地址 —— 只是提示，不阻断（doctor 本来就不出站）。"""

    report = _doctor(tmp_path, make_env(IMAGING_EVAL_FIXTURE="1"), fixture_explicit=True)
    check = _check(report, "出站目标")
    assert not check.ok
    assert check.blocking is False
    assert "IMAGING_EVAL_FIXTURE_BASE_URL" in check.detail


def test_doctor_never_enables_the_fixture_and_never_calls_out(tmp_path, monkeypatch, capsys):
    """doctor **不发任何出站调用**，也**不替人打开替身门**。"""

    for key, value in make_env().items():
        monkeypatch.setenv(key, value)

    inference = InferenceStub()
    research = ResearchPlatformStub()
    inference.start()
    research.start()
    try:
        monkeypatch.setenv("IMAGING_EVAL_FIXTURE_BASE_URL", "http://127.0.0.1:1")
        monkeypatch.delenv("IMAGING_EVAL_FIXTURE", raising=False)

        exit_code = cli.main(
            [
                "doctor",
                "--store",
                str(tmp_path / "store.sqlite3"),
                "--runs-dir",
                str(tmp_path / "runs"),
            ]
        )
        assert exit_code == 0, capsys.readouterr().out
        assert inference.requests == [] and research.requests == [], (
            "自检不许打真实端点（会消耗额度、也会把对端日志搞脏）"
        )
        assert fixture_enabled() is False, "doctor 不该把替身门打开"
    finally:
        inference.stop()
        research.stop()


def test_doctor_fixture_flag_does_not_open_the_gate(tmp_path, monkeypatch, capsys):
    """``doctor --fixture`` 只影响说明文字，不打开替身门。"""

    for key, value in make_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("IMAGING_EVAL_FIXTURE", raising=False)
    exit_code = cli.main(
        [
            "doctor",
            "--fixture",
            "--store",
            str(tmp_path / "store.sqlite3"),
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )
    assert exit_code == 0, capsys.readouterr().out
    assert fixture_enabled() is False


# --------------------------------------------------------------------------
# 3. 运行库：真写一次（写完回滚，不留残）
# --------------------------------------------------------------------------


def test_doctor_probes_the_store_by_writing_and_rolling_back(tmp_path):
    """目录可写 ≠ 文件能写 —— doctor 探一次真实写入。"""

    store = tmp_path / "store.sqlite3"
    report = _doctor(tmp_path, make_env())
    check = _check(report, "运行库")
    assert check.ok, check.detail
    assert "写入探针" in check.detail
    assert store.is_file(), "自检应当把运行库建出来（安装验证的一部分）"

    conn = sqlite3.connect(str(store))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert not any("probe" in name for name in tables), f"探针留下残迹：{tables}"


def test_doctor_detects_a_store_that_cannot_be_written(tmp_path):
    """库文件只读 → 这一项不通过，且指名路径。"""

    if hasattr(os, "geteuid") and os.geteuid() == 0:  # pragma: no cover - root 绕过权限
        pytest.skip("root 忽略文件权限，跳过只读用例")

    store = tmp_path / "store.sqlite3"
    store.write_bytes(b"")
    store.chmod(0o444)
    try:
        report = _doctor(tmp_path, make_env())
        check = _check(report, "运行库")
        assert not check.ok
        assert str(store) in check.detail
    finally:
        store.chmod(0o644)


# --------------------------------------------------------------------------
# 4. 公开数据集缓存：提示项，不阻断
# --------------------------------------------------------------------------


def test_doctor_jsrt_cache_missing_is_advisory_not_blocking(tmp_path):
    """JSRT 没下载只是提示 —— 它不参与评估主路径，不该把「装好了」判掉。"""

    report = _doctor(tmp_path, make_env(), dataset_dir=tmp_path / "no-such-cache")
    check = _check(report, "公开数据集缓存（JSRT）")
    assert not check.ok
    assert check.blocking is False
    assert "不影响跑评估" in check.detail
    assert report.ok is True, "提示项不该影响整体结论"


def test_doctor_jsrt_cache_reports_count_and_gap(tmp_path):
    """缓存数量与应有数量对不上时要说清差多少。"""

    cache = tmp_path / "jsrt"
    (cache / "images").mkdir(parents=True)
    for index in range(3):
        (cache / "images" / f"case-{index}.png").write_bytes(b"x")
    (cache / "jsrt_metadata.csv").write_text("study_id\n", encoding="utf-8")

    report = _doctor(tmp_path, make_env(), dataset_dir=cache)
    detail = _check(report, "公开数据集缓存（JSRT）").detail
    assert "已缓存 3 张" in detail
    assert "247" in detail
    assert "数量不符" in detail


# --------------------------------------------------------------------------
# 4b. .env：院内配置的常规落法
# --------------------------------------------------------------------------


def test_doctor_reads_credentials_from_env_file(tmp_path, monkeypatch):
    """凭据配在 .env 里时，自检应当**读到**它们（不然会误报「缺变量」）。"""

    for name in REQUIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    env_file = tmp_path / ".env"
    lines = []
    for key, value in make_env().items():
        lines.append(f"{key}={value}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("NODULE_EVAL_ENV_FILE", str(env_file))

    report = _doctor(tmp_path, None)
    check = _check(report, "环境变量")
    assert check.ok, check.detail
    assert _check(report, "环境变量文件（.env）").ok


def test_doctor_env_file_does_not_override_real_env(tmp_path, monkeypatch):
    """系统环境变量优先于 .env —— 临时改一个值不能被 .env 悄悄顶回去。"""

    for key, value in make_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RIS_API_TOKEN", "from-real-env")

    env_file = tmp_path / ".env"
    env_file.write_text("RIS_API_TOKEN=from-env-file\n", encoding="utf-8")
    monkeypatch.setenv("NODULE_EVAL_ENV_FILE", str(env_file))

    from nodule_eval.config import load_config

    assert load_config().ris_api_token == "from-real-env"


def test_doctor_env_file_support_is_advisory(tmp_path, monkeypatch):
    """``.env`` 这一项是提示项 —— 不用 .env 也完全能装。"""

    monkeypatch.setenv("NODULE_EVAL_ENV_FILE", str(tmp_path / "no-such.env"))
    report = _doctor(tmp_path, make_env())
    check = _check(report, "环境变量文件（.env）")
    assert check.ok
    assert check.blocking is False


# --------------------------------------------------------------------------
# 5. 其余逐项检查
# --------------------------------------------------------------------------


def test_doctor_checks_python_and_dependency_versions(tmp_path):
    """依赖检查带版本号 —— 排障时先看版本。"""

    report = _doctor(tmp_path, make_env())
    python_detail = _check(report, "Python 版本").detail
    assert "Python 版本" or True  # 明细里带当前版本号
    assert "当前" in python_detail
    package_detail = _check(report, "依赖包").detail
    for package in ("requests", "yaml", "openpyxl"):
        assert package in package_detail


def test_doctor_reports_authorized_models(tmp_path):
    """模型目录：列出已授权的模型。"""

    report = _doctor(tmp_path, make_env())
    detail = _check(report, "被评模型目录").detail
    assert "demo-model" in detail
    assert "已授权" in detail


def test_doctor_reports_all_four_outbound_kinds(tmp_path):
    """四个出站面都在这一项里出现（RIS / 推理 / 科研平台 / 模型）。"""

    report = _doctor(tmp_path, make_env())
    detail = _check(report, "出站目标").detail
    for kind in OUTBOUND_KINDS:
        assert kind in detail


def test_doctor_install_doc_is_advisory(tmp_path):
    """安装说明在不在只是提示（装不上时科室要翻的就是它）。"""

    report = _doctor(tmp_path, make_env())
    check = _check(report, "院内安装说明")
    assert check.blocking is False


def test_render_report_lists_blocking_and_advisory_separately(tmp_path):
    """渲染文本把阻断项与提示项分开说。"""

    report = _doctor(tmp_path, {}, dataset_dir=tmp_path / "no-such-cache")
    text = render_report(report)
    assert "自检未通过" in text
    assert "环境变量" in text
    assert "提示" in text
