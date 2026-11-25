#!/usr/bin/env python3
"""Summarize actual API calls and saved frames; never labels a new frame a menu."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from PIL import Image

HERE=Path(__file__).resolve().parent
EMU=HERE.parent


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inspect(directory):
    result_path=directory/"result.json"
    result=json.loads(result_path.read_text())
    trace_path=directory/"graphics/calls.jsonl"
    trace=[json.loads(line) for line in trace_path.read_text().splitlines() if line]
    entries=[item for item in trace if item.get("name")=="guest_api_entry"]
    frames=[]
    for path in sorted((directory/"graphics").glob("frame-*.png")):
        with Image.open(path) as image:
            rgba=image.convert("RGBA")
            alpha=rgba.getchannel("A")
            frames.append({"name":path.name,"size":list(rgba.size),
                "png_sha256":sha(path.read_bytes()),"rgba_sha256":sha(rgba.tobytes()),
                "nonzero_alpha_pixels":sum(count for value,count in enumerate(alpha.histogram()) if value),
                "nonzero_alpha_bbox":alpha.getbbox(),
                "unique_rgba_colors":len(rgba.getcolors(rgba.width*rgba.height))})
    return {"directory":str(directory.resolve()),"result_sha256":sha(result_path.read_bytes()),
        "trace_sha256":sha(trace_path.read_bytes()),"stop_reason":result.get("stop_reason"),
        "trace_entries":len(trace),"api_entry_count":len(entries),
        "api_sequence":[e["api"] for e in entries],
        "api_counts":dict(Counter(e["api"] for e in entries)),
        "draws":result.get("graphics_draws"),"swaps":result.get("graphics_swaps"),
        "draw_events":[e for e in trace if e.get("name","").startswith("draw_")],
        "clear_events":[e for e in trace if e.get("name")=="clear"],
        "frames":frames,"ui_state":result.get("original_ui_state_at_stop"),
        "unsupported_events":[e for e in trace if "unsupported" in e.get("name","").lower()],
        "last_event":trace[-1] if trace else None}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run",type=Path,default=EMU/"menu-smp/results/local-ui-mode35-2g")
    p.add_argument("--baseline",type=Path,default=EMU/"menu-smp/results/isolated-display-gradient")
    p.add_argument("--output",type=Path,default=HERE/"live-run-analysis/mode35-2g.json")
    a=p.parse_args()
    run=inspect(a.run)
    baseline=inspect(a.baseline)
    known={f["rgba_sha256"] for f in baseline["frames"]}
    all_known=bool(run["frames"]) and all(f["rgba_sha256"] in known for f in run["frames"])
    output={"scope":"Read-only actual guest API trace and generated pixel comparison; no guest execution",
        "run":run,"baseline":baseline,
        "api_sequence_identical":run["api_sequence"]==baseline["api_sequence"],
        "all_saved_frames_pixel_identical_to_baseline":all_known,
        "interpretation":("Only the same previously verified initialization warmup/clear pixels; no new menu image"
                          if all_known else "Pixel output differs; manual trace/visual analysis required before calling it a menu"),
        "backend_modified":False}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(output,indent=2)+"\n")
    print(json.dumps({"report":str(a.output.resolve()),"stop_reason":run["stop_reason"],
        "trace_entries":run["trace_entries"],"draws":run["draws"],"swaps":run["swaps"],
        "same_api_sequence":output["api_sequence_identical"],
        "same_pixels":all_known,"frames":run["frames"]},indent=2))


if __name__=="__main__":
    main()
