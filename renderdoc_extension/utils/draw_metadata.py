"""Bounded metadata serialization and linked PSO reads; no UI or resource-data calls."""
import base64
import json
import math
import struct
import time
import zlib

DRAW_METADATA_REVISION = "draw-metadata-3"
MAX_BYTES = 1048576
MAX_STRING_BYTES = 4096
MAX_SIGNATURES = 128
MAX_VIEWPORTS = 16
MAX_SCISSORS = 16
MAX_TARGETS = 64
MAX_JSON_BYTES = 4 * 1048576
MAX_DIAGNOSTICS = 128
MAX_VISITED_NODES = 32768
TIME_BUDGET_MS = 250
MISSING = object()


def integer(value, name, minimum=1, maximum=0xffffffff):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("%s must be an integer in [%d, %d]" % (name, minimum, maximum))
    return value


def shader_arguments(event_id, stage, max_bytes, disassembly_target, max_text_bytes):
    integer(event_id, "event_id")
    if not isinstance(stage, str) or stage not in ("vertex", "pixel"):
        raise ValueError("stage must be vertex or pixel")
    integer(max_bytes, "max_bytes", 1, MAX_BYTES)
    integer(max_text_bytes, "max_text_bytes", 1, MAX_BYTES)
    if disassembly_target is not None:
        if (not isinstance(disassembly_target, str) or not disassembly_target or
                len(disassembly_target) > MAX_STRING_BYTES or
                len(disassembly_target.encode("utf-8")) > MAX_STRING_BYTES):
            raise ValueError("disassembly_target must be a nonempty bounded target string or null")


def event_arguments(event_id_min, event_id_max, max_events):
    integer(event_id_min, "event_id_min")
    integer(event_id_max, "event_id_max")
    integer(max_events, "max_events", 1, 128)
    if event_id_max < event_id_min or event_id_max - event_id_min + 1 > 256:
        raise ValueError("event range must be inclusive, ordered, and span at most 256 IDs")


def result_base(operation, **identity):
    result = {"operation": operation, "revision": DRAW_METADATA_REVISION,
              "supported": True, "complete": False, "diagnostics": []}
    result.update(identity)
    return result


def diagnostic(result, code, path, message):
    result["complete"] = False
    result["diagnostic_count"] = result.get("diagnostic_count", 0) + 1
    if len(result["diagnostics"]) < MAX_DIAGNOSTICS:
        result["diagnostics"].append({"code": code, "path": path, "message": message})
    else:
        result["diagnostics_truncated"] = True


def member(obj, name, result, path):
    # Only absent attributes are interpreted as missing. Native/API exceptions
    # propagate to the service's single abort boundary, never to a fallback.
    value = getattr(obj, name, MISSING) if obj is not None else MISSING
    if value is MISSING:
        diagnostic(result, "missing_field", path, "Runtime does not expose this member")
    return value


def text(value, result, path, limit=MAX_STRING_BYTES):
    if value is MISSING:
        return None
    if not isinstance(value, str):
        diagnostic(result, "invalid_field_type", path, "Expected a string")
        return None
    # Check character count before making the bounded UTF-8 copy.
    if len(value) > limit or len(value.encode("utf-8")) > limit:
        diagnostic(result, "string_limit", path, "String omitted: UTF-8 byte limit exceeded")
        return None
    return value


def scalar(value, kind, result, path, bits=None, key=None):
    if value is MISSING:
        return None
    if kind in ("enum", "resource"):
        return text(str(value), result, path)
    if kind == "text":
        return text(value, result, path)
    if kind == "bool":
        valid = isinstance(value, bool)
    elif kind == "int":
        valid = isinstance(value, int) and not isinstance(value, bool) and -(1 << 63) <= value < (1 << 64)
    else:
        valid = (isinstance(value, (int, float)) and not isinstance(value, bool) and
                 (isinstance(value, float) or -(1 << 63) <= value < (1 << 64)))
    if not valid:
        diagnostic(result, "invalid_field_type", path, "Unexpected native scalar type or range")
        return None
    if isinstance(value, float):
        if bits is not None:
            bits[key] = "0x" + struct.pack(">d", value).hex()
        if not math.isfinite(value):
            return "NaN" if math.isnan(value) else ("+Infinity" if value > 0 else "-Infinity")
    return value


