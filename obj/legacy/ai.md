# 生成式AI 技术架构分析

## 一、整体架构概览

本项目在 PyQt6 桌面应用中内嵌了一个完整的多模型 AI 聊天系统，位于"互联网"分类的"生成式AI"子标签页下。整个 AI 模块由三个核心层次组成：

```
ui/ai_page.py               ── GUI 聊天界面 + 配置对话框 + 模型选择器
core/ai_client.py             ── AI 客户端（HTTP 请求 + SSE 流式解析）
core/ai_connection_test.py    ── AI 连接测试（纯逻辑，无 Qt 依赖）
core/workers.py               ── QThread 工作线程（AiChatWorker, AiConnectionTestWorker）
utils/settings.py             ── 多提供商配置持久化（JSON 文件）
cli/commands/ai_cmd.py        ── CLI 命令行接口
```

支持的 AI 服务商预设：
- **OpenAI** (GPT-4o)
- **DeepSeek** (deepseek-v4-pro)
- **Kimi / 月之暗面** (kimi-k2-0711-preview)
- **MiniMax / 稀宇** (MiniMax-M2.7)
- **通义千问 / 阿里** (qwen-plus)
- **讯飞星火** (generalv3)
- **Ollama** (本地部署，llama3)
- **Groq** (llama-3.1-70b-versatile)
- **Together AI** (Llama-3.3-70B-Instruct-Turbo)
- **自定义** (任意 OpenAI 兼容 API)

---

## 二、全流程详解：从 API 获取到最终输出

### 步骤 1：API 配置获取

**数据流向：** 用户配置 → `Settings` 持久化 → `AiPage` 读取 → `AiClient` 消费

```python
# ui/ai_page.py:_send_message()
api_url, api_key, model, temperature, max_tokens, system_prompt = \
    settings.get_active_ai_config()
```

`settings.get_active_ai_config()` 在 `utils/settings.py:257-267` 定义，返回 6 元组。

**配置持久化结构**（`%APPDATA%/ArchiveTool/settings.json`）：
```json
{
  "ai_active_provider": "DeepSeek",
  "ai_providers": {
    "DeepSeek": {
      "api_url": "https://api.deepseek.com/v1/chat/completions",
      "api_key": "sk-...",
      "model": "deepseek-v4-pro",
      "temperature": 1.0,
      "max_tokens": 4096,
      "system_prompt": ""
    },
    "自定义": { ... }
  },
  "ai_custom_models": {
    "my-custom-model": {
      "api_url": "https://...",
      "api_key": "...",
      "model": "...",
      "temperature": 1.0,
      "max_tokens": 4096,
      "system_prompt": ""
    }
  }
}
```

每个提供商独立保存完整配置（URL、Key、Model、Temperature、MaxTokens、SystemPrompt）。`ai_active_provider` 记录当前激活的提供商名称。

### 步骤 2：消息构建

**核心函数：** `core/ai_client.py:build_messages(system_prompt, history)`

```python
def build_messages(system_prompt, history):
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    return messages
```

消息数组遵循 OpenAI Chat Completions 标准格式：
- `{"role": "system", "content": "..."}` — 系统提示词（可选，来自配置）
- `{"role": "user", "content": "..."}` — 用户历史消息
- `{"role": "assistant", "content": "..."}` — AI 历史回复

对话历史保存在 `AiPage._messages` 列表中，每轮对话后追加 `assistant` 回复。

### 步骤 3：请求发送 — OpenAI vs Anthropic 双协议兼容

**核心类：** `core/ai_client.py:AiClient`

**构造函数：**
```python
def __init__(self, base_url, api_key, model, temperature=1.0, max_tokens=4096):
```

`AiClient` 不预先假定协议格式，而是通过两个关键机制实现兼容：

#### 3a. 协议检测

**URL 自动检测** (`_is_anthropic_url()`):
```python
def _is_anthropic_url(self):
    url_lower = self.base_url.lower()
    return "/anthropic" in url_lower or "/messages" in url_lower
```

URL 中包含 `/anthropic` 或 `/messages` 时判定为 Anthropic 协议，否则为 OpenAI 协议。

#### 3b. OpenAI 格式请求体 (`_build_openai_payload`)

