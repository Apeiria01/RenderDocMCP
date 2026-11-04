# RenderDoc MCP Server

RenderDoc UI拡張機能として動作するMCPサーバー。AIアシスタントがRenderDocのキャプチャデータにアクセスし、DirectX 11/12のグラフィックスデバッグを支援する。

## アーキテクチャ

**ハイブリッドプロセス分離方式**:

```
Codex/AI Client (stdio)
        │
        ▼
MCP Server Process (標準Python + FastMCP 2.0)
        │ File-based IPC (%TEMP%/renderdoc_mcp/)
        ▼
RenderDoc Process (Extension + File Polling)
```

## プロジェクト構成

```
RenderDocMCP/
├── mcp_server/                        # MCPサーバー
│   ├── server.py                      # FastMCPエントリーポイント
│   ├── config.py                      # 設定
│   └── bridge/
│       └── client.py                  # ファイルベースIPCクライアント
│
├── renderdoc_extension/               # RenderDoc拡張機能
│   ├── __init__.py                    # register()/unregister()
│   ├── extension.json                 # マニフェスト
│   ├── socket_server.py               # ファイルベースIPCサーバー
│   ├── request_handler.py             # リクエスト処理
│   └── renderdoc_facade.py            # RenderDoc APIラッパー
│
└── scripts/
    └── install_extension.py           # 拡張機能インストール
```

## MCPツール

| ツール名 | 説明 |
|---------|------|
| `list_captures` | 指定ディレクトリ内の.rdcファイル一覧を取得 |
| `open_capture` | キャプチャファイルを開く（既存キャプチャは自動で閉じる） |
| `get_capture_status` | キャプチャ読込状態確認 |
| `get_draw_calls` | ドローコール一覧（階層構造、フィルタリング対応） |
| `get_frame_summary` | フレーム全体の統計情報（ドローコール数、マーカー一覧等） |
| `find_draws_by_shader` | シェーダー名でドローコールを逆引き検索 |
| `find_draws_by_texture` | テクスチャ名でドローコールを逆引き検索 |
| `find_draws_by_resource` | リソースIDでドローコールを逆引き検索 |
| `find_resource_usage` | Texture/buffer の Copy/Clear/Resolve を含む使用履歴 |
| `get_draw_call_details` | 特定ドローコールの詳細 |
| `get_action_timings` | アクションのGPU実行時間を取得 |
| `get_shader_info` | シェーダーソース/定数バッファ実値（有界） |
| `get_bridge_info` | 読み込み済み拡張の inspection revision と module path |
| `get_constant_buffers` | 単一イベントの定数実値/root constants と有界 raw bytes |
| `get_postvs_data` | 単一 draw/instance/view の MeshFormat と有界 PostVS bytes |
| `get_buffer_contents` | バッファデータ取得（オフセット/長さ指定可） |
| `get_texture_info` | テクスチャメタデータ |
| `get_texture_data` | テクスチャピクセルデータ取得（mip/slice/3Dスライス対応） |
| `get_pipeline_state` | パイプライン状態全体 |
| `list_shader_hashes` | シェーダーバイトコードのCRC32 hash一覧 |

### get_draw_calls フィルタリングオプション

```python
get_draw_calls(
    include_children=True,      # 子アクションを含める
    marker_filter="Camera.Render",  # このマーカー配下のみ取得
    exclude_markers=["GUI.Repaint", "UIR.DrawChain"],  # 除外するマーカー
    event_id_min=7372,          # event_id範囲の開始
    event_id_max=7600,          # event_id範囲の終了
    only_actions=True,          # マーカーを除外（ドローコールのみ）
    flags_filter=["Drawcall", "Dispatch"],  # 特定フラグのみ
)
```

### キャプチャ管理ツール

```python
# ディレクトリ内のキャプチャファイルを列挙
list_captures(directory="D:\\captures")
# → {"count": 3, "captures": [{"filename": "game.rdc", "path": "...", "size_bytes": 12345, "modified_time": "..."}, ...]}

# キャプチャファイルを開く（既存キャプチャは自動で閉じられる）
open_capture(capture_path="D:\\captures\\game.rdc")
# → {"success": true, "filename": "game.rdc", "api": "D3D11"}
```

### 逆引き検索ツール

```python
# シェーダー名で検索（部分一致）
find_draws_by_shader(shader_name="Toon", stage="pixel",
                     event_id_min=100, event_id_max=500, max_events=32)

# テクスチャ名で検索（部分一致）
find_draws_by_texture(texture_name="CharacterSkin")

# リソースIDで検索（完全一致）
find_draws_by_resource(resource_id="ResourceId::12345")
```

### GPU タイミング取得

