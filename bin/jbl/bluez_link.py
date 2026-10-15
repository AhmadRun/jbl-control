"""Keep bluetoothd off the buds' control link.

Our LE scan leaves BlueZ a temporary device object for the buds' "-LE"
address. While that object exists, bluetoothd attaches its own GATT client to
any connection to the address and takes the ATT bearer (the kernel allows one
per connection), so our raw ATT socket is shut out; the buds also reject
BlueZ's GATT client (ATT error 0x0e), so going through BlueZ is no way out
either. Removing the object before connecting keeps the link ours.

Only ever touches random-address objects, never the paired headset.
Uses a private blocking D-Bus connection: callers are on worker threads.
"""
import dbus

BLUEZ = "org.bluez"
PROPS = "org.freedesktop.DBus.Properties"
DEVICE = "org.bluez.Device1"


def release(le_address):
    """Disconnect and forget BlueZ's object for an LE address, if it has one."""
    bus = dbus.SystemBus(private=True)
    try:
        om = dbus.Interface(bus.get_object(BLUEZ, "/"), "org.freedesktop.DBus.ObjectManager")
        suffix = "/dev_" + le_address.upper().replace(":", "_")
        for path, ifaces in om.GetManagedObjects().items():
            dev = ifaces.get(DEVICE)
            if not str(path).endswith(suffix) or dev is None:
                continue
            if str(dev.get("AddressType", "")) != "random" or bool(dev.get("Paired", False)):
                return False
            if bool(dev.get("Connected", False)):
                try:
                    dbus.Interface(bus.get_object(BLUEZ, path), DEVICE).Disconnect()
                except dbus.DBusException:
                    pass
            adapter = dbus.Interface(bus.get_object(BLUEZ, str(path).rsplit("/", 1)[0]), "org.bluez.Adapter1")
            adapter.RemoveDevice(path)
            return True
        return False
    except dbus.DBusException:
        return False
    finally:
        bus.close()