```python
payload = {
    "model": self.model,
    "messages": messages,       # 含 system + user + assistant 完整历史
    "stream": True,             # 强制流式输出
    "temperature": self.temperature,
    "max_tokens": self.max_tokens,
}
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {self.api_key}",
}
```

#### 3c. Anthropic 格式请求体 (`_build_anthropic_payload`)

```python
# 从 messages 中分离 system 和对话消息
system_prompts = [m["content"] for m in messages if m["role"] == "system"]
chat_messages = [m for m in messages if m["role"] != "system"]

payload = {
    "model": self.model,
    "messages": chat_messages,  # 不含 system 角色
    "stream": True,
    "temperature": self.temperature,
    "max_tokens": self.max_tokens,
}
if system_prompts:
    payload["system"] = system_prompts[0]  # Anthropic 的 system 是顶层字段

headers = {
    "Content-Type": "application/json",
    "x-api-key": self.api_key,           # 注意：x-api-key 而非 Bearer
    "anthropic-version": "2023-06-01",
}
```

关键差异：
- **System Prompt 位置**：OpenAI 放在 messages 数组内作为 `system` 角色；Anthropic 放在 JSON 顶层的 `system` 字段
- **认证头**：OpenAI 用 `Authorization: Bearer <key>`；Anthropic 用 `x-api-key: <key>` + `anthropic-version` 头
- **Stream 实现**：两者都用 SSE，但事件格式完全不同

#### 3d. HTTP 请求发起

```python
# core/ai_client.py:chat_stream() 129-130 行
response = requests.post(url, headers=headers, json=payload,
                         stream=True, timeout=(30, 120))
```

使用 Python `requests` 库发起 POST 请求：
- `stream=True` — 启用流式响应，不一次性加载全部 body
- `timeout=(30, 120)` — 连接超时 30s，读取超时 120s
- 请求运行在 `AiChatWorker`（QThread）中，不阻塞 GUI 主线程

### 步骤 4：SSE 流式响应解析

这是整个流程中最复杂的部分。两种协议的 SSE 格式完全不同，`chat_stream` 实现了统一的自动检测和解析。

#### 4a. OpenAI SSE 格式

```
data: {"id":"...","object":"chat.completion.chunk","choices":[{"delta":{"content":"Hello"},"index":0}]}

data: {"id":"...","choices":[{"delta":{"content":" world"},"index":0}]}

data: [DONE]
```

解析逻辑：
```python
# 自动检测：data JSON 中包含 "choices" 字段 → OpenAI 格式
if "choices" in data:
    detected_format = "openai"

# 提取文本：choices[0].delta.content
delta = data.get("choices", [{}])[0].get("delta", {})
content = delta.get("content", "")
```

#### 4b. Anthropic SSE 格式

```
event: message_start
data: {"type":"message_start","message":{"id":"...","model":"..."}}

event: content_block_start
data: {"type":"content_block_start","content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}

event: content_block_delta
data: {"type":"content_block_delta","delta":{"type":"text_delta","text":" world"}}

event: message_stop
data: {"type":"message_stop"}
```

解析逻辑：
```python
# Anthropic SSE 每个 data 行前有一个 "event: <type>" 行
if line.startswith("event: "):
    current_event = line[7:].strip()   # 记录事件类型
    continue

# 自动检测：data JSON 中包含 "type" 字段 → Anthropic 格式
if "type" in data:
    detected_format = "anthropic"

# 提取文本（两种来源）：
# 1. content_block_delta → delta.text
if data.get("type") == "content_block_delta":
    delta = data.get("delta", {})
    content = delta.get("text", "")

# 2. content_block_start → content_block.text（首 token 可能在此）
elif data.get("type") == "content_block_start":
    cb = data.get("content_block", {})
    content = cb.get("text", "")
```

#### 4c. 统一迭代器

```python
for line in response.iter_lines(decode_unicode=True):
    # 逐行读取 SSE 流
    ...
    if content:
        token_count += 1
        yield content    # 每个 token 逐块 yield 给调用者
```

`chat_stream(messages)` 是一个 Python 生成器（generator），每次 `yield` 一个文本片段。调用方在 `AiChatWorker.run()` 中遍历：

