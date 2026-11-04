# RenderDoc MCP Server

## Bounded action context and resource provenance (`action-context-1`)

`get_action_context(event_id, before_actions=8, after_actions=2, max_events=128)`
locates one exact action by scanning only action nodes before reading selected
actions' API-event arrays. This avoids unrelated large queue/root event arrays.
Synthetic parent EIDs may be later than their children and never prune traversal.
The neighboring window is ordered by EID; it is not a proof of command-list
identity or submission ordering. Closest actions receive the shared event budget
first. Each selected action contributes a bounded tail of its `events`; omitted
earlier events are explicit. Typed SDChunk trees expose API arguments (including
indirect signatures/buffers/count buffers and offsets) while opaque blobs are
omitted. Optional captured call stacks contain at most 32 addresses, with no
symbol lookup.

`get_resource_provenance(resource_ids, max_chunks=32)` accepts 1–16 unique,
non-null `ResourceId::N` strings. It establishes exact resource identity through
`ReplayController.GetResources()`, then reads only linked `initialisationChunks`
and parent/derived resource IDs. The chunk limit is shared across requested
resources. Resource-creation descriptors can expose heap types and command
signature layouts; initialization metadata alone does not identify a game CPU
function or prove that a GPU producer was absent.

Both methods run entirely inside one official replay callback and never call
`SetFrameEvent`, read buffer/texture bytes, resolve symbols, or write captures.
Bounds cover action/resource/event work, SD child count/depth, elapsed time and
4 MiB ASCII JSON output. `complete` covers requested non-blob metadata only;
check `diagnostics`, per-array completion flags and optional call-stack status.
Native calls cannot be hard-cancelled. Native errors propagate and stop the
request; no retry, restoration, or alternate target is scheduled.

Before use, verify `get_bridge_info().feature_info.action_context`: revision
`action-context-1`, matching service revision, current class/method/module and
membership-service identities, and `dependencies_coherent=true`. Existing
shader/rasterizer reads now verify exact action membership with an action-only
fast path before their bounded APIEvent-only fallback. Existing pipeline,
shader extraction, and indexed search paths are otherwise unchanged.

Regression tests: `python -m unittest discover -s tests -v`. Deploy only changed
files after checking the original SHA-256 manifest, then use the official
`pyrenderdoc.Extensions().LoadExtension("renderdoc_mcp_bridge")` reload. Disk test
success and version strings do not establish that a live capture has loaded it.

RenderDoc UI拡張機能として動作するMCPサーバー。AIアシスタントがRenderDocのキャプチャデータにアクセスし、グラフィックスデバッグを支援する。

## アーキテクチャ

```
Claude/AI Client (stdio)
        │
        ▼
MCP Server Process (Python + FastMCP 2.0)
        │ File-based IPC (%TEMP%/renderdoc_mcp/)
        ▼
RenderDoc Process (Extension)
```

RenderDoc内蔵のPythonにはsocketモジュールがないため、ファイルベースのIPCで通信を行う。

## セットアップ

### 1. RenderDoc拡張機能のインストール

```bash
python scripts/install_extension.py
```

拡張機能は `%APPDATA%\qrenderdoc\extensions\renderdoc_mcp_bridge` にインストールされる。

### 2. RenderDocで拡張機能を有効化

1. RenderDocを起動
2. Tools > Manage Extensions
3. "RenderDoc MCP Bridge" を有効化

### 3. MCPサーバーのインストール

```bash
uv tool install
uv tool update-shell  # PATHに追加
```

シェルを再起動すると `renderdoc-mcp` コマンドが使えるようになる。

> **Note**: `--editable` を付けると、ソースコードの変更が即座に反映される（開発時に便利）。
> 安定版としてインストールする場合は `uv tool install .` を使用。

### 4. MCPクライアントの設定

#### Claude Desktop

`claude_desktop_config.json` に追加:

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp"
    }
  }
}
```

#### Claude Code

`.mcp.json` に追加:

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp"
    }
  }
}
```

## 使い方

