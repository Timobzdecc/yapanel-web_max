"""Unit tests for DLNA / UPnP MediaRenderer & HTTP audio stream casting (extmusic/dlna.py)."""
from __future__ import annotations

import threading
import unittest
import urllib.request

from api.extmusic import register
from api.router import ApiRequest, Router
from extmusic.dlna import (
    MaxDlnaRenderer,
    build_device_xml,
    build_scpd_xml,
    format_dlna_duration,
    parse_didl_metadata,
    parse_dlna_duration,
    parse_soap_action,
    validate_stream_url,
)
from extmusic.service import ExtMusicService


class _FakeGlagol:
    def __init__(self, volume=0.45):
        self.device_id = "TEST_DEVICE_MAX"
        self.state = {"volume": volume}
        self.ws = object()


class _FakeExtSvc:
    def __init__(self):
        self.current = None
        self.queue = []
        self.playing = False
        self.position_ms = 65000
        self.duration_ms = 245000
        self.calls = []

    def play_external_stream(self, track, queue=None, next_track=None, multiroom_follower=False):
        self.current = dict(track)
        self.playing = True
        self.calls.append(("play_external_stream", track, next_track, multiroom_follower))
        return self.status()

    def pause(self):
        self.playing = False
        self.calls.append(("pause",))
        return self.status()

    def resume(self):
        self.playing = True
        self.calls.append(("resume",))
        return self.status()

    def stop(self):
        self.current = None
        self.playing = False
        self.calls.append(("stop",))
        return self.status()

    def next(self):
        self.calls.append(("next",))
        return self.status()

    def prev(self):
        self.calls.append(("prev",))
        return self.status()

    def seek(self, position_ms):
        self.position_ms = position_ms
        self.calls.append(("seek", position_ms))
        return self.status()

    def status(self):
        return {
            "current": self.current,
            "player": {
                "playing": self.playing,
                "state": "playing" if self.playing else ("paused" if self.current else "stopped"),
                "position": self.position_ms,
                "duration": self.duration_ms,
                "buffering": False,
            },
        }


class _FakePanel:
    def __init__(self):
        self.adb = type("_Adb", (), {"is_local": False, "shell": lambda *a, **k: ("", "", 0)})()
        self.glagol = _FakeGlagol()
        self._extmusic_service = _FakeExtSvc()
        self._settings = {}
        self.vol_calls = []

    def _load_settings(self):
        return self._settings

    def set_volume(self, data):
        self.vol_calls.append(data)
        self.glagol.state["volume"] = int(data["level"]) / 100.0
        return {"ok": True}


class TestDlnaHelpers(unittest.TestCase):
    def test_validate_stream_url_allows_lan_and_blocks_loopback_metadata(self):
        self.assertEqual(
            validate_stream_url("http://192.168.1.50:32469/track.flac"),
            "http://192.168.1.50:32469/track.flac",
        )
        self.assertEqual(
            validate_stream_url("https://icecast.example.org/radio.mp3"),
            "https://icecast.example.org/radio.mp3",
        )
        for bad in (
            "",
            "file:///etc/passwd",
            "ftp://192.168.1.50/a.mp3",
            "http://127.0.0.1:8765/secret",
            "http://localhost/test.mp3",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/a.mp3",
            "http://192.168.1.1/a\nb.mp3",
        ):
            with self.assertRaises(ValueError, msg=bad):
                validate_stream_url(bad)

    def test_didl_parsing_and_xxe_rejection(self):
        didl = (
            '&lt;DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"&gt;'
            "&lt;item id=\"1\"&gt;"
            "&lt;dc:title&gt;Bohemian Rhapsody&lt;/dc:title&gt;"
            "&lt;upnp:artist&gt;Queen&lt;/upnp:artist&gt;"
            "&lt;upnp:album&gt;A Night at the Opera&lt;/upnp:album&gt;"
            "&lt;upnp:albumArtURI&gt;http://192.168.1.50:32469/cover.jpg&lt;/upnp:albumArtURI&gt;"
            "&lt;res duration=\"0:05:55.500\"&gt;http://192.168.1.50:32469/song.flac&lt;/res&gt;"
            "&lt;/item&gt;&lt;/DIDL-Lite&gt;"
        )
        meta = parse_didl_metadata(didl, "http://192.168.1.50:32469/song.flac")
        self.assertEqual(meta["title"], "Bohemian Rhapsody")
        self.assertEqual(meta["artist"], "Queen")
        self.assertEqual(meta["album"], "A Night at the Opera")
        self.assertEqual(meta["thumb"], "http://192.168.1.50:32469/cover.jpg")
        self.assertEqual(meta["duration"], 356)
        self.assertEqual(format_dlna_duration(355), "0:05:55")
        self.assertEqual(parse_dlna_duration("1:02:03"), 3723)

        # Fallback title from URL
        plain = parse_didl_metadata("", "http://192.168.1.20/music/01%20Test%20Track.flac")
        self.assertEqual(plain["title"], "01 Test Track")

        # XXE must be rejected before parsing
        xxe = '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><DIDL-Lite/>'
        with self.assertRaises(ValueError):
            parse_didl_metadata(xxe)

    def test_device_and_scpd_xml_generation(self):
        dev_xml = build_device_xml("http://192.168.1.144:8789", "Макс Кухня", "uuid:abc123")
        self.assertIn("MediaRenderer:1", dev_xml)
        self.assertIn("AVTransport:1", dev_xml)
        self.assertIn("RenderingControl:1", dev_xml)
        self.assertIn("ConnectionManager:1", dev_xml)
        for kind in ("avtransport", "rendering", "connection"):
            self.assertIn("<scpd", build_scpd_xml(kind))

    def test_soap_parsing_rejects_xxe(self):
        soap = (
            b'<?xml version="1.0"?>'
            b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            b'<s:Body><u:SetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
            b"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>42</DesiredVolume>"
            b"</u:SetVolume></s:Body></s:Envelope>"
        )
        act, args = parse_soap_action({"SOAPAction": '"urn:schemas-upnp-org:service:RenderingControl:1#SetVolume"'}, soap)
        self.assertEqual(act, "SetVolume")
        self.assertEqual(args["DesiredVolume"], "42")

        with self.assertRaises(ValueError):
            parse_soap_action({}, b'<!DOCTYPE x><Envelope/>')


