"""DLNA / UPnP MediaRenderer & HTTP Audio Cast receiver for YapAnel.

Provides zero-install audio casting from BubbleUPnP, mconnect, Symfonium,
Plex, Jellyfin, and local NAS/PC apps directly into the station's native
GStreamer + Silergy SY6045S Hi-Fi DSP audio pipeline.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import logging
import os
import posixpath
import re
import socket
import struct
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

_log = logging.getLogger("yapanel.extmusic.dlna")

DLNA_PORT_DEFAULT = 8789
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

AVTRANSPORT_URN = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING_URN = "urn:schemas-upnp-org:service:RenderingControl:1"
CONNECTION_URN = "urn:schemas-upnp-org:service:ConnectionManager:1"
RENDERER_URN = "urn:schemas-upnp-org:device:MediaRenderer:1"

_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "169.254.169.254",
        "100.100.100.200",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
_XXE_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)

_PROTOCOL_INFO_SINK = ",".join(
    [
        "http-get:*:audio/mpeg:*",
        "http-get:*:audio/mp3:*",
        "http-get:*:audio/mp4:*",
        "http-get:*:audio/m4a:*",
        "http-get:*:audio/aac:*",
        "http-get:*:audio/flac:*",
        "http-get:*:audio/x-flac:*",
        "http-get:*:audio/ogg:*",
        "http-get:*:audio/opus:*",
        "http-get:*:audio/wav:*",
        "http-get:*:audio/x-wav:*",
        "http-get:*:audio/L16;rate=44100;channels=2:*",
        "http-get:*:audio/L16;rate=48000;channels=2:*",
        "http-get:*:audio/*:*",
        "http-get:*:application/ogg:*",
    ]
)


def detect_lan_ip() -> str:
    """Detect the primary LAN IPv4 address without sending packets."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.168.1.1", 80))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


def validate_stream_url(url: str, allow_lan: bool = True) -> str:
    """Validate an external audio/cover URL (allows LAN RFC1918 + public HTTP/HTTPS, blocks loopback/metadata)."""
    raw = str(url or "").strip()
    if not raw or len(raw) > 4096:
        raise ValueError("Некорректный URL потока")
    if any(ord(ch) < 32 or ch.isspace() for ch in raw):
        raise ValueError("URL потока содержит недопустимые символы")
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as err:
        raise ValueError("Некорректный URL потока") from err
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("Разрешены только http:// и https:// ссылки")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not parsed.netloc or not host:
        raise ValueError("В URL не указан сервер")
    try:
        port = parsed.port
    except ValueError as err:
        raise ValueError("Некорректный порт в URL") from err
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("Некорректный порт в URL")
    if host in _BLOCKED_HOSTS:
        raise ValueError("Обращение к служебным адресам запрещено")

    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is None and host.isdigit():
        try:
            ip = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            ip = None

    if ip is not None:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise ValueError("Локальные loopback и link-local адреса запрещены")
        if not allow_lan and ip.is_private:
            raise ValueError("Приватные LAN адреса запрещены")
    return raw


def parse_dlna_duration(text: str) -> int:
    """Parse DLNA duration ('H:MM:SS' or 'HH:MM:SS.fff' or seconds) into integer seconds."""
    raw = str(text or "").strip()
    if not raw:
        return 0
    if raw.upper() == "NOT_IMPLEMENTED":
        return 0
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) == 3:
            try:
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds = float(parts[2])
                return max(0, int(round(hours * 3600 + minutes * 60 + seconds)))
            except ValueError:
                return 0
        if len(parts) == 2:
            try:
                minutes = int(parts[0])
                seconds = float(parts[1])
                return max(0, int(round(minutes * 60 + seconds)))
            except ValueError:
                return 0
    try:
        return max(0, int(round(float(raw))))
    except ValueError:
        return 0


