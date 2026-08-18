"""Calculator（Stage 4 归因确定性核心）。

依据冻结文档（数据对象设计 §11 / SPEC §5.8）：

```text
period_change：delta = current - comparison；change_rate = delta / comparison
contribution：item_delta = item.current - item.comparison
              contribution_rate = item_delta / total_delta
              total_delta == 0 → 全部 contribution_rate = null
              同号 → driver；反号 → offset；item_delta==0 → neutral；
              total_delta==0 → neutral（贡献率允许 >1 或 <0，不 clamp）
unit_price：unit_price = sales_amount / sales_quantity；quantity==0 → null
```

本模块必须是纯确定性模块，禁止：LLM、Repository、HTTP、SQL。

内部使用 Decimal(str(value)) 完成金额与比率运算，模型输出时转换为
JSON number（float），避免二进制 float 误差污染固定验收值。

- 金额 / 数量 / 变化额 / 平均单件销售额：保留 2 位小数；
- change_rate / contribution_rate：保留 4 位小数（小数比例）。
"""

from decimal import Decimal, ROUND_HALF_UP

from app.models.analysis import (
    CalculationType,
    ComparisonRow,
    ContributionCalculation,
    ContributionItem,
    FactorDirection,
    MetricKey,
    Observation,
    PeriodChangeCalculation,
    UnitPriceCalculation,
)

# 固定验收场景校验常量（供测试直接引用）
SCENARIO_ONE_COMPARISON = 109030.5
SCENARIO_ONE_CURRENT = 80009.0
SCENARIO_ONE_DELTA = -29021.5
SCENARIO_ONE_CHANGE_RATE = -0.2662

SCENARIO_TWO_QUANTITY_COMPARISON = 151
SCENARIO_TWO_QUANTITY_CURRENT = 322
SCENARIO_TWO_QUANTITY_CHANGE_RATE = 1.1325
SCENARIO_TWO_AMOUNT_COMPARISON = 80009.0
SCENARIO_TWO_AMOUNT_CURRENT = 90120.0
SCENARIO_TWO_AMOUNT_CHANGE_RATE = 0.1264
SCENARIO_TWO_UNIT_PRICE_COMPARISON = 529.86
SCENARIO_TWO_UNIT_PRICE_CURRENT = 279.88

# 公式文案（数据对象设计 §11，可展示公式，不是可执行代码）
_FORMULA_PERIOD_CHANGE = "变化额 = 本期值 - 对比期值；变化率 = 变化额 / 对比期值"
_FORMULA_CONTRIBUTION = "成员变化额 = 成员本期值 - 成员对比期值；贡献率 = 成员变化额 / 总体变化额"
_FORMULA_UNIT_PRICE = "平均单件销售额 = 销售额 / 销售数量"


def _dec(value) -> Decimal:
    return Decimal(str(value))


