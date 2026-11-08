"""提示词外置与版本化（任务 47）。

归并口径是会变的 —— 换个写法读法，同一批例子的结论就可能不一样；
而模型归并的结论**直接进评估结论**（敏感度、特异度从它算出来）。
口径变了却说不清「这批结论是哪版口径跑的」，评估结论就不可追、不可复现。

这一组断言覆盖四件事：

1. **提示词住在文件里、版本号住在文件名里** —— 改口径就另存一版；
2. **提示词文件的哈希进模型调用底账** —— 凭哈希反查回是哪一版；
3. **提示词与输出契约对得上** —— 提示词声明的字段 = 契约里的字段；
   对不上（提示词教模型输出契约外的字段 / 契约必填字段提示词没声明）
   → 载入即报错，**不回落内置兜底提示词**；
4. **安全边界落到代码** —— 提示词必须写明「不改写原文」「不输出诊断」；
   而模型输出里真的冒出诊断结论类字段 → **判解析失败**（走规则降级），
   光在提示词里写一句「不许输出诊断」是不够的。

外加一条端到端：真 CLI 跑一轮 ``--merge model``，
底账那一行的 ``prompt_sha256`` 必须**等于提示词文件的实际哈希**。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from conftest import make_env
from stub_inference import InferenceStub
from stub_model import ModelStub

from nodule_eval.contracts import (
    MERGE_FORBIDDEN_OUTPUT_FIELDS,
    MERGE_LESION_FIELDS,
    MERGE_OUTPUT_FIELDS,
    MERGE_REQUIRED_FIELDS,
)
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.goldstd.merge import (
    MERGE_OUTPUT_FIELDS as MERGE_MODULE_OUTPUT_FIELDS,
)
from nodule_eval.goldstd.merge import (
    build_merge_prompt,
    merge_by_model,
    parse_merge_output,
)
from nodule_eval.goldstd.prompt import (
    DEFAULT_PROMPT_PATH,
    PROMPT_SAFETY_CLAUSES,
    PromptBundle,
    assert_prompt_matches_contract,
    assert_prompt_safety,
    extract_json_example,
    load_prompt_bundle,
)
from nodule_eval.audit.modelcall import (
    PromptRef,
    build_prompt_index,
    hash_prompt_file,
    parse_prompt_version,
    resolve_prompt_version,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = PROJECT_ROOT / "prompts"

#: 一段真实的胸片金标准原文（属性齐全）。
GOLD_TEXT = "右下肺见一磨玻璃结节，最大径约8mm，边界欠清。"

#: 模型对上面这段的归并结果（字段齐全）。
GOOD_PAYLOAD: dict[str, object] = {
    "presence": True,
    "location": "右肺下叶",
    "size_mm": 8.0,
    "density": "磨玻璃",
    "evidence_span": "右下肺见一磨玻璃结节，最大径约8mm",
    "confidence": 0.92,
    "needs_human_review": False,
}


# --------------------------------------------------------------------------
# 1. 提示词外置 + 版本号
# --------------------------------------------------------------------------


def test_prompt_lives_in_a_versioned_file():
    """提示词住在 ``prompts/`` 下的独立文件里，文件名带版本号。"""

    assert DEFAULT_PROMPT_PATH.is_file(), "语义归并提示词必须外置成独立文件"
    assert DEFAULT_PROMPT_PATH.parent == PROMPTS_DIR
    assert DEFAULT_PROMPT_PATH.name == "goldstd_merge.v1.md"


def test_prompt_version_is_read_from_the_file_name():
    """**版本号住在文件名里** —— 改名即换版。"""

    assert parse_prompt_version(Path("prompts/goldstd_merge.v1.md")) == "v1"
    assert parse_prompt_version(Path("prompts/goldstd_merge.v12.md")) == "v12"
    assert parse_prompt_version(Path("prompts/goldstd_merge.md")) == "", (
        "文件名里没有版本号就返回空串，不猜是哪一版"
    )


def test_prompt_bundle_reports_version_and_file_hash():
    """载入后拿得到**版本号**与**文件内容哈希**。"""

    bundle = load_prompt_bundle()
    assert isinstance(bundle, PromptBundle)
    assert bundle.name == "goldstd_merge"
    assert bundle.version == "v1"
    assert bundle.relative_path == "prompts/goldstd_merge.v1.md"
    assert bundle.sha256 == hash_prompt_file(DEFAULT_PROMPT_PATH)
    assert bundle.sha256 == hashlib.sha256(
        DEFAULT_PROMPT_PATH.read_bytes()
    ).hexdigest()


def test_renaming_the_prompt_file_changes_the_version_but_not_the_hash(tmp_path):
    """**改名即换版**：版本号变、文件内容哈希不变（内容没动过）。"""

    source = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")
    v1 = tmp_path / "goldstd_merge.v1.md"
    v2 = tmp_path / "goldstd_merge.v2.md"
    v1.write_text(source, encoding="utf-8")
    v2.write_text(source, encoding="utf-8")

    bundle_v1 = load_prompt_bundle(v1)
    bundle_v2 = load_prompt_bundle(v2)

    assert bundle_v1.version == "v1"
    assert bundle_v2.version == "v2"
    assert bundle_v1.sha256 == bundle_v2.sha256, "内容没改，哈希就不该变"


def test_prompt_file_without_a_version_is_rejected(tmp_path):
    """文件名不带版本号 → 载入即报错（口径查不出来就不许跑）。"""

    unversioned = tmp_path / "goldstd_merge.md"
    shutil.copyfile(DEFAULT_PROMPT_PATH, unversioned)

    with pytest.raises(NoduleEvalError) as excinfo:
        load_prompt_bundle(unversioned)
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "版本号" in excinfo.value.detail


def test_missing_prompt_file_raises_instead_of_falling_back(tmp_path):
    """提示词文件读不到 → 报错，**不回落内置兜底提示词**。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        load_prompt_bundle(tmp_path / "no-such-prompt.md")
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "提示词文件不存在" in excinfo.value.detail


