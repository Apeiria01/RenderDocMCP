"""
RenderDoc MCP Server
FastMCP 2.0 server providing access to RenderDoc capture data.
"""

from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field, StrictBool, StrictInt, StrictStr

from .bridge.client import RenderDocBridge, RenderDocBridgeError
from .config import settings

# Initialize FastMCP server
mcp = FastMCP(
    name="RenderDoc MCP Server",
)

# RenderDoc bridge client
bridge = RenderDocBridge(host=settings.renderdoc_host, port=settings.renderdoc_port)


@mcp.tool
def list_instances() -> dict:
    """
    List all running RenderDoc instances reachable via the MCP bridge.

    Each instance is identified by its process id (pid) and reports which
    capture file it currently has loaded. Returns:
      - count: number of live instances
      - selected_pid: pid that subsequent tool calls will talk to (null if none)
      - instances: list of {pid, loaded, filename, renderdoc_version,
        heartbeat_age_sec, selected}

    With a single instance it is selected automatically. With multiple
    instances you must call select_instance(pid) before using other tools.
    """
    return bridge.list_instances()


@mcp.tool
def select_instance(pid: int) -> dict:
    """
    Select which RenderDoc instance subsequent tool calls talk to.

    Use list_instances to see the available pids and which capture each
    instance has loaded. The selection persists until that instance exits.
    Returns the selected instance's info.
    """
    return bridge.select_instance(pid)


@mcp.tool
def get_capture_status() -> dict:
    """
    Check if a capture is currently loaded in RenderDoc.
    Returns the capture status and API type if loaded.
    """
    return bridge.call("get_capture_status")


@mcp.tool
def get_bookmarks() -> dict:
    """
    Get the Event Browser bookmarks set on the current capture in RenderDoc.

    These are the bookmarks the user added in the RenderDoc UI (Ctrl+B on an
    event). Returns:
      - loaded: whether a capture is loaded
      - count: number of bookmarks
      - bookmarks: list of {eventId, text}, sorted by eventId

    Bookmarks are saved inside the .rdc capture, so they persist across sessions.
    """
    return bridge.call("get_bookmarks")


@mcp.tool
def get_draw_calls(
    include_children: bool = True,
    marker_filter: str | None = None,
    exclude_markers: list[str] | None = None,
    event_id_min: int | None = None,
    event_id_max: int | None = None,
    only_actions: bool = False,
    flags_filter: list[str] | None = None,
) -> dict:
    """
    Get the list of all draw calls and actions in the current capture.

    Args:
        include_children: Include child actions in the hierarchy (default: True)
        marker_filter: Only include actions under markers containing this string (partial match)
        exclude_markers: Exclude actions under markers containing these strings (list of partial matches)
        event_id_min: Only include actions with event_id >= this value
        event_id_max: Only include actions with event_id <= this value
        only_actions: If True, exclude marker actions (PushMarker/PopMarker/SetMarker)
        flags_filter: Only include actions with these flags (list of flag names, e.g. ["Drawcall", "Dispatch"])

    Returns a hierarchical tree of actions including markers, draw calls,
    dispatches, and other GPU events.
    """
    params: dict[str, object] = {"include_children": include_children}
    if marker_filter is not None:
        params["marker_filter"] = marker_filter
    if exclude_markers is not None:
        params["exclude_markers"] = exclude_markers
    if event_id_min is not None:
        params["event_id_min"] = event_id_min
    if event_id_max is not None:
        params["event_id_max"] = event_id_max
    if only_actions:
        params["only_actions"] = only_actions
    if flags_filter is not None:
        params["flags_filter"] = flags_filter
    return bridge.call("get_draw_calls", params)


@mcp.tool
def get_frame_summary() -> dict:
    """
    Get a summary of the current capture frame.

    Returns statistics about the frame including:
    - API type (D3D11, D3D12, Vulkan, etc.)
    - Total action count
    - Statistics: draw calls, dispatches, clears, copies, presents, markers
    - Top-level markers with event IDs and child counts
    - Resource counts: textures, buffers
    """
    return bridge.call("get_frame_summary")


