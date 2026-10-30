# 新机器安装与本地模型配置

源码仓库：https://github.com/Tian-w-w/nl_operate

## 安装什么

- Git：下载代码。
- Python 3.11 或更新版本，推荐使用 Python 3.12。Linux 还需要对应的 `venv`、`pip` 支持。
- Node.js：推荐 Node.js 22，至少 22.12，包含 npm；项目也支持 Node.js 20.19 及以上的 20 系列。
- 浏览器：访问网页即可，日常运行不需要安装 Playwright 浏览器。

Python 依赖由 `requirements.lock` 安装，前端依赖由 `frontend/package-lock.json` 安装。默认 SQLite 随 Python 使用，不需要另外安装 MySQL、PostgreSQL、Redis 或 Docker。模型已在另一台机器部署时，项目机器不需要安装模型权重、CUDA 或 GPU 推理环境。

## Windows / PowerShell 首次安装

以下命令在准备存放代码的目录执行。若系统找不到 `python`，将初始化参数替换成自己的 Python 3.11+ 可执行文件绝对路径。

```powershell
git clone https://github.com/Tian-w-w/nl_operate.git
cd nl_operate
Copy-Item .env.example .env
.\scripts\bootstrap.ps1 -Python python
.\.venv\Scripts\python.exe -m scripts.manage db-seed
.\scripts\dev.ps1
```

