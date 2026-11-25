#!/usr/bin/env python3
"""Verify the existing S5 UI experiment and extract its actual test inputs."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import zlib

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT.parent / "s5-v29-offline/lut-firmware-ui-experiment"
SOURCE_SHA = "b2ec4aeec209693b874df8dfdf11fc85b38fa4675b44ec554b7a167f430827e1"
UI_SHA = "5727be9315ee229291783a483090efb88be9fd75fcdc032f19314152738564de"
DUMP_SHA = "7ae165083396d4c9213f7a1c019ac56823d0ed4196fa14b8eb25e553a45a21e4"


def sha(b):
    return hashlib.sha256(b).hexdigest()


def prepare(output):
    output.mkdir(parents=True, exist_ok=False)
    source = (ROOT.parent / "s5-v29-offline/S5___V29.bin").read_bytes()
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    packed = (ASSETS / manifest["output"]).read_bytes()
    if sha(source) != SOURCE_SHA or sha(packed) != UI_SHA:
        raise ValueError("Unexpected source or UI experiment image identity")
    if zlib.crc32(packed[0x200:]) & 0xffffffff != struct.unpack_from("<I", packed, 0x40)[0]:
        raise ValueError("Experiment outer CRC mismatch")
    dump_path = Path.home() / "相机功能验证/data/program_0x86280000.bin"
    if sha(dump_path.read_bytes()) != DUMP_SHA:
        raise ValueError("Unexpected main-program dump identity")
    changed = []
    restored = bytearray(packed)
    restored[0x40:0x44] = source[0x40:0x44]
    key_tree = ast.parse((Path.home()/"相机功能验证/s5_decrypt.py").read_text())
    key = next(ast.literal_eval(n.value) for n in key_tree.body
               if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "KEY" for t in n.targets))
    tool = Path.home()/"相机功能验证/panasonic-aes5"
    for index in range(48):
        entry = 0x2ec+index*0x5c
        name = source[entry:entry+12].rstrip(b"\0").decode()
        offset, size = struct.unpack_from("<II", source, entry+12)
        offset += 0x200
        if source[offset:offset+size] == packed[offset:offset+size]:
            continue
        if name not in ("postboot3", "osdover"):
            raise ValueError("Unexpected changed component: "+name)
        filename = "postboot3-lut-experiment.bin" if name == "postboot3" else "osdover-ui-experiment.bin"
        encrypted = output/(name+".encrypted.bin")
        plain = output/filename
        encrypted.write_bytes(packed[offset:offset+size])
        proc = subprocess.run([str(tool), str(encrypted), str(plain), key,
                               packed[entry+0x3c:entry+0x4c].hex(), "0", str(size)],
                              capture_output=True)
        if proc.returncode:
            raise RuntimeError("Local decoder failed for "+name+"; return code "+str(proc.returncode))
        data = plain.read_bytes()
        if len(data) != size or sha(data) != packed[entry+0x1c:entry+0x3c].hex():
            raise ValueError("Decoded component does not match package digest: "+name)
        if data != (ASSETS/filename).read_bytes():
            raise ValueError("Decoded package differs from prior sidecar: "+name)
        changed.append({"name": name, "sha256": sha(data), "bytes": size,
                        "decoded_from_actual_package": True, "sidecar_exact_match": True})
        restored[offset:offset+size] = source[offset:offset+size]
        restored[entry+0x1c:entry+0x3c] = source[entry+0x1c:entry+0x3c]
        encrypted.unlink()
    if restored != source or {c["name"] for c in changed} != {"postboot3", "osdover"}:
        raise ValueError("Package differs outside the two declared components/digests/CRC")
    payload = (ASSETS/"payload.bin").read_bytes()
    component = (output/"postboot3-lut-experiment.bin").read_bytes()
    offset = int(manifest["payload_address"], 16)-0x87707000
    if component[offset:offset+len(payload)] != payload:
        raise ValueError("Payload differs from decrypted package bytes")
    (output/"payload.bin").write_bytes(payload)
    osd = (output/"osdover-ui-experiment.bin").read_bytes()
    labels = []
    for label in manifest["menu_text_changes"]:
        pos = int(label["offset"], 16)
        width = len(label["original"].encode("utf-16le"))
        actual = osd[pos:pos+width].decode("utf-16le").rstrip(" ")
        if actual != label["replacement"] or osd[pos+width:pos+width+2] != b"\0\0":
            raise ValueError("Menu resource text or terminator mismatch")
        labels.append(actual)
    for name in ("manifest.json", "emulate.py"):
        shutil.copy2(ASSETS/name, output/name)
    report = {"package": str(ASSETS/manifest["output"]), "package_sha256": UI_SHA,
              "original_sha256": SOURCE_SHA, "outer_crc_verified": True,
              "changed_components": changed, "unchanged_components": 46,
              "only_declared_package_ranges_changed": True, "menu_labels_verified": labels,
              "dump": str(dump_path), "dump_sha256": DUMP_SHA,
              "payload_sha256": sha(payload), "test_inputs_from_actual_package": True,
              "scope": "Offline package verification, not camera installation or complete system boot"}
    (output/"package-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(prepare(a.output.resolve()), ensure_ascii=False, indent=2))
