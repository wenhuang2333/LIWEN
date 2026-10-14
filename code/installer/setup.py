"""
帽兜工具 — 安装程序
轻量级安装向导，将应用文件解压到用户指定目录并创建桌面快捷方式。
"""
import os
import sys
import shutil
import tarfile
import lzma
import threading

from utils.helpers import open_file_or_dir, spawn_detached

from installer.shortcut import (
    create_shortcut, get_desktop_path, get_start_menu_programs_path,
)

from PyQt6.QtWidgets import (
    QApplication, QWizard, QWizardPage, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog, QProgressBar,
    QCheckBox, QMessageBox,
)
from PyQt6.QtCore import Qt, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QPixmap


# 应用标识统一来自 utils/constants.py，避免各处写死版本号后不同步
_CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)
from utils.constants import (  # noqa: E402
    APP_NAME_ZH as APP_NAME,
    APP_NAME_EN,
    APP_VERSION,
    APP_AUTHOR as APP_PUBLISHER,
    APP_EMAIL,
    APP_NAME_SLUG,
    LEGACY_APP_SLUG,
)
DEFAULT_INSTALL_DIR = os.path.join(
    os.environ.get("ProgramFiles", "C:\\Program Files"), APP_NAME)

# Detect if running from PyInstaller bundle
if getattr(sys, "frozen", False):
    BUNDLE_DIR = sys._MEIPASS
else:
    # installer/ 位于 code/ 下一级 → 上溯两级即项目根目录
    BUNDLE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ARCHIVE_TAR_XZ = os.path.join(BUNDLE_DIR, f"{APP_NAME_SLUG}.tar.xz")

# 项目根目录（仅源码运行时的 dist/ 回退查找使用）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Dark theme QSS (compact, matching main app) ──
SETUP_STYLE = """
QWidget {
    background-color: #1e1e2e;
    color: #e8e8f0;
    font-size: 13px;
}
QWizard {
    background-color: #1e1e2e;
}
QWizard QLabel {
    color: #e8e8f0;
}
QLineEdit {
    background-color: #252536;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 8px 12px;
    color: #e8e8f0;
}
QPushButton {
    background-color: #89b4fa;
    color: #1e1e2e;
    border: none;
    border-radius: 6px;
    padding: 8px 20px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #b4d0fb;
}
QPushButton:disabled {
    background-color: #45475a;
    color: #9498a4;
}
QPushButton#secondaryBtn {
    background-color: #45475a;
    color: #e8e8f0;
}
QPushButton#secondaryBtn:hover {
    background-color: #585b70;
}
QProgressBar {
    background-color: #252536;
    border: 1px solid #45475a;
    border-radius: 4px;
    text-align: center;
    color: #e8e8f0;
}
QProgressBar::chunk {
    background-color: #89b4fa;
    border-radius: 3px;
}
QCheckBox {
    color: #c8ccd4;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 2px solid #45475a;
    background-color: #252536;
}
QCheckBox::indicator:checked {
    background-color: #89b4fa;
    border-color: #89b4fa;
}
"""


class _Signals(QObject):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    finished = pyqtSignal(bool, str)


