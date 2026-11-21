import json
import math
from pathlib import Path
from unittest.mock import MagicMock

from extmusic import fast_yt
from extmusic.gstbridge import GstBridge, NORM_MAX_DB, NORM_MIN_DB, compute_norm_gain
from extmusic.service import ExtMusicService
from extmusic.store import ExtMusicStore


def test_extract_loudness_db_sources():
    assert fast_yt._extract_loudness_db({"loudnessDb": 7.57}, {}) == 7.57
    assert (
        fast_yt._extract_loudness_db(
            {},
            {"playerConfig": {"audioConfig": {"loudnessDb": -3.25}}},
        )
        == -3.25
    )
    # Fallback via trackAbsoluteLoudnessLkfs - loudnessTargetLkfs
    assert (
        fast_yt._extract_loudness_db(
            {},
            {
                "playerConfig": {
                    "audioConfig": {
                        "trackAbsoluteLoudnessLkfs": -6.43,
                        "loudnessTargetLkfs": -14,
                    }
                }
            },
        )
        == 7.57
    )
    assert (
        fast_yt._extract_loudness_db(
            {},
            {"playerConfig": {"audioConfig": {"perceptualLoudnessDb": -20.0}}},
        )
        == -6.0
    )
    # Invalid / out-of-range / missing -> None
    assert fast_yt._extract_loudness_db({}, {}) is None
    assert fast_yt._extract_loudness_db({"loudnessDb": "bad"}, {}) is None
    assert fast_yt._extract_loudness_db({"loudnessDb": float("nan")}, {}) is None
    assert fast_yt._extract_loudness_db({"loudnessDb": 99.0}, {}) is None


def test_compute_norm_gain_math_and_clamping():
    assert compute_norm_gain(None) is None
    assert compute_norm_gain(True) is None
    assert compute_norm_gain(float("nan")) is None
    assert compute_norm_gain(0.0) == 1.0
    # Loud track (+6 dB above -14 LUFS) attenuated by -6 dB -> ~0.5012
    assert math.isclose(compute_norm_gain(6.0), 10.0 ** (-6.0 / 20.0), rel_tol=1e-3)
    # Quiet track (-3 dB) boosted by +3 dB -> ~1.4125
    assert math.isclose(compute_norm_gain(-3.0), 10.0 ** (3.0 / 20.0), rel_tol=1e-3)
    # Clamped at +4 dB max boost and -16 dB max attenuation
    assert compute_norm_gain(-12.0) == round(10.0 ** (NORM_MAX_DB / 20.0), 4)
    assert compute_norm_gain(22.0) == round(10.0 ** (NORM_MIN_DB / 20.0), 4)


def test_gstbridge_norm_lifecycle_and_proxy_url(monkeypatch):
    bridge = GstBridge(panel=None)
    try:
        bridge.ensure_injected = lambda: True
        bridge._init_streamer = lambda *_a, **_k: None
        bridge.clear_alice_music_suppression = lambda: None
        written = []
        bridge._write_override = lambda u: written.append(u)
        bridge._send_glagol = lambda payload: payload

        assert bridge.norm_status() == {"enabled": True, "loudness_db": None, "gain": None}

        bridge.play("https://example.test/audio.m4a", title="Loud", duration=120, loudness_db=7.57)
        expected_gain = compute_norm_gain(7.57)
        assert f"&norm={expected_gain:.4f}" in written[-1]
        assert bridge.norm_status() == {"enabled": True, "loudness_db": 7.57, "gain": expected_gain}

        # _proxy_url during seek preserves active norm gain
        seek_url = bridge._proxy_url(12.5)
        assert f"seek=12.5&" in seek_url
        assert f"&norm={expected_gain:.4f}" in seek_url

        # Disabling normalization clears norm_gain and omits &norm=
        bridge.set_normalize(False)
        assert bridge.norm_status() == {"enabled": False, "loudness_db": 7.57, "gain": None}
        assert "&norm=" not in bridge._proxy_url(0.0)

        # Play with loudness_db while disabled ignores loudness gain
        bridge.play("https://example.test/audio2.m4a", title="Raw", duration=120, loudness_db=8.0)
        assert "&norm=" not in written[-1]

        # Re-enabling recomputes gain from last_loudness_db
        bridge.set_normalize(True)
        assert bridge.norm_status()["gain"] == compute_norm_gain(8.0)

        # Release resets per-track state
        bridge.release()
        assert bridge.norm_status() == {"enabled": True, "loudness_db": None, "gain": None}
    finally:
        try:
            if bridge._server is not None:
                bridge._server.shutdown()
                bridge._server.server_close()
        except Exception:
            pass


def test_store_normalize_persistence(tmp_path: Path):
    store = ExtMusicStore(tmp_path / ".extmusic.json")
    assert store.normalize() is True
    store.save_normalize(False)
    assert store.normalize() is False
    store2 = ExtMusicStore(tmp_path / ".extmusic.json")
    assert store2.normalize() is False
    store2.save_normalize(True)
    assert store.normalize() is True


def test_service_hw_configure_and_resolve_zero_loudness(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "extmusic.service.ExtMusicStore",
        lambda: ExtMusicStore(tmp_path / ".extmusic.json"),
    )
    panel = MagicMock()
    svc = ExtMusicService(panel)
    try:
        assert svc.hw_status()["normalize"]["enabled"] is True

        res = svc.hw_configure({"normalize_enabled": False})
        assert res["normalize"]["enabled"] is False
        assert svc.store.normalize() is False

        # Ensure _resolve_gst preserves loudness_db=0.0 (truthy-check trap)
        monkeypatch.setattr(
            fast_yt,
            "fast_resolve",
            lambda vid, cookies=None: {
                "id": vid,
                "url": "https://example.test/a.m4a",
                "duration": 180,
                "loudness_db": 0.0,
            },
        )
        resolved = svc._resolve_gst({"source": "yt", "id": "dQw4w9WgXcQ", "title": "Test", "artist": "Art"})
        assert resolved["loudness_db"] == 0.0
    finally:
        svc.stop_hw()
        try:
            if svc.gst._server is not None:
                svc.gst._server.shutdown()
                svc.gst._server.server_close()
        except Exception:
            pass
