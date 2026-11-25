#!/usr/bin/env python3
"""Synthetic NV16 frame -> actual experimental ARM hook in QEMU -> host JPEG.

This is an offline image pipeline, not a camera boot, shutter or hardware codec.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import re
import struct
import sys
import time
import uuid

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat, __version__ as PIL_VERSION

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from handoff.run_ui_qemu import (QemuArm, DUMP_BASE, MODULE, CALL, GETTER, MEMCPY,
    CAVE, DESCRIPTOR, STACK, SOURCE_DESCRIPTOR, Y, C, STRIDE, DUMP_SHA, PATCH_SHA,
    UI_SHA, stop_at)

WIDTH, HEIGHT = 6000, 4000


def sha(data):
    return hashlib.sha256(data).hexdigest()


def scene():
    """Deliberately artificial scene with gradients, mountains and color bars."""
    sky = Image.new("RGB", (1500, 1000))
    d = ImageDraw.Draw(sky)
    for y in range(1000):
        t = y / 999
        d.line((0, y, 1500, y), fill=(int(38+166*t), int(102+99*t), int(174+14*t)))
    d.ellipse((1100, 100, 1260, 260), fill=(255, 234, 166))
    d.polygon([(0, 660), (210, 290), (450, 610), (700, 310), (1060, 700)], fill=(62, 87, 110))
    d.polygon([(130, 430), (210, 290), (295, 430), (215, 393)], fill=(225, 232, 235))
    d.polygon([(0, 760), (470, 455), (730, 780), (1080, 490), (1500, 790), (1500, 1000), (0, 1000)], fill=(48, 104, 85))
    d.polygon([(0, 900), (1500, 760), (1500, 1000), (0, 1000)], fill=(162, 137, 85))
    colors = [(235,235,235),(210,40,30),(30,190,55),(40,70,210),(220,190,20),(20,180,190),(180,40,170),(25,25,25)]
    for x, color in enumerate(colors):
        d.rectangle((40+x*174, 855, 40+(x+1)*174-5, 940), fill=color)
    d.rectangle((30, 28, 665, 65), fill=(18, 28, 38))
    d.text((42, 39), "SYNTHETIC TEST FRAME | QEMU LUT | HOST JPEG", fill="white")
    return sky.resize((WIDTH, HEIGHT), Image.Resampling.BICUBIC)


def to_nv16(frame):
    y, cb, cr = frame.convert("YCbCr").split()
    yraw = y.tobytes()
    cbraw = cb.resize((WIDTH//2, HEIGHT), Image.Resampling.BOX).tobytes()
    crraw = cr.resize((WIDTH//2, HEIGHT), Image.Resampling.BOX).tobytes()
    craw = bytearray(WIDTH*HEIGHT)
    craw[0::2], craw[1::2] = cbraw, crraw
    return tuple(b"".join(raw[row*WIDTH:(row+1)*WIDTH]+bytes([padding])*(STRIDE-WIDTH)
                          for row in range(HEIGHT))
                 for raw, padding in ((yraw, 0xa5), (craw, 0x5a)))


def from_nv16(y, c):
    y = b"".join(y[row*STRIDE:row*STRIDE+WIDTH] for row in range(HEIGHT))
    c = b"".join(c[row*STRIDE:row*STRIDE+WIDTH] for row in range(HEIGHT))
    cb = Image.frombytes("L", (WIDTH//2, HEIGHT), c[0::2]).resize((WIDTH, HEIGHT), Image.Resampling.NEAREST)
    cr = Image.frombytes("L", (WIDTH//2, HEIGHT), c[1::2]).resize((WIDTH, HEIGHT), Image.Resampling.NEAREST)
    return Image.merge("YCbCr", (Image.frombytes("L", (WIDTH, HEIGHT), y), cb, cr)).convert("RGB")


def run_hook(args, mode, inputs, patch, payload, dump):
    directory = args.output/("qemu-lut-on" if mode else "qemu-lut-off")
    desc = bytearray(0x38)
    struct.pack_into("<IIHHIIHHHHI", desc, 0, Y, STRIDE*HEIGHT, STRIDE, HEIGHT,
                     C, STRIDE*HEIGHT, STRIDE, HEIGHT, 0, 0, 0)
    struct.pack_into("<HHI", desc, 0x1c, WIDTH, HEIGHT, 2)
    trace = []
    with QemuArm(args.qemu, directory, ram="3G", cpu="cortex-a7",
                 loads=[(dump,DUMP_BASE),(args.output/"input-Y.bin",Y),(args.output/"input-C.bin",C)]) as emu:
        emu.write_mem(CAVE, payload)
        emu.write_mem(CALL, patch[CALL-MODULE:CALL-MODULE+4])
        emu.write_mem(0x08bdc310, struct.pack("<I", mode))
        emu.write_mem(SOURCE_DESCRIPTOR, desc)
        for i in range(16):
            emu.write_reg(i, 0)
        for i in range(4,12):
            emu.write_reg(i, 0x12345678)
        for key,value in [("sp",STACK+0xf000),("r0",1),("r1",DESCRIPTOR),("lr",0),
                          ("cpsr",0x600001f3),("pc",CALL)]:
            emu.write_reg(key,value)
        stop_at(emu, GETTER, trace, "original_getter")
        emu.step(timeout=5)
        stop_at(emu, MEMCPY, trace, "original_memcpy")
        emu.step(timeout=5)
        stop_at(emu, CALL+4, trace, "original_call_return", timeout=60)
        checks = {"descriptor_copied":emu.read_mem(DESCRIPTOR,0x38)==desc,
                  "return_value_zero":emu.read_reg("r0")==0,
                  "stack_restored":emu.read_reg("sp")==STACK+0xf000,
                  "saved_registers_preserved":all(emu.read_reg(i)==0x12345678 for i in range(4,12))}
        planes = []
        for label,address,original in [("Y",Y,inputs[0]),("C",C,inputs[1])]:
            file = directory/("output-"+label+".bin")
            emu.dump_memory(address,STRIDE*HEIGHT,file)
            actual=file.read_bytes()
            planes.append(actual)
            checks[label+"_all_row_padding_preserved"] = all(
                actual[row*STRIDE+WIDTH:(row+1)*STRIDE] == original[row*STRIDE+WIDTH:(row+1)*STRIDE]
                for row in range(HEIGHT))
            if not mode:
                checks[label+"_entire_plane_unchanged"] = actual == original
    return planes, {"mode":mode,"trace":trace,"checks":checks,
                    "plane_sha256":dict(zip(("Y","C"),map(sha,planes)))}


def verify_pixels(inputs, actual, lut):
    """Independent Python integer interpolation and YCbCr math, sampled pairs."""
    def limit(v):
        return max(0,min(255,v))
    def blend(a,b,w):
        return (a*(255-w)+b*w+127)//255
    def apply_lut(rgb):
        axes=[(v*32//255,min(32,v*32//255+1),v*32%255) for v in rgb]
        r,g,b=axes
        result=[]
        for channel in range(3):
            corners=[]
            for z in b[:2]:
                for y in g[:2]:
                    corners.append(blend(lut[3*(r[0]+33*y+1089*z)+channel],
                                         lut[3*(r[1]+33*y+1089*z)+channel],r[2]))
            result.append((blend(blend(*corners[:2],g[2]),blend(*corners[2:],g[2]),b[2])+128)//257)
        return result
    rng=random.Random(0x5335)
    positions={(0,0),(0,5998),(3999,0),(3999,5998),(2000,3000)}
    positions.update((rng.randrange(HEIGHT),2*rng.randrange(WIDTH//2)) for _ in range(4096))
    for row,x in sorted(positions):
        offset=row*STRIDE+x
        cb,cr=inputs[1][offset]-128,inputs[1][offset+1]-128
        ys=[];cbs=[];crs=[]
        for k in (0,1):
            y=inputs[0][offset+k]
            rgb=[limit(y+((359*cr+128)>>8)),limit(y-((88*cb+183*cr+128)>>8)),limit(y+((454*cb+128)>>8))]
            r,g,b=apply_lut(rgb)
            ys.append(limit((77*r+150*g+29*b+128)>>8))
            cbs.append(limit(128+((-43*r-85*g+128*b+128)>>8)))
            crs.append(limit(128+((128*r-107*g-21*b+128)>>8)))
        expected=bytes(ys),bytes(((sum(cbs)+1)//2,(sum(crs)+1)//2))
        if (actual[0][offset:offset+2],actual[1][offset:offset+2]) != expected:
            raise RuntimeError("Independent LUT reference mismatch at (%s,%s)"%(x,row))
    return {"pairs_checked":len(positions),"pixels_checked":2*len(positions),"all_exact":True}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path)
    p.add_argument("--input",type=Path,help="Optional source image; center-cropped/resized to 6000x4000 on the host")
    p.add_argument("--inputs",type=Path,default=ROOT/"results/handoff-ui-check")
    p.add_argument("--qemu",type=Path,default=ROOT/"toolchain/build/qemu-system-arm")
    a=p.parse_args()
    a.output=(a.output or ROOT/"capture/results"/(time.strftime("capture-%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6])).resolve()
    a.output.mkdir(parents=True,exist_ok=False)
    a.qemu=a.qemu.resolve()
    package=json.loads((a.inputs/"package-verification.json").read_text())
    dump=Path(package["dump"])
    patch=(a.inputs/"postboot3-lut-experiment.bin").read_bytes()
    payload=(a.inputs/"payload.bin").read_bytes()
    for actual,expected,label in [(sha(Path(package["package"]).read_bytes()),UI_SHA,"experiment BIN"),
                                  (sha(dump.read_bytes()),DUMP_SHA,"main dump"),(sha(patch),PATCH_SHA,"patch")]:
        if actual != expected:
            raise RuntimeError(label+" identity mismatch")
    if patch[CAVE-MODULE:CAVE-MODULE+len(payload)] != payload:
        raise RuntimeError("Payload does not match verified component")
    lut_file=ROOT.parent/"s5-v29-offline/lut-firmware-ui-experiment/film_lut.h"
    lut=[int(v) for v in re.findall(r"\d+",lut_file.read_text().split("{",1)[1])]
    if len(lut)!=33**3*3 or struct.pack("<%dH"%len(lut),*lut) not in payload:
        raise RuntimeError("Reference LUT is not the table embedded in the actual payload")
    if a.input:
        with Image.open(a.input) as image:
            frame=ImageOps.fit(ImageOps.exif_transpose(image).convert("RGB"),(WIDTH,HEIGHT),method=Image.Resampling.LANCZOS)
    else:
        frame=scene()
    inputs=to_nv16(frame)
    del frame
    for label,plane in zip(("Y","C"),inputs):
        (a.output/("input-"+label+".bin")).write_bytes(plane)
    records=[]; images=[]
    for mode in (0,1):
        actual,record=run_hook(a,mode,inputs,patch,payload,dump)
        if mode:
            record["independent_reference"]=verify_pixels(inputs,actual,lut)
            record["checks"]["output_changed"] = any(x != y for x,y in zip(actual,inputs))
        if not all(record["checks"].values()):
            raise RuntimeError("QEMU output checks failed: "+json.dumps(record))
        image=from_nv16(*actual)
        path=a.output/("capture_lut.jpg" if mode else "capture_off.jpg")
        comment="Offline synthetic NV16 -> experimental S5 LUT in QEMU -> Pillow host JPEG; not original camera capture"
        exif=Image.Exif();exif[270]=comment
        image.save(path,quality=95,subsampling=1,exif=exif,comment=comment.encode())
        with Image.open(path) as decoded:
            decoded.load()
            if decoded.size!=(WIDTH,HEIGHT) or decoded.format!="JPEG":
                raise RuntimeError("JPEG decode verification failed")
        record["jpeg"]={"path":str(path),"sha256":sha(path.read_bytes()),"bytes":path.stat().st_size,"decoded_size":[WIDTH,HEIGHT]}
        image.thumbnail((900,600),Image.Resampling.LANCZOS)
        images.append(image)
        records.append(record)
        print(("LUT on" if mode else "LUT off")+": full frame PASS, "+path.name,flush=True)
    preview=Image.new("RGB",(1800,650),(22,25,30));d=ImageDraw.Draw(preview)
    for i,(image,label) in enumerate(zip(images,("LUT OFF | HOST JPEG","ACTUAL BIN LUT IN QEMU | HOST JPEG"))):
        d.text((20+900*i,18),label,fill="white")
        preview.paste(image,(900*i,50))
    preview.save(a.output/"preview.jpg",quality=90)
    result={"passed":True,"frame":[WIDTH,HEIGHT],"source":"host generated synthetic test scene" if not a.input else str(a.input.resolve()),
            "package_sha256":UI_SHA,"payload_sha256":sha(payload),"qemu_sha256":sha(a.qemu.read_bytes()),
            "input_plane_sha256":dict(zip(("Y","C"),map(sha,inputs))),"reference_lut_sha256":sha(lut_file.read_bytes()),
            "jpeg_encoder":"Pillow %s on macOS host"%PIL_VERSION,"cases":records,
            "preview_mean_absolute_rgb_difference":ImageStat.Stat(ImageChops.difference(*images)).mean,
            "limitations":["No S5 menu, sensor, shutter, autofocus, AE, AWB, ISP or native camera JPEG encoder executed",
                           "Actual experimental BIN code and original getter/memcpy execute with supplied registers and flat synthetic RAM",
                           "NV16 CbCr, full-range BT.601 and descriptor ABI remain experimental assumptions",
                           "Color conversion, chroma subsampling and final JPEG encoding use host Pillow",
                           "Independent integer reference covers sampled pixels; full off-case identity and every row padding byte checked"]}
    (a.output/"verification.json").write_text(json.dumps(result,indent=2)+"\n")
    print(a.output)


if __name__=="__main__":
    main()