# --------------------------------------------------------------------------
# 2. 提示词 ↔ 输出契约 一致性
# --------------------------------------------------------------------------


def test_output_contract_lives_in_contracts_and_merge_re_exports_it():
    """输出契约的**唯一真源在 contracts.py**，归并模块只是转出。"""

    assert MERGE_MODULE_OUTPUT_FIELDS is MERGE_OUTPUT_FIELDS
    assert MERGE_OUTPUT_FIELDS == (
        "presence",
        "location",
        "size_mm",
        "density",
        "evidence_span",
        "confidence",
        "needs_human_review",
    )
    assert set(MERGE_REQUIRED_FIELDS).issubset(MERGE_OUTPUT_FIELDS)


def test_prompt_example_declares_exactly_the_contract_fields():
    """提示词示例里声明的字段 = 契约里的字段（不多不少）。"""

    bundle = load_prompt_bundle()
    example = extract_json_example(bundle.template_text)

    unknown = set(example) - set(MERGE_OUTPUT_FIELDS) - {"lesions"}
    assert not unknown, f"提示词示例里有契约之外的字段：{sorted(unknown)}"
    missing = [name for name in MERGE_REQUIRED_FIELDS if name not in example]
    assert not missing, f"提示词示例缺必填字段：{missing}"


def test_prompt_declaring_a_field_outside_the_contract_is_rejected(tmp_path):
    """提示词教模型输出契约外的字段（如 ``diagnosis``）→ 载入即报错。"""

    text = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").replace(
        '"presence": true,', '"presence": true,\n  "diagnosis": "右下肺结节",', 1
    )
    broken = tmp_path / "goldstd_merge.v1.md"
    broken.write_text(text, encoding="utf-8")

    with pytest.raises(NoduleEvalError) as excinfo:
        load_prompt_bundle(broken)
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "diagnosis" in excinfo.value.detail
    assert "输出契约之外" in excinfo.value.detail


def test_prompt_missing_a_required_field_is_rejected(tmp_path):
    """契约里的必填字段提示词没声明 → 载入即报错（模型不会输出，每次都解析失败）。"""

    text = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")
    # 把示例里的 ``evidence_span`` 整行去掉（回指锚点是必填的）。
    lines = [
        line
        for line in text.splitlines()
        if '"evidence_span": "右下肺见一磨玻璃结节，最大径约8mm",' not in line
    ]
    broken = tmp_path / "goldstd_merge.v1.md"
    broken.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(NoduleEvalError) as excinfo:
        load_prompt_bundle(broken)
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "evidence_span" in excinfo.value.detail
    assert "缺必填字段" in excinfo.value.detail


def test_prompt_lesion_entry_outside_the_contract_is_rejected(tmp_path):
    """多病灶条目里出现契约外的字段 → 载入即报错。"""

    text = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").replace(
        '"evidence_span": "右下肺见一磨玻璃结节，最大径约8mm"\n    }',
        '"evidence_span": "右下肺见一磨玻璃结节，最大径约8mm",\n      "malignancy": "低危"\n    }',
        1,
    )
    broken = tmp_path / "goldstd_merge.v1.md"
    broken.write_text(text, encoding="utf-8")

    with pytest.raises(NoduleEvalError) as excinfo:
        load_prompt_bundle(broken)
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "malignancy" in excinfo.value.detail


