"""Optional external music sources (VK Music, YouTube Music)."""
from __future__ import annotations

import http.client
import socket
import subprocess
import urllib.request
from typing import Any, Optional, Tuple

_DEFAULT_URLOPEN = urllib.request.urlopen


def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Resolve host via IPv4 (AF_INET) without monkey-patching socket.getaddrinfo globally."""
    target_family = socket.AF_INET if family in (0, socket.AF_UNSPEC) else family
    return socket.getaddrinfo(host, port, target_family, type, proto, flags)


def ipv4_create_connection(
    address: Tuple[str, int],
    timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address: Optional[Tuple[str, int]] = None,
) -> socket.socket:
    """Connect using IPv4-only resolution to avoid dead IPv6 DNS timeouts on Android."""
    host, port = address
    err: Optional[BaseException] = None
    for res in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        af, socktype, proto, _, sa = res
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            err = None
            return sock
        except OSError as exc:
            err = exc
            if sock is not None:
                sock.close()
    if err is not None:
        raise err
    raise OSError("getaddrinfo returns an empty list")


class _IPv4HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = ipv4_create_connection


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = ipv4_create_connection


class _IPv4HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request):
        return self.do_open(_IPv4HTTPConnection, req)


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request):
        return self.do_open(
            _IPv4HTTPSConnection,
            req,
            context=self._context,
            check_hostname=self._check_hostname,
        )


def build_ipv4_opener(*handlers: Any) -> urllib.request.OpenerDirector:
    """Build a scoped urllib opener using IPv4-only HTTP/HTTPS transport handlers."""
    return urllib.request.build_opener(_IPv4HTTPHandler, _IPv4HTTPSHandler, *handlers)


_IPV4_OPENER = build_ipv4_opener()


def is_urlopen_mocked() -> bool:
    """Return True if urllib.request.urlopen has been monkeypatched by a test."""
    return (
        urllib.request.urlopen is not _DEFAULT_URLOPEN
        or getattr(urllib.request.urlopen, "__module__", "") != "urllib.request"
    )


def ipv4_urlopen(req: Any, timeout: float = 10.0):
    """Scoped IPv4 HTTP/HTTPS opener respecting urllib.request.urlopen test mocks."""
    if is_urlopen_mocked():
        return urllib.request.urlopen(req, timeout=timeout)
    return _IPV4_OPENER.open(req, timeout=timeout)


def close_adb_forward(adb: Any, port: Optional[int]) -> bool:
    """Close a previously forwarded local ADB port if active."""
    if port is None or adb is None or getattr(adb, "is_local", False):
        return False
    close_fn = getattr(adb, "close_forward", None)
    if callable(close_fn):
        try:
            return bool(close_fn(int(port)))
        except Exception:
            return False
    if getattr(adb, "adb", None) == "pyadb":
        try:
            import pyadb
            return bool(pyadb.close_forward(int(port)))
        except Exception:
            return False
    base_fn = getattr(adb, "_base", None)
    if callable(base_fn):
        try:
            subprocess.run(
                list(base_fn()) + ["forward", "--remove", f"tcp:{int(port)}"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            return True
        except Exception:
            return False
    return False
