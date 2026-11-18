"""Native playback integration for Yandex Station Max.

Injects external audio stream URLs (YouTube Music, etc.) directly into
Station Max's official GStreamer media pipeline (yio_souphttpsrc).
This allows external music to play through the station's official native audio
pipeline, driving the hardware front display visualizer (spectrogramAnimation)
100% natively without any fake framebuffers or custom display hacks.

Supports instant seeking via DASH sidx fragment splicing through a lightweight
local streaming proxy (http://127.0.0.1:8788/stream.mp4?seek=<sec>).
"""
from __future__ import annotations

import base64
import http.client
import http.server
import logging
import os
import re
import shlex
import socketserver
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import ipv4_urlopen
from .fast_yt import VISIONOS_USER_AGENT

_log = logging.getLogger("yapanel.gst")

OVERRIDE_PATH = "/data/data/com.yandex.io.sdk/files/override_url.txt"
PID_TRACK_PATH = "/data/data/com.yandex.io.sdk/files/injected_pid.txt"
USER_SEEK_FLAG = "/data/data/com.yandex.io.sdk/files/user_seek.txt"
HOOK_SCRIPT = "/data/local/tmp/hook_location.js"
FALLBACK_TRACK_ID = "153109827"
PROXY_PORT = 8788
_NON_RETRYABLE_HTTP = (400, 401, 404, 410, 416)


class GstBridgeError(ValueError):
    pass


def _open_range(
    url: str,
    start: int,
    end: Optional[int] = None,
    attempts: int = 12,
    timeout: float = 3.0,
    max_elapsed: Optional[float] = None,
):
    """Open an upstream byte-range stream via IPv4, retrying transient stalls and multi-exit 403s."""
    range_val = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
    deadline = (time.monotonic() + max_elapsed) if max_elapsed is not None else None
    user_agent: Optional[str] = VISIONOS_USER_AGENT
    last_err: Optional[BaseException] = None
    total_attempts = max(1, attempts)
    for attempt in range(total_attempts):
        req_timeout = timeout
        if deadline is not None:
            rem = deadline - time.monotonic()
            if rem <= 0.2 and attempt > 0:
                break
            req_timeout = min(timeout, max(0.5, rem))
        headers = {"Range": range_val}
        if user_agent is not None:
            headers["User-Agent"] = user_agent
        req = urllib.request.Request(url, headers=headers)
        try:
            return ipv4_urlopen(req, timeout=req_timeout)
        except urllib.error.HTTPError as err:
            if err.code in _NON_RETRYABLE_HTTP:
                raise
            last_err = err
            if err.code == 403:
                user_agent = None if user_agent is not None else VISIONOS_USER_AGENT
        except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as err:
            last_err = err
        if attempt + 1 < total_attempts:
            time.sleep(0.05)
    assert last_err is not None
    raise last_err


