#!/usr/bin/env python3
"""Execute original scatter initialization and compare complete destination bytes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
sys.path.insert(0,str(ROOT))
from probe.qemu_arm import QemuArm
from display.prepare_components import prepare, BASE, PARTS


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path,default=HERE/"scatter-check")
    a=p.parse_args();a.output=a.output.resolve();a.output.mkdir(parents=True,exist_ok=False)
    plan=prepare(HERE/"prepared")
    full=Path(plan["image"]).read_bytes()
    results=[]
    for kind,path in [("program_only",PARTS/"03_program.bin"),("all_modules",Path(plan["image"]))]:
        checks=[]
        with QemuArm(ROOT/"toolchain/build/qemu-system-arm",a.output/kind,loads=[(path,BASE)]) as emu:
            for key,val in [("cpsr",0x600001f3),("sp",0x13000),("pc",0x06280090)]:
                emu.write_reg(key,val)
            emu.breakpoint_add(0x068e4684)
            stop=emu.resume(timeout=5)
            if emu.read_reg("pc")!=0x068e4684:
                raise RuntimeError("Original scatter handler did not reach its normal continuation")
            for index,item in enumerate(plan["scatter"]):
                source,dest,size=int(item["source"],16),int(item["destination"],16),item["bytes"]
                out=a.output/kind/("region-%d.bin"%index)
                emu.dump_memory(dest,size,out)
                actual=out.read_bytes()
                expected=full[source-BASE:source-BASE+size] if item["operation"]=="copy" else bytes(size)
                checks.append({**item,"all_bytes_match_full_component_source":actual==expected,
                               "destination_sha256":hashlib.sha256(actual).hexdigest(),
                               "expected_sha256":hashlib.sha256(expected).hexdigest(),
                               "destination_all_zero":not any(actual)})
            results.append({"kind":kind,"original_scatter_returned":True,"stop":stop,"pc":"0x068e4684","checks":checks})
    expected_bad=[item for item in results[0]["checks"] if not item["all_bytes_match_full_component_source"]]
    passed=all(item["all_bytes_match_full_component_source"] for item in results[1]["checks"]) and len(expected_bad)==3
    report={"passed":passed,"cases":results,"menu_rendered":False,
            "scope":"Original 0x06280090 scatter routine, stopping before C runtime continuation; synthetic stack/CPU state",
            "finding":"Loading only program leaves three postboot RW copies zero; loading all modules restores exact original bytes"}
    (a.output/"verification.json").write_text(json.dumps(report,indent=2)+"\n")
    print("Original scatter full-byte check:","PASS" if passed else "FAIL")
    print("Program-only incorrect regions:",[x["destination"] for x in expected_bad])
    print(a.output)
    return 0 if passed else 1


if __name__=="__main__":
    sys.exit(main())
