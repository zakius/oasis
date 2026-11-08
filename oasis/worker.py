"""The server's background workers: each a thread that does one pass of its work, waits, and goes again for as long as
the server runs. A pass that fails is logged and the next one comes as usual."""
import logging
import threading

log = logging.getLogger(__name__)


def start(name: str, step, every: float, wake: threading.Event | None = None):
    """Runs step() in a thread, again and again. After each pass it waits what step returned, or every seconds when it
    returned None or failed; setting wake ends the wait early."""
    wake = wake or threading.Event()

    def run():
        while True:
            wait = every
            try:
                found = step()
                if found is not None:
                    wait = found
            except Exception:
                log.exception("%s failed", name)
            wake.wait(wait)
            wake.clear()
    threading.Thread(target=run, name=name, daemon=True).start()
