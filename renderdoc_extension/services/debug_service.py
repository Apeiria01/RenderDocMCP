"""Bounded shader debugging and pixel history, executed on the replay queue.

DebugPixel/DebugVertex/DebugThread hand back a ShaderDebugTrace that must be
stepped with ContinueDebug and released with FreeTrace. Every trace here lives
and dies inside a single replay callback: no debugger handle is ever cached
across IPC requests, so a cancelled or failed request cannot leak one.

Shader debugging perturbs replay state, so the requested event is re-selected
with SetFrameEvent(event_id, True) after the trace is freed, matching what
RenderDoc's own shader viewer does.
"""
import bisect
import struct
import time

import renderdoc as rd

from ..utils import Helpers, Parsers, Serializers
from ..utils.inspection import integer, json_number, resource_id

DEBUG_SERVICE_REVISION = "shader-debug-1"

DEFAULT_MAX_STEPS = 512
MAX_STEPS = 20000
DEFAULT_TIME_BUDGET_MS = 8000
MAX_TIME_BUDGET_MS = 25000
MAX_SOURCE_MAPPINGS = 512
MAX_SOURCE_FILES = 64
MAX_EVENT_MARKS = 64
DEFAULT_HISTORY_EVENTS = 256
MAX_HISTORY_EVENTS = 4096

_HISTORY_FAILURES = (
    ("sampleMasked", "sample_masked"),
    ("backfaceCulled", "backface_culled"),
    ("depthClipped", "depth_clipped"),
    ("depthBoundsFailed", "depth_bounds_failed"),
    ("viewClipped", "view_clipped"),
    ("scissorClipped", "scissor_clipped"),
    ("shaderDiscarded", "shader_discarded"),
    ("depthTestFailed", "depth_test_failed"),
    ("stencilTestFailed", "stencil_test_failed"),
    ("predicationSkipped", "predication_skipped"),
)


def _flag(name):
    return getattr(rd.ActionFlags, name, None)


