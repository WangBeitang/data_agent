"""统一分析数据对象（Stage 2 + Stage 3 + Stage 4 范围）。

依据冻结文档《制造业销售经营归因分析系统_数据对象设计.md》实现当前阶段所需公共对象：

- Stage 2：JsonScalar / ObservationStatus / QueryTable / QueryExecutionResult
- Stage 3：RequestMode / AnalysisMode / RouteSource / AnalysisStatus / RouteResult
- Stage 4：ActionType / MetricKey / DimensionKey / CalculationType /
  FactorDirection / FilterOperator / Period / FilterCondition /
  MetricPeriodValue / ComparisonRow / AttributionTarget / Action /
  Observation / PeriodChangeCalculation / ContributionItem /
  ContributionCalculation / UnitPriceCalculation / Evidence

设计约束（来自冻结 SPEC §3.2 / §5.1 / 数据对象设计 §20）：

- 本文件为中立模型模块，不依赖 LLM、数据库、LangGraph 节点；
- 不放置业务流程函数；
- QueryExecutionResult 不包含 Attribution/Planner/Evidence 信息；
- AttributionReport 及其子对象属于 Stage 5，本阶段不提前实现；
- 不拆 enums.py，不新建 models 子包。
"""

from datetime import date
from enum import Enum
from typing import Any, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 数据对象设计冻结的 JSON 标量类型
JsonScalar: TypeAlias = str | int | float | bool | None


class ObservationStatus(str, Enum):
    """查询执行结果状态（数据对象设计 §4.8）。"""

    success = "success"
    empty = "empty"
    failed = "failed"


class RequestMode(str, Enum):
    """请求模式（API QueryRequest.mode，接口设计 §4.1）。"""

    auto = "auto"
    query = "query"
    attribution = "attribution"


class AnalysisMode(str, Enum):
    """最终分析模式（route.resolved_mode / done.mode）。

    只允许 query / attribution，永远不允许 auto。
    """

    query = "query"
    attribution = "attribution"


class RouteSource(str, Enum):
    """意图路由来源（route.source）。"""

    forced = "forced"
    rule = "rule"
    llm = "llm"


class AnalysisStatus(str, Enum):
    """一次分析整体状态（done.status）。"""

    running = "running"
    completed = "completed"
    partial = "partial"
    failed = "failed"


class RouteResult(BaseModel):
    """意图路由结果（数据对象设计 §4.3 / API 接口设计 §7 route 事件）。

    - requested_mode：请求模式（auto/query/attribution）；
    - resolved_mode：最终模式，只允许 query/attribution；
    - source：forced / rule / llm；
    - rule：简短可展示规则文本，无则 null。
    """

    requested_mode: RequestMode
    resolved_mode: AnalysisMode
    source: RouteSource
    rule: str | None = None

    @model_validator(mode="after")
    def _check_resolved_mode(self) -> "RouteResult":
        # AnalysisMode 本身只含 query/attribution，此处为契约性兜底校验
        if self.resolved_mode not in (AnalysisMode.query, AnalysisMode.attribution):
            raise ValueError("resolved_mode 只能是 query 或 attribution")
        return self


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


# ==================== Stage 4：归因确定性核心数据对象 ====================
# 数据对象设计 §4.5～§4.11 基础枚举


class ActionType(str, Enum):
    """归因 Action 白名单（数据对象设计 §4.5 / 概要设计 §10）。

    枚举本身即形成第一层白名单：第一版严格只允许以下 8 类动作。
    """

    compare_period = "compare_period"
    breakdown_region = "breakdown_region"
    breakdown_category = "breakdown_category"
    breakdown_product = "breakdown_product"
    breakdown_customer = "breakdown_customer"
    analyze_unit_price = "analyze_unit_price"
    calculate_contribution = "calculate_contribution"
    finish_analysis = "finish_analysis"


class MetricKey(str, Enum):
    """核心销售经营指标（数据对象设计 §4.6）。"""

    sales_amount = "sales_amount"
    sales_quantity = "sales_quantity"
    order_count = "order_count"
    avg_unit_sales_amount = "avg_unit_sales_amount"


class DimensionKey(str, Enum):
    """归因拆解维度白名单（数据对象设计 §4.7）。

    第一版不允许生产、库存、设备、质量、成本、利润、产能等维度。
    """

    region = "region"
    category = "category"
    product = "product"
    customer = "customer"
    customer_level = "customer_level"


class CalculationType(str, Enum):
    """确定性计算类型（数据对象设计 §4.9）。"""

    period_change = "period_change"
    contribution = "contribution"
    unit_price = "unit_price"


class FactorDirection(str, Enum):
    """维度成员方向（数据对象设计 §4.10）。"""

    driver = "driver"
    offset = "offset"
    neutral = "neutral"


class FilterOperator(str, Enum):
    """Filter 白名单运算符（数据对象设计 §4.11）。

    第一版只开放 eq / in，不允许 where_sql、SQL 运算符或任意表达式。
    """

    eq = "eq"
    in_ = "in"


