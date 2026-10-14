# Import base infrastructure first
from core.formats._base import ArchiveHandler, register_handler
from core.formats._registry import FormatRegistry

# Import all handlers to trigger auto-registration
from core.formats.zip_handler import ZipHandler
from core.formats.seven_z_handler import SevenZHandler
from core.formats.tar_handler import TarHandler
from core.formats.tar_gz_handler import TarGzHandler
from core.formats.tar_bz2_handler import TarBz2Handler
from core.formats.tar_xz_handler import TarXzHandler
from core.formats.shishan_handler import ShishanHandler
from core.formats.custom_handler import CustomHandler
