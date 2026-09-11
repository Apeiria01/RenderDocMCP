"""
Resource information service for RenderDoc.
"""

import base64

import renderdoc as rd

from ..utils import Parsers


class ResourceService:
    """Resource information service"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def _find_texture_by_id(self, controller, resource_id):
        """Find texture by resource ID"""
        target_id = Parsers.extract_numeric_id(resource_id)
        for tex in controller.GetTextures():
            tex_id_str = str(tex.resourceId)
            tex_id = Parsers.extract_numeric_id(tex_id_str)
            if tex_id == target_id:
                return tex
        return None

    @staticmethod
    def _validate_event_id(event_id):
        if event_id is not None and (
            isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0
        ):
            raise ValueError("event_id must be a positive integer")

    @staticmethod
    def _validate_byte_range(offset, length):
        for name, value in (("offset", offset), ("length", length)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("%s must be a non-negative integer" % name)

    @staticmethod
    def _select_event(controller, event_id):
        """Validate and select an event within the caller's replay callback."""
        if event_id is None:
            return

        # Include API events between actions, not just draw/dispatch event IDs.
        pending = list(controller.GetRootActions())
        while pending:
            action = pending.pop()
            if action.eventId == event_id or any(
                event.eventId == event_id for event in action.events
            ):
                # Reuse the current state when reading several resources at one event.
                controller.SetFrameEvent(event_id, False)
                return
            pending.extend(action.children)
        raise ValueError("Event not found in capture: %d" % event_id)

    def get_buffer_contents(self, resource_id, offset=0, length=0, event_id=None):
        """Read buffer bytes, optionally immediately after a specific event."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        self._validate_event_id(event_id)
        self._validate_byte_range(offset, length)
        result = {"data": None, "error": None}

        def callback(controller):
            # Resolve the buffer by numeric id. A ResourceId cannot be constructed
            # from a raw integer in the Python bindings (its `id` field is private),
            # so match against the live buffer list instead.
            target_id = Parsers.extract_numeric_id(resource_id)
            buf_desc = None
            for buf in controller.GetBuffers():
                if Parsers.extract_numeric_id(str(buf.resourceId)) == target_id:
                    buf_desc = buf
                    break

            if not buf_desc:
                result["error"] = "Buffer not found: %s" % resource_id
                return

            rid = buf_desc.resourceId

            if offset > buf_desc.length:
                result["error"] = "offset exceeds buffer size"
                return
            remaining = buf_desc.length - offset
            if length > remaining:
                result["error"] = "length exceeds remaining buffer size"
                return
            actual_length = length if length > 0 else remaining

            # Selection and readback must stay in the same BlockInvoke callback.
            try:
                self._select_event(controller, event_id)
                data = (controller.GetBufferData(rid, offset, actual_length)
                        if actual_length else b"")
            except Exception as e:
                result["error"] = "Failed to read buffer: %s" % str(e)
                return

            result["data"] = {
                "resource_id": resource_id,
                "event_id": event_id,
                "length": len(data),
                "total_size": buf_desc.length,
                "offset": offset,
                "content_base64": base64.b64encode(data).decode("ascii"),
            }

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def get_texture_info(self, resource_id):
        """Get texture metadata"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"texture": None, "error": None}

        def callback(controller):
            try:
                tex_desc = self._find_texture_by_id(controller, resource_id)

                if not tex_desc:
                    result["error"] = "Texture not found: %s" % resource_id
                    return

                result["texture"] = {
                    "resource_id": resource_id,
                    "width": tex_desc.width,
                    "height": tex_desc.height,
                    "depth": tex_desc.depth,
                    "array_size": tex_desc.arraysize,
                    "mip_levels": tex_desc.mips,
                    "format": str(tex_desc.format.Name()),
                    "dimension": str(tex_desc.type),
                    "msaa_samples": tex_desc.msSamp,
                    "byte_size": tex_desc.byteSize,
                }
            except Exception as e:
                import traceback
                result["error"] = "Error: %s\n%s" % (str(e), traceback.format_exc())

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["texture"]

    def get_texture_data(self, resource_id, mip=0, slice=0, sample=0, depth_slice=None,
                         event_id=None):
        """Read texture bytes, optionally immediately after a specific event."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        self._validate_event_id(event_id)
        result = {"data": None, "error": None}

        def callback(controller):
            tex_desc = self._find_texture_by_id(controller, resource_id)

            if not tex_desc:
                result["error"] = "Texture not found: %s" % resource_id
                return

            # Validate mip level
            if mip < 0 or mip >= tex_desc.mips:
                result["error"] = "Invalid mip level %d (texture has %d mips)" % (
                    mip,
                    tex_desc.mips,
                )
                return

            # Validate slice for array/cube textures
            max_slices = tex_desc.arraysize
            if tex_desc.cubemap:
                max_slices = tex_desc.arraysize * 6
            if slice < 0 or (max_slices > 1 and slice >= max_slices):
                result["error"] = "Invalid slice %d (texture has %d slices)" % (
                    slice,
                    max_slices,
                )
                return

            # Validate sample for MSAA
            if sample < 0 or (tex_desc.msSamp > 1 and sample >= tex_desc.msSamp):
                result["error"] = "Invalid sample %d (texture has %d samples)" % (
                    sample,
                    tex_desc.msSamp,
                )
                return

            # Calculate dimensions at this mip level
            mip_width = max(1, tex_desc.width >> mip)
            mip_height = max(1, tex_desc.height >> mip)
            mip_depth = max(1, tex_desc.depth >> mip)

            # Validate depth_slice for 3D textures
            is_3d = tex_desc.depth > 1
            if depth_slice is not None:
                if not is_3d:
                    result["error"] = "depth_slice can only be used with 3D textures"
                    return
                if depth_slice < 0 or depth_slice >= mip_depth:
                    result["error"] = "Invalid depth_slice %d (texture has %d depth at mip %d)" % (
                        depth_slice,
                        mip_depth,
                        mip,
                    )
                    return

            # Create subresource specification
            sub = rd.Subresource()
            sub.mip = mip
            sub.slice = slice
            sub.sample = sample

            # Selection and readback must stay in the same BlockInvoke callback.
            try:
                self._select_event(controller, event_id)
                data = controller.GetTextureData(tex_desc.resourceId, sub)
            except Exception as e:
                result["error"] = "Failed to get texture data: %s" % str(e)
                return

            # Extract depth slice for 3D textures if requested
            output_depth = mip_depth
            if is_3d and depth_slice is not None:
                total_size = len(data)
                bytes_per_slice = total_size // mip_depth
                slice_start = depth_slice * bytes_per_slice
                slice_end = slice_start + bytes_per_slice
                data = data[slice_start:slice_end]
                output_depth = 1

            result["data"] = {
                "resource_id": resource_id,
                "event_id": event_id,
                "width": mip_width,
                "height": mip_height,
                "depth": output_depth,
                "mip": mip,
                "slice": slice,
                "sample": sample,
                "depth_slice": depth_slice,
                "format": str(tex_desc.format.Name()),
                "dimension": str(tex_desc.type),
                "is_3d": is_3d,
                "total_depth": mip_depth if is_3d else 1,
                "data_length": len(data),
                "content_base64": base64.b64encode(data).decode("ascii"),
            }

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]
