#!/usr/bin/env python3
"""Original S5 JPU driver -> explicit host software device -> guest JPEG.

An experimental debugger-mediated device model, not a native QEMU MMIO device.
Only the observed 6000x4000 NV16 job and polling status path are supported.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import time
import uuid

from PIL import Image, __version__ as PIL_VERSION

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from capture import from_nv16
from probe_jpu import (ROOT,QemuArm,BASE,MODULE,START,STATUS,DESC,STACK,RETURN,
                       OBJECT,Y,C,OUTPUT,MMIO,STRIDE,HEIGHT,DUMP_SHA,invoke)

CONTROL,DEVICE_STATUS,IRQ_STATUS=0x7c156004,0x7c157808,0x7c15603c
LENGTH_REGS=(0x7c156050,0x7c15602c,0x7c156030,0x7c156034)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def word(emu,address):
    return struct.unpack("<I",emu.read_mem(address,4))[0]


def set_word(emu,address,value):
    emu.write_mem(address,struct.pack("<I",value))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture-dir",type=Path,default=HERE/"results/first-capture",
                   help="An already verified capture.py output; LUT-on planes are used")
    p.add_argument("--output",type=Path)
    a=p.parse_args()
    a.output=(a.output or HERE/"results"/("jpu-model-"+time.strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6])).resolve()
    a.output.mkdir(parents=True,exist_ok=False)
    record=json.loads((a.capture_dir/"verification.json").read_text())
    if not record["passed"]:
        raise RuntimeError("Capture output is not verified")
    active=next(case for case in record["cases"] if case["mode"]==1)
    planes=[a.capture_dir/"qemu-lut-on"/("output-"+name+".bin") for name in ("Y","C")]
    for name,path in zip(("Y","C"),planes):
        if path.stat().st_size!=STRIDE*HEIGHT or sha(path.read_bytes())!=active["plane_sha256"][name]:
            raise RuntimeError("Capture plane identity mismatch")
    dump=Path.home()/"相机功能验证/data/program_0x86280000.bin"
    code=dump.read_bytes()
    if sha(code)!=DUMP_SHA:
        raise RuntimeError("Main dump identity mismatch")
    stock=(ROOT.parent/"s5-v29-offline/unpacked-v29/07_postboot3.bin").read_bytes()
    lo,hi=0x877f6600,0x877f6f04
    if code[lo-BASE:hi-BASE]!=stock[lo-MODULE:hi-MODULE]:
        raise RuntimeError("Original JPU driver bytes mismatch")
    # Verify literal-derived register locations used by the model, so a changed
    # firmware layout cannot silently receive the assumed completion values.
    literals={0x877f69c0:0x7c156000,0x877f69c4:0x7c157800,
              0x877f6eec:0x7c156024,0x877f6ef0:0x7c157000}
    for address,value in literals.items():
        if struct.unpack_from("<I",code,address-BASE)[0]!=value:
            raise RuntimeError("JPU register literal mismatch")
    result={"model":"debugger-mediated software JPU, Pillow host backend",
            "original_driver_region_sha256":sha(code[lo-BASE:hi-BASE]),
            "capture_record":str((a.capture_dir/"verification.json").resolve()),
            "capture_plane_sha256":active["plane_sha256"],"events":[]}
    with QemuArm(ROOT/"toolchain/build/qemu-system-arm",a.output/"qemu",ram="3G",cpu="cortex-a7",
                 loads=[(dump,BASE),(planes[0],Y),(planes[1],C)]) as emu:
        emu.breakpoint_add(RETURN)
        for address in (0x7c156000,0x7c157000,0x7c157800):
            emu.write_mem(address,bytes(0x400))
        # The descriptor is a deliberately synthetic job. Only fields consumed
        # by the observed low-level driver are provided; encoder tables/quality
        # are interpreted by the software backend, not by S5 hardware.
        desc=bytearray(0x80)
        struct.pack_into("<IIHHIIHH",desc,8,Y,STRIDE*HEIGHT,STRIDE,HEIGHT,C,STRIDE*HEIGHT,STRIDE,HEIGHT)
        struct.pack_into("<HH",desc,0x24,6000,4000)
        struct.pack_into("<III",desc,0x28,OUTPUT,4*1024*1024,4*1024*1024)
        emu.write_mem(DESC,desc)
        emu.write_mem(OUTPUT,bytes([0xcc])*4096)
        # A data-watchpoint attempt did not respond in this QEMU build. Trace
        # the known original start sequence and then its precise store opcode.
        emu.breakpoint_add(0x877f6856)
        started=invoke(emu,START,DESC)
        result["events"].append({"stage":"original_driver_start_sequence",**started})
        if emu.read_reg("pc")!=0x877f6856:
            raise RuntimeError("Expected original start sequence was not reached")
        emu.breakpoint_remove(0x877f6856)
        emu.breakpoint_add(0x877f671c)
        stop=emu.resume(timeout=5)
        result["events"].append({"stage":"original_start_register_store","stop":stop,
                                 "registers":{k:hex(v) for k,v in emu.registers().items()}})
        if emu.read_reg("pc")!=0x877f671c:
            raise RuntimeError("Expected original register store was not reached")
        if emu.read_reg("r1")!=CONTROL or emu.read_reg("r3")&1!=1:
            raise RuntimeError("Original store is not the observed start-bit write")
        emu.breakpoint_remove(0x877f671c)
        # Complete the original store, then verify its actual guest RAM value.
        emu.step(timeout=5)
        if not word(emu,CONTROL)&1:
            raise RuntimeError("Original driver did not commit its start bit")
        stop=emu.resume(timeout=5)
        if emu.read_reg("pc")!=RETURN:
            raise RuntimeError("Original start routine did not return")
        result["events"].append({"stage":"original_start_return","stop":stop,"pc":hex(emu.read_reg("pc"))})
        pre=invoke(emu,STATUS,DESC+0x100)
        pre_words=struct.unpack("<3I",emu.read_mem(DESC+0x100,12))
        if not pre["returned"] or pre_words!=(0,0,0):
            raise RuntimeError("Unexpected pre-completion status")
        result["before_software_completion"]={"original_status":pre,"words":list(pre_words),
                                              "output_sentinel_intact":emu.read_mem(OUTPUT,4096)==bytes([0xcc])*4096}
        # Read the job from what the real driver actually wrote, rather than
        # taking the Python-supplied descriptor values as the device request.
        job={"y_address":word(emu,MMIO+0x78),"y_stride":word(emu,MMIO+0x7c),"y_height":word(emu,MMIO+0x80),
             "c_address":word(emu,MMIO+0x84),"c_stride":word(emu,MMIO+0x88),"c_height":word(emu,MMIO+0x8c),
             "width":word(emu,MMIO+0xa4),"height":word(emu,MMIO+0xa8),
             "output_address":word(emu,MMIO+0x60),"output_end":word(emu,MMIO+0x70)}
        if (job["width"],job["height"],job["y_stride"],job["c_stride"],job["y_height"],job["c_height"])!=(6000,4000,6016,6016,4000,4000):
            raise RuntimeError("Software model supports only the observed full-frame NV16 layout")
        frame=[]
        for name in ("y","c"):
            path=a.output/("device-input-"+name+".bin")
            emu.dump_memory(job[name+"_address"],job[name+"_stride"]*job[name+"_height"],path)
            data=path.read_bytes()
            if sha(data)!=active["plane_sha256"][name.upper()]:
                raise RuntimeError("Device read differs from verified QEMU LUT plane")
            frame.append(data)
        output=io.BytesIO()
        comment="Software JPU model: original S5 driver in QEMU; Pillow host codec; synthetic frame; not camera JPEG"
        exif=Image.Exif();exif[270]=comment
        from_nv16(*frame).save(output,format="JPEG",quality=95,subsampling=1,exif=exif,comment=comment.encode())
        jpeg=output.getvalue()
        capacity=job["output_end"]-job["output_address"]
        if not 0<len(jpeg)<=capacity<=8*1024*1024:
            raise RuntimeError("Software JPEG exceeds the original driver's output-buffer bounds")
        emu.write_mem(job["output_address"],jpeg)
        # 0x877f6d88 combines these four low bytes in big-endian order, then
        # subtracts 14. This is an observed reader ABI, not a hardware spec.
        device_length=len(jpeg)+14
        for address,shift in zip(LENGTH_REGS,(24,16,8,0)):
            set_word(emu,address,(device_length>>shift)&255)
        # 0x877f6940 only returns 0x0b when both observed bits are set.
        set_word(emu,DEVICE_STATUS,word(emu,DEVICE_STATUS)|2)
        set_word(emu,IRQ_STATUS,word(emu,IRQ_STATUS)|0x40)
        # The status wrapper exposes bit zero here as its third result word.
        # Keep zero; no peripheral error condition is being modeled.
        set_word(emu,MMIO+0x4c,0)
        result["software_device_writes"]={"backend":"Pillow "+PIL_VERSION,"quality":95,"subsampling":1,
              "buffer_address":hex(job["output_address"]),"jpeg_bytes":len(jpeg),"jpeg_sha256":sha(jpeg),
              "status_registers":{hex(DEVICE_STATUS):hex(word(emu,DEVICE_STATUS)),hex(IRQ_STATUS):hex(word(emu,IRQ_STATUS))},
              "length_registers":{hex(address):word(emu,address) for address in LENGTH_REGS}}
        post=invoke(emu,STATUS,DESC+0x100)
        post_words=struct.unpack("<3I",emu.read_mem(DESC+0x100,12))
        if not post["returned"] or post_words!=(0xb,len(jpeg),0):
            raise RuntimeError("Original status wrapper did not report the modeled completion and exact length")
        result["after_software_completion"]={"original_status":post,"words":list(post_words)}
        exported=a.output/"guest-output.jpg"
        emu.dump_memory(job["output_address"],post_words[1],exported)
        if exported.read_bytes()!=jpeg:
            raise RuntimeError("Exported guest output differs from software device DMA write")
        with Image.open(exported) as decoded:
            decoded.load()
            if decoded.size!=(6000,4000) or decoded.format!="JPEG":
                raise RuntimeError("Guest JPEG failed decode validation")
            decoded.thumbnail((1200,800),Image.Resampling.LANCZOS)
            decoded.save(a.output/"preview.jpg",quality=90)
        result["job_read_from_guest_mmio"]=job
        result["export"]={"path":str(exported),"sha256":sha(exported.read_bytes()),"decoded_dimensions":[6000,4000]}
    result.update(passed=True,limitations=["Pillow is an explicitly substituted software device; original S5 compression hardware/quality/tables are not reproduced",
        "Original unmodified driver start/configuration/status functions execute, with a synthetic incomplete job descriptor",
        "Debugger instruction breakpoints mediate one synchronous encode request; no real QEMU MMIO implementation or interrupt wiring",
        "Completion register semantics are derived from observed firmware readers; no physical device validation",
        "Uses previously verified synthetic QEMU LUT-on planes; no menu, sensor, shutter or camera ISP execution"])
    (a.output/"verification.json").write_text(json.dumps(result,indent=2)+"\n")
    print("Original driver -> software JPU -> original status -> guest JPEG: PASS")
    print("Original status:",post_words,"; guest JPEG: 6000 x 4000")
    print(a.output)


if __name__=="__main__":
    main()
