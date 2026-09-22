"""D3D11/12 output-merger snapshots (RenderDoc's unified Descriptor API)."""

import renderdoc as rd


def _resource_id(value):
    return None if value == rd.ResourceId.Null() else str(value)


def _stencil_face(face):
    return {
        "function": str(face.function),
        "fail_operation": str(face.failOperation),
        "depth_fail_operation": str(face.depthFailOperation),
        "pass_operation": str(face.passOperation),
        "reference": int(face.reference),
        "compare_mask": int(face.compareMask),
        "write_mask": int(face.writeMask),
    }


def _equation(equation):
    return {
        "source": str(equation.source),
        "destination": str(equation.destination),
        "operation": str(equation.operation),
    }


def _blend_target(target, slot, independent):
    mask = int(target.writeMask)
    return {
        "slot": slot,
        "effective_blend_slot": slot if independent else 0,
        "enabled": bool(target.enabled),
        "logic_operation_enabled": bool(target.logicOperationEnabled),
        "color": _equation(target.colorBlend),
        "alpha": _equation(target.alphaBlend),
        "logic_operation": str(target.logicOperation),
        "write_mask": mask,
        "write_channels": "".join(c for i, c in enumerate("RGBA") if mask & (1 << i)),
    }


def serialize_output_merger(controller, api):
    """Read one already-selected event; never seek or access CaptureContext here."""
    if api == rd.GraphicsAPI.D3D11:
        state = controller.GetD3D11PipelineState()
        om = state.outputMerger
        sample_mask = om.blendState.sampleMask
    elif api == rd.GraphicsAPI.D3D12:
        state = controller.GetD3D12PipelineState()
        om = state.outputMerger
        sample_mask = state.rasterizer.sampleMask
    else:
        return {"supported": False, "reason": "Output-merger details support D3D11 and D3D12 only"}

    names = {str(r.resourceId): str(r.name) for r in controller.GetResources()}
    textures = {str(t.resourceId): t for t in controller.GetTextures()}

    def descriptor(view):
        rid = _resource_id(view.resource)
        result = {"bound": rid is not None, "resource_id": rid,
                  "view_id": _resource_id(view.view)}
        if rid is None:
            return result
        result.update({
            "resource_name": names.get(rid, ""),
            "format": view.format.Name(),
            "type": str(view.type),
            "flags": str(view.flags),
            "texture_type": str(view.textureType),
            "first_mip": int(view.firstMip),
            "num_mips": int(view.numMips),
            "first_slice": int(view.firstSlice),
            "num_slices": int(view.numSlices),
            "byte_offset": int(view.byteOffset),
            "byte_size": int(view.byteSize),
        })
        if hasattr(view, "secondary"):
            result["secondary_resource_id"] = _resource_id(view.secondary)
        for source, field in (
                ("counterByteOffset", "counter_byte_offset"),
                ("bufferStructCount", "buffer_struct_count"),
                ("elementByteSize", "element_byte_size")):
            if hasattr(view, source):
                result[field] = int(getattr(view, source))
        tex = textures.get(rid)
        if tex is not None:
            result["texture"] = {
                "format": tex.format.Name(),
                "width": int(tex.width), "height": int(tex.height),
                "depth": int(tex.depth), "array_size": int(tex.arraysize),
                "mip_levels": int(tex.mips), "msaa_samples": int(tex.msSamp),
            }
        return result

    ds = om.depthStencilState
    blend = om.blendState
    depth_bound = om.depthTarget.resource != rd.ResourceId.Null()
    depth = descriptor(om.depthTarget) if depth_bound else None
    if depth is not None:
        depth.update({"depth_read_only": bool(om.depthReadOnly),
                      "stencil_read_only": bool(om.stencilReadOnly)})
    depth_stencil = {
        "state_resource_id": _resource_id(ds.resourceId) if hasattr(ds, "resourceId") else None,
        "depth_enable": bool(ds.depthEnable),
        "depth_write": bool(ds.depthWrites),
        "depth_function": str(ds.depthFunction),
        "depth_read_only": bool(om.depthReadOnly),
        "stencil_read_only": bool(om.stencilReadOnly),
        # State eligibility only, not evidence of actual fragment/depth writes.
        "depth_write_allowed": bool(depth_bound and ds.depthEnable and ds.depthWrites
                                    and not om.depthReadOnly
                                    and ds.depthFunction != rd.CompareFunction.Never),
        "stencil_enable": bool(ds.stencilEnable),
        "front_face": _stencil_face(ds.frontFace),
        "back_face": _stencil_face(ds.backFace),
    }
    if api == rd.GraphicsAPI.D3D12:
        depth_stencil["depth_bounds"] = {
            "enabled": bool(ds.depthBoundsEnable),
            "minimum": float(ds.minDepthBounds), "maximum": float(ds.maxDepthBounds),
        }

    targets = []
    for slot, target in enumerate(om.renderTargets):
        item = descriptor(target)
        item["slot"] = slot
        targets.append(item)
    result = {
        "supported": True,
        "depth_target": depth,
        "render_targets": targets,
        "depth_stencil": depth_stencil,
        "blend": {
            "state_resource_id": _resource_id(blend.resourceId) if hasattr(blend, "resourceId") else None,
            "alpha_to_coverage": bool(blend.alphaToCoverage),
            "independent_blend": bool(blend.independentBlend),
            "blend_factor": [float(x) for x in blend.blendFactor],
            "sample_mask": int(sample_mask),
            "targets": [_blend_target(b, i, blend.independentBlend)
                        for i, b in enumerate(blend.blends)],
        },
    }
    if api == rd.GraphicsAPI.D3D11:
        result["uav_start_slot"] = int(om.uavStartSlot)
    # Fixed D3D11 OM slots differ from D3D12 shader descriptor accesses.
    try:
        if api == rd.GraphicsAPI.D3D11:
            descriptor_range = rd.DescriptorRange()
            descriptor_range.offset = 0
            descriptor_range.descriptorSize = state.descriptorByteSize
            descriptor_range.count = state.descriptorCount
            descriptor_range.type = rd.DescriptorType.Unknown
            locations = controller.GetDescriptorLocations(state.descriptorStore, [descriptor_range])
            descriptors = controller.GetDescriptors(state.descriptorStore, [descriptor_range])
            if len(locations) != len(descriptors):
                raise ValueError("Descriptor location/count mismatch")
            uavs = []
            for location, view in zip(locations, descriptors):
                if (location.category == rd.DescriptorCategory.ReadWriteResource
                        and location.stageMask & rd.ShaderStageMask.Pixel
                        and view.resource != rd.ResourceId.Null()):
                    item = descriptor(view)
                    item["slot"] = int(location.fixedBindNumber)
                    uavs.append(item)
            result["uavs"] = {"scope": "all_bound_pixel_slots",
                              "bindings": sorted(uavs, key=lambda u: u["slot"])}
        else:
            pipe = controller.GetPipelineState()
            reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
            shader_resources = reflection.readWriteResources if reflection else []
            uavs = []
            for used in pipe.GetReadWriteResources(rd.ShaderStage.Pixel, False):
                access = used.access
                item = descriptor(used.descriptor)
                item["access"] = {
                    "index": int(access.index), "array_element": int(access.arrayElement),
                    "descriptor_store": _resource_id(access.descriptorStore),
                    "byte_offset": int(access.byteOffset), "byte_size": int(access.byteSize),
                    "statically_unused": bool(access.staticallyUnused),
                }
                item["binding"] = None
                if (access.index != rd.DescriptorAccess.NoShaderBinding
                        and 0 <= access.index < len(shader_resources)):
                    binding = shader_resources[access.index]
                    item["binding"] = {
                        "name": str(binding.name),
                        "register": int(binding.fixedBindNumber) + int(access.arrayElement),
                        "register_space": int(binding.fixedBindSetOrSpace),
                        "array_element": int(access.arrayElement),
                    }
                uavs.append(item)
            result["uavs"] = {"scope": "pixel_shader_accesses", "bindings": uavs}
    except Exception as exc:
        # Older descriptor APIs must not hide the core OM snapshot.
        result["uavs_error"] = str(exc)
    return result