@mcp.tool
def find_draws_by_shader(
    shader_name: str,
    stage: Literal["vertex", "hull", "domain", "geometry", "pixel", "compute"] | None = None,
    event_id_min: StrictInt | None = None,
    event_id_max: StrictInt | None = None,
    max_results: StrictInt = 1000,
    max_events: StrictInt = 32,
    time_budget_ms: StrictInt = 2000,
) -> dict:
    """
    Search shader names/entry points within an explicit event range.

    Both event_id_min and event_id_max are required for this bounded replay
    search. Upstream RenderDoc does not provide a portable shader usage index.
    At most max_events (1-128, default 32) draw/dispatch events are checked.
    time_budget_ms (1-10000) is cooperative between replay calls: an individual
    GPU replay cannot be interrupted. The original UI-selected event is restored.
    A partial response includes next_event_id; resume by passing it as
    event_id_min with the original upper bound. Never interpret partial matches
    as a complete result. max_results limits the returned page (1-10000).
    """
    return bridge.call("find_draws_by_shader", {
        "shader_name": shader_name, "stage": stage,
        "event_id_min": event_id_min, "event_id_max": event_id_max,
        "max_results": max_results, "max_events": max_events,
        "time_budget_ms": time_budget_ms,
    })


@mcp.tool
def find_draws_by_texture(
    texture_name: str,
    event_id_min: StrictInt | None = None,
    event_id_max: StrictInt | None = None,
    max_results: StrictInt = 1000,
) -> dict:
    """
    Find texture uses in draw/dispatch events through the loaded usage index.

    Matches a partial texture name. No per-event GPU replay is performed.
    Index coverage is not proof of actual pixel access and may omit dynamic
    bindless uses. Slot-level binding details require separate bounded inspection.
    Optional event bounds filter results; max_results (1-10000) limits each page.
    Resume a partial page with event_id_min=next_event_id. To include copies,
    clears and resolves, resolve the resource ID and use find_resource_usage.
    """
    return bridge.call("find_draws_by_texture", {
        "texture_name": texture_name, "event_id_min": event_id_min,
        "event_id_max": event_id_max, "max_results": max_results,
    })


@mcp.tool
def find_draws_by_resource(
    resource_id: str,
    event_id_min: StrictInt | None = None,
    event_id_max: StrictInt | None = None,
    max_results: StrictInt = 1000,
    max_events: StrictInt = 32,
    time_budget_ms: StrictInt = 2000,
) -> dict:
    """
    Find draw/dispatch uses of a resource ID, e.g. ResourceId::12345 or 12345.

    Textures and buffers use the standard loaded usage index plus output-target
    metadata without replaying events. Its coverage may omit dynamic bindless
    uses; it does not report every bound-but-unused descriptor or exact slots.
    Shader IDs require both event bounds and use bounded replay instead of
    nonstandard shader GetUsage support: max_events is 1-128, time_budget_ms
    is 1-10000. max_results (1-10000) limits returned matches for either path.
    Resume partial results using next_event_id as event_id_min. Use
    find_resource_usage for copy/clear/resolve events of a texture or buffer.
    """
    return bridge.call("find_draws_by_resource", {
        "resource_id": resource_id, "event_id_min": event_id_min,
        "event_id_max": event_id_max, "max_results": max_results,
        "max_events": max_events, "time_budget_ms": time_budget_ms,
    })


