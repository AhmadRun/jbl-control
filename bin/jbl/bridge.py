"""jbl-bridge: one Bluetooth session shared by every bar and by jblctl.

    jbl-bridge [--address AA:BB:..] [--daemon [--linger SECONDS]] [--verbose]

The first instance to take the lock in $XDG_RUNTIME_DIR becomes the owner: it
holds the Fast Pair stream and the BLE control link, prints one JSON state
line per change on stdout, takes one command per line on stdin, and serves the
same over a Unix socket. Every later instance (another monitor's bar, jblctl)
is a relay that copies stdin to the socket and the socket to stdout. When the
owner goes away the relays race for the lock and one of them takes over.

The widget runs one without --daemon, so it lives exactly as long as the bar.
jblctl starts a --daemon owner with a linger timeout when none is running.
"""
import argparse
import ctypes
import fcntl
import json
import os
import selectors
import signal
import socket
import sys
import time

RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
SOCK_PATH = os.path.join(RUNTIME, "jbl-control.sock")
LOCK_PATH = os.path.join(RUNTIME, "jbl-control.lock")


def log_to_stderr(verbose):
    def log(*args):
        if verbose:
            print("jbl-bridge:", *args, file=sys.stderr, flush=True)
    return log


def die_with_parent():
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def try_lock():
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def try_connect():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(SOCK_PATH)
        return s
    except OSError:
        s.close()
        return None


# ---------------------------------------------------------------- relay

