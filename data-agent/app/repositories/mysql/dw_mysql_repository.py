from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _to_json_scalar(value):
    """将数据库返回值归一化为 JsonScalar（str | int | float | bool | null）。

    - Decimal（MySQL SUM 等返回类型）转为 float；
    - date/datetime 等其它类型转为字符串，保证结果可 JSON 序列化。

    归一化发生在 repository 读取边界，QueryTable 不再做值类型转换。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


class DWMysqlRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_column_types(self, table_name: str)->dict[str,str]:
        """获取当前表的所有字段的类型"""
        # 定义查询sql
        sql = f"show columns from {table_name}"
        # 执行sql
        result = await self.session.execute(text(sql))
        # 整理数据并返回
        return {row.Field:row.Type for row in result.all()}

    async def get_column_values(self, table_name:str, column_name:str, limit:int=10) ->list:
        """查询指定字段的前limit个字段值"""
        # 定义查询sql
        sql = f"select distinct {column_name} from {table_name} limit {limit}"
        # 执行sql
        result = await self.session.execute(text(sql))
        # 整理数据并返回
        return result.scalars().all()

    async def get_db_infos(self) ->dict[str,str]:
        result = await self.session.execute(text("select version()"))
        version = result.scalar()
        dialect = self.session.get_bind().dialect.name
        return {"version":version, "dialect":dialect}

    async def validate_sql(self, sql):
        """校验SQL"""
        await self.session.execute(text(f"explain {sql}"))

    async def execute_sql(self, sql):
        """执行一条查询的SQL（兼容旧调用，仅返回行数据）"""
        result = await self.session.execute(text(sql))
        # return result.mappings().all()   #rowMapping对象  不能直接序列化
        return [dict(row) for row in result.mappings().all()]  # 转换为了字典的数组 =》方便序列化为json字符串

    async def execute_query(self, sql) -> tuple[list[str], list[dict]]:
        """结构化执行一条查询SQL，返回 (columns, rows)。

        - columns：使用 result.keys() 取得，SQL 返回 0 行时仍能取得列名；
        - rows：使用 result.mappings().all() 取得，并在本边界将数据库值
          归一化为 JsonScalar（Decimal→float、date→str），保证可 JSON 序列化。
        """
        result = await self.session.execute(text(sql))
        columns = list(result.keys())
        rows = [
            {key: _to_json_scalar(value) for key, value in dict(row).items()}
            for row in result.mappings().all()
        ]
        return columns, rows

    """
    序列化：将某种语言的对象或数组转为特定格式的字符串（json/yaml）
    反序列化：将特定格式的字符串转换为某种语言的对象或数组
    """