1. RenderDocを起動し、キャプチャファイル (.rdc) を開く
2. MCPクライアント (Claude等) から RenderDoc のデータにアクセス

## MCPツール一覧

| ツール | 説明 |
|--------|------|
| `get_capture_status` | キャプチャの読み込み状態を確認 |
| `get_draw_calls` | ドローコール一覧を階層構造で取得 |
| `get_draw_call_details` | 特定のドローコールの詳細情報を取得 |
| `get_shader_info` | シェーダーのソースコード・定数バッファの値を取得 |
| `get_buffer_contents` | バッファの内容を取得 (Base64) |
| `get_texture_info` | テクスチャのメタデータを取得 |
| `get_texture_data` | テクスチャのピクセルデータを取得 (Base64) |
| `get_pipeline_state` | パイプライン状態を取得 |
| `get_output_merger_state` | 指定イベントのDSV/RTV・深度/ステンシル・ブレンド・UAV状態を取得 |
| `list_shader_hashes` | シェーダーバイトコードのCRC32 hashを一覧 |
| `find_resource_usage` | Texture/buffer usage history including Copy/Clear/Resolve |

## Resource search performance and portability (1.2)

Texture/buffer reverse lookups use upstream `ReplayController.GetUsage` and
`ActionDescription.outputs/depthOut` metadata. They do **not** call
`SetFrameEvent` for every draw. Requests are scheduled with upstream
`AsyncInvoke(callback)` after UI metadata has been copied, so the UI thread
is free while the replay queue handles the search.

```python
# Draw/dispatch uses only; default path performs no per-event replay.
find_draws_by_resource(resource_id="ResourceId::44880", max_results=1000)
find_draws_by_texture(texture_name="UI", event_id_min=100000)

# Full indexed history, including Copy/Clear/Resolve.
find_resource_usage(resource_id="ResourceId::44880")

# Shader objects have no portable upstream GetUsage support.
# An explicit event range is required; each request is bounded.
find_draws_by_shader(shader_name="main", stage="pixel",
                     event_id_min=108000, event_id_max=108928,
                     max_events=32, time_budget_ms=2000)
```

- `max_results`: 1–10000 (default 1000). Matches are grouped by event.
- `partial=true` means the page or replay budget ended. Resume using
  `event_id_min=next_event_id`, retaining the original upper bound.
  `total_matches` counts this page; it is not an estimate of unvisited events.
- Shader name/ID searches require both event bounds. `max_events` is 1–128,
  default 32; `time_budget_ms` is 1–10000, default 2000. These searches restore
  the UI-selected event. Time and cancellation limits are cooperative between
  calls; an already-running GPU replay or final restoration cannot be aborted.
- Usage indexes can omit dynamic/bindless descriptor ranges. Their results
  describe recorded uses, not every bound-but-unused descriptor or proof of
  per-pixel GPU access. `coverage` reports these limits; `(0, Unused)` is
  reported as an untracked resource, never as a real event 0. Exact descriptor
  slots require inspecting an individual event.
- `find_draws_by_resource` accepts shader IDs only via bounded replay. The new
  code never calls `GetUsage(shader_id)`, including on forks that implement it.
  PSO/root-signature and other non-texture/buffer IDs are not passed to GetUsage.
- API provenance was checked against upstream RenderDoc v1.39. No custom
  RenderDoc core APIs were added or required. In particular, the local fork's
  shader usage indexing is not used. Other pre-existing tools are outside this
  search change.

### Transport behavior

Requests/responses are published atomically. New clients use per-request
response files and a mailbox lock; the server still accepts legacy clients,
and the client can read legacy `response.json` replies. Timed-out requests get
an ID-specific cancellation marker; an in-flight request is not overwritten by
another client. A cancelled search stops at its next cancellation check.

A live or uncheckable process is retained even when its heartbeat is stale.
Only confirmed dead processes are pruned. Asynchronous searches leave UI
heartbeats running; unrelated existing synchronous tools retain their prior
execution model. Updating both server and extension is necessary for cooperative
cancellation and per-request transport behavior. New clients require the safe-search
capability before sending any reverse lookup to an instance; an older extension
is rejected with reload instructions instead of running its unbounded search.
Compatibility with legacy replies is retained for other tools, but an old
extension cannot honor the new cancellation protocol.

