"""边界场景的加载与替身构造（**只在测试层**）。

场景样例落在 ``tests/fixtures/scenarios/*.json``，每个文件同时描述：

- **期望的错误码**（``expected_error_code``）与口径标记（``counts_toward_metrics``）
- **替身该怎么配**（``stub`` 块）：``kind`` + 该端点替身认识的参数

同一份 JSON 既喂给替身（造出边界响应），又喂给断言（检查产品侧落到了哪个错误码）——
这样「样例」与「期望」不会各写一份、走偏。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

#: 场景样例目录。
SCENARIOS_DIR = Path(__file__).resolve().parent / "fixtures" / "scenarios"


def load_scenario(name: str) -> dict[str, Any]:
    """按文件名读一份场景样例（不含 ``.json`` 后缀）。"""

    path = SCENARIOS_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"场景样例不存在：{path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):  # pragma: no cover - 样例文件写坏时
        raise ValueError(f"场景样例 {path} 顶层须为对象")
    return dict(payload)


def scenario_names() -> list[str]:
    """列出全部场景样例名（按文件名排序）。"""

    return sorted(path.stem for path in SCENARIOS_DIR.glob("*.json"))


def stub_spec(scenario: Mapping[str, Any]) -> dict[str, Any]:
    """取场景里的替身配置块。"""

    spec = scenario.get("stub")
    if not isinstance(spec, Mapping):
        raise ValueError(f"场景 {scenario.get('scenario')} 缺少 stub 配置块")
    return dict(spec)


def build_stub(scenario: Mapping[str, Any]) -> Any:
    """按场景的 ``stub`` 块造出对应的替身实例（未启动）。

    ``kind`` 取 ``ris`` / ``inference`` / ``research_platform``；
    ``model`` 类的「不可达」场景不造替身（它靠指向不可达端口来表达）。
    """

    spec = stub_spec(scenario)
    kind = str(spec.get("kind") or "")

    if kind == "ris":
        from stub_ris import RISStub

        return RISStub(
            items=spec.get("items") or [],
            page_size=int(spec.get("page_size") or 0),
            status_sequence=spec.get("status_sequence"),
        )

    if kind == "inference":
        from stub_inference import InferenceStub

        return InferenceStub(
            candidates=spec.get("candidates"),
            status_sequence=spec.get("status_sequence"),
            omit_fields=spec.get("omit_fields") or (),
            omit_candidate_fields=spec.get("omit_candidate_fields") or (),
            echo_study_uid=bool(spec.get("echo_study_uid", True)),
        )

    if kind == "research_platform":
        from stub_research_platform import ResearchPlatformStub

        return ResearchPlatformStub(
            status_sequence=spec.get("status_sequence"),
            reject_body=spec.get("reject_body"),
        )

    raise ValueError(f"未知的替身类型：{kind}")
