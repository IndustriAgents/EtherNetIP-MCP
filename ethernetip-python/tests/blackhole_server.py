"""Run the real ethernetip-mcp CLI against a fake controller the tests can black-hole.

Used by test_cancellation.py over stdio. While the file named by BH_GATE does
not exist, the controller is unreachable: open()/read()/write() wait up to
BH_WAIT seconds for it, then fail like a socket timeout. Every write that
reaches the controller is appended to the file named by BH_WRITES.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_pycomm3 import FakeController, FakeLogixDriver  # noqa: E402
from pycomm3 import CommError  # noqa: E402

import ethernetip_mcp.eip_client as eip_client  # noqa: E402
from ethernetip_mcp.cli import main  # noqa: E402

GATE = Path(os.environ["BH_GATE"])
WRITES = Path(os.environ["BH_WRITES"])
WAIT = float(os.environ.get("BH_WAIT", "1.5"))


def wait_for_peer() -> None:
    end = time.monotonic() + WAIT
    while not GATE.exists():
        if time.monotonic() > end:
            raise CommError("failed to receive reply: timed out")
        time.sleep(0.02)


class BlackHoleDriver(FakeLogixDriver):
    def open(self, *args, **kwargs):
        wait_for_peer()
        return super().open(*args, **kwargs)

    def read(self, *args, **kwargs):
        wait_for_peer()
        return super().read(*args, **kwargs)

    def write(self, *args, **kwargs):
        wait_for_peer()
        result = super().write(*args, **kwargs)
        with WRITES.open("a") as log:
            log.write(f"{time.time():.3f} {args!r}\n")
        return result


controller = FakeController()


def factory(path: str) -> BlackHoleDriver:
    driver = BlackHoleDriver(path)
    driver.controller = controller
    controller.drivers.append(driver)
    return driver


eip_client.LogixDriver = factory  # what EIPClient uses when no factory is passed

if __name__ == "__main__":
    main([])
