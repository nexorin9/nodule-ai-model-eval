"""三条出站面的**端点契约**（测试层的参照模型）。

这份表是 ``spec.md``「部署与集成」与各适配器字段规范的**机器可读版本**：
URL 路径、查询参数、鉴权头来源、请求体字段集、响应体必填字段，逐条写死。

**为什么要有这张表**：适配器自己的测试（``test_ris_adapter.py`` 等）验的是
「这个适配器按自己的意思工作」；这张表验的是「**按 spec.md 的意思工作**」。
两者不是一回事 —— 适配器与契约一起漂移时，只有契约表能发现。

**三条出站面**（本产品的全部出站面，没有第四条）：

===================  ==========  ==========================================
端点                 方向        契约
===================  ==========  ==========================================
RIS 检查清单          读（只读）  ``GET /api/v1/imaging-studies``
推理服务              写（调用）  ``POST {模型目录里的 endpoint_path}``
科研平台评估登记      写（生效）  ``POST /api/v1/model-evaluation-runs``
===================  ==========  ==========================================

**凭据纪律**：契约里**没有**密钥字段 —— 鉴权一律是
``Authorization: Bearer $环境变量``。本模块的 :func:`check_request` 会把
「请求体 / URL / 查询参数里出现令牌原文」判为违规：**令牌只许待在请求头里**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

#: 令牌在黄金文件里的占位写法（``Bearer {RIS_API_TOKEN}``）。
AUTH_MARKER = "Bearer {{{env}}}"


@dataclass(frozen=True)
class EndpointContract:
    """一个端点的契约。

    ``query_params``
        允许出现在查询参数里的**全部**参数名；出现契约外的参数即违规
        （对端看不懂的多余参数，有时会让它按另一种语义解析）。
    ``required_query_params``
        必须出现的参数名。
    ``request_body_fields``
        请求体的**字段集**（空元组 = 该端点无请求体，如 RIS 的 GET）。
    ``response_fields``
        响应体里**必须在场**的字段 —— 缺一个就报明确错误，不填默认值。
    ``response_item_fields``
        响应体里**逐条记录**（数组元素）必须具备的字段。
    ``paths``
        端点路径 —— 推理服务按模型目录而变，故按 ``model_id`` 给；
        只有一个路径的端点用 ``default_path``。
    """

    key: str
    title: str
    method: str
    auth_env: str
    default_path: str = ""
    paths: Mapping[str, str] = field(default_factory=dict)
    query_params: tuple[str, ...] = ()
    required_query_params: tuple[str, ...] = ()
    request_body_fields: tuple[str, ...] = ()
    response_fields: tuple[str, ...] = ()
    response_item_fields: tuple[str, ...] = ()

    def path_for(self, model_id: str = "") -> str:
        """取该端点在本场景下的路径。"""

        if model_id and model_id in self.paths:
            return self.paths[model_id]
        return self.default_path

    def auth_marker(self) -> str:
        """黄金文件里使用的鉴权占位写法。"""

        return AUTH_MARKER.format(env=self.auth_env)


#: 院内 RIS 检查清单（读）。
RIS_CONTRACT = EndpointContract(
    key="ris",
    title="院内 RIS 检查清单",
    method="GET",
    auth_env="RIS_API_TOKEN",
    default_path="/api/v1/imaging-studies",
    query_params=("modality", "from", "to", "cursor"),
    required_query_params=("modality",),
    request_body_fields=(),
    response_fields=("items", "next_cursor"),
    response_item_fields=("accession_no", "study_uid"),
)

#: 厂商肺结节 AI 推理服务（写：逐例调用）。
INFERENCE_CONTRACT = EndpointContract(
    key="inference",
    title="厂商肺结节 AI 推理服务",
    method="POST",
    auth_env="INFERENCE_API_TOKEN",
    default_path="/api/v1/detections",
    paths={
        "demo-model": "/api/v1/detections",
        "second-model": "/api/v2/nodule-detections",
    },
    request_body_fields=("study_uid", "image_ref", "model_id", "model_version"),
    response_fields=("study_uid", "candidates"),
    response_item_fields=("position", "size_mm", "density", "confidence"),
)

#: 院内科研平台评估登记（写：本产品真正改变业务状态的那条面）。
RESEARCH_CONTRACT = EndpointContract(
    key="research_platform",
    title="院内科研平台评估登记",
    method="POST",
    auth_env="RESEARCH_PLATFORM_TOKEN",
    default_path="/api/v1/model-evaluation-runs",
    request_body_fields=(
        "run_id",
        "model_id",
        "model_version",
        "dataset_ref",
        "metrics",
        "case_rows",
        "submitted_by",
        "submitted_at",
    ),
    response_fields=("registration_id", "run_id", "status", "received_at"),
)

#: 三条出站面的契约（按端点顺序）。
ALL_CONTRACTS: tuple[EndpointContract, ...] = (
    RIS_CONTRACT,
    INFERENCE_CONTRACT,
    RESEARCH_CONTRACT,
)


def contract_for(key: str) -> EndpointContract:
    """按端点键取契约。"""

    for contract in ALL_CONTRACTS:
        if contract.key == key:
            return contract
    raise KeyError(f"没有这条端点的契约：{key}")


def expected_auth_header(contract: EndpointContract, env: Mapping[str, str]) -> str:
    """该端点应当携带的鉴权头取值（``Bearer $环境变量`` 的实际值）。"""

    token = str(env.get(contract.auth_env, "")).strip()
    return f"Bearer {token}"


def check_request(
    contract: EndpointContract,
    request: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    model_id: str = "",
) -> list[str]:
    """把**实际发出**的请求对着契约逐条核，返回违规清单（空 = 全部相符）。

    ``request`` 用的是替身记下的那条记录：
    ``{"method", "path", "query", "headers", "body"}``。

    核这几件事：

    1. 方法与路径（推理服务按模型目录取路径）；
    2. 查询参数：不许有契约外的参数，契约要求的参数不许少；
    3. 鉴权头：等于 ``Bearer $环境变量`` 的实际值；
    4. **令牌不出现在路径 / 查询参数 / 请求体里** —— 令牌只许待在请求头；
    5. 请求体字段集与契约**逐字相符**（含顺序）。
    """

    problems: list[str] = []
    headers = {str(k): str(v) for k, v in (request.get("headers") or {}).items()}
    query = {str(k): str(v) for k, v in (request.get("query") or {}).items()}
    body = request.get("body")
    path = str(request.get("path") or "")

    if str(request.get("method") or "").upper() != contract.method:
        problems.append(
            f"方法不符：期望 {contract.method}，实得 {request.get('method')}"
        )

    expected_path = contract.path_for(model_id)
    if path != expected_path:
        problems.append(f"路径不符：期望 {expected_path}，实得 {path}")

    for name in sorted(query):
        if name not in contract.query_params:
            problems.append(f"出现了契约外的查询参数：{name}")
    for name in contract.required_query_params:
        if name not in query:
            problems.append(f"缺少必需的查询参数：{name}")

    auth = headers.get("Authorization", "")
    expected_auth = expected_auth_header(contract, env)
    if auth != expected_auth:
        problems.append(f"鉴权头不符：期望 {expected_auth!r}，实得 {auth!r}")

    token = str(env.get(contract.auth_env, "")).strip()
    if token:
        haystack = f"{path}?{query} {body!r}"
        if token in haystack:
            problems.append("令牌出现在了路径 / 查询参数 / 请求体里（只许待在请求头）")

    if contract.request_body_fields:
        if not isinstance(body, Mapping):
            problems.append(f"请求体不是 JSON 对象（实得 {type(body).__name__}）")
        else:
            got = tuple(body.keys())
            if got != contract.request_body_fields:
                problems.append(
                    "请求体字段集与契约不符："
                    f"期望 {list(contract.request_body_fields)}，实得 {list(got)}"
                )
    elif body not in (None, "", {}):
        problems.append(f"该端点不该有请求体（实得 {body!r}）")

    return problems


def check_response(
    contract: EndpointContract,
    payload: Any,
) -> list[str]:
    """把响应体对着契约核字段是否齐备（空 = 齐备）。"""

    problems: list[str] = []
    if not isinstance(payload, Mapping):
        return [f"响应顶层不是对象（实得 {type(payload).__name__}）"]

    for name in contract.response_fields:
        if name not in payload:
            problems.append(f"响应缺少字段 {name}")

    items = payload.get("items")
    if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                problems.append(f"第 {index + 1} 条不是对象")
                continue
            for name in contract.response_item_fields:
                if name not in item:
                    problems.append(f"第 {index + 1} 条缺少字段 {name}")

    candidates = payload.get("candidates")
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        for index, item in enumerate(candidates):
            if not isinstance(item, Mapping):
                problems.append(f"第 {index + 1} 个候选不是对象")
                continue
            for name in contract.response_item_fields:
                if name not in item:
                    problems.append(f"第 {index + 1} 个候选缺少字段 {name}")

    return problems