```python
for token in self.client.chat_stream(self.messages):
    if self._cancel:
        break
    self.token.emit(token)   # 通过 Qt 信号发送到 GUI 主线程
```

#### 4d. 性能监控

请求过程中记录以下指标并通过 `[AI DEBUG]` 前缀打印到控制台：
- HTTP 连接耗时（`t1 - t0`）
- 首个 token 到达时间 TTF（`first_token_time - t1`）
- 总 token 数量和总耗时

### 步骤 5：流式输出到 GUI

**信号链路：**

```
AiChatWorker.token (pyqtSignal, QThread)
    → AiPage._on_token(token)  (GUI 主线程)
        → 追加 token 文本 → 更新界面 QLabel
```

**首次 token 到达**：
1. 隐藏"正在思考..."动画气泡
2. 从 bubble pool 中取出一个 AI 风格气泡
3. 设置初始文本内容

**后续 token 到达**：
1. 累积 `_current_ai_text += token`
2. 调用 `_update_bubble_text()` 替换 QLabel 的富文本内容
3. 自动滚动到底部

**流式完成**（`_on_finished`）：
1. 最终化气泡（显示操作按钮：复制、引用）
2. 将完整回复追加到 `_messages` 历史
3. 持久化对话到文件
4. 更新侧边栏时间戳

**流式中断**：
- 用户点击"停止"→ `_stop_generation()` → `worker.cancel()` 设置取消标记
- 保留下载的部分内容并追加到消息历史

### 步骤 6：富文本渲染与 Markdown 支持

**核心函数：** `ui/ai_page.py:_render_content(text, is_user)`

支持以下格式：
- **代码块** `` ```...``` `` → 深色背景等宽字体代码区（`#11111b` 背景 + `#a6e3a1` 绿色文字）
- **行内代码** `` `...` `` → `#fab387` 橙色行内代码
- **粗体** `**...**` → `<b>...</b>`
- **斜体** `*...*` → `<i>...</i>`
- HTML 转义：`&` `&amp;`，`<` `&lt;`，`>` `&gt;`
- 换行转换为 `<br>`

用户气泡：蓝色背景 `#89b4fa` + 深色文字 + 右对齐
AI 气泡：灰色背景 `#313244` + 浅色文字 + 左对齐 + 头像

---

## 三、连接测试流程

### 测试入口

两个入口均调用同一核心逻辑 (`core/ai_connection_test.py`)：

1. **UiConfigDialog 中的"测试连接"按钮**：
   ```
   _test_connection() → AiConnectionTestDialog(config) → AiConnectionTestWorker
   ```
   在配置对话框中原地测试，不保存。成功后可点"应用"保存。

2. **"应用"按钮强制测试**：
   ```
   _apply() → AiConnectionTestDialog(config) → 成功则 accept() + 保存
   ```
   保存前必须通过测试，失败则停留在配置对话框供修正。

### 测试步骤 (`test_ai_connection`)

```
1. URL 验证 ── 检查是否以 http:// 或 https:// 开头
2. DNS 解析 ── socket.getaddrinfo() 解析域名
3. TCP 连接 ── socket.connect() 测试 8 秒超时
4. API 请求 ── POST 发送最小化请求 {"max_tokens": 1, "messages": [{"role":"user","content":"Hi"}]}
```

每步都有独立的错误消息：
- `401` → "API Key 无效"
- `403` → "访问被拒绝（权限或余额）"
- `404` → "API 端点不存在"
- `429` → "请求过于频繁"
- `SSL 错误` → "证书验证失败"
- `连接超时` → "防火墙或代理问题"

### CLI 测试

```bash
python main.py ai-test-connection --provider DeepSeek
```

---

## 四、自定义配置的页面设计与管理

### 配置层次结构

```
全局设置 (settings.json)
├── ai_active_provider: str     ← 当前激活的提供商名
├── ai_providers: dict           ← 每个提供商的完整配置
│   ├── "OpenAI": {api_url, api_key, model, temperature, max_tokens, system_prompt}
│   ├── "DeepSeek": {...}
│   └── ...
└── ai_custom_models: dict       ← 额外的自定义模型配置
    ├── "my-model-1": {api_url, api_key, model, temperature, max_tokens, system_prompt}
    └── "my-model-2": {...}
```

