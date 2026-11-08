"""金标准归并的**输出契约**（任务 19 从 ``goldstd/__init__.py`` 拆出来）。

**为什么单独拆一个模块**：归并分三层落地 ——
存在性（任务 14）、属性（任务 18 / 19）、语义（任务 21）——
三层都要吐**同一份**结构。规则基线（:mod:`nodule_eval.goldstd.rules`）与
语义归并（:mod:`nodule_eval.goldstd.merge`，任务 21）互相不认识，
只能靠这一份契约对齐；契约自己待在这个模块里，两边都从这里 import，
不会绕成环（``rules`` → ``schema`` → 空，``merge`` → ``schema`` → 空）。

**判定层只认 canonical**：部位与密度的标准标签由
:mod:`nodule_eval.goldstd.labels` 的等价表给出，金标准侧与模型侧共用同一张表；
本模块只定义**形状**，不定义标签集。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "AFFIRMATIVE_MARKERS",
    "GoldLabels",
    "LesionLabels",
    "NEGATION_MARKERS",
    "NORMALIZE_SOURCE_MODEL",
    "NORMALIZE_SOURCE_RULE",
]

#: 金标准归并来源：``rule``（规则基线）/ ``model``（语义归并，任务 21）。
#:
#: 这两个取值是**契约层**的合法取值（``contracts.NORMALIZE_SOURCES`` 与之逐字一致）——
#: 评估表 ``normalize_source`` 列只写这两个值之一。
NORMALIZE_SOURCE_RULE = "rule"
NORMALIZE_SOURCE_MODEL = "model"

#: **否定标记** —— 命中即「这一句没说有结节」。
#:
#: 「未见」「未发现」「无明显」这类词是放射科写「没有」的固定说法。
NEGATION_MARKERS: tuple[str, ...] = (
    "未见",
    "未发现",
    "未探及",
    "未闻及",
    "无明显",
    "无异常",
    "无结节",
    "无病灶",
    "无发现",
    "无新发",
    "无典型",
    "未见异常",
    "未见明显",
    "未见明确",
    "未见典型",
    "未见新发",
    "不存在",
    "排除",
    "除外",
)
#: 刻意**不收单字「无」**：它在「无心影」「无特殊病史」这类句子里先出现，
#: 会把一句「无特殊病史右下肺见结节」判成阴性 —— 一次假阴性就是一次假漏诊。
#: 只收「无明显」「无异常」「无结节」这类**完整说法**。

#: **肯定标记** —— 命中即「这一句说了有病灶」。
AFFIRMATIVE_MARKERS: tuple[str, ...] = (
    "结节",
    "磨玻璃",
    "ggn",
    "ggo",
    "实性",
    "病灶",
    "肿块",
    "阴影",
    "斑片",
    "粟粒",
    "钙化",
    "空洞",
    "囊性",
)


@dataclass(frozen=True)
class LesionLabels:
    """**一处**病灶的标签（多病灶口径：一句里说了几处就落几条）。

    ``location`` / ``size_mm`` / ``density``
        这一处病灶的属性三层。认不出来时留 ``None``，**绝不猜**。
    ``finding``
        病灶词原文（「磨玻璃结节」「结节影」…）—— 复核时一眼看到这一处
        到底说的是什么。
    ``evidence_span``
        这一处病灶所在的原文片段（指回原文，可复核）。
    """

    location: str | None = None
    size_mm: float | None = None
    density: str | None = None
    finding: str = ""
    evidence_span: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "location": self.location,
            "size_mm": self.size_mm,
            "density": self.density,
            "finding": self.finding,
            "evidence_span": self.evidence_span,
        }


@dataclass(frozen=True)
class GoldLabels:
    """金标准归并的结果。

    ``presence``
        **存在性**：金标准说的是「有病灶」还是「没病灶」。
        ``None`` = **读不出来**（原文里既没肯定也没否定）——
        调用方必须按「没拿到判定」处理，不许当成阴性。
    ``location`` / ``size_mm`` / ``density``
        属性三层 —— 标准标签（部位与密度）与毫米值（尺寸）。
        认不出来时留 ``None``，**绝不猜一个近似标签**。
        取的是**首个病灶**的值；首个病灶没给这个属性时，回落到第一个
        给得出该值的病灶。
    ``lesions``
        **多病灶标签集合**（任务 20）：每一处病灶一条。
        顶层三个属性是它的投影，判定层（任务 27 的最严口径）拿它逐处比对。
    ``evidence_span``
        判出存在性的那一句原文（截断到 120 字）。复核的人靠它一眼看到
        「这一例凭什么这么判」。
    ``confidence``
        归并置信度。规则基线**没有**置信度可言（``None``）；
        语义归并（任务 21）给模型自评的置信度。
    ``needs_human_review``
        这一例的归并**不足以支撑判定** —— 挂人工复核。
    ``source``
        归并来源：``rule``（规则基线）/ ``model``（语义归并，任务 21）。
    ``degraded``
        这一例的归并走了**降级路径**（任务 24）：模型这一层没成功 ——
        端点不可达 / 请求被拒 / 输出不合契约 / 证据回指对不上 ——
        结论改由**规则基线**给出。

        **降级不等于失败**：降级例**仍进指标分母**（结论是确定的），
        但单列「降级数」并挂人工复核。把降级例排除出分母，
        等于把「模型这一层没跑成」算成「模型没看见」，敏感度会被凭空拉低。
    ``degrade_reason``
        降级原因（中文）—— 模型为什么没成功。
    ``degrade_error_code``
        触发降级的错误码（``MODEL_UNAVAILABLE`` / ``MODEL_REQUEST_REJECTED`` /
        ``FIELD_MAPPING_MISSING`` / ``CONTRACT_INVALID``）。
    """

    presence: bool | None = None
    location: str | None = None
    size_mm: float | None = None
    density: str | None = None
    lesions: tuple[LesionLabels, ...] = ()
    evidence_span: str = ""
    confidence: float | None = None
    needs_human_review: bool = False
    source: str = NORMALIZE_SOURCE_RULE
    degraded: bool = False
    degrade_reason: str = ""
    degrade_error_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "presence": self.presence,
            "location": self.location,
            "size_mm": self.size_mm,
            "density": self.density,
            "lesions": [lesion.to_dict() for lesion in self.lesions],
            "evidence_span": self.evidence_span,
            "confidence": self.confidence,
            "needs_human_review": self.needs_human_review,
            "source": self.source,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "degrade_error_code": self.degrade_error_code,
        }
