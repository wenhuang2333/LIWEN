import os
import subprocess
import sys


def create_shortcut(target: str, shortcut_path: str, working_dir: str = "",
                    icon: str = "", description: str = "",
                    run_as_admin: bool = False) -> bool:
    """Create a Windows .lnk shortcut via PowerShell COM.

    Returns True on success, False on failure.
    """
    ps_cmd = (
        f'$ws = New-Object -ComObject WScript.Shell;'
        f'$s = $ws.CreateShortcut("{shortcut_path}");'
        f'$s.TargetPath = "{target}";'
    )
    if working_dir:
        ps_cmd += f'$s.WorkingDirectory = "{working_dir}";'
    if icon and os.path.exists(icon):
        ps_cmd += f'$s.IconLocation = "{icon}";'
    if description:
        ps_cmd += f'$s.Description = "{description}";'
    ps_cmd += '$s.Save()'

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
        )
        if not (result.returncode == 0 and os.path.exists(shortcut_path)):
            return False
    except Exception:
        return False

    if run_as_admin and os.path.exists(shortcut_path):
        try:
            with open(shortcut_path, "r+b") as f:
                f.seek(0x15)
                byte_val = f.read(1)[0]
                f.seek(0x15)
                f.write(bytes([byte_val | 0x20]))
        except Exception:
            pass

    return True


def get_desktop_path() -> str:
    """Return the current user's Desktop folder path."""
    return os.path.join(os.path.expanduser("~"), "Desktop")


def get_start_menu_programs_path() -> str:
    """Return the current user's Start Menu Programs folder."""
    return os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs",
    )
