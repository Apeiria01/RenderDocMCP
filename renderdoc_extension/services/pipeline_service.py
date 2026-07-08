"""
Pipeline state service for RenderDoc.
"""

import zlib

import renderdoc as rd

from ..utils import Parsers, Serializers, Helpers


class PipelineService:
    """Pipeline state service"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def get_shader_info(self, event_id, stage):
        """Get shader information for a specific stage"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"shader": None, "error": None}

        def callback(controller):
            controller.SetFrameEvent(event_id, True)

            pipe = controller.GetPipelineState()
            stage_enum = Parsers.parse_stage(stage)

            shader = pipe.GetShader(stage_enum)
            if shader == rd.ResourceId.Null():
                result["error"] = "No %s shader bound" % stage
                return

            entry = pipe.GetShaderEntryPoint(stage_enum)
            reflection = pipe.GetShaderReflection(stage_enum)

            shader_info = {
                "resource_id": str(shader),
                "entry_point": entry,
                "stage": stage,
            }

            # Get disassembly
            try:
                targets = controller.GetDisassemblyTargets(True)
                if targets:
                    disasm = controller.DisassembleShader(
                        pipe.GetGraphicsPipelineObject(), reflection, targets[0]
                    )
                    shader_info["disassembly"] = disasm
            except Exception as e:
                shader_info["disassembly_error"] = str(e)

            # Get constant buffer info
            if reflection:
                shader_hash = self._get_shader_hash_info(reflection)
                if shader_hash:
                    shader_info["shader_hash"] = shader_hash

                shader_info["constant_buffers"] = self._get_cbuffer_info(
                    controller, pipe, reflection, stage_enum
                )
                shader_info["resources"] = self._get_resource_bindings(reflection)

            result["shader"] = shader_info

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["shader"]

    def get_pipeline_state(self, event_id):
        """Get full pipeline state at an event"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"pipeline": None, "error": None}

        def callback(controller):
            controller.SetFrameEvent(event_id, True)

            pipe = controller.GetPipelineState()
            api = controller.GetAPIProperties().pipelineType

            pipeline_info = {
                "event_id": event_id,
                "api": str(api),
            }

            # Shader stages with detailed bindings
            stages = {}
            stage_list = Helpers.get_all_shader_stages()
            for stage in stage_list:
                shader = pipe.GetShader(stage)
                if shader != rd.ResourceId.Null():
                    stage_info = {
                        "resource_id": str(shader),
                        "entry_point": pipe.GetShaderEntryPoint(stage),
                    }

                    reflection = pipe.GetShaderReflection(stage)

                    shader_hash = self._get_shader_hash_info(reflection)
                    if shader_hash:
                        stage_info["shader_hash"] = shader_hash

                    stage_info["resources"] = self._get_stage_resources(
                        controller, pipe, stage, reflection
                    )
                    stage_info["uavs"] = self._get_stage_uavs(
                        controller, pipe, stage, reflection
                    )
                    stage_info["samplers"] = self._get_stage_samplers(
                        pipe, stage, reflection
                    )
                    stage_info["constant_buffers"] = self._get_stage_cbuffers(
                        controller, pipe, stage, reflection
                    )

                    stages[str(stage)] = stage_info

            pipeline_info["shaders"] = stages

            # Viewports (unified API exposes GetViewport(i) with no count, so probe
            # indices and stop on the first that errors).
            try:
                viewports = []
                for i in range(16):
                    try:
                        v = pipe.GetViewport(i)
                    except Exception:
                        break
                    if v.width == 0.0 and v.height == 0.0:
                        continue
                    viewports.append({
                        "index": i,
                        "x": v.x,
                        "y": v.y,
                        "width": v.width,
                        "height": v.height,
                        "min_depth": v.minDepth,
                        "max_depth": v.maxDepth,
                    })
                if viewports:
                    pipeline_info["viewports"] = viewports
            except Exception as e:
                pipeline_info["viewports_error"] = str(e)

            # Scissors (same probing approach).
            try:
                scissors = []
                for i in range(16):
                    try:
                        s = pipe.GetScissor(i)
                    except Exception:
                        break
                    if s.width == 0 and s.height == 0:
                        continue
                    scissors.append({
                        "index": i,
                        "x": s.x,
                        "y": s.y,
                        "width": s.width,
                        "height": s.height,
                    })
                if scissors:
                    pipeline_info["scissors"] = scissors
            except Exception as e:
                pipeline_info["scissors_error"] = str(e)

            # Render targets + depth target (unified descriptor API; each entry is
            # a Descriptor whose bound resource is `.resource`).
            try:
                rts = []
                for i, rt in enumerate(pipe.GetOutputTargets()):
                    if rt.resource == rd.ResourceId.Null():
                        continue
                    rts.append({"index": i, "resource_id": str(rt.resource)})
                pipeline_info["render_targets"] = rts

                depth = pipe.GetDepthTarget()
                if depth.resource != rd.ResourceId.Null():
                    pipeline_info["depth_target"] = str(depth.resource)
            except Exception as e:
                pipeline_info["render_targets_error"] = str(e)

            # Input assembly: topology, vertex buffers, input layout, index buffer.
            # Each sub-block is guarded independently so a wrong API name in a
            # given RenderDoc version degrades to an *_error field instead of
            # silently dropping the whole IA section.
            ia_info = {}

            try:
                ia_info["topology"] = str(pipe.GetPrimitiveTopology())
            except Exception as e:
                ia_info["topology_error"] = str(e)

            # Raw vertex buffers bound on the IA (sticky API state). This count
            # matches ReShade state_tracking's vertex_buffers (incl. unused slots).
            try:
                vbs = []
                for slot, vb in enumerate(pipe.GetVBuffers()):
                    if vb.resourceId == rd.ResourceId.Null():
                        continue
                    vbs.append({
                        "slot": slot,
                        "resource_id": str(vb.resourceId),
                        "byte_offset": vb.byteOffset,
                        "byte_stride": vb.byteStride,
                        "byte_size": vb.byteSize,
                    })
                ia_info["vertex_buffers"] = vbs
            except Exception as e:
                ia_info["vertex_buffers_error"] = str(e)

            # Input layout attributes. `used` + `vb_slot` reveal which VB slots
            # the current layout actually consumes -- i.e. what RenderDoc's IA
            # panel shows. used_vb_slots vs vertex_buffers exposes stale binds.
            try:
                attrs = []
                used_slots = set()
                for a in pipe.GetVertexInputs():
                    slot = int(a.vertexBuffer)
                    attrs.append({
                        "name": a.name,
                        "vb_slot": slot,
                        "byte_offset": a.byteOffset,
                        "format": a.format.Name(),
                        "per_instance": bool(a.perInstance),
                        "used": bool(a.used),
                    })
                    if a.used and slot >= 0:
                        used_slots.add(slot)
                ia_info["vertex_inputs"] = attrs
                ia_info["used_vb_slots"] = sorted(used_slots)
            except Exception as e:
                ia_info["vertex_inputs_error"] = str(e)

            try:
                ib = pipe.GetIBuffer()
                if ib.resourceId != rd.ResourceId.Null():
                    ia_info["index_buffer"] = {
                        "resource_id": str(ib.resourceId),
                        "byte_offset": ib.byteOffset,
                        "byte_stride": ib.byteStride,
                    }
            except Exception as e:
                ia_info["index_buffer_error"] = str(e)

            if ia_info:
                pipeline_info["input_assembly"] = ia_info

            result["pipeline"] = pipeline_info

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["pipeline"]

    def list_set_render_targets(self):
        """Enumerate every OMSetRenderTargets call, tag each with its enclosing
        debug marker (in-pass vs out-of-pass), and resolve the bound rtv[0]."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None, "error": None}

        def callback(controller):
            sdfile = controller.GetStructuredFile()
            num_chunks = len(sdfile.chunks)

            # Pass 1: walk the action tree tracking the marker stack, and collect
            # every OMSetRenderTargets event. It is a state-setting call (not an
            # action), so it only shows up inside each action's `events` list.
            calls = []

            def walk(action, marker_stack):
                for ev in action.events:
                    ci = ev.chunkIndex
                    if ci >= num_chunks:
                        continue
                    try:
                        name = sdfile.chunks[ci].name
                    except Exception:
                        continue
                    if "OMSetRenderTargets" in name:
                        calls.append({
                            "event_id": ev.eventId,
                            "chunk": name,
                            "marker": marker_stack[-1] if marker_stack else None,
                            "in_colour_pass": any("Colour Pass" in m for m in marker_stack),
                            "in_any_marker": len(marker_stack) > 0,
                        })
                # A PushMarker action's own events belong to the PARENT scope, so
                # push the marker only when descending into its children.
                next_stack = marker_stack
                if action.flags & rd.ActionFlags.PushMarker:
                    next_stack = marker_stack + [action.GetName(sdfile)]
                for child in action.children:
                    walk(child, next_stack)

            for root in controller.GetRootActions():
                walk(root, [])

            # Pass 2: resolve rtv[0]/dsv per call. Ascending eid order lets RenderDoc
            # replay forward incrementally instead of restarting for each event.
            calls.sort(key=lambda c: c["event_id"])
            for c in calls:
                try:
                    controller.SetFrameEvent(c["event_id"], False)
                    pipe = controller.GetPipelineState()
                    targets = pipe.GetOutputTargets()
                    c["rt_count"] = sum(
                        1 for t in targets if t.resource != rd.ResourceId.Null()
                    )
                    if targets and targets[0].resource != rd.ResourceId.Null():
                        c["rtv0"] = str(targets[0].resource)
                        c["rtv0_view"] = str(targets[0].view)
                    else:
                        c["rtv0"] = None
                        c["rtv0_view"] = None
                    depth = pipe.GetDepthTarget()
                    c["dsv"] = (
                        str(depth.resource)
                        if depth.resource != rd.ResourceId.Null()
                        else None
                    )
                except Exception as e:
                    c["resolve_error"] = str(e)

            in_pass = [c for c in calls if c["in_colour_pass"]]
            out_pass = [c for c in calls if not c["in_colour_pass"]]

            def rtv0_histogram(items):
                hist = {}
                for c in items:
                    key = c.get("rtv0") or "(none)"
                    hist[key] = hist.get(key, 0) + 1
                return hist

            result["data"] = {
                "total": len(calls),
                "in_colour_pass_count": len(in_pass),
                "out_of_colour_pass_count": len(out_pass),
                "in_pass_rtv0_histogram": rtv0_histogram(in_pass),
                "out_pass_rtv0_histogram": rtv0_histogram(out_pass),
                "calls": calls,
            }

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def list_shader_hashes(
        self,
        stage="pixel",
        event_id_min=None,
        event_id_max=None,
        unique_only=False,
        limit=None,
    ):
        """List shader hashes observed in pipeline state events."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None, "error": None}

        def callback(controller):
            structured_file = controller.GetStructuredFile()
            actions = Helpers.flatten_actions(controller.GetRootActions())
            api = controller.GetAPIProperties().pipelineType

            stage_filter = (stage or "pixel").lower()
            if stage_filter == "all":
                stages = Helpers.get_all_shader_stages()
            else:
                stages = [Parsers.parse_stage(stage_filter)]

            events = []
            unique = {}

            for action in actions:
                event_id = action.eventId
                if event_id_min is not None and event_id < event_id_min:
                    continue
                if event_id_max is not None and event_id > event_id_max:
                    continue
                if not (action.flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.Dispatch)):
                    continue

                controller.SetFrameEvent(event_id, True)
                pipe = controller.GetPipelineState()

                for stage_enum in stages:
                    shader = pipe.GetShader(stage_enum)
                    if shader == rd.ResourceId.Null():
                        continue

                    reflection = pipe.GetShaderReflection(stage_enum)
                    shader_hash = self._get_shader_hash_info(reflection)
                    if not shader_hash:
                        continue

                    stage_name = self._stage_name(stage_enum)
                    entry_point = pipe.GetShaderEntryPoint(stage_enum)
                    resource_id = str(shader)

                    item = {
                        "event_id": event_id,
                        "action_id": action.actionId,
                        "name": action.GetName(structured_file),
                        "stage": stage_name,
                        "resource_id": resource_id,
                        "entry_point": entry_point,
                        "hash_dec": shader_hash["dec"],
                        "hash_hex": shader_hash["hex"],
                        "bytecode_size": shader_hash["bytecode_size"],
                    }

                    if "encoding" in shader_hash:
                        item["encoding"] = shader_hash["encoding"]

                    try:
                        resource_name = self.ctx.GetResourceName(shader)
                        if resource_name:
                            item["resource_name"] = resource_name
                    except Exception:
                        pass

                    if not unique_only:
                        events.append(item)
                        if limit is not None and len(events) >= limit:
                            break

                    key = (stage_name, shader_hash["dec"])
                    if key not in unique:
                        unique[key] = {
                            "stage": stage_name,
                            "hash_dec": shader_hash["dec"],
                            "hash_hex": shader_hash["hex"],
                            "bytecode_size": shader_hash["bytecode_size"],
                            "resource_ids": [],
                            "entry_points": [],
                            "event_ids": [],
                            "first_event_id": event_id,
                        }
                        if "encoding" in shader_hash:
                            unique[key]["encoding"] = shader_hash["encoding"]

                    unique_item = unique[key]
                    if resource_id not in unique_item["resource_ids"]:
                        unique_item["resource_ids"].append(resource_id)
                    if entry_point not in unique_item["entry_points"]:
                        unique_item["entry_points"].append(entry_point)
                    unique_item["event_ids"].append(event_id)

                if limit is not None and not unique_only and len(events) >= limit:
                    break

            unique_shaders = list(unique.values())
            unique_shaders.sort(key=lambda item: (item["first_event_id"], item["stage"], item["hash_dec"]))
            for item in unique_shaders:
                item["event_count"] = len(item["event_ids"])

            result["data"] = {
                "api": str(api),
                "stage": stage_filter,
                "event_id_min": event_id_min,
                "event_id_max": event_id_max,
                "count": len(events) if not unique_only else sum(len(i["event_ids"]) for i in unique_shaders),
                "unique_count": len(unique_shaders),
                "unique_shaders": unique_shaders,
            }

            if not unique_only:
                result["data"]["events"] = events
            if limit is not None:
                result["data"]["limit"] = limit

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def _get_shader_hash_info(self, reflection):
        """Compute the ReShade/ShaderToggler-compatible CRC32 from shader bytecode."""
        if not reflection:
            return None

        raw_bytes = getattr(reflection, "rawBytes", None)
        if not raw_bytes:
            return None

        try:
            raw_bytes = bytes(raw_bytes)
        except Exception:
            return None

        if not raw_bytes:
            return None

        hash_value = zlib.crc32(raw_bytes) & 0xFFFFFFFF
        info = {
            "dec": hash_value,
            "hex": "0x%08X" % hash_value,
            "bytecode_size": len(raw_bytes),
        }

        encoding = getattr(reflection, "encoding", None)
        if encoding is not None:
            info["encoding"] = str(encoding)

        return info

    def _stage_name(self, stage):
        """Return stable lower-case stage names for MCP output."""
        stage_names = {
            rd.ShaderStage.Vertex: "vertex",
            rd.ShaderStage.Hull: "hull",
            rd.ShaderStage.Domain: "domain",
            rd.ShaderStage.Geometry: "geometry",
            rd.ShaderStage.Pixel: "pixel",
            rd.ShaderStage.Compute: "compute",
        }
        return stage_names.get(stage, str(stage))

    def _get_stage_resources(self, controller, pipe, stage, reflection):
        """Get shader resource views (SRVs) for a stage"""
        resources = []
        try:
            srvs = pipe.GetReadOnlyResources(stage, False)

            name_map = {}
            if reflection:
                for res in reflection.readOnlyResources:
                    name_map[res.fixedBindNumber] = res.name

            for srv in srvs:
                if srv.descriptor.resource == rd.ResourceId.Null():
                    continue

                slot = srv.access.index
                res_info = {
                    "slot": slot,
                    "name": name_map.get(slot, ""),
                    "resource_id": str(srv.descriptor.resource),
                }

                res_info.update(
                    self._get_resource_details(controller, srv.descriptor.resource)
                )

                res_info["first_mip"] = srv.descriptor.firstMip
                res_info["num_mips"] = srv.descriptor.numMips
                res_info["first_slice"] = srv.descriptor.firstSlice
                res_info["num_slices"] = srv.descriptor.numSlices

                resources.append(res_info)
        except Exception as e:
            resources.append({"error": str(e)})

        return resources

    def _get_stage_uavs(self, controller, pipe, stage, reflection):
        """Get unordered access views (UAVs) for a stage"""
        uavs = []
        try:
            uav_list = pipe.GetReadWriteResources(stage, False)

            name_map = {}
            if reflection:
                for res in reflection.readWriteResources:
                    name_map[res.fixedBindNumber] = res.name

            for uav in uav_list:
                if uav.descriptor.resource == rd.ResourceId.Null():
                    continue

                slot = uav.access.index
                uav_info = {
                    "slot": slot,
                    "name": name_map.get(slot, ""),
                    "resource_id": str(uav.descriptor.resource),
                }

                uav_info.update(
                    self._get_resource_details(controller, uav.descriptor.resource)
                )

                uav_info["first_element"] = uav.descriptor.firstMip
                uav_info["num_elements"] = uav.descriptor.numMips

                uavs.append(uav_info)
        except Exception as e:
            uavs.append({"error": str(e)})

        return uavs

    def _get_stage_samplers(self, pipe, stage, reflection):
        """Get samplers for a stage"""
        samplers = []
        try:
            sampler_list = pipe.GetSamplers(stage, False)

            name_map = {}
            if reflection:
                for samp in reflection.samplers:
                    name_map[samp.fixedBindNumber] = samp.name

            for samp in sampler_list:
                slot = samp.access.index
                samp_info = {
                    "slot": slot,
                    "name": name_map.get(slot, ""),
                }

                desc = samp.descriptor
                try:
                    samp_info["address_u"] = str(desc.addressU)
                    samp_info["address_v"] = str(desc.addressV)
                    samp_info["address_w"] = str(desc.addressW)
                except AttributeError:
                    pass

                try:
                    samp_info["filter"] = str(desc.filter)
                except AttributeError:
                    pass

                try:
                    samp_info["max_anisotropy"] = desc.maxAnisotropy
                except AttributeError:
                    pass

                try:
                    samp_info["min_lod"] = desc.minLOD
                    samp_info["max_lod"] = desc.maxLOD
                    samp_info["mip_lod_bias"] = desc.mipLODBias
                except AttributeError:
                    pass

                try:
                    samp_info["border_color"] = [
                        desc.borderColor[0],
                        desc.borderColor[1],
                        desc.borderColor[2],
                        desc.borderColor[3],
                    ]
                except (AttributeError, TypeError):
                    pass

                try:
                    samp_info["compare_function"] = str(desc.compareFunction)
                except AttributeError:
                    pass

                samplers.append(samp_info)
        except Exception as e:
            samplers.append({"error": str(e)})

        return samplers

    def _get_stage_cbuffers(self, controller, pipe, stage, reflection):
        """Get constant buffers for a stage from shader reflection"""
        cbuffers = []
        try:
            if not reflection:
                return cbuffers

            for cb in reflection.constantBlocks:
                slot = cb.bindPoint if hasattr(cb, 'bindPoint') else cb.fixedBindNumber
                cb_info = {
                    "slot": slot,
                    "name": cb.name,
                    "byte_size": cb.byteSize,
                    "variable_count": len(cb.variables) if cb.variables else 0,
                    "variables": [],
                }
                if cb.variables:
                    for var in cb.variables:
                        cb_info["variables"].append({
                            "name": var.name,
                            "byte_offset": var.byteOffset,
                            "type": str(var.type.name) if var.type else "",
                        })
                cbuffers.append(cb_info)

        except Exception as e:
            cbuffers.append({"error": str(e)})

        return cbuffers

    def _get_resource_details(self, controller, resource_id):
        """Get details about a resource (texture or buffer)"""
        details = {}

        try:
            resource_name = self.ctx.GetResourceName(resource_id)
            if resource_name:
                details["resource_name"] = resource_name
        except Exception:
            pass

        for tex in controller.GetTextures():
            if tex.resourceId == resource_id:
                details["type"] = "texture"
                details["width"] = tex.width
                details["height"] = tex.height
                details["depth"] = tex.depth
                details["array_size"] = tex.arraysize
                details["mip_levels"] = tex.mips
                details["format"] = str(tex.format.Name())
                details["dimension"] = str(tex.type)
                details["msaa_samples"] = tex.msSamp
                return details

        for buf in controller.GetBuffers():
            if buf.resourceId == resource_id:
                details["type"] = "buffer"
                details["length"] = buf.length
                return details

        return details

    def _get_cbuffer_info(self, controller, pipe, reflection, stage):
        """Get constant buffer information and values"""
        cbuffers = []

        for i, cb in enumerate(reflection.constantBlocks):
            cb_info = {
                "name": cb.name,
                "slot": i,
                "size": cb.byteSize,
                "variables": [],
            }

            try:
                # GetConstantBuffer was removed; the unified API returns a
                # UsedDescriptor whose `.descriptor` carries the bound buffer.
                bind = pipe.GetConstantBlock(stage, i, 0)
                if bind.descriptor.resource != rd.ResourceId.Null():
                    variables = controller.GetCBufferVariableContents(
                        pipe.GetGraphicsPipelineObject(),
                        reflection.resourceId,
                        stage,
                        reflection.entryPoint,
                        i,
                        bind.descriptor.resource,
                        bind.descriptor.byteOffset,
                        bind.descriptor.byteSize,
                    )
                    cb_info["variables"] = Serializers.serialize_variables(variables)
            except Exception as e:
                cb_info["error"] = str(e)

            cbuffers.append(cb_info)

        return cbuffers

    def _get_resource_bindings(self, reflection):
        """Get shader resource bindings"""
        resources = []

        try:
            for res in reflection.readOnlyResources:
                resources.append(
                    {
                        "name": res.name,
                        "type": str(res.resType),
                        "binding": res.fixedBindNumber,
                        "access": "ReadOnly",
                    }
                )
        except Exception:
            pass

        try:
            for res in reflection.readWriteResources:
                resources.append(
                    {
                        "name": res.name,
                        "type": str(res.resType),
                        "binding": res.fixedBindNumber,
                        "access": "ReadWrite",
                    }
                )
        except Exception:
            pass

        return resources
