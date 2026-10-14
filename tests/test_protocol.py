import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from jbl import protocol as p  # noqa: E402

H = bytes.fromhex


class Encode(unittest.TestCase):
    # Expected bytes are the ones captured from the JBL app / a Wave Buds 2.
    def test_notify_on(self):
        self.assertEqual(p.notify_on(), H("aa9b020101"))

    def test_mode_frames(self):
        self.assertEqual(p.mode_get(), H("aa910111"))
        self.assertEqual(p.mode_set("off"), H("aa91071001000200 0300"))
        self.assertEqual(p.mode_set("anc"), H("aa91071001010200 0300"))
        self.assertEqual(p.mode_set("ambient"), H("aa91071001000201 0300"))
        self.assertEqual(p.mode_set("talkthru"), H("aa91071001000200 0301"))
        with self.assertRaises(ValueError):
            p.mode_set("loud")

    def test_anc_level_clamped(self):
        self.assertEqual(p.anc_level_set(5), H("aa9103200405"))
        self.assertEqual(p.anc_level_set(42), H("aa9103200407"))

    def test_info_get_uses_long_length(self):
        frame = p.info_get((p.FID_BATT_LEFT, p.FID_BATT_RIGHT))
        self.assertEqual(frame, H("aa06 0500 01 001a 001b"))

    def test_info_set(self):
        self.assertEqual(p.safe_sound_set(True), H("aa06 0500 0000 2001 01"))
        self.assertEqual(p.spatial_set(True, "game"), H("aa06 0600 0000 0c02 0103"))
        self.assertEqual(p.auto_off_set(True, 30), H("aa06 0700 0000 0b03 01 1e00"))

    def test_eq_get_short_length(self):
        self.assertEqual(p.eq_get(), H("aaa20201ff"))

    def test_eq_set_layout(self):
        frame = p.eq_preset_set(3)
        self.assertEqual(frame[:2], H("aaa2"))
        length = struct.unpack_from("<H", frame, 2)[0]
        self.assertEqual(length, len(frame) - 4)
        self.assertEqual(length, 16 + 10 * 10)
        decoded = p.decode_eq(frame[4:])
        self.assertEqual(decoded["eq"]["preset"], 3)
        self.assertEqual(decoded["eq"]["gains"], list(p.EQ_PRESETS[3][1]))

    def test_eq_gain_clamped(self):
        decoded = p.decode_eq(p.eq_set(p.EQ_CUSTOM, [9] * 10)[4:])
        self.assertEqual(decoded["eq"]["gains"], [6.0] * 10)
        self.assertEqual(decoded["eq"]["preset"], "custom")

    def test_misc(self):
        self.assertEqual(p.find_set(True, None), H("aa23030001ff"))
        self.assertEqual(p.voice_prompts_set(False), H("aa93020300"))
        self.assertEqual(p.standby_set(True), H("aa960500010102 00"))
        self.assertEqual(p.gestures_set([("double-tap-left", "play-pause")]), H("aa7703000308"))
        self.assertEqual(p.power_off(), H("aa9700"))

    def test_factory_reset_is_blocked(self):
        with self.assertRaises(p.BlockedCommand):
            p.encode(0x95)
        with self.assertRaises(p.BlockedCommand):
            p.encode(0x95, b"\x00", long_len=True)


