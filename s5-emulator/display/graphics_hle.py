#!/usr/bin/env python3
"""Opt-in EGL/OpenVG API boundary for the unchanged S5 UI caller.

This is a software replacement for the graphics driver, not an S5 GPU model.
It contains no menu, text, icon, or layout data. Unsupported calls stop the
caller rather than silently acknowledging an operation that was not rendered.
"""
import json
import math
from pathlib import Path
import struct

try:
    from .openvg_backend import OpenVGBackend, UnsupportedVG, IDENTITY, COORD_COUNTS
except ImportError:
    from openvg_backend import OpenVGBackend, UnsupportedVG, IDENTITY, COORD_COUNTS


# The core VG exports lack strings. These names are inferred from original
# wrapper ABI, enum values, lower validation code and real postboot1 callers.
# Named EGL and DMP entries below come directly from the firmware export table.
CORE = {
    0x065d9280: ("vgGetError", 0),
    0x065d92a2: ("vgFlush", 0), 0x065d92ca: ("vgFinish", 0),
    0x065d92f2: ("vgLoadIdentity", 0), 0x065d9306: ("vgLoadMatrix", 1),
    0x065d931e: ("vgGetMatrix", 1), 0x065d9336: ("vgMultMatrix", 1),
    0x065d934e: ("vgTranslate", 2), 0x065d936a: ("vgScale", 2),
    0x065d9386: ("vgShear", 2), 0x065d93a2: ("vgRotate", 1),
    0x065d93ba: ("vgSetf", 2), 0x065d93d6: ("vgSeti", 2),
    0x065d93f2: ("vgSetfv", 3), 0x065d9412: ("vgSetiv", 3),
    0x065d9432: ("vgGetf", 1), 0x065d9450: ("vgGeti", 1),
    0x065d946e: ("vgGetVectorSize", 1), 0x065d948c: ("vgGetfv", 3),
    0x065d94ac: ("vgGetiv", 3),
    0x065d94cc: ("vgSetParameterf", 3), 0x065d9572: ("vgSetParameteri", 3),
    0x065d961c: ("vgSetParameterfv", 4), 0x065d96ca: ("vgSetParameteriv", 4),
    0x065d9962: ("vgGetParameterfv", 4),
    0x065d9b0c: ("vgClear", 4), 0x065d9b48: ("vgCreatePath", 7),
    0x065d9b80: ("vgClearPath", 2), 0x065d9b9c: ("vgDestroyPath", 1),
    0x065d9bc6: ("vgRemovePathCapabilities", 2),
    0x065d9c1c: ("vgAppendPathData", 4), 0x065d9c64: ("vgTransformPath", 2),
    0x065d9d4e: ("vgDrawPath", 2),
    0x065d9da4: ("vgCreatePaint", 0), 0x065d9dbe: ("vgDestroyPaint", 1),
    0x065d9de8: ("vgSetPaint", 2), 0x065d9e04: ("vgGetPaint", 1),
    0x065d9e22: ("vgSetColor", 2), 0x065d9e3e: ("vgGetColor", 1),
    0x065d9e5c: ("vgPaintPattern", 2),
    0x065d9e78: ("vgCreateImage", 4), 0x065d9ea0: ("vgDestroyImage", 1),
    0x065d9ef0: ("vgImageSubData", 8), 0x065d9f24: ("vgGetImageSubData", 8),
    0x065d9fda: ("vgDrawImage", 1),
}

ARGUMENTS = {
    "eglGetError": 0, "eglGetDisplay": 1, "eglInitialize": 3,
    "eglTerminate": 1, "eglBindAPI": 1, "eglQueryAPI": 0,
    "eglChooseConfig": 5, "eglGetConfigs": 4, "eglGetConfigAttrib": 4,
    "eglCreateContext": 4, "eglDestroyContext": 2,
    "eglCreateWindowSurface": 4, "eglCreatePbufferSurface": 3,
    "eglCreatePbufferFromClientBuffer": 5,
    "eglDestroySurface": 2, "eglMakeCurrent": 4, "eglQuerySurface": 4,
    "eglSwapBuffers": 2, "eglSurfaceAttrib": 4, "eglSwapInterval": 2,
    "eglGetCurrentContext": 0, "eglGetCurrentSurface": 1, "eglGetCurrentDisplay": 0,
    "eglQueryFrameDMP": 3,
    "vgDrawPathDataDMP": 8, "vgCreateImageStandardDataDMP": 6,
    "vgDrawSubImageDMP": 5,
    "__egl_native_window_bridge__": 3,
    "__egl_frame_mode_control__": 2,
}


