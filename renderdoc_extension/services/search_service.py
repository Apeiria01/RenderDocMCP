"""Portable reverse lookup: usage metadata first, explicitly bounded shader replay.

GetUsage(shader) is intentionally never used: upstream RenderDoc does not index
shader objects. Resource usage indexes may omit large/bindless descriptor ranges.
"""

import time

import renderdoc as rd

from ..utils import Parsers, Helpers


class SearchService:
    """Search recorded resource usage without replaying every draw in the frame."""

    def __init__(self, ctx, invoke_fn, request_control=None):
        self.ctx = ctx
        self._invoke = invoke_fn
        self.request_control = request_control

    def _check_cancelled(self):
        if self.request_control is not None:
            self.request_control.check_cancelled()

    def _validate(self, event_id_min, event_id_max, max_results):
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        for name, value in (("event_id_min", event_id_min),
                            ("event_id_max", event_id_max)):
            if value is not None and (isinstance(value, bool) or
                                      not isinstance(value, int) or value < 0):
                raise ValueError("%s must be a non-negative integer" % name)
        if (event_id_min is not None and event_id_max is not None and
                event_id_min > event_id_max):
            raise ValueError("event_id_min must not exceed event_id_max")
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 10000:
            raise ValueError("max_results must be between 1 and 10000")
        self._check_cancelled()

    @staticmethod
    def _in_range(event_id, low, high):
        return (low is None or event_id >= low) and (high is None or event_id <= high)

    def _actions(self, controller):
        # Iterative traversal avoids recursion depth and repeated list copies.
        stack = list(reversed(controller.GetRootActions()))
        actions = {}
        while stack:
            self._check_cancelled()
            action = stack.pop()
            actions[action.eventId] = action
            stack.extend(reversed(action.children))
        return actions

    @staticmethod
    def _is_draw(action):
        return bool(action.flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.Dispatch))

    def _resource_names(self, resources):
        names = {str(resource.resourceId): str(resource.name) for resource in resources}
        # Async requests carry a UI-thread snapshot of user-defined names.
        names.update(getattr(self.ctx, "resource_names", {}))
        return names

    @staticmethod
    def _is_shader(resource):
        return resource.type == rd.ResourceType.Shader

    @staticmethod
    def _is_indexed_resource(resource):
        return resource.type in (
            rd.ResourceType.Texture, rd.ResourceType.Buffer,
            getattr(rd.ResourceType, "SwapchainImage", rd.ResourceType.Texture),
        )

    @staticmethod
    def _resource_id(resource_id):
        number = Parsers.extract_numeric_id(resource_id)
        if number <= 0:
            raise ValueError("resource_id must identify a non-null resource")
        return "ResourceId::%d" % number

    @staticmethod
    def _result(method):
        return {
            "matches": [], "total_matches": 0, "total_matches_is_page_count": True,
            "partial": False, "next_event_id": None, "stop_reason": None,
            "query_method": method,
        }

    def _indexed(self, controller, resources, names, low, high, max_results,
                 draws_only):
        for resource in resources:
            if not self._is_indexed_resource(resource):
                raise ValueError("Portable resource usage queries support only textures and buffers; %s has type %s" % (str(resource.resourceId), str(resource.type)))
        actions = self._actions(controller)
        structured_file = controller.GetStructuredFile()
        result = self._result("resource_usage_index")
        result["coverage"] = {
            "scope": "Recorded usage index plus action output/depth metadata",
            "binding_slots_available": False,
            "untracked_resources": [],
            "limitations": [
                "Large or bindless descriptor ranges may be missing from RenderDoc's usage index; an empty result does not prove the resource was unused.",
                "Shader binding slots and descriptor details require inspection at a selected event.",
            ],
        }
        result["matched_resources"] = [
            {"resource_id": str(resource.resourceId),
             "name": names.get(str(resource.resourceId), "")}
            for resource in resources
        ]
        result["scanned_draws"] = sum(
            1 for action in actions.values()
            if self._is_draw(action) and self._in_range(action.eventId, low, high)
        ) if draws_only else 0
        result["scanned_actions"] = len(actions)
        result["inspected_usage_entries"] = 0
        events = {}

        def add(event_id, resource_id, usage, source):
            if not self._in_range(event_id, low, high):
                return
            action = actions.get(event_id)
            if draws_only and (action is None or not self._is_draw(action)):
                return
            usages = events.setdefault(event_id, {})
            key = (resource_id, usage, source)
            usages[key] = {
                "resource_id": resource_id,
                "resource_name": names.get(resource_id, ""),
                "usage": usage, "source": source,
            }

        resource_ids = set()
        for resource in resources:
            self._check_cancelled()
            # Defensive guard: never depend on fork-only shader usage indexing.
            if self._is_shader(resource):
                raise ValueError("Shader resources require a bounded shader query")
            resource_id = str(resource.resourceId)
            resource_ids.add(resource_id)
            for usage in controller.GetUsage(resource.resourceId):
                self._check_cancelled()
                result["inspected_usage_entries"] += 1
                if usage.eventId == 0 and usage.usage == rd.ResourceUsage.Unused:
                    result["coverage"]["untracked_resources"].append(resource_id)
                    continue
                add(usage.eventId, resource_id, str(usage.usage), "usage_index")

        # Official ActionDescription metadata covers RT/depth attachments without
        # SetFrameEvent or a per-draw pipeline snapshot.
        for action in actions.values():
            self._check_cancelled()
            if not self._is_draw(action) or not self._in_range(action.eventId, low, high):
                continue
            for index, output in enumerate(action.outputs):
                resource_id = str(output)
                if resource_id in resource_ids:
                    add(action.eventId, resource_id, "ColorTarget[%d]" % index,
                        "action_metadata")
            depth = str(action.depthOut)
            if depth in resource_ids:
                add(action.eventId, depth, "DepthTarget", "action_metadata")

        event_ids = sorted(events)
        if len(event_ids) > max_results:
            result.update(partial=True, stop_reason="max_results",
                          next_event_id=event_ids[max_results])
            event_ids = event_ids[:max_results]
        for event_id in event_ids:
            self._check_cancelled()
            action = actions.get(event_id)
            usages = list(events[event_id].values())
            result["matches"].append({
                "event_id": event_id,
                "name": action.GetName(structured_file) if action else "Event %d" % event_id,
                "match_reason": "; ".join("%s: %s" % (u["resource_id"], u["usage"]) for u in usages),
                "usages": usages,
            })
        result["total_matches"] = len(result["matches"])
        return result

    @staticmethod
    def _validate_replay(low, high, max_events, time_budget_ms):
        if low is None or high is None:
            raise ValueError("Shader searches require both event_id_min and event_id_max; full-frame shader replay is disabled")
        if isinstance(max_events, bool) or not isinstance(max_events, int) or not 1 <= max_events <= 128:
            raise ValueError("max_events must be between 1 and 128")
        if (isinstance(time_budget_ms, bool) or not isinstance(time_budget_ms, int)
                or not 1 <= time_budget_ms <= 10000):
            raise ValueError("time_budget_ms must be between 1 and 10000")

    def _shader_replay(self, controller, matcher, low, high, max_results,
                       max_events, time_budget_ms, original_event):
        self._validate_replay(low, high, max_events, time_budget_ms)
        actions = self._actions(controller)
        candidates = sorted((action for action in actions.values()
                             if self._is_draw(action) and self._in_range(action.eventId, low, high)),
                            key=lambda action: action.eventId)
        structured_file = controller.GetStructuredFile()
        result = self._result("bounded_shader_replay")
        result.update(scanned_draws=0, candidate_draws=len(candidates))
        result["coverage"] = {
            "scope": "Shader bindings at draw/dispatch events within the requested range",
            "limitations": ["Time/cancellation checks occur between replay calls; a single driver replay and final event restoration cannot be interrupted."],
        }
        deadline = time.monotonic() + time_budget_ms / 1000.0
        changed_event = False
        try:
            for action in candidates:
                self._check_cancelled()
                reason = None
                if result["scanned_draws"] >= max_events:
                    reason = "max_events"
                elif len(result["matches"]) >= max_results:
                    reason = "max_results"
                elif time.monotonic() >= deadline:
                    reason = "time_budget"
                if reason:
                    result.update(partial=True, stop_reason=reason,
                                  next_event_id=action.eventId)
                    break
                # Mark before seeking: restore even if the driver raises midway.
                changed_event = True
                controller.SetFrameEvent(action.eventId, False)
                self._check_cancelled()
                pipe = controller.GetPipelineState()
                match_reason = matcher(pipe)
                self._check_cancelled()
                result["scanned_draws"] += 1
                if match_reason:
                    result["matches"].append({
                        "event_id": action.eventId,
                        "name": action.GetName(structured_file),
                        "match_reason": match_reason,
                    })
        finally:
            if changed_event:
                controller.SetFrameEvent(original_event, False)
        result["total_matches"] = len(result["matches"])
        return result

    def find_draws_by_shader(self, shader_name, stage=None, event_id_min=None,
                             event_id_max=None, max_results=1000,
                             max_events=32, time_budget_ms=2000):
        """Inspect shader names/entry points only in an explicit, bounded range."""
        self._validate(event_id_min, event_id_max, max_results)
        self._validate_replay(event_id_min, event_id_max, max_events, time_budget_ms)
        if not shader_name or not shader_name.strip():
            raise ValueError("shader_name must not be empty")
        stages = [Parsers.parse_stage(stage)] if stage else Helpers.get_all_shader_stages()
        original_event = self.ctx.CurEvent()
        result = {}

        def callback(controller):
            names = self._resource_names(controller.GetResources())
            needle = shader_name.lower()

            def matcher(pipe):
                for shader_stage in stages:
                    shader = pipe.GetShader(shader_stage)
                    if shader == rd.ResourceId.Null():
                        continue
                    name = names.get(str(shader), "")
                    if needle in name.lower():
                        return "%s name: '%s'" % (str(shader_stage), name)
                    entry = pipe.GetShaderEntryPoint(shader_stage)
                    if needle in entry.lower():
                        return "%s entry_point: '%s'" % (str(shader_stage), entry)
                return None

            result.update(self._shader_replay(
                controller, matcher, event_id_min, event_id_max, max_results,
                max_events, time_budget_ms, original_event))

        self._invoke(callback)
        return result

    def find_draws_by_texture(self, texture_name, event_id_min=None,
                              event_id_max=None, max_results=1000):
        """Find draw/dispatch usage of named textures through official indexes."""
        self._validate(event_id_min, event_id_max, max_results)
        if not texture_name or not texture_name.strip():
            raise ValueError("texture_name must not be empty")
        result = {}

        def callback(controller):
            resources = controller.GetResources()
            names = self._resource_names(resources)
            texture_ids = {str(texture.resourceId) for texture in controller.GetTextures()}
            needle = texture_name.lower()
            selected = [resource for resource in resources
                        if str(resource.resourceId) in texture_ids and
                        needle in names.get(str(resource.resourceId), "").lower()]
            result.update(self._indexed(controller, selected, names, event_id_min,
                                        event_id_max, max_results, True))

        self._invoke(callback)
        return result

    def find_draws_by_resource(self, resource_id, event_id_min=None,
                               event_id_max=None, max_results=1000,
                               max_events=32, time_budget_ms=2000):
        """Indexed resource usage; shader IDs use bounded upstream replay APIs."""
        self._validate(event_id_min, event_id_max, max_results)
        target = self._resource_id(resource_id)
        # Capture on the caller/UI thread, before entering the replay callback.
        original_event = self.ctx.CurEvent()
        result = {}

        def callback(controller):
            resources = controller.GetResources()
            names = self._resource_names(resources)
            resource = next((resource for resource in resources
                             if str(resource.resourceId) == target), None)
            if resource is None:
                raise ValueError("Resource not found: %s" % target)
            if self._is_shader(resource):
                stages = Helpers.get_all_shader_stages()

                def matcher(pipe):
                    for stage in stages:
                        if str(pipe.GetShader(stage)) == target:
                            return "%s shader" % str(stage)
                    return None

                result.update(self._shader_replay(
                    controller, matcher, event_id_min, event_id_max, max_results,
                    max_events, time_budget_ms, original_event))
            else:
                result.update(self._indexed(controller, [resource], names, event_id_min,
                                            event_id_max, max_results, True))

        self._invoke(callback)
        return result

    def find_resource_usage(self, resource_id, event_id_min=None,
                            event_id_max=None, max_results=1000):
        """Return all recorded resource uses, including copy/clear/resolve events."""
        self._validate(event_id_min, event_id_max, max_results)
        target = self._resource_id(resource_id)
        result = {}

        def callback(controller):
            resources = controller.GetResources()
            resource = next((resource for resource in resources
                             if str(resource.resourceId) == target), None)
            if resource is None:
                raise ValueError("Resource not found: %s" % target)
            if self._is_shader(resource):
                raise ValueError("Upstream RenderDoc does not provide shader-object usage indexes; use find_draws_by_resource with event_id_min and event_id_max")
            result.update(self._indexed(controller, [resource], self._resource_names(resources),
                                        event_id_min, event_id_max, max_results, False))

        self._invoke(callback)
        return result
