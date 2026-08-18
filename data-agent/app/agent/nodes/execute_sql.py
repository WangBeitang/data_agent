from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.core.log import logger


async def execute_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    runtime.stream_writer({"stage": "执行SQL", "stage_code": "sql_execution"})

    try:
        sql = state["sql"]
        dw_mysql_repo = runtime.context["dw_mysql_repo"]

        # 结构化执行：取得列名与行数据（0 行时仍能取得 columns）
        columns, rows = await dw_mysql_repo.execute_query(sql)

        # 迁移期兼容：保留 legacy {"result": [...]}，保证现有外部 SSE / 前端仍能工作
        runtime.stream_writer({"result": rows})
        # 内部结构化字段：供 QueryService 组装 QueryExecutionResult，不强行改变外部 SSE 契约
        runtime.stream_writer({
            "query_result": {
                "sql": sql,
                "columns": columns,
                "rows": rows,
            }
        })

        logger.info(f"执行SQL完成 rows={len(rows)} columns={columns}")

        # 最终 State 可获得最终 sql / result_columns / result_rows
        return {"result_columns": columns, "result_rows": rows}
    except Exception as e:
        logger.error(f"执行SQL失败 sql={state.get('sql')!r}", exc_info=True)
        raise
