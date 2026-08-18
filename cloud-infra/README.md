# Data Agent 云端共享基础服务

本目录只为现有问数代码补充 Qdrant（向量数据库）和 Elasticsearch（全文检索引擎）。MySQL 复用云服务器已有 `customer-service-mysql`，Embedding（向量化）模型在 Mac 本地运行，不使用历史 `docker_windows/`。

2026-08-18 已在云服务器完成部署，并从 Mac 验证 `49.235.159.92:6333`、`49.235.159.92:9200` 和既有 MySQL `49.235.159.92:3308` 可达。Qdrant 初始集合为空，Elasticsearch 集群为 `green`。以下步骤保留用于重建和故障排查。

云服务器总内存约 3.6 GiB，必须分步启动并观察资源，不能首次直接同时启动两个服务。

## 1. 上传与预检

将本目录上传到云服务器独立目录后执行：

```bash
cd <cloud-infra目录>
docker compose config
```

预期：配置可以完整展开且没有报错。

## 2. 先启动 Qdrant

```bash
docker compose up -d qdrant
docker compose ps
curl -fsS http://127.0.0.1:6333/collections
docker stats --no-stream
free -h
```

预期：`/collections` 返回 JSON，容器保持运行。若容器重启、被 OOM（内存不足）终止或服务器 available（可用）内存明显不足，先停止，不继续启动 Elasticsearch。

## 3. 再启动 Elasticsearch

```bash
docker compose up -d elasticsearch
docker compose ps
curl -fsS http://127.0.0.1:9200/_cluster/health?pretty
docker stats --no-stream
free -h
```

预期：Elasticsearch 最终为 `yellow` 或 `green`；单节点环境出现 `yellow` 通常表示副本无法分配，不妨碍当前单节点测试。

停止条件：

- Elasticsearch 持续重启或出现 OOM；
- available（可用）内存长期低于约 500 MiB；
- Swap（交换空间）持续快速增长；
- 现有 Milvus、MySQL 或其他业务容器出现异常。

满足停止条件时执行：

```bash
docker compose stop elasticsearch qdrant
```

`stop` 不删除数据卷。只有确认测试数据不再需要时，才考虑删除卷；不要执行未经确认的 `docker compose down -v`。

## 4. Mac 本地连接地址

后端仍在 Mac 运行时，`conf/app_config.yaml` 使用云服务器地址：

```yaml
qdrant:
  host: <云服务器IP>
  port: 6333
  embedding_size: 1024

es:
  host: <云服务器IP>
  port: 9200
  index_name: data_agent
```

现有 Elasticsearch Repository（数据访问层）实际使用硬编码索引 `data-agent-value_index`，`es.index_name` 当前没有参与该索引命名。
