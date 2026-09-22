"""Bounded shader inspection helpers. Call GPU helpers only on the replay queue."""
import base64
import math

import renderdoc as rd

from .serializers import Serializers

INSPECTION_REVISION = "shader-inspection-1"
MAX_READ_BYTES = 8 * 1024 * 1024
DEFAULT_CB_BYTES = 1024 * 1024


def integer(value, name, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("%s must be an integer >= %d" % (name, minimum))
    if maximum is not None and value > maximum:
        raise ValueError("%s must be <= %d" % (name, maximum))
    return value


def json_number(value):
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("+Infinity" if value > 0 else "-Infinity")
    return value


def resource_id(value):
    return None if value == rd.ResourceId.Null() else str(value)


def format_info(fmt):
    result = {"name": str(fmt.Name())}
    for key, attr, cast in (("type", "type", str), ("comp_type", "compType", str),
                            ("comp_count", "compCount", int),
                            ("comp_byte_width", "compByteWidth", int)):
        if hasattr(fmt, attr):
            result[key] = cast(getattr(fmt, attr))
    # This is the descriptor's typed format name, not the storage format.
    result["srgb"] = "SRGB" in result["name"].upper()
    return result


def access_binding(used, bindings):
    access = used.access
    index = int(access.index)
    element = int(access.arrayElement)
    info = {
        "slot": index, "slot_semantics": "reflection_index", "name": "",
        "access": {"index": index, "array_element": element,
                   "descriptor_store": resource_id(access.descriptorStore),
                   "byte_offset": int(access.byteOffset), "byte_size": int(access.byteSize),
                   "statically_unused": bool(access.staticallyUnused)},
        "binding": None,
    }
    if index != 0xffff and 0 <= index < len(bindings):
        binding = bindings[index]
        info["name"] = str(binding.name)
        info["binding"] = {"name": str(binding.name),
                           "fixed_bind_number": int(binding.fixedBindNumber),
                           "fixed_bind_set_or_space": int(binding.fixedBindSetOrSpace),
                           "array_element": element}
    return info


def sampler_info(used, bindings):
    info = access_binding(used, bindings)
    try:
        desc = used.sampler  # UsedDescriptor.descriptor is EMPTY for samplers.
        info["descriptor_type"] = str(desc.type)
        valid = (getattr(rd.DescriptorType, "Sampler", None),
                 getattr(rd.DescriptorType, "ImageSampler", None))
        if desc.type not in valid:
            raise ValueError("Not a valid sampler descriptor: %s" % desc.type)
        info.update({
            "object_id": resource_id(desc.object),
            "address_u": str(desc.addressU), "address_v": str(desc.addressV),
            "address_w": str(desc.addressW), "compare_function": str(desc.compareFunction),
            "filter": {"minify": str(desc.filter.minify),
                       "magnify": str(desc.filter.magnify),
                       "mip": str(desc.filter.mip), "function": str(desc.filter.filter)},
            "max_anisotropy": json_number(float(desc.maxAnisotropy)),
            "min_lod": json_number(float(desc.minLOD)),
            "max_lod": json_number(float(desc.maxLOD)),
            "mip_lod_bias": json_number(float(desc.mipBias)),
            "border_color_type": str(desc.borderColorType),
            "border_color_raw_uint": [int(desc.borderColorValue.uintValue[i]) for i in range(4)],
            "srgb_border": bool(desc.srgbBorder),
            "unnormalized": bool(desc.unnormalized),
            "seamless_cubemaps": bool(desc.seamlessCubemaps),
            "creation_time_constant": bool(desc.creationTimeConstant),
        })
        if desc.borderColorType == rd.CompType.UInt:
            values = desc.borderColorValue.uintValue
        elif desc.borderColorType == rd.CompType.SInt:
            values = desc.borderColorValue.intValue
        else:
            values = desc.borderColorValue.floatValue
        info["border_color"] = [json_number(values[i]) for i in range(4)]
    except Exception as exc:
        info["error"] = str(exc)
    return info


def raw_read(controller, resource, offset, length, required_length):
    """Never pass RenderDoc's unbounded length=0 sentinel, even for empty ranges."""
    data = bytes(controller.GetBufferData(resource, offset, length)) if length else b""
    if len(data) > length:
        raise ValueError("GetBufferData returned more bytes than requested")
    return {"resource_id": resource_id(resource), "byte_offset": offset,
            "requested_byte_length": length, "required_byte_length": required_length,
            "data_length": len(data), "complete": len(data) == required_length,
            "content_base64": base64.b64encode(data).decode("ascii")}


def variables_complete(variables):
    for var in variables:
        if "value_error" in var:
            return False
        if "members" in var:
            if not variables_complete(var["members"]):
                return False
        elif "value" not in var:
            return False
    return True


def constant_buffers(controller, pipe, reflection, stage, block_index=None,
                     array_element=0, max_bytes=DEFAULT_CB_BYTES, include_raw=True):
    """Read reflection-sized ranges, with one shared byte budget for all blocks."""
    blocks = reflection.constantBlocks
    if block_index is not None and block_index >= len(blocks):
        raise ValueError("block_index is outside ShaderReflection.constantBlocks")
    if block_index is None and array_element:
        raise ValueError("array_element > 0 requires an explicit block_index")
    indices = list(range(min(len(blocks), 128))) if block_index is None else [block_index]
    for i in indices:
        if array_element >= int(blocks[i].bindArraySize):
            raise ValueError("array_element is outside the reflected block array")
    result = []
    remaining = max_bytes
    buffers = {str(b.resourceId): int(b.length) for b in controller.GetBuffers()}
    pipeline = (pipe.GetComputePipelineObject() if stage == rd.ShaderStage.Compute
                else pipe.GetGraphicsPipelineObject())
    for i in indices:
        cb = blocks[i]
        required = int(cb.byteSize)
        item = {"name": str(cb.name), "slot": i, "block_index": i,
                "slot_semantics": "reflection_index", "size": required,
                "array_element": array_element, "bind_array_size": int(cb.bindArraySize),
                "fixed_bind_number": int(cb.fixedBindNumber),
                "fixed_bind_set_or_space": int(cb.fixedBindSetOrSpace),
                "buffer_backed": bool(cb.bufferBacked), "variables": [],
                "values_complete": False, "complete": False}
        result.append(item)
        try:
            used = pipe.GetConstantBlock(stage, i, array_element)
            desc = used.descriptor
            rid = desc.resource
            offset, size = int(desc.byteOffset), int(desc.byteSize)
            item["binding"] = {"resource_id": resource_id(rid), "byte_offset": offset,
                               "byte_size": size, "null_resource": rid == rd.ResourceId.Null()}
            if required <= 0:
                item["diagnostic"] = "Reflection byte size is zero; no unbounded read attempted"
                continue
            allowed = min(required, remaining)
            if rid != rd.ResourceId.Null():
                total = buffers.get(str(rid))
                item["binding"]["resource_byte_size"] = total
                if total is None:
                    item["diagnostic"] = "Bound buffer size unavailable; read skipped"
                    continue
                if offset < 0 or offset > total or size < 0:
                    item["diagnostic"] = "Invalid constant buffer range; read skipped"
                    continue
                # A zero descriptor size is not permission for an unbounded read.
                allowed = min(allowed, size, total - offset)
                remaining -= allowed  # Reserve even if the driver read raises.
                if include_raw:
                    item["raw"] = raw_read(controller, rid, offset, allowed, required)
            else:
                remaining -= allowed
                if include_raw:
                    item["raw"] = {"available": False, "reason": "No buffer resource; values use GetCBufferVariableContents"}
            # Decoding an incomplete range can synthesize zero-filled values. Do not claim those.
            if allowed != required:
                item["diagnostic"] = "Byte budget or binding range is smaller than reflected block; values skipped"
            else:
                try:
                    variables = controller.GetCBufferVariableContents(
                        pipeline, reflection.resourceId, stage, reflection.entryPoint,
                        i, rid, offset, required)
                    item["variables"] = Serializers.serialize_variables(variables)
                    item["values_complete"] = (len(variables) == len(cb.variables)
                                               and variables_complete(item["variables"]))
                    if not item["values_complete"]:
                        item["values_error"] = "Missing or unserializable shader values; inspect variables and raw bytes"
                except Exception as exc:
                    item["values_error"] = str(exc)
            item["complete"] = item["values_complete"] or item.get("raw", {}).get("complete", False)
        except Exception as exc:
            item["error"] = str(exc)
    return {"constant_buffers": result, "byte_budget": max_bytes,
            "budget_consumed": max_bytes - remaining,
            "block_count": len(blocks), "blocks_returned": len(result),
            "scope": "one array element per selected reflection block",
            "complete": all(item["complete"] for item in result) and (block_index is not None or len(result) == len(blocks))}
