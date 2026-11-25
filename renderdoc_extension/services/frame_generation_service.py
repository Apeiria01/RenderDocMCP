"""DLSS Frame Generation events recorded by the RenderDoc fork; metadata only.

The fork (C:\\renderdoc, d3d12_ngx.cpp) serializes every NVSDK_NGX_D3D12_EvaluateFeature
call for NGX feature 11 as the command-list chunk "NGX::DLSSG_Evaluate", replayed as a
Copy action named "DLSS-G Evaluate #N (i/n)", and the outputs it read back while
capturing as the device chunk "Internal::DLSSG_SavedOutputs". Upstream RenderDoc has
neither chunk. One metadata-only replay callback: zero SetFrameEvent, no texture or
buffer reads; saved image bytes ("Contents") are never read.
"""
import time

import renderdoc as rd

from . import context_service as cs

FRAME_GENERATION_REVISION = "dlssg-events-1"
EVALUATE_CHUNK = "NGX::DLSSG_Evaluate"
SAVED_OUTPUTS_CHUNK = "Internal::DLSSG_SavedOutputs"
EVALUATE_NAME_PREFIX = "DLSS-G Evaluate"
# Chunk member names in serialization order (Serialise_NGXEvaluateFrameGen).
ROLES = ("Backbuffer", "Depth", "MotionVectors", "HUDLess", "UI", "UIAlpha",
         "BidirectionalDistortionField", "OutputInterpolated", "OutputReal",
         "OutputDisableInterpolation")
# Per-output members of Serialise_NGXFrameGenOutputs, repeated NumOutputs times.
OUTPUT_FIELDS = {"IsBuffer": "is_buffer", "Format": "dxgi_format", "Width": "width",
                 "Height": "height", "RowPitch": "row_pitch", "ContentsLength": "bytes"}
MAX_SAVED_CHILDREN = 2 + 8 * 32
MAX_STRUCTURED_CHUNKS = 1 << 24
TIME_BUDGET_MS = 5000
NULL_RESOURCE = "ResourceId::0"
# ActionFlags values (replay_enums.h) for runtimes or fixtures without the enum member.
COPY_FLAG, PRESENT_FLAG = 0x0400, 0x0100


class Budget(cs.Budget):
    """Same accounting as context_service, with a budget sized for a whole-capture scan."""

    def check(self, path):
        if self.reason:
            return False
        if (time.monotonic() - self.start) * 1000 >= TIME_BUDGET_MS:
            return self.stop("time_budget", path)
        return True

    def summary(self):
        return dict(cs.Budget.summary(self), time_budget_ms=TIME_BUDGET_MS)


def flag(name, fallback):
    return getattr(getattr(rd, "ActionFlags", None), name, fallback)


def compact(node):
    """Typed SDReader output -> plain values; arrays become lists, structs dicts."""
    if not isinstance(node, dict):
        return None
    if "children" in node:
        kids = node.get("children") or []
        if node.get("type") == "Array":
            return [compact(child) for child in kids]
        return {child.get("name"): compact(child) for child in kids}
    return node.get("value")


def resource_id(value):
    return value if isinstance(value, str) and value != NULL_RESOURCE else None


def ngx_succeeded(value):
    # NVSDK_NGX_SUCCEED: anything outside the 0xBAD00000 failure range.
    return (value & 0xFFF00000) != 0xBAD00000 if isinstance(value, int) else None