class TestDlnaRendererAndRoutes(unittest.TestCase):
    def test_soap_lifecycle_set_uri_play_seek_position_volume(self):
        panel = _FakePanel()
        rend = MaxDlnaRenderer(panel)

        set_uri_body = (
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            '<s:Body><u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            "<InstanceID>0</InstanceID>"
            "<CurrentURI>http://192.168.1.50:8200/Music/track.mp3</CurrentURI>"
            "<CurrentURIMetaData>&lt;DIDL-Lite xmlns=\"urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/\" "
            "xmlns:dc=\"http://purl.org/dc/elements/1.1/\"&gt;"
            "&lt;item&gt;&lt;dc:title&gt;Moonlight&lt;/dc:title&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;</CurrentURIMetaData>"
            "</u:SetAVTransportURI></s:Body></s:Envelope>"
        ).encode("utf-8")
        st, _ = rend.handle_soap("/dlna/control/avtransport", {"SOAPAction": '"urn:x#SetAVTransportURI"'}, set_uri_body)
        self.assertEqual(st, 200)

        play_body = b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID><Speed>1</Speed></u:Play></s:Body></s:Envelope>'
        st, _ = rend.handle_soap("/dlna/control/avtransport", {"SOAPAction": '"urn:x#Play"'}, play_body)
        self.assertEqual(st, 200)
        self.assertEqual(panel._extmusic_service.current["title"], "Moonlight")
        self.assertEqual(panel._extmusic_service.current["source"], "dlna")

        seek_body = b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:Seek xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID><Target>0:01:30</Target></u:Seek></s:Body></s:Envelope>'
        st, _ = rend.handle_soap("/dlna/control/avtransport", {"SOAPAction": '"urn:x#Seek"'}, seek_body)
        self.assertEqual(st, 200)
        self.assertEqual(panel._extmusic_service.position_ms, 90000)

        pos_body = b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:GetPositionInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID></u:GetPositionInfo></s:Body></s:Envelope>'
        st, resp_bytes = rend.handle_soap("/dlna/control/avtransport", {"SOAPAction": '"urn:x#GetPositionInfo"'}, pos_body)
        self.assertEqual(st, 200)
        self.assertIn(b"<RelTime>0:01:30</RelTime>", resp_bytes)

        vol_body = b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:SetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1"><InstanceID>0</InstanceID><DesiredVolume>60</DesiredVolume></u:SetVolume></s:Body></s:Envelope>'
        st, _ = rend.handle_soap("/dlna/control/rendering", {"SOAPAction": '"urn:x#SetVolume"'}, vol_body)
        self.assertEqual(st, 200)
        self.assertEqual(panel.vol_calls[-1]["level"], 60)

        proto_body = b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:GetProtocolInfo xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1"/></s:Body></s:Envelope>'
        st, resp_bytes = rend.handle_soap("/dlna/control/connection", {"SOAPAction": '"urn:x#GetProtocolInfo"'}, proto_body)
        self.assertEqual(st, 200)
        self.assertIn(b"audio/flac", resp_bytes)

    def test_dlna_http_server_and_router_endpoints(self):
        panel = _FakePanel()
        rend = MaxDlnaRenderer(panel)
        rend._port = 0  # ephemeral port for unit test
        rend._ssdp_loop = lambda: None  # avoid UDP 1900 host conflict
        rend._send_ssdp_notify = lambda *a, **k: None
        self.assertTrue(rend.start())
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{rend.port}/dlna/device.xml", timeout=3) as resp:
                xml = resp.read().decode("utf-8")
            self.assertIn("MediaRenderer:1", xml)
        finally:
            rend.stop()

        svc = ExtMusicService(panel)
        svc.gst.available = lambda: False
        svc.player.play = lambda url: None
        svc.player.stop = lambda: None
        panel._extmusic_service = svc

        router = Router()
        register(router)
        get_dlna = router.match("GET", "/api/extmusic/dlna")
        self.assertIsNotNone(get_dlna)
        self.assertTrue(get_dlna(ApiRequest(panel=panel))["ok"])

        post_dlna = router.match("POST", "/api/extmusic/dlna")
        cast_res = post_dlna(
            ApiRequest(
                panel=panel,
                data={"action": "cast", "url": "https://cdn.example.com/live.mp3", "title": "Rock FM"},
            )
        )
        self.assertEqual(cast_res["current_title"], "Rock FM")
        self.assertEqual(svc.current["source"], "dlna")
        svc.stop()


if __name__ == "__main__":
    unittest.main()
