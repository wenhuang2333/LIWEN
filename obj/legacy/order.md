# 帽兜工具 CLI 命令参考

## 基本用法

```
# 打包后的 .exe（用户安装后）
ArchiveTool.exe <命令> [参数...]

# 从源码运行
python main.py <命令> [参数...]

# 无参数启动 → 打开 GUI 图形界面
ArchiveTool.exe
```

> **关于 .exe CLI 输出**：打包后的 `ArchiveTool.exe` 使用 `--windowed` 模式构建，运行时不会弹出控制台窗口。
> 当你在 cmd.exe 或 PowerShell 中运行 `ArchiveTool.exe compress ...` 时，程序会自动
> 附加到当前终端并输出结果。你可以像使用系统命令一样正常使用它。

所有命令返回 `0` 表示成功，`1` 表示失败。进度信息输出到 stderr，结果输出到 stdout。

---

## 一、压缩/解压命令

### compress — 压缩文件/目录

```
python main.py compress <路径...> -o <输出路径> [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `paths` | 位置参数 (多个) | 必填 | 要压缩的文件/目录路径 |
| `-o, --output` | string | 必填 | 输出归档路径 |
| `-f, --format` | string | `zip` | 格式: zip, 7z, tar, tar.gz, tar.bz2, tar.xz, zhans |
| `-p, --password` | string | 无 | 归档密码 |
| `-V, --volume-size` | int | 无 | 分卷大小 (字节, 仅 7z) |
| `-l, --level` | int (0-9) | `0` | 压缩级别 (0=存储, 9=最大) |
| `--seed-code` | string | 无 | 自定义种子码 (仅 zhans 格式) |

**示例:**
```bash
# 压缩为 ZIP
python main.py compress docs/ -o docs.zip

# 使用密码压缩为 7z 高压缩
python main.py compress *.txt -o texts.7z -f 7z -p "mypassword" -l 9

# 压缩为 tar.gz
python main.py compress project/ -o project.tar.gz -f tar.gz

