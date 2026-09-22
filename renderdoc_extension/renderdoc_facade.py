"""
RenderDoc API Facade
Provides thread-safe access to RenderDoc's ReplayController and CaptureContext.
Searches use AsyncInvoke; existing bounded reads use BlockInvoke.
"""

import os

from .services.inspection_service import InspectionService
from .utils.inspection import INSPECTION_REVISION
from .services import (
    CaptureManager,
    ActionService,
    SearchService,
    ResourceService,
    PipelineService,
)


class _SearchContextSnapshot:
    """Capture UI-owned metadata before scheduling replay work."""

    def __init__(self, ctx, request_control=None):
        self._loaded = ctx.IsCaptureLoaded()
        if not self._loaded:
            raise ValueError("No capture loaded")
        self._event_id = int(ctx.CurEvent())
        self.resource_names = {}
        for index, resource in enumerate(ctx.GetResources()):
            if request_control is not None and index % 256 == 0:
                request_control.check_cancelled()
            self.resource_names[str(resource.resourceId)] = str(
                ctx.GetResourceName(resource.resourceId)
            )

    def IsCaptureLoaded(self):
        return self._loaded

    def CurEvent(self):
        return self._event_id


class RenderDocFacade:
    """
    Facade for RenderDoc API access.

    This class delegates all operations to specialized service classes:
    - CaptureManager: Capture management (status, list, open)
    - ActionService: Draw call / action operations
    - SearchService: Reverse lookup searches
    - ResourceService: Texture and buffer data
    - PipelineService: Pipeline state and shader info
    """

    def __init__(self, ctx):
        """
        Initialize facade with CaptureContext.

        Args:
            ctx: The pyrenderdoc CaptureContext from register()
        """
        self.ctx = ctx

        # Initialize service classes
        self._capture = CaptureManager(ctx, self._invoke)
        self._action = ActionService(ctx, self._invoke)
        self._search = SearchService(ctx, self._invoke)
        self._resource = ResourceService(ctx, self._invoke)
        self._pipeline = PipelineService(ctx, self._invoke)

    def _invoke(self, callback):
        """Invoke callback on replay thread via BlockInvoke"""
        self.ctx.Replay().BlockInvoke(callback)

    # ==================== Capture Management ====================

    def get_capture_status(self):
        """Check if a capture is loaded and get API info"""
        return self._capture.get_capture_status()

    def get_bookmarks(self):
        """Get the Event Browser bookmarks set on this capture.

        Returns {loaded, count, bookmarks:[{eventId, text}]}. Bookmarks are a
        UI-level concept owned by ICaptureContext; GetBookmarks() is a plain UI
        call so no BlockInvoke onto the replay thread is required.
        """
        if not self.ctx.IsCaptureLoaded():
            return {"loaded": False, "count": 0, "bookmarks": []}

        marks = self.ctx.GetBookmarks()
        result = []
        for m in marks:
            try:
                text = str(m.text)
            except Exception:
                text = ""
            result.append({"eventId": int(m.eventId), "text": text})
        result.sort(key=lambda b: b["eventId"])
        return {"loaded": True, "count": len(result), "bookmarks": result}

    def list_captures(self, directory):
        """List all .rdc files in the specified directory"""
        return self._capture.list_captures(directory)

    def open_capture(self, capture_path):
        """Open a capture file in RenderDoc"""
        return self._capture.open_capture(capture_path)

    # ==================== Draw Call / Action Operations ====================

    def get_draw_calls(
        self,
        include_children=True,
        marker_filter=None,
        exclude_markers=None,
        event_id_min=None,
        event_id_max=None,
        only_actions=False,
        flags_filter=None,
    ):
        """Get all draw calls/actions in the capture with optional filtering"""
        return self._action.get_draw_calls(
            include_children=include_children,
            marker_filter=marker_filter,
            exclude_markers=exclude_markers,
            event_id_min=event_id_min,
            event_id_max=event_id_max,
            only_actions=only_actions,
            flags_filter=flags_filter,
        )

    def get_frame_summary(self):
        """Get a summary of the current capture frame"""
        return self._action.get_frame_summary()

    def get_draw_call_details(self, event_id):
        """Get detailed information about a specific draw call"""
        return self._action.get_draw_call_details(event_id)

    def get_action_timings(self, event_ids=None, marker_filter=None, exclude_markers=None):
        """Get GPU timing information for actions"""
        return self._action.get_action_timings(
            event_ids=event_ids,
            marker_filter=marker_filter,
            exclude_markers=exclude_markers,
        )

    # ==================== Search Operations ====================

    def search_async(self, search_fn, complete, request_control=None):
        """Snapshot on UI, then run a request on the official replay queue.

        complete(result, error) runs on the replay thread and must not touch UI.
        Each request owns its service, context snapshot and cancellation token.
        """
        snapshot = _SearchContextSnapshot(self.ctx, request_control)

        def replay_work(controller):
            try:
                if request_control is not None:
                    request_control.check_cancelled()
                service = SearchService(
                    snapshot, lambda callback: callback(controller),
                    request_control=request_control,
                )
                result = search_fn(service)
            except Exception as exc:
                complete(None, exc)
            else:
                complete(result, None)

        # Single-argument form is shared by upstream RenderDoc versions;
        # optional tag argument ordering has changed between releases.
        self.ctx.Replay().AsyncInvoke(replay_work)

    def find_draws_by_shader(self, shader_name, stage=None, **options):
        """Search shaders only within an explicitly bounded replay range."""
        return self._search.find_draws_by_shader(shader_name, stage, **options)

    def find_draws_by_texture(self, texture_name, **options):
        """Search texture usage without seeking replay events."""
        return self._search.find_draws_by_texture(texture_name, **options)

    def find_draws_by_resource(self, resource_id, **options):
        """Use the texture/buffer index, or bounded replay for a shader."""
        return self._search.find_draws_by_resource(resource_id, **options)

    def find_resource_usage(self, resource_id, **options):
        """Include copy, clear and resolve events from the standard usage index."""
        return self._search.find_resource_usage(resource_id, **options)

    # ==================== Resource Operations ====================

    def get_buffer_contents(self, resource_id, offset=0, length=0, event_id=None):
        """Get buffer data, optionally immediately after a specific event."""
        return self._resource.get_buffer_contents(
            resource_id, offset, length, event_id=event_id
        )

    def get_texture_info(self, resource_id):
        """Get texture metadata"""
        return self._resource.get_texture_info(resource_id)

    def get_texture_data(self, resource_id, mip=0, slice=0, sample=0, depth_slice=None,
                         event_id=None):
        """Get texture bytes, optionally immediately after a specific event."""
        return self._resource.get_texture_data(
            resource_id, mip, slice, sample, depth_slice, event_id=event_id
        )

    # ==================== Pipeline Operations ====================

    def get_bridge_info(self):
        """Prove which extension code is loaded, without reading or replacing resources."""
        return {"inspection_revision": INSPECTION_REVISION,
                "module_path": os.path.abspath(__file__),
                "capture_loaded": bool(self.ctx.IsCaptureLoaded()),
                "features": ["constant_buffer_values", "typed_srv_formats",
                             "sampler_descriptors", "bounded_postvs"]}

    def get_constant_buffers(self, event_id, stage, **options):
        return InspectionService(self.ctx, self._invoke).get_constant_buffers(event_id, stage, **options)

    def get_postvs_data(self, event_id, **options):
        return InspectionService(self.ctx, self._invoke).get_postvs_data(event_id, **options)

    def get_shader_info(self, event_id, stage):
        """Get shader information for a specific stage"""
        return self._pipeline.get_shader_info(event_id, stage)

    def get_output_merger_state(self, event_id):
        """Get depth/stencil, DSV/RTVs and blending at one event."""
        return self._pipeline.get_output_merger_state(event_id)

    def get_pipeline_state(self, event_id):
        """Get full pipeline state at an event"""
        return self._pipeline.get_pipeline_state(event_id)

    def list_set_render_targets(self):
        """Enumerate OMSetRenderTargets calls with in/out-pass classification and rtv[0]"""
        return self._pipeline.list_set_render_targets()

    def list_shader_hashes(
        self,
        stage="pixel",
        event_id_min=None,
        event_id_max=None,
        unique_only=False,
        limit=None,
    ):
        """List shader hashes observed in pipeline state events"""
        return self._pipeline.list_shader_hashes(
            stage=stage,
            event_id_min=event_id_min,
            event_id_max=event_id_max,
            unique_only=unique_only,
            limit=limit,
        )