def format_dlna_duration(seconds: float) -> str:
    """Format seconds into UPnP 'H:MM:SS'."""
    total = max(0, int(round(float(seconds or 0))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1].lower()
    if ":" in tag:
        return tag.split(":", 1)[1].lower()
    return tag.lower()


def _fallback_title_from_url(url: str) -> str:
    if not url:
        return "DLNA Поток"
    try:
        path = urllib.parse.urlsplit(url).path
        base = posixpath.basename(urllib.parse.unquote(path or ""))
        if base:
            stem = os.path.splitext(base)[0].strip()
            if stem:
                return stem[:200]
        host = urllib.parse.urlsplit(url).hostname or ""
        if host:
            return f"Поток ({host})"
    except Exception:  # noqa: BLE001
        pass
    return "DLNA Поток"


def parse_didl_metadata(didl_xml: str, fallback_url: str = "") -> Dict[str, Any]:
    """Safely extract title/artist/album/thumb/duration from UPnP DIDL-Lite XML."""
    raw = str(didl_xml or "").strip()
    if raw and not raw.lstrip().startswith("<"):
        raw = html.unescape(raw).strip()
    if raw and _XXE_RE.search(raw):
        raise ValueError("Недопустимый DIDL XML (DOCTYPE/ENTITY запрещены)")

    title = ""
    artist = ""
    album = ""
    thumb = ""
    duration = 0

    if raw:
        try:
            root = ET.fromstring(raw)
            for elem in root.iter():
                name = _local_tag(elem.tag)
                txt = (elem.text or "").strip()
                if name == "title" and txt and not title:
                    title = txt[:300]
                elif name in ("artist", "creator") and txt and not artist:
                    role = str(elem.attrib.get("role") or "").strip().lower()
                    if not role or role in ("albumartist", "performer") or name == "artist":
                        artist = txt[:300]
                elif name == "album" and txt and not album:
                    album = txt[:300]
                elif name == "albumarturi" and txt and not thumb:
                    try:
                        thumb = validate_stream_url(txt, allow_lan=True)[:1000]
                    except ValueError:
                        thumb = ""
                elif name == "res" and not duration:
                    dur_attr = elem.attrib.get("duration") or ""
                    if dur_attr:
                        duration = parse_dlna_duration(dur_attr)
        except ET.ParseError as err:
            raise ValueError(f"Некорректный DIDL XML: {err}") from err

    if not title:
        title = _fallback_title_from_url(fallback_url)
    if not artist:
        artist = "DLNA / Поток"
    return {
        "title": title,
        "artist": artist,
        "album": album,
        "thumb": thumb,
        "duration": duration,
    }


def parse_soap_action(headers: Any, body_bytes: bytes) -> Tuple[str, Dict[str, str]]:
    """Parse UPnP SOAPAction header + XML Envelope body cleanly (rejecting XXE)."""
    raw_body = (body_bytes or b"").decode("utf-8", errors="replace").strip()
    if raw_body and _XXE_RE.search(raw_body):
        raise ValueError("DOCTYPE/ENTITY запрещены в SOAP XML")

    action_name = ""
    soap_hdr = ""
    if headers is not None:
        soap_hdr = str(headers.get("SOAPAction") or headers.get("soapaction") or "").strip()
    if "#" in soap_hdr:
        action_name = soap_hdr.split("#", 1)[1].strip().strip('"').strip("'")

    args: Dict[str, str] = {}
    if raw_body:
        root = ET.fromstring(raw_body)
        body_el = None
        for elem in root:
            if _local_tag(elem.tag) == "body":
                body_el = elem
                break
        if body_el is None:
            body_el = root
        action_el = None
        for child in body_el:
            action_el = child
            break
        if action_el is not None:
            if not action_name:
                action_name = elem_local(action_el.tag)
            for arg in action_el:
                args[arg_local(arg.tag)] = (arg.text or "").strip()

    if not action_name:
        raise ValueError("Не указан SOAPAction")
    return action_name, args


def elem_local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else (tag.split(":", 1)[1] if ":" in tag else tag)


def arg_local(tag: str) -> str:
    return elem_local(tag)


def build_device_xml(base_url: str, friendly_name: str, udn: str) -> str:
    """Build UPnP MediaRenderer:1 root description XML."""
    esc_name = xml_escape(friendly_name or "Яндекс Станция Макс")
    esc_udn = xml_escape(udn)
    esc_base = xml_escape(base_url.rstrip("/"))
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<root xmlns="urn:schemas-upnp-org:device-1-0">\n'
        "  <specVersion><major>1</major><minor>0</minor></specVersion>\n"
        f"  <URLBase>{esc_base}</URLBase>\n"
        "  <device>\n"
        f"    <deviceType>{RENDERER_URN}</deviceType>\n"
        f"    <friendlyName>{esc_name}</friendlyName>\n"
        "    <manufacturer>YapAnel</manufacturer>\n"
        "    <modelName>Yandex Station Max</modelName>\n"
        "    <modelNumber>yandexstation_2</modelNumber>\n"
        f"    <UDN>{esc_udn}</UDN>\n"
        "    <serviceList>\n"
        "      <service>\n"
        f"        <serviceType>{AVTRANSPORT_URN}</serviceType>\n"
        "        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>\n"
        "        <SCPDURL>/dlna/AVTransport.xml</SCPDURL>\n"
        "        <controlURL>/dlna/control/avtransport</controlURL>\n"
        "        <eventSubURL>/dlna/event/avtransport</eventSubURL>\n"
        "      </service>\n"
        "      <service>\n"
        f"        <serviceType>{RENDERING_URN}</serviceType>\n"
        "        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>\n"
        "        <SCPDURL>/dlna/RenderingControl.xml</SCPDURL>\n"
        "        <controlURL>/dlna/control/rendering</controlURL>\n"
        "        <eventSubURL>/dlna/event/rendering</eventSubURL>\n"
        "      </service>\n"
        "      <service>\n"
        f"        <serviceType>{CONNECTION_URN}</serviceType>\n"
        "        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>\n"
        "        <SCPDURL>/dlna/ConnectionManager.xml</SCPDURL>\n"
        "        <controlURL>/dlna/control/connection</controlURL>\n"
        "        <eventSubURL>/dlna/event/connection</eventSubURL>\n"
        "      </service>\n"
        "    </serviceList>\n"
        "  </device>\n"
        "</root>\n"
    )