### Update and validate

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe scripts/install_extension.py
```

Restart the MCP client connection to refresh tool schemas. For an already-open
RenderDoc capture, reload the installed extension in its Python Shell:

```python
pyrenderdoc.Extensions().LoadExtension("renderdoc_mcp_bridge")
```

This uses upstream extension reload and does not require closing the capture.
Existing in-memory code is not replaced merely by copying files to disk.

## 使用例

### ドローコール一覧の取得

```
get_draw_calls(include_children=true)
```

### シェーダー情報の取得

```
get_shader_info(event_id=123, stage="pixel")
```

### パイプライン状態の取得

```
get_pipeline_state(event_id=123)
```

### シェーダーハッシュ一覧の取得

```
list_shader_hashes(stage="pixel", event_id_min=8000, event_id_max=9000)
list_shader_hashes(stage="all", unique_only=True, method="usage_index")
```

`list_shader_hashes` now uses **shader-hash-index-1**. On replay cores that record
shader-object usage (including the local D3D12 build), it enumerates that index
without `SetFrameEvent` and computes CRC32 once per unique shader per request.
The native capability is established from positive, stage-matching usage entries,
not a RenderDoc version string. `coverage` reports missing candidate events and
the limits of the recorded index; `complete` describes index traversal and paging,
not independent proof of every shader binding.

- `method="auto"` prefers the index. If unavailable, replay is permitted only with
  both explicit EID bounds. `method="usage_index"` never replays.
- `method="bounded_replay"` requires both bounds, with `max_events` 1–128 (default
  32) and `time_budget_ms` 1–10000 (default 5000). It skips unrelated compute or
  graphics actions, caches repeated shader hashes, and leaves replay at its last
  event. Native errors/cancellation never trigger restoration or another fallback.
- `limit` caps event-stage rows (default 10000), also in `unique_only=True` mode.
  A unique-only page aggregates only that page's rows. Resume with the exact
  `next_cursor` and unchanged query/capture; an EID alone can lose a VS/PS row when
  a page boundary splits the event. Inspect `supported`, `partial`, `complete`,
  `diagnostics`, and `coverage`; incomplete index traversal has no reliable cursor.
- Requests use the official replay queue asynchronously and cooperatively check
  cancellation and work budgets. Individual native calls are not hard-cancellable.
  Action/resource/usage/shader-byte/output limits bound local work and responses.
- The client requires the installed bridge's `shader_hash_enumeration=1` capability
  before sending this request, preventing accidental use of the old full-frame
  synchronous loop. `get_bridge_info().feature_info.shader_hashes` reports loaded
  revision and wiring identity independently of older inspection features.

For an upstream core without shader-object usage, use a bounded query, for example:

```python
list_shader_hashes(stage="pixel", method="bounded_replay",
                   event_id_min=8000, event_id_max=8100,
                   max_events=16, time_budget_ms=2000)
