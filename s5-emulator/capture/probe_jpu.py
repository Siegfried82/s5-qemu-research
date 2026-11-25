#!/usr/bin/env python3
"""Bounded execution of original S5 JPU driver code with RAM-backed MMIO.

This deliberately does NOT provide a JPU device or claim JPEG encoding.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from probe.qemu_arm import QemuArm

BASE=0x86280000
MODULE=0x87707000
CONFIG,START,STATUS=0x877f68ca,0x877f6e98,0x877f6ebe
DESC,STACK,RETURN,OBJECT=0x8e000000,0x8f00f000,0x8eff0000,0x8e100000
Y,C,OUTPUT=0x90000000,0x92000000,0x94000000
MMIO=0x7c157000
STRIDE,HEIGHT=6016,4000
DUMP_SHA="7ae165083396d4c9213f7a1c019ac56823d0ed4196fa14b8eb25e553a45a21e4"


def invoke(emu,entry,r1):
    for i in range(16):
        emu.write_reg(i,0)
    for key,value in [("sp",STACK),("lr",RETURN|1),("r0",OBJECT),("r1",r1),("cpsr",0x600001f3),("pc",entry)]:
        emu.write_reg(key,value)
    try:
        stop=emu.resume(timeout=5)
        returned=emu.read_reg("pc")==RETURN
        result={"returned":returned,"stop":stop}
    except TimeoutError as exc:
        result={"returned":False,"timeout":str(exc)}
    result["registers"]={k:hex(v) for k,v in emu.registers().items()}
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path)
    a=p.parse_args()
    a.output=(a.output or ROOT/"capture/results"/("jpu-"+time.strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6])).resolve()
    a.output.mkdir(parents=True,exist_ok=False)
    dump=Path.home()/"相机功能验证/data/program_0x86280000.bin"
    code=dump.read_bytes()
    if hashlib.sha256(code).hexdigest()!=DUMP_SHA:
        raise RuntimeError("Main dump identity changed")
    stock=(ROOT.parent/"s5-v29-offline/unpacked-v29/07_postboot3.bin").read_bytes()
    start,end=0x877f6600,0x877f6f04
    if code[start-BASE:end-BASE]!=stock[start-MODULE:end-MODULE]:
        raise RuntimeError("Driver region differs from original decrypted firmware component")
    # The original driver treats these as hardware registers. Here they are
    # plain memory, so writing a start bit cannot encode or signal completion.
    with QemuArm(ROOT/"toolchain/build/qemu-system-arm",a.output/"qemu",ram="3G",cpu="cortex-a7",loads=[(dump,BASE)]) as emu:
        emu.breakpoint_add(RETURN)
        emu.write_mem(MMIO,bytes(4096))
        planes=struct.pack("<IIHHIIHH",Y,STRIDE*HEIGHT,STRIDE,HEIGHT,C,STRIDE*HEIGHT,STRIDE,HEIGHT)
        emu.write_mem(DESC,planes)
        config=invoke(emu,CONFIG,DESC)
        words=struct.unpack("<9I",emu.read_mem(MMIO+0x78,36))
        config["written_registers"]={hex(MMIO+0x78+4*i):hex(v) for i,v in enumerate(words)}
        config["checks"]={"y_pointer_written":words[0]==Y,"y_stride_written":words[1]==STRIDE,
                          "y_height_written":words[2]==HEIGHT,"c_pointer_written":words[3]==C,
                          "c_stride_written":words[4]==STRIDE,"c_height_written":words[5]==HEIGHT}
        # Synthetic, incomplete upper-level descriptor. These fields are based
        # on inspected loads in the original driver, not a captured camera job.
        desc=bytearray(0x80)
        desc[8:8+len(planes)]=planes
        struct.pack_into("<HH",desc,0x24,6000,4000)
        struct.pack_into("<III",desc,0x28,OUTPUT,1024*1024,1024*1024)
        emu.write_mem(DESC,desc)
        emu.write_mem(OUTPUT,bytes([0xcc])*4096)
        driver=invoke(emu,START,DESC)
        driver["mmio_words_nonzero"]={hex(MMIO+4*i):hex(v) for i,v in enumerate(struct.unpack("<256I",emu.read_mem(MMIO,1024))) if v}
        driver["output_first_4096_bytes_unchanged"]=emu.read_mem(OUTPUT,4096)==bytes([0xcc])*4096
        emu.write_mem(DESC+0x100,bytes([0xcc])*12)
        status=invoke(emu,STATUS,DESC+0x100)
        status["returned_status_words"]=list(struct.unpack("<3I",emu.read_mem(DESC+0x100,12)))
    result={"original_driver_bytes_verified":True,"driver_region":[hex(start),hex(end)],
            "driver_region_sha256":hashlib.sha256(code[start-BASE:end-BASE]).hexdigest(),
            "input_configuration":config,"driver_start":driver,"status_query":status,
            "jpeg_produced":False,"limitations":["Standalone functions with fabricated registers and descriptors; no scheduler or camera task",
            "Hardware MMIO deliberately backed by inert RAM, not an implemented JPU model",
            "Synthetic upper-level descriptor is incomplete and not evidence of a valid camera JPEG job",
            "A returned driver function only confirms CPU execution and stores, not device operation",
            "No status completion synthesized and no output JPEG bytes generated"]}
    (a.output/"verification.json").write_text(json.dumps(result,indent=2)+"\n")
    print("Original input config: "+("PASS" if config["returned"] and all(config["checks"].values()) else "FAIL"))
    print("Original start returned:",driver["returned"],"; status words:",status["returned_status_words"],"; JPEG: none")
    print(a.output)


if __name__=="__main__":
    main()