def test_prompt_without_a_json_example_is_rejected(tmp_path):
    """提示词里没有 JSON 输出示例 → 载入即报错（字段无从核对）。"""

    import re

    text = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")
    # 只挖掉 JSON 示例那一段（占位符等其余部分留着，
    # 否则先报的是「缺占位符」，测不到这条校验）。
    stripped = re.sub(r"```json.*?```", "", text, flags=re.DOTALL)
    broken = tmp_path / "goldstd_merge.v1.md"
    broken.write_text(stripped, encoding="utf-8")

    with pytest.raises(NoduleEvalError) as excinfo:
        load_prompt_bundle(broken)
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "JSON 输出示例" in excinfo.value.detail


def test_contract_check_reports_the_allowed_fields():
    """校验报错时要说清**合法字段有哪些**（改提示词的人才知道怎么改）。"""

    text = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").replace(
        '"presence": true,', '"presence": true,\n  "stage": "T1",', 1
    )
    with pytest.raises(NoduleEvalError) as excinfo:
        assert_prompt_matches_contract(text)
    detail = excinfo.value.detail
    assert "stage" in detail
    assert "evidence_span" in detail, "报错要列出合法字段"


# --------------------------------------------------------------------------
# 3. 安全边界：提示词写明 + 代码校验
# --------------------------------------------------------------------------


def test_shipped_prompt_declares_both_safety_clauses():
    """出厂提示词必须写明**两条安全条款**。"""

    text = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")
    for markers in PROMPT_SAFETY_CLAUSES.values():
        assert any(marker in text for marker in markers), f"缺安全条款：{markers}"


def test_prompt_missing_a_safety_clause_is_rejected(tmp_path):
    """安全条款缺一条 → 载入即报错（口径文件本身不能把边界丢了）。"""

    text = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")
    for markers in PROMPT_SAFETY_CLAUSES.values():
        for marker in markers:
            text = text.replace(marker, "（此条略）")
    broken = tmp_path / "goldstd_merge.v1.md"
    broken.write_text(text, encoding="utf-8")

    with pytest.raises(NoduleEvalError) as excinfo:
        load_prompt_bundle(broken)
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "安全条款" in excinfo.value.detail


def test_safety_check_names_the_missing_clause():
    """报错要指名缺的是哪一条，不是一句「不合规」。"""

    text = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").replace(
        "不改写原文", "（此条略）"
    )
    with pytest.raises(NoduleEvalError) as excinfo:
        assert_prompt_safety(text)
    assert "不改写原文" in excinfo.value.detail


@pytest.mark.parametrize("field", ["diagnosis", "impression", "recommendation", "诊断"])
def test_model_output_carrying_a_diagnosis_field_is_a_parse_failure(field):
    """**光在提示词里写「不许输出诊断」是不够的** —— 代码得拦。"""

    payload = dict(GOOD_PAYLOAD)
    payload[field] = "右下肺磨玻璃结节，建议三个月复查"

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_merge_output(json.dumps(payload, ensure_ascii=False))
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert field in excinfo.value.detail
    assert "诊断" in excinfo.value.detail


def test_forbidden_output_fields_are_declared_in_contracts():
    """禁字段清单在 contracts.py 里（唯一真源）。"""

    assert "diagnosis" in MERGE_FORBIDDEN_OUTPUT_FIELDS
    assert "impression" in MERGE_FORBIDDEN_OUTPUT_FIELDS
    assert "诊断" in MERGE_FORBIDDEN_OUTPUT_FIELDS
    # 禁字段**不能**与合法字段重叠。
    assert not set(MERGE_FORBIDDEN_OUTPUT_FIELDS) & set(MERGE_OUTPUT_FIELDS)


def test_diagnosis_field_output_degrades_to_the_rule_baseline():
    """模型带诊断字段 → **降级到规则基线**，不是把这一例判失败。"""

    class _StubGateway:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        def complete(self, *, system, user, json_schema=None):
            self.calls.append({"system": system, "user": user})
            payload = dict(GOOD_PAYLOAD)
            payload["diagnosis"] = "右下肺磨玻璃结节"
            return json.dumps(payload, ensure_ascii=False)

    result = merge_by_model(GOLD_TEXT, gateway=_StubGateway())

    assert result.degraded is True
    assert result.labels.source == "rule"
    assert result.labels.degraded is True
    assert "diagnosis" in result.reason
    # 降级例仍有结论（规则基线读得出来），不是「没结论」。
    assert result.labels.presence is True


