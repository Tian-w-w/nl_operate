# LLM4Web · 自然语言订单工作台

新机器安装、启动及连接另一台机器的模型：[部署指南](docs/deployment.md)。模型 URL、Key 和模型名填写在**项目根目录 `.env`**，该文件不提交到 GitHub。

v0.5 新增可引用来源的知识问答、服务端多轮查询上下文和收货城市去重统计。内置知识为明确标注的演示规则，正式使用前应替换。[功能与测试示例](docs/features_v0.5.md) · [本轮验证](docs/verification_v05.md)。

v0.4 已接入持久化只读Agent循环、步骤面板、本地模型兼容接口及独立评测。[配置与功能](docs/features_v0.4_agent.md) · [本轮验收](docs/verification_v04_agent.md)。本地URL、Key、模型名留空且调用关闭，待部署后填写；没有真实模型效果成绩。

v0.3 已增加高级筛选、常用查询、完整商品/物流详情、统计看板与操作审计：[功能说明](docs/features_v0.3.md) · [本轮验收](docs/features_v03_verification.md)。

只读 Agent 第一阶段历史记录：[工具契约与预写任务标准](docs/agent_tools_milestone_1.md)。

此前工程验收：[2026-10-27 验收记录](docs/verification_2026-10-27.md)。

基于 [原始研究方案](docs/Research_LLM4Web.md) 开始实施的本地研究项目。自然语言通过受限工具改变真实页面与合成订单数据，修改必须预览、确认并检查版本。

**当前为工程原型：订单后台、默认离线单步助手、持久化只读Agent循环、本地模型兼容接口，以及可选OpenAI适配器。真实模型接口仅以模拟响应验证；本地URL/Key留空，实际模型效果等待部署后评测。未完成B1/B2/B3、Text-to-SQL或研究实验。** 规则/脚本模式只用于检查业务与评价器，不能据此报告LLM任务成功率。

## 快速启动（Windows / PowerShell）

要求 Python **3.11+**、Node **20.19+ / 22.12+**，首次安装需要网络。本机旧 Anaconda Python 3.9 不适用，请明确指定新解释器。

```powershell
cd E:\项目经历\llm4web
# 新机器：替换为自己的 Python 3.11+ 路径
.\scripts\bootstrap.ps1 -Python 'C:\path\to\python.exe'
.\.venv\Scripts\python.exe -m scripts.manage db-seed
.\scripts\dev.ps1
```

访问 **http://127.0.0.1:8000**。本次工作区依赖、构建与 1,000 条演示订单已经初始化，直接运行 `scripts/dev.ps1` 即可。不要再次对有订单的数据库 seed；重复 seed 会被拒绝。

`bootstrap.ps1` 安装 `requirements.lock`、执行 `npm ci`、构建前端并运行 Alembic。本地使用 SQLite，无需 Docker。`.env.example` 可复制为 `.env`；`scripts.manage` 加载 `.env`，直接启动 uvicorn 时须自己提供环境变量。

开发前端时，终端一运行后端，终端二运行：

```powershell
cd frontend
npm ci
npm run dev
# http://127.0.0.1:5173；/api 代理到 :8000
```

## 演示步骤

1. 右上角选择“租户 A · 操作员”。三个演示身份由服务器白名单映射到租户与角色。
2. 输入：`找出上周已付款但未发货的上海订单，按金额从高到低展示`。
3. 输入：`其中等待超过48小时的标为优先处理`，检查完整订单列表与差异，再点击“确认并提交”。
4. 输入：`导出这些订单`，点击真实 CSV 下载链接。
5. 切换只读身份重复修改，查看拒绝状态；输入 `清理一下这些订单`，查看澄清。

业务时钟固定为 **2026-10-26 12:00 +08:00**，过期计时使用真实 UTC 时钟。数据和所有客户均为合成。演示登录允许切换身份，**仅供本地实验，不能作为生产认证或公开部署**。

离线规则支持单城市、五种订单状态、上周创建/付款、金额不低于/不超过、等待超过 N 小时、金额排序、上轮继续筛选、当前页第二条/勾选项标记、按城市/状态统计、导出和取消。未知条件会澄清；不支持任意自然语言。确认不能由聊天文字或模型参数代替。

## 检查与复现

```powershell
# 静态检查、生成契约漂移与后端测试
.\.venv\Scripts\python.exe -m ruff check backend evaluation scripts tests
.\.venv\Scripts\python.exe -m scripts.generate_contracts --check
.\.venv\Scripts\python.exe -m scripts.manage test
# 单独生成可追溯的开发集运行记录和报告
.\.venv\Scripts\python.exe -m scripts.manage test-smoke
.\.venv\Scripts\python.exe -m scripts.manage eval-report --run-dir 'experiments/runs/<run_id>'
# 前端类型检查与构建
cd frontend
npm run build
```

浏览器检查在 Windows 使用 Microsoft Edge，默认在独立的 **18000** 端口启动测试服务，不复用或停止 8000 演示服务。Linux CI 使用 Chromium：

```powershell
# 从项目根目录运行，目标只能是 runtime/ 下的专用 fixture 库
.\.venv\Scripts\python.exe -m scripts.manage fixture-reset --database 'sqlite:///./runtime/llm4web_fixture_e2e.db' --confirm-fixture
cd frontend
npm run test:e2e
```

