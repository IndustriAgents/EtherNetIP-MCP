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
from pycomm3.cip.status_info import EXTEND_CODES, SERVICE_STATUS

from ethernetip_mcp.eip_client import (
    MAX_BACKOFF_S,
    EIPClient,
    EIPClientConfig,
    EIPClientError,
    OutcomeUnknownError,
    check_cip_write,
    write_error_outcome,
)

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


class StubbedSocketLogixDriver(LogixDriver):
    """The real LogixDriver.open()/_initialize_driver(), with only the network I/O stubbed."""

    product_name = "2080-LC50-24QWB"

    def _list_identity(self) -> dict:
        return {"product_name": self.product_name}

    def get_plc_info(self) -> dict:
        return {
            "vendor": "Rockwell Automation/Allen-Bradley",
            "product_type": "Programmable Logic Controller",
            "product_code": 1,
            "revision": {"major": 21, "minor": 11},
            "status": b"\x00\x00",
            "serial": "00000001",
            "product_name": self.product_name,
        }

    def get_plc_name(self) -> str:
        self._info["name"] = "Prog"
        return "Prog"

    def get_tag_list(self, program: str | None = None, cache: bool = True) -> list:
        return []

    def close(self) -> None:
        self._connection_opened = False


@pytest.mark.parametrize(
    "product, configured, micro800, cip_path",
    [
        ("2080-LC50-24QWB", True, True, []),  # pycomm3 strips backplane/0 for a Micro800
        ("2080-LC30-48QWB", False, True, []),
        ("1756-L83E/B", False, False, [("bp", "0")]),
    ],
)
def test_real_initialize_driver_detects_micro800(
    monkeypatch: pytest.MonkeyPatch, product: str, configured: bool, micro800: bool, cip_path: list
) -> None:
    from pycomm3.cip_driver import CIPDriver

    def fake_cip_open(self: CIPDriver) -> bool:  # socket + session registration
        self._connection_opened = True
        return True

    monkeypatch.setattr(CIPDriver, "open", fake_cip_open)
    monkeypatch.setattr(StubbedSocketLogixDriver, "product_name", product)
    built: list[LogixDriver] = []

    def factory(path: str) -> LogixDriver:
        built.append(StubbedSocketLogixDriver(path))
        return built[-1]

    client = EIPClient(EIPClientConfig(host="10.0.0.9", micro800=configured, **FAST), driver_factory=factory)
    client._connect_once()
    driver = built[0]
    assert driver._micro800 is micro800  # set by pycomm3's own _initialize_driver
    assert [(seg.port, str(seg.link_address)) for seg in driver._cfg["cip_path"]] == cip_path
    assert client.connection_status()["micro800_detected"] is micro800


def test_real_initialize_driver_micro800_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from pycomm3.cip_driver import CIPDriver

    monkeypatch.setattr(CIPDriver, "open", lambda self: setattr(self, "_connection_opened", True) or True)
    monkeypatch.setattr(StubbedSocketLogixDriver, "product_name", "1769-L33ER/B")
    client = EIPClient(EIPClientConfig(micro800=True, **FAST), driver_factory=StubbedSocketLogixDriver)
    with pytest.raises(EIPClientError, match="1769-L33ER/B'?, not a Micro800"):
        client._connect_once()


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
    assert results == [
        {
            "tag": "MotorSpeed",
            "value": 3.0,
            "data_type": "REAL",
            "error": None,
            "outcome": "written",
            "request_sent": True,
        }
    ]


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
        "description": None,  # pycomm3 does not read tag descriptions
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


# -- writes are never repeated once they may have reached the controller ------


async def test_write_whose_reply_is_lost_is_sent_exactly_once() -> None:
    controller = FakeController(reply_errors=[CommError("failed to receive reply")])
    client = make_client(controller, max_retries=3)
    with pytest.raises(OutcomeUnknownError, match="may have been applied") as info:
        await client.write_tag("Batch_Count", 7)
    assert info.value.meta["outcome"] == "unknown" and info.value.meta["attempts"] == 1
    assert controller.all_calls("write") == [("write", (("Batch_Count", 7),))]  # not re-sent
    assert controller.tags["Batch_Count"] == (7, "DINT")  # it was in fact applied


