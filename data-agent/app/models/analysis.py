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

from decimal import Decimal
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


def _to_json_scalar(value: Any) -> JsonScalar:
    """将数据库返回值转换为 JsonScalar。

    - Decimal（MySQL SUM 等返回类型）转为 float；
    - date/datetime 等其它类型转为字符串，保证结果可 JSON 序列化。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


class QueryTable(BaseModel):
    """一次问数的原始结果表（数据对象设计 §5.3）。

    - columns：数据库真实返回的列名（空结果时也必须保留）；
    - rows：原始数据行；
    - row_count：数据行数，必须等于 len(rows)。
    """

    columns: list[str]
    rows: list[dict[str, JsonScalar]]
    row_count: int

    @field_validator("rows")
    @classmethod
    def _coerce_rows(cls, rows: list[dict[str, Any]]) -> list[dict[str, JsonScalar]]:
        return [{k: _to_json_scalar(v) for k, v in row.items()} for row in rows]

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
            if self.error is not None:
                raise ValueError("empty 状态要求 error 为 null")
        elif self.status == ObservationStatus.failed:
            if self.error is None:
                raise ValueError("failed 状态必须具有 error")
        return self