每次运行保存 `manifest.json / tasks.jsonl / events.jsonl / summary.json / summary.csv / report.md / artifacts/`，含静态数据摘要、适配器与评价器摘要、依赖、代码提交、逐任务终态及 CSV。20 条任务都是开发冒烟集，标准答案为静态手写数据，尚待独立人工复核；没有冻结测试集。评价器会检查顺序、金额、修改目标、非目标数据和真实导出，并由故意错误结果验证。

## 已实现范围

- React + TypeScript + Vite：手工筛选、分页、详情、统计、勾选、预览确认、下载、执行记录与刷新恢复。
- FastAPI + Pydantic：严格参数白名单、服务器身份、上下文版本、受控 JSON 页面状态。
- SQLAlchemy + Alembic：五张业务表及结果集、会话、计划/步骤、确认、操作、审计、导出表。
- 固定种子：3 租户、100 客户、50 商品、1,000 订单；独立的 16 单边界 fixture。
- 修改仅限已付款未发货订单的优先级/复核状态；500 条上限，整体事务，版本冲突整体撤销。
- 确认绑定集合、patch、版本、身份、页面版本与期限；新查询/选中变化取消旧预览。
- 幂等键绑定请求摘要，操作与审计和业务写入同事务；提交后丢响应可查询/重试同一结果。
- CSV 为持久化内容，UTF-8 BOM、固定字段、SHA-256、归属检查与导出幂等；下载时由数据库内容提供文件。

## 目录

```text
backend/app/          配置、数据模型、服务、API、规则适配器
backend/migrations/   固定版本迁移；schema_v1 不引用可变 ORM metadata
frontend/             本地订单工作台
data/tasks/           20 条开发任务，不向适配器传 oracle
data/oracles/         独立静态标准终态
evaluation/           运行器、终态评价器、报告
tests/                可靠性、评价器反例与浏览器测试
scripts/              Windows 初始化/启动与管理命令
docs/                 业务、架构、评测及实际进度
```

## PostgreSQL 与下一阶段

`compose.yaml` 提供 PostgreSQL 17 本地服务。安装 Docker 后启动数据库，将 `DATABASE_URL` 改为 `postgresql+psycopg://llm4web:local_synthetic_only@127.0.0.1:5432/llm4web_demo`，再执行 `db-migrate` 和 `db-seed`。当前本地验收针对 SQLite，**本机没有 Docker/PostgreSQL，新增 PostgreSQL 测试尚未在本机运行，未配置 RLS/只读 SQL 角色**。

本轮修改见 [工程化记录](docs/engineering_2026-10-27.md)。下一阶段验证真实模型、实际运行 PostgreSQL/容器，再建立 B1/B2/B3 与消融；之后实现数据库级受限 Text-to-SQL。当前没有 SQL 执行入口，也没有浏览器自主操作或模型训练。

数据库、密钥、缓存、node_modules 和逐次运行文件已列入 `.gitignore`。新环境通过锁文件与迁移复现；原始研究方案保留于 `docs/`，原文件未改动。

## 模型接入、CI 与容器

本地模型接入见[本地模型配置](docs/features_v0.4_agent.md)。填写`.env`中的`MODEL_NAME`、`LOCAL_MODEL_BASE_URL`、可选`LOCAL_MODEL_API_KEY`，选择`MODEL_ADAPTER=local`和正确协议。网关提供完整请求地址时，改用`LOCAL_MODEL_ENDPOINT_URL`，它优先于基础地址且不拼接路径。准备好后才启用`MODEL_CALLS_ENABLED=true`。模板默认关闭模型调用。网页选择“只读Agent”使用多步流程；`python -m scripts.manage agent-eval`只运行脚本回归，显式加`--real`才使用已配置的模型。原有单步路径保留。

默认规则模式不产生模型费用。若启用 OpenAI，在后端 `.env` 中填写 `.env.example` 的模型标识、key、输入/输出费率、单会话及全项目预算，并显式设置 MODEL_ADAPTER=openai 和 MODEL_CALLS_ENABLED=true。费率没有默认猜测值；配置不完整会拒绝启用。**本轮没有真实密钥联调或模型成绩。**

模型网络调用发生在数据库事务外，返回后重新检查会话与上下文；费用未知时保留预算占用。当前会话用量可通过 `GET /api/model-usage` 查看。`.env` 不提交。

前端类型由实际 OpenAPI 生成：变更 API 后运行 `python -m scripts.generate_contracts` 并提交 `frontend/src/api-types.ts`。CI 会检查漂移。

CI 已配置 Windows/Linux 检查、独立 PostgreSQL 集成、Chromium 浏览器测试与 Docker 构建；尚未推送执行。本机没有 Docker/PostgreSQL，对应实机检查会明确跳过。PostgreSQL 测试只接受专用测试库：

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/llm4web_test'
.\.venv\Scripts\python.exe -m pytest -m postgres
```

Docker 路径使用合成演示认证并仅绑定回环地址：

```text
docker compose --profile app build app
docker compose --profile app run --rm app python -m scripts.manage db-migrate
docker compose --profile app run --rm app python -m scripts.manage db-seed
docker compose --profile app up -d
```

镜像默认关闭演示认证，Compose 的本地 app profile 显式启用；真实认证尚未实现，不公开部署。`/api/health` 检查进程，`/api/ready` 检查数据库与迁移是否就绪。

浏览器测试可通过 LLM4WEB_TEST_PORT、LLM4WEB_PYTHON、PLAYWRIGHT_CHANNEL 指定端口、解释器与浏览器。测试库不与演示库共用。
