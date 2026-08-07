"""RenderDoc MCP Bridge Extension."""

from . import renderdoc_facade
from . import request_handler
from . import socket_server


_context = None
_server = None
_version = ""

try:
    import qrenderdoc as qrd

    _has_qrenderdoc = True
except ImportError:
    _has_qrenderdoc = False


def register(version, ctx):
    """Called when the extension is loaded."""
    global _context, _server, _version
    _version = version
    _context = ctx

    facade = renderdoc_facade.RenderDocFacade(ctx)
    handler = request_handler.RequestHandler(facade)

    ui_invoker = None
    if not socket_server.HAS_QT:
        ui_invoker = ctx.Extensions().GetMiniQtHelper().InvokeOntoUIThread

    def _instance_info():
        """Identity published in info.json for instance discovery."""
        info = {"renderdoc_version": version, "loaded": False, "filename": None}
        try:
            if ctx.IsCaptureLoaded():
                info["loaded"] = True
                info["filename"] = ctx.GetCaptureFilename()
        except Exception:
            pass
        return info

    _server = socket_server.MCPBridgeServer(
        host="127.0.0.1",
        port=19876,
        handler=handler,
        ui_invoker=ui_invoker,
        info_provider=_instance_info,
    )
    _server.start()

    if _has_qrenderdoc:
        try:
            ctx.Extensions().RegisterWindowMenu(
                qrd.WindowMenu.Tools, ["MCP Bridge", "Status"], _show_status
            )
        except Exception as exc:
            print("[MCP Bridge] Could not register menu: %s" % str(exc))

    print("[MCP Bridge] Extension loaded (RenderDoc %s)" % version)
    print("[MCP Bridge] File IPC ready at %s" % _server.instance_dir)


def unregister():
    """Called when the extension is unloaded."""
    global _server
    if _server:
        _server.stop()
        _server = None
    print("[MCP Bridge] Extension unloaded")


def _show_status(ctx, data):
    if _server and _server.is_running():
        ctx.Extensions().MessageDialog(
            "MCP Bridge is running.\nInstance mailbox: %s" % _server.instance_dir,
            "MCP Bridge Status",
        )
    else:
        ctx.Extensions().ErrorDialog(
            "MCP Bridge is not running", "MCP Bridge Status"
        )
