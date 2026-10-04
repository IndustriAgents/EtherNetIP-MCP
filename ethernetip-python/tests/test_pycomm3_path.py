"""The CIP path: how the client drives pycomm3's LogixDriver.

No controller is available, so most tests use ``FakeLogixDriver``, which binds
every call against the installed pycomm3 signatures (see fake_pycomm3.py). The
port/timeout/retry tests at the end run the real ``LogixDriver`` against a
local TCP listener that never answers.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator

import pytest
from fake_pycomm3 import REAL, FakeController, FakeLogixDriver, bind_real
from pycomm3 import CommError, LogixDriver, RequestError

from ethernetip_mcp.eip_client import EIPClient, EIPClientConfig, EIPClientError

FAST = {"max_retries": 0, "retry_backoff_base": 0.0}


def make_client(controller: FakeController, **config: object) -> EIPClient:
    return EIPClient(EIPClientConfig(**{**FAST, **config}), driver_factory=controller.factory)


# -- the fake really enforces pycomm3's signatures --------------------------


@pytest.mark.parametrize(
    "method, args, kwargs",
    [
        ("write", ("MotorSpeed", 1.0), {"datatype": None}),  # old write_tag
        ("read", ("Tank_Levels",), {"count": 3}),  # old read_array
        ("write", (), {"MotorSpeed": 1.0, "Batch_Count": 2}),  # old write_multiple_tags
    ],
)
def test_old_call_shapes_are_rejected_by_real_signatures(method: str, args: tuple, kwargs: dict) -> None:
    with pytest.raises(TypeError):
        bind_real(method, *args, **kwargs)
    driver = FakeController().factory("10.0.0.5")
    driver.open()
    with pytest.raises(TypeError):
        getattr(driver, method)(*args, **kwargs)


def test_constructor_kwargs_are_silently_dropped_by_pycomm3() -> None:
    """Why ENIP_PORT/ENIP_TIMEOUT used to do nothing (and why the fake refuses them)."""
    real = REAL("10.0.0.5", port=5025, timeout=1.0, micro800=True)
    assert real._cfg["port"] == 44818
    assert real._cfg["socket_timeout"] == 5.0
    with pytest.raises(TypeError, match="silently ignores"):
        FakeLogixDriver("10.0.0.5", port=5025)


def test_public_socket_timeout_setter_does_not_reach_the_socket() -> None:
    """pycomm3 1.2.14's setter writes _cfg['socket_timout']; open() reads 'socket_timeout'."""
    driver = REAL("10.0.0.5")
    driver.socket_timeout = 1.0
    assert driver._cfg["socket_timeout"] == 5.0


# -- connection settings reach pycomm3 ---------------------------------------


def test_port_timeout_and_slot_reach_the_driver() -> None:
    controller = FakeController()
    client = make_client(controller, host="10.0.0.5", port=44819, slot=2, timeout=1.25)
    client._connect_once()
    driver = controller.drivers[0]
    assert driver._cfg["ip address"] == "10.0.0.5"
    assert driver._cfg["port"] == 44819
    assert driver._cfg["socket_timeout"] == 1.25
    assert [(seg.port, str(seg.link_address)) for seg in driver._cfg["cip_path"]] == [("bp", "2")]
    assert controller.all_calls("open") == [("open", 44819, 1.25)]


def test_route_from_enip_path() -> None:
    controller = FakeController()
    config = EIPClientConfig.from_env({"ENIP_PATH": "10.0.0.5/backplane/3/enet/192.168.1.20", "ENIP_PORT": "2222"})
    EIPClient(config, driver_factory=controller.factory)._connect_once()
    driver = controller.drivers[0]
    assert driver._cfg["port"] == 2222
    assert [(seg.port, str(seg.link_address)) for seg in driver._cfg["cip_path"]] == [
        ("backplane", "3"),
        ("enet", "192.168.1.20"),
    ]


def test_micro800_matching_controller_connects() -> None:
    controller = FakeController(product_name="2080-LC50-24QWB")
    client = make_client(controller, host="10.0.0.9", micro800=True)
    client._connect_once()
    status = client.connection_status()
    assert status["micro800_detected"] is True
    assert status["connection_path"] == "10.0.0.9:44818"