class FrameGenerationService:
    def __init__(self, ctx, invoke_fn):
        self.ctx, self._invoke = ctx, invoke_fn

    def _run(self, identity, work):
        result = dict(operation="get_dlssg_events", revision=FRAME_GENERATION_REVISION,
                      supported=True, complete=False, diagnostics=[], **identity)
        result["complete_scope"] = ("recorded RenderDoc-fork DLSS-G chunks and copy/present action "
                                    "metadata in this capture; no replay, no pixel data")
        if not self.ctx.IsCaptureLoaded():
            result["supported"] = False
            cs.diagnostic(result, "no_capture", "capture", "No capture loaded")
        else:
            outcome = {}

            def callback(controller):
                budget = Budget(result)
                try:
                    work(controller, result, budget)
                    result["traversal"] = budget.summary()
                except Exception as exc:
                    outcome["error"] = exc
                else:
                    outcome["finished"] = True
            self._invoke(callback)
            if "error" in outcome:
                raise outcome["error"]
            if not outcome.get("finished"):
                raise RuntimeError("Replay callback did not complete")
        result["complete"] = not result.get("diagnostic_count")
        result["status"] = "complete" if result["complete"] else "incomplete"
        return result

    def get_dlssg_events(self, max_evaluates=64):
        cs.integer(max_evaluates, "max_evaluates", 1, 256)

        def work(controller, result, budget):
            result.update(evaluate_count=0, evaluates=[], saved_output_chunk_count=0, presents=[])
            records, complete = cs.scan_actions(controller, result, budget)
            result["action_scan_complete"] = complete
            structured = cs.call(controller, "GetStructuredFile", result, "ReplayController.GetStructuredFile")
            if structured is cs.MISSING:
                return
            chunks = cs.member(structured, "chunks", result, "SDFile.chunks")
            nchunks = cs._length(chunks, result, "SDFile.chunks")
            if nchunks is None:
                return
            names = self._resource_names(controller, result, budget)
            reader = cs.SDReader(result, budget)
            copy, present = flag("Copy", COPY_FLAG), flag("Present", PRESENT_FLAG)
            last_copy = {}  # destination ResourceId -> (event_id, source ResourceId)
            evaluates = result["evaluates"]
            records.sort(key=lambda row: -1 if row[0] is None else row[0])
            for eid, node, parents in records:
                if not budget.check("actions"):
                    break
                flags = cs.member(node, "flags", result, "action.flags")
                if flags is cs.MISSING:
                    break
                if flags & copy:
                    source = str(cs.member(node, "copySource", result, "action.copySource"))
                    destination = str(cs.member(node, "copyDestination", result, "action.copyDestination"))
                    if self._is_evaluate(node, chunks, nchunks, result, path="action[%s]" % eid):
                        result["evaluate_count"] += 1
                        if len(evaluates) < max_evaluates:
                            evaluates.append(self._evaluate(eid, node, parents, structured, chunks,
                                                            names, reader, result))
                    if resource_id(destination):
                        last_copy[destination] = (eid, resource_id(source))
                if flags & present:
                    result["presents"].append(self._present(eid, node, structured, names,
                                                            last_copy, evaluates, result))
            if result["evaluate_count"] > len(evaluates):
                cs.diagnostic(result, "evaluate_output_limit", "evaluates",
                              "More DLSS-G evaluates exist than max_evaluates; only the first are decoded")
            if evaluates:
                self._attach_saved_outputs(chunks, nchunks, names, evaluates, result, budget)
            if not result["evaluate_count"]:
                result["note"] = ("No %s chunk in this capture. The fork records an evaluate only "
                                  "into command lists submitted inside the captured present interval; "
                                  "with DLSS-G that interval can hold just the pacer's output copy."
                                  % EVALUATE_CHUNK)

        return self._run(dict(max_evaluates=max_evaluates), work)

    @staticmethod
    def _resource_names(controller, result, budget):
        resources = cs.call(controller, "GetResources", result, "ReplayController.GetResources")
        n = cs._length(resources, result, "resources")
        names = {}
        if n is None:
            return names
        if n > cs.MAX_RESOURCES:
            budget.stop("resources_limit", "resources")
            return names
        for i in range(n):
            if not budget.take("resources", cs.MAX_RESOURCES, "resources"):
                break
            obj = resources[i]
            key = str(cs.member(obj, "resourceId", result, "ResourceDescription.resourceId"))
            names[key] = cs.bounded_text(cs.member(obj, "name", result, "ResourceDescription.name"),
                                         result, "ResourceDescription.name")
        return names

    @staticmethod
    def _is_evaluate(node, chunks, nchunks, result, path):
        """The action is the fork's evaluate iff its own last API event is that chunk."""
        custom = getattr(node, "customName", None)
        if isinstance(custom, str) and not custom.startswith(EVALUATE_NAME_PREFIX):
            return False  # ordinary copies never read their event arrays
        events = cs.member(node, "events", result, path + ".events")
        n = cs._length(events, result, path + ".events")
        if not n:
            return False
        ci = cs.native_int(cs.member(events[n - 1], "chunkIndex", result, path + ".chunkIndex"),
                           result, path + ".chunkIndex", high=0xffffffff)
        if ci is None or ci >= nchunks:
            return False
        matched = getattr(chunks[ci], "name", None) == EVALUATE_CHUNK
        if not matched and isinstance(custom, str):
            cs.diagnostic(result, "evaluate_chunk_mismatch", path,
                          "Action is named like a DLSS-G evaluate but its last event is another chunk")
        return matched

    @staticmethod
    def _evaluate(eid, node, parents, structured, chunks, names, reader, result):
        events = node.events
        ci = events[len(events) - 1].chunkIndex
        fields = compact(reader.read(chunks[ci], "chunk[%d]" % ci)) or {}
        params = fields.get("Parameters") if isinstance(fields.get("Parameters"), dict) else {}
        ngx = params.get("ngxResult")
        reset = params.get("reset")
        resources = {}
        for role in ROLES:
            rid = resource_id(fields.get(role))
            if rid:
                resources[role] = dict(resource_id=rid, name=names.get(rid))
        return dict(
            event_id=eid, chunk_index=ci,
            name=cs.bounded_text(cs.call(node, "GetName", result, "ActionDescription.GetName", structured),
                                 result, "action.name"),
            parent_path=[cs.bounded_text(cs.call(parent, "GetName", result, "ActionDescription.GetName",
                                                 structured), result, "parent.name")
                         for _, parent in parents],
            command_list=fields.get("pCommandList"), eval_seq=fields.get("evalSeq"),
            ngx_result=("0x%08x" % ngx) if isinstance(ngx, int) else None,
            succeeded=ngx_succeeded(ngx),
            multi_frame_index=params.get("multiFrameIndex"),
            multi_frame_count=params.get("multiFrameCount"),
            reset=bool(reset) if isinstance(reset, int) else None,
            backbuffer_frame_id=params.get("backbufferFrameID"),
            resources=resources, parameters=params,
            saved_outputs=None, saved_outputs_chunk_index=None)

    @staticmethod
    def _present(eid, node, structured, names, last_copy, evaluates, result):
        image = resource_id(str(cs.member(node, "copyDestination", result, "action.copyDestination")))
        out = dict(event_id=eid,
                   name=cs.bounded_text(cs.call(node, "GetName", result, "ActionDescription.GetName", structured),
                                        result, "action.name"),
                   image=image, image_name=names.get(image) if image else None,
                   last_copy=None, dlssg_role=None, dlssg_evaluate_event_id=None)
        if image in last_copy:
            copy_eid, source = last_copy[image]
            out["last_copy"] = dict(event_id=copy_eid, source=source,
                                    source_name=names.get(source) if source else None)
            # Clones swap roles every evaluate: classify by the latest earlier evaluate.
            for evaluate in reversed(evaluates):
                if evaluate["event_id"] >= copy_eid:
                    continue
                role = next((r for r, v in evaluate["resources"].items() if v["resource_id"] == source), None)
                if role:
                    out["dlssg_role"], out["dlssg_evaluate_event_id"] = role, evaluate["event_id"]
                    break
        return out

    @staticmethod
    def _attach_saved_outputs(chunks, nchunks, names, evaluates, result, budget):
        if nchunks > MAX_STRUCTURED_CHUNKS:
            budget.stop("structured_chunks_limit", "SDFile.chunks")
            return
        by_seq = {e["eval_seq"]: e for e in evaluates if isinstance(e["eval_seq"], int)}
        for i in range(nchunks):
            if i % 1024 == 0 and not budget.check("SDFile.chunks"):
                return
            chunk = chunks[i]
            if getattr(chunk, "name", None) != SAVED_OUTPUTS_CHUNK:
                continue
            result["saved_output_chunk_count"] += 1
            saved = FrameGenerationService._read_saved(chunk, "chunk[%d]" % i, result)
            evaluate = by_seq.get(saved["eval_seq"])
            if evaluate is None:
                continue
            roles = {v["resource_id"]: r for r, v in evaluate["resources"].items()}
            for output in saved["outputs"]:
                output["role"] = roles.get(output["resource_id"])
                output["name"] = names.get(output["resource_id"])
                output["saved"] = isinstance(output.get("bytes"), int) and output["bytes"] > 0
            evaluate["saved_outputs"], evaluate["saved_outputs_chunk_index"] = saved["outputs"], i
        for evaluate in evaluates:
            if evaluate["saved_outputs"] is None:
                evaluate["saved_outputs_note"] = ("No saved-outputs chunk: replay pastes nothing, "
                                                  "the outputs keep stale contents")

    @staticmethod
    def _read_saved(chunk, path, result):
        """Sequential reader: per-output member names repeat, Contents is never read."""
        out = dict(eval_seq=None, output_count=None, outputs=[])
        n = cs.native_int(cs.call(chunk, "NumChildren", result, path + ".NumChildren"), result, path)
        if n is None:
            return out
        if n > MAX_SAVED_CHILDREN:
            cs.diagnostic(result, "sd_child_limit", path, "Only a bounded prefix of saved outputs is returned")
        enums = getattr(rd, "SDBasic", None)
        integers = tuple(getattr(enums, k) for k in ("UnsignedInteger", "SignedInteger")
                         if getattr(enums, k, cs.MISSING) is not cs.MISSING)
        resource = getattr(enums, "Resource", cs.MISSING)
        current = None
        for k in range(min(n, MAX_SAVED_CHILDREN)):
            child = cs.call(chunk, "GetChild", result, path + ".GetChild", k)
            if child is cs.MISSING:
                break
            name = getattr(child, "name", None)
            basic = getattr(getattr(child, "type", None), "basetype", None)
            if name == "Resource" and basic == resource:
                current = dict(resource_id=resource_id(str(child.AsResourceId())))
                out["outputs"].append(current)
            elif basic in integers and (name in ("evalSeq", "NumOutputs") or name in OUTPUT_FIELDS):
                value = cs.native_int(child.AsInt(), result, path + "." + name)
                if name == "evalSeq":
                    out["eval_seq"] = value
                elif name == "NumOutputs":
                    out["output_count"] = value
                elif current is not None:
                    current[OUTPUT_FIELDS[name]] = value
        return out