class MP4Streamer:
    """Parses MP4 DASH sidx box to allow instantaneous seek via fragment slicing."""

    def __init__(self, youtube_url: str):
        self.url = youtube_url
        self.hdr = b""
        self.segments: List[Tuple[float, int, int, int]] = []  # (time_sec, byte_offset, size, decode_time)
        self.total_size = 0
        self.duration_sec = 0.0
        self.ready = False
        self._lock = threading.Lock()
        self._parse()

    def _read_range(self, start: int, end: int) -> Tuple[bytes, int]:
        total_size = 0
        with _open_range(self.url, start, end, attempts=12, timeout=2.0) as resp:
            chunk = resp.read()
            cr = resp.headers.get("Content-Range")
            if cr and "/" in cr:
                try:
                    total_size = int(cr.split("/")[-1])
                except ValueError:
                    pass
        return chunk, total_size

    def _parse(self) -> None:
        total_size = 0
        m = re.search(r"clen=(\d+)", self.url)
        if m:
            total_size = int(m.group(1))
        try:
            data, cr_total = self._read_range(0, 32767)
            if cr_total:
                total_size = cr_total
        except Exception as err:  # noqa: BLE001
            _log.warning("MP4Streamer initial range failed: %s", err)
            return

        offset = 0
        sidx_data = None
        hdr_end = 0
        while True:
            if offset + 8 > len(data):
                break
            size, tag = struct.unpack(">I4s", data[offset:offset + 8])
            tag_str = tag.decode("latin1", "replace")
            if size == 1:
                if offset + 16 > len(data):
                    break
                size = struct.unpack(">Q", data[offset + 8:offset + 16])[0]
            if size < 8:
                break
            box_end = offset + size
            if tag_str == "sidx":
                if box_end > len(data):
                    try:
                        extra, _ = self._read_range(len(data), box_end - 1)
                        data += extra
                    except Exception as err:  # noqa: BLE001
                        _log.warning("MP4Streamer sidx tail fetch failed: %s", err)
                        return
                if box_end > len(data):
                    _log.warning("MP4Streamer sidx truncated (%d > %d)", box_end, len(data))
                    return
                sidx_data = data[offset:box_end]
                hdr_end = offset
                break
            if box_end > len(data) and tag_str in ("moov", "ftyp", "free") and box_end <= 262144:
                try:
                    extra, _ = self._read_range(len(data), box_end + 16383)
                    if not extra:
                        break
                    data += extra
                except Exception as err:  # noqa: BLE001
                    _log.warning("MP4Streamer header fetch failed: %s", err)
                    return
            offset = box_end

        if not sidx_data or hdr_end == 0:
            _log.warning("MP4Streamer sidx box not found (bytes=%d)", len(data))
            return

        hdr = data[:hdr_end]
        segments: List[Tuple[float, int, int, int]] = []
        duration_sec = 0.0
        ready = False

        try:
            size, tag, version_flags = struct.unpack(">I4sI", sidx_data[:12])
            version = version_flags >> 24
            ref_id, timescale = struct.unpack(">II", sidx_data[12:20])
            if version == 0:
                earliest_time, first_offset = struct.unpack(">II", sidx_data[20:28])
                s_off = 28
            else:
                earliest_time, first_offset = struct.unpack(">QQ", sidx_data[20:36])
                s_off = 36
            reserved, count = struct.unpack(">HH", sidx_data[s_off:s_off + 4])
            s_off += 4

            cur_time = earliest_time
            cur_byte = hdr_end + size + first_offset
            for _ in range(count):
                ref_info, subseg_dur, sap_info = struct.unpack(">III", sidx_data[s_off:s_off + 12])
                subseg_size = ref_info & 0x7FFFFFFF
                time_sec = cur_time / timescale
                segments.append((time_sec, cur_byte, subseg_size, cur_time))
                cur_time += subseg_dur
                cur_byte += subseg_size
                s_off += 12

            duration_sec = cur_time / timescale
            if not total_size and segments:
                total_size = segments[-1][1] + segments[-1][2]
            ready = True
        except Exception:
            pass

        with self._lock:
            self.hdr = hdr
            self.segments = segments
            self.total_size = total_size
            self.duration_sec = duration_sec
            self.ready = ready

    def snapshot(self) -> Tuple[bool, bytes, int, float, List[Tuple[float, int, int, int]]]:
        with self._lock:
            return self.ready, self.hdr, self.total_size, self.duration_sec, list(self.segments)

    def get_seek_offset(self, target_sec: float) -> Tuple[int, float, int]:
        with self._lock:
            if not self.segments or target_sec <= 0:
                return 0, 0.0, 0
            best_seg = self.segments[0]
            for seg in self.segments:
                if seg[0] <= target_sec:
                    best_seg = seg
                else:
                    break
            return best_seg[1], best_seg[0], best_seg[3]


