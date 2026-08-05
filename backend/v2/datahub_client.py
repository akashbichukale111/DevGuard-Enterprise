"""
datahub_client.py — one MCP stdio client, shared by the read-side agents.

Everything in here was learned the hard way against the live server during
live integration against DataHub Core. Three of those findings
are encoded as behaviour rather than comments, because a comment would not have
stopped the bug recurring:

  * finding 17 — `DATAHUB_TELEMETRY_ENABLED=false` is set unconditionally.
    Without it the server blocks ~90 s per call retrying Mixpanel POSTs that a
    restricted network 403s at CONNECT. Every call looks like a hang.
  * finding 18 — `capabilities()` returns the live `inputSchema` for every tool,
    and `describe(tool)` exposes it, because three separate tools turned out to
    have argument names that could not be guessed from the contract.
  * finding 4 — the tool list is the *negotiated* capability set, not a
    constant. `search_documents` / `grep_documents` are genuinely absent on a
    catalog with no documents, which is the pinned environment trap the Archivist must handle.

The allowlist check runs BEFORE the request is written to the pipe, so an agent
overreaching never touches the network. That is what makes the "enforced in
code" true rather than aspirational.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from backend.v2.handoff import ToolAllowlist, ToolCallRecord, check_mutation_scope

DEFAULT_GMS_URL = "http://localhost:8080"
DEFAULT_MCP_PACKAGE = "mcp-server-datahub@0.6.0"

#: Long enough for a cold `uvx` resolve on first use; short enough that a real
#: hang surfaces inside a demo rather than after it.
HANDSHAKE_TIMEOUT_S = 240.0
CALL_TIMEOUT_S = 120.0


class DataHubUnavailableError(RuntimeError):
    """The MCP server could not be started or did not complete the handshake."""


@dataclass
class ToolResult:
    """One tools/call outcome, in the shape the agents actually consume."""

    tool: str
    arguments: dict
    ok: bool
    duration_ms: float
    text: str = ""
    structured: Any = None
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)

    def record(self, raw_ref: Optional[str] = None) -> ToolCallRecord:
        return ToolCallRecord(
            tool=self.tool, arguments=self.arguments, ok=self.ok,
            duration_ms=self.duration_ms, raw_ref=raw_ref, error=self.error,
        )


@dataclass(frozen=True)
class Capabilities:
    """The negotiated capability set (step 4, "CAPABILITY NEGOTIATION").

    Captured as evidence in its own right: the incident loop says "list MCP tools; record
    capability set as evidence". What the server offers on the day is a fact
    about the run, not a constant of the system.
    """

    server_name: str
    server_version: str
    protocol_version: str
    tools: dict[str, dict]

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self.tools)

    def has(self, tool: str) -> bool:
        return tool in self.tools

    def describe(self, tool: str) -> dict:
        """The live inputSchema. Finding 18: never guess argument names."""
        return self.tools[tool].get("inputSchema", {})

    def missing_from(self, wanted: frozenset[str]) -> frozenset[str]:
        return frozenset(wanted) - self.tool_names


class DataHubMCPClient:
    """A minimal JSON-RPC stdio client for `mcp-server-datahub`.

    Not a general MCP implementation — deliberately. It speaks exactly the three
    messages the reference-incident needs (`initialize`, `tools/list`, `tools/call`) and
    nothing else, which keeps the trust surface of a subprocess we pipe a token
    into as small as it can be.
    """

    def __init__(
        self,
        *,
        token: str,
        gms_url: str = DEFAULT_GMS_URL,
        package: str = DEFAULT_MCP_PACKAGE,
        enable_mutations: bool = False,
    ) -> None:
        self._token = token
        self._gms_url = gms_url
        self._package = package
        self._enable_mutations = enable_mutations
        self._proc: Optional[subprocess.Popen] = None
        self._next_id = 0
        self._capabilities: Optional[Capabilities] = None
        self._stderr: list[str] = []

    # ---------------------------------------------------------------- lifecycle

    def __enter__(self) -> "DataHubMCPClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _env(self) -> dict:
        env = dict(os.environ)
        env["DATAHUB_GMS_URL"] = self._gms_url
        env["DATAHUB_GMS_TOKEN"] = self._token
        # Finding 17. Not optional, not configurable: there is no scenario in
        # which we want a tool call to sit behind four telemetry retries.
        env["DATAHUB_TELEMETRY_ENABLED"] = "false"
        env["TOOLS_IS_MUTATION_ENABLED"] = "true" if self._enable_mutations else "false"
        return env

    def start(self) -> Capabilities:
        if self._proc is not None:
            assert self._capabilities is not None
            return self._capabilities

        self._proc = subprocess.Popen(
            ["uvx", self._package],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._env(), text=True, bufsize=1,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()

        init = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "devguard", "version": "2.0"},
        }, timeout=HANDSHAKE_TIMEOUT_S)
        if init is None:
            self.close()
            raise DataHubUnavailableError(
                "MCP server did not respond to initialize; stderr tail:\n"
                + "".join(self._stderr[-20:])
            )
        self._notify("notifications/initialized", {})

        listed = self._request("tools/list", {}, timeout=HANDSHAKE_TIMEOUT_S) or {}
        tools = {t["name"]: t for t in listed.get("result", {}).get("tools", [])}
        info = init.get("result", {})
        self._capabilities = Capabilities(
            server_name=info.get("serverInfo", {}).get("name", "unknown"),
            # Finding 5: this disagrees with the PyPI version. Recorded as-is.
            server_version=info.get("serverInfo", {}).get("version", "unknown"),
            protocol_version=info.get("protocolVersion", "unknown"),
            tools=tools,
        )
        return self._capabilities

    def close(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    @property
    def capabilities(self) -> Capabilities:
        if self._capabilities is None:
            return self.start()
        return self._capabilities

    # ------------------------------------------------------------------- calls

    def call(self, agent: str, tool: str, arguments: dict) -> ToolResult:
        """Invoke a tool as `agent`, enforcing that agent's allowlist first.

        The allowlist check is the first statement on purpose, and the entity
        scope check is the second. The mutation allowlist wants the mutation allowlist "enforced
        in code" across tools, entity types and scope; enforcing any of it after
        the request is already on the wire would enforce nothing.
        """
        ToolAllowlist(agent).check(tool)
        check_mutation_scope(tool, arguments)

        if not self.capabilities.has(tool):
            # Not an error — this is capability negotiation. The pinned environment's trap says some
            # tools are hidden by design, and callers are expected to degrade.
            return ToolResult(
                tool=tool, arguments=arguments, ok=False, duration_ms=0.0,
                error=f"tool {tool!r} is not offered by this server "
                      f"(negotiated set has {len(self.capabilities.tools)} tools)",
            )

        t0 = time.monotonic()
        resp = self._request("tools/call", {"name": tool, "arguments": arguments},
                             timeout=CALL_TIMEOUT_S)
        elapsed = (time.monotonic() - t0) * 1000.0

        if resp is None:
            return ToolResult(tool=tool, arguments=arguments, ok=False,
                              duration_ms=elapsed,
                              error=f"no response within {CALL_TIMEOUT_S}s")

        result = resp.get("result", {})
        text = ""
        content = result.get("content") or []
        if content and isinstance(content[0], dict):
            text = content[0].get("text", "")
        is_error = bool(result.get("isError"))
        return ToolResult(
            tool=tool, arguments=arguments, ok=not is_error, duration_ms=elapsed,
            text=text, structured=result.get("structuredContent"),
            error=text if is_error else None, raw=resp,
        )

    # -------------------------------------------------------------- transport

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr.append(line)
            if len(self._stderr) > 400:  # bounded; this is a diagnostic buffer
                del self._stderr[:200]

    def _send(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise DataHubUnavailableError("MCP server is not running")
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict, *, timeout: float) -> Optional[dict]:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                    "params": params})
        for message in self._read_messages(timeout):
            if message.get("id") == request_id:
                return message
        return None

    def _read_messages(self, timeout: float) -> Iterator[dict]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise DataHubUnavailableError(
                    f"MCP server exited rc={proc.returncode}; stderr tail:\n"
                    + "".join(self._stderr[-20:])
                )
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # the server interleaves non-JSON noise on stdout


def load_token(path: Optional[str] = None) -> str:
    """Read the GMS token from a file or the environment.

    The secret-hygiene rule: never commit a token. The token is read at runtime from a path given
    by `DATAHUB_TOKEN_FILE`, and it is never logged, echoed, or written into any
    evidence artifact — the capture layer in `proofpack.py` strips it.
    """
    path = path or os.environ.get("DATAHUB_TOKEN_FILE")
    if path:
        with open(path) as fh:
            return fh.read().strip()
    token = os.environ.get("DATAHUB_GMS_TOKEN", "")
    if not token:
        raise DataHubUnavailableError(
            "no DataHub token: set DATAHUB_TOKEN_FILE or DATAHUB_GMS_TOKEN"
        )
    return token
