"""Bounded action context and resource provenance; metadata only, no replay seek."""
import json
import math
import re
import time

import renderdoc as rd

CONTEXT_REVISION = "action-context-1"
MAX_ACTIONS = 32768
MAX_ACTION_DEPTH = 128
MAX_RESOURCES = 131072
MAX_EVENTS_VISITED = 2048
MAX_SD_NODES = 8192
MAX_SD_DEPTH = 8
MAX_SD_CHILDREN = 128
MAX_STRING_BYTES = 4096
MAX_JSON_BYTES = 4 * 1048576
TIME_BUDGET_MS = 1000
MISSING = object()


def integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("%s must be an integer in [%d, %d]" % (name, low, high))
    return value


def diagnostic(result, code, path, message):
    result["diagnostic_count"] = result.get("diagnostic_count", 0) + 1
    if len(result["diagnostics"]) < 128:
        result["diagnostics"].append(dict(code=code, path=path, message=message))


def member(obj, name, result, path):
    value = getattr(obj, name, MISSING)
    if value is MISSING:
        diagnostic(result, "missing_field", path, "Runtime does not expose this field")
    return value


def native_int(value, result, path, low=0, high=(1 << 64) - 1):
    if value is MISSING:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        diagnostic(result, "invalid_integer", path, "Native integer type/range is invalid")
        return None
    return value


def bounded_text(value, result, path):
    if value is MISSING:
        return None
    if not isinstance(value, str):
        diagnostic(result, "invalid_string", path, "Expected a string")
        return None
    if len(value) > MAX_STRING_BYTES or len(value.encode("utf-8")) > MAX_STRING_BYTES:
        diagnostic(result, "string_limit", path, "String omitted; UTF-8 byte limit exceeded")
        return None
    return value


def call(obj, name, result, path, *args):
    method = member(obj, name, result, path)
    if method is MISSING or not callable(method):
        result["supported"] = False
        if method is not MISSING:
            diagnostic(result, "missing_api", path, "Runtime API is not callable")
        return MISSING
    return method(*args)


class Budget:
    def __init__(self, result):
        self.result = result
        self.start = time.monotonic()
        self.counts = dict(actions=0, events=0, resources=0, sd_nodes=0)
        self.reason = None

    def stop(self, reason, path):
        if self.reason is None:
            self.reason = reason
            diagnostic(self.result, reason, path, "Metadata traversal stopped at its work budget")
        return False

    def check(self, path):
        if self.reason:
            return False
        if (time.monotonic() - self.start) * 1000 >= TIME_BUDGET_MS:
            return self.stop("time_budget", path)
        return True

    def take(self, kind, limit, path):
        if not self.check(path):
            return False
        if self.counts[kind] >= limit:
            return self.stop(kind + "_limit", path)
        self.counts[kind] += 1
        return True

    def summary(self):
        return dict(self.counts, stop_reason=self.reason,
                    time_budget_ms=TIME_BUDGET_MS, native_calls_hard_cancellable=False)


def _length(value, result, path):
    if value is MISSING or value is None:
        if value is None:
            diagnostic(result, "null_sequence", path, "Native metadata sequence is null")
        return None
    return len(value)


def scan_actions(controller, result, budget, roots=MISSING, roots_supplied=False):
    """Read children and eventId only, never action.events; no EID tree pruning."""
    if not roots_supplied:
        roots = call(controller, "GetRootActions", result, "ReplayController.GetRootActions")
    count = _length(roots, result, "actions")
    if count is None:
        return [], False
    # Cursor frames avoid materializing or reading an over-budget child array.
    stack = [(roots, count, 0, ())]
    records = []
    while stack:
        sequence, count, index, parents = stack.pop()
        if index >= count:
            continue
        if not budget.take("actions", MAX_ACTIONS, "actions"):
            return records, False
        if len(parents) > MAX_ACTION_DEPTH:
            budget.stop("action_depth_limit", "actions.children")
            return records, False
        node = sequence[index]
        stack.append((sequence, count, index + 1, parents))
        eid = native_int(member(node, "eventId", result, "action.eventId"), result,
                         "action.eventId", high=0xffffffff)
        records.append((eid, node, parents))
        children = member(node, "children", result, "action.children")
        nchildren = _length(children, result, "action.children")
        if nchildren is None:
            return records, False
        if nchildren:
            stack.append((children, nchildren, 0, parents + ((eid, node),)))
    return records, not budget.reason and all(row[0] is not None for row in records)


