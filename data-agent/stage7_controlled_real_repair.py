"""Stage 7 收口：受控真实默认 repair 验收脚本（不进生产调用路径）。

人为制造 result_contract drift（仅在脚本内 monkeypatch DataAgent graph，
使其返回确定性错误契约结果），随后调用生产 `QueryService.execute(query,
result_contract)`，**不注入 contract_repair fake**，必须实际走：

    真实 _default_contract_repair
    → 当前真实 LLM（app.agent.llm.llm）
    → DWMysqlRepository.validate_sql（真实 MySQL EXPLAIN）
    → 真实 MySQL execute
    → 二次硬契约校验

验收至少证明：
- repair 被触发恰好 1 次（validate_sql / execute_query 各仅 1 次调用）；
- 修复 SQL 通过数据库校验和执行；
- 返回列 / period 值满足 result_contract；
- QueryExecutionResult 最终为 success；
- Attribution query_action_count 不因此增加、不新增 action_start
  （通过真实 AttributionGraph + 可编程 Planner 的完整归因运行验证）。

用法：
    uv run python stage7_controlled_real_repair.py
"""

import asyncio
from datetime import date
from unittest.mock import patch

from app.attribution.graph import AttributionGraph
from app.attribution.planner import Planner
from app.attribution.report_generator import ReportGenerator
from app.attribution.state import initial_state
from app.clients.mysql_client_manager import dw_mysql_client_manager
from app.models.analysis import (
    Action,
    ActionType,
    AnalysisMode,
    AnalysisStatus,
    AttributionTarget,
    DimensionKey,
    MetricKey,
    ObservationStatus,
    Period,
    RequestMode,
    RouteResult,
    RouteSource,
)
from app.repositories.mysql.dw_mysql_repository import DWMysqlRepository
from app.services.query_service import QueryService
from app.services.result_contract import validate_contract_result

# ==================== 冻结期间与上下文（供 repair LLM 使用） ====================