async def test_write_retries_only_opening_the_session() -> None:
    controller = FakeController(open_errors=[CommError("refused"), CommError("refused")])
    result, meta = await make_client(controller, max_retries=3).write_tag("Batch_Count", 8)
    assert result["value"] == 8 and meta["attempts"] == 3
    assert controller.opens == 3
    assert len(controller.all_calls("write")) == 1


async def test_write_that_never_got_a_session_is_reported_not_sent() -> None:
    controller = FakeController(open_errors=[CommError("refused")] * 5)
    with pytest.raises(EIPClientError) as info:
        await make_client(controller, max_retries=1).write_tag("Batch_Count", 9)
    assert not isinstance(info.value, OutcomeUnknownError)
    assert info.value.meta["outcome"] == "not_sent" and info.value.meta["attempts"] == 2
    assert controller.all_calls("write") == []


async def test_write_on_a_broken_session_is_not_retried() -> None:
    controller = FakeController(op_errors=[CommError("failed to send message")])
    client = make_client(controller, max_retries=3)
    await client.ensure_connection()
    with pytest.raises(OutcomeUnknownError):
        await client.write_tag("Batch_Count", 10)
    assert len(controller.all_calls("write")) == 1


async def test_set_plc_time_whose_reply_is_lost_is_sent_exactly_once() -> None:
    controller = FakeController(reply_errors=[CommError("failed to receive reply")])
    with pytest.raises(OutcomeUnknownError, match="may have been applied"):
        await make_client(controller, max_retries=3).set_plc_time()
    assert len(controller.set_time_calls) == 1


async def test_batch_write_whose_reply_is_lost_reports_every_entry_unknown() -> None:
    controller = FakeController(reply_errors=[CommError("failed to receive reply")])
    results, meta = await make_client(controller, max_retries=3).write_multiple_tags(
        [("MotorSpeed", 1.0, None), ("Batch_Count", 5, None)]
    )
    assert len(controller.all_calls("write")) == 1
    assert [r["outcome"] for r in results] == ["unknown", "unknown"]
    assert all("may have been applied" in r["error"] for r in results)
    assert meta["outcome"] == "unknown"


async def test_batch_write_without_a_session_reports_not_sent() -> None:
    controller = FakeController(open_errors=[CommError("refused")] * 5)
    results, _ = await make_client(controller, max_retries=0).write_multiple_tags(
        [("MotorSpeed", 1.0, None), ("Batch_Count", 5, None)]
    )
    assert [r["outcome"] for r in results] == ["not_sent", "not_sent"]
    assert controller.all_calls("write") == []


async def test_reads_are_still_retried_after_a_lost_reply() -> None:
    controller = FakeController(op_errors=[CommError("failed to receive reply")])
    result, meta = await make_client(controller, max_retries=1).read_tag("MotorSpeed")
    assert result["value"] == 1450.0 and meta["attempts"] == 2


def test_backoff_is_capped() -> None:
    client = EIPClient(EIPClientConfig(max_retries=10, retry_backoff_base=60.0))
    assert client._backoff(1) == MAX_BACKOFF_S == 30.0
    assert max(client._backoff(n) for n in range(1, 11)) == MAX_BACKOFF_S
    assert EIPClient(EIPClientConfig(retry_backoff_base=0.5))._backoff(3) == 2.0


# -- outcome and request_sent on every write (suite rule 1) -------------------


def assert_outcome(meta: dict, outcome: str) -> None:
    assert meta["outcome"] == outcome
    assert meta["request_sent"] is (outcome != "not_sent")


async def test_successful_write_reports_written() -> None:
    _, meta = await make_client(FakeController()).write_tag("MotorSpeed", 5.0)
    assert_outcome(meta, "written")


async def test_controller_rejection_reports_rejected_and_sent() -> None:
    controller = FakeController(write_rejections={"Batch_Count": "Permission denied"})
    with pytest.raises(EIPClientError, match="the controller refused it \\(Permission denied\\)") as info:
        await make_client(controller).write_tag("Batch_Count", 3)
    assert_outcome(info.value.meta, "rejected")
    assert len(controller.all_calls("write")) == 1


async def test_unknown_tag_is_refused_before_sending() -> None:
    controller = FakeController()
    with pytest.raises(EIPClientError, match="refused before sending: Tag doesn't exist - Nope") as info:
        await make_client(controller).write_tag("Nope", 3)
    assert_outcome(info.value.meta, "not_sent")
    assert controller.all_calls("write") == []