def _float2(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _float4(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _overall_row(observation: Observation) -> ComparisonRow:
    """总体比较行（dimension_value=None）。"""
    for row in observation.normalized_rows:
        if row.dimension_value is None:
            return row
    raise ValueError("期间变化计算需要总体比较行（dimension_value=None）")


def _period_values(observation: Observation, metric: MetricKey) -> tuple[float, float]:
    row = _overall_row(observation)
    metric_value = row.metric_values.get(metric)
    if metric_value is None:
        raise ValueError(f"Observation 缺少指标 {metric.value}")
    if metric_value.current_value is None or metric_value.comparison_value is None:
        raise ValueError(f"指标 {metric.value} 存在空值，无法计算期间变化")
    return metric_value.current_value, metric_value.comparison_value


def _direction(item_delta: Decimal, total_delta: Decimal) -> FactorDirection:
    """方向规则（数据对象设计 §11.2）。"""
    if total_delta == 0 or item_delta == 0:
        return FactorDirection.neutral
    if (item_delta > 0) == (total_delta > 0):
        return FactorDirection.driver
    return FactorDirection.offset


def period_change(
    observation: Observation,
    metric: MetricKey,
    *,
    calculation_id: str | None = None,
) -> PeriodChangeCalculation:
    """期间变化计算：delta = current - comparison；change_rate = delta / comparison。

    comparison == 0 时 change_rate = None；不输出 NaN/Infinity。
    """
    current_value, comparison_value = _period_values(observation, metric)
    current_dec = _dec(current_value)
    comparison_dec = _dec(comparison_value)
    delta = current_dec - comparison_dec
    if comparison_dec == 0:
        change_rate = None
    else:
        change_rate = _float4(delta / comparison_dec)

    return PeriodChangeCalculation(
        calculation_id=calculation_id or f"c_{CalculationType.period_change.value}_{metric.value}",
        source_observation_ids=[observation.observation_id],
        metric=metric,
        formula=_FORMULA_PERIOD_CHANGE,
        current_value=_float2(current_dec),
        comparison_value=_float2(comparison_dec),
        delta=_float2(delta),
        change_rate=change_rate,
    )


def contribution(
    observation: Observation,
    metric: MetricKey,
    total_delta: float,
    *,
    calculation_id: str | None = None,
) -> ContributionCalculation:
    """维度贡献计算：item_delta / total_delta；方向来自确定性规则。

    - total_delta == 0 → 全部 contribution_rate=null，方向 neutral；
    - contribution_rate 允许 >1 或 <0，不 clamp 到 [0,1]；
    - dimension 取自 Observation.dimension（必须是维度拆解 Observation）。
    """
    if observation.dimension is None:
        raise ValueError("贡献计算需要维度拆解 Observation（dimension 非空）")

    total_delta_dec = _dec(total_delta)
    items: list[ContributionItem] = []
    for row in observation.normalized_rows:
        if row.dimension_value is None:
            continue
        metric_value = row.metric_values.get(metric)
        if metric_value is None:
            raise ValueError(f"Observation 缺少指标 {metric.value}")
        if metric_value.current_value is None or metric_value.comparison_value is None:
            raise ValueError(f"指标 {metric.value} 存在空值，无法计算贡献")
        current_dec = _dec(metric_value.current_value)
        comparison_dec = _dec(metric_value.comparison_value)
        item_delta = current_dec - comparison_dec
        if total_delta_dec == 0:
            contribution_rate = None
        else:
            contribution_rate = _float4(item_delta / total_delta_dec)
        items.append(
            ContributionItem(
                member=row.dimension_value,
                current_value=_float2(current_dec),
                comparison_value=_float2(comparison_dec),
                delta=_float2(item_delta),
                contribution_rate=contribution_rate,
                direction=_direction(item_delta, total_delta_dec),
            )
        )

    if not items:
        raise ValueError("贡献计算没有可用的维度成员行")

    return ContributionCalculation(
        calculation_id=calculation_id or f"c_{CalculationType.contribution.value}_{metric.value}",
        source_observation_ids=[observation.observation_id],
        metric=metric,
        formula=_FORMULA_CONTRIBUTION,
        dimension=observation.dimension,
        total_delta=_float2(total_delta_dec),
        items=items,
    )


def unit_price(
    observation: Observation,
    *,
    calculation_id: str | None = None,
) -> UnitPriceCalculation:
    """平均单件销售额计算：sales_amount / sales_quantity。

    - 任一期间 quantity == 0 → 该期间 unit_price=None；
    - 任一期间 unit_price 无法计算 → delta/change_rate 置 None，禁止补值；
    - 不允许 NaN/Infinity。
    """
    amount_row = _overall_row(observation)
    amount_metric = amount_row.metric_values.get(MetricKey.sales_amount)
    quantity_metric = amount_row.metric_values.get(MetricKey.sales_quantity)
    if amount_metric is None or quantity_metric is None:
        raise ValueError("平均单件销售额计算需要 sales_amount 与 sales_quantity")
    if amount_metric.current_value is None or amount_metric.comparison_value is None:
        raise ValueError("销售额存在空值，无法计算平均单件销售额")
    if quantity_metric.current_value is None or quantity_metric.comparison_value is None:
        raise ValueError("销售数量存在空值，无法计算平均单件销售额")

    current_amount = _dec(amount_metric.current_value)
    current_quantity = _dec(quantity_metric.current_value)
    comparison_amount = _dec(amount_metric.comparison_value)
    comparison_quantity = _dec(quantity_metric.comparison_value)

    if current_quantity == 0:
        current_unit_price = None
    else:
        current_unit_price = _float2(current_amount / current_quantity)
    if comparison_quantity == 0:
        comparison_unit_price = None
    else:
        comparison_unit_price = _float2(comparison_amount / comparison_quantity)

    if current_unit_price is None or comparison_unit_price is None:
        delta = None
        change_rate = None
    else:
        delta_dec = _dec(current_unit_price) - _dec(comparison_unit_price)
        delta = _float2(delta_dec)
        change_rate = _float4(delta_dec / _dec(comparison_unit_price)) if comparison_unit_price != 0 else None

    return UnitPriceCalculation(
        calculation_id=calculation_id or f"c_{CalculationType.unit_price.value}_sales",
        source_observation_ids=[observation.observation_id],
        metric=MetricKey.avg_unit_sales_amount,
        formula=_FORMULA_UNIT_PRICE,
        current_sales_amount=_float2(current_amount),
        current_sales_quantity=_float2(current_quantity),
        current_unit_price=current_unit_price,
        comparison_sales_amount=_float2(comparison_amount),
        comparison_sales_quantity=_float2(comparison_quantity),
        comparison_unit_price=comparison_unit_price,
        delta=delta,
        change_rate=change_rate,
    )
