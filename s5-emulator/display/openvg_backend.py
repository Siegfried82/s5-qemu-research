#!/usr/bin/env python3
"""Explicit software rendering of captured OpenVG calls, using macOS CoreGraphics.

No menu layout, labels, fonts or images are invented here. Callers must supply
the original firmware's path segments, coordinates, image bytes and state.
Unsupported drawing states raise instead of silently producing a substitute.
"""
import ctypes as ct
from ctypes.util import find_library
import hashlib
import json
import math
from pathlib import Path
import struct

from PIL import Image


class UnsupportedVG(RuntimeError):
    pass


class Point(ct.Structure):
    _fields_=[("x",ct.c_double),("y",ct.c_double)]
class Size(ct.Structure):
    _fields_=[("width",ct.c_double),("height",ct.c_double)]
class Rect(ct.Structure):
    _fields_=[("origin",Point),("size",Size)]
class Affine(ct.Structure):
    _fields_=[(key,ct.c_double) for key in ("a","b","c","d","tx","ty")]


def rectangle(x,y,w,h):
    return Rect(Point(x,y),Size(w,h))


class Quartz:
    def __init__(self):
        location=find_library("CoreGraphics")
        if not location:raise UnsupportedVG("CoreGraphics is unavailable on this host")
        self.lib=ct.CDLL(location)
        P,F,N,B=ct.c_void_p,ct.c_double,ct.c_size_t,ct.c_bool
        defs={
            "CGColorSpaceCreateDeviceRGB":([],P),"CGColorSpaceCreateWithName":([P],P),"CGColorSpaceRelease":([P],None),
            "CGBitmapContextCreate":([P,N,N,N,N,P,ct.c_uint32],P),"CGContextRelease":([P],None),
            "CGContextSaveGState":([P],None),"CGContextRestoreGState":([P],None),
            "CGContextSetRGBFillColor":([P,F,F,F,F],None),"CGContextSetRGBStrokeColor":([P,F,F,F,F],None),
            "CGContextSetBlendMode":([P,ct.c_int],None),"CGContextSetShouldAntialias":([P,B],None),
            "CGContextSetInterpolationQuality":([P,ct.c_int],None),
            "CGContextFillRect":([P,Rect],None),"CGContextClipToRects":([P,ct.POINTER(Rect),N],None),
            "CGContextConcatCTM":([P,Affine],None),"CGContextSetLineWidth":([P,F],None),
            "CGContextSetLineCap":([P,ct.c_int],None),"CGContextSetLineJoin":([P,ct.c_int],None),
            "CGContextSetMiterLimit":([P,F],None),"CGContextSetLineDash":([P,F,ct.POINTER(F),N],None),
            "CGPathCreateMutable":([],P),"CGPathRelease":([P],None),
            "CGPathMoveToPoint":([P,P,F,F],None),"CGPathAddLineToPoint":([P,P,F,F],None),
            "CGPathAddQuadCurveToPoint":([P,P,F,F,F,F],None),"CGPathAddCurveToPoint":([P,P,F,F,F,F,F,F],None),
            "CGPathCloseSubpath":([P],None),"CGContextAddPath":([P,P],None),"CGContextDrawPath":([P,ct.c_int],None),
            "CGContextClip":([P],None),"CGContextEOClip":([P],None),
            "CGContextReplacePathWithStrokedPath":([P],None),"CGContextGetClipBoundingBox":([P],Rect),
            "CGGradientCreateWithColorComponents":([P,ct.POINTER(F),ct.POINTER(F),N],P),
            "CGGradientRelease":([P],None),"CGContextDrawLinearGradient":([P,P,Point,Point,ct.c_uint32],None),
            "CGDataProviderCreateWithData":([P,P,N,P],P),"CGDataProviderRelease":([P],None),
            "CGImageCreate":([N,N,N,N,N,P,ct.c_uint32,P,P,B,ct.c_int],P),"CGImageRelease":([P],None),
            "CGContextDrawImage":([P,Rect,P],None),"CGContextFlush":([P],None)}
        for name,(args,result) in defs.items():
            fun=getattr(self.lib,name);fun.argtypes=args;fun.restype=result;setattr(self,name,fun)
        self.srgb_name=ct.c_void_p.in_dll(self.lib,"kCGColorSpaceSRGB")