async def test_local_encoding_error_is_not_sent() -> None:
    # pycomm3 fails to encode 1.5 for a DINT before sending ("Invalid Tag Request - ...").
    with pytest.raises(EIPClientError) as info:
        await make_client(FakeController()).write_tag("Batch_Count", 1.5)
    assert_outcome(info.value.meta, "not_sent")


async def test_lost_reply_reports_unknown_and_sent() -> None:
    controller = FakeController(reply_errors=[CommError("failed to receive reply")])
    with pytest.raises(OutcomeUnknownError) as info:
        await make_client(controller).write_tag("Batch_Count", 4)
    assert_outcome(info.value.meta, "unknown")


async def test_failed_fragmented_write_is_unknown_not_rejected() -> None:
    controller = FakeController(write_errors_after_apply={"MotorSpeed": "One or more fragment responses failed"})
    with pytest.raises(OutcomeUnknownError, match="may have been applied") as info:
        await make_client(controller).write_tag("MotorSpeed", 9.0)
    assert_outcome(info.value.meta, "unknown")


async def test_set_plc_time_reports_outcome() -> None:
    _, meta = await make_client(FakeController()).set_plc_time()
    assert_outcome(meta, "written")
    lost = FakeController(reply_errors=[CommError("failed to receive reply")])
    with pytest.raises(OutcomeUnknownError) as info:
        await make_client(lost).set_plc_time()
    assert_outcome(info.value.meta, "unknown")
    refused = FakeController(open_errors=[CommError("refused")] * 3)
    with pytest.raises(EIPClientError) as info:
        await make_client(refused, max_retries=1).set_plc_time()
    assert_outcome(info.value.meta, "not_sent")
    assert refused.set_time_calls == []


def _general_error(ext: int) -> str:
    """The text pycomm3 builds for CIP general status 0xFF with a Logix extended code."""
    return f"{SERVICE_STATUS[0xFF]} - {EXTEND_CODES[0xFF][ext]}  (ff, {ext:0>2x})"


@pytest.mark.parametrize(
    "error, outcome",
    [
        # refused by pycomm3 before anything was sent
        ("Tag doesn't exist - X", "not_sent"),
        ("('Failed to parse tag request', 'X')", "not_sent"),
        ("Invalid Tag Request - RequestError('Unable to create a writable value')", "not_sent"),
        ("Error encoding value - TypeError()", "not_sent"),
        ("Failed to build request path for tag", "not_sent"),
        ("Failed to create request path for tag", "not_sent"),
        ("No response data received", "not_sent"),
        # refused by the controller before executing it
        (SERVICE_STATUS[0x0F], "rejected"),
        (SERVICE_STATUS[0x16], "rejected"),
        (SERVICE_STATUS[0x05] + " - " + EXTEND_CODES[0x05][0x0000] + "  (05, 00)", "rejected"),
        (_general_error(0x2107), "rejected"),
        (_general_error(0x2108), "rejected"),
        # everything else: the write may have been applied
        ("Failed to parse reply - unpack requires a buffer of 4 bytes", "unknown"),
        ("Unknown Error", "unknown"),
        ("Unknown Error (99)", "unknown"),
        ("One or more fragment responses failed", "unknown"),
        ("Invalid tag request - KeyError(0)", "unknown"),
        (SERVICE_STATUS[0x06], "unknown"),  # partial transfer
        (SERVICE_STATUS[0x07], "unknown"),  # connection lost
        (SERVICE_STATUS[0x1E], "unknown"),  # embedded service error
        (SERVICE_STATUS[0xFE], "unknown"),  # message timeout
        (SERVICE_STATUS[0xFF], "unknown"),  # general error without a known code
        (_general_error(0x2110), "unknown"),  # unable to write
        (_general_error(0x2105), "unknown"),  # access beyond end of the object
        ("Privilege violation", "unknown"),  # not a pycomm3 text
    ],
)
def test_write_error_classification(error: str, outcome: str) -> None:
    assert write_error_outcome(error) == outcome


# -- booleans are never written into numeric tags (suite rule 7) ----------------


