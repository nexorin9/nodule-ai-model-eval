"""**语义参照读法** —— 对照实验里「模型侧」的本地实现（**只在测试层**）。

**它是什么**：一份**确定性**的读法，比规则基线多认得一些写法：

- 规则基线的**病灶词表**收的是「结节 / 磨玻璃 / 实性 / 病灶 / 肿块 / 阴影 / 斑片 / 粟粒 /
  钙化 / 空洞 / 囊性」这些**词形**；放射科报告里还有一大类写法是
  **形态描述 + 密度**（「类圆形高密度影」「条片状高密度影」）——
  规则基线认不出来，整句读不出存在性。本模块补的就是这一层；
- 规则基线**从原文中找部位 / 尺寸 / 密度**时要求那一处先被认成「病灶」；
  认不成就没有病灶，也就没有属性。本模块在判定为阳性之后，
  仍会回到原文里把部位 / 尺寸 / 密度**捞回来**。

**它不是什么**：它**不是**大模型。这是一段确定性的 Python 代码，
回答同样的问题、认同样的写法，但**没有**大模型的泛化能力。
因此对照表里模型一侧的数字**只代表「一个比规则基线宽一点的确定性读法」**，
**不代表真实模型的能力** —— 报告与结论摘要里都会写明这一点。

**为什么放在 ``tests/`` 层**：它是**替身的一部分**（本地协议替身背后的应答逻辑），
产品代码（``src/``）不 import 它，默认出站路径也永远不会命中它。

**它仍然要过产品侧的全部校验**：回指校验（``evidence_span`` 必须是原文的连续子串）、
标签复核（部位 / 密度须能在等价表里归一且原文里确实写着、尺寸须与原文里的数值对得上）
—— 都在 :mod:`nodule_eval.goldstd.merge` 里，与真模型走**同一套**校验。
所以「本地参照读法」能给出的结论，**是真模型也给得出的**（受同一套约束）。
"""

from __future__ import annotations

import re
from typing import Any

from nodule_eval.goldstd.labels import find_densities, find_locations
from nodule_eval.goldstd.rules import find_sizes, merge_by_rules

__all__ = [
    "BROAD_FINDING_TERMS",
    "answer_for_text",
    "extract_user_message",
]


#: **比规则基线更宽的病灶词表** —— 放射科报告里常见的形态描述 + 密度写法。
#:
#: 这些词在 :mod:`nodule_eval.goldstd.schema` 的 ``AFFIRMATIVE_MARKERS`` 里**没有**，
#: 规则基线遇到它们会整句读不出存在性。列在这里是**逐条**加的 ——
#: 加一个词就等于「规则基线的一个已知缺口被补上了」。
BROAD_FINDING_TERMS: tuple[str, ...] = (
    "类圆形高密度影",
    "条片状高密度影",
    "高密度影",
    "类圆形影",
    "圆形影",
    "结节灶",
    "小结节灶",
    "粟粒灶",
    "索条影",
    "斑片影",
    "斑点影",
    "类结节",
    "结节样",
    "占位",
    "白斑",
)

#: 句末标点（切句用）。
_SENTENCE_SPLIT = re.compile(r"[。；;！!？?\n]")


def extract_user_message(request_body: Any) -> str:
    """从 OpenAI-compatible 请求体里取出**用户消息**（也就是金标准原文）。

    取不到就返回空串 —— 调用方会看到「模型读到的是空文本」，
    而不是悄悄拿别的东西顶上。
    """

    if not isinstance(request_body, dict):
        return ""
    messages = request_body.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def _broad_finding_sentence(text: str) -> str:
    """找出带**宽词表**病灶词的那一句（原文的连续子串）。

    找不到就返回空串 —— **不猜**。
    """

    for sentence in _SENTENCE_SPLIT.split(text):
        if not sentence:
            continue
        for term in BROAD_FINDING_TERMS:
            if term in sentence:
                return sentence
    return ""


def answer_for_text(text: str) -> dict[str, Any]:
    """给一段金标准原文，返回一份**归并输出契约**形状的答案。

    形状与 :data:`nodule_eval.goldstd.merge.MERGE_JSON_SCHEMA` 一致
    （``presence`` / ``location`` / ``size_mm`` / ``density`` /
    ``evidence_span`` / ``confidence`` / ``needs_human_review``）。

    **捞属性的三条纪律**（错一条就会把别处病灶的属性串过来）：

    1. **范围**：只在**证据句**里捞 —— 证据句是「宽词表认出的那一处病灶所在的一句」。
       整句捞会把「两肺纹理增粗」里的「双肺」当成病灶部位；
    2. **不覆盖已有归属**：规则基线已经认出病灶（``lesions`` 非空）时，
       **不重认部位与密度** —— 那些属性已经归到具体某处病灶上了，
       再从原文捞一遍就是「属性串味」（项目最在意的那类误召）；
    3. **只认唯一**：尺寸 / 密度在范围内**只有一处**时才捞 ——
       有多处就不猜是哪一处的。
    """

    rule = merge_by_rules(text)
    labels = rule.labels

    presence = labels.presence
    evidence = labels.evidence_span
    source = "rule"

    if presence is None:
        # 规则基线读不出存在性 —— 宽词表再找一次。
        sentence = _broad_finding_sentence(text)
        if sentence:
            presence = True
            evidence = sentence
            source = "broad-lexicon"
        elif evidence:
            # 规则基线已经标出了「不表态」的那一句 —— 交白卷。
            presence = None
            source = "unreadable"

    location = labels.location
    size_mm = labels.size_mm
    density = labels.density

    if presence is True:
        # 范围：证据句优先（宽词表认出的那一处），否则整段原文。
        scope = evidence if source == "broad-lexicon" else text
        first_lesion = labels.lesions[0] if labels.lesions else None
        no_lesion = first_lesion is None

        if location is None and no_lesion:
            found = find_locations(scope)
            if found:
                location = found[0].canonical
        if size_mm is None:
            sizes = find_sizes(scope)
            if len(sizes) == 1:
                size_mm = sizes[0].value_mm
        if density is None and no_lesion:
            densities = find_densities(scope)
            if len(densities) == 1:
                density = densities[0].canonical

    if not evidence:
        evidence = text[:120]

    return {
        "presence": presence,
        "location": location,
        "size_mm": size_mm,
        "density": density,
        "evidence_span": evidence.strip(),
        # 宽词表补出来的结论比规则基线自己的结论弱一点 —— 置信度写低，
        # 复核的人一眼看得出这一例的结论出处不同。
        "confidence": (0.75 if source == "broad-lexicon" else 0.9)
        if presence is not None
        else 0.0,
        "needs_human_review": presence is None,
    }
