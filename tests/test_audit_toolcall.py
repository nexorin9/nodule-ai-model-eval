"""调用底账（``runs/<run_id>/tool_calls.jsonl``）：并发不交错、坏行可跳过、id 唯一。

这一组断言回答四个问题：

1. **并发写会不会把底账写花** —— 逐例 fan-out 是多线程跑的（任务 15），
   十几个线程同时追加，行与行**不能互相咬合**（不能出现半行 + 半行拼成一行）；
2. **一行坏数据会不会带塌整份底账** —— 底账是唯一能说清「这例凭什么算漏判」
   的东西，坏行必须跳过**且计数上报**，不能读完悄悄少几行还让人以为账是全的；
3. **id 规则稳不稳** —— ``tool_call_id`` 全局唯一，同一例重跑生成**新** id，
   旧 id 不复用、旧记录不被覆盖；
4. **看一行底账知不知道发生了什么** —— ``name`` 是「模型标识 + 动作名」，
   ``args`` 落齐五项入参，字段集与源产品 ``ToolCallLog`` 一致。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from stub_inference import InferenceStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.audit.toolcall import (
    CALL_ARG_FIELDS,
    TOOL_CALL_FIELDS,
    ToolCallRecorder,
    build_call_args,
    build_call_name,
    missing_call_arg_fields,
    new_tool_call_id,
)
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import CaseInput, EvalRequest
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.store import RunStore

#: 并发写用的线程数与每线程行数（合计 200 行）。
THREADS = 8
PER_THREAD = 25


def _recorder(tmp_path: Path) -> ToolCallRecorder:
    return ToolCallRecorder(tmp_path / "run-001")


def _lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------
# 1. 并发写不交错
# --------------------------------------------------------------------------


def test_concurrent_appends_keep_every_line_intact(tmp_path):
    """八个线程同时追加：行数一条不少，每行都是完整的一行 JSON。"""

    recorder = _recorder(tmp_path)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for item in range(PER_THREAD):
                recorder.record(
                    name="demo-model.detect",
                    args={
                        "accession_no": f"ACC-{index:02d}-{item:03d}",
                        "study_uid": f"STUDY-{index:02d}-{item:03d}",
                        "image_ref": f"dx://local/{index:02d}/{item:03d}",
                        "model_id": "demo-model",
                        "model_version": "v1.0.0",
                    },
                    content=f"payload-{index:02d}-{item:03d}",
                )
        except BaseException as exc:  # pragma: no cover - 只在失败时出现
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"并发写不该抛异常：{errors}"
    lines = _lines(recorder.path)
    assert len(lines) == THREADS * PER_THREAD, "并发写不能丢行"

    # 每行都能单独解析 —— 半行咬合在这一步就会暴露。
    records = [json.loads(line) for line in lines]
    assert len(records) == THREADS * PER_THREAD


def test_concurrent_appends_do_not_mix_two_rows_into_one(tmp_path):
    """并发写不能把两行的内容拼成一行（内容与该行自己的入参一一对应）。"""

    recorder = _recorder(tmp_path)

    def worker(index: int) -> None:
        for item in range(PER_THREAD):
            recorder.record(
                name="demo-model.detect",
                args={
                    "accession_no": f"ACC-{index:02d}-{item:03d}",
                    "study_uid": f"STUDY-{index:02d}-{item:03d}",
                    "image_ref": f"dx://local/{index:02d}/{item:03d}",
                    "model_id": "demo-model",
                    "model_version": "v1.0.0",
                },
                content=f"content-{index:02d}-{item:03d}",
            )

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    records = [json.loads(line) for line in _lines(recorder.path)]
    assert len(records) == THREADS * PER_THREAD
    for record in records:
        accession = record["args"]["accession_no"]
        expected = f"content-{accession.split('-')[1]}-{accession.split('-')[2]}"
        assert record["content"] == expected, "一行底账的内容不能是另一行拼上来的"
    # id 逐行唯一
    ids = [record["tool_call_id"] for record in records]
    assert len(set(ids)) == len(ids)


def test_ids_are_unique_under_concurrency(tmp_path):
    """并发下生成的 id 互不重复（否则反查会张冠李戴）。"""

    recorder = _recorder(tmp_path)
    ids: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        local = [new_tool_call_id() for _ in range(50)]
        with lock:
            ids.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(ids) == 400
    assert len(set(ids)) == 400


# --------------------------------------------------------------------------
# 2. 坏行可跳过且计数上报
# --------------------------------------------------------------------------


def test_corrupt_line_is_skipped_and_counted(tmp_path):
    """一行坏 JSON 不影响其余行；坏行数**看得见**，不静默丢。"""

    recorder = _recorder(tmp_path)
    for index in range(3):
        recorder.record(
            name="demo-model.detect",
            args={
                "accession_no": f"ACC-{index:04d}",
                "study_uid": f"STUDY-{index:04d}",
                "image_ref": f"dx://local/{index:04d}",
                "model_id": "demo-model",
                "model_version": "v1.0.0",
            },
            content=f"payload-{index:04d}",
        )

    # 手工插一行坏数据（模拟写了一半就断电 / 外部工具改坏了文件）。
    with open(recorder.path, "a", encoding="utf-8") as handle:
        handle.write('{"tool_call_id": "tc_broken", "name": "demo-mod\n')

    recorder.record(
        name="demo-model.detect",
        args={
            "accession_no": "ACC-9999",
            "study_uid": "STUDY-9999",
            "image_ref": "dx://local/9999",
            "model_id": "demo-model",
            "model_version": "v1.0.0",
        },
        content="payload-9999",
    )

    records = recorder.read_all()
    assert len(records) == 4, "坏行跳过后，其余四行必须一条不少"
    assert [record["args"]["accession_no"] for record in records] == [
        "ACC-0000",
        "ACC-0001",
        "ACC-0002",
        "ACC-9999",
    ]

    scan = recorder.scan()
    assert scan.corrupt_lines == 1
    assert scan.total_lines == 5
    assert len(scan.records) == 4


def test_line_missing_required_field_counts_as_corrupt(tmp_path):
    """少字段的行也算坏行 —— 半条底账不能当完整记录用。"""

    recorder = _recorder(tmp_path)
    recorder.record(
        name="demo-model.detect",
        args={
            "accession_no": "ACC-0001",
            "study_uid": "STUDY-0001",
            "image_ref": "dx://local/0001",
            "model_id": "demo-model",
            "model_version": "v1.0.0",
        },
        content="payload",
    )
    with open(recorder.path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tool_call_id": "tc_x", "name": "demo-model.detect"}) + "\n")

    scan = recorder.scan()
    assert scan.corrupt_lines == 1
    assert len(scan.records) == 1


def test_blank_lines_are_counted_but_not_treated_as_records(tmp_path):
    """空行不占记录数，但计数上报。"""

    recorder = _recorder(tmp_path)
    recorder.record(
        name="demo-model.detect",
        args={
            "accession_no": "ACC-0001",
            "study_uid": "STUDY-0001",
            "image_ref": "dx://local/0001",
            "model_id": "demo-model",
            "model_version": "v1.0.0",
        },
        content="payload",
    )
    with open(recorder.path, "a", encoding="utf-8") as handle:
        handle.write("\n")

    scan = recorder.scan()
    assert scan.blank_lines == 1
    assert len(scan.records) == 1


# --------------------------------------------------------------------------
# 3. id 规则
# --------------------------------------------------------------------------


def test_tool_call_id_shape_and_uniqueness():
    """id 形如 ``tc_<32 位十六进制>``，多次生成互不重复。"""

    ids = {new_tool_call_id() for _ in range(2000)}
    assert len(ids) == 2000
    for call_id in list(ids)[:20]:
        assert call_id.startswith("tc_")
        assert len(call_id) == 35
        assert all(char in "0123456789abcdef" for char in call_id[3:])


def test_rerun_same_case_gets_a_new_id_and_keeps_the_old_record(tmp_path):
    """同一例重跑生成**新** id；旧 id 仍能反查到旧记录（旧产物不被覆盖）。"""

    recorder = _recorder(tmp_path)
    first = recorder.record(
        name="demo-model.detect",
        args={
            "accession_no": "ACC-0001",
            "study_uid": "STUDY-0001",
            "image_ref": "dx://local/0001",
            "model_id": "demo-model",
            "model_version": "v1.0.0",
        },
        content="第一轮返回",
    )
    second = recorder.record(
        name="demo-model.detect",
        args={
            "accession_no": "ACC-0001",
            "study_uid": "STUDY-0001",
            "image_ref": "dx://local/0001",
            "model_id": "demo-model",
            "model_version": "v1.0.0",
        },
        content="第二轮返回",
    )

    assert first.tool_call_id != second.tool_call_id
    assert recorder.find(first.tool_call_id)["content"] == "第一轮返回"
    assert recorder.find(second.tool_call_id)["content"] == "第二轮返回"
    assert recorder.find("tc_not_exists") is None
    assert recorder.find("") is None


# --------------------------------------------------------------------------
# 4. 一行底账读不读得懂
# --------------------------------------------------------------------------


def test_record_line_carries_the_source_five_fields(tmp_path):
    """字段集与源产品 ``ToolCallLog`` 一致：五个字段，一个不多一个不少。"""

    recorder = _recorder(tmp_path)
    record = recorder.record(
        name="demo-model.detect",
        args={
            "accession_no": "ACC-0001",
            "study_uid": "STUDY-0001",
            "image_ref": "dx://local/0001",
            "model_id": "demo-model",
            "model_version": "v1.0.0",
        },
        content="payload",
    )
    payload = json.loads(_lines(recorder.path)[0])
    assert set(payload) == set(TOOL_CALL_FIELDS)
    assert payload["tool_call_id"] == record.tool_call_id
    assert payload["name"] == "demo-model.detect"
    assert payload["timestamp"]


def test_call_name_is_model_plus_action():
    """``name`` = 被评模型标识 + 动作名；缺模型标识直接报契约错。"""

    assert build_call_name("demo-model", "detect") == "demo-model.detect"
    assert build_call_name("second-model", "detect") == "second-model.detect"

    with pytest.raises(NoduleEvalError) as excinfo:
        build_call_name("", "detect")
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "model_id" in excinfo.value.detail

    with pytest.raises(NoduleEvalError) as excinfo:
        build_call_name("demo-model", "")
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID


def test_call_args_shape_is_fixed_and_complete():
    """``args`` 固定五项；缺哪项体检能点出名字。"""

    args = build_call_args(
        accession_no="ACC-0001",
        study_uid="STUDY-0001",
        image_ref="dx://local/0001",
        model_id="demo-model",
        model_version="v1.0.0",
    )
    assert list(args) == list(CALL_ARG_FIELDS)
    assert args["accession_no"] == "ACC-0001"
    assert args["model_version"] == "v1.0.0"
    assert missing_call_arg_fields(args) == []

    partial = build_call_args(
        accession_no="ACC-0001",
        study_uid="STUDY-0001",
        image_ref=None,
        model_id="demo-model",
        model_version="v1.0.0",
    )
    assert partial["image_ref"] == ""
    assert missing_call_arg_fields(partial) == ["image_ref"]


# --------------------------------------------------------------------------
# 5. 端到端：引擎真跑一次，底账长什么样
# --------------------------------------------------------------------------


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


def test_engine_writes_readable_name_and_args(tmp_path, full_env, monkeypatch):
    """真跑一次：底账那行的 name / args 人一眼看得懂。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(
            EvalRequest(
                run_id="ledger-e2e",
                model_id="demo-model",
                model_version="v1.0.0",
                dataset_ref="底账可读性验收样例",
                cases=(
                    CaseInput(
                        accession_no="ACC-0001",
                        gold_standard_text="右下肺见一磨玻璃结节，大小约 8mm。",
                        image_ref="dx://local/0001",
                        study_uid="STUDY-0001",
                        row_no=1,
                    ),
                ),
            )
        )
        store = engine.test_store  # type: ignore[attr-defined]
    finally:
        stub.stop()

    records = [
        json.loads(line)
        for line in _lines(tmp_path / "runs" / "ledger-e2e" / "tool_calls.jsonl")
    ]
    assert len(records) == 1
    record = records[0]
    assert set(record) == set(TOOL_CALL_FIELDS)
    assert record["name"] == "demo-model.detect"
    assert record["args"]["accession_no"] == "ACC-0001"
    assert record["args"]["study_uid"] == "STUDY-0001"
    assert record["args"]["image_ref"] == "dx://local/0001"
    assert record["args"]["model_id"] == "demo-model"
    assert record["args"]["model_version"] == "v1.0.0"
    assert record["tool_call_id"] == response.case_rows[0].tool_call_id

    # 挂了 run store 时走 store 的登记口：库里的调用索引与底账 id 一一对应。
    assert store.count_tool_calls("ledger-e2e") == 1
    store.close()


