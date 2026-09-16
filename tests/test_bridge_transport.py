"""IPC regression tests with real temporary mailboxes and no RenderDoc install."""

import importlib
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from mcp_server.bridge import client

PACKAGE = "_transport_test_extension"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "renderdoc_extension")]
with patch.dict(sys.modules, {PACKAGE: package}):
    server_module = importlib.import_module(PACKAGE + ".socket_server")
    control_module = importlib.import_module(PACKAGE + ".request_control")


class EchoHandler:
    def handle(self, request):
        return {"id": request["id"], "result": request["params"]}


class DeferredHandler:
    def __init__(self):
        self.ready = threading.Event()
        self.complete = None
        self.control = None
        self.request = None

    def handle_async(self, request, complete, control):
        self.request = request
        self.complete = complete
        self.control = control
        self.ready.set()
        return True


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.instances = os.path.join(self.temporary.name, "instances")
        os.makedirs(self.instances)
        self.patches = [
            patch.object(server_module, "INSTANCES_DIR", self.instances),
            patch.object(server_module, "HAS_QT", False),
            patch.object(client, "INSTANCES_DIR", self.instances),
            patch.object(client, "IPC_ROOT", self.temporary.name),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.server = server_module.MCPBridgeServer(
            None, None, EchoHandler(), ui_invoker=lambda callback: callback()
        )
        self.server._running = True
        self.server._heartbeat_tick = 0
        self.bridge = client.RenderDocBridge()
        self.bridge.timeout = 1.0
        self.worker_stop = threading.Event()
        self.workers = []
        self.addCleanup(self._stop_workers)

    def _stop_workers(self):
        self.worker_stop.set()
        for worker in self.workers:
            worker.join(2)
        self.server.stop()

    def _worker(self, target):
        worker = threading.Thread(target=target)
        worker.daemon = True
        worker.start()
        self.workers.append(worker)
        return worker

    def _start_poller(self):
        def poll():
            while not self.worker_stop.wait(0.005):
                self.server._poll_request()
        self._worker(poll)

    def _exchange(self, bridge=None):
        return (bridge or self.bridge)._exchange(
            "echo", {"answer": 42}, self.server.request_file,
            self.server.response_file, self.server.lock_file, os.getpid()
        )

    def _write(self, path, value):
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(value, stream)

    def _read(self, path):
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)

    def test_stale_live_heartbeat_never_prunes_instance(self):
        self._write(self.server.info_file, {"heartbeat": time.time() - 3600})
        with patch.object(client, "_pid_alive", return_value=True):
            instances = self.bridge._scan_instances()
        self.assertEqual([os.getpid()], [item["pid"] for item in instances])
        self.assertTrue(os.path.isdir(self.server.instance_dir))

    def test_client_prunes_definitely_dead_instance(self):
        dead = os.path.join(self.instances, "999999")
        os.makedirs(dead)
        with patch.object(client, "_pid_alive", side_effect=lambda pid: pid != 999999):
            self.bridge._scan_instances()
        self.assertFalse(os.path.exists(dead))

    def test_server_preserves_live_or_unknown_sibling(self):
        sibling = os.path.join(self.instances, "999999")
        os.makedirs(sibling)
        self._write(os.path.join(sibling, "info.json"), {"heartbeat": 1})
        for state in (True, None):
            with patch.object(server_module, "_pid_alive", return_value=state):
                self.server._prune_stale_siblings()
            self.assertTrue(os.path.isdir(sibling))
        with patch.object(server_module, "_pid_alive", return_value=False):
            self.server._prune_stale_siblings()
        self.assertFalse(os.path.exists(sibling))

    def test_roundtrip_preserves_foreign_response(self):
        foreign = {"id": "someone-else", "result": "keep"}
        self._write(self.server.response_file, foreign)
        self._start_poller()
        self.assertEqual({"answer": 42}, self._exchange())
        self.assertEqual(foreign, self._read(self.server.response_file))
        self.assertFalse(os.path.exists(self.server.active_file))

    def test_old_extension_partial_json_is_retried(self):
        def legacy_server():
            while not self.worker_stop.wait(0.002):
                if os.path.exists(self.server.request_file) and not os.path.exists(self.server.lock_file):
                    request = self._read(self.server.request_file)
                    os.remove(self.server.request_file)
                    with open(self.server.response_file, "w", encoding="utf-8") as stream:
                        stream.write('{"id":')
                    time.sleep(0.08)
                    self._write(self.server.response_file, {"id": request["id"], "result": "legacy"})
                    return
        self._worker(legacy_server)
        self.assertEqual("legacy", self._exchange())

    def test_queued_request_is_not_overwritten(self):
        original = {"id": "queued", "method": "other"}
        self._write(self.server.request_file, original)
        with self.assertRaisesRegex(client.RenderDocBridgeError, "queued or running"):
            self._exchange()
        self.assertEqual(original, self._read(self.server.request_file))

    def test_live_client_lock_is_not_stolen(self):
        path = os.path.join(self.server.instance_dir, "client.lock")
        owner = {"id": "other-client", "pid": os.getpid()}
        self._write(path, owner)
        with self.assertRaisesRegex(client.RenderDocBridgeError, "another MCP client"):
            self._exchange()
        self.assertEqual(owner, self._read(path))
        os.remove(path)

    def test_timeout_marks_exact_request_and_blocks_until_completion(self):
        handler = DeferredHandler()
        self.server.handler = handler
        self.bridge.timeout = 0.08
        self._start_poller()
        with self.assertRaisesRegex(client.RenderDocBridgeError, "cancellation requested"):
            self._exchange()
        self.assertTrue(handler.ready.is_set())
        self.assertTrue(handler.control.is_cancelled())
        self.assertEqual(handler.request["id"], self._read(handler.control.cancel_file)["id"])
        active = self._read(self.server.active_file)
        with self.assertRaisesRegex(client.RenderDocBridgeError, "queued or running"):
            self._exchange()
        self.assertEqual(active, self._read(self.server.active_file))
        handler.complete({"id": handler.request["id"], "result": "late"})
        self.assertFalse(os.path.exists(self.server.active_file))
        self.assertFalse(os.path.exists(handler.control.cancel_file))
        self.assertFalse(os.path.exists(control_module.request_path(
            self.server.instance_dir, "response", handler.request["id"])))
        self.server.handler = EchoHandler()
        self.bridge.timeout = 1.0
        self.assertEqual({"answer": 42}, self._exchange())

    def test_expired_queued_request_never_calls_handler(self):
        request = {"id": "expired", "method": "echo", "deadline": time.time() - 1}
        self._write(self.server.request_file, request)
        with patch.object(self.server.handler, "handle") as handle:
            self.server._poll_request()
        handle.assert_not_called()
        self.assertFalse(os.path.exists(self.server.active_file))
        self.assertFalse(self.server._request_pending)

    def test_cancel_marker_requires_matching_id(self):
        control = control_module.RequestControl({"id": "own"}, self.server.instance_dir)
        self._write(control.cancel_file, {"id": "other"})
        self.assertFalse(control.is_cancelled())
        self._write(control.cancel_file, {"id": "own"})
        with patch.object(control_module.time, "monotonic", return_value=control._next_file_check):
            with self.assertRaises(control_module.RequestCancelled):
                control.check_cancelled()

    def test_stop_cancels_async_and_late_completion_cannot_recreate_mailbox(self):
        handler = DeferredHandler()
        self.server.handler = handler
        request = {"id": "in-flight", "method": "echo"}
        self.server._process_scheduled_request(request)
        self.server.stop()
        self.assertTrue(handler.control.is_cancelled())
        handler.complete({"id": request["id"], "result": "too-late"})
        self.assertFalse(os.path.exists(self.server.instance_dir))
        # Even after a replacement extension creates the same PID directory,
        # completion from the old generation may not touch its files.
        os.makedirs(self.server.instance_dir)
        marker = os.path.join(self.server.instance_dir, "replacement")
        Path(marker).write_text("new", encoding="utf-8")
        handler.complete({"id": request["id"], "result": "too-late"})
        self.assertEqual(["replacement"], os.listdir(self.server.instance_dir))

    def test_scheduled_request_from_old_generation_is_ignored(self):
        generation = self.server._generation
        self.server._generation += 1
        with patch.object(self.server.handler, "handle") as handle:
            self.server._process_scheduled_request({"id": "old"}, generation)
        handle.assert_not_called()

    def test_response_publication_commits_after_busy_state_cleanup(self):
        request = {"id": "commit", "response_mode": "per_request"}
        control = control_module.RequestControl(request, self.server.instance_dir)
        self.server._active_control = control
        self.server._request_pending = True
        self._write(self.server.active_file, {"id": request["id"]})
        states_at_publication = []
        atomic_write = server_module._atomic_write_json

        def publish(path, response):
            states_at_publication.append((
                os.path.exists(self.server.active_file),
                self.server._request_pending,
                self.server._active_control,
            ))
            atomic_write(path, response)

        with patch.object(server_module, "_atomic_write_json", side_effect=publish):
            self.server._finish_request(
                request, {"id": request["id"], "result": "done"},
                control, self.server._generation,
            )
        self.assertEqual([(False, False, None)], states_at_publication)
        self._start_poller()
        self.assertEqual({"answer": 42}, self._exchange())

    def test_extension_advertises_safe_search_protocol(self):
        info = self._read(self.server.info_file)
        self.assertEqual(2, info["protocol_version"])
        self.assertEqual(1, info["capabilities"]["safe_search"])
        instance = self.bridge.list_instances()["instances"][0]
        self.assertEqual(2, instance["protocol_version"])
        self.assertEqual(1, instance["capabilities"]["safe_search"])

    def test_old_or_incomplete_capabilities_block_every_search_before_send(self):
        unsupported = [
            {"heartbeat": time.time()},
            {"protocol_version": 2},
            {"protocol_version": 1, "capabilities": {"safe_search": 1}},
            {"protocol_version": 2, "capabilities": {"safe_search": 0}},
            {"protocol_version": 2, "capabilities": "invalid"},
        ]
        for info in unsupported:
            self._write(self.server.info_file, info)
            for method in client.SAFE_SEARCH_METHODS:
                with self.subTest(info=info, method=method):
                    with patch.object(self.bridge, "_exchange") as exchange:
                        with self.assertRaisesRegex(client.RenderDocBridgeError, "Reload.*No search request"):
                            self.bridge.call(method, {})
                    exchange.assert_not_called()
                    self.assertFalse(os.path.exists(self.server.request_file))

    def test_legacy_mailbox_search_is_blocked_but_normal_calls_remain_compatible(self):
        with patch.object(self.bridge, "_resolve_target", return_value=None):
            with patch.object(self.bridge, "_exchange", return_value="ordinary") as exchange:
                with self.assertRaisesRegex(client.RenderDocBridgeError, "Reload"):
                    self.bridge.call("find_draws_by_resource", {})
                exchange.assert_not_called()
                self.assertEqual("ordinary", self.bridge.call("get_capture_status", {}))
                exchange.assert_called_once()

    def test_old_multi_instance_ordinary_call_is_still_allowed(self):
        self._write(self.server.info_file, {"heartbeat": time.time()})
        self._start_poller()
        self.assertEqual({"ordinary": True}, self.bridge.call("get_capture_status", {"ordinary": True}))

    def test_safe_search_is_allowed_after_updated_capability_appears(self):
        self._write(self.server.info_file, {"heartbeat": time.time()})
        with self.assertRaisesRegex(client.RenderDocBridgeError, "Reload"):
            self.bridge.call("find_draws_by_texture", {})
        self.server._write_info()
        self._start_poller()
        self.assertEqual({"name": "UI"}, self.bridge.call("find_draws_by_texture", {"name": "UI"}))

    def test_cancel_file_checks_are_throttled_and_cancellation_is_latched(self):
        control = control_module.RequestControl({"id": "throttle"}, self.server.instance_dir)
        real_open = open
        with patch.object(control_module.time, "monotonic", return_value=100.0):
            with patch("builtins.open", wraps=real_open) as file_open:
                for _ in range(100000):
                    self.assertFalse(control.is_cancelled())
            self.assertEqual(1, file_open.call_count)
        self._write(control.cancel_file, {"id": "throttle"})
        with patch.object(control_module.time, "monotonic", return_value=100.041):
            self.assertTrue(control.is_cancelled())
        os.remove(control.cancel_file)
        self.assertTrue(control.is_cancelled())

    def test_deadline_and_local_cancel_bypass_file_poll_throttle(self):
        control = control_module.RequestControl(
            {"id": "deadline", "deadline": 1000.0}, self.server.instance_dir
        )
        with patch.object(control_module.time, "time", return_value=999.0):
            self.assertFalse(control.is_cancelled())
        with patch.object(control_module.time, "time", return_value=1000.0):
            self.assertTrue(control.is_cancelled())
        control = control_module.RequestControl({"id": "local"}, self.server.instance_dir)
        self.assertFalse(control.is_cancelled())
        control.cancel()
        self.assertTrue(control.is_cancelled())

    def test_atomic_write_keeps_old_json_when_publish_fails(self):
        path = os.path.join(self.server.instance_dir, "atomic.json")
        self._write(path, {"version": "old"})
        for module in (client, server_module):
            with patch.object(module.os, "replace", side_effect=OSError("failed")):
                with self.assertRaises(OSError):
                    module._atomic_write_json(path, {"version": "new"})
            self.assertEqual({"version": "old"}, self._read(path))
            self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(self.server.instance_dir)))


if __name__ == "__main__":
    unittest.main()