```

### 指定イベントのリソース取得

`get_texture_data` と `get_buffer_contents` は `event_id` を指定できる。
指定したイベントの実行直後まで移動してから、同一の replay callback 内でデータを読み出す。
事前の `get_pipeline_state` 呼び出しは不要。

```python
get_texture_data(event_id=1200, resource_id="ResourceId::22573", mip=0, slice=0, sample=0)
get_buffer_contents(event_id=1200, resource_id="ResourceId::12345", offset=256, length=512)
```

- `event_id` はキャプチャ内に存在する正の整数。draw/dispatch 以外の API イベントも指定可能。
- `resource_id` は `"ResourceId::12345"` または数値文字列 `"12345"`。
- 応答には読み出しに使用した `event_id` が含まれる。
- `event_id` を省略すると従来どおり現在の replay 状態を読み、応答の `event_id` は `null`。
  GUI の選択イベントと replay 状態は一致するとは限らないため、GUI の ID を代入しない。
- 読み出し後も replay は指定イベントに留まる。GUI の選択表示は変更しない。
- 同じイベントを連続して指定する場合は `SetFrameEvent(event_id, False)` で現在の状態を再利用する。
- buffer の `length=0` は `offset` から末尾まで。負の値や範囲外の読み出しはエラー。
- 内容は `content_base64` に入る生バイト列。テクスチャの PNG/DDS 変換は行わない。

MCP サーバーと RenderDoc 拡張の両方を更新する必要がある。

```powershell
uv pip install --python .venv/Scripts/python.exe --reinstall-package renderdoc-mcp -e . --no-deps
.venv/Scripts/python.exe scripts/install_extension.py
```

更新後、MCP クライアント側の接続を再起動する。起動中の RenderDoc では Python Shell から
`pyrenderdoc.Extensions().LoadExtension("renderdoc_mcp_bridge")` を実行すると、
キャプチャを閉じずに拡張を再読み込みできる。RenderDoc が起動していなければ次回起動時に反映される。

### テクスチャデータの取得

```
# 2Dテクスチャのmip 0を取得
get_texture_data(resource_id="ResourceId::123")

# 特定のmipレベルを取得
get_texture_data(resource_id="ResourceId::123", mip=2)

# キューブマップの特定の面を取得 (0=X+, 1=X-, 2=Y+, 3=Y-, 4=Z+, 5=Z-)
get_texture_data(resource_id="ResourceId::456", slice=3)

# 3Dテクスチャの特定の深度スライスを取得
get_texture_data(resource_id="ResourceId::789", depth_slice=5)
```

### バッファデータの部分取得

```
# バッファ全体を取得
get_buffer_contents(resource_id="ResourceId::123")

# オフセット256から512バイト取得
get_buffer_contents(resource_id="ResourceId::123", offset=256, length=512)
```

## 要件

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- RenderDoc 1.20+

> **Note**: 既存ツールはWindows + DirectX 11で動作確認済み。1.2の索引検索と非同期経路はWindows + DirectX 12の実キャプチャでも検証しました。
> Linux/macOS + Vulkan/OpenGL環境でも動作する可能性がありますが、未検証です。

## ライセンス

MIT


## Output-merger state at a specific event

Use the focused query for a D3D11/D3D12 draw or an API state-setting event:

```python
snapshot = get_output_merger_state(event_id=677)
om = snapshot["output_merger"]
depth_state = om["depth_stencil"]
dsv = om["depth_target"]
blend = om["blend"]

# Read depth pixels immediately after the SAME event.
# Select each required slice and MSAA sample explicitly.
if dsv is not None:
    pixels = get_texture_data(
        event_id=snapshot["event_id"],
        resource_id=dsv["resource_id"],
        mip=dsv["first_mip"],
        slice=dsv["first_slice"],
        sample=0,
    )