# ==================== 公共值对象（数据对象设计 §5） ====================


class Period(BaseModel):
    """闭区间分析期间（数据对象设计 §5.1）。"""

    label: str
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _check_date_range(self) -> "Period":
        if self.start_date > self.end_date:
            raise ValueError("Period.start_date 必须 <= end_date")
        return self


class FilterCondition(BaseModel):
    """受控下钻过滤条件（数据对象设计 §5.2）。

    只允许 operator=eq|in、dimension=DimensionKey、values 至少 1 个；
    不允许 where_sql / sql / 任意表达式（extra="forbid" 拒绝任何多余字段）。
    """

    model_config = ConfigDict(extra="forbid")

    dimension: DimensionKey
    operator: FilterOperator
    values: list[str] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def _check_values(cls, values: list[str]) -> list[str]:
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("FilterCondition.values 必须是非空字符串")
        return values


class MetricPeriodValue(BaseModel):
    """同一指标在本期与对比期的数值（数据对象设计 §5.4）。"""

    current_value: float | None
    comparison_value: float | None


class ComparisonRow(BaseModel):
    """Calculator 使用的统一归一化行（数据对象设计 §5.5）。

    - dimension_value：维度成员；总体比较时为 None；
    - metric_values：一个或多个指标的本期/对比期值。
    """

    dimension_value: str | None
    metric_values: dict[MetricKey, MetricPeriodValue]


# ==================== 归因目标（数据对象设计 §7） ====================


class AttributionTarget(BaseModel):
    """冻结一次归因分析的指标与比较期间（数据对象设计 §7）。"""

    metrics: list[MetricKey] = Field(min_length=1, max_length=3)
    current_period: Period
    comparison_period: Period


# ==================== Action（数据对象设计 §9） ====================


class Action(BaseModel):
    """Planner 提交给执行环境的受控决策（数据对象设计 §9.1）。

    第一版严格白名单 8 类动作，逐类完成条件校验（§9.2）：

    - compare_period：metrics>=1、期间必填、dimension=null；
    - breakdown_region：dimension=region、期间必填、metrics>=1；
    - breakdown_category：dimension=category、期间必填、metrics>=1；
    - breakdown_product：dimension=product、期间必填、metrics>=1；
    - breakdown_customer：dimension=customer|customer_level、期间必填、metrics>=1；
    - analyze_unit_price：期间必填、metrics 至少包含 sales_amount+sales_quantity；
    - calculate_contribution：source_observation_ids>=1、metrics 恰好 1 个；
    - finish_analysis：不发起查询，仅要求 reason。

    reason 为 1～200 字可展示动作目的，不保存隐藏推理。
    """

    model_config = ConfigDict(extra="forbid")

    action_id: str
    type: ActionType
    metrics: list[MetricKey] = Field(default_factory=list)
    current_period: Period | None = None
    comparison_period: Period | None = None
    dimension: DimensionKey | None = None
    filters: list[FilterCondition] = Field(default_factory=list)
    source_observation_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _check_type_conditions(self) -> "Action":
        has_metrics = len(self.metrics) >= 1
        has_periods = self.current_period is not None and self.comparison_period is not None
        t = self.type

        if t == ActionType.compare_period:
            if not has_metrics:
                raise ValueError("compare_period 必须至少指定 1 个指标")
            if not has_periods:
                raise ValueError("compare_period 必须指定 current_period 和 comparison_period")
            if self.dimension is not None:
                raise ValueError("compare_period 不允许设置 dimension")
        elif t == ActionType.breakdown_region:
            if self.dimension != DimensionKey.region:
                raise ValueError("breakdown_region 的 dimension 必须为 region")
            if not has_metrics:
                raise ValueError("breakdown_region 必须至少指定 1 个指标")
            if not has_periods:
                raise ValueError("breakdown_region 必须指定 current_period 和 comparison_period")
        elif t == ActionType.breakdown_category:
            if self.dimension != DimensionKey.category:
                raise ValueError("breakdown_category 的 dimension 必须为 category")
            if not has_metrics:
                raise ValueError("breakdown_category 必须至少指定 1 个指标")
            if not has_periods:
                raise ValueError("breakdown_category 必须指定 current_period 和 comparison_period")
        elif t == ActionType.breakdown_product:
            if self.dimension != DimensionKey.product:
                raise ValueError("breakdown_product 的 dimension 必须为 product")
            if not has_metrics:
                raise ValueError("breakdown_product 必须至少指定 1 个指标")
            if not has_periods:
                raise ValueError("breakdown_product 必须指定 current_period 和 comparison_period")
        elif t == ActionType.breakdown_customer:
            if self.dimension not in (DimensionKey.customer, DimensionKey.customer_level):
                raise ValueError("breakdown_customer 的 dimension 只能是 customer 或 customer_level")
            if not has_metrics:
                raise ValueError("breakdown_customer 必须至少指定 1 个指标")
            if not has_periods:
                raise ValueError("breakdown_customer 必须指定 current_period 和 comparison_period")
        elif t == ActionType.analyze_unit_price:
            if not has_periods:
                raise ValueError("analyze_unit_price 必须指定 current_period 和 comparison_period")
            if MetricKey.sales_amount not in self.metrics or MetricKey.sales_quantity not in self.metrics:
                raise ValueError("analyze_unit_price 必须至少包含 sales_amount 和 sales_quantity")
        elif t == ActionType.calculate_contribution:
            if len(self.source_observation_ids) < 1:
                raise ValueError("calculate_contribution 必须至少引用 1 个 source Observation")
            if len(self.metrics) != 1:
                raise ValueError("calculate_contribution 只能针对 1 个目标指标")
        elif t == ActionType.finish_analysis:
            # 不发起查询；reason 必填已在字段层校验
            pass
        return self


