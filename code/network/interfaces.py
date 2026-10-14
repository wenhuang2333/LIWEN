"""LAN interface enumeration with virtual adapter filtering.

Filters out VMware, Hyper-V, Docker, WSL, VirtualBox, ZeroTier, Tailscale,
and other virtual/loopback/APIPA adapters. Returns only real LAN interfaces
with their IP, netmask, and directed broadcast address.
"""

import ipaddress
import socket
import struct
import sys

# Interface name patterns to exclude (case-insensitive substring match)
_EXCLUDE_PATTERNS = [
    "vmware", "virtualbox", "hyper-v", "docker", "wsl",
    "zerotier", "tailscale", "loopback", "tunnel", "teredo",
    "isatap", "6to4", "bluetooth", "wintun", "tap-", "vpn",
    "radmin", "hamachi", "openvpn", "wireguard", "pseudo",
    "software loopback",
]

# IP prefixes to exclude
_EXCLUDE_IP_PREFIXES = [
    "127.",        # loopback
    "169.254.",    # APIPA / link-local
    "0.",          # null
]


def _is_virtual_name(name: str) -> bool:
    lower = name.lower()
    return any(pat in lower for pat in _EXCLUDE_PATTERNS)


def _is_valid_ip(ip_str: str) -> bool:
    """判断是否是可用于局域网传输的本机地址。

    原实现要求 ``addr.is_private``，这会漏掉两类真实场景：

    * **CGNAT 100.64.0.0/10** —— 运营商/校园网/部分热点分配的就是这个段
      （RFC 6598），Python 的 ``is_private`` 为 False，于是这类网络下
      "局域网传输"完全找不到本机 IP；
    * **同网段的公网地址** —— 例如 203.0.113.0/24 这类机构内网直连公网段，
      网卡本身可直连对端，却因为不是 private 被排除。

    因此这里改成"排除法"：只要是合法的单播 IPv4、不是回环/链路本地/
    未指定/组播/保留地址，就认为可用。
    """
    for prefix in _EXCLUDE_IP_PREFIXES:
        if ip_str.startswith(prefix):
            return False
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if addr.version != 4:
        return False
    # 排除法：只剔除明确不可用的特殊地址。
    # 注意 100.64.0.0/10 (CGNAT) 在 Python 里 is_private=False 且
    # is_reserved=False，因此会被保留 —— 这正是我们要的结果。
    if addr.is_loopback or addr.is_link_local or addr.is_multicast:
        return False
    if addr.is_unspecified or addr.is_reserved:
        return False
    return True


def get_lan_interfaces() -> list[dict]:
    """Return list of real LAN interfaces.

    Each entry: {
        "name": "Wi-Fi",
        "ip": "192.168.31.15",
        "netmask": "255.255.255.0",
        "prefix": 24,
        "network": "192.168.31.0/24",
        "broadcast": "192.168.31.255",
    }
    """
    interfaces = []

    try:
        import psutil
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
    except ImportError:
        # Fallback: use socket-based approach
        return _fallback_get_interface()

    for name, snic_addrs in addrs.items():
        if _is_virtual_name(name):
            continue
        if name not in stats:
            continue
        stat = stats[name]
        if not stat.isup:
            continue

        for snic in snic_addrs:
            if snic.family != socket.AF_INET:
                continue
            ip_str = snic.address
            mask_str = snic.netmask or "255.255.255.0"

            if not _is_valid_ip(ip_str):
                continue

            try:
                net = ipaddress.IPv4Network(
                    f"{ip_str}/{mask_str}", strict=False)
                interfaces.append({
                    "name": name,
                    "ip": ip_str,
                    "netmask": mask_str,
                    "prefix": net.prefixlen,
                    "network": str(net),
                    "broadcast": str(net.broadcast_address),
                })
            except (ValueError, ipaddress.AddressValueError):
                continue

    return interfaces


def _fallback_get_interface() -> list[dict]:
    """Fallback when psutil is unavailable — returns the primary interface only."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and _is_valid_ip(ip):
            return [{
                "name": socket.gethostname(),
                "ip": ip,
                "netmask": "255.255.255.0",
                "prefix": 24,
                "network": f"{'.'.join(ip.split('.')[:3])}.0/24",
                "broadcast": f"{'.'.join(ip.split('.')[:3])}.255",
            }]
    except Exception:
        pass
    return []


def get_primary_interface() -> dict | None:
    """Return the primary (first) LAN interface, or None."""
    ifaces = get_lan_interfaces()
    return ifaces[0] if ifaces else None


def get_local_ip() -> str:
    iface = get_primary_interface()
    if iface:
        return iface["ip"]
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


def get_all_broadcast_addresses() -> list[str]:
    """Return all directed broadcast addresses for LAN interfaces."""
    return [iface["broadcast"] for iface in get_lan_interfaces()
            if iface.get("broadcast")]
