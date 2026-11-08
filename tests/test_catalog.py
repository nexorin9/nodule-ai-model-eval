"""被评模型目录与响应字段映射的验收。

这一组断言对应采购 / 换版时最实在的两个麻烦：

1. **目录里躺着没授权的模型** —— 不能让它悄悄参与评估，也不能让它悄悄消失；
   要么明确拒绝并说明原因，要么在目录概览里列出「为什么这家没参评」。
2. **不同厂商叫法不一样** —— 尺寸叫 ``size_mm`` / ``diameter_mm``、候选列表叫
   ``candidates`` / ``findings``。业务代码必须只认内部键；两家厂商的同一份内容
   归一之后必须**逐字段相同**，否则敏感度 / 特异度就没有可比性。

字段缺失一律 ``FIELD_MAPPING_MISSING`` 且写明**缺哪个字段、来自哪个模型** ——
填 0 等于凭空造出一个尺寸，把一次漏检盖成一次正确排除。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_env

from nodule_eval.adapters.http import HttpClient
from nodule_eval.adapters.inference import InferenceAdapter
from nodule_eval.catalog import (
    Catalog,
    DEFAULT_RESPONSE_MAPPING,
    ModelSpec,
    load_catalog,
    load_model_spec,
    resolve_path,
)
from nodule_eval.config import load_config
from nodule_eval.errors import ErrorCode, NoduleEvalError
from stub_inference import InferenceStub

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = PROJECT_ROOT / "models"

#: 一份「标准命名」厂商的响应。
VENDOR_A_PAYLOAD = {
    "study_uid": "",
    "model_id": "demo-model",
    "model_version": "v1.0.0",
    "candidates": [
        {
            "position": "右下肺",
            "size_mm": 8.0,
            "density": "磨玻璃",
            "confidence": 0.92,
        }
    ],
}

#: 另一家厂商的响应：候选列表叫 findings 且包在 result 一层，其余字段也各叫各的。
VENDOR_B_PAYLOAD = {
    "study_uid": "",
    "model_id": "second-model",
    "model_version": "v2.3.0",
    "result": {
        "findings": [
            {
                "location": "右下肺",
                "diameter_mm": 8.0,
                "attenuation": "磨玻璃",
                "score": 0.92,
            }
        ]
    },
}


def _adapter(base_url: str, model_id: str) -> InferenceAdapter:
    env = make_env(INFERENCE_BASE_URL=base_url)
    config = load_config(env)
    spec = load_model_spec(model_id)
    client = HttpClient(sleep=lambda _s: None)
    return InferenceAdapter(config, spec, client=client)


# --------------------------------------------------------------------------
# 目录本身
# --------------------------------------------------------------------------


def test_catalog_declares_two_models_with_different_vendor_namings():
    """目录里至少两款模型，且第二款用的是另一家的字段命名。"""

    catalog = load_catalog(MODELS_DIR)

    assert "demo-model" in catalog.authorized
    assert "second-model" in catalog.authorized
    assert catalog.authorized["demo-model"].response_mapping["size"] == "size_mm"
    assert catalog.authorized["second-model"].response_mapping["size"] == "diameter_mm"
    # 候选列表路径支持点号路径（有的厂商把结果包一层）
    assert catalog.authorized["second-model"].response_mapping["candidates"] == "result.findings"


def test_each_model_declares_its_own_endpoint_path():
    """端点路径按模型走 —— 不同厂商的推理服务不在同一个路径上。"""

    demo = load_model_spec("demo-model")
    second = load_model_spec("second-model")

    assert demo.endpoint_path == "/api/v1/detections"
    assert second.endpoint_path == "/api/v2/nodule-detections"


def test_unauthorized_model_is_refused_with_chinese_reason():
    """未授权模型直接拒绝加载 —— 报 ``MODEL_UNAVAILABLE`` 并说明未获授权。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        load_model_spec("unauthorized-model", models_dir=PROJECT_ROOT / "tests" / "fixtures" / "models")

    err = excinfo.value
    assert err.code is ErrorCode.MODEL_UNAVAILABLE
    assert "未获授权" in err.message
    assert "不参与本轮评估" in err.message


