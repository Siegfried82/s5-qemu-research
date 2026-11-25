#!/usr/bin/env python3
"""Read-only component-backed model of the observed 0x40000000 storage space."""
import hashlib
import json
from pathlib import Path
import struct

ROOT=Path(__file__).resolve().parents[1]
FIRMWARE=ROOT.parent/"s5-v29-offline"
SOURCE_SHA="b2ec4aeec209693b874df8dfdf11fc85b38fa4675b44ec554b7a167f430827e1"
BASE,FLASH_ORIGIN=0x40000000,0x01240000


class FirmwareStorage:
    """Only known component bytes are readable; holes fail explicitly."""
    def __init__(self):
        source=(FIRMWARE/"S5___V29.bin").read_bytes()
        if hashlib.sha256(source).hexdigest()!=SOURCE_SHA:
            raise ValueError("Original firmware package identity mismatch")
        self.regions=[]
        for index in range(5,48):
            pos=0x2ec+index*0x5c
            name=source[pos:pos+12].rstrip(b"\0").decode("ascii")
            size,flash=struct.unpack_from("<2I",source,pos+0x10)
            data=(FIRMWARE/"unpacked-v29"/("%02d_%s.bin"%(index,name))).read_bytes()
            digest=hashlib.sha256(data).hexdigest()
            if len(data)!=size or digest!=source[pos+0x1c:pos+0x3c].hex():
                raise ValueError("Original component identity mismatch: "+name)
            start=BASE+flash-FLASH_ORIGIN
            if start<BASE:
                raise ValueError("Component lies before modeled storage origin")
            self.regions.append({"index":index,"name":name,"start":start,"end":start+size,
                                 "flash_offset":flash,"sha256":digest,"data":data})
        self.regions.sort(key=lambda row:row["start"])
        for left,right in zip(self.regions,self.regions[1:]):
            if left["end"]>right["start"]:
                raise ValueError("Overlapping logical component ranges")

    def read(self,address,size):
        if not isinstance(address,int) or not isinstance(size,int) or size<0 or address<BASE or address+size>0x100000000:
            raise ValueError("Invalid modeled storage read")
        result=bytearray()
        while size:
            row=next((r for r in self.regions if r["start"]<=address<r["end"]),None)
            if row is None:
                raise ValueError("Unmodeled storage gap at 0x%x"%address)
            count=min(size,row["end"]-address)
            result.extend(row["data"][address-row["start"]:address-row["start"]+count])
            address+=count;size-=count
        return bytes(result)

    def manifest(self):
        return {"source_sha256":SOURCE_SHA,"logical_base":hex(BASE),"flash_origin":hex(FLASH_ORIGIN),
                "formula":"logical = package component flash_offset (+0x14) - 0x01240000 + 0x40000000",
                "evidence":"postboot table 0x06f52bb0, osdover read 0x06867c8c/0x06867e5c, koutei_kao read 0x06867ca0/0x06867e88",
                "read_api":{"0x06809992":"r0=logical byte source, r1=RAM destination, r2=byte count; 0 success/-1 failure; lock wrapper",
                            "0x068098b2":"same r0/r1/r2 plus r3=cache-routing flag; original unaligned-byte reader",
                            "0x068609b6":"r0=(logical-0x40000000)/512 sector index, r1=sector count, r2=destination, r3=1 in observed calls; 0 success"},
                "regions":[{k:(hex(v) if k in ("start","end","flash_offset") else v) for k,v in row.items() if k!="data"} for row in self.regions],
                "limitations":["Logical mapping reconstructed from original firmware constants, not physical NAND geometry",
                               "Only packaged bytes modeled; gaps and missing persistent settings fail rather than invent data",
                               "Read-only HLE; no erase, program, bad blocks, timing, ECC, interrupts or DMA coherency"]}


# Public runner-facing name; retained descriptive name keeps existing probes stable.
StorageMap=FirmwareStorage


if __name__=="__main__":
    model=FirmwareStorage()
    target=Path(__file__).resolve().parent/"prepared/storage-map.json"
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(model.manifest(),indent=2)+"\n")
    print(target)
