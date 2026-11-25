#!/usr/bin/env python3
"""Execute the original synchronous ADC function against the QEMU device.

Only its OS semaphore acquire/release calls are substituted in this isolated
probe. The original ADC register writes, polling and result copy all execute.
No IRQ is injected and no original firmware instruction is patched.
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

ENTRY=0x0659ed98
SEMAPHORES=(0x066c23ac,0x066c2558)
RETURN=0x18000
STACK=0x24000
OUTPUT=0x25000


def call(q,channel,count):
    q.write_mem(OUTPUT,bytes.fromhex("cdab")*16)
    for index in range(16):q.write_reg(index,0)
    for register,value in [("cpsr",0x600001f3),("sp",STACK),("lr",RETURN|1),
                           ("pc",ENTRY),("r0",channel),("r1",count),("r2",OUTPUT)]:
        q.write_reg(register,value)
    calls=[]
    for _ in range(6):
        reply=q.resume_cpu(0,timeout=3) if q.machine!="none" else q.resume(timeout=3)
        pc=q.read_reg("pc")
        if pc==RETURN:break
        if pc not in SEMAPHORES:raise RuntimeError("Unexpected ADC stop: "+hex(pc)+" "+reply)
        if q.read_reg("r0")!=25:raise RuntimeError("Unexpected semaphore identity")
        calls.append({"pc":hex(pc),"id":25})
        q.write_reg("r0",0)
        q.write_reg("pc",q.read_reg("lr")&~1)
    else:raise RuntimeError("Original ADC driver did not return")
    return {"channel":channel,"count":count,"r0":q.read_reg("r0"),
            "output_halfwords":list(struct.unpack("<16H",q.read_mem(OUTPUT,32))),
            "semaphore_substitutions":calls,"returned_to_original_lr":q.read_reg("pc")==RETURN,
            "stack_preserved":q.read_reg("sp")==STACK}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=HERE/"driver-check")
    args=parser.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    program=ROOT.parent/"s5-v29-offline/unpacked-v29/03_program.bin"
    data=program.read_bytes()
    contract=json.loads((HERE/"adc-contract.json").read_text())
    if hashlib.sha256(data).hexdigest()!=contract["sha256"]:raise RuntimeError("Program hash mismatch")
    # These bytes are exactly the original scatter source and destination;
    # no boot RAM snapshot or fabricated pointer table is used.
    rw=data[0x070879b8-0x06280000:0x070879b8-0x06280000+0x54fa4]
    rwfile=output/"original-program-rw.bin";rwfile.write_bytes(rw)
    source={"address":"0x070879b8","destination":"0x08a67000","bytes":len(rw),"sha256":hashlib.sha256(rw).hexdigest()}
    report={"original_program_sha256":hashlib.sha256(data).hexdigest(),"rw_source":source,
            "original_adc_instructions_patched":False,"irqs_injected":False,
            "scope":"Isolated original ADC function; semaphore25 acquire/release only are host substitutes; no battery/main decision branches executed", "cases":[]}
    binary=ROOT/"toolchain/build/qemu-system-arm"
    for profile,sample in [("zero",0),("raw_0x300",0x300)]:
        with QemuArm(binary,output/profile,machine="s5-rtos",smp=4,ram="512M",
                     loads=[(program,0x06280000),(rwfile,0x08a67000)],
                     address_ranges=[(0,0x20000000),(0x70000000,0x10000000),(0x80000000,0x20000000)]) as q:
            q.qmp.command("qom-set",{"path":"/machine","property":"adc11-sample","value":sample})
            q.select_cpu(0)
            for pc in (*SEMAPHORES,RETURN):q.breakpoint_add(pc)
            for channel,count,expected in [(11,1,[sample]),(11,0,[sample]),(10,2,[0,sample]),(20,1,[0]),(30,1,[0])]:
                case=call(q,channel,count);case.update(profile=profile,external_raw_adc11=sample,expected_halfwords=expected)
                case["passed"]=(case["r0"]==1 and case["stack_preserved"] and case["returned_to_original_lr"]
                    and case["output_halfwords"][:len(expected)]==expected
                    and case["output_halfwords"][len(expected):]==[0xabcd]*(16-len(expected)))
                report["cases"].append(case)
    report["passed"]=all(x["passed"] for x in report["cases"])
    report["qemu_sha256"]=hashlib.sha256(binary.read_bytes()).hexdigest()
    (output/"verification.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({"passed":report["passed"],"cases":[{k:x[k] for k in ("profile","channel","count","r0","expected_halfwords","passed")} for x in report["cases"]]},indent=2))
    return 0 if report["passed"] else 1


if __name__=="__main__":sys.exit(main())