### 配置入口一览

| 入口 | 位置 | 功能 |
|------|------|------|
| **AiConfigDialog** | AI 聊天页 → "选择模型" → "配置 API" | 完整的提供商配置（URL/Key/Model/温度/Token/System Prompt） |
| **ModelSelectorMenu** | AI 聊天页 → "选择模型"按钮 | 快速切换模型 + 查看配置状态 |
| **CustomModelDialog** | ModelSelectorMenu → "管理自定义模型" | 自定义模型的 CRUD 管理 |
| **MyPage AI 区块** | 设置页面 → "生成式AI" | 在设置页中直接配置当前提供商的参数 |

### 配置对话框 (AiConfigDialog)

**文件：** `ui/ai_page.py:31-228`

**UI 布局：**
```
┌──────────────────────────────────────┐
│  API 配置                             │
│  请填写完整的 API 端点地址...          │
│                                      │
│  服务商预设  [DeepSeek        ▼]     │
│  API 地址    [https://api.deep...   ] │
│  API Key     [●●●●●●●●●●●●●●●●●]   │
│  模型名称    [deepseek-v4-pro       ] │
│  Temperature [1.0]  Max Tokens [4096] │
│  System Prompt                        │
│  ┌────────────────────────────────┐   │
│  │ (可选)                         │   │
│  └────────────────────────────────┘   │
│                                      │
│        [取消]  [测试连接]  [应用]      │
└──────────────────────────────────────┘
```

**交互细节：**
- 切换"服务商预设"下拉框时，自动填入对应的 API 地址和默认模型
- 每个提供商的配置独立保存，切换时自动加载已有配置
- "测试连接"先测试不保存，"应用"强制测试通过后才保存
- API Key 字段使用 `EchoMode.Password` 掩码显示
- Temperature 范围 0.0–2.0，步长 0.1
- Max Tokens 范围 1–131072，步长 256

### 模型选择器菜单 (ModelSelectorMenu)

**文件：** `ui/ai_page.py:387-541`

**菜单结构：**
```
┌──────────────────────────────────────┐
│  选择模型                    (粗体)    │
│ ──────────────────────────────────── │
│  ● OpenAI  —  gpt-4o [已配置]        │
│     DeepSeek  —  deepseek-v4-pro     │
│     Kimi (月之暗面)  —  kimi-k2...   │
│     MiniMax (稀宇)  —  MiniMax-M2.7 │
│     通义千问 (阿里)  —  qwen-plus    │
│     讯飞星火  —  generalv3           │
│     Ollama  —  llama3                │
│     Groq  —  llama-3.1-70b-versatile│
│     Together AI  —  Llama-3.3-70B... │
│ ──────────────────────────────────── │
│  自定义模型                            │
│  ✎  my-custom  —  https://... [已配置] │
│  ⚙ 管理自定义模型…                     │
│ ──────────────────────────────────── │
│  ⚙ 配置 API...                        │
│  🗑 新建对话                           │
└──────────────────────────────────────┘
```

**关键设计：**
- `●` 标记当前激活的提供商
- `[已配置]` 标记已填写 API Key 的提供商
- 点击任意预设提供商 → 打开该提供商的配置对话框
- 点击自定义模型 → 直接激活（加载完整配置）
- 动态从 `settings.json` 读取，刷新时重建菜单项

### 自定义模型管理 (CustomModelDialog)

**文件：** `ui/ai_page.py:548-831`

支持操作：
- **添加模型** → 打开 AiConfigDialog 填写完整配置 → 保存到 `ai_custom_models` dict
- **编辑配置** → 加载已有配置到 AiConfigDialog 修改 → 覆盖保存
- **删除模型** → 确认后从 `ai_custom_models` 中移除
- **使用所选模型** → 将完整配置加载到"自定义"提供商并激活

列表项格式：`● my-model  —  https://api.example.com/... [已配置]`
- 提供详细的 Tooltip（模型/地址/温度/MaxToken/密钥状态）
- 选中后底部显示详情预览

### 设置页 AI 区块 (MyPage)

**文件：** `ui/my_page.py:227-322`

在"设置"页面的"生成式AI"分区中提供相同的配置能力：
- 服务商预设下拉框
- API 端点输入
- API Key 输入（密码模式）
- 模型名称输入
- Temperature / Max Tokens 数值调节
- System Prompt 文本编辑