def fields(obj, spec, result, path):
    values, bits = {}, {}
    for name, kind in spec:
        values[name] = scalar(member(obj, name, result, path + "." + name), kind,
                              result, path + "." + name, bits, name)
    if bits:
        values["float_bits_binary64"] = bits
    return values


def sequence(value, limit, result, path):
    if value is MISSING or value is None:
        if value is None:
            diagnostic(result, "missing_data", path, "Native sequence is null")
        return 0
    count = len(value)
    if count > limit:
        diagnostic(result, "item_limit", path, "Sequence exceeds the returned-item limit")
    return min(count, limit)


def rasterizer(state, result):
    raster = member(state, "rasterizer", result, "rasterizer")
    native = member(raster, "state", result, "rasterizer.state")
    spec = (("fillMode", "enum"), ("cullMode", "enum"), ("frontCCW", "bool"),
            ("depthBias", "number"), ("depthBiasClamp", "number"),
            ("slopeScaledDepthBias", "number"), ("depthClip", "bool"),
            ("lineRasterMode", "enum"), ("forcedSampleCount", "int"),
            ("conservativeRasterization", "enum"), ("shadingRateImage", "resource"))
    data = fields(native, spec, result, "rasterizer.state")
    for name, kind in (("baseShadingRate", "int"), ("shadingRateCombiners", "enum")):
        path = "rasterizer.state." + name
        values = member(native, name, result, path)
        count = sequence(values, 2, result, path)
        data[name] = [scalar(values[i], kind, result, path + "[%d]" % i) for i in range(count)]
        if count != 2 and values is not MISSING:
            diagnostic(result, "unexpected_count", path, "Expected the official two-element array")
    snapshot = {"state": data, "sampleMask": scalar(
        member(raster, "sampleMask", result, "rasterizer.sampleMask"), "int",
        result, "rasterizer.sampleMask")}
    for name, limit, spec in (
            ("viewports", MAX_VIEWPORTS, (("x", "number"), ("y", "number"),
                ("width", "number"), ("height", "number"), ("minDepth", "number"), ("maxDepth", "number"))),
            ("scissors", MAX_SCISSORS, (("x", "int"), ("y", "int"),
                ("width", "int"), ("height", "int"), ("enabled", "bool")))):
        path = "rasterizer." + name
        items = member(raster, name, result, path)
        snapshot[name] = [fields(items[i], spec, result, path + "[%d]" % i)
                          for i in range(sequence(items, limit, result, path))]
    result["rasterizer"] = snapshot
    result["float_bits_semantics"] = "IEEE754 binary64 of exposed Python values; not a native-memory snapshot"


# Upstream d3d12_stringise.cpp names (NOT the C++ method names). Both
# Serialise_CreateGraphicsPipelineState and Serialise_CreatePipelineState name
# the descriptor pDesc; d3d12_serialise.cpp / dxgi_common.cpp serialize the
# direct pDesc.SampleDesc.Count/Quality members. Never recursively find a name.
PSO_CREATION_CHUNKS = ("ID3D12Device::CreateGraphicsPipeline",
                       "ID3D12Device2::CreatePipelineState")
MAX_SAMPLING_RESOURCES = 32768
MAX_SAMPLING_CHUNKS = 64
MAX_SAMPLING_CHILDREN = 128
MAX_SAMPLING_NODES = 4096
MAX_SAMPLING_DEPTH = 3  # chunk=0, pDesc=1, SampleDesc=2, Count/Quality=3
SAMPLING_TIME_BUDGET_MS = 250


