#!/usr/bin/env python3
"""Read-only original render-queue / LCD callback / component-ready evidence."""
import argparse, hashlib, json, mmap, re, struct
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--snapshot',type=Path,default=ROOT/'menu-smp/results/local-frame-paced-menu-timeout10')
p.add_argument('--output',type=Path,default=ROOT/'display/render-wait-analysis')
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
original=ROOT.parent/'s5-v29-offline/unpacked-v29'
segments=[(0x6280000,original/'03_program.bin'),(0x7267000,original/'05_postboot1.bin')]
blobs=[(base,path.read_bytes())for base,path in segments]
def rd(addr,size):
 for base,data in blobs:
  if base<=addr and addr+size<=base+len(data):return data[addr-base:addr-base+size]
 raise ValueError(hex(addr))
boot=json.loads((a.snapshot/'result.json').read_text())
with (a.snapshot/'ram.bin').open('rb')as f:
 m=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ);u=lambda addr:struct.unpack_from('<I',m,addr)[0]
 def queue(i):
  q=u(0x8e1b94c+(i-1)*4)
  return dict(id=hex(i),object=hex(q),capacity=u(q+4),array=hex(u(q+12)),write=u(q+16),read=u(q+20),count=u(q+24))
 def task(t):
  sp=u(t+8);end=u(t+16)
  return dict(tcb=hex(t),id=u(t+0xc0),entry=hex(u(t+0xd8)),runs=u(t+4),state=u(t+0x30),sp=hex(sp),wait_type=u(t+0x100),wait_id=hex(u(t+0x104)),saved_code_words=[{'address':hex(x),'value':hex(u(x))}for x in range(sp,min(end+1,sp+0x300)-3,4)if 0x6280000<=u(x)<0x76e7000])
 eid=u(0x6f498d0);eobj=u(0x8e18d80+(eid-1)*4)
 observed=dict(tasks=[task(t)for t in (0x8dffe14,0x8e136b0,0x8e13d04,0x8e15000)],queues=[queue(i)for i in(15,0x101,0x115)],
  event86=dict(id=eid,object=hex(eobj),flags=u(eobj+8)),gui_callback=hex(u(0x8a9d610)),layout_work_scene=hex(struct.unpack_from('<H',m,0xb392e98)[0]),
  producer_scene=hex(struct.unpack_from('<H',m,0x8cb10d0)[0]),layout_message=[hex(u(0xb3b2868+x))for x in range(0,20,4)],
  producer_state=m[0x8aa1168:0x8aa11a8].hex(),readiness_override=m[0x8aa1146],lcd_state=u(0x8ab488c),avio=u(0x8a9c7f8),
  selector=m[0x8ab1bba],draws=boot.get('graphics_draws'),swaps=boot.get('graphics_swaps'))
trace=a.snapshot/'qemu/qemu-trace.log'
lines=[{'line':n,'text':s}for n,s in enumerate(trace.read_text().splitlines(),1)if re.search(r'addr=0x7c015(?:1f[48c]|200)\b',s)]
report=dict(scope='Read-only existing snapshot and original opcodes. No new QEMU or guest modification.',snapshot=str(a.snapshot.resolve()),
 originals=[dict(path=str(path),base=hex(base),sha256=hashlib.sha256(data).hexdigest())for (base,path),(_,data)in zip(segments,blobs)],observed=observed,
 lcd_chain=['232 ->6497338 ->649694e ->648b728 ->647ca76 ->647e796 ->64bcfe4(queue15,word3)','task18 case3 at647e898 ->callback[08a9d604+c]=727cf59','727cf58 queries event86; onlybit0 sends {0,10e,11f438,0,0} toq115 then clearsbit0','727ca4e sends11f439 toq115 then727ca5a sets event86bit0;727ca5e is original request observation boundary'],
 scene_chain=['task285 q115 loop6475dd4 calls6832c1c after actual events','6832cc0 gets original scene66c1958;6832cce writes08cb10c8+8 and marksdirty','68334b0 calls6469ebe(2); nonzero plus6771096()==0 returnswithout q101send','6833506 actualsender67ea590(queue101,reply115,source11d,ee000,payload08cb10c8)','task270727cc9c ->727cac2 copies payload via681475e ->727c896 constructs/draws original layout'],
 missing_registers=dict(addresses=['0x7c0151f4','0x7c0151f8','0x7c0151fc','0x7c015200'],
  read='6680720 copies these four words to caller output',write='6680708 stores caller four words and returns0',
  software_rmw='6469ee8 sets original RTOS event; under668076c(1f) lock reads4words, ORs word2 with savedr1mask, writes4words, unlocks',
  mask2_provenance='6548f6a(group0)setsr9=2;6549440(0)loads/verifiesLOAD/scatters original component;success6549046MOVr1,r9;6549048BL6469ee8',
  consumer='6469ebe(mask) returns0 if word2 containsmask, else8000000e',
  narrower_model='Preserve four software-written32bit values shared across CPUs, reset0; do not setmask2 independently.7c0151f0 reset/togglecommand is outside this narrow contract.',
  trace=lines),
 conclusions=['No current task270 Vsync wait: it waits originalq101 withzeroqueuedmessages; EGLinit task265 exited.',
 'OriginalLCD232 can call GUIcallback butevent86 flags0 currently prevents a GUIrequest; repeatedLCD alone is not established as the cause.',
 'Originalsoftware-written component-completion flags are lost in unmodeledMMIO, making actual scene submission readiness fail. A rerun preserving writes is needed to validate causality.',
 'worksceneffff vs sourcescene1001 is consistent with missed later submission, notproof of memcpyfailure. No menu pixels exist in this snapshot.'])
(a.output/'evidence.json').write_text(json.dumps(report,indent=2)+'\n')
from capstone import Cs,CS_ARCH_ARM,CS_MODE_THUMB
from capstone.arm import ARM_REG_PC
c=Cs(CS_ARCH_ARM,CS_MODE_THUMB);c.detail=True;out=[]
for addr,size in [(0x648b728,0x1e),(0x647ca76,0x22),(0x647e796,0x46),(0x647e898,0x30),(0x727cf58,0x44),(0x727ca10,0x50),(0x6548f6a,0x36),(0x6549022,0x2a),(0x6549440,0x6e),(0x6469ee8,0x30),(0x6680708,0x30),(0x6469ebe,0x2a),(0x68334b0,0x68),(0x6832c1c,0xbc),(0x727cf9c,0x44),(0x727cc9c,0x38)]:
 out.append('\nREGION '+hex(addr))
 for i in c.disasm(rd(addr,size),addr):
  suffix=''
  if i.mnemonic.startswith('ldr')and len(i.operands)>1 and i.operands[1].type==3 and i.operands[1].mem.base==ARM_REG_PC:
   lit=((i.address+4)&~3)+i.operands[1].mem.disp;suffix=f' ; [{lit:08x}]={int.from_bytes(rd(lit,4),"little"):08x}'
  out.append(f'{i.address:08x} {i.bytes.hex():10} {i.mnemonic:8} {i.op_str}{suffix}')
(a.output/'original.disasm.txt').write_text('\n'.join(out)+'\n')
print(json.dumps({'event86':observed['event86'],'source_scene':observed['producer_scene'],'work_scene':observed['layout_work_scene'],'ignored_completion_writes':len(lines),'output':str(a.output)},indent=2))