def test_normal_output_has_no_diagnosis_field():
    """正常输出不受影响（禁字段校验不误伤）。"""

    parsed = parse_merge_output(json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    assert parsed.presence is True
    assert parsed.location == "右肺下叶"
    assert parsed.size_mm == 8.0


# --------------------------------------------------------------------------
# 4. 提示词哈希 → 模型调用底账
# --------------------------------------------------------------------------


def test_prompt_ref_carries_path_version_and_hash():
    """提示词引用 = 路径 + 版本号 + 内容哈希。"""

    ref = PromptRef.from_file(DEFAULT_PROMPT_PATH)
    assert ref.path == "prompts/goldstd_merge.v1.md"
    assert ref.version == "v1"
    assert ref.sha256 == hash_prompt_file(DEFAULT_PROMPT_PATH)


def test_prompt_hash_resolves_back_to_the_file():
    """凭哈希能反查回 ``prompts/`` 下的具体那一版。"""

    index = build_prompt_index(PROMPTS_DIR)
    bundle = load_prompt_bundle()
    assert resolve_prompt_version(bundle.sha256, index) == "prompts/goldstd_merge.v1.md"
    assert resolve_prompt_version("0" * 64, index) == "", "查不到不猜是哪一版"


def test_ledger_prompt_hash_equals_the_prompt_file_hash(tmp_path):
    """底账那一行的 ``prompt_sha256`` = **提示词文件的实际哈希**。"""

    from nodule_eval.audit.modelcall import ModelCallRecorder

    recorder = ModelCallRecorder(tmp_path)
    recorder.record(
        meta={
            "tool_call_id": "tc_" + "a" * 32,
            "model": "nodule-goldstd-merge",
            "base_url_host": "llm.test-hospital.internal",
            "started_at": "2026-11-08T09:30:00",
            "latency_ms": 812,
            "prompt_tokens": 128,
            "completion_tokens": 64,
            "prompt_sha256": hashlib.sha256(b"rendered").hexdigest(),
            "raw_response_sha256": hashlib.sha256(b"response").hexdigest(),
            "fallback_used": False,
        },
        tool_call_id="tc_" + "a" * 32,
        prompt_ref=PromptRef.from_file(DEFAULT_PROMPT_PATH),
    )

    row = json.loads(recorder.path.read_text(encoding="utf-8").splitlines()[0])
    file_hash = hash_prompt_file(DEFAULT_PROMPT_PATH)
    assert row["prompt_sha256"] == file_hash, "底账里的提示词哈希要是文件哈希"
    assert row["prompt_template_sha256"] == file_hash
    assert row["prompt_version"] == "v1"
    assert row["prompt_template"] == "prompts/goldstd_merge.v1.md"
    # 实际发出去的那份提示词的哈希另有一栏（两者分工不同）。
    assert row["prompt_rendered_sha256"] == hashlib.sha256(b"rendered").hexdigest()


# --------------------------------------------------------------------------
# 5. 端到端：真 CLI 跑一轮 --merge model
# --------------------------------------------------------------------------


def test_cli_run_records_the_prompt_file_hash_in_the_ledger(tmp_path, monkeypatch):
    """真 CLI 进程 + 真 socket 替身 → 底账里的提示词哈希对得上文件。"""

    from nodule_eval import cli

    inference_stub = InferenceStub()
    model_stub = ModelStub(content=json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    inference_stub.start()
    model_stub.start()
    try:
        env = make_env(
            INFERENCE_BASE_URL=inference_stub.base_url,
            OPENAI_BASE_URL=model_stub.base_url,
        )
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        cases_path = tmp_path / "cases.json"
        cases_path.write_text(
            json.dumps(
                {
                    "run_id": "prompt-version-e2e",
                    "model_id": "demo-model",
                    "model_version": "v1.0.0",
                    "dataset_ref": "提示词版本验收",
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

        exit_code = cli.main(
            [
                "run",
                "--cases",
                str(cases_path),
                "--model-id",
                "demo-model",
                "--merge",
                "model",
                "--run-id",
                "prompt-version-e2e",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--store",
                str(tmp_path / "store.sqlite3"),
            ]
        )
    finally:
        inference_stub.stop()
        model_stub.stop()

    assert exit_code == 0

    ledger = tmp_path / "runs" / "prompt-version-e2e" / "model_calls.jsonl"
    rows = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    row = rows[0]
    file_hash = hash_prompt_file(DEFAULT_PROMPT_PATH)
    assert row["prompt_sha256"] == file_hash, "底账里的提示词哈希 = 文件实际哈希"
    assert row["prompt_version"] == "v1"
    assert row["prompt_template"] == "prompts/goldstd_merge.v1.md"
    assert resolve_prompt_version(row["prompt_sha256"], build_prompt_index(PROMPTS_DIR)) == (
        "prompts/goldstd_merge.v1.md"
    )


def test_cli_terminal_prints_the_prompt_version(tmp_path, monkeypatch, capsys):
    """终端**直接说清**「这批结论是哪版口径跑的」（不用翻底账才知道）。"""

    from nodule_eval import cli

    inference_stub = InferenceStub()
    model_stub = ModelStub(content=json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    inference_stub.start()
    model_stub.start()
    try:
        env = make_env(
            INFERENCE_BASE_URL=inference_stub.base_url,
            OPENAI_BASE_URL=model_stub.base_url,
        )
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        cases_path = tmp_path / "cases.json"
        cases_path.write_text(
            json.dumps(
                {
                    "run_id": "prompt-version-terminal",
                    "model_id": "demo-model",
                    "model_version": "v1.0.0",
                    "dataset_ref": "提示词版本终端输出验收",
                    "cases": [
                        {
                            "accession_no": "ACC-PV-0002",
                            "gold_standard_text": GOLD_TEXT,
                            "image_ref": "dx://local/0002",
                            "study_uid": "STUDY-PV-0002",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        exit_code = cli.main(
            [
                "run",
                "--cases",
                str(cases_path),
                "--model-id",
                "demo-model",
                "--merge",
                "model",
                "--run-id",
                "prompt-version-terminal",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--store",
                str(tmp_path / "store.sqlite3"),
            ]
        )
    finally:
        inference_stub.stop()
        model_stub.stop()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "归并口径版本" in out, "终端没有说清这批结论是哪版口径跑的"
    assert "v1" in out
    assert "prompts/goldstd_merge.v1.md" in out
    file_hash = hash_prompt_file(DEFAULT_PROMPT_PATH)
    assert file_hash[:12] in out, "终端那一行要带文件哈希（凭它可反查回那一版）"


def test_cli_terminal_omits_the_prompt_version_for_the_rule_baseline(
    tmp_path, monkeypatch, capsys
):
    """走规则基线时**不打印**那一行（没调模型就没有口径版本，别塞占位）。"""

    from nodule_eval import cli

    inference_stub = InferenceStub()
    inference_stub.start()
    try:
        env = make_env(INFERENCE_BASE_URL=inference_stub.base_url)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        cases_path = tmp_path / "cases.json"
        cases_path.write_text(
            json.dumps(
                {
                    "run_id": "prompt-version-rule",
                    "model_id": "demo-model",
                    "model_version": "v1.0.0",
                    "dataset_ref": "规则基线终端输出验收",
                    "cases": [
                        {
                            "accession_no": "ACC-PV-0003",
                            "gold_standard_text": GOLD_TEXT,
                            "image_ref": "dx://local/0003",
                            "study_uid": "STUDY-PV-0003",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        exit_code = cli.main(
            [
                "run",
                "--cases",
                str(cases_path),
                "--model-id",
                "demo-model",
                "--merge",
                "rule",
                "--run-id",
                "prompt-version-rule",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--store",
                str(tmp_path / "store.sqlite3"),
            ]
        )
    finally:
        inference_stub.stop()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "归并口径版本" not in out, "规则基线没有提示词版本，别打印那一行"


def test_build_merge_prompt_renders_the_label_table_from_the_equivalence_table():
    """提示词渲染后标签表已填好 —— 标签表换了不用改提示词。"""

    prompt = build_merge_prompt()
    from nodule_eval.goldstd.labels import canonical_densities, canonical_locations

    for label in canonical_locations():
        assert label in prompt
    for label in canonical_densities():
        assert label in prompt
    assert "{{LOCATIONS}}" not in prompt
    assert "{{DENSITIES}}" not in prompt


def test_lesion_fields_are_declared_in_the_contract():
    """多病灶条目的合法字段也在契约里（提示词示例逐字对得上）。"""

    assert MERGE_LESION_FIELDS == ("location", "size_mm", "density", "evidence_span")
    bundle = load_prompt_bundle()
    example = extract_json_example(bundle.template_text)
    lesions = example.get("lesions")
    assert isinstance(lesions, list) and lesions
    for item in lesions:
        assert set(item) == set(MERGE_LESION_FIELDS)
