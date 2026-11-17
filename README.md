# Arena-Agents · AI 狼人杀（Werewolf）多智能体博弈沙盒

[English](README.en.md) | **简体中文**

**Arena-Agents 是一个开源的 AI 狼人杀、社交推理与多智能体博弈沙盒。** 游戏规则写在 Markdown
剧本中；**每一位玩家和主持人都是异步 Agent**（前端 Actor 随时收发消息，后端 Worker 负责思考和
工具调用），由 LLM 主持人按剧本手册推进流程，引擎只提供与游戏无关的频道、发言权、请求与账本。
项目内置一套完整的 12 人狼人杀，也可以用同一引擎运行其他社交推理、剧本杀或桌游规则。

关键词：**AI 狼人杀、狼人杀、多智能体狼人杀、AI 多智能体博弈、社交推理、LLM 智能体、
AI 桌游、AI 剧本杀、social deduction、AI Werewolf、LLM Werewolf**。

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.12.1--dev-blue.svg)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)

<img src="docs/images/v012-main-view.png" alt="AI 狼人杀实时对局主界面（上帝视角）" width="100%">

## 主要能力

- **全员异步 Agent（v0.12）**：每个玩家和主持人都是 Actor + Worker。Actor 永远能收发消息，
  不会被思考或工具执行阻塞；发言、投票、质询和能力随时可以发生，引擎在**提交的那一刻**校验。
- **引擎不替你省钱（v0.12.1）**：每一条投递给 agent 的消息都会立刻唤醒它（没有防抖、没有轮数上限、没有
  “执行深度”），狼人可以随时“拍刀”、白狼王随时自爆；agent 只有三种状态——思考 / 行动 / 休眠，
  `sleep(秒数)` 可以给自己设闹钟。token 靠**编排**管理：每个 agent 一条持久的只追加对话（命中供应商的
  前缀缓存）、运行中到达的消息合并注入、窗口快满时才压缩。
- **预算上限（按局）**：顶栏的 token / 金额统计旁有一个上限按钮，可设 token 和 / 或美元上限，并选择
  “达到上限后强制停止”；80% 变黄，100% 提示或暂停（原因“预算”）。费用按**四类 token**
  （未命中缓存的输入 / 缓存读 / 缓存写 / 输出）和四个价格精确计算。
- **价格源**：Provider 对话框里可从价格表（默认 LiteLLM 的 `model_prices_and_context_window.json`）
  自动填入模型的四个价格和上下文窗口，你仍可手动修改。
- **角色名册**：12 个座位 `N1`–`N12`，默认名字 Alice…Leo，可改名并保存；每人有一个由程序化素材库随机组合的
  **像素头像**（可重新生成全部或单个）；性格设定与记忆是工作区里的纯文本文件，默认为空，可导入 / 导出 /
  清空，也可让主持人的模型把几句要点“完善”成草稿；赛后手动“复盘”会把战绩和第一人称笔记写进记忆。
- **自适应界面**：从 1920 宽屏到手机竖屏都能用（按平台与宽度自动切换布局）。
- **LLM 主持人驱动流程**：轮流发言 = 主持人公告 + 发言权管理（`set_floor_queue` 等工具）；
  规则文本与“主持人手册”写在剧本里，引擎本身不含任何游戏词汇。
- **频道与发言权**：可见性只由频道决定；主持人可随时关闭发言权（玩家收到“无发言权”回执），
  发言既有时间限制也有字数限制；玩家可以私聊质询主持人，主持人可以惩罚骚扰者（关闭其私聊）。
- **只追加的账本**：所有已发生的事都记在账本里，存档、上下文和观战都以它为准；主持人可以改玩家的
  当前状态（出局、复活…），但不能改写历史。
- **可执行 Markdown 剧本（schema v2）**：身份池、模板、能力、检查项和默认限额写在
  `scenarios/*.md` 的 front matter 中，正文是规则说明和只给主持人看的手册。
- **多 Provider / 多模型**：支持 OpenAI Chat Completions、OpenAI Responses、Anthropic
  Messages；主持人及每位玩家可以使用不同模型和思考深度。
- **模型模板**：保存 3 个 Provider/模型/思考深度预设，勾选玩家后一键应用。
- **工作文件夹（Workspace）**：每个工作区独立保存 Provider、模板、绑定、对局、日志和加密密钥；
  不同游戏、不同 API 配置不会互相污染。
