# Argus Core + iMessage 入口 设计文档

- 日期：2026-10-13
- 状态：待审阅
- 范围：Argus 路线图中的第 1 个子项目

## 1. 背景与目标

Argus 是一个基于 Claude Code 的个人 AI 助手，长期运行在家里的 Mac mini 上，用户通过 iPhone 上的 iMessage 跟它对话。

### 整体路线图

每个子项目独立走一遍"设计 → spec → 实现计划 → 实现"。

| 顺序 | 子项目 | 说明 |
|---|---|---|
| **1** | **Core + iMessage 入口**（本文档） | 地基：收发消息、会话管理、记忆、数据存储 |
| 2 | 每日简报 | 定时汇总并推送 |
| 3 | 学习助手 | 跟踪学习内容与进度 |
| 4 | 健身计划 | 训练计划、训练记录、**饮食记录**、身体数据 |
| 5 | 申请 / 注册 | 浏览器自动化；自主级别可配置（见第 9 节） |

### Core 的目标

1. 用户在 iPhone 上给自己发 iMessage，几秒内收到 Argus 的回复。
2. 同一天内对话有上下文；每天凌晨 4 点自动开新会话。
3. Argus 能读写 Markdown 记忆，能搜索过去的对话，能对结构化数据做只读查询。
4. Mac 重启或程序崩溃后无需人工干预，自动恢复，不漏消息、不重复回复。

### 不在本次范围内

- 简报、学习、健身、申请的具体业务逻辑和数据表
- 向量语义搜索（以后需要时加 `sqlite-vec`，不改架构）
- iMessage 以外的通讯渠道（接口预留，不实现）

## 2. 已确定的决策

| 决策 | 结论 | 理由 |
|---|---|---|
| 运行环境 | 用户现有的 Mac mini（M4，16GB），常年开机 | 待机功耗低；AI 计算在云端，本机负载很小 |
| 通讯渠道 | iMessage，**用户自己给自己发消息** | 不需要新 Apple ID，Mac 上仍能使用用户自己的 iMessage |
| Argus 消息格式 | 同一条消息内以 `🤖 ` 开头，后接正文 | 区分 Argus 与用户；一条消息只产生一个通知 |
| iMessage 接入方式 | 轮询 `~/Library/Messages/chat.db` + AppleScript 发送 | 无第三方依赖；终端已有完全磁盘访问权限 |
| Claude 调用方式 | Claude Agent SDK（Python），`ClaudeSDKClient` | 会话常驻、工具可写成进程内 Python 函数、支持打断 |
| 计费 | 使用用户现有的 Claude 订阅 | 用户选择；与用户自己使用 Claude Code 共享额度 |
| 会话粒度 | 每天一个会话，凌晨 4 点切换 | 一天内连贯；跨天信息靠记忆延续 |
| 存储 | 文字记忆 → Markdown；结构化数据与对话存档 → SQLite | Markdown 便于用户直接查看修改；SQLite 便于统计和按日期查询 |
| 检索 | 结构化数据用 SQL；历史对话用 SQLite FTS5 全文搜索 | 按日期查询需要精确结果，SQL 比向量检索可靠 |

## 3. 架构

```
launchd（开机自启、崩溃重启）
  └─ argus/main.py
        ├─ channel/imessage.py   每秒轮询 chat.db，用 AppleScript 发送
        ├─ brain.py              每天一个 ClaudeSDKClient，消息排队
        │     └─ tools/          进程内 MCP 工具
        ├─ store.py              唯一访问 SQLite 的模块
        └─ scheduler.py          定时任务（本期只有每日切换和备份）
```

### 目录结构

```
Argus/
├── argus/
│   ├── main.py
│   ├── channel/
│   │   ├── base.py        # Channel 接口：receive() / send()
│   │   └── imessage.py
│   ├── brain.py
│   ├── tools/
│   │   ├── memory.py      # 读写 Markdown 记忆
│   │   ├── data.py        # query_data（只读 SQL）
│   │   └── history.py     # search_history（FTS5）
│   ├── store.py
│   └── scheduler.py
├── memory/                # Markdown 记忆
├── data/argus.db          # SQLite
├── backups/               # 每日备份，保留 30 天
├── logs/                  # 按天切分的日志
├── .claude/skills/        # 后续每个模块一个 skill
├── CLAUDE.md              # Argus 的人设与规则
└── setup.sh               # 一次性系统设置
```

### 组件职责

| 组件 | 职责 | 不负责 | 依赖 |
|---|---|---|---|
| `channel` | 收发消息、过滤回声与重复 | 任何 AI 逻辑 | macOS（chat.db、AppleScript） |
| `brain` | 管理每日会话、排队、打断、调用 Claude | iMessage 细节 | Agent SDK、`tools` |
| `tools` | 给 Claude 提供数据能力 | 会话管理 | `store` |
| `store` | SQLite 连接、建表、读写 | 业务逻辑 | 无 |
| `scheduler` | 到点触发任务 | 任务内容 | `brain`、`store` |

`Channel` 是抽象接口，以后加 Telegram 或网页只需新增一个实现，其他模块不改。

## 4. 消息处理流程

1. 用户在 iPhone 上给自己发消息。
2. `channel` 每 1 秒查询 chat.db，只看用户与自己的那个对话，从 `state` 表记录的断点之后读取。过滤掉：
   - `sent_ids` 表中记录过的消息（Argus 自己发的）
   - 以 `🤖` 开头的消息
   - 同一条消息在数据库中的重复记录（自己发给自己时可能同时出现"已发送"和"已接收"两条）
