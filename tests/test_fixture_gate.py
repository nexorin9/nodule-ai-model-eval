"""替身门：默认路径绝不命中本地替身，缺凭据指名报出且不静默回落。

这一组断言是本产品「**默认路径是不是真的走了真实协议**」的唯一证据来源。

三条红线：

1. **默认路径不碰替身** —— 替身地址就摆在环境变量里，只要没显式开开关，
   出站客户端就绝不会打过去；
2. **替身只在显式开启时可达** —— ``--fixture`` 或 ``IMAGING_EVAL_FIXTURE=1``，
   且必须同时给出替身地址（缺了就指名报错，不猜地址）；
3. **缺凭据就是缺凭据** —— 少一个环境变量就指名报出那个变量名，
   **绝不**偷偷回落到本地替身（回落会让「跑通了」变成假的）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_env

from nodule_eval.config import (
    REQUIRED_ENV_VARS,
    load_config,
    load_model_spec,
    missing_env_vars,
)
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.fixture_gate import (
    FIXTURE_BASE_URL_ENV,
    FIXTURE_ENV,
    fixture_base_url,
    fixture_enabled,
    routed_base_urls,
)
from nodule_eval.engine import Engine
from stub_inference import InferenceStub

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "nodule_eval"
TESTS_DIR = PROJECT_ROOT / "tests"


def _engine(fixture: bool = False, **env_overrides: str) -> Engine:
    env = make_env(**env_overrides)
    config = load_config(env)
    spec = load_model_spec("demo-model")
    return Engine(config, spec, fixture=fixture)


# --------------------------------------------------------------------------
# 1. 默认路径不碰替身
# --------------------------------------------------------------------------


def test_default_path_never_touches_the_stub(monkeypatch):
    """替身地址就在环境变量里 —— 默认路径也绝不打过去。"""

    stub = InferenceStub()
    stub.start()
    try:
        monkeypatch.setenv("INFERENCE_BASE_URL", "https://nodule.test-vendor.internal")
        monkeypatch.setenv(FIXTURE_ENV, "0")
        monkeypatch.setenv(FIXTURE_BASE_URL_ENV, stub.base_url)

        engine = _engine(fixture=False)

        assert engine.adapter.url == (
            "https://nodule.test-vendor.internal/api/v1/detections"
        )
        assert not engine.adapter.url.startswith(stub.base_url)
        assert stub.requests == [], "替身不得收到任何请求"
    finally:
        stub.stop()


def test_fixture_switch_off_keeps_every_endpoint_on_the_real_host():
    """替身关闭时，四个出站面全部留在各自的环境变量地址上。"""

    env = make_env()
    config = load_config(env)

    routed = routed_base_urls(config, fixture=False, env=env)

    assert routed["ris"] == env["RIS_BASE_URL"]
    assert routed["inference"] == env["INFERENCE_BASE_URL"]
    assert routed["research_platform"] == env["RESEARCH_PLATFORM_BASE_URL"]
    assert routed["model"] == env["OPENAI_BASE_URL"]


def test_engine_routes_to_the_fixture_only_when_the_gate_is_open(full_env, monkeypatch):
    """两个替身分别代表「真实端点」与「替身端点」——出站打到哪一个由开关决定。

    这是替身门最硬的一条证据：同一个引擎，开关关着打 A，开关开着打 B，
    **两次都是真的 HTTP 出站**（不是断言某个 mock 被调用过）。

    替身地址经**进程环境变量**给出 —— 这正是 CLI 走的那条路
    （``--fixture`` / ``IMAGING_EVAL_FIXTURE=1`` + ``IMAGING_EVAL_FIXTURE_BASE_URL``）。
    """

    real_side = InferenceStub()
    fixture_side = InferenceStub()
    real_side.start()
    fixture_side.start()
    try:
        monkeypatch.setenv("INFERENCE_BASE_URL", real_side.base_url)
        monkeypatch.setenv(FIXTURE_BASE_URL_ENV, fixture_side.base_url)
        monkeypatch.setenv(FIXTURE_ENV, "1")

        config = load_config()
        spec = load_model_spec("demo-model")

        closed = Engine(config, spec, fixture=False)
        assert closed.adapter.url.startswith(real_side.base_url)

        opened = Engine(config, spec, fixture=True)
        assert opened.adapter.url.startswith(fixture_side.base_url)
    finally:
        real_side.stop()
        fixture_side.stop()


def test_stub_modules_exist_only_under_tests():
    """三份替身只在 ``tests/`` 下 —— 产品目录里不得有替身文件。"""

    for name in ("stub_ris.py", "stub_inference.py", "stub_research_platform.py"):
        assert (TESTS_DIR / name).is_file(), f"tests/{name} 应当存在"
        assert not list(SRC_DIR.rglob(name)), f"src/ 下不得出现 {name}"


def test_product_code_imports_no_stub_module():
    """静态扫描：``src/`` 不得 import 任何替身 / 测试层模块。"""

    offenders: list[str] = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(
                ("import stub_", "from stub_", "import tests", "from tests", "import scenarios")
            ):
                offenders.append(f"{path.name}: {stripped}")
    assert not offenders, f"产品代码 import 了测试层模块：{offenders}"


def test_product_code_has_no_local_address_fallback():
    """静态扫描：产品代码里不得出现本地回环地址或替身字样。"""

    forbidden = (
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "example.com",
        "stub_ris",
        "stub_inference",
        "stub_research_platform",
    )
    offenders: list[str] = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.name}:{token}")
    assert not offenders, f"产品代码里出现替身痕迹：{offenders}"


# --------------------------------------------------------------------------
# 2. 替身只在显式开启时可达
# --------------------------------------------------------------------------


def test_fixture_is_off_by_default():
    """不传开关、环境变量没开 —— 替身一律关闭。"""

    assert fixture_enabled(False, env={}) is False
    assert fixture_enabled(False, env={FIXTURE_ENV: "0"}) is False
    assert fixture_enabled(False, env={FIXTURE_ENV: "false"}) is False


def test_fixture_turns_on_by_cli_flag_or_env_var():
    """两个开关都能开替身：命令行 ``--fixture`` 或环境变量 ``IMAGING_EVAL_FIXTURE=1``。"""

    assert fixture_enabled(True, env={}) is True
    assert fixture_enabled(False, env={FIXTURE_ENV: "1"}) is True
    assert fixture_enabled(False, env={FIXTURE_ENV: "true"}) is True


def test_fixture_routes_every_endpoint_to_the_fixture_host():
    """替身开启时四个出站面一起改走替身 —— 不允许半开（那会让结论失去可比性）。"""

    stub = InferenceStub()
    stub.start()
    try:
        env = make_env()
        env[FIXTURE_BASE_URL_ENV] = stub.base_url
        config = load_config(env)

        routed = routed_base_urls(config, fixture=True, env=env)

        assert set(routed.values()) == {stub.base_url}
    finally:
        stub.stop()


def test_per_endpoint_fixture_url_overrides_the_shared_one():
    """端点专属替身地址优先于统一地址。"""

    env = make_env()
    env["IMAGING_EVAL_FIXTURE_RIS_BASE_URL"] = "http://127.0.0.1:9001"
    env[FIXTURE_BASE_URL_ENV] = "http://127.0.0.1:9002"

    assert fixture_base_url("ris", env=env) == "http://127.0.0.1:9001"
    assert fixture_base_url("inference", env=env) == "http://127.0.0.1:9002"


def test_fixture_enabled_without_base_url_is_refused_and_names_the_variable():
    """开了开关却没给替身地址 —— 指名报错，不猜地址、不回落真实端点。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        fixture_base_url("inference", env={})

    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert FIXTURE_BASE_URL_ENV in excinfo.value.message


