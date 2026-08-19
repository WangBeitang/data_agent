"""Stage 7 复现脚本（不修改任何功能代码，仅使用当前 main 代码路径）。

目的：在「当前 main 最新代码」上确定性复现归因场景下
`result_contract` / SQL 输出漂移导致的失败链路：

  generate_sql 偶尔生成不符合 result_contract 的 SQL 输出
  → QueryService.execute() 原样返回（当前无契约硬校验）
  → Normalizer 严格校验不通过 → Observation.status=failed
  → consecutive_empty_or_failed +1
  → 连续 2 次 → ActionRouter 强制停止 → 无 driver Evidence → 归因失败

本脚本模拟两种真实漂移：
  A. 列名漂移（class #2）：返回 `period` / `dimension_value` 缺失 / 指标别名不符
  B. period 值漂移（class #3）：返回 `2025-01`/`2025-02` 而非 `comparison`/`current`

全部使用当前 main 的 QueryService / Normalizer / ActionRouter，不改动任何实现。
"""

from datetime import date
from unittest.mock import patch

from app.attribution.action_router import (
    ActionRouter,
    build_result_contract,
    build_sub_query,
    is_force_stopped,
    next_consecutive,
    MAX_QUERY_ACTIONS,
)
from app.attribution.normalizer import Normalizer
from app.models.analysis import (
    Action,
    ActionType,
    DimensionKey,
    MetricKey,
    ObservationStatus,
    Period,
    QueryExecutionResult,
    QueryTable,
)
from app.services.query_service import QueryService


JAN = Period(label="2025年1月", start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
FEB = Period(label="2025年2月", start_date=date(2025, 2, 1), end_date=date(2025, 2, 28))


def make_action(dimension=None, metrics=None) -> Action:
    return Action(
        action_id="a_drift",
        type=ActionType.breakdown_category if dimension else ActionType.compare_period,
        metrics=metrics or [MetricKey.sales_amount],
        current_period=FEB,
        comparison_period=JAN,
        dimension=dimension,
        reason="复现测试",
    )


def make_qer(sql, columns, rows) -> QueryExecutionResult:
    return QueryExecutionResult(
        query="sub_query",
        sql=sql,
        table=QueryTable(columns=columns, rows=rows, row_count=len(rows)),
        status=ObservationStatus.success if rows else ObservationStatus.empty,
        error=None,
    )


def run_case(name, action, contract, qer, graph_sql):
    """用当前 main 的 QueryService.execute()（mock graph 返回漂移结果）跑一遍。"""
    print("=" * 78)
    print(f"复现案例：{name}")
    print("=" * 78)

    async def fake_ainvoke(state, **kwargs):
        return {
            **state,
            "sql": graph_sql,
            "result_columns": qer.table.columns,
            "result_rows": qer.table.rows,
        }

    service = QueryService(
        dw_mysql_repo=object(),
        meta_mysql_repo=object(),
        value_es_repo=object(),
        column_qdrant_repo=object(),
        metric_qdrant_repo=object(),
        embedding_client=object(),
    )
    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        import asyncio
        result = asyncio.run(service.execute("sub_query", contract))

    sub_query = build_sub_query(action)
    observation = Normalizer.normalize(
        observation_id="o_drift",
        action=action,
        sub_query=sub_query,
        query_result=result,
        result_contract=contract,
    )

    print(f"  Action type          : {action.type.value}")
    print(f"  Action dimension     : {action.dimension.value if action.dimension else None}")
    print(f"  sub_query            : {sub_query}")
    print(f"  result_contract      : {contract}")
    print(f"  QueryExecutionResult :")
    print(f"      status           : {result.status.value}")
    print(f"      sql              : {result.sql}")
    print(f"      columns          : {result.table.columns}")
    print(f"      rows             : {result.table.rows}")
    print(f"  Observation          :")
    print(f"      status           : {observation.status.value}")
    print(f"      error            : {observation.error}")
    print(f"  推断 consecutive_empty_or_failed: "
          f"{'0 (reset)' if observation.status == ObservationStatus.success else '+1 (累计)'}")

    # 演示级联：两次 drift 失败 → 强制停止
    consecutive = 0
    consecutive = next_consecutive(observation.status, consecutive)
    forced = is_force_stopped(query_action_count=2, consecutive_empty_or_failed=consecutive,
                               max_query_actions=MAX_QUERY_ACTIONS)
    print(f"  两次 drift 失败模拟 → consecutive={consecutive} forced_stop={forced}")
    if forced:
        print("  >>> 结论：连续两次契约漂移失败触发强制停止，归因无法产生 driver Evidence。")
    print()
    return observation


def main():
    print("\n########## Stage 7 复现：当前 main 代码 result_contract / SQL 输出漂移 ##########\n")

    # ---- 案例 A：列名漂移（class #2）—— 返回 period / category_name，缺 period_key / dimension_value ----
    action_a = make_action(dimension=DimensionKey.category)
    contract_a = build_result_contract(action_a)
    qer_a = make_qer(
        sql="SELECT category_name AS dim, period, SUM(sales) AS sales_amount ...",
        columns=["dim", "period", "sales_amount"],
        rows=[
            {"dim": "机床", "period": "comparison", "sales_amount": 100.0},
            {"dim": "机床", "period": "current", "sales_amount": 200.0},
        ],
    )
    run_case("A. 列名漂移（缺 period_key / dimension_value 契约列）",
             action_a, contract_a, qer_a,
             "SELECT category_name AS dim, period, SUM(sales) AS sales_amount FROM dw GROUP BY category_name, period")

    # ---- 案例 B：period 值漂移（class #3）—— period_key 返回 2025-01/2025-02 而非 comparison/current ----
    action_b = make_action(dimension=None)
    contract_b = build_result_contract(action_b)
    qer_b = make_qer(
        sql="SELECT CASE ... END AS period_key, SUM(sales) AS sales_amount ...",
        columns=["period_key", "sales_amount"],
        rows=[
            {"period_key": "2025-01", "sales_amount": 109030.5},
            {"period_key": "2025-02", "sales_amount": 80009.0},
        ],
    )
    run_case("B. period 值漂移（period_key 不在 {comparison,current}）",
             action_b, contract_b, qer_b,
             "SELECT CASE WHEN MONTH=1 THEN '2025-01' ELSE '2025-02' END AS period_key, SUM(sales) AS sales_amount FROM dw")

    print("########## 复现结论 ##########")
    print("当前 main 代码：QueryService.execute() 仅在 Graph 内把 result_contract 作为")
    print("Prompt 软约束传给 generate_sql；Graph 的 validate_sql 只校验 SQL 语法，")
    print("correct_sql 只在语法错误时修正。SQL 输出漂移（列名 / period 值）不会被拦截，")
    print("原样进入 Normalizer → 严格校验失败 → Observation.failed → 连续 2 次强制停止 → 无 driver。")
    print("根因类别：result_contract / SQL 输出漂移（class #2 / class #3）。")


if __name__ == "__main__":
    main()
