import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea,
    QPushButton, QSizePolicy,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap

from utils.paths import static_path

APP_NAME = "帽兜工具"
APP_VERSION = "4.16.1"
APP_AUTHOR = "十山月"
APP_EMAIL = "1275679733@qq.com"
APP_DESCRIPTION = (
    "一款集文件压缩/解压、局域网文件传输、局域网联机大厅、VPN隧道联机、"
    "可视化网页爬虫、HTTP请求发送、AI对话、磁盘空间分析、故障检测与修复"
    "于一体的 Windows 桌面工具。\n\n"
    "支持 ZIP / 7z / TAR / TAR.GZ / TAR.BZ2 / TAR.XZ / SHISHAN / ZHANS 等格式，"
    "提供 0-10 级专有压缩刻度，支持自定义种子码格式。"
)


def _static_path(filename: str) -> str:
    return static_path(filename)


class _ExpandableSection(QWidget):
    def __init__(self, title: str, body: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._btn = QPushButton("▸ " + title)
        self._btn.setStyleSheet(
            "QPushButton { text-align: left; background: transparent; border: none;"
            "  color: #c8ccd4; font-size: 13px; font-weight: 600; padding: 8px 12px;"
            "  border-radius: 4px; }"
            "QPushButton:hover { background-color: #313244; }"
        )
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.clicked.connect(self._toggle)
        layout.addWidget(self._btn)

        self._body = QLabel(body)
        self._body.setWordWrap(True)
        self._body.setStyleSheet(
            "color: #9498a4; font-size: 12px; padding: 4px 12px 8px 24px;"
            " line-height: 1.6;"
        )
        self._body.setVisible(False)
        layout.addWidget(self._body)

    def _toggle(self):
        visible = not self._body.isVisible()
        self._body.setVisible(visible)
        self._btn.setText(("▾ " if visible else "▸ ") + self._btn.text()[2:])


class AboutPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(0)

        title = QLabel("关于本应用")
        title.setObjectName("myCardTitle")
        root.addWidget(title)
        root.addSpacing(16)

        # Scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(16)

        # ── Section 1: App Info Card ──
        card = QWidget()
        card.setObjectName("settingsPanel")
        card.setMaximumWidth(560)
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(10)
        card_layout.setContentsMargins(32, 24, 32, 24)
        card_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        avatar_path = _static_path("head.jpg")
        if os.path.exists(avatar_path):
            avatar = QLabel()
            pix = QPixmap(avatar_path).scaled(80, 80,
                                              Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation)
            avatar.setPixmap(pix)
            avatar.setFixedSize(80, 80)
            avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card_layout.addWidget(avatar, alignment=Qt.AlignmentFlag.AlignCenter)
            card_layout.addSpacing(4)

        name_lbl = QLabel(APP_NAME)
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_lbl.setStyleSheet("font-size: 20px; font-weight: 700; color: #e8e8f0;")
        card_layout.addWidget(name_lbl)

        ver_lbl = QLabel(f"版本 {APP_VERSION}")
        ver_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ver_lbl.setStyleSheet("font-size: 13px; color: #89b4fa;")
        card_layout.addWidget(ver_lbl)

        card_layout.addSpacing(4)

        sep = QLabel()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #313244;")
        card_layout.addWidget(sep)

        card_layout.addSpacing(4)

        info_style = "font-size: 13px; color: #c8ccd4;"
        for label_text, value_text in [("作者", APP_AUTHOR), ("邮箱", APP_EMAIL)]:
            row = QHBoxLayout()
            row.addWidget(QLabel(label_text))
            row.addStretch()
            row.addWidget(QLabel(value_text))
            for w in row.findChildren(QLabel):
                w.setStyleSheet(info_style)
            card_layout.addLayout(row)

        card_layout.addSpacing(4)
        desc = QLabel(APP_DESCRIPTION)
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setStyleSheet("font-size: 12px; color: #9498a4; line-height: 1.5;")
        card_layout.addWidget(desc)

        card_centered = QHBoxLayout()
        card_centered.addStretch()
        card_centered.addWidget(card)
        card_centered.addStretch()
        content_layout.addLayout(card_centered)

        # ── Section 2: Usage Guide ──
        content_layout.addWidget(self._section_title("使用指南"))

        content_layout.addWidget(_ExpandableSection(
            "压缩 / 解压",
            "- 支持格式: ZIP、7z、TAR、TAR.GZ、TAR.BZ2、TAR.XZ、SHISHAN（石山格式）、ZHANS（阵格式）\n"
            "- 操作方式: 拖拽文件/文件夹到窗口，或点击添加文件按钮\n"
            "- 密码保护: ZIP 和 7z 支持 AES 加密，SHISHAN 和 ZHANS 支持 AES-256-GCM 加密\n"
            "- 分卷压缩: 7z 格式支持按大小分卷（如 100MB/卷）\n"
            "- 种子码: 自定义种子码可生成可复现的压缩文件，便于校验和分享\n"
            "- 压缩级别: 0-10 级调整（0=仅存储，10=极限压缩，体积与速度的权衡）\n"
            "- 预览压缩包: 支持不解压直接查看压缩包内容，可选择性提取单个文件\n"
            "- ZHANS 格式特性: 支持内容去重（基于哈希的重复文件跳过）和自适应压缩策略\n"
            "- 右键菜单: 安装后可通过 Windows 右键菜单快速压缩/解压文件"
        ))
        content_layout.addWidget(_ExpandableSection(
            "局域网传输",
            "- 发送模式: 添加文件/文件夹，扫描设备，链路确认后发送\n"
            "- 接收模式: 点击开始监听，等待发送方连接\n"
            "- 设备发现: 使用 UDP 广播自动发现局域网内设备，也可手动扫描\n"
            "- 链路确认 (Ping): 选择设备后点击链路确认，验证 TCP 连接是否通畅\n"
            "- 自动接收: 在「设置」页面开启后，无需手动确认即可接收文件\n"
            "- 传输流程: 发送方自动压缩为临时 ZIP → TCP 传输 → 接收方自动解压\n"
            "- 防火墙: 确保 TCP 端口 12345 和 UDP 端口 12346 在防火墙中开放"
        ))
        content_layout.addWidget(_ExpandableSection(
            "联机大厅",
            "- 创建房间: 设置房间名称、可选密码和虚拟子网，点击创建\n"
            "- 加入房间: 搜索局域网内的房间，选中后点击加入（加密房间需输入密码）\n"
            "- 已保存房间: 创建或加入过的房间会自动保存，下次可快速加入\n"
            "- 局域网直连模式: 基于真实局域网 IP 通信，无需虚拟网卡或管理员权限\n"
            "- VPN 隧道模式: 创建虚拟局域网（10.99.0.0/16），支持跨网段联机，需安装 wintun.dll\n"
            "- 信令服务器: VPN 模式下可启用信令服务器，方便其他玩家通过公网地址连接\n"
            "- 语音通话: 房间内支持多人语音通话（需安装 PyAudio）\n"
            "- 离开房间: 点击离开房间后停止广播，返回大厅\n"
            "- 房间独立: 每个房间使用独立的端口，数据互不干扰"
        ))
        content_layout.addWidget(_ExpandableSection(
            "空间索引",
            "- 磁盘分析: 选择驱动器后扫描全部文件，生成层级化空间占用视图\n"
            "- 可视化图表: 横向柱状图展示占用空间最大的项目\n"
            "- 文件树: 树形列表显示所有文件夹和文件，支持按名称、大小、占比、项目数排序\n"
            "- 快速定位: 可展开任意目录查看子项的空间占用详情\n"
            "- 扫描引擎: 基于 os.scandir + SQLite 存储，支持大规模文件系统的快速索引"
        ))
        content_layout.addWidget(_ExpandableSection(
            "网页爬虫",
            "- 四步操作流程: ①定位列表容器 → ②添加提取字段 → ③设置翻页规则 → ④调整反爬选项\n"
            "- 可视化配置: 内嵌 Chromium 浏览器，可直接浏览目标网页并点选抓取元素\n"
            "- 容器定位: 点击页面中的列表项（如商品卡片），自动生成容器 CSS 选择器\n"
            "- 元素检查器: 鼠标悬停高亮、点击选中元素，自动生成 CSS 选择器和 XPath\n"
            "- 字段提取: 在容器内定义子字段选择器，支持属性提取（如 href、src、data-*）\n"
            "- 实时预览: 即时预览当前页面中提取到的字段值，无需运行爬取\n"
            "- URL 模式翻页: 支持 {page} 占位符自动替换页码（如 /page/{page}）\n"
            "- 链接翻页: 自动识别「下一页」链接并跳转，支持多种翻页按钮样式\n"
            "- 反爬策略: 可配置请求延迟、UA 伪装、代理、自动重试等反反爬措施\n"
            "- 站点检测: 自动检查 robots.txt、JS 依赖、CAPTCHA 检测，评估站点可爬性\n"
            "- 预览模式: 优先使用预览首页验证规则，确认无误后再正式多页爬取\n"
            "- 数据导出: 抓取结果以表格预览，支持导出为 CSV 或 JSON"
        ))
        content_layout.addWidget(_ExpandableSection(
            "HTTP 请求",
            "- 请求方法: 支持 GET / POST / PUT / DELETE / PATCH / HEAD / OPTIONS / TRACE\n"
            "- 参数管理: 查询字符串参数以键值对表格管理\n"
            "- 请求头: 自定义请求头，支持批量粘贴\n"
            "- 请求体: 支持 none / form-data / x-www-form-urlencoded / raw / binary 五种模式\n"
            "- JSON 编辑器: raw 模式下支持 JSON 格式化和语法校验\n"
            "- 响应查看: 显示状态码、响应时间、响应体大小，支持响应头和 Cookie 子页面"
        ))
        content_layout.addWidget(_ExpandableSection(
            "设置",
            "- 用户名: 在局域网传输和联机中显示的名称\n"
            "- 主题色: 可选蓝/紫/绿/橙/红五种配色，即时生效\n"
            "- 默认接收路径: 接收文件时的默认保存目录\n"
            "- 默认密码: 压缩时的预设密码（可在压缩界面覆盖）\n"
            "- 自动接收: 开启后无需手动确认即可接收局域网传输文件\n"
            "- 爬虫默认配置: 请求延迟、超时、最大重试次数、UA 和导出路径\n"
            "- 所有设置自动保存，下次启动时恢复"
        ))
        content_layout.addWidget(_ExpandableSection(
            "故障检测",
            "- 局域网检测: 检查网络接口、端口状态、设备发现、语音功能\n"
            "- 基础检测: 检查压缩模块、配置文件完整性\n"
            "- 全局检查: 串联局域网检测和基础检测，全面诊断\n"
            "- 一键修复: 自动修复检测到的问题\n"
            "- 独立修复工具: 「进行修复」会启动独立的修复程序，可安全修复后自动重启主程序"
        ))

        # ── Section 3: FAQ ──
        content_layout.addSpacing(8)
        content_layout.addWidget(self._section_title("常见问题"))

        content_layout.addWidget(_ExpandableSection(
            "联机功能需要管理员权限吗？",
            "局域网直连模式不需要管理员权限，使用真实局域网 IP 进行通信。\n"
            "VPN 隧道模式需要管理员权限以创建虚拟网卡（TUN 适配器），"
            "程序会自动请求 UAC 提权。如果取消提权，将自动回退到局域网直连模式。"
        ))
        content_layout.addWidget(_ExpandableSection(
            "VPN 隧道模式和直连模式有什么区别？",
            "直连模式: 基于真实局域网 IP，无需管理员权限，适用于同一局域网内的玩家。\n"
            "VPN 隧道模式: 创建虚拟局域网（10.99.0.0/16），通过虚拟网卡和 TUN 隧道通信，"
            "可跨越不同物理网络，支持信令服务器实现公网联机。"
        ))
        content_layout.addWidget(_ExpandableSection(
            "防火墙需要开放哪些端口？",
            "- TCP 12345: 局域网文件传输端口\n"
            "- UDP 12346: 局域网设备发现端口\n"
            "- UDP 12347: 房间发现端口\n"
            "联机大厅的房间端口为动态分配。"
            "建议在 Windows 防火墙「允许应用通过防火墙」中添加本程序的规则。"
        ))
        content_layout.addWidget(_ExpandableSection(
            "为什么其他玩家发现不了我的房间？",
            "1. 确认所有玩家在同一局域网内（连接同一路由器/WiFi）\n"
            "2. 检查防火墙是否阻止了 UDP 端口 12347（房间发现）\n"
            "3. 确认房间创建者已点击创建房间并保持运行\n"
            "4. 尝试关闭 VPN/代理软件，它们可能干扰 UDP 广播\n"
            "5. 检查路由器是否开启了 AP 隔离（AP Isolation），如是则关闭\n"
            "6. 使用「故障检测」页面的局域网检测功能诊断网络状态"
        ))
        content_layout.addWidget(_ExpandableSection(
            "爬虫功能会触发网站的反爬机制吗？",
            "本工具提供了多种反反爬措施：可配置请求延迟、自定义 User-Agent、"
            "代理服务器、自动重试等。使用前建议先通过「站点检测」功能评估目标网站的"
            "可爬性（robots.txt 规则、JS 依赖、CAPTCHA 检测等）。"
            "请遵守目标网站的 robots.txt 和服务条款，合理使用爬虫功能。"
        ))
        content_layout.addWidget(_ExpandableSection(
            "ZHANS 格式和 SHISHAN 格式有什么区别？",
            "ZHANS（阵格式）是 SHISHAN（石山格式）的超集，在 SHISHAN 的基础上新增了:\n"
            "- 内容去重: 基于 XOR 过滤器检测重复文件内容，自动跳过重复项\n"
            "- 自适应压缩: 根据文件类型自动选择最优的 zlib 压缩策略\n"
            "- 去重感知压缩级别: 压缩级别会根据去重结果自动优化\n"
            "SHISHAN 格式目前仅支持解压，新建压缩请使用 ZHANS 格式。"
        ))
        content_layout.addWidget(_ExpandableSection(
            "内嵌浏览器无法加载某些网页？",
            "可能原因及解决方法:\n"
            "1. 目标网站设置了 X-Frame-Options 或 CSP 策略禁止嵌入，这是网站自身的安全限制\n"
            "2. 部分网站依赖 Chrome 特有 API，QtWebEngine 可能不完全兼容\n"
            "3. 检查网络代理是否配置正确（设置页面可配置爬虫代理）\n"
            "4. 内嵌浏览器仅用于「可视化配置」阶段帮助点选元素，实际抓取使用 requests + lxml，"
            "即使内嵌浏览器加载缓慢或不完美，不影响最终抓取结果"
        ))
        content_layout.addWidget(_ExpandableSection(
            "爬虫配置了选择器但抓取不到数据？",
            "1. 是否设置了「列表容器」选择器？— 先点击页面中的列表项设置容器，"
            "再添加子字段（如标题、价格），这是最关键的步骤\n"
            "2. 确认选择器语法正确 — 在「② 字段规则」中使用即时预览验证选择器\n"
            "3. 子字段选择器应相对于容器编写，而非全局选择器 — 容器内应使用简短的类名选择器\n"
            "4. 目标数据可能是 JS 动态渲染的 — requests 只能获取静态 HTML，"
            "可通过浏览器 F12 查看页面源码确认目标数据是否在原始 HTML 中\n"
            "5. 网站可能检测到爬虫并返回了不同的页面结构 — 尝试调整请求延迟或更换 UA\n"
            "6. 使用「预览首页」功能先验证单页提取结果，确认无误后再正式运行\n"
            "7. 分页 URL 参数配置是否正确 — 检查页码参数的递增逻辑和 URL 模式"
        ))
        content_layout.addWidget(_ExpandableSection(
            "爬虫运行一段时间后卡住或报错？",
            "1. 目标网站触发了反爬限流 — 增大请求延迟（爬虫默认 2 秒），降低请求频率\n"
            "2. IP 被暂时封禁 — 配置代理服务器后重试\n"
            "3. 网络不稳定 — 增大超时时间（默认 30 秒）和最大重试次数（默认 3 次）\n"
            "4. 目标网站结构在中途变化 — 检查是第几页开始失败的，排查页面结构差异\n"
            "5. 点击「停止」按钮可随时中断抓取，已抓取的数据不会丢失"
        ))
        content_layout.addWidget(_ExpandableSection(
            "HTTP 请求返回 SSL 证书错误？",
            "1. 目标网站使用自签名证书或证书已过期 — 在请求设置中取消勾选「SSL 验证」\n"
            "2. 系统根证书库不完整 — 更新 Windows 或安装缺失的根证书\n"
            "3. 代理或防火墙在中间篡改了 HTTPS 流量 — 检查网络环境\n"
            "4. 注意: 关闭 SSL 验证会降低安全性，仅在测试环境或信任的网站使用"
        ))
        content_layout.addWidget(_ExpandableSection(
            "HTTP 请求的 POST 请求体乱码或格式错误？",
            "1. 确认 Body 类型选择了正确的模式（form-data / x-www-form-urlencoded / raw / binary）\n"
            "2. 使用 raw 模式发送 JSON 时，先用「格式化」按钮校验 JSON 语法\n"
            "3. 中文等非 ASCII 字符 — 确保在 Headers 中设置了正确的 Content-Type 和 charset\n"
            "4. 文件上传使用 binary 模式，大型文件请合理设置超时时间\n"
            "5. 某些 API 要求特定的 Content-Type（如 application/json），"
            "使用 form-data 模式会导致参数格式不被服务端识别"
        ))
        content_layout.addWidget(_ExpandableSection(
            "空间索引扫描大容量硬盘很慢？",
            "1. 扫描速度取决于磁盘 IO 性能 — HDD 普遍慢于 SSD，这是硬件限制\n"
            "2. 系统盘（C 盘）包含大量小文件和系统目录，扫描耗时更长是正常现象\n"
            "3. 扫描分三个阶段: 文件收集 → 大小汇总 → 数据库写入，进度条反映各阶段进度\n"
            "4. 可以在扫描过程中切换到其他功能使用，扫描在后台线程运行不会阻塞界面\n"
            "5. 扫描结果会缓存到 SQLite 数据库，下次打开同一驱动器时无需重新扫描"
        ))
        from utils.settings import SETTINGS_DIR as _data_dir
        content_layout.addWidget(_ExpandableSection(
            "空间索引中某些目录没有显示或显示不完整？",
            "1. 系统保护目录无权限访问，会自动跳过\n"
            "2. 部分目录需要管理员权限才能读取 — 尝试以管理员身份运行程序\n"
            "3. 符号链接和硬链接不会被重复计算，避免循环引用\n"
            "4. 文件路径超过系统最大长度限制的项会被跳过\n"
            "5. 如果整个驱动器都没有数据，检查杀毒软件是否阻止了磁盘扫描"
        ))
        content_layout.addWidget(_ExpandableSection(
            "空间索引数据库文件可以删除吗？",
            f"索引数据库存储在 {_data_dir} 目录下。\n"
            "可以安全删除 — 删除后下次打开空间索引页面会提示重新扫描。\n"
            "数据库文件大小通常为扫描文件数量的 1-2%，建议定期清理以释放磁盘空间。"
        ))
        content_layout.addWidget(_ExpandableSection(
            "如何完全卸载本应用？",
            f"1. 删除程序安装目录\n"
            f"2. 删除用户数据: {_data_dir} 文件夹\n"
            "3. 如果安装了右键菜单: 以管理员身份运行 python installer/right_menu.py uninstall\n"
            "4. 清理以上几项即可完全卸载"
        ))

        content_layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    def _section_title(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "font-size: 15px; font-weight: 700; color: #e8e8f0;"
            " padding: 4px 0; border-bottom: 1px solid #313244; margin-top: 4px;"
        )
        return lbl
