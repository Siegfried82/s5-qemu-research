#!/usr/bin/env python3
"""Bounded offline probes of original S5 Linux startup programs.

The shell wrappers are newly written research commands. Programs and scripts
inside the guest are from the verified original S5 V2.9 rootfs images.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent
EMULATOR = HERE.parent

PROBES = {
    "startup-inventory": {
        "seconds": 5,
        "purpose": "Read original startup scripts and original executable/menu inventories; do not launch services.",
        "command": """for f in /etc/inittab /etc/modules /etc/init.d/rcS /etc/init.d/S02-mount /etc/init.d/S03-modules /etc/init.d/S10-ifup; do echo "--- $f"; /bin/cat "$f"; done; for d in /etc/init.d /etc/menuxml /usr/local/bin /usr/bin /sbin /mnt/rfs2/usr/local/bin; do echo "--- $d"; /bin/ls -l "$d"; done; :""",
    },
    "pvc-inventory": {
        "seconds": 5,
        "purpose": "Read original Panasonic service directory, startup helpers, and the beginning of the original menu XML.",
        "command": """for d in /usr/pvc /usr/pvc/bin /usr/pvc/lib /usr/pvc/etc /mnt/rfs2/usr/pvc/bin /etc/www; do echo "--- $d"; /bin/ls -l "$d"; done; for f in /etc/fstab /etc/init.d/netstart /etc/init.d/mediastart /etc/init.d/usbdevstart /etc/init.d/netdevstart; do echo "--- $f"; /bin/cat "$f"; done; echo '--- XML first 20 lines'; n=0; while IFS= read -r line; do printf '%s\\n' "$line"; n=$((n+1)); [ "$n" -ge 20 ] && break; done < /etc/menuxml/MC801_allmenu.xml; :""",
    },
    "nwa-direct": {
        "seconds": 5,
        "purpose": "Execute the original /usr/pvc/bin/nwa service in its stock working directory with its stock library path; no driver substitutions.",
        "command": """export LD_LIBRARY_PATH=/usr/pvc/lib; cd /usr/pvc/bin || exit; echo 'ORIGINAL_PROGRAM /usr/pvc/bin/nwa'; exec ./nwa""",
    },
    "nwa-strace": {
        "seconds": 5,
        "purpose": "Execute original nwa under the original strace to capture its first real missing dependency.",
        "command": """export LD_LIBRARY_PATH=/usr/pvc/lib; cd /usr/pvc/bin || exit; echo 'ORIGINAL_PROGRAM nwa observed by ORIGINAL_PROGRAM strace'; exec /usr/bin/strace -f -s 80 -e trace=file,ioctl,process,mmap2 ./nwa""",
    },
    "nwa-with-stock-modules": {
        "seconds": 7,
        "purpose": "Load original ICC/ICSM modules, then execute original nwa under original strace; record module and device failures without substituting a driver.",
        "command": """echo 'ORIGINAL_MODULE cpu_iccdrv'; /sbin/modprobe cpu_iccdrv; echo "ICC_MODPROBE_EXIT=$?"; echo 'ORIGINAL_MODULE cpu_icsmdrv'; /sbin/modprobe cpu_icsmdrv; echo "ICSM_MODPROBE_EXIT=$?"; /bin/cat /proc/modules; export LD_LIBRARY_PATH=/usr/pvc/lib; cd /usr/pvc/bin || exit; exec /usr/bin/strace -f -s 80 -e trace=file,ioctl,process,mmap2 ./nwa""",
    },
    "stock-boot-20s": {
        "mode": "boot",
        "seconds": 20,
        "purpose": "Run the original kernel and original /sbin/init startup for 20 seconds, preserving RAM to distinguish a later boot stage from an unchanged wait.",
    },
    "stock-boot-5s": {
        "mode": "boot",
        "seconds": 5,
        "purpose": "Run original Linux /sbin/init for 5 seconds with the same current model, for comparison with the 20-second boot.",
    },
    "stock-rcs-processes": {
        "seconds": 8,
        "purpose": "Execute original rcS startup scripts under original Bash, then inspect actual guest processes; PID1 remains the explicitly synthetic command harness.",
        "command": """echo 'ORIGINAL_SCRIPT /etc/init.d/rcS'; /bin/bash -x /etc/init.d/rcS; echo "RCS_EXIT=$?"; echo '--- live processes'; /bin/ps -eo pid,ppid,stat,comm,args; echo '--- module state'; /bin/cat /proc/modules; echo '--- uptime'; /bin/cat /proc/uptime""",
    },
    "menu-xml-export": {
        "seconds": 7,
        "purpose": "Copy original S5 protocol menu XML byte-for-byte through the guest output channel; this is not the on-camera GUI.",
        "command": "/bin/cat /etc/menuxml/MC801_allmenu.xml",
        "append": "root=/dev/mtdblock0 slram=slram0,0x83100000,+0x00300000,slram1,0x83400000,+0x00A00000 console=ttyAMA,115200 lpj=6213632 log_buf_len=2097152",
    },
    "menu-and-program-hashes": {
        "seconds": 5,
        "purpose": "Compute SHA-256 inside original guest userland for exported protocol data and original service binaries.",
        "command": "/usr/bin/openssl dgst -sha256 /etc/menuxml/MC801_allmenu.xml /usr/pvc/bin/nwa /usr/pvc/lib/libicc.so",
    },
    "nwa-binary-export": {
        "seconds": 5,
        "purpose": "Copy original nwa executable bytes for narrowly scoped dependency analysis; no program modification.",
        "command": "/bin/cat /usr/pvc/bin/nwa",
    },
    "libicc-binary-export": {
        "seconds": 5,
        "purpose": "Copy original libicc.so bytes to identify the ioctl immediately preceding nwa exit; no library modification.",
        "command": "/bin/cat /usr/pvc/lib/libicc.so",
    },
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("probe", choices=sorted(PROBES))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    spec = PROBES[args.probe]
    output = (args.output or HERE / "results" / (args.probe+"-"+uuid.uuid4().hex[:8])).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        parser.error("Output directory must not already exist")
    record = {
        "probe": args.probe,
        "purpose": spec["purpose"],
        "wrapper": "new research shell command" if spec.get("mode", "run") == "run" else "QEMU reconstruction boot shim; original Linux /sbin/init",
        "guest_programs": "original S5 V2.9 binaries/scripts from read-only verified rootfs",
        "camera_connection": False,
        "network_device": False,
        "command": spec.get("command"),
        "seconds": spec["seconds"],
        "bootargs_override": spec.get("append"),
    }
    command = [sys.executable, str(EMULATOR / "s5.py"), spec.get("mode", "run"), "--seconds", str(spec["seconds"]),
               "--output", str(output)]
    if spec.get("mode", "run") == "run":
        command += ["--command", spec["command"]]
    else:
        command += ["--keep-ram"]
    if spec.get("append"):
        command += ["--append", spec["append"]]
    completed = subprocess.run(command, cwd=str(EMULATOR), capture_output=True)
    if output.exists():
        (output / "probe.json").write_text(json.dumps(record, ensure_ascii=False, indent=2)+"\n")
        (output / "runner-stdout.log").write_bytes(completed.stdout)
        (output / "runner-stderr.log").write_bytes(completed.stderr)
        print("Evidence:", output)
        if (output / "result.json").exists():
            print((output / "result.json").read_text())
    if completed.returncode and completed.stderr:
        print(completed.stderr.decode("utf-8", "replace"), file=sys.stderr)
    sys.exit(completed.returncode)


if __name__ == "__main__":
    main()