```python
# 全アクションのタイミングを取得
get_action_timings()
# → {"available": true, "unit": "CounterUnit.Seconds", "timings": [...], "total_duration_ms": 12.5, "count": 150}

# 特定のイベントIDのみ取得
get_action_timings(event_ids=[100, 200, 300])

# マーカーでフィルタリング
get_action_timings(marker_filter="Camera.Render", exclude_markers=["GUI.Repaint"])
```

**注意**: GPUタイミングカウンターはハードウェア/ドライバーによっては利用できない場合があります。
`available: false` が返された場合、そのキャプチャではタイミング情報を取得できません。

## 通信プロトコル

ファイルベースIPC:
- IPCディレクトリ: `%TEMP%/renderdoc_mcp/`
- `request.json`: リクエスト（MCPサーバー → RenderDoc）
- `response.json`: レスポンス（RenderDoc → MCPサーバー）
- `lock`: 書き込み中ロックファイル
- ポーリング間隔: 100ms（RenderDoc側）

## 開発ノート

- RenderDoc内蔵Pythonにはsocket/QtNetworkモジュールがないため、ファイルベースIPCを採用
- RenderDoc拡張機能はPython 3.6標準ライブラリのみ使用
- CB/PostVS は単一イベントの `BlockInvoke` 内で取得し、整数を暗黙変換しない。raw read の length=0（全体取得）は禁止、共有上限は最大8 MiB。root constants の null resource をスキップせず公式 API で照会し、値取得失敗は明示する。
- SRV の storage format と descriptor view format を分離する。`access.index` は reflection index、sampler は `UsedDescriptor.sampler`。PostVS を world-space と称さない。
- ライブ更新は既存 source/installed ファイルと SHA-256 をバックアップし、変更ファイルのみ配置する。MCP リクエスト完了・client 終了後、公式 `pyrenderdoc.Extensions().LoadExtension("renderdoc_mcp_bridge")` を各 Python Shell で実行する。`unregister` は処理中なら拒否する。`sys.modules` 操作や capture の再起動は不要。
- 上流の公式 loader は子 module を列挙順で reload し、依存順を保証しない。package の `from ... import Class` は旧 class を保持し得るため、inspection の service/helper は module オブジェクト経由で呼出時の属性を参照する。本拡張から追加の `importlib.reload` は行わない。
- `get_bridge_info` は facade の文字列だけで判定しない。`shader-inspection-2`、全 `component_revisions` 一致、`dependencies_coherent` と二つの identity フラグを確認後、typed SRV・sampler・CB の実結果を検証する。reload 順序テストは隔離 fake package のみを使用し、ライブ検証の代用にしない。
- 収集のローカル検証失敗は独立 evidence gate に incomplete と記録し、他の有界読出しを継続できる。timeout/transport/API エラーなら直ちに停止し、追加 replay を enqueue しない。再開時は既知不良の pipeline を再要求せず、旧 CB/raw の長さ・SHA-256 を再検証して参照する。raw 完全性と decoded values 完全性は別に報告する。
- ReplayControllerへのアクセスは公式の replay queue 経由で行う。検索は UI メタデータをスナップショットして `AsyncInvoke(callback)`、既存の単発読み出しは `BlockInvoke`。
- Texture/buffer 逆引きは上流の `GetUsage` と action metadata を使い、全 draw の `SetFrameEvent` 走査は禁止。Shader の `GetUsage` は改造版固有。`list_shader_hashes` の `shader-hash-index-1` は正の stage-matching usage で利用可否を検証し、記録 index の coverage と traversal completeness を区別する。他の portable shader 検索および index 非対応時の fallback は明示範囲・件数・時間上限付きにする。version 文字列だけで index 完全性を断定しない。

## Bounded draw metadata (`draw-metadata-3`)

