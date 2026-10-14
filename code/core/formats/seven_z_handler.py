import contextlib
import glob
import lzma
import os
import re
import shutil
import struct
import tempfile
import time
from pathlib import Path
from typing import Optional, Callable

from core.formats._base import ArchiveHandler, register_handler

try:
    from utils.logger import error as log_error, warn as log_warn, exception as log_exception
except Exception:  # pragma: no cover - 日志模块不可用时退化为静默
    def log_error(message: str):
        pass

    def log_warn(message: str):
        pass

    def log_exception(message: str):
        pass


# 用户可见的错误文案（workers.py / CLI 会把异常文本直接显示出来）
PASSWORD_ERROR = "密码错误或压缩包损坏"
NEED_PASSWORD_ERROR = "此压缩包已加密，需要密码"
CORRUPT_ERROR = "压缩包已损坏或格式不正确"

# 分卷命名：multivolumefile 原生 4 位（.0001），本项目按 7-Zip 习惯写 3 位（.001），
# 读取时两种都认。
_VOLUME_PART_RE = re.compile(r"(?i)(\.7z)\.(\d{2,4})$")


class _ProgressReporter:
    """进度回调节流器：只在整数百分比变化且距上次上报 >=0.1s 时回调。

    原先每写一个文件就回调一次，2000 个文件会产生 2000 个跨线程 Qt 信号，
    UI 事件队列被塞满后直接卡死。结束时强制补一次 100%。
    """

    MIN_INTERVAL = 0.1

    def __init__(self, callback: Optional[Callable[[int], None]], total: int):
        self._callback = callback
        self._total = max(int(total), 1)
        self._last_percent = -1
        self._last_time = 0.0

    def advance(self, done: int, force: bool = False) -> None:
        if self._callback is None:
            return
        percent = min(int(done / self._total * 100), 100)
        now = time.monotonic()
        if not force:
            if percent <= self._last_percent:
                return
            if now - self._last_time < self.MIN_INTERVAL:
                return
        self._last_percent = percent
        self._last_time = now
        try:
            self._callback(percent)
        except Exception:  # 回调属于 UI 侧代码，出错不能影响压缩本身
            log_exception("进度回调执行失败")

    def finish(self) -> None:
        self.advance(self._total, force=True)


def _volume_base(archive_path: str) -> Optional[str]:
    """分卷路径 → 基名（multivolumefile 需要基名才能拼接各卷）；非分卷返回 None。

    同时兼容本程序写出的 ``.7z.001`` 与 multivolumefile/py7zr 原生的 ``.7z.0001``；
    传入的是主名（xxx.7z）而实际只有分卷存在时也能识别。
    """
    m = _VOLUME_PART_RE.search(archive_path)
    if m:
        return archive_path[: m.start(1)] + m.group(1)
    if not os.path.exists(archive_path):
        for suffix in (".001", ".0001"):
            if os.path.exists(archive_path + suffix):
                return archive_path
    return None


@contextlib.contextmanager
def _open_for_read(archive_path: str, password: Optional[str] = None):
    """打开 7z 读取（自动识别并拼接 .7z.001/.7z.0001 分卷）。"""
    import py7zr

    base = _volume_base(archive_path)
    if base is None:
        if not os.path.exists(archive_path):
            raise FileNotFoundError(f"压缩包不存在: {archive_path}")
        with py7zr.SevenZipFile(archive_path, "r", password=password) as szf:
            yield szf
        return

    import multivolumefile
    if not glob.glob(glob.escape(base) + ".*"):
        raise FileNotFoundError(f"找不到分卷文件: {base}.001")
    with multivolumefile.MultiVolume(base, mode="rb") as mvf:
        with py7zr.SevenZipFile(mvf, "r", password=password) as szf:
            yield szf


def _translate_7z_error(exc: BaseException) -> Optional[ValueError]:
    """把 py7zr / lzma 的底层异常翻译成中文 ValueError；无需翻译时返回 None。"""
    import py7zr.exceptions as p7z_err

    if isinstance(exc, ValueError):
        return None  # 已经是我们自己抛出的可读错误
    if isinstance(exc, (FileNotFoundError, PermissionError,
                        IsADirectoryError, NotADirectoryError)):
        return None
    if isinstance(exc, p7z_err.PasswordRequired):
        return ValueError(NEED_PASSWORD_ERROR)
    if isinstance(exc, p7z_err.CrcError):
        return ValueError(PASSWORD_ERROR)
    if isinstance(exc, (p7z_err.Bad7zFile, lzma.LZMAError, EOFError,
                        struct.error, IndexError)):
        return ValueError(CORRUPT_ERROR)
    if isinstance(exc, p7z_err.UnsupportedCompressionMethodError):
        return ValueError("压缩包使用了不支持的压缩算法")
    if isinstance(exc, p7z_err.AbsolutePathError):
        return ValueError("压缩包中包含不安全的绝对路径，已拒绝解压")
    if isinstance(exc, OSError):
        return ValueError(f"无法读取压缩包: {exc}")
    return None


