#!/usr/bin/env python3
"""Prepare original S5 program, postboot modules and OSD data at derived addresses."""
import argparse
import hashlib
import json
from pathlib import Path
import struct

ROOT=Path(__file__).resolve().parents[1]
PARTS=ROOT.parent/"s5-v29-offline/unpacked-v29"
BASE=0x06280000


def sha(data):
    return hashlib.sha256(data).hexdigest()


def prepare(output):
    output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    verification=json.loads((PARTS/"verification.json").read_text())
    records={row["index"]:row for row in verification["components"]}
    def component(index):
        record=records[index]
        path=PARTS/record["file"]
        data=path.read_bytes()
        if len(data)!=record["bytes"] or sha(data)!=record["sha256"]:
            raise RuntimeError("Original component identity changed: "+str(path))
        return path,data
    p,program=component(3)
    image=bytearray(program)
    loads=[{"component":"program","file":str(p),"address":hex(BASE),"bytes":len(program),"sha256":sha(program)}]
    for slot,index in enumerate(range(5,10)):
        address=0x06f52bb0+slot*16
        source,destination,header,size=struct.unpack_from("<4I",program,address-BASE)
        path,data=component(index)
        if len(data)!=size or header!=destination or destination!=BASE+len(image):
            raise RuntimeError("Original postboot table disagrees with component geometry")
        words=struct.unpack_from("<7I",data)
        if words[0]!=0x44414f4c:
            raise RuntimeError("Missing original LOAD header")
        loads.append({"component":records[index]["name"],"file":str(path),"address":hex(destination),
                      "bytes":size,"sha256":sha(data),"original_table_address":hex(address),
                      "storage_source":hex(source),"data_copy":{"source":hex(words[5]),"end":hex(words[6]),
                      "destination":hex(words[3]),"bytes":words[6]-words[5]},
                      "zero_region":{"start":hex(words[1]),"end":hex(words[2])}})
        image.extend(data)
    path,osddata=component(21)
    osd_base=BASE+len(image)
    if osd_base!=0x07fe7000 or struct.unpack_from("<I",osddata,0x10)[0]!=osd_base+0x84 or struct.unpack_from("<2I",osddata,0x74)!=(osd_base+0x14,osd_base+0x20):
        raise RuntimeError("OSD data base does not match embedded self pointers")
    loads.append({"component":"osddata","file":str(path),"address":hex(osd_base),"bytes":len(osddata),"sha256":sha(osddata),
                  "address_evidence":"Pointers at component +0x10, +0x74, +0x78 point to +0x84,+0x14,+0x20; immediately precedes program RW destination 0x08a67000"})
    image.extend(osddata)
    scatter=[]
    for address in range(0x062800fc,0x0628015c,16):
        source,dest,size,handler=struct.unpack_from("<4I",program,address-BASE)
        if handler not in (0x062800c4,0x062800e0):
            raise RuntimeError("Unknown scatter handler")
        scatter.append({"table_address":hex(address),"source":hex(source),"destination":hex(dest),"bytes":size,
                        "handler":hex(handler),"operation":"copy" if handler==0x062800c4 else "zero"})
    if BASE+len(image)!=int(scatter[0]["destination"],16):
        raise RuntimeError("End of loaded OSD data does not match original RW destination")
    out=output/"original-rtos-and-osddata.bin"
    out.write_bytes(image)
    font_path,font=component(20)
    if font[:9]!=b"FDL Font\0" or struct.unpack_from("<I",program,0x06867e5c-BASE)[0]!=0x41260000:
        raise RuntimeError("OSD font storage identity/literal mismatch")
    manifest={"image":str(out),"address":hex(BASE),"end":hex(BASE+len(image)),"bytes":len(image),"sha256":sha(image),
              "loads":loads,"scatter":scatter,
              "storage_only_resources":[{"component":"osdover","file":str(font_path),"storage_source":"0x41260000",
              "bytes":len(font),"sha256":sha(font),"evidence":"0x06867c82 loads 0x41260000 at 0x06867c8c and verifies FDL Font via 0x06867c38/0x06809992", 
              "ram_address":None,"reason":"Storage-read API, no permanent RAM base established"}],
              "limitations":["Address derivation from original data tables and code, not complete boot verification",
                  "Does not implement storage reads, interrupts, graphics hardware or menu event loop",
                  "High 0x80000000 alias must be implemented by the board or mapping; copying two independent images is not coherent aliasing",
                  "Do not place osdover at a guessed RAM address; map its storage read requests"]}
    (output/"load-plan.json").write_text(json.dumps(manifest,indent=2)+"\n")
    return manifest


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path,default=ROOT/"display/prepared")
    a=p.parse_args()
    m=prepare(a.output)
    print(json.dumps({k:m[k] for k in ("image","address","end","bytes","sha256")},indent=2))
