"""Cooperative request cancellation without RenderDoc or UI dependencies."""

import json
import os
import threading
import time


def request_path(directory, kind, request_id):
    # Encode JSON-RPC ids so they can never escape the mailbox. This needs
    # no optional hashlib/socket modules in RenderDoc's embedded Python.
    encoded = str(request_id).encode("utf-8")
    if len(encoded) > 100:
        raise ValueError("IPC request id exceeds 100 UTF-8 bytes")
    key = encoded.hex()
    return os.path.join(directory, "%s-%s.json" % (kind, key))


class RequestCancelled(RuntimeError):
    pass


class RequestControl:
    def __init__(self, request, instance_dir):
        self.request_id = request.get("id")
        self.deadline = request.get("deadline")
        if not isinstance(self.deadline, (int, float)):
            self.deadline = None
        self.cancel_file = request_path(instance_dir, "cancel", self.request_id)
        self._cancelled = threading.Event()
        self._next_file_check = 0.0

    def cancel(self):
        self._cancelled.set()

    def is_cancelled(self):
        if self._cancelled.is_set():
            return True
        if self.deadline is not None and time.time() >= self.deadline:
            return True
        # Large indexed searches may check this for every action. Keep the
        # deadline and in-process cancellation immediate, but bound filesystem
        # polling to 40 ms so 100k actions do not imply 100k open() calls.
        now = time.monotonic()
        if now < self._next_file_check:
            return False
        self._next_file_check = now + 0.04
        try:
            with open(self.cancel_file, "r", encoding="utf-8") as stream:
                if json.load(stream).get("id") == self.request_id:
                    self._cancelled.set()
                    return True
        except (OSError, ValueError, AttributeError):
            pass
        return False

    def check_cancelled(self):
        if self.is_cancelled():
            raise RequestCancelled("Request cancelled or deadline exceeded")