def relay(sock, use_stdin):
    """Copy stdin -> owner and owner -> stdout. Returns 'owner-gone' or 'stdin-closed'."""
    sel = selectors.DefaultSelector()
    sel.register(sock, selectors.EVENT_READ, "sock")
    if use_stdin:
        os.set_blocking(sys.stdin.fileno(), False)
        sel.register(sys.stdin.fileno(), selectors.EVENT_READ, "stdin")
    out = sys.stdout.buffer
    try:
        while True:
            for key, _ in sel.select():
                if key.data == "sock":
                    data = sock.recv(65536)
                    if not data:
                        return "owner-gone"
                    out.write(data)
                    out.flush()
                else:
                    try:
                        data = os.read(sys.stdin.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        return "stdin-closed"
                    sock.sendall(data)
    except (BrokenPipeError, ConnectionResetError):
        return "owner-gone"
    finally:
        sel.close()
        sock.close()


# ---------------------------------------------------------------- owner

class Owner:
    def __init__(self, args, log):
        import dbus
        import dbus.mainloop.glib
        from gi.repository import GLib
        from . import device, fastpair, link

        self.GLib = GLib
        self.device = device
        self.link = link
        self.args = args
        self.log = log
        self.loop = GLib.MainLoop()
        self.sessions = {}
        self.names = {}
        self.selected = args.address.upper() if args.address else None
        self.clients = {}       # socket -> read buffer
        self.pending = False
        self.last_line = None
        self.last_client = time.monotonic()

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.watcher = fastpair.BluezWatcher(self.bus, self._on_device, self._on_gone,
                                             self._on_fragment, log=log)

        try:
            os.unlink(SOCK_PATH)
        except FileNotFoundError:
            pass
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o600)
        self.server.listen(8)
        self.server.setblocking(False)
        GLib.io_add_watch(self.server.fileno(), GLib.IO_IN, self._accept)
        if args.stdin:
            os.set_blocking(sys.stdin.fileno(), False)
            self.stdin_buf = b""
            GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP, self._on_stdin)
        if args.linger:
            GLib.timeout_add_seconds(5, self._check_linger)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self.quit)
        self._schedule()

    # ------------------------------------------------------------ devices

    def active(self):
        if self.selected and self.selected in self.sessions:
            return self.sessions[self.selected]
        return next(iter(self.sessions.values()), None)

    def _on_device(self, address, name):
        self.names[address] = name
        if address not in self.sessions:
            s = self.device.Session(address, name, lambda _snap: self._schedule(), self.link.open_link,
                                    log=self.log)
            s.on_control_failed = lambda sess: self.GLib.idle_add(self._find_le, sess.address)
            self.sessions[address] = s
        self._schedule()

    def _on_gone(self, address):
        s = self.sessions.pop(address, None)
        if s:
            s.stop()
        self._schedule()

    def _find_le(self, address):
        """The control link failed: look for the address the buds advertise now."""
        def found(le_address):
            s = self.sessions.get(address)
            if s is None:
                return
            if le_address:
                s.start_control(le_address)
            else:
                # Nothing advertised: a fresh Fast Pair announcement is the other source.
                self.watcher.refresh_stream(address)

        if address in self.sessions:
            self.watcher.find_le_address(address, found)
        return False

    def _on_fragment(self, address, fragment):
        s = self.sessions.get(address)
        if s is None:
            return
        fragment = dict(fragment)
        le = fragment.pop("leAddress", None)
        s.update(fragment)
        if le and s.snapshot().get("control") != "ready":
            s.start_control(le)
            # The announced address is often one rotation behind; scan alongside
            # so the advertised one is at hand if this attempt times out.
            self._find_le(address)

    # ------------------------------------------------------------ output

    def _schedule(self):
        # Coalesce bursts of updates (and hop onto the GLib thread) into one line.
        if not self.pending:
            self.pending = True
            self.GLib.idle_add(self._flush)

    def state_line(self):
        s = self.active()
        state = s.snapshot() if s else {"connected": False, "features": {}, "battery": {}}
        state["type"] = "state"
        state["devices"] = [{"address": a, "name": self.names.get(a, "JBL")} for a in self.sessions]
        state["selected"] = s.address if s else None
        state["fastPair"] = self.watcher.profile_ok
        return json.dumps(state, sort_keys=True) + "\n"

    def _flush(self):
        self.pending = False
        line = self.state_line()
        if line != self.last_line:
            self.last_line = line
            self.broadcast(line)
        return False

    def broadcast(self, line):
        if self.args.stdin:
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except BrokenPipeError:
                self.quit()
        for c in list(self.clients):
            self._send(c, line)

    def _send(self, client, line):
        try:
            client.sendall(line.encode())
        except OSError:
            self._drop(client)

    def _error(self, client, message):
        line = json.dumps({"type": "error", "message": message}) + "\n"
        if client is None:
            self.broadcast(line)
        else:
            self._send(client, line)

    # ------------------------------------------------------------ input

    def _accept(self, *_):
        try:
            c, _ = self.server.accept()
        except OSError:
            return True
        c.setblocking(False)
        self.clients[c] = b""
        self.last_client = time.monotonic()
        self.GLib.io_add_watch(c.fileno(), self.GLib.IO_IN | self.GLib.IO_HUP | self.GLib.IO_ERR,
                               lambda *_a: self._on_client(c))
        self._send(c, self.state_line())
        return True

    def _on_client(self, c):
        if c not in self.clients:
            return False
        try:
            data = c.recv(4096)
        except BlockingIOError:
            return True
        except OSError:
            data = b""
        if not data:
            self._drop(c)
            return False
        buf = self.clients[c] + data
        *lines, self.clients[c] = buf.split(b"\n")
        for line in lines:
            self.command(line.decode("utf-8", "replace"), c)
        return True

    def _drop(self, c):
        if self.clients.pop(c, None) is not None:
            self.last_client = time.monotonic()
            try:
                c.close()
            except OSError:
                pass

    def _on_stdin(self, _fd, cond):
        try:
            data = os.read(sys.stdin.fileno(), 4096)
        except BlockingIOError:
            return True
        if not data:
            self.quit()
            return False
        buf = self.stdin_buf + data
        *lines, self.stdin_buf = buf.split(b"\n")
        for line in lines:
            self.command(line.decode("utf-8", "replace"), None)
        return True

    def command(self, line, client):
        from .commands import CommandError, parse
        if not line.strip():
            return
        s = self.active()
        try:
            op = parse(line, s.snapshot() if s else {})
        except (CommandError, ValueError) as e:
            return self._error(client, str(e))
        if op[0] == "status":
            line = self.state_line()
            return self._send(client, line) if client else self.broadcast(line)
        if op[0] == "select":
            if op[1] not in self.sessions:
                return self._error(client, "no connected JBL device %s" % op[1])
            self.selected = op[1]
            self._schedule()
            return self._done(client, None)
        if s is None:
            return self._error(client, "no JBL device connected")
        if op[0] == "refresh":
            self.watcher.refresh_stream(s.address)
            self._find_le(s.address)
            return self._done(client, None)
        if s.snapshot().get("control") == "unavailable":
            return self._error(client, "the buds' controls are not reachable right now")
        # While the link is (re)connecting the command waits in the queue.
        s.submit(*op, done=lambda err: self.GLib.idle_add(self._done, client, err))

    def _done(self, client, error):
        """Tell the client that sent a command how it went (the widget reads
        lastError from the state instead)."""
        if client is not None and client in self.clients:
            self._send(client, json.dumps({"type": "done", "error": error}) + "\n")
        return False

    # ------------------------------------------------------------ lifetime

    def _check_linger(self):
        if not self.clients and time.monotonic() - self.last_client > self.args.linger:
            self.quit()
            return False
        return True

    def quit(self, *_):
        for s in self.sessions.values():
            s.stop()
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass
        self.loop.quit()
        return False

    def run(self):
        self.loop.run()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jbl-bridge")
    ap.add_argument("--address", default="")
    ap.add_argument("--daemon", action="store_true", help="no stdin/stdout; serve the socket only")
    ap.add_argument("--linger", type=int, default=0, help="with --daemon: exit after N idle seconds")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    args.stdin = not args.daemon
    log = log_to_stderr(args.verbose)
    if args.stdin:
        die_with_parent()

    while True:
        lock = try_lock()
        if lock is not None:
            log("owner")
            Owner(args, log).run()
            return 0
        if args.daemon:
            return 0  # an owner already serves the socket
        sock = try_connect()
        if sock is None:
            time.sleep(0.3)
            continue
        log("relay")
        if args.address:
            sock.sendall(("select %s\n" % args.address).encode())
        if relay(sock, args.stdin) == "stdin-closed":
            return 0
        time.sleep(0.2)
