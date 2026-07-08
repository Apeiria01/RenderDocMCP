"""
Reverse lookup search service for RenderDoc.
"""

import renderdoc as rd

from ..utils import Parsers, Helpers


class SearchService:
    """Reverse lookup search service"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def _search_draws(self, matcher_fn):
        """
        Common template for searching draw calls.

        Args:
            matcher_fn: Function(pipe, controller, action, ctx) -> match_reason or None
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"matches": [], "scanned_draws": 0}

        def callback(controller):
            root_actions = controller.GetRootActions()
            structured_file = controller.GetStructuredFile()
            all_actions = Helpers.flatten_actions(root_actions)

            # Filter to only draw calls and dispatches
            draw_actions = [
                a for a in all_actions
                if a.flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.Dispatch)
            ]
            result["scanned_draws"] = len(draw_actions)

            for action in draw_actions:
                controller.SetFrameEvent(action.eventId, False)
                pipe = controller.GetPipelineState()

                match_reason = matcher_fn(pipe, controller, action, self.ctx)
                if match_reason:
                    result["matches"].append({
                        "event_id": action.eventId,
                        "name": action.GetName(structured_file),
                        "match_reason": match_reason,
                    })

        self._invoke(callback)
        result["total_matches"] = len(result["matches"])
        return result

    def find_draws_by_shader(self, shader_name, stage=None):
        """Find all draw calls using a shader with the given name (partial match)."""
        # Determine which stages to check
        if stage:
            stages_to_check = [Parsers.parse_stage(stage)]
        else:
            stages_to_check = Helpers.get_all_shader_stages()

        def matcher(pipe, controller, action, ctx):
            for s in stages_to_check:
                shader = pipe.GetShader(s)
                if shader == rd.ResourceId.Null():
                    continue

                reflection = pipe.GetShaderReflection(s)
                if reflection:
                    entry_point = pipe.GetShaderEntryPoint(s)
                    shader_debug_name = ""
                    try:
                        shader_debug_name = ctx.GetResourceName(shader)
                    except Exception:
                        pass

                    if shader_name.lower() in entry_point.lower():
                        return "%s entry_point: '%s'" % (str(s), entry_point)
                    elif shader_debug_name and shader_name.lower() in shader_debug_name.lower():
                        return "%s name: '%s'" % (str(s), shader_debug_name)
            return None

        return self._search_draws(matcher)

    def find_draws_by_texture(self, texture_name):
        """Find all draw calls using a texture with the given name (partial match)."""
        stages_to_check = Helpers.get_all_shader_stages()

        def matcher(pipe, controller, action, ctx):
            # Check SRVs (read-only resources)
            for stage in stages_to_check:
                try:
                    srvs = pipe.GetReadOnlyResources(stage, False)
                    for srv in srvs:
                        if srv.descriptor.resource == rd.ResourceId.Null():
                            continue
                        res_name = ""
                        try:
                            res_name = ctx.GetResourceName(srv.descriptor.resource)
                        except Exception:
                            pass
                        if res_name and texture_name.lower() in res_name.lower():
                            return "%s SRV: '%s'" % (str(stage), res_name)
                except Exception:
                    pass

                # Check UAVs (read-write resources)
                try:
                    uavs = pipe.GetReadWriteResources(stage, False)
                    for uav in uavs:
                        if uav.descriptor.resource == rd.ResourceId.Null():
                            continue
                        res_name = ""
                        try:
                            res_name = ctx.GetResourceName(uav.descriptor.resource)
                        except Exception:
                            pass
                        if res_name and texture_name.lower() in res_name.lower():
                            return "%s UAV: '%s'" % (str(stage), res_name)
                except Exception:
                    pass

            # Check render targets (unified descriptor API)
            try:
                for i, rt in enumerate(pipe.GetOutputTargets()):
                    if rt.resource != rd.ResourceId.Null():
                        res_name = ""
                        try:
                            res_name = ctx.GetResourceName(rt.resource)
                        except Exception:
                            pass
                        if res_name and texture_name.lower() in res_name.lower():
                            return "RenderTarget[%d]: '%s'" % (i, res_name)
            except Exception:
                pass

            return None

        return self._search_draws(matcher)

    def find_draws_by_resource(self, resource_id):
        """Find all draw calls using a specific resource ID (exact match)."""
        # Compare by canonical string form. A ResourceId cannot be constructed from a
        # raw integer in the Python bindings (its `id` field is private), so the old
        # parse_resource_id produced a Null id that matched every empty bind slot.
        target_str = "ResourceId::%d" % Parsers.extract_numeric_id(resource_id)
        stages_to_check = Helpers.get_all_shader_stages()

        def matcher(pipe, controller, action, ctx):
            null = rd.ResourceId.Null()

            # Check shaders
            for stage in stages_to_check:
                shader = pipe.GetShader(stage)
                if shader != null and str(shader) == target_str:
                    return "%s shader" % str(stage)

            # Check SRVs and UAVs
            for stage in stages_to_check:
                try:
                    for srv in pipe.GetReadOnlyResources(stage, False):
                        res = srv.descriptor.resource
                        if res != null and str(res) == target_str:
                            return "%s SRV slot %d" % (str(stage), srv.access.index)
                except Exception:
                    pass

                try:
                    for uav in pipe.GetReadWriteResources(stage, False):
                        res = uav.descriptor.resource
                        if res != null and str(res) == target_str:
                            return "%s UAV slot %d" % (str(stage), uav.access.index)
                except Exception:
                    pass

            # Check render targets + depth target (unified descriptor API)
            try:
                for i, rt in enumerate(pipe.GetOutputTargets()):
                    if rt.resource != null and str(rt.resource) == target_str:
                        return "RenderTarget[%d]" % i
                depth = pipe.GetDepthTarget()
                if depth.resource != null and str(depth.resource) == target_str:
                    return "DepthTarget"
            except Exception:
                pass

            return None

        return self._search_draws(matcher)
