"""Stage 2：DWMysqlRepository.execute_query 结构化查询测试。

使用 fake session，不依赖真实 MySQL：
- 返回 (columns, rows)；
- SQL 返回 0 行时仍能取得 columns。
"""

import asyncio
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


def test_execute_sql_legacy_kept():
    """现有 execute_sql() 保留，避免无关兼容问题。"""
    result = _FakeExecResult(
        columns=["a"],
        rows=[{"a": 1}],
    )
    repo = _make_repo(result)
    rows = asyncio.run(repo.execute_sql("SELECT ..."))
    assert rows == [{"a": 1}]
