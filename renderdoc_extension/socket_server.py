"""
File-based IPC Server for RenderDoc MCP Bridge.

Multi-instance layout: each RenderDoc instance owns a private mailbox
directory %TEMP%/renderdoc_mcp/instances/<pid>/ containing:
  request.json / response.json / lock  - compatible legacy exchange
  response-<id>.json / cancel-<id>.json - request-specific replies/cancellation
  active.json / client.lock            - in-flight request / client ownership
  info.json                            - identity + heartbeat, rewritten every
                                         ~2s so the MCP server can discover
                                         live instances and address exactly one

Uses a Qt timer when PySide2 is available. RenderDoc builds compiled without
PySide2 fall back to a small Python polling thread that schedules request
handling onto RenderDoc's UI thread.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

from .request_control import RequestControl, request_path

try:
    from PySide2.QtCore import QObject, QTimer

    HAS_QT = True
except ImportError:
    HAS_QT = False
    QTimer = None

    class QObject(object):
        pass


# IPC layout (must match mcp_server/bridge/client.py)
IPC_ROOT = os.path.join(tempfile.gettempdir(), "renderdoc_mcp")
INSTANCES_DIR = os.path.join(IPC_ROOT, "instances")

POLL_INTERVAL_MS = 100
HEARTBEAT_TICKS = 20  # rewrite info.json every N polls (~2s at 100ms/poll)
STALE_SECS = 60  # minimum age before pruning a positively confirmed dead PID


def _pid_alive(pid):
    """Return False only when death is established, None if unavailable.

    Some embedded Python builds omit ctypes. A stale heartbeat alone is
    never enough to delete another instance's mailbox.
    """
    if pid <= 0:
        return None
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False if ctypes.get_last_error() == 87 else None
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259
                return None
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return None


def _atomic_write_json(path, value):
    tmp = path + "." + os.urandom(16).hex() + ".tmp"
    try:
        with open(tmp, "x", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _remove_owned_json(path, request_id):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
        if value.get("id") == request_id:
            os.remove(path)
    except (OSError, ValueError, AttributeError):
        pass


class MCPBridgeServer(QObject):
    """File-based IPC server for MCP bridge communication (one per instance)."""

    def __init__(
        self, host, port, handler, parent=None, ui_invoker=None, info_provider=None
    ):
        if HAS_QT:
            super(MCPBridgeServer, self).__init__(parent)
        else:
            super(MCPBridgeServer, self).__init__()

        self.handler = handler
        self._ui_invoker = ui_invoker
        self._info_provider = info_provider
        self._timer = None
        self._thread = None
        self._stop_event = None
        self._running = False
        self._request_pending = False
        self._active_control = None
        self._generation = 0
        self._pending_lock = threading.Lock()
        self._heartbeat_tick = HEARTBEAT_TICKS  # write info.json on first poll

        self.instance_dir = os.path.join(INSTANCES_DIR, str(os.getpid()))
        self.request_file = os.path.join(self.instance_dir, "request.json")
        self.response_file = os.path.join(self.instance_dir, "response.json")
        self.lock_file = os.path.join(self.instance_dir, "lock")
        self.info_file = os.path.join(self.instance_dir, "info.json")
        self.active_file = os.path.join(self.instance_dir, "active.json")

        if not HAS_QT and self._ui_invoker is None:
            raise RuntimeError(
                "PySide2 is unavailable and no RenderDoc UI-thread invoker was provided"
            )

        if not os.path.exists(self.instance_dir):
            os.makedirs(self.instance_dir)
        # Publish identity immediately so concurrent starters don't mistake
        # this fresh mailbox for a crash leftover.
        self._write_info()

    def start(self):
        """Start polling for requests."""
        with self._pending_lock:
            self._generation += 1
            self._running = True
            self._request_pending = False
            self._active_control = None
        self._prune_stale_siblings()
        self._cleanup_files()
        self._write_info()

        if HAS_QT:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._poll_request)
            self._timer.start(POLL_INTERVAL_MS)
            backend = "Qt timer"
        else:
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._poll_loop,
                name="RenderDocMCPBridge",
            )
            self._thread.daemon = True
            self._thread.start()
            backend = "threaded poller"

        print("[MCP Bridge] File-based IPC server started (%s)" % backend)
        print("[MCP Bridge] Instance mailbox: %s" % self.instance_dir)
        return True

    def stop(self):
        """Stop the server."""
        with self._pending_lock:
            self._running = False
            self._generation += 1
            if self._active_control is not None:
                self._active_control.cancel()

        if self._timer:
            self._timer.stop()
            self._timer = None

        if self._stop_event:
            self._stop_event.set()

        if self._thread and threading.current_thread() is not self._thread:
            self._thread.join(1.0)

        self._thread = None
        self._stop_event = None
        try:
            shutil.rmtree(self.instance_dir)
        except Exception:
            pass
        print("[MCP Bridge] Server stopped")

    def is_running(self):
        """Check if the server is running."""
        return self._running

    def _poll_loop(self):
        while self._running and not self._stop_event.wait(POLL_INTERVAL_MS / 1000.0):
            self._poll_request()

    def _prune_stale_siblings(self):
        """Prune old mailboxes only when the PID is positively known dead."""
        try:
            names = os.listdir(INSTANCES_DIR)
        except Exception:
            return
        for name in names:
            dirpath = os.path.join(INSTANCES_DIR, name)
            if dirpath == self.instance_dir:
                continue
            try:
                if _pid_alive(int(name)) is not False:
                    continue
                info_file = os.path.join(dirpath, "info.json")
                if os.path.isfile(info_file):
                    with open(info_file, "r", encoding="utf-8") as f:
                        heartbeat = json.load(f).get("heartbeat", 0)
                    stale = time.time() - heartbeat > STALE_SECS
                else:
                    stale = time.time() - os.path.getmtime(dirpath) > STALE_SECS
                if stale:
                    shutil.rmtree(dirpath)
                    print("[MCP Bridge] Pruned stale instance mailbox: %s" % dirpath)
            except Exception:
                pass

    def _cleanup_files(self):
        for path in [self.request_file, self.response_file, self.lock_file, self.active_file]:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

    def _write_info(self):
        """Write identity + heartbeat. Runs on the UI thread (ctx access)."""
        info = {"pid": os.getpid(), "heartbeat": time.time()}
        if self._info_provider:
            try:
                info.update(self._info_provider())
            except Exception:
                pass
        # Version these guarantees independently of the RenderDoc version.
        # The client must not send full-frame searches to an older extension.
        info["protocol_version"] = 2
        info["capabilities"] = {"safe_search": 1, "shader_hash_enumeration": 1}
        try:
            # Self-heal: recreate the mailbox if something removed it
            if not os.path.isdir(self.instance_dir):
                os.makedirs(self.instance_dir)
            _atomic_write_json(self.info_file, info)
        except Exception:
            pass

    def _heartbeat(self):
        self._heartbeat_tick += 1
        if self._heartbeat_tick < HEARTBEAT_TICKS:
            return
        self._heartbeat_tick = 0
        if HAS_QT:
            self._write_info()
            return
        generation = self._generation
        try:
            self._ui_invoker(
                lambda: self._write_info()
                if self._running and generation == self._generation else None
            )
        except Exception:
            pass

    def _set_request_pending(self, value):
        with self._pending_lock:
            self._request_pending = value

    def _claim_request(self):
        with self._pending_lock:
            if self._request_pending:
                return False
            self._request_pending = True
            return True

    def _poll_request(self):
        if not self._running:
            return
        self._heartbeat()
        if not os.path.exists(self.request_file) or os.path.exists(self.lock_file):
            return
        if not self._claim_request():
            return

        try:
            with open(self.request_file, "r", encoding="utf-8") as request_file:
                request = json.load(request_file)
            if not isinstance(request, dict):
                raise ValueError("Request must be a JSON object")
            request_path(self.instance_dir, "cancel", request.get("id"))
            # Publish busy state before removing the request, closing the gap
            # in which another client could overwrite an in-flight exchange.
            _atomic_write_json(self.active_file, {"id": request.get("id")})
            os.remove(self.request_file)
        except Exception as exc:
            self._set_request_pending(False)
            print("[MCP Bridge] Error reading request: %s" % str(exc))
            traceback.print_exc()
            return

        generation = self._generation
        if HAS_QT:
            self._process_scheduled_request(request, generation)
            return

        try:
            self._ui_invoker(lambda: self._process_scheduled_request(request, generation))
        except Exception as exc:
            control = RequestControl(request, self.instance_dir)
            with self._pending_lock:
                if generation != self._generation or not self._running:
                    return
                self._active_control = control
            self._finish_request(request, {
                "id": request.get("id"),
                "error": {"code": -32603, "message": str(exc)},
            }, control, generation)
            print("[MCP Bridge] Error scheduling request: %s" % str(exc))
            traceback.print_exc()

    def _finish_request(self, request, response, control, generation):
        # Completion can arrive on the replay thread. Only file I/O and our
        # mutex are allowed here; never access the RenderDoc capture context.
        with self._pending_lock:
            if not self._running or generation != self._generation:
                return
            if self._active_control is not control:
                return  # Ignore accidental duplicate/late completion.
            cancelled = control.is_cancelled()
            # The response is the client's completion signal. Clear the busy
            # marker and state first so a client that immediately calls again
            # cannot observe success followed by a spurious "still running".
            # Hold the mutex through publication: the poller may only claim
            # the next request once this completion has committed.
            _remove_owned_json(self.active_file, request.get("id"))
            _remove_owned_json(control.cancel_file, request.get("id"))
            self._active_control = None
            self._request_pending = False
            try:
                if not cancelled:
                    response["pid"] = os.getpid()
                    destination = self.response_file
                    if request.get("response_mode") == "per_request":
                        destination = request_path(self.instance_dir, "response", request.get("id"))
                    _atomic_write_json(destination, response)
            except Exception as exc:
                print("[MCP Bridge] Error writing response: %s" % str(exc))
                traceback.print_exc()

    def _process_scheduled_request(self, request, generation=None):
        if generation is None:
            generation = self._generation
        control = RequestControl(request, self.instance_dir)
        with self._pending_lock:
            if not self._running or generation != self._generation:
                return
            self._active_control = control

        def complete(response):
            self._finish_request(request, response, control, generation)

        try:
            control.check_cancelled()
            handle_async = getattr(self.handler, "handle_async", None)
            if handle_async is not None and handle_async(request, complete, control):
                return
            response = self.handler.handle(request)
        except Exception as exc:
            if not control.is_cancelled():
                traceback.print_exc()
            response = {
                "id": request.get("id"),
                "error": {"code": -32603, "message": str(exc)},
            }
        complete(response)
