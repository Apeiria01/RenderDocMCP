"""
Request Handler for RenderDoc MCP Bridge
Routes incoming requests to appropriate facade methods.
"""

import traceback

from .request_control import RequestCancelled


class RequestHandler:
    """Handles incoming MCP bridge requests"""

    def __init__(self, facade):
        self.facade = facade
        self._methods = {
            "ping": self._handle_ping,
            "get_bookmarks": self._handle_get_bookmarks,
            "get_capture_status": self._handle_get_capture_status,
            "get_draw_calls": self._handle_get_draw_calls,
            "get_frame_summary": self._handle_get_frame_summary,
            "find_draws_by_shader": self._handle_find_draws_by_shader,
            "find_draws_by_texture": self._handle_find_draws_by_texture,
            "find_draws_by_resource": self._handle_find_draws_by_resource,
            "find_resource_usage": self._handle_find_resource_usage,
            "get_draw_call_details": self._handle_get_draw_call_details,
            "get_action_timings": self._handle_get_action_timings,
            "get_shader_info": self._handle_get_shader_info,
            "get_bridge_info": self._handle_get_bridge_info,
            "get_constant_buffers": self._handle_get_constant_buffers,
            "get_postvs_data": self._handle_get_postvs_data,
            "get_buffer_contents": self._handle_get_buffer_contents,
            "get_texture_info": self._handle_get_texture_info,
            "get_texture_data": self._handle_get_texture_data,
            "get_pipeline_state": self._handle_get_pipeline_state,
            "get_output_merger_state": self._handle_get_output_merger_state,
            "list_set_render_targets": self._handle_list_set_render_targets,
            "list_shader_hashes": self._handle_list_shader_hashes,
            "list_captures": self._handle_list_captures,
            "open_capture": self._handle_open_capture,
        }

    def handle(self, request):
        """Handle a request and return response"""
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})

        try:
            if method not in self._methods:
                return self._error_response(
                    request_id, -32601, "Method not found: %s" % method
                )

            result = self._methods[method](params)
            return {"id": request_id, "result": result}

        except ValueError as e:
            return self._error_response(request_id, -32602, str(e))
        except Exception as e:
            traceback.print_exc()
            return self._error_response(request_id, -32000, str(e))

    _SEARCH_METHODS = frozenset((
        "find_draws_by_shader", "find_draws_by_texture",
        "find_draws_by_resource", "find_resource_usage",
    ))

    def handle_async(self, request, complete, control=None):
        """Return True when a search owns the asynchronous response callback."""
        method = request.get("method")
        if method not in self._SEARCH_METHODS:
            return False
        request_id = request.get("id")

        def finished(result, error):
            if error is None:
                response = {"id": request_id, "result": result}
            else:
                # Cancellation is cooperative between RenderDoc API calls.
                if isinstance(error, RequestCancelled):
                    code = -32800
                elif isinstance(error, (ValueError, TypeError)):
                    code = -32602
                else:
                    code = -32000
                response = self._error_response(request_id, code, str(error))
            complete(response)

        try:
            self.facade.search_async(
                lambda service: self._dispatch_search(
                    method, request.get("params", {}), service
                ),
                finished, request_control=control,
            )
        except Exception as exc:
            finished(None, exc)
        return True

    def _dispatch_search(self, method, params, target=None):
        target = target if target is not None else self.facade
        parameter = {
            "find_draws_by_shader": "shader_name",
            "find_draws_by_texture": "texture_name",
            "find_draws_by_resource": "resource_id",
            "find_resource_usage": "resource_id",
        }[method]
        value = params.get(parameter)
        if value is None:
            raise ValueError("%s is required" % parameter)
        options = {
            "event_id_min": params.get("event_id_min"),
            "event_id_max": params.get("event_id_max"),
            "max_results": params.get("max_results", 1000),
        }
        if method in ("find_draws_by_shader", "find_draws_by_resource"):
            options["max_events"] = params.get("max_events", 32)
            options["time_budget_ms"] = params.get("time_budget_ms", 2000)
        if method == "find_draws_by_shader":
            options["stage"] = params.get("stage")
        return getattr(target, method)(value, **options)

    def _error_response(self, request_id, code, message):
        """Create an error response"""
        return {"id": request_id, "error": {"code": code, "message": message}}

    def _handle_ping(self, params):
        """Handle ping request"""
        return {"status": "ok", "message": "pong"}

    def _handle_get_bookmarks(self, params):
        """Handle get_bookmarks request.

        Reads the current Event Browser bookmarks from the CaptureContext.
        Each bookmark has an eventId and optional text. Runs on the RenderDoc
        UI thread (handler is driven by a QTimer), so direct ctx access is safe.
        """
        return self.facade.get_bookmarks()

    def _handle_get_capture_status(self, params):
        """Handle get_capture_status request"""
        return self.facade.get_capture_status()

    def _handle_get_draw_calls(self, params):
        """Handle get_draw_calls request"""
        include_children = params.get("include_children", True)
        marker_filter = params.get("marker_filter")
        exclude_markers = params.get("exclude_markers")
        event_id_min = params.get("event_id_min")
        event_id_max = params.get("event_id_max")
        only_actions = params.get("only_actions", False)
        flags_filter = params.get("flags_filter")
        return self.facade.get_draw_calls(
            include_children=include_children,
            marker_filter=marker_filter,
            exclude_markers=exclude_markers,
            event_id_min=event_id_min,
            event_id_max=event_id_max,
            only_actions=only_actions,
            flags_filter=flags_filter,
        )

    def _handle_get_frame_summary(self, params):
        """Handle get_frame_summary request"""
        return self.facade.get_frame_summary()

    def _handle_find_draws_by_shader(self, params):
        return self._dispatch_search("find_draws_by_shader", params)

    def _handle_find_draws_by_texture(self, params):
        return self._dispatch_search("find_draws_by_texture", params)

    def _handle_find_draws_by_resource(self, params):
        return self._dispatch_search("find_draws_by_resource", params)

    def _handle_find_resource_usage(self, params):
        return self._dispatch_search("find_resource_usage", params)

    def _handle_get_draw_call_details(self, params):
        """Handle get_draw_call_details request"""
        event_id = params.get("event_id")
        if event_id is None:
            raise ValueError("event_id is required")
        return self.facade.get_draw_call_details(int(event_id))

    def _handle_get_action_timings(self, params):
        """Handle get_action_timings request"""
        event_ids = params.get("event_ids")
        marker_filter = params.get("marker_filter")
        exclude_markers = params.get("exclude_markers")
        return self.facade.get_action_timings(
            event_ids=event_ids,
            marker_filter=marker_filter,
            exclude_markers=exclude_markers,
        )

    def _handle_get_shader_info(self, params):
        """Handle get_shader_info request"""
        event_id = params.get("event_id")
        stage = params.get("stage")
        if event_id is None:
            raise ValueError("event_id is required")
        if stage is None:
            raise ValueError("stage is required")
        return self.facade.get_shader_info(event_id, stage)

    def _handle_get_bridge_info(self, params):
        return self.facade.get_bridge_info()

    def _handle_get_constant_buffers(self, params):
        return self.facade.get_constant_buffers(
            params.get("event_id"), params.get("stage"),
            block_index=params.get("block_index"),
            array_element=params.get("array_element", 0),
            max_bytes=params.get("max_bytes", 1048576),
        )

    def _handle_get_postvs_data(self, params):
        return self.facade.get_postvs_data(
            params.get("event_id"), instance=params.get("instance", 0),
            view=params.get("view", 0), stage=params.get("stage", "vsout"),
            max_bytes=params.get("max_bytes", 8388608),
        )

    def _handle_get_buffer_contents(self, params):
        """Handle get_buffer_contents request"""
        resource_id = params.get("resource_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        offset = params.get("offset", 0)
        length = params.get("length", 0)
        return self.facade.get_buffer_contents(
            resource_id, offset, length, event_id=params.get("event_id")
        )

    def _handle_get_texture_info(self, params):
        """Handle get_texture_info request"""
        resource_id = params.get("resource_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        return self.facade.get_texture_info(resource_id)

    def _handle_get_texture_data(self, params):
        """Handle get_texture_data request"""
        resource_id = params.get("resource_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        mip = params.get("mip", 0)
        slice_idx = params.get("slice", 0)
        sample = params.get("sample", 0)
        depth_slice = params.get("depth_slice")  # None = full volume
        return self.facade.get_texture_data(
            resource_id, mip, slice_idx, sample, depth_slice,
            event_id=params.get("event_id"),
        )

    def _handle_get_output_merger_state(self, params):
        """Do not coerce event IDs; service validates type and capture membership."""
        return self.facade.get_output_merger_state(params.get("event_id"))

    def _handle_get_pipeline_state(self, params):
        """Handle get_pipeline_state request"""
        event_id = params.get("event_id")
        if event_id is None:
            raise ValueError("event_id is required")
        return self.facade.get_pipeline_state(event_id)

    def _handle_list_set_render_targets(self, params):
        """Handle list_set_render_targets request"""
        return self.facade.list_set_render_targets()

    def _handle_list_shader_hashes(self, params):
        """Handle list_shader_hashes request"""
        stage = params.get("stage", "pixel")
        event_id_min = params.get("event_id_min")
        event_id_max = params.get("event_id_max")
        unique_only = params.get("unique_only", False)
        limit = params.get("limit")
        return self.facade.list_shader_hashes(
            stage=stage,
            event_id_min=int(event_id_min) if event_id_min is not None else None,
            event_id_max=int(event_id_max) if event_id_max is not None else None,
            unique_only=bool(unique_only),
            limit=int(limit) if limit is not None else None,
        )

    def _handle_list_captures(self, params):
        """Handle list_captures request"""
        directory = params.get("directory")
        if directory is None:
            raise ValueError("directory is required")
        return self.facade.list_captures(directory)

    def _handle_open_capture(self, params):
        """Handle open_capture request"""
        capture_path = params.get("capture_path")
        if capture_path is None:
            raise ValueError("capture_path is required")
        return self.facade.open_capture(capture_path)