class Canvas:
    def __init__(self,q,width,height):
        if not (0<width<=8192 and 0<height<=8192 and width*height<=32_000_000):
            raise ValueError("Invalid bounded surface geometry")
        self.q,self.width,self.height=q,width,height
        # Original surface+a0 is a read-and-clear flag set only after the GPU
        # wait handler observes and resets status bit 26 (064e5e50..64e5ebc).
        # This synchronous software backend has no corresponding GPU fault.
        # Unsupported rendering still raises; it is never reported as success
        # through this flag. Keep a real latch rather than a constant query.
        self.frame_retry_status=0
        self.frame_mode=0
        self.buffer=(ct.c_ubyte*(width*height*4))()
        self.colors=q.CGColorSpaceCreateWithName(q.srgb_name)
        # Big-endian RGBA bytes, premultiplied alpha. Quartz's first memory row
        # is the top display row; OpenVG/Quartz user coordinates are bottom-up.
        self.context=q.CGBitmapContextCreate(self.buffer,width,height,8,width*4,self.colors,0x4001)
        if not self.context:raise RuntimeError("Cannot create CoreGraphics bitmap")
    def image(self):
        return self.premultiplied_image().convert("RGBA")
    def premultiplied_image(self):
        self.q.CGContextFlush(self.context)
        return Image.frombytes("RGBa",(self.width,self.height),bytes(self.buffer))
    def close(self):
        if self.context:self.q.CGContextRelease(self.context);self.context=None
        if self.colors:self.q.CGColorSpaceRelease(self.colors);self.colors=None


IDENTITY=[1.,0.,0.,0.,1.,0.,0.,0.,1.]
COORD_COUNTS={0:0,2:2,4:2,6:1,8:1,10:4,12:6,14:2,16:4,18:5,20:5,22:5,24:5}


def multiply(a,b):
    return [sum(a[k*3+row]*b[col*3+k] for k in range(3)) for col in range(3) for row in range(3)]


def path_operations(segments,coordinates,scale=1.,bias=0.):
    """Translate captured OpenVG path data into absolute Quartz operations."""
    if not math.isfinite(scale) or not math.isfinite(bias) or scale==0:raise ValueError("Invalid path scale/bias")
    values=iter([float(v)*scale+bias for v in coordinates])
    current=(0.,0.);start=current;last_quad=None;last_cubic=None;ops=[]
    for segment in segments:
        op=segment&~1;relative=segment&1
        if op not in COORD_COUNTS:raise UnsupportedVG("Unknown path segment: "+hex(segment))
        try:v=[next(values) for _ in range(COORD_COUNTS[op])]
        except StopIteration:raise ValueError("Insufficient captured path coordinates") from None
        if not all(math.isfinite(x) for x in v):raise ValueError("Non-finite path coordinates")
        def point(x,y):return (x+current[0],y+current[1]) if relative else (x,y)
        old=current;quad=cubic=None
        if op==0:ops.append(("Z",));current=start
        elif op==2:current=point(*v);start=current;ops.append(("M",*current))
        elif op==4:current=point(*v);ops.append(("L",*current))
        elif op==6:current=(v[0]+current[0] if relative else v[0],current[1]);ops.append(("L",*current))
        elif op==8:current=(current[0],v[0]+current[1] if relative else v[0]);ops.append(("L",*current))
        elif op in (10,14):
            if op==10:quad=point(*v[:2]);end=point(*v[2:])
            else:quad=(2*current[0]-last_quad[0],2*current[1]-last_quad[1]) if last_quad else current;end=point(*v)
            ops.append(("Q",*quad,*end));current=end
        elif op in (12,16):
            if op==12:c1=point(*v[:2]);cubic=point(*v[2:4]);end=point(*v[4:])
            else:c1=(2*current[0]-last_cubic[0],2*current[1]-last_cubic[1]) if last_cubic else current;cubic=point(*v[:2]);end=point(*v[2:])
            ops.append(("C",*c1,*cubic,*end));current=end
        else:
            end=point(*v[3:]);ops.extend(arc_cubics(old,end,v[0],v[1],v[2],op in (22,24),op in (18,22)));current=end
        last_quad,last_cubic=quad,cubic
    try:next(values)
    except StopIteration:return ops
    raise ValueError("Unused captured path coordinates")


def arc_cubics(start,end,rx,ry,rotation,large,ccw):
    """Endpoint ellipse to cubic curves; maximum angular span is pi/2."""
    if start==end:return []
    rx,ry=abs(rx),abs(ry)
    if not rx or not ry:return [("L",*end)]
    phi=math.radians(rotation%360);co,si=math.cos(phi),math.sin(phi)
    dx,dy=(start[0]-end[0])/2,(start[1]-end[1])/2
    x,y=co*dx+si*dy,-si*dx+co*dy
    ratio=x*x/(rx*rx)+y*y/(ry*ry)
    if ratio>1:factor=math.sqrt(ratio);rx*=factor;ry*=factor
    numerator=max(0,rx*rx*ry*ry-rx*rx*y*y-ry*ry*x*x)
    denominator=rx*rx*y*y+ry*ry*x*x
    factor=math.sqrt(numerator/denominator) if denominator else 0
    if large==ccw:factor=-factor
    cxp,cyp=factor*rx*y/ry,-factor*ry*x/rx
    cx=co*cxp-si*cyp+(start[0]+end[0])/2;cy=si*cxp+co*cyp+(start[1]+end[1])/2
    angle=math.atan2((y-cyp)/ry,(x-cxp)/rx)
    end_angle=math.atan2((-y-cyp)/ry,(-x-cxp)/rx)
    delta=(end_angle-angle)%(2*math.pi)
    if not ccw:delta-=2*math.pi
    n=max(1,math.ceil(abs(delta)/(math.pi/2)));step=delta/n;result=[]
    def xy(t):return cx+rx*co*math.cos(t)-ry*si*math.sin(t),cy+rx*si*math.cos(t)+ry*co*math.sin(t)
    def deriv(t):return -rx*co*math.sin(t)-ry*si*math.cos(t),-rx*si*math.sin(t)+ry*co*math.cos(t)
    for k in range(n):
        a,b=angle+k*step,angle+(k+1)*step;t=4/3*math.tan(step/4)
        p0,p1=xy(a),xy(b);d0,d1=deriv(a),deriv(b)
        result.append(("C",p0[0]+t*d0[0],p0[1]+t*d0[1],p1[0]-t*d1[0],p1[1]-t*d1[1],*p1))
    result[-1]=(*result[-1][:-2],*end)
    return result