# 使用种子码压缩为 .zhans 格式
python main.py compress secret/ -o secret.zhans -f zhans --seed-code "myseed" -l 5
```

### decompress — 解压归档

```
python main.py decompress <归档路径> [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `archive` | 位置参数 | 必填 | 归档文件路径 |
| `-d, --dest` | string | `.` | 解压目标目录 |
| `-p, --password` | string | 无 | 归档密码 |
| `--seed-code` | string | 无 | 种子码 (仅 zhans 格式) |

**示例:**
```bash
python main.py decompress archive.zip -d ./output
python main.py decompress secret.zhans --seed-code "myseed" -d ./decrypted
python main.py decompress encrypted.7z -p "mypassword"
```

### preview — 列出归档内容

```
python main.py preview <归档路径>
```

**示例:**
```bash
python main.py preview archive.zip
```

输出: 归档内所有文件/目录的名称、大小、类型表格。

### extract — 提取单个文件

```
python main.py extract <归档路径> <成员名> [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `archive` | 位置参数 | 必填 | 归档文件路径 |
| `member` | 位置参数 | 必填 | 归档内文件路径/名称 |
| `-d, --dest` | string | `.` | 输出目录 |
| `-p, --password` | string | 无 | 归档密码 |

**示例:**
```bash
python main.py extract archive.zip "readme.txt" -d ./extracted
```

### check — 检测归档信息

```
python main.py check <归档路径>
```

输出: 归档格式名称、文件大小、是否加密。

**示例:**
```bash
python main.py check unknown.bin
```

---

## 二、局域网传输命令

### lan-send — 发送文件

```
python main.py lan-send <文件...> -H <目标IP> [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `files` | 位置参数 (多个) | 必填 | 要发送的文件/目录 |
| `-H, --host` | string | 必填 | 目标主机 IP |
| `-p, --port` | int | `12345` | 目标端口 |
| `-n, --name` | string | `""` | 发送者名称 |
| `--no-compress` | flag | false | 不预先压缩 (发送原始文件) |
| `--no-request` | flag | false | 跳过握手确认 (直接发送) |

**示例:**
```bash
python main.py lan-send file.txt -H 192.168.1.100
python main.py lan-send *.jpg -H 192.168.1.100 -n "我的电脑" --no-compress
```

### lan-receive — 接收文件

```
python main.py lan-receive [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `-p, --port` | int | `12345` | 监听端口 |
| `-d, --dest` | string | `.` | 保存目录 |
| `-y, --yes` | flag | false | 自动接受所有传输 (不询问) |

**示例:**
```bash
python main.py lan-receive -d ./downloads
python main.py lan-receive -p 12346 -y
```

按 `Ctrl+C` 停止监听。

### lan-scan — 扫描设备

```
python main.py lan-scan [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `-t, --timeout` | float | `3.0` | 扫描超时 (秒) |

**示例:**
```bash
python main.py lan-scan
python main.py lan-scan -t 5.0
```

### lan-ping — 链路确认

```
python main.py lan-ping -H <目标IP> [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `-H, --host` | string | 必填 | 目标主机 IP |
| `-p, --port` | int | `12345` | 目标端口 |
| `-t, --timeout` | int | `3` | 超时 (秒) |

**示例:**
```bash
python main.py lan-ping -H 192.168.1.100
```

---

## 三、联机房间命令

### room-scan — 扫描房间

```
python main.py room-scan [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `-t, --timeout` | float | `3.0` | 扫描超时 (秒) |

**示例:**
```bash
python main.py room-scan
```

### room-create — 创建房间

```
python main.py room-create -n <房间名> [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `-n, --name` | string | 必填 | 房间名称 |
| `--password` | string | `""` | 房间密码 (留空无密码) |
| `-p, --port` | int | `12345` | 游戏端口 |
| `--max-players` | int | `8` | 最大玩家数 |

**示例:**
```bash
python main.py room-create -n "我的房间" --password "secret"
python main.py room-create -n "公开对战" -p 12346 --max-players 4
```

按 `Ctrl+C` 停止广播并关闭房间。

### room-join — 加入房间

```
python main.py room-join -H <主机IP> --room-id <房间ID> [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `-H, --host` | string | 必填 | 房间主机 IP |
| `-p, --port` | int | `12345` | 房间端口 |
| `--room-id` | string | 必填 | 房间 ID (从 room-scan 获取) |
| `--password` | string | `""` | 房间密码 |

**示例:**
```bash
python main.py room-join -H 192.168.1.100 --room-id "a1b2c3d4" --password "secret"
```

---

## 四、爬虫命令

### crawl — 运行爬虫

```
python main.py crawl --url <起始URL> [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--url` | string | 必填 | 起始 URL |
| `--fields` | JSON string | 必填* | 字段定义 |
| `--fields-file` | string | 必填* | 从 JSON 文件读取字段定义 |
| `--max-pages` | int | `1` | 最大页数 |
| `--delay` | float | `2.0` | 请求间隔 (秒) |
| `--timeout` | int | `30` | HTTP 超时 (秒) |
| `--retries` | int | `3` | 最大重试次数 |
| `--proxy` | string | 无 | 代理地址 |
| `--next-selector` | string | 无 | 下一页链接 CSS 选择器 |
| `--url-pattern` | string | 无 | URL 翻页模式 `{page}` |
| `--container` | string | 无 | 数据容器 CSS 选择器 |
| `-o, --output` | string | 无 | 输出 JSON 文件路径 |

*: `--fields` 和 `--fields-file` 必须提供其中之一。

**示例:**
```bash
# 提取标题
python main.py crawl --url "https://example.com" \
  --fields '[{"name":"标题","selector":"h1","type":"css"}]'

# 多页翻页
python main.py crawl --url "https://example.com/list" \
  --fields-file fields.json --next-selector "a.next-page" --max-pages 5

# URL 模式翻页
python main.py crawl --url "https://example.com/page/1" \
  --fields '[{"name":"标题","selector":".title","type":"css"}]' \
  --url-pattern "https://example.com/page/{page}" --max-pages 10 \
  -o results.json
```

**字段定义格式 (fields JSON):**
```json
[
  {"name": "字段名", "selector": "CSS选择器或XPath", "type": "css 或 xpath"},
  {"name": "价格", "selector": ".price", "type": "css"}
]
```

### site-check — 检测站点可行性

```
python main.py site-check --url <目标URL>
```

输出: HTTP 状态、SSL 状态、robots.txt 分析、CMS/框架检测、反爬迹象检测。

**示例:**
```bash
python main.py site-check --url "https://example.com"
```

---

## 五、HTTP 请求命令

### http-request — 发送 HTTP 请求

```
python main.py http-request --url <URL> [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `-X, --method` | string | `GET` | HTTP 方法 |
| `--url` | string | 必填 | 请求 URL |
| `--params` | JSON string | 无 | 查询参数 `{"key":"value"}` |
| `--headers` | JSON string | 无 | 请求头 `{"Content-Type":"application/json"}` |
| `--body-type` | string | `none` | body 类型: none, raw, form-data, x-www-form-urlencoded, binary |
| `--body` | string | 无 | 请求体内容 |
| `--body-file` | string | 无 | 从文件读取请求体 |
| `--timeout` | int | `30` | 超时 (秒) |
| `--no-redirect` | flag | false | 禁止重定向 |
| `--no-ssl` | flag | false | 忽略 SSL 证书验证 |
| `--proxy` | string | 无 | 代理地址 |
| `-o, --output` | string | 无 | 响应体写入文件 |
| `--include-headers` | flag | false | 输出中包含响应头 |

**示例:**
```bash
# GET 请求
python main.py http-request --url "https://httpbin.org/get"

# POST JSON
python main.py http-request -X POST --url "https://httpbin.org/post" \
  --headers '{"Content-Type":"application/json"}' \
  --body '{"name":"test","value":123}'

# POST 表单
python main.py http-request -X POST --url "https://httpbin.org/post" \
  --body-type x-www-form-urlencoded \
  --body '[{"key":"username","value":"admin"},{"key":"password","value":"123456"}]'

# 下载文件
python main.py http-request --url "https://example.com/file.pdf" \
  -o downloaded.pdf

# 带认证
python main.py http-request --url "https://api.example.com/data" \
  --headers '{"Authorization":"Bearer token123"}'
```

---

## 六、AI 命令

### ai-chat — AI 对话

```
python main.py ai-chat -m <消息> [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `-m, --message` | string | 必填 | 用户消息内容 |
| `-s, --system` | string | 无 | 系统提示词 |
| `--provider` | string | 默认 | AI 提供商名称 |

**示例:**
```bash
python main.py ai-chat -m "用Python写一个快排算法"
python main.py ai-chat -m "翻译成英文: 你好世界" -s "你是一个翻译助手"
python main.py ai-chat -m "Hello" --provider "DeepSeek"
```

使用非流式调用 (一次返回完整回复)。需要先在 GUI 设置中配置 AI 提供商。

### ai-test-connection — 测试连接

```
python main.py ai-test-connection [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--provider` | string | 默认 | AI 提供商名称 |

**示例:**
```bash
python main.py ai-test-connection
python main.py ai-test-connection --provider "OpenAI"
```

执行 5 步连接测试: URL验证 → DNS解析 → TCP连接 → API请求 → 结果分析。

---

## 七、空间分析命令

### space-scan — 扫描磁盘空间

```
python main.py space-scan <路径> [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `path` | 位置参数 | 必填 | 扫描根路径 |
| `-o, --output` | string | 无 | 输出 JSON 报告文件 |

**示例:**
```bash
python main.py space-scan C:\
python main.py space-scan C:\Users -o report.json
```

扫描结果保存到 SQLite 索引数据库 (`space_index_X.db`)，可供后续查询。

### space-query — 查询空间索引

```
python main.py space-query <路径>
```

**示例:**
```bash
python main.py space-query C:\Users
python main.py space-query C:\Windows\System32
```

查询之前扫描的目录大小和子项。需先运行 `space-scan` 对应的盘符。

---

## 八、诊断命令

### diagnostic-check — 运行诊断

```
python main.py diagnostic-check [选项]
```

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--mode` | string | `all` | 检测模式: lan (局域网), basic (基础), all (全部) |

**示例:**
```bash
python main.py diagnostic-check
python main.py diagnostic-check --mode lan
python main.py diagnostic-check --mode basic
```

局域网模式检测: 网卡接口、子网冲突、端口占用、UDP广播、设备发现、房间扫描、语音模块。

基础模式检测: 压缩模块、配置完整性、空间索引数据库、AI配置、HTTP模块。

---

## 九、设置命令

### settings-get — 读取设置

```
python main.py settings-get <键名>
```

**示例:**
```bash
python main.py settings-get username
python main.py settings-get theme
```

### settings-set — 修改设置

```
python main.py settings-set <键名> <JSON值>
```

**示例:**
```bash
python main.py settings-set username '"我的新名称"'
python main.py settings-set auto_accept true
python main.py settings-set lan_default_port 12346
```

字符串值需要 JSON 引号：`'"hello"'`。布尔/数字直接写：`true`, `false`, `123`。

### settings-list — 列出全部设置

```
python main.py settings-list [--json]
```

**示例:**
```bash
python main.py settings-list
python main.py settings-list --json
```

敏感值 (含 key/password/secret 的键) 自动隐去。

---

## 仅 GUI 功能

以下功能需要图形界面，CLI 暂不支持：

| 功能 | 原因 |
|------|------|
| 语音聊天 (Voice Chat) | 需要实时音频输入输出 |
| VPN 隧道 (VPN Tunneling) | 需要虚拟网卡管理和实时轮询 |
| 可视化爬虫配置 | 需要嵌入式 Chromium 浏览器进行元素选取 |
| 拖放文件操作 | 需要 GUI 拖放交互 |
| 归档预览对话框 | 需要表格 UI 和选择性提取界面 |
| 故障一键修复 | 修复操作可能影响 GUI 状态 |

---

## 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 成功 |
| `1` | 失败 (参数错误、操作失败、连接失败等) |

---

## 日志

所有 CLI 操作的日志写入 `log.txt` (位于安装目录)。每次启动覆盖前次日志。