@pytest.mark.parametrize(
    "tag, value", [("Batch_Count", True), ("MotorSpeed", False), ("Tank_Levels", [1.0, True, 2.0])]
)
async def test_bool_into_numeric_tag_is_refused_before_sending(tag: str, value: object) -> None:
    controller = FakeController()
    with pytest.raises(EIPClientError, match="a boolean is not accepted for a numeric value") as info:
        await make_client(controller).write_tag(tag, value)
    assert_outcome(info.value.meta, "not_sent")
    assert controller.all_calls("write") == []


async def test_bool_into_bool_tag_and_bits_is_fine() -> None:
    controller = FakeController()
    _, meta = await make_client(controller).write_tag("Running", False)
    assert_outcome(meta, "written")
    driver = controller.drivers[0]
    check_cip_write(driver, "Batch_Count.3", True)  # a bit of a DINT takes a boolean
    check_cip_write(driver, "Batch_Count", 7)


async def test_batch_sends_only_entries_that_pass_the_local_checks() -> None:
    controller = FakeController()
    results, _ = await make_client(controller).write_multiple_tags(
        [("MotorSpeed", 1.0, None), ("Batch_Count", True, None), ("Nope", 1, None), ("Running", True, None)]
    )
    assert [(r["tag"], r["outcome"], r["request_sent"]) for r in results] == [
        ("MotorSpeed", "written", True),
        ("Batch_Count", "not_sent", False),
        ("Nope", "not_sent", False),
        ("Running", "written", True),
    ]
    assert controller.all_calls("write") == [("write", (("MotorSpeed", 1.0), ("Running", True)))]


async def test_batch_with_nothing_sendable_sends_nothing() -> None:
    controller = FakeController()
    results, _ = await make_client(controller).write_multiple_tags([("Nope", 1, None), ("Batch_Count", True, None)])
    assert [r["outcome"] for r in results] == ["not_sent", "not_sent"]
    assert controller.all_calls("write") == []


async def test_batch_lost_reply_marks_only_sent_entries_unknown() -> None:
    controller = FakeController(reply_errors=[CommError("failed to receive reply")])
    results, _ = await make_client(controller).write_multiple_tags([("MotorSpeed", 1.0, None), ("Nope", 1, None)])
    assert [(r["outcome"], r["request_sent"]) for r in results] == [("unknown", True), ("not_sent", False)]


async def test_batch_fragment_failure_is_unknown() -> None:
    controller = FakeController(write_errors_after_apply={"MotorSpeed": "One or more fragment responses failed"})
    results, _ = await make_client(controller).write_multiple_tags([("MotorSpeed", 1.0, None), ("Running", 1, None)])
    assert [r["outcome"] for r in results] == ["unknown", "written"]


# -- liveness probe before a write on an idle session (decision 10) ----------


def probes(controller: FakeController) -> int:
    return len([c for c in controller.all_calls("generic_message") if c[1] == "liveness_probe"])


async def test_idle_dead_session_is_rebuilt_before_the_write() -> None:
    controller = FakeController()
    client = make_client(controller, max_retries=0, write_probe_idle=10.0)
    await client.ensure_connection()
    client._last_io -= 60  # the session has been idle for a minute...
    controller.op_errors.append(CommError("socket connection broken"))  # ...and the controller dropped it
    result, meta = await client.write_tag("Batch_Count", 11)
    assert_outcome(meta, "written")
    assert probes(controller) == 1
    assert len(controller.drivers) == 2  # reconnected before writing
    assert len(controller.all_calls("write")) == 1  # the write went out once, on the new session
    assert controller.tags["Batch_Count"] == (11, "DINT")


async def test_idle_live_session_is_reused_after_the_probe() -> None:
    controller = FakeController()
    client = make_client(controller, write_probe_idle=10.0)
    await client.ensure_connection()
    client._last_io -= 60
    await client.write_tag("Batch_Count", 12)
    assert probes(controller) == 1 and len(controller.drivers) == 1


async def test_recently_used_session_is_not_probed() -> None:
    controller = FakeController()
    client = make_client(controller, write_probe_idle=10.0)
    await client.read_tag("MotorSpeed")
    await client.write_tag("Batch_Count", 13)
    assert probes(controller) == 0


async def test_probe_idle_zero_probes_before_every_write_but_not_reads() -> None:
    controller = FakeController()
    client = make_client(controller, write_probe_idle=0.0)
    await client.ensure_connection()
    await client.read_tag("MotorSpeed")
    await client.write_tag("Batch_Count", 1)
    await client.write_tag("Batch_Count", 2)
    assert probes(controller) == 2


