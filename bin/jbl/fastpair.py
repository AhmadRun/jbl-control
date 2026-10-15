"""BlueZ side: find connected JBL devices and hold their Fast Pair stream.

Runs on the GLib main loop. The Fast Pair Message Stream is an RFCOMM service
the buds announce over SDP; registering a client profile for its UUID makes
BlueZ do the SDP lookup and hand us the connected socket. On every connect the
buds push their Fast Pair model id, the current (rotating) BLE address the
control service lives on, and left/right/case battery.
"""
import os
import socket

import dbus
import dbus.service
from gi.repository import GLib

from . import protocol as p

BLUEZ = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
BATTERY_IFACE = "org.bluez.Battery1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROFILE_PATH = "/io/github/ahmadrun/jblcontrol/fastpair"


def address_from_path(path):
    tail = str(path).rsplit("/", 1)[-1]
    return tail[4:].replace("_", ":") if tail.startswith("dev_") else None


def is_jbl(props):
    """A paired JBL headset (its classic identity, not the "-LE" advertiser)."""
    if str(props.get("AddressType", "public")) != "public":
        return False
    modalias = str(props.get("Modalias", ""))
    name = str(props.get("Name", props.get("Alias", "")))
    if name.endswith("-LE"):
        return False
    return modalias.lower().startswith("bluetooth:v%04x" % p.JBL_VENDOR_ID) or name.upper().startswith("JBL")


def is_control_advertiser(props, classic_name):
    """The buds' control side: a random-address LE advertiser named "<name>-LE"
    (seen on Wave Buds 2 and TUNE230NC), or carrying Harman's service data."""
    if str(props.get("AddressType", "")) != "random":
        return False
    name = str(props.get("Name", props.get("Alias", "")))
    if classic_name and name == classic_name + "-LE":
        return True
    service_data = props.get("ServiceData") or {}
    return name.upper().startswith("JBL") and any(str(k).lower() == p.HARMAN_SERVICE for k in service_data)


class Profile(dbus.service.Object):
    def __init__(self, bus, watcher):
        super().__init__(bus, PROFILE_PATH)
        self.watcher = watcher

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, device, fd, _props):
        self.watcher._stream_opened(address_from_path(device), fd.take())

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, device):
        self.watcher._stream_closed(address_from_path(device))

    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        pass


class Stream:
    def __init__(self, address, fd, on_fragment, on_close):
        self.address = address
        self.sock = socket.socket(fileno=fd)
        self.sock.setblocking(False)
        self.buf = b""
        self.on_fragment = on_fragment
        self.on_close = on_close
        self.watch = GLib.io_add_watch(fd, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._on_io)

    def _on_io(self, _fd, cond):
        if cond & GLib.IO_IN:
            try:
                data = self.sock.recv(4096)
            except BlockingIOError:
                return True
            except OSError:
                data = b""
            if data:
                msgs, self.buf = p.parse_fast_pair(self.buf + data)
                for m in msgs:
                    fragment = p.decode_fast_pair(*m)
                    if fragment:
                        self.on_fragment(self.address, fragment)
                return True
        self.close()
        self.on_close(self.address)
        return False

    def close(self):
        if self.watch:
            GLib.source_remove(self.watch)
            self.watch = None
        try:
            self.sock.close()
        except OSError:
            pass


