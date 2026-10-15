import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
sys.path.insert(0, os.path.dirname(__file__))

from fake_buds import FakeBuds  # noqa: E402
from jbl import device, link  # noqa: E402
from jbl import protocol as p  # noqa: E402


def wait_for(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in %.1fs" % timeout)


class LinkDiscovery(unittest.TestCase):
    def test_finds_wave_buds_2_handles(self):
        buds = FakeBuds()
        got = []
        lk = link.Link("AA:BB:CC:DD:EE:FF", got.append, lambda: None, sock=buds.client_sock, timeout=2)
        try:
            self.assertEqual(lk.ctl.write_handle, 0xa202)
            self.assertEqual(lk.notify_handle, 0xa205)
            self.assertEqual(lk.mtu, 517)
            self.assertTrue(buds.notify_on)
            wait_for(lambda: got)
            self.assertTrue(got[0].startswith(b"\xaa\x12"))
        finally:
            lk.close()
            buds.close()


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CACHE_HOME"] = self.tmp.name
        self.states = []
        self.buds = FakeBuds(silent={p.CMD_SERIAL}, refuse_modes={"anc"})

        def factory(addr, on_bytes, on_close):
            return link.Link(addr, on_bytes, on_close, sock=self.buds.client_sock, timeout=2)

        self.session = device.Session("50:1B:6A:2F:2D:FF", "JBL Wave Buds 2", self.states.append, factory,
                                      pace=0.01, reply_timeout=0.3)
        self.session.update({"model": "ea59a0"})
        self.session.start_control("4A:02:E4:49:B0:65")
        wait_for(lambda: self.session.snapshot()["control"] == "ready")

    def tearDown(self):
        self.session.stop()
        self.buds.close()
        self.tmp.cleanup()

    def test_probe_reads_state_and_features(self):
        s = self.session.snapshot()
        self.assertEqual(s["mode"], "anc")
        self.assertEqual(s["name"], "JBL Wave Buds 2")
        self.assertEqual(s["firmware"], "0.3.4")
        self.assertEqual(s["battery"]["left"], {"level": 80, "charging": False})
        self.assertEqual(s["battery"]["right"], {"level": 90, "charging": True})
        self.assertEqual(s["eq"]["preset"], 3)
        self.assertTrue(s["voicePrompts"])
        f = s["features"]
        self.assertEqual(f["modes"], ["off", "anc", "ambient", "talkthru"])
        self.assertTrue(f["eq"] and f["voicePrompts"] and f["findBuds"] and f["autoOff"])
        self.assertTrue(f["voiceAware"] and f["lowLatency"])
        self.assertFalse(f["ancLevel"] or f.get("gestures") or f["spatial"])
        self.assertEqual(s["audioMode"], "audio")

    def test_probe_cache_skips_silent_features(self):
        cache = device.load_cache()
        self.assertNotIn("serial", cache["ea59a0"]["answered"])
        self.assertIn("eq", cache["ea59a0"]["answered"])

    def test_refused_mode_reports_error_and_keeps_real_mode(self):
        self.session.submit("mode", "ambient")
        wait_for(lambda: self.session.snapshot()["mode"] == "ambient")
        self.session.submit("mode", "anc")
        wait_for(lambda: "lastError" in self.session.snapshot())
        s = self.session.snapshot()
        self.assertEqual(s["mode"], "ambient")
        self.assertIn("both buds", s["lastError"])

    def test_ignored_low_latency_is_demoted(self):
        self.session.submit("low-latency", True)
        wait_for(lambda: self.session.snapshot()["features"]["lowLatency"] is False)
        self.assertFalse(self.session.snapshot()["lowLatency"])
        self.assertNotIn("lowLatency", device.load_cache()["ea59a0"]["answered"])

    def test_voice_aware_keeps_extra_byte(self):
        self.session.submit("voice-aware", 3)
        wait_for(lambda: self.buds.voice_aware == (3, 0))
        wait_for(lambda: self.session.snapshot()["voiceAware"] == 3)

    def test_mode_command_round_trip(self):
        self.session.submit("mode", "ambient")
        wait_for(lambda: self.buds.mode == "ambient")
        wait_for(lambda: self.session.snapshot()["mode"] == "ambient")

    def test_touch_control_push_updates_state(self):
        self.buds.mode = "talkthru"
        self.buds.notify(self.buds.mode_report())
        wait_for(lambda: self.session.snapshot()["mode"] == "talkthru")

    def test_eq_preset_and_custom(self):
        self.session.submit("eq-preset", 1)
        wait_for(lambda: self.buds.eq[0] == 1)
        self.session.submit("eq-custom", [1, 2, 3, 4, 5, 6, 7, -7, 0, 0])
        wait_for(lambda: self.buds.eq[0] == p.EQ_CUSTOM)
        self.assertEqual(self.buds.eq[1], (1, 2, 3, 4, 5, 6, 6, -6, 0, 0))
        self.assertEqual(self.session.snapshot()["eq"]["preset"], "custom")

    def test_bad_command_reports_error(self):
        self.session.submit("mode", "loud")
        wait_for(lambda: "lastError" in self.session.snapshot())

    def test_link_loss_is_reported(self):
        self.buds.disconnect()
        wait_for(lambda: self.session.snapshot()["control"] in ("reconnecting", "unavailable", "connecting"))


if __name__ == "__main__":
    unittest.main()
