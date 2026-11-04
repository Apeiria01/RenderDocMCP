"""Single-event, bounded CB and post-transform inspection on the replay queue."""
import struct

import renderdoc as rd

from ..utils import Helpers, Parsers, inspection
from ..utils.inspection import DEFAULT_CB_BYTES, MAX_READ_BYTES

INSPECTION_SERVICE_REVISION = "shader-inspection-2"


class InspectionService:
    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def _run(self, event_id, work):
        Helpers.validate_event_id(event_id)
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        result = {}

        def callback(controller):
            try:
                result["data"] = work(controller)
            except Exception as exc:
                result["error"] = exc

        self._invoke(callback)
        if "error" in result:
            raise result["error"]
        return result["data"]

    def get_constant_buffers(self, event_id, stage, block_index=None,
                             array_element=0, max_bytes=DEFAULT_CB_BYTES):
        integer, constant_buffers = inspection.integer, inspection.constant_buffers
        if not isinstance(stage, str) or stage not in (
                "vertex", "hull", "domain", "geometry", "pixel", "compute"):
            raise ValueError("Invalid shader stage")
        if block_index is not None:
            integer(block_index, "block_index")
        integer(array_element, "array_element")
        integer(max_bytes, "max_bytes", 1, MAX_READ_BYTES)
        stage_enum = Parsers.parse_stage(stage)

        def work(controller):
            Helpers.select_event(controller, event_id)
            pipe = controller.GetPipelineState()
            reflection = pipe.GetShaderReflection(stage_enum)
            if reflection is None:
                return {"event_id": event_id, "stage": stage, "supported": False,
                        "complete": False, "reason": "No shader reflection available"}
            result = constant_buffers(controller, pipe, reflection, stage_enum,
                                      block_index, array_element, max_bytes)
            result.update(event_id=event_id, stage=stage, supported=True,
                          shader_resource_id=str(reflection.resourceId),
                          entry_point=str(reflection.entryPoint))
            return result

        return self._run(event_id, work)

    def get_postvs_data(self, event_id, instance=0, view=0,
                        stage="vsout", max_bytes=MAX_READ_BYTES):
        integer = inspection.integer
        integer(instance, "instance")
        integer(view, "view")
        integer(max_bytes, "max_bytes", 1, MAX_READ_BYTES)
        if stage not in ("vsout", "gsout"):
            raise ValueError("stage must be vsout or gsout")

        def work(controller):
            # Walk CPU action metadata only, not SetFrameEvent across draws.
            pending = list(controller.GetRootActions())
            action = None
            while pending:
                candidate = pending.pop()
                if candidate.eventId == event_id:
                    action = candidate
                    break
                pending.extend(candidate.children)
            if action is None or not action.flags & rd.ActionFlags.Drawcall:
                raise ValueError("PostVS requires an exact draw action event_id")
            count = int(action.numInstances)
            if not action.flags & rd.ActionFlags.Instanced:
                count = max(1, count)
            if instance >= count:
                raise ValueError("instance is outside this draw's instance count")
            result = {"event_id": event_id, "instance": instance, "view": view,
                      "stage": stage, "byte_budget": max_bytes,
                      "supported": False, "complete": False,
                      "coordinate_space": "shader_output_not_world_space",
                      "transformed_by_exporter": False,
                      "instance_semantics": "zero-based index, not instanceOffset",
                      "buffers": {}}
            mesh_enum = getattr(getattr(rd, "MeshDataStage", None),
                                "VSOut" if stage == "vsout" else "GSOut", None)
            if mesh_enum is None or not hasattr(controller, "GetPostVSData"):
                result["reason"] = "This RenderDoc runtime does not expose GetPostVSData for the requested stage"
                return result
            controller.SetFrameEvent(event_id, False)
            pipe = controller.GetPipelineState()
            if not hasattr(pipe, "MultiviewBroadcastCount"):
                result["reason"] = "Runtime cannot validate view count; no post-transform read attempted"
                return result
            views = max(1, int(pipe.MultiviewBroadcastCount()))
            if view >= views:
                raise ValueError("view is outside MultiviewBroadcastCount")
            result["view_count"] = views
            try:
                mesh = controller.GetPostVSData(instance, view, mesh_enum)
                result["mesh_format"] = self._mesh_format(mesh)
                result["coordinate_space"] = ("post_projection_shader_output" if mesh.unproject
                                              else "shader_output_not_world_space")
                if str(mesh.status):
                    result["reason"] = str(mesh.status)
                    return result
                if mesh.vertexResourceId == rd.ResourceId.Null():
                    result["reason"] = "GetPostVSData returned no vertex buffer"
                    return result
                result["supported"] = True
                self._mesh_buffers(controller, mesh, max_bytes, result)
            except Exception as exc:
                result["reason"] = str(exc)
            return result

        return self._run(event_id, work)

    @staticmethod
    def _mesh_format(mesh):
        format_info = inspection.format_info
        json_number, resource_id = inspection.json_number, inspection.resource_id
        result = {"format": format_info(mesh.format), "missing_fields": []}
        fields = {
            "indexResourceId": resource_id, "indexByteOffset": int,
            "indexByteStride": int, "indexByteSize": int, "baseVertex": int,
            "vertexResourceId": resource_id, "vertexByteOffset": int,
            "vertexByteStride": int, "vertexByteSize": int,
            "topology": str, "numIndices": int, "instStepRate": int,
            "restartIndex": int, "nearPlane": float, "farPlane": float,
            "unproject": bool, "flipY": bool, "instanced": bool,
            "showAlpha": bool, "allowRestart": bool, "status": str,
            "meshletOffset": int, "meshletIndexOffset": int,
            "perPrimitiveOffset": int, "perPrimitiveStride": int,
        }
        for name, cast in fields.items():
            if hasattr(mesh, name):
                result[name] = json_number(cast(getattr(mesh, name)))
            else:
                result["missing_fields"].append(name)
        if hasattr(mesh, "meshColor"):
            result["meshColor"] = [json_number(float(getattr(mesh.meshColor, c))) for c in "xyzw"]
        if hasattr(mesh, "dispatchSize"):
            result["dispatchSize"] = [int(mesh.dispatchSize[i]) for i in range(3)]
        for name, members in (("meshletSizes", ("numIndices", "numVertices")),
                              ("taskSizes", ("x", "y", "z"))):
            if hasattr(mesh, name):
                values = getattr(mesh, name)
                result[name] = [{key: int(getattr(values[i], key)) for key in members}
                                for i in range(min(len(values), 1024))]
                if len(values) > 1024:
                    result[name + "_total"] = len(values)
                    result[name + "_truncated"] = True
            else:
                result["missing_fields"].append(name)
        return result

    @staticmethod
    def _mesh_buffers(controller, mesh, budget, result):
        raw_read = inspection.raw_read
        n = int(mesh.numIndices)
        stride = int(mesh.vertexByteStride)
        if n <= 0 or stride <= 0:
            result["reason"] = "Empty post-transform mesh or invalid vertex stride"
            return
        lengths = {str(b.resourceId): int(b.length) for b in controller.GetBuffers()}

        def bounds(resource, offset, length, base_offset, byte_size):
            if min(offset, length, base_offset) < 0:
                raise ValueError("Negative MeshFormat range")
            # Zero and all-ones denote unspecified ranges on some APIs.
            if byte_size not in (0, 0xffffffff, 0xffffffffffffffff):
                if offset < base_offset or offset + length > base_offset + byte_size:
                    raise ValueError("Requested range exceeds MeshFormat byte size")
            total = lengths.get(str(resource))
            if total is not None and offset + length > total:
                raise ValueError("Requested range exceeds resource buffer size")
            return total

        min_vertex, max_vertex = 0, n - 1
        if mesh.indexResourceId != rd.ResourceId.Null() and mesh.indexByteStride:
            index_stride = int(mesh.indexByteStride)
            if index_stride not in (1, 2, 4):
                result["reason"] = "Unsupported index byte stride"
                return
            size = n * index_stride
            if size > budget:
                result["reason"] = "Index range exceeds byte budget; no partial index interpretation"
                return
            offset = int(mesh.indexByteOffset)
            total = bounds(mesh.indexResourceId, offset, size, offset,
                           int(getattr(mesh, "indexByteSize", 0)))
            data = raw_read(controller, mesh.indexResourceId, offset, size, size)
            data["resource_byte_size"] = total
            result["buffers"]["index"] = data
            budget -= size
            if not data["complete"]:
                result["reason"] = "Short index buffer read; vertex range not inferred"
                return
            import base64
            raw = base64.b64decode(data["content_base64"])
            restart = int(mesh.restartIndex) & ((1 << (8 * index_stride)) - 1)
            low, high = None, None
            for (index,) in struct.iter_unpack("<" + {1: "B", 2: "H", 4: "I"}[index_stride], raw):
                if mesh.allowRestart and index == restart:
                    continue
                vertex = index + int(mesh.baseVertex)
                if vertex < 0:
                    raise ValueError("Index plus MeshFormat.baseVertex is negative")
                low = vertex if low is None else min(low, vertex)
                high = vertex if high is None else max(high, vertex)
            if low is None:
                result["reason"] = "Index buffer contains only restart indices"
                return
            min_vertex, max_vertex = low, high
        elif mesh.indexResourceId != rd.ResourceId.Null() or mesh.indexByteStride:
            result["reason"] = "Inconsistent MeshFormat index binding"
            return
        result["vertex_range"] = {"first_vertex": min_vertex, "last_vertex": max_vertex,
                                  "record_count": max_vertex - min_vertex + 1,
                                  "includes_unreferenced_gaps": True}
        offset = int(mesh.vertexByteOffset) + min_vertex * stride
        size = (max_vertex - min_vertex + 1) * stride
        result["vertex_range"].update(byte_offset=offset, required_byte_length=size)
        if size > budget:
            result["reason"] = "Vertex range exceeds remaining byte budget; vertex read skipped"
            return
        total = bounds(mesh.vertexResourceId, offset, size, int(mesh.vertexByteOffset),
                       int(getattr(mesh, "vertexByteSize", 0)))
        data = raw_read(controller, mesh.vertexResourceId, offset, size, size)
        data["resource_byte_size"] = total
        data["range_validation"] = ("resource_length_and_returned_length" if total is not None
                                    else "MeshFormat_derived_range_and_returned_length")
        result["buffers"]["vertex"] = data
        result["complete"] = data["complete"]
        if not data["complete"]:
            result["reason"] = "Short vertex buffer read"