async def test_micro800_mismatch_is_refused_without_retrying() -> None:
    controller = FakeController(product_name="1756-L83E/B")
    client = make_client(controller, micro800=True, max_retries=3)
    with pytest.raises(EIPClientError, match="ENIP_MICRO800=true.*1756-L83E/B") as info:
        await client.read_tag("MotorSpeed")
    assert info.value.meta["attempts"] == 1
    assert controller.opens == 1
    assert controller.all_calls("close")  # the session was not left open


def test_micro800_is_detected_even_when_not_configured() -> None:
    controller = FakeController(product_name="2080-LC30")
    client = make_client(controller)
    client._connect_once()
    assert client.connection_status()["micro800_detected"] is True


# -- call shapes --------------------------------------------------------------


async def test_read_tag() -> None:
    controller = FakeController()
    result, meta = await make_client(controller).read_tag("MotorSpeed")
    assert result == {"tag": "MotorSpeed", "value": 1450.0, "data_type": "REAL"}
    assert meta["backend"] == "cip" and meta["attempts"] == 1
    assert controller.all_calls("read") == [("read", ("MotorSpeed",))]


async def test_read_array_uses_element_count_syntax() -> None:
    controller = FakeController()
    client = make_client(controller)
    result, _ = await client.read_tag("Tank_Levels", 2)
    assert result == {"tag": "Tank_Levels", "value": [32.4, 31.9], "data_type": "REAL[2]", "elements": 2}
    single, _ = await client.read_tag("Tank_Levels", 1)
    assert single["value"] == [32.4]  # pycomm3 returns a bare value; read_array returns a list
    assert controller.all_calls("read") == [("read", ("Tank_Levels{2}",)), ("read", ("Tank_Levels{1}",))]


async def test_read_count_in_tag_name_and_conflicts() -> None:
    controller = FakeController()
    client = make_client(controller)
    result, _ = await client.read_tag("Tank_Levels{3}")
    assert result["elements"] == 3
    with pytest.raises(ValueError, match="asks for 3 elements but count is 2"):
        await client.read_tag("Tank_Levels{3}", 2)
    with pytest.raises(ValueError, match="malformed"):
        await client.read_tag("Tank_Levels{x}")


async def test_read_error_from_controller_is_a_failure_not_a_value() -> None:
    controller = FakeController()
    with pytest.raises(EIPClientError, match="Tag doesn't exist - Nope") as info:
        await make_client(controller, max_retries=3).read_tag("Nope")
    assert info.value.meta["attempts"] == 1  # not retried


async def test_read_multiple_tags_reports_each_result() -> None:
    controller = FakeController()
    results, _ = await make_client(controller).read_multiple_tags(["MotorSpeed", "Nope", "Tank_Levels{2}"])
    assert results == [
        {"tag": "MotorSpeed", "value": 1450.0, "data_type": "REAL", "error": None},
        {"tag": "Nope", "value": None, "data_type": None, "error": "Tag doesn't exist - Nope"},
        {"tag": "Tank_Levels", "value": [32.4, 31.9], "data_type": "REAL[2]", "error": None},
    ]
    assert controller.all_calls("read") == [("read", ("MotorSpeed", "Nope", "Tank_Levels{2}"))]


async def test_write_tag_passes_a_tag_value_tuple() -> None:
    controller = FakeController()
    client = make_client(controller)
    result, meta = await client.write_tag("MotorSpeed", 1200.0, "REAL")
    assert result == {"tag": "MotorSpeed", "value": 1200.0, "data_type": "REAL"}
    assert "warning" not in meta
    assert controller.all_calls("write") == [("write", (("MotorSpeed", 1200.0),))]
    assert controller.tags["MotorSpeed"] == (1200.0, "REAL")


async def test_write_list_uses_element_count_syntax() -> None:
    controller = FakeController()
    result, _ = await make_client(controller).write_tag("Tank_Levels", [1.0, 2.0])
    assert result["data_type"] == "REAL[2]"
    assert controller.all_calls("write") == [("write", (("Tank_Levels{2}", [1.0, 2.0]),))]
    assert controller.tags["Tank_Levels"][0] == [1.0, 2.0, 33.1]


