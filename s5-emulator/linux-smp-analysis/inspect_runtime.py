#!/usr/bin/env python3
"""Decode original Linux printk and verified task fields from an SMP RAM dump.

All reads are offline. Stack return values are explicitly not full backtraces.
"""
import argparse
import bisect
import hashlib
import json
import mmap
from pathlib import Path
import struct
import sys

HERE = Path(__file__).resolve().parent
EMU = HERE.parent
sys.path.insert(0, str(EMU / "analysis"))
from read_kmsg import read_kmsg


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot", type=Path, default=EMU / "menu-smp/results/linux-peer-loader")
    p.add_argument("--output", type=Path, default=HERE)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    result = json.loads((a.snapshot / "result.json").read_text())
    symbols = sorted((int(line.split()[0],16), line.split()[2]) for line in
                     (EMU / "analysis/System.map").read_text().splitlines())
    addresses = [x[0] for x in symbols]

    def symbol(address):
        if not 0xc0008000 <= address < 0xc0467148:
            return None
        pos = bisect.bisect_right(addresses,address)-1
        return symbols[pos][1] + "+" + hex(address-symbols[pos][0])

    kernel = (EMU/"analysis/kernel.raw").read_bytes()
    assert hashlib.sha256(kernel).hexdigest() == "84f99f8b9d4200a30fe2968e8a3fa29aea6d00aa90c153929e399208d1d3048a"
    with (a.snapshot / "ram.bin").open("rb") as f:
        ram = mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ)

        def read(va,n):
            if not 0xc0000000 <= va < va+n <= 0xc2f00000:
                raise ValueError("Task pointer is outside verified Linux linear RAM")
            # VA - 3fe00000 = high PA; high PA - 80000000 = dump offset.
            return ram[va-0xbfe00000:va-0xbfe00000+n]

        def word(va):
            return struct.unpack("<I",read(va,4))[0]

        log = read_kmsg(ram,base=0x80000000)
        cpu3 = result["final_registers"][3]
        thread_info = int(cpu3["r13"],16) & ~0x1fff
        start = word(thread_info+0xc)
        task = start
        seen = set()
        tasks = []
        while task not in seen:
            if len(seen) >= 512:
                raise ValueError("Too many processes; unsupported snapshot or loop corruption")
            seen.add(task)
            comm = read(task+0x2d8,16).split(b"\0")[0].decode("ascii")
            stack = word(task+4)
            saved_sp = word(stack+0x3c)
            saved_fp = word(stack+0x38)
            saved_pc = word(stack+0x40)
            caller = word(saved_fp) if stack+0x48 <= saved_fp < stack+0x2000 else None
            nxt = word(task+0x17c)
            if word(nxt+4) != task+0x17c:
                raise ValueError("Process list next/prev consistency failed")
            tasks.append({"task_va":hex(task),"pid":word(task+0x1f4),
                          "comm":comm,"state_raw":hex(word(task)),
                          "stack_va":hex(stack),"saved_sp":hex(saved_sp),
                          "saved_pc":hex(saved_pc),"saved_pc_symbol":symbol(saved_pc),
                          "saved_schedule_caller":hex(caller) if caller else None,
                          "saved_schedule_caller_symbol":symbol(caller) if caller else None})
            task = nxt-0x17c
        if task != start:
            raise ValueError("Process list did not close at its starting node")
        helper_table = (a.snapshot/"page-table.bin").read_bytes()
        helper_intact = ram[0x100000:0x100000+len(helper_table)] == helper_table
        dtb_intact = ram[0x22ff000:0x2300000] == (EMU.parent/"s5-v29-offline/unpacked-v29/28_dtb.bin").read_bytes()
        runtime = {
            "scope":"Read-only snapshot evidence; no guest execution or state changes by this script",
            "snapshot":str(a.snapshot.resolve()),
            "result_sha256":hashlib.sha256((a.snapshot/"result.json").read_bytes()).hexdigest(),
            "cpu3":cpu3,"cpu3_pc_symbol":symbol(int(cpu3["r15"],16)),
            "original_dtb_unchanged_in_ram":dtb_intact,
            "rtos_helper_pgd_unchanged_in_ram":helper_intact,
            "process_list_closed_bidirectionally":True,"task_count":len(tasks),"tasks":tasks,
            "field_evidence":{
                "thread_info_task":"c002e628: LDR r0,[thread_info,#0xc]",
                "task_list":"c003755c..7568: next at task+17c, subtract17c",
                "task_name":"c00b7210: task+2d8, copies16 bytes",
                "pid":"c00374fc: LDR r2,[task,#1f4]",
                "stack":"c00374d0: task+4 is thread_info/stack",
                "context":"c0011424..1428 storesr4-r11,sp,lr to thread_info+1c",
                "schedule_caller":"c0346738..6740 savesLR atFP; used only as one saved caller, not an unwound backtrace",
            },
            "milestones":[{"cpu":e.get("cpu"),"pc":e.get("pc"),"label":e.get("label")}
                          for e in result["events"] if e.get("cpu")==3],
            "scope_limits":["Linux user processes existing does not prove all their services/RTOS IPC handshakes completed",
                            "Missing initial console is recorded by the guest; no console/PTY was injected",
                            "Display isolation still delays original task66; this is not an unmodified complete camera boot",
                            "One saved return address is not a full stack unwind; task list contains processes, not all threads"]}
        ram.close()
    (a.output/"linux-peer-runtime.json").write_text(json.dumps(runtime,indent=2)+"\n")
    (a.output/"linux-peer-kmsg.json").write_text(json.dumps(log,ensure_ascii=False,indent=2)+"\n")
    (a.output/"linux-peer-kmsg.txt").write_text("".join(
        "[%12.6f] %s\n" % (r["timestamp_ns"]/1e9,r["text"]) for r in log["records"]))
    print(json.dumps({"task_count":len(tasks),"cpu3_pc":runtime["cpu3_pc_symbol"],
                      "original_dtb_unchanged":dtb_intact,"helper_pgd_unchanged":helper_intact,
                      "printk_records":len(log["records"])},indent=2))


if __name__ == "__main__":
    main()
