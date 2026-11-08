"""配置层：全部凭据与模型参数只从环境变量读取。

**源产品证据**：参考实现的入口从环境变量收集 ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``
再交给模型客户端（``github_ref/MedRAX/main.py:76, 113-117``）—— 那份配置面散在入口脚本里，
且键名缺失时直接不传。本产品把它收敛成**单一 config 层**：变量缺失时抛
``CREDENTIAL_MISSING`` 并**指名变量名**，绝不静默回落本地替身、绝不静默改用默认值。

被评模型的端点路径、版本与响应字段映射放在 ``models/<model_id>.yaml``；
**未授权的模型不加载、不参与本轮评估**（目录的解析与授权落在 :mod:`nodule_eval.catalog`）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .catalog import (
    Catalog,
    CatalogIssue,
    ModelSpec,
    default_models_dir,
    list_model_specs,
    load_catalog,
    load_model_spec,
)
from .errors import ErrorCode, NoduleEvalError

# --------------------------------------------------------------------------
# 环境变量清单
# --------------------------------------------------------------------------

RIS_BASE_URL = "RIS_BASE_URL"
RIS_API_TOKEN = "RIS_API_TOKEN"
INFERENCE_BASE_URL = "INFERENCE_BASE_URL"
INFERENCE_API_TOKEN = "INFERENCE_API_TOKEN"
RESEARCH_PLATFORM_BASE_URL = "RESEARCH_PLATFORM_BASE_URL"
RESEARCH_PLATFORM_TOKEN = "RESEARCH_PLATFORM_TOKEN"
OPENAI_BASE_URL = "OPENAI_BASE_URL"
OPENAI_API_KEY = "OPENAI_API_KEY"
OPENAI_MODEL = "OPENAI_MODEL"
MODEL_TIMEOUT = "MODEL_TIMEOUT"
MODEL_MAX_RETRIES = "MODEL_MAX_RETRIES"
MODEL_TEMPERATURE = "MODEL_TEMPERATURE"
#: 写回提交体的大小上限（**可选**变量 —— 不是凭据，不配就用默认值）。
RESEARCH_PLATFORM_MAX_PAYLOAD_BYTES = "RESEARCH_PLATFORM_MAX_PAYLOAD_BYTES"

#: 全部必填环境变量。**一个都不能少**：缺任何一项都指名报错，不设静默默认值。
REQUIRED_ENV_VARS: tuple[str, ...] = (
    RIS_BASE_URL,
    RIS_API_TOKEN,
    INFERENCE_BASE_URL,
    INFERENCE_API_TOKEN,
    RESEARCH_PLATFORM_BASE_URL,
    RESEARCH_PLATFORM_TOKEN,
    OPENAI_BASE_URL,
    OPENAI_API_KEY,
    OPENAI_MODEL,
    MODEL_TIMEOUT,
    MODEL_MAX_RETRIES,
    MODEL_TEMPERATURE,
)

#: 数值型环境变量的合法区间（越界即指名报错，不静默截断）。
TIMEOUT_RANGE = (1.0, 600.0)
MAX_RETRIES_RANGE = (0, 5)
TEMPERATURE_RANGE = (0.0, 2.0)

#: 写回提交体的**默认大小上限**（字节，1 MiB）。
#:
#: 院内科研平台的登记接口通常有请求体上限；超限时**明确报错并提示分批**，
#: 绝不静默截断逐例明细（截断 = 对端登记的例数与本轮结论对不上）。
DEFAULT_MAX_PAYLOAD_BYTES = 1_048_576


def join_url(base: str, path: str) -> str:
    """拼接端点 URL，避免出现双斜杠或丢路径段。"""

    return f"{base.rstrip('/')}/{path.lstrip('/')}"


# --------------------------------------------------------------------------
# .env 文件（院内配置的常规落法）
# --------------------------------------------------------------------------

#: 指向 .env 文件的环境变量（缺省用项目根下的 ``.env``）。
ENV_FILE_ENV = "NODULE_EVAL_ENV_FILE"

#: .env 文件的缺省名（相对项目根）。
DEFAULT_ENV_FILENAME = ".env"


def default_env_file_path() -> Path:
    """``.env`` 的缺省位置：项目根下。

    按**包的位置**算，不按当前工作目录算 —— 宏是从 Excel 里起进程的，
    工作目录往往是表格所在目录，不是项目目录；按 CWD 找就找不到配置了。
    """

    override = str(os.environ.get(ENV_FILE_ENV, "")).strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / DEFAULT_ENV_FILENAME


def load_env_file(path: Path | str | None = None) -> Path | None:
    """把 ``.env`` 里的键值读进进程环境，返回用到的文件路径。

    **已存在的环境变量不被覆盖**（``override=False``）：院内常见做法是
    「机器级环境变量兜底、``.env`` 补差」，反过来会让临时改一个变量
    悄悄失效。

    找不到文件就什么都不做 —— 没装 ``.env`` 不是错误，
    直接用系统环境变量同样能跑。
    """

    target = Path(path) if path is not None else default_env_file_path()
    if not target.is_file():
        return None
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - 没装 python-dotenv 时 .env 不生效
        return None
    load_dotenv(dotenv_path=str(target), override=False)
    return target


@dataclass(frozen=True)
class Config:
    """一次运行所需的全部配置。"""

    ris_base_url: str
    ris_api_token: str
    inference_base_url: str
    inference_api_token: str
    research_platform_base_url: str
    research_platform_token: str
    openai_base_url: str
    openai_api_key: str
    openai_model: str
    model_timeout: float
    model_max_retries: int
    model_temperature: float
    #: 写回提交体大小上限（字节）。**可选** —— 不配就是默认的 1 MiB。
    research_platform_max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES

    # ---- 鉴权头：凭据一律来自环境变量，不落盘、不进日志 ----
    @property
    def ris_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.ris_api_token}"}

    @property
    def inference_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.inference_api_token}"}

    @property
    def research_platform_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.research_platform_token}"}

    @property
    def model_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.openai_api_key}",
            "Content-Type": "application/json",
        }

    # ---- 端点 ----
    def ris_studies_url(self) -> str:
        return join_url(self.ris_base_url, "/api/v1/imaging-studies")

    def inference_detections_url(self, endpoint_path: str) -> str:
        return join_url(self.inference_base_url, endpoint_path)

    def research_platform_runs_url(self) -> str:
        return join_url(self.research_platform_base_url, "/api/v1/model-evaluation-runs")

    def model_chat_url(self) -> str:
        return join_url(self.openai_base_url, "/chat/completions")

    def redacted(self) -> dict[str, Any]:
        """给 doctor / 日志用的脱敏视图：只显式标注 token 是否已配置。"""

        return {
            RIS_BASE_URL: self.ris_base_url,
            RIS_API_TOKEN: "已配置" if self.ris_api_token else "未配置",
            INFERENCE_BASE_URL: self.inference_base_url,
            INFERENCE_API_TOKEN: "已配置" if self.inference_api_token else "未配置",
            RESEARCH_PLATFORM_BASE_URL: self.research_platform_base_url,
            RESEARCH_PLATFORM_TOKEN: "已配置" if self.research_platform_token else "未配置",
            OPENAI_BASE_URL: self.openai_base_url,
            OPENAI_API_KEY: "已配置" if self.openai_api_key else "未配置",
            OPENAI_MODEL: self.openai_model,
            MODEL_TIMEOUT: self.model_timeout,
            MODEL_MAX_RETRIES: self.model_max_retries,
            MODEL_TEMPERATURE: self.model_temperature,
            RESEARCH_PLATFORM_MAX_PAYLOAD_BYTES: self.research_platform_max_payload_bytes,
        }


def _missing_env_error(names: list[str]) -> NoduleEvalError:
    return NoduleEvalError(ErrorCode.CREDENTIAL_MISSING, names="、".join(names))


def _require_url(env: Mapping[str, str], name: str) -> str:
    value = env[name].strip()
    if not (value.startswith("http://") or value.startswith("https://")):
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"环境变量 {name} 必须是 http:// 或 https:// 开头的地址（实得 {value!r}）",
        )
    return value.rstrip("/")


def _require_float(env: Mapping[str, str], name: str, low: float, high: float) -> float:
    raw = env[name].strip()
    try:
        value = float(raw)
    except ValueError:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"环境变量 {name} 必须是数字（实得 {raw!r}）",
        ) from None
    if not (low <= value <= high):
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"环境变量 {name} 须在 {low} 与 {high} 之间（实得 {value}）",
        )
    return value


def _require_int(env: Mapping[str, str], name: str, low: int, high: int) -> int:
    raw = env[name].strip()
    try:
        value = int(raw)
    except ValueError:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"环境变量 {name} 必须是整数（实得 {raw!r}）",
        ) from None
    if not (low <= value <= high):
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"环境变量 {name} 须在 {low} 与 {high} 之间（实得 {value}）",
        )
    return value


def resolve_max_payload_bytes(env: Mapping[str, str] | None = None) -> int:
    """取写回提交体的大小上限（**可选**变量，缺省 1 MiB）。

    这是**可选**变量 —— 它不是凭据，不配就用默认值；配了写错（不是正整数）
    则**指名报错**，不静默用默认值糊弄过去（上限配错了要么白白拒掉一轮，
    要么放过去一堆对端拒收的请求）。
    """

    source: Mapping[str, str] = os.environ if env is None else env
    raw = str(source.get(RESEARCH_PLATFORM_MAX_PAYLOAD_BYTES, "")).strip()
    if not raw:
        return DEFAULT_MAX_PAYLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                f"环境变量 {RESEARCH_PLATFORM_MAX_PAYLOAD_BYTES} 必须是正整数"
                f"（实得 {raw!r}）"
            ),
        ) from None
    if value <= 0:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                f"环境变量 {RESEARCH_PLATFORM_MAX_PAYLOAD_BYTES} 必须大于 0"
                f"（实得 {value}）"
            ),
        )
    return value


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """从环境变量读全部凭据与模型参数。

    缺任何一个变量都抛 ``CREDENTIAL_MISSING`` 并**列出缺哪几个**；
    **不存在任何本地替身回落路径** —— 缺凭据就是缺凭据。

    传了 ``env`` 就只认它（测试与嵌入调用用）；
    没传则先读 ``.env``（若有），再读进程环境 —— 院内机器上凭据
    多数是配在 ``.env`` 里的。
    """

    if env is None:
        load_env_file()
    source: Mapping[str, str] = os.environ if env is None else env

    missing: list[str] = []
    for name in REQUIRED_ENV_VARS:
        raw = source.get(name)
        if raw is None or not str(raw).strip():
            missing.append(name)
    if missing:
        raise _missing_env_error(missing)

    return Config(
        ris_base_url=_require_url(source, RIS_BASE_URL),
        ris_api_token=source[RIS_API_TOKEN].strip(),
        inference_base_url=_require_url(source, INFERENCE_BASE_URL),
        inference_api_token=source[INFERENCE_API_TOKEN].strip(),
        research_platform_base_url=_require_url(source, RESEARCH_PLATFORM_BASE_URL),
        research_platform_token=source[RESEARCH_PLATFORM_TOKEN].strip(),
        openai_base_url=_require_url(source, OPENAI_BASE_URL),
        openai_api_key=source[OPENAI_API_KEY].strip(),
        openai_model=source[OPENAI_MODEL].strip(),
        model_timeout=_require_float(source, MODEL_TIMEOUT, *TIMEOUT_RANGE),
        model_max_retries=_require_int(source, MODEL_MAX_RETRIES, *MAX_RETRIES_RANGE),
        model_temperature=_require_float(source, MODEL_TEMPERATURE, *TEMPERATURE_RANGE),
        research_platform_max_payload_bytes=resolve_max_payload_bytes(source),
    )


def missing_env_vars(env: Mapping[str, str] | None = None) -> list[str]:
    """列出缺失的环境变量名（doctor 子命令用，不抛异常）。"""

    if env is None:
        load_env_file()
    source: Mapping[str, str] = os.environ if env is None else env
    missing: list[str] = []
    for name in REQUIRED_ENV_VARS:
        raw = source.get(name)
        if raw is None or not str(raw).strip():
            missing.append(name)
    return missing