class Frames(unittest.TestCase):
    def test_reassembles_split_frame(self):
        r = p.FrameReader()
        self.assertEqual(r.feed(H("aa9107120101")), [])
        frames = r.feed(H("02000300"))
        self.assertEqual(len(frames), 1)
        self.assertEqual(p.decode(frames[0]), {"mode": "anc", "modeSlots": [1, 2, 3]})

    def test_two_frames_in_one_notification(self):
        r = p.FrameReader()
        frames = r.feed(H("aa9107120100020103 00aa9107120100020003 01"))
        self.assertEqual([p.decode(f)["mode"] for f in frames], ["ambient", "talkthru"])

    def test_truncated_frame_is_surfaced(self):
        # Wave Buds 2 battery push cut at the 20-byte default ATT MTU.
        r = p.FrameReader()
        self.assertEqual(r.feed(H("aa06140002000601050 01a0200140 01b02001e00".replace(" ", ""))), [])
        frames = r.feed(H("aa910712010102000300"))
        self.assertTrue(frames[0]["truncated"])
        self.assertEqual(p.decode(frames[0])["battery"],
                         {"left": {"level": 20, "charging": False},
                          "right": {"level": 30, "charging": False}})
        self.assertEqual(p.decode(frames[1])["mode"], "anc")

    def test_garbage_before_header_skipped(self):
        r = p.FrameReader()
        frames = r.feed(H("0102aa910712010002000301"))
        self.assertEqual(p.decode(frames[0])["mode"], "talkthru")


class Decode(unittest.TestCase):
    def test_name_push(self):
        frame = p.FrameReader().feed(H("aa12 25") + b"JBL Wave Buds 2" + bytes(22))[0]
        self.assertEqual(p.decode(frame), {"name": "JBL Wave Buds 2"})

    def test_info_reply(self):
        body = H("02 000503 000304 001a0201 50 001b0200 5a 001c0200 ff 000b03 01 1e00 002001 01")
        state, fids = p.decode_info(body)
        self.assertEqual(state["firmware"], "0.3.4")
        self.assertEqual(state["battery"]["left"], {"level": 80, "charging": True})
        self.assertEqual(state["battery"]["right"], {"level": 90, "charging": False})
        self.assertIsNone(state["battery"]["case"])
        self.assertEqual(state["autoOff"], {"enabled": True, "minutes": 30})
        self.assertTrue(state["safeSound"])
        self.assertIn(p.FID_AUTO_OFF, fids)

    def test_anc_level_report(self):
        self.assertEqual(p.decode_mode(H("220403")), {"ancLevel": 3})

    def test_voice_prompt_reply(self):
        self.assertEqual(p.decode_voice_prompt(H("0501")), {"voicePrompts": True})

    def test_sav_only_known_blobs(self):
        frame = {"cmd": 0x80, "raw": p.SAV_FRAMES["video"], "payload": b"", "truncated": False}
        self.assertEqual(p.decode(frame), {"lowLatency": True})
        frame = {"cmd": 0x80, "raw": H("aa8009001122334455667788"), "payload": b"", "truncated": False}
        self.assertEqual(p.decode(frame), {})


class FastPair(unittest.TestCase):
    def test_wave_buds_2_announcement(self):
        # Verbatim from a Wave Buds 2 Message Stream: model, BLE address, battery.
        msgs, rest = p.parse_fast_pair(H("03010003ea59a0 030200064a02e449b065 03030003323c64"))
        self.assertEqual(rest, b"")
        state = {}
        for m in msgs:
            state.update(p.decode_fast_pair(*m))
        self.assertEqual(state["model"], "ea59a0")
        self.assertEqual(state["leAddress"], "4A:02:E4:49:B0:65")
        self.assertEqual(state["battery"]["left"], {"level": 50, "charging": False})
        self.assertEqual(state["battery"]["case"], {"level": 100, "charging": False})

    def test_partial_and_charging(self):
        msgs, rest = p.parse_fast_pair(H("03030003b27f64 0301"))
        self.assertEqual(rest, H("0301"))
        state = p.decode_fast_pair(*msgs[0])
        self.assertEqual(state["battery"]["left"], {"level": 50, "charging": True})
        self.assertIsNone(state["battery"]["right"])

    def test_headphones_single_battery(self):
        self.assertEqual(p.decode_fast_pair(3, 3, H("27")),
                         {"battery": {"main": {"level": 39, "charging": False}}})


if __name__ == "__main__":
    unittest.main()
