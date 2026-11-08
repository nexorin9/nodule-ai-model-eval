"""替身门（fixture gate）：默认路径绝不命中本地替身。

这一层存在的原因很直接：**上一轮同形态项目被判定「默认路径仍是替身」**，
导致整条链路实际没跑过真实协议。本产品把「能不能用替身」变成**显式开关**，
并把它写成可被测试断言的性质，而不是靠约定：

- **默认（fixture 关闭）**：出站目标**只**来自环境变量里的真实端点地址。
  代码里没有任何本地回环地址的兜底、没有「连不上就换本地」的分支、没有硬编码地址。
- **显式开启**（``--fixture`` 或 ``IMAGING_EVAL_FIXTURE=1``）：出站目标改用替身地址。
  该地址**必须显式给出** —— 没给就报错并指名变量，绝不自己猜一个地址、
  绝不回落真实端点（回落会掩盖测试意图）。

**三个端点各有一条替身路径**（RIS 读、推理服务写、科研平台写），所以替身地址
支持按端点分别给出；没给端点专属地址时回落到统一地址
``IMAGING_EVAL_FIXTURE_BASE_URL``。

替身服务本身只存在于 ``tests/`` 层，产品代码不 import 任何替身模块。
"""

from __future__ import annotations

import os
from typing import Mapping

from .errors import ErrorCode, NoduleEvalError

#: 替身开关的环境变量名（``.env.example`` 已列）。
FIXTURE_ENV = "IMAGING_EVAL_FIXTURE"
#: 统一替身端点地址的环境变量名。
FIXTURE_BASE_URL_ENV = "IMAGING_EVAL_FIXTURE_BASE_URL"

#: 各端点的替身地址变量名（缺省时回落到统一替身地址）。
#:
#: 分端点给出是因为三份替身是**三个独立进程**（RIS 清单、推理服务、科研平台），
#: 端点各自的路径前缀不同，合成一份反而会掩盖「到底打到了哪个服务」。
FIXTURE_KIND_ENV: dict[str, str] = {
    "ris": "IMAGING_EVAL_FIXTURE_RIS_BASE_URL",
    "inference": "IMAGING_EVAL_FIXTURE_INFERENCE_BASE_URL",
    "research_platform": "IMAGING_EVAL_FIXTURE_RESEARCH_BASE_URL",
    "model": "IMAGING_EVAL_FIXTURE_MODEL_BASE_URL",
}

#: 视为「开启」的环境变量取值。
_TRUTHY = {"1", "true", "yes", "on"}


def fixture_enabled(cli_flag: bool = False, env: Mapping[str, str] | None = None) -> bool:
    """替身是否开启：命令行 ``--fixture`` 或环境变量 ``IMAGING_EVAL_FIXTURE=1``。"""

    if cli_flag:
        return True
    source: Mapping[str, str] = os.environ if env is None else env
    return str(source.get(FIXTURE_ENV, "")).strip().lower() in _TRUTHY


def _validate_url(raw: str, name: str) -> str:
    if not (raw.startswith("http://") or raw.startswith("https://")):
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"环境变量 {name} 必须是 http:// 或 https:// 开头的地址",
        )
    return raw.rstrip("/")


def fixture_base_url(kind: str, env: Mapping[str, str] | None = None) -> str:
    """取某个端点的替身基址。

    顺序：端点专属变量 → 统一变量。都没有就抛 ``CONTRACT_INVALID`` 并**指名变量**，
    不猜地址、不回落真实端点（回落会掩盖测试意图，也等于默认路径偷偷变成替身）。
    """

    source: Mapping[str, str] = os.environ if env is None else env
    missing: list[str] = []

    kind_env = FIXTURE_KIND_ENV.get(kind)
    if kind_env:
        raw = str(source.get(kind_env, "")).strip()
        if raw:
            return _validate_url(raw, kind_env)
        missing.append(kind_env)

    raw = str(source.get(FIXTURE_BASE_URL_ENV, "")).strip()
    if raw:
        return _validate_url(raw, FIXTURE_BASE_URL_ENV)

    missing.append(FIXTURE_BASE_URL_ENV)
    raise NoduleEvalError(
        ErrorCode.CONTRACT_INVALID,
        detail=(
            f"已开启替身模式，但环境变量 {'、'.join(missing)} 未提供"
            f"（不会自动回落到真实端点，以免掩盖测试意图）"
        ),
    )


def resolve_base_url(
    real_base_url: str,
    *,
    fixture: bool,
    env: Mapping[str, str] | None = None,
    kind: str | None = None,
) -> str:
    """解析本次出站用的端点基址。

    ``fixture=False``（默认）：原样返回环境变量里的真实端点基址 —— **这是唯一的默认路径**。
    ``fixture=True``：取替身基址（见 :func:`fixture_base_url`）；地址缺失时
    抛 ``CONTRACT_INVALID`` 并指名变量名，不猜地址、不回落真实端点。
    """

    if not fixture:
        return real_base_url

    source: Mapping[str, str] = os.environ if env is None else env
    if kind is None:
        raw = str(source.get(FIXTURE_BASE_URL_ENV, "")).strip()
        if not raw:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=(
                    f"已开启替身模式，但环境变量 {FIXTURE_BASE_URL_ENV} 未提供"
                    f"（不会自动回落到真实端点，以免掩盖测试意图）"
                ),
            )
        return _validate_url(raw, FIXTURE_BASE_URL_ENV)

    return fixture_base_url(kind, env=source)


def routed_base_urls(
    config,
    *,
    fixture: bool,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """四个出站面的基址：默认全部取真实端点，替身开启时全部改走替身。

    这是**默认路径唯一该有的形状** —— 替身开关要么全开要么全关，
    不会出现「RIS 走替身、推理走真实」这种半开状态（那会让评估结论失去可比性）。
    """

    real = {
        "ris": config.ris_base_url,
        "inference": config.inference_base_url,
        "research_platform": config.research_platform_base_url,
        "model": config.openai_base_url,
    }
    if not fixture:
        return dict(real)
    return {kind: fixture_base_url(kind, env=env) for kind in real}