- `get_rasterizer_state(event_id)`, `get_shader_artifact(event_id, stage, max_bytes, disassembly_target, max_text_bytes)`, and `get_api_events(event_id_min, event_id_max, max_events)` are separate read-only tools. Keep the old five-component `shader-inspection-2` gate and `features` list compatible; check all three `feature_info.draw_metadata` revisions and module/class identities independently, not only a revision string.
- Extension code stays Python 3.6 stdlib-only. Instantiate `DrawMetadataService` via the current module at call time; reference helpers (including `SamplingReader`) through module attributes. Do not modify the old pipeline/shader/search execution paths. CRC32 uses existing zlib; compute SHA256 in the external acquisition checker, not an unverified embedded hashlib dependency. Shader entry points already come from `reflection.entryPoint`.
- Rasterizer sampling is `result.sampling`, never fictitious `D3D12OM.multiSampleCount/multiSampleQuality` properties. Omit those old `outputMerger` keys. Keep rasterizer state, zero-sized viewports/scissors, `sampleMask` and exposed float bits unchanged. `complete_scope` denotes requested metadata, not shader execution or image equivalence.
- Within the SAME official replay callback after exact membership and one `SetFrameEvent`: read `state.pipelineResourceId`, `ReplayController.GetResources()` and the exact unique `ResourceDescription.resourceId`, then its `initialisationChunks` and `ReplayController.GetStructuredFile().chunks[index]`. Never use CaptureContext receivers, scan the global SDFile, replay per event, read blobs/buffers, or substitute texture `msSamp/msQual` or defaults.
- The only accepted chunk names are `ID3D12Device::CreateGraphicsPipeline` (intentionally no `State` suffix) and `ID3D12Device2::CreatePipelineState`. Both use direct `pDesc.SampleDesc.Count/Quality`; require direct typed Resource `pPipelineState` to equal the selected non-null PSO. Use bounded `NumChildren/GetChild`, detect duplicate names at each visited parent, and reject arbitrary nested matches. Count/Quality must be childless `SDBasic.UnsignedInteger`, `byteSize=4`, and strict Python uint32 values (Count > 0). Report type names as exactly `UnsignedInteger` by enum equality, not `str(enum)` parsing.
- Preserve `sampling={complete,source:'D3D12 PSO initialisation chunks',pipeline_resource_id,sample_count,sample_quality,provenance,traversal}`. Provenance includes exact chunk index/name, `sample_desc_path:'pDesc.SampleDesc'`, Count/Quality types, and `pPipelineState` resource linkage. Missing, invalid, duplicate, unknown (including unrecognized auxiliary chunks), conflicting or over-budget metadata leaves both values null, sampling incomplete and the top-level result incomplete with `sampling.*` diagnostics. Partial provenance is evidence of inspected candidates, not a success claim.
- Sampling limits: 32768 resources, 64 linked chunks, 128 direct children per object, 4096 child visits, depth 3 (chunk=0), cooperative elapsed 250 ms; strings stay bounded and total ASCII JSON stays <=4 MiB or the payload is explicitly omitted. Include traversal counts/limits/completion and `native_calls_hard_cancellable:false`. A resource list over its limit is not partially accepted, even if its first entry matches.
- The allowlist was checked against official upstream `v1.x` `d3d12_stringise.cpp`, `d3d12_device_wrap.cpp`, `d3d12_device_wrap2.cpp`, `d3d12_serialise.cpp`, and `dxgi_common.cpp` (links in README). These sources corroborate the serialization contract, not the current capture or a locally reported 1.47 runtime. Live sampling proof remains unavailable until the user's official reload and bounded validation.
- Raw shader bytes and optional disassembly text are each capped at 1 MiB. Validate length before copying/base64; bound signatures, strings, viewport/scissor arrays and JSON. Default disassembly is disabled; an explicit available target is tried once. Reflection signatures have no interpolation mode: never infer it from semantic names.
- API-event ranges are inclusive, at most 256 EIDs and 128 returned events. Bound traversal work/time, not only returned rows; owning actions may lie outside the range. Budget exhaustion, missing chunks and conflicts must stay explicit; incomplete traversal cannot promise global order/coverage or a reliable page cursor. Do not scan by replaying every event.
- `IsCaptureLoaded` is checked on the UI thread. `GetRootActions`, `GetResources` and `GetStructuredFile` belong to ReplayController for these tools: read/traverse/serialize inside one official replay callback. Raster/shader membership and state reads share that callback; API events use one metadata-only callback with zero SetFrameEvent. Fakes must reject the wrong receiver, metadata access/retained getter calls outside the callback, and the old fictitious OM sampling properties. Revision 1's incorrect UI receiver is not a compatibility fallback.
- Output/traversal budgets cannot interrupt a single native getter or BlockInvoke. A timeout/API/transport error stops further requests, with no restoration, retry, alternate target or hidden fallback. Native exceptions terminate and propagate, never become partial success. No texture/buffer reads, shader replacement or capture writes. Collection clients check supported/complete/diagnostics before requiring success-only fields; missing membership is not a graphics API change.
- Run `tests/test_draw_metadata.py` and the isolated reload-order tests plus the full regression suite before deploying only changed files. Do not call new tools against an old loaded instance. Backups and disk tests do not prove the live feature is loaded or that native fields are available.

## 参考リンク

- [FastMCP](https://github.com/jlowin/fastmcp)
- [RenderDoc Python API](https://renderdoc.org/docs/python_api/index.html)
- [RenderDoc Extension Registration](https://renderdoc.org/docs/how/how_python_extension.html)
