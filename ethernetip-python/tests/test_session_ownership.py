"""Cancellation cannot interrupt the next native session owner."""

import threading

from ethernetip_mcp.eip_client import EIPClient, EIPClientConfig, _CallState


def test_stale_holding_snapshot_cannot_close_a_later_calls_socket():
    client = EIPClient(EIPClientConfig())
    old, new = _CallState(), _CallState()
    old_held, release_old, new_held, release_new = (threading.Event() for _ in range(4))
    closed = []

    def first():
        with client._session(old):
            old_held.set()
            release_old.wait(2)

    def second():
        with client._session(new):
            new_held.set()
            release_new.wait(2)

    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start()
    assert old_held.wait(2)
    _, holding = old.cancel()
    assert holding is True
    b.start()
    release_old.set()
    assert new_held.wait(2)
    try:
        old.interrupt_if_owned(lambda: closed.append("new socket"))
        assert closed == []
    finally:
        release_new.set()
        a.join(2)
        b.join(2)
    assert not a.is_alive() and not b.is_alive()


def test_interruption_holds_ownership_until_socket_is_closed():
    state = _CallState()
    state.set_holding(True)
    interrupting, release_interrupt, handed_off = (threading.Event() for _ in range(3))

    def close_socket():
        interrupting.set()
        release_interrupt.wait(2)

    def handoff():
        state.set_holding(False)
        handed_off.set()

    a = threading.Thread(target=lambda: state.interrupt_if_owned(close_socket))
    a.start()
    assert interrupting.wait(2)
    b = threading.Thread(target=handoff)
    b.start()
    try:
        assert not handed_off.wait(0.05)
    finally:
        release_interrupt.set()
        a.join(2)
        b.join(2)
    assert handed_off.is_set()


def test_failed_attempt_cannot_reset_a_session_used_after_its_handoff(monkeypatch):
    from contextlib import contextmanager

    from fake_pycomm3 import FakeController
    from pycomm3 import CommError

    from ethernetip_mcp.eip_client import EIPClientError

    controller = FakeController()
    client = EIPClient(EIPClientConfig(max_retries=0), driver_factory=controller.factory)
    old, new = _CallState(), _CallState()
    handed_off, next_completed = threading.Event(), threading.Event()
    original_session = client._session
    outcomes = []

    @contextmanager
    def session(state=None, timeout=None):
        try:
            with original_session(state, timeout):
                yield
        finally:
            if state is old:
                handed_off.set()
                next_completed.wait(2)

    monkeypatch.setattr(client, "_session", session)

    def fail(driver):
        raise CommError("reply lost")

    def first():
        try:
            client._execute_sync("old write", fail, False, old)
        except EIPClientError as exc:
            outcomes.append(exc.meta["outcome"])

    def second():
        if handed_off.wait(2):
            client._execute_sync("new read", lambda driver: driver.read("Batch_Count"), True, new)
        next_completed.set()

    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start()
    b.start()
    a.join(3)
    b.join(3)
    try:
        assert outcomes == ["unknown"]
        assert next_completed.is_set()
        assert client._driver is not None and client._driver.connected
        assert not a.is_alive() and not b.is_alive()
    finally:
        client._disconnect_sync()
