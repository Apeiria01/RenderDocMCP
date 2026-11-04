"""
RenderDoc API Facade
Provides thread-safe access to RenderDoc's ReplayController and CaptureContext.
Searches use AsyncInvoke; existing bounded reads use BlockInvoke.
"""

import os

from . import services as service_modules, utils as utility_modules
from .services import inspection_service, pipeline_service, draw_metadata_service, shader_hash_service, context_service
from .utils import inspection, serializers, draw_metadata
from .services import (
    CaptureManager,
    ActionService,
    SearchService,
    ResourceService,
)

FACADE_REVISION = "shader-inspection-2"
DRAW_METADATA_FACADE_REVISION = "draw-metadata-3"
SHADER_HASH_FACADE_REVISION = "shader-hash-index-1"
ACTION_CONTEXT_FACADE_REVISION = "action-context-1"


class _ShaderHashContextSnapshot:
    """Only UI-owned capture availability is needed for shader enumeration."""

    def __init__(self, ctx):
        self._loaded = ctx.IsCaptureLoaded()
        if not self._loaded:
            raise ValueError("No capture loaded")

    def IsCaptureLoaded(self):
        return self._loaded


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
        # The services package can retain an old class alias after unordered reload.
        self._pipeline = pipeline_service.PipelineService(ctx, self._invoke)

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

    def shader_hashes_async(self, hash_fn, complete, request_control=None):
        """Enumerate on the official replay queue without blocking the UI."""
        snapshot = _ShaderHashContextSnapshot(self.ctx)

        def replay_work(controller):
            try:
                if request_control is not None:
                    request_control.check_cancelled()
                service = shader_hash_service.ShaderHashService(
                    snapshot, lambda callback: callback(controller),
                    request_control=request_control,
                )
                result = hash_fn(service)
            except Exception as exc:
                complete(None, exc)
            else:
                complete(result, None)

        self.ctx.Replay().AsyncInvoke(replay_work)

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
        revisions = {
            "facade": FACADE_REVISION,
            "pipeline": getattr(pipeline_service, "PIPELINE_INSPECTION_REVISION", None),
            "inspection_service": getattr(inspection_service, "INSPECTION_SERVICE_REVISION", None),
            "inspection_helpers": inspection.INSPECTION_REVISION,
            "serializers": getattr(serializers, "VARIABLE_SERIALIZER_REVISION", None),
        }
        current = self._pipeline.__class__ is pipeline_service.PipelineService
        serializer_current = getattr(inspection, "serializers", None) is serializers
        return {"inspection_revision": FACADE_REVISION,
                "component_revisions": revisions,
                "pipeline_instance_current": current,
                "serializer_module_current": serializer_current,
                "dependencies_coherent": current and serializer_current and all(
                    value == FACADE_REVISION for value in revisions.values()),
                "module_path": os.path.abspath(__file__),
                "capture_loaded": bool(self.ctx.IsCaptureLoaded()),
                "features": ["constant_buffer_values", "typed_srv_formats",
                             "sampler_descriptors", "bounded_postvs"],
                "feature_info": {"draw_metadata": self._draw_metadata_info(),
                                 "shader_hashes": self._shader_hash_info(),
                                 "action_context": self._action_context_info()}}

    def _action_context_info(self):
        names = ("get_action_context", "get_resource_provenance")
        service_class = getattr(context_service, "ContextService", None)
        available = all(callable(getattr(service_class, name, None)) for name in names)
        service_current = getattr(service_modules, "context_service", None) is context_service
        membership_current = getattr(draw_metadata_service, "context_service", None) is context_service
        class_current = self.__class__ is RenderDocFacade
        methods_current = all(
            getattr(getattr(self, name, None), "__func__", None) is getattr(RenderDocFacade, name, None)
            and getattr(getattr(RenderDocFacade, name, None), "__globals__", {}).get("context_service") is context_service
            for name in names)
        return {"revision": ACTION_CONTEXT_FACADE_REVISION, "available": available,
                "service_revision": getattr(context_service, "CONTEXT_REVISION", None),
                "facade_class_current": class_current, "facade_methods_current": methods_current,
                "service_module_current": service_current,
                "action_membership_service_current": membership_current,
                "dependencies_coherent": bool(available and service_current and membership_current and class_current and methods_current
                    and getattr(context_service, "CONTEXT_REVISION", None) == ACTION_CONTEXT_FACADE_REVISION),
                "service_path": getattr(context_service, "__file__", None),
                "replay_seeks": False, "service_lifetime": "constructed per call from current module attribute"}

    def get_action_context(self, event_id, before_actions=8, after_actions=2, max_events=128):
        return context_service.ContextService(self.ctx, self._invoke).get_action_context(
            event_id, before_actions, after_actions, max_events)

    def get_resource_provenance(self, resource_ids, max_chunks=32):
        return context_service.ContextService(self.ctx, self._invoke).get_resource_provenance(resource_ids, max_chunks)

    def _shader_hash_info(self):
        service_class = getattr(shader_hash_service, "ShaderHashService", None)
        available = callable(getattr(service_class, "list_shader_hashes", None))
        service_current = getattr(service_modules, "shader_hash_service", None) is shader_hash_service
        class_current = self.__class__ is RenderDocFacade
        methods_current = all(
            getattr(getattr(self, name, None), "__func__", None) is getattr(RenderDocFacade, name, None)
            and getattr(getattr(RenderDocFacade, name, None), "__globals__", {}).get("shader_hash_service") is shader_hash_service
            for name in ("list_shader_hashes", "shader_hashes_async"))
        service_revision = getattr(shader_hash_service, "SHADER_HASH_REVISION", None)
        return {"revision": SHADER_HASH_FACADE_REVISION,
                "available": available,
                "service_revision": service_revision,
                "facade_class_current": class_current,
                "facade_methods_current": methods_current,
                "service_module_current": service_current,
                "dependencies_coherent": bool(available and service_current and class_current
                                              and methods_current and service_revision == SHADER_HASH_FACADE_REVISION),
                "native_index_support": "detected from recorded shader usage per request",
                "service_path": getattr(shader_hash_service, "__file__", None)}

    def _draw_metadata_info(self):
        revisions = {
            "facade": DRAW_METADATA_FACADE_REVISION,
            "service": getattr(draw_metadata_service, "DRAW_METADATA_SERVICE_REVISION", None),
            "helper": getattr(draw_metadata, "DRAW_METADATA_REVISION", None),
        }
        identities = {
            "facade_service_module_current": getattr(service_modules, "draw_metadata_service", None) is draw_metadata_service,
            "facade_helper_module_current": getattr(utility_modules, "draw_metadata", None) is draw_metadata,
            "service_helper_module_current": getattr(draw_metadata_service, "draw_metadata", None) is draw_metadata,
        }
        names = ("get_rasterizer_state", "get_shader_artifact", "get_api_events")
        service_class = getattr(draw_metadata_service, "DrawMetadataService", None)
        available = all(callable(getattr(service_class, name, None)) for name in names)
        available = available and all(callable(getattr(draw_metadata, name, None)) for name in (
            "integer", "shader_arguments", "event_arguments", "rasterizer", "raw_shader",
            "signatures", "walk", "event_record", "finish", "sampling_base", "SamplingReader"))
        methods_current = all(
            getattr(getattr(self, name, None), "__func__", None) is getattr(RenderDocFacade, name, None)
            and getattr(getattr(RenderDocFacade, name, None), "__globals__", {}).get("draw_metadata_service") is draw_metadata_service
            for name in names)
        service_wiring_current = all(
            getattr(getattr(service_class, name, None), "__globals__", {}).get("draw_metadata") is draw_metadata
            for name in names)
        class_current = self.__class__ is RenderDocFacade
        coherent = (available and class_current and methods_current and service_wiring_current
                    and all(identities.values()) and all(
                        value == DRAW_METADATA_FACADE_REVISION for value in revisions.values()))
        return {"revision": DRAW_METADATA_FACADE_REVISION, "available": available,
                "component_revisions": revisions, "module_identities": identities,
                "facade_class_current": class_current, "facade_methods_current": methods_current,
                "service_wiring_current": service_wiring_current, "dependencies_coherent": coherent,
                "service_lifetime": "constructed per call from current module attribute",
                "module_paths": {"facade": os.path.abspath(__file__),
                                 "service": getattr(draw_metadata_service, "__file__", None),
                                 "helper": getattr(draw_metadata, "__file__", None)}}

    def get_rasterizer_state(self, event_id):
        return draw_metadata_service.DrawMetadataService(self.ctx, self._invoke).get_rasterizer_state(event_id)

    def get_shader_artifact(self, event_id, stage, **options):
        return draw_metadata_service.DrawMetadataService(self.ctx, self._invoke).get_shader_artifact(event_id, stage, **options)

    def get_api_events(self, event_id_min, event_id_max, max_events=128):
        return draw_metadata_service.DrawMetadataService(self.ctx, self._invoke).get_api_events(event_id_min, event_id_max, max_events)

    def get_constant_buffers(self, event_id, stage, **options):
        return inspection_service.InspectionService(self.ctx, self._invoke).get_constant_buffers(event_id, stage, **options)

    def get_postvs_data(self, event_id, **options):
        return inspection_service.InspectionService(self.ctx, self._invoke).get_postvs_data(event_id, **options)

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
        method="auto",
        max_events=32,
        time_budget_ms=5000,
        cursor=None,
    ):
        """Compatibility entry point; MCP requests use shader_hashes_async."""
        return shader_hash_service.ShaderHashService(self.ctx, self._invoke).list_shader_hashes(
            stage=stage,
            event_id_min=event_id_min,
            event_id_max=event_id_max,
            unique_only=unique_only,
            limit=limit,
            method=method,
            max_events=max_events,
            time_budget_ms=time_budget_ms,
            cursor=cursor,
        )
