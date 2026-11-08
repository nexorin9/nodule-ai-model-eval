"""CLI 子命令族（任务 39）。

CLI 是**运维与排障通道**，不是本产品的首要入口（首要入口是表格内插件）。
这一组断言盯住三件事：

1. **子命令族齐了、人都会用** —— 每个子命令 ``--help`` 可用；
2. **缺参数时给的是中文指名提示**，不是 argparse 的英文
   ``the following arguments are required``（院里信息科的人看不懂要补哪个）；
3. **几个硬边界在 CLI 上也没破**：
   写回闸门（未复核 → 一次出站都不发）、幂等回放（重复提交只登记一条）、
   导出件不落金标准原文。

**doctor 的专项断言在 ``tests/test_doctor.py``**（任务 40 从这里拆出去单独成文件）。

**断言走的是真实协议**：科研平台与推理端点都是真监听 socket 的 HTTP 服务，
产品的 ``requests.Session`` / URL 拼接 / Bearer 头 / ``Idempotency-Key`` 一行都没改。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_env
from stub_inference import InferenceStub
from stub_research_platform import ResearchPlatformStub

from nodule_eval import cli
from nodule_eval.errors import ErrorCode
from nodule_eval.export import EXPORT_CASE_COLUMNS
from nodule_eval.store import RUN_STATUS_EVALUATED, RUN_STATUS_SUBMITTED

#: 子命令族 —— 产品入口是表格内插件，CLI 这一族是运维与排障通道。
SUBCOMMANDS: tuple[str, ...] = (
    "run",
    "assemble",
    "submit",
    "inspect",
    "replay",
    "doctor",
    "export",
    "redact-scan",
)

#: 放射科写的金标准原文（含中文标点与换行）。
GOLD_TEXT = "右下肺见一磨玻璃结节，大小约 8mm。"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _cases_payload(
    *,
    run_id: str = "cli-001",
    count: int = 2,
    gold_text: str = GOLD_TEXT,
) -> dict:
    """造一份完整请求（每例自带影像标识，不必回读 RIS）。"""

    return {
        "run_id": run_id,
        "model_id": "demo-model",
        "model_version": "v1.0.0",
        "dataset_ref": "CLI 样例（胸片肺结节）",
        "cases": [
            {
                "accession_no": f"ACC-{index:03d}",
                "gold_standard_text": gold_text,
                "image_ref": f"dx://local/{index:03d}",
                "study_uid": f"STUDY-{index:03d}",
                "row_no": index + 2,
            }
            for index in range(count)
        ],
    }


def _write_cases(tmp_path: Path, payload: dict, name: str = "cases.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _fixture_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    inference: InferenceStub | None = None,
    research: ResearchPlatformStub | None = None,
) -> None:
    """把四个出站面都接到替身上（替身门要求「全开或全关」，不能半开）。"""

    unified = (research or inference).base_url if (research or inference) else "https://x.invalid"
    env = make_env(
        IMAGING_EVAL_FIXTURE="1",
        IMAGING_EVAL_FIXTURE_BASE_URL=unified,
    )
    if inference is not None:
        env["IMAGING_EVAL_FIXTURE_INFERENCE_BASE_URL"] = inference.base_url
        env["INFERENCE_BASE_URL"] = inference.base_url
    if research is not None:
        env["IMAGING_EVAL_FIXTURE_RESEARCH_BASE_URL"] = research.base_url
        env["RESEARCH_PLATFORM_BASE_URL"] = research.base_url
    for key, value in env.items():
        monkeypatch.setenv(key, value)


# --------------------------------------------------------------------------
# 1. 子命令族齐了
# --------------------------------------------------------------------------


@pytest.mark.parametrize("command", SUBCOMMANDS)
def test_every_subcommand_has_usable_help(command: str, capsys):
    """每个子命令 ``--help`` 都能用，且**说清自己是干什么的**。"""

    with pytest.raises(SystemExit) as excinfo:
        cli.main([command, "--help"])
    assert excinfo.value.code == 0, f"{command} --help 应当正常退出"

    out = capsys.readouterr().out
    assert command in out, f"{command} 的帮助里没出现子命令名"
    assert "usage:" in out
    assert len(out.strip()) > 40, f"{command} 的帮助太空了，等于没写"


def test_top_level_help_lists_the_whole_family(capsys):
    """顶层帮助里能看到整族子命令。"""

    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for command in SUBCOMMANDS:
        assert command in out, f"顶层帮助里少了 {command}"


# --------------------------------------------------------------------------
# 2. 缺参数 → 中文指名提示（不是 argparse 的英文）
# --------------------------------------------------------------------------


def test_run_without_cases_reports_contract_invalid(capsys):
    """``run`` 缺病例输入文件 —— 报错要指名 ``--cases``。"""

    exit_code = cli.main(["run", "--model-id", "demo-model"])
    assert exit_code == cli.EXIT_PRODUCT_ERROR

    err = capsys.readouterr().err
    assert ErrorCode.CONTRACT_INVALID.value in err
    assert "--cases" in err, "报错要指名该补哪个参数"
    assert "--request" in err, "要同时说明另一个等价入口"


def test_run_without_model_id_reports_contract_invalid(tmp_path, capsys):
    """``run`` 缺 ``--model-id``（且请求文件里也没给）—— 报错要指名。"""

    cases_file = _write_cases(tmp_path, {"cases": _cases_payload()["cases"]})
    exit_code = cli.main(["run", "--cases", str(cases_file)])
    assert exit_code == cli.EXIT_PRODUCT_ERROR

    err = capsys.readouterr().err
    assert ErrorCode.CONTRACT_INVALID.value in err
    assert "--model-id" in err


def test_run_rejects_both_cases_and_request(tmp_path, capsys):
    """``--cases`` 与 ``--request`` 同时给 —— 口径打架，当场报错。"""

    cases_file = _write_cases(tmp_path, _cases_payload())
    request_file = _write_cases(tmp_path, _cases_payload(), name="request.json")
    exit_code = cli.main(
        [
            "run",
            "--cases",
            str(cases_file),
            "--request",
            str(request_file),
            "--model-id",
            "demo-model",
        ]
    )
    assert exit_code == cli.EXIT_PRODUCT_ERROR
    assert "只能给一个" in capsys.readouterr().err


def test_request_entry_rejects_a_bare_case_array(tmp_path, capsys):
    """``--request`` 只收完整请求 —— 给成病例数组要报错并指向 ``--cases``。"""

    array_file = tmp_path / "array.json"
    array_file.write_text(
        json.dumps(_cases_payload()["cases"], ensure_ascii=False),
        encoding="utf-8",
    )
    exit_code = cli.main(
        ["run", "--request", str(array_file), "--model-id", "demo-model"]
    )
    assert exit_code == cli.EXIT_PRODUCT_ERROR

    err = capsys.readouterr().err
    assert ErrorCode.CONTRACT_INVALID.value in err
    assert "--request" in err and "--cases" in err


def test_no_model_gateway_conflicts_with_merge_model(tmp_path, monkeypatch, full_env, capsys):
    """``--no-model-gateway`` 与 ``--merge model`` 打架 —— 不猜哪个优先。"""

    monkeypatch.setenv("IMAGING_EVAL_FIXTURE", "1")
    monkeypatch.setenv("IMAGING_EVAL_FIXTURE_BASE_URL", "https://stub.invalid")
    cases_file = _write_cases(tmp_path, _cases_payload())

    exit_code = cli.main(
        [
            "run",
            "--cases",
            str(cases_file),
            "--model-id",
            "demo-model",
            "--merge",
            "model",
            "--no-model-gateway",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--store",
            str(tmp_path / "store.sqlite3"),
        ]
    )
    assert exit_code == cli.EXIT_PRODUCT_ERROR
    err = capsys.readouterr().err
    assert "--no-model-gateway" in err and "--merge model" in err


def test_export_rejects_an_unknown_format(tmp_path, capsys):
    """导出格式写错 —— 当场报错，不静默挑一个。"""

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["export", "--run-id", "x", "--format", "pdf"])
    assert excinfo.value.code != 0
    assert "pdf" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 3. run 端到端 + 归并口径
# --------------------------------------------------------------------------


def test_run_end_to_end_then_export_submit_and_replay(tmp_path, monkeypatch, capsys):
    """跑一轮 → 导出结论表 → 人工复核后写回 → 回放回执。

    一条命令走完产品主路径的**四段**：评估 → 导出 → 写回 → 核对。
    """

    inference = InferenceStub()
    research = ResearchPlatformStub()
    inference.start()
    research.start()
    try:
        _fixture_env(monkeypatch, inference=inference, research=research)
        cases_file = _write_cases(tmp_path, _cases_payload(run_id="cli-e2e-001"))
        store_path = str(tmp_path / "store.sqlite3")
        runs_dir = str(tmp_path / "runs")

        # ---- ① 跑一轮 ----
        exit_code = cli.main(
            [
                "run",
                "--cases",
                str(cases_file),
                "--model-id",
                "demo-model",
                "--run-id",
                "cli-e2e-001",
                "--runs-dir",
                runs_dir,
                "--store",
                store_path,
                "--no-model-gateway",
            ]
        )
        assert exit_code == 0, capsys.readouterr().out
        out = capsys.readouterr().out
        assert "归并口径" in out and "规则基线" in out, "要说清这一轮走的哪条归并口径"
        assert len(inference.requests) == 2, "两例各调一次推理端点"

        # ---- ② 导出结论表（不依赖宏）----
        export_dir = tmp_path / "export"
        exit_code = cli.main(
            [
                "export",
                "--run-id",
                "cli-e2e-001",
                "--runs-dir",
                runs_dir,
                "--store",
                store_path,
                "--out-dir",
                str(export_dir),
            ]
        )
        assert exit_code == 0
        workbook = export_dir / "评估结论_cli-e2e-001.xlsx"
        missed_csv = export_dir / "漏诊清单_cli-e2e-001.csv"
        cases_csv = export_dir / "逐例明细_cli-e2e-001.csv"
        assert workbook.is_file() and missed_csv.is_file() and cases_csv.is_file()

        # ---- ③ 未复核就写回：闸门拦下，**一次出站都不发** ----
        exit_code = cli.main(
            [
                "submit",
                "--run-id",
                "cli-e2e-001",
                "--runs-dir",
                runs_dir,
                "--store",
                store_path,
            ]
        )
        assert exit_code == cli.EXIT_PRODUCT_ERROR
        err = capsys.readouterr().err
        assert ErrorCode.WRITEBACK_NOT_REVIEWED.value in err
        assert len(research.requests) == 0, "闸门拒绝时一次出站调用都不该发"

        # ---- ④ 人工复核后写回：真出站，登记一条 ----
        exit_code = cli.main(
            [
                "submit",
                "--run-id",
                "cli-e2e-001",
                "--review",
                "--runs-dir",
                runs_dir,
                "--store",
                store_path,
            ]
        )
        assert exit_code == 0, capsys.readouterr().out
        out = capsys.readouterr().out
        assert "已写回" in out and "登记号" in out
        posts = [r for r in research.requests if r["method"] == "POST"]
        assert len(posts) == 1, "写回一次只登记一条"
        assert posts[0]["path"] == "/api/v1/model-evaluation-runs"
        assert posts[0]["headers"].get("Idempotency-Key"), "写回必须带幂等键"

        # ---- ⑤ 再写回一次：幂等回放，对端不多一条登记 ----
        exit_code = cli.main(
            [
                "submit",
                "--run-id",
                "cli-e2e-001",
                "--runs-dir",
                runs_dir,
                "--store",
                store_path,
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "幂等回放" in out
        # **「已写回」与「闸门拦下」在终端上必须分得开** ——
        # 前者是流程走完了，后者是没复核就想写出去。
        assert "本轮已写回" in out and "拦下" not in out
        posts = [r for r in research.requests if r["method"] == "POST"]
        assert len(posts) == 1, "重复提交不该多出一条登记"
        assert len(research.registrations) == 1

        # ---- ⑥ 回放回执（默认零出站）----
        exit_code = cli.main(
            [
                "replay",
                "--run-id",
                "cli-e2e-001",
                "--runs-dir",
                runs_dir,
                "--store",
                store_path,
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "本地回执状态" in out and "registered" in out
        assert len(research.requests) == 1, "回放默认零出站"

        # ---- ⑦ 回放并核对（一次 GET，不发 POST）----
        exit_code = cli.main(
            [
                "replay",
                "--run-id",
                "cli-e2e-001",
                "--verify",
                "--runs-dir",
                runs_dir,
                "--store",
                store_path,
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "登记确实生效了" in out
        gets = [r for r in research.requests if r["method"] == "GET"]
        assert len(gets) == 1, "核对只发一次 GET"
    finally:
        inference.stop()
        research.stop()


def test_replay_without_a_receipt_says_so(tmp_path, monkeypatch, full_env, capsys):
    """没提交过就回放 —— 报错要说清「压根没提交过」，不返回空结果。"""

    from nodule_eval.store import RunStore

    store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
    store.upsert_run(
        run_id="never-submitted",
        dataset_ref="CLI 样例（胸片肺结节）",
        model_id="demo-model",
        model_version="v1.0.0",
        status=RUN_STATUS_EVALUATED,
    )
    store.close()

    exit_code = cli.main(
        [
            "replay",
            "--run-id",
            "never-submitted",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--store",
            str(tmp_path / "store.sqlite3"),
        ]
    )
    assert exit_code == cli.EXIT_PRODUCT_ERROR
    err = capsys.readouterr().err
    assert ErrorCode.CONTRACT_INVALID.value in err
    assert "没有写回回执" in err


def test_submit_dry_run_never_leaves_the_hospital(tmp_path, monkeypatch, capsys):
    """``--dry-run`` 只核对口径，**一次出站都不发**。"""

    inference = InferenceStub()
    research = ResearchPlatformStub()
    inference.start()
    research.start()
    try:
        _fixture_env(monkeypatch, inference=inference, research=research)
        cases_file = _write_cases(tmp_path, _cases_payload(run_id="cli-dry-001"))
        store_path = str(tmp_path / "store.sqlite3")
        runs_dir = str(tmp_path / "runs")
        cli.main(
            [
                "run",
                "--cases",
                str(cases_file),
                "--model-id",
                "demo-model",
                "--run-id",
                "cli-dry-001",
                "--runs-dir",
                runs_dir,
                "--store",
                store_path,
            ]
        )
        capsys.readouterr()

        exit_code = cli.main(
            [
                "submit",
                "--run-id",
                "cli-dry-001",
                "--review",
                "--dry-run",
                "--runs-dir",
                runs_dir,
                "--store",
                store_path,
            ]
        )
        assert exit_code == 0, capsys.readouterr().out
        out = capsys.readouterr().out
        assert "干跑模式" in out and "幂等键" in out
        assert len(research.requests) == 0, "干跑不许发出站调用"
    finally:
        inference.stop()
        research.stop()


# --------------------------------------------------------------------------
# 4. 导出件：不落金标准原文
# --------------------------------------------------------------------------


def test_export_never_writes_the_gold_standard_text(tmp_path, monkeypatch, capsys):
    """导出件里**没有金标准原文** —— 结论是要往外发的东西。"""

    inference = InferenceStub()
    inference.start()
    try:
        _fixture_env(monkeypatch, inference=inference)
        cases_file = _write_cases(tmp_path, _cases_payload(run_id="cli-export-001"))
        store_path = str(tmp_path / "store.sqlite3")
        runs_dir = str(tmp_path / "runs")
        cli.main(
            [
                "run",
                "--cases",
                str(cases_file),
                "--model-id",
                "demo-model",
                "--run-id",
                "cli-export-001",
                "--runs-dir",
                runs_dir,
                "--store",
                store_path,
            ]
        )
        capsys.readouterr()

        export_dir = tmp_path / "export"
        exit_code = cli.main(
            [
                "export",
                "--run-id",
                "cli-export-001",
                "--runs-dir",
                runs_dir,
                "--store",
                store_path,
                "--out-dir",
                str(export_dir),
            ]
        )
        assert exit_code == 0
    finally:
        inference.stop()

    # CSV 里既没有原文，也没有原文那一列。
    cases_csv = (export_dir / "逐例明细_cli-export-001.csv").read_text(encoding="utf-8-sig")
    assert "gold_standard_text" not in cases_csv
    assert GOLD_TEXT not in cases_csv
    assert "gold_summary" in cases_csv, "给的是归并后的结构化摘要"
    assert "右肺下叶" in cases_csv

    # 工作簿里同样没有原文。
    from openpyxl import load_workbook

    workbook = load_workbook(export_dir / "评估结论_cli-export-001.xlsx")
    assert set(workbook.sheetnames) >= {"评估结论", "逐例明细", "漏诊清单", "字段说明"}
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows(values_only=True):
            for value in row:
                assert value is None or GOLD_TEXT not in str(value), (
                    f"工作簿 {sheet.title} 里出现了金标准原文"
                )

    # 导出的列与契约一致（不各写一份）。
    header = cases_csv.splitlines()[0].split(",")
    assert header == list(EXPORT_CASE_COLUMNS)


def test_export_says_so_when_the_run_is_unknown(tmp_path, capsys):
    """库里没有这一轮 —— 指名报错，不导出一份空表。"""

    exit_code = cli.main(
        [
            "export",
            "--run-id",
            "no-such-run",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--store",
            str(tmp_path / "store.sqlite3"),
        ]
    )
    assert exit_code == cli.EXIT_PRODUCT_ERROR
    assert "没有这一轮" in capsys.readouterr().err