def sampling_base():
    return {"complete": False, "source": "D3D12 PSO initialisation chunks",
            "pipeline_resource_id": None, "sample_count": None, "sample_quality": None,
            "provenance": [], "traversal": {"attempted": False,
                                           "native_calls_hard_cancellable": False}}


class SamplingReader:
    """Bounded linked metadata only; MUST run wholly inside the replay callback.

    No native exception is caught here. A budget can stop between native calls,
    never interrupt GetResources/GetStructuredFile or a single SDObject getter.
    """
    def __init__(self, rd, result):
        self.rd, self.result = rd, result
        self.output = result["sampling"]
        self.started = time.monotonic()
        self.reason = None
        self.counts = {"resources_visited": 0, "chunk_indices_visited": 0,
                       "chunks_visited": 0, "children_visited": 0, "native_calls": 0}
        self.depth = 0
        self.resources_complete = False
        self.chunks_complete = False

    def fail(self, code, path, message):
        diagnostic(self.result, code, path, message)
        return MISSING

    def stop(self, code, path):
        if self.reason is None:
            self.reason = code
            self.fail(code, path, "PSO sampling traversal budget exhausted; values withheld")
        return False

    def check(self, path, depth=0):
        if self.reason:
            return False
        if (time.monotonic() - self.started) * 1000 >= SAMPLING_TIME_BUDGET_MS:
            return self.stop("sampling_time_limit", path)
        if depth > MAX_SAMPLING_DEPTH:
            return self.stop("sampling_depth_limit", path)
        self.depth = max(self.depth, depth)
        return True

    def take(self, key, limit, path, depth=0):
        if not self.check(path, depth):
            return False
        if self.counts[key] >= limit:
            return self.stop("sampling_node_limit", path)
        self.counts[key] += 1
        return True

    def get(self, obj, name, path):
        if not self.check(path):
            return MISSING
        value = member(obj, name, self.result, path)
        return value if self.check(path) else MISSING

    def call(self, obj, name, path, *args):
        method = self.get(obj, name, path)
        if method is MISSING:
            if not self.reason:
                self.result["supported"] = False
            return MISSING
        if not callable(method):
            self.result["supported"] = False
            return self.fail("missing_api", path, "Required runtime API is not callable")
        if not self.check(path):
            return MISSING
        self.counts["native_calls"] += 1
        value = method(*args)
        return value if self.check(path) else MISSING

    def length(self, values, limit, path):
        if values is MISSING or not self.check(path):
            return None
        if values is None:
            self.fail("missing_data", path, "Native metadata sequence is null")
            return None
        size = len(values)
        if not self.check(path):
            return None
        if size > limit:
            self.stop("sampling_item_limit", path)
            return None
        return size

    def uint(self, value, minimum, path):
        if value is MISSING:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 0xffffffff:
            self.fail("invalid_uint32", path, "Expected uint32 (Count must be positive); no coercion")
            return None
        return value

    def typed(self, obj, expected, path):
        enum = self.get(getattr(self.rd, "SDBasic", None), expected, "sampling.SDBasic." + expected)
        if enum is MISSING:
            self.result["supported"] = False
            return False
        native_type = self.get(obj, "type", path + ".type")
        basic = self.get(native_type, "basetype", path + ".type.basetype")
        if basic is MISSING:
            return False
        if basic != enum:
            self.fail("unexpected_sd_type", path + ".type.basetype", "Expected SDBasic." + expected)
            return False
        return True

    def children(self, obj, expected, path, depth, kind="Struct", exact=False):
        if not self.check(path, depth) or not self.typed(obj, kind, path):
            return None
        size = self.uint(self.call(obj, "NumChildren", path + ".NumChildren"), 0, path)
        if size is None:
            return None
        if size > MAX_SAMPLING_CHILDREN:
            self.stop("sampling_child_limit", path)
            return None
        seen, found = set(), {}
        for i in range(size):
            if not self.take("children_visited", MAX_SAMPLING_NODES, path, depth + 1):
                return None
            child = self.call(obj, "GetChild", path + ".GetChild", i)
            if child is MISSING:
                return None
            name = text(self.get(child, "name", path + "[%d].name" % i), self.result, path + "[%d].name" % i)
            if name is None:
                return None
            if name in seen:
                self.fail("duplicate_sd_field", path, "Duplicate direct child name; no first-match selection")
                return None
            seen.add(name)
            if name in expected:
                found[name] = child
            elif exact:
                self.fail("unknown_sd_field", path, "Unexpected member in SampleDesc")
                return None
        if any(name not in found for name in expected):
            self.fail("missing_sd_field", path, "Required direct child missing: " + ", ".join(name for name in expected if name not in found))
            return None
        return found

    def leaf(self, obj, kind, path, depth):
        if not self.check(path, depth) or not self.typed(obj, kind, path):
            return MISSING
        size = self.call(obj, "NumChildren", path + ".NumChildren")
        if size is MISSING:
            return MISSING
        if isinstance(size, bool) or not isinstance(size, int) or size != 0:
            return self.fail("unexpected_sd_children", path, "Scalar metadata must have no children")
        if kind == "UnsignedInteger":
            native_type = self.get(obj, "type", path + ".type")
            width = self.get(native_type, "byteSize", path + ".type.byteSize")
            if isinstance(width, bool) or not isinstance(width, int) or width != 4:
                return self.fail("unexpected_sd_width", path, "SampleDesc UINT must have byteSize=4")
        return self.call(obj, "AsResourceId" if kind == "Resource" else "AsInt", path)

    def read(self, controller, state):
        before = self.result.get("diagnostic_count", 0)
        values = self._read(controller, state)
        if (values is not None and self.check("sampling") and
                self.result.get("diagnostic_count", 0) == before):
            self.output.update(complete=True, sample_count=values[0], sample_quality=values[1])
        self.output["traversal"] = dict(self.counts, attempted=True,
            resource_scan_complete=self.resources_complete, chunk_scan_complete=self.chunks_complete,
            max_resources=MAX_SAMPLING_RESOURCES, max_initialisation_chunks=MAX_SAMPLING_CHUNKS,
            max_children_per_object=MAX_SAMPLING_CHILDREN, max_child_nodes=MAX_SAMPLING_NODES,
            max_depth=MAX_SAMPLING_DEPTH, deepest_visited=self.depth,
            time_budget_ms=SAMPLING_TIME_BUDGET_MS,
            elapsed_ms=(time.monotonic() - self.started) * 1000,
            exhausted=self.reason is not None, stop_reason=self.reason,
            native_calls_hard_cancellable=False, global_chunk_scan_performed=False)

    def _read(self, controller, state):
        pso = self.get(state, "pipelineResourceId", "sampling.pipelineResourceId")
        null = self.call(getattr(self.rd, "ResourceId", None), "Null", "sampling.ResourceId.Null")
        if pso is MISSING or null is MISSING:
            return None
        if not isinstance(pso, type(null)) or pso == null:
            self.fail("invalid_pipeline_resource", "sampling.pipelineResourceId", "Selected PSO must be a non-null ResourceId")
            return None
        self.output["pipeline_resource_id"] = scalar(pso, "resource", self.result, "sampling.pipeline_resource_id")
        if self.output["pipeline_resource_id"] is None:
            return None
        resources = self.call(controller, "GetResources", "sampling.ReplayController.GetResources")
        count = self.length(resources, MAX_SAMPLING_RESOURCES, "sampling.resources")
        if count is None:
            return None
        match, match_index = None, None
        for i in range(count):
            if not self.take("resources_visited", MAX_SAMPLING_RESOURCES, "sampling.resources"):
                return None
            resource = resources[i]
            rid = self.get(resource, "resourceId", "sampling.resources[%d].resourceId" % i)
            if rid is MISSING:
                return None
            if not isinstance(rid, type(null)):
                self.fail("invalid_resource_id", "sampling.resources[%d].resourceId" % i, "Expected ResourceId")
                return None
            if rid == pso:
                if match is not None:
                    self.fail("duplicate_resource", "sampling.resources", "Multiple descriptions identify the selected PSO")
                    return None
                match, match_index = resource, i
        self.resources_complete = True
        if match is None:
            self.fail("pipeline_resource_missing", "sampling.resources", "No ResourceDescription exactly matches pipelineResourceId")
            return None
        self.output["resource_description_index"] = match_index
        indices = self.get(match, "initialisationChunks", "sampling.initialisationChunks")
        count = self.length(indices, MAX_SAMPLING_CHUNKS, "sampling.initialisationChunks")
        if count is None:
            return None
        if not count:
            self.fail("missing_initialisation_chunks", "sampling.initialisationChunks", "No PSO creation provenance is available")
            return None
        structured = self.call(controller, "GetStructuredFile", "sampling.ReplayController.GetStructuredFile")
        chunks = self.get(structured, "chunks", "sampling.SDFile.chunks")
        # Only length + linked index lookups; never enumerate the global SDFile.
        total = self.length(chunks, 0xffffffff, "sampling.SDFile.chunks")
        if total is None:
            return None
        seen, agreed = set(), None
        for i in range(count):
            path = "sampling.initialisationChunks[%d]" % i
            if not self.take("chunk_indices_visited", MAX_SAMPLING_CHUNKS, path):
                return None
            index = self.uint(indices[i], 0, path)
            if index is None:
                return None
            if index in seen or index >= total:
                self.fail("duplicate_chunk_index" if index in seen else "chunk_out_of_range", path, "PSO initialisation chunk index must be unique and in range")
                return None
            seen.add(index)
            path = "sampling.chunks[%d]" % index
            if not self.take("chunks_visited", MAX_SAMPLING_CHUNKS, path):
                return None
            chunk = chunks[index]
            name = text(self.get(chunk, "name", path + ".name"), self.result, path + ".name")
            if name not in PSO_CREATION_CHUNKS:
                self.fail("unknown_pso_chunk", path, "Unrecognized PSO initialisation chunk; no nested SampleDesc search")
                return None
            root = self.children(chunk, ("pDesc", "pPipelineState"), path, 0, "Chunk")
            if root is None:
                return None
            linked = self.leaf(root["pPipelineState"], "Resource", path + ".pPipelineState", 1)
            if linked is MISSING:
                return None
            if not isinstance(linked, type(null)) or linked != pso:
                self.fail("pipeline_chunk_mismatch", path + ".pPipelineState", "Creation chunk does not identify the selected PSO")
                return None
            desc = self.children(root["pDesc"], ("SampleDesc",), path + ".pDesc", 1)
            if desc is None:
                return None
            sample_path = path + ".pDesc.SampleDesc"
            sample = self.children(desc["SampleDesc"], ("Count", "Quality"), sample_path, 2, exact=True)
            if sample is None:
                return None
            count_value = self.uint(self.leaf(sample["Count"], "UnsignedInteger", sample_path + ".Count", 3), 1, sample_path + ".Count")
            if count_value is None:
                return None
            quality = self.uint(self.leaf(sample["Quality"], "UnsignedInteger", sample_path + ".Quality", 3), 0, sample_path + ".Quality")
            if quality is None:
                return None
            values = (count_value, quality)
            self.output["provenance"].append({"chunk_index": index, "chunk_name": name,
                "sample_desc_path": "pDesc.SampleDesc", "count_type": "UnsignedInteger",
                "quality_type": "UnsignedInteger", "pipeline_resource_path": "pPipelineState",
                "pipeline_resource_id": self.output["pipeline_resource_id"]})
            if agreed is not None and values != agreed:
                self.fail("conflicting_sample_desc", sample_path, "Linked PSO creation chunks disagree; both values withheld")
                return None
            agreed = values
        self.chunks_complete = True
        return agreed


