# AGENTS.md

## 项目定位

本目录包含一个可运行的问数原型，当前以电商订单数据为演示数据，通过 LangGraph（图式工作流编排框架）完成“元数据召回 -> SQL 生成 -> SQL 校验/修正 -> MySQL 查询 -> SSE（服务器推送事件）流式返回”。后续目标是在保留问数核心链路的基础上，将业务数据替换为制造业数据，并增加多步下钻和经营归因报告能力。

当前开发优先级是核心业务演示链路。默认使用单一演示用户，不建设登录、IAM（身份与访问管理）、多租户或复杂运维后台；除非后续任务明确要求，不要把资源投入这些外围能力。

正式本地测试环境是 macOS。历史 `data-agent/docker_windows/` 已由用户删除；正式模型、SQL 和云端基础设施配置分别位于 `models/`、`sql/`、`cloud-infra/`，不得重新创建或引用 `docker_windows/`。

## 目录结构

- `data-agent/`：Python 3.12 + FastAPI（Python Web 框架）后端。
  - `main.py`：后端入口，默认监听 `0.0.0.0:8000`。
  - `app/agent/graph.py`：当前单轮问数 LangGraph（图式工作流编排框架）。
  - `app/agent/nodes/`：字段/指标/字段值召回、元数据过滤、SQL 生成、校验、修正和执行节点。
  - `app/services/query_service.py`：执行 Graph（工作流图）并输出 SSE（服务器推送事件）。
  - `app/services/meta_knowledge_service.py`：构建 MySQL 元数据库、Qdrant（向量数据库）集合和 Elasticsearch（全文检索引擎）索引。
  - `app/scripts/build_meta_knowledge.py`：元数据知识构建入口。
  - `conf/app_config.yaml`：运行连接配置；当前包含本地连接和模型配置，不要在日志或回复中输出密码/API Key（接口密钥）。
  - `conf/meta_config.yaml`：当前电商表、字段、别名和指标定义；制造业改造时这是核心修改入口之一。
  - `sql/`：正式使用的 `meta2`、`dw2` 初始化 SQL 和当前电商演示数据。
  - `prompts/`：召回、过滤、SQL 生成与修正 Prompt（提示词）。
- `date-agent-frontend/`：Vue 3 + Vite 前端。目录名当前确实是 `date-agent-frontend`，不要在未核对引用范围前擅自改名。
  - `src/App.vue`：单页聊天界面，消费 `/api/query` 的 SSE（服务器推送事件），展示执行步骤和结果表格。
  - `vite.config.js`：开发代理固定转发到 `http://localhost:8000`。
- `cloud-infra/`：为云服务器补充 Qdrant（向量数据库）和低内存 Elasticsearch（全文检索引擎）的独立 Compose（多容器编排）及分步启动说明。

外层 `data_agent/` 当前不是 Git 仓库；两个子目录也没有可用的 Git 元数据。不要假设存在提交历史或可用 `git` 回滚。

## 当前运行链路

请求入口为 `POST /api/query`，请求体为：

```json
{"query": "统计各个地区的销售额"}
```

当前 Graph（工作流图）按以下顺序执行：

1. Jieba（中文分词工具）提取关键词；
2. 并行召回字段、指标和字段值；
3. 合并召回结果，并从元数据库补充表关系；
4. 由 LLM（大语言模型）过滤指标和表字段；
5. 补充当前日期和 MySQL 版本；
6. 由 LLM（大语言模型）生成一条 SQL；
7. 通过 `EXPLAIN` 校验 SQL，失败时调用一次 SQL 修正；
8. 执行 SQL 并将结果表通过 SSE（服务器推送事件）返回前端。

当前 Graph（工作流图）是一次查询、一次 SQL 的问数链路，不包含经营归因所需的分析计划、循环下钻、影响量计算和最终归因报告。后续改造应优先在现有链路外增加“分析计划 -> 多次问数子任务 -> 汇总证据 -> 报告”的上层编排，不要先重写已经存在的元数据召回链路。

## 启动所需外部服务

当前代码在启动和查询时依赖以下服务：

