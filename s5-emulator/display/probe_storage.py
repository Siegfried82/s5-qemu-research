#!/usr/bin/env python3
"""Verify original byte-reader ABI using HLE only at the sector-read boundary."""
import hashlib
import json
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path.insert(0,str(ROOT))
from probe.qemu_arm import QemuArm
from display.storage import FirmwareStorage,BASE
from display.prepare_components import prepare

RETURN,DEST=0x10000,0x10000000


def main():
    out=HERE/"storage-check";out.mkdir(parents=True,exist_ok=False)
    model=FirmwareStorage();plan=prepare(HERE/"prepared")
    results=[]
    cases=[("font_header",0x41260000,16,DEST),
           ("font_unaligned_head_middle_tail",0x412600ff,0x901,DEST+7),
           ("postboot3_header",0x404a0000,28,DEST),
           ("below_storage_range",0x3fffffff,16,DEST)]
    with QemuArm(ROOT/"toolchain/build/qemu-system-arm",out/"qemu",loads=[(Path(plan["image"]),0x06280000)]) as emu:
        emu.breakpoint_add(RETURN)
        emu.breakpoint_add(0x068609b6)
        for name,source,size,destination in cases:
            emu.write_mem(destination,bytes([0xcc])*(size+16))
            for reg,value in [("cpsr",0x600001f3),("sp",0x14000),("lr",RETURN|1),("r0",source),
                              ("r1",destination),("r2",size),("r3",0),("pc",0x068098b2)]:
                emu.write_reg(reg,value)
            trace=[]
            for event in range(16):
                stop=emu.resume(timeout=3);pc=emu.read_reg("pc")
                if pc==RETURN:break
                if pc!=0x068609b6:raise RuntimeError("Unexpected reader stop: "+hex(pc))
                sector,count,target,device=[emu.read_reg(i) for i in range(4)]
                if count>8192 or device!=1:raise RuntimeError("Unexpected sector read ABI")
                data=model.read(BASE+sector*512,count*512)
                emu.write_mem(target,data)
                trace.append({"sector":sector,"count":count,"source":hex(BASE+sector*512),"target":hex(target),
                              "device":device,"sha256":hashlib.sha256(data).hexdigest()})
                emu.write_reg("r0",0)
                emu.write_reg("pc",emu.read_reg("lr")&~1)
            else:raise RuntimeError("Reader exceeded bounded sector-call count")
            returned=emu.read_reg("r0")
            actual=emu.read_mem(destination,size)
            invalid=name=="below_storage_range"
            checks={"return_value":returned==(0xffffffff if invalid else 0),
                    "bytes":actual==(bytes([0xcc])*size if invalid else model.read(source,size)),
                    "trailing_sentinel":emu.read_mem(destination+size,16)==bytes([0xcc])*16,
                    "stack_restored":emu.read_reg("sp")==0x14000}
            results.append({"case":name,"source":hex(source),"size":size,"destination":hex(destination),
                            "return_value":hex(returned),"sector_calls":trace,"checks":checks,"passed":all(checks.values())})
    report={"passed":all(r["passed"] for r in results),"cases":results,
            "scope":"Original 0x068098b2 executes unchanged; only 0x068609b6 sector I/O is replaced with original component bytes",
            "limitations":["No physical storage controller, lock-wrapper execution, DMA, timing or interrupt emulation",
                           "Unknown packaged-storage gaps are not supplied"]}
    (out/"verification.json").write_text(json.dumps(report,indent=2)+"\n")
    print("Original storage byte-reader + sector HLE:","PASS" if report["passed"] else "FAIL")
    print(out)
    return 0 if report["passed"] else 1


if __name__=="__main__":
    sys.exit(main())
