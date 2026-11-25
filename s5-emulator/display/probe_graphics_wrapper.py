#!/usr/bin/env python3
"""Run an original S5 application drawing wrapper into the software VG backend.

This validates the API interception boundary with original ARM instructions.
Its test shape and surface are synthetic. It does not boot or render a menu.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
sys.path.insert(0,str(ROOT))
from probe.qemu_arm import QemuArm
from display.graphics_hle import GraphicsHLE, word_float


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=HERE/"wrapper-check")
    args=parser.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    image=HERE/"prepared/original-rtos-and-osddata.bin"
    plan=json.loads((HERE/"prepared/load-plan.json").read_text())
    data=image.read_bytes()
    expected="fb95f93ee28628de61df77c5b6efcde56be64e88d1815d660fbac0325653eefe"
    if hashlib.sha256(data).hexdigest()!=expected:raise RuntimeError("Original component image hash mismatch")
    report={"synthetic_surface_and_shape":True,"menu_rendered":False,"original_wrapper":"0x072a34d6",
            "scope":"Original app wrapper, not complete menu logic; EGL/VG software state deliberately supplied", "stops":[]}
    with QemuArm(ROOT/"toolchain/build/qemu-system-arm",output/"qemu",loads=[(image,0x06280000)]) as emu:
        driver=GraphicsHLE(emu,output/"graphics")
        surface=driver.backend.create_surface(64,64,origin="synthetic geometry for actual S5 ARM wrapper ABI check")
        driver.backend.make_current(surface)
        driver.backend.set_state(0x1121,[0,0,0,1]);driver.backend.clear(0,0,64,64)
        paint=driver.backend.create_paint();driver.backend.paints[paint][0x1a01]=[0,1,0,1];driver.backend.set_paint(paint,2)
        emu.write_mem(0x21000,bytes([2,4,4,4,0]))
        emu.write_mem(0x22000,struct.pack("<8h",8,8,24,8,24,24,8,24))
        emu.write_mem(0x20000,struct.pack("<4I",5,0x21000,0x22000,2))
        for register,value in [("cpsr",0x600001f3),("sp",0x20000),("lr",0x18001),
                               ("pc",0x072a34d6),("r0",0),("r1",1),("r2",word_float(1)),("r3",0)]:
            emu.write_reg(register,value)
        preserved={index:0x11110000+index for index in range(4,12)}
        for index,value in preserved.items():emu.write_reg(index,value)
        for pc in (0x065da884,0x065d9280,0x18000):emu.breakpoint_add(pc)
        for _ in range(5):
            stop=emu.resume(timeout=3);pc=emu.read_reg("pc")
            report["stops"].append({"pc":hex(pc),"reply":stop})
            if pc==0x18000:break
            if pc not in (0x065da884,0x065d9280):raise RuntimeError("Unexpected original wrapper stop")
            driver.handle(pc,emu.registers())
        frame=driver.backend.surfaces[surface].image()
        report["returned"]=emu.read_reg("pc")==0x18000
        report["return_r0"]=emu.read_reg("r0")
        report["callee_saved_preserved"]=all(emu.read_reg(k)==v for k,v in preserved.items())
        report["stack_preserved"]=emu.read_reg("sp")==0x20000
        report["captured_original_api_args_match_expected"]=[x["name"] for x in driver.backend.events].count("draw_captured_path")==1
        report["pixels_match_known_test_shape"]=frame.getpixel((16,47))==(0,255,0,255) and frame.getpixel((40,40))==(0,0,0,255)
        # Explicit probe snapshot, no synthetic eglSwapBuffers guest call.
        frame.save(output/"synthetic-wrapper-result.png")
        driver.close()
    report["passed"]=all(report[k] for k in ("returned","callee_saved_preserved","stack_preserved","captured_original_api_args_match_expected","pixels_match_known_test_shape"))
    (output/"verification.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))


if __name__=="__main__":main()
