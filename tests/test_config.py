"""配置层测试：凭据缺失必须指名报错，且绝不回落本地替身。"""

from pathlib import Path

import pytest

from nodule_eval.config import (
    REQUIRED_ENV_VARS,
    Config,
    list_model_specs,
    load_config,
    load_model_spec,
    missing_env_vars,
)
from nodule_eval.errors import ErrorCode, NoduleEvalError

FIXTURE_MODELS_DIR = Path(__file__).resolve().parent / "fixtures" / "models"

BASE_ENV = {
    "RIS_BASE_URL": "https://ris.example-hospital.internal",
    "RIS_API_TOKEN": "ris-token-placeholder",
    "INFERENCE_BASE_URL": "https://nodule-ai.example-vendor.internal",
    "INFERENCE_API_TOKEN": "inference-token-placeholder",
    "RESEARCH_PLATFORM_BASE_URL": "https://research.example-hospital.internal",
    "RESEARCH_PLATFORM_TOKEN": "research-token-placeholder",
    "OPENAI_BASE_URL": "https://api.example-llm.internal/v1",
    "OPENAI_API_KEY": "llm-key-placeholder",
    "OPENAI_MODEL": "nodule-goldstd-merge",
    "MODEL_TIMEOUT": "20",
    "MODEL_MAX_RETRIES": "2",
    "MODEL_TEMPERATURE": "0",
}


def test_all_twelve_env_vars_are_required():
    assert len(REQUIRED_ENV_VARS) == 12
    assert set(REQUIRED_ENV_VARS) == set(BASE_ENV)


def test_load_config_reads_every_value():
    config = load_config(env=dict(BASE_ENV))

    assert isinstance(config, Config)
    assert config.ris_base_url == "https://ris.example-hospital.internal"
    assert config.ris_api_token == "ris-token-placeholder"
    assert config.inference_base_url == "https://nodule-ai.example-vendor.internal"
    assert config.inference_api_token == "inference-token-placeholder"
    assert config.research_platform_base_url == "https://research.example-hospital.internal"
    assert config.research_platform_token == "research-token-placeholder"
    assert config.openai_base_url == "https://api.example-llm.internal/v1"
    assert config.openai_api_key == "llm-key-placeholder"
    assert config.openai_model == "nodule-goldstd-merge"
    assert config.model_timeout == 20.0
    assert config.model_max_retries == 2
    assert config.model_temperature == 0.0


@pytest.mark.parametrize("name", REQUIRED_ENV_VARS)
def test_missing_env_var_raises_and_names_the_variable(name):
    env = dict(BASE_ENV)
    del env[name]

    with pytest.raises(NoduleEvalError) as excinfo:
        load_config(env=env)

    err = excinfo.value
    assert err.code == ErrorCode.CREDENTIAL_MISSING
    assert name in err.message


@pytest.mark.parametrize("name", REQUIRED_ENV_VARS)
def test_blank_env_var_counts_as_missing(name):
    env = dict(BASE_ENV)
    env[name] = "   "

    with pytest.raises(NoduleEvalError) as excinfo:
        load_config(env=env)

    assert excinfo.value.code == ErrorCode.CREDENTIAL_MISSING
    assert name in excinfo.value.message


def test_missing_env_error_names_all_missing_vars_at_once():
    env = dict(BASE_ENV)
    del env["INFERENCE_BASE_URL"]
    del env["INFERENCE_API_TOKEN"]

    with pytest.raises(NoduleEvalError) as excinfo:
        load_config(env=env)

    message = excinfo.value.message
    assert "INFERENCE_BASE_URL" in message
    assert "INFERENCE_API_TOKEN" in message


def test_missing_credential_never_falls_back_to_local_stub():
    """缺凭据就是缺凭据 —— 报错文案里不得出现任何本地替身地址。"""

    env = dict(BASE_ENV)
    del env["INFERENCE_BASE_URL"]

    with pytest.raises(NoduleEvalError) as excinfo:
        load_config(env=env)

    message = excinfo.value.message.lower()
    assert "localhost" not in message
    assert "127.0.0.1" not in message
    assert "替身" in excinfo.value.message  # 明确告知不会回落替身


def test_missing_env_vars_helper_lists_gaps_without_raising():
    env = dict(BASE_ENV)
    del env["OPENAI_API_KEY"]
    env["OPENAI_MODEL"] = ""

    assert missing_env_vars(env) == ["OPENAI_API_KEY", "OPENAI_MODEL"]