- **安全运行**：对局运行中锁定模型与工作区；切换工作区必须先暂停并保存成功。
- **本地加密**：Provider 密钥和对局存档使用 AES-256-GCM；Windows 工作区主密钥由 DPAPI 保护。
- **API 端点预览**：保存 Provider 前显示最终消息地址和模型列表地址；支持自动拼接 `/v1/...`
  或直接填写完整自定义 endpoint。
- **观战界面**：玩家用像素头像 + 名字 + 身份标签表示，点击标签看工具调用/思考/token 统计；
  信息流只显示发言和请求卡片（整条渲染，不流式），支持“上帝视角”和任意玩家视角。
- **演示模式**：`ARENA_DEMO=1` 用脚本化机器人代替模型，不需要 API Key 就能完整体验一局。

## 界面截图

| 玩家指标浮层（点击标签） | 玩家视角（只看到该玩家可见的内容） |
|---|---|
| <img src="docs/images/v012-popover.png" alt="玩家指标浮层" width="100%"> | <img src="docs/images/v012-player-view.png" alt="玩家视角" width="100%"> |
| **深色主题** | **工作文件夹** |
| <img src="docs/images/v012-main-dark.png" alt="深色主题" width="100%"> | <img src="docs/images/workspace-modal.png" alt="工作文件夹选择" width="100%"> |
| **Provider 管理与 API 地址预览** | **模型模板与玩家绑定** |
| <img src="docs/images/provider-modal.png" alt="Provider 管理与 API 地址预览" width="100%"> | <img src="docs/images/templates-bindings.png" alt="模型模板与玩家绑定" width="100%"> |

> 截图来自本地**演示模式**对局（脚本化机器人）；工作区路径和 Provider 主机名已做像素化脱敏，不包含任何真实凭据。

详细操作见 [docs/ui-guide.md](docs/ui-guide.md)。

## 工作原理

```text
浏览器 ──REST + SSE v2（带 id 的增量事件，支持断线续传）──► FastAPI API ── 工作文件夹 (.arena/)
                                                              │              ├─ providers.v1.enc
                                                              ▼              ├─ settings.json
                                                      GameOrchestrator       ├─ sessions/*.enc
                                                              │              ├─ logs/<对局>/trace-*.jsonl
                              ┌───────────────────────────────┴────────┐     └─ 工作区密钥
                              ▼                                        ▼
        World（同步、与游戏无关）                       Runtime（asyncio）
        提交闸门 → 账本（只追加）                       13 个 Agent = Actor（pending）+ Worker（LLM 循环）
        频道 · 发言权 · 请求 · 模板                     持久会话 · 休眠闹钟 · Supervisor · 看门狗
        能力 · 质询 · 随机数 · 游戏时钟                 Brain：状态板 + 记忆 + 可见历史 + 工具 schema
                                                              │
                                                              ▼
                                      OpenAI Chat / OpenAI Responses / Anthropic Messages 适配器
```

引擎本身不理解“狼人”“警长”“第 2 夜”等游戏概念（`tests/v12/test_neutrality.py` 会扫描）。
剧本给出规则和手册，**主持人 Agent** 用工具推进流程；玩家用 `speak` / `respond` / `use_ability` /
`challenge_moderator` 行动。思维链和工具调用只写进明文 trace 日志，前端不展示。模块说明见
[docs/architecture.md](docs/architecture.md)，实现契约见 [docs/v0.12-contracts.md](docs/v0.12-contracts.md)。

## 快速开始

### 环境要求

- Python 3.12
- Node.js 20（仅前端测试需要）
- 一个 Provider API Key（OpenAI 兼容、OpenAI Responses 或 Anthropic）

### 本机运行

```bash
git clone <repository-url> Arena-Agents
cd Arena-Agents

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。

首次启动会创建默认工作区：

```text
Windows: %USERPROFILE%\Arena-Agents\Default
macOS:   ~/Arena-Agents/Default
Linux:   <应用数据目录>/workspace/default
```

Provider 配置在界面中完成，本地使用不需要 `.env`。

### 演示模式（无需模型 / API Key）

```bash
ARENA_DEMO=1 ARENA_DISABLE_WORKSPACE=1 uvicorn app.main:app --host 127.0.0.1 --port 8000
```

演示模式用脚本化机器人扮演主持人和 12 名玩家，走真实的引擎、频道、发言权、请求和界面，
但没有任何网络请求和费用；演示局不支持从存档继续（需要新建对局）。

### 隔离的开发实例与真实模型冒烟

```bash
# 与你的真实工作区完全隔离的实例（数据在 .demo-data/，已被 git 忽略）
python tools/dev_server.py --demo --port 8123
python tools/dev_server.py --copy-providers-from "<工作区>/.arena" --port 8124   # 复制 Provider 与绑定，会调用真实模型并产生费用