def decode_image(data,format,width,height,stride=None):
    """Read actual packed OpenVG standard-format bytes, not host font substitutes."""
    base=format&0x3f;flags=format&0xc0
    if base in (0,1,2,7,8,9):bpp=4
    elif base in (3,4,5):bpp=2
    elif base in (6,10,11):bpp=1
    else:raise UnsupportedVG("Packed OpenVG image format "+hex(format))
    # Linear-light formats require correct color treatment; refuse for now.
    if base in (7,8,9,10):raise UnsupportedVG("Linear-light OpenVG images are not implemented")
    stride=width*bpp if stride is None else stride
    if stride<width*bpp or len(data)<stride*(height-1)+width*bpp:raise ValueError("Captured image buffer is short")
    raw=b"".join(data[y*stride:y*stride+width*bpp] for y in range(height))
    if bpp==4:
        order={0:"ABGR",0x40:"BGRA",0x80:"ARGB",0xc0:"RGBA"}[flags]
        image=Image.frombytes("RGBA",(width,height),raw,"raw",order)
        if base==0:image.putalpha(255)
        elif base==2:image=Image.frombytes("RGBa",image.size,image.tobytes()).convert("RGBA")
    elif bpp==2:
        if flags:raise UnsupportedVG("Swizzled 16-bit OpenVG image format")
        rgba=bytearray(width*height*4)
        for index,(value,) in enumerate(struct.iter_unpack("<H",raw)):
            if base==3:r,g,b,a=(value>>11)*255//31,((value>>5)&63)*255//63,(value&31)*255//31,255
            elif base==4:r,g,b,a=(value>>11)*255//31,((value>>6)&31)*255//31,((value>>1)&31)*255//31,(value&1)*255
            else:r,g,b,a=((value>>12)&15)*17,((value>>8)&15)*17,((value>>4)&15)*17,(value&15)*17
            rgba[index*4:index*4+4]=bytes((r,g,b,a))
        image=Image.frombytes("RGBA",(width,height),bytes(rgba))
    elif base==6:image=Image.frombytes("L",(width,height),raw).convert("RGBA")
    else:
        # A_8 uses alpha only; RGB components are one in the OpenVG image model.
        image=Image.new("RGBA",(width,height),(255,255,255,255));image.putalpha(Image.frombytes("L",(width,height),raw))
    # Standard image data is supplied with bottom scanline first.
    return image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)


