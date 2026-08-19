import json
from typing import Awaitable, Callable, Optional, Union

from langchain_core.embeddings import Embeddings

from app.agent.context import DataAgentContext
from app.agent.graph import graph
from app.agent.state import DataAgentState
from app.attribution.normalizer import validate_contract_result
from app.core.log import logger
from app.models.analysis import ObservationStatus, QueryExecutionResult, QueryTable
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw_mysql_repository import DWMysqlRepository
from app.repositories.mysql.meta_mysql_repository import MetaMysqlRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository

# 稳定、安全的用户可见错误信息（详细异常只写服务端日志）
_SAFE_QUERY_FAILED_MESSAGE = "问数执行失败：无法完成 SQL 生成、校验或执行，请调整问题后重试。"
_SAFE_CONTRACT_FAILED_MESSAGE = (
    "问数执行失败：SQL 输出不符合结果结构契约，已尝试一次自动修复仍不满足要求，请调整问题后重试。"
)
_SAFE_SSE_ERROR_MESSAGE = "查询过程中出现异常，请调整问题后重试。"

# 受控内部 SQL 修复函数签名：
#   async def repair(*, query, sql, result_contract, reason, context) -> str | None
# 返回修正后的 SQL 文本；返回 None 或抛异常视为修复失败。
ContractRepairFn = Callable[..., Awaitable[Optional[str]]]

# legacy SSE 允许对外暴露的事件键（stage / stage_code / result）
# 内部结构化事件（如 execute_sql 的 query_result）不得进入外部 SSE
_LEGACY_SSE_ALLOWED_KEYS = {"stage", "stage_code", "result"}


