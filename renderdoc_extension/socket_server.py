"""
File-based IPC Server for RenderDoc MCP Bridge.

Multi-instance layout: each RenderDoc instance owns a private mailbox
directory %TEMP%/renderdoc_mcp/instances/<pid>/ containing:
  request.json / response.json / lock  - same exchange protocol as before
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
import tempfile
import threading
import time
import traceback

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
STALE_SECS = 60  # heartbeat older than this = crash leftover, safe to prune


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
        self._pending_lock = threading.Lock()
        self._heartbeat_tick = HEARTBEAT_TICKS  # write info.json on first poll

        self.instance_dir = os.path.join(INSTANCES_DIR, str(os.getpid()))
        self.request_file = os.path.join(self.instance_dir, "request.json")
        self.response_file = os.path.join(self.instance_dir, "response.json")
        self.lock_file = os.path.join(self.instance_dir, "lock")
        self.info_file = os.path.join(self.instance_dir, "info.json")

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
        self._running = True
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
        self._running = False

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
        """Remove leftover mailboxes of crashed instances (stale heartbeat)."""
        try:
            names = os.listdir(INSTANCES_DIR)
        except Exception:
            return
        for name in names:
            dirpath = os.path.join(INSTANCES_DIR, name)
            if dirpath == self.instance_dir:
                continue
            try:
                # Only prune on positive evidence of staleness: an info.json
                # with an old heartbeat, or an info-less dir that has sat
                # untouched for a long time. A mailbox created moments ago by
                # a concurrently-starting instance has no info.json yet and
                # must not be deleted.
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
        for path in [self.request_file, self.response_file, self.lock_file]:
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
        try:
            # Self-heal: recreate the mailbox if something removed it
            if not os.path.isdir(self.instance_dir):
                os.makedirs(self.instance_dir)
            tmp = self.info_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(info, f)
            os.replace(tmp, self.info_file)
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
        try:
            self._ui_invoker(self._write_info)
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
            os.remove(self.request_file)
        except Exception as exc:
            self._set_request_pending(False)
            print("[MCP Bridge] Error reading request: %s" % str(exc))
            traceback.print_exc()
            return

        if HAS_QT:
            self._process_scheduled_request(request)
            return

        try:
            self._ui_invoker(lambda: self._process_scheduled_request(request))
        except Exception as exc:
            self._set_request_pending(False)
            print("[MCP Bridge] Error scheduling request: %s" % str(exc))
            traceback.print_exc()

    def _process_scheduled_request(self, request):
        try:
            if not self._running:
                return

            try:
                response = self.handler.handle(request)
            except Exception as exc:
                traceback.print_exc()
                response = {
                    "id": request.get("id"),
                    "error": {"code": -32603, "message": str(exc)},
                }

            # Stamp the responder so the client can verify it reached the
            # instance it targeted.
            response["pid"] = os.getpid()

            with open(self.response_file, "w", encoding="utf-8") as response_file:
                json.dump(response, response_file)
        except Exception as exc:
            print("[MCP Bridge] Error processing request: %s" % str(exc))
            traceback.print_exc()
        finally:
            self._set_request_pending(False)
