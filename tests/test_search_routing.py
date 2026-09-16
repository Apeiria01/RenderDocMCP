"""Search API routing and UI/replay thread ownership regression tests."""
import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_extension():
    name = '_search_routing_test_extension'
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT / 'renderdoc_extension')]
    rd = types.ModuleType('renderdoc')
    with patch.dict(sys.modules, {name: package, 'renderdoc': rd}):
        facade = importlib.import_module(name + '.renderdoc_facade')
        handler = importlib.import_module(name + '.request_handler')
        control = importlib.import_module(name + '.request_control')
    return facade, handler, control


class Context:
    def __init__(self):
        self.ui = True
        self.loaded = True
        self.queued = []
        self.resource = types.SimpleNamespace(resourceId='ResourceId::7', name='native')

    def _ui_only(self):
        if not self.ui:
            raise AssertionError('CaptureContext accessed from replay thread')

    def IsCaptureLoaded(self):
        self._ui_only()
        return self.loaded

    def CurEvent(self):
        self._ui_only()
        return 42

    def GetResources(self):
        self._ui_only()
        return [self.resource]

    def GetResourceName(self, rid):
        self._ui_only()
        return 'custom UI texture'

    def Replay(self):
        self._ui_only()
        return self

    def AsyncInvoke(self, callback):
        self._ui_only()
        self.queued.append(callback)

    def BlockInvoke(self, callback):
        raise AssertionError('Search must not block the UI thread')

    def drain(self):
        self.ui = False
        try:
            while self.queued:
                self.queued.pop(0)('controller')
        finally:
            self.ui = True


class ProbeSearchService:
    def __init__(self, ctx, invoke_fn, request_control=None):
        self.ctx = ctx
        self.invoke = invoke_fn
        self.control = request_control

    def _run(self, value, **options):
        self.invoke(lambda controller: self._assert_controller(controller))
        return {'value': value, 'options': options,
                'original_event': self.ctx.CurEvent(),
                'resource_name': self.ctx.resource_names['ResourceId::7']}

    @staticmethod
    def _assert_controller(controller):
        if controller != 'controller':
            raise AssertionError('Wrong replay controller')

    find_draws_by_resource = _run
    find_draws_by_texture = _run
    find_draws_by_shader = _run
    find_resource_usage = _run


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.facade_module, cls.handler_module, cls.control_module = load_extension()
        from mcp_server import server
        cls.server = server
        cls.tools = asyncio.run(server.mcp.get_tools())

    def setUp(self):
        self.ctx = Context()
        self.facade = self.facade_module.RenderDocFacade(self.ctx)
        self.handler = self.handler_module.RequestHandler(self.facade)
        self.service_patch = patch.object(self.facade_module, 'SearchService', ProbeSearchService)
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)

    def test_search_returns_before_replay_and_uses_only_ui_snapshot(self):
        responses = []
        request = {'id': 'a', 'method': 'find_draws_by_resource',
                   'params': {'resource_id': '7', 'event_id_min': 10,
                              'event_id_max': 20, 'max_events': 3}}
        self.assertTrue(self.handler.handle_async(request, responses.append))
        self.assertEqual(responses, [])
        self.assertEqual(len(self.ctx.queued), 1)
        self.ctx.drain()
        result = responses[0]['result']
        self.assertEqual(result['original_event'], 42)
        self.assertEqual(result['resource_name'], 'custom UI texture')
        self.assertEqual(result['options']['max_events'], 3)
        self.assertEqual(result['options']['event_id_max'], 20)

    def test_non_search_keeps_existing_synchronous_routing(self):
        responses = []
        self.assertFalse(self.handler.handle_async({'id': 'p', 'method': 'ping'}, responses.append))
        self.assertEqual(self.ctx.queued, [])
        self.assertEqual(self.handler.handle({'id': 'p', 'method': 'ping'})['result']['status'], 'ok')

    def test_missing_capture_and_parameter_are_errors_not_hanging_requests(self):
        self.ctx.loaded = False
        responses = []
        self.handler.handle_async({'id': 'a', 'method': 'find_resource_usage', 'params': {'resource_id': '7'}}, responses.append)
        self.assertEqual(responses[0]['error']['code'], -32602)
        self.assertEqual(self.ctx.queued, [])
        self.ctx.loaded = True
        responses = []
        self.handler.handle_async({'id': 'b', 'method': 'find_resource_usage', 'params': {}}, responses.append)
        self.ctx.drain()
        self.assertEqual(responses[0]['error']['code'], -32602)

    def test_cancelled_queued_request_skips_search_and_returns_cancel_error(self):
        control = self.control_module.RequestControl({'id': 'c'}, str(ROOT))
        responses = []
        self.handler.handle_async({'id': 'c', 'method': 'find_resource_usage', 'params': {'resource_id': '7'}}, responses.append, control)
        control.cancel()
        self.ctx.drain()
        self.assertEqual(responses[0]['error']['code'], -32800)

    def test_registered_mcp_tools_forward_options_through_async_handler(self):
        ctx, handler = self.ctx, self.handler
        class Bridge:
            def call(self, method, params):
                responses = []
                if not handler.handle_async({'id': 'tool', 'method': method, 'params': params}, responses.append):
                    raise AssertionError('Tool did not use async search routing')
                if responses:
                    raise AssertionError('Replay work ran on UI request stack')
                ctx.drain()
                if 'error' in responses[0]:
                    raise ValueError(responses[0]['error'])
                return responses[0]['result']
        with patch.object(self.server, 'bridge', Bridge()):
            for method, field in [('find_resource_usage', 'resource_id'), ('find_draws_by_resource', 'resource_id'), ('find_draws_by_texture', 'texture_name'), ('find_draws_by_shader', 'shader_name')]:
                with self.subTest(method=method):
                    result = self.tools[method].fn(**{field: '7', 'event_id_min': 10, 'event_id_max': 20, 'max_results': 2})
                    self.assertEqual(result['options']['max_results'], 2)
                    self.assertEqual(result['options']['event_id_min'], 10)

    def test_registered_mcp_tools_reject_non_integer_limits_before_transport(self):
        with patch.object(self.server, 'bridge') as bridge:
            for value in [True, '3', 3.0, 1.2]:
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        asyncio.run(self.tools['find_resource_usage'].run({'resource_id': '7', 'max_results': value}))
            bridge.call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