def raw_shader(raw, max_bytes, result):
    item = {"available": raw is not MISSING and raw is not None, "complete": False,
            "byte_budget": max_bytes, "required_byte_length": None, "data_length": 0}
    if not item["available"]:
        if raw is None:
            diagnostic(result, "missing_data", "rawBytes", "Shader raw bytes are unavailable")
        return item
    size = len(raw)  # MUST precede bytes(), slicing, hashing and base64.
    item["required_byte_length"] = size
    if size == 0 or size > max_bytes:
        diagnostic(result, "empty_raw" if size == 0 else "raw_limit", "rawBytes",
                   "Raw bytes omitted: empty or larger than the requested byte budget")
        return item
    data = bytes(raw)
    if len(data) != size or len(data) > max_bytes:
        diagnostic(result, "raw_length_mismatch", "rawBytes", "Raw length changed during bounded copy; data omitted")
        return item
    item.update(complete=True, data_length=len(data),
                crc32="0x%08X" % (zlib.crc32(data) & 0xffffffff),
                content_base64=base64.b64encode(data).decode("ascii"))
    return item


def signatures(reflection, name, result):
    spec = (("varName", "text"), ("semanticName", "text"), ("semanticIdxName", "text"),
            ("semanticIndex", "int"), ("perPrimitiveRate", "bool"), ("regIndex", "int"),
            ("systemValue", "enum"), ("varType", "enum"), ("regChannelMask", "int"),
            ("channelUsedMask", "int"), ("needSemanticIndex", "bool"), ("compCount", "int"),
            ("stream", "int"))
    values = member(reflection, name, result, name)
    return [fields(values[i], spec, result, name + "[%d]" % i)
            for i in range(sequence(values, MAX_SIGNATURES, result, name))]


