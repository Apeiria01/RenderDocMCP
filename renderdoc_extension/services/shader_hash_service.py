"""Shader hashes from recorded usage, with an explicitly bounded replay fallback.

The shader-object usage index is a local replay-core capability, not an upstream
version guarantee. Positive, stage-matching entries establish its availability.
No GPU event is selected by the indexed path. Completeness describes enumeration
of that recorded index, not proof that every possible shader binding was recorded.
"""

import json
import time
import zlib

import renderdoc as rd


SHADER_HASH_REVISION = "shader-hash-index-1"
MAX_RESOURCES = 65536
MAX_ACTIONS = 262144
MAX_USAGE_ENTRIES = 1048576
MAX_SHADER_BYTES = 1048576
MAX_TOTAL_SHADER_BYTES = 268435456
MAX_ROWS = 262144
MAX_OUTPUT_BYTES = 8388608
MAX_TEXT = 512

_STAGES = (
    ("vertex", "Vertex", "VS_Resource"),
    ("hull", "Hull", "HS_Resource"),
    ("domain", "Domain", "DS_Resource"),
    ("geometry", "Geometry", "GS_Resource"),
    ("pixel", "Pixel", "PS_Resource"),
    ("compute", "Compute", "CS_Resource"),
    ("amplification", "Amplification", "AS_Resource"),
    ("mesh", "Mesh", "MS_Resource"),
)
_ORDER = {name: index for index, (name, _, _) in enumerate(_STAGES)}


class _Incomplete(Exception):
    """A local evidence/budget failure; never a swallowed native API exception."""


class _IndexUnavailable(Exception):
    pass


