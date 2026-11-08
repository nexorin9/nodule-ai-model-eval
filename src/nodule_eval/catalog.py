"""被评模型目录（``models/<model_id>.yaml``）—— 端点路径、版本、响应字段映射与授权开关的真源。

**源产品证据**：参考实现的入口用一张 ``all_tools`` 工具字典配 ``tools_to_use`` 做
**选择性初始化** —— 只有被列进 ``tools_to_use`` 的工具才真正构造出来，没列的一个都不加载
（``github_ref/MedRAX/main.py:68-75``）。它把「哪些能力参与本轮」这件事从代码里
搬到了**一张声明表**上；同一份入口还从环境变量收 ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``
（``github_ref/MedRAX/main.py:113-117``）。

本产品把同一机制改写为**按模型目录选型**：

- 被评模型在 ``models/<model_id>.yaml`` 里声明端点路径、版本、响应字段映射与授权开关；
- **未授权的模型不加载、不参与本轮评估**，拒绝时给出中文原因（**不静默跳过** ——
  目录里每一条没被启用的声明都会带着原因出现在 :attr:`Catalog.issues` 里，
  便于医生 / 信息科问「为什么这家模型没参评」时答得上来）；
- **厂商各自的字段命名差异由这张表吸收**：厂商对结节尺寸、候选列表、置信度各有各的叫法，
  业务代码只认内部键 ``size`` / ``candidates`` / ``confidence``；
  **业务代码里不出现厂商字段名**；
- 映射缺哪个字段就报 ``FIELD_MAPPING_MISSING`` 并写明缺哪个字段、来自哪个模型，
  **不猜、不填 0、不当默认值**。

字段映射的值既可以是单个字段名（``candidates``），也可以是**点号路径**
（形如 ``a.b``）—— 有的厂商把整段结果包在一层对象里。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .contracts import REQUIRED_MAPPING_KEYS
from .errors import ErrorCode, NoduleEvalError

#: 推理端点的缺省路径（模型目录未声明时用；模型目录一旦声明以目录为准）。
DEFAULT_ENDPOINT_PATH = "/api/v1/detections"

#: 缺省字段映射：内部键 → 厂商响应里的字段名。
#: 这份表只在**没有模型目录声明**时兜底；一旦有声明，一律以目录为准。
DEFAULT_RESPONSE_MAPPING: dict[str, str] = {
    "candidates": "candidates",
    "position": "position",
    "size": "size_mm",
    "density": "density",
    "confidence": "confidence",
}


def contract_invalid(detail: str) -> NoduleEvalError:
    """契约类失败统一走 ``CONTRACT_INVALID``。"""

    return NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail=detail)


def resolve_path(payload: Any, path: str) -> tuple[bool, Any]:
    """按点号路径取值，返回 ``(是否命中, 值)``。

    映射值既可以是单个字段名（``candidates``），也可以是嵌套路径
    （形如 ``a.b``）。路径中任一段缺失即视为**未命中** ——
    调用方据此报 ``FIELD_MAPPING_MISSING``，**不猜、不填默认值**。
    """

    if not path:
        return False, None
    current = payload
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


@dataclass(frozen=True)
class ModelSpec:
    """一个被评模型的声明。

    ``response_mapping`` 把厂商各自的字段命名收敛成产品内部的一套名字 ——
    业务代码只认内部键（``candidates`` / ``position`` / ``size`` / ``density`` /
    ``confidence``），**不出现厂商字段名**。
    """

    model_id: str
    model_version: str
    endpoint_path: str
    response_mapping: Mapping[str, str]
    authorized: bool
    source_path: str

    def mapped(self, key: str) -> str:
        """取映射后的字段名（可能是点号路径）。"""

        try:
            return self.response_mapping[key]
        except KeyError:  # pragma: no cover - 载入时已校验过
            raise NoduleEvalError(
                ErrorCode.FIELD_MAPPING_MISSING,
                detail=f"模型 {self.model_id} 的响应字段映射缺少 {key}",
            ) from None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "endpoint_path": self.endpoint_path,
            "response_mapping": dict(self.response_mapping),
            "authorized": self.authorized,
            "source_path": self.source_path,
        }


@dataclass(frozen=True)
class CatalogIssue:
    """目录里**没被启用**的一条声明（未授权，或声明不合规）。"""

    model_id: str
    source_path: str
    error_code: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "source_path": self.source_path,
            "error_code": self.error_code,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Catalog:
    """一次目录扫描的结果：启用了谁、没启用谁、为什么。"""

    models_dir: str
    authorized: Mapping[str, ModelSpec]
    issues: tuple[CatalogIssue, ...] = ()

    def get(self, model_id: str) -> ModelSpec:
        """取一个**已授权**模型的声明。

        不存在 → ``MODEL_UNAVAILABLE``；存在但未授权 → ``MODEL_UNAVAILABLE``
        并写明「未获授权」。两条路径都**不静默回落**到任何默认模型。
        """

        key = (model_id or "").strip()
        if not key:
            raise contract_invalid("model_id 必填且不得为空")
        spec = self.authorized.get(key)
        if spec is not None:
            return spec
        for issue in self.issues:
            if issue.model_id == key or issue.source_path.endswith(f"{key}.yaml"):
                raise NoduleEvalError(
                    ErrorCode.MODEL_UNAVAILABLE,
                    detail=f"被评模型 {key} 未获授权，不参与本轮评估（{issue.reason}）",
                )
        raise NoduleEvalError(
            ErrorCode.MODEL_UNAVAILABLE,
            detail=f"未找到模型目录文件 {self.models_dir}/{key}.yaml（被评模型 {key} 不参与本轮评估）",
        )

    def authorized_ids(self) -> tuple[str, ...]:
        """已授权模型的标识（供 doctor 列出可选模型）。"""

        return tuple(sorted(self.authorized))

    def describe(self) -> list[dict[str, Any]]:
        """给 doctor 用的目录概览：启用的与没启用的都列出来。"""

        rows = [
            {
                "model_id": spec.model_id,
                "model_version": spec.model_version,
                "endpoint_path": spec.endpoint_path,
                "authorized": True,
                "reason": "已授权，参与本轮评估",
                "source_path": spec.source_path,
            }
            for spec in self.authorized.values()
        ]
        rows.extend(
            {
                "model_id": issue.model_id,
                "model_version": "",
                "endpoint_path": "",
                "authorized": False,
                "reason": issue.reason,
                "source_path": issue.source_path,
            }
            for issue in self.issues
        )
        return rows


def default_models_dir() -> Path:
    """默认模型目录：项目根下的 ``models/``。"""

    return Path(__file__).resolve().parents[2] / "models"


def _mapping_missing(model_id: str, source_path: str, key: str) -> NoduleEvalError:
    return NoduleEvalError(
        ErrorCode.FIELD_MAPPING_MISSING,
        detail=(
            f"模型目录 {source_path}（model_id={model_id}）缺少字段映射：{key}"
        ),
    )


def _parse_spec(path: Path, requested_id: str) -> ModelSpec:
    """解析并校验一个模型目录文件。

    - 顶层缺必填声明或响应字段映射 → ``FIELD_MAPPING_MISSING``（写明缺哪个字段）
    - ``authorized: false`` → ``MODEL_UNAVAILABLE``：**未授权模型不加载、不参与本轮**
    """

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise NoduleEvalError(
            ErrorCode.MODEL_UNAVAILABLE,
            detail=f"模型目录文件 {path} 解析失败：{exc}",
        ) from exc

    if not isinstance(raw, Mapping):
        raise contract_invalid(f"模型目录文件 {path} 顶层须为映射结构")

    for key in ("model_id", "model_version", "endpoint_path", "response_mapping", "authorized"):
        if key not in raw:
            raise _mapping_missing(requested_id, str(path), key)

    authorized = raw["authorized"]
    if not isinstance(authorized, bool):
        raise contract_invalid(f"模型目录文件 {path} 的 authorized 须为布尔值")
    if not authorized:
        raise NoduleEvalError(
            ErrorCode.MODEL_UNAVAILABLE,
            detail=f"被评模型 {requested_id} 未获授权，不参与本轮评估",
        )

    mapping_raw = raw["response_mapping"]
    if not isinstance(mapping_raw, Mapping):
        raise contract_invalid(f"模型目录文件 {path} 的 response_mapping 须为映射结构")
    missing = [key for key in REQUIRED_MAPPING_KEYS if key not in mapping_raw]
    if missing:
        raise _mapping_missing(requested_id, str(path), "response_mapping." + missing[0])

    response_mapping: dict[str, str] = {}
    for key in REQUIRED_MAPPING_KEYS:
        value = mapping_raw[key]
        if not isinstance(value, str) or not value.strip():
            raise _mapping_missing(requested_id, str(path), f"response_mapping.{key}")
        response_mapping[key] = value.strip()

    return ModelSpec(
        model_id=str(raw["model_id"]),
        model_version=str(raw["model_version"]),
        endpoint_path=str(raw["endpoint_path"]),
        response_mapping=response_mapping,
        authorized=True,
        source_path=str(path),
    )


def load_model_spec(model_id: str, models_dir: Path | str | None = None) -> ModelSpec:
    """加载并校验 ``models/<model_id>.yaml``。

    - 目录文件不存在 → ``MODEL_UNAVAILABLE``（指名找不到哪个文件）
    - 缺必填声明或响应字段映射 → ``FIELD_MAPPING_MISSING``（写明缺哪个字段）
    - ``authorized: false`` → ``MODEL_UNAVAILABLE``：**未授权模型不加载、不参与本轮**
    """

    if not isinstance(model_id, str) or not model_id.strip():
        raise contract_invalid("model_id 必填且不得为空")
    model_id = model_id.strip()

    base = Path(models_dir) if models_dir is not None else default_models_dir()
    path = base / f"{model_id}.yaml"
    if not path.is_file():
        raise NoduleEvalError(
            ErrorCode.MODEL_UNAVAILABLE,
            detail=f"未找到模型目录文件 {path}（被评模型 {model_id} 不参与本轮评估）",
        )
    return _parse_spec(path, model_id)


def load_catalog(models_dir: Path | str | None = None) -> Catalog:
    """扫描模型目录，返回「启用了谁、没启用谁、为什么」。

    未授权或声明不合规的文件**不会被静默跳过**：它们带着中文原因进
    :attr:`Catalog.issues`，由 doctor / 目录概览展示。
    """

    base = Path(models_dir) if models_dir is not None else default_models_dir()
    if not base.is_dir():
        raise NoduleEvalError(
            ErrorCode.MODEL_UNAVAILABLE,
            detail=f"模型目录不存在：{base}（被评模型目录须随产品分发）",
        )

    authorized: dict[str, ModelSpec] = {}
    issues: list[CatalogIssue] = []
    for path in sorted(base.glob("*.yaml")):
        try:
            spec = _parse_spec(path, path.stem)
        except NoduleEvalError as exc:
            issues.append(
                CatalogIssue(
                    model_id=path.stem,
                    source_path=str(path),
                    error_code=exc.code.value,
                    reason=exc.detail or exc.message,
                )
            )
            continue
        authorized[spec.model_id] = spec

    return Catalog(
        models_dir=str(base),
        authorized=authorized,
        issues=tuple(issues),
    )


def list_model_specs(models_dir: Path | str | None = None) -> list[ModelSpec]:
    """列出模型目录里**已授权**的模型（doctor 用；目录不存在时返回空表）。"""

    base = Path(models_dir) if models_dir is not None else default_models_dir()
    if not base.is_dir():
        return []
    specs: list[ModelSpec] = []
    for path in sorted(base.glob("*.yaml")):
        try:
            specs.append(_parse_spec(path, path.stem))
        except NoduleEvalError:
            # 未授权或不合规的模型不进本轮，但也不该让 doctor 崩掉。
            continue
    return specs
