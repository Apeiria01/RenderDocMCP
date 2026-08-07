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

STALE_SECS = 60  # dead-pid mailboxes older than this get pruned


class RenderDocBridgeError(Exception):
    """Error communicating with RenderDoc bridge"""

    pass


if sys.platform == "win32":
    import ctypes

    def _pid_alive(pid: int) -> bool:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)

else:

    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True


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
            except (OSError, ValueError):
                # info.json missing or mid-write; fall through to pid check
                pass

            # Prune only on positive evidence: dead pid, or a published
            # heartbeat that has gone stale. A mailbox whose info.json hasn't
            # appeared yet belongs to an instance that is still starting up.
            heartbeat = info.get("heartbeat")
            if not _pid_alive(pid) or (
                heartbeat is not None and now - heartbeat > STALE_SECS
            ):
                shutil.rmtree(dirpath, ignore_errors=True)
                continue

            instances.append(
                {
                    "pid": pid,
                    "loaded": info.get("loaded", False),
                    "filename": info.get("filename"),
                    "renderdoc_version": info.get("renderdoc_version"),
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
        }

        try:
            # Clean up any stale response file
            if os.path.exists(response_file):
                os.remove(response_file)

            # Create lock file to signal we're writing
            with open(lock_file, "w") as f:
                f.write("lock")

            # Write request
            with open(request_file, "w", encoding="utf-8") as f:
                json.dump(request, f)

            # Remove lock file to signal write complete
            os.remove(lock_file)

            # Wait for response
            start_time = time.time()
            while True:
                if os.path.exists(response_file):
                    # Small delay to ensure file is fully written
                    time.sleep(0.01)

                    with open(response_file, "r", encoding="utf-8") as f:
                        response = json.load(f)

                    os.remove(response_file)

                    if response.get("id") != request["id"]:
                        raise RenderDocBridgeError(
                            "Mismatched response id (is another MCP client "
                            "talking to the same RenderDoc instance?)"
                        )

                    responder = response.get("pid")
                    if expect_pid is not None and responder not in (None, expect_pid):
                        raise RenderDocBridgeError(
                            f"Response came from pid {responder}, expected "
                            f"{expect_pid}"
                        )

                    if "error" in response:
                        error = response["error"]
                        raise RenderDocBridgeError(
                            f"[{error['code']}] {error['message']}"
                        )

                    return response.get("result")

                # Check timeout
                if time.time() - start_time > self.timeout:
                    raise RenderDocBridgeError(
                        "Request timed out"
                        + (
                            f" (target RenderDoc instance pid {expect_pid})"
                            if expect_pid is not None
                            else " (legacy shared mailbox; is any RenderDoc "
                            "instance with the MCP Bridge extension running?)"
                        )
                    )

                # Poll interval
                time.sleep(0.05)

        except RenderDocBridgeError:
            raise
        except Exception as e:
            raise RenderDocBridgeError(f"Communication error: {e}")
