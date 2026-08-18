import asyncio

from langgraph.constants import START, END
from langgraph.graph import StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import DataAgentContext
from app.agent.nodes.correct_sql import correct_sql
from app.agent.nodes.execute_sql import execute_sql
from app.agent.nodes.generate_sql import generate_sql
from app.agent.nodes.add_extra_context import add_extra_context
from app.agent.nodes.filter_metric import filter_metric
from app.agent.nodes.filter_table import filter_table
from app.agent.nodes.merge_retrieved_info import merge_retrieved_info
from app.agent.nodes.recall_metric import recall_metric
from app.agent.nodes.recall_value import recall_value
from app.agent.nodes.recall_column import recall_column
from app.agent.nodes.extract_keywords import extract_keywords
from app.agent.nodes.validate_sql import validate_sql
from app.core.log import logger
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw_mysql_repository import DWMysqlRepository
from app.repositories.mysql.meta_mysql_repository import MetaMysqlRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository
from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import dw_mysql_client_manager, meta_mysql_client_manager
from app.clients.qdrant_client_manager import qdrant_client_manager

from app.agent.state import DataAgentState

graph_builder = StateGraph(state_schema=DataAgentState, context_schema=DataAgentContext)

# 添加节点
graph_builder.add_node("extract_keywords", extract_keywords)
graph_builder.add_node("recall_column",recall_column)
graph_builder.add_node("recall_value",recall_value)
graph_builder.add_node("recall_metric",recall_metric)

graph_builder.add_node("merge_retrieved_info",merge_retrieved_info)
graph_builder.add_node("filter_metric",filter_metric)
graph_builder.add_node("filter_table",filter_table)
graph_builder.add_node("add_extra_context",add_extra_context)
graph_builder.add_node("generate_sql",generate_sql)
graph_builder.add_node("validate_sql",validate_sql)
graph_builder.add_node("correct_sql",correct_sql)
graph_builder.add_node("execute_sql",execute_sql)

# 添加边
graph_builder.add_edge(START, "extract_keywords")
graph_builder.add_edge("extract_keywords","recall_column")
graph_builder.add_edge("extract_keywords","recall_metric")
graph_builder.add_edge("extract_keywords","recall_value")
graph_builder.add_edge("recall_column","merge_retrieved_info")
graph_builder.add_edge("recall_metric","merge_retrieved_info")
graph_builder.add_edge("recall_value","merge_retrieved_info")
graph_builder.add_edge("merge_retrieved_info","filter_metric")
graph_builder.add_edge("merge_retrieved_info","filter_table")
graph_builder.add_edge("filter_metric","add_extra_context")
graph_builder.add_edge("filter_table","add_extra_context")
graph_builder.add_edge("add_extra_context","generate_sql")
graph_builder.add_edge("generate_sql","validate_sql")

# 添加条件边
graph_builder.add_conditional_edges(
    "validate_sql",
    lambda state: "execute_sql" if state.get("error") is None else "correct_sql",
    {"execute_sql": "execute_sql", "correct_sql": "correct_sql"}
)
graph_builder.add_edge("correct_sql","execute_sql")
graph_builder.add_edge("execute_sql", END)

# 编译
graph = graph_builder.compile()

if __name__ == "__main__":
    async def test():

        # 1. 初始所有客户端管理器
        embedding_client_manager.init()
        es_client_manager.init()
        dw_mysql_client_manager.init()
        meta_mysql_client_manager.init()
        qdrant_client_manager.init()

        try:
            # 2. 创建构建的业务对象
            async with (dw_mysql_client_manager.session_factory() as dw_session,
                        meta_mysql_client_manager.session_factory() as meta_session):
                dw_session: AsyncSession
                meta_session: AsyncSession

                state = DataAgentState(query="统计一下各个地区的买了多少钱？")
                context = DataAgentContext(
                    dw_mysql_repo=DWMysqlRepository(dw_session),
                    meta_mysql_repo=MetaMysqlRepository(meta_session),
                    value_es_repo=ValueESRepository(es_client_manager.client),
                    column_qdrant_repo=ColumnQdrantRepository(qdrant_client_manager.client),
                    metric_qdrant_repo=MetricQdrantRepository(qdrant_client_manager.client),
                    embedding_client=embedding_client_manager.client
                )

                # context = DataAgentContext()
                async for chunk in graph.astream(
                        input=state,
                        context=context,
                        stream_mode="custom"
                ):
                    print(chunk)

        except Exception as e:
            # 5。如果失败了，回滚事务
            await dw_session.rollback()
            await meta_session.rollback()
            logger.error(f"构建知识库失败： {str(e)}")
            raise e  # 方便查看异常具体情况
        finally:
            # 6. 最终都要关闭客户端管理器
            await es_client_manager.close()
            await dw_mysql_client_manager.close()
            await meta_mysql_client_manager.close()
            await qdrant_client_manager.close()






    asyncio.run(test())
    # print(graph.get_graph().draw_ascii())
    # print(graph.get_graph().draw_mermaid())