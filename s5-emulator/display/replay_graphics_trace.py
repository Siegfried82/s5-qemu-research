#!/usr/bin/env python3
"""Replay captured host-renderer semantic events, without QEMU or new UI data."""
import argparse
import json
from pathlib import Path
from openvg_backend import OpenVGBackend


def replay(source,output):
    rows=[json.loads(line) for line in source.read_text().splitlines()]
    b=OpenVGBackend(output);handles={};pending=None;main=None;observations=[]
    def h(v):return handles[int(v,0) if isinstance(v,str)else v]
    try:
        for row in rows:
            name=row['name'];index=row['index'];drew=False
            if name=='guest_api_entry':pending=row
            elif name=='guest_api_return':
                api=row['api'];args=pending['r0_r3'] if pending else []
                if api=='vgCreatePaint':handles[row['result']]=b.create_paint()
                elif api=='vgSetColor':b.paints[h(args[0])][0x1a01]=[(args[1]>>shift&255)/255 for shift in (24,16,8,0)]
                elif api=='vgSetPaint':b.set_paint(h(args[0])if args[0]else 0,args[1])
            elif name=='create_surface' and row['origin']!='original VGImage client buffer':
                value=b.create_surface(row['width'],row['height'],row['origin']);handles[int(row['handle'],0)]=value
                if main is None:main=value
            elif name=='captured_image':
                handles[int(row['handle'],0)]=b.create_image(Path(row['source_path']).read_bytes(),int(row['format'],0),row['width'],row['height'],row['stride'])
            elif name=='bind_client_image_surface':handles[int(row['surface'],0)]=b.create_image_surface(h(row['image']))
            elif name=='destroy_surface':b.destroy_surface(h(row['surface']))
            elif name=='destroy_image':b.destroy_image(h(row['image']))
            elif name=='make_current':b.make_current(h(row['surface']))
            elif name in ('set_state','vendor_resource_configuration','vendor_yuv_coefficients'):
                b.set_state(int(row['parameter'],0),row['values'])
            elif name=='set_matrix':b.matrices[int(row['mode'],0)]=row['matrix']
            elif name=='paint_parameter':b.set_paint_parameter(h(row['handle']),int(row['parameter'],0),row['values'])
            elif name=='destroy_paint':b.destroy_paint(h(row['handle']))
            elif name=='clear':b.clear(*row['rect']);drew=True
            elif name=='draw_captured_path':
                b.draw_path_data(row['segments'],row['coordinates'],row['modes'],row['datatype'],row['scale'],row['bias']);drew=True
            elif name=='draw_captured_image':b.draw_image(h(row['handle']),*row['source_rect']);drew=True
            elif name=='software_swap':b.swap(h(row['surface']))
            if drew and main is not None:
                image=b.surfaces[main].image()
                region=image.crop((160,250,800,390))
                observations.append(dict(index=index,operation=name,surface=b.surface,
                    center_alpha_nonzero=sum(p[3]>0 for p in region.getdata()),
                    center_bright=sum(p[0]>100 and p[3]>100 for p in region.getdata())))
        (output/'replay-observations.json').write_text(json.dumps(observations,indent=2)+'\n')
        return observations
    finally:b.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('trace',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();observed=replay(args.trace,args.output)
    print(json.dumps(observed,indent=2))