# 带硬性调用上限的真实模型冒烟（会产生费用），结束后打印工具使用、被拒绝的调用和发言权时间线
python tools/real_smoke.py --from "<工作区>/.arena" --scenario scenarios/minimal_test.md --players 4 --max-calls 60
```

### Docker Compose

```bash
cp .env.example .env
# 建议设置 ARENA_API_TOKEN，保护配置写接口
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())" \
  > /secure/arena-master-key

export ARENA_MASTER_KEY_HOST_FILE=/secure/arena-master-key
docker compose up -d --build
```

容器以非 root 用户运行，默认只把 `127.0.0.1:${PORT:-8000}` 映射到宿主机；运行数据位于
`/data/workspace/default/.arena`。主密钥通过 Docker Secret 或独立密钥文件提供，缺失时应用
拒绝写入，不会回退到明文。

## 配置模型

1. 打开 **Provider → 新增 Provider**。
2. 选择协议：OpenAI Chat Completions / OpenAI Responses / Anthropic Messages。
3. 填写名称、Base URL、API Key。
4. 选择是否自动拼接 API 路径：
   - **开启**：Base URL 视为根地址，最终使用 `/v1/chat/completions`、`/v1/responses` 或
     `/v1/messages`，模型发现使用 `/v1/models`。
   - **关闭**：Base URL 就是完整消息地址，原样发送；模型发现关闭，只能手动填写模型 ID。
5. 获取模型（自动拼接开启时可用），每次选择一个模型，手动填写输入/输出价格（美元/1M tokens）。
6. 在 **模型绑定** 中创建模板 1/2/3，或逐人选择 Provider/模型/思考深度。

完整说明见 [docs/providers.md](docs/providers.md)。

## 工作文件夹

工作文件夹是一个普通目录，所有私有运行状态位于 `.arena/`：

```text
<workspace>/
├─ .arena-workspace.json      工作区标记
└─ .arena/
   ├─ master.v1.dpapi         Windows DPAPI 包装的工作区密钥
   ├─ providers.v1.enc        加密 Provider 注册表
   ├─ providers.v1.backup.enc 已知良好备份
   ├─ settings.json           默认绑定、模板、上次对局
   ├─ sessions/<id>/          当前与上一版加密对局
   ├─ logs/  exports/
   └─ workspace.lock