# ==================== Observation（数据对象设计 §10） ====================


class Observation(BaseModel):
    """Provider 返回的环境事实（数据对象设计 §10.1）。

    同时保留原始 QueryExecutionResult 与归一化 ComparisonRow：

    - success：query_result 必须 success，normalized_rows 非空，error=null；
    - empty：query_result 必须 empty，normalized_rows=[]，error=null；
    - failed：归一化或 Provider 失败，error 必填（query_result 原样保留）。
    """

    observation_id: str
    action_id: str
    sub_query: str
    query_result: QueryExecutionResult
    dimension: DimensionKey | None = None
    normalized_rows: list[ComparisonRow] = Field(default_factory=list)
    status: ObservationStatus
    error: str | None = None

    @model_validator(mode="after")
    def _check_status_consistency(self) -> "Observation":
        if self.status == ObservationStatus.success:
            if self.query_result.status != ObservationStatus.success:
                raise ValueError("success Observation 必须来自 success QueryExecutionResult")
            if not self.normalized_rows:
                raise ValueError("success Observation 必须具有归一化行")
            if self.error is not None:
                raise ValueError("success Observation 要求 error 为 null")
        elif self.status == ObservationStatus.empty:
            if self.query_result.status != ObservationStatus.empty:
                raise ValueError("empty Observation 必须来自 empty QueryExecutionResult")
            if self.normalized_rows:
                raise ValueError("empty Observation 必须没有归一化行")
            if self.error is not None:
                raise ValueError("empty Observation 要求 error 为 null")
        elif self.status == ObservationStatus.failed:
            if self.error is None:
                raise ValueError("failed Observation 必须具有 error")
        return self


# ==================== Calculation（数据对象设计 §11） ====================


class _CalculationBase(BaseModel):
    """三类 Calculation 的公共字段（数据对象设计 §11）。"""

    calculation_id: str
    source_observation_ids: list[str] = Field(min_length=1)
    metric: MetricKey


class PeriodChangeCalculation(_CalculationBase):
    """期间变化计算（数据对象设计 §11.1）。"""

    type: CalculationType = CalculationType.period_change
    formula: str = "变化额 = 本期值 - 对比期值；变化率 = 变化额 / 对比期值"
    current_value: float
    comparison_value: float
    delta: float
    change_rate: float | None = None


class ContributionItem(BaseModel):
    """单个维度成员贡献（数据对象设计 §11.2）。"""

    member: str
    current_value: float
    comparison_value: float
    delta: float
    contribution_rate: float | None = None
    direction: FactorDirection


class ContributionCalculation(_CalculationBase):
    """维度贡献计算（数据对象设计 §11.2）。

    total_delta 为 0 时所有 contribution_rate=null；贡献率允许 >1 或 <0。
    """

    type: CalculationType = CalculationType.contribution
    formula: str = "成员变化额 = 成员本期值 - 成员对比期值；贡献率 = 成员变化额 / 总体变化额"
    dimension: DimensionKey
    total_delta: float
    items: list[ContributionItem]


class UnitPriceCalculation(_CalculationBase):
    """平均单件销售额计算（数据对象设计 §11.3）。"""

    type: CalculationType = CalculationType.unit_price
    formula: str = "平均单件销售额 = 销售额 / 销售数量"
    current_sales_amount: float
    current_sales_quantity: float
    current_unit_price: float | None = None
    comparison_sales_amount: float
    comparison_sales_quantity: float
    comparison_unit_price: float | None = None
    delta: float | None = None
    change_rate: float | None = None


Calculation: TypeAlias = PeriodChangeCalculation | ContributionCalculation | UnitPriceCalculation


# ==================== Evidence（数据对象设计 §12） ====================


class Evidence(BaseModel):
    """可追溯的最小证据单元（数据对象设计 §12）。

    约束：observation_ids 至少 1 个；不复制 SQL，通过 observation_id
    追溯到 Observation.query_result.sql；不设计数值置信度字段。
    """

    evidence_id: str
    action_id: str
    observation_ids: list[str] = Field(min_length=1)
    calculation_ids: list[str] = Field(default_factory=list)
    title: str
    statement: str
    metric: MetricKey
    dimension: DimensionKey | None = None
    member: str | None = None
    direction: FactorDirection | None = None