class OpenVGBackend:
    def __init__(self,output):
        self.output=Path(output);self.output.mkdir(parents=True,exist_ok=True)
        self.q=Quartz();self.surfaces={};self.surface=None;self.images={};self.paths={};self.paints={}
        self.image_premultiplied={};self.image_surfaces={};self.surface_images={}
        self.next_id=1;self.draw_count=0;self.swaps=0;self.events=[];self.error=0
        self.state={0x1100:0x1400,0x1101:0x1900,0x1102:4,0x1103:0x1202,0x1104:0x2001,0x1105:0x1f00,
                    0x1106:[],0x1110:1.,0x1111:0x1700,0x1112:0x1800,0x1113:4.,0x1114:[],0x1115:0.,
                    0x1116:0,0x1120:[0.,0.,0.,0.],0x1121:[0.,0.,0.,0.],0x1130:0,0x1131:0}
        self.matrices={mode:IDENTITY.copy() for mode in range(0x1400,0x1405)}
        self.bound_paints={1:0,2:0};self.retired_paints={}

    def log(self,name,**values):
        event={"index":len(self.events),"name":name,**values};self.events.append(event)
        with (self.output/"calls.jsonl").open("a") as stream:stream.write(json.dumps(event)+"\n")

    def handle(self,kind):
        value=(kind<<28)|self.next_id;self.next_id+=1;return value

    def create_surface(self,width,height,origin="captured guest dimensions"):
        h=self.handle(6);self.surfaces[h]=Canvas(self.q,width,height)
        self.log("create_surface",handle=hex(h),width=width,height=height,origin=origin);return h

    def make_current(self,surface):
        if surface not in self.surfaces:raise ValueError("Unknown software surface")
        self.surface=surface;self.log("make_current",surface=hex(surface))

    def set_state(self,param,values):
        if param==0xc070:
            # Original 064dd20c..064dd222: one integer, multiple of 8,
            # inclusive range8..2048; stored at VG-state+1c0. The software
            # driver has its own memory/geometry limits, not the GPU's limits.
            if len(values)!=1 or values[0]!=int(values[0]) or not 8<=values[0]<=2048 or int(values[0])&7:
                raise ValueError("Original C070 resource configuration requires 8..2048, aligned to8")
            self.state[param]=int(values[0])
            self.log("vendor_resource_configuration",parameter=hex(param),values=list(values),
                     implementation="retained; host rasterizer uses its own bounded resource limits",
                     official_enum_name=None)
            return
        if param in range(0xc120,0xc126):
            # Original 064dd256..064dd36e and extension string at06ff436d:
            # DMPX_conversion_YUV_hw_raw_setting. Preserve exact hardware
            # coefficient widths and clamps. The implemented RGB surfaces and
            # standard RGB/L/A image formats do not enter YUV conversion;
            # YUV/native-format image APIs are still unsupported and stop.
            count={0xc120:6,0xc121:2,0xc122:3,0xc123:9,0xc124:2,0xc125:3}[param]
            if len(values)!=count or any(v!=int(v) for v in values):
                raise ValueError("Invalid original YUV coefficient vector")
            if param in (0xc120,0xc123):converted=[int(v)&0x7ff for v in values]
            elif param in (0xc121,0xc124):converted=[max(-128,min(128,int(v))) for v in values]
            else:converted=[max(0,min(1023,int(v))) for v in values]
            self.state[param]=converted
            self.log("vendor_yuv_configuration",parameter=hex(param),values=converted,
                     implementation="retained; YUV/native-format drawing remains unsupported")
            return
        if param not in self.state:raise UnsupportedVG("OpenVG state parameter "+hex(param))
        if param in (0x1106,0x1114,0x1120,0x1121):self.state[param]=list(values)
        else:
            if len(values)!=1:raise ValueError("Scalar state must have one value")
            self.state[param]=values[0]
        self.log("set_state",parameter=hex(param),values=list(values))

    def matrix(self,values=None,multiply_current=False):
        mode=int(self.state[0x1100])
        if mode not in self.matrices:raise UnsupportedVG("Unknown matrix mode")
        if values is None:return self.matrices[mode].copy()
        if len(values)!=9 or not all(math.isfinite(v) for v in values):raise ValueError("Invalid matrix")
        self.matrices[mode]=multiply(self.matrices[mode],values) if multiply_current else list(values)
        self.log("set_matrix",mode=hex(mode),matrix=self.matrices[mode])

    def create_paint(self):
        h=self.handle(2)
        self.paints[h]={0x1a00:0x1b00,0x1a01:[0.,0.,0.,1.],0x1a02:0x1c00,
                        0x1a03:[0.,0.,0.,0.,1.,1.,1.,1.,1.,1.],
                        0x1a04:[0.,0.,1.,0.],0x1a05:[0.,0.,0.,0.,1.],
                        0x1a06:0x1d00,0x1a07:1}
        return h

    def set_paint_parameter(self,handle,param,values):
        if handle not in self.paints and handle>>28==2:self.error=0x1000;return
        if handle not in self.paints:raise UnsupportedVG("Only paint parameters are implemented")
        if param not in self.paints[handle]:raise UnsupportedVG("Paint parameter "+hex(param))
        enums={0x1a00:range(0x1b00,0x1b04),0x1a02:range(0x1c00,0x1c03),
               0x1a06:range(0x1d00,0x1d04),0x1a07:(0,1)}
        if param in enums:
            if len(values)!=1 or values[0] not in enums[param]:raise ValueError("Invalid paint enum value")
            value=int(values[0])
        else:
            count={0x1a01:4,0x1a04:4,0x1a05:5}.get(param)
            if count is not None and len(values)!=count:raise ValueError("Invalid paint vector count")
            if param==0x1a03 and len(values)%5:raise ValueError("Color ramp stops require offset/R/G/B/A groups")
            if not all(math.isfinite(x) for x in values):raise ValueError("Non-finite paint parameter")
            value=list(values)
        self.paints[handle][param]=value
        self.log("paint_parameter",handle=hex(handle),parameter=hex(param),values=list(values),
                 note="Paint state retained; unsupported paint rendering still stops at draw")

    def set_paint(self,handle,modes):
        if not modes or modes&~3:self.error=0x1001;return
        if handle and handle not in self.paints:self.error=0x1000;return
        for mode in (1,2):
            if modes&mode:self.bound_paints[mode]=handle
        for old in tuple(self.retired_paints):
            if old not in self.bound_paints.values():del self.retired_paints[old]

    def destroy_paint(self,handle):
        # Original64ff594 clears object+4 public handle, decrements its owner
        # refcount, then64fd7c0 frees only at zero. Context bindings retain refs.
        if handle not in self.paints:self.error=0x1000;return
        paint=self.paints.pop(handle)
        if handle in self.bound_paints.values():self.retired_paints[handle]=paint
        self.log("destroy_paint",handle=hex(handle),retained_for_bound_modes=[mode for mode,h in self.bound_paints.items()if h==handle])

    def get_paint(self,mode):
        if mode not in (1,2):self.error=0x1001;return 0
        handle=self.bound_paints[mode]
        # Native64fd9e8 returns bound-object+4, zeroed by64ff594 on destroy.
        return handle if handle in self.paints else 0

    def _bound_paint(self,mode):
        handle=self.bound_paints[mode]
        return self.paints.get(handle,self.retired_paints.get(handle,
            {0x1a00:0x1b00,0x1a01:[0.,0.,0.,1.]}))

    def color(self,mode):
        p=self._bound_paint(mode)
        if p.get(0x1a00)!=0x1b00:raise UnsupportedVG("Gradient or pattern paint has not been implemented")
        values=p.get(0x1a01)
        if len(values)!=4:raise ValueError("Paint color needs four channels")
        return [max(0.,min(1.,float(x))) for x in values]

    def linear_gradient(self,mode):
        """Validate a captured paint before drawing any pixels; None is solid."""
        paint=self._bound_paint(mode)
        if paint is None or paint[0x1a00]==0x1b00:return None
        if paint[0x1a00]!=0x1b01:raise UnsupportedVG("Radial or pattern paint rendering is not implemented")
        points=paint[0x1a04];dx,dy=points[2]-points[0],points[3]-points[1]
        if dx*dx+dy*dy<1e-20:raise UnsupportedVG("Degenerate linear gradient")
        ramp=paint[0x1a03] or [0.,0.,0.,0.,1.,1.,1.,1.,1.,1.]
        stops=[list(ramp[i:i+5]) for i in range(0,len(ramp),5)]
        if len(stops)<2 or any(not 0<=x[0]<=1 for x in stops) or any(a[0]>b[0] for a,b in zip(stops,stops[1:])):
            raise UnsupportedVG("Noncanonical color ramp stop ordering")
        for stop in stops:stop[1:]=[max(0.,min(1.,x)) for x in stop[1:]]
        # Quartz alpha interpolation has not been compared to both OpenVG
        # premultiplied-ramp modes. Actual observed paints have constant alpha.
        if any(stop[4]!=stops[0][4] for stop in stops):
            raise UnsupportedVG("Variable-alpha gradient interpolation is not verified")
        if stops[0][0]>0:stops.insert(0,[0.,*stops[0][1:]])
        if stops[-1][0]<1:stops.append([1.,*stops[-1][1:]])
        matrix=self.matrices[0x1402 if mode==2 else 0x1403]
        if matrix[2]!=0 or matrix[5]!=0 or matrix[8]!=1 or abs(matrix[0]*matrix[4]-matrix[1]*matrix[3])<1e-15:
            raise UnsupportedVG("Singular or perspective paint matrix")
        spread=paint[0x1a02]
        if spread not in (0x1c00,0x1c01,0x1c02):raise UnsupportedVG("Gradient spread mode")
        return {"points":points,"stops":stops,"matrix":matrix,"spread":spread}

    def _stroke_state(self,ctx):
        q=self.q;q.CGContextSetLineWidth(ctx,float(self.state[0x1110]))
        cap=self.state[0x1111]-0x1700;join=self.state[0x1112]-0x1800
        if cap not in (0,1,2) or join not in (0,1,2):raise UnsupportedVG("Stroke cap/join")
        q.CGContextSetLineCap(ctx,cap);q.CGContextSetLineJoin(ctx,join)
        q.CGContextSetMiterLimit(ctx,float(self.state[0x1113]))
        dash=self.state[0x1114]
        if dash:
            if self.state[0x1116]:raise UnsupportedVG("Per-subpath dash reset")
            q.CGContextSetLineDash(ctx,float(self.state[0x1115]),(ct.c_double*len(dash))(*dash),len(dash))

    def _draw_linear_gradient(self,ctx,spec):
        q=self.q;m=spec["matrix"]
        q.CGContextConcatCTM(ctx,Affine(m[0],m[1],m[3],m[4],m[6],m[7]))
        stops=spec["stops"];components=[x for stop in stops for x in stop[1:]]
        colors=q.CGColorSpaceCreateWithName(q.srgb_name)
        gradient=q.CGGradientCreateWithColorComponents(colors,(ct.c_double*len(components))(*components),
                                                       (ct.c_double*len(stops))(*(x[0] for x in stops)),len(stops))
        try:
            if not gradient:raise RuntimeError("Cannot create captured linear gradient")
            x0,y0,x1,y1=spec["points"];dx,dy=x1-x0,y1-y0
            if spec["spread"]==0x1c00:
                q.CGContextDrawLinearGradient(ctx,gradient,Point(x0,y0),Point(x1,y1),3)
            else:
                clip=q.CGContextGetClipBoundingBox(ctx);x,y,w,h=clip.origin.x,clip.origin.y,clip.size.width,clip.size.height
                projections=[((px-x0)*dx+(py-y0)*dy)/(dx*dx+dy*dy) for px,py in ((x,y),(x+w,y),(x,y+h),(x+w,y+h))]
                if not all(math.isfinite(t) for t in projections):return  # Empty clip.
                first,last=math.floor(min(projections)),math.ceil(max(projections))
                if last-first>8192:raise UnsupportedVG("Gradient repetition exceeds bounded host work")
                for n in range(first,last+1):
                    start,end=Point(x0+n*dx,y0+n*dy),Point(x1+n*dx,y1+n*dy)
                    if spec["spread"]==0x1c02 and n&1:start,end=end,start
                    q.CGContextDrawLinearGradient(ctx,gradient,start,end,0)
        finally:
            if gradient:q.CGGradientRelease(gradient)
            q.CGColorSpaceRelease(colors)

    def create_path(self,datatype=3,scale=1.,bias=0.,capabilities=0xfff):
        h=self.handle(1);self.paths[h]={"datatype":datatype,"scale":scale,"bias":bias,
            "capabilities":capabilities&0xfff,"segments":[],"coords":[]};return h

    def transform_path(self,destination,source):
        """Append a captured path transformed by PATH_USER_TO_SURFACE.

        Original wrapper65d9c64 ->64e4698 checks sourcecap8/destinationcap16.
        Khronos OpenVG1.1 section8.6.8 defines append and self-copy semantics.
        Normalize relative/H/V/smooth polynomial segments to absolute geometry;
        never interpret the current VG_MATRIX_MODE as the transform selector.
        Integer destinations and elliptical arc output are explicitly unsupported
        until their coordinate conversion/arc parameter contracts are implemented.
        """
        if source not in self.paths or destination not in self.paths:
            self.error=0x1000;return
        src,dst=self.paths[source],self.paths[destination]
        if not src["capabilities"]&8 or not dst["capabilities"]&16:
            self.error=0x1003;return
        if dst["datatype"]!=3:
            raise UnsupportedVG("TransformPath integer destination conversion is not implemented")
        if any(segment&~1 in (18,20,22,24) for segment in src["segments"]):
            raise UnsupportedVG("TransformPath elliptical arc parameter conversion is not implemented")
        matrix=list(self.matrices[0x1400])
        if (not all(math.isfinite(x)for x in matrix)
                or matrix[2]!=0 or matrix[5]!=0 or matrix[8]!=1):
            raise UnsupportedVG("TransformPath requires a finite affine path matrix")
        if not math.isfinite(dst["scale"]) or dst["scale"]==0 or not math.isfinite(dst["bias"]):
            raise ValueError("TransformPath destination scale/bias invalid")
        # Snapshot before any append: src==dst is legal and must only copy once.
        snapshot={k:list(v)if isinstance(v,list)else v for k,v in src.items()}
        operations=path_operations(snapshot["segments"],snapshot["coords"],snapshot["scale"],snapshot["bias"])
        segments,coords=[],[]
        for op in operations:
            segments.append({"Z":0,"M":2,"L":4,"Q":10,"C":12}[op[0]])
            for x,y in zip(op[1::2],op[2::2]):
                xy=(matrix[0]*x+matrix[3]*y+matrix[6],matrix[1]*x+matrix[4]*y+matrix[7])
                for value in xy:
                    raw=(value-dst["bias"])/dst["scale"]
                    try:raw=struct.unpack('<f',struct.pack('<f',raw))[0]
                    except OverflowError:raise UnsupportedVG("TransformPath coordinate exceeds FLOAT range")from None
                    if not math.isfinite(raw):raise UnsupportedVG("TransformPath coordinate is not finite")
                    coords.append(raw)
        if len(dst["segments"])+len(segments)>1_000_000:
            raise UnsupportedVG("TransformPath exceeds bounded host geometry")
        capture={"source":snapshot,"matrix_path_user_to_surface":matrix,
                 "destination_before":{k:list(v)if isinstance(v,list)else v for k,v in dst.items()},
                 "appended_segments":segments,"appended_coordinates":coords}
        raw=json.dumps(capture,separators=(',',':')).encode()
        digest=hashlib.sha256(raw).hexdigest()
        path=self.output/("transform-path-"+digest+".json")
        path.write_bytes(raw)
        dst["segments"].extend(segments);dst["coords"].extend(coords)
        self.log("transform_path",source=hex(source),destination=hex(destination),
                 matrix=matrix,segments_appended=len(segments),source_unchanged=source!=destination,
                 captured_geometry=str(path),sha256=digest)

    def create_image(self,data,format,width,height,stride=None):
        if not(0<width<=8192 and 0<height<=8192 and width*height<=32_000_000):raise ValueError("Image dimensions exceed bounds")
        image=decode_image(data,format,width,height,stride)
        h=self.handle(4);self.images[h]=image
        if format&0x3f==2:
            # Retain exact premultiplied 8-bit components. An unpremultiply /
            # premultiply round trip would change low-alpha image storage.
            row=width*4;pitch=row if stride is None else stride
            raw=b''.join(data[y*pitch:y*pitch+row] for y in range(height))
            order={0:"ABGR",0x40:"BGRA",0x80:"ARGB",0xc0:"RGBA"}[format&0xc0]
            packed=Image.frombytes("RGBA",(width,height),raw,"raw",order)
            self.image_premultiplied[h]=Image.frombytes("RGBa",packed.size,packed.tobytes()).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        path=self.output/("image-%08x.bin"%h);path.write_bytes(data)
        self.log("captured_image",handle=hex(h),format=hex(format),width=width,height=height,stride=stride,
                 source_path=str(path),sha256=hashlib.sha256(data).hexdigest());return h

    def image_pixels(self,handle,premultiplied=False):
        if handle not in self.images:raise ValueError("Unknown image handle")
        surface=self.image_surfaces.get(handle)
        if surface is not None:
            image=self.surfaces[surface].premultiplied_image()
        else:image=self.image_premultiplied.get(handle)
        if image is not None:return image if premultiplied else image.convert("RGBA")
        image=self.images[handle]
        return image.convert("RGBa") if premultiplied else image

    def create_image_surface(self,image):
        if image not in self.images:raise ValueError("Unknown client VGImage")
        if image in self.image_surfaces:raise UnsupportedVG("VGImage is already bound to a pbuffer")
        pixels=self.image_pixels(image,True)
        surface=self.create_surface(pixels.width,pixels.height,"original VGImage client buffer")
        canvas=self.surfaces[surface]
        ct.memmove(canvas.buffer,pixels.tobytes(),pixels.width*pixels.height*4)
        self.image_surfaces[image]=surface;self.surface_images[surface]=image
        self.log("bind_client_image_surface",surface=hex(surface),image=hex(image),
                 storage="shared software Canvas; VGImage reads see rendered pixels")
        return surface

    def destroy_surface(self,surface):
        if surface==self.surface:raise UnsupportedVG("Destroying current surface")
        canvas=self.surfaces[surface];image=self.surface_images.pop(surface,None)
        if image is not None:
            self.image_surfaces.pop(image,None)
            # The public VGImage may already have been destroyed. The surface
            # retains its backing until this point, without reviving a handle.
            if image in self.images:
                pixels=canvas.premultiplied_image()
                self.image_premultiplied[image]=pixels;self.images[image]=pixels.convert("RGBA")
        del self.surfaces[surface];canvas.close()
        self.log("destroy_surface",surface=hex(surface),client_image=hex(image) if image else None)

    def destroy_image(self,image):
        if image not in self.images:self.error=0x1000;return
        del self.images[image];self.image_premultiplied.pop(image,None)
        self.log("destroy_image",image=hex(image),retained_by_surface=self.image_surfaces.get(image))

    def _begin(self,mode=None,clear=False):
        if self.surface is None:raise UnsupportedVG("No software surface is current")
        if self.state[0x1130]:raise UnsupportedVG("OpenVG mask buffer is not implemented")
        canvas=self.surfaces[self.surface];q=self.q;ctx=canvas.context
        q.CGContextSaveGState(ctx)
        try:
            if self.state[0x1131]:
                values=self.state[0x1106]
                if len(values)%4:raise ValueError("Scissor rectangle list is malformed")
                rects=(Rect*(len(values)//4))(*(rectangle(*values[i:i+4]) for i in range(0,len(values),4)))
                q.CGContextClipToRects(ctx,rects,len(rects))
            blend=0x2000 if clear else self.state[0x1104]
            if blend not in (0x2000,0x2001):raise UnsupportedVG("OpenVG blend mode "+hex(blend))
            q.CGContextSetBlendMode(ctx,17 if blend==0x2000 else 0)
            q.CGContextSetShouldAntialias(ctx,self.state[0x1103]!=0x1200)
            if mode is not None:
                m=self.matrices[mode]
                if m[2]!=0 or m[5]!=0 or m[8]!=1:raise UnsupportedVG("Perspective image transform is not implemented")
                q.CGContextConcatCTM(ctx,Affine(m[0],m[1],m[3],m[4],m[6],m[7]))
            return ctx
        except Exception:
            q.CGContextRestoreGState(ctx);raise

    def clear(self,x,y,width,height):
        ctx=self._begin(clear=True)
        try:
            color=self.state[0x1121]
            if len(color)!=4:raise ValueError("Clear color needs four channels")
            self.q.CGContextSetRGBFillColor(ctx,*color)
            self.q.CGContextFillRect(ctx,rectangle(x,y,width,height))
        finally:self.q.CGContextRestoreGState(ctx)
        self.draw_count+=1;self.log("clear",rect=[x,y,width,height],color=color)

    def draw_path_data(self,segments,coords,modes,datatype=3,scale=1.,bias=0.):
        if modes not in (1,2,3):raise ValueError("Invalid path paint modes")
        gradients={mode:self.linear_gradient(mode) for mode in (2,1) if modes&mode}
        rule=self.state[0x1101]
        if rule not in (0x1900,0x1901):raise UnsupportedVG("Unknown fill rule")
        ops=path_operations(segments,coords,scale,bias);q=self.q;path=q.CGPathCreateMutable()
        if not path:raise RuntimeError("Cannot create host path")
        try:
            for op in ops:
                fun={"M":q.CGPathMoveToPoint,"L":q.CGPathAddLineToPoint,"Q":q.CGPathAddQuadCurveToPoint,"C":q.CGPathAddCurveToPoint}.get(op[0])
                if fun:fun(path,None,*op[1:])
                else:q.CGPathCloseSubpath(path)
            ctx=self._begin(0x1400)
            try:
                if modes&1:self._stroke_state(ctx)
                if not any(gradients.values()):
                    if modes&2:q.CGContextSetRGBFillColor(ctx,*self.color(2))
                    if modes&1:q.CGContextSetRGBStrokeColor(ctx,*self.color(1))
                    q.CGContextAddPath(ctx,path)
                    draw_mode=2 if modes==1 else (1 if rule==0x1900 else 0) if modes==2 else (4 if rule==0x1900 else 3)
                    q.CGContextDrawPath(ctx,draw_mode)
                else:
                    # OpenVG fills first, then strokes. Clip each paint to the
                    # original path or its actual stroked outline, then apply
                    # the independent paint-to-user transform.
                    for mode,spec in gradients.items():
                        q.CGContextSaveGState(ctx)
                        try:
                            q.CGContextAddPath(ctx,path)
                            if spec:
                                if mode==1:q.CGContextReplacePathWithStrokedPath(ctx)
                                (q.CGContextEOClip if mode==2 and rule==0x1900 else q.CGContextClip)(ctx)
                                self._draw_linear_gradient(ctx,spec)
                            else:
                                (q.CGContextSetRGBFillColor if mode==2 else q.CGContextSetRGBStrokeColor)(ctx,*self.color(mode))
                                q.CGContextDrawPath(ctx,2 if mode==1 else 1 if rule==0x1900 else 0)
                        finally:q.CGContextRestoreGState(ctx)
            finally:q.CGContextRestoreGState(ctx)
        finally:q.CGPathRelease(path)
        self.draw_count+=1;self.log("draw_captured_path",segments=list(segments),coordinates=coords,coordinate_count=len(coords),modes=modes,
                                   datatype=datatype,scale=scale,bias=bias,
                                   paint_types={str(mode):self._bound_paint(mode).get(0x1a00,0x1b00) for mode in gradients},
                                   geometry_sha256=hashlib.sha256(json.dumps([list(segments),coords]).encode()).hexdigest())

    def draw_image(self,handle,x=0,y=0,width=None,height=None):
        if self.state[0x1105]!=0x1f00:raise UnsupportedVG("Image multiply/stencil mode is not implemented")
        if handle not in self.images:raise ValueError("Unknown image handle")
        if self.image_surfaces.get(handle)==self.surface:
            self.error=0x1006;return  # VG_IMAGE_IN_USE_ERROR: current render target.
        source=self.image_pixels(handle,True);width=source.width if width is None else width;height=source.height if height is None else height
        if min(x,y,width,height)<0 or x+width>source.width or y+height>source.height:raise ValueError("Image subrectangle out of range")
        source=source.crop((x,source.height-y-height,x+width,source.height-y))
        raw=source.tobytes();buffer=ct.create_string_buffer(raw);q=self.q
        provider=q.CGDataProviderCreateWithData(None,buffer,len(raw),None)
        colors=q.CGColorSpaceCreateWithName(q.srgb_name)
        image=q.CGImageCreate(width,height,8,32,width*4,colors,0x4001,provider,None,True,0)
        if not image:
            q.CGDataProviderRelease(provider);q.CGColorSpaceRelease(colors);raise RuntimeError("Cannot create host image")
        try:
            ctx=self._begin(0x1401)
            try:
                quality=self.state[0x1102]
                if quality not in (1,2,4):raise UnsupportedVG("Unknown image quality")
                q.CGContextSetInterpolationQuality(ctx,{1:1,2:2,4:3}[quality])
                q.CGContextDrawImage(ctx,rectangle(0,0,width,height),image)
            finally:q.CGContextRestoreGState(ctx)
        finally:q.CGImageRelease(image);q.CGDataProviderRelease(provider);q.CGColorSpaceRelease(colors)
        self.draw_count+=1;self.log("draw_captured_image",handle=hex(handle),source_rect=[x,y,width,height])

    def swap(self,surface=None):
        surface=self.surface if surface is None else surface
        if surface not in self.surfaces:raise ValueError("Unknown surface at swap")
        canvas=self.surfaces[surface];path=self.output/("frame-%04d.png"%self.swaps);self.swaps+=1
        canvas.image().save(path)
        self.log("software_swap",surface=hex(surface),path=str(path),draw_calls=self.draw_count,
                 pixels_sha256=hashlib.sha256(bytes(canvas.buffer)).hexdigest(),
                 provenance="Software rendering of supplied calls; menu validity requires a real firmware call trace")
        return path

    def close(self):
        for canvas in self.surfaces.values():canvas.close()