def test_two_processes_never_corrupt_each_other(tmp_path):
    """两个进程各写一半：底账仍然是两堆完整行（跨进程也不咬合）。"""

    import subprocess
    import sys

    recorder = _recorder(tmp_path)
    script = (
        "import sys;"
        "sys.path.insert(0, %r);" % str(Path(__file__).resolve().parents[1] / "src")
        + "from nodule_eval.audit.toolcall import ToolCallRecorder;"
        "r = ToolCallRecorder(%r);" % str(recorder.path.parent)
        + "[r.record(name='demo-model.detect', args={'accession_no': 'ACC-%04d' % i,"
        " 'study_uid': 'S', 'image_ref': 'd', 'model_id': 'demo-model',"
        " 'model_version': 'v1'}, content='c-%04d' % i) for i in range(60)]"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(2)
    ]
    for proc in procs:
        _out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err.decode("utf-8", "replace")

    lines = _lines(recorder.path)
    assert len(lines) == 120
    records = [json.loads(line) for line in lines]
    assert len({record["tool_call_id"] for record in records}) == 120
    contents = [record["content"] for record in records]
    assert len(contents) == 120
    assert set(contents) == {f"c-{index:04d}" for index in range(60)}, "内容不能被别的行拼上来"
    assert all(content.count("-") == 1 for content in contents), "一行内容必须来自一次调用"