class TraversalBudget:
    """Cooperative Python traversal budget, not a native-call timeout."""
    def __init__(self):
        self.visited = 0
        self.actions = 0
        self.events = 0
        self.started = time.monotonic()
        self.reason = None
        self.metadata_complete = True

    def take(self, kind):
        if self.visited >= MAX_VISITED_NODES:
            self.reason = "visited_node_limit"
        elif (time.monotonic() - self.started) * 1000 >= TIME_BUDGET_MS:
            self.reason = "traversal_time_limit"
        if self.reason:
            return False
        self.visited += 1
        if kind == "action":
            self.actions += 1
        else:
            self.events += 1
        return True

    def summary(self):
        return {"visited_nodes": self.visited, "actions_visited": self.actions,
                "events_visited": self.events, "max_visited_nodes": MAX_VISITED_NODES,
                "time_budget_ms": TIME_BUDGET_MS, "exhausted": self.reason is not None,
                "metadata_complete": self.metadata_complete,
                "stop_reason": self.reason, "native_calls_hard_cancellable": False}


def walk(roots, budget, result):
    """Iterative DFS; reserve budget BEFORE dereferencing the next array item."""
    if roots is MISSING or roots is None:
        budget.metadata_complete = False
        return
    # Each frame holds the native sequence, not a copied/flattened subtree.
    stack = [[roots, 0, len(roots), "action", None]]
    while stack:
        frame = stack[-1]
        if frame[1] == frame[2]:
            stack.pop()
            continue
        if not budget.take(frame[3]):
            diagnostic(result, budget.reason, "actions.events", "Traversal stopped; completeness and reliable pagination are unavailable")
            return
        node = frame[0][frame[1]]
        frame[1] += 1
        kind, owner = frame[3], frame[4]
        yield kind, node, owner
        if kind == "action":
            children = member(node, "children", result, "action.children")
            events = member(node, "events", result, "action.events")
            if children is not MISSING and children is not None:
                stack.append([children, 0, len(children), "action", None])
            else:
                budget.metadata_complete = False
            if events is not MISSING and events is not None:
                stack.append([events, 0, len(events), "event", node])
            else:
                budget.metadata_complete = False
            if children is None or events is None:
                diagnostic(result, "missing_data", "action", "Null children or events sequence")