```

`get_pipeline_state(event_id=677)` includes the identical `output_merger`
snapshot while retaining its existing top-level `depth_target` and
`render_targets` fields. Use the focused tool to avoid reading shader bindings
and constant buffers when only OM is needed.

| Field | Contents |
|---|---|
| `depth_target` | Null when unbound; otherwise resource/view IDs, view format, descriptor flags, mip/slice range, read-only depth/stencil flags, and underlying texture dimensions/format/sample count |
| `render_targets` | RTV descriptors with explicit slots; unbound slots remain present with `bound=false` |
| `depth_stencil` | Depth enable/write/compare function, read-only flags, front/back stencil operations, reference, compare/write masks; D3D12 depth bounds |
| `blend` | State object ID when applicable, alpha-to-coverage, independent blending, blend factor, sample mask, per-target color/alpha equations, logic operations, write mask and RGBA channels |
| `uav_start_slot` | D3D11 OM UAV start slot |
| `uavs` | D3D11 bound pixel UAV slots or D3D12 pixel shader descriptor accesses, with an explicit `scope` |

Semantics and limits:

- Both pipeline tools require a positive integer event ID. Missing, non-existent,
  boolean, fractional and string IDs are rejected. Nested actions and API events
  between draws (for example `OMSetRenderTargets`) are valid.
- Event selection and snapshot occur in one replay callback, immediately **after**
  the requested event. The UI selection is unchanged; the replay controller
  stays at that event, matching explicit-event texture/buffer reads. Always pass
  the same `event_id` to later pixel reads instead of depending on shared state.
- `depth_write` is the raw depth write flag. `depth_write_allowed` combines a
  bound DSV, enabled depth test/write, writable depth aspect, and a comparison
  other than Never. It does **not** prove a fragment passed coverage, stencil,
  depth bounds, sample masking or other tests, nor that any pixel changed.
- DSV `format` describes the **view**; `depth_target.texture.format` describes
  the resource (which can be typeless). Use `resource_id`, not `view_id`, for
  `get_texture_data`. A multi-slice or MSAA DSV may require multiple pixel reads.
- `blend.targets` contains the raw reported state entries. If `independent_blend`
  is false, `effective_blend_slot` is 0 for every entry; interpret effective
  blending using `targets[effective_blend_slot]`, including its write mask.
- D3D11 `uavs.scope=all_bound_pixel_slots` includes bound OM UAVs even when the
  current shader does not declare them, using the descriptor-store API.
  D3D12 `uavs.scope=pixel_shader_accesses` covers the shader access list, not
  every heap descriptor. `access.index` is a reflection index; `binding.register`
  and `binding.register_space` identify the shader register. Direct heap accesses
  without a reflected binding have `binding=null`. Neither access-list membership
  nor `statically_unused=false` proves an actual GPU write.
- Unsupported graphics APIs return `output_merger.supported=false` and a reason.
  Descriptor API failures are exposed as `uavs_error`; full pipeline queries
  expose OM snapshot failures as `output_merger_error` rather than silently
  returning incomplete OM state.

After updating, copy/reinstall the extension and reload it in RenderDoc, then
reconnect the MCP client so its tool list refreshes. For an already-open capture,
the RenderDoc Python Shell can reload it without closing the capture:

```python
pyrenderdoc.Extensions().LoadExtension("renderdoc_mcp_bridge")
```

Regression checks: `python -m unittest discover -s tests -v`.

### Bounded shader inspection

```python
get_bridge_info()
get_constant_buffers(event_id=2479, stage="vertex", max_bytes=1048576)
get_constant_buffers(event_id=2479, stage="pixel", block_index=0)
get_postvs_data(event_id=2479, instance=0, view=0, stage="vsout", max_bytes=8388608)
```

- `get_bridge_info().inspection_revision == "shader-inspection-2"` identifies
  the **loaded** facade, but that string alone is insufficient. All five
  `component_revisions` must match, and `dependencies_coherent`,
  `pipeline_instance_current` and `serializer_module_current` must be true.
  The response also returns the loaded module path. Disk installation alone
  does not establish that either open RenderDoc instance has reloaded it.
- CB queries return real decoded values (recursive arrays/structs/matrices) plus
  reflection-sized raw bytes with resource ID, offset, requested and actual size.
  A shared `max_bytes` budget is 1..8 MiB. `block_index` is a reflection index,
  not register bN. Omitting it selects up to 128 blocks, **one** array element
  per block; other elements require an explicit block index. Missing data,
  zero-sized reflection, smaller bindings and budget limits are never reported
  as complete. Null-resource root/push constants still call the official
  `GetCBufferVariableContents`; raw-buffer bytes are unavailable for them.
- `values_complete` means all returned variables were decoded; `complete`
  permits complete raw bytes as a fallback. Reflection is authoritative for type
  interpretation; this tool does not guess matrix names or reinterpret UInt as
  Float. Nonfinite floats are JSON strings `NaN`, `+Infinity`, `-Infinity`;
  `value_bits` retains the shader value's bits. Matrix values are row-major as
  returned by RenderDoc, not necessarily their raw memory order.
- Pipeline SRVs retain legacy `format` (storage format), and add `resource_format`
  and `view_format` (actual descriptor typed format, including sRGB). Legacy
  `slot` is explicitly a reflection index; use `binding.fixed_bind_number`,
  `fixed_bind_set_or_space` and `array_element` for API-specific bindings, never
  assume the reflection index is tN. Samplers use `UsedDescriptor.sampler` and
  report filter modes, addressing, mip bias/LOD, comparison and typed border
  color. Descriptor membership is not proof that a pixel sampled the resource.
- PostVS accepts an exact draw event, a zero-based instance (not instanceOffset),
  and an in-range multiview view. It makes one replay selection and one
  `GetPostVSData` call, returning actual MeshFormat properties and exact bounded
  raw ranges. Indices + baseVertex (excluding restart) determine the vertex
  interval; unreferenced gaps are preserved. The limit caps readback, not the
  driver's internal cost of generating output for that one draw. Derived replay
  buffers may not appear in GetBuffers; range provenance and short reads are
  explicit. Unsupported APIs/stages/status return `supported=false` or
  `complete=false` plus a reason, never guessed mesh data.
- Post-transform positions are shader output, often clip/post-projection space,
  **not world space**. No unprojection or other geometry transform is performed.
  All these tools are capture-read-only. Replay remains at the chosen event;
  the GUI selection is unchanged. They never close or replace a capture.

For a live capture update, back up the changed source/installed files and their
SHA-256 hashes, copy **only** changed files (do not use the directory-replacing
installer during a live update), and run the regression suite. Wait for all MCP
requests to finish and disconnect the client, then run the official
`pyrenderdoc.Extensions().LoadExtension("renderdoc_mcp_bridge")` in **each**
open instance's Python Shell. `unregister` refuses an active/pending request;
never force a reload around that guard. Never clear `sys.modules` or reload
guessed internal modules. Reconnect the MCP client and verify all the dependency
fields above, followed by actual typed SRV/sampler/CB results. Unit tests fake
the GPU boundary; they do not substitute for real MCP capture validation.

The inspected upstream `PythonContext.cpp` loader reloads extension submodules
in module-table enumeration order, not dependency order. A parent package's
`from child import Class` can therefore keep an old class after the child has
reloaded, even when the facade reports a new revision. Inspection services and
helpers now retain module objects and resolve their current attributes at use
time. The extension does **not** perform additional `importlib.reload` calls.
`tests/test_extension_reload.py` reproduces stale aliases and verifies insertion,
parent-first and child-first reload orders in isolated fake packages; this does
not assert the exact implementation of a particular installed RenderDoc binary.

For collection clients, treat dependency coherence, decoded CB values, raw-byte
integrity, typed SRV/sampler metadata and PostVS as independent evidence gates.
A **local validation** failure after a successful request must remain incomplete,
but need not prevent a different bounded read. Any timeout/transport/API exception
must stop the request sequence instead of queueing more replay work. The project
collector `Scripts/RenderReplica/verify_mcp_inspection.py --collect --resume-from
<prior-evidence-directory>` reuses known E2479 CB/pipeline evidence, rechecks every
binary length/SHA-256 from disk, and reads only missing E2479 PostVS plus E1550 CB.
It writes a new directory, allows only the explicitly reviewed revision-1 recovery
surface, never retries its known-stale pipeline, and cannot declare the full
revision-2 evidence set complete. Raw-complete is not decoded-values-complete or
world-space reconstruction; the normal `--collect` mode still requires coherent
revision-2 dependencies before issuing replay reads.

### Bounded draw metadata (`draw-metadata-3`)

Three separate read-only tools fill gaps without expanding the existing pipeline
or shader-info queries:

```python
info = get_bridge_info()
# Check feature_info.draw_metadata availability, all three draw-metadata-3
# component revisions, module/class identities and coherence before new calls.
get_rasterizer_state(event_id=2479)
get_shader_artifact(event_id=2479, stage="vertex", max_bytes=1048576)
get_shader_artifact(event_id=2479, stage="pixel", max_bytes=1048576)
get_api_events(event_id_min=2464, event_id_max=2479, max_events=128)
```

- `get_rasterizer_state` supports D3D12 and returns actual rasterizer fields,
  viewports/scissors, `sampleMask`, exposed float bits and separately proven PSO
  sampling. Missing API members are reported, not replaced by defaults.
  Zero-sized viewport/scissor entries are retained. Revision 3 removes the old
  `outputMerger.multiSampleCount/multiSampleQuality` output: these are **not**
  native `D3D12OM` fields. Use `sampling` below, not texture `msSamp/msQual`.
- `get_shader_artifact` reads one VS or PS reflection, original `rawBytes`,
  `reflection.entryPoint`, encoding, identity, CRC32, input/output signatures and
  actual disassembly targets. Default `disassembly_target=None` performs **no
  disassembly**. An explicitly selected available target is attempted once,
  without fallback. `max_bytes` and `max_text_bytes` are each 1..1048576;
  oversized payloads remain incomplete. The external caller computes SHA256
  from decoded original bytes. Reflection signatures do **not** contain
  interpolation modes; `_centroid` in a semantic name proves no such mode.
- `get_api_events` reads official action/APIEvent/structured-chunk metadata, not
  a per-EID replay scan. Inclusive ranges contain at most 256 IDs and return at
  most 128 events. An event's owning action may be outside the requested range.
  Node/time traversal budgets, missing chunks, duplicates/conflicts and output
  limits are explicit. Incomplete traversal does not establish global sorting,
  complete coverage or a reliable next-page cursor. An event ID is not itself
  proof of an exact before-draw replay boundary; `fileOffset` is not an absolute
  offset into the capture file.

#### PSO sampling contract

Within the **same** official replay callback, after exact event membership and
one `SetFrameEvent(event_id, False)`, the rasterizer tool follows:

`D3D12State.pipelineResourceId` -> `ReplayController.GetResources()` -> exact,
unique `ResourceDescription.resourceId` -> `initialisationChunks` ->
`ReplayController.GetStructuredFile().chunks[index]` -> PSO creation fields.

Only these upstream structured shapes are accepted (names are literal):

| `chunk_name` | Sample descriptor path | Required identity field |
|---|---|---|
| `ID3D12Device::CreateGraphicsPipeline` | `pDesc.SampleDesc` | `pPipelineState` |
| `ID3D12Device2::CreatePipelineState` | `pDesc.SampleDesc` | `pPipelineState` |

The first name intentionally has **no `State` suffix**, unlike the C++ API
method. `pPipelineState` must have `SDBasic.Resource` type before `AsResourceId`
is called, and must equal the selected non-null PSO. Both direct `Count` and
`Quality` children must be childless `SDBasic.UnsignedInteger`, `byteSize=4`,
with strict uint32 `AsInt()` results; Count must be positive. Enum equality,
not the spelling of `str(enum)`, produces the reported `UnsignedInteger` names.

The result contains:

```text
sampling = {
  complete: bool,
  source: "D3D12 PSO initialisation chunks",
  pipeline_resource_id: str | null,
  sample_count: int | null,
  sample_quality: int | null,
  provenance: [{
    chunk_index: int, chunk_name: str, sample_desc_path: "pDesc.SampleDesc",
    count_type: "UnsignedInteger", quality_type: "UnsignedInteger",
    pipeline_resource_path: "pPipelineState", pipeline_resource_id: str
  }],
  resource_description_index: int,  # only after exact unique resource linkage
  traversal: { ...counts, limits, completion, elapsed time and stop reason... }
}
```

No global structured-file scan, arbitrary nested `SampleDesc` search, blob read,
per-event replay scan, texture substitution or guessed default is used. Direct
child lookup uses bounded `NumChildren/GetChild`, checks duplicates before
selecting a field, and follows only the allowlisted path. Every linked chunk
must be recognized and all verified creation records must agree. Missing,
invalid, duplicate, unknown (including unrecognized auxiliary initialization
chunks), conflicting or over-budget metadata leaves **both sample values null**,
`sampling.complete=false`, top-level `complete=false`, and diagnostic paths
under `sampling`. Partial provenance may describe inspected candidates, never
successful sampling. A valid sampling sub-result can coexist with another
incomplete rasterizer field; check top-level completeness too.

Sampling limits are 32768 resource descriptions, 64 linked chunk indices,
128 direct children per object, 4096 child visits, depth 3 (chunk=0), and 250 ms
cooperative elapsed time. Oversized resource/chunk lists are rejected before
item traversal. Traversal evidence includes visited counts, scan-completion
flags, limits, elapsed time, stop reason, `global_chunk_scan_performed=false`
and `native_calls_hard_cancellable=false`. The existing 4096-byte string and
4 MiB total ASCII-JSON bounds still apply; exceeding the latter returns an
explicit `output_omitted` failure rather than the original payload.
`complete_scope` explicitly covers requested rasterizer/PSO metadata, **not
shader execution or image equivalence**.

Serialization provenance was checked against official upstream `v1.x` sources:

- [d3d12_stringise.cpp](https://github.com/baldurk/renderdoc/blob/v1.x/renderdoc/driver/d3d12/d3d12_stringise.cpp): literal chunk names.
- [d3d12_device_wrap.cpp](https://github.com/baldurk/renderdoc/blob/v1.x/renderdoc/driver/d3d12/d3d12_device_wrap.cpp) and [d3d12_device_wrap2.cpp](https://github.com/baldurk/renderdoc/blob/v1.x/renderdoc/driver/d3d12/d3d12_device_wrap2.cpp): named `pDesc`, typed `pPipelineState` and PSO resource linkage.
- [d3d12_serialise.cpp](https://github.com/baldurk/renderdoc/blob/v1.x/renderdoc/driver/d3d12/d3d12_serialise.cpp): direct `SampleDesc` in graphics and expanded-stream descriptors.
- [dxgi_common.cpp](https://github.com/baldurk/renderdoc/blob/v1.x/renderdoc/driver/dxgi/dxgi_common.cpp): direct `DXGI_SAMPLE_DESC.Count/Quality` serialization.
- [ResourceDescription](https://renderdoc.org/docs/python_api/renderdoc/resources.html) and [structured data API](https://renderdoc.org/docs/python_api/renderdoc/structured_data.html): `initialisationChunks`, `NumChildren/GetChild`, typed conversion contracts.

These sources corroborate the implementation's allowlist; they do **not** prove
that the current capture or a locally reported 1.47 binary exposes those shapes.
**Live revision-3 sampling proof remains unavailable until the user's official
reload and bounded validation.** Tests only exercise real-shaped fakes.

#### Execution and update boundaries

Integers reject strings, floats and booleans. String/signature/array/total-JSON
limits supplement the raw/text limits. `IsCaptureLoaded` runs on the UI thread;
`GetRootActions`, `GetResources` and `GetStructuredFile` use ReplayController
receivers for these tools and are traversed inside one official replay callback.
Raster/shader membership and state reads share that callback. API events use
one metadata-only callback with **zero SetFrameEvent**, not a per-event scan.
Native metadata and retained getters stay in the callback. Revision 2 corrected
revision 1's invalid CaptureContext receiver; revision 3 preserves that rule.
Tests reject wrong receivers/threads and the fictitious OM sampling properties.
These bounds cannot hard-cancel one native getter, disassembly or `BlockInvoke`,
or cap its internal GPU cost. Native/API exceptions terminate and propagate;
timeout/transport/API failures stop the sequence without restore, retry, changed
targets or additional queued reads. No texture/buffer data, additional shader
stages, capture writes or resource replacements are involved. Check
supported/complete/diagnostics before requiring success-only fields: unavailable
membership is not a change of graphics API.

The feature has its own revision and module/class coherence checks under
`feature_info.draw_metadata`. Existing `features` remains a list and the old
five-component `shader-inspection-2` contract remains unchanged. Old pipeline,
shader-inspection and search execution paths are unchanged.

Use SHA-guarded backups and deploy only changed extension files after tests pass
and all requests/clients have ended. Reload only the target instance that needs
the new tools using the official command, then refresh the MCP client connection
and verify both feature gates. Do not force the busy-unregister guard or repeatedly
reload a failing request. Unit tests and successful copying do not establish live
capture validation, original VS/PS execution, interpolation or image equivalence.
