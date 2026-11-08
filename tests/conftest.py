"""让 ``src/`` 下的包在 pytest 里可直接 import（项目未做可安装打包）。"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402  （须在 sys.path 调整之后导入）

#: 一套**齐备的**环境变量（全部为占位值，无任何真实凭据）。
#: 配置层要求 12 项一个不少，测试里逐一覆盖时以这份为底。
BASE_ENV: dict[str, str] = {
    "RIS_BASE_URL": "https://ris.test-hospital.internal",
    "RIS_API_TOKEN": "test-ris-token",
    "INFERENCE_BASE_URL": "https://nodule.test-vendor.internal",
    "INFERENCE_API_TOKEN": "test-inference-token",
    "RESEARCH_PLATFORM_BASE_URL": "https://research.test-hospital.internal",
    "RESEARCH_PLATFORM_TOKEN": "test-research-token",
    "OPENAI_BASE_URL": "https://llm.test-hospital.internal/v1",
    "OPENAI_API_KEY": "test-model-token",
    "OPENAI_MODEL": "nodule-goldstd-merge",
    "MODEL_TIMEOUT": "20",
    "MODEL_MAX_RETRIES": "2",
    "MODEL_TEMPERATURE": "0",
}


def make_env(**overrides: str) -> dict[str, str]:
    """取一份齐备的环境变量副本，可逐项覆盖。"""

    env = dict(BASE_ENV)
    env.update(overrides)
    return env


@pytest.fixture
def full_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """把 12 项环境变量全部装进当前进程（替身地址由各测试自行覆盖）。"""

    env = make_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env
