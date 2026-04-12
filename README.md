# 训记.skills

一个小而专注的 训记.skills 仓库：
它能帮 Agent **读取训练**、**写入训练**、**串联训练工作流**，但不会直接替代训记 App 内部的计划系统。

面向 AI Agent 的训记（Xunji）Open API 技能仓库，用于**读取训练记录**、**分析历史训练**、以及**写入/更新训练记录**。

这个仓库基于训记 App 新开放的 Open API 开发，适合接入 OpenCode、Claude Code、Codex 或其他可调用 Python / shell 的 Agent 工作流。

> [!IMPORTANT]
> 这个仓库**不能直接创建训记 App 内部的计划对象**。
> 它能做的是写入新的训练记录或更新已有记录；如果你想在 App 里形成正式计划，通常需要先写入训练内容，再回到训记 App 内确认、整理或自行生成计划。

## 可用技能

### `xunji-reader`

读取训记训练数据。

- 调用 `POST /api_trains_for_llm`
- 按日期读取训练记录
- 本地缓存训练历史，减少重复请求
- 维护动作库 `action-library.json`，供后续写入流程复用

适合用于：训练复盘、历史分析、动作名称对齐、生成训练摘要。

### `xunji-writer`

写回训记训练数据。

- 调用 `POST /api_upsert_trains_for_llm`
- 支持更新已有记录
- 支持新建训练记录和休息日
- 写入前做结构校验、动作名对齐与规范化
- 支持 `--dry-run` 先验证再写入

适合用于：把 Agent 生成的结构化训练内容安全写回训记。

## 项目定位

- **适合 Agent 集成**：核心能力放在独立 Python 脚本里，不绑定单一宿主
- **支持读取与写入**：既能读训练历史，也能把规范化结果写回 App
- **写入更安全**：有动作库对齐、结构校验、`--dry-run` 等保护机制
- **内置本地缓存**：适合反复分析同一批训练数据
- **功能边界清晰**：仓库只聚焦两个技能，不试图做完整 SDK

## 仓库结构

```text
xunji/
├── xunji-reader/
│   ├── SKILL.md
│   └── scripts/fetch_xunji_trains.py
└── xunji-writer/
    ├── SKILL.md
    └── scripts/upsert_xunji_trains.py
```

## 快速开始

### 1. 唯一必要配置：设置 API Key

```bash
export XUNJI_API_KEY="your-api-key"
```

配置好 `XUNJI_API_KEY` 之后，就可以把这两个 skill 接入 Agent 工作流。

### 2. 推荐方式：以 Agent 工作流为主

#### 训练分析工作流

读取 Agent 读取最近几天训练 → 分析 Agent 总结训练量与动作分布 → 输出复盘结论。

#### 训练草稿写回工作流

规划 Agent 生成下一次训练草稿 → 格式化 Agent 转成结构化训练行 → 写入 Agent 用 `--dry-run` 校验 → 写回训记。

#### App 内继续计划流程

先通过 writer 写入新的训练内容 → 回到训记 App 中确认、另存为模板 → 继续形成正式计划。

### 3. 可选方式：手动调用脚本

如果你不是通过 Agent 调用，也可以直接手动执行脚本。

#### 读取训练数据

```bash
python3 xunji-reader/scripts/fetch_xunji_trains.py --date 2026-04-02
```

只输出训练行：

```bash
python3 xunji-reader/scripts/fetch_xunji_trains.py --date 2026-04-02 --format lines
```

强制刷新缓存：

```bash
python3 xunji-reader/scripts/fetch_xunji_trains.py --date 2026-04-02 --refresh
```

#### 写入前先校验

```bash
python3 xunji-writer/scripts/upsert_xunji_trains.py \
  --date 2026-04-02 \
  --res-file /tmp/xunji-res.json \
  --dry-run
```

#### 写入新的训练记录

```bash
python3 xunji-writer/scripts/upsert_xunji_trains.py \
  --date 2026-04-02 \
  --allow-new-records \
  --line '2026-04-02,胸部训练,状态不错,1.卧推,1组,60kg,10次,2组,60kg,8次'
```

## 使用范围

这个仓库更适合作为：

- AI Agent 与训记训练数据之间的桥梁
- 训练记录读写自动化工具
- 多 Agent 训练工作流里的数据接入层

它**不**是：

- 完整的训记官方 SDK
- 完整计划管理 API
- 对训记 App 内计划系统的一比一替代

## 优点

- 可以直接接入多种 Agent 框架
- 读写边界清晰，适合自动化拆分
- 有本地缓存，重复分析成本低
- 有动作库和结构校验，降低写错风险
- Python 脚本可单独运行，也可作为 skill 使用

## 局限性

- **不能直接创建训记 App 内部计划对象**
- 新建记录后，read-after-write 不能总是作为可靠确认方式
- 写入流程依赖动作库对齐，动作名不明确时需要人工确认
- 类似 `10x3` 这种歧义表达不会被自动猜测


## 安全说明

- 使用 `XUNJI_API_KEY` 环境变量，不要把密钥写进仓库
- 本地缓存会保存训练数据，注意设备本身的访问控制
- 真实写入前建议先用 `--dry-run` 做校验
