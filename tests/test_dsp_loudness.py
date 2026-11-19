"""Unit tests for Dynamic Loudness (auto-loudness) in dsp_max.py."""
from __future__ import annotations

import math
import types
import unittest

import dsp_max
from dsp_max import BYPASS_HEX, MaxDSP, compute_loudness_boost


class _MockGlagol:
    def __init__(self, volume=0.5):
        self.state = {"volume": volume}
        self.ws = object()


class _MockPanel:
    def __init__(self, volume=0.5):
        self._settings = {}
        self.adb = None
        self.glagol = _MockGlagol(volume=volume)

    def _load_settings(self):
        return self._settings


def _make_dsp(volume=0.2):
    panel = _MockPanel(volume=volume)
    dsp = MaxDSP(panel)
    dsp.is_supported = lambda: True
    writes = []
    dsp._write_i2c_batch = lambda cmds: (writes.append(list(cmds)), True)[1]
    dsp._save_state = lambda: True
    dsp._update_boot_script = lambda: True
    return dsp, panel, writes


class TestDynamicLoudness(unittest.TestCase):
    def test_curve_shape_quantization_and_bounds(self):
        # Above reference volume (55%) -> zero boost
        self.assertEqual(compute_loudness_boost(55), (0.0, 0.0))
        self.assertEqual(compute_loudness_boost(75), (0.0, 0.0))
        self.assertEqual(compute_loudness_boost(100), (0.0, 0.0))

        # Invalid inputs -> zero boost
        self.assertEqual(compute_loudness_boost(None), (0.0, 0.0))
        self.assertEqual(compute_loudness_boost(True), (0.0, 0.0))
        self.assertEqual(compute_loudness_boost(float("nan")), (0.0, 0.0))

        # Monotonically stronger at lower volume, quantized to 0.5 dB steps
        b10, t10 = compute_loudness_boost(10)
        b20, t20 = compute_loudness_boost(20)
        b35, t35 = compute_loudness_boost(35)
        self.assertGreater(b10, b20)
        self.assertGreater(b20, b35)
        self.assertGreater(b35, 0.0)
        self.assertGreater(t10, t20)
        self.assertGreaterEqual(t20, t35)
        for val in (b10, t10, b20, t20, b35, t35):
            self.assertEqual(round(val * 2) / 2, val)

    def test_enable_loudness_preserves_profile_and_boosts_response(self):
        dsp, panel, writes = _make_dsp(volume=0.15)
        dsp.apply_profile("hifi", save=False)
        base_resp = dsp.calculate_response()
        writes.clear()

        res = dsp.action({"action": "set_filter", "filter": "loudness", "enabled": True})
        self.assertTrue(res["ok"])
        self.assertEqual(dsp.active_profile, "hifi")
        self.assertTrue(dsp.filters["loudness"])
        self.assertTrue(res["loudness"]["enabled"])
        self.assertEqual(res["loudness"]["volume"], 15)
        self.assertGreater(res["loudness"]["bass_db"], 3.0)
        self.assertGreater(res["loudness"]["treble_db"], 1.0)

        boosted_resp = dsp.calculate_response()
        idx_60 = min(range(len(base_resp["freqs"])), key=lambda i: abs(base_resp["freqs"][i] - 60.0))
        self.assertGreater(boosted_resp["system_db"][idx_60], base_resp["system_db"][idx_60] + 2.0)

    def test_update_volume_writes_only_shelves_and_skips_unchanged_steps(self):
        dsp, _panel, writes = _make_dsp(volume=0.70)
        dsp.apply_profile("hifi", save=False)
        dsp.set_filter("loudness", True, save=False)
        writes.clear()

        # Still above 55% -> no I2C writes
        r_hi = dsp.update_volume(80)
        self.assertTrue(r_hi["ok"])
        self.assertFalse(r_hi["changed"])
        self.assertEqual(writes, [])

        # Drop to 20% -> single minimal batch with soft-mute + 3D + 3C + soft-unmute
        r_lo = dsp.update_volume(20)
        self.assertTrue(r_lo["ok"])
        self.assertTrue(r_lo["changed"])
        self.assertEqual(len(writes), 1)
        batch = writes[0]
        regs = [p.split()[0] for t, _a, p in batch if t != "__SLEEP__" and p.split()[0] not in ("03", "06", "22")]
        self.assertEqual(regs, ["3D", "3C"])
        # Expected registers updated so verify_registers stays consistent
        self.assertIn((dsp_max.WOOFER_SYS, "3D"), dsp._expected_regs)
        self.assertNotIn(BYPASS_HEX, dsp._expected_regs[(dsp_max.WOOFER_SYS, "3D")])

        # Same quantized volume -> no extra I2C write
        writes.clear()
        r_same = dsp.update_volume(20)
        self.assertTrue(r_same["ok"])
        self.assertFalse(r_same["changed"])
        self.assertEqual(writes, [])

        # Return to 65% -> loudness returns to 0 dB (golden 3D bypass)
        r_flat = dsp.update_volume(65)
        self.assertTrue(r_flat["ok"])
        self.assertTrue(r_flat["changed"])
        self.assertEqual(r_flat["bass_db"], 0.0)
        self.assertEqual(r_flat["treble_db"], 0.0)
        self.assertIn(BYPASS_HEX, dsp._expected_regs[(dsp_max.WOOFER_SYS, "3D")])

    def test_loudness_respects_safe_total_caps_and_stock_reset(self):
        dsp, _panel, _writes = _make_dsp(volume=0.05)
        dsp.set_filter("loudness", True, save=False)
        dsp.apply_profile("club", save=False)
        eff_bass, eff_treble, _active, _boost = dsp._effective_shelves(
            dsp.params["bass_gain"], dsp.params["treble_gain"], True, profile_name="club", volume_pct=5
        )
        self.assertLessEqual(eff_bass, dsp_max.LOUDNESS_MAX_TOTAL_BASS)
        self.assertLessEqual(eff_treble, dsp_max.LOUDNESS_MAX_TOTAL_TREBLE)

        # Stock profile turns loudness off
        dsp.apply_profile("stock", save=False)
        self.assertFalse(dsp.filters["loudness"])
        self.assertEqual(dsp._loudness_boost(volume_pct=10), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
