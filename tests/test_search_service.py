"""Regression tests for indexed lookup cost and bounded shader replay.

Fake RenderDoc exposes upstream APIs only. Pipeline seeks fail in index tests,
so accidentally reintroducing per-draw replay cannot silently pass.
"""

import importlib
import sys
import types
import unittest
from enum import IntFlag
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class Flags(IntFlag):
    Drawcall = 1
    Dispatch = 2
    Copy = 4
    Clear = 8
    Resolve = 16


class ResourceId:
    def __init__(self, number):
        self.number = number

    def __str__(self):
        return "ResourceId::%d" % self.number

    def __eq__(self, other):
        return isinstance(other, ResourceId) and self.number == other.number

    def __hash__(self):
        return hash(self.number)

    @staticmethod
    def Null():
        return ResourceId(0)


def load_search():
    name = "_search_test_extension"
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT / "renderdoc_extension")]
    rd = types.ModuleType("renderdoc")
    rd.ActionFlags = Flags
    rd.ResourceId = ResourceId
    rd.ResourceUsage = types.SimpleNamespace(Unused="Unused")
    rd.ResourceType = types.SimpleNamespace(Shader="Shader", Texture="Texture", Buffer="Buffer")
    rd.ShaderStage = types.SimpleNamespace(
        Vertex="VS", Hull="HS", Domain="DS", Geometry="GS", Pixel="PS", Compute="CS")
    with patch.dict(sys.modules, {name: package, "renderdoc": rd}):
        return importlib.import_module(name + ".services.search_service")


def action(event, flags=Flags.Drawcall, outputs=None, depth=None, children=None):
    return types.SimpleNamespace(
        eventId=event, flags=flags, outputs=outputs or [],
        depthOut=depth or ResourceId.Null(), children=children or [],
        GetName=lambda structured: "Action %d" % event)


def usage(event, kind="PS_Resource"):
    return types.SimpleNamespace(eventId=event, usage=kind)


def resource(number, name, kind="Texture"):
    return types.SimpleNamespace(resourceId=ResourceId(number), name=name, type=kind)


class Controller:
    def __init__(self):
        self.resources = [resource(100, "UI layer"), resource(101, "UI mask"),
                          resource(200, "UI vertex data", "Buffer"),
                          resource(300, "UI pixel shader", "Shader")]
        self.actions = [action(10), action(20, Flags.Dispatch), action(30)]
        self.usages = {100: [usage(10), usage(20)], 101: [], 200: [], 300: []}
        self.usage_calls = []
        self.seeks = []
        self.pipeline_calls = []
        self.allow_replay = False
        self.event = 7
        self.shaders = {10: {"PS": ResourceId(300)}, 20: {"CS": ResourceId(300)}}
        self.entries = {10: {"PS": "PaintUI"}}
        self.pipeline_error = None
        self.cancel_after_seek = None

    def GetResources(self):
        return self.resources

    def GetTextures(self):
        return [r for r in self.resources if r.type == "Texture"]

    def GetRootActions(self):
        return self.actions

    def GetStructuredFile(self):
        return object()

    def GetUsage(self, resource_id):
        if resource_id.number == 300:
            raise AssertionError("Shader GetUsage is a fork-only API behavior")
        self.usage_calls.append(resource_id)
        return self.usages.get(resource_id.number, [])

    def SetFrameEvent(self, event_id, force):
        if not self.allow_replay:
            raise AssertionError("Indexed queries must never replay")
        self.seeks.append(event_id)
        self.event = event_id
        if self.cancel_after_seek is not None and event_id != 7:
            self.cancel_after_seek.cancelled = True

    def GetPipelineState(self):
        self.pipeline_calls.append(self.event)
        if self.pipeline_error:
            raise self.pipeline_error
        return self

    def GetShader(self, stage):
        return self.shaders.get(self.event, {}).get(stage, ResourceId.Null())

    def GetShaderEntryPoint(self, stage):
        return self.entries.get(self.event, {}).get(stage, "Main")


class Context:
    def __init__(self):
        self.loaded = True
        self.inside_replay = False
        self.resource_names = {}

    def IsCaptureLoaded(self):
        if self.inside_replay:
            raise AssertionError("Capture UI state must be read before replay")
        return self.loaded

    def CurEvent(self):
        if self.inside_replay:
            raise AssertionError("Current UI event must be captured before replay")
        return 7


class Cancelled(Exception):
    pass


class Control:
    def __init__(self):
        self.cancelled = False

    def check_cancelled(self):
        if self.cancelled:
            raise Cancelled("Request cancelled")


class SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_search()

    def setUp(self):
        self.controller = Controller()
        self.ctx = Context()
        self.control = Control()

        def invoke(callback):
            self.ctx.inside_replay = True
            try:
                callback(self.controller)
            finally:
                self.ctx.inside_replay = False

        self.service = self.module.SearchService(self.ctx, invoke, self.control)

    @staticmethod
    def events(result):
        return [match["event_id"] for match in result["matches"]]

    def test_large_frame_uses_one_index_lookup_and_zero_pipeline_seeks(self):
        self.controller.actions = [action(event) for event in range(1, 10001)]
        result = self.service.find_draws_by_resource("100")
        self.assertEqual(self.events(result), [10, 20])
        self.assertEqual(self.controller.usage_calls, [ResourceId(100)])
        self.assertEqual(self.controller.seeks, [])
        self.assertEqual(self.controller.pipeline_calls, [])
        self.assertEqual(result["scanned_draws"], 10000)
        self.assertEqual(result["query_method"], "resource_usage_index")
        self.assertIn("bindless", str(result["coverage"]))
        self.assertFalse(result["coverage"]["binding_slots_available"])

    def test_texture_search_filters_buffers_and_shader_names(self):
        self.controller.usages[101] = [usage(30)]
        self.ctx.resource_names["ResourceId::101"] = "Renamed UI"
        result = self.service.find_draws_by_texture("ui")
        self.assertEqual(self.events(result), [10, 20, 30])
        self.assertEqual(self.controller.usage_calls, [ResourceId(100), ResourceId(101)])
        self.assertEqual(result["matched_resources"][1]["name"], "Renamed UI")

    def test_action_outputs_and_depth_supplement_missing_usage_index(self):
        self.controller.usages[100] = []
        self.controller.actions = [action(10, outputs=[ResourceId(100)]),
                                   action(20, depth=ResourceId(100))]
        result = self.service.find_draws_by_resource("ResourceId::100")
        self.assertEqual(self.events(result), [10, 20])
        self.assertIn("ColorTarget[0]", result["matches"][0]["match_reason"])
        self.assertIn("DepthTarget", result["matches"][1]["match_reason"])

    def test_draw_queries_exclude_copy_clear_resolve_but_usage_query_includes_them(self):
        self.controller.actions += [action(11, Flags.Copy), action(12, Flags.Clear),
                                    action(13, Flags.Resolve)]
        self.controller.usages[100] += [usage(11, "CopySrc"), usage(12, "Clear"),
                                      usage(13, "ResolveDst"), usage(14, "CopyDst")]
        self.assertEqual(self.events(self.service.find_draws_by_resource("100")), [10, 20])
        result = self.service.find_resource_usage("100")
        self.assertEqual(self.events(result), [10, 11, 12, 13, 14, 20])
        self.assertEqual(result["matches"][4]["name"], "Event 14")

    def test_index_pagination_groups_all_resources_per_event_without_duplicates(self):
        self.controller.usages[101] = [usage(10), usage(10), usage(30)]
        page = self.service.find_draws_by_texture("UI", max_results=1)
        self.assertEqual(self.events(page), [10])
        self.assertEqual(len(page["matches"][0]["usages"]), 2)
        self.assertTrue(page["partial"])
        self.assertEqual(page["next_event_id"], 20)
        self.assertEqual(page["stop_reason"], "max_results")
        tail = self.service.find_draws_by_texture("UI", event_id_min=page["next_event_id"])
        self.assertEqual(self.events(tail), [20, 30])
        self.assertFalse(tail["partial"])
        self.assertIsNone(tail["next_event_id"])

    def test_index_range_is_inclusive_and_empty_index_discloses_coverage(self):
        self.assertEqual(self.events(self.service.find_resource_usage("100", 20, 20)), [20])
        result = self.service.find_draws_by_resource("101")
        self.assertEqual(result["matches"], [])
        self.assertIn("does not prove", str(result["coverage"]))

    def test_invalid_ids_and_limits_fail_without_replay(self):
        for kwargs in ({"resource_id": "0"}, {"resource_id": "999"},
                       {"resource_id": "100", "max_results": 0},
                       {"resource_id": "100", "event_id_min": 20, "event_id_max": 10},
                       {"resource_id": "100", "event_id_min": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.service.find_draws_by_resource(**kwargs)
        self.assertEqual(self.controller.seeks, [])

    def test_non_resource_objects_never_use_fork_only_usage_index(self):
        self.controller.resources.append(resource(400, "UI PSO", "PipelineState"))
        for query in (self.service.find_draws_by_resource, self.service.find_resource_usage):
            with self.assertRaisesRegex(ValueError, "only textures and buffers"):
                query("400")
        self.assertEqual(self.controller.usage_calls, [])

    def test_not_tracked_sentinel_is_reported_without_a_fake_event_zero(self):
        self.controller.usages[100] = [usage(0, "Unused")]
        self.controller.actions[0].outputs = [ResourceId(100)]
        result = self.service.find_resource_usage("100")
        self.assertEqual(self.events(result), [10])
        self.assertEqual(result["coverage"]["untracked_resources"], ["ResourceId::100"])

    def test_non_draw_attachment_metadata_does_not_invent_usage(self):
        self.controller.usages[100] = []
        self.controller.actions = [action(10, flags=0, outputs=[ResourceId(100)])]
        self.assertEqual(self.service.find_resource_usage("100")["matches"], [])

    def test_shader_name_and_id_require_explicit_range_without_shader_usage_lookup(self):
        with self.assertRaisesRegex(ValueError, "both event_id"):
            self.service.find_draws_by_shader("UI")
        with self.assertRaisesRegex(ValueError, "both event_id"):
            self.service.find_draws_by_resource("300")
        with self.assertRaisesRegex(ValueError, "Upstream"):
            self.service.find_resource_usage("300")
        self.assertEqual(self.controller.seeks, [])
        self.assertEqual(self.controller.usage_calls, [])

    def test_shader_event_limit_returns_resume_cursor_and_restores_original_event(self):
        self.controller.allow_replay = True
        page = self.service.find_draws_by_resource("300", 10, 30, max_events=1)
        self.assertEqual(self.events(page), [10])
        self.assertEqual(page["scanned_draws"], 1)
        self.assertTrue(page["partial"])
        self.assertEqual(page["stop_reason"], "max_events")
        self.assertEqual(page["next_event_id"], 20)
        self.assertEqual(self.controller.seeks, [10, 7])
        tail = self.service.find_draws_by_resource("300", page["next_event_id"], 30)
        self.assertEqual(self.events(tail), [20])
        self.assertFalse(tail["partial"])
        self.assertEqual(self.controller.event, 7)

    def test_shader_name_matches_debug_name_without_reflection_and_entry_point(self):
        self.controller.allow_replay = True
        by_name = self.service.find_draws_by_shader("UI pixel", "pixel", 10, 30)
        self.assertEqual(self.events(by_name), [10])
        by_entry = self.service.find_draws_by_shader("Paint", "pixel", 10, 30)
        self.assertEqual(self.events(by_entry), [10])
        self.assertEqual(self.controller.event, 7)

    def test_shader_time_budget_stops_before_next_replay(self):
        self.controller.allow_replay = True
        with patch.object(self.module.time, "monotonic", side_effect=[0.0, 0.0, 3.0]):
            result = self.service.find_draws_by_resource("300", 10, 30)
        self.assertEqual(result["stop_reason"], "time_budget")
        self.assertEqual(result["next_event_id"], 20)
        self.assertEqual(self.controller.seeks, [10, 7])

    def test_shader_max_results_stops_before_additional_replay(self):
        self.controller.allow_replay = True
        result = self.service.find_draws_by_shader("UI", event_id_min=10, event_id_max=30,
                                                   max_results=1)
        self.assertEqual(result["stop_reason"], "max_results")
        self.assertEqual(result["next_event_id"], 20)
        self.assertEqual(self.controller.seeks, [10, 7])

    def test_shader_failure_and_cancellation_restore_original_event(self):
        self.controller.allow_replay = True
        self.controller.pipeline_error = RuntimeError("GPU failed")
        with self.assertRaisesRegex(RuntimeError, "GPU failed"):
            self.service.find_draws_by_resource("300", 10, 30)
        self.assertEqual(self.controller.seeks, [10, 7])
        self.controller.seeks.clear()
        self.controller.pipeline_error = None
        self.controller.cancel_after_seek = self.control
        with self.assertRaises(Cancelled):
            self.service.find_draws_by_resource("300", 10, 30)
        self.assertEqual(self.controller.seeks, [10, 7])

    def test_index_cancellation_is_not_swallowed(self):
        self.control.cancelled = True
        with self.assertRaises(Cancelled):
            self.service.find_draws_by_resource("100")
        self.assertEqual(self.controller.usage_calls, [])

    def test_shader_hard_cap_validation_prevents_excessive_work(self):
        for kwargs in ({"max_events": 129}, {"max_events": 0},
                       {"time_budget_ms": 0}, {"time_budget_ms": 10001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.service.find_draws_by_shader("UI", event_id_min=10, event_id_max=30,
                                                   **kwargs)
        self.assertEqual(self.controller.seeks, [])

    def test_deep_marker_hierarchy_does_not_hit_python_recursion_limit(self):
        node = action(10)
        for event in range(1000, 3000):
            node = action(event, flags=0, children=[node])
        self.controller.actions = [node]
        result = self.service.find_draws_by_resource("100")
        self.assertEqual(self.events(result), [10])
        self.assertEqual(result["scanned_actions"], 2001)


if __name__ == "__main__":
    unittest.main()