class ShaderHashService:
    def __init__(self, ctx, invoke_fn, request_control=None):
        self.ctx = ctx
        self._invoke = invoke_fn
        self.request_control = request_control

    @staticmethod
    def _integer(name, value, low, high, optional=False):
        if optional and value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError("%s must be an integer between %d and %d" % (name, low, high))

    def _cancel(self):
        if self.request_control is not None:
            self.request_control.check_cancelled()

    def _check(self):
        self._cancel()
        if time.monotonic() >= self._deadline:
            raise _Incomplete("time_budget")

    @staticmethod
    def _text(value):
        return str(value)[:MAX_TEXT]

    @staticmethod
    def _key(row):
        return (row["event_id"], _ORDER[row["stage"]])

    def _stage_values(self, stage):
        result = []
        for name, enum_name, usage_name in _STAGES:
            if stage != "all" and stage != name:
                continue
            enum_value = getattr(rd.ShaderStage, enum_name, None)
            if enum_value is not None:
                result.append((name, enum_value, usage_name))
        if not result:
            raise ValueError("Shader stage is unavailable: %s" % stage)
        return result

    @staticmethod
    def _is_compute(action):
        return bool(action.flags & getattr(rd.ActionFlags, "Dispatch", 0))

    @staticmethod
    def _is_graphics(action):
        flags = getattr(rd.ActionFlags, "Drawcall", 0) | getattr(rd.ActionFlags, "MeshDispatch", 0)
        return bool(action.flags & flags)

    def _relevant(self, action, stage):
        if stage == "compute":
            return self._is_compute(action)
        if not self._is_graphics(action):
            return False
        mesh = bool(action.flags & getattr(rd.ActionFlags, "MeshDispatch", 0))
        if stage in ("vertex", "hull", "domain", "geometry"):
            return not mesh
        if stage in ("amplification", "mesh"):
            return mesh
        return True

    def _actions(self, controller):
        roots = controller.GetRootActions()
        if len(roots) > MAX_ACTIONS:
            raise _Incomplete("action_budget")
        pending = list(reversed(roots))
        actions = {}
        while pending:
            self._check()
            action = pending.pop()
            self._counters["scanned_actions"] += 1
            if self._counters["scanned_actions"] > MAX_ACTIONS:
                raise _Incomplete("action_budget")
            children = action.children
            if len(children) + len(pending) > MAX_ACTIONS:
                raise _Incomplete("action_budget")
            pending.extend(reversed(children))
            if not (self._is_graphics(action) or self._is_compute(action)):
                continue
            eid = action.eventId
            if isinstance(eid, bool) or not isinstance(eid, int) or not 1 <= eid <= 4294967295:
                raise _Incomplete("invalid_action_event_id")
            if eid in actions:
                raise _Incomplete("duplicate_action_event_id")
            actions[eid] = action
        return actions

    def _in_range(self, eid):
        return ((self._low is None or eid >= self._low) and
                (self._high is None or eid <= self._high))

    def _hash(self, reflection, cache_key, defer_time=False):
        if cache_key in self._hash_cache:
            return self._hash_cache[cache_key]
        check = self._cancel if defer_time else self._check
        check()
        if reflection is None:
            raise _Incomplete("missing_shader_reflection")
        raw = reflection.rawBytes
        size = len(raw)
        if size <= 0:
            raise _Incomplete("missing_shader_bytecode")
        if size > MAX_SHADER_BYTES:
            raise _Incomplete("shader_byte_budget")
        if self._counters["hashed_bytes"] + size > MAX_TOTAL_SHADER_BYTES:
            raise _Incomplete("total_shader_byte_budget")
        raw = bytes(raw)
        if len(raw) != size:
            raise _Incomplete("shader_byte_length_mismatch")
        value = zlib.crc32(raw) & 0xFFFFFFFF
        self._counters["hashed_shaders"] += 1
        self._counters["hashed_bytes"] += size
        info = {"hash_dec": value, "hash_hex": "0x%08X" % value, "bytecode_size": size}
        encoding = getattr(reflection, "encoding", None)
        if encoding is not None:
            info["encoding"] = self._text(encoding)
        self._hash_cache[cache_key] = info
        check()
        return info

    def _row(self, action, stage, resource_id, entry_point, info, resource_name=None):
        eid = action.eventId
        if eid not in self._action_names:
            self._action_names[eid] = self._text(action.GetName(self._structured))
        row = {"event_id": eid, "action_id": action.actionId,
               "name": self._action_names[eid], "stage": stage,
               "resource_id": self._text(resource_id), "entry_point": self._text(entry_point)}
        row.update(info)
        if resource_name is not None:
            row["resource_name"] = self._text(resource_name)
        return row

    def _usage_index(self, controller, actions):
        api = controller.GetAPIProperties().pipelineType
        allowed = [getattr(rd.GraphicsAPI, name, None) for name in ("D3D11", "D3D12")]
        if api not in allowed:
            raise _IndexUnavailable("shader_usage_index_requires_d3d11_or_d3d12")
        if not all(callable(getattr(controller, name, None)) for name in
                   ("GetResources", "GetShaderEntryPoints", "GetUsage", "GetShader")):
            raise _IndexUnavailable("shader_usage_api_unavailable")
        self._check()
        resources = controller.GetResources()
        if len(resources) > MAX_RESOURCES:
            raise _Incomplete("resource_budget")
        names = getattr(self.ctx, "resource_names", {})
        shader_ids = set()
        rows = {}
        for resource in resources:
            self._check()
            self._counters["scanned_resources"] += 1
            if resource.type != rd.ResourceType.Shader:
                continue
            rid = resource.resourceId
            rid_text = str(rid)
            if rid_text in shader_ids:
                raise _Incomplete("duplicate_shader_resource_id")
            shader_ids.add(rid_text)
            entries = controller.GetShaderEntryPoints(rid)
            self._check()
            if not entries:
                continue
            # D3D shader objects contain one entry point. Avoid inventing a
            # pipeline/entry contract for APIs that permit multiple entries.
            if len(entries) != 1:
                raise _Incomplete("ambiguous_shader_entry_points")
            entry = entries[0]
            stages = [(name, enum_value, usage_name) for name, enum_value, usage_name in self._stages
                      if enum_value == entry.stage]
            if not stages:
                continue
            name, _, usage_name = stages[0]
            expected_usage = getattr(rd.ResourceUsage, usage_name, None)
            if expected_usage is None:
                raise _IndexUnavailable("shader_usage_stage_enum_unavailable")
            usages = controller.GetUsage(rid)
            self._check()
            if self._counters["usage_entries"] + len(usages) > MAX_USAGE_ENTRIES:
                raise _Incomplete("usage_budget")
            wanted = set()
            for usage in usages:
                self._check()
                self._counters["usage_entries"] += 1
                eid = usage.eventId
                if isinstance(eid, bool) or not isinstance(eid, int) or not 0 <= eid <= 4294967295:
                    raise _Incomplete("invalid_usage_event_id")
                if eid == 0 or usage.usage != expected_usage:
                    continue
                action = actions.get(eid)
                if action is None or not self._relevant(action, name):
                    self._unmatched_usage += 1
                    continue
                self._index_evidence += 1
                if self._in_range(eid):
                    wanted.add(eid)
            if not wanted:
                continue
            self._check()
            reflection = controller.GetShader(rd.ResourceId.Null(), rid, entry)
            info = self._hash(reflection, (rid_text, name))
            resource_name = names.get(rid_text, getattr(resource, "name", rid_text))
            entry_name = getattr(entry, "name", getattr(reflection, "entryPoint", ""))
            for eid in wanted:
                self._check()
                key = (eid, name)
                row = self._row(actions[eid], name, rid_text, entry_name, info, resource_name)
                old = rows.get(key)
                if old is not None and (old["resource_id"], old["hash_dec"]) != (rid_text, info["hash_dec"]):
                    raise _Incomplete("conflicting_shader_usage")
                rows[key] = row
                if len(rows) > MAX_ROWS:
                    raise _Incomplete("row_budget")
        if self._index_evidence == 0:
            raise _IndexUnavailable("no_positive_shader_object_usage")
        return list(rows.values())

    def _replay(self, controller, candidates):
        if self._low is None or self._high is None:
            raise ValueError("bounded_replay requires event_id_min and event_id_max")
        rows = []
        stop_reason = None
        next_event = None
        visible_count = 0
        for action in candidates:
            # A cursor may split stage rows within an event. Re-select only that
            # event and filter its already-returned stages after collection.
            if self._after is not None and action.eventId < self._after[0]:
                continue
            self._cancel()
            if visible_count >= self._limit:
                stop_reason, next_event = "limit", action.eventId
                break
            if self._counters["replayed_events"] >= self._max_events:
                stop_reason, next_event = "max_events", action.eventId
                break
            if time.monotonic() >= self._deadline:
                stop_reason, next_event = "time_budget", action.eventId
                break
            controller.SetFrameEvent(action.eventId, False)
            self._counters["replayed_events"] += 1
            self._cancel()
            pipe = controller.GetPipelineState()
            for name, stage, _ in self._stages:
                if not self._relevant(action, name):
                    continue
                self._cancel()
                rid = pipe.GetShader(stage)
                if rid == rd.ResourceId.Null():
                    continue
                rid_text = str(rid)
                # Time is checked between events. Finish a selected event's
                # bounded stage group so its resume cursor cannot drop a stage.
                # Cancellation/native failures still abort immediately.
                cache_key = (rid_text, name)
                if cache_key not in self._hash_cache:
                    self._cancel()
                    reflection = pipe.GetShaderReflection(stage)
                    info = self._hash(reflection, cache_key, defer_time=True)
                else:
                    info = self._hash_cache[cache_key]
                self._cancel()
                entry = pipe.GetShaderEntryPoint(stage)
                self._cancel()
                resource_name = getattr(self.ctx, "resource_names", {}).get(rid_text)
                row = self._row(action, name, rid_text, entry, info, resource_name)
                rows.append(row)
                if self._after is None or self._key(row) > self._after:
                    visible_count += 1
        return rows, stop_reason, next_event

    def _read_cursor(self, cursor, identity):
        if cursor is None:
            return None, None
        if not isinstance(cursor, str) or len(cursor) > 4096:
            raise ValueError("cursor must be a JSON string of at most 4096 characters")
        try:
            data = json.loads(cursor)
        except (ValueError, TypeError):
            raise ValueError("Invalid shader hash cursor")
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("query") != identity:
            raise ValueError("Shader hash cursor does not match this query")
        eid, stage = data.get("last_eid"), data.get("last_stage")
        self._integer("cursor.last_eid", eid, 0, 4294967295)
        if stage not in _ORDER and stage != "before":
            raise ValueError("Invalid cursor stage")
        backend = data.get("backend")
        if backend not in ("shader_usage_index", "bounded_shader_replay"):
            raise ValueError("Invalid cursor backend")
        explicit_backend = {"usage_index": "shader_usage_index",
                            "bounded_replay": "bounded_shader_replay"}.get(identity["method"])
        if explicit_backend is not None and backend != explicit_backend:
            raise ValueError("Cursor backend conflicts with requested method")
        if backend == "bounded_shader_replay" and (self._low is None or self._high is None):
            raise ValueError("Replay cursor requires explicit event bounds")
        if not self._in_range(eid):
            raise ValueError("Cursor event is outside the requested range")
        if stage != "before" and stage not in [name for name, _, _ in self._stages]:
            raise ValueError("Cursor stage is outside the requested stage filter")
        return (eid, -1 if stage == "before" else _ORDER[stage]), backend

    def _cursor(self, key, backend):
        eid, order = key
        stage = "before" if order < 0 else _STAGES[order][0]
        return json.dumps({"version": 1, "query": self._identity, "backend": backend,
                           "last_eid": eid, "last_stage": stage}, separators=(",", ":"), sort_keys=True)

    def _group(self, rows):
        groups = {}
        resource_sets, entry_sets = {}, {}
        for row in rows:
            self._cancel()
            key = (row["stage"], row["hash_dec"])
            if key not in groups:
                item = {key: row[key] for key in ("stage", "hash_dec", "hash_hex", "bytecode_size")}
                item.update(resource_ids=[], entry_points=[], event_ids=[], first_event_id=row["event_id"])
                if "encoding" in row:
                    item["encoding"] = row["encoding"]
                groups[key] = item
                resource_sets[key], entry_sets[key] = set(), set()
            item = groups[key]
            if row["resource_id"] not in resource_sets[key]:
                item["resource_ids"].append(row["resource_id"])
                resource_sets[key].add(row["resource_id"])
            if row["entry_point"] not in entry_sets[key]:
                item["entry_points"].append(row["entry_point"])
                entry_sets[key].add(row["entry_point"])
            item["event_ids"].append(row["event_id"])
        result = sorted(groups.values(), key=lambda item: (item["first_event_id"], _ORDER[item["stage"]], item["hash_dec"]))
        for item in result:
            item["event_count"] = len(item["event_ids"])
        return result

    def _finish(self, result, rows, source_complete, stop_reason=None, next_event=None):
        self._cancel()
        rows.sort(key=self._key)
        self._cancel()
        if self._after is not None:
            rows = [row for row in rows if self._key(row) > self._after]
        page = rows[:self._limit]
        remaining = len(rows) > len(page)
        result["count"] = len(page)
        result["unique_shaders"] = self._group(page)
        result["unique_count"] = len(result["unique_shaders"])
        if not self._unique_only:
            result["events"] = page
        result["complete"] = bool(source_complete and not remaining)
        result["partial"] = not result["complete"]
        result["stop_reason"] = "limit" if remaining else stop_reason
        if remaining:
            result["next_cursor"] = self._cursor(self._key(page[-1]), result["query_method"])
            result["next_event_id"] = rows[len(page)]["event_id"]
        elif next_event is not None:
            # Preserve progress even if an event had no bound requested shader.
            result["next_cursor"] = self._cursor((next_event, -1), result["query_method"])
            result["next_event_id"] = next_event
        result["counters"] = dict(self._counters)
        # Bound encoded output, not merely event count. Do not manufacture a
        # resume cursor after omitting an over-budget response.
        if len(json.dumps(result, ensure_ascii=True).encode("utf-8")) > MAX_OUTPUT_BYTES:
            result.update(count=0, unique_count=0, unique_shaders=[], complete=False,
                          partial=True, stop_reason="output_byte_budget", next_cursor=None,
                          next_event_id=None)
            if not self._unique_only:
                result["events"] = []
            result["diagnostics"].append("output_byte_budget")
        return result

    def list_shader_hashes(self, stage="pixel", event_id_min=None, event_id_max=None,
                           unique_only=False, limit=None, method="auto", max_events=32,
                           time_budget_ms=5000, cursor=None):
        """Enumerate recorded hashes, or replay only an explicit bounded range.

        ``limit`` limits event-stage rows even when ``unique_only`` aggregates
        them. Pass ``next_cursor`` unchanged to resume without splitting loss.
        Replay mode leaves the controller at its last event; GUI selection is
        unchanged. Native errors/cancellation propagate, with no extra replay.
        """
        if not isinstance(stage, str) or stage not in _ORDER and stage != "all":
            raise ValueError("Unknown shader stage")
        if method not in ("auto", "usage_index", "bounded_replay"):
            raise ValueError("method must be auto, usage_index, or bounded_replay")
        if not isinstance(unique_only, bool):
            raise ValueError("unique_only must be a boolean")
        self._integer("event_id_min", event_id_min, 0, 4294967295, True)
        self._integer("event_id_max", event_id_max, 0, 4294967295, True)
        if event_id_min is not None and event_id_max is not None and event_id_min > event_id_max:
            raise ValueError("event_id_min must not exceed event_id_max")
        self._integer("limit", limit, 1, 10000, True)
        self._integer("max_events", max_events, 1, 128)
        self._integer("time_budget_ms", time_budget_ms, 1, 10000)
        if method == "bounded_replay" and (event_id_min is None or event_id_max is None):
            raise ValueError("bounded_replay requires event_id_min and event_id_max")
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        self._cancel()
        self._stages = self._stage_values(stage)
        self._low, self._high = event_id_min, event_id_max
        self._max_events, self._limit = max_events, limit if limit is not None else 10000
        self._unique_only = unique_only
        self._identity = {"stage": stage, "event_id_min": event_id_min,
                          "event_id_max": event_id_max, "unique_only": unique_only,
                          "method": method}
        self._after, cursor_backend = self._read_cursor(cursor, self._identity)
        self._hash_cache, self._action_names = {}, {}
        self._counters = {"scanned_actions": 0, "scanned_resources": 0,
                          "usage_entries": 0, "hashed_shaders": 0,
                          "hashed_bytes": 0, "replayed_events": 0}
        self._index_evidence, self._unmatched_usage = 0, 0
        result = {"revision": SHADER_HASH_REVISION, "api": None, "stage": stage,
                  "event_id_min": event_id_min, "event_id_max": event_id_max,
                  "count": 0, "unique_count": 0, "unique_shaders": [],
                  "limit": self._limit, "count_is_page_count": True,
                  "supported": True, "complete": False, "partial": True,
                  "query_method": "shader_usage_index", "stop_reason": None,
                  "next_event_id": None, "next_cursor": None, "diagnostics": [],
                  "coverage": {}, "native_calls_hard_cancellable": False}
        if not unique_only:
            result["events"] = []

        def callback(controller):
            self._deadline = time.monotonic() + time_budget_ms / 1000.0
            try:
                self._check()
                result["api"] = self._text(controller.GetAPIProperties().pipelineType)
                actions = self._actions(controller)
                candidates = sorted((a for a in actions.values() if self._in_range(a.eventId)
                                     and any(self._relevant(a, name) for name, _, _ in self._stages)),
                                    key=lambda a: a.eventId)
                result["candidate_events"] = len(candidates)
                self._structured = controller.GetStructuredFile()
                backend = cursor_backend or ("bounded_shader_replay" if method == "bounded_replay" else "shader_usage_index")
                if not candidates:
                    result["query_method"] = backend
                    result["coverage"] = {"scope": "no relevant draw/dispatch events in range",
                                          "binding_completeness_proven": True}
                    self._finish(result, [], True)
                    return
                rows = []
                if backend == "shader_usage_index":
                    try:
                        rows = self._usage_index(controller, actions)
                    except _IndexUnavailable as exc:
                        result["diagnostics"].append(str(exc))
                        # No implicit whole-frame replay. A native failure or an
                        # exhausted budget never enters this compatibility path.
                        if method == "auto" and self._low is not None and self._high is not None and cursor_backend is None:
                            self._check()
                            backend = "bounded_shader_replay"
                        else:
                            result.update(supported=False, stop_reason="index_unavailable")
                            result["coverage"] = {"scope": "shader-object usage index unavailable or unproven",
                                                  "binding_completeness_proven": False}
                            self._finish(result, [], False, "index_unavailable")
                            return
                result["query_method"] = backend
                if backend == "shader_usage_index":
                    covered = {row["event_id"] for row in rows}
                    missing = [a.eventId for a in candidates if a.eventId not in covered]
                    result["coverage"] = {
                        "scope": "recorded shader-object usage",
                        "binding_completeness_proven": False,
                        "positive_stage_matching_entries": self._index_evidence,
                        "unmatched_usage_entries": self._unmatched_usage,
                        "uncovered_candidate_event_count": len(missing),
                        "uncovered_candidate_events": missing[:256],
                        "limitations": ["Absent index entries do not prove an unbound shader. Some draws legitimately have no shader in a requested optional stage.",
                                        "complete describes traversal and pagination of recorded entries, not independent proof of every binding."]}
                    self._finish(result, rows, True)
                else:
                    rows, stop_reason, next_event = self._replay(controller, candidates)
                    result["coverage"] = {"scope": "bound shaders observed by bounded replay",
                                          "binding_completeness_proven": next_event is None,
                                          "controller_left_at_last_event": True,
                                          "limitations": ["Time is checked between event groups; cancellation is checked between native calls. A native replay cannot be forcibly interrupted."]}
                    self._finish(result, rows, next_event is None, stop_reason, next_event)
            except _Incomplete as exc:
                reason = str(exc)
                result["diagnostics"].append(reason)
                result["coverage"] = {"scope": "incomplete traversal; no ordered complete result is claimed",
                                      "binding_completeness_proven": False}
                self._finish(result, [], False, reason)
            self._cancel()

        self._invoke(callback)
        return result