@mcp.tool
def find_resource_usage(
    resource_id: str,
    event_id_min: StrictInt | None = None,
    event_id_max: StrictInt | None = None,
    max_results: StrictInt = 1000,
) -> dict:
    """
    List indexed texture/buffer uses, including draw, copy, clear and resolve.

    Uses upstream GetUsage without seeking replay events; shader IDs are not
    supported by this tool. Includes usage categories, with entries grouped by
    event. max_results (1-10000) limits the page; optional event bounds filter it.
    Resume partial results with event_id_min=next_event_id. Usage index coverage
    may omit dynamic bindless uses and is not proof of per-pixel GPU access.
    """
    return bridge.call("find_resource_usage", {
        "resource_id": resource_id, "event_id_min": event_id_min,
        "event_id_max": event_id_max, "max_results": max_results,
    })


@mcp.tool
def get_draw_call_details(event_id: int) -> dict:
    """
    Get detailed information about a specific draw call.

    Args:
        event_id: The event ID of the draw call to inspect

    Includes vertex/index counts, resource outputs, and other metadata.
    """
    return bridge.call("get_draw_call_details", {"event_id": event_id})


@mcp.tool
def get_action_timings(
    event_ids: list[int] | None = None,
    marker_filter: str | None = None,
    exclude_markers: list[str] | None = None,
) -> dict:
    """
    Get GPU timing information for actions (draw calls, dispatches, etc.).

    Args:
        event_ids: Optional list of specific event IDs to get timings for.
                   If not specified, returns timings for all actions.
        marker_filter: Only include actions under markers containing this string (partial match).
        exclude_markers: Exclude actions under markers containing these strings.

    Returns timing data including:
    - available: Whether GPU timing counters are supported
    - unit: Time unit (typically "seconds")
    - timings: List of {event_id, name, duration_seconds, duration_ms}
    - total_duration_ms: Sum of all durations
    - count: Number of timing entries

    Note: GPU timing counters may not be available on all hardware/drivers.
    """
    params: dict[str, object] = {}
    if event_ids is not None:
        params["event_ids"] = event_ids
    if marker_filter is not None:
        params["marker_filter"] = marker_filter
    if exclude_markers is not None:
        params["exclude_markers"] = exclude_markers
    return bridge.call("get_action_timings", params)


@mcp.tool
def get_bridge_info() -> dict:
    """Read the loaded extension's inspection revision and module path (not disk version)."""
    return bridge.call("get_bridge_info", {})


@mcp.tool
def get_rasterizer_state(
    event_id: Annotated[StrictInt, Field(ge=1, le=4294967295)],
) -> dict:
    """Read D3D12 rasterizer, viewport/scissor and proven PSO sampling metadata.

    Requires coherent feature_info.draw_metadata revision draw-metadata-3.
    Exact membership, SetFrameEvent, pipeline state, ReplayController.GetResources
    and GetStructuredFile all share one official replay callback. Sampling follows
    pipelineResourceId -> exact ResourceDescription.initialisationChunks -> indexed
    PSO creation chunks. Only ID3D12Device::CreateGraphicsPipeline and
    ID3D12Device2::CreatePipelineState at pDesc.SampleDesc are accepted; the typed
    pPipelineState must match. sampling reports complete/source/pipeline_resource_id,
    sample_count/sample_quality, provenance (chunk/index/path/UnsignedInteger types)
    and traversal budgets. No fictitious outputMerger multisample fields, texture
    sample substitution, nested name search, blob reads or guessed defaults.
    Missing/invalid/unknown/conflicting metadata stays incomplete with null values.
    Zero-sized viewports/scissors, sampleMask and exposed float bits are preserved.
    Replay remains immediately after event_id; GUI selection is unchanged.
    This proves requested metadata, not shader execution or image equivalence.
    Resource/chunk/child/depth/time/JSON bounds cannot hard-cancel a native getter.
    API failures terminate without restore, retry or hidden fallback.
    """
    return bridge.call("get_rasterizer_state", {"event_id": event_id})


