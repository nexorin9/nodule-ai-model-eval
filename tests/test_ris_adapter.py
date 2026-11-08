"""RIS 读适配器：URL / 鉴权头 / 游标翻页 / 字段级校验 / 失败不静默。

RIS 是本产品的**权威数据源**：检查号 → 影像标识的绑定关系只认它。这一组测试
盯住四件事：

1. **实际发出去的请求** —— URL 路径、查询参数、``Authorization`` 头、翻页序列，
   全部对着替身（真 socket、真 ``requests.Session``）断言，不 mock 掉 HTTP 层；
2. **字段级校验** —— ``accession_no`` / ``study_uid`` 必填非空，缺一条就判失败
   （丢一条 = 凭空少一例样本）；
3. **模态过滤与计数** —— 非 DX 记录直接过滤**并计数**；``image_ref`` 缺失留空，
   不瞎编引用；
4. **失败不静默** —— 拉不到就是 ``RIS_READ_FAILED``，错误信息带**页码与游标**，
   绝不返回空清单当作「没有病例」。

**源产品证据**：参考实现的病例清单抓取是「翻页取号 + 每页条数对不上就告警」
（``github_ref/MedRAX/data/get_cases.py:29-38``）。本产品把同一机制改写到 RIS 的
游标翻页上，并把「告警」升级为**可断言的计数**。
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import make_env
from stub_ris import DEFAULT_ITEM, RISStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.adapters.ris import RISAdapter
from nodule_eval.config import load_config
from nodule_eval.errors import ErrorCode, NoduleEvalError


def _adapter(stub: RISStub, **kwargs: Any) -> RISAdapter:
    """对着替身造一个 RIS 适配器（退避不真睡，测试要快）。"""

    env = make_env(RIS_BASE_URL=stub.base_url)
    config = load_config(env)
    client = HttpClient(sleep=lambda _s: None)
    return RISAdapter(config, client=client, **kwargs)


def _item(accession_no: Any = "ACC-001", **overrides: Any) -> dict[str, Any]:
    """一条 DX 检查记录（可覆写任一字段）。"""

    payload = dict(DEFAULT_ITEM)
    payload.update(overrides)
    payload["accession_no"] = accession_no
    return payload


# --------------------------------------------------------------------------
# 1. 实际发出去的请求
# --------------------------------------------------------------------------


def test_request_url_query_and_auth_header_are_the_spec_ones():
    """URL 路径、查询参数、鉴权头必须与 ``spec.md`` 的字段规范一致。"""

    stub = RISStub(items=[_item("ACC-001")])
    stub.start()
    try:
        adapter = _adapter(stub)
        adapter.fetch_studies(
            modality="DX", date_from="2026-03-01", date_to="2026-03-31"
        )
    finally:
        stub.stop()

    assert len(stub.requests) == 1
    request = stub.requests[0]
    assert request["method"] == "GET"
    assert request["path"] == "/api/v1/imaging-studies"
    assert request["query"]["modality"] == "DX"
    assert request["query"]["from"] == "2026-03-01"
    assert request["query"]["to"] == "2026-03-31"
    assert request["headers"]["Authorization"] == "Bearer test-ris-token"
    # 凭据只来自环境变量 —— 请求体里不夹带任何密钥字段。
    assert request["body"] is None


def test_cursor_pagination_walks_every_page_and_stops_on_empty_cursor():
    """翻页请求序列：首页不带 cursor，之后每页带上上一页返回的游标。"""

    items = [_item("ACC-001"), _item("ACC-002"), _item("ACC-003")]
    stub = RISStub(items=items, page_size=1)
    stub.start()
    try:
        adapter = _adapter(stub)
        result = adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    assert len(stub.requests) == 3
    assert "cursor" not in stub.requests[0]["query"], "首页不该带游标"
    assert stub.requests[1]["query"]["cursor"] == "1"
    assert stub.requests[2]["query"]["cursor"] == "2"

    assert result.pages == 3
    assert result.total_count == 3
    assert [item.accession_no for item in result.items] == ["ACC-001", "ACC-002", "ACC-003"]


def test_pagination_stops_when_cursor_is_empty():
    """对端不再给游标就停 —— 不多打一次空请求。"""

    stub = RISStub(items=[_item("ACC-001"), _item("ACC-002")])
    stub.start()
    try:
        adapter = _adapter(stub)
        result = adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    assert len(stub.requests) == 1
    assert result.pages == 1
    assert result.total_count == 2


# --------------------------------------------------------------------------
# 2. 字段级校验
# --------------------------------------------------------------------------


@pytest.mark.parametrize("missing_field", ["accession_no", "study_uid"])
def test_missing_required_field_fails_the_read(missing_field: str):
    """必填字段缺失 —— 判 ``RIS_READ_FAILED``，写明缺哪个字段，不静默丢行。"""

    bad = _item("ACC-001")
    bad.pop(missing_field)
    stub = RISStub(items=[bad])
    stub.start()
    try:
        adapter = _adapter(stub)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.RIS_READ_FAILED
    assert missing_field in err.message, "错误信息须写明缺哪个字段"
    assert "第 1 页" in err.message, "错误信息须带页位置"
    assert err.counts_toward_metrics is False


def test_blank_required_field_is_rejected_too():
    """空白字符串同样算缺失 —— 「有值但没内容」不是有效检查号。"""

    stub = RISStub(items=[_item("ACC-001", study_uid="   ")])
    stub.start()
    try:
        adapter = _adapter(stub)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.RIS_READ_FAILED
    assert "study_uid" in excinfo.value.message


def test_numeric_accession_no_is_coerced_instead_of_rejected():
    """对端把检查号以 JSON 数字返回 —— 归一成字符串，不误判成缺字段。"""

    stub = RISStub(items=[_item(accession_no=2026030001)])
    stub.start()
    try:
        adapter = _adapter(stub)
        result = adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    assert result.total_count == 1
    assert result.items[0].accession_no == "2026030001"


def test_missing_image_ref_is_left_empty_not_faked():
    """``image_ref`` 缺失时留空并计数 —— 后续按 ``accession_no`` 从 RIS 兜底取。"""

    stub = RISStub(items=[_item("ACC-001", image_ref="")])
    stub.start()
    try:
        adapter = _adapter(stub)
        result = adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    assert result.total_count == 1
    assert result.items[0].image_ref is None, "缺失的 image_ref 必须留空，不能瞎编一个"
    assert result.missing_image_ref == 1


# --------------------------------------------------------------------------
# 3. 模态过滤与计数
# --------------------------------------------------------------------------


def test_non_dx_records_are_filtered_and_counted():
    """CT 记录直接过滤**并计数** —— 过滤掉的条数必须看得见。"""

    items = [
        _item("ACC-001"),
        _item("ACC-002", modality="CT", study_uid="STUDY-CT-002"),
        _item("ACC-003"),
    ]
    stub = RISStub(items=items)
    stub.start()
    try:
        adapter = _adapter(stub)
        result = adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    assert result.raw_count == 3
    assert result.total_count == 2
    assert result.filtered_non_modality == 1
    assert {item.accession_no for item in result.items} == {"ACC-001", "ACC-003"}
    assert result.counts()["filtered_non_modality"] == 1


def test_missing_modality_is_kept_and_counted_as_unknown():
    """对端没给 ``modality`` —— 保留并计入「模态未知」，**不静默丢行**。"""

    stub = RISStub(items=[_item("ACC-001", modality="")])
    stub.start()
    try:
        adapter = _adapter(stub)
        result = adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    assert result.total_count == 1
    assert result.unknown_modality == 1
    assert result.filtered_non_modality == 0


def test_modality_comparison_is_case_insensitive():
    """对端写 ``dx`` 与写 ``DX`` 视作同一件事。"""

    stub = RISStub(items=[_item("ACC-001", modality="dx")])
    stub.start()
    try:
        adapter = _adapter(stub)
        result = adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    assert result.total_count == 1
    assert result.filtered_non_modality == 0


# --------------------------------------------------------------------------
# 4. 失败不静默
# --------------------------------------------------------------------------


def test_http_failure_reports_status_and_page_position():
    """连续 5xx —— 判 ``RIS_READ_FAILED``，带 HTTP 状态与页位置，绝不返回空清单。"""

    stub = RISStub(items=[_item("ACC-001")], status_sequence=[500, 500, 500])
    stub.start()
    try:
        adapter = _adapter(stub)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.RIS_READ_FAILED
    assert err.http_status == 500
    assert "HTTP 500" in err.message
    assert "第 1 页" in err.message
    assert err.retryable is True, "5xx 值得重试"


def test_failure_on_a_later_page_names_the_cursor():
    """第二页才失败 —— 错误信息须指出是哪一页、哪个游标，否则查不动。"""

    items = [_item("ACC-001"), _item("ACC-002")]
    stub = RISStub(items=items, page_size=1, status_sequence=[200, 500, 500, 500])
    stub.start()
    try:
        adapter = _adapter(stub)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.RIS_READ_FAILED
    assert err.http_status == 500
    assert "第 2 页" in err.message
    assert "cursor=1" in err.message


def test_empty_list_is_a_valid_answer_not_a_failure():
    """区间内确实没有检查 —— 是合法答案，返回计数，不当失败、也不当「有病例」。"""

    stub = RISStub(items=[])
    stub.start()
    try:
        adapter = _adapter(stub)
        result = adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    assert result.total_count == 0
    assert result.raw_count == 0
    assert result.pages == 1


def test_invalid_date_parameter_is_named():
    """日期格式不对 —— 指名报错，不把坏日期悄悄发给对端。"""

    stub = RISStub(items=[_item("ACC-001")])
    stub.start()
    try:
        adapter = _adapter(stub)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.fetch_studies(modality="DX", date_from="2026/03/01")
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.CONTRACT_INVALID
    assert "from" in err.message
    assert len(stub.requests) == 0, "参数不合法时不该发出请求"


def test_date_range_order_is_checked():
    """``from`` 晚于 ``to`` —— 指名报错。"""

    stub = RISStub(items=[_item("ACC-001")])
    stub.start()
    try:
        adapter = _adapter(stub)
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.fetch_studies(
                modality="DX", date_from="2026-03-31", date_to="2026-03-01"
            )
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert len(stub.requests) == 0


# --------------------------------------------------------------------------
# 5. 兼容入口与原始记录
# --------------------------------------------------------------------------


def test_fetch_all_keeps_raw_records_for_cross_checking():
    """兼容入口保留原始记录 —— 「对端返回了什么」可单独核对。"""

    items = [_item("ACC-001"), _item("ACC-002")]
    stub = RISStub(items=items, page_size=1)
    stub.start()
    try:
        adapter = _adapter(stub)
        raw = adapter.fetch_all(modality="DX")
    finally:
        stub.stop()

    assert len(raw) == 2
    assert all(isinstance(item, dict) for item in raw)
    assert {item["accession_no"] for item in raw} == {"ACC-001", "ACC-002"}


def test_adapter_never_falls_back_to_a_local_endpoint():
    """默认出站地址只来自 ``RIS_BASE_URL`` —— 代码里没有本地回环兜底。"""

    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "nodule_eval"
        / "adapters"
        / "ris.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("localhost", "127.0.0.1", "example.com", "http://"):
        assert forbidden not in source, f"RIS 适配器里不应出现 {forbidden}"


def test_read_result_counts_view_is_stable():
    """计数视图的键固定 —— 装配清单与汇总行按它取值。"""

    stub = RISStub(items=[_item("ACC-001")])
    stub.start()
    try:
        adapter = _adapter(stub)
        result = adapter.fetch_studies(modality="DX")
    finally:
        stub.stop()

    counts = result.counts()
    assert set(counts) == {
        "raw_count",
        "total_count",
        "filtered_non_modality",
        "unknown_modality",
        "missing_image_ref",
        "pages",
    }
    assert all(isinstance(value, int) for value in counts.values())