def f32(word):
    return struct.unpack("<f", struct.pack("<I", word & 0xffffffff))[0]


def word_float(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def signed(word):
    return word - 0x100000000 if word & 0x80000000 else word


class GraphicsHLE:
    """Stop-safe API adapter. `handle` returns an event, or raises UnsupportedVG.

    `breakpoints` maps low executable address to API name. Install only after
    opting in to driver replacement, and keep all entries installed so a fake
    host object handle cannot leak into an original unsupported driver API.
    `regs` may be a list of r0..r15 or a dict using r0..r12/sp/lr/pc.
    Current GDB CPU selection is never changed.
    """
    def __init__(self, emu, output_dir, window_resolver=None):
        self.emu = emu
        self.backend = OpenVGBackend(output_dir)
        named = json.loads(Path(__file__).with_name("graphics-api-table.json").read_text())["apis"]
        self.breakpoints = {int(x["entry"],16): x["name"] for x in named
                            if x["name"].startswith(("egl", "vg"))}
        self.breakpoints.update({pc: row[0] for pc,row in CORE.items()})
        self.breakpoints[0x065daab4]="__unmodeled_vg_context_lookup__"
        # These original app helpers directly inspect driver/native structures.
        # A software surface handle must never be passed through them unchecked.
        self.breakpoints[0x064c46c4]="__egl_native_window_bridge__"
        # Not named by the export table. Native body validates mode 0 or 3
        # and stores current draw surface+94; see frame query evidence.
        self.breakpoints[0x064c42b8]="__egl_frame_mode_control__"
        # Native queries 064de7da and 064de686 run unchanged on original DWD
        # objects. The latter's E010 query returns a buffer index, not format.
        # The bridge below ensures callers receive a captured original native
        # window rather than interpreting a host software-surface handle.
        self.arg_counts = {name:n for name,n in CORE.values()} | ARGUMENTS
        self.window_resolver = window_resolver
        self.native_windows = {}
        self.contexts = set()
        self.context_attributes = {}
        self.current_context = 0
        self.display = 0x6f000001
        self.initialized_displays = set()
        self.config = 0x6f000002
        # Original config ID 6, source row 5 at 0701d8dc+5*84. The original
        # initializer 064cff9c copies RGBA=8 each, buffer=32, depth/stencil=0.
        # Only attributes implemented by this software surface are offered.
        self.config_attributes = {0x3028:6,0x3020:32,0x3021:8,0x3022:8,
                                  0x3023:8,0x3024:8,0x3025:0,0x3026:0,
                                  0x3031:0,0x3032:0,0x3040:2}
        self.api = 0x30a1
        self.egl_error = 0x3000
        self.surface_attributes = {}
        self.surface_windows = {}
        self.image_info = {}

    @staticmethod
    def register(regs, index):
        if not isinstance(regs,dict): return regs[index]
        name = ("sp" if index==13 else "lr" if index==14 else "pc" if index==15 else "r%d"%index)
        value = regs.get(name, regs.get("r%d"%index, regs.get(index)))
        if value is None: raise ValueError("Missing register " + name)
        return int(value,0) if isinstance(value,str) else value

    def read(self, address, count):
        if count < 0 or count > 128 * 1024 * 1024: raise ValueError("Graphics guest read exceeds bounds")
        return bytes(self.emu.read_mem(address, count))

    def vector(self, pointer, count, fmt):
        if not 0 <= count <= 4_000_000: raise ValueError("Graphics vector exceeds bounds")
        return list(struct.unpack("<"+str(count)+fmt, self.read(pointer,count*struct.calcsize(fmt))))

    def put(self, pointer, values, fmt="I"):
        if pointer: self.emu.write_mem(pointer,struct.pack("<"+str(len(values))+fmt,*values))

    def attributes(self, pointer):
        values = {}
        if pointer:
            for index in range(128):
                key = self.vector(pointer+index*8,1,"I")[0]
                if key == 0x3038: return values
                values[key] = self.vector(pointer+index*8+4,1,"I")[0]
            raise UnsupportedVG("Unterminated EGL attribute list")
        return values

    def register_native_window(self, handle, width, height, evidence):
        if not handle or not evidence: raise ValueError("Native window registration needs captured handle and evidence")
        self.native_windows[handle] = (width,height,evidence)
        self.backend.log("observed_native_window",handle=hex(handle),width=width,height=height,evidence=evidence)

    def native_window_arguments(self, regs):
        """At real 064deeba entry capture these; register its *actual* return handle later."""
        return {"width":self.register(regs,1),"height":self.register(regs,2),
                "evidence":"original native window call 0x064deeba r1/r2 and its returned r0"}

    def handle(self, pc, regs):
        pc &= ~1
        name = self.breakpoints.get(pc)
        if name is None: return False
        count = self.arg_counts.get(name)
        base_args = [self.register(regs,i) for i in range(4)]
        call = {"pc":hex(pc),"api":name,"r0_r3":base_args,"lr":hex(self.register(regs,14))}
        self.backend.log("guest_api_entry",**call)
        try:
            if count is None: raise UnsupportedVG("ABI/implementation not established for " + name)
            args = base_args[:count]
            if count>4: args += self.vector(self.register(regs,13),count-4,"I")
            result = self.call(name,args)
        except Exception as error:
            self.backend.log("unsupported_or_invalid_call",**call,error=str(error),exception=type(error).__name__)
            # Do not advance PC, set a success return, or invent a rendered frame.
            raise
        if result is not None: self.emu.write_reg(0,result & 0xffffffff)
        lr = self.register(regs,14)
        # Equivalent to BX LR, preserving other flags and mode bits.
        cpsr = self.emu.read_reg(25)
        self.emu.write_reg(25,(cpsr | 0x20) if lr&1 else (cpsr & ~0x20))
        self.emu.write_reg(15,lr&~1)
        self.backend.log("guest_api_return",api=name,result=result,return_pc=hex(lr&~1))
        return {"api":name,"result":result,"software_driver":True}

    def path_data(self,datatype,count,segments,coords):
        if datatype not in (0,1,2,3): raise UnsupportedVG("Path datatype " + str(datatype))
        if not 0<count<=1_000_000: raise ValueError("Path segment count exceeds bounds")
        seg = list(self.read(segments,count))
        if any((x&~1) not in COORD_COUNTS for x in seg): raise UnsupportedVG("Unknown captured path segment")
        number = sum(COORD_COUNTS[x&~1] for x in seg)
        return seg,self.vector(coords,number,("b","h","i","f")[datatype])

    def call(self, name, a):
        b = self.backend
        if name=="__egl_frame_mode_control__":
            canvas=self.frame_query_surface(a[0])
            if a[1] not in (0,3):raise UnsupportedVG("Unknown original frame mode "+hex(a[1]))
            # Original 064e066c skips only GPU command kinds 1/8/10 when
            # surface+a0==1 and mode==3. Our renderer produces no such GPU
            # fault; if one is introduced, do not guess the job mapping.
            if canvas.frame_retry_status:
                raise UnsupportedVG("GPU frame-recovery job filtering has no software equivalent")
            canvas.frame_mode=a[1];self.egl_error=0x3000
            b.log("original_frame_mode",surface=hex(b.surface),mode=a[1],
                  implementation="state retained; GPU-fault-specific job filtering is not emulated")
            return 1
        if name=="__egl_native_window_bridge__":
            native=self.surface_windows.get(a[1])
            if native is None:raise UnsupportedVG("No captured original native window for software surface")
            if not a[2]:raise ValueError("Native-window output pointer is null")
            self.put(a[2],[native]);return 1
        if name.startswith("egl"): return self.egl(name,a)
        if name=="vgGetError": value=b.error;b.error=0;return value
        if name in ("vgFlush","vgFinish"):
            # This software driver completes drawing synchronously.
            if b.surface is not None:b.q.CGContextFlush(b.surfaces[b.surface].context)
            return
        if name=="vgLoadIdentity": b.matrix(IDENTITY);return
        if name in ("vgLoadMatrix","vgMultMatrix"): b.matrix(self.vector(a[0],9,"f"),name=="vgMultMatrix");return
        if name=="vgGetMatrix": self.put(a[0],b.matrix(),"f");return
        if name in ("vgTranslate","vgScale","vgShear","vgRotate"):
            v=list(map(f32,a));m=IDENTITY.copy()
            if name=="vgTranslate":m[6:8]=v
            elif name=="vgScale":m[0],m[4]=v
            elif name=="vgShear":m[3],m[1]=v
            else:
                angle=math.radians(v[0]);m[0]=m[4]=math.cos(angle);m[1]=math.sin(angle);m[3]=-m[1]
            b.matrix(m,True);return
        if name in ("vgSetf","vgSeti","vgSetfv","vgSetiv"):
            values=self.vector(a[2],a[1],"f" if name=="vgSetfv" else "i") if name.endswith("v") else [f32(a[1]) if name=="vgSetf" else signed(a[1])]
            b.set_state(a[0],values);return
        if name in ("vgGetf","vgGeti","vgGetVectorSize","vgGetfv","vgGetiv"):
            if a[0] not in b.state:raise UnsupportedVG("Unknown queried VG state "+hex(a[0]))
            v=b.state[a[0]];v=v if isinstance(v,list) else [v]
            if name=="vgGetVectorSize":return len(v)
            if name.endswith("v"):
                if a[1]>len(v):raise ValueError("State vector count is larger than vector")
                fmt="f" if name=="vgGetfv" else "i";self.put(a[2],[float(x) if fmt=="f" else int(x) for x in v[:a[1]]],fmt);return
            return word_float(v[0]) if name=="vgGetf" else int(v[0])
        if name=="vgClear":b.clear(*map(signed,a));return
        if name=="vgCreatePaint":return b.create_paint()
        if name=="vgDestroyPaint":b.destroy_paint(a[0]);return
        if name=="vgSetPaint":b.set_paint(*a);return
        if name=="vgGetPaint":return b.get_paint(a[0])
        if name in ("vgSetColor","vgGetColor"):
            if a[0] not in b.paints:b.error=0x1000;return 0 if name=="vgGetColor"else None
            if name=="vgSetColor":b.paints[a[0]][0x1a01]=[(a[1]>>shift&255)/255 for shift in (24,16,8,0)];return
            return sum(round(x*255)<<shift for x,shift in zip(b.paints[a[0]][0x1a01],(24,16,8,0)))
        if name.startswith("vgSetParameter"):
            v=self.vector(a[3],a[2],"f" if name.endswith("fv") else "i") if name.endswith("v") else [f32(a[2]) if name.endswith("f") else signed(a[2])]
            b.set_paint_parameter(a[0],a[1],v);return
        if name=="vgGetParameterfv":
            if a[0] not in b.paints:
                if a[0] in b.paths or a[0] in b.images:raise UnsupportedVG("Non-paint GetParameterfv is not implemented")
                b.error=0x1000;return
            if a[1] not in b.paints[a[0]]:raise UnsupportedVG("Unknown paint query parameter "+hex(a[1]))
            values=b.paints[a[0]][a[1]];values=values if isinstance(values,list)else [values]
            if not 0<a[2]<=len(values) or not a[3] or a[3]&3:b.error=0x1001;return
            self.put(a[3],[float(x)for x in values[:a[2]]],"f");return
        if name=="vgCreatePath":
            if a[0]!=0:raise UnsupportedVG("Nonstandard path format")
            return b.create_path(a[1],f32(a[2]),f32(a[3]),a[6])
        if name=="vgClearPath":
            b.paths[a[0]]["segments"]=[];b.paths[a[0]]["coords"]=[];b.paths[a[0]]["capabilities"]=a[1]&0xfff;return
        if name=="vgDestroyPath":del b.paths[a[0]];return
        if name=="vgRemovePathCapabilities":
            if a[0] not in b.paths:b.error=0x1000;return
            b.paths[a[0]]["capabilities"] &= ~a[1];return
        if name=="vgAppendPathData":
            p=b.paths[a[0]];s,c=self.path_data(p["datatype"],*a[1:]);p["segments"]+=s;p["coords"]+=c;return
        if name=="vgTransformPath":b.transform_path(*a);return
        if name=="vgDrawPath":
            p=b.paths[a[0]];b.draw_path_data(p["segments"],p["coords"],a[1],p["datatype"],p["scale"],p["bias"]);return
        if name=="vgDrawPathDataDMP":
            if a[0]!=0:raise UnsupportedVG("Nonstandard direct path format")
            s,c=self.path_data(a[1],a[4],a[5],a[6]);b.draw_path_data(s,c,a[7],a[1],f32(a[2]),f32(a[3]));return
        if name=="vgCreateImageStandardDataDMP":
            # Unlike vgImageSubData, this DMP entry takes the whole standard
            # allocation. Original 065e7224(mode0) rounds width to16 except
            # exact width8, and height to8. 065e73ae stores pitch as aligned
            # width * bytes/pixel; 065e7bea checks the full padded size.
            base=a[0]&0x3f
            bpp=4 if base in (0,1,2) else 2 if base in (3,4,5) else 1 if base in (6,11) else None
            if bpp is None:raise UnsupportedVG("DMP standard-image storage format "+hex(a[0]))
            width,height=a[1:3]
            storage_width=8 if width==8 else (width+15)&~15
            storage_height=(height+7)&~7;pitch=storage_width*bpp
            if not width or not height or a[3]!=pitch*storage_height:
                raise ValueError("DMP image allocation does not match original padded geometry")
            h=b.create_image(self.read(a[4],a[3]),a[0],width,height,pitch)
            self.image_info[h]=(a[0],width,height)
            b.log("original_standard_image_layout",image=hex(h),width=width,height=height,
                  storage_width=storage_width,storage_height=storage_height,pitch=pitch,bytes=a[3])
            return h
        if name=="vgDestroyImage":b.destroy_image(a[0]);self.image_info.pop(a[0],None);return
        if name=="vgDrawImage":b.draw_image(a[0]);return
        if name=="vgDrawSubImageDMP":b.draw_image(a[0],*map(signed,a[1:]));return
        raise UnsupportedVG("Not implemented: " + name)

    def frame_query_surface(self,display):
        if display!=self.display or display not in self.initialized_displays:
            raise UnsupportedVG("Frame control requires an initialized software EGL display")
        if self.api!=0x30a1 or self.current_context not in self.contexts:
            raise UnsupportedVG("Frame control requires a current OpenVG context")
        if self.backend.surface not in self.backend.surfaces:
            raise UnsupportedVG("Frame control requires a current draw surface")
        return self.backend.surfaces[self.backend.surface]

    def egl(self,name,a):
        b=self.backend
        if name=="eglGetError":v=self.egl_error;self.egl_error=0x3000;return v
        if name=="eglGetDisplay":return self.display
        if name=="eglInitialize":
            if a[0]!=self.display:raise UnsupportedVG("Unknown software EGL display")
            self.put(a[1],[1]);self.put(a[2],[4]);self.initialized_displays.add(a[0]);return 1
        if name in ("eglChooseConfig","eglGetConfigs"):
            if name=="eglChooseConfig":
                attrs=self.attributes(a[1]);configs,size,number=a[2:]
                # This exact selection is present at 07435b54 in the original
                # S5 application; broader filters need capability matching.
                if attrs!={0x3028:6}:raise UnsupportedVG("Only observed original config ID 6 selection is supported: "+str(attrs))
            else:configs,size,number=a[1:]
            if not number:raise ValueError("EGL config count output is null")
            if configs and size:self.put(configs,[self.config])
            self.put(number,[1 if not configs or size else 0]);return 1
        if name=="eglGetConfigAttrib":
            if a[1]!=self.config:raise ValueError("Unknown software EGL config")
            if a[2] not in self.config_attributes:raise UnsupportedVG("Unimplemented software config attribute "+hex(a[2]))
            self.put(a[3],[self.config_attributes[a[2]]]);return 1
        if name=="eglBindAPI":
            if a[0]!=0x30a1:raise UnsupportedVG("Only OpenVG API is implemented")
            self.api=a[0];return 1
        if name=="eglQueryAPI":return self.api
        if name=="eglCreateContext":
            if self.contexts:raise UnsupportedVG("Multiple independent EGL contexts need separate VG state")
            attrs=self.attributes(a[3])
            # Captured original caller 0728fddc, isolated-display-lcd. The
            # original OpenVG validator 064c3224..064c3346 accepts these
            # resource-size parameters, then copies them into context fields
            # +3c,+40,+44,+48,+4c,+50 at 064d83f6..064d840c. No framebuffer,
            # pixel-format or rendering state is encoded by this exact list.
            # Host memory replaces the GPU's allocation pools; record those
            # requested sizes without claiming identical pool exhaustion.
            observed={0x3ff7:0x80000,0x3ff8:0x40000,0x3ff9:0x10000,
                      0x3ffa:0x10000,0x3fce:0x10000,0x3fcf:0x10000}
            if attrs and attrs!=observed:
                raise UnsupportedVG("EGL context attributes need review: "+str(attrs))
            if a[2]:raise UnsupportedVG("Shared EGL contexts are not implemented")
            h=b.handle(7);self.contexts.add(h);self.context_attributes[h]=attrs
            if attrs:b.log("original_context_resource_requests",context=hex(h),attributes={hex(k):v for k,v in attrs.items()},
                           implementation="host allocations; original GPU pool limits are not emulated")
            return h
        if name=="eglGetCurrentContext":return self.current_context
        if name=="eglGetCurrentDisplay":return self.display if self.current_context else 0
        if name=="eglGetCurrentSurface":return b.surface or 0
        if name=="eglQueryFrameDMP":
            # Native 064c4362 takes (display, attribute, out_u32), selecting
            # the calling OpenVG context's current draw surface. 064daee2
            # returns surface+a0 and immediately clears that one word. Its
            # sole setter is GPU status-bit-26 recovery, not ordinary drawing
            # and not a framebuffer pointer. See query-frame-dmp.disasm.txt.
            canvas=self.frame_query_surface(a[0])
            if a[1]!=0x3fff:raise UnsupportedVG("Unknown frame-query attribute "+hex(a[1]))
            if not a[2] or a[2]&3:raise ValueError("Frame-query output must be a nonnull aligned word")
            value=canvas.frame_retry_status
            self.put(a[2],[value]);canvas.frame_retry_status=0;self.egl_error=0x3000
            b.log("original_frame_status_read_clear",surface=hex(b.surface),value=value,
                  implementation="software rendering; original GPU bit-26 recovery is not generated")
            return 1
        if name=="eglCreateWindowSurface":
            attrs=self.attributes(a[3])
            # Standard EGL 1.2/1.3: VG_ALPHA_FORMAT_PRE and VG_COLORSPACE_sRGB.
            # Captured at 0728fea8. Canvas uses a named sRGB, premultiplied
            # RGBA bitmap; linear-light and NONPRE requests still stop.
            if attrs and attrs!={0x3088:0x308c,0x3087:0x3089}:
                raise UnsupportedVG("Window surface attributes need review: "+str(attrs))
            dims=self.native_windows.get(a[2])
            if dims is None and self.window_resolver:dims=self.window_resolver(a[2],self.read)
            if dims is None:raise UnsupportedVG("Capture original 064deeba r1/r2 and returned window handle before creating this surface")
            h=b.create_surface(dims[0],dims[1],origin=dims[2]);self.surface_attributes[h]=attrs.copy()
            self.surface_windows[h]=a[2];return h
        if name=="eglCreatePbufferSurface":
            attrs=self.attributes(a[2])
            if set(attrs)-{0x3057,0x3056}:raise UnsupportedVG("Pbuffer attributes need review")
            h=b.create_surface(attrs.get(0x3057,0),attrs.get(0x3056,0),origin="captured EGL pbuffer attributes")
            self.surface_attributes[h]={};return h
        if name=="eglCreatePbufferFromClientBuffer":
            # Captured 07290600 passes EGL_OPENVG_IMAGE and a null fifth
            # argument. The size, colorspace and alpha format come from the
            # actual client image, not EGL_WIDTH/EGL_HEIGHT attributes.
            if a[0]!=self.display or a[0] not in self.initialized_displays:
                raise UnsupportedVG("Client-buffer surface requires initialized display")
            if a[1]!=0x3096 or a[3]!=self.config:
                raise UnsupportedVG("Unknown client-buffer type or config")
            if self.attributes(a[4]):raise UnsupportedVG("Client-buffer attributes require review")
            info=self.image_info.get(a[2])
            if info is None or a[2] not in b.images:raise UnsupportedVG("Unknown client VGImage")
            if info[0]&0x3f!=2:
                raise UnsupportedVG("Only captured sRGBA_8888_PRE client-image format is implemented")
            h=b.create_image_surface(a[2])
            self.surface_attributes[h]={0x3088:0x308c,0x3087:0x3089}
            self.egl_error=0x3000;return h
        if name=="eglMakeCurrent":
            if a[1]!=a[2]:raise UnsupportedVG("Separate draw/read surfaces")
            if a[3] not in self.contexts:raise UnsupportedVG("Unknown EGL context")
            b.make_current(a[1]);self.current_context=a[3];return 1
        if name=="eglQuerySurface":
            canvas=b.surfaces[a[1]];values={0x3057:canvas.width,0x3056:canvas.height,
                                        0x3093:self.surface_attributes[a[1]].get(0x3093,0x3094),
                                        0x3088:0x308c,0x3087:0x3089}
            if a[2] not in values:raise UnsupportedVG("EGL surface query "+hex(a[2]))
            self.put(a[3],[values[a[2]]]);return 1
        if name=="eglSwapBuffers":b.swap(a[1]);return 1
        if name=="eglSurfaceAttrib":
            if a[2]==0x3fdd and a[3]==0x3fde:
                if a[1] not in self.surface_windows:
                    raise UnsupportedVG("Observed linear-buffer mode requires a native window surface")
                # Original 064c2aaa stores this in surface+c8. Its consumer
                # 064d2ebe/064d2f34 selects the linear CPU buffer-copy path
                # 06503e3c (row memcpy), replacing a graphics-engine path.
                # The software driver operates on a linear host bitmap. This
                # does not establish native guest channel packing or synchronize
                # guest DWD buffers; exported frames are host-driver output.
                self.surface_attributes[a[1]][a[2]]=a[3]
                b.log("original_linear_buffer_mode",surface=hex(a[1]),attribute=hex(a[2]),value=hex(a[3]),
                      implementation="linear host bitmap; native guest pixel buffers are not synchronized")
                return 1
            if a[2]!=0x3093 or a[3] not in (0x3094,0x3095):raise UnsupportedVG("EGL surface attribute needs review: "+str(a[2:]))
            # DESTROYED permits undefined contents after swap; retaining them
            # is also valid. PRESERVED requires retaining them, which we do.
            self.surface_attributes[a[1]][a[2]]=a[3];return 1
        if name=="eglSwapInterval":return 1  # Host capture has no real display cadence.
        if name=="eglDestroySurface":
            b.destroy_surface(a[1]);self.surface_windows.pop(a[1],None)
            self.surface_attributes.pop(a[1],None);return 1
        if name=="eglDestroyContext":
            if a[1]==self.current_context:raise UnsupportedVG("Destroying current context")
            self.contexts.remove(a[1]);self.context_attributes.pop(a[1],None);return 1
        # Config selection deliberately waits for actual caller requirements;
        # it must not claim support for unimplemented formats or semantics.
        raise UnsupportedVG("EGL operation requires captured caller requirements: " + name)

    def close(self):
        self.backend.close()
