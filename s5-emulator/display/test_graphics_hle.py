#!/usr/bin/env python3
"""Host renderer + guest ABI tests. These synthetic calls are NOT S5 menus."""
import hashlib
import json
from pathlib import Path
import shutil
import struct

from graphics_hle import GraphicsHLE, UnsupportedVG, word_float
from openvg_backend import decode_image, path_operations


class Memory:
    def __init__(self):self.data=bytearray(0x10000);self.regs=[0]*26;self.regs[25]=0x600001d3
    def read_mem(self,a,n):return bytes(self.data[a:a+n])
    def write_mem(self,a,data):self.data[a:a+len(data)]=data
    def read_reg(self,n):return self.regs[n]
    def write_reg(self,n,value):self.regs[n]=value


def main():
    output=Path(__file__).with_name("backend-selftest")
    # Only this exact self-test directory is replaced; no firmware evidence.
    if output.exists():shutil.rmtree(output)
    emu=Memory();driver=GraphicsHLE(emu,output);b=driver.backend;checks=[]
    def ok(name,condition):
        if not condition:raise AssertionError(name)
        checks.append(name)
    def invoke(pc,args):
        regs={"r%d"%i:0 for i in range(16)}
        regs.update({"r%d"%i:value for i,value in enumerate(args[:4])})
        regs.update(r13=0x100,r14=0x201,r15=pc)
        emu.write_mem(0x100,struct.pack("<%dI"%len(args[4:]),*args[4:]))
        result=driver.handle(pc,regs)
        ok("return via original LR from "+hex(pc),emu.regs[15]==0x200 and emu.regs[25]&0x20)
        return result

    # A real caller's dimensions are required. These are explicitly synthetic.
    emu.write_mem(0x300,struct.pack("<3I",0x3028,6,0x3038))
    invoke(0x64c13ba,[driver.display,0x300,0x340,1,0x344])
    ok("original static config 6 request",struct.unpack("<2I",emu.read_mem(0x340,8))==(driver.config,1))
    invoke(0x64c19a6,[driver.display,driver.config,0x3028,0x348])
    ok("config 6 identity query",emu.read_mem(0x348,4)==struct.pack("<I",6))
    driver.register_native_window(0x1234,32,32,"synthetic ABI self-test; not a camera window")
    original_context_attrs=[0x3ff7,0x80000,0x3ff8,0x40000,0x3ff9,0x10000,
                            0x3ffa,0x10000,0x3fce,0x10000,0x3fcf,0x10000,0x3038]
    emu.write_mem(0x380,struct.pack("<13I",*original_context_attrs))
    context=driver.call("eglCreateContext",[driver.display,driver.config,0,0x380])
    ok("captured original context resource sizes recorded",driver.context_attributes[context][0x3ff7]==0x80000)
    b.set_state(0xc070,[1024]);b.set_state(0xc120,[0,-88,454,359,-183,0])
    b.set_state(0xc124,[-500,500]);b.set_state(0xc125,[-1,512,2048])
    ok("original vendor state width and clamp rules",b.state[0xc070]==1024 and b.state[0xc120]==[0,1960,454,359,1865,0]
       and b.state[0xc124]==[-128,128] and b.state[0xc125]==[0,512,1023])
    try:b.set_state(0xc070,[1025])
    except ValueError:pass
    else:raise AssertionError("Misaligned original vendor resource value accepted")
    ok("invalid vendor state preserves prior state",b.state[0xc070]==1024)
    emu.write_mem(0x360,struct.pack("<5I",0x3088,0x308c,0x3087,0x3089,0x3038))
    surface=driver.call("eglCreateWindowSurface",[driver.display,driver.config,0x1234,0x360])
    ok("captured premultiplied sRGB surface attrs recorded",driver.surface_attributes[surface]=={0x3088:0x308c,0x3087:0x3089})
    invoke(0x64c46c4,[driver.display,surface,0x350])
    ok("native bridge returns observed window handle",emu.read_mem(0x350,4)==struct.pack("<I",0x1234))
    ok("original native buffer queries are not replaced",0x64de7da not in driver.breakpoints and 0x64de686 not in driver.breakpoints)
    driver.call("eglSurfaceAttrib",[driver.display,surface,0x3fdd,0x3fde])
    ok("observed linear-buffer mode recorded",driver.surface_attributes[surface][0x3fdd]==0x3fde)
    try:driver.call("eglSurfaceAttrib",[driver.display,surface,0x3fdd,0x3fdf])
    except UnsupportedVG:pass
    else:raise AssertionError("Unknown alternate surface mode silently accepted")
    ok("alternate surface mode stops",driver.surface_attributes[surface][0x3fdd]==0x3fde)
    driver.call("eglMakeCurrent",[driver.display,surface,surface,context])
    emu.write_mem(0x400,struct.pack("<4f",0,0,0,1))
    invoke(0x65d93f2,[0x1121,4,0x400]);invoke(0x65d9b0c,[0,0,32,32])
    paint=invoke(0x65d9da4,[])["result"]
    gradient=driver.call("vgCreatePaint",[])
    driver.call("vgSetParameteri",[gradient,0x1a00,0x1b01])
    emu.write_mem(0x480,struct.pack("<4f",0,0,10,0))
    driver.call("vgSetParameterfv",[gradient,0x1a04,4,0x480])
    ok("unused original gradient state retained",b.paints[gradient][0x1a00]==0x1b01 and b.paints[gradient][0x1a04]==[0,0,10,0])
    b.set_paint(gradient,3)
    gradient_surface=b.create_surface(32,32,"synthetic gradient semantic test")
    b.make_current(gradient_surface)
    b.set_paint_parameter(gradient,0x1a04,[0,0,8,0])
    b.set_paint_parameter(gradient,0x1a02,[0x1c01])
    b.set_paint_parameter(gradient,0x1a03,[0,1,0,0,1,1,0,0,1,1])
    b.draw_path_data([2,4,4,4,0],[0,0,32,0,32,32,0,32],3)
    gradient_image=b.surfaces[gradient_surface].image()
    ok("linear gradient repeats actual interpolated colors",gradient_image.getpixel((1,16))==gradient_image.getpixel((9,16))
       and gradient_image.getpixel((1,16))[0]>gradient_image.getpixel((5,16))[0]
       and gradient_image.getpixel((1,16))[2]<gradient_image.getpixel((5,16))[2])
    b.set_paint_parameter(gradient,0x1a02,[0x1c02])
    b.draw_path_data([2,4,4,4,0],[0,0,32,0,32,32,0,32],2)
    reflected=b.surfaces[gradient_surface].image()
    ok("linear gradient reflect reverses alternate interval",reflected.getpixel((1,16))==reflected.getpixel((14,16)))
    gradient_image.save(output/"gradient-test.png")
    clear_before=b.state[0x1121];b.state[0x1121]=[0,0,0,0];b.clear(0,0,32,32);b.state[0x1121]=clear_before
    b.set_paint_parameter(gradient,0x1a02,[0x1c01]);b.state[0x1110]=4
    b.matrices[0x1403]=[1,0,0,0,1,0,4,0,1]
    b.draw_path_data([2,4,4,4,0],[4,4,28,4,28,28,4,28],1)
    stroked=b.surfaces[gradient_surface].image()
    ok("gradient stroke clips to original stroked outline",stroked.getpixel((4,16))[3]>0
       and stroked.getpixel((16,16))[3]==0 and stroked.getpixel((0,16))[3]==0)
    ok("gradient stroke paint transform preserves repeat period",stroked.getpixel((6,27))==stroked.getpixel((14,27)))
    b.state[0x1110]=1.;b.matrices[0x1403]=[1,0,0,0,1,0,0,0,1]
    b.make_current(surface)
    b.set_paint_parameter(gradient,0x1a00,[0x1b03])
    before_gradient=bytes(b.surfaces[surface].buffer)
    try:b.draw_path_data([2,4,4,0],[1,1,8,1,8,8],2)
    except UnsupportedVG:pass
    else:raise AssertionError("Pattern draw silently replaced by solid")
    ok("unsupported pattern draw preserves pixels",bytes(b.surfaces[surface].buffer)==before_gradient)
    invoke(0x65d9e22,[paint,0xff0000ff]);invoke(0x65d9de8,[paint,2])
    emu.write_mem(0x500,bytes([2,4,4,4,0]))
    emu.write_mem(0x600,struct.pack("<8h",1,1,8,1,8,8,1,8))
    invoke(0x65da884,[0,1,word_float(1),word_float(0),5,0x500,0x600,2])
    frame=b.surfaces[surface].image()
    ok("original-style DMP signed-short path ABI gives red square",frame.getpixel((4,27))==(255,0,0,255))
    ok("VG bottom-left coordinates preserve top background",frame.getpixel((4,4))==(0,0,0,255))
    # Bottom row red/green, top row blue/white, packed original-style little-endian RGBA words.
    pixels=struct.pack("<4I",0xff0000ff,0x00ff00ff,0x0000ffff,0xffffffff)
    # DMP standard storage is16×8 for this logical2×2 image (pitch64).
    padded=pixels[:8]+bytes(56)+pixels[8:]+bytes(56)+bytes(64*6)
    emu.write_mem(0x1000,padded)
    image=invoke(0x65da992,[1,2,2,len(padded),0x1000,1])["result"]
    emu.write_mem(0x800,struct.pack("<9f",1,0,0,0,1,0,16,16,1))
    invoke(0x65d93d6,[0x1100,0x1401]);invoke(0x65d9306,[0x800])
    invoke(0x65da76e,[image,0,0,2,2])
    frame=b.surfaces[surface].image()
    expected=[(255,0,0,255),(0,255,0,255),(0,0,255,255),(255,255,255,255)]
    ok("captured image channels, stride and row direction",[frame.getpixel(p) for p in ((16,15),(17,15),(16,14),(17,14))]==expected)
    ok("RGBA4444 source format",decode_image(struct.pack("<H",0xf00f),5,1,1).getpixel((0,0))==(255,0,0,255))
    ok("PRE source unpremultiplication",decode_image(struct.pack("<I",0x80000080),2,1,1).getpixel((0,0))==(255,0,0,128))
    ok("relative/smooth path coordinates",path_operations([2,11,15],[1,1,1,0,2,2,2,2])==[("M",1.,1.),("Q",2.,1.,3.,3.),("Q",4.,5.,5.,5.)])
    # Unknown actual API must not fake a return or frame.
    before=(emu.regs.copy(),b.swaps,b.draw_count)
    try:driver.handle(0x65daab4,{"r%d"%i:0 for i in range(16)})
    except UnsupportedVG:pass
    else:raise AssertionError("Unmodeled driver guard did not stop")
    ok("unknown driver call stops without advancing CPU or frame",before==(emu.regs,b.swaps,b.draw_count))
    b.state[0x1130]=1
    try:b.draw_image(image)
    except UnsupportedVG:pass
    else:raise AssertionError("Masking silently ignored")
    b.state[0x1130]=0
    ok("unsupported masking emits no drawing",b.draw_count==before[2])
    invoke(0x64c3cda,[driver.display,surface])
    driver.close()
    report={"purpose":"Synthetic renderer and guest ABI tests; NOT actual S5 UI execution", "checks":checks,
            "count":len(checks),"passed":True,"frame_sha256":hashlib.sha256((output/"frame-0000.png").read_bytes()).hexdigest()}
    (output/"verification.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))


if __name__=="__main__":main()