def test_fixture_base_url_must_be_an_http_url():
    """替身地址写错格式 —— 指名报错，不当作可用地址。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        fixture_base_url("inference", env={FIXTURE_BASE_URL_ENV: "127.0.0.1:9000"})

    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID


# --------------------------------------------------------------------------
# 3. 缺凭据指名报出，且不静默回落
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", REQUIRED_ENV_VARS)
def test_missing_credential_names_the_variable_and_never_falls_back(name):
    """doctor 的自检底座：少哪个变量就报哪个变量名，且不回落替身。"""

    env = make_env()
    del env[name]

    assert missing_env_vars(env) == [name]

    with pytest.raises(NoduleEvalError) as excinfo:
        load_config(env=env)

    err = excinfo.value
    assert err.code is ErrorCode.CREDENTIAL_MISSING
    assert name in err.message
    lowered = err.message.lower()
    assert "localhost" not in lowered
    assert "127.0.0.1" not in lowered
    assert "替身" in err.message, "必须明确告知不会回落到本地替身"


def test_missing_credential_does_not_fall_back_even_with_fixture_on():
    """替身开关开着也救不了缺凭据 —— 缺了就是缺了。"""

    env = make_env()
    del env["INFERENCE_API_TOKEN"]
    env[FIXTURE_ENV] = "1"
    env[FIXTURE_BASE_URL_ENV] = "http://127.0.0.1:9000"

    with pytest.raises(NoduleEvalError) as excinfo:
        load_config(env=env)

    assert excinfo.value.code is ErrorCode.CREDENTIAL_MISSING
    assert "INFERENCE_API_TOKEN" in excinfo.value.message