def test_unauthorized_model_is_reported_not_silently_skipped(tmp_path: Path):
    """目录扫描时，未授权模型带原因进 ``issues`` —— 不静默跳过。"""

    (tmp_path / "allowed-model.yaml").write_text(
        "model_id: allowed-model\n"
        "model_version: '1.0'\n"
        "authorized: true\n"
        "endpoint_path: /api/v1/detections\n"
        "response_mapping:\n"
        "  candidates: candidates\n"
        "  position: position\n"
        "  size: size_mm\n"
        "  density: density\n"
        "  confidence: confidence\n",
        encoding="utf-8",
    )
    (tmp_path / "blocked-model.yaml").write_text(
        "model_id: blocked-model\n"
        "model_version: '1.0'\n"
        "authorized: false\n"
        "endpoint_path: /api/v1/detections\n"
        "response_mapping:\n"
        "  candidates: candidates\n"
        "  position: position\n"
        "  size: size_mm\n"
        "  density: density\n"
        "  confidence: confidence\n",
        encoding="utf-8",
    )

    catalog = load_catalog(tmp_path)

    assert catalog.authorized_ids() == ("allowed-model",)
    assert len(catalog.issues) == 1
    issue = catalog.issues[0]
    assert issue.model_id == "blocked-model"
    assert issue.error_code == ErrorCode.MODEL_UNAVAILABLE.value
    assert "未获授权" in issue.reason

    # 从目录里取未授权模型同样被拒，且说明原因
    with pytest.raises(NoduleEvalError) as excinfo:
        catalog.get("blocked-model")
    assert excinfo.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert "未获授权" in excinfo.value.message


def test_catalog_describe_lists_authorized_and_blocked_models():
    """目录概览要把「谁参评、谁没参评、为什么」都列出来。"""

    catalog = load_catalog(MODELS_DIR)
    rows = catalog.describe()
    ids = {row["model_id"] for row in rows}

    assert {"demo-model", "second-model"} <= ids
    assert all(row["reason"] for row in rows)


def test_shipped_catalog_reports_the_unauthorized_template():
    """随产品分发的模板声明是未授权的 —— 它必须出现在目录概览里而不是消失。"""

    catalog = load_catalog(MODELS_DIR)

    assert "vendor-template" not in catalog.authorized
    blocked = {issue.model_id for issue in catalog.issues}
    assert "vendor-template" in blocked
    issue = next(item for item in catalog.issues if item.model_id == "vendor-template")
    assert "未获授权" in issue.reason