async def test_write_list_length_must_match_explicit_count() -> None:
    controller = FakeController()
    with pytest.raises(ValueError, match="asks for 3 elements but 2 values"):
        await make_client(controller).write_tag("Tank_Levels{3}", [1.0, 2.0])
    with pytest.raises(ValueError, match="give a list of 2 values"):
        await make_client(controller).write_tag("Tank_Levels{2}", 1.0)
    assert controller.all_calls("write") == []


async def test_write_rejected_by_controller_is_reported() -> None:
    controller = FakeController()
    with pytest.raises(EIPClientError, match="Unable to create a writable value"):
        await make_client(controller).write_tag("Batch_Count", 1.5)
    with pytest.raises(EIPClientError, match="Tag doesn't exist"):
        await make_client(controller).write_tag("Nope", 1)


async def test_write_data_type_mismatch_is_flagged() -> None:
    controller = FakeController()
    _, meta = await make_client(controller).write_tag("Batch_Count", 5, "REAL")
    assert "controller tag is DINT" in meta["warning"]


async def test_write_multiple_tags_passes_tuples() -> None:
    controller = FakeController()
    results, _ = await make_client(controller).write_multiple_tags(
        [("MotorSpeed", 1.0, None), ("Batch_Count", 7, "REAL"), ("Tank_Levels", [5.0, 6.0, 7.0], None)]
    )
    assert [r["error"] for r in results] == [None, None, None]
    assert "controller tag is DINT" in results[1]["warning"]
    assert controller.all_calls("write") == [
        ("write", (("MotorSpeed", 1.0), ("Batch_Count", 7), ("Tank_Levels{3}", [5.0, 6.0, 7.0])))
    ]


async def test_write_multiple_tags_reports_partial_failure() -> None:
    controller = FakeController()
    results, _ = await make_client(controller).write_multiple_tags([("MotorSpeed", 1.0, None), ("Nope", 2, None)])
    assert results[0]["error"] is None
    assert "Tag doesn't exist" in results[1]["error"]


async def test_single_entry_write_multiple_tags() -> None:
    controller = FakeController()
    results, _ = await make_client(controller).write_multiple_tags([("MotorSpeed", 3.0, None)])
    assert results == [{"tag": "MotorSpeed", "value": 3.0, "data_type": "REAL", "error": None}]


async def test_get_tag_list_does_not_replace_pycomm3s_tag_cache() -> None:
    controller = FakeController()
    client = make_client(controller)
    tags, meta = await client.get_tag_list()
    assert meta["count"] == len(tags) == 4
    assert {
        "tag": "Tank_Levels",
        "data_type": "REAL",
        "tag_type": "atomic",
        "dimensions": [3],
        "alias": False,
        "external_access": "Read/Write",
    } in tags
    json.dumps(tags)  # pycomm3's type_class objects are gone
    assert controller.all_calls("get_tag_list") == [("get_tag_list", None, False)]
    # With cache=True pycomm3 would forget the program tags; reads still work.
    result, _ = await client.read_tag("Program:MainProgram.Alarm_Message")
    assert result["value"] == "OK"


async def test_get_tag_list_program_scope() -> None:
    controller = FakeController()
    tags, _ = await make_client(controller).get_tag_list("MainProgram")
    assert [t["tag"] for t in tags] == ["Program:MainProgram.Alarm_Message"]


async def test_get_plc_info_reads_the_info_dict() -> None:
    controller = FakeController()
    info, _ = await make_client(controller).get_controller_info()
    assert info == {
        "name": "FakeProgram",
        "vendor": "Rockwell Automation/Allen-Bradley",
        "product_type": "Programmable Logic Controller",
        "product_code": 166,
        "product_name": "1756-L83E/B",
        "revision": {"major": 33, "minor": 11},
        "firmware": "33.11",
        "serial": "c0ffee00",
        "keyswitch": "REMOTE RUN",
    }
    json.dumps(info)


async def test_get_plc_time_is_flat() -> None:
    controller = FakeController(plc_time_us=1_791_092_767_930_000)
    payload, _ = await make_client(controller).get_plc_time()
    assert payload == {"plc_time": "2026-10-04T05:46:07.930000", "microseconds": 1_791_092_767_930_000}


