"""语义归并提示词的**版本化加载**与**口径校验**（任务 47）。

**为什么提示词要外置、还要带版本号**：归并口径（怎么把一句放射科原文
读成「有没有 / 什么部位 / 多大 / 什么密度」）是会变的 —— 换个写法读法，
同一批例子的结论就可能不一样。而模型归并的结论会**直接进评估结论**
（敏感度、特异度都从它算出来）。口径变了却说不清「这批结论是哪版跑的」，
评估结论就不可追、不可复现。

所以本模块把三件事钉死：

1. **提示词住在文件里，不住在代码里**（沿用源产品
   ``github_ref/MedRAX/main.py:47-48`` 的 ``load_prompts_from_file``
   与 ``prompts["MEDICAL_ASSISTANT"]`` 做法）。改口径 = 改文件，
   **不动判定逻辑代码**。
2. **版本号住在文件名里** —— ``prompts/goldstd_merge.v1.md`` 的 ``v1``。
   改口径就**另存一版**（``v2``），老版留在仓库里；旧结论仍能对回
   它当时用的那一版。文件哈希（``sha256``）认「内容是不是同一份」，
   版本号认「这是第几版口径」—— 两个都要，缺一不可。
3. **提示词与输出契约必须对得上** —— 提示词里声明的字段要和
   :mod:`nodule_eval.contracts` 里的输出契约**逐字一致**：提示词要了
   契约里没有的字段（模型照着输出 → 解析层拒收），或者契约里必填的字段
   提示词没要求（模型不输出 → 每次都判解析失败）。
   口径写在提示词里、契约写在代码里，两边各写一份就会漂移。

**提示词里的安全边界要落到代码校验**：提示词必须写明「不改写原文」
与「不输出诊断、结论或治疗建议」；而模型输出里真的冒出诊断结论类字段
（``diagnosis`` / ``impression`` / ``诊断`` …）时，
:func:`nodule_eval.goldstd.merge.parse_merge_output` **判解析失败** →
走规则降级（任务 24）。光在提示词里写一句「不许输出诊断」是不够的 ——
模型不总是听话，代码得拦。

**没有兜底提示词**：提示词文件读不到、占位符缺了、字段对不上、
安全条款缺了 —— 一律抛 ``CONTRACT_INVALID``。兜底提示词会让
「这轮用的哪版口径」查不出来，比直接报错糟得多。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..audit.modelcall import parse_prompt_version
from ..contracts import (
    MERGE_LESION_FIELDS,
    MERGE_OUTPUT_FIELDS,
    MERGE_REQUIRED_FIELDS,
)
from ..errors import ErrorCode, NoduleEvalError

__all__ = [
    "DEFAULT_PROMPT_PATH",
    "PROMPT_FILENAME_PATTERN",
    "PROMPT_NAME",
    "PROMPT_SAFETY_CLAUSES",
    "PromptBundle",
    "assert_prompt_matches_contract",
    "assert_prompt_safety",
    "extract_json_example",
    "load_prompt_bundle",
]


#: 语义归并提示词的**缺省文件**（外置、可版本化）。
#:
#: 位置在项目根的 ``prompts/`` 下 —— 与源产品把 system prompt 放在
#: ``medrax/docs/system_prompts.txt``（``github_ref/MedRAX/main.py:117``
#: 传的那个路径）是同一个做法。
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "goldstd_merge.v1.md"

#: 提示词文件的**命名约定**：``<名字>.v<版本号>.md``。
#:
#: 名字与版本号都必须在文件名里 —— 没有版本号的提示词文件，
#: 「这批结论是哪版口径跑的」就答不出来。
PROMPT_FILENAME_PATTERN = re.compile(
    r"^(?P<name>[a-z][a-z0-9_]*)\.v(?P<version>\d+)\.md$"
)

#: 提示词的**缺省名字**（语义归并用）。
PROMPT_NAME = "goldstd_merge"

#: 提示词里**必须写明**的安全条款。
#:
#: 每一项是一组**可接受的措辞**（任一命中即算写明）——
#: 条款的**措辞**可以改，**条款本身**不能少。少一条就报错，
#: 不静默放行。
PROMPT_SAFETY_CLAUSES: dict[str, tuple[str, ...]] = {
    # 原文是科室维护的输入，归并只读不写。
    "no_rewrite": ("不改写原文", "不得改写原文", "不许改写原文"),
    # 本产品只做结构化标注，不出诊断 / 结论 / 建议。
    "no_diagnosis": ("不输出诊断", "不给诊断", "不输出诊断、结论"),
}

#: 提示词里的**标签表占位符** —— 由等价表在运行时填进去，
#: **标签表换了不用改提示词**（等价表是唯一真源）。
LOCATIONS_TOKEN = "{{LOCATIONS}}"
DENSITIES_TOKEN = "{{DENSITIES}}"

#: 提示词里 JSON 输出示例所在的围栏。
#:
#: 整个围栏内容一起取（不是只取第一对大括号）—— 示例里有多病灶数组，
#: 嵌套大括号按正则的非贪婪匹配会被截断。
_JSON_FENCE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def _contract_error(detail: str) -> NoduleEvalError:
    """提示词口径类失败统一走 ``CONTRACT_INVALID``。"""

    return NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail=detail)


@dataclass(frozen=True)
class PromptBundle:
    """一版提示词的**全部身份信息**（路径 / 版本号 / 哈希 / 正文）。

    ``name`` / ``version``
        从文件名解析。``version`` 为 ``v1`` 这样的形式；
        文件名不合约定 → 载入即报错（不是空串）。
    ``path`` / ``relative_path``
        文件的绝对路径与**相对项目根**的路径（底账里落后者）。
    ``sha256``
        **文件内容哈希** —— 落进模型调用底账的 ``prompt_sha256``，
        事后凭它反查回这一版文件。
    ``template_text`` / ``rendered_text``
        文件原文与**渲染后**的提示词（标签表已填进去）。
        只有 ``rendered_text`` 会发出去；底账里只留它的哈希。
    ``example_fields``
        提示词示例 JSON 里声明的输出字段 —— 与输出契约逐字比对过。
    """

    name: str
    version: str
    path: Path
    relative_path: str
    sha256: str
    template_text: str
    rendered_text: str
    example_fields: tuple[str, ...] = ()

    @property
    def prompt_ref(self) -> Any:
        """这一版提示词的**审计引用**（路径 + 版本号 + 哈希）。

        类型是 :class:`nodule_eval.audit.modelcall.PromptRef`；这里不 import
        是为了避免审计层与业务层互相 import（审计层更低一层）。
        """

        from ..audit.modelcall import PromptRef

        return PromptRef(
            path=self.relative_path, sha256=self.sha256, version=self.version
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "path": self.relative_path,
            "sha256": self.sha256,
            "example_fields": list(self.example_fields),
        }


def _project_root() -> Path:
    """项目根（``src/nodule_eval/goldstd/prompt.py`` → 上溯三级）。"""

    return Path(__file__).resolve().parents[3]


def _relative_to_project(path: Path) -> str:
    """把绝对路径压成**相对项目根**的路径（压不动就只留文件名）。"""

    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(_project_root()))
    except ValueError:
        return resolved.name


def extract_json_example(text: str) -> dict[str, Any]:
    """从提示词正文里取出**JSON 输出示例**。

    提示词里必须有一份可直接照抄的输出示例 —— 没有示例，
    「提示词要求的字段」就没法核对（模型看到什么形状全靠示例）。

    取不到 → 抛 ``CONTRACT_INVALID``（不静默跳过校验）。
    """

    blocks = _JSON_FENCE.findall(text or "")
    if not blocks:
        raise _contract_error(
            "语义归并提示词里没有 JSON 输出示例（```json 代码块）"
            "—— 示例缺了就核不准提示词要求的字段"
        )
    last_error = ""
    for block in blocks:
        try:
            payload = json.loads(block)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            continue
        if isinstance(payload, Mapping):
            return dict(payload)
        last_error = f"顶层不是对象（实得 {type(payload).__name__}）"
    raise _contract_error(
        f"语义归并提示词里的 JSON 输出示例解析失败：{last_error}"
    )


def assert_prompt_matches_contract(text: str, *, where: str = "语义归并提示词") -> tuple[str, ...]:
    """校验提示词声明的字段与**输出契约一致**。

    三个方向都要对得上：

    1. **提示词示例里不许有契约之外的字段** —— 提示词教模型输出
       ``diagnosis``，模型就会输出，解析层就会拒收 —— 那是白跑一趟；
    2. **契约里的必填字段，提示词必须都声明** —— 提示词没要求，
       模型就不给，每次都判解析失败；
    3. **多病灶条目里的字段**同样只允许契约里的那几个。

    返回示例里声明的字段名（元组），供调用方记进产物。

    不一致 → 抛 ``CONTRACT_INVALID`` 并**写明差在哪**。
    """

    example = extract_json_example(text)

    allowed = set(MERGE_OUTPUT_FIELDS) | {"lesions"}
    unknown = sorted(set(example) - allowed)
    if unknown:
        raise _contract_error(
            f"{where}声明了输出契约之外的字段：{'、'.join(unknown)}"
            f"（合法字段只有 {'、'.join(sorted(allowed))}）"
        )

    missing = [name for name in MERGE_REQUIRED_FIELDS if name not in example]
    if missing:
        raise _contract_error(
            f"{where}的输出示例缺必填字段：{'、'.join(missing)}"
            "（这些字段缺一个，模型输出就不合契约）"
        )

    lesions = example.get("lesions")
    if lesions is not None:
        if not isinstance(lesions, list):
            raise _contract_error(
                f"{where}的输出示例里 lesions 不是数组"
                f"（实得 {type(lesions).__name__}）"
            )
        for index, item in enumerate(lesions):
            if not isinstance(item, Mapping):
                raise _contract_error(
                    f"{where}的输出示例里 lesions[{index}] 不是对象"
                )
            item_unknown = sorted(set(item) - set(MERGE_LESION_FIELDS))
            if item_unknown:
                raise _contract_error(
                    f"{where}的输出示例里 lesions[{index}] 声明了契约之外的字段："
                    f"{'、'.join(item_unknown)}"
                    f"（合法字段只有 {'、'.join(MERGE_LESION_FIELDS)}）"
                )
            item_missing = [name for name in MERGE_LESION_FIELDS if name not in item]
            if item_missing:
                raise _contract_error(
                    f"{where}的输出示例里 lesions[{index}] 缺字段："
                    f"{'、'.join(item_missing)}"
                )

    return tuple(sorted(example))


def assert_prompt_safety(text: str, *, where: str = "语义归并提示词") -> None:
    """校验提示词写明了**安全边界**。

    两条都要在：

    - **不改写原文** —— 金标准原文由科室维护，归并只读不写；
    - **不输出诊断、结论或治疗建议** —— 本产品只做结构化标注。

    缺哪条就报哪条（写明缺的是哪条），**不静默放行**。
    提示词里没写 ≠ 模型不会输出；真正拦得住的是解析层的字段校验
    （见 :func:`nodule_eval.goldstd.merge.parse_merge_output`），
    这里守的是「口径文件本身别把边界丢了」。
    """

    body = text or ""
    for clause, markers in PROMPT_SAFETY_CLAUSES.items():
        if not any(marker in body for marker in markers):
            label = "不改写原文" if clause == "no_rewrite" else "不输出诊断、结论或治疗建议"
            raise _contract_error(
                f"{where}缺安全条款：{label}（需要出现其中一种写法："
                f"{' / '.join(markers)}）"
            )


def load_prompt_bundle(
    prompt_path: Path | str | None = None,
    *,
    locations: Sequence[str] | None = None,
    densities: Sequence[str] | None = None,
    require_version: bool = True,
) -> PromptBundle:
    """载入一版提示词，并把它的**身份**与**口径**一起校验掉。

    参数
    ----
    prompt_path
        提示词文件（不给就用 :data:`DEFAULT_PROMPT_PATH`）。
    locations / densities
        标签表（不给就从等价表取当前值）。
    require_version
        是否强制文件名带版本号。默认提示词**要**（``v1``）；
        临时试写的提示词可以放行（``require_version=False``）。

    校验顺序（先便宜后贵，失败原因最贴切的那条先报）：

    1. 文件存在、读得出来；
    2. 文件名带版本号（``require_version=True`` 时）；
    3. 标签表占位符在；
    4. 输出示例与输出契约一致；
    5. 安全条款写明了。

    任何一条不满足 → 抛 ``CONTRACT_INVALID``，**不回落内置兜底提示词**
    （兜底会让「这轮用的哪版口径」查不出来）。
    """

    path = Path(prompt_path) if prompt_path is not None else DEFAULT_PROMPT_PATH
    if not path.is_file():
        raise _contract_error(f"语义归并提示词文件不存在：{path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _contract_error(
            f"语义归并提示词文件读取失败：{path}（{exc}）"
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _contract_error(
            f"语义归并提示词文件不是 UTF-8 编码：{path}（{exc}）"
        ) from exc

    match = PROMPT_FILENAME_PATTERN.match(path.name)
    if require_version and match is None:
        raise _contract_error(
            f"语义归并提示词文件名必须带版本号：{path.name}"
            f"（约定形如 {PROMPT_NAME}.v1.md —— 改口径就另存一版）"
        )
    name = match.group("name") if match else path.stem
    version = parse_prompt_version(path)

    if LOCATIONS_TOKEN not in text or DENSITIES_TOKEN not in text:
        raise _contract_error(
            f"语义归并提示词缺标签表占位符：{path}"
            f"（需要 {LOCATIONS_TOKEN} 与 {DENSITIES_TOKEN}）"
        )

    example_fields = assert_prompt_matches_contract(text, where=f"提示词 {path.name}")
    assert_prompt_safety(text, where=f"提示词 {path.name}")

    from .labels import canonical_densities, canonical_locations

    location_list = list(locations) if locations is not None else list(canonical_locations())
    density_list = list(densities) if densities is not None else list(canonical_densities())
    rendered = (
        text.replace(LOCATIONS_TOKEN, "、".join(location_list))
        .replace(DENSITIES_TOKEN, "、".join(density_list))
    )

    return PromptBundle(
        name=name,
        version=version,
        path=path,
        relative_path=_relative_to_project(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        template_text=text,
        rendered_text=rendered,
        example_fields=example_fields,
    )