如 PowerShell 提示脚本执行被策略阻止，可以在当前终端执行 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`，再运行脚本；该设置只作用于当前进程。

访问 <http://127.0.0.1:8000>。初始化脚本会创建虚拟环境、安装后端依赖、安装和构建前端、迁移数据库。`db-seed` 只在空库执行一次，生成 1,000 条合成订单；后续启动不需要重复执行。

## Linux / macOS 首次安装

下面假设 Python 3.12 的命令为 `python3.12`；可替换为本机其他满足版本要求的解释器。

```bash
git clone https://github.com/Tian-w-w/nl_operate.git
cd nl_operate
cp .env.example .env
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
cd frontend
npm ci
npm run build
cd ..
.venv/bin/python -m scripts.manage db-migrate
.venv/bin/python -m scripts.manage db-seed
.venv/bin/python -m scripts.manage dev
```

访问地址同上。以上是按现有跨平台管理入口整理的安装流程，当前工作区没有另外进行 Linux/macOS 新机器安装验收。

## 模型 URL 和 Key 填在哪里

编辑克隆目录下的 `.env`，例如 Windows 下的 `D:\projects\nl_operate\.env`。当前工作区对应 `E:\项目经历\llm4web\.env`。文件与 `README.md`、`compose.yaml` 位于同一层，**不是** `frontend/.env` 或 `backend/.env`。新机器只在没有 `.env` 时从模板复制，不要覆盖已有配置。

把 `.env` 中的对应行修改为以下内容。IP、端口、模型名和 Key 都替换为实际值：

```dotenv
MODEL_ADAPTER=local
MODEL_CALLS_ENABLED=true
MODEL_NAME=实际的模型标识
LOCAL_MODEL_BASE_URL=http://192.168.1.100:8000/v1
LOCAL_MODEL_API_KEY=实际的服务密钥
LOCAL_MODEL_PROTOCOL=chat_completions
```

- `MODEL_NAME` 必须是模型服务接受的标识，可以从服务配置或其 `/v1/models` 接口确认。
- `LOCAL_MODEL_BASE_URL` 是 API 基础地址。通常包含 `/v1`，不要填写完整的 `/chat/completions` 或 `/responses` 请求地址；项目会拼接后缀。
- 如果平台提供的是完整请求地址，改用 `LOCAL_MODEL_ENDPOINT_URL`。此字段优先于 `LOCAL_MODEL_BASE_URL`，直接向该地址发送 POST，不拼接路径；协议仍由 `LOCAL_MODEL_PROTOCOL` 指定。
- `LOCAL_MODEL_API_KEY` 会以 `Authorization: Bearer ...` 发送；模型服务没有鉴权时可以留空。本地模型使用此字段，`OPENAI_API_KEY` 留空即可。
- `chat_completions` 请求基础地址后的 `/chat/completions`；如果服务提供 Responses 兼容接口，则改为 `responses`。
- 需要兼容所选协议，支持工具调用或返回接口要求的严格 JSON 决策；仅能自由聊天的模型接口无法保证执行 Agent 任务。

例如企业网关要求直接请求自定义路径时，可以配置：

```dotenv
MODEL_ADAPTER=local
MODEL_CALLS_ENABLED=true
MODEL_NAME=平台提供的模型标识
LOCAL_MODEL_BASE_URL=
LOCAL_MODEL_ENDPOINT_URL=https://gateway.example.com/v3/openai/model
LOCAL_MODEL_API_KEY=实际的服务密钥
LOCAL_MODEL_PROTOCOL=chat_completions
```

将示例域名替换为实际地址。请求格式仍为所选协议的 JSON，默认鉴权仍为 `Authorization: Bearer ...`；如果网关还要求自定义请求体或鉴权，需要按平台调用示例另外适配。完整地址与基础地址都必须使用 HTTP(S)，不能包含用户名、密码、查询参数或 fragment。模型标识和密钥不要放入 URL。

模型在另一台机器时，填写**模型机器的局域网 IP / 可达域名**。`127.0.0.1` 指向项目进程所在机器；只有两者在同一台机器上运行时才适用。模型服务需要监听可供项目机器访问的地址，并允许对应端口连接。前端通过本项目后端调用模型，Key 无需提供给前端。

`.env`、`.env` 的其他本地变体及数据库已排除出 Git，仓库只保存空凭据模板。通过 `python -m scripts.manage dev` 或 `scripts/dev.ps1` 启动会加载根目录 `.env`；直接执行 uvicorn 不会自动加载这个文件。已有进程环境变量优先于 `.env`，修改后需要**停止并重新启动后端**。

## 启动、关闭与验证

v0.5 新增知识库、多轮追加和城市数量统计，操作示例及正式资料替换方式见 [v0.5 功能说明](features_v0.5.md)。

Windows 后续启动：

```powershell
cd D:\projects\nl_operate
.\scripts\dev.ps1
```

Linux/macOS 后续启动：在仓库根目录执行 `.venv/bin/python -m scripts.manage dev`。服务在当前终端前台运行，按 **Ctrl+C** 关闭。默认构建产物由后端提供，不需要同时启动 Vite。

可以访问 <http://127.0.0.1:8000/api/ready> 检查数据库就绪状态。模型配置生效后，在网页助手执行模式中选择“只读 Agent”，输入：

> 查出上海等待发货超过 48 小时的订单，按金额降序排列。如果超过 10 条就导出，否则展示列表并告诉我数量。

核对步骤记录、实际订单及导出文件。日常演示数据与评测边界数据不同，不能假定演示结果固定为某个数量。就绪接口正常不代表已成功连接模型，真实任务与真实评测才会实际调用模型。

真实模型评测在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m scripts.manage agent-eval --real
```

Linux/macOS 将解释器路径替换为 `.venv/bin/python`。结果写入 `experiments/runs/`，使用独立实验数据库。去掉 `--real` 运行的是脚本回归，不能作为真实模型成绩。

默认模型单次超时 20 秒、单任务最多 6 次模型决策 / 4 次工具调用、总时限 90 秒。慢速模型可调整 `.env` 的 `MODEL_TIMEOUT_SECONDS`（最多 60）和 `AGENT_MAX_SECONDS`（最多 180）；保留执行上限。每个会话还有 `MODEL_MAX_CALLS_PER_SESSION` 限制。

## 更新代码

先停止后端，在仓库根目录执行 `git pull --ff-only`。Windows 再运行初始化脚本更新依赖、构建前端并迁移数据库，Linux/macOS 重复相应安装、构建及 `db-migrate` 命令。保留 `.env` 和 `runtime/`，不要重新复制模板或重复初始化演示数据。迁移已有数据库前先备份数据库。

项目当前使用演示身份切换，后端只接受本机访问；这里的步骤面向本地运行。连接远端模型不需要将本项目网页暴露到公网。PostgreSQL / Docker 是可选路径，另见根目录 README；当前工作区未实机验收该路径。