def test_unauthorized_model_is_refused_by_the_cli(monkeypatch, tmp_path, capsys):
    """CLI 层面：未授权模型直接被拒，退出码 2，且指名说清为什么。"""

    from nodule_eval.cli import main

    for key, value in make_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("IMAGING_EVAL_FIXTURE", "1")
    monkeypatch.setenv("IMAGING_EVAL_FIXTURE_BASE_URL", "http://127.0.0.1:9")

    exit_code = main(
        [
            "run",
            "--cases",
            str(PROJECT_ROOT / "tests" / "fixtures" / "cases" / "single_case.json"),
            "--model-id",
            "vendor-template",
            "--runs-dir",
            str(tmp_path),
            "--fixture",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "MODEL_UNAVAILABLE" in captured.err
    assert "未获授权" in captured.err


def test_unknown_model_reports_missing_catalog_file():
    """目录里没有的模型指名报找不到哪个文件 —— 不回落任何默认模型。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        load_model_spec("no-such-model")

    err = excinfo.value
    assert err.code is ErrorCode.MODEL_UNAVAILABLE
    assert "no-such-model" in err.message


# --------------------------------------------------------------------------
# 字段映射缺失
# --------------------------------------------------------------------------


def test_missing_mapping_field_names_field_and_source_model(tmp_path: Path):
    """目录缺 ``size`` 映射 —— 写明缺哪个字段、来自哪个模型。"""

    (tmp_path / "gap-model.yaml").write_text(
        "model_id: gap-model\n"
        "model_version: '1.0'\n"
        "authorized: true\n"
        "endpoint_path: /api/v1/detections\n"
        "response_mapping:\n"
        "  candidates: candidates\n"
        "  position: position\n"
        "  density: density\n"
        "  confidence: confidence\n",
        encoding="utf-8",
    )

    with pytest.raises(NoduleEvalError) as excinfo:
        load_model_spec("gap-model", models_dir=tmp_path)

    err = excinfo.value
    assert err.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "size" in err.message
    assert "gap-model" in err.message
    assert err.counts_toward_metrics is False


def test_missing_response_field_names_vendor_field_and_model():
    """厂商响应里缺映射表声明的字段 —— 写明缺哪个字段、来自哪个模型。"""

    payload = {
        "study_uid": "",
        "result": {
            "findings": [
                {
                    "location": "右下肺",
                    # 刻意缺 diameter_mm：厂商少给了一个字段
                    "attenuation": "磨玻璃",
                    "score": 0.92,
                }
            ]
        },
    }
    stub = InferenceStub(raw_payload=payload)
    stub.start()
    try:
        adapter = _adapter(stub.base_url, "second-model")
        with pytest.raises(NoduleEvalError) as excinfo:
            adapter.detect(
                study_uid="STUDY-VENDOR-B-001",
                image_ref=None,
                model_id="second-model",
                model_version="v2.3.0",
            )
    finally:
        stub.stop()

    err = excinfo.value
    assert err.code is ErrorCode.FIELD_MAPPING_MISSING
    assert "diameter_mm" in err.message
    assert "second-model" in err.message
    assert err.counts_toward_metrics is False


# --------------------------------------------------------------------------
# 两家厂商命名归一到同一份结果
# --------------------------------------------------------------------------


def test_two_vendor_namings_normalize_to_identical_candidates():
    """同样的内容、两家厂商的叫法 —— 归一之后必须逐字段相同。"""

    results = {}
    for model_id, payload in (
        ("demo-model", VENDOR_A_PAYLOAD),
        ("second-model", VENDOR_B_PAYLOAD),
    ):
        stub = InferenceStub(raw_payload=payload)
        stub.start()
        try:
            adapter = _adapter(stub.base_url, model_id)
            results[model_id] = adapter.detect(
                study_uid="STUDY-COMPARE-001",
                image_ref="dx://demo/study-compare-001",
                model_id=model_id,
                model_version="1.0",
            )
        finally:
            stub.stop()

    vendor_a = results["demo-model"].candidates[0].to_dict()
    vendor_b = results["second-model"].candidates[0].to_dict()

    assert vendor_a == vendor_b
    assert vendor_a == {
        "position": "右下肺",
        "size_mm": 8.0,
        "density": "磨玻璃",
        "confidence": 0.92,
    }


def test_vendor_b_nested_candidates_path_is_read():
    """厂商把候选包在 ``result.findings`` 里 —— 照样读得出来。"""

    stub = InferenceStub(raw_payload=VENDOR_B_PAYLOAD)
    stub.start()
    try:
        adapter = _adapter(stub.base_url, "second-model")
        result = adapter.detect(
            study_uid="STUDY-COMPARE-001",
            image_ref=None,
            model_id="second-model",
            model_version="v2.3.0",
        )
    finally:
        stub.stop()

    assert len(result.candidates) == 1
    assert result.candidates[0].size_mm == 8.0
    assert result.summary() == "右下肺 | 8.0mm | 磨玻璃 | 0.92"


def test_adapter_url_follows_each_models_endpoint_path():
    """适配器打到的路径由模型目录决定，不是写死的。"""

    stub = InferenceStub(raw_payload=VENDOR_A_PAYLOAD)
    stub.start()
    try:
        demo = _adapter(stub.base_url, "demo-model")
        second = _adapter(stub.base_url, "second-model")
    finally:
        stub.stop()

    assert demo.url.endswith("/api/v1/detections")
    assert second.url.endswith("/api/v2/nodule-detections")


# --------------------------------------------------------------------------
# 纪律：厂商字段名不得出现在业务代码里
# --------------------------------------------------------------------------


def test_business_code_contains_no_vendor_field_names():
    """厂商叫法只允许出现在模型目录与测试层，业务代码里不出现。"""

    vendor_only_names = ("diameter_mm", "attenuation", "result.findings")
    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in vendor_only_names:
            if name in text:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{name}")

    assert offenders == [], f"厂商字段名泄漏进业务代码：{offenders}"


def test_default_mapping_lives_in_catalog_not_in_business_modules():
    """缺省映射表归 catalog 管 —— 业务模块不自带一份厂商叫法。"""

    assert set(DEFAULT_RESPONSE_MAPPING) == {
        "candidates",
        "position",
        "size",
        "density",
        "confidence",
    }
    inference_text = (PROJECT_ROOT / "src" / "nodule_eval" / "adapters" / "inference.py").read_text(
        encoding="utf-8"
    )
    assert "DEFAULT_RESPONSE_MAPPING" in inference_text
    # 适配器只引用，不自带一份映射表
    assert "DEFAULT_RESPONSE_MAPPING: dict" not in inference_text
    assert '"size": "size_mm"' not in inference_text


# --------------------------------------------------------------------------
# 路径解析原语
# --------------------------------------------------------------------------


def test_resolve_path_handles_flat_and_nested_paths():
    payload = {"candidates": [1], "result": {"findings": [2]}}

    assert resolve_path(payload, "candidates") == (True, [1])
    assert resolve_path(payload, "result.findings") == (True, [2])
    assert resolve_path(payload, "result.missing") == (False, None)
    assert resolve_path(payload, "candidates.deeper") == (False, None)
    assert resolve_path(payload, "") == (False, None)