class SDReader:
    """Never touches SDFile.buffers/SDObject.data; opaque Buffer values are omitted."""
    def __init__(self, result, budget):
        self.result, self.budget = result, budget

    def read(self, obj, path, depth=0):
        result = self.result
        if not self.budget.take("sd_nodes", MAX_SD_NODES, path):
            return {"omitted": "work_budget"}
        if depth > MAX_SD_DEPTH:
            diagnostic(result, "sd_depth_limit", path, "Nested SDObject omitted")
            return {"omitted": "depth_limit"}
        name = bounded_text(member(obj, "name", result, path + ".name"), result, path + ".name")
        dtype = member(obj, "type", result, path + ".type")
        basic = member(dtype, "basetype", result, path + ".type.basetype")
        width = native_int(member(dtype, "byteSize", result, path + ".type.byteSize"), result, path + ".type.byteSize")
        kinds = ("Chunk", "Struct", "Array", "Null", "Buffer", "String", "Enum",
                 "UnsignedInteger", "SignedInteger", "Float", "Boolean", "Character", "Resource", "GPUAddress")
        enums = getattr(rd, "SDBasic", None)
        kind = next((k for k in kinds if getattr(enums, k, MISSING) is not MISSING
                     and basic == getattr(enums, k)), None)
        out = dict(name=name, type=kind, byte_size=width)
        type_name = getattr(dtype, "name", MISSING)
        if type_name is not MISSING:
            out["type_name"] = bounded_text(type_name, result, path + ".type.name")
        if kind is None:
            diagnostic(result, "unknown_sd_type", path, "No recognized typed SDObject scalar/container")
            out["omitted"] = "unknown_type"
            return out
        if kind == "Buffer":
            out["omitted"] = "opaque_blob"
            out["content_read"] = False
            return out
        if kind == "Chunk":
            # Optional capture metadata: absent stack capture is not evidence
            # that the game had no CPU call stack. Never resolve symbols here.
            metadata = getattr(obj, "metadata", MISSING)
            addresses = getattr(metadata, "callstack", MISSING)
            if addresses is MISSING or addresses is None:
                out["callstack"] = dict(available=False, count=None, addresses=[], complete=False,
                                        reason="Runtime chunk does not expose callstack metadata")
            else:
                count = len(addresses)
                stack = dict(available=True, count=count, addresses=[], complete=count <= 32)
                out["callstack"] = stack
                if count > 32:
                    diagnostic(result, "callstack_limit", path, "Only first 32 captured addresses returned")
                for i in range(min(count, 32)):
                    if not self.budget.check(path + ".callstack"):
                        stack["complete"] = False
                        break
                    address = native_int(addresses[i], result, path + ".callstack[%d]" % i)
                    stack["addresses"].append("0x%x" % address if address is not None else None)
                    if address is None:
                        stack["complete"] = False
        n = native_int(call(obj, "NumChildren", result, path + ".NumChildren"), result, path + ".NumChildren")
        if n is None:
            return out
        if kind in ("Chunk", "Struct", "Array"):
            out.update(child_count=n, children=[])
            if n > MAX_SD_CHILDREN:
                diagnostic(result, "sd_child_limit", path, "Only a bounded prefix of this container is returned")
            names = set()
            if depth == MAX_SD_DEPTH and n:
                diagnostic(result, "sd_depth_limit", path, "Nested SDObject children omitted before native GetChild")
                out["children_complete"] = False
                return out
            for i in range(min(n, MAX_SD_CHILDREN)):
                if not self.budget.check(path) or self.budget.counts["sd_nodes"] >= MAX_SD_NODES:
                    self.budget.stop("sd_nodes_limit", path)
                    break
                child = call(obj, "GetChild", result, path + ".GetChild", i)
                if child is MISSING:
                    break
                value = self.read(child, path + "[%d]" % i, depth + 1)
                child_name = value.get("name")
                if kind != "Array" and child_name is not None:
                    if child_name in names:
                        diagnostic(result, "duplicate_sd_field", path, "Duplicate child names are preserved without selecting one")
                    names.add(child_name)
                out["children"].append(value)
            out["children_complete"] = len(out["children"]) == n
            return out
        if n != 0:
            diagnostic(result, "scalar_has_children", path, "Typed scalar unexpectedly contains child nodes")
            return out
        if kind == "Null":
            out["value"] = None
        elif kind == "Resource":
            value = call(obj, "AsResourceId", result, path + ".AsResourceId")
            out["value"] = bounded_text(str(value), result, path) if value is not MISSING else None
        elif kind in ("UnsignedInteger", "SignedInteger", "Enum", "GPUAddress"):
            value = call(obj, "AsInt", result, path + ".AsInt")
            # Python's AsInt can expose a signed 64-bit storage interpretation.
            # Do not guess/wrap a negative unsigned/enum/address back to uint64.
            out["value"] = native_int(value, result, path,
                                      low=-(1 << 63) if kind == "SignedInteger" else 0,
                                      high=(1 << 63) - 1 if kind == "SignedInteger" else (1 << 64) - 1)
        elif kind == "Boolean":
            value = call(obj, "AsBool", result, path + ".AsBool")
            if isinstance(value, bool):
                out["value"] = value
            else:
                diagnostic(result, "invalid_boolean", path, "Native bool has invalid type")
        elif kind == "Float":
            value = call(obj, "AsFloat", result, path + ".AsFloat")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                diagnostic(result, "invalid_float", path, "Native float has invalid type")
            else:
                out["value"] = value if math.isfinite(value) else ("NaN" if math.isnan(value) else "+Infinity" if value > 0 else "-Infinity")
        elif kind == "String":
            value = call(obj, "AsString", result, path + ".AsString")
            out["value"] = bounded_text(value, result, path)
        elif kind == "Character":
            # AsString reads separate string storage, not the character union.
            # Avoid raw data access; no verified character getter is assumed.
            out["omitted"] = "unsupported_character_scalar"
            diagnostic(result, "unsupported_character_scalar", path,
                       "Character storage is not interpreted through AsString")
        return out