class _StreamRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    bridge: Optional[GstBridge] = None

    @staticmethod
    def _patch_tfdt_range(chunk: bytearray, start_scan: int, end_scan: int, base_decode_time: int) -> None:
        if base_decode_time <= 0 or start_scan >= end_scan:
            return
        pos = start_scan
        limit = len(chunk)
        while pos < end_scan:
            idx = chunk.find(b"tfdt", pos, end_scan)
            if idx == -1:
                break
            if idx >= 4 and idx + 5 <= limit:
                b_sz = struct.unpack(">I", chunk[idx - 4:idx])[0]
                ver = chunk[idx + 4]
                if ver == 0 and b_sz == 16 and idx + 12 <= limit:
                    dt = struct.unpack(">I", chunk[idx + 8:idx + 12])[0]
                    struct.pack_into(">I", chunk, idx + 8, max(0, dt - base_decode_time))
                elif ver == 1 and b_sz == 20 and idx + 16 <= limit:
                    dt = struct.unpack(">Q", chunk[idx + 8:idx + 16])[0]
                    struct.pack_into(">Q", chunk, idx + 8, max(0, dt - base_decode_time))
            pos = idx + 4

    @classmethod
    def _patch_tfdt(cls, chunk: bytearray, base_decode_time: int) -> None:
        cls._patch_tfdt_range(chunk, 0, len(chunk), base_decode_time)

    @staticmethod
    def _extract_clen(url: str) -> int:
        m = re.search(r"(?:[?&])clen=(\d+)", url)
        return int(m.group(1)) if m else 0

    @staticmethod
    def _open_upstream(
        bridge: "GstBridge",
        primary_url: str,
        start: int,
        end: Optional[int] = None,
        attempts: int = 12,
        timeout: float = 2.5,
        max_elapsed: float = 6.0,
    ):
        try:
            return _open_range(primary_url, start, end, attempts=attempts, timeout=timeout, max_elapsed=max_elapsed)
        except urllib.error.HTTPError as first_err:
            if first_err.code == 416:
                raise
            last_err: BaseException = first_err
        except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as first_err:
            last_err = first_err

        with bridge._lock:
            backups = [u for u in (getattr(bridge, "_fallback_urls", None) or []) if u and u != primary_url]
        for alt_url in backups:
            try:
                resp = _open_range(alt_url, start, end, attempts=4, timeout=timeout, max_elapsed=max_elapsed)
                with bridge._lock:
                    if bridge._last_url == primary_url:
                        bridge._last_url = alt_url
                        bridge._fallback_urls = [u for u in bridge._fallback_urls if u != alt_url]
                return resp
            except urllib.error.HTTPError as alt_err:
                if alt_err.code == 416:
                    raise
                last_err = alt_err
            except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as alt_err:
                last_err = alt_err
        raise last_err

    @staticmethod
    def _parse_upstream_sizes(resp: Any, start: int) -> Tuple[int, Optional[int]]:
        hdrs = getattr(resp, "headers", None) or {}
        cr = str(hdrs.get("Content-Range") or "")
        total = 0
        m = re.search(r"/(\d+)\s*$", cr)
        if m:
            total = int(m.group(1))
        cl_raw = hdrs.get("Content-Length")
        cl = int(cl_raw) if (cl_raw is not None and str(cl_raw).strip().isdigit()) else None
        if total <= 0 and cl is not None and cl > 0:
            total = start + cl
        return total, cl

    def _stream_upstream(self, upstream: Any, bytes_left: Optional[int], base_decode_time: int) -> int:
        if base_decode_time <= 0:
            while bytes_left is None or bytes_left > 0:
                to_read = 65536 if bytes_left is None else min(65536, bytes_left)
                raw = upstream.read(to_read)
                if not raw:
                    break
                self.wfile.write(raw)
                if bytes_left is not None:
                    bytes_left -= len(raw)
            return bytes_left if bytes_left is not None else 0

        prefix = b""
        pending = bytearray()
        while bytes_left is None or bytes_left > 0:
            to_read = 65536 if bytes_left is None else min(65536, bytes_left)
            raw = upstream.read(to_read)
            if not raw:
                break
            if bytes_left is not None:
                bytes_left -= len(raw)
            buf = bytearray(prefix)
            buf.extend(pending)
            buf.extend(raw)
            len_prefix = len(prefix)
            safe_end = max(len_prefix, len(buf) - 20)
            self._patch_tfdt_range(buf, len_prefix, safe_end, base_decode_time)
            if safe_end > len_prefix:
                self.wfile.write(buf[len_prefix:safe_end])
                prefix = bytes(buf[max(0, safe_end - 4):safe_end])
            pending = buf[safe_end:]
        if pending:
            buf = bytearray(prefix)
            buf.extend(pending)
            len_prefix = len(prefix)
            self._patch_tfdt_range(buf, len_prefix, len(buf), base_decode_time)
            self.wfile.write(buf[len_prefix:])
        return bytes_left if bytes_left is not None else 0

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress default console clutter

    def do_HEAD(self) -> None:
        self._handle(head_only=True)

    def do_GET(self) -> None:
        self._handle(head_only=False)

    def _handle(self, head_only: bool = False) -> None:
        bridge = self.bridge
        if not bridge:
            self.send_response(404)
            self.end_headers()
            return
        with bridge._lock:
            upstream_url = bridge._last_url
            streamer = bridge.streamer
            default_seek_sec = getattr(bridge, "_seek_offset_sec", 0.0)
        if not upstream_url:
            self.send_response(404)
            self.end_headers()
            return

        query = self.path.split("?")[-1] if "?" in self.path else ""
        params = dict(qc.split("=") for qc in query.split("&") if "=" in qc)
        if "seek" in params:
            try:
                target_seek_sec = float(params["seek"])
            except ValueError:
                target_seek_sec = default_seek_sec
        else:
            target_seek_sec = default_seek_sec

        byte_off = 0
        base_decode_time = 0
        s_ready = False
        hdr = b""
        total_size = 0
        segments: List[Tuple[float, int, int, int]] = []
        if streamer is not None:
            s_ready, hdr, total_size, _, segments = streamer.snapshot()
            if not s_ready:
                hdr = b""
                total_size = 0
        hdr_len = len(hdr)

        if streamer is not None and s_ready and target_seek_sec > 0:
            byte_off, _, base_decode_time = streamer.get_seek_offset(target_seek_sec)

        if not total_size and segments:
            total_size = segments[-1][1] + segments[-1][2]
        if not total_size and byte_off == 0:
            total_size = self._extract_clen(upstream_url)

        virtual_total = (
            hdr_len + max(0, total_size - byte_off)
            if (byte_off > 0 and total_size > 0)
            else total_size
        )

        # Parse client Range header
        range_header = self.headers.get("Range", "")
        r_start = 0
        req_end: Optional[int] = None
        if range_header.startswith("bytes="):
            parts = range_header[6:].split("-", 1)
            if parts[0].isdigit():
                r_start = int(parts[0])
            if len(parts) > 1 and parts[1].isdigit():
                req_end = int(parts[1])

        if virtual_total > 0 and r_start >= virtual_total:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{virtual_total}")
            self.end_headers()
            return

        if virtual_total > 0:
            r_end: Optional[int] = min(req_end, virtual_total - 1) if req_end is not None else (virtual_total - 1)
        else:
            r_end = req_end

        if head_only and virtual_total == 0:
            try:
                with self._open_upstream(bridge, upstream_url, 0, 0, attempts=8, timeout=2.5, max_elapsed=5.5) as probe:
                    probe_total, _ = self._parse_upstream_sizes(probe, 0)
                if probe_total > 0:
                    virtual_total = probe_total
                    if r_start >= virtual_total:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{virtual_total}")
                        self.end_headers()
                        return
                    r_end = min(req_end, virtual_total - 1) if req_end is not None else (virtual_total - 1)
            except Exception as error:  # noqa: BLE001
                _log.warning("[GST_PROXY] Upstream probe error: %s", error)
                self.send_response(502)
                self.end_headers()
                return

        content_length: Optional[int] = max(0, r_end - r_start + 1) if r_end is not None else None
        hdr_slice = (
            hdr[r_start:min(hdr_len, r_start + content_length)]
            if (byte_off > 0 and content_length is not None and r_start < hdr_len and content_length > 0)
            else b""
        )
        upstream_bytes_left: Optional[int] = (content_length - len(hdr_slice)) if content_length is not None else None

        upstream = None
        if not head_only and (upstream_bytes_left is None or upstream_bytes_left > 0):
            cur_after = r_start + len(hdr_slice)
            upstream_start = cur_after if byte_off == 0 else byte_off + (cur_after - hdr_len)
            upstream_end = None if upstream_bytes_left is None else (upstream_start + upstream_bytes_left - 1)
            try:
                upstream = self._open_upstream(
                    bridge, upstream_url, upstream_start, upstream_end, attempts=12, timeout=2.5, max_elapsed=6.0
                )
            except urllib.error.HTTPError as err:
                if err.code == 416:
                    self.send_response(416)
                    if virtual_total > 0:
                        self.send_header("Content-Range", f"bytes */{virtual_total}")
                    self.end_headers()
                    return
                _log.warning("[GST_PROXY] Upstream open HTTP error: %s", err)
                self.send_response(502)
                self.end_headers()
                return
            except Exception as error:  # noqa: BLE001
                _log.warning("[GST_PROXY] Upstream open error: %s", error)
                self.send_response(502)
                self.end_headers()
                return

            if virtual_total == 0 or r_end is None:
                up_total, up_cl = self._parse_upstream_sizes(upstream, upstream_start)
                if virtual_total == 0 and up_total > 0 and byte_off == 0:
                    virtual_total = up_total
                if byte_off == 0:
                    if r_end is None:
                        if up_cl is not None:
                            r_end = r_start + up_cl - 1
                        elif virtual_total > 0:
                            r_end = virtual_total - 1
                    else:
                        if virtual_total > 0:
                            r_end = min(r_end, virtual_total - 1)
                        if up_cl is not None:
                            r_end = min(r_end, r_start + up_cl - 1)
                if r_end is not None:
                    content_length = max(0, r_end - r_start + 1)
                    if byte_off == 0:
                        upstream_bytes_left = content_length

        try:
            if range_header and r_end is not None:
                self.send_response(206)
                total_label = str(virtual_total) if virtual_total > 0 else "*"
                self.send_header("Content-Range", f"bytes {r_start}-{r_end}/{total_label}")
            else:
                self.send_response(200)

            self.send_header("Content-Type", "audio/mp4")
            self.send_header("Accept-Ranges", "bytes")
            if content_length is not None:
                self.send_header("Content-Length", str(content_length))
                self.send_header("Connection", "keep-alive")
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()

            if head_only:
                return
            if r_start == 0:
                with bridge._lock:
                    if bridge._is_active and not bridge._is_paused:
                        bridge._seek_start_time = time.time() + 0.35
                        bridge._stream_started = True
            if hdr_slice:
                self.wfile.write(hdr_slice)
            if upstream is not None and (upstream_bytes_left is None or upstream_bytes_left > 0):
                rem = self._stream_upstream(upstream, upstream_bytes_left, base_decode_time)
                if rem > 0:
                    self.close_connection = True
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as error:  # noqa: BLE001
            print(f"[GST_PROXY] Stream error: {error}", flush=True)
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except Exception:
                    pass