async def test_set_plc_time_sends_microseconds() -> None:
    controller = FakeController()
    before = int(time.time() * 1_000_000)
    payload, _ = await make_client(controller).set_plc_time()
    sent = controller.set_time_calls[0]
    assert isinstance(sent, int) and sent >= before
    assert payload["microseconds"] == sent


async def test_ping_does_io() -> None:
    controller = FakeController()
    info, _ = await make_client(controller).ping()
    assert info == {"product_name": "1756-L83E/B"}
    assert controller.all_calls("get_plc_info")


# -- retries ------------------------------------------------------------------


async def test_connection_attempts_are_not_nested() -> None:
    """Each attempt opens once: max_retries=3 means 4 opens, not (3+1)**2 = 16."""
    controller = FakeController(open_errors=[CommError("refused")] * 10)
    client = make_client(controller, max_retries=3)
    with pytest.raises(EIPClientError, match="failed after 4 attempt") as info:
        await client.read_tag("MotorSpeed")
    assert info.value.meta["attempts"] == 4
    assert controller.opens == 4


async def test_transient_open_failure_then_success() -> None:
    controller = FakeController(open_errors=[CommError("refused"), OSError("unreachable")])
    result, meta = await make_client(controller, max_retries=3).read_tag("MotorSpeed")
    assert result["value"] == 1450.0
    assert meta["attempts"] == 3
    assert controller.opens == 3


async def test_broken_session_is_rebuilt_before_retrying() -> None:
    controller = FakeController(op_errors=[CommError("socket connection broken")])
    client = make_client(controller, max_retries=1)
    await client.ensure_connection()
    result, meta = await client.read_tag("MotorSpeed")
    assert result["value"] == 1450.0 and meta["attempts"] == 2
    assert len(controller.drivers) == 2
    assert ("close",) in controller.drivers[0].calls


async def test_request_errors_are_not_retried() -> None:
    controller = FakeController(op_errors=[RequestError("bad request")])
    with pytest.raises(EIPClientError, match="bad request") as info:
        await make_client(controller, max_retries=3).read_tag("MotorSpeed")
    assert info.value.meta["attempts"] == 1
    assert controller.opens == 1


async def test_session_refused_counts_as_a_failed_attempt() -> None:
    controller = FakeController(open_result=False)
    with pytest.raises(EIPClientError, match="did not register a CIP session"):
        await make_client(controller, max_retries=1).read_tag("MotorSpeed")
    assert controller.opens == 2


async def test_session_is_reused_between_calls() -> None:
    controller = FakeController()
    client = make_client(controller)
    await client.read_tag("MotorSpeed")
    await client.read_tag("Batch_Count")
    assert controller.opens == 1
    status = client.connection_status()
    assert status["connected"] is True and status["last_error"] is None and status["last_contact"]
    await client.close()
    assert client.connection_status()["connected"] is False


async def test_startup_connection_failure_does_not_raise() -> None:
    controller = FakeController(open_errors=[CommError("refused")])
    client = make_client(controller, max_retries=5)
    await client.ensure_connection()  # logs a warning instead of raising
    assert controller.opens == 1  # a single attempt, so startup stays quick
    status = client.connection_status()
    assert status["connected"] is False and "refused" in status["last_error"]


# -- the real LogixDriver against a listener that never answers ---------------


class BlackHole:
    """Accepts TCP connections and never replies, counting them."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self.accepted: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted.append(conn)

    def close(self) -> None:
        self.sock.close()
        for conn in self.accepted:
            conn.close()


@pytest.fixture
def black_hole() -> Iterator[BlackHole]:
    hole = BlackHole()
    yield hole
    hole.close()


async def test_real_pycomm3_uses_enip_port_and_timeout(black_hole: BlackHole) -> None:
    config = EIPClientConfig(port=black_hole.port, timeout=0.3, max_retries=2, retry_backoff_base=0.0)
    client = EIPClient(config, driver_factory=LogixDriver)
    start = time.perf_counter()
    with pytest.raises(EIPClientError, match="failed after 3 attempt.*timed out"):
        await client.read_tag("MotorSpeed")
    elapsed = time.perf_counter() - start
    assert len(black_hole.accepted) == 3  # ENIP_PORT reached the socket; one connection per attempt
    assert 0.8 < elapsed < 3.0  # 3 x ENIP_TIMEOUT (0.3 s), far below pycomm3's default 5 s each