class ContextService:
    def __init__(self, ctx, invoke_fn):
        self.ctx, self._invoke = ctx, invoke_fn

    def _run(self, operation, identity, work):
        result = dict(operation=operation, revision=CONTEXT_REVISION, supported=True,
                      complete=False, diagnostics=[], **identity)
        result["complete_scope"] = "requested bounded metadata; opaque blobs excluded, no producer semantics or GPU execution proof"
        result["limits"] = dict(actions=MAX_ACTIONS, action_depth=MAX_ACTION_DEPTH,
                                events=MAX_EVENTS_VISITED, resources=MAX_RESOURCES,
                                sd_nodes=MAX_SD_NODES, sd_depth=MAX_SD_DEPTH,
                                sd_children=MAX_SD_CHILDREN, string_bytes=MAX_STRING_BYTES,
                                json_bytes=MAX_JSON_BYTES, time_budget_ms=TIME_BUDGET_MS)
        if not self.ctx.IsCaptureLoaded():
            result["supported"] = False
            diagnostic(result, "no_capture", "capture", "No capture loaded")
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
        size = 0
        for part in json.JSONEncoder(ensure_ascii=True, allow_nan=False).iterencode(result):
            size += len(part)
            if size > MAX_JSON_BYTES:
                omitted = dict(operation=operation, revision=CONTEXT_REVISION,
                               supported=result["supported"], complete=False, status="incomplete",
                               output_omitted=True, diagnostics=[], **identity)
                diagnostic(omitted, "json_limit", "result", "Entire payload omitted at JSON byte limit")
                return omitted
        return result

    def get_action_context(self, event_id, before_actions=8, after_actions=2, max_events=128):
        integer(event_id, "event_id", 1, 0xffffffff)
        integer(before_actions, "before_actions", 0, 32)
        integer(after_actions, "after_actions", 0, 32)
        integer(max_events, "max_events", 1, 512)

        def work(controller, result, budget):
            records, complete = scan_actions(controller, result, budget)
            matches = [row for row in records if row[0] == event_id]
            result.update(selection=dict(event_found=False, action_scan_complete=complete), actions=[])
            if not complete or len(matches) != 1:
                diagnostic(result, "action_unverified" if not complete else "duplicate_action" if matches else "action_not_found",
                           "event_id", "Exactly one action could not be established; no event/chunk data was read")
                return
            result["selection"]["event_found"] = True
            records.sort(key=lambda row: row[0])
            target_index = next(i for i, row in enumerate(records) if row[0] == event_id)
            selected = records[max(0, target_index - before_actions):target_index + after_actions + 1]
            selected_ids = [row[0] for row in selected]
            if len(set(selected_ids)) != len(selected_ids):
                diagnostic(result, "duplicate_neighbor_action", "actions",
                           "Neighboring action EIDs are duplicated; no API-event chain was selected")
                return
            structured = call(controller, "GetStructuredFile", result, "ReplayController.GetStructuredFile")
            chunks = member(structured, "chunks", result, "SDFile.chunks")
            nchunks = _length(chunks, result, "SDFile.chunks")
            if nchunks is None:
                return
            reader, remaining = SDReader(result, budget), max_events
            result["event_selection"] = "Nearest selected actions first; bounded tail of each owning action.events, returned in EID order. Not a complete command-list history."
            result["file_offset_semantics"] = "APIEvent.fileOffset is not an absolute capture byte offset"
            priority = sorted(selected, key=lambda row: (abs(row[0] - event_id), row[0]))
            outputs = {}
            for eid, node, parents in priority:
                if not budget.check("actions.metadata"):
                    break
                name = call(node, "GetName", result, "ActionDescription.GetName", structured)
                out = dict(event_id=eid,
                           action_id=native_int(member(node, "actionId", result, "action.actionId"), result, "action.actionId"),
                           name=bounded_text(name, result, "action.name"),
                           flags=bounded_text(str(member(node, "flags", result, "action.flags")), result, "action.flags"),
                           parent_path=[], events=[])
                for peid, parent in parents:
                    if not budget.check("parent_path"):
                        break
                    out["parent_path"].append(dict(event_id=peid,
                        name=bounded_text(call(parent, "GetName", result, "ActionDescription.GetName", structured), result, "parent.name")))
                events = member(node, "events", result, "action.events")
                n = _length(events, result, "action.events")
                out.update(event_count=n, events_complete=False)
                outputs[eid] = out
                if n is None:
                    continue
                take = min(n, remaining)
                if take < n:
                    diagnostic(result, "event_output_limit", "action[%d].events" % eid,
                               "Only the bounded tail is returned; earlier API events are unobserved")
                for i in range(n - take, n):
                    if not budget.take("events", MAX_EVENTS_VISITED, "action.events"):
                        break
                    event = events[i]
                    event_eid = native_int(member(event, "eventId", result, "APIEvent.eventId"), result, "APIEvent.eventId", high=0xffffffff)
                    ci = native_int(member(event, "chunkIndex", result, "APIEvent.chunkIndex"), result, "APIEvent.chunkIndex", high=0xffffffff)
                    ev = dict(event_id=event_eid, chunk_index=ci,
                              file_offset=native_int(member(event, "fileOffset", result, "APIEvent.fileOffset"), result, "APIEvent.fileOffset"))
                    if ci is None or ci == 0xffffffff or ci >= nchunks:
                        diagnostic(result, "unavailable_chunk", "APIEvent.chunkIndex", "Missing, NoChunk, or out-of-range chunk index")
                        ev["chunk"] = None
                    else:
                        ev["chunk"] = reader.read(chunks[ci], "chunk[%d]" % ci)
                    out["events"].append(ev)
                    remaining -= 1
                out["events_complete"] = len(out["events"]) == n
            result["actions"] = [outputs[eid] for eid in sorted(outputs)]

        return self._run("get_action_context", dict(event_id=event_id,
            before_actions=before_actions, after_actions=after_actions, max_events=max_events), work)

    def get_resource_provenance(self, resource_ids, max_chunks=32):
        if not isinstance(resource_ids, (list, tuple)) or not 1 <= len(resource_ids) <= 16:
            raise ValueError("resource_ids must contain 1 to 16 explicit resource IDs")
        if any(not isinstance(v, str) or not re.fullmatch(r"ResourceId::[1-9][0-9]{0,19}", v)
               or int(v.split("::")[1]) >= 1 << 64 for v in resource_ids):
            raise ValueError("resource_ids must be non-null ResourceId::uint64 strings")
        if len(set(resource_ids)) != len(resource_ids):
            raise ValueError("resource_ids must be unique")
        integer(max_chunks, "max_chunks", 1, 64)

        def work(controller, result, budget):
            resources = call(controller, "GetResources", result, "ReplayController.GetResources")
            n = _length(resources, result, "resources")
            result["resources"] = []
            if n is None:
                return
            if n > MAX_RESOURCES:
                budget.stop("resources_limit", "resources")
                return
            matches = {rid: [] for rid in resource_ids}
            for i in range(n):
                if not budget.take("resources", MAX_RESOURCES, "resources"):
                    return
                obj = resources[i]
                rid = member(obj, "resourceId", result, "ResourceDescription.resourceId")
                if rid is MISSING:
                    return
                key = str(rid)
                if key in matches:
                    matches[key].append(obj)
            if any(len(v) != 1 for v in matches.values()):
                for rid, found in matches.items():
                    if len(found) != 1:
                        diagnostic(result, "resource_identity_unverified", rid, "Resource is missing or duplicated; initialization chunks were not read")
                return
            structured = call(controller, "GetStructuredFile", result, "ReplayController.GetStructuredFile")
            chunks = member(structured, "chunks", result, "SDFile.chunks")
            nchunks = _length(chunks, result, "SDFile.chunks")
            if nchunks is None:
                return
            reader, remaining = SDReader(result, budget), max_chunks
            for rid in resource_ids:
                if not budget.check("resources.metadata"):
                    break
                obj = matches[rid][0]
                out = dict(resource_id=rid,
                    name=bounded_text(member(obj, "name", result, "resource.name"), result, "resource.name"),
                    type=bounded_text(str(member(obj, "type", result, "resource.type")), result, "resource.type"),
                    initialisation_chunks=[])
                for native, key in (("parentResources", "parent_resources"), ("derivedResources", "derived_resources")):
                    values = member(obj, native, result, "resource." + native)
                    size = _length(values, result, "resource." + native)
                    out[key] = []
                    if size is None:
                        continue
                    if size > 128:
                        diagnostic(result, "resource_relation_limit", key, "Only bounded relationship prefix returned")
                    for i in range(min(size, 128)):
                        if not budget.check(key):
                            break
                        out[key].append(bounded_text(str(values[i]), result, key))
                indices = member(obj, "initialisationChunks", result, "resource.initialisationChunks")
                size = _length(indices, result, "resource.initialisationChunks")
                result["resources"].append(out)
                out["initialisation_chunk_count"] = size
                if size is None:
                    continue
                take = min(size, remaining)
                if take < size:
                    diagnostic(result, "chunk_output_limit", rid, "Initialization chunk prefix only; remaining chunks unobserved")
                for i in range(take):
                    if not budget.check("initialisationChunks"):
                        break
                    ci = native_int(indices[i], result, "resource.initialisationChunks[%d]" % i, high=0xffffffff)
                    if ci is None or ci >= nchunks:
                        diagnostic(result, "invalid_chunk_index", rid, "Resource initialization chunk out of range")
                        continue
                    out["initialisation_chunks"].append(dict(chunk_index=ci, chunk=reader.read(chunks[ci], "chunk[%d]" % ci)))
                    remaining -= 1
                out["initialisation_chunks_complete"] = len(out["initialisation_chunks"]) == size

        return self._run("get_resource_provenance", dict(resource_ids=list(resource_ids), max_chunks=max_chunks), work)
