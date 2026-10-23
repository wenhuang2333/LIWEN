"""帽兜工具 — 修复工具 (独立 EXE 入口)"""
import os
import sys
import time
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QLabel, QProgressBar, QMessageBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QIcon

from utils.logger import (
    setup_logging, info, error, warn, install_crash_handler, close as log_close,
)
from utils.helpers import open_file_or_dir
from utils.paths import static_path
from utils.repair_shared import (
    load_check_results, delete_repair_task, acquire_lock, release_lock,
)
from utils.repair_helpers import find_main_exe
from ui.diagnostic_page import (
    CheckResult, LAN_CHECKS, BASIC_CHECKS, REPAIR_REGISTRY,
    _repair_free_port, _repair_install_audio, _repair_repair_config,
)

STYLE = """
QDialog {
    background-color: #1e1e2e;
    border: 1px solid #313244;
}
QLabel {
    color: #cdd6f4;
    font-size: 13px;
}
QProgressBar {
    border: 1px solid #313244;
    border-radius: 4px;
    background-color: #313244;
    text-align: center;
    color: #cdd6f4;
    font-size: 11px;
    height: 22px;
}
QProgressBar::chunk {
    background-color: #89b4fa;
    border-radius: 3px;
}
"""


class RepairThread(QThread):
    progress_updated = pyqtSignal(int, str)
    repair_done = pyqtSignal(int, int, str)
    repair_error = pyqtSignal(str)

    def __init__(self, task_data: dict | None):
        super().__init__()
        self._task_data = task_data
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _log(self, msg: str):
        info(msg)

    def _run_checks(self, checks: list) -> list:
        results = []
        total = len(checks)
        for idx, (check_id, category, name, func, repair_id) in enumerate(checks):
            if self._cancel:
                break
            pct = int((idx / total) * 50)
            self.progress_updated.emit(pct, f"正在检查: {name}")
            try:
                result = func(self._log)
            except Exception as e:
                result = CheckResult(
                    status="fail",
                    detail=f"检查执行异常: {type(e).__name__}: {e}",
                    suggestions=["诊断框架内部错误，请联系开发者"])
            result.check_id = check_id
            result.category = category
            result.name = name
            result.repair_id = repair_id
            result.timestamp = datetime.now().isoformat()
            results.append(result)
            self._log(f"[{result.status}] {name}: {result.detail}")
        return results

    def _run_repair(self, repair_id: str, check_name: str, port: int = 0) -> tuple:
        entry = REPAIR_REGISTRY.get(repair_id)
        if entry is None:
            return (False, f"未找到修复程序: {repair_id}")

        display_name, func, _needs_admin, _mode = entry
        if func is None and repair_id == "free_port":
            func = lambda log, p=port: _repair_free_port(log, p)
        if func is None:
            return (False, f"修复程序未实现: {repair_id}")

        self._log(f"── 正在修复: {display_name} ({check_name}) ──")
        try:
            success, message = func(self._log)
        except Exception as e:
            success = False
            message = f"修复异常: {type(e).__name__}: {e}"
        self._log(f"[{'完成' if success else '失败'}] {display_name}: {message}")
        return (success, message)

    def run(self):
        try:
            if self._task_data is not None:
                self._run_targeted()
            else:
                self._run_global()
        except Exception as e:
            self.repair_error.emit(f"修复过程异常: {type(e).__name__}: {e}")

    def _run_targeted(self):
        entry_list = self._task_data.get("results", [])
        main_exe = self._task_data.get("main_exe_path", "")
        total = len(entry_list)

        self._log(f"=== 定向修复模式: {total} 个问题 ===")
        info(f"定向修复模式启动, {total} 个待修复项")

        port_map = {"tcp_12345": 12345, "udp_12346": 12346, "udp_12347": 12347}

        success_count = 0
        fail_count = 0

        for idx, entry in enumerate(entry_list):
            if self._cancel:
                break
            repair_id = entry.get("repair_id", "")
            check_id = entry.get("check_id", "")
            check_name = entry.get("name", repair_id)
            port = port_map.get(check_id, 0)
            pct = int(((idx + 1) / total) * 100)
            self.progress_updated.emit(pct, f"正在修复: {check_name}")

            ok, msg = self._run_repair(repair_id, check_name, port)
            if ok:
                success_count += 1
            else:
                fail_count += 1

        self.repair_done.emit(success_count, fail_count, main_exe)

    def _run_global(self):
        self._log("=== 全局修复模式 ===")
        info("全局修复模式启动")

        all_checks = list(LAN_CHECKS) + list(BASIC_CHECKS)
        results = self._run_checks(all_checks)

        fixable = [r for r in results
                   if r.status in ("fail", "warning") and r.repair_id]
        self._log(f"检测完成: {len(results)} 项, {len(fixable)} 个可修复问题")

        if not fixable:
            self.repair_done.emit(0, 0, find_main_exe())
            return

        success_count = 0
        fail_count = 0
        total = len(fixable)

        for idx, r in enumerate(fixable):
            if self._cancel:
                break
            pct = int(50 + ((idx + 1) / total) * 50)
            self.progress_updated.emit(pct, f"正在修复: {r.name}")

            port = 0
            if r.repair_id == "free_port":
                import re
                m = re.search(r"端口\s*(\d+)", r.name)
                if m:
                    port = int(m.group(1))

            ok, msg = self._run_repair(r.repair_id, r.name, port)
            if ok:
                success_count += 1
            else:
                fail_count += 1

        self.repair_done.emit(success_count, fail_count, find_main_exe())


