"""End-to-end guard: the server speaks clean JSON-RPC and registers both tools.

stdout carries the protocol, so a single stray print() anywhere in the import
graph breaks the server in Claude Desktop with an opaque error. Discipline cannot
enforce that; this test can.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.core.config import ENV_FILE

PROJECT_ROOT = Path(__file__).resolve().parents[1]

REQUESTS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
]


@pytest.fixture(scope="module")
def handshake() -> tuple[subprocess.CompletedProcess, list[dict]]:
    """Drive a real handshake, holding stdin open until the replies arrive.

    Closing stdin up front would race the server's shutdown-on-EOF against its
    reply to tools/list, which makes the assertions below flaky.
    """
    env = {**os.environ, "RENILE_API_TOKEN": "test-token", "LOG_LEVEL": "INFO"}

    process = subprocess.Popen(
        [sys.executable, "-m", "src.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=PROJECT_ROOT, env=env,
    )
    killer = threading.Timer(60, process.kill)
    killer.start()
    try:
        assert process.stdin and process.stdout
        for request in REQUESTS:
            process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()

        # One reply per request carrying an id; notifications get none.
        expected = sum(1 for r in REQUESTS if "id" in r)
        messages = []
        while len(messages) < expected:
            line = process.stdout.readline()
            if not line:
                break
            if line.strip():
                messages.append(json.loads(line))

        # communicate() closes stdin itself, which triggers the server's shutdown.
        stdout_rest, stderr = process.communicate(timeout=30)
    finally:
        killer.cancel()
        if process.poll() is None:
            process.kill()

    stdout = "".join(json.dumps(m) + "\n" for m in messages) + stdout_rest
    result = subprocess.CompletedProcess(
        process.args, process.returncode, stdout=stdout, stderr=stderr
    )
    return result, messages


def test_server_exits_cleanly(handshake):
    result, _ = handshake
    assert result.returncode == 0, result.stderr


def test_stdout_is_pure_json_rpc(handshake):
    """Every non-empty stdout line must parse as JSON. No logs, no prints."""
    result, _ = handshake
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        message = json.loads(line)   # raises if anything non-JSON leaked
        assert message.get("jsonrpc") == "2.0"


def test_logs_go_to_stderr(handshake):
    result, _ = handshake
    assert "renile_mcp_starting" in result.stderr


def test_initialize_reports_server_identity(handshake):
    _, messages = handshake
    result = next(m for m in messages if m.get("id") == 1)["result"]
    assert result["serverInfo"]["name"] == "renile-iot"
    assert result["instructions"]


def test_exactly_two_tools_are_registered(handshake):
    _, messages = handshake
    tools = next(m for m in messages if m.get("id") == 2)["result"]["tools"]
    assert {t["name"] for t in tools} == {"get_all_devices", "get_latest_readings"}


def test_tool_schemas_are_model_ready(handshake):
    _, messages = handshake
    tools = {t["name"]: t for t in
             next(m for m in messages if m.get("id") == 2)["result"]["tools"]}

    for tool in tools.values():
        assert tool["description"].strip()
        assert tool["annotations"]["readOnlyHint"] is True
        # An object return keeps the output schema unwrapped; a list annotation
        # would nest everything under {"result": ...}.
        assert tool["outputSchema"]["type"] == "object"
        assert "result" not in tool["outputSchema"].get("properties", {})

    assert tools["get_all_devices"]["inputSchema"].get("properties", {}) == {}

    device = tools["get_latest_readings"]["inputSchema"]["properties"]["device"]
    assert device["description"]
    assert "required" not in tools["get_latest_readings"]["inputSchema"]


def test_missing_token_exits_with_an_actionable_message(monkeypatch, capsys):
    """A missing token must produce a readable sentence, not an import traceback.

    Driven in-process rather than as a subprocess: ENV_FILE is resolved from
    __file__, so a real src/.env would be found no matter what cwd or env a
    subprocess is given.
    """
    import src.server as server
    from src.core.config import Settings

    monkeypatch.delenv("RENILE_API_TOKEN", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    monkeypatch.setattr(
        server, "get_settings", lambda: (_ for _ in ()).throw(excinfo.value)
    )

    with pytest.raises(SystemExit) as exit_info:
        server.main()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "RENILE_API_TOKEN" in captured.err
    assert str(ENV_FILE) in captured.err
    assert captured.out == ""
