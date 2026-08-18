"""Stage 2：稳定 stage_code 测试。

验证冻结 SPEC §3.7 的 stage_code 映射：
- 关键节点必须产生稳定 code，同时保留中文 stage；
- 不修改节点业务逻辑（仅验证事件结构）。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent.state import DataAgentState

# 冻结映射（SPEC §3.7）
EXPECTED = {
    "extract_keywords": "query_retrieval",
    "recall_column": "query_retrieval",
    "recall_metric": "query_retrieval",
    "recall_value": "query_retrieval",
    "merge_retrieved_info": "query_retrieval",
    "filter_metric": "query_retrieval",
    "filter_table": "query_retrieval",
    "add_extra_context": "query_retrieval",
    "generate_sql": "sql_generation",
    "validate_sql": "sql_validation",
    "correct_sql": "sql_validation",
    "execute_sql": "sql_execution",
}


class FakeRuntime:
    def __init__(self, context=None):
        self.context = context or {}
        self.events = []

    def stream_writer(self, event):
        self.events.append(event)


def _run(coro):
    return asyncio.run(coro)


def _make_context(**extra):
    context = {
        "embedding_client": MagicMock(),
        "column_qdrant_repo": MagicMock(),
        "metric_qdrant_repo": MagicMock(),
        "value_es_repo": MagicMock(),
        "meta_mysql_repo": MagicMock(),
        "dw_mysql_repo": MagicMock(),
    }
    context.update(extra)
    return context


# ---------- fake LLM chain（可被 langchain 接受的 callable） ----------

class _FakeLLM:
    """返回字符串的 callable，langchain 会将其包装为 RunnableLambda。

    对 JsonOutputParser 场景返回 JSON 字符串（如 "[]"、"{}"），
    对 StrOutputParser 场景直接返回 SQL 字符串。
    """

    def __init__(self, value: str):
        self.value = value

    def __call__(self, input_data):
        return self.value


# ---------- 基础 state ----------

def _base_state(**extra):
    state = DataAgentState(
        query="统计2025年各月销售额",
        keywords=["销售额"],
        sql="SELECT 1",
        error=None,
        recall_columns=[],
        recall_metrics=[],
        recall_values=[],
        table_infos=[],
        metric_infos=[],
        date_info={"date": "2026-08-18", "weekday": "Tuesday", "quarter": "Q3"},
        db_info={"version": "8.4", "dialect": "mysql"},
    )
    state.update(extra)
    return state


# ---------- 单个节点的 stage_code 断言 ----------

def _assert_stage_code(node_name, node_fn, state, runtime, **patches):
    with patches:
        _run(node_fn(state, runtime)) if not _is_sync(node_fn) else node_fn(state, runtime)
    assert runtime.events, f"{node_name} 未产生事件"
    event = runtime.events[0]
    assert event.get("stage_code") == EXPECTED[node_name], f"{node_name} stage_code 错误: {event}"
    assert event.get("stage"), f"{node_name} 必须保留中文 stage: {event}"


def _is_sync(fn):
    import inspect
    return not inspect.iscoroutinefunction(fn)


# ---------- 各节点测试 ----------

def test_extract_keywords_stage_code():
    from app.agent.nodes.extract_keywords import extract_keywords

    state = DataAgentState(query="统计各区域销售额")
    runtime = FakeRuntime()
    extract_keywords(state, runtime)
    assert runtime.events[0]["stage_code"] == "query_retrieval"
    assert runtime.events[0]["stage"] == "提取关键字"


def test_recall_column_stage_code():
    from app.agent.nodes.recall_column import recall_column

    emb = MagicMock()
    emb.aembed_query = AsyncMock(return_value=[0.1])
    repo = MagicMock()
    repo.search = AsyncMock(return_value=[])
    state = _base_state()
    runtime = FakeRuntime(_make_context(embedding_client=emb, column_qdrant_repo=repo))
    with patch("app.agent.nodes.recall_column.llm", _FakeLLM("[]")), \
         patch("app.agent.nodes.recall_column.load_prompt", lambda name: "{query}"):
        _run(recall_column(state, runtime))
    assert runtime.events[0]["stage_code"] == "query_retrieval"
    assert runtime.events[0]["stage"] == "召回字段"


def test_recall_metric_stage_code():
    from app.agent.nodes.recall_metric import recall_metric

    emb = MagicMock()
    emb.aembed_query = AsyncMock(return_value=[0.1])
    repo = MagicMock()
    repo.search = AsyncMock(return_value=[])
    state = _base_state()
    runtime = FakeRuntime(_make_context(embedding_client=emb, metric_qdrant_repo=repo))
    with patch("app.agent.nodes.recall_metric.llm", _FakeLLM("[]")), \
         patch("app.agent.nodes.recall_metric.load_prompt", lambda name: "{query}"):
        _run(recall_metric(state, runtime))
    assert runtime.events[0]["stage_code"] == "query_retrieval"
    assert runtime.events[0]["stage"] == "召回指标"


def test_recall_value_stage_code():
    from app.agent.nodes.recall_value import recall_value

    repo = MagicMock()
    repo.search = AsyncMock(return_value=[])
    state = _base_state()
    runtime = FakeRuntime(_make_context(value_es_repo=repo))
    with patch("app.agent.nodes.recall_value.llm", _FakeLLM("[]")), \
         patch("app.agent.nodes.recall_value.load_prompt", lambda name: "{query}"):
        _run(recall_value(state, runtime))
    assert runtime.events[0]["stage_code"] == "query_retrieval"
    assert runtime.events[0]["stage"] == "召回字段值"


def test_merge_retrieved_info_stage_code():
    from app.agent.nodes.merge_retrieved_info import merge_retrieved_info

    state = _base_state()
    runtime = FakeRuntime(_make_context())
    _run(merge_retrieved_info(state, runtime))
    assert runtime.events[0]["stage_code"] == "query_retrieval"
    assert runtime.events[0]["stage"] == "合并召回"


def test_filter_metric_stage_code():
    from app.agent.nodes.filter_metric import filter_metric

    state = _base_state()
    runtime = FakeRuntime(_make_context())
    with patch("app.agent.nodes.filter_metric.llm", _FakeLLM("{}")), \
         patch("app.agent.nodes.filter_metric.load_prompt", lambda name: "{query}"):
        _run(filter_metric(state, runtime))
    assert runtime.events[0]["stage_code"] == "query_retrieval"
    assert runtime.events[0]["stage"] == "过滤指标"


def test_filter_table_stage_code():
    from app.agent.nodes.filter_table import filter_table

    state = _base_state()
    runtime = FakeRuntime(_make_context())
    with patch("app.agent.nodes.filter_table.llm", _FakeLLM("{}")), \
         patch("app.agent.nodes.filter_table.load_prompt", lambda name: "{query}"):
        _run(filter_table(state, runtime))
    assert runtime.events[0]["stage_code"] == "query_retrieval"
    assert runtime.events[0]["stage"] == "过滤表"


def test_add_extra_context_stage_code():
    from app.agent.nodes.add_extra_context import add_extra_context

    repo = MagicMock()
    repo.get_db_infos = AsyncMock(return_value={"version": "8.4", "dialect": "mysql"})
    state = _base_state()
    runtime = FakeRuntime(_make_context(dw_mysql_repo=repo))
    _run(add_extra_context(state, runtime))
    assert runtime.events[0]["stage_code"] == "query_retrieval"
    assert runtime.events[0]["stage"] == "添加额外信息"


def test_generate_sql_stage_code():
    from app.agent.nodes.generate_sql import generate_sql

    state = _base_state()
    runtime = FakeRuntime(_make_context())
    with patch("app.agent.nodes.generate_sql.llm", _FakeLLM("SELECT 1")), \
         patch("app.agent.nodes.generate_sql.load_prompt",
               lambda name: "{table_infos}\n{metric_infos}\n{date_info}\n{db_info}\n{result_contract}\n{query}"):
        _run(generate_sql(state, runtime))
    assert runtime.events[0]["stage_code"] == "sql_generation"
    assert runtime.events[0]["stage"] == "生成SQL"


def test_validate_sql_stage_code():
    from app.agent.nodes.validate_sql import validate_sql

    repo = MagicMock()
    repo.validate_sql = AsyncMock()
    state = _base_state()
    runtime = FakeRuntime(_make_context(dw_mysql_repo=repo))
    _run(validate_sql(state, runtime))
    assert runtime.events[0]["stage_code"] == "sql_validation"
    assert runtime.events[0]["stage"] == "校验SQL"


def test_correct_sql_stage_code():
    from app.agent.nodes.correct_sql import correct_sql

    state = _base_state(error="SQL语法错误")
    runtime = FakeRuntime(_make_context())
    with patch("app.agent.nodes.correct_sql.llm", _FakeLLM("SELECT 1")), \
         patch("app.agent.nodes.correct_sql.load_prompt",
               lambda name: "{table_infos}\n{metric_infos}\n{date_info}\n{db_info}\n{query}\n{sql}\n{error}"):
        _run(correct_sql(state, runtime))
    assert runtime.events[0]["stage_code"] == "sql_validation"
    assert runtime.events[0]["stage"] == "校正SQL"


def test_execute_sql_stage_code_and_structured_result():
    from app.agent.nodes.execute_sql import execute_sql

    repo = MagicMock()
    repo.execute_query = AsyncMock(return_value=(["销售额"], [{"销售额": 109030.5}]))
    state = _base_state()
    runtime = FakeRuntime(_make_context(dw_mysql_repo=repo))
    returned = _run(execute_sql(state, runtime))

    # stage_code 稳定
    assert runtime.events[0]["stage_code"] == "sql_execution"
    assert runtime.events[0]["stage"] == "执行SQL"
    # legacy result 保留
    assert {"result": [{"销售额": 109030.5}]} in runtime.events
    # 内部结构化字段
    query_result = [e for e in runtime.events if "query_result" in e]
    assert len(query_result) == 1
    assert query_result[0]["query_result"]["columns"] == ["销售额"]
    assert query_result[0]["query_result"]["sql"] == "SELECT 1"
    # 最终 State 可获得结构化结果
    assert returned["result_columns"] == ["销售额"]
    assert returned["result_rows"] == [{"销售额": 109030.5}]


def test_all_expected_nodes_have_stable_stage_codes():
    """确保冻结映射表中的节点全部存在对应文件并被测试覆盖到阶段映射。"""
    from pathlib import Path

    nodes_dir = Path(__file__).parents[1] / "app" / "agent" / "nodes"
    for node_name in EXPECTED:
        assert (nodes_dir / f"{node_name}.py").exists(), f"缺少节点文件 {node_name}"