@pytest.mark.parametrize(
    "name,value",
    [
        ("MODEL_TIMEOUT", "abc"),
        ("MODEL_TIMEOUT", "-5"),
        ("MODEL_MAX_RETRIES", "2.5"),
        ("MODEL_MAX_RETRIES", "9"),
        ("MODEL_TEMPERATURE", "hot"),
        ("MODEL_TEMPERATURE", "3"),
        ("RIS_BASE_URL", "ris.example-hospital.internal"),
    ],
)
def test_invalid_env_value_names_the_variable(name, value):
    env = dict(BASE_ENV)
    env[name] = value

    with pytest.raises(NoduleEvalError) as excinfo:
        load_config(env=env)

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert name in excinfo.value.message


def test_headers_carry_bearer_token_from_env():
    config = load_config(env=dict(BASE_ENV))

    assert config.ris_headers == {"Authorization": "Bearer ris-token-placeholder"}
    assert config.inference_headers == {"Authorization": "Bearer inference-token-placeholder"}
    assert config.research_platform_headers == {
        "Authorization": "Bearer research-token-placeholder"
    }
    assert config.model_headers["Authorization"] == "Bearer llm-key-placeholder"
    assert config.model_headers["Content-Type"] == "application/json"


def test_endpoint_urls_join_without_double_slash():
    config = load_config(env=dict(BASE_ENV))

    assert config.ris_studies_url() == (
        "https://ris.example-hospital.internal/api/v1/imaging-studies"
    )
    assert config.inference_detections_url("/api/v1/detections") == (
        "https://nodule-ai.example-vendor.internal/api/v1/detections"
    )
    assert config.research_platform_runs_url() == (
        "https://research.example-hospital.internal/api/v1/model-evaluation-runs"
    )
    assert config.model_chat_url() == "https://api.example-llm.internal/v1/chat/completions"


def test_redacted_view_never_exposes_token_values():
    config = load_config(env=dict(BASE_ENV))

    redacted = config.redacted()

    assert redacted["RIS_API_TOKEN"] == "已配置"
    assert redacted["OPENAI_API_KEY"] == "已配置"
    assert "ris-token-placeholder" not in str(redacted)
    assert "llm-key-placeholder" not in str(redacted)


# --------------------------------------------------------------------------
# 被评模型目录
# --------------------------------------------------------------------------


def test_load_model_spec_reads_endpoint_version_and_mapping():
    spec = load_model_spec("ok-model", models_dir=FIXTURE_MODELS_DIR)

    assert spec.model_id == "fixture-model"
    assert spec.model_version == "1.4.2"
    assert spec.endpoint_path == "/api/v1/detections"
    assert spec.authorized is True
    assert spec.response_mapping["size"] == "size_mm"
    assert spec.mapped("candidates") == "candidates"


def test_missing_mapping_field_raises_field_mapping_missing_and_names_the_field():
    with pytest.raises(NoduleEvalError) as excinfo:
        load_model_spec("missing-size-model", models_dir=FIXTURE_MODELS_DIR)

    err = excinfo.value
    assert err.code == ErrorCode.FIELD_MAPPING_MISSING
    assert "size" in err.message
    assert "missing-size-model" in err.message


def test_missing_top_level_declaration_names_the_field():
    with pytest.raises(NoduleEvalError) as excinfo:
        load_model_spec("incomplete-model", models_dir=FIXTURE_MODELS_DIR)

    err = excinfo.value
    assert err.code == ErrorCode.FIELD_MAPPING_MISSING
    assert "endpoint_path" in err.message


def test_unauthorized_model_is_refused_and_not_loaded():
    with pytest.raises(NoduleEvalError) as excinfo:
        load_model_spec("unauthorized-model", models_dir=FIXTURE_MODELS_DIR)

    err = excinfo.value
    assert err.code == ErrorCode.MODEL_UNAVAILABLE
    assert "未获授权" in err.message


def test_unknown_model_reports_missing_catalog_file():
    with pytest.raises(NoduleEvalError) as excinfo:
        load_model_spec("no-such-model", models_dir=FIXTURE_MODELS_DIR)

    err = excinfo.value
    assert err.code == ErrorCode.MODEL_UNAVAILABLE
    assert "no-such-model" in err.message


def test_list_model_specs_only_returns_authorized_models():
    specs = list_model_specs(models_dir=FIXTURE_MODELS_DIR)

    loaded = {spec.model_id for spec in specs}
    assert loaded == {"fixture-model"}
