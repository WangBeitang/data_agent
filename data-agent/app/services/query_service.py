import json

from langchain_core.embeddings import Embeddings

from app.agent.context import DataAgentContext
from app.agent.graph import graph
from app.agent.state import DataAgentState
from app.core.log import logger
from app.models.analysis import ObservationStatus, QueryExecutionResult, QueryTable
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw_mysql_repository import DWMysqlRepository
from app.repositories.mysql.meta_mysql_repository import MetaMysqlRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository

# 稳定、安全的用户可见错误信息（详细异常只写服务端日志）
_SAFE_QUERY_FAILED_MESSAGE = "问数执行失败：无法完成 SQL 生成、校验或执行，请调整问题后重试。"
_SAFE_SSE_ERROR_MESSAGE = "查询过程中出现异常，请调整问题后重试。"


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
    ) -> QueryExecutionResult:
        """结构化同步执行一次问数，返回 QueryExecutionResult。

        - 普通问数：result_contract=None，行为与 Stage 1 一致；
        - 归因子查询：传入 result_contract 约束 SQL 输出列别名。
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

        table = QueryTable(columns=columns, rows=rows, row_count=len(rows))
        status = ObservationStatus.success if table.row_count > 0 else ObservationStatus.empty
        return QueryExecutionResult(
            query=query,
            sql=sql,
            table=table,
            status=status,
            error=None,
        )

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
        """
        try:
            async for chunk in self.stream(query):
                yield f"data: {json.dumps(chunk, ensure_ascii=False, default=str)} \n\n"
        except Exception as e:
            logger.error(f"问数流式执行失败 query={query!r}", exc_info=True)
            # 安全错误信息，不暴露 str(e)
            yield f"data: {json.dumps({'error': _SAFE_SSE_ERROR_MESSAGE}, ensure_ascii=False, default=str)} \n\n"
