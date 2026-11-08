"""金标准归并：把放射科写的自由文本读成结构化标签。

**引擎主路径需要先有一个能用的归并口** —— 否则「调用 → 归并 → 判定 → 下一例」
这条链在「归并」这一格是断的（任务 14 落的就是这一格）。

本包按**职责**分成四块：

=====================  ==========================================================
模块                   职责
=====================  ==========================================================
``schema``            输出契约 :class:`GoldLabels` 与标记词表（任务 19 拆出）
``labels``            部位 / 密度等价表（任务 18；金标准侧与模型侧共用的唯一真源）
``rules``             **规则基线**归并（任务 19：存在性 + 部位 + 密度 + 尺寸）
``regression``        否定误召 / 漏召回归集（任务 20；真实写法 + 期望标签）
``merge``             语义归并（任务 21：走 ModelGateway，带 ``evidence_span`` 回指校验）
=====================  ==========================================================

**规则基线不是临时替身**：它既在模型不可达时接管归并（任务 24），
又是任务 25「规则基线 vs 语义归并」对照实验的对照组 —— 所以它自己就跑得出一份
与语义归并**同构**的结论。

**读不出来就说读不出来**：本包判不出的原文交回 ``presence=None``，
由判定层按「没拿到判定」处理（挂人工复核、不进指标分母）——
宁可少一例进统计，也不用一个猜出来的存在性去污染敏感度。

**原文零改写**：本包只读原文（优先读读取层产出的**分析用副本**
``gold_standard_normalized``），一个字都不往回写。
"""

from __future__ import annotations

from typing import Any

from .schema import (
    AFFIRMATIVE_MARKERS,
    NEGATION_MARKERS,
    NORMALIZE_SOURCE_MODEL,
    NORMALIZE_SOURCE_RULE,
    GoldLabels,
)

__all__ = [
    "AFFIRMATIVE_MARKERS",
    "GoldLabels",
    "NEGATION_MARKERS",
    "NORMALIZE_SOURCE_MODEL",
    "NORMALIZE_SOURCE_RULE",
    "normalize_densities",
    "normalize_density",
    "normalize_gold_standard",
    "normalize_location",
    "normalize_locations",
]

#: 等价表的归一函数、回归集的载入函数也从这个包导出 ——
#: **金标准侧与模型侧走同一个入口**，两边不会各写各的归一。
#: 惰性导入（PEP 562）是为了让 ``python -m nodule_eval.goldstd.labels``
#: 不因包初始化时提前导入而报警告。
_LAZY_EXPORTS = frozenset(
    {
        "normalize_location",
        "normalize_locations",
        "normalize_density",
        "normalize_densities",
        "load_regression_set",
        "regression_cases",
    }
)


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        if name.startswith("normalize_"):
            from . import labels

            return getattr(labels, name)
        from . import regression

        return getattr(regression, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def normalize_gold_standard(
    text: str,
    *,
    normalized_text: str | None = None,
) -> GoldLabels:
    """把一段金标准原文读成 :class:`GoldLabels`（**规则基线**）。

    参数
    ----
    text
        金标准**原文**。本函数只读它，一个字都不改。
    normalized_text
        读取层产出的**分析用副本**（全角数字与中文标点已归一）。
        给了就用它匹配标记 —— 原文里写「，」还是「,」不该影响判读。

    **判定规则**（按句 + **否定作用域**，任务 20）：

    - 一句里**有未被否定的病灶词** → 这句说有病灶；
    - 一句里**没有病灶词但有否定词** → 这句说没病灶；
    - 一句里两者都没有 → 这句**不表态**（忽略它）；
    - 否定词**只管它自己那句**：管紧跟着的那处病灶（「未见**明显**结节」），
      管不到隔了「可见」的新陈述（「未见异常**可见**磨玻璃影」→ 阳性）。

    合并口径：**任一句说有 → 存在**；否则**任一句说没有 → 不存在**；
    两句都没有 → **读不出来**（``presence=None``）。

    **为什么这么保守**：一句「未见异常」和一句「见磨玻璃结节」同时出现时，
    整句判阴性会把一次真实的阳性算成漏诊 —— 敏感度直接被拉低。
    宁可把这一例交给人复核，也不用一个猜出来的存在性去污染指标。

    属性三层（部位 / 密度 / 尺寸）由规则基线一并给出，**只从未被否定的
    病灶词上取**；认不出来时留 ``None`` 并把原词记进
    :class:`~nodule_eval.goldstd.rules.RuleDiagnostics`，**绝不猜一个近似标签**。
    多病灶时逐处落 ``GoldLabels.lesions``（顶层三个属性是首个病灶的投影）。
    需要过程台账时直接调 :func:`nodule_eval.goldstd.rules.merge_by_rules`。
    """

    # 规则基线放在函数里导入：包初始化时**不**提前导入 labels，
    # ``python -m nodule_eval.goldstd.labels`` 才不会报「模块已被导入」告警。
    from .rules import merge_by_rules

    return merge_by_rules(text, normalized_text=normalized_text).labels
