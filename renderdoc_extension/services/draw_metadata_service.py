"""Independent G2 metadata reads on the official replay queue after a UI capture check."""
import renderdoc as rd

from ..utils import draw_metadata
from . import context_service

DRAW_METADATA_SERVICE_REVISION = "draw-metadata-3"


class DrawMetadataService:
    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    @staticmethod
    def _function(obj, name, result, path):
        value = draw_metadata.member(obj, name, result, path)
        if value is draw_metadata.MISSING:
            result["supported"] = False
            return None
        if not callable(value):
            draw_metadata.diagnostic(result, "missing_api", path, "Required runtime API is not callable")
            result["supported"] = False
            return None
        return value

    def _roots(self, controller, result):
        getter = self._function(controller, "GetRootActions", result, "ReplayController.GetRootActions")
        if getter is None:
            return draw_metadata.MISSING
        roots = getter()
        if roots is None:
            draw_metadata.diagnostic(result, "missing_data", "actions", "Root action metadata is null")
        return roots

    def _event_snapshot(self, controller, event_id, result):
        # Most callers select a real action. Locate it without reading unrelated
        # queue/root.events arrays, which can contain tens of thousands of APIs.
        # Do not prune children by parent EID: synthetic parents can be later.
        fast = {"supported": True, "diagnostics": []}
        action_budget = context_service.Budget(fast)
        roots = self._roots(controller, result)
        if roots is draw_metadata.MISSING or roots is None:
            records, action_complete = [], False
        else:
            records, action_complete = context_service.scan_actions(
                controller, fast, action_budget, roots=roots, roots_supplied=True)
        action_matches = [row for row in records if row[0] == event_id]
        if action_complete and len(action_matches) == 1 and not fast["diagnostics"]:
            result["selection"] = {"event_found": True,
                                   "source": "ReplayController exact action metadata",
                                   "traversal": action_budget.summary(),
                                   "action_scan_complete": True}
            return True
        if action_complete and len(action_matches) > 1:
            result["selection"] = {"event_found": False, "action_scan_complete": True,
                                   "traversal": action_budget.summary()}
            draw_metadata.diagnostic(result, "duplicate_action", "event_id",
                                     "Multiple action records have the requested EID; no state read attempted")
            return False
        # Preserve APIEvent-only selection, and its existing independent budget.
        budget = draw_metadata.TraversalBudget()
        found = False
        for kind, node, owner in draw_metadata.walk(roots, budget, result):
            candidate = draw_metadata.native_event_id(node, result, kind + ".eventId")
            if candidate is None:
                budget.metadata_complete = False
            elif candidate == event_id:
                found = True
                break
        result["selection"] = {"event_found": found, "source": "ReplayController action/APIEvent metadata",
                               "traversal": budget.summary()}
        if not found:
            incomplete = budget.reason or not budget.metadata_complete
            draw_metadata.diagnostic(result, "event_unverified" if incomplete else "event_not_found",
                                     "event_id", "No exact event membership established; SetFrameEvent and state reads were not attempted")
        return found

    def _run(self, result, work):
        # CaptureContext belongs to UI; native metadata belongs to the callback.
        if not self.ctx.IsCaptureLoaded():
            result["supported"] = False
            draw_metadata.diagnostic(result, "no_capture", "capture", "No capture loaded")
            return draw_metadata.finish(result)
        # Exceptions terminate the callback. No restore, alternate target, or
        # later native reads are scheduled, even if BlockInvoke swallows them.
        outcome = {}

        def callback(controller):
            try:
                work(controller)
            except Exception as exc:
                outcome["error"] = exc
            else:
                outcome["finished"] = True

        self._invoke(callback)
        if "error" in outcome:
            raise outcome["error"]
        if not outcome.get("finished"):
            raise RuntimeError("BlockInvoke did not complete the metadata callback")
        return draw_metadata.finish(result)

    def _pipeline_api(self, controller, result):
        getter = self._function(controller, "GetAPIProperties", result, "ReplayController.GetAPIProperties")
        if getter is None:
            return draw_metadata.MISSING
        api = draw_metadata.member(getter(), "pipelineType", result, "APIProperties.pipelineType")
        if api is draw_metadata.MISSING:
            result["supported"] = False
            return api
        result["api"] = draw_metadata.scalar(api, "enum", result, "APIProperties.pipelineType")
        return api

    def _d3d12(self, controller, result):
        api = self._pipeline_api(controller, result)
        if api is draw_metadata.MISSING:
            return False
        d3d12 = getattr(getattr(rd, "GraphicsAPI", None), "D3D12", draw_metadata.MISSING)
        if d3d12 is draw_metadata.MISSING or api != d3d12:
            result["supported"] = False
            draw_metadata.diagnostic(result, "unsupported_api", "api", "Rasterizer metadata requires an explicitly identified D3D12 pipeline")
            return False
        return True

    def get_rasterizer_state(self, event_id):
        draw_metadata.integer(event_id, "event_id")
        result = draw_metadata.result_base("get_rasterizer_state", event_id=event_id)
        result["complete_scope"] = "requested rasterizer and PSO sampling metadata, not shader execution or image equivalence"
        result["sampling"] = draw_metadata.sampling_base()

        def work(controller):
            if not self._d3d12(controller, result):
                return
            if not self._event_snapshot(controller, event_id, result):
                return
            seek = self._function(controller, "SetFrameEvent", result, "ReplayController.SetFrameEvent")
            getter = self._function(controller, "GetD3D12PipelineState", result, "ReplayController.GetD3D12PipelineState")
            if seek is None or getter is None:
                return
            seek(event_id, False)
            result["selection"]["set_frame_event_completed"] = True
            result["selection"]["semantics"] = "state after the exact requested event; not a before-draw proof"
            state = getter()
            draw_metadata.rasterizer(state, result)
            draw_metadata.SamplingReader(rd, result).read(controller, state)

        return self._run(result, work)

    def get_shader_artifact(self, event_id, stage, max_bytes=1048576,
                            disassembly_target=None, max_text_bytes=1048576):
        draw_metadata.shader_arguments(event_id, stage, max_bytes, disassembly_target, max_text_bytes)
        result = draw_metadata.result_base("get_shader_artifact", event_id=event_id, stage=stage)
        result["complete_scope"] = "requested shader metadata, not interpolation verification"
        result["interpolation"] = {"available": False, "complete": False,
                                   "reason": "SigParameter does not expose interpolation; no mode is inferred from names"}
        result["disassembly"] = {"requested": disassembly_target is not None,
                                 "target": disassembly_target, "available": False,
                                 "complete": disassembly_target is None,
                                 "text_byte_budget": max_text_bytes}

        def work(controller):
            if self._pipeline_api(controller, result) is draw_metadata.MISSING:
                return
            if not self._event_snapshot(controller, event_id, result):
                return
            stage_enum = getattr(getattr(rd, "ShaderStage", None), "Vertex" if stage == "vertex" else "Pixel", draw_metadata.MISSING)
            if stage_enum is draw_metadata.MISSING:
                result["supported"] = False
                draw_metadata.diagnostic(result, "missing_api", "ShaderStage", "Requested stage enum is unavailable")
                return
            seek = self._function(controller, "SetFrameEvent", result, "ReplayController.SetFrameEvent")
            get_pipe = self._function(controller, "GetPipelineState", result, "ReplayController.GetPipelineState")
            if seek is None or get_pipe is None:
                return
            seek(event_id, False)
            result["selection"]["set_frame_event_completed"] = True
            pipe = get_pipe()
            get_shader = self._function(pipe, "GetShader", result, "PipeState.GetShader")
            get_reflection = self._function(pipe, "GetShaderReflection", result, "PipeState.GetShaderReflection")
            if get_shader is None or get_reflection is None:
                return
            shader = get_shader(stage_enum)
            if shader == rd.ResourceId.Null():
                draw_metadata.diagnostic(result, "no_shader", "shader", "No shader is bound at the requested stage")
                return
            result["shader_resource_id"] = draw_metadata.scalar(shader, "resource", result, "shader_resource_id")
            reflection = get_reflection(stage_enum)
            if reflection is None:
                draw_metadata.diagnostic(result, "no_reflection", "reflection", "No shader reflection is available")
                return
            reflected_shader = draw_metadata.member(reflection, "resourceId", result, "ShaderReflection.resourceId")
            result["reflection_resource_id"] = draw_metadata.scalar(reflected_shader, "resource", result, "ShaderReflection.resourceId")
            if reflected_shader is not draw_metadata.MISSING and reflected_shader != shader:
                draw_metadata.diagnostic(result, "shader_identity_mismatch", "ShaderReflection.resourceId", "Reflection does not identify the bound shader")
            result["entry_point"] = draw_metadata.text(draw_metadata.member(
                reflection, "entryPoint", result, "ShaderReflection.entryPoint"), result, "ShaderReflection.entryPoint")
            result["encoding"] = draw_metadata.scalar(draw_metadata.member(
                reflection, "encoding", result, "ShaderReflection.encoding"), "enum", result, "ShaderReflection.encoding")
            result["raw"] = draw_metadata.raw_shader(draw_metadata.member(
                reflection, "rawBytes", result, "ShaderReflection.rawBytes"), max_bytes, result)
            result["input_signature"] = draw_metadata.signatures(reflection, "inputSignature", result)
            result["output_signature"] = draw_metadata.signatures(reflection, "outputSignature", result)
            get_targets = self._function(controller, "GetDisassemblyTargets", result, "ReplayController.GetDisassemblyTargets")
            if get_targets is None:
                return
            targets = get_targets(True)
            known = []
            for i in range(draw_metadata.sequence(targets, draw_metadata.MAX_TARGETS, result, "disassembly_targets")):
                target = draw_metadata.text(targets[i], result, "disassembly_targets[%d]" % i)
                if target is not None:
                    known.append(target)
            result["disassembly_targets"] = known
            if disassembly_target is None:
                return
            if disassembly_target not in known:
                draw_metadata.diagnostic(result, "unknown_disassembly_target", "disassembly_target", "Explicit target is not in the bounded native target list; disassembly was not called")
                return
            disassemble = self._function(controller, "DisassembleShader", result, "ReplayController.DisassembleShader")
            get_graphics = self._function(pipe, "GetGraphicsPipelineObject", result, "PipeState.GetGraphicsPipelineObject")
            if disassemble is None or get_graphics is None:
                return
            output = disassemble(get_graphics(), reflection, disassembly_target)
            output = draw_metadata.text(output, result, "disassembly.text", max_text_bytes)
            if output is not None:
                if not output:
                    draw_metadata.diagnostic(result, "empty_disassembly", "disassembly.text", "Native disassembly returned empty text")
                result["disassembly"].update(available=True, complete=bool(output), text=output,
                                              text_byte_length=len(output.encode("utf-8")))

        return self._run(result, work)

    def get_api_events(self, event_id_min, event_id_max, max_events=128):
        draw_metadata.event_arguments(event_id_min, event_id_max, max_events)
        result = draw_metadata.result_base("get_api_events", event_id_min=event_id_min,
                                           event_id_max=event_id_max)
        result.update(events=[], count=0, max_events=max_events, duplicate_records=0, conflicting_records=0,
                      order="action_tree_traversal", global_sort_performed=False,
                      next_cursor=None, cursor_reliable=False,
                      traversal_complete=False, deduplication_complete=False,
                      file_offset_semantics="APIEvent.fileOffset is not an absolute capture byte offset",
                      boundary_semantics="Official APIEvent IDs only; no synthetic or before-draw boundary inference")

        def work(controller):
            budget = draw_metadata.TraversalBudget()
            roots = self._roots(controller, result)
            if roots is draw_metadata.MISSING or roots is None:
                budget.metadata_complete = False
                result["traversal"] = budget.summary()
                return
            get_file = self._function(controller, "GetStructuredFile", result, "ReplayController.GetStructuredFile")
            if get_file is None:
                budget.metadata_complete = False
                result["traversal"] = budget.summary()
                return
            chunks = draw_metadata.member(get_file(), "chunks", result, "SDFile.chunks")
            if chunks is draw_metadata.MISSING or chunks is None:
                if chunks is None:
                    draw_metadata.diagnostic(result, "missing_data", "SDFile.chunks", "Structured chunks are null")
                budget.metadata_complete = False
                result["traversal"] = budget.summary()
                return
            seen = {}
            stopped = False
            for kind, node, owner in draw_metadata.walk(roots, budget, result):
                if kind != "event":
                    continue
                eid = draw_metadata.native_event_id(node, result, "APIEvent.eventId")
                if eid is None:
                    budget.metadata_complete = False
                    continue
                if not event_id_min <= eid <= event_id_max:
                    continue
                record = draw_metadata.event_record(node, owner, chunks, result)
                variants = seen.setdefault(eid, [])
                if record in variants:
                    result["duplicate_records"] += 1
                    continue
                if variants:
                    result["conflicting_records"] += 1
                    draw_metadata.diagnostic(result, "conflicting_event", "APIEvent.eventId", "Same event ID has different official metadata; no record was silently replaced")
                variants.append(record)
                result["events"].append(record)
                # Stop without accessing another event. It is intentionally unknown
                # whether this was the last matching record in the whole capture.
                if len(result["events"]) >= max_events:
                    stopped = True
                    draw_metadata.diagnostic(result, "event_output_limit", "events", "Result limit reached before traversal completed; no reliable cursor")
                    break
            result["traversal"] = budget.summary()
            result["traversal_complete"] = not stopped and not budget.reason and budget.metadata_complete
            result["deduplication_complete"] = result["traversal_complete"] and not result["diagnostics"]
            result["count"] = len(result["events"])

        return self._run(result, work)
