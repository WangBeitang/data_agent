"""统一分析数据对象（Stage 2 范围）。

依据冻结文档《制造业销售经营归因分析系统_数据对象设计.md》实现本阶段所需公共对象：

- JsonScalar / ObservationStatus / QueryTable / QueryExecutionResult

设计约束（来自冻结 SPEC §3.2 / §5.1）：

- 本文件为中立模型模块，不依赖 LLM、数据库、LangGraph 节点；
- 不放置业务流程函数；
- QueryExecutionResult 不包含 Attribution/Planner/Evidence 信息；
- 后续 Stage 的枚举与模型（RouteResult、Action、Observation、Calculation、
  Evidence、AttributionReport 等）在对应 Stage 再行补充，本阶段不提前实现。
"""

from enum import Enum
from typing import Any, TypeAlias

from pydantic import BaseModel, field_validator, model_validator

# 数据对象设计冻结的 JSON 标量类型
JsonScalar: TypeAlias = str | int | float | bool | None


class ObservationStatus(str, Enum):
    """查询执行结果状态（数据对象设计 §4.8）。"""

    success = "success"
    empty = "empty"
    failed = "failed"


class QueryTable(BaseModel):
    """一次问数的原始结果表（数据对象设计 §5.3）。

    - columns：数据库真实返回的列名（空结果时也必须保留）；
    - rows：原始数据行，值必须已经是 JsonScalar；
    - row_count：数据行数，必须等于 len(rows)。

    数据库值（Decimal、date 等）到 JsonScalar 的归一化发生在
    repository 读取边界（DWMysqlRepository.execute_query），
    本模型只做结构校验，不做值类型转换。
    """

    columns: list[str]
    rows: list[dict[str, JsonScalar]]
    row_count: int

    @field_validator("rows", mode="before")
    @classmethod
    def _check_scalars(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for row in rows:
            for key, value in row.items():
                if value is not None and not isinstance(value, (str, int, float, bool)):
                    raise ValueError(
                        f"QueryTable 行值必须为 JsonScalar，got {type(value).__name__}: {key}"
                    )
        return rows

    @model_validator(mode="after")
    def _check_row_count(self) -> "QueryTable":
        if self.row_count != len(self.rows):
            raise ValueError("QueryTable.row_count 必须等于 len(rows)")
        return self


class QueryExecutionResult(BaseModel):
    """一次问数执行的结构化最终结果（数据对象设计 §8）。

    状态严格遵循冻结定义：

    - success：SQL 成功且 row_count > 0，error 为 null；
    - empty：SQL 成功且 row_count == 0，error 为 null；
    - failed：生成、校验或执行失败，并具有安全的 error（sql 允许为 null）。
    """

    query: str
    sql: str | None
    table: QueryTable
    status: ObservationStatus
    error: str | None

    @model_validator(mode="after")
    def _check_status_consistency(self) -> "QueryExecutionResult":
        if self.status == ObservationStatus.success:
            if not self.sql:
                raise ValueError("success 状态必须携带最终 SQL")
            if self.table.row_count <= 0:
                raise ValueError("success 状态要求 row_count > 0")
            if self.error is not None:
                raise ValueError("success 状态要求 error 为 null")
        elif self.status == ObservationStatus.empty:
            # empty = SQL 执行成功但无数据：sql 必须非空、row_count == 0、error 为 null
            if not self.sql:
                raise ValueError("empty 状态要求 SQL 已成功执行，sql 不能为 null")
            if self.table.row_count != 0:
                raise ValueError("empty 状态要求 row_count == 0")
            if self.error is not None:
                raise ValueError("empty 状态要求 error 为 null")
        elif self.status == ObservationStatus.failed:
            if self.error is None:
                raise ValueError("failed 状态必须具有 error")
        return self