def build_scpd_xml(service_kind: str) -> str:
    """Return compact UPnP SCPD description XML for AVTransport/RenderingControl/ConnectionManager."""
    kind = str(service_kind or "").strip().lower()
    if kind == "avtransport":
        actions = [
            ("SetAVTransportURI", [("InstanceID", "in", "A_ARG_TYPE_InstanceID"), ("CurrentURI", "in", "AVTransportURI"), ("CurrentURIMetaData", "in", "AVTransportURIMetaData")]),
            ("SetNextAVTransportURI", [("InstanceID", "in", "A_ARG_TYPE_InstanceID"), ("NextURI", "in", "NextAVTransportURI"), ("NextURIMetaData", "in", "NextAVTransportURIMetaData")]),
            ("Play", [("InstanceID", "in", "A_ARG_TYPE_InstanceID"), ("Speed", "in", "TransportPlaySpeed")]),
            ("Pause", [("InstanceID", "in", "A_ARG_TYPE_InstanceID")]),
            ("Stop", [("InstanceID", "in", "A_ARG_TYPE_InstanceID")]),
            ("Next", [("InstanceID", "in", "A_ARG_TYPE_InstanceID")]),
            ("Previous", [("InstanceID", "in", "A_ARG_TYPE_InstanceID")]),
            ("Seek", [("InstanceID", "in", "A_ARG_TYPE_InstanceID"), ("Unit", "in", "A_ARG_TYPE_SeekMode"), ("Target", "in", "A_ARG_TYPE_SeekTarget")]),
            ("GetTransportInfo", [("InstanceID", "in", "A_ARG_TYPE_InstanceID"), ("CurrentTransportState", "out", "TransportState"), ("CurrentTransportStatus", "out", "TransportStatus"), ("CurrentSpeed", "out", "TransportPlaySpeed")]),
            ("GetPositionInfo", [("InstanceID", "in", "A_ARG_TYPE_InstanceID"), ("Track", "out", "CurrentTrack"), ("TrackDuration", "out", "CurrentTrackDuration"), ("TrackMetaData", "out", "CurrentTrackMetaData"), ("TrackURI", "out", "CurrentTrackURI"), ("RelTime", "out", "RelativeTimePosition"), ("AbsTime", "out", "AbsoluteTimePosition"), ("RelCount", "out", "RelativeCounterPosition"), ("AbsCount", "out", "AbsoluteCounterPosition")]),
            ("GetMediaInfo", [("InstanceID", "in", "A_ARG_TYPE_InstanceID"), ("NrTracks", "out", "NumberOfTracks"), ("MediaDuration", "out", "CurrentMediaDuration"), ("CurrentURI", "out", "AVTransportURI"), ("CurrentURIMetaData", "out", "AVTransportURIMetaData"), ("NextURI", "out", "NextAVTransportURI"), ("NextURIMetaData", "out", "NextAVTransportURIMetaData"), ("PlayMedium", "out", "PlaybackStorageMedium"), ("RecordMedium", "out", "RecordStorageMedium"), ("WriteStatus", "out", "RecordMediumWriteStatus")]),
        ]
        state_vars = [
            ("A_ARG_TYPE_InstanceID", "ui4"),
            ("A_ARG_TYPE_SeekMode", "string"),
            ("A_ARG_TYPE_SeekTarget", "string"),
            ("AVTransportURI", "string"),
            ("AVTransportURIMetaData", "string"),
            ("NextAVTransportURI", "string"),
            ("NextAVTransportURIMetaData", "string"),
            ("CurrentTrack", "ui4"),
            ("CurrentTrackDuration", "string"),
            ("CurrentTrackMetaData", "string"),
            ("CurrentTrackURI", "string"),
            ("RelativeTimePosition", "string"),
            ("AbsoluteTimePosition", "string"),
            ("RelativeCounterPosition", "i4"),
            ("AbsoluteCounterPosition", "i4"),
            ("NumberOfTracks", "ui4"),
            ("CurrentMediaDuration", "string"),
            ("TransportState", "string"),
            ("TransportStatus", "string"),
            ("TransportPlaySpeed", "string"),
            ("PlaybackStorageMedium", "string"),
            ("RecordStorageMedium", "string"),
            ("RecordMediumWriteStatus", "string"),
        ]
    elif kind == "rendering":
        actions = [
            ("GetVolume", [("InstanceID", "in", "A_ARG_TYPE_InstanceID"), ("Channel", "in", "A_ARG_TYPE_Channel"), ("CurrentVolume", "out", "Volume")]),
            ("SetVolume", [("InstanceID", "in", "A_ARG_TYPE_InstanceID"), ("Channel", "in", "A_ARG_TYPE_Channel"), ("DesiredVolume", "in", "Volume")]),
            ("GetMute", [("InstanceID", "in", "A_ARG_TYPE_InstanceID"), ("Channel", "in", "A_ARG_TYPE_Channel"), ("CurrentMute", "out", "Mute")]),
            ("SetMute", [("InstanceID", "in", "A_ARG_TYPE_InstanceID"), ("Channel", "in", "A_ARG_TYPE_Channel"), ("DesiredMute", "in", "Mute")]),
        ]
        state_vars = [
            ("A_ARG_TYPE_InstanceID", "ui4"),
            ("A_ARG_TYPE_Channel", "string"),
            ("Volume", "ui2"),
            ("Mute", "boolean"),
        ]
    else:
        actions = [
            ("GetProtocolInfo", [("Source", "out", "SourceProtocolInfo"), ("Sink", "out", "SinkProtocolInfo")]),
            ("GetCurrentConnectionIDs", [("ConnectionIDs", "out", "CurrentConnectionIDs")]),
            ("GetCurrentConnectionInfo", [("ConnectionID", "in", "A_ARG_TYPE_ConnectionID"), ("RcsID", "out", "A_ARG_TYPE_RcsID"), ("AVTransportID", "out", "A_ARG_TYPE_AVTransportID"), ("ProtocolInfo", "out", "A_ARG_TYPE_ProtocolInfo"), ("PeerConnectionManager", "out", "A_ARG_TYPE_ConnectionManager"), ("PeerConnectionID", "out", "A_ARG_TYPE_ConnectionID"), ("Direction", "out", "A_ARG_TYPE_Direction"), ("Status", "out", "A_ARG_TYPE_ConnectionStatus")]),
        ]
        state_vars = [
            ("SourceProtocolInfo", "string"),
            ("SinkProtocolInfo", "string"),
            ("CurrentConnectionIDs", "string"),
            ("A_ARG_TYPE_ConnectionID", "i4"),
            ("A_ARG_TYPE_RcsID", "i4"),
            ("A_ARG_TYPE_AVTransportID", "i4"),
            ("A_ARG_TYPE_ProtocolInfo", "string"),
            ("A_ARG_TYPE_ConnectionManager", "string"),
            ("A_ARG_TYPE_Direction", "string"),
            ("A_ARG_TYPE_ConnectionStatus", "string"),
        ]

    act_xml = []
    for name, arg_list in actions:
        args_str = "\n".join(
            f"        <argument><name>{an}</name><direction>{ad}</direction><relatedStateVariable>{av}</relatedStateVariable></argument>"
            for an, ad, av in arg_list
        )
        act_xml.append(f"    <action>\n      <name>{name}</name>\n      <argumentList>\n{args_str}\n      </argumentList>\n    </action>")

    var_xml = "\n".join(
        f'    <stateVariable sendEvents="no"><name>{vn}</name><dataType>{vt}</dataType></stateVariable>'
        for vn, vt in state_vars
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<scpd xmlns="urn:schemas-upnp-org:service-1-0">\n'
        "  <specVersion><major>1</major><minor>0</minor></specVersion>\n"
        f"  <actionList>\n{chr(10).join(act_xml)}\n  </actionList>\n"
        f"  <serviceStateTable>\n{var_xml}\n  </serviceStateTable>\n"
        "</scpd>\n"
    )


