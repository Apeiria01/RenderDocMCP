"""
Common helper functions for RenderDoc operations.
"""

import renderdoc as rd


class Helpers:
    """Common helper functions (static methods)"""

    @staticmethod
    def validate_event_id(event_id):
        """Reject coercible/non-positive IDs before replay work is scheduled."""
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise ValueError("event_id must be a positive integer")

    @staticmethod
    def select_event(controller, event_id):
        """Validate action/API event existence and seek inside a replay callback."""
        Helpers.validate_event_id(event_id)
        pending = list(controller.GetRootActions())
        while pending:
            action = pending.pop()
            if action.eventId == event_id or any(e.eventId == event_id for e in action.events):
                controller.SetFrameEvent(event_id, False)
                return
            pending.extend(action.children)
        raise ValueError("Event not found in capture: %d" % event_id)

    @staticmethod
    def flatten_actions(actions):
        """Flatten hierarchical actions to a list"""
        flat = []
        for action in actions:
            flat.append(action)
            if action.children:
                flat.extend(Helpers.flatten_actions(action.children))
        return flat

    @staticmethod
    def count_children(action):
        """Count total number of children recursively"""
        count = 0
        if action.children:
            for child in action.children:
                count += 1
                count += Helpers.count_children(child)
        return count

    @staticmethod
    def get_all_shader_stages():
        """Get list of all shader stages"""
        return [
            rd.ShaderStage.Vertex,
            rd.ShaderStage.Hull,
            rd.ShaderStage.Domain,
            rd.ShaderStage.Geometry,
            rd.ShaderStage.Pixel,
            rd.ShaderStage.Compute,
        ]
