"""A minimal ATT (GATT client) over an LE L2CAP socket, stdlib only.

Python 3.14 accepts (address, psm, cid, address_type) for L2CAP, which is all
it takes to open the fixed ATT channel (CID 4) straight to the buds' random LE
address, the same way btgatt-client does, without needing a BlueZ device
object for an address nobody scanned for.

Only what the JBL control link needs: MTU exchange, primary service and
characteristic discovery by UUID, CCCD write, Write Request, notifications.
"""
import queue
import socket
import struct
import threading
import uuid

ATT_CID = 4
GATT_PRIMARY = 0x2800
GATT_CHARACTERISTIC = 0x2803
GATT_CCCD = 0x2902

OP_ERROR = 0x01
OP_MTU_REQ = 0x02
OP_MTU_RSP = 0x03
OP_FIND_INFO_REQ = 0x04
OP_FIND_INFO_RSP = 0x05
OP_FIND_BY_TYPE_REQ = 0x06
OP_FIND_BY_TYPE_RSP = 0x07
OP_READ_BY_TYPE_REQ = 0x08
OP_READ_BY_TYPE_RSP = 0x09
OP_WRITE_REQ = 0x12
OP_WRITE_RSP = 0x13
OP_NOTIFY = 0x1B
OP_INDICATE = 0x1D
OP_CONFIRM = 0x1E
OP_WRITE_CMD = 0x52
COMMAND_FLAG = 0x40

ERR_ATTR_NOT_FOUND = 0x0A
ERR_REQ_NOT_SUPPORTED = 0x06

WANTED_MTU = 517


class AttError(OSError):
    att_code = None


def uuid_le(u):
    return uuid.UUID(str(u)).bytes[::-1]


def uuid_from_le(b):
    if len(b) == 2:
        return uuid.UUID("0000%04x-0000-1000-8000-00805f9b34fb" % struct.unpack("<H", b)[0])
    return uuid.UUID(bytes=bytes(b[::-1]))


