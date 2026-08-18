"""Stage 2：DWMysqlRepository.execute_query 结构化查询测试。

使用 fake session，不依赖真实 MySQL：
- 返回 (columns, rows)；
- SQL 返回 0 行时仍能取得 columns；
- 数据库值在 repository 边界归一化为 JsonScalar（Decimal→float）。
"""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from app.repositories.mysql.dw_mysql_repository import DWMysqlRepository


class _FakeRow:
    def __init__(self, data: dict):
        self._data = data

    def keys(self):
        return self._data.keys()

    def __getitem__(self, key):
        return self._data[key]


class _FakeMappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return [_FakeRow(r) for r in self._rows]


class _FakeExecResult:
    def __init__(self, columns, rows):
        self._columns = columns
        self._rows = rows

    def keys(self):
        return self._columns

    def mappings(self):
        return _FakeMappings(self._rows)


def _make_repo(result):
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    return DWMysqlRepository(session)


def test_execute_query_returns_columns_and_rows():
    result = _FakeExecResult(
        columns=["region_name", "sales_amount"],
        rows=[{"region_name": "华东", "sales_amount": 107373.0}],
    )
    repo = _make_repo(result)
    columns, rows = asyncio.run(repo.execute_query("SELECT ..."))
    assert columns == ["region_name", "sales_amount"]
    assert rows == [{"region_name": "华东", "sales_amount": 107373.0}]


def test_execute_query_empty_still_returns_columns():
    result = _FakeExecResult(columns=["sales_amount"], rows=[])
    repo = _make_repo(result)
    columns, rows = asyncio.run(repo.execute_query("SELECT ..."))
    assert columns == ["sales_amount"]
    assert rows == []


def test_execute_query_normalizes_decimal_to_float():
    """数据库值在 repository 读取边界归一化为 JsonScalar（Decimal→float）。"""
    result = _FakeExecResult(
        columns=["月份", "销售额"],
        rows=[{"月份": 1, "销售额": Decimal("109030.5")}],
    )
    repo = _make_repo(result)
    columns, rows = asyncio.run(repo.execute_query("SELECT ..."))
    assert columns == ["月份", "销售额"]
    assert rows == [{"月份": 1, "销售额": 109030.5}]
    assert isinstance(rows[0]["销售额"], float)


def test_execute_query_normalizes_date_to_str():
    """date/datetime 等类型转为字符串，保证结果可 JSON 序列化。"""
    from datetime import date

    result = _FakeExecResult(
        columns=["日期"],
        rows=[{"日期": date(2025, 1, 1)}],
    )
    repo = _make_repo(result)
    columns, rows = asyncio.run(repo.execute_query("SELECT ..."))
    assert rows == [{"日期": "2025-01-01"}]


def test_execute_sql_legacy_kept():
    """现有 execute_sql() 保留，避免无关兼容问题。"""
    result = _FakeExecResult(
        columns=["a"],
        rows=[{"a": 1}],
    )
    repo = _make_repo(result)
    rows = asyncio.run(repo.execute_sql("SELECT ..."))
    assert rows == [{"a": 1}]
