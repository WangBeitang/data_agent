from elasticsearch import AsyncElasticsearch

from app.models.es.value_info_es import ValueInfoES


class ValueESRepository:
    index_name = "data-agent-value_index"
    mappings = {
        "dynamic": False,
        "properties": {
            "id": {"type": "keyword"},
            "value": {
                "type": "text",
                "analyzer": "standard",
                "fields": {
                    "keyword": {"type": "keyword", "ignore_above": 512}
                },
            },
            "type": {"type": "keyword"},
            "column_id": {"type": "keyword"},
            "column_name": {"type": "keyword"},
            "table_id": {"type": "keyword"},
            "table_name": {"type": "keyword"},
        }
    }
    def __init__(self, client: AsyncElasticsearch):
        self.client = client

    # 确保索引存在
    async def _ensure_index(self):
        client = self.client
        index_name = self.index_name
        # 创建index
            # 测试：如果存在，删除，后面都创建
            # 生产：如果不存在才创建
        if await client.indices.exists(index=index_name):
            await client.indices.delete(index=index_name)
        await client.indices.create(
            index=index_name,
            mappings=self.mappings,
        )

    # 批量插入多个字段值信息数据
    async def insert_values(self, values: list[ValueInfoES]):
        # 确保索引存在
        await self._ensure_index()

        index_dict = {
            "index": {
                "_index": self.index_name
            }
        }

        # 将要保存的数据全部收集到operations中
        operations = []
        for value in values:
            operations.append(index_dict)
            operations.append(value)

        # 批量插入多个字段值信息数据
        # 分批批量插入
        batch_size = 10
        for i in range(0, len(operations), batch_size):
            # 得到当前批次的operations
            batch_operations = operations[i:i+batch_size]
            # 批量插入当前批次的数据
            await self.client.bulk(operations=batch_operations)

    async def search(self, keyword:str)->list[ValueInfoES]:
        result = await self.client.search(
            index=self.index_name,
            query={
                "bool": {
                    "should": [
                        {"term": {"value.keyword": {"value": keyword, "boost": 3.0}}},
                        {"match": {"value": {"query": keyword}}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        )
        # print(result)
        # print(result["hits"]["hits"][0]["_source"])

        # 读取文档数据并整理成目标结构返回
        return [ValueInfoES(**item["_source"]) for item in result["hits"]["hits"]]
