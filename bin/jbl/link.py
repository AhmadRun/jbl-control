"""Open the JBL control link: ATT straight to the buds' random LE address."""
import time

from . import att
from . import protocol as p


class Link:
    def __init__(self, le_address, on_bytes, on_close, sock=None, timeout=10.0):
        self.notify_handle = None
        self._on_bytes = on_bytes

        def on_notify(handle, value):
            # Until discovery finishes the only CCCD we have enabled is the
            # control one, and the buds push their name the moment it is.
            if self.notify_handle is None or handle == self.notify_handle:
                on_bytes(value)

        self.client = att.AttClient(le_address, on_notify=on_notify, on_close=on_close,
                                    timeout=timeout, sock=sock)
        try:
            self.mtu = self.client.exchange_mtu()
            ctl = att.ControlLink(self.client, p.SERVICE_UUID, p.WRITE_UUID, p.NOTIFY_UUID)
        except OSError:
            self.client.close()
            raise
        self.ctl = ctl
        self.notify_handle = ctl.notify_handle

    @property
    def closed(self):
        return self.client.closed

    def send(self, frame):
        self.ctl.send(frame)

    def close(self):
        self.client.close()


def open_link(le_address, on_bytes, on_close):
    """A raw ATT socket, after making sure bluetoothd has no object for the
    address (see bluez_link) that would take the ATT bearer from us."""
    from . import bluez_link
    if bluez_link.release(le_address):
        time.sleep(1.0)  # let the old connection drop before dialing again
    # A live advertiser answers within a second; a stale address never does,
    # and the sooner that is known the sooner the LE scan finds the real one.
    return Link(le_address, on_bytes, on_close, timeout=5.0)