class DebugService:
    """Single-invocation shader debugging. One request owns one trace."""

    def __init__(self, ctx, invoke_fn, request_control=None):
        self.ctx = ctx
        self._invoke = invoke_fn
        self._control = request_control

    # ==================== plumbing ====================

    def _run(self, work):
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

    def _check_cancelled(self):
        if self._control is not None:
            self._control.check_cancelled()

    @staticmethod
    def _find_action(controller, event_id, required_flag=None, kind=None):
        """Locate the exact action for an event without seeking replay."""
        Helpers.validate_event_id(event_id)
        pending = list(controller.GetRootActions())
        while pending:
            candidate = pending.pop()
            if candidate.eventId == event_id:
                if required_flag is not None and not candidate.flags & required_flag:
                    raise ValueError(
                        "event_id %d is not a %s action" % (event_id, kind))
                return candidate
            pending.extend(candidate.children)
        raise ValueError("Event not found in capture: %d" % event_id)

    @staticmethod
    def _options(max_steps=DEFAULT_MAX_STEPS, time_budget_ms=DEFAULT_TIME_BUDGET_MS,
                 include_steps=True, include_changes=True, include_source=True,
                 include_variables=True, include_constants=False,
                 include_source_mapping=False):
        integer(max_steps, "max_steps", 1, MAX_STEPS)
        integer(time_budget_ms, "time_budget_ms", 1, MAX_TIME_BUDGET_MS)
        flags = {"include_steps": include_steps, "include_changes": include_changes,
                 "include_source": include_source, "include_variables": include_variables,
                 "include_constants": include_constants,
                 "include_source_mapping": include_source_mapping}
        for name, value in flags.items():
            if not isinstance(value, bool):
                raise ValueError("%s must be a boolean" % name)
        options = {"max_steps": max_steps, "time_budget_ms": time_budget_ms}
        options.update(flags)
        return options

    # ==================== capabilities ====================

    def get_debug_capabilities(self):
        """Report whether this capture's API supports debugging at all."""
        def work(controller):
            props = controller.GetAPIProperties()
            return {
                "api": str(props.pipelineType),
                "local_renderer": str(props.localRenderer),
                "vendor": str(props.vendor),
                "degraded": bool(props.degraded),
                "remote_replay": bool(props.remoteReplay),
                "shader_debugging": bool(getattr(props, "shaderDebugging", False)),
                "pixel_history": bool(getattr(props, "pixelHistory", False)),
                "dxil_shaders": bool(getattr(props, "DXILShaders", False)),
                "runtime_methods": {name: hasattr(controller, name) for name in (
                    "DebugPixel", "DebugVertex", "DebugThread", "DebugMeshThread",
                    "ContinueDebug", "FreeTrace", "PixelHistory")},
                "debug_pixel_inputs_struct": hasattr(rd, "DebugPixelInputs"),
                "service_revision": DEBUG_SERVICE_REVISION,
                "limits": {"max_steps": MAX_STEPS,
                           "max_time_budget_ms": MAX_TIME_BUDGET_MS,
                           "max_history_events": MAX_HISTORY_EVENTS},
            }

        return self._run(work)

    # ==================== shader debugging ====================

    def _debug(self, event_id, required_flag, kind, start, options):
        """Select the event, run one bounded trace, free it and restore replay."""
        Helpers.validate_event_id(event_id)

        def work(controller):
            action = self._find_action(controller, event_id, required_flag, kind)
            props = controller.GetAPIProperties()
            result = {"event_id": event_id, "api": str(props.pipelineType),
                      "invocation": kind, "supported": False, "complete": False,
                      "shader_debugging_supported": bool(
                          getattr(props, "shaderDebugging", False)),
                      "service_revision": DEBUG_SERVICE_REVISION}
            if not result["shader_debugging_supported"]:
                result["reason"] = ("This API/replay reports no shader debugging support "
                                    "(APIProperties.shaderDebugging is false)")
                return result
            if not hasattr(controller, "ContinueDebug") or not hasattr(controller, "FreeTrace"):
                result["reason"] = "This RenderDoc runtime does not expose ContinueDebug/FreeTrace"
                return result
            controller.SetFrameEvent(event_id, False)
            trace = None
            try:
                trace = start(controller, action, result)
                if trace is None:
                    result["reason"] = "RenderDoc returned no debug trace for this invocation"
                else:
                    self._fill_trace(controller, trace, options, result)
            finally:
                if trace is not None:
                    controller.FreeTrace(trace)
                # Debugging leaves the replay in an implementation-defined
                # state; force a re-selection of the requested event.
                controller.SetFrameEvent(event_id, True)
            return result

        return self._run(work)

    def debug_pixel(self, event_id, x, y, sample=None, primitive=None, view=None, **options):
        """Debug the fragment shader invocation writing to one pixel."""
        opts = self._options(**options)
        integer(x, "x")
        integer(y, "y")
        for value, name in ((sample, "sample"), (primitive, "primitive"), (view, "view")):
            if value is not None:
                integer(value, name)

        def start(controller, action, result):
            result["pixel"] = {"x": x, "y": y, "sample": sample,
                               "primitive": primitive, "view": view,
                               "coordinate_origin": "top_left_of_the_bound_target"}
            if hasattr(rd, "DebugPixelInputs"):
                inputs = rd.DebugPixelInputs()
                if sample is not None:
                    inputs.sample = sample
                if primitive is not None:
                    inputs.primitive = primitive
                if view is not None:
                    inputs.view = view
                result["debug_pixel_signature"] = "inputs_struct"
                return controller.DebugPixel(x, y, inputs)
            # Older runtimes take sample/primitive positionally.
            no_preference = 0xffffffff
            result["debug_pixel_signature"] = "positional_sample_primitive"
            if view is not None:
                raise ValueError("This RenderDoc runtime cannot select a multiview view")
            return controller.DebugPixel(
                x, y, no_preference if sample is None else sample,
                no_preference if primitive is None else primitive)

        return self._debug(event_id, _flag("Drawcall"), "pixel", start, opts)

    def debug_vertex(self, event_id, vertex=0, instance=0, index=None, view=0, **options):
        """Debug one vertex shader invocation of a draw."""
        opts = self._options(**options)
        integer(vertex, "vertex")
        integer(instance, "instance")
        integer(view, "view")
        if index is not None:
            integer(index, "index")

        def start(controller, action, result):
            resolved = self._vertex_index(controller, action, vertex, index, result)
            result["vertex"] = {"vertex": vertex, "instance": instance,
                                "index": resolved, "view": view}
            return controller.DebugVertex(vertex, instance, resolved, view)

        return self._debug(event_id, _flag("Drawcall"), "vertex", start, opts)

    def debug_thread(self, event_id, group=None, thread=None, mesh=False, **options):
        """Debug one compute (or mesh/amplification) shader thread."""
        opts = self._options(**options)
        if not isinstance(mesh, bool):
            raise ValueError("mesh must be a boolean")
        group_id = self._triple(group, "group")
        thread_id = self._triple(thread, "thread")
        method_name = "DebugMeshThread" if mesh else "DebugThread"

        def start(controller, action, result):
            if not hasattr(controller, method_name):
                raise ValueError("This RenderDoc runtime does not expose %s" % method_name)
            result["thread"] = {"group": group_id, "thread": thread_id, "mesh": mesh}
            self._check_dispatch_bounds(action, group_id, thread_id, result)
            return getattr(controller, method_name)(group_id, thread_id)

        required = None if mesh else _flag("Dispatch")
        kind = "mesh_thread" if mesh else "thread"
        return self._debug(event_id, required, kind, start, opts)

    @staticmethod
    def _triple(value, name):
        if value is None:
            return [0, 0, 0]
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError("%s must be a list of three integers" % name)
        for component in value:
            integer(component, name)
        return [int(component) for component in value]

    @staticmethod
    def _check_dispatch_bounds(action, group, thread, result):
        """Reject out-of-range ids when the capture records the dimensions."""
        bounds = {}
        for attr, key, values, label in (
                ("dispatchDimension", "dispatch_dimension", group, "group"),
                ("dispatchThreadsDimension", "threads_dimension", thread, "thread")):
            recorded = getattr(action, attr, None)
            if recorded is None:
                continue
            limits = [int(recorded[i]) for i in range(3)]
            bounds[key] = limits
            if all(limit > 0 for limit in limits):
                for axis in range(3):
                    if values[axis] >= limits[axis]:
                        raise ValueError(
                            "%s[%d]=%d is outside the recorded %s %s"
                            % (label, axis, values[axis], key, limits))
        if bounds:
            result["dispatch_bounds"] = bounds

    @staticmethod
    def _vertex_index(controller, action, vertex, index, result):
        """Resolve the vertex-input index the way RenderDoc's mesh view does."""
        indexed_flag = _flag("Indexed")
        indexed = bool(indexed_flag is not None and action.flags & indexed_flag)
        info = {"indexed": indexed, "vertex": vertex,
                "num_indices": int(action.numIndices),
                "base_vertex": int(action.baseVertex),
                "index_offset": int(action.indexOffset),
                "vertex_offset": int(action.vertexOffset)}
        result["vertex_lookup"] = info
        if int(action.numIndices) and vertex >= int(action.numIndices):
            raise ValueError("vertex is outside this draw's numIndices")
        if index is not None:
            info.update(index=index, index_source="caller")
            return index
        if not indexed:
            resolved = vertex + int(action.vertexOffset)
            info.update(index=resolved, index_source="vertex_plus_vertexOffset")
            return resolved
        buffer = controller.GetPipelineState().GetIBuffer()
        stride = int(buffer.byteStride)
        if buffer.resourceId == rd.ResourceId.Null() or stride not in (1, 2, 4):
            raise ValueError(
                "Indexed draw without a readable index buffer; pass index explicitly")
        offset = int(buffer.byteOffset) + (int(action.indexOffset) + vertex) * stride
        data = bytes(controller.GetBufferData(buffer.resourceId, offset, stride))
        if len(data) < stride:
            raise ValueError("Short index buffer read; pass index explicitly")
        raw = struct.unpack_from({1: "<B", 2: "<H", 4: "<I"}[stride], data)[0]
        base = int(action.baseVertex)
        # Base vertex is applied after the fetch and clamps at zero, as in
        # RenderDoc's own CalcIndex.
        if base < 0:
            resolved = 0 if raw < -base else raw + base
        else:
            resolved = raw + base
        info.update(index=resolved, raw_index=int(raw),
                    index_byte_stride=stride, index_byte_offset=offset,
                    index_source="index_buffer_plus_baseVertex")
        return resolved

    # ==================== trace serialization ====================

    def _fill_trace(self, controller, trace, options, result):
        result["stage"] = str(trace.stage)
        result["debugger_available"] = trace.debugger is not None
        result["inputs"] = Serializers.serialize_variables(trace.inputs)
        for key, source in (("read_only_resources", trace.readOnlyResources),
                            ("read_write_resources", trace.readWriteResources),
                            ("samplers", trace.samplers)):
            result[key] = [{"name": str(var.name)} for var in source]
        if options["include_constants"]:
            result["constant_blocks"] = Serializers.serialize_variables(trace.constantBlocks)
        result.update(self._shader_debug_info(controller, trace))
        if options["include_source_mapping"]:
            result["source_variables"] = self._source_mappings(trace.sourceVars)
        if trace.debugger is None:
            result["reason"] = (
                "The trace has no debugger: this invocation was not executed (no "
                "fragment covers the pixel at this event, or the vertex/thread id is "
                "outside the draw) or the shader cannot be debugged. Check "
                "debug_info.debuggable and debug_info.debug_status.")
            return
        result["supported"] = True
        self._step(controller, trace, options, result)

    @staticmethod
    def _shader_debug_info(controller, trace):
        """Report debuggability and source file names from the reflection."""
        info = {"debug_info": {"available": False}}
        try:
            reflection = controller.GetPipelineState().GetShaderReflection(trace.stage)
            if reflection is None:
                return info
            info["shader_resource_id"] = resource_id(reflection.resourceId)
            info["entry_point"] = str(reflection.entryPoint)
            debug = reflection.debugInfo
            files = []
            for index, source in enumerate(debug.files):
                if index >= MAX_SOURCE_FILES:
                    break
                files.append({"index": index, "filename": str(source.filename)})
            info["debug_info"] = {
                "available": True,
                "debuggable": bool(getattr(debug, "debuggable", True)),
                "debug_status": str(getattr(debug, "debugStatus", "")),
                "source_debug_information": bool(
                    getattr(debug, "sourceDebugInformation", False)),
                "entry_source_name": str(getattr(debug, "entrySourceName", "")),
                "file_count": len(debug.files),
                "files": files,
            }
        except Exception as exc:
            info["debug_info"] = {"available": False, "error": str(exc)}
        return info

    @staticmethod
    def _source_mappings(source_vars):
        mappings = []
        for mapping in source_vars:
            if len(mappings) >= MAX_SOURCE_MAPPINGS:
                break
            mappings.append({
                "name": str(mapping.name),
                "type": str(mapping.type),
                "rows": int(mapping.rows),
                "columns": int(mapping.columns),
                "offset": int(mapping.offset),
                "signature_index": int(mapping.signatureIndex),
                "undefined_value": bool(getattr(mapping, "undefinedValue", False)),
                "variables": [{"name": str(ref.name), "type": str(ref.type),
                               "component": int(ref.component)}
                              for ref in mapping.variables],
            })
        return {"count": len(source_vars), "truncated": len(source_vars) > len(mappings),
                "mappings": mappings}

    @staticmethod
    def _line_lookup(trace):
        """instInfo is sparse and ordered; the lower-bound entry is valid."""
        instructions, infos = [], []
        for entry in trace.instInfo:
            instructions.append(int(entry.instruction))
            line = entry.lineInfo
            infos.append({"instruction": int(entry.instruction),
                          "disassembly_line": int(line.disassemblyLine),
                          "file_index": int(line.fileIndex),
                          "line_start": int(line.lineStart),
                          "line_end": int(line.lineEnd),
                          "column_start": int(line.colStart),
                          "column_end": int(line.colEnd)})
        ordered = all(instructions[i] <= instructions[i + 1]
                      for i in range(len(instructions) - 1))

        def lookup(instruction):
            if not instructions:
                return None
            if ordered:
                position = bisect.bisect_right(instructions, instruction) - 1
            else:
                position, best = -1, None
                for index, value in enumerate(instructions):
                    if value <= instruction and (best is None or value > best):
                        position, best = index, value
            if position < 0:
                return None
            entry = dict(infos[position])
            entry["exact"] = entry["instruction"] == instruction
            return entry

        return lookup

    @staticmethod
    def _events(flags):
        names = []
        for name in ("SampleLoadGather", "GeneratedNanOrInf", "DebugBreak"):
            value = getattr(rd.ShaderEvents, name, None)
            if value is not None and flags & value:
                names.append(name)
        return names

    def _step(self, controller, trace, options, result):
        """Run ContinueDebug under a step and wall-clock budget."""
        lookup = self._line_lookup(trace)
        deadline = time.monotonic() + options["time_budget_ms"] / 1000.0
        variables = {}
        steps = []
        marks = {"nan_or_inf": [], "debug_break": [], "sample_load_gather": 0,
                 "truncated": False}
        executed = 0
        last = None
        stop = "finished"
        while True:
            self._check_cancelled()
            batch = controller.ContinueDebug(trace.debugger)
            if not batch:
                break
            for state in batch:
                executed += 1
                events = self._events(state.flags)
                for name, key in (("GeneratedNanOrInf", "nan_or_inf"),
                                  ("DebugBreak", "debug_break")):
                    if name not in events:
                        continue
                    if len(marks[key]) < MAX_EVENT_MARKS:
                        marks[key].append(int(state.stepIndex))
                    else:
                        marks["truncated"] = True
                if "SampleLoadGather" in events:
                    marks["sample_load_gather"] += 1
                changes = self._apply(state, variables,
                                      options["include_steps"] and options["include_changes"])
                last = {"step_index": int(state.stepIndex),
                        "next_instruction": int(state.nextInstruction),
                        "events": events,
                        "callstack": [str(frame) for frame in state.callstack]}
                if options["include_source"]:
                    source = lookup(int(state.nextInstruction))
                    if source is not None:
                        last["next_instruction_source"] = source
                if options["include_steps"]:
                    record = dict(last)
                    if changes is not None:
                        record["changes"] = changes
                    steps.append(record)
                if executed >= options["max_steps"]:
                    stop = "max_steps"
                    break
            if stop != "finished":
                break
            if time.monotonic() >= deadline:
                stop = "time_budget_ms"
                break
        result["complete"] = stop == "finished"
        result["truncated"] = stop != "finished"
        result["stop_reason"] = stop
        result["steps_executed"] = executed
        result["step_budget"] = options["max_steps"]
        result["shader_events"] = {
            "nan_or_inf_step_indices": marks["nan_or_inf"],
            "debug_break_step_indices": marks["debug_break"],
            "sample_load_gather_steps": marks["sample_load_gather"],
            "step_indices_truncated": marks["truncated"],
        }
        if options["include_steps"]:
            result["steps"] = steps
        if last is not None:
            last_state = dict(last)
            if options["include_variables"]:
                last_state["variables"] = [variables[name] for name in sorted(variables)]
                last_state["variables_semantics"] = (
                    "mutable variables accumulated from every change, at the shader's "
                    "last step" if result["complete"] else
                    "mutable variables at the last executed step, NOT the shader's "
                    "final state")
            result["last_state"] = last_state
        elif result["complete"]:
            result["reason"] = "The debugger produced no states for this invocation"

    @staticmethod
    def _apply(state, variables, want_records):
        """Apply one state's changes forward, serializing each value once.

        An empty before name means the variable was created on this step, an
        empty after name that it went out of scope - RenderDoc's own forward
        application rule.
        """
        records = [] if want_records else None
        for change in state.changes:
            before_name = str(change.before.name)
            after_name = str(change.after.name)
            record = {"name": after_name or before_name}
            if not before_name:
                record["kind"] = "created"
            elif not after_name:
                record["kind"] = "destroyed"
            else:
                record["kind"] = "modified"
            if before_name and want_records:
                record["before"] = Serializers.serialize_variables([change.before])[0]
            if after_name:
                after = Serializers.serialize_variables([change.after])[0]
                variables[after_name] = after
                if want_records:
                    record["after"] = after
            elif before_name:
                variables.pop(before_name, None)
            if want_records:
                records.append(record)
        return records

    # ==================== pixel history ====================

    def pixel_history(self, event_id, x, y, target_resource_id=None, mip=None,
                      slice=None, sample=0, type_cast=None,
                      max_events=DEFAULT_HISTORY_EVENTS, include_values=True):
        """List every fragment that touched one pixel up to this event."""
        Helpers.validate_event_id(event_id)
        integer(x, "x")
        integer(y, "y")
        integer(sample, "sample")
        integer(max_events, "max_events", 1, MAX_HISTORY_EVENTS)
        if mip is not None:
            integer(mip, "mip")
        if slice is not None:
            integer(slice, "slice")
        if not isinstance(include_values, bool):
            raise ValueError("include_values must be a boolean")
        if target_resource_id is not None and not isinstance(target_resource_id, str):
            raise ValueError("target_resource_id must be a resource ID string")
        if type_cast is not None and not isinstance(type_cast, str):
            raise ValueError("type_cast must be a CompType name")

        def work(controller):
            self._find_action(controller, event_id)
            props = controller.GetAPIProperties()
            result = {"event_id": event_id, "api": str(props.pipelineType),
                      "supported": bool(getattr(props, "pixelHistory", False)),
                      "complete": False, "service_revision": DEBUG_SERVICE_REVISION}
            if not result["supported"]:
                result["reason"] = ("This API/replay reports no pixel history support "
                                    "(APIProperties.pixelHistory is false)")
                return result
            if not hasattr(controller, "PixelHistory"):
                result["supported"] = False
                result["reason"] = "This RenderDoc runtime does not expose PixelHistory"
                return result
            controller.SetFrameEvent(event_id, False)
            target, subresource = self._history_target(
                controller, result, target_resource_id, mip, slice, sample, x, y)
            cast = self._comp_type(type_cast)
            sub = rd.Subresource()
            sub.mip, sub.slice, sub.sample = subresource
            result["pixel"] = {"x": x, "y": y,
                               "coordinate_origin": "top_left_of_the_texture",
                               "type_cast": str(cast)}
            modifications = controller.PixelHistory(target, x, y, sub, cast)
            total = len(modifications)
            events = []
            for modification in modifications:
                if len(events) >= max_events:
                    break
                events.append(self._modification(modification, include_values))
            result.update(count=total, returned=len(events),
                          truncated=total > len(events), complete=total == len(events),
                          modifications=events)
            result["passed_event_ids"] = [entry["event_id"] for entry in events
                                          if entry.get("passed")]
            return result

        return self._run(work)

    @staticmethod
    def _comp_type(name):
        if name is None:
            return rd.CompType.Typeless
        value = getattr(rd.CompType, name, None)
        if value is None:
            raise ValueError("Unknown CompType: %s" % name)
        return value

    @classmethod
    def _history_target(cls, controller, result, requested, mip, slice, sample, x, y):
        """Resolve the texture, then bound-check the pixel before the native call.

        A ResourceId cannot be built from an integer in the Python bindings, so
        a requested id is matched against the live texture list by numeric id,
        exactly as the buffer/texture readers do.
        """
        textures = list(controller.GetTextures())
        if requested is not None:
            wanted = Parsers.extract_numeric_id(requested)
            texture = None
            for candidate in textures:
                if Parsers.extract_numeric_id(str(candidate.resourceId)) == wanted:
                    texture = candidate
                    break
            if texture is None:
                raise ValueError(
                    "Target resource is not a texture in this capture: %s" % requested)
            target = texture.resourceId
            source = "caller"
            first_mip, first_slice = 0, 0
        else:
            chosen = None
            for view in controller.GetPipelineState().GetOutputTargets():
                if view.resource != rd.ResourceId.Null():
                    chosen = view
                    break
            if chosen is None:
                raise ValueError(
                    "No render target is bound at this event; pass target_resource_id")
            target = chosen.resource
            source = "output_target_0"
            first_mip = int(getattr(chosen, "firstMip", 0))
            first_slice = int(getattr(chosen, "firstSlice", 0))
            texture = None
            for candidate in textures:
                if str(candidate.resourceId) == str(target):
                    texture = candidate
                    break
            if texture is None:
                raise ValueError(
                    "The bound render target is not a texture in this capture: %s"
                    % str(target))
        subresource = (first_mip if mip is None else mip,
                       first_slice if slice is None else slice,
                       sample)
        result["target"] = {"resource_id": str(target), "source": source,
                            "mip": subresource[0], "slice": subresource[1],
                            "sample": subresource[2]}
        cls._validate_pixel(texture, subresource, x, y, result["target"])
        return target, subresource

    @staticmethod
    def _validate_pixel(texture, subresource, x, y, info):
        """Fail before the native call rather than inside it."""
        mip, slice_index, sample = subresource
        width = max(1, int(texture.width) >> mip)
        height = max(1, int(texture.height) >> mip)
        info.update(format=texture.format.Name(), mip_width=width, mip_height=height,
                    mip_levels=int(texture.mips), array_size=int(texture.arraysize),
                    msaa_samples=int(texture.msSamp))
        if mip >= int(texture.mips):
            raise ValueError("mip is outside the texture's mip levels")
        if slice_index >= max(1, int(texture.arraysize)):
            raise ValueError("slice is outside the texture's array size")
        if sample >= max(1, int(texture.msSamp)):
            raise ValueError("sample is outside the texture's MSAA sample count")
        if x >= width or y >= height:
            raise ValueError(
                "Pixel (%d, %d) is outside the %dx%d mip %d of the target"
                % (x, y, width, height, mip))

    @classmethod
    def _modification(cls, modification, include_values):
        entry = {"event_id": int(modification.eventId),
                 "fragment_index": int(modification.fragIndex),
                 "primitive_id": int(modification.primitiveID),
                 "direct_shader_write": bool(modification.directShaderWrite),
                 "unbound_pixel_shader": bool(modification.unboundPS)}
        failures = []
        for attr, name in _HISTORY_FAILURES:
            if bool(getattr(modification, attr, False)):
                failures.append(name)
        entry["failed_tests"] = failures
        passed = getattr(modification, "Passed", None)
        entry["passed"] = bool(passed()) if callable(passed) else not failures
        if include_values:
            for attr, key in (("preMod", "pre_mod"), ("shaderOut", "shader_out"),
                              ("postMod", "post_mod")):
                entry[key] = cls._value(getattr(modification, attr))
        return entry

    @staticmethod
    def _value(value):
        colour = value.col
        valid = getattr(value, "IsValid", None)
        return {"color_float": [json_number(float(colour.floatValue[i])) for i in range(4)],
                "color_uint": [int(colour.uintValue[i]) for i in range(4)],
                "color_int": [int(colour.intValue[i]) for i in range(4)],
                "depth": json_number(float(value.depth)),
                "stencil": int(value.stencil),
                "valid": bool(valid()) if callable(valid) else None}