def _remove_quietly(path: str) -> None:
    """删除文件；失败只记日志（清理临时文件不能掩盖真正的异常）。"""
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as e:
        log_error(f"清理临时文件失败: {path}: {e}")


def _merge_tree(src_dir: str, dest_dir: str) -> None:
    """把临时目录里已完整解出的内容并入目标目录（同名覆盖，保持原有覆盖行为）。"""
    for dirpath, _dirnames, filenames in os.walk(src_dir):
        rel = os.path.relpath(dirpath, src_dir)
        target_dir = dest_dir if rel == "." else os.path.join(dest_dir, rel)
        os.makedirs(target_dir, exist_ok=True)
        for fn in filenames:
            src = os.path.join(dirpath, fn)
            dst = os.path.join(target_dir, fn)
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            try:
                os.replace(src, dst)
            except PermissionError as e:
                raise PermissionError(
                    f"无法写入目标文件（可能被其他程序占用）: {dst}") from e
            except OSError:
                shutil.move(src, dst)


@register_handler
class SevenZHandler(ArchiveHandler):

    @classmethod
    def format_name(cls) -> str:
        return "7z"

    @classmethod
    def extensions(cls) -> list[str]:
        return [".7z"]

    @classmethod
    def supports_volumes(cls) -> bool:
        # 分卷写入/读取已真实实现（multivolumefile + py7zr），见 compress/_open_for_read
        return True

    @classmethod
    def supports_password(cls) -> bool:
        return True

    @classmethod
    def detect(cls, filepath: str) -> bool:
        path = Path(filepath)
        ext = path.suffix.lower()
        if ext == ".7z":
            return True
        # 分卷：xxx.7z.001 / xxx.7z.0001
        double = "".join(path.suffixes[-2:]).lower() if len(path.suffixes) >= 2 else ""
        if double.startswith(".7z.") and _VOLUME_PART_RE.search(double):
            return True
        try:
            with open(filepath, "rb") as f:
                return f.read(4) == b"7z\xbc\xaf"
        except OSError:
            return False

    @staticmethod
    def _get_arcroot(file_paths):
        from utils.helpers import get_common_parent
        if len(file_paths) == 1:
            p = Path(file_paths[0])
            return str(p.parent) if p.is_file() else str(p.parent)
        return get_common_parent(file_paths)

    @staticmethod
    def _walk_files(root):
        """列出 root（文件或目录）下的所有文件；路径不存在时抛 FileNotFoundError。"""
        root = Path(root)
        if not root.exists():
            raise FileNotFoundError(f"找不到要压缩的文件或文件夹: {root}")
        if root.is_file():
            yield str(root)
        elif root.is_dir():
            for dirpath, _, filenames in os.walk(root):
                for fn in filenames:
                    yield str(Path(dirpath) / fn)

    @staticmethod
    def compress(file_paths, output_path, password=None, volume_size=None,
                 progress_callback=None, seed_code=None, compress_level=0):
        if compress_level > 0:
            from core.formats.custom_handler import CustomHandler
            return CustomHandler.compress(
                file_paths, output_path,
                password=password, volume_size=volume_size,
                progress_callback=progress_callback,
                compress_level=compress_level,
                wrapped_format=SevenZHandler.format_name(),
            )
        import py7zr

        file_paths = [str(p) for p in (file_paths or [])]
        # 先校验输入：不存在的路径必须报错，绝不能静默跳过
        missing = [p for p in file_paths if not os.path.exists(p)]
        if missing:
            raise FileNotFoundError("找不到要压缩的文件或文件夹: " + ", ".join(missing))
        common_parent = SevenZHandler._get_arcroot(file_paths)
        entries = [(fp, str(Path(fp).relative_to(common_parent)))
                   for p in file_paths for fp in SevenZHandler._walk_files(p)]
        if not entries:
            raise ValueError("没有可压缩的文件：所选内容为空")

        filters = []
        if password:
            filters.append({"id": py7zr.FILTER_CRYPTO_AES256_SHA256})
        szf_kwargs = {"password": password}
        if filters:
            szf_kwargs["filters"] = filters

        reporter = _ProgressReporter(progress_callback, len(entries))
        processed = 0

        def _write(target):
            """把 entries 写入 target（路径或 multivolumefile 对象）。"""
            nonlocal processed
            with py7zr.SevenZipFile(target, "w", **szf_kwargs) as szf:
                for fp, arcname in entries:
                    szf.write(fp, arcname)
                    processed += 1
                    reporter.advance(processed)

        if volume_size:
            volume_size = int(volume_size)
            if volume_size <= 0:
                raise ValueError("分卷大小必须大于 0")
            if os.path.isfile(output_path):
                # 同名单文件归档会被 .001 分卷"遮蔽"（读取时优先认它），提示一下
                log_warn(f"分卷压缩不会覆盖同名的单文件归档: {output_path}")
            return SevenZHandler._compress_volumes(
                output_path, volume_size, _write, reporter, lambda: processed)

        # 先写同目录的 .part，整包成功后再原子替换，失败绝不破坏用户已有归档
        tmp_path = output_path + ".part"
        try:
            _write(tmp_path)
            try:
                os.replace(tmp_path, output_path)
            except PermissionError as e:
                raise PermissionError(
                    f"无法写入目标文件（可能被其他程序占用）: {output_path}") from e
        except Exception:
            log_exception(f"7z 压缩失败: {output_path}")
            raise
        finally:
            _remove_quietly(tmp_path)

        reporter.finish()
        return {
            "path": output_path,
            "size": os.path.getsize(output_path),
            "file_count": processed,
        }

    @staticmethod
    def _compress_volumes(output_path, volume_size, write_func, reporter, processed_getter):
        """多卷写入：<output>.7z.001/.002...；写入期用 .part 基名，成功后再改名。"""
        import multivolumefile

        tmp_base = output_path + ".part"
        final_parts = []
        try:
            with multivolumefile.MultiVolume(
                    tmp_base, mode="wb", volume=volume_size, ext_digits=3) as mvf:
                write_func(mvf)
            for part in sorted(glob.glob(glob.escape(tmp_base) + ".*")):
                dst = f"{output_path}.{part.rsplit('.', 1)[-1]}"
                try:
                    os.replace(part, dst)
                except PermissionError as e:
                    raise PermissionError(
                        f"无法写入目标文件（可能被其他程序占用）: {dst}") from e
                final_parts.append(dst)
            # 清掉上一轮残留的多余分卷，否则下次读取会把它们拼进来
            keep = set(final_parts)
            for old in glob.glob(glob.escape(output_path) + ".*"):
                if old not in keep and _VOLUME_PART_RE.search(old):
                    _remove_quietly(old)
        except Exception:
            log_exception(f"7z 分卷压缩失败: {output_path}")
            raise
        finally:
            for leftover in glob.glob(glob.escape(tmp_base) + ".*"):
                _remove_quietly(leftover)

        reporter.finish()
        return {
            "path": final_parts[0],
            "size": sum(os.path.getsize(p) for p in final_parts),
            "file_count": processed_getter(),
        }

    @staticmethod
    def decompress(archive_path, dest_dir, password=None, progress_callback=None):
        os.makedirs(dest_dir, exist_ok=True)
        # 先解到目标目录下的临时目录，整包成功后再并入；失败不留半个文件
        tmp_dir = tempfile.mkdtemp(prefix=".liwen_7z_", dir=dest_dir)
        total = 0
        reporter = _ProgressReporter(progress_callback, 1)
        try:
            try:
                with _open_for_read(archive_path, password) as szf:
                    infos = szf.list()
                    total = len([i for i in infos
                                 if not getattr(i, "is_directory", False)]) or len(infos)
                    reporter = _ProgressReporter(progress_callback, max(total, 1))
                    szf.extractall(tmp_dir)
            except Exception as e:
                log_exception(f"7z 解压失败: {archive_path}")
                clean = _translate_7z_error(e)
                if clean is None:
                    raise
                raise clean from e
            _merge_tree(tmp_dir, dest_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        reporter.finish()
        return {"dest": dest_dir, "file_count": total}

    @staticmethod
    def list_contents(archive_path: str) -> list[dict]:
        """列出归档内容；压缩包损坏时抛出可读错误（不再静默返回空列表）。"""
        try:
            with _open_for_read(archive_path) as szf:
                result = []
                for info in szf.list():
                    result.append({
                        "name": getattr(info, "filename", str(info)),
                        "size": getattr(info, "uncompressed", 0) or 0,
                        "compressed_size": getattr(info, "compressed", 0) or 0,
                        "modified": getattr(info, "creationtime", None),
                        "is_dir": bool(getattr(info, "is_directory", False)),
                    })
                return result
        except Exception as e:
            log_exception(f"读取 7z 目录失败: {archive_path}")
            clean = _translate_7z_error(e)
            if clean is None:
                raise
            raise clean from e

    @staticmethod
    def extract_single(archive_path, member_name, dest_dir, password=None):
        os.makedirs(dest_dir, exist_ok=True)
        tmp_dir = tempfile.mkdtemp(prefix=".liwen_7z_", dir=dest_dir)
        try:
            try:
                with _open_for_read(archive_path, password) as szf:
                    if member_name not in szf.getnames():
                        raise ValueError(f"压缩包中未找到: {member_name}")
                    szf.extract(tmp_dir, targets=[member_name])
            except Exception as e:
                log_exception(f"7z 提取单个文件失败: {archive_path} -> {member_name}")
                clean = _translate_7z_error(e)
                if clean is None:
                    raise
                raise clean from e
            _merge_tree(tmp_dir, dest_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return os.path.join(dest_dir, member_name)

    @staticmethod
    def is_encrypted(archive_path: str) -> bool:
        try:
            with _open_for_read(archive_path) as szf:
                return bool(szf.needs_password())
        except Exception:
            return False
