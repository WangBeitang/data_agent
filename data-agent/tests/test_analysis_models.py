"""Stage 2：公共分析数据对象测试。

覆盖：
- QueryTable：success / empty / row_count / 空结果保留 columns / Decimal 转换；
- QueryExecutionResult：success / empty / failed 及状态一致性校验。
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.analysis import ObservationStatus, QueryExecutionResult, QueryTable


# ==================== QueryTable ====================

def test_query_table_success():
    table = QueryTable(
        columns=["月份", "销售额"],
        rows=[{"月份": 1, "销售额": 109030.5}],
        row_count=1,
    )
    assert table.columns == ["月份", "销售额"]
    assert table.rows == [{"月份": 1, "销售额": 109030.5}]
    assert table.row_count == 1


def test_query_table_empty_keeps_columns():
    """SQL 成功但 0 行：rows=[]、row_count=0，columns 仍保存数据库真实返回列名。"""
    table = QueryTable(columns=["销售额"], rows=[], row_count=0)
    assert table.columns == ["销售额"]
    assert table.rows == []
    assert table.row_count == 0


def test_query_table_row_count_must_match_len_rows():
    with pytest.raises(ValidationError):
        QueryTable(columns=["销售额"], rows=[{"销售额": 1}], row_count=2)


def test_query_table_row_count_zero_with_non_empty_rows_fails():
    with pytest.raises(ValidationError):
        QueryTable(columns=["销售额"], rows=[{"销售额": 1}], row_count=0)


def test_query_table_rejects_non_json_scalar():
    """数据库值归一化发生在 repository 边界，QueryTable 不接受非 JsonScalar 值。"""
    with pytest.raises(ValidationError):
        QueryTable(
            columns=["销售额"],
            rows=[{"销售额": Decimal("109030.5")}],
            row_count=1,
        )


# ==================== QueryExecutionResult ====================

def test_execution_result_success():
    result = QueryExecutionResult(
        query="统计2025年各月销售额",
        sql="SELECT ...",
        table=QueryTable(columns=["月份", "销售额"], rows=[{"月份": 1, "销售额": 109030.5}], row_count=1),
        status=ObservationStatus.success,
        error=None,
    )
    assert result.status == ObservationStatus.success
    assert result.error is None
    assert result.table.row_count == 1


def test_execution_result_empty():
    result = QueryExecutionResult(
        query="查询2028年销售额",
        sql="SELECT ...",
        table=QueryTable(columns=["销售额"], rows=[], row_count=0),
        status=ObservationStatus.empty,
        error=None,
    )
    assert result.status == ObservationStatus.empty
    assert result.error is None
    assert result.table.columns == ["销售额"]
    assert result.table.rows == []
    assert result.table.row_count == 0


def test_execution_result_failed():
    result = QueryExecutionResult(
        query="查询无法执行的问题",
        sql=None,
        table=QueryTable(columns=[], rows=[], row_count=0),
        status=ObservationStatus.failed,
        error="安全错误信息",
    )
    assert result.status == ObservationStatus.failed
    assert result.error == "安全错误信息"
    assert result.sql is None


def test_execution_result_success_requires_rows():
    """success 状态不允许 row_count == 0。"""
    with pytest.raises(ValidationError):
        QueryExecutionResult(
            query="q",
            sql="SELECT ...",
            table=QueryTable(columns=["销售额"], rows=[], row_count=0),
            status=ObservationStatus.success,
            error=None,
        )


def test_execution_result_failed_requires_error():
    """failed 状态必须有 error。"""
    with pytest.raises(ValidationError):
        QueryExecutionResult(
            query="q",
            sql=None,
            table=QueryTable(columns=[], rows=[], row_count=0),
            status=ObservationStatus.failed,
            error=None,
        )


def test_execution_result_success_requires_sql():
    with pytest.raises(ValidationError):
        QueryExecutionResult(
            query="q",
            sql=None,
            table=QueryTable(columns=["销售额"], rows=[{"销售额": 1}], row_count=1),
            status=ObservationStatus.success,
            error=None,
        )


def test_execution_result_empty_requires_no_error():
    with pytest.raises(ValidationError):
        QueryExecutionResult(
            query="q",
            sql="SELECT ...",
            table=QueryTable(columns=["销售额"], rows=[], row_count=0),
            status=ObservationStatus.empty,
            error="不应出现",
        )


def test_execution_result_empty_requires_sql():
    """empty = SQL 成功执行但无数据，sql 不能为 null。"""
    with pytest.raises(ValidationError):
        QueryExecutionResult(
            query="q",
            sql=None,
            table=QueryTable(columns=["销售额"], rows=[], row_count=0),
            status=ObservationStatus.empty,
            error=None,
        )


def test_execution_result_empty_requires_zero_rows():
    """empty 状态要求 row_count == 0。"""
    with pytest.raises(ValidationError):
        QueryExecutionResult(
            query="q",
            sql="SELECT ...",
            table=QueryTable(columns=["销售额"], rows=[{"销售额": 1}], row_count=1),
            status=ObservationStatus.empty,
            error=None,
        )
