#!/usr/bin/env python3
"""Execute the existing S5 UI/LUT firmware fragment in QEMU with synthetic RAM."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from probe.qemu_arm import QemuArm
DUMP_BASE, MODULE, CALL = 0x86280000, 0x87707000, 0x877537a2
GETTER, MEMCPY, CAVE = 0x87864acc, 0x8782bd3c, 0x879f7900
DESCRIPTOR, STACK, SOURCE_DESCRIPTOR = 0x8e000000, 0x8f000000, 0x0b3d4224
Y, C, STRIDE = 0x90000000, 0x92000000, 6016
DUMP_SHA = "7ae165083396d4c9213f7a1c019ac56823d0ed4196fa14b8eb25e553a45a21e4"
PATCH_SHA = "bae10700dfa6abd7021f84b59590e0e566f467651d2cac2e8a536a444527469a"
UI_SHA = "5727be9315ee229291783a483090efb88be9fd75fcdc032f19314152738564de"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stop_at(emu, address, trace, label, timeout=15):
    emu.breakpoint_add(address)
    stop = emu.resume(timeout=timeout)
    pc = emu.read_reg("pc")
    trace.append({"stage": label, "pc": hex(pc), "stop": stop})
    if not stop.startswith(("T05", "S05")) or pc != address:
        raise RuntimeError("Unexpected stop at %s: %s pc=%s" % (label, stop, hex(pc)))
    emu.breakpoint_remove(address)


def run_case(a, name, width, mode):
    started = time.monotonic()
    r = {"case": name, "width": width, "view_assist_type": mode, "passed": False, "trace": []}
    patch = (a.inputs/"postboot3-lut-experiment.bin").read_bytes()
    dump = Path.home()/"相机功能验证/data/program_0x86280000.bin"
    desc = bytearray(0x38)
    struct.pack_into("<IIHHIIHHHHI", desc, 0, Y, STRIDE*4000, STRIDE, 4000,
                     C, STRIDE*4000, STRIDE, 4000, 0, 0, 0)
    struct.pack_into("<HHI", desc, 0x1c, width, 4000, 2)
    try:
        loads=[(dump,DUMP_BASE)]
        if name == "enabled_full_frame":
            for plane,address,value in [("Y",Y,100),("C",C,128)]:
                path=a.output/("input-"+plane+".bin")
                path.write_bytes(bytes([value])*(STRIDE*4000))
                loads.append((path,address))
        with QemuArm(a.qemu, a.output/name, ram="3G", cpu="cortex-a7", loads=loads) as emu:
            emu.write_mem(CAVE, (a.inputs/"payload.bin").read_bytes())
            emu.write_mem(CALL, patch[CALL-MODULE:CALL-MODULE+4])
            emu.write_mem(0x08bdc310, struct.pack("<I", mode))
            emu.write_mem(SOURCE_DESCRIPTOR, desc)
            emu.write_mem(Y, bytes([100])*STRIDE)
            emu.write_mem(C, bytes([128])*STRIDE)
            for i in range(16):
                emu.write_reg(i, 0)
            for i in range(4,12):
                emu.write_reg(i, 0x12345678)
            for key, value in [("sp",STACK+0xf000),("r0",1),("r1",DESCRIPTOR),
                               ("cpsr",0x600001f3),("pc",CALL)]:
                emu.write_reg(key,value)
            if not emu.read_reg("cpsr") & 0x20:
                raise RuntimeError("Thumb state was not set")
            stop_at(emu, GETTER, r["trace"], "original_getter")
            emu.step(timeout=5)
            stop_at(emu, MEMCPY, r["trace"], "original_memcpy")
            emu.step(timeout=5)
            active = name in ("enabled_first_tile","enabled_full_frame")
            expected = (108,125,131) if active else (100,128,128)
            if name == "enabled_first_tile":
                stop_at(emu, CAVE, r["trace"], "first_lut_entry")
                emu.step(timeout=5)
                stop_at(emu, CAVE, r["trace"], "second_lut_entry_after_first_tile")
            else:
                stop_at(emu, CALL+4, r["trace"], "original_call_return", timeout=60 if active else 15)
            y,c = emu.read_mem(Y,120),emu.read_mem(C,120)
            checks = {"descriptor_copied": emu.read_mem(DESCRIPTOR,0x38)==desc,
                      "120_y_pixels_expected": y==bytes([expected[0]])*120,
                      "60_cbcr_pairs_expected": c==bytes(expected[1:])*60,
                      "row_padding_untouched": emu.read_mem(Y+6000,STRIDE-6000)==bytes([100])*(STRIDE-6000)}
            if name != "enabled_full_frame":
                checks["next_tile_untouched"] = emu.read_mem(Y+120,120)==bytes([100])*120 and emu.read_mem(C+120,120)==bytes([128])*120
            else:
                full_checks={}
                for plane,address,row in [("Y",Y,bytes([108])*6000+bytes([100])*(STRIDE-6000)),
                                           ("C",C,bytes([125,131])*3000+bytes([128])*(STRIDE-6000))]:
                    path=a.output/("output-"+plane+".bin")
                    emu.dump_memory(address,STRIDE*4000,path)
                    actual=path.read_bytes()
                    expected_hash=sha(row*4000)
                    checks["entire_"+plane+"_plane_matches"] = sha(actual)==expected_hash
                    full_checks[plane]={"bytes":len(actual),"actual_sha256":sha(actual),"expected_sha256":expected_hash}
                r["full_frame"]={"width":6000,"height":4000,"pixels":24000000,"planes":full_checks}
            if name != "enabled_first_tile":
                checks.update(return_value_zero=emu.read_reg("r0")==0,
                              stack_restored=emu.read_reg("sp")==STACK+0xf000,
                              saved_registers_preserved=all(emu.read_reg(i)==0x12345678 for i in range(4,12)))
            r.update(checks=checks, passed=all(checks.values()), output_y_cb_cr=[y[0],c[0],c[1]],
                     stopped_pc=hex(emu.read_reg("pc")), full_function_returned=name != "enabled_first_tile")
    except Exception as exc:
        r["error"] = str(exc)
    r["elapsed_seconds"] = round(time.monotonic()-started,3)
    (a.output/(name+".json")).write_text(json.dumps(r,indent=2)+"\n")
    return r


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs",type=Path,default=ROOT/"results/handoff-ui-check")
    p.add_argument("--output",type=Path)
    p.add_argument("--qemu",type=Path,default=ROOT/"toolchain/build/qemu-system-arm")
    p.add_argument("--case",choices=("all","full-frame"),default="all")
    a = p.parse_args()
    if a.output is None:
        a.output=ROOT/"results"/(time.strftime("ui-qemu-%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6])
    a.inputs,a.output,a.qemu = a.inputs.resolve(),a.output.resolve(),a.qemu.resolve()
    a.output.mkdir(parents=True,exist_ok=False)
    package = json.loads((a.inputs/"package-verification.json").read_text())
    if package["package_sha256"]!=UI_SHA or sha(Path(package["package"]).read_bytes())!=UI_SHA:
        p.exit(1,"The original UI experiment BIN changed; do not reuse these cached test inputs\n")
    patch,payload = (a.inputs/"postboot3-lut-experiment.bin").read_bytes(),(a.inputs/"payload.bin").read_bytes()
    if sha(Path(package["dump"]).read_bytes())!=DUMP_SHA or sha(patch)!=PATCH_SHA:
        p.exit(1,"Firmware identity mismatch\n")
    if patch[CAVE-MODULE:CAVE-MODULE+len(payload)]!=payload:
        p.exit(1,"Payload/component mismatch\n")
    cases=[]
    selected = [("enabled_full_frame",6000,1)] if a.case=="full-frame" else [("switch_off",6000,0),("hlg_mode",6000,2),("wrong_dimensions",5999,1),("enabled_first_tile",6000,1)]
    for name,width,mode in selected:
        r=run_case(a,name,width,mode)
        cases.append(r)
        print(name+": "+("PASS" if r["passed"] else "FAIL "+r.get("error","check failed")),flush=True)
    report={"all_passed":all(c["passed"] for c in cases),"engine":"QEMU 9.2.4 TCG Cortex-A7",
            "package_sha256":package["package_sha256"],"decrypted_component_sha256":sha(patch),
            "qemu_sha256":sha(a.qemu.read_bytes()),"cases":cases,
            "limitations":["Flat synthetic RAM, supplied initial registers and breakpoints",
                           "Actual package code executed, not complete camera boot",
                           "Menu resources verified as bytes; no menu rendering/input simulation",
                           "Full-frame execution only tested when explicitly selected and its case passed",
                           "Synthetic NV16 input; no JPEG hardware, real DMA/cache ownership or camera timing validation"]}
    (a.output/"verification.json").write_text(json.dumps(report,indent=2)+"\n")
    print(a.output)
    return 0 if report["all_passed"] else 1


if __name__=="__main__":
    sys.exit(main())
