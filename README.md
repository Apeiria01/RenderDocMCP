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

> **Note**: 動作確認はWindows + DirectX 11環境でのみ行っています。
> Linux/macOS + Vulkan/OpenGL環境でも動作する可能性がありますが、未検証です。

## ライセンス

MIT