class _ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        super().handle_error(request, client_address)


class GstBridge:
    def __init__(self, panel: Any = None) -> None:
        self.panel = panel
        self._lock = threading.RLock()
        self._stream_gen = 0
        self._last_injected_pid: Optional[int] = None
        self._is_active: bool = False
        self._is_paused: bool = False
        self._last_title: str = ""
        self._last_artist: str = ""
        self._last_url: str = ""
        self._fallback_urls: List[str] = []
        self._seek_offset_sec: float = 0.0
        self._seek_start_time: float = 0.0
        self._stream_started: bool = False
        self._paused_pos: float = 0.0
        self._track_duration: float = 0.0
        self.streamer: Optional[MP4Streamer] = None
        self._streamer_done: threading.Event = threading.Event()
        self._streamer_done.set()
        self._server: Optional[_ReusableTCPServer] = None
        self._ensure_server()

    def _ensure_server(self) -> None:
        """Start the local HTTP streaming proxy, falling back to an ephemeral port if PROXY_PORT is busy."""
        with self._lock:
            if self._server is not None:
                return
            handler_cls = type("_BoundStreamHandler", (_StreamRequestHandler,), {"bridge": self})
            ports = (PROXY_PORT, 0) if PROXY_PORT else (0,)
            for idx, candidate in enumerate(ports):
                try:
                    self._server = _ReusableTCPServer(("127.0.0.1", candidate), handler_cls)
                    t = threading.Thread(target=self._server.serve_forever, daemon=True, name="gst-proxy-server")
                    t.start()
                    if candidate == 0 and PROXY_PORT:
                        bound = int(self._server.server_address[1] or 0)
                        _log.warning("[GST_PROXY] Bound fallback ephemeral port %d instead of busy %d", bound, PROXY_PORT)
                    return
                except Exception as error:  # noqa: BLE001
                    if idx == 0 and len(ports) > 1:
                        _log.warning("[GST_PROXY] Port %d unavailable (%s), retrying on ephemeral port", candidate, error)
                    else:
                        _log.warning("[GST_PROXY] Failed to start local proxy server: %s", error)

    def close(self) -> None:
        """Stop the local streaming proxy TCP server and release its listening socket."""
        with self._lock:
            srv = self._server
            self._server = None
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass

    shutdown = close

    def _exec_result(self, cmd: str) -> Tuple[str, str, int]:
        """Run a shell command locally on device or via panel.adb and return (stdout, stderr, rc)."""
        if getattr(type(self), "_exec", None) is not GstBridge._exec or self.__dict__.get("_exec") is not None:
            out = self._exec(cmd)
            return str(out or ""), "", 0
        if os.environ.get("PANEL_LOCAL") == "1" or not hasattr(self.panel, "adb"):
            try:
                res = subprocess.run(
                    ["sh", "-c", cmd],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    check=False,
                )
                return (
                    res.stdout.decode("utf-8", errors="replace").strip(),
                    res.stderr.decode("utf-8", errors="replace").strip(),
                    int(res.returncode),
                )
            except Exception as exc:
                return "", str(exc), 1
        try:
            out, err, rc = self.panel.adb.shell(cmd, check=False, timeout=10)
            return (out or "").strip(), (err or "").strip(), int(rc if rc is not None else 0)
        except Exception as exc:
            return "", str(exc), 1

    def _exec(self, cmd: str) -> str:
        """Run a shell command locally on device or via panel.adb."""
        out, _, _ = self._exec_result(cmd)
        return out

    def _get_sdk_pid(self) -> Optional[int]:
        out = self._exec("pidof com.yandex.io.sdk")
        for part in out.split():
            if part.isdigit():
                return int(part)
        return None

    def available(self, max_age: float = 8.0) -> bool:
        return self._get_sdk_pid() is not None

    def ensure_injected(self) -> bool:
        pid = self._get_sdk_pid()
        if not pid:
            return False
        with self._lock:
            if self._last_injected_pid == pid:
                return True

        # Fast check if frida-agent is currently mapped into SDK memory
        in_mem = self._exec(f"grep -q frida-agent /proc/{pid}/maps && echo 1 || echo 0")
        if in_mem.strip() == "1":
            with self._lock:
                self._last_injected_pid = pid
            return True

        # Ensure hook script is present and up-to-date on the device
        q_hook = shlex.quote(HOOK_SCRIPT)
        src_hook = "/data/yapanel/device/extmusic/hook_location.js"
        _, _, hook_rc = self._exec_result(
            f"if [ -f {src_hook} ] && {{ [ ! -f {q_hook} ] || ! cmp -s {src_hook} {q_hook}; }}; "
            f"then cp {src_hook} {q_hook} && chmod 666 {q_hook}; fi; "
            f"[ -f {q_hook} ]"
        )
        if hook_rc != 0:
            return False

        # Find frida-inject on the target device and verify it is executable
        frida_out, _, frida_rc = self._exec_result(
            "if [ -x /data/local/tmp/frida-inject ]; then echo /data/local/tmp/frida-inject; "
            "elif [ -f /data/local/tmp/frida-inject ]; then chmod 755 /data/local/tmp/frida-inject 2>/dev/null; "
            "[ -x /data/local/tmp/frida-inject ] && echo /data/local/tmp/frida-inject; "
            "else for f in /data/app/com.yandex.io.sdk*/lib/arm64/frida-inject; do "
            "if [ -x \"$f\" ]; then echo \"$f\"; break; fi; done; fi"
        )
        frida_bin = frida_out.splitlines()[0].strip() if frida_out.strip() else ""
        if frida_rc != 0 or not frida_bin:
            return False

        # Inject hook_location.js with -e (eternalize and exit)
        cmd = (
            f"chmod 777 /data/data/com.yandex.io.sdk/files 2>/dev/null; "
            f"{shlex.quote(frida_bin)} -p {int(pid)} -s {q_hook} -e"
        )
        out, err, rc = self._exec_result(cmd)
        combined = f"{out}\n{err}".lower()
        if rc != 0 or any(tok in combined for tok in ("not found", "permission denied", "unable to", "failed", "error:")):
            return False
        time.sleep(0.3)
        with self._lock:
            self._last_injected_pid = pid
        return True

    def _send_glagol(self, payload: Dict[str, Any]) -> None:
        """Send command directly to Glagol daemon without recursion."""
        glagol = getattr(self.panel, "glagol", None)
        if glagol and hasattr(glagol, "send"):
            glagol.send(payload)

    def _write_device_file(self, path: str, content: str) -> None:
        written = False
        if os.environ.get("PANEL_LOCAL") == "1" or not hasattr(self.panel, "adb"):
            try:
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                os.chmod(path, 0o666)
                written = True
            except Exception:
                pass
        if not written:
            b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
            q_path = shlex.quote(path)
            self._exec(f"echo -n '{b64}' | base64 -d > {q_path} && chmod 666 {q_path}")

    def _write_override(self, url: str) -> None:
        self._write_device_file(OVERRIDE_PATH, url)

    def _init_streamer(self, url: str) -> None:
        with self._lock:
            gen = self._stream_gen
            cur_url = self._last_url
            is_active = self._is_active
            done_evt = self._streamer_done
            backups = [u for u in self._fallback_urls if u and u != url]
        try:
            if cur_url and cur_url != url:
                return
            streamer = None
            resolved_url = url
            for cand in [url, *backups]:
                try:
                    cand_streamer = MP4Streamer(cand)
                except Exception as error:
                    print(f"[GST] Failed to parse MP4Streamer: {error}", flush=True)
                    cand_streamer = None
                if cand_streamer is not None and getattr(cand_streamer, "ready", False):
                    streamer = cand_streamer
                    resolved_url = cand
                    break
            if streamer is None or not getattr(streamer, "ready", False):
                return
            with self._lock:
                if self._stream_gen != gen:
                    return
                if self._last_url == url or (not self._last_url and not is_active and gen == 0):
                    # Never overwrite a healthy streamer already resolved by an urgent seek()
                    if not (self.streamer is not None and getattr(self.streamer, "ready", False)):
                        self.streamer = streamer
                        if resolved_url != url and self._last_url == url:
                            self._last_url = resolved_url
                            self._fallback_urls = [u for u in self._fallback_urls if u != resolved_url]
        finally:
            done_evt.set()

    def _proxy_url(self, seek_sec: float = 0.0) -> Optional[str]:
        with self._lock:
            srv = self._server
        if srv is None or not getattr(srv, "server_address", None):
            return None
        port = int(srv.server_address[1] or 0)
        if port <= 0:
            return None
        return f"http://127.0.0.1:{port}/stream.mp4?seek={seek_sec}&t={int(time.time() * 1000)}"

    def _restart_glagol_playback(self, context: str) -> None:
        try:
            self._send_glagol({"command": "stop"})
        except Exception:
            pass
        time.sleep(0.06)
        try:
            self._send_glagol({"command": "playMusic", "id": FALLBACK_TRACK_ID, "type": "track"})
        except Exception as error:
            print(f"[GST] Glagol playMusic failed on {context}: {error}", flush=True)

    def play(
        self,
        url: str,
        title: str = "",
        artist: str = "",
        duration: int = 0,
        fallback_urls: Optional[List[str]] = None,
    ) -> int:
        if not url:
            raise GstBridgeError("Пустой адрес потока")
        self._ensure_server()
        if not self.ensure_injected():
            self.release()
            raise GstBridgeError("Не удалось подключить аудио-хук (frida-inject)")
        backups = [str(u) for u in (fallback_urls or []) if u and str(u) != url][:4]
        proxy_url = self._proxy_url(0.0)
        with self._lock:
            self._stream_gen += 1
            self._last_url = url
            self._fallback_urls = backups
            self._last_title = title
            self._last_artist = artist
            self._track_duration = float(duration or 0)
            self._seek_offset_sec = 0.0
            self._paused_pos = 0.0
            self._seek_start_time = time.time()
            self._stream_started = proxy_url is None
            self.streamer = None
            self._streamer_done = threading.Event()
            self._is_active = True
            self._is_paused = False

        # Parse sidx in background thread for instant seek readiness
        threading.Thread(target=self._init_streamer, args=(url,), daemon=True).start()

        # 1. Route initial playback through local retry proxy when bound (avoids 403 / libsoup stalls)
        self._write_override(proxy_url or url)

        # 2. Trigger native playback via Glagol
        try:
            self._send_glagol({"command": "playMusic", "id": FALLBACK_TRACK_ID, "type": "track"})
        except Exception as error:
            self.release()
            raise GstBridgeError(f"Ошибка запуска родного плеера: {error}") from error

        return 1

    def pause(self) -> int:
        with self._lock:
            if not self._is_paused:
                self._is_paused = True
                now = time.time()
                if not self._stream_started and self._seek_start_time > 0 and (now - self._seek_start_time) < 8.0:
                    self._paused_pos = self._seek_offset_sec
                elif self._seek_offset_sec > 0:
                    self._paused_pos = self._seek_offset_sec + max(0.0, now - self._seek_start_time)
                else:
                    self._paused_pos = max(0.0, now - self._seek_start_time) if self._seek_start_time > 0 else 0.0
        try:
            self._send_glagol({"command": "stop"})
        except Exception:
            pass
        return 1

    def resume(self) -> int:
        with self._lock:
            self._is_paused = False
            self._seek_offset_sec = self._paused_pos
            self._seek_start_time = time.time()
            pos_ms = int(self._paused_pos * 1000)
        return self.seek(pos_ms)

    def _clear_override(self) -> None:
        self._write_override("")

    def release(self) -> int:
        """Reset local GstBridge state and clear override URL without stopping Glagol."""
        with self._lock:
            self._stream_gen += 1
            self._is_active = False
            self._is_paused = False
            self._last_url = ""
            self._fallback_urls = []
            self._seek_offset_sec = 0.0
            self._paused_pos = 0.0
            self._seek_start_time = 0.0
            self._stream_started = False
            self.streamer = None
            self._streamer_done.set()
        self._clear_override()
        return 1

    def stop(self) -> int:
        self.release()
        try:
            self._send_glagol({"command": "stop"})
        except Exception:
            pass
        return 1

    def seek(self, position_ms: int) -> int:
        self._ensure_server()
        if not self.ensure_injected():
            raise GstBridgeError("Не удалось подключить аудио-хук (frida-inject)")
        sec = max(0.0, float(position_ms) / 1000.0)

        with self._lock:
            last_url = self._last_url
            streamer = self.streamer
            gen = self._stream_gen
            done_evt = self._streamer_done

        # If seeking to beginning, reload via local retry proxy when bound
        if sec <= 0.5:
            proxy_url = self._proxy_url(0.0)
            with self._lock:
                self._seek_offset_sec = 0.0
                self._paused_pos = 0.0
                self._seek_start_time = time.time()
                self._stream_started = proxy_url is None
                self._is_paused = False
                last_url = self._last_url
            if not last_url:
                return 0
            self._write_override(proxy_url or last_url)
            self._restart_glagol_playback("seek to 0")
            return 1

        # Wait briefly if background init is in flight, then retry MP4Streamer if still unready
        if (streamer is None or not getattr(streamer, "ready", False)) and not done_evt.is_set():
            done_evt.wait(timeout=3.5)
            with self._lock:
                streamer = self.streamer

        if (streamer is None or not getattr(streamer, "ready", False)) and last_url:
            try:
                new_streamer = MP4Streamer(last_url)
                if getattr(new_streamer, "ready", False):
                    with self._lock:
                        if self._stream_gen == gen and self._last_url == last_url:
                            self.streamer = new_streamer
                    streamer = new_streamer
            except Exception as error:
                print(f"[GST] Failed to create MP4Streamer: {error}", flush=True)

        if streamer and getattr(streamer, "ready", False) and streamer.segments:
            byte_off, actual_sec, base_decode_time = streamer.get_seek_offset(sec)
            proxy_url = self._proxy_url(actual_sec)
            if proxy_url:
                with self._lock:
                    if self._stream_gen != gen:
                        return 1
                    self._seek_offset_sec = actual_sec
                    self._paused_pos = actual_sec
                    self._seek_start_time = time.time()
                    self._stream_started = False
                    self._is_paused = False
                self._write_override(proxy_url)
                print(f"[GST] Seeking to {sec:.2f}s -> actual {actual_sec:.2f}s (offset {byte_off}, base_dt={base_decode_time}): {proxy_url}", flush=True)
                self._restart_glagol_playback("seek")
                return 1
            _log.warning("[GST] Proxy server unavailable for seek (%.2fs), falling back to Glagol rewind", sec)
            if last_url:
                self._write_override(last_url)

        print(f"[GST] Falling back to Glagol rewind ({sec:.2f}s), streamer={streamer}", flush=True)
        self._write_device_file(USER_SEEK_FLAG, "1")
        with self._lock:
            self._seek_offset_sec = sec
            self._paused_pos = sec
            self._seek_start_time = time.time()
            self._stream_started = True
        try:
            self._send_glagol({"command": "rewind", "position": sec})
        except Exception:
            pass
        return 1

    def player(self) -> Dict[str, Any]:
        glagol = getattr(self.panel, "glagol", None)
        gstate = (getattr(glagol, "state", None) or {}) if glagol else {}
        pstate = gstate.get("playerState") or {}
        is_playing = bool(gstate.get("playing"))
        dur_sec = float(pstate.get("duration") or 0.0)

        with self._lock:
            is_paused = self._is_paused
            is_active = self._is_active
            paused_pos = self._paused_pos
            seek_offset_sec = self._seek_offset_sec
            seek_start_time = self._seek_start_time
            stream_started = self._stream_started
            track_duration = self._track_duration
            streamer = self.streamer

        buffering = False
        now = time.time()
        if is_paused:
            status = "paused"
            is_playing = False
            effective_pos = paused_pos
        elif is_playing or is_active:
            status = "playing"
            is_playing = True
            if (not stream_started and seek_start_time > 0 and (now - seek_start_time) < 8.0) or (
                stream_started and seek_start_time > now
            ):
                buffering = True
                effective_pos = seek_offset_sec
            elif seek_offset_sec > 0:
                effective_pos = seek_offset_sec + max(0.0, now - seek_start_time)
            elif seek_start_time > 0:
                effective_pos = max(0.0, now - seek_start_time)
            else:
                effective_pos = float(pstate.get("progress") or 0.0)
        else:
            status = "stopped"
            effective_pos = 0.0

        streamer_dur = 0.0
        if streamer is not None:
            _, _, _, streamer_dur, _ = streamer.snapshot()
        effective_dur = track_duration if track_duration > 0 else (
            streamer_dur if streamer_dur > 0 else dur_sec
        )

        return {
            "ok": True,
            "state": status,
            "playing": is_playing,
            "buffering": buffering,
            "position": int(effective_pos * 1000),
            "duration": int(effective_dur * 1000),
            "levels": [],
            "error": "",
        }
