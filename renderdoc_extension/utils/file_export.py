"""
Write replay readbacks to a local file instead of returning them as base64.

A full-resolution readback does not fit in one MCP stdio message: FastMCP sends
a dict result twice (text content + structured content), base64 adds a third,
and Claude's stdio client disconnects on any JSON-RPC line over 16 MB. A
2560x1440 RGBA8 texture (14.7 MB raw) becomes a ~39 MB message. Writing the
raw bytes here also keeps them out of the file IPC response.
"""

import hashlib
import os
import uuid

FILE_EXPORT_REVISION = "file-export-1"


def validate_output_path(output_path):
    """Reject anything but a non-empty absolute path before replay work starts."""
    if output_path is None:
        return None
    if not isinstance(output_path, str) or not output_path.strip():
        raise ValueError("output_path must be a non-empty string")
    if "\0" in output_path:
        raise ValueError("output_path must not contain NUL characters")
    drive = os.path.splitdrive(output_path)[0]
    # On Windows "\\foo" is drive-relative, not absolute: require a drive or UNC share.
    if not os.path.isabs(output_path) or (os.name == "nt" and not drive):
        raise ValueError("output_path must be an absolute path: %s" % output_path)
    return output_path


def resolve_output_path(output_path, default_name):
    """An existing directory, or a path ending in a separator, receives default_name."""
    if output_path.endswith(("/", "\\")) or os.path.isdir(output_path):
        return os.path.join(output_path, default_name)
    return output_path


def write_export(output_path, data, default_name):
    """Atomically write data (creating parent directories) and describe the file."""
    data = bytes(data)
    path = os.path.abspath(resolve_output_path(output_path, default_name))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex)
    try:
        with open(tmp, "wb") as stream:
            stream.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return {"output_path": path, "bytes_written": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}
