"""goals 域建表语句 —— 契约 §7（Goal 实体）。

**当前状态：占位。** 本模块是 `FEATURE_SPLIT.md` F-00 的**验收演示**，
证明「新建域模块即自动建表，无需改动 `persistence.py` 或本包任何文件」。

真正的 Goal 表定义由 F-17 落地（依赖 WP-2.0 补写的《G 组自主目标治理补充设计》，
并须先裁决 Goal.status 七态 vs 十值冲突，见 `core/contracts.py`）。

在此之前**不建立 goals 表**：过早固化会与后续设计补写的结果冲突，
且会让 `goal_id` 外键指向一张语义未定的表。契约字段已固定在
`core/contracts.py::GOAL_FIELDS` / `GOAL_STATUS_SPEC` / `GOAL_TYPE`。

对应的迁移版本在 F-17 中以 `migrations.MIGRATIONS` 追加方式加入。
"""

DOMAIN = "goals"

#: 故意为空 —— 见模块说明。保留模块以锁定域名与文档位置。
TABLES: list = []

#: 本域规划中的表（供 F-17 与文档引用，不参与建表）
PLANNED_TABLES = ("goals", "goal_evidence")