class BluezWatcher:
    """Calls back on the GLib thread:

        on_device(address, name)        a JBL device connected
        on_gone(address)                it disconnected
        on_fragment(address, fragment)  Fast Pair / BlueZ battery facts
    """

    def __init__(self, bus, on_device, on_gone, on_fragment, log=None):
        self.bus = bus
        self.on_device = on_device
        self.on_gone = on_gone
        self.on_fragment = on_fragment
        self.log = log or (lambda *a: None)
        self.devices = {}   # address -> props (only JBL)
        self.connected = set()
        self.streams = {}
        self.scan = None
        self.profile = Profile(bus, self)
        mgr = dbus.Interface(bus.get_object(BLUEZ, "/org/bluez"), "org.bluez.ProfileManager1")
        try:
            mgr.RegisterProfile(PROFILE_PATH, p.FAST_PAIR_STREAM_UUID,
                                {"Role": "client", "Name": "jbl-control fast pair"})
            self.profile_ok = True
        except dbus.DBusException as e:
            self.log("fast pair profile:", e)
            self.profile_ok = False
        bus.add_signal_receiver(self._props_changed, dbus_interface=PROPS_IFACE,
                                signal_name="PropertiesChanged", bus_name=BLUEZ, path_keyword="path")
        bus.add_signal_receiver(self._added, dbus_interface=OM_IFACE, signal_name="InterfacesAdded",
                                bus_name=BLUEZ)
        om = dbus.Interface(bus.get_object(BLUEZ, "/"), OM_IFACE)
        for path, ifaces in om.GetManagedObjects().items():
            self._added(path, ifaces)

    # ------------------------------------------------------------ BlueZ objects

    def _added(self, path, ifaces):
        dev = ifaces.get(DEVICE_IFACE)
        if dev is None:
            return
        if self.scan and is_control_advertiser(dev, self.scan["name"]) and dev.get("RSSI") is not None:
            self._scan_found(str(dev.get("Address", address_from_path(path))))
            return
        if not is_jbl(dev):
            return
        address = str(dev.get("Address", address_from_path(path)))
        self.devices[address] = {"path": str(path), "name": str(dev.get("Alias", dev.get("Name", "JBL")))}
        if BATTERY_IFACE in ifaces:
            self._bluez_battery(address, ifaces[BATTERY_IFACE].get("Percentage"))
        if bool(dev.get("Connected", False)):
            self._connected(address)

    def _props_changed(self, iface, changed, _invalidated, path=None):
        address = address_from_path(path)
        if address is None:
            return
        if iface == DEVICE_IFACE:
            if self.scan and address not in self.devices and ("RSSI" in changed or "ServiceData" in changed):
                # Seen again during our scan (BlueZ kept the object from an earlier one).
                try:
                    props = dbus.Interface(self.bus.get_object(BLUEZ, path), PROPS_IFACE).GetAll(DEVICE_IFACE)
                except dbus.DBusException:
                    return
                if is_control_advertiser(props, self.scan["name"]):
                    self._scan_found(str(props.get("Address", address)))
                return
            if address not in self.devices:
                try:
                    props = dbus.Interface(self.bus.get_object(BLUEZ, path), PROPS_IFACE).GetAll(DEVICE_IFACE)
                except dbus.DBusException:
                    return
                self._added(path, {DEVICE_IFACE: props})
                return
            if "Connected" in changed:
                if changed["Connected"]:
                    self._connected(address)
                else:
                    self._disconnected(address)
        elif iface == BATTERY_IFACE and address in self.devices and "Percentage" in changed:
            self._bluez_battery(address, changed["Percentage"])

    def _bluez_battery(self, address, pct):
        if pct is not None:
            self.on_fragment(address, {"bluezBattery": int(pct)})

    def _connected(self, address):
        if address in self.connected:
            return
        self.connected.add(address)
        self.on_device(address, self.devices[address]["name"])
        # Give the audio profiles a moment before asking for another channel.
        GLib.timeout_add(1500, lambda: (self.open_stream(address), False)[1])

    def _disconnected(self, address):
        self.connected.discard(address)
        stream = self.streams.pop(address, None)
        if stream:
            stream.close()
        self.on_gone(address)

    # ------------------------------------------------------------ Fast Pair stream

    def _device(self, address):
        return dbus.Interface(self.bus.get_object(BLUEZ, self.devices[address]["path"]), DEVICE_IFACE)

    def open_stream(self, address):
        if not self.profile_ok or address not in self.connected or address in self.streams:
            return
        self._device(address).ConnectProfile(
            p.FAST_PAIR_STREAM_UUID,
            reply_handler=lambda: None,
            error_handler=lambda e: self.log("fast pair connect:", e))

    def refresh_stream(self, address):
        """Drop and reopen the stream: the only way to hear the current LE address."""
        stream = self.streams.pop(address, None)
        if stream:
            stream.close()
        if address not in self.connected:
            return

        def reopen(*_):
            GLib.timeout_add(800, lambda: (self.open_stream(address), False)[1])

        self._device(address).DisconnectProfile(p.FAST_PAIR_STREAM_UUID, reply_handler=reopen,
                                                error_handler=reopen)

    # ------------------------------------------------------------ LE address scan

    def find_le_address(self, address, callback, timeout=12):
        """Scan briefly for the buds' control advertiser and call back with its
        address (or None). The Fast Pair announcement goes stale once the buds
        rotate their address; the advertisement never does."""
        if self.scan or address not in self.devices:
            return False
        adapter_path = self.devices[address]["path"].rsplit("/", 1)[0]
        adapter = dbus.Interface(self.bus.get_object(BLUEZ, adapter_path), "org.bluez.Adapter1")
        self.scan = {"address": address, "name": self.devices[address]["name"], "callback": callback,
                     "adapter": adapter}
        try:
            adapter.SetDiscoveryFilter({"Transport": "le", "DuplicateData": dbus.Boolean(True)})
            adapter.StartDiscovery()
        except dbus.DBusException as e:
            self.log("le scan:", e)
            self._scan_done(None)
            return False
        self.scan["timer"] = GLib.timeout_add_seconds(timeout, lambda: (self._scan_done(None), False)[1])
        self.log("le scan started for", address)
        return True

    def _scan_found(self, le_address):
        self.log("le advertiser", le_address)
        self._scan_done(le_address)

    def _scan_done(self, le_address):
        scan, self.scan = self.scan, None
        if not scan:
            return
        if scan.get("timer") and le_address is not None:
            GLib.source_remove(scan["timer"])
        try:
            scan["adapter"].StopDiscovery()
        except dbus.DBusException:
            pass
        try:
            scan["adapter"].SetDiscoveryFilter({})
        except dbus.DBusException:
            pass
        scan["callback"](le_address)

    def _stream_opened(self, address, fd):
        if address not in self.devices:
            os.close(fd)
            return
        old = self.streams.pop(address, None)
        if old:
            old.close()
        self.streams[address] = Stream(address, fd, self.on_fragment, self._stream_closed)
        self.log("fast pair stream open", address)

    def _stream_closed(self, address):
        stream = self.streams.pop(address, None)
        if stream:
            stream.close()
