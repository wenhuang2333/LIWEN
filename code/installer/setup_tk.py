"""
帽兜工具 — 轻量安装程序 (tkinter)
不依赖 PyQt6，安装包体积大幅减小。
"""
import os
import sys
import tarfile
import lzma
import threading
import subprocess

from utils.helpers import open_file_or_dir, spawn_detached

# ── tkinter imports ──
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from installer.shortcut import (
    create_shortcut, get_desktop_path, get_start_menu_programs_path,
)

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
    APP_INSTALL_SLUG,
    LEGACY_APP_SLUG,
)
# 与 setup.py 保持一致：安装目录名固定为 LIWEN（单一来源 constants.py）
DEFAULT_INSTALL_DIR = os.path.join(
    os.environ.get("ProgramFiles", "C:\\Program Files"), APP_INSTALL_SLUG)

if getattr(sys, "frozen", False):
    BUNDLE_DIR = sys._MEIPASS
else:
    # installer/ 位于 code/ 下一级 → 上溯两级即项目根目录
    BUNDLE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ARCHIVE_TAR_XZ = os.path.join(BUNDLE_DIR, f"{APP_NAME_SLUG}.tar.xz")

# 项目根目录（仅源码运行时的 dist/ 回退查找使用）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Catppuccin Mocha dark palette ──
BG = "#1e1e2e"
FG = "#e8e8f0"
SURFACE = "#252536"
BORDER = "#45475a"
ACCENT = "#89b4fa"
ACCENT_TEXT = "#1e1e2e"
MUTED = "#9498a4"
BTN_HOVER = "#b4d0fb"


class SetupWizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} 安装程序")
        self.geometry("600x440")
        self.resizable(False, False)
        self.configure(bg=BG)

        # center on screen
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - 600) // 2
        y = (sh - 440) // 2
        self.geometry(f"+{x}+{y}")

        self._install_dir = tk.StringVar(value=DEFAULT_INSTALL_DIR)
        self._create_desktop = tk.BooleanVar(value=True)
        self._create_startmenu = tk.BooleanVar(value=True)
        self._launch_on_finish = tk.BooleanVar(value=True)
        self._delete_installer = tk.BooleanVar(value=False)

        self._pages = []
        self._current_page = -1

        self._build_ui()
        self._show_page(0)

    # ── helpers ──

    def _styled_button(self, parent, text, command=None, accent=True):
        kw = {
            "text": text, "command": command, "borderwidth": 0,
            "font": ("Microsoft YaHei UI", 10),
            "padx": 20, "pady": 8,
            "cursor": "hand2",
        }
        if accent:
            kw.update({
                "bg": ACCENT, "fg": ACCENT_TEXT, "activebackground": BTN_HOVER,
                "activeforeground": ACCENT_TEXT,
            })
        else:
            kw.update({
                "bg": BORDER, "fg": FG, "activebackground": "#585b70",
                "activeforeground": FG,
            })
        return tk.Button(parent, **kw)

    def _nav_buttons(self, parent, back_cmd, next_cmd, next_text="下一步"):
        frame = tk.Frame(parent, bg=BG)
        frame.pack(side="bottom", fill="x", padx=24, pady=16)

        back_btn = self._styled_button(frame, "上一步", back_cmd, accent=False)
        back_btn.pack(side="left")

        next_btn = self._styled_button(frame, next_text, next_cmd)
        next_btn.pack(side="right")

        cancel_btn = self._styled_button(frame, "取消", self.destroy, accent=False)
        cancel_btn.pack(side="right", padx=(0, 8))

        return back_btn, next_btn

    # ── pages ──

    def _build_ui(self):
        container = tk.Frame(self, bg=BG)
        container.pack(fill="both", expand=True)
        self._container = container

    def _clear_container(self):
        for w in self._container.winfo_children():
            w.destroy()

    def _show_page(self, index):
        self._clear_container()
        self._current_page = index

        if index == 0:
            self._welcome_page()
        elif index == 1:
            self._path_page()
        elif index == 2:
            self._install_page()
        elif index == 3:
            self._finish_page()

    # ── Page 0: Welcome ──

    def _welcome_page(self):
        f = tk.Frame(self._container, bg=BG)

        tk.Label(f, text=f"欢迎安装 {APP_NAME}",
                 font=("Microsoft YaHei UI", 18, "bold"),
                 bg=BG, fg=FG).pack(pady=(40, 4))
        tk.Label(f, text=f"版本 {APP_VERSION} — {APP_PUBLISHER}",
                 font=("Microsoft YaHei UI", 11),
                 bg=BG, fg=ACCENT).pack(pady=(0, 20))

        desc = (
            f"{APP_NAME} 是一款集文件压缩/解压、局域网文件传输、\n"
            "局域网联机大厅、可视化网页爬虫、\n"
            "HTTP请求发送、AI智能对话、磁盘空间索引分析、\n"
            "故障检测与修复于一体的跨平台桌面工具。\n\n"
            "本安装程序将把应用文件解压到您指定的目录，\n"
            "并创建桌面和开始菜单快捷方式。"
        )
        tk.Label(f, text=desc, font=("Microsoft YaHei UI", 10),
                 bg=BG, fg=MUTED, justify="left").pack(pady=(0, 30))

        tk.Label(f, text="点击「下一步」继续。",
                 font=("Microsoft YaHei UI", 9), bg=BG, fg=MUTED).pack()

        self._nav_buttons(f,
                          back_cmd=None,
                          next_cmd=lambda: self._show_page(1))
        f.pack(fill="both", expand=True)

    # ── Page 1: Path ──

    def _path_page(self):
        f = tk.Frame(self._container, bg=BG)

        tk.Label(f, text="选择安装位置",
                 font=("Microsoft YaHei UI", 16, "bold"),
                 bg=BG, fg=FG).pack(pady=(30, 4))
        tk.Label(f, text="请选择应用安装目录，需要至少 450 MB 可用空间。",
                 font=("Microsoft YaHei UI", 10), bg=BG, fg=MUTED).pack(pady=(0, 20))

        # path row
        path_frame = tk.Frame(f, bg=BG)
        path_frame.pack(fill="x", padx=40, pady=(0, 10))
        tk.Label(path_frame, text="安装目录:",
                 font=("Microsoft YaHei UI", 10), bg=BG, fg=FG).pack(anchor="w")

        entry_frame = tk.Frame(path_frame, bg=SURFACE, highlightbackground=BORDER,
                               highlightthickness=1)
        entry_frame.pack(fill="x", pady=(4, 8))
        entry = tk.Entry(entry_frame, textvariable=self._install_dir,
                         font=("Microsoft YaHei UI", 10),
                         bg=SURFACE, fg=FG, insertbackground=FG,
                         relief="flat", borderwidth=6)
        entry.pack(fill="x", padx=2, pady=2)

        browse_btn = self._styled_button(path_frame, "浏览...",
                                          self._browse_folder, accent=False)
        browse_btn.pack(anchor="w")

        # checkboxes
        cb_frame = tk.Frame(f, bg=BG)
        cb_frame.pack(fill="x", padx=40, pady=(16, 0))

        cb1 = tk.Checkbutton(cb_frame, text="创建桌面快捷方式",
                             variable=self._create_desktop,
                             font=("Microsoft YaHei UI", 10),
                             bg=BG, fg=FG, selectcolor=SURFACE,
                             activebackground=BG, activeforeground=FG)
        cb1.pack(anchor="w", pady=2)

        cb2 = tk.Checkbutton(cb_frame, text="创建开始菜单快捷方式",
                             variable=self._create_startmenu,
                             font=("Microsoft YaHei UI", 10),
                             bg=BG, fg=FG, selectcolor=SURFACE,
                             activebackground=BG, activeforeground=FG)
        cb2.pack(anchor="w", pady=2)

        def _go_next():
            p = self._install_dir.get().strip()
            if not p:
                messagebox.showwarning("提示", "请选择安装目录。", parent=self)
                return
            self._show_page(2)

        self._nav_buttons(f,
                          back_cmd=lambda: self._show_page(0),
                          next_cmd=_go_next)
        f.pack(fill="both", expand=True)

    def _browse_folder(self):
        folder = filedialog.askdirectory(title="选择安装目录", parent=self)
        if folder:
            self._install_dir.set(os.path.join(folder, APP_INSTALL_SLUG))

    # ── Page 2: Install ──

    def _install_page(self):
        f = tk.Frame(self._container, bg=BG)

        tk.Label(f, text="正在安装",
                 font=("Microsoft YaHei UI", 16, "bold"),
                 bg=BG, fg=FG).pack(pady=(30, 4))
        tk.Label(f, text="请稍候，正在解压应用文件...",
                 font=("Microsoft YaHei UI", 10), bg=BG, fg=MUTED).pack(pady=(0, 20))

        self._prog_var = tk.DoubleVar(value=0)
        self._prog_bar = ttk.Progressbar(f, variable=self._prog_var,
                                          maximum=100, length=500)
        self._prog_bar.pack(padx=40, pady=(0, 8))

        self._status_var = tk.StringVar(value="准备中...")
        tk.Label(f, textvariable=self._status_var,
                 font=("Microsoft YaHei UI", 9), bg=BG, fg=MUTED).pack()

        # Style the progress bar with dark colors
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TProgressbar", background=ACCENT, troughcolor=SURFACE,
                        bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT)

        # Navigation (disabled during install)
        self._back_btn, self._next_btn = self._nav_buttons(
            f,
            back_cmd=None,
            next_cmd=None,
            next_text="安装中..."
        )
        self._back_btn.config(state="disabled")
        self._next_btn.config(state="disabled")

        f.pack(fill="both", expand=True)

        # Start installation
        self._start_install()

    def _start_install(self):
        self._thread = threading.Thread(target=self._run_install, daemon=True)
        self._thread.start()
        self._poll_thread()

    def _poll_thread(self):
        if self._thread.is_alive():
            self.after(100, self._poll_thread)
        else:
            if self._install_success:
                self._next_btn.config(text="下一步", state="normal",
                                      command=lambda: self._show_page(3))
                self._back_btn.config(state="disabled")
            else:
                self._next_btn.config(text="关闭", state="normal",
                                      command=self.destroy)

    def _run_install(self):
        try:
            install_dir = self._install_dir.get().strip()
            create_desk = self._create_desktop.get()
            create_start = self._create_startmenu.get()

            # Step 1: Validate archive
            self._set_status("正在验证安装包...", 5)
            if not os.path.exists(ARCHIVE_TAR_XZ):
                self._install_fail(f"找不到安装包数据: {ARCHIVE_TAR_XZ}")
                return

            # Step 2: Create install directory
            self._set_status("正在创建安装目录...", 10)
            os.makedirs(install_dir, exist_ok=True)

            # Step 3: Extract .tar.xz
            self._set_status("正在解压文件...", 15)
            with lzma.open(ARCHIVE_TAR_XZ, "rb") as f:
                with tarfile.open(fileobj=f, mode="r") as tf:
                    members = tf.getmembers()
                    total = len(members)
                    for i, member in enumerate(members):
                        tf.extract(member, install_dir, filter="data")
                        pct = 15 + int((i + 1) / max(total, 1) * 65)
                        self._set_status(
                            f"正在解压: {os.path.basename(member.name)}", pct)

            # Step 4: Find main exe
            self._set_status("正在配置快捷方式...", 85)
            main_exe = self._find_exe(install_dir)
            if not main_exe:
                self._install_fail("安装包中未找到可执行文件，安装可能不完整。")
                return

            # Step 5: Create shortcuts
            if create_desk:
                desktop_lnk = os.path.join(get_desktop_path(), f"{APP_NAME}.lnk")
                create_shortcut(main_exe, desktop_lnk,
                                working_dir=install_dir, icon=main_exe,
                                description=APP_NAME, run_as_admin=False)

            if create_start:
                start_dir = os.path.join(get_start_menu_programs_path(), APP_NAME)
                os.makedirs(start_dir, exist_ok=True)
                start_lnk = os.path.join(start_dir, f"{APP_NAME}.lnk")
                create_shortcut(main_exe, start_lnk,
                                working_dir=install_dir, icon=main_exe,
                                description=APP_NAME, run_as_admin=False)

            # Step 6: Write uninstaller
            self._write_uninstaller(install_dir)

            self._set_status("安装完成！", 100)
            self._install_success = True

        except Exception as e:
            self._install_fail(f"安装失败: {e}")

    def _set_status(self, text, pct):
        # thread-safe update via schedule
        self.after(0, lambda: self._status_var.set(text))
        self.after(0, lambda: self._prog_var.set(pct))

    def _install_fail(self, msg):
        def _fail():
            self._status_var.set("安装失败")
            self._next_btn.config(text="关闭", state="normal", command=self.destroy)
        self.after(0, _fail)
        self._install_success = False

    def _find_exe(self, install_dir):
        for root, dirs, files in os.walk(install_dir):
            dirs[:] = [d for d in dirs if "repair" not in d.lower()]
            for f in files:
                if f.lower().endswith(".exe") and (
                        APP_NAME_SLUG in f or LEGACY_APP_SLUG in f):
                    return os.path.join(root, f)
        for root, dirs, files in os.walk(install_dir):
            dirs[:] = [d for d in dirs if "repair" not in d.lower()]
            for f in files:
                if f.lower().endswith(".exe"):
                    return os.path.join(root, f)
        return ""

    def _schedule_delete_installer(self):
        """Write a batch script that deletes the setup exe after we exit."""
        setup_exe = sys.executable
        bat_path = os.path.join(
            os.environ.get("TEMP", os.path.expanduser("~")), "_del_setup.bat")
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write("@echo off\n")
            f.write(f'set "EXE={setup_exe}"\n')
            f.write(":retry\n")
            f.write("ping 127.0.0.1 -n 2 >nul\n")
            f.write('if not exist "%EXE%" goto done\n')
            f.write('del /f /q "%EXE%" 2>nul\n')
            f.write('if exist "%EXE%" goto retry\n')
            f.write(":done\n")
            f.write('del /f /q "%~f0"\n')
        try:
            spawn_detached(["cmd", "/c", bat_path])
        except Exception:
            pass

    def _write_uninstaller(self, install_dir):
        bat_path = os.path.join(install_dir, "uninstall.bat")
        desktop_lnk = os.path.join(get_desktop_path(), f"{APP_NAME}.lnk")
        start_lnk = os.path.join(get_start_menu_programs_path(), APP_NAME)
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write("@echo off\n")
            f.write("echo ============================================\n")
            f.write("echo   帽兜工具 — 卸载程序\n")
            f.write("echo ============================================\n")
            f.write("echo.\n")
            f.write("echo 即将卸载帽兜工具，此操作不可撤销。\n")
            f.write("echo 安装目录将被删除: \n")
            f.write(f"echo   {install_dir}\n")
            f.write("echo.\n")
            f.write("set /p CONFIRM=确认卸载请输入 Y 并按回车: \n")
            f.write('if /i not "%CONFIRM%"=="Y" (\n')
            f.write("    echo 已取消卸载。\n")
            f.write("    pause\n")
            f.write("    exit /b\n")
            f.write(")\n")
            f.write("echo.\n")
            f.write("echo 正在删除应用文件...\n")
            f.write(f'rmdir /s /q "{install_dir}" 2>nul\n')
            f.write(f'if exist "{install_dir}" (\n')
            f.write("    echo 部分文件可能正在使用中，请手动删除。\n")
            f.write(f'    echo 目录: {install_dir}\n')
            f.write(") else (\n")
            f.write("    echo 应用文件已删除。\n")
            f.write(")\n")
            f.write("echo.\n")
            f.write("echo 正在清理快捷方式...\n")
            f.write(f'del /f /q "{desktop_lnk}" 2>nul\n')
            f.write(f'rmdir /s /q "{start_lnk}" 2>nul\n')
            f.write("echo.\n")
            f.write("echo 卸载完成！\n")
            f.write("echo.\n")
            f.write("pause\n")

    # ── Page 3: Finish ──

    def _finish_page(self):
        f = tk.Frame(self._container, bg=BG)

        tk.Label(f, text="安装完成",
                 font=("Microsoft YaHei UI", 18, "bold"),
                 bg=BG, fg="#a6e3a1").pack(pady=(40, 4))
        tk.Label(f, text=f"{APP_NAME} 已成功安装到您的计算机。",
                 font=("Microsoft YaHei UI", 11), bg=BG, fg=FG).pack(pady=(4, 20))

        desc = (
            "您可以通过桌面快捷方式或开始菜单启动应用。\n\n"
            "如需卸载，请运行安装目录中的 uninstall.bat，\n"
            "或直接删除安装目录及快捷方式。"
        )
        tk.Label(f, text=desc, font=("Microsoft YaHei UI", 10),
                 bg=BG, fg=MUTED, justify="left").pack(pady=(0, 20))

        cb1 = tk.Checkbutton(f, text="立即启动帽兜工具",
                             variable=self._launch_on_finish,
                             font=("Microsoft YaHei UI", 10),
                             bg=BG, fg=FG, selectcolor=SURFACE,
                             activebackground=BG, activeforeground=FG)
        cb1.pack(anchor="center", pady=2)

        cb2 = tk.Checkbutton(f, text="删除安装程序（节省磁盘空间）",
                             variable=self._delete_installer,
                             font=("Microsoft YaHei UI", 10),
                             bg=BG, fg=FG, selectcolor=SURFACE,
                             activebackground=BG, activeforeground=FG)
        cb2.pack(anchor="center", pady=2)

        def _on_finish():
            if self._launch_on_finish.get():
                exe = self._find_exe(self._install_dir.get().strip())
                if exe:
                    open_file_or_dir(exe)
            if self._delete_installer.get() and getattr(sys, "frozen", False):
                self._schedule_delete_installer()
            self.destroy()

        self._nav_buttons(f,
                          back_cmd=None,
                          next_cmd=_on_finish,
                          next_text="完成")
        f.pack(fill="both", expand=True)


def main():
    if sys.platform != "win32":
        print("安装程序仅在 Windows 上可用。")
        print("在其他平台上，请直接运行 python code/main.py 或使用 python code/build.py 构建。")
        return

    if not os.path.exists(ARCHIVE_TAR_XZ):
        # Running from source — look in <project root>/dist/
        alt = os.path.join(PROJECT_ROOT, "dist", f"{APP_NAME_SLUG}.tar.xz")
        if os.path.exists(alt):
            import builtins
            globals()["ARCHIVE_TAR_XZ"] = alt
        else:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "错误",
                f"找不到安装包数据。\n\n请确保 {APP_NAME_SLUG}.tar.xz 与安装程序在同一目录。")
            return

    app = SetupWizard()
    app.mainloop()


if __name__ == "__main__":
    main()