class InstallThread(threading.Thread):
    """Background thread that extracts files and creates shortcuts."""

    def __init__(self, install_dir: str, create_desktop: bool,
                 create_start_menu: bool):
        super().__init__(daemon=True)
        self._install_dir = install_dir
        self._create_desktop = create_desktop
        self._create_start_menu = create_start_menu
        self.signals = _Signals()

    def run(self):
        try:
            # Step 1: Validate archive
            self.signals.status.emit("正在验证安装包...")
            self.signals.progress.emit(5)
            if not os.path.exists(ARCHIVE_TAR_XZ):
                self.signals.finished.emit(False,
                    f"找不到安装包数据: {ARCHIVE_TAR_XZ}\n请确保安装程序完整。")
                return

            # Step 2: Create install directory
            self.signals.status.emit("正在创建安装目录...")
            self.signals.progress.emit(10)
            os.makedirs(self._install_dir, exist_ok=True)

            # Step 3: Extract files from .tar.xz
            self.signals.status.emit("正在解压文件...")
            with lzma.open(ARCHIVE_TAR_XZ, "rb") as f:
                with tarfile.open(fileobj=f, mode="r") as tf:
                    members = tf.getmembers()
                    total = len(members)
                    for i, member in enumerate(members):
                        tf.extract(member, self._install_dir, filter="data")
                        pct = 10 + int((i + 1) / max(total, 1) * 75)
                        self.signals.progress.emit(pct)
                        self.signals.status.emit(
                            f"正在解压: {os.path.basename(member.name)}")

            # Step 4: Find the main exe
            self.signals.status.emit("正在配置快捷方式...")
            self.signals.progress.emit(85)
            exe_path = self._find_exe()
            if not exe_path:
                self.signals.finished.emit(False,
                    "安装包中未找到可执行文件，安装可能不完整。")
                return

            # Step 5: Create shortcuts
            icon_path = exe_path  # .exe contains embedded icon

            if self._create_desktop:
                desktop_lnk = os.path.join(
                    get_desktop_path(), f"{APP_NAME}.lnk")
                create_shortcut(
                    exe_path, desktop_lnk,
                    working_dir=self._install_dir,
                    icon=icon_path,
                    description=APP_NAME,
                    run_as_admin=True,
                )

            if self._create_start_menu:
                start_dir = os.path.join(
                    get_start_menu_programs_path(), APP_NAME)
                os.makedirs(start_dir, exist_ok=True)
                start_lnk = os.path.join(start_dir, f"{APP_NAME}.lnk")
                create_shortcut(
                    exe_path, start_lnk,
                    working_dir=self._install_dir,
                    icon=icon_path,
                    description=APP_NAME,
                    run_as_admin=True,
                )

            # Step 6: Write uninstaller
            self._write_uninstaller()

            self.signals.progress.emit(100)
            self.signals.status.emit("安装完成！")
            self.signals.finished.emit(True, "")

        except Exception as e:
            self.signals.finished.emit(False, f"安装失败: {e}")

    def _find_exe(self) -> str:
        for root, dirs, files in os.walk(self._install_dir):
            for f in files:
                if f.lower().endswith(".exe") and (
                        APP_NAME_SLUG in f or LEGACY_APP_SLUG in f):
                    return os.path.join(root, f)
        for root, dirs, files in os.walk(self._install_dir):
            for f in files:
                if f.lower().endswith(".exe"):
                    return os.path.join(root, f)
        return ""

    def _write_uninstaller(self):
        uninstall_cmd = os.path.join(self._install_dir, "uninstall.bat")
        desktop_lnk = os.path.join(get_desktop_path(), f"{APP_NAME}.lnk")
        start_menu_dir = os.path.join(get_start_menu_programs_path(), APP_NAME)
        with open(uninstall_cmd, "w", encoding="utf-8") as f:
            f.write("@echo off\n")
            f.write("echo ============================================\n")
            f.write("echo   帽兜工具 — 卸载程序\n")
            f.write("echo ============================================\n")
            f.write("echo.\n")
            f.write("echo 即将卸载帽兜工具，此操作不可撤销。\n")
            f.write(f"echo 安装目录: {self._install_dir}\n")
            f.write("echo.\n")
            f.write("set /p CONFIRM=确认卸载请输入 Y 并按回车: \n")
            f.write('if /i not "%CONFIRM%"=="Y" (\n')
            f.write("    echo 已取消卸载。\n")
            f.write("    pause\n")
            f.write("    exit /b\n")
            f.write(")\n")
            f.write("echo.\n")
            f.write("echo 正在删除应用文件...\n")
            f.write(f'rmdir /s /q "{self._install_dir}" 2>nul\n')
            f.write(f'if exist "{self._install_dir}" (\n')
            f.write("    echo 部分文件可能正在使用中，请手动删除。\n")
            f.write(") else (\n")
            f.write("    echo 应用文件已删除。\n")
            f.write(")\n")
            f.write("echo.\n")
            f.write("echo 正在清理快捷方式...\n")
            f.write(f'del /f /q "{desktop_lnk}" 2>nul\n')
            f.write(f'rmdir /s /q "{start_menu_dir}" 2>nul\n')
            f.write("echo.\n")
            f.write("echo 卸载完成！\n")
            f.write("pause\n")

    @staticmethod
    def write_delete_setup_bat(setup_exe_path: str) -> str:
        bat_path = os.path.join(
            os.environ.get("TEMP", os.path.expanduser("~")), "_del_setup.bat")
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write("@echo off\n")
            f.write(f'set "EXE={setup_exe_path}"\n')
            f.write(":retry\n")
            f.write("ping 127.0.0.1 -n 2 >nul\n")
            f.write('if not exist "%EXE%" goto done\n')
            f.write('del /f /q "%EXE%" 2>nul\n')
            f.write('if exist "%EXE%" goto retry\n')
            f.write(":done\n")
            f.write('del /f /q "%~f0"\n')
        return bat_path


