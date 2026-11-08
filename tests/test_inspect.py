"""按调用记录 ID 反查：从结论回到原始那一次调用。

这一组断言回答四个问题 —— 也就是放射科与信息科问「**这例凭什么算漏判**」
时，本产品必须答得上来的四件事：

1. **查得到吗** —— 表格「调用记录ID」列里的那个 id，能不能把那一次调用的
   **入参与返回**原样捞出来，并把这一例的**判定与判定依据**接上；
2. **查不到时说什么** —— id 不存在时**不能返回空结果**（那等于告诉复核的人
   「查过了，没问题」），必须报「未找到该调用记录」并**列出可用的 run_id**，
   让人知道自己查错了哪一轮；
3. **产物缺一半怎么办** —— 调用底账与逐例明细必须**都在**；缺一侧就报
   **产物不完整**，不拿一份看着齐全、其实缺了半边的结果糊弄过去；
4. **核查片段能不能直接往外发** —— 片段里要有结论、依据、原始调用，
   **且不得夹带金标准原文**（原文可能带姓名 / 年龄 / 住院号）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from stub_inference import InferenceStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.audit.inspect import (
    available_run_ids,
    gold_summary_from_labels,
    inspect_tool_call,
    render_inspect,
    render_snippet,
)
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import CaseInput, EvalRequest
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.store import RunStore

#: 端到端样例：金标准写**有**病灶，替身每例回一个候选。
GOLD_POSITIVE = "右下肺见一磨玻璃结节，大小约 8mm。"
GOLD_NEGATIVE = "双肺未见明显结节影。"


def _engine(tmp_path: Path) -> Engine:
    config = load_config()
    spec = load_model_spec("demo-model")
    store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
    engine = Engine(
        config,
        spec,
        client=HttpClient(sleep=lambda _seconds: None),
        runs_dir=tmp_path / "runs",
        store=store,
    )
    engine.test_store = store  # type: ignore[attr-defined]
    return engine


def _run_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """真跑一轮（真 socket 替身 + 真 ``requests.Session``），返回第一个调用 id。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(
            EvalRequest(
                run_id="inspect-e2e",
                model_id="demo-model",
                model_version="v1.0.0",
                dataset_ref="反查链路验收样例",
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
        store = engine.test_store  # type: ignore[attr-defined]
    finally:
        stub.stop()
    store.close()
    assert response.case_rows
    return response.case_rows[0].tool_call_id


# --------------------------------------------------------------------------
# 1. 按 id 命中：入参与返回 + 判定与判定依据
# --------------------------------------------------------------------------


def test_inspect_by_id_echoes_args_and_content(tmp_path, full_env, monkeypatch):
    """查得到：那一次调用的入参与返回原样回显。"""

    call_id = _run_once(tmp_path, monkeypatch)
    result = inspect_tool_call(tmp_path / "runs", call_id)

    assert result.tool_call_id == call_id
    assert result.run_id == "inspect-e2e"
    assert result.accession_no == "ACC-0001"

    args = result.call["args"]
    assert args["accession_no"] == "ACC-0001"
    assert args["study_uid"] == "STUDY-0001"
    assert args["image_ref"] == "dx://local/0001"
    assert args["model_id"] == "demo-model"
    assert args["model_version"] == "v1.0.0"
    assert result.call["name"] == "demo-model.detect"
    assert result.call["content"], "调用返回不能为空 —— 反查就是为了看它"


def test_inspect_joins_verdict_and_judge_basis(tmp_path, full_env, monkeypatch):
    """查得到：这一例的判定与判定依据接得上。"""

    call_id = _run_once(tmp_path, monkeypatch)
    result = inspect_tool_call(tmp_path / "runs", call_id)

    assert result.verdict in {"hit", "miss", "false_positive", "true_negative"}
    assert result.verdict_label, "判定要给中文说法，不是只给一个英文枚举"
    assert result.judge_basis, "判定依据不能为空 —— 「凭什么这么判」就是它"
    assert "存在性" in result.judge_basis


def test_inspect_joins_gold_summary_but_not_the_original_text(
    tmp_path, full_env, monkeypatch
):
    """金标准给的是**归并摘要**与**原文哈希**，不是原文（原文可能带患者信息）。"""

    call_id = _run_once(tmp_path, monkeypatch)
    result = inspect_tool_call(tmp_path / "runs", call_id)

    assert result.gold_summary, "金标准摘要不能为空"
    assert result.gold_standard_sha256, "原文哈希要留着 —— 核对原文时用它对表里那一行"
    # 片段与全文都不得出现金标准原文
    assert GOLD_POSITIVE not in render_inspect(result)
    assert GOLD_POSITIVE not in render_snippet(result)
    assert GOLD_NEGATIVE not in render_inspect(result)


def test_inspect_finds_the_call_across_runs_without_run_id(
    tmp_path, full_env, monkeypatch
):
    """不给 ``--run-id`` 也能查到（调用记录 ID 全局唯一，至多命中一个）。"""

    call_id = _run_once(tmp_path, monkeypatch)
    result = inspect_tool_call(tmp_path / "runs", call_id)
    assert result.run_id == "inspect-e2e"
    assert available_run_ids(tmp_path / "runs") == ("inspect-e2e",)


# --------------------------------------------------------------------------
# 2. 找不到就说找不到
# --------------------------------------------------------------------------


def test_unknown_id_reports_not_found_and_lists_run_ids(tmp_path, full_env, monkeypatch):
    """id 不存在 → 报「未找到该调用记录」并**列出可用的 run_id**。"""

    _run_once(tmp_path, monkeypatch)

    with pytest.raises(NoduleEvalError) as excinfo:
        inspect_tool_call(tmp_path / "runs", "tc_deadbeef")
    exc = excinfo.value

    assert exc.code == ErrorCode.TOOL_CALL_NOT_FOUND
    assert "未找到该调用记录" in exc.message
    assert "tc_deadbeef" in exc.message
    assert "inspect-e2e" in exc.message, "报错要列出可用的 run_id，让人知道自己查错了哪一轮"


def test_unknown_id_does_not_return_an_empty_result(tmp_path, full_env, monkeypatch):
    """查不到**不是**「查过了、没问题」—— 必须抛错，不能返回空结果。"""

    _run_once(tmp_path, monkeypatch)
    with pytest.raises(NoduleEvalError):
        inspect_tool_call(tmp_path / "runs", "tc_not_exists")


def test_empty_tool_call_id_is_a_contract_error(tmp_path, full_env):
    """空 id 直接报契约错（不是「找不到」—— 是压根没给要查什么）。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        inspect_tool_call(tmp_path / "runs", "   ")
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID


def test_missing_run_dir_reports_not_found(tmp_path, full_env, monkeypatch):
    """限定了一个不存在的 run → 同样报找不到，并列出真正可用的 run。"""

    _run_once(tmp_path, monkeypatch)

    with pytest.raises(NoduleEvalError) as excinfo:
        inspect_tool_call(tmp_path / "runs", "tc_whatever", run_id="no-such-run")
    exc = excinfo.value

    assert exc.code == ErrorCode.TOOL_CALL_NOT_FOUND
    assert "inspect-e2e" in exc.message


# --------------------------------------------------------------------------
# 3. 缺一侧就报产物不完整
# --------------------------------------------------------------------------


def test_missing_case_rows_csv_reports_incomplete(tmp_path, full_env, monkeypatch):
    """逐例明细不在 → 反查链路是断的，报产物不完整。"""

    call_id = _run_once(tmp_path, monkeypatch)
    (tmp_path / "runs" / "inspect-e2e" / "case_rows.csv").unlink()

    with pytest.raises(NoduleEvalError) as excinfo:
        inspect_tool_call(tmp_path / "runs", call_id)
    exc = excinfo.value

    assert exc.code == ErrorCode.INSPECT_ARTIFACT_INCOMPLETE
    assert "case_rows.csv" in exc.message
    assert "不完整" in exc.message


def test_missing_tool_calls_jsonl_reports_incomplete(tmp_path, full_env, monkeypatch):
    """调用底账不在 → 同样报产物不完整。"""

    call_id = _run_once(tmp_path, monkeypatch)
    (tmp_path / "runs" / "inspect-e2e" / "tool_calls.jsonl").unlink()

    with pytest.raises(NoduleEvalError) as excinfo:
        inspect_tool_call(tmp_path / "runs", call_id)
    exc = excinfo.value

    assert exc.code == ErrorCode.INSPECT_ARTIFACT_INCOMPLETE
    assert "tool_calls.jsonl" in exc.message


def test_call_without_conclusion_row_reports_incomplete(tmp_path, full_env, monkeypatch):
    """**只有底账、没有结论行** → 对不上账，报产物不完整（不返回半边结果）。"""

    call_id = _run_once(tmp_path, monkeypatch)
    rows_path = tmp_path / "runs" / "inspect-e2e" / "case_rows.csv"
    lines = rows_path.read_text(encoding="utf-8-sig").splitlines()
    rows_path.write_text(
        "\n".join([lines[0]] + lines[2:]) + "\n", encoding="utf-8-sig"
    )

    with pytest.raises(NoduleEvalError) as excinfo:
        inspect_tool_call(tmp_path / "runs", call_id)
    exc = excinfo.value

    assert exc.code == ErrorCode.INSPECT_ARTIFACT_INCOMPLETE
    assert "逐例明细" in exc.message
    assert call_id in exc.message


def test_duplicate_conclusion_rows_report_incomplete(tmp_path, full_env, monkeypatch):
    """同一个调用 id 挂着两行结论 → 对不上账，报产物不完整。"""

    call_id = _run_once(tmp_path, monkeypatch)
    rows_path = tmp_path / "runs" / "inspect-e2e" / "case_rows.csv"
    lines = rows_path.read_text(encoding="utf-8-sig").splitlines()
    rows_path.write_text("\n".join(lines + [lines[1]]) + "\n", encoding="utf-8-sig")

    with pytest.raises(NoduleEvalError) as excinfo:
        inspect_tool_call(tmp_path / "runs", call_id)
    assert excinfo.value.code == ErrorCode.INSPECT_ARTIFACT_INCOMPLETE


def test_missing_case_artifact_degrades_but_still_answers(tmp_path, full_env, monkeypatch):
    """逐例产物不在 → **不报错**（判定与调用仍在），但明说依据不可得。"""

    call_id = _run_once(tmp_path, monkeypatch)
    cases_dir = tmp_path / "runs" / "inspect-e2e" / "cases"
    for child in cases_dir.iterdir():
        for attempt in child.iterdir():
            (attempt / "case.json").unlink()

    result = inspect_tool_call(tmp_path / "runs", call_id)

    assert result.case_artifact_found is False
    assert result.verdict, "判定仍在（来自逐例明细）"
    assert result.call["content"], "调用返回仍在（来自调用底账）"
    text = render_inspect(result)
    assert "不可得" in text, "依据不可得时要**说**，不能空着让人以为没有依据"


# --------------------------------------------------------------------------
# 4. 可复制的核查片段
# --------------------------------------------------------------------------


def test_snippet_carries_conclusion_basis_and_raw_call(tmp_path, full_env, monkeypatch):
    """核查片段：结论 → 依据 → 原始调用，一段话能直接贴进工单。"""

    call_id = _run_once(tmp_path, monkeypatch)
    result = inspect_tool_call(tmp_path / "runs", call_id)
    snippet = render_snippet(result)

    assert snippet.startswith("【肺结节AI模型评估 · 调用核查片段】")
    assert "run_id：inspect-e2e" in snippet
    assert "检查号：ACC-0001" in snippet
    assert f"调用记录ID：{call_id}" in snippet
    assert "判定：" in snippet
    assert "判定依据：" in snippet
    assert "调用入参：" in snippet
    assert "调用返回：" in snippet
    assert "dx://local/0001" in snippet, "入参要能在片段里看见（核对影像引用靠它）"


def test_snippet_is_self_contained_without_leaking_gold_text(
    tmp_path, full_env, monkeypatch
):
    """片段自带上下文（不必再翻别的文件），且不夹带金标准原文。"""

    call_id = _run_once(tmp_path, monkeypatch)
    result = inspect_tool_call(tmp_path / "runs", call_id)
    snippet = render_snippet(result)

    assert GOLD_POSITIVE not in snippet
    assert "姓名" not in snippet
    assert result.gold_standard_sha256 in snippet


def test_gold_summary_from_labels_handles_presence_and_lesions():
    """摘要口径：多处病灶逐处拼；没写属性的写「未写部位 / 尺寸 / 密度」。"""

    assert (
        gold_summary_from_labels(
            {
                "presence": True,
                "lesions": [
                    {"location": "右肺下叶", "size_mm": 8.0, "density": "磨玻璃"},
                    {"location": "左肺上叶", "size_mm": None, "density": None},
                ],
            }
        )
        == "右肺下叶 8.0mm 磨玻璃；左肺上叶"
    )
    assert (
        gold_summary_from_labels(
            {"presence": True, "lesions": [{"location": None, "size_mm": None, "density": None}]}
        )
        == "金标准写明有病灶（未写部位 / 尺寸 / 密度）"
    )
    assert gold_summary_from_labels({"presence": False, "lesions": []}) == (
        "金标准写明未见结节"
    )
    assert gold_summary_from_labels({"presence": None, "lesions": []}) == (
        "金标准的存在性读不出来（需人工复核）"
    )
    assert gold_summary_from_labels(None) == ""


# --------------------------------------------------------------------------
# 5. CLI 端到端
# --------------------------------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    """跑一次真 CLI 进程（子进程里 import 项目自己的包）。"""

    project_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(project_root / "src")
    return subprocess.run(
        [sys.executable, "-m", "nodule_eval.cli", *args],
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_inspect_prints_the_snippet(tmp_path, full_env, monkeypatch):
    """CLI：``inspect --tool-call-id`` 退出码 0，输出里有入参与返回。"""

    call_id = _run_once(tmp_path, monkeypatch)
    proc = _cli(
        "inspect",
        "--tool-call-id",
        call_id,
        "--runs-dir",
        str(tmp_path / "runs"),
    )

    assert proc.returncode == 0, proc.stderr
    assert "ACC-0001" in proc.stdout
    assert "dx://local/0001" in proc.stdout
    assert "调用入参" in proc.stdout
    assert "调用返回" in proc.stdout


def test_cli_inspect_json_output(tmp_path, full_env, monkeypatch):
    """CLI：``--json`` 给表格插件 / 脚本读。"""

    call_id = _run_once(tmp_path, monkeypatch)
    proc = _cli(
        "inspect",
        "--tool-call-id",
        call_id,
        "--runs-dir",
        str(tmp_path / "runs"),
        "--json",
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["tool_call_id"] == call_id
    assert payload["accession_no"] == "ACC-0001"
    assert payload["call"]["args"]["model_id"] == "demo-model"


def test_cli_inspect_unknown_id_exits_with_product_error(tmp_path, full_env, monkeypatch):
    """CLI：查不到 → 退出码 2，错误打在标准错误上（不是静默成功）。"""

    _run_once(tmp_path, monkeypatch)
    proc = _cli(
        "inspect",
        "--tool-call-id",
        "tc_nope",
        "--runs-dir",
        str(tmp_path / "runs"),
    )

    assert proc.returncode == 2
    assert "TOOL_CALL_NOT_FOUND" in proc.stderr
    assert "未找到该调用记录" in proc.stderr
    assert "inspect-e2e" in proc.stderr