3. `brain` 检查当天会话：
   - 当天会话不存在（过了凌晨 4 点）→ 关闭旧会话，创建新的 `ClaudeSDKClient`，把 `memory/MEMORY.md` 读进上下文。
   - 已存在 → 直接使用。
4. Claude 处理消息，按需调用工具。
5. `channel` 用 AppleScript 发送 `🤖 <回复>`，把消息 ID 写进 `sent_ids`。
6. `store` 把这一轮对话（用户消息 + Argus 回复）写进 `messages`，并同步到 `messages_fts`。

### 边界情况

| 情况 | 行为 |
|---|---|
| 处理中又收到新消息 | 排队，按顺序处理 |
| 收到 `停` | 打断当前任务 |
| 处理超过 15 秒 | 先发送 `🤖 收到，正在处理…` |
| 程序首次启动 | 只处理启动之后的新消息，不回复历史消息 |
| 程序重启 | 从 `state` 表的断点继续 |

## 5. 数据存储

### SQLite 表（Core 只建这些）

| 表 | 字段（概要） | 用途 |
|---|---|---|
| `messages` | id、时间、发送者（user/argus）、内容、session_id | 对话存档 |
| `messages_fts` | FTS5 虚拟表，索引 `messages.内容` | `search_history` |
| `sessions` | id、日期、SDK 会话 ID、开始时间、结束时间 | 每日会话管理 |
| `sent_ids` | iMessage guid、发送时间 | 过滤 Argus 自己的消息 |
| `state` | key、value | 断点（chat.db 已处理到的 ROWID）等 |

后续模块（如健身的饮食表、训练表）各自在自己的子项目中定义。

### Markdown 记忆

格式沿用 Claude Code memory：一个事实一个文件，带 frontmatter；`memory/MEMORY.md` 为索引。

- 每天开新会话时只自动加载 `MEMORY.md`，具体文件按需读取。
- 用户可直接查看、修改、删除。

### 工具

| 工具 | 作用 | 安全限制 |
|---|---|---|
| `memory_read` / `memory_write` / `memory_delete` | 读写 `memory/` 下的文件 | 只能操作 `memory/` 目录 |
| `query_data` | 执行 SQL 查询 | 以**只读模式**打开数据库，只能 `SELECT` |
| `search_history` | FTS5 搜索过去的对话，可按日期范围过滤 | 只读 |

写入结构化数据只能通过各模块的专用工具（如后续的 `log_meal`），每个工具只写自己的表。

### 备份

每天凌晨 4 点切换会话时，把 `data/` 和 `memory/` 复制到 `backups/<日期>/`，保留最近 30 天。

## 6. 出错处理

| 情况 | 处理 |
|---|---|
| Claude 调用失败 / 订阅额度用完 | 回复 `🤖 出错了：<原因>`；消息标记为未处理，用户说"重试"时重新处理 |
| iMessage 发送失败 | 重试 3 次，仍失败则写日志 |
| 程序崩溃 | launchd 自动重启，从断点继续 |
| Mac 重启 / 停电恢复 | 自动开机 + 自动登录 + launchd 开机自启 |
| chat.db 无法读取 | 写日志，并发 macOS 系统通知提醒用户 |

## 7. 运行与部署

- **launchd**：开机自启、崩溃重启。
- **日志**：`logs/argus.log`，按天切分。
- **`setup.sh`** 负责一次性设置，需要用户手动操作的步骤给出明确提示：
  - 完全磁盘访问权限（读 chat.db）
  - 自动化权限（AppleScript 控制"信息" App）
  - 关闭自动睡眠（当前设置为闲置 1 分钟睡眠）
  - 打开停电后自动开机（当前 `autorestart` 为 0）
  - 开机自动登录 macOS 用户
  - 安装并加载 launchd 配置

## 8. 测试

### 第一步：Spike（写正式代码之前）

用户给自己发几条测试消息，检查真实 chat.db 中自发自收消息的记录方式，确认第 4 节的去重方案可靠。

**如果不可靠**：回退为"'信息' App 登录单独的 Apple ID"的方案（用户的 iPhone 给该 Apple ID 发消息）。

### 单元测试

- `channel`：用伪造的 chat.db 测试过滤逻辑，重点覆盖重复记录和 `🤖` 回声。
- `tools`：用临时数据库测试只读限制、FTS 搜索、记忆读写的目录限制。
- `brain`：用伪造的 Claude 客户端测试每日切换（含 4 点边界）、排队、`停` 打断、超时提示。
- `store`：建表、断点读写。

### 端到端验收

1. 在 iPhone 上发 `你好`，几秒内收到 `🤖` 开头的回复。
2. 同一天内追问能引用上文。
3. 让 Argus 记住一件事，确认 `memory/` 下生成了对应文件。
4. 问"我之前说过什么关于 X 的话"，能搜到历史对话。
5. 发一条需要较长时间的任务后发 `停`，任务被打断。
6. 重启 Mac，不做任何操作，Argus 恢复工作且没有重复回复。

## 9. 后续子项目的已知需求（记录备用，不在本期实现）

- **申请 / 注册**：必须具备**全自动**能力（填表、提交、通过 Gmail API 读取邮件验证码/验证链接）；同时支持按任务或按网站设置为"提交前确认"。遇到图形验证码等无法自动完成的步骤时，截图推送给用户处理后继续。不设计绕过验证码的方案。账号密码存放在 macOS 钥匙串或密码管理器 CLI 中，不明文写入配置。短信验证需要另行接入短信转发。
- **健身计划**：包括饮食记录；数据写入 SQLite，供"上周吃得/练得怎么样"这类按日期的查询使用。
- **订阅额度**：Argus 与用户自己使用 Claude Code 共享订阅额度；如果不够用，再考虑接入 API key（Agent SDK 支持，只需改认证方式）。