1. MySQL：同一个 MySQL 实例中使用两个数据库。
   - `meta2`：表、字段、指标及其关系元数据。
   - `dw2`：当前电商演示数据仓库；制造业改造后需要替换或新增制造业库。
2. Qdrant（向量数据库）：保存字段和指标向量。
   - 当前集合名硬编码为 `data-agent_column_collection` 和 `data-agent_metric_collection`。
   - 重跑元数据构建脚本会删除并重建这两个集合。
3. Elasticsearch（全文检索引擎）：保存可检索字段值。
   - 当前索引名硬编码为 `data-agent-value_index`。
   - 重跑元数据构建脚本会删除并重建该索引。
4. Embedding（向量化）模型：当前使用本机目录 `models/bge-large-zh-v1.5`，由后端通过 `HuggingFaceEmbeddings`（进程内本地向量模型客户端）在 CPU 上直接加载，向量维度为 1024；不依赖独立 HTTP 服务或 Docker（容器）。
5. LLM（大语言模型）服务：通过 LangChain（大模型应用框架）调用，模型名和 API Key（接口密钥）来自 `conf/app_config.yaml`。

Kibana（Elasticsearch 可视化工具）不参与程序运行，不是必需依赖。

## 单云服务器复用原则

资源有限时不要为本项目重复部署基础设施：

- 复用已有 MySQL 实例，在其中创建本项目独立数据库；不需要运行第二个 MySQL 容器。
- 复用已有 Qdrant（向量数据库）实例，但必须使用本项目独立集合名，避免构建脚本删除其他项目集合。
- 复用已有 Elasticsearch（全文检索引擎）实例，但必须使用本项目独立索引名。
- 当前 Embedding（向量化）模型固定在 Mac 本机进程内运行。更换模型或输出维度后必须重建 Qdrant（向量数据库）集合，不能混用历史向量。
- 后端、前端和共享基础服务可以位于同一台云服务器。第一版无需 Kubernetes（容器编排平台）、服务注册中心或独立网关。
- 当前后端在 Mac 本地运行，MySQL、Qdrant（向量数据库）和 Elasticsearch（全文检索引擎）通过云服务器公网地址访问，Embedding（向量化）模型在 Mac 进程内运行。若后续部署拓扑变化，必须按实际网络位置重新核对地址，不能机械改成 `localhost`。

用户于 2026-08-18 提供的云服务器 `docker ps` 截图显示，当前已有以下容器：

- MySQL 8.4，对外映射宿主机 `3308 -> 3306`；
- Redis 7；
- MongoDB 7；
- Milvus 2.6.15 standalone，以及配套 MinIO、etcd；
- Nginx。

这些是用户提供的当时状态，不等于本地已实际连通验证。当前 `data-agent` 可直接复用 MySQL；Redis、MongoDB、MinIO、etcd 和 Nginx 不属于现有问数核心链路的直接依赖。Milvus（向量数据库）不能通过改配置直接替代 Qdrant（向量数据库），需要先替换客户端、Repository（数据访问层）和元数据构建逻辑。

资源方案曾评估过两种，当前已经选择第一种作为基线，后续 Agent（开发代理）不得擅自切换：

1. **当前采用**：原代码基线方案，保留 Qdrant（向量数据库）和 Elasticsearch（全文检索引擎），云端只补这两个服务；Embedding（向量化）模型在 Mac 本地运行。优点是问数代码改动最少，先验证原项目。
2. **暂不采用**：复用现有 Milvus（向量数据库）替代 Qdrant（向量数据库），并用 MySQL 字段值字典替代 Elasticsearch（全文检索引擎）。只有基线因资源无法运行时再重新评估。

2026-08-18 用户提供的资源快照已经确认：云服务器总内存约 3.6 GiB、available（可用）约 1.6 GiB，Swap（交换空间）已使用约 833 MiB；其中 Milvus standalone 约占 964 MiB，MinIO 约占 199 MiB，MySQL 约占 294 MiB。用户决定先尝试增加 Qdrant 和低内存配置的 Elasticsearch，以换取问数代码基本不改。部署文件位于 `cloud-infra/`，必须先启动 Qdrant 并观察资源，再启动 Elasticsearch；出现 OOM、持续重启、available 内存长期低于约 500 MiB 或现有容器异常时立即停止新增服务。

