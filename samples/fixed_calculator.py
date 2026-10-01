"""average() 的修复版实现。

修复要点（对应审查报告中的问题 2、3、8）：
- 空序列不再抛 ZeroDivisionError，而是显式抛 ValueError；
- 先物化为 list，兼容生成器 / 迭代器 / dict 视图等任意可迭代对象；
- 使用 math.fsum 累加，减少浮点误差。
"""

from __future__ import annotations

import math
from collections.abc import Iterable


def average(numbers: Iterable[float]) -> float:
    """返回 numbers 的算术平均值。

    Args:
        numbers: 任意可迭代对象（list / tuple / 生成器 / dict 视图等）。

    Returns:
        平均值，类型为 float。

    Raises:
        ValueError: 当 numbers 为空序列时。
        TypeError: 当元素不支持数值运算时。
    """
    values = list(numbers)          # 兼容生成器/迭代器，避免依赖 len()
    if not values:
        raise ValueError("average() 不接受空序列")
    return math.fsum(values) / len(values)
