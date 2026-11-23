# 💰 智能记账助手（AgentScope 2.0 + Gradio）

用自然语言记账的对话式助手：说一句"今天打车花了 32 块"，它自动归类、写库、并在超出预算时提醒你。

## 功能

- **自然语言记账**：自动抽取物品 / 金额 / 分类，支持模糊归类（打车 → 交通、奶茶 → 餐饮、纸巾 → 日用）
- **查询**：按分类查、按月查（"上个月餐饮花了多少"）
- **月度汇总**：总额 + 各分类金额、笔数、占比
- **删除错账**：按 id 精确删，或按关键词删最近一笔，并回显删掉的内容
- **预算管理**：给分类设月度预算，记账时自动检查是否超支

## 技术栈

| 组件 | 选型 |
|---|---|
| Agent 框架 | AgentScope 2.0.6（Toolkit + FunctionTool） |
| 大模型 | 通义千问 qwen-max（DashScope） |
| 界面 | Gradio 6 ChatInterface |
| 存储 | SQLite（`my_money.db`，自动建表 / 迁移） |

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

配置 API Key（二选一）：

```bash
set DASHSCOPE_API_KEY=sk-xxxx      # Windows CMD
export DASHSCOPE_API_KEY=sk-xxxx   # macOS / Linux
```

或在项目根目录新建 `key.txt`，里面只写一行 key。

启动：

```bash
python Hello.py
# 浏览器打开 http://127.0.0.1:7860
```

## 目录结构

```
iot-agent/
├─ Hello.py           # 主程序：工具函数 + Agent + Gradio 界面
├─ requirements.txt
├─ key.txt            # 本地密钥，已在 .gitignore 中
├─ my_money.db        # 本地账本，已在 .gitignore 中
└─ 启动.bat            # Windows 一键启动
```

## 设计说明

- **权限模式**：demo 使用 `PermissionMode.BYPASS` 让工具直接执行；生产环境建议改回默认模式，把"写库"这类操作交给用户确认。
- **兼容性**：`build_agent()` 会按 `inspect.signature` 判断当前 AgentScope 版本支持哪个权限参数，2.0.x 与旧版都能跑。
- **数据迁移**：`init_db()` 会自动给老库补 `created_at` 列，并把历史分类名归一化。

## 踩坑记录

开发过程中踩过的四个坑，都是真实报错，记下来备查。

**1. `TypeError: Agent.__init__() got an unexpected keyword argument 'require_tool_permission'`**

早期代码照抄了旧版 API 的布尔开关，但 AgentScope 2.0.6 的 `Agent` 已经没有这个参数——2.0 把权限做成了引擎。
现在的写法用 `inspect.signature` 判断当前版本支持哪个参数，两代 API 都能跑：

```python
params = inspect.signature(Agent.__init__).parameters
if "state" in params:                      # 2.0.x
    kwargs["state"] = AgentState(permission_context=PermissionContext(mode=PermissionMode.BYPASS))
if "require_tool_permission" in params:    # 旧版
    kwargs["require_tool_permission"] = False
```

**2. 网页能打开，但发消息后一直回 "I'm waiting for your permission or the external execution to finish."**

2.0 默认 `PermissionMode.DEFAULT`，每次工具调用都要用户确认；Gradio 界面没有确认入口，于是工具永远不执行。
演示环境用 `BYPASS` 直接放行；生产环境应改回默认模式，并接上确认 UI。

**3. `attempt to write a readonly database`**

数据库原先用相对路径 `sqlite3.connect("my_money.db")`，从别的目录启动时落到了别处。
改成 `BASE_DIR = Path(__file__).resolve().parent`、`DB_PATH = BASE_DIR / "my_money.db"`，与启动目录无关。

**4. Gradio 回调里的 `run_until_complete` 与 `nest_asyncio`**

Gradio 本身运行在事件循环中，同步函数里再 `loop.run_until_complete()` 会冲突（`This event loop is already running`）。
改成 `async def gradio_interface(...)` + `await`，并去掉 `nest_asyncio` 依赖。

## 说明

本项目为学习实践，账本数据仅保存在本地 SQLite，不上传任何消费记录。