class AttClient:
    def __init__(self, address, address_type=socket.BDADDR_LE_RANDOM, on_notify=None,
                 on_close=None, timeout=10.0, sock=None):
        self.address = address
        self.on_notify = on_notify
        self.on_close = on_close
        self.mtu = 23
        self.timeout = timeout
        self._responses = queue.Queue()
        self._req_lock = threading.Lock()
        self._closed = threading.Event()
        if sock is not None:
            self.sock = sock  # already connected (tests use a socketpair)
        else:
            self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
            try:
                self.sock.bind((socket.BDADDR_ANY, 0, ATT_CID, socket.BDADDR_LE_PUBLIC))
                self.sock.settimeout(timeout)
                self.sock.connect((address, 0, ATT_CID, address_type))
                self.sock.settimeout(None)
            except OSError:
                self.sock.close()
                raise
        self._reader = threading.Thread(target=self._read_loop, name="att-reader", daemon=True)
        self._reader.start()

    # ------------------------------------------------------------ plumbing

    def close(self):
        if not self._closed.is_set():
            self._closed.set()
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()

    @property
    def closed(self):
        return self._closed.is_set()

    def _read_loop(self):
        try:
            while not self._closed.is_set():
                pdu = self.sock.recv(1024)
                if not pdu:
                    break
                self._dispatch(pdu)
        except OSError:
            pass
        finally:
            self._closed.set()
            self._responses.put(None)
            if self.on_close:
                self.on_close()

    def _dispatch(self, pdu):
        op = pdu[0]
        if op in (OP_NOTIFY, OP_INDICATE):
            handle = struct.unpack_from("<H", pdu, 1)[0]
            if op == OP_INDICATE:
                self._send(bytes([OP_CONFIRM]))
            if self.on_notify:
                self.on_notify(handle, bytes(pdu[3:]))
        elif op == OP_MTU_REQ:
            # The buds may negotiate too; answer so they do not stall waiting.
            self._send(struct.pack("<BH", OP_MTU_RSP, WANTED_MTU))
        elif op & COMMAND_FLAG:
            pass  # a command to our GATT server, which we do not have
        elif op % 2 == 0 and op != OP_CONFIRM:
            # A request to our (non-existent) GATT server.
            self._send(struct.pack("<BBHB", OP_ERROR, op, 0, ERR_REQ_NOT_SUPPORTED))
        else:
            self._responses.put(pdu)

    def _send(self, pdu):
        try:
            self.sock.send(pdu)
        except OSError:
            self.close()

    def request(self, pdu, expect):
        with self._req_lock:
            if self.closed:
                raise AttError("ATT link closed")
            while not self._responses.empty():
                self._responses.get_nowait()
            self._send(pdu)
            try:
                rsp = self._responses.get(timeout=self.timeout)
            except queue.Empty:
                raise AttError("ATT request 0x%02x timed out" % pdu[0]) from None
            if rsp is None:
                raise AttError("ATT link closed")
            if rsp[0] == OP_ERROR:
                err = AttError("ATT error 0x%02x for request 0x%02x" % (rsp[4] if len(rsp) > 4 else 0, pdu[0]))
                err.att_code = rsp[4] if len(rsp) > 4 else 0
                raise err
            if rsp[0] != expect:
                raise AttError("unexpected ATT response 0x%02x" % rsp[0])
            return rsp

    # ------------------------------------------------------------ GATT ops

    def exchange_mtu(self, wanted=WANTED_MTU):
        rsp = self.request(struct.pack("<BH", OP_MTU_REQ, wanted), OP_MTU_RSP)
        self.mtu = max(23, min(wanted, struct.unpack_from("<H", rsp, 1)[0]))
        return self.mtu

    def find_service(self, service_uuid):
        """(start, end) handles of a primary service, or None."""
        try:
            rsp = self.request(struct.pack("<BHHH", OP_FIND_BY_TYPE_REQ, 0x0001, 0xFFFF, GATT_PRIMARY)
                               + uuid_le(service_uuid), OP_FIND_BY_TYPE_RSP)
        except AttError as e:
            if e.att_code == ERR_ATTR_NOT_FOUND:
                return None
            raise
        if len(rsp) < 5:
            return None
        return struct.unpack_from("<HH", rsp, 1)

    def characteristics(self, start, end):
        """[(decl_handle, props, value_handle, uuid)] inside a handle range."""
        out = []
        cur = start
        while cur <= end:
            try:
                rsp = self.request(struct.pack("<BHHH", OP_READ_BY_TYPE_REQ, cur, end, GATT_CHARACTERISTIC),
                                   OP_READ_BY_TYPE_RSP)
            except AttError as e:
                if e.att_code == ERR_ATTR_NOT_FOUND:
                    break
                raise
            item = rsp[1]
            last = None
            for i in range(2, len(rsp) - item + 1, item):
                decl, props, value = struct.unpack_from("<HBH", rsp, i)
                out.append((decl, props, value, uuid_from_le(rsp[i + 5:i + item])))
                last = decl
            if last is None or last >= end:
                break
            cur = last + 1
        return out

    def find_descriptor(self, start, end, wanted=GATT_CCCD):
        cur = start
        while cur <= end:
            try:
                rsp = self.request(struct.pack("<BHH", OP_FIND_INFO_REQ, cur, end), OP_FIND_INFO_RSP)
            except AttError as e:
                if e.att_code == ERR_ATTR_NOT_FOUND:
                    return None
                raise
            step = 4 if rsp[1] == 1 else 18
            last = None
            for i in range(2, len(rsp) - step + 1, step):
                handle = struct.unpack_from("<H", rsp, i)[0]
                if step == 4 and struct.unpack_from("<H", rsp, i + 2)[0] == wanted:
                    return handle
                last = handle
            if last is None or last >= end:
                return None
            cur = last + 1
        return None

    def write(self, handle, value):
        self.request(struct.pack("<BH", OP_WRITE_REQ, handle) + bytes(value), OP_WRITE_RSP)


class ControlLink:
    """The write/notify characteristic pair of one GATT service."""

    def __init__(self, client, service_uuid, write_uuid, notify_uuid):
        self.client = client
        rng = client.find_service(service_uuid)
        if rng is None:
            raise AttError("control service not found")
        chars = client.characteristics(*rng)
        by_uuid = {c[3]: c for c in chars}
        w = by_uuid.get(uuid.UUID(str(write_uuid)))
        n = by_uuid.get(uuid.UUID(str(notify_uuid)))
        if not w or not n:
            raise AttError("control characteristics not found")
        self.write_handle = w[2]
        self.notify_handle = n[2]
        following = [c[0] for c in chars if c[0] > n[0]]
        cccd = client.find_descriptor(n[2] + 1, (min(following) - 1) if following else rng[1])
        if cccd is None:
            raise AttError("notify descriptor not found")
        client.write(cccd, b"\x01\x00")

    def send(self, frame):
        self.client.write(self.write_handle, frame)