def _soap_response(service_urn: str, action: str, fields: List[Tuple[str, Any]]) -> bytes:
    inner = "".join(f"<{k}>{xml_escape(str(v))}</{k}>" for k, v in fields)
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action}Response xmlns:u="{service_urn}">{inner}</u:{action}Response>'
        "</s:Body></s:Envelope>"
    )
    return body.encode("utf-8")


def _soap_fault(code: int, desc: str) -> bytes:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body><s:Fault>"
        "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>"
        "<detail><UPnPError xmlns=\"urn:schemas-upnp-org:control-1-0\">"
        f"<errorCode>{int(code)}</errorCode><errorDescription>{xml_escape(desc)}</errorDescription>"
        "</UPnPError></detail>"
        "</s:Fault></s:Body></s:Envelope>"
    )
    return body.encode("utf-8")


class _DlnaHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _DlnaHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    renderer: Optional["MaxDlnaRenderer"] = None

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_xml(self, xml_text: str, status: int = 200) -> None:
        data = xml_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, status: int = 200, ctype: str = 'text/xml; charset="utf-8"') -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(data)

    def do_GET(self) -> None:
        rend = self.renderer
        if not rend:
            self.send_response(404)
            self.end_headers()
            return
        path = self.path.split("?", 1)[0]
        if path == "/dlna/device.xml":
            host = str(self.headers.get("Host") or "").strip()
            base_url = f"http://{host}" if host else rend.base_url()
            self._send_xml(build_device_xml(base_url, rend.friendly_name, rend.udn))
            return
        scpd_map = {
            "/dlna/AVTransport.xml": "avtransport",
            "/dlna/RenderingControl.xml": "rendering",
            "/dlna/ConnectionManager.xml": "connection",
        }
        if path in scpd_map:
            self._send_xml(build_scpd_xml(scpd_map[path]))
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        rend = self.renderer
        if not rend:
            self.send_response(404)
            self.end_headers()
            return
        raw_len = self.headers.get("Content-Length") or "0"
        try:
            length = min(262144, max(0, int(raw_len)))
        except ValueError:
            self._send_bytes(_soap_fault(402, "Invalid Content-Length"), status=500)
            return
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?", 1)[0]
        status, payload = rend.handle_soap(path, self.headers, body)
        self._send_bytes(payload, status=status)

    def do_SUBSCRIBE(self) -> None:
        sid = f"uuid:yapanel-sub-{int(time.time() * 1000)}"
        self.send_response(200)
        self.send_header("SID", sid)
        self.send_header("TIMEOUT", "Second-1800")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_UNSUBSCRIBE(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True


class MaxDlnaRenderer:
    """DLNA / UPnP MediaRenderer & direct HTTP stream cast controller."""

    def __init__(self, panel):
        self.panel = panel
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._httpd: Optional[_DlnaHTTPServer] = None
        self._ssdp_thread: Optional[threading.Thread] = None
        self._ssdp_sock: Optional[socket.socket] = None

        cfg = self._load_config()
        self._enabled: bool = bool(cfg.get("enabled", True))
        self._friendly_name: str = str(cfg.get("friendly_name") or "Яндекс Станция Макс").strip()[:80] or "Яндекс Станция Макс"
        self._port: int = int(cfg.get("port") or DLNA_PORT_DEFAULT)
        self._bound_port: int = 0
        self._last_mute: bool = False

        ident = str(getattr(getattr(panel, "glagol", None), "device_id", "") or os.uname().nodename if hasattr(os, "uname") else "max")
        self.udn: str = "uuid:yapanel-max-" + hashlib.sha1(ident.encode("utf-8")).hexdigest()[:12]

        self._current_uri: str = ""
        self._current_metadata: str = ""
        self._current_track: Optional[Dict[str, Any]] = None
        self._next_uri: str = ""
        self._next_metadata: str = ""
        self._next_track: Optional[Dict[str, Any]] = None

    def _load_config(self) -> Dict[str, Any]:
        try:
            loader = getattr(self.panel, "_load_settings", None)
            s = loader() if callable(loader) else None
            if not isinstance(s, dict):
                import server

                s = server.load_settings()
            cfg = s.get("extmusic_dlna") if isinstance(s, dict) else None
            if isinstance(cfg, dict):
                return dict(cfg)
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _save_config(self) -> None:
        payload = {
            "enabled": bool(self._enabled),
            "friendly_name": self._friendly_name,
            "port": self._port,
        }
        try:
            import server

            updater = getattr(server, "update_settings_section", None)
            if callable(updater):
                updater("extmusic_dlna", payload)
        except Exception:  # noqa: BLE001
            pass

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def friendly_name(self) -> str:
        return self._friendly_name

    @property
    def port(self) -> int:
        return self._bound_port or self._port

    def base_url(self) -> str:
        lan_ip = detect_lan_ip()
        return f"http://{lan_ip}:{self.port}"

    def _ext_service(self):
        return getattr(self.panel, "_extmusic_service", None)

    def start(self) -> bool:
        if not self._enabled:
            return False
        with self._lock:
            if self._httpd is not None:
                return True
            self._stop_evt.clear()
            handler_cls = type("_BoundDlnaHandler", (_DlnaHandler,), {"renderer": self})
            ports = (self._port, 0) if self._port else (0,)
            for cand in ports:
                for host in ("0.0.0.0", "127.0.0.1"):
                    try:
                        srv = _DlnaHTTPServer((host, cand), handler_cls)
                        self._httpd = srv
                        self._bound_port = int(srv.server_address[1] or cand)
                        threading.Thread(target=srv.serve_forever, daemon=True, name="dlna-http").start()
                        break
                    except OSError:
                        self._httpd = None
                if self._httpd is not None:
                    break
            if self._httpd is None:
                _log.warning("DLNA HTTP server could not bind port %s", self._port)
                return False
            self._ssdp_thread = threading.Thread(target=self._ssdp_loop, daemon=True, name="dlna-ssdp")
            self._ssdp_thread.start()
            _log.info("DLNA MediaRenderer listening on port %d (%s)", self._bound_port, self._friendly_name)
            return True

    def stop(self) -> None:
        self._stop_evt.set()
        self._send_ssdp_notify("ssdp:byebye")
        with self._lock:
            srv = self._httpd
            self._httpd = None
            self._bound_port = 0
            sock = self._ssdp_sock
            self._ssdp_sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass

    def _ssdp_notify_targets(self) -> List[Tuple[str, str]]:
        return [
            ("upnp:rootdevice", f"{self.udn}::upnp:rootdevice"),
            (self.udn, self.udn),
            (RENDERER_URN, f"{self.udn}::{RENDERER_URN}"),
            (AVTRANSPORT_URN, f"{self.udn}::{AVTRANSPORT_URN}"),
            (RENDERING_URN, f"{self.udn}::{RENDERING_URN}"),
            (CONNECTION_URN, f"{self.udn}::{CONNECTION_URN}"),
        ]

    def _send_ssdp_notify(self, nts: str = "ssdp:alive") -> None:
        if not self._bound_port:
            return
        location = f"{self.base_url()}/dlna/device.xml"
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            for nt, usn in self._ssdp_notify_targets():
                msg = (
                    "NOTIFY * HTTP/1.1\r\n"
                    f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
                    "CACHE-CONTROL: max-age=1800\r\n"
                    f"LOCATION: {location}\r\n"
                    f"NT: {nt}\r\n"
                    f"NTS: {nts}\r\n"
                    "SERVER: Linux/4.9 UPnP/1.0 YapAnel-DLNA/1.0\r\n"
                    f"USN: {usn}\r\n\r\n"
                ).encode("utf-8")
                try:
                    sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
                except OSError:
                    break
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _ssdp_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", SSDP_PORT))
            mreq = struct.pack("4sl", socket.inet_aton(SSDP_ADDR), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(1.5)
            self._ssdp_sock = sock
        except OSError as err:
            _log.debug("SSDP multicast bind skipped (%s); HTTP/SOAP renderer still active", err)
            try:
                sock.close()
            except OSError:
                pass
            return

        last_alive = 0.0
        location = f"{self.base_url()}/dlna/device.xml"
        wanted_sts = {
            "ssdp:all",
            "upnp:rootdevice",
            self.udn.lower(),
            RENDERER_URN.lower(),
            AVTRANSPORT_URN.lower(),
            RENDERING_URN.lower(),
            CONNECTION_URN.lower(),
        }
        while not self._stop_evt.is_set():
            now = time.monotonic()
            if now - last_alive >= 60.0:
                self._send_ssdp_notify("ssdp:alive")
                last_alive = now
                location = f"{self.base_url()}/dlna/device.xml"
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", errors="replace")
            if not text.upper().startswith("M-SEARCH"):
                continue
            m_st = re.search(r"(?im)^ST:\s*([^\r\n]+)", text)
            st = (m_st.group(1).strip() if m_st else "ssdp:all")
            st_lower = st.lower()
            if st_lower not in wanted_sts:
                continue
            resp_targets = self._ssdp_notify_targets() if st_lower == "ssdp:all" else [
                (st, self.udn if st_lower == self.udn.lower() else f"{self.udn}::{st}")
            ]
            for r_st, r_usn in resp_targets:
                reply = (
                    "HTTP/1.1 200 OK\r\n"
                    "CACHE-CONTROL: max-age=1800\r\n"
                    "EXT:\r\n"
                    f"LOCATION: {location}\r\n"
                    "SERVER: Linux/4.9 UPnP/1.0 YapAnel-DLNA/1.0\r\n"
                    f"ST: {r_st}\r\n"
                    f"USN: {r_usn}\r\n\r\n"
                ).encode("utf-8")
                try:
                    sock.sendto(reply, addr)
                except OSError:
                    pass

    def _make_track(self, clean_url: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        tid = "dlna_" + hashlib.sha1(clean_url.encode("utf-8")).hexdigest()[:12]
        return {
            "source": "dlna",
            "id": tid,
            "url": clean_url,
            "title": str(meta.get("title") or "DLNA Поток")[:300],
            "artist": str(meta.get("artist") or "DLNA / Поток")[:300],
            "album": str(meta.get("album") or "")[:300],
            "thumb": str(meta.get("thumb") or "")[:1000],
            "duration": int(meta.get("duration") or 0),
        }

    def cast_url(
        self,
        url: str,
        title: str = "",
        artist: str = "",
        thumb: str = "",
        album: str = "",
        duration: int = 0,
    ) -> Dict[str, Any]:
        """Play a validated LAN/HTTP audio stream immediately via ExtMusicService."""
        clean_url = validate_stream_url(url, allow_lan=True)
        clean_thumb = validate_stream_url(thumb, allow_lan=True) if str(thumb or "").strip() else ""
        meta = {
            "title": str(title or "").strip() or _fallback_title_from_url(clean_url),
            "artist": str(artist or "").strip() or "DLNA / Поток",
            "album": str(album or "").strip(),
            "thumb": clean_thumb,
            "duration": max(0, int(duration or 0)),
        }
        track = self._make_track(clean_url, meta)
        self._current_uri = clean_url
        self._current_metadata = ""
        self._current_track = track
        svc = self._ext_service()
        if svc is None:
            raise ValueError("Сервис внешнего плеера не инициализирован")
        return svc.play_external_stream(track)

    def _read_volume(self) -> int:
        try:
            rv = getattr(self.panel, "status", None)
            if callable(rv):
                # Prefer cached non-blocking max_read_volume via Glagol when available
                glagol = getattr(self.panel, "glagol", None)
                st = getattr(glagol, "state", None) if glagol is not None else None
                if isinstance(st, dict) and isinstance(st.get("volume"), (int, float)):
                    val = float(st["volume"])
                    return int(round(val * 100.0)) if 0.0 <= val <= 1.0 else int(round(max(0.0, min(100.0, val))))
        except Exception:  # noqa: BLE001
            pass
        return 50

    def handle_soap(self, path: str, headers: Any, body_bytes: bytes) -> Tuple[int, bytes]:
        try:
            action, args = parse_soap_action(headers, body_bytes)
        except (ValueError, ET.ParseError) as err:
            return 500, _soap_fault(402, str(err))

        svc = self._ext_service()
        if path.endswith("/avtransport"):
            urn = AVTRANSPORT_URN
            try:
                if action == "SetAVTransportURI":
                    uri = str(args.get("CurrentURI") or "").strip()
                    md_xml = str(args.get("CurrentURIMetaData") or "")
                    if not uri:
                        self._current_uri = ""
                        self._current_metadata = ""
                        self._current_track = None
                        if svc is not None:
                            svc.stop()
                        return 200, _soap_response(urn, action, [])
                    clean_url = validate_stream_url(uri, allow_lan=True)
                    meta = parse_didl_metadata(md_xml, fallback_url=clean_url)
                    self._current_uri = clean_url
                    self._current_metadata = md_xml
                    self._current_track = self._make_track(clean_url, meta)
                    return 200, _soap_response(urn, action, [])

                if action == "SetNextAVTransportURI":
                    uri = str(args.get("NextURI") or "").strip()
                    md_xml = str(args.get("NextURIMetaData") or "")
                    if not uri:
                        self._next_uri = ""
                        self._next_metadata = ""
                        self._next_track = None
                        return 200, _soap_response(urn, action, [])
                    clean_url = validate_stream_url(uri, allow_lan=True)
                    meta = parse_didl_metadata(md_xml, fallback_url=clean_url)
                    self._next_uri = clean_url
                    self._next_metadata = md_xml
                    self._next_track = self._make_track(clean_url, meta)
                    return 200, _soap_response(urn, action, [])

                if action == "Play":
                    if svc is None:
                        return 500, _soap_fault(501, "Service unavailable")
                    cur = getattr(svc, "current", None)
                    st = svc.status() if cur else {}
                    p = (st.get("player") or {}) if isinstance(st, dict) else {}
                    if self._current_track and (
                        not cur or cur.get("url") != self._current_track.get("url") or p.get("state") == "stopped"
                    ):
                        nxt = self._next_track
                        svc.play_external_stream(self._current_track, next_track=nxt)
                    elif cur and not p.get("playing"):
                        svc.resume()
                    return 200, _soap_response(urn, action, [])

                if action == "Pause":
                    if svc is not None:
                        svc.pause()
                    return 200, _soap_response(urn, action, [])

                if action == "Stop":
                    if svc is not None:
                        svc.stop()
                    return 200, _soap_response(urn, action, [])

                if action == "Next":
                    if svc is not None:
                        svc.next()
                    return 200, _soap_response(urn, action, [])

                if action == "Previous":
                    if svc is not None:
                        svc.prev()
                    return 200, _soap_response(urn, action, [])

                if action == "Seek":
                    if svc is not None:
                        target_sec = parse_dlna_duration(args.get("Target") or "0")
                        svc.seek(target_sec * 1000)
                    return 200, _soap_response(urn, action, [])

                if action == "GetTransportInfo":
                    tstate = "STOPPED"
                    if svc is not None and getattr(svc, "current", None):
                        p = (svc.status().get("player") or {})
                        if p.get("playing"):
                            tstate = "PLAYING"
                        elif p.get("state") == "paused":
                            tstate = "PAUSED_PLAYBACK"
                        elif p.get("buffering"):
                            tstate = "TRANSITIONING"
                    return 200, _soap_response(
                        urn,
                        action,
                        [
                            ("CurrentTransportState", tstate),
                            ("CurrentTransportStatus", "OK"),
                            ("CurrentSpeed", "1"),
                        ],
                    )

                if action == "GetPositionInfo":
                    pos_s = 0
                    dur_s = 0
                    uri = self._current_uri
                    md = self._current_metadata
                    if svc is not None and getattr(svc, "current", None):
                        st = svc.status()
                        p = st.get("player") or {}
                        pos_s = int((p.get("position") or 0) // 1000)
                        dur_s = int((p.get("duration") or 0) // 1000)
                        uri = str((st.get("current") or {}).get("url") or uri)
                    dur_str = format_dlna_duration(dur_s)
                    pos_str = format_dlna_duration(pos_s)
                    return 200, _soap_response(
                        urn,
                        action,
                        [
                            ("Track", 1 if uri else 0),
                            ("TrackDuration", dur_str),
                            ("TrackMetaData", md),
                            ("TrackURI", uri),
                            ("RelTime", pos_str),
                            ("AbsTime", pos_str),
                            ("RelCount", pos_s),
                            ("AbsCount", pos_s),
                        ],
                    )

                if action == "GetMediaInfo":
                    dur_s = int((self._current_track or {}).get("duration") or 0)
                    return 200, _soap_response(
                        urn,
                        action,
                        [
                            ("NrTracks", 1 if self._current_uri else 0),
                            ("MediaDuration", format_dlna_duration(dur_s)),
                            ("CurrentURI", self._current_uri),
                            ("CurrentURIMetaData", self._current_metadata),
                            ("NextURI", self._next_uri),
                            ("NextURIMetaData", self._next_metadata),
                            ("PlayMedium", "NETWORK"),
                            ("RecordMedium", "NOT_IMPLEMENTED"),
                            ("WriteStatus", "NOT_IMPLEMENTED"),
                        ],
                    )
            except ValueError as err:
                return 500, _soap_fault(714, str(err))
            return 500, _soap_fault(401, f"Unsupported AVTransport action: {action}")

        if path.endswith("/rendering"):
            urn = RENDERING_URN
            if action == "GetVolume":
                return 200, _soap_response(urn, action, [("CurrentVolume", self._read_volume())])
            if action == "SetVolume":
                try:
                    lvl = max(0, min(100, int(round(float(args.get("DesiredVolume") or 50)))))
                except (TypeError, ValueError):
                    return 500, _soap_fault(402, "Invalid DesiredVolume")
                setter = getattr(self.panel, "set_volume", None)
                if callable(setter):
                    setter({"level": lvl})
                return 200, _soap_response(urn, action, [])
            if action == "GetMute":
                return 200, _soap_response(urn, action, [("CurrentMute", 1 if self._last_mute else 0)])
            if action == "SetMute":
                raw = str(args.get("DesiredMute") or "0").strip().lower()
                self._last_mute = raw in ("1", "true", "yes")
                setter = getattr(self.panel, "set_volume", None)
                if callable(setter):
                    setter({"level": 0 if self._last_mute else 35})
                return 200, _soap_response(urn, action, [])
            return 500, _soap_fault(401, f"Unsupported RenderingControl action: {action}")

        if path.endswith("/connection"):
            urn = CONNECTION_URN
            if action == "GetProtocolInfo":
                return 200, _soap_response(urn, action, [("Source", ""), ("Sink", _PROTOCOL_INFO_SINK)])
            if action == "GetCurrentConnectionIDs":
                return 200, _soap_response(urn, action, [("ConnectionIDs", "0")])
            if action == "GetCurrentConnectionInfo":
                return 200, _soap_response(
                    urn,
                    action,
                    [
                        ("RcsID", 0),
                        ("AVTransportID", 0),
                        ("ProtocolInfo", ""),
                        ("PeerConnectionManager", ""),
                        ("PeerConnectionID", -1),
                        ("Direction", "Input"),
                        ("Status", "OK"),
                    ],
                )
            return 500, _soap_fault(401, f"Unsupported ConnectionManager action: {action}")

        return 500, _soap_fault(401, "Unknown control endpoint")

    def configure(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("Ожидается JSON-объект настроек DLNA")
        changed = False
        if "enabled" in data and data["enabled"] is not None:
            want = bool(data["enabled"])
            if want != self._enabled:
                self._enabled = want
                changed = True
                if want:
                    self.start()
                else:
                    self.stop()
        if "friendly_name" in data and data["friendly_name"] is not None:
            name = str(data["friendly_name"]).strip()[:80]
            if name and name != self._friendly_name:
                self._friendly_name = name
                changed = True
                if self._enabled and self._httpd is not None:
                    self._send_ssdp_notify("ssdp:alive")
        if changed:
            self._save_config()

        if str(data.get("action") or "").strip().lower() == "cast" or data.get("url"):
            res = self.cast_url(
                url=str(data.get("url") or ""),
                title=str(data.get("title") or ""),
                artist=str(data.get("artist") or ""),
                thumb=str(data.get("thumb") or ""),
            )
            return {**self.status(), "playback": res}

        return self.status()

    def status(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "enabled": self._enabled,
            "running": self._httpd is not None,
            "friendly_name": self._friendly_name,
            "port": self.port,
            "device_url": f"{self.base_url()}/dlna/device.xml" if self._httpd is not None else "",
            "udn": self.udn,
            "current_uri": self._current_uri,
            "current_title": str((self._current_track or {}).get("title") or ""),
        }
