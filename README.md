# RenderDoc MCP Server

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
list_shader_hashes(stage="all", unique_only=true)
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