@mcp.tool
def get_shader_artifact(
    event_id: Annotated[StrictInt, Field(ge=1, le=4294967295)],
    stage: Literal["vertex", "pixel"],
    max_bytes: Annotated[StrictInt, Field(ge=1, le=1048576)] = 1048576,
    disassembly_target: Annotated[StrictStr, Field(max_length=4096)] | None = None,
    max_text_bytes: Annotated[StrictInt, Field(ge=1, le=1048576)] = 1048576,
) -> dict:
    """Read original shader bytes, CRC32 and reflection signatures for ONE VS/PS.

    Requires draw-metadata-3. Raw bytes and optional text are separately capped
    at 1 MiB. Returns actual encoding and available disassembly targets; by
    default no disassembly is performed. An explicit known target is tried once,
    with no fallback. Native failures stop the request, without restoring replay.
    SHA256 is left to the caller. Reflection signatures do NOT prove interpolation
    modes; semantic names such as _centroid are not an interpolation contract.
    No other shader stages, texture/buffer reads or shader replacement occur.
    Output bounds do not limit the driver's internal cost of one native getter.
    """
    return bridge.call("get_shader_artifact", {
        "event_id": event_id, "stage": stage, "max_bytes": max_bytes,
        "disassembly_target": disassembly_target, "max_text_bytes": max_text_bytes,
    })


@mcp.tool
def get_api_events(
    event_id_min: Annotated[StrictInt, Field(ge=1, le=4294967295)],
    event_id_max: Annotated[StrictInt, Field(ge=1, le=4294967295)],
    max_events: Annotated[StrictInt, Field(ge=1, le=128)] = 128,
) -> dict:
    """Read APIEvent/action/chunk metadata within an inclusive range <=256 EIDs.

    Requires draw-metadata-3. One metadata-only callback reads the controller's
    GetRootActions/GetStructuredFile; zero SetFrameEvent, no per-event replay.
    The owning action may lie outside the range. Traversal has node and cooperative
    time budgets in addition to the result limit (1..128). Incomplete traversal does
    not establish global sorting/coverage or a reliable next-page cursor.
    Missing chunks, duplicate/conflicting events and limits remain explicit.
    A returned EID alone is not proof of an exact before-draw replay boundary.
    """
    return bridge.call("get_api_events", {
        "event_id_min": event_id_min, "event_id_max": event_id_max,
        "max_events": max_events,
    })


@mcp.tool
def get_action_context(
    event_id: Annotated[StrictInt, Field(ge=1, le=4294967295)],
    before_actions: Annotated[StrictInt, Field(ge=0, le=32)] = 8,
    after_actions: Annotated[StrictInt, Field(ge=0, le=32)] = 2,
    max_events: Annotated[StrictInt, Field(ge=1, le=512)] = 128,
) -> dict:
    """Read exact action context and typed structured API parameters without replay.

    Requires coherent feature_info.action_context revision action-context-1.
    First scans only action nodes, not their APIEvent arrays, to verify one exact
    action and its parent path. Returns an EID-ordered neighboring action window;
    synthetic parent EIDs are never used to prune children. Nearest actions get
    the event budget first, with the bounded tail of each owning action.events.
    Omitted earlier events are explicit; this is not a full command-list history.
    Typed ExecuteIndirect/Copy/bind parameters include resource IDs and offsets.
    Opaque blobs are never read. All getters share one official replay callback,
    with action/event/SD child/depth/time/JSON limits. No SetFrameEvent, buffer
    reads, capture writes, or state restoration; native errors propagate.
    """
    return bridge.call("get_action_context", {"event_id": event_id,
        "before_actions": before_actions, "after_actions": after_actions,
        "max_events": max_events})


