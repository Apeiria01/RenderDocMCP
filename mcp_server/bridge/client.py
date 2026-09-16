"""
RenderDoc Bridge Client
Communicates with the RenderDoc extension via file-based IPC.

Multi-instance aware: each RenderDoc instance owns a private mailbox at
%TEMP%/renderdoc_mcp/instances/<pid>/ and advertises itself via an info.json
heartbeat (see renderdoc_extension/socket_server.py). The client discovers
live instances, targets exactly one, and verifies the responder's pid.

If no per-instance mailboxes exist, falls back to the legacy single shared
mailbox (%TEMP%/renderdoc_mcp/request.json) so old extensions keep working.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from typing import Any

# IPC layout (must match renderdoc_extension/socket_server.py)
IPC_ROOT = os.path.join(tempfile.gettempdir(), "renderdoc_mcp")
INSTANCES_DIR = os.path.join(IPC_ROOT, "instances")

# Legacy single-mailbox layout (pre multi-instance extensions)
LEGACY_REQUEST_FILE = os.path.join(IPC_ROOT, "request.json")
LEGACY_RESPONSE_FILE = os.path.join(IPC_ROOT, "response.json")
LEGACY_LOCK_FILE = os.path.join(IPC_ROOT, "lock")

SAFE_SEARCH_METHODS = frozenset({
    "find_draws_by_texture", "find_draws_by_resource", "find_draws_by_shader",
    "find_resource_usage",
})


class RenderDocBridgeError(Exception):
    """Error communicating with RenderDoc bridge"""

    pass


def _pid_alive(pid: int) -> bool:
    """False only for a definitely dead PID; access denied is not death."""
    if not isinstance(pid, int) or pid <= 0:
        return True
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _atomic_write_json(path, value):
    tmp = path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(tmp, "x", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _request_path(directory, kind, request_id):
    encoded = str(request_id).encode("utf-8")
    if len(encoded) > 100:
        raise ValueError("IPC request id exceeds 100 UTF-8 bytes")
    key = encoded.hex()
    return os.path.join(directory, "%s-%s.json" % (kind, key))


def _remove_owned_json(path, request_id):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
        if value.get("id") == request_id:
            os.remove(path)
    except (OSError, ValueError, AttributeError):
        pass


class RenderDocBridge:
    """Client for communicating with RenderDoc extensions via file-based IPC"""

    def __init__(self, host: str = "127.0.0.1", port: int = 19876):
        # host/port are kept for API compatibility but not used
        self.host = host
        self.port = port
        self.timeout = 30.0  # seconds
        self._selected_pid: int | None = None
        self._call_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Instance discovery / selection
    # ------------------------------------------------------------------

    def _scan_instances(self) -> list[dict]:
        """List live instances, pruning mailboxes of dead processes."""
        instances = []
        try:
            names = os.listdir(INSTANCES_DIR)
        except OSError:
            return instances

        now = time.time()
        for name in names:
            dirpath = os.path.join(INSTANCES_DIR, name)
            try:
                pid = int(name)
            except ValueError:
                continue

            info = {}
            try:
                with open(
                    os.path.join(dirpath, "info.json"), "r", encoding="utf-8"
                ) as f:
                    info = json.load(f)
                if not isinstance(info, dict):
                    info = {}
            except (OSError, ValueError):
                # info.json missing or mid-write; fall through to pid check
                pass

            # A replay can keep the UI heartbeat stale for minutes. Prune
            # only a definitely dead PID, never an unresponsive live process.
            heartbeat = info.get("heartbeat")
            if not isinstance(heartbeat, (int, float)):
                heartbeat = None
            if not _pid_alive(pid):
                shutil.rmtree(dirpath, ignore_errors=True)
                continue

            instances.append(
                {
                    "pid": pid,
                    "loaded": info.get("loaded", False),
                    "filename": info.get("filename"),
                    "renderdoc_version": info.get("renderdoc_version"),
                    "protocol_version": info.get("protocol_version"),
                    "capabilities": info.get("capabilities", {}),
                    "heartbeat_age_sec": round(now - heartbeat, 1)
                    if heartbeat is not None
                    else None,
                }
            )

        instances.sort(key=lambda i: i["pid"])
        return instances

    def list_instances(self) -> dict:
        """List running RenderDoc instances and which one is selected."""
        with self._call_lock:
            instances = self._scan_instances()
            if self._selected_pid is not None and not any(
                i["pid"] == self._selected_pid for i in instances
            ):
                self._selected_pid = None
            for inst in instances:
                inst["selected"] = inst["pid"] == self._selected_pid
            result = {
                "count": len(instances),
                "selected_pid": self._selected_pid,
                "instances": instances,
            }
            if not instances:
                result["note"] = (
                    "No multi-instance mailboxes found. Either no RenderDoc is "
                    "running, or the running instances use the old single-mailbox "
                    "extension (calls will fall back to the legacy shared mailbox)."
                )
            return result

    def select_instance(self, pid: int) -> dict:
        """Select the RenderDoc instance that subsequent calls talk to."""
        with self._call_lock:
            instances = self._scan_instances()
            for inst in instances:
                if inst["pid"] == pid:
                    self._selected_pid = pid
                    inst["selected"] = True
                    return inst
            available = ", ".join(
                "%d (%s)" % (i["pid"], i["filename"] or "no capture")
                for i in instances
            )
            raise RenderDocBridgeError(
                f"No live RenderDoc instance with pid {pid}. "
                f"Available: {available or 'none'}"
            )

    def _resolve_target(self) -> int | None:
        """Pick the target pid, or None to use the legacy shared mailbox."""
        instances = self._scan_instances()

        if self._selected_pid is not None:
            if any(i["pid"] == self._selected_pid for i in instances):
                return self._selected_pid
            gone = self._selected_pid
            self._selected_pid = None
            raise RenderDocBridgeError(
                f"The selected RenderDoc instance (pid {gone}) is no longer "
                "running. Call list_instances and select_instance again."
            )

        if len(instances) == 1:
            self._selected_pid = instances[0]["pid"]
            return self._selected_pid

        if len(instances) == 0:
            return None  # legacy fallback

        listing = "; ".join(
            "pid %d: %s" % (i["pid"], i["filename"] or "no capture")
            for i in instances
        )
        raise RenderDocBridgeError(
            f"{len(instances)} RenderDoc instances are running ({listing}). "
            "Call select_instance(pid) to choose which one to talk to."
        )

    def _require_safe_search(self, target_pid):
        """Fail before publishing searches to an old, potentially quadratic server."""
        info = {}
        if target_pid is not None:
            try:
                path = os.path.join(INSTANCES_DIR, str(target_pid), "info.json")
                with open(path, "r", encoding="utf-8") as stream:
                    info = json.load(stream)
            except (OSError, ValueError):
                pass
        if isinstance(info, dict):
            protocol = info.get("protocol_version")
            capabilities = info.get("capabilities")
            safe_search = capabilities.get("safe_search") if isinstance(capabilities, dict) else None
            if (isinstance(protocol, int) and protocol >= 2
                    and isinstance(safe_search, int) and safe_search >= 1):
                return
        raise RenderDocBridgeError(
            "The selected RenderDoc extension does not advertise safe search "
            "(IPC protocol 2, safe_search capability). Reload the updated MCP "
            "Bridge extension in RenderDoc, then retry. No search request was sent."
        )

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call a method on the selected RenderDoc instance."""
        with self._call_lock:
            if not os.path.exists(IPC_ROOT):
                raise RenderDocBridgeError(
                    "Cannot connect to the RenderDoc MCP Bridge. Make sure "
                    "RenderDoc is running with the MCP Bridge extension loaded."
                )

            target_pid = self._resolve_target()
            if method in SAFE_SEARCH_METHODS:
                self._require_safe_search(target_pid)
            if target_pid is None:
                return self._exchange(
                    method,
                    params,
                    LEGACY_REQUEST_FILE,
                    LEGACY_RESPONSE_FILE,
                    LEGACY_LOCK_FILE,
                    expect_pid=None,
                )

            mailbox = os.path.join(INSTANCES_DIR, str(target_pid))
            return self._exchange(
                method,
                params,
                os.path.join(mailbox, "request.json"),
                os.path.join(mailbox, "response.json"),
                os.path.join(mailbox, "lock"),
                expect_pid=target_pid,
            )

    def _exchange(
        self,
        method: str,
        params: dict[str, Any] | None,
        request_file: str,
        response_file: str,
        lock_file: str,
        expect_pid: int | None,
    ) -> Any:
        request = {
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or {},
            "deadline": time.time() + self.timeout,
            "response_mode": "per_request",
        }
        mailbox = os.path.dirname(request_file)
        own_response = _request_path(mailbox, "response", request["id"])
        cancel_file = _request_path(mailbox, "cancel", request["id"])
        active_file = os.path.join(mailbox, "active.json")
        client_lock = os.path.join(mailbox, "client.lock")
        acquired = False
        submitted = False
        start_time = time.monotonic()

        try:
            # Serialize independent MCP clients, including across processes.
            # Never steal an active client's lock just because time elapsed.
            for attempt in range(2):
                try:
                    with open(client_lock, "x", encoding="utf-8") as stream:
                        json.dump({"id": request["id"], "pid": os.getpid()}, stream)
                    acquired = True
                    break
                except FileExistsError:
                    try:
                        with open(client_lock, "r", encoding="utf-8") as stream:
                            owner = json.load(stream)
                        owner_pid = owner.get("pid")
                        if isinstance(owner_pid, int) and not _pid_alive(owner_pid):
                            _remove_owned_json(client_lock, owner.get("id"))
                            continue
                    except (OSError, ValueError, AttributeError):
                        pass
                    raise RenderDocBridgeError("RenderDoc mailbox is busy with another MCP client")
            if not acquired:
                raise RenderDocBridgeError("Could not acquire the RenderDoc mailbox")
            if any(os.path.exists(path) for path in (request_file, active_file, lock_file)):
                raise RenderDocBridgeError(
                    "RenderDoc still has a queued or running request; wait for it to finish "
                    "or acknowledge cancellation before retrying"
                )

            # The legacy write lock is retained for older extensions. The
            # request itself is published by rename, so no reader sees half JSON.
            with open(lock_file, "x", encoding="utf-8") as stream:
                json.dump({"id": request["id"]}, stream)
            try:
                _atomic_write_json(request_file, request)
                submitted = True
            finally:
                _remove_owned_json(lock_file, request["id"])

            while True:
                # New extensions use request-specific files. Older extensions
                # still reply to response.json, which may be mid-write or belong
                # to a previous/other client; never consume somebody else's reply.
                for path in (own_response, response_file):
                    try:
                        with open(path, "r", encoding="utf-8") as stream:
                            response = json.load(stream)
                    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
                        continue
                    if not isinstance(response, dict) or response.get("id") != request["id"]:
                        continue
                    responder = response.get("pid")
                    if expect_pid is not None and responder not in (None, expect_pid):
                        raise RenderDocBridgeError(
                            f"Response came from pid {responder}, expected {expect_pid}"
                        )
                    _remove_owned_json(path, request["id"])
                    if "error" in response:
                        error = response["error"]
                        raise RenderDocBridgeError(f"[{error['code']}] {error['message']}")
                    return response.get("result")

                if time.monotonic() - start_time >= self.timeout:
                    # This is cooperative: an in-flight native replay call must
                    # return before the extension can inspect the token.
                    _atomic_write_json(cancel_file, {"id": request["id"]})
                    raise RenderDocBridgeError(
                        "Request timed out; cancellation requested"
                        + (
                            f" (target RenderDoc instance pid {expect_pid})"
                            if expect_pid is not None
                            else " (legacy shared mailbox; older extensions may not support cancellation)"
                        )
                    )
                time.sleep(0.05)
        except RenderDocBridgeError:
            raise
        except Exception as exc:
            if submitted:
                try:
                    _atomic_write_json(cancel_file, {"id": request["id"]})
                except OSError:
                    pass
            raise RenderDocBridgeError(f"Communication error: {exc}")
        finally:
            if acquired:
                _remove_owned_json(client_lock, request["id"])