class RepairDialog(QDialog):
    def __init__(self):
        super().__init__()
        self._thread: RepairThread | None = None
        self._main_exe = ""
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("帽兜工具 - 修复工具")
        self.setFixedSize(480, 160)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
        )
        self.setStyleSheet(STYLE)

        icon_path = static_path("logo.jpg")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 24, 30, 24)
        layout.setSpacing(16)

        self._task_label = QLabel("正在初始化…")
        self._task_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = QFont()
        font.setPointSize(11)
        self._task_label.setFont(font)
        layout.addWidget(self._task_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        layout.addStretch()

    def start(self):
        task_data = load_check_results()
        if task_data is not None:
            info(f"读取修复任务: {len(task_data.get('results', []))} 项")
        else:
            info("未找到修复任务文件，进入全局修复模式")

        if not acquire_lock():
            QMessageBox.warning(
                self, "修复工具已在运行",
                "检测到另一个修复工具实例正在运行中。\n\n"
                "请等待当前修复任务完成后再试。")
            QApplication.quit()
            return

        self._thread = RepairThread(task_data)
        self._thread.progress_updated.connect(self._on_progress)
        self._thread.repair_done.connect(self._on_done)
        self._thread.repair_error.connect(self._on_error)
        self._thread.start()

    def _on_progress(self, pct: int, text: str):
        self._progress.setValue(pct)
        self._task_label.setText(text)

    def _on_done(self, success: int, fail: int, main_exe: str):
        self._main_exe = main_exe
        delete_repair_task()
        release_lock()

        info(f"修复完成: {success} 成功, {fail} 失败")

        if fail == 0 and success == 0:
            msg = "未发现需要修复的问题。\n\n即将重启主程序。"
        else:
            msg = f"修复完成: {success} 成功, {fail} 失败。\n\n即将重启主程序。"

        QMessageBox.information(self, "修复完成", msg)

        self._restart_main()

    def _on_error(self, msg: str):
        error(f"修复过程异常: {msg}")
        release_lock()
        QMessageBox.critical(
            self, "修复失败",
            f"修复过程中发生错误:\n\n{msg}\n\n"
            "请尝试重新运行主程序的故障检测。")
        QApplication.quit()

    def _restart_main(self):
        if os.path.exists(self._main_exe):
            try:
                open_file_or_dir(self._main_exe)
                info(f"已重启主程序: {self._main_exe}")
            except Exception as e:
                error(f"重启主程序失败: {e}")
                QMessageBox.warning(
                    self, "提示",
                    f"修复已完成，但无法自动重启主程序。\n\n"
                    f"请手动启动: {self._main_exe}")
        else:
            warn(f"主程序不存在: {self._main_exe}")
            QMessageBox.warning(
                self, "提示",
                "修复已完成，但无法找到主程序。\n请手动启动帽兜工具。")
        QApplication.quit()

    def closeEvent(self, event):
        if self._thread and self._thread.isRunning():
            self._thread.cancel()
            self._thread.wait(3000)
        release_lock()
        event.accept()


def main():
    setup_logging()
    info("修复工具启动")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    install_crash_handler()

    icon_path = static_path("logo.jpg")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    dialog = RepairDialog()
    dialog.show()
    dialog.start()

    app.exec()


if __name__ == "__main__":
    main()