```

- 只有对局处于 idle/paused/finished 时才允许切换工作区；运行中返回 409。
- 切换前会先保存当前对局；保存失败或目标无效时保持当前工作区。
- 原生文件夹选择器只允许本机回环使用；Docker/无桌面环境使用绝对路径输入。
- 代码资源（`config.yml`、`scenarios/`、`prompts/`）由所有工作区共享。
- 早期单目录数据会复制到默认工作区并验证，原文件不会删除。

完整说明见 [docs/workspaces.md](docs/workspaces.md)。

## 界面操作

- **玩家块**：极简图案 + 短 id + 一个主标签；点击标签查看该 Agent 的工具调用次数、思考次数、
  LLM 调用次数、token 与费用。出局者置灰。
- **信息流**：只显示发言、公告和质询回复，整条渲染；没有思维链、工具调用和流式输出。
- **观战视角**：顶栏下拉框切换“上帝视角”或某名玩家的视角（只显示该玩家有权看到的频道与消息）。
- **频道栏**：显示每个频道的发言权状态（🔒 关闭 / 🎤 持麦 / 📋 队列 / 💬 自由讨论 / ❄ 冻结）；
  “质询工单”单独成组。
- **对局控制**：开始 / 暂停 / 继续；对局结束后仍可点击“继续”恢复（主持人会收到通知）。
- **看板**：顶栏按钮整体折叠；左右侧栏在各自头部折叠；分隔条可拖动，双击恢复默认。布局保存在浏览器
  `localStorage`。

## 环境变量

| 变量 | 默认值 | 用途 |
|---|---|---|
| `ARENA_WORKSPACE_DIR` | `%USERPROFILE%\Arena-Agents\Default`（Windows/macOS） | 初始/默认工作区 |
| `ARENA_DISABLE_WORKSPACE` | 未设置 | 设为 1 禁用工作区，退回单目录兼容模式 |
| `ARENA_DATA_DIR` | 平台应用目录 | 应用状态/兼容数据目录 |
| `ARENA_MASTER_KEY_FILE` / `ARENA_MASTER_KEY` | 未设置（Windows 用 DPAPI） | Linux/Docker 的 32 字节工作区主密钥 |
| `ARENA_API_TOKEN` | 未设置 | `/api/*` 访问令牌；SSE 也支持 `?token=` |
| `ARENA_ALLOWED_ORIGINS` | 未设置 | 额外允许的跨源来源 |
| `ARENA_ALLOWED_BASE_HOSTS` | 未设置 | Provider 主机白名单（可选） |
| `ARENA_ALLOW_PRIVATE_PROVIDERS` | `false` | 允许本机/内网 Provider（如 Ollama） |
| `ARENA_MAX_IMPORT_BYTES` | `20971520` | 导入存档大小上限 |
| `ARENA_BIND` | `127.0.0.1` | Docker 宿主机绑定地址 |
| `ARENA_DEMO` | 未设置 | 设为 1 启用演示模式（脚本化机器人，不调用模型） |

## 安全

- Provider Key 加密保存，API 永不返回；修改协议、Base URL 或拼接模式必须重新输入 Key。
- 非 GET 请求校验同源 `Origin`；无 token 时配置写接口仅允许回环客户端。
- Provider URL 阻止云元数据/链路本地地址；本机/内网 Provider 需要显式开启。
- 工作区切换和原生文件夹选择仅限本机；运行中不允许切换。
- 导入存档有大小与结构校验，替换前不会破坏当前对局。
- 默认监听 `127.0.0.1`；对外暴露请配置 TLS 反向代理与 `ARENA_API_TOKEN`。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests
```

测试完全离线，不调用真实 LLM（引擎测试用脚本化的 FakeLLM / 演示机器人）。安装 Node.js 后会运行前端
测试，否则自动跳过。CI 覆盖 Ubuntu 与 Windows。

## 目录结构

```text
app/
├─ world/        物理层：提交闸门、账本、频道、发言权、请求、模板、能力、质询（同步、游戏无关）
├─ runtime/      异步运行时：Actor/Worker、Supervisor、LLM 闸门、trace 日志、事件总线、游戏时钟
├─ agents/       Brain：工具 schema、上下文构建、笔记与压缩、提示注入防护
├─ game/         编排器、会话容器（存档 v4）、名册 / 预算 / 复盘、绑定、工作区切换、演示机器人
├─ api/          REST + SSE v2、Provider、模板、工作区
├─ engine/       LLM 客户端、表达式引擎
├─ llm/          三协议适配器与 API 端点规划
├─ providers/    加密 Provider 注册表、模型的四个价格、价格源
├─ avatars.py    像素头像（素材库 static/avatar/parts.json 由 tools/avatar_lab.py 生成）
├─ storage/      工作区、设置、名册、对局、加密
└─ migration.py  旧数据迁移
scenarios/       Markdown 剧本（schema v2）
prompts/         主持人 / 玩家提示词模板
knowledge/       主持人指南与策略资料
static/          单页观战界面
tests/           离线回归、API、存储与 UI 测试
docs/            架构、Provider、工作区与 UI 文档
```

## 版本说明

当前开发版本为 **0.12.1**：在 0.12 的异步 Agent 之上，去掉引擎里所有“替用户省钱”的限制，
改为持久会话 + 缓存 + 合并 + 压缩的编排，并加入按局预算、四价计费与价格源、角色名册（像素头像、性格与记忆）
和自适应界面。**v0.12.1 不兼容 v0.12.0 及更早的对局存档**（旧存档会保留在存档列表中、不会被改动）。
最近的稳定版本是 0.11.0。高层发布说明见 [CHANGELOG.md](CHANGELOG.md)。

## 文档

- [架构](docs/architecture.md)
- [v0.12 开发方案与决策记录](docs/v0.12-async-agents-plan.md) · [v0.12.1 方案](docs/v0.12.1-plan.md) · [实现契约](docs/v0.12-contracts.md)
- [Provider 与模型](docs/providers.md)
- [工作区与加密数据](docs/workspaces.md)
- [界面操作指南](docs/ui-guide.md)
- [English README](README.en.md)
- [发布说明](CHANGELOG.md)

## 贡献

欢迎提交 Issue 和 Pull Request。提交前请：

1. 游戏规则放在 `scenarios/`，引擎保持游戏无关。
2. 行为变更附带离线测试。
3. 不要提交 `.env`、API Key、工作区、本地路径或运行数据。
4. 运行 `python -m pytest -q tests` 和 `node --check static/js/*.js`。

## 许可证

MIT，见 [LICENSE](LICENSE)。
