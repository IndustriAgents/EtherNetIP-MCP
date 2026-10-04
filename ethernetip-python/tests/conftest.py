"""Shared fixtures: the mock PLC as a subprocess, and how to launch the MCP server."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))  # for fake_pycomm3

REPO_ROOT = TESTS_DIR.parents[1]
MOCK_DIR = REPO_ROOT / "ethernetip-mock-server"

# Every setting the server reads. Tests set all of them, blank meaning "unset",
# so neither the caller's environment nor a developer's .env can leak in
# (load_dotenv never overrides a variable that is already present).
SERVER_VARS = (
    "ENIP_HOST",
    "ENIP_PORT",
    "ENIP_SLOT",
    "ENIP_PATH",
    "ENIP_JSON_BRIDGE",
    "ENIP_TIMEOUT",
    "ENIP_MAX_RETRIES",
    "ENIP_RETRY_BACKOFF_BASE",
    "ENIP_MICRO800",
    "ENIP_DEBUG",
    "ENIP_WRITES_ENABLED",
    "ENIP_SYSTEM_CMDS_ENABLED",
    "TAG_MAP_FILE",
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def server_env(**overrides: str) -> dict[str, str]:
    env = {name: "" for name in SERVER_VARS}
    env.update(overrides)
    return env


def server_command() -> list[str]:
    """The installed ``ethernetip-mcp`` console script of this environment."""
    script = Path(sys.executable).with_name("ethernetip-mcp")
    if script.exists():
        return [str(script)]
    return [sys.executable, "-m", "ethernetip_mcp.cli"]


def _uv() -> str:
    uv = shutil.which("uv")
    if uv:
        return uv
    message = "uv is needed to start the mock PLC (ethernetip-mock-server)"
    if os.environ.get("ENIP_REQUIRE_INTEGRATION"):
        pytest.fail(message)
    pytest.skip(message)


class MockPLC:
    """The mock PLC from ../ethernetip-mock-server, run with ``uv run --locked``."""

    def __init__(self, log_path: Path) -> None:
        self.host = "127.0.0.1"
        self.port = free_port()
        self.log_path = log_path
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> MockPLC:
        command = [
            _uv(),
            "run",
            "--locked",
            "--directory",
            str(MOCK_DIR),
            "ethernetip-mock-server",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--update-interval",
            "3600",  # no simulated value changes during a test
        ]
        env = {k: v for k, v in os.environ.items() if not k.startswith("MOCK_ENIP_") and k != "VIRTUAL_ENV"}
        with self.log_path.open("wb") as log:
            self.process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True
            )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"mock PLC exited early:\n{self.log_path.read_text(errors='replace')}")
            try:
                with socket.create_connection((self.host, self.port), timeout=0.5):
                    return self
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"mock PLC did not listen on {self.port}:\n{self.log_path.read_text(errors='replace')}")

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=10)


@pytest.fixture
def mock_plc(tmp_path: Path) -> Iterator[MockPLC]:
    """A fresh mock PLC per test, so writes in one test cannot affect another."""
    plc = MockPLC(tmp_path / "mock.log").start()
    try:
        yield plc
    finally:
        plc.stop()


@pytest.fixture
def closed_port() -> int:
    """A local port with nothing listening on it."""
    return free_port()
