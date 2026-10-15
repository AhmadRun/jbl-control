"""A fake JBL Wave Buds 2 on the far end of a SEQPACKET socketpair.

GATT server with the attribute layout captured from a real Wave Buds 2 (the
excelpoint service at 0xa200, write 0xa202, notify 0xa205, CCCD 0xa206), and
a firmware emulation answering Protocol-3 frames the way the capture shows.
"""
import socket
import struct
import threading
import uuid

from jbl import att
from jbl import protocol as p

PRIMARY = 0x2800
CHAR = 0x2803

# (handle, type uuid16 or UUID, value bytes)
SERVICES = [
    (0x0001, 0x001f, uuid.UUID("00001800-0000-1000-8000-00805f9b34fb")),
    (0xa000, 0xa0ff, uuid.UUID("86868686-8686-8686-8686-868686868686")),
    (0xa200, 0xa2ff, p.SERVICE_UUID),
]
CHARS = [
    # decl, props, value, uuid
    (0xa001, 0x1c, 0xa002, uuid.UUID("97979797-9797-9797-9797-979797979797")),
    (0xa201, 0x0c, 0xa202, p.WRITE_UUID),
    (0xa204, 0x10, 0xa205, p.NOTIFY_UUID),
]
DESCRIPTORS = [(0xa003, 0x2902), (0xa203, 0x2901), (0xa206, 0x2902), (0xa207, 0x2901)]


class FakeBuds:
    def __init__(self, silent=(), refuse_modes=(), mtu=517):
        self.refuse_modes = set(refuse_modes)
        self.voice_aware = (2, 0)
        self.sav = p.SAV_FRAMES["audio"]
        self.client_sock, self.sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.mtu = mtu
        self.notify_on = False
        self.writes = []
        self.silent = set(silent)
        self.mode = "anc"
        self.eq = (3, p.EQ_PRESETS[3][1])
        self.voice_prompts = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def disconnect(self):
        """Drop the link from the buds' side, like walking out of range."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self):
        for s in (self.sock, self.client_sock):
            try:
                s.close()
            except OSError:
                pass

    # ------------------------------------------------------------ GATT server

    def _loop(self):
        try:
            while True:
                pdu = self.sock.recv(1024)
                if not pdu:
                    return
                self._handle(pdu)
        except OSError:
            return

    def _error(self, op, handle, code):
        self.sock.send(struct.pack("<BBHB", att.OP_ERROR, op, handle, code))

    def _handle(self, pdu):
        op = pdu[0]
        if op == att.OP_MTU_REQ:
            self.sock.send(struct.pack("<BH", att.OP_MTU_RSP, self.mtu))
        elif op == att.OP_FIND_BY_TYPE_REQ:
            start, end, typ = struct.unpack_from("<HHH", pdu, 1)
            want = att.uuid_from_le(pdu[7:])
            hits = [s for s in SERVICES if s[2] == want and s[0] >= start]
            if typ != PRIMARY or not hits:
                return self._error(op, start, att.ERR_ATTR_NOT_FOUND)
            self.sock.send(bytes([att.OP_FIND_BY_TYPE_RSP]) + struct.pack("<HH", hits[0][0], hits[0][1]))
        elif op == att.OP_READ_BY_TYPE_REQ:
            start, end, typ = struct.unpack_from("<HHH", pdu, 1)
            hits = [c for c in CHARS if start <= c[0] <= end]
            if typ != CHAR or not hits:
                return self._error(op, start, att.ERR_ATTR_NOT_FOUND)
            item = 2 + 1 + 2 + 16
            body = b"".join(struct.pack("<HBH", d, pr, v) + att.uuid_le(u) for d, pr, v, u in hits[:2])
            self.sock.send(bytes([att.OP_READ_BY_TYPE_RSP, item]) + body)
        elif op == att.OP_FIND_INFO_REQ:
            start, end = struct.unpack_from("<HH", pdu, 1)
            hits = [d for d in DESCRIPTORS if start <= d[0] <= end]
            if not hits:
                return self._error(op, start, att.ERR_ATTR_NOT_FOUND)
            body = b"".join(struct.pack("<HH", h, t) for h, t in hits)
            self.sock.send(bytes([att.OP_FIND_INFO_RSP, 1]) + body)
        elif op == att.OP_WRITE_REQ:
            handle = struct.unpack_from("<H", pdu, 1)[0]
            value = pdu[3:]
            self.sock.send(bytes([att.OP_WRITE_RSP]))
            if handle == 0xa206:
                self.notify_on = value[:1] == b"\x01"
                self.notify(b"\xaa\x12\x25" + b"JBL Wave Buds 2".ljust(37, b"\x00"))
            elif handle == 0xa202:
                self.writes.append(bytes(value))
                self._firmware(bytes(value))
        else:
            self._error(op, 0, att.ERR_REQ_NOT_SUPPORTED)

    def notify(self, value):
        if self.notify_on:
            self.sock.send(struct.pack("<BH", att.OP_NOTIFY, 0xa205) + value[:self.mtu - 3])

    # ------------------------------------------------------------ firmware

    def mode_report(self):
        return bytes([0xaa, 0x91, 0x07, 0x12, 1, int(self.mode == "anc"), 2, int(self.mode == "ambient"),
                      3, int(self.mode == "talkthru")])

    def _firmware(self, frame):
        cmd = frame[1]
        if cmd in self.silent:
            return
        if frame == p.notify_on():
            self.notify(bytes.fromhex("aa06050002001f0100"))
        elif frame == p.mode_get():
            self.notify(self.mode_report())
        elif cmd == p.CMD_ANC and frame[3] == 0x10:
            pairs = dict(zip(frame[4::2], frame[5::2]))
            wanted = {1: "anc", 2: "ambient", 3: "talkthru"}.get(
                next((k for k, v in pairs.items() if v), 0), "off")
            if wanted in self.refuse_modes:
                # What a Wave Buds 2 with one bud in the case does for ANC.
                self.notify(self.mode_report())
                return
            self.mode = wanted
            self.notify(self.mode_report())
            self.notify(self.mode_report())
        elif cmd == p.CMD_INFO and frame[4] == 0x01:
            body = bytes.fromhex("02 000503 000304 000601 05 001a02 0050 001b02 015a 001c02 00ff 000b03 01 1e00")
            self.notify(bytes([0xaa, 0x06]) + struct.pack("<H", len(body)) + body)
        elif frame == p.eq_get():
            cat, gains = self.eq
            body = p.eq_set(cat, gains)[4:]
            self.notify(bytes([0xaa, 0xa2]) + struct.pack("<H", len(body)) + body)
        elif cmd == p.CMD_EQ and frame[4] == 0x03:
            self.eq = (frame[5], tuple(p.decode_eq(frame[4:])["eq"]["gains"]))
        elif frame == p.voice_prompts_get():
            self.notify(bytes([0xaa, 0x93, 0x02, 0x05, int(self.voice_prompts)]))
        elif cmd == p.CMD_FIND and frame[3] == 0x01:
            self.notify(bytes.fromhex("aa2303020000"))
        elif cmd == p.CMD_VOICE_AWARE:
            if frame[3] == 0x00:
                self.voice_aware = (frame[4], frame[5] if len(frame) > 5 else 0xf8)
            self.notify(bytes([0xaa, 0x98, 0x03, 0x02]) + bytes(self.voice_aware))
        elif cmd == p.CMD_SAV:
            # Answers with its own blob and ignores the Tune Flex 2 ones, like a Wave Buds 2.
            self.notify(self.sav[:3] + b"\x02" + self.sav[4:])
        # everything else: silent, like the real firmware for unknown commands