@mcp.tool
def get_resource_provenance(
    resource_ids: Annotated[list[Annotated[StrictStr, Field(pattern=r"^ResourceId::[1-9][0-9]{0,19}$")]], Field(min_length=1, max_length=16)],
    max_chunks: Annotated[StrictInt, Field(ge=1, le=64)] = 32,
) -> dict:
    """Read exact resource parents/derived IDs and typed initialization chunks.

    Requires coherent feature_info.action_context revision action-context-1.
    Explicit unique non-null ResourceId strings only. GetResources establishes
    unique identity before indexing ResourceDescription.initialisationChunks;
    creation descriptors expose available heap/signature fields. max_chunks is
    shared across requested resources. Opaque contents/blobs are omitted, never
    read; absence of a writer here does not establish CPU/GPU generation origin.
    One metadata-only replay callback, zero SetFrameEvent, bounded resources,
    relations/chunks/SD children/depth/time/JSON; errors stop without retry.
    """
    return bridge.call("get_resource_provenance", {"resource_ids": resource_ids,
        "max_chunks": max_chunks})


@mcp.tool
def get_constant_buffers(
    event_id: StrictInt,
    stage: Literal["vertex", "hull", "domain", "geometry", "pixel", "compute"],
    block_index: StrictInt | None = None,
    array_element: StrictInt = 0,
    max_bytes: StrictInt = 1048576,
) -> dict:
    """Read actual constant values and bounded raw bytes at one exact event.

    block_index is the reflection index, NOT a register. Omit for all blocks
    (up to 128), one array_element per block. An element > 0 requires block_index.
    max_bytes is a shared reflection-byte budget, 1..8 MiB; no length=0 reads.
    Binding resource/offset/size and completeness are explicit. Null-resource
    root/push constants are queried via GetCBufferVariableContents, not skipped.
    Nonfinite floats use NaN/+Infinity/-Infinity strings with raw value bits.
    Replay remains at event_id; the GUI selection is not changed. No capture writes.
    """
    return bridge.call("get_constant_buffers", {
        "event_id": event_id, "stage": stage, "block_index": block_index,
        "array_element": array_element, "max_bytes": max_bytes,
    })


@mcp.tool
def get_postvs_data(
    event_id: StrictInt,
    instance: StrictInt = 0,
    view: StrictInt = 0,
    stage: Literal["vsout", "gsout"] = "vsout",
    max_bytes: StrictInt = 8388608,
) -> dict:
    """Read post-transform buffers for ONE draw/instance/view (never scan draws).

    Returns actual MeshFormat metadata and base64 index/vertex ranges, capped
    at max_bytes (1..8 MiB total). instance is zero-based, not instanceOffset.
    Indices, baseVertex and restart determine the vertex interval. Short reads,
    unavailable APIs and budget limits are explicit; no partial indices are used.
    This is shader output (often clip/post-projection), NOT world-space geometry.
    No unprojection, capture writes or resource replacement is performed.
    Replay remains at event_id; GUI selection is not changed. Driver/runtime
    limitations may return supported=false or complete=false with reason.
    """
    return bridge.call("get_postvs_data", {
        "event_id": event_id, "instance": instance, "view": view,
        "stage": stage, "max_bytes": max_bytes,
    })


@mcp.tool
def get_shader_info(
    event_id: StrictInt,
    stage: Literal["vertex", "hull", "domain", "geometry", "pixel", "compute"],
) -> dict:
    """
    Get shader information for a specific stage at a given event.

    Args:
        event_id: The event ID to inspect the shader at
        stage: The shader stage (vertex, hull, domain, geometry, pixel, compute)

    Returns shader disassembly, constant buffer values, and resource bindings.
    """
    return bridge.call("get_shader_info", {"event_id": event_id, "stage": stage})


@mcp.tool
def get_buffer_contents(
    resource_id: str,
    offset: StrictInt = 0,
    length: StrictInt = 0,
    event_id: StrictInt | None = None,
) -> dict:
    """
    Read the contents of a buffer resource.

    Args:
        resource_id: The resource ID of the buffer to read
        offset: Byte offset to start reading from (default: 0)
        length: Number of bytes to read, 0 for the remainder from offset (default: 0)
        event_id: Optional positive event ID. When provided, replay to immediately
                  after this event and read the buffer in one replay callback.
                  Omit to read the existing replay state without moving it.

    Returns base64-encoded bytes and metadata, including event_id (null when omitted).
    The selected replay state remains at event_id. This does not change the GUI selection.
    """
    params = {"resource_id": resource_id, "offset": offset, "length": length}
    if event_id is not None:
        params["event_id"] = event_id
    return bridge.call("get_buffer_contents", params)