class QueryService:
    def __init__(self,
                 dw_mysql_repo: DWMysqlRepository,
                 meta_mysql_repo: MetaMysqlRepository,
                 value_es_repo: ValueESRepository,
                 column_qdrant_repo: ColumnQdrantRepository,
                 metric_qdrant_repo: MetricQdrantRepository,
                 embedding_client: Embeddings):
        self.dw_mysql_repo = dw_mysql_repo
        self.meta_mysql_repo = meta_mysql_repo
        self.value_es_repo = value_es_repo
        self.column_qdrant_repo = column_qdrant_repo
        self.metric_qdrant_repo = metric_qdrant_repo
        self.embedding_client = embedding_client

    def _build_context(self) -> DataAgentContext:
        return DataAgentContext(
            dw_mysql_repo=self.dw_mysql_repo,
            meta_mysql_repo=self.meta_mysql_repo,
            value_es_repo=self.value_es_repo,
            column_qdrant_repo=self.column_qdrant_repo,
            metric_qdrant_repo=self.metric_qdrant_repo,
            embedding_client=self.embedding_client
        )

    async def execute(
        self,
        query: str,
        result_contract: dict | None = None,
        *,
        contract_repair: ContractRepairFn | None = None,
    ) -> QueryExecutionResult:
        """结构化同步执行一次问数，返回 QueryExecutionResult。

        - 普通问数：result_contract=None，行为与 Stage 1 一致；
        - 归因子查询：传入 result_contract 约束 SQL 输出列别名。

        硬执行契约（Stage 7）：归因子查询在 DataAgent Graph 产出 SQL 输出
        后，若输出不满足 result_contract，最多进行一次受控内部 SQL 修复
        （不增加 Attribution query_action_count、不产生新 Action /
        action_start、不改动 6 次查询上限），修复后重新校验；若仍违反契约，
        正常返回 failed，由现有 Attribution 停止机制处理，不无限重试。
        """
        state = DataAgentState(query=query, result_contract=result_contract)
        context = self._build_context()
        try:
            result_state = await graph.ainvoke(state, context=context)
        except Exception as e:
            logger.error(f"问数执行失败 query={query!r}", exc_info=True)
            return QueryExecutionResult(
                query=query,
                sql=None,
                table=QueryTable(columns=[], rows=[], row_count=0),
                status=ObservationStatus.failed,
                error=_SAFE_QUERY_FAILED_MESSAGE,
            )

        sql = result_state.get("sql")
        columns = result_state.get("result_columns")
        rows = result_state.get("result_rows")

        # execute_sql 未成功写入结构化结果（生成/校验/执行失败）→ failed
        if columns is None or rows is None:
            logger.error(f"问数执行未获得结构化结果 query={query!r} error={result_state.get('error')!r}")
            return QueryExecutionResult(
                query=query,
                sql=sql,
                table=QueryTable(columns=[], rows=[], row_count=0),
                status=ObservationStatus.failed,
                error=_SAFE_QUERY_FAILED_MESSAGE,
            )

        # 硬契约校验（仅归因子查询）：违反则进行一次受控内部 SQL 修复
        if result_contract is not None:
            reason = validate_contract_result(columns, rows, result_contract)
            if reason is not None:
                logger.warning(
                    f"SQL 输出违反 result_contract query={query!r} reason={reason!r}，尝试一次内部修复"
                )
                repaired = await self._do_contract_repair(
                    query=query,
                    sql=sql,
                    result_contract=result_contract,
                    reason=reason,
                    result_state=result_state,
                    repair_fn=contract_repair,
                )
                if repaired is None:
                    # 修复失败（无修复函数 / 修复抛错 / 修复 SQL 校验或执行失败）
                    return QueryExecutionResult(
                        query=query,
                        sql=sql,
                        table=QueryTable(columns=[], rows=[], row_count=0),
                        status=ObservationStatus.failed,
                        error=_SAFE_CONTRACT_FAILED_MESSAGE,
                    )
                repaired_sql, columns, rows = repaired
                sql = repaired_sql
                # 重新校验：仍违反 → failed，不无限重试
                reason2 = validate_contract_result(columns, rows, result_contract)
                if reason2 is not None:
                    logger.error(
                        f"内部契约修复后仍违反 result_contract query={query!r} reason={reason2!r}"
                    )
                    return QueryExecutionResult(
                        query=query,
                        sql=sql,
                        table=QueryTable(columns=[], rows=[], row_count=0),
                        status=ObservationStatus.failed,
                        error=_SAFE_CONTRACT_FAILED_MESSAGE,
                    )

        table = QueryTable(columns=columns, rows=rows, row_count=len(rows))
        status = ObservationStatus.success if table.row_count > 0 else ObservationStatus.empty
        return QueryExecutionResult(
            query=query,
            sql=sql,
            table=table,
            status=status,
            error=None,
        )

    # ==================== 受控内部 SQL 修复（Stage 7） ====================

    async def _do_contract_repair(
        self,
        *,
        query: str,
        sql: str | None,
        result_contract: dict,
        reason: str,
        result_state: dict,
        repair_fn: ContractRepairFn | None,
    ) -> "tuple[str, list[str], list[dict]] | None":
        """一次受控内部 SQL 修复。

        返回 (corrected_sql, columns, rows)；失败返回 None。
        本函数发生在一次 QueryService.execute() 内部，由 Attribution 视角
        只是一次 query Action，因此不增加 query_action_count，不产生新
        Action / action_start，不改变 6 次查询上限。
        """
        fn = repair_fn if repair_fn is not None else self._default_contract_repair
        try:
            corrected_sql = await fn(
                query=query,
                sql=sql,
                result_contract=result_contract,
                reason=reason,
                context={
                    "table_infos": result_state.get("table_infos"),
                    "metric_infos": result_state.get("metric_infos"),
                    "date_info": result_state.get("date_info"),
                    "db_info": result_state.get("db_info"),
                },
            )
        except Exception as e:
            logger.error(f"内部契约修复生成 SQL 失败 query={query!r}", exc_info=True)
            return None
        if not isinstance(corrected_sql, str) or not corrected_sql.strip():
            logger.error(f"内部契约修复返回非法 SQL query={query!r}")
            return None
        # 校验语法 + 执行（复用现有 repository 边界，与 Graph execute_sql 一致）
        try:
            await self.dw_mysql_repo.validate_sql(corrected_sql)
            columns, rows = await self.dw_mysql_repo.execute_query(corrected_sql)
        except Exception as e:
            logger.error(f"内部契约修复 SQL 校验/执行失败 query={query!r}", exc_info=True)
            return None
        return (corrected_sql, columns, rows)

    async def _default_contract_repair(
        self,
        *,
        query: str,
        sql: str | None,
        result_contract: dict,
        reason: str,
        context: dict,
    ) -> str:
        """默认契约修复：基于上下文 + 契约失败原因，LLM 生成修正后的 SQL。

        仅在生产路径（contract_repair 未注入）使用；测试通过注入 fake 覆盖。
        """
        from app.agent.llm import llm
        from app.prompt.prompt_loader import load_prompt
        import yaml
        from langchain_core.output_parsers import StrOutputParser
        from langchain_core.prompts import PromptTemplate

        prompt_template = PromptTemplate(
            template=load_prompt("repair_sql_contract"),
            input_variables=[
                "query",
                "sql",
                "result_contract",
                "reason",
                "table_infos",
                "metric_infos",
                "date_info",
                "db_info",
            ],
        )
        output_parser = StrOutputParser()
        chain = prompt_template | llm | output_parser
        corrected_sql = await chain.ainvoke(
            {
                "query": query,
                "sql": sql or "",
                "result_contract": yaml.dump(result_contract, allow_unicode=True, sort_keys=False),
                "reason": reason,
                "table_infos": yaml.dump(context.get("table_infos") or [], allow_unicode=True, sort_keys=False),
                "metric_infos": yaml.dump(context.get("metric_infos") or [], allow_unicode=True, sort_keys=False),
                "date_info": yaml.dump(context.get("date_info") or {}, allow_unicode=True, sort_keys=False),
                "db_info": yaml.dump(context.get("db_info") or {}, allow_unicode=True, sort_keys=False),
            }
        )
        return corrected_sql

    async def stream(self, query: str):
        """内部流式执行：透传 Graph custom 事件（stage / result / query_result）。"""
        state = DataAgentState(query=query)
        context = self._build_context()
        async for chunk in graph.astream(
                input=state,
                context=context,
                stream_mode="custom"
        ):
            yield chunk

    async def search(self, query: str):
        """兼容层：以当前 SSE 字符串格式输出流式事件，供现有 router 与前端使用。

        Stage 2 保留本方法；仅做 SSE 字符串格式适配，不复制 Graph 业务逻辑。
        只透传 legacy 契约允许的事件（stage/stage_code/result），
        内部结构化事件（query_result）不得泄露到外部 SSE。
        """
        try:
            async for chunk in self.stream(query):
                # 白名单过滤：包含非 legacy 键（如 query_result）的事件跳过
                if not set(chunk.keys()).issubset(_LEGACY_SSE_ALLOWED_KEYS):
                    continue
                yield f"data: {json.dumps(chunk, ensure_ascii=False, default=str)} \n\n"
        except Exception as e:
            logger.error(f"问数流式执行失败 query={query!r}", exc_info=True)
            # 安全错误信息，不暴露 str(e)
            yield f"data: {json.dumps({'error': _SAFE_SSE_ERROR_MESSAGE}, ensure_ascii=False, default=str)} \n\n"