# ── Wizard Pages ──

class WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle(f"欢迎安装 {APP_NAME}")
        self.setSubTitle(f"版本 {APP_VERSION} — {APP_PUBLISHER}")

        layout = QVBoxLayout(self)
        layout.addSpacing(20)

        desc = QLabel(
            f"{APP_NAME} 是一款集文件压缩/解压、局域网文件传输、\n"
            f"局域网联机大厅/VPN隧道联机、可视化网页爬虫、\n"
            f"HTTP请求发送、AI智能对话、磁盘空间索引分析、\n"
            f"故障检测与修复于一体的 Windows 桌面工具。\n\n"
            f"本安装程序将把应用文件解压到您指定的目录，\n"
            f"并创建桌面和开始菜单快捷方式。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #c8ccd4; line-height: 1.6;")
        layout.addWidget(desc)

        layout.addSpacing(20)
        note = QLabel("点击「下一步」继续。")
        note.setStyleSheet("color: #9498a4;")
        layout.addWidget(note)
        layout.addStretch()


class PathPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("选择安装位置")
        self.setSubTitle("请选择应用安装目录，需要至少 450 MB 可用空间。")

        layout = QVBoxLayout(self)
        layout.addSpacing(10)

        layout.addWidget(QLabel("安装目录:"))

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit(DEFAULT_INSTALL_DIR)
        path_row.addWidget(self._path_edit)

        browse_btn = QPushButton("浏览...")
        browse_btn.setObjectName("secondaryBtn")
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        layout.addSpacing(12)
        space_hint = QLabel("")
        space_hint.setStyleSheet("color: #9498a4; font-size: 11px;")
        layout.addWidget(space_hint)
        self._space_hint = space_hint

        layout.addSpacing(16)

        self._desktop_cb = QCheckBox("创建桌面快捷方式")
        self._desktop_cb.setChecked(True)
        layout.addWidget(self._desktop_cb)

        self._startmenu_cb = QCheckBox("创建开始菜单快捷方式")
        self._startmenu_cb.setChecked(True)
        layout.addWidget(self._startmenu_cb)

        layout.addStretch()

        self.registerField("install_path*", self._path_edit)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "选择安装目录")
        if folder:
            self._path_edit.setText(os.path.join(folder, APP_NAME))

    def install_path(self) -> str:
        return self._path_edit.text().strip()

    def create_desktop(self) -> bool:
        return self._desktop_cb.isChecked()

    def create_start_menu(self) -> bool:
        return self._startmenu_cb.isChecked()


class InstallPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("正在安装")
        self.setSubTitle("请稍候，正在解压应用文件...")
        self._finished = False
        self._success = False

        layout = QVBoxLayout(self)
        layout.addSpacing(10)

        self._status = QLabel("准备中...")
        self._status.setStyleSheet("color: #c8ccd4;")
        layout.addWidget(self._status)

        layout.addSpacing(8)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        self._detail = QLabel("")
        self._detail.setStyleSheet("color: #9498a4; font-size: 11px;")
        layout.addWidget(self._detail)

        layout.addStretch()

    def initializePage(self):
        """Called when this page becomes current — start installation."""
        path_page = self.wizard().page(1)
        install_dir = path_page.install_path()
        create_desk = path_page.create_desktop()
        create_start = path_page.create_start_menu()

        self._thread = InstallThread(install_dir, create_desk, create_start)
        self._thread.signals.progress.connect(self._progress.setValue)
        self._thread.signals.status.connect(self._detail.setText)
        self._thread.signals.finished.connect(self._on_finished)
        self._thread.start()

        # Disable Back/Next until done
        self.setCommitPage(True)

    def isComplete(self) -> bool:
        return self._finished

    def _on_finished(self, success: bool, message: str):
        self._finished = True
        self._success = success
        if success:
            self._status.setText("安装完成！")
            self._status.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        else:
            self._status.setText("安装失败")
            self._status.setStyleSheet("color: #f38ba8; font-weight: bold;")
            self._detail.setText(message)
        self.completeChanged.emit()


class FinishPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("安装完成")
        self.setSubTitle(f"{APP_NAME} 已成功安装到您的计算机。")

        layout = QVBoxLayout(self)
        layout.addSpacing(10)

        desc = QLabel(
            "您可以通过桌面快捷方式或开始菜单启动应用。\n\n"
            "如需卸载，请运行安装目录中的 uninstall.bat，\n"
            "或直接删除安装目录及快捷方式。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #c8ccd4; line-height: 1.6;")
        layout.addWidget(desc)

        layout.addSpacing(16)
        self._launch_cb = QCheckBox("立即启动帽兜工具")
        self._launch_cb.setChecked(True)
        layout.addWidget(self._launch_cb)

        self._delete_installer_cb = QCheckBox("删除安装程序")
        self._delete_installer_cb.setChecked(False)
        layout.addWidget(self._delete_installer_cb)

        layout.addStretch()

    def launch_on_finish(self) -> bool:
        return self._launch_cb.isChecked()

    def delete_installer_on_finish(self) -> bool:
        return self._delete_installer_cb.isChecked()


class SetupWizard(QWizard):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} 安装程序")
        self.setMinimumSize(560, 400)
        self.resize(600, 440)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)

        self._welcome = WelcomePage()
        self._path = PathPage()
        self._install = InstallPage()
        self._finish = FinishPage()

        self.addPage(self._welcome)
        self.addPage(self._path)
        self.addPage(self._install)
        self.addPage(self._finish)

        self.setButtonText(QWizard.WizardButton.NextButton, "下一步")
        self.setButtonText(QWizard.WizardButton.BackButton, "上一步")
        self.setButtonText(QWizard.WizardButton.CancelButton, "取消")
        self.setButtonText(QWizard.WizardButton.FinishButton, "完成")

        self.finished.connect(self._on_finish)

    def _on_finish(self, result):
        if result == QWizard.DialogCode.Accepted:
            if self._finish.launch_on_finish():
                exe = self._find_installed_exe()
                if exe:
                    open_file_or_dir(exe)
            if self._finish.delete_installer_on_finish() and getattr(sys, "frozen", False):
                try:
                    bat_path = InstallThread.write_delete_setup_bat(sys.executable)
                    spawn_detached(["cmd", "/c", bat_path, sys.executable])
                except Exception:
                    pass

    def _find_installed_exe(self) -> str:
        install_dir = self._path.install_path()
        for root, dirs, files in os.walk(install_dir):
            for f in files:
                if f.lower().endswith(".exe"):
                    return os.path.join(root, f)
        return ""


def main():
    if sys.platform != "win32":
        print("安装程序仅在 Windows 上可用。")
        print("在其他平台上，请直接运行 python code/main.py 或使用 python code/build.py 构建。")
        return

    if not os.path.exists(ARCHIVE_TAR_XZ):
        alt = os.path.join(PROJECT_ROOT, "dist", f"{APP_NAME_SLUG}.tar.xz")
        if os.path.exists(alt):
            globals()["ARCHIVE_TAR_XZ"] = alt
        else:
            app = QApplication(sys.argv)
            QMessageBox.critical(
                None, "错误",
                f"找不到安装包数据。\n\n"
                f"请确保 {APP_NAME_SLUG}.tar.xz 与安装程序在同一目录。\n\n"
                f"搜索路径: {ARCHIVE_TAR_XZ}"
            )
            return

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(SETUP_STYLE)

    wizard = SetupWizard()
    wizard.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