同日已完成实际部署和 Mac 侧连通验证：云服务器 `49.235.159.92` 的 MySQL `3308`、Qdrant `6333`、Elasticsearch `9200` 均可达；Qdrant `/collections` 返回成功且初始集合为空，Elasticsearch 集群状态为 `green`。新增服务启动后系统 available（可用）内存约 968 MiB、Swap（交换空间）约使用 971 MiB，Qdrant 约占 43 MiB，Elasticsearch 约占 733 MiB/768 MiB。该状态可继续做基线测试，但 Elasticsearch 已接近容器上限，禁止继续增加云端常驻服务；测试期间持续观察 OOM、Swap 增长和现有服务状态。

Mac 开发机已确认是 arm64、16 GiB 内存。完整 Sentence Transformers（句向量模型框架）目录已由用户移动到 `data-agent/models/bge-large-zh-v1.5/`，约 1.2 GiB、输出维度 1024；`data-agent/.gitignore` 已忽略 `/models/`，不要复制或提交模型权重。

当前 `EmbeddingClientManager` 已改为 `HuggingFaceEmbeddings`（进程内本地向量模型客户端），从 `models/bge-large-zh-v1.5` 加载模型并在 CPU 上生成归一化向量。`sentence-transformers`（句向量模型框架）和 SQLAlchemy（Python 数据库工具）的异步运行依赖 `greenlet` 已写入 `pyproject.toml` 和 `uv.lock`；不要恢复 HTTP 客户端，也不要在 Mac 上启动 TEI（文本向量推理）容器。

## 已确认的启动顺序

### 1. 安装后端依赖

从 `data-agent/` 执行：

```bash
uv sync --locked
```

项目要求 Python 3.12，版本来自 `.python-version` 和 `pyproject.toml`。

### 2. 准备 MySQL 数据库

在目标 MySQL 实例执行：

```bash
mysql -h <mysql-host> -P <mysql-port> -u <mysql-user> -p < sql/meta.sql
mysql -h <mysql-host> -P <mysql-port> -u <mysql-user> -p < sql/dw.sql
```

两个 SQL 脚本会重建其负责的演示表。重复执行前先确认库中没有需要保留的业务数据；不要在未确认数据用途时执行。

### 3. 配置服务地址

修改 `data-agent/conf/app_config.yaml`，至少核对：

- `db_meta`、`db_dw`：MySQL 地址、端口、账号、密码和数据库名；
- `qdrant`：Qdrant（向量数据库）地址、端口和向量维度；
- `embedding`：本地模型目录、运行设备和向量归一化开关；
- `es`：Elasticsearch（全文检索引擎）地址和端口；
- `llm`：模型名和 API Key（接口密钥）。

当前代码直接从 YAML 读取敏感值；后续若改为环境变量，要同时更新配置加载代码和启动说明，不能只改配置文件。

### 4. 构建元数据知识

确认 MySQL、Qdrant（向量数据库）、Elasticsearch（全文检索引擎）可访问，且本地 Embedding（向量化）模型目录完整后，从 `data-agent/` 执行：

```bash
uv run python -m app.scripts.build_meta_knowledge
```

该步骤必须在首次查询前完成。它会读取 `conf/meta_config.yaml`，写入 `meta2`，重建两个 Qdrant（向量数据库）集合和一个 Elasticsearch（全文检索引擎）索引。

### 5. 启动后端

从 `data-agent/` 执行：

```bash
uv run python main.py
```

最小接口验证：

```bash
curl -N -X POST http://127.0.0.1:8000/api/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"统计各个地区的销售额"}'
```

预期依次收到多个 `stage` 事件，最后收到 `result` 数组；仅返回 HTTP 200 不能证明查询链路成功。

### 6. 启动前端

从 `date-agent-frontend/` 执行：

```bash
npm ci
npm run dev
```