JAN = Period(label="2025年1月", start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
FEB = Period(label="2025年2月", start_date=date(2025, 2, 1), end_date=date(2025, 2, 28))

TABLE_INFOS = [
    {
        "name": "fact_order",
        "description": "销售订单事实表",
        "columns": [
            {"name": "order_id", "description": "订单ID", "type": "varchar(30)"},
            {"name": "customer_id", "description": "客户ID", "type": "varchar(20)"},
            {"name": "product_id", "description": "产品ID", "type": "varchar(20)"},
            {"name": "date_id", "description": "日期ID", "type": "int"},
            {"name": "region_id", "description": "区域ID", "type": "varchar(20)"},
            {"name": "order_quantity", "description": "销售数量", "type": "int"},
            {"name": "order_amount", "description": "销售额", "type": "float"},
        ],
    }
]
METRIC_INFOS = [
    {
        "id": "sales_amount",
        "name": "销售额",
        "description": "销售额，口径为销售订单金额合计 SUM(order_amount)",
    }
]
DATE_INFO = {"today": "2025-03-31"}
DB_INFO = {"version": "8.4.0", "dialect": "mysql"}

DRIFT_CONTEXT = {
    "table_infos": TABLE_INFOS,
    "metric_infos": METRIC_INFOS,
    "date_info": DATE_INFO,
    "db_info": DB_INFO,
}


def _fake_graph(wrong_columns, wrong_rows, sql):
    """DataAgent graph 的受控替身：返回确定性的错误契约结果 + 真实上下文。"""

    async def fake_ainvoke(state, **kwargs):
        return {
            **state,
            "sql": sql,
            "result_columns": wrong_columns,
            "result_rows": wrong_rows,
            "table_infos": TABLE_INFOS,
            "metric_infos": METRIC_INFOS,
            "date_info": DATE_INFO,
            "db_info": DB_INFO,
        }

    return fake_ainvoke


class _Counter:
    def __init__(self):
        self.validate = 0
        self.execute = 0


def _init_counters(service: QueryService):
    """保存原始 repository 方法（供计数包装反复使用，避免叠包）。"""
    service._orig_validate = service.dw_mysql_repo.validate_sql
    service._orig_execute = service.dw_mysql_repo.execute_query


def _wrap_counters(service: QueryService, counter: _Counter):
    """包装真实 repository 方法做调用计数（仍调用原实现，不改行为）。"""
    orig_validate = service._orig_validate
    orig_execute = service._orig_execute

    async def counted_validate(sql):
        counter.validate += 1
        return await orig_validate(sql)

    async def counted_execute(sql):
        counter.execute += 1
        return await orig_execute(sql)

    service.dw_mysql_repo.validate_sql = counted_validate
    service.dw_mysql_repo.execute_query = counted_execute


async def _direct_execute_acceptance(service: QueryService, contract: dict):
    """直接调用生产 QueryService.execute(query, result_contract)，不注入 repair fake。"""
    print("=" * 78)
    print("受控真实默认 repair 验收：直接 QueryService.execute（缺 period_key 列漂移）")
    print("=" * 78)

    counter = _Counter()
    _wrap_counters(service, counter)

    fake_ainvoke = _fake_graph(
        wrong_columns=["sales_amount"],
        wrong_rows=[{"sales_amount": 109030.5}, {"sales_amount": 80009.0}],
        sql=("SELECT SUM(order_amount) AS sales_amount FROM fact_order "
             "WHERE date_id BETWEEN 20250101 AND 20250228"),
    )

    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        # 本函数已在事件循环内（main await 调用），直接 await 生产 execute
        result = await service.execute(
            "分别统计2025年1月和2025年2月的销售额。", contract
        )

    print(f"  QueryExecutionResult.status   : {result.status.value}")
    print(f"  QueryExecutionResult.sql      : {result.sql}")
    print(f"  columns                       : {result.table.columns}")
    print(f"  rows                          : {result.table.rows}")
    print(f"  validate_sql 调用次数          : {counter.validate}")
    print(f"  execute_query 调用次数         : {counter.execute}")
    print(f"  二次硬契约校验 reason          : "
          f"{validate_contract_result(result.table.columns, result.table.rows, contract)!r}")

    assert counter.validate == 1, "validate_sql 必须恰好调用 1 次（只 repair 一次）"
    assert counter.execute == 1, "execute_query 必须恰好调用 1 次（只 repair 一次）"
    assert result.status == ObservationStatus.success, "最终必须 success"
    assert validate_contract_result(result.table.columns, result.table.rows, contract) is None, \
        "修复结果必须满足 result_contract"
    period_values = {r["period_key"] for r in result.table.rows}
    assert period_values <= {"comparison", "current"}, f"period 值必须满足契约：{period_values}"
    print("  >>> 通过：真实默认 repair 恰好 1 次，修复 SQL 通过真实 DB 校验/执行，契约满足。")

    # 第二种漂移：period_key 取 2025-01/2025-02（期间值漂移）
    print("=" * 78)
    print("受控真实默认 repair 验收：直接 QueryService.execute（period 值漂移）")
    print("=" * 78)
    counter2 = _Counter()
    _wrap_counters(service, counter2)
    fake_ainvoke2 = _fake_graph(
        wrong_columns=["period_key", "sales_amount"],
        wrong_rows=[
            {"period_key": "2025-01", "sales_amount": 109030.5},
            {"period_key": "2025-02", "sales_amount": 80009.0},
        ],
        sql=("SELECT CASE WHEN date_id BETWEEN 20250101 AND 20250131 THEN '2025-01' "
             "ELSE '2025-02' END AS period_key, SUM(order_amount) AS sales_amount "
             "FROM fact_order WHERE date_id BETWEEN 20250101 AND 20250228 GROUP BY period_key"),
    )
    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke2
        result2 = await service.execute(
            "分别统计2025年1月和2025年2月的销售额。", contract
        )

    print(f"  QueryExecutionResult.status   : {result2.status.value}")
    print(f"  QueryExecutionResult.sql      : {result2.sql}")
    print(f"  columns                       : {result2.table.columns}")
    print(f"  rows                          : {result2.table.rows}")
    print(f"  validate_sql 调用次数          : {counter2.validate}")
    print(f"  execute_query 调用次数         : {counter2.execute}")
    print(f"  二次硬契约校验 reason          : "
          f"{validate_contract_result(result2.table.columns, result2.table.rows, contract)!r}")

    assert counter2.validate == 1 and counter2.execute == 1
    assert result2.status == ObservationStatus.success
    assert validate_contract_result(result2.table.columns, result2.table.rows, contract) is None
    assert {r["period_key"] for r in result2.table.rows} <= {"comparison", "current"}
    print("  >>> 通过：period 值漂移经真实默认 repair 修复，契约满足。")
    return result


class _ScriptedPlanner(Planner):
    """可编程 Planner（复用 Stage 5/7 测试替身模式，不依赖真实 LLM）。"""

    def __init__(self, actions):
        super().__init__(llm=None)
        self._actions = list(actions)

    def plan(self, state_view, feedback=None, validator=None):
        for _ in range(2):
            if not self._actions:
                return None
            candidate = self._actions.pop(0)
            if candidate is None:
                return None
            if validator is not None:
                validation = validator(candidate)
                if not validation.ok:
                    continue
            return candidate
        return None

    def fallback_action(self, state_view, tried_keys):
        return None


class _FakeReportLLM:
    def invoke(self, prompt):
        raise RuntimeError("report llm unavailable")


async def _attribution_count_acceptance(service: QueryService, contract: dict) -> dict:
    """真实 AttributionGraph 运行：证明内部 repair 不增加 query_action_count / action_start。

    3 个查询 Action 各触发一次真实默认 repair，随后 finish。
    期望：query_action_count == 3，action_start 恰好 4 次（3 查询 + 1 finish）。
    """
    print("=" * 78)
    print("受控真实默认 repair 验收：Attribution query_action_count / action_start 不增加")
    print("=" * 78)

    target = AttributionTarget(
        metrics=[MetricKey.sales_amount], current_period=FEB, comparison_period=JAN
    )

    async def fake_ainvoke(state, **kwargs):
        return {
            **state,
            "sql": "SELECT SUM(order_amount) AS sales_amount FROM fact_order",
            "result_columns": ["sales_amount"],
            "result_rows": [{"sales_amount": 109030.5}, {"sales_amount": 80009.0}],
            "table_infos": TABLE_INFOS,
            "metric_infos": METRIC_INFOS,
            "date_info": DATE_INFO,
            "db_info": DB_INFO,
        }

    planner = _ScriptedPlanner([
        Action(action_id="a1", type=ActionType.compare_period, metrics=[MetricKey.sales_amount],
               current_period=FEB, comparison_period=JAN, reason="t"),
        Action(action_id="a2", type=ActionType.breakdown_region, dimension=DimensionKey.region,
               metrics=[MetricKey.sales_amount], current_period=FEB, comparison_period=JAN, reason="t"),
        Action(action_id="a3", type=ActionType.breakdown_category, dimension=DimensionKey.category,
               metrics=[MetricKey.sales_amount], current_period=FEB, comparison_period=JAN, reason="t"),
        Action(action_id="a9", type=ActionType.finish_analysis, reason="t"),
    ])
    state = initial_state(
        "an_real_repair", "为什么2月销售额下降？", RequestMode.auto,
        RouteResult(requested_mode=RequestMode.auto, resolved_mode=AnalysisMode.attribution,
                    source=RouteSource.rule), target,
    )

    graph = AttributionGraph(
        query_service=service, planner=planner, report_generator=ReportGenerator(llm=_FakeReportLLM())
    )
    events = []
    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke

        # 本函数已在事件循环内（main await 调用），直接 await 收集事件
        async for e in graph.run(state):
            events.append(e)

    n_action_start = [e["type"] for e in events].count("action_start")
    print(f"  state.query_action_count : {state['query_action_count']}")
    print(f"  state.status             : {state['status'].value}")
    print(f"  action_start 事件次数      : {n_action_start}")
    print(f"  成功 Observation 数        : "
          f"{sum(1 for o in state['observations'] if o.status == ObservationStatus.success)}")

    # 3 个查询 Action：query_action_count 必须等于 3（内部 repair 未 +1），
    # action_start 必须恰好 4（3 查询 + 1 finish），即 repair 未新增 action_start。
    assert state["query_action_count"] == 3, "query_action_count 不得因内部 repair 增加"
    assert n_action_start == 4, "内部 repair 不得产生额外 action_start"
    print("  >>> 通过：内部 repair 不增加 query_action_count、不新增 action_start。")
    return {"query_action_count": state["query_action_count"], "action_start": n_action_start,
            "status": state["status"].value}


async def main():
    dw_mysql_client_manager.init()
    try:
        async with dw_mysql_client_manager.session_factory() as dw_session:
            dw_repo = DWMysqlRepository(dw_session)
            service = QueryService(
                dw_mysql_repo=dw_repo,
                meta_mysql_repo=object(),       # 受控脚本：DataAgent graph 已替身，不使用
                value_es_repo=object(),
                column_qdrant_repo=object(),
                metric_qdrant_repo=object(),
                embedding_client=object(),
            )
            contract = {
                "period_alias": "period_key",
                "dimension_alias": None,
                "metric_aliases": {"sales_amount": "sales_amount"},
                "period_values": ["comparison", "current"],
            }
            _init_counters(service)
            await _direct_execute_acceptance(service, contract)
            counts = await _attribution_count_acceptance(service, contract)
            print("\n########## 受控真实默认 repair 验收结论 ##########")
            print(f"repair 恰好 1 次 / 修复 SQL 通过真实 DB 校验执行 / 契约满足 / 最终 success：PASS")
            print(f"query_action_count={counts['query_action_count']}（未增加）、"
                  f"action_start={counts['action_start']}（未新增）、status={counts['status']}")
    finally:
        await dw_mysql_client_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
