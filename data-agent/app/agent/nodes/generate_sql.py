import yaml
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import llm
from app.agent.state import DataAgentState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt

# result_contract 为空时的占位文本：保持普通问数行为
_NO_CONTRACT_TEXT = "无（普通问数，不约束输出列名）"


async def generate_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    runtime.stream_writer({"stage": "生成SQL", "stage_code": "sql_generation"})
    try:
        query = state["query"]
        table_infos = state["table_infos"]
        metric_infos = state["metric_infos"]
        date_info = state["date_info"]
        db_info = state["db_info"]
        # Stage 2：读取结果结构契约；普通问数时为 None
        result_contract = state.get("result_contract")

        prompt_template = PromptTemplate(
            template=load_prompt("generate_sql"),
            input_variables=["query", "table_infos", "metric_infos", "date_info", "db_info", "result_contract"],
        )
        output_parser = StrOutputParser()
        chain = prompt_template | llm | output_parser
        sql = await chain.ainvoke({
            "query": query,
            "table_infos": yaml.dump(table_infos,allow_unicode=True,sort_keys=False),
            "metric_infos": yaml.dump(metric_infos,allow_unicode=True,sort_keys=False),
            "date_info": yaml.dump(date_info,allow_unicode=True,sort_keys=False),
            "db_info": yaml.dump(db_info,allow_unicode=True,sort_keys=False),
            "result_contract": yaml.dump(result_contract, allow_unicode=True, sort_keys=False)
            if result_contract else _NO_CONTRACT_TEXT,
        })

        logger.info(f"生成SQL完成：{sql}")

        return {"sql": sql}
    except Exception as e:
        logger.error(f"生成SQL失败", exc_info=True)
        raise