@mcp.tool
def get_texture_info(resource_id: str) -> dict:
    """
    Get metadata about a texture resource.

    Args:
        resource_id: The resource ID of the texture

    Includes dimensions, format, mip levels, and other properties.
    """
    return bridge.call("get_texture_info", {"resource_id": resource_id})


@mcp.tool
def get_texture_data(
    resource_id: str,
    mip: int = 0,
    slice: int = 0,
    sample: int = 0,
    depth_slice: int | None = None,
    event_id: StrictInt | None = None,
) -> dict:
    """
    Read the pixel data of a texture resource.

    Args:
        resource_id: The resource ID of the texture to read
        mip: Mip level to retrieve (default: 0)
        slice: Array slice or cube face index (default: 0)
               For cube maps: 0=X+, 1=X-, 2=Y+, 3=Y-, 4=Z+, 5=Z-
        sample: MSAA sample index (default: 0)
        depth_slice: For 3D textures only, extract a specific depth slice (default: None = full volume)
                     When specified, returns only the 2D slice at that depth index
        event_id: Optional positive event ID. When provided, replay to immediately
                  after this event and read the texture in one replay callback.
                  Omit to read the existing replay state without moving it.

    Returns raw texture bytes as base64 (not a PNG), dimensions, format and event_id
    (null when omitted). The selected replay state remains at event_id.
    This does not change the GUI selection.
    """
    params = {"resource_id": resource_id, "mip": mip, "slice": slice, "sample": sample}
    if depth_slice is not None:
        params["depth_slice"] = depth_slice
    if event_id is not None:
        params["event_id"] = event_id
    return bridge.call("get_texture_data", params)


@mcp.tool
def get_output_merger_state(event_id: StrictInt) -> dict:
    """
    Get D3D11/D3D12 output-merger state immediately after an event.

    Accepts a positive draw/action OR API state-setting event ID. Selection and
    snapshot happen atomically on the replay thread; the UI selection is not
    changed, and the replay controller remains at the requested event, matching
    get_texture_data(event_id=...) semantics.

    Returns DSV/RTV resource and view IDs, formats, mip/slice ranges and texture
    metadata; depth test/write/function/read-only flags; front/back stencil;
    per-target color/alpha blending, logic operations, color masks, blend factor,
    alpha-to-coverage and sample mask. Unbound DSV is null; RTV slots are retained.
    Blend targets are raw entries; effective state is targets[effective_blend_slot].
    UAVs report all bound D3D11 pixel slots or D3D12 shader accesses with register
    mapping and explicit scope; descriptor-query failures appear as uavs_error.
    depth_write_allowed is state eligibility, NOT proof of actual pixel writes.
    Other APIs return output_merger.supported=false with a reason.

    For depth pixels, pass depth_target.resource_id, first_mip and first_slice to
    get_texture_data with the SAME event_id (choose an MSAA sample explicitly).
    """
    return bridge.call("get_output_merger_state", {"event_id": event_id})


@mcp.tool
def get_pipeline_state(event_id: StrictInt) -> dict:
    """
    Get the full graphics pipeline state at a specific event.

    Args:
        event_id: The event ID to get pipeline state at

    Returns detailed pipeline state including:
    - Bound shaders with entry points for each stage
    - Shader resources (SRVs): textures and buffers with dimensions, format, slot, name
    - UAVs (RWTextures/RWBuffers): resource details with dimensions and format
    - Samplers: addressing modes, filter settings, LOD parameters
    - Constant buffers: slot, size, variable count
    - Render targets and depth target
    - D3D11/D3D12 output_merger: DSV/RTVs, depth/stencil and blending
    - Viewports and input assembly state
    """
    return bridge.call("get_pipeline_state", {"event_id": event_id})