开发模式下前端通过 Vite 代理访问本机 `8000` 端口。若前后端不在同一主机，必须调整 `vite.config.js` 或增加统一反向代理。

## Docker 与本地运行边界

- Mac 本地只运行前端、后端和进程内 Embedding（向量化）模型，不依赖本地 Docker。
- 云端 Qdrant（向量数据库）和 Elasticsearch（全文检索引擎）使用 `cloud-infra/` 中的 Compose（多容器编排），MySQL 复用既有容器。
- 仓库包含约 1.2 GB 的本地 Embedding（向量化）模型权重；不要再次复制或提交模型。

## 开发与验证约束

- 当前未发现统一的 pytest（Python 测试框架）测试套件或 CI（持续集成）配置；`app/agent/graph_test.py` 和各模块的 `__main__` 主要是手工演示入口。
- 修改 Graph（工作流图）、Prompt（提示词）、元数据或数据库结构后，至少重新执行元数据构建和 `/api/query` 端到端验证。
- `app/**/logs/`、`__pycache__/`、前端 `dist/` 和本地模型权重属于运行或构建产物，除非任务明确要求，不要编辑或复制。
- 当前日志中已有成功问数记录，也有 SQL 语法可执行但关联条件错误、结果为空的记录；`EXPLAIN` 只能验证 SQL 可执行，不能验证业务语义正确。后续归因改造必须为核心演示问题准备确定性期望结果。
- 云端 Elasticsearch（全文检索引擎）使用官方镜像且未安装 IK（中文分词插件）；字段值索引使用内置 `standard` 分析器并保留 `keyword` 精确匹配子字段，不得在未更新云端镜像的情况下重新引入 `ik_max_word`。
- 当前目标不是生产级安全平台。第一版不增加登录、IAM（身份与访问管理）、多租户、复杂审批和独立数据库安全子系统；但不要删除已有 SQL 查询约束和校验节点，因为它们属于问数核心链路的稳定性保障。

## 2026-08-18 已验证基线

- `uv sync --locked` 可完成依赖安装；本机真实加载 `bge-large-zh-v1.5` 后可生成维度为 1024、数值有限的向量。
- 用户新建的 MySQL 账号可同时访问 `meta2` 和 `dw2`；`dw2.fact_order` 当前有 115 行演示数据。
- `uv run python -m app.scripts.build_meta_knowledge` 已完整成功。构建后 MySQL 元数据为 5 张表、24 个字段、2 个指标、3 条字段指标关系；Qdrant（向量数据库）的字段集合为 98 个 point（向量记录）、指标集合为 8 个 point（向量记录）；Elasticsearch（全文检索引擎）的字段值索引为 75 条 document（文档记录）。
- 用户更新 LLM（大语言模型）的 API Key（接口密钥）后，`POST /api/query` 已完成关键词提取、字段/指标/字段值召回、LLM（大语言模型）过滤、SQL 生成、`EXPLAIN` 校验和 MySQL 执行的完整 SSE（服务器推送事件）链路。基线问题“统计各个地区的销售额”返回华东 107373.0、华南 70202.0、华北 41099.5、西南 31528.0、华中 28957.0；使用独立 SQL 直接复算得到相同结果。
- 当前基础问数基线已经可用，不需要重新初始化 MySQL、Qdrant（向量数据库）、Elasticsearch（全文检索引擎）或本地 Embedding（向量化）模型。任何回复和日志都不得暴露真实 API Key（接口密钥）。

## 制造业归因改造方向

后续任务开始前，先冻结两条制造业经营归因场景，再决定表结构和指标。建议保持分层：

```text
归因分析编排层
  -> 将一个“为什么”问题拆成多次问数任务
  -> 调用现有问数 Graph
  -> 根据每次 Observation（观察结果）决定继续下钻或结束
  -> 汇总可复算证据并生成归因报告

现有问数执行层
  -> 元数据召回
  -> SQL 生成/校验/修正
  -> MySQL 执行
  -> 返回结构化结果
```

不要为了包装成“多智能体”而拆出多个无必要 Agent（智能体）。第一版使用一个归因分析 Agent（智能体）加现有问数子流程即可。
