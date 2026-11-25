#!/usr/bin/env python3
"""Replay the captured first gradient warm-up draw, not a menu or live swap."""
import argparse
import hashlib
import json
from pathlib import Path
from openvg_backend import OpenVGBackend


def main():
    here=Path(__file__).resolve().parent
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace",type=Path,default=here.parent/"menu-smp/results/isolated-display-vendor-state/graphics/calls.jsonl")
    parser.add_argument("--output",type=Path,default=here/"gradient-replay")
    args=parser.parse_args()
    raw=args.trace.read_bytes();events=[json.loads(row) for row in raw.splitlines()]
    b=OpenVGBackend(args.output);paints={};paths={};last_call=None;target=None
    def paint(original):
        if original==0:return 0
        if original not in paints:paints[original]=b.create_paint()
        return paints[original]
    for event in events:
        name=event["name"]
        if name=="create_surface":
            surface=b.create_surface(event["width"],event["height"],"captured original native window dimensions")
            b.make_current(surface)
        elif name=="paint_parameter":
            b.set_paint_parameter(paint(int(event["handle"],16)),int(event["parameter"],16),event["values"])
        elif name=="set_state":b.set_state(int(event["parameter"],16),event["values"])
        elif name=="set_matrix":b.matrices[int(event["mode"],16)]=event["matrix"]
        elif name=="guest_api_entry":
            last_call=event
            if event["api"]=="vgSetPaint":b.set_paint(paint(event["r0_r3"][0]),event["r0_r3"][1])
        elif name=="draw_captured_path":
            if last_call is None or last_call["api"]!="vgDrawPath":raise ValueError("Unexpected trace geometry context")
            paths[last_call["r0_r3"][0]]=event
            b.draw_path_data(event["segments"],event["coordinates"],event["modes"],event["datatype"],event["scale"],event["bias"])
        elif name=="unsupported_or_invalid_call":
            if event["api"]!="vgDrawPath" or "Gradient" not in event["error"]:
                raise ValueError("Trace did not stop at the expected first gradient draw")
            target=event;shape=paths[event["r0_r3"][0]]
            b.draw_path_data(shape["segments"],shape["coordinates"],event["r0_r3"][1],shape["datatype"],shape["scale"],shape["bias"])
            break
    if target is None:raise ValueError("No captured failed gradient draw")
    frame=b.surfaces[b.surface].image();output=args.output/"replayed-warmup.png";frame.save(output)
    pixels=[frame.getpixel((x,frame.height-1-y)) for x in range(4) for y in range(4)]
    gray_levels=sorted({r for r,g,blue,a in pixels if r==g==blue and a})
    if len(gray_levels)<2:raise AssertionError("Captured gradient collapsed to a solid color")
    report={"purpose":"Replay of unchanged original GUI's captured 4x4 initialization warm-up path; not a menu screenshot or live swap",
            "source_trace":str(args.trace.resolve()),"trace_sha256":hashlib.sha256(raw).hexdigest(),
            "target_api":target,"native_dimensions":list(frame.size),"original_path":shape,
            "nonconstant_gray_levels_in_original_4x4_path":gray_levels,"passed":True,"output":str(output.resolve())}
    (args.output/"verification.json").write_text(json.dumps(report,indent=2)+"\n")
    b.close();print(json.dumps({"passed":True,"gray_levels":gray_levels,"output":str(output)},indent=2))


if __name__=="__main__":main()
