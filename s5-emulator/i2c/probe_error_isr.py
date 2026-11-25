#!/usr/bin/env python3
"""Run each original I2C ISR error branch with explicit synthetic register input.

This tests the meaning of status bit 3 to the firmware, not its physical cause.
Only OS context query, IRQ wrapper calls and final event delivery are intercepted.
No device IRQ is injected, no original instruction is changed, and no peer ACK
or successful transaction is supplied.
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

ENTRY=0x06499734
RETURN=0x18000
STACK=0x24000
CONTEXT=0x065477bc
DISABLE=0x0658bb0c
CLEAR=0x0658bb3e
EVENTS=(0x06576118,0x065490e0)
ORIGINAL_SHA="fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe"

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output",type=Path,default=HERE/"error-isr-check")
    args=ap.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    program=ROOT.parent/"s5-v29-offline/unpacked-v29/03_program.bin"
    data=program.read_bytes();assert hashlib.sha256(data).hexdigest()==ORIGINAL_SHA
    binary=ROOT/"toolchain/build/qemu-system-arm"
    cases=[]
    bit0_controls=[]
    with QemuArm(binary,output/"qemu",machine="none",ram="3G",loads=[(program,0x06280000)]) as q:
        for pc in (RETURN,CONTEXT,DISABLE,CLEAR,*EVENTS):q.breakpoint_add(pc)
        # Event18 is the actual original ROM word, read by 066ce4d6.
        original_event=int.from_bytes(data[0x06f49740-0x06280000:0x06f49744-0x06280000],"little")
        assert original_event==18
        for mode in range(4):
            for context in (0,1):
                for bus in (0,4):
                    q.write_mem(0x08d31ac8,bytes(0x20))
                    q.write_mem(0x08d31acc,bytes([mode]))
                    q.write_mem(0x08aa7b0e,bytes([4]))
                    q.write_mem(0x7f058004+bus*0x100,struct.pack("<I",0xf8))
                    for index in range(16):q.write_reg(index,0)
                    for reg,val in [("cpsr",0x600001f3),("sp",STACK),("lr",RETURN|1),("pc",ENTRY),("r0",bus)]:q.write_reg(reg,val)
                    trace=[]
                    for _ in range(8):
                        q.resume(timeout=3);pc=q.read_reg("pc")
                        if pc==RETURN:break
                        if pc not in (CONTEXT,DISABLE,CLEAR,*EVENTS):raise RuntimeError(hex(pc))
                        trace.append({"pc":hex(pc),"r0":q.read_reg("r0"),"r1":q.read_reg("r1")})
                        q.write_reg("r0",context if pc==CONTEXT else 0)
                        q.write_reg("pc",q.read_reg("lr")&~1)
                    else:raise RuntimeError("ISR did not return")
                    event=[e for e in trace if int(e["pc"],16) in EVENTS]
                    irq=[e for e in trace if int(e["pc"],16) in (DISABLE,CLEAR)]
                    passed=(len(event)==1 and event[0]["r0"]==18 and event[0]["r1"]==2
                            and int(event[0]["pc"],16)==EVENTS[context]
                            and [e["pc"] for e in irq]==[hex(DISABLE),hex(CLEAR)]
                            and all(e["r0"]==0x7c+bus for e in irq)
                            and q.read_reg("sp")==STACK and q.read_reg("pc")==RETURN)
                    cases.append({"bus":bus,"mode":mode,"context_query_result":context,
                                  "injected_controller_status":"0xf8","trace":trace,"passed":passed})
        # Exynos-compatible byte layouts alone do not prove compatible error bits.
        # Confirm that changing only status bit0 does not take the error branch.
        normal={0:0x06499518,1:0x0649927e,2:0x06499138,3:0x06498ecc}
        for pc in normal.values():q.breakpoint_add(pc)
        for mode in range(4):
            for status in (0xf0,0xf1):
                q.write_mem(0x08d31acc,bytes([mode]))
                q.write_mem(0x7f058004,struct.pack("<I",status))
                for reg,val in [("cpsr",0x600001f3),("sp",STACK),("lr",RETURN|1),("pc",ENTRY),("r0",0)]:q.write_reg(reg,val)
                q.resume(timeout=3);pc=q.read_reg("pc")
                bit0_controls.append({"mode":mode,"controller_status":hex(status),"reached_pc":hex(pc),
                                      "expected_normal_state_machine":hex(normal[mode]),"passed":pc==normal[mode]})
    report={"passed":all(c["passed"] for c in cases+bit0_controls),"cases":cases,"bit0_controls":bit0_controls,
            "program_sha256":ORIGINAL_SHA,"qemu_sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),
            "original_instructions_modified":False,"actual_device_irq_injected":False,
            "synthetic_input":"Flat RAM at controller status address contains 0xf8; descriptor mode and context are explicit test parameters",
            "substitutions":["logical IRQ disable and pending clear wrappers (logged)","OS context query (0 or 1)","event delivery endpoint (logged, not executed)"],
            "conclusion":"All tested original ISR dispatch modes turn status bit 3 into event18 mask2; this does not establish bit3 is physical NACK or validate RTOS queue/event execution"}
    (output/"verification.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({"passed":report["passed"],"error_cases":len(cases),"bit0_controls":len(bit0_controls)}))
    return 0 if report["passed"] else 1

if __name__=="__main__":sys.exit(main())