def native_event_id(node, result, path):
    value = member(node, "eventId", result, path)
    if value is MISSING:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xffffffff:
        diagnostic(result, "invalid_event_id", path, "Native event ID is not a uint32")
        return None
    return value


def event_record(event, owner, chunks, result):
    record = {"event_id": native_event_id(event, result, "APIEvent.eventId"),
              "owner_action": {"event_id": native_event_id(owner, result, "ActionDescription.eventId")}}
    offset = member(event, "fileOffset", result, "APIEvent.fileOffset")
    record["file_offset"] = scalar(offset, "int", result, "APIEvent.fileOffset")
    index = member(event, "chunkIndex", result, "APIEvent.chunkIndex")
    chunk = {"index": None, "name": None, "status": "unavailable"}
    record["chunk"] = chunk
    if index is MISSING:
        return record
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 0xffffffff:
        diagnostic(result, "invalid_chunk_index", "APIEvent.chunkIndex", "Chunk index is not a uint32")
        return record
    chunk["index"] = index
    # APIEvent's documented uint32 NoChunk sentinel, not a byte offset.
    if index == 0xffffffff:
        chunk["status"] = "no_chunk"
        diagnostic(result, "no_chunk", "APIEvent.chunkIndex", "APIEvent uses NoChunk; no structured chunk is available")
    elif chunks is MISSING or chunks is None:
        diagnostic(result, "missing_chunks", "SDFile.chunks", "Structured chunks unavailable")
    elif index >= len(chunks):
        diagnostic(result, "chunk_out_of_range", "APIEvent.chunkIndex", "Chunk index exceeds SDFile.chunks")
    else:
        name = member(chunks[index], "name", result, "SDChunk.name")
        chunk["name"] = text(name, result, "SDChunk.name")
        if chunk["name"] is not None:
            chunk["status"] = "available"
    return record


