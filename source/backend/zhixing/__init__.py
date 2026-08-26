"""知行 Zhixing —— 自动化交易系统(第三代)

版本号的**唯一来源**。其他任何地方不得再出现版本字面量。

二代教训:`settings.py:10` 和 `__init__.py:1` 两处硬编码 "1.2.0",必然飘。
"""

# 版本方案:代数.日期.当日构建号,不是 semver
__version__ = "3.260817.00"

# 系统标识。写进每一份归档,用于把三代的输出和二代区分开。
# 二代写 "tradepilot",三代写 "zhixing"。
SYSTEM_NAME = "zhixing"

__all__ = ["__version__", "SYSTEM_NAME"]
