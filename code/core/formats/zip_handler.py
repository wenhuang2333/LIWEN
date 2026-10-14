import os
import shutil
import struct
import tempfile
import time
import zipfile
import zlib
import lzma
from pathlib import Path
from typing import Optional, Callable

from core.formats._base import ArchiveHandler, register_handler

try:
    from utils.logger import error as log_error, exception as log_exception
except Exception:  # pragma: no cover - 日志模块不可用时退化为静默
    def log_error(message: str):
        pass

    def log_exception(message: str):
        pass


# 用户可见的错误文案（workers.py / CLI 会把异常文本直接显示出来）
PASSWORD_ERROR = "密码错误或压缩包损坏"
CORRUPT_ERROR = "压缩包已损坏或格式不正确"
NEED_PASSWORD_ERROR = "此压缩包已加密，需要密码"

# 读取器只选一次：pyzipper.AESZipFile 兼容普通 zip 且支持 AES
_READER_CACHE: dict = {}


class _ProgressReporter:
    """进度回调节流器：只在整数百分比变化且距上次上报 >=0.1s 时回调。

    原先每写/解一个文件就回调一次，2000 个文件会产生 2000 个跨线程 Qt 信号，
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


def _reader_class():
    """返回读取用的 zip 类：优先 pyzipper.AESZipFile（支持 AES），否则标准库。"""
    if "cls" not in _READER_CACHE:
        try:
            import pyzipper
            _READER_CACHE["cls"] = pyzipper.AESZipFile
            _READER_CACHE["bad"] = (zipfile.BadZipFile, pyzipper.BadZipFile)
        except Exception:  # pyzipper 缺失时退化为标准库（无法解 AES）
            log_error("pyzipper 不可用，AES 加密压缩包将无法解压")
            _READER_CACHE["cls"] = zipfile.ZipFile
            _READER_CACHE["bad"] = (zipfile.BadZipFile,)
    return _READER_CACHE["cls"]


def _translate_zip_error(exc: BaseException) -> Optional[ValueError]:
    """把底层异常翻译成中文 ValueError；无需翻译时返回 None（由调用方原样抛出）。"""
    _reader_class()
    if isinstance(exc, ValueError):
        return None  # 已经是我们自己抛出的可读错误
    if isinstance(exc, (FileNotFoundError, PermissionError,
                        IsADirectoryError, NotADirectoryError)):
        return None  # 文件系统错误（目标被占用/权限不足）保持原样
    if isinstance(exc, _READER_CACHE["bad"]):
        return ValueError(CORRUPT_ERROR)
    if isinstance(exc, NotImplementedError):
        return ValueError("压缩包使用了不支持的加密或压缩算法")
    if isinstance(exc, RuntimeError):
        text = str(exc).lower()
        if "password" in text or "encrypted" in text:
            return ValueError(PASSWORD_ERROR)
        return ValueError(CORRUPT_ERROR)
    if isinstance(exc, (EOFError, struct.error, zlib.error, lzma.LZMAError, IndexError, OverflowError)):
        return ValueError(CORRUPT_ERROR)
    if isinstance(exc, OSError):
        # 目录偏移损坏导致 seek 越界时不会有 filename；写目标文件失败会带 filename
        if getattr(exc, "filename", None) is None:
            return ValueError(CORRUPT_ERROR)
        return None
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
class ZipHandler(ArchiveHandler):

    @classmethod
    def format_name(cls) -> str:
        return "zip"

    @classmethod
    def extensions(cls) -> list[str]:
        return [".zip"]

    @classmethod
    def supports_password(cls) -> bool:
        return True

    @classmethod
    def detect(cls, filepath: str) -> bool:
        ext = Path(filepath).suffix.lower()
        double = "".join(Path(filepath).suffixes[-2:]).lower() if len(Path(filepath).suffixes) >= 2 else ""
        if ext == ".zip" or double == ".zip.001":
            return True
        try:
            with open(filepath, "rb") as f:
                return f.read(2) == b"PK"
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
                wrapped_format=ZipHandler.format_name(),
            )
        file_paths = [str(p) for p in (file_paths or [])]
        # 先校验输入：不存在的路径必须报错，绝不能静默跳过
        missing = [p for p in file_paths if not os.path.exists(p)]
        if missing:
            raise FileNotFoundError("找不到要压缩的文件或文件夹: " + ", ".join(missing))
        common_parent = ZipHandler._get_arcroot(file_paths)
        entries = [(fp, str(Path(fp).relative_to(common_parent)))
                   for p in file_paths for fp in ZipHandler._walk_files(p)]
        if not entries:
            raise ValueError("没有可压缩的文件：所选内容为空")

        if password:
            try:
                import pyzipper
            except ImportError as e:
                raise ValueError("当前环境缺少 pyzipper，无法创建 AES 加密压缩包") from e
            zf_cls = pyzipper.AESZipFile
            zf_kwargs = {"compression": pyzipper.ZIP_DEFLATED,
                         "encryption": pyzipper.WZ_AES}
        else:
            zf_cls = zipfile.ZipFile
            zf_kwargs = {"compression": zipfile.ZIP_DEFLATED}

        reporter = _ProgressReporter(progress_callback, len(entries))
        processed = 0
        # 先写同目录的 .part，整包成功后再原子替换，失败绝不破坏用户已有归档
        tmp_path = output_path + ".part"
        try:
            with zf_cls(tmp_path, "w", **zf_kwargs) as zf:
                if password:
                    zf.setpassword(password.encode("utf-8"))
                for fp, arcname in entries:
                    zf.write(fp, arcname)
                    processed += 1
                    reporter.advance(processed)
            try:
                os.replace(tmp_path, output_path)
            except PermissionError as e:
                raise PermissionError(
                    f"无法写入目标文件（可能被其他程序占用）: {output_path}") from e
        except Exception:
            log_exception(f"压缩失败: {output_path}")
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
    def decompress(archive_path, dest_dir, password=None, progress_callback=None):
        zf_cls = _reader_class()
        pwd = password.encode("utf-8") if password else None
        os.makedirs(dest_dir, exist_ok=True)
        # 先解到目标目录下的临时目录，整包成功后再并入；失败不留半个文件
        tmp_dir = tempfile.mkdtemp(prefix=".liwen_zip_", dir=dest_dir)
        total = 0
        reporter: Optional[_ProgressReporter] = None
        try:
            try:
                with zf_cls(archive_path, "r") as zf:
                    members = zf.infolist()
                    total = len(members)
                    reporter = _ProgressReporter(progress_callback, max(total, 1))
                    if pwd is None and any(info.flag_bits & 0x1 for info in members):
                        raise ValueError(NEED_PASSWORD_ERROR)
                    for i, member in enumerate(members):
                        zf.extract(member, tmp_dir, pwd=pwd)
                        reporter.advance(i + 1)
            except Exception as e:
                log_exception(f"解压失败: {archive_path}")
                clean = _translate_zip_error(e)
                if clean is None:
                    raise
                raise clean from e
            _merge_tree(tmp_dir, dest_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        if reporter is not None:
            reporter.finish()
        return {"dest": dest_dir, "file_count": total}

    @staticmethod
    def list_contents(archive_path: str) -> list[dict]:
        """列出归档内容；压缩包损坏时抛出可读错误（不再静默返回空列表）。"""
        zf_cls = _reader_class()
        try:
            with zf_cls(archive_path, "r") as zf:
                return [
                    {
                        "name": info.filename,
                        "size": info.file_size,
                        "compressed_size": info.compress_size,
                        "modified": info.date_time,
                        "is_dir": info.is_dir(),
                    }
                    for info in zf.infolist()
                ]
        except Exception as e:
            log_exception(f"读取压缩包目录失败: {archive_path}")
            clean = _translate_zip_error(e)
            if clean is None:
                raise
            raise clean from e

    @staticmethod
    def extract_single(archive_path, member_name, dest_dir, password=None):
        zf_cls = _reader_class()
        pwd = password.encode("utf-8") if password else None
        os.makedirs(dest_dir, exist_ok=True)
        # 同样先解到临时目录，避免校验失败时留下半个文件
        tmp_dir = tempfile.mkdtemp(prefix=".liwen_zip_", dir=dest_dir)
        try:
            try:
                with zf_cls(archive_path, "r") as zf:
                    if member_name not in zf.namelist():
                        raise ValueError(f"压缩包中未找到: {member_name}")
                    if pwd is None and zf.getinfo(member_name).flag_bits & 0x1:
                        raise ValueError(NEED_PASSWORD_ERROR)
                    zf.extract(member_name, tmp_dir, pwd=pwd)
            except Exception as e:
                log_exception(f"提取单个文件失败: {archive_path} -> {member_name}")
                clean = _translate_zip_error(e)
                if clean is None:
                    raise
                raise clean from e
            _merge_tree(tmp_dir, dest_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return os.path.join(dest_dir, member_name)

    @staticmethod
    def is_encrypted(archive_path: str) -> bool:
        zf_cls = _reader_class()
        try:
            with zf_cls(archive_path, "r") as zf:
                return any(info.flag_bits & 0x1 for info in zf.infolist())
        except Exception:
            return False