@mcp.tool
def list_set_render_targets() -> dict:
    """
    Enumerate every OMSetRenderTargets call in the capture.

    OMSetRenderTargets is a state-setting call (not a draw/dispatch), so it is NOT
    returned by get_draw_calls. This walks the structured event list to find them.

    For each call, reports: event_id, the enclosing debug marker, whether it is
    inside a "Colour Pass" marker (in_colour_pass), the bound rtv[0] resource and
    view, the render-target count, and the depth target.

    Also returns rtv[0] histograms for in-pass vs out-of-pass calls, so you can see
    how render targets differ between the marked geometry/UI passes and the
    post-processing / composite steps between them.
    """
    return bridge.call("list_set_render_targets", {})


@mcp.tool
def list_shader_hashes(
    stage: Literal["all", "vertex", "hull", "domain", "geometry", "pixel", "compute"] = "pixel",
    event_id_min: StrictInt | None = None,
    event_id_max: StrictInt | None = None,
    unique_only: StrictBool = False,
    limit: StrictInt | None = None,
    method: Literal["auto", "usage_index", "bounded_replay"] = "auto",
    max_events: Annotated[StrictInt, Field(ge=1, le=128)] = 32,
    time_budget_ms: Annotated[StrictInt, Field(ge=1, le=10000)] = 5000,
    cursor: StrictStr | None = None,
) -> dict:
    """
    List shader CRC32 hashes using recorded shader-object usage when available.

    Args:
        stage: Shader stage to inspect. Defaults to pixel. Use "all" to inspect all stages.
        event_id_min: Optional minimum event ID.
        event_id_max: Optional maximum event ID.
        unique_only: Omit events; unique_shaders summarizes the same bounded page.
        limit: Maximum event-stage rows in a page, including unique_only mode.
        method: auto prefers the usage index; usage_index never replays. A missing
                index only permits bounded replay when BOTH event bounds exist.
        max_events: At most 128 events may be replayed in one bounded page.
        time_budget_ms: Cooperative work budget; one native getter cannot be cancelled.
        cursor: Exact next_cursor returned by a previous page with the same query.

    The indexed path hashes each shader once per request and performs zero
    SetFrameEvent calls. It requires a driver recording shader-object usage,
    such as the user's custom D3D12 build. Standard cores may return unsupported.
    Check supported, complete, partial, coverage and diagnostics before treating
    a result as exhaustive. A bounded replay leaves replay at its last event;
    exceptions/cancellation never trigger restoration or another fallback.
    """
    params: dict[str, object] = {
        "stage": stage,
        "unique_only": unique_only,
        "method": method,
        "max_events": max_events,
        "time_budget_ms": time_budget_ms,
        "cursor": cursor,
    }
    if event_id_min is not None:
        params["event_id_min"] = event_id_min
    if event_id_max is not None:
        params["event_id_max"] = event_id_max
    if limit is not None:
        params["limit"] = limit
    return bridge.call("list_shader_hashes", params)


@mcp.tool
def list_captures(directory: str) -> dict:
    """
    List all RenderDoc capture files (.rdc) in the specified directory.

    Args:
        directory: The directory path to search for capture files

    Returns a list of capture files with their metadata including:
    - filename: The capture file name
    - path: Full path to the file
    - size_bytes: File size in bytes
    - modified_time: Last modified timestamp (ISO format)
    """
    return bridge.call("list_captures", {"directory": directory})


@mcp.tool
def open_capture(capture_path: str) -> dict:
    """
    Open a RenderDoc capture file (.rdc).

    Args:
        capture_path: Full path to the capture file to open

    Returns success status and information about the opened capture.
    Note: This will close any currently open capture.
    """
    return bridge.call("open_capture", {"capture_path": capture_path})


def main():
    """Run the MCP server"""
    mcp.run()


if __name__ == "__main__":
    main()