async def test_failed_reconnect_after_probe_is_not_sent() -> None:
    controller = FakeController()
    client = make_client(controller, max_retries=0, write_probe_idle=10.0)
    await client.ensure_connection()
    client._last_io -= 60
    controller.op_errors.append(CommError("socket connection broken"))
    controller.open_errors.append(CommError("refused"))
    with pytest.raises(EIPClientError) as info:
        await client.write_tag("Batch_Count", 14)
    assert_outcome(info.value.meta, "not_sent")
    assert controller.all_calls("write") == []


# -- round 3: classification after a send, structures, ranges, deadlines -------


@pytest.mark.parametrize(
    "error, outcome, exc_type",
    [
        ("Failed to parse reply - unpack requires a buffer of 4 bytes", "unknown", OutcomeUnknownError),
        ("Unknown Error", "unknown", OutcomeUnknownError),
        ("No response data received", "not_sent", EIPClientError),
        ("Permission denied", "rejected", EIPClientError),
    ],
)
async def test_single_write_uses_the_classifier(error: str, outcome: str, exc_type: type) -> None:
    controller = FakeController(write_rejections={"Batch_Count": error})
    with pytest.raises(exc_type) as info:
        await make_client(controller).write_tag("Batch_Count", 3)
    assert_outcome(info.value.meta, outcome)
    if outcome == "unknown":
        assert "may have been applied" in str(info.value)