def finish(result):
    if "sampling" in result and not result["sampling"]["complete"]:
        diagnostic(result, "sampling_incomplete", "sampling", "Requested PSO sampling metadata is unproven; no defaults or texture substitution")
    result["complete"] = not result["diagnostics"] and not result.get("diagnostic_count", 0)
    result["status"] = "complete" if result["complete"] else "incomplete"
    result["limits"] = {"raw_bytes": MAX_BYTES, "text_bytes": MAX_BYTES,
                        "string_bytes": MAX_STRING_BYTES, "signatures_per_direction": MAX_SIGNATURES,
                        "viewports": MAX_VIEWPORTS, "scissors": MAX_SCISSORS,
                        "disassembly_targets": MAX_TARGETS, "json_bytes": MAX_JSON_BYTES}
    # Count the actual ASCII JSON representation without joining another large
    # output string. Inputs have already been bounded before serialization.
    size = 0
    for part in json.JSONEncoder(ensure_ascii=True, allow_nan=False).iterencode(result):
        size += len(part)
        if size > MAX_JSON_BYTES:
            identity = {key: result[key] for key in ("event_id", "stage", "event_id_min", "event_id_max") if key in result}
            omitted = result_base(result["operation"], **identity)
            omitted.update(supported=result["supported"], status="incomplete", output_omitted=True)
            diagnostic(omitted, "json_limit", "result", "Payload omitted: total JSON output limit exceeded")
            omitted["json_byte_limit"] = MAX_JSON_BYTES
            return omitted
    return result