所有修改通过 `_auto_save()` 方法即时持久化到 `settings.json`，无需手动保存按钮。

### 对话管理

**存储位置：** `%APPDATA%/ArchiveTool/ai_conversations/`

```
ai_conversations/
├── index.json                 ← 对话索引 [{id, title, created_at, updated_at}, ...]
├── a1b2c3d4e5f6.json         ← 单次对话完整消息历史
└── ...
```

**索引文件 (index.json):**
```json
{
  "conversations": [
    {
      "id": "a1b2c3d4e5f6",
      "title": "Python 代码优化",
      "created_at": "2026-05-25T10:30:00",
      "updated_at": "2026-05-25T11:45:00"
    }
  ]
}
```

**对话文件 (a1b2c3d4e5f6.json):**
```json
{
  "id": "a1b2c3d4e5f6",
  "title": "Python 代码优化",
  "messages": [
    {"role": "user", "content": "如何优化这段代码？"},
    {"role": "assistant", "content": "可以使用以下方法..."},
    {"role": "user", "content": "能再详细一点吗？"},
    {"role": "assistant", "content": "当然..."}
  ],
  "created_at": "2026-05-25T10:30:00",
  "updated_at": "2026-05-25T11:45:00"
}
```

**对话生命周期：**
1. **新建** → 初始 `_current_conv_id = None`，消息仅存内存
2. **首次发送消息** → `_create_and_save_conversation()` 生成 UUID，写入 `index.json` + 对话文件
3. **每次 AI 回复完成** → `_save_current_conversation()` 更新对话文件 + 索引时间戳
4. **切换对话** → 保存当前对话 → `_release_all_bubbles()` 释放 UI 组件 → 从文件加载新对话 → 复用 bubble pool 渲染
5. **删除** → 从索引移除 → 删除对话文件 → 如当前对话则重置为新建状态

### Bubble Pool 复用机制

**文件：** `ui/ai_page.py:1598-1784`

为优化大量消息的显示性能，气泡组件采用池化复用：

```
_pool_user: list[QFrame]    ← 闲置用户气泡（左对齐蓝底）
_pool_ai: list[QFrame]      ← 闲置 AI 气泡（右对齐灰底）
_active_bubbles: list        ← 当前显示的气泡列表
```

- `_acquire_bubble(role)` → 从池中取出或新建
- `_release_all_bubbles()` → 隐藏所有气泡，归还池中，不销毁
- 切换对话时完全复用，避免频繁创建/销毁 QWidget

---

## 五、CLI 命令行支持

**文件：** `cli/commands/ai_cmd.py`

```bash
# 发送消息（非流式）
python main.py ai-chat -m "你好，解释一下 Python 装饰器"

# 指定提供商
python main.py ai-chat --provider DeepSeek -m "写一个快速排序" -s "你是Python专家"

# 连接测试
python main.py ai-test-connection
python main.py ai-test-connection --provider OpenAI
```

CLI 模式使用 `AiClient.chat()` 非流式调用（`stream=False`），适合脚本和自动化场景。

---

## 六、版本迁移机制

`utils/settings.py:_migrate_ai_settings()` (154–230 行) 自动处理配置版本升级：

1. **检测旧格式** → 检查是否存在 `ai_api_url`、`ai_api_key` 等扁平 key
2. **URL 迁移** → `core/ai_client.py:migrate_api_url()` 将旧域名和缺少 `/chat/completions` 的 URL 自动更新
3. **提供商匹配** → 根据旧 URL 匹配预设提供商名称
4. **结构升级** → 转换为 `ai_providers` + `ai_active_provider` + `ai_custom_models` 新格式
5. **自定义模型迁移** → `list[str]` → `dict[str, dict]` 结构升级

---

## 七、平台兼容性

- AI 模块纯 Python 实现，依赖 `requests` + `PyQt6`，跨平台（Windows/macOS/Linux）通用
- 配置路径通过 `_get_app_data_dir()` 适配各平台
- CLI 模式无 Qt 依赖，可在无 GUI 的服务器环境运行
- 本地 Ollama 预设支持离线/私有化部署场景
