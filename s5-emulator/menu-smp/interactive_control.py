#!/usr/bin/env python3
"""Loopback-only JSONL control channel between s5_interactive.py and boot.py.

Uses only the Python 3 standard library. Strict loopback binding (127.0.0.1),
constant-time per-session token authentication, bounded key queue (max 8
pending clicks), and structured status/event snapshots.
"""
from __future__ import annotations

import collections
import hmac
import json
import secrets
import socket
import threading
import time
import uuid
from typing import Any

from key_probe import KEYS

PROTOCOL_VERSION = 1
MAX_PENDING_KEYS = 8
MAX_LINE_BYTES = 65536
MAX_BOOT_STAGES = 128
MAX_KEY_HISTORY = 256
MAX_ERRORS = 64

VALID_KEYS: frozenset[str] = frozenset(KEYS.keys())


def _utc_now() -> float:
    return time.time()


def _encode_message(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


class InteractiveControlError(RuntimeError):
    """Error raised for control protocol or authentication failures."""


class InteractiveControlClient:
    """Client embedded inside opt-in interactive boot.py runs."""

    def __init__(
        self,
        port: int,
        token: str,
        *,
        host: str = "127.0.0.1",
        max_pending: int = MAX_PENDING_KEYS,
        connect_timeout: float = 5.0,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("InteractiveControlClient only connects to 127.0.0.1")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("InteractiveControlClient requires a valid TCP port 1..65535")
        if not isinstance(token, str) or not token:
            raise ValueError("InteractiveControlClient requires a non-empty auth token")
        if type(max_pending) is not int or not 1 <= max_pending <= 32:
            raise ValueError("max_pending must be an int in 1..32")

        self.host = host
        self.port = port
        self._token = token
        self.max_pending = max_pending

        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pending: collections.deque[dict[str, Any]] = collections.deque()
        self._stop_requested = False
        self._stop_reason: str | None = None
        self._closed = False
        self._connected = False

        sock = socket.create_connection((host, port), timeout=connect_timeout)
        try:
            sock.settimeout(connect_timeout)
            self._sock = sock
            self._rfile = sock.makefile("rb")
            self._send_raw({"type": "auth", "token": token, "protocol": PROTOCOL_VERSION})
            raw_reply = self._rfile.readline(MAX_LINE_BYTES)
            if not raw_reply:
                raise InteractiveControlError("Control server closed connection before auth_ok")
            reply = json.loads(raw_reply.decode("utf-8"))
            if not isinstance(reply, dict) or reply.get("type") != "auth_ok":
                reason = reply.get("reason") if isinstance(reply, dict) else "unexpected_reply"
                raise InteractiveControlError(f"Control auth rejected: {reason}")
            sock.settimeout(None)
            self._connected = True
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="s5-interactive-control-client",
            daemon=True,
        )
        self._reader_thread.start()

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected and not self._closed

    @property
    def stop_requested(self) -> bool:
        with self._lock:
            return self._stop_requested

    @property
    def stop_reason(self) -> str | None:
        with self._lock:
            return self._stop_reason

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def has_pending_key(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def pop_next_key(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._pending:
                return None
            return self._pending.popleft()

    def _send_raw(self, payload: dict[str, Any]) -> bool:
        data = _encode_message(payload)
        with self._send_lock:
            if self._closed:
                return False
            try:
                self._sock.sendall(data)
                return True
            except OSError:
                with self._lock:
                    self._connected = False
                return False

    def send_event(self, event_type: str, **fields: Any) -> bool:
        msg = {"type": event_type, "ts": _utc_now(), **fields}
        return self._send_raw(msg)

    def _reader_loop(self) -> None:
        try:
            while True:
                line = self._rfile.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    self.send_event("error", reason="command_line_too_long")
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.send_event("error", reason="malformed_command_json")
                    continue
                if not isinstance(msg, dict):
                    self.send_event("error", reason="invalid_command_type")
                    continue
                self._handle_command(msg)
        except OSError:
            pass
        finally:
            with self._lock:
                self._connected = False
                if not self._closed and not self._stop_requested:
                    self._stop_requested = True
                    self._stop_reason = "control_channel_closed"

    def _handle_command(self, msg: dict[str, Any]) -> None:
        cmd_type = msg.get("type")
        if cmd_type == "stop":
            reason = str(msg.get("reason") or "user_requested")
            with self._lock:
                self._stop_requested = True
                self._stop_reason = reason
            return
        if cmd_type == "key_click":
            req_id = str(msg.get("id") or uuid.uuid4().hex[:12])
            key = msg.get("key")
            if not isinstance(key, str) or key not in VALID_KEYS:
                self.send_event("key_rejected", id=req_id, key=key, reason="invalid_key")
                return
            with self._lock:
                if self._stop_requested:
                    self.send_event("key_rejected", id=req_id, key=key, reason="stopping")
                    return
                if len(self._pending) >= self.max_pending:
                    self.send_event(
                        "key_rejected",
                        id=req_id,
                        key=key,
                        reason="queue_full",
                        max_pending=self.max_pending,
                    )
                    return
                entry = {"id": req_id, "key": key, "queued_at": _utc_now()}
                self._pending.append(entry)
                depth = len(self._pending)
            self.send_event("key_queued", id=req_id, key=key, queue_depth=depth)
            return
        self.send_event("error", reason=f"unknown_command:{cmd_type}")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connected = False
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._rfile.close()
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class InteractiveControlServer:
    """Loopback TCP server owned by s5_interactive.py to control one boot.py child."""

    def __init__(
        self,
        *,
        token: str | None = None,
        max_pending: int = MAX_PENDING_KEYS,
    ) -> None:
        if type(max_pending) is not int or not 1 <= max_pending <= 32:
            raise ValueError("max_pending must be an int in 1..32")
        self.token = token if token is not None else secrets.token_hex(24)
        if not isinstance(self.token, str) or not self.token:
            raise ValueError("token must be a non-empty string")
        self.max_pending = max_pending

        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._closed = False
        self._client_sock: socket.socket | None = None
        self._connected = False
        self._authenticated = False
        self._stopped_event = threading.Event()

        self._state: dict[str, Any] = {
            "protocol": PROTOCOL_VERSION,
            "connected": False,
            "authenticated": False,
            "interactive_ready": False,
            "stopped": False,
            "stop_reason": None,
            "current_stage": "starting",
            "hello": None,
            "boot_stages": [],
            "swaps": [],
            "latest_swap": None,
            "ui_state": None,
            "pending_keys": [],
            "active_key": None,
            "key_history": [],
            "errors": [],
            "last_event_ts": None,
            "rejected_auth_attempts": 0,
        }
        self._in_flight: dict[str, dict[str, Any]] = {}

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        srv.settimeout(0.2)
        self._server_sock = srv
        self.host, self.port = srv.getsockname()

        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="s5-interactive-control-server",
            daemon=True,
        )
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    break
            try:
                conn, _addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_connection,
                args=(conn,),
                name="s5-interactive-control-conn",
                daemon=True,
            ).start()

    def _send_to_client(self, sock: socket.socket, payload: dict[str, Any]) -> bool:
        data = _encode_message(payload)
        with self._send_lock:
            try:
                sock.sendall(data)
                return True
            except OSError:
                return False

    def _handle_connection(self, conn: socket.socket) -> None:
        rfile = None
        is_active_client = False
        try:
            conn.settimeout(5.0)
            rfile = conn.makefile("rb")
            raw = rfile.readline(MAX_LINE_BYTES + 1)
            if not raw or len(raw) > MAX_LINE_BYTES:
                self._send_to_client(conn, {"type": "auth_error", "reason": "missing_or_oversize_auth"})
                return
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_to_client(conn, {"type": "auth_error", "reason": "invalid_json"})
                return
            if not isinstance(msg, dict) or msg.get("type") != "auth":
                self._send_to_client(conn, {"type": "auth_error", "reason": "expected_auth"})
                return
            candidate = msg.get("token")
            if not isinstance(candidate, str) or not hmac.compare_digest(candidate, self.token):
                with self._lock:
                    self._state["rejected_auth_attempts"] += 1
                self._send_to_client(conn, {"type": "auth_error", "reason": "invalid_token"})
                return

            with self._lock:
                if self._closed or self._authenticated:
                    self._send_to_client(conn, {"type": "auth_error", "reason": "client_already_connected"})
                    return
                self._client_sock = conn
                self._connected = True
                self._authenticated = True
                self._state["connected"] = True
                self._state["authenticated"] = True
                self._state["last_event_ts"] = _utc_now()
                is_active_client = True

            conn.settimeout(None)
            self._send_to_client(conn, {"type": "auth_ok", "protocol": PROTOCOL_VERSION})

            while True:
                line = rfile.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    continue
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(event, dict):
                    self._ingest_event(event)
        except OSError:
            pass
        finally:
            if rfile is not None:
                try:
                    rfile.close()
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass
            if is_active_client:
                with self._lock:
                    self._client_sock = None
                    self._connected = False
                    self._state["connected"] = False
                    self._state["interactive_ready"] = False
                    if not self._state["stopped"]:
                        self._state["stopped"] = True
                        self._state["stop_reason"] = self._state["stop_reason"] or "disconnected"
                    self._in_flight.clear()
                    self._state["pending_keys"] = []
                    self._state["active_key"] = None
                self._stopped_event.set()

    def _find_history_entry_locked(self, req_id: str) -> dict[str, Any] | None:
        for item in reversed(self._state["key_history"]):
            if item.get("id") == req_id:
                return item
        return None

    def _sync_pending_locked(self) -> None:
        self._state["pending_keys"] = [
            {"id": entry["id"], "key": entry["key"], "status": entry["status"]}
            for entry in self._in_flight.values()
        ]

    def _ingest_event(self, event: dict[str, Any]) -> None:
        ev_type = event.get("type")
        now = event.get("ts") or _utc_now()
        with self._lock:
            self._state["last_event_ts"] = now
            if ev_type == "hello":
                self._state["hello"] = {k: v for k, v in event.items() if k != "type"}
                self._state["current_stage"] = "emulator_started"
            elif ev_type == "boot_stage":
                stage = str(event.get("stage") or "unknown")
                self._state["current_stage"] = stage
                stages = self._state["boot_stages"]
                stages.append(
                    {
                        "stage": stage,
                        "detail": event.get("detail"),
                        "pc": event.get("pc"),
                        "cpu": event.get("cpu"),
                        "ts": now,
                    }
                )
                if len(stages) > MAX_BOOT_STAGES:
                    del stages[:-MAX_BOOT_STAGES]
            elif ev_type == "swap":
                swap_rec = {
                    "frame_id": event.get("frame_id"),
                    "swap_count": event.get("swap_count"),
                    "draw_calls": event.get("draw_calls"),
                    "render_scene": event.get("render_scene"),
                    "render_modal": event.get("render_modal"),
                    "source_scene": event.get("source_scene"),
                    "source_modal": event.get("source_modal"),
                    "ui_state": event.get("ui_state"),
                    "pixels_sha256": event.get("pixels_sha256"),
                    "ts": now,
                }
                self._state["swaps"].append(swap_rec)
                self._state["latest_swap"] = swap_rec
                if event.get("ui_state") is not None:
                    self._state["ui_state"] = event.get("ui_state")
            elif ev_type == "ready":
                self._state["interactive_ready"] = True
                self._state["current_stage"] = "interactive_ready"
                if event.get("ui_state") is not None:
                    self._state["ui_state"] = event.get("ui_state")
                stages = self._state["boot_stages"]
                stages.append(
                    {
                        "stage": "interactive_ready",
                        "detail": event.get("detail") or "Ready for live interactive input",
                        "ts": now,
                    }
                )
                if len(stages) > MAX_BOOT_STAGES:
                    del stages[:-MAX_BOOT_STAGES]
            elif ev_type == "key_queued":
                req_id = str(event.get("id") or "")
                entry = self._in_flight.get(req_id) or self._find_history_entry_locked(req_id)
                if entry is not None:
                    entry["status"] = "queued"
                    entry["queued_ts"] = now
                    self._sync_pending_locked()
            elif ev_type == "key_dispatched":
                req_id = str(event.get("id") or "")
                action = event.get("action")
                entry = self._in_flight.get(req_id) or self._find_history_entry_locked(req_id)
                if entry is not None:
                    entry["status"] = "dispatched_press" if action == 1 else "dispatched_release"
                    entry["last_action"] = action
                    entry["dispatch_swap_count"] = event.get("swap_count")
                    entry["dispatched_ts"] = now
                    self._state["active_key"] = {
                        "id": entry["id"],
                        "key": entry["key"],
                        "status": entry["status"],
                    }
                    self._sync_pending_locked()
            elif ev_type == "key_consumed":
                req_id = str(event.get("id") or "")
                entry = self._in_flight.pop(req_id, None) or self._find_history_entry_locked(req_id)
                if entry is not None:
                    entry["status"] = "consumed"
                    entry["consumed_ts"] = now
                    entry["duration_ms"] = event.get("duration_ms")
                    entry["swap_count_at_consume"] = event.get("swap_count")
                if self._state["active_key"] and self._state["active_key"].get("id") == req_id:
                    self._state["active_key"] = None
                self._sync_pending_locked()
            elif ev_type in ("key_rejected", "key_error"):
                req_id = str(event.get("id") or "")
                reason = event.get("reason") or event.get("error") or ev_type
                entry = self._in_flight.pop(req_id, None) or self._find_history_entry_locked(req_id)
                if entry is not None:
                    entry["status"] = ev_type
                    entry["error"] = reason
                    entry["finished_ts"] = now
                if self._state["active_key"] and self._state["active_key"].get("id") == req_id:
                    self._state["active_key"] = None
                self._sync_pending_locked()
                errs = self._state["errors"]
                errs.append({"kind": ev_type, "id": req_id, "key": event.get("key"), "reason": reason, "ts": now})
                if len(errs) > MAX_ERRORS:
                    del errs[:-MAX_ERRORS]
            elif ev_type == "stopped":
                self._state["interactive_ready"] = False
                self._state["stopped"] = True
                self._state["stop_reason"] = event.get("stop_reason") or "stopped"
                self._state["current_stage"] = "stopped"
                self._in_flight.clear()
                self._state["pending_keys"] = []
                self._state["active_key"] = None
                self._stopped_event.set()
            elif ev_type == "error":
                errs = self._state["errors"]
                errs.append(
                    {
                        "kind": "error",
                        "reason": event.get("reason") or event.get("error") or "unknown_error",
                        "ts": now,
                    }
                )
                if len(errs) > MAX_ERRORS:
                    del errs[:-MAX_ERRORS]

    def enqueue_key(self, key: str, *, request_id: str | None = None) -> tuple[bool, int, dict[str, Any]]:
        """Validate and forward a live key_click command to boot.py.

        Returns (ok, http_status, response_dict).
        """
        if not isinstance(key, str) or key not in VALID_KEYS:
            return False, 400, {"ok": False, "error": "invalid_key", "key": key}

        req_id = request_id or uuid.uuid4().hex[:12]
        with self._lock:
            if not self._connected or self._client_sock is None:
                return False, 409, {"ok": False, "error": "emulator_not_connected"}
            if self._state["stopped"]:
                return False, 409, {"ok": False, "error": "session_stopped"}
            if not self._state["interactive_ready"]:
                return False, 409, {"ok": False, "error": "not_ready_for_input"}
            if len(self._in_flight) >= self.max_pending:
                return (
                    False,
                    429,
                    {
                        "ok": False,
                        "error": "key_queue_full",
                        "max_pending": self.max_pending,
                        "pending_count": len(self._in_flight),
                    },
                )
            entry: dict[str, Any] = {
                "id": req_id,
                "key": key,
                "status": "sending",
                "requested_ts": _utc_now(),
            }
            self._in_flight[req_id] = entry
            history = self._state["key_history"]
            history.append(entry)
            if len(history) > MAX_KEY_HISTORY:
                del history[:-MAX_KEY_HISTORY]
            self._sync_pending_locked()
            sock = self._client_sock

        sent = self._send_to_client(sock, {"type": "key_click", "id": req_id, "key": key})
        if not sent:
            with self._lock:
                self._in_flight.pop(req_id, None)
                entry["status"] = "send_failed"
                entry["error"] = "control_socket_write_failed"
                self._sync_pending_locked()
            return False, 503, {"ok": False, "error": "control_socket_write_failed", "id": req_id}

        return (
            True,
            202,
            {
                "ok": True,
                "id": req_id,
                "key": key,
                "status": "queued",
                "pending_count": len(self._in_flight),
            },
        )

    def request_stop(self, reason: str = "user_requested") -> bool:
        with self._lock:
            sock = self._client_sock
        if sock is None:
            return False
        return self._send_to_client(sock, {"type": "stop", "reason": reason})

    def wait_stopped(self, timeout: float = 10.0) -> bool:
        return self._stopped_event.wait(timeout=timeout)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "protocol": self._state["protocol"],
                "connected": self._state["connected"],
                "authenticated": self._state["authenticated"],
                "interactive_ready": self._state["interactive_ready"],
                "stopped": self._state["stopped"],
                "stop_reason": self._state["stop_reason"],
                "current_stage": self._state["current_stage"],
                "hello": dict(self._state["hello"]) if self._state["hello"] else None,
                "boot_stages": [dict(s) for s in self._state["boot_stages"]],
                "swaps": [dict(s) for s in self._state["swaps"]],
                "latest_swap": dict(self._state["latest_swap"]) if self._state["latest_swap"] else None,
                "ui_state": dict(self._state["ui_state"]) if isinstance(self._state["ui_state"], dict) else self._state["ui_state"],
                "pending_keys": [dict(k) for k in self._state["pending_keys"]],
                "pending_count": len(self._in_flight),
                "max_pending": self.max_pending,
                "active_key": dict(self._state["active_key"]) if self._state["active_key"] else None,
                "key_history": [dict(k) for k in self._state["key_history"][-40:]],
                "errors": [dict(e) for e in self._state["errors"]],
                "last_event_ts": self._state["last_event_ts"],
                "rejected_auth_attempts": self._state["rejected_auth_attempts"],
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sock = self._client_sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        try:
            self._server_sock.close()
        except OSError:
            pass
        self._accept_thread.join(timeout=2.0)