@pytest.mark.parametrize(
    "error, outcome",
    [("Failed to parse reply - x", "unknown"), ("Unknown Error", "unknown"), ("Object does not exist", "rejected")],
)
async def test_set_plc_time_uses_the_classifier(error: str, outcome: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from pycomm3 import Tag

    controller = FakeController()
    original = FakeLogixDriver.set_plc_time

    def failing(self: FakeLogixDriver, *args: object, **kwargs: object) -> Tag:
        original(self, *args, **kwargs)
        return Tag("set_plc_time", None, None, error)

    monkeypatch.setattr(FakeLogixDriver, "set_plc_time", failing)
    with pytest.raises(EIPClientError) as info:
        await make_client(controller).set_plc_time()
    assert_outcome(info.value.meta, outcome)


def struct_controller() -> FakeController:
    controller = FakeController(structs={"MOTOR_UDT": {"Speed": "REAL", "Running": "BOOL", "Starts": "DINT"}})
    controller.tags["Motor"] = ({"Speed": 1.0, "Running": False, "Starts": 0}, "MOTOR_UDT")
    return controller


@pytest.mark.parametrize("value", [{"Speed": True}, {"Starts": False}, {"Running": True, "Speed": True}])
async def test_bool_into_numeric_structure_member_is_refused(value: dict) -> None:
    controller = struct_controller()
    with pytest.raises(EIPClientError, match=r"Motor\.(Speed|Starts) is (REAL|DINT); a boolean") as info:
        await make_client(controller).write_tag("Motor", value)
    assert_outcome(info.value.meta, "not_sent")
    assert controller.all_calls("write") == []


async def test_structure_with_proper_types_is_written() -> None:
    controller = struct_controller()
    _, meta = await make_client(controller).write_tag("Motor", {"Speed": 2.5, "Running": True, "Starts": 3})
    assert_outcome(meta, "written")


@pytest.mark.parametrize(
    "tag, value", [("Tank_Levels", [1.0, 2.0, 3.0, 4.0]), ("Tank_Levels[2]", [1.0, 2.0]), ("Tank_Levels[3]", 1.0)]
)
async def test_array_write_past_the_end_is_refused(tag: str, value: object) -> None:
    controller = FakeController()
    with pytest.raises(EIPClientError, match="would go past the end") as info:
        await make_client(controller).write_tag(tag, value)
    assert_outcome(info.value.meta, "not_sent")
    assert controller.all_calls("write") == []


def test_array_write_inside_the_array_passes_the_check() -> None:
    controller = FakeController()
    make_client(controller)._connect_once()
    driver = controller.drivers[0]
    check_cip_write(driver, "Tank_Levels", [1.0, 2.0, 3.0], 3)
    check_cip_write(driver, "Tank_Levels[1]", [1.0, 2.0], 2)
    check_cip_write(driver, "Tank_Levels[2]", 1.0, None)


async def test_probe_error_reply_also_reconnects() -> None:
    controller = FakeController(probe_reply_error="Service not supported")
    client = make_client(controller, write_probe_idle=10.0)
    await client.ensure_connection()
    client._last_io -= 60
    _, meta = await client.write_tag("Batch_Count", 21)
    assert_outcome(meta, "written")
    assert probes(controller) == 1 and len(controller.drivers) == 2
    assert len(controller.all_calls("write")) == 1


async def test_deadline_during_a_write_is_unknown_and_frees_the_session() -> None:
    controller = FakeController(hang_writes=True)
    client = make_client(controller, deadline=0.5)
    await client.ensure_connection()
    start = time.perf_counter()
    with pytest.raises(OutcomeUnknownError, match="deadline") as info:
        await client.write_tag("Batch_Count", 22)
    assert time.perf_counter() - start < 3.0
    assert_outcome(info.value.meta, "unknown")
    assert len(controller.all_calls("write")) == 1
    assert client._lock.acquire(timeout=3.0)  # the worker thread let go of the session
    client._lock.release()


async def test_deadline_while_connecting_for_a_write_is_not_sent() -> None:
    controller = FakeController(hang_open=True)
    client = make_client(controller, deadline=0.5, max_retries=3)
    with pytest.raises(EIPClientError, match="nothing was sent") as info:
        await client.write_tag("Batch_Count", 23)
    assert_outcome(info.value.meta, "not_sent")
    assert controller.all_calls("write") == []
    assert client._lock.acquire(timeout=3.0)
    client._lock.release()
    time.sleep(0.2)
    assert controller.opens == 1  # the abandoned worker did not retry


async def test_deadline_during_a_read_is_a_clean_error() -> None:
    controller = FakeController(hang_open=True)
    with pytest.raises(EIPClientError, match="deadline") as info:
        await make_client(controller, deadline=0.5).read_tag("MotorSpeed")
    assert not isinstance(info.value, OutcomeUnknownError)
    assert "outcome" not in info.value.meta


async def test_startup_connection_is_bounded_by_the_deadline() -> None:
    controller = FakeController(hang_open=True)
    client = make_client(controller, deadline=0.5)
    start = time.perf_counter()
    await client.ensure_connection()
    assert time.perf_counter() - start < 3.0
    assert "deadline" in client.connection_status()["last_error"]


def test_deadline_defaults_cover_retries_and_backoff() -> None:
    config = EIPClientConfig()
    assert config.deadline_s() == pytest.approx(5.0 * 5 + (0.5 + 1 + 2) + 15.0)
    assert EIPClientConfig(deadline=7.0).deadline_s() == 7.0


class Trickler:
    """Answers with an EtherNet/IP header claiming 65535 bytes, then sends one byte every 0.1 s.

    Each recv() returns well inside the socket timeout, so pycomm3 would keep
    reading for hours; only the overall deadline ends the call.
    """

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.closed_by_client = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._trickle, args=(conn,), daemon=True).start()

    def _trickle(self, conn: socket.socket) -> None:
        try:
            conn.recv(1024)  # the RegisterSession request
            conn.sendall(b"\x65\x00\xff\xff" + b"\x00" * 20)
            for _ in range(10_000):
                conn.sendall(b"\x00")
                time.sleep(0.1)
        except OSError:
            self.closed_by_client.set()
        finally:
            conn.close()

    def close(self) -> None:
        self.sock.close()


async def test_real_pycomm3_garbled_reply_is_bounded_by_the_deadline() -> None:
    trickler = Trickler()
    try:
        config = EIPClientConfig(port=trickler.port, timeout=0.5, max_retries=0, deadline=1.5)
        client = EIPClient(config, driver_factory=LogixDriver)
        start = time.perf_counter()
        with pytest.raises(EIPClientError, match="deadline"):
            await client.read_tag("MotorSpeed")
        assert time.perf_counter() - start < 4.0
        assert trickler.closed_by_client.wait(5.0)  # the socket was closed, so the worker stopped
        assert client._lock.acquire(timeout=3.0)
        client._lock.release()
        with pytest.raises(EIPClientError, match="nothing was sent") as info:
            await client.write_tag("MotorSpeed", 1.0)  # stuck while registering the session
        assert info.value.meta["request_sent"] is False
    finally:
        trickler.close()
