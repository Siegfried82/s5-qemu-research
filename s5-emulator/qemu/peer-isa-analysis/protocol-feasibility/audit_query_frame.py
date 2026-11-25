#!/usr/bin/env python3
"""Audit named eglQueryFrameDMP and its internal frame-mode companion."""
import hashlib
import json
import struct

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from audit_ui_service import HERE, EMU, PROGRAM, BASE, EXPECTED_SHA


def main():
    p = PROGRAM.read_bytes()
    assert hashlib.sha256(p).hexdigest() == EXPECTED_SHA
    post = (PROGRAM.parent/'05_postboot1.bin').read_bytes()
    blobs = [(BASE, p), (0x7267000, post)]
    def rd(a, n):
        for b, d in blobs:
            if b <= a and a+n <= b+len(d):
                return d[a-b:a-b+n]
        raise ValueError(hex(a))
    def tbb(a,n):
        return {i:hex(a+2*x) for i,x in enumerate(rd(a,n))}
    ranges = [
        ('original internal frame-mode setter',0x64C42B8,0x64C4362),
        ('named eglQueryFrameDMP',0x64C4362,0x64C4404),
        ('surface mode/latch get/set/read-clear leaves',0x64DAECC,0x64DAF0A),
        ('job dispatch prefix before TBB data',0x64E066C,0x64E0688),
        ('job dispatch case1/8/10 and remaining consumers',0x64E0696,0x64E0728),
        ('GPU wait dispatch prefix',0x64E5E50,0x64E5E64),
        ('GPU wait flag-setting path',0x64E5E70,0x64E5ECA),
        ('GPU read wrappers and status interpretation',0x64C0C82,0x64C0CCA),
        ('text first query',0x7296C32,0x7296C72),
        ('text second query and quality fallback',0x7296DCE,0x7296E70),
    ]
    c = Cs(CS_ARCH_ARM,CS_MODE_THUMB);c.detail=True
    lines = ['Original program SHA256: '+EXPECTED_SHA,
             'Static named-export/API and original caller evidence. No guest mutation.',
             'TBB bytes are separately reported as data, not disassembled.']
    for label,a,b in ranges:
        lines.append(f'\n{label}: {a:#x}..{b:#x}')
        for i in c.disasm(rd(a,b-a),a):
            note=''
            if i.mnemonic.startswith('ldr') and '[pc,' in i.op_str:
                lit=((i.address+4)&~3)+i.operands[-1].mem.disp
                note=f' ; [{lit:#x}]={int.from_bytes(rd(lit,4),"little"):#x}'
            lines.append(f'{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:10} {i.op_str}{note}')
    (HERE/'query-frame-disassembly.txt').write_text('\n'.join(lines)+'\n')
    snapshot=EMU/'menu-smp/results/local-paint-lifecycle/result.json'
    j=json.loads(snapshot.read_text());cpu=j['graphics_error']['cpu'];regs=j['final_registers'][cpu]
    assert regs['r15']=='0x64c4362' and regs['r14']=='0x7296c67'
    assert regs['r1']=='0x3fff'
    report={
        'scope':'Read-only original API contract, not a successful runtime implementation',
        'program_sha256':EXPECTED_SHA,'postboot1_sha256':hashlib.sha256(post).hexdigest(),
        'snapshot':str(snapshot),'observed_cpu':cpu,'observed_registers':regs,
        'named_api':{'name':'eglQueryFrameDMP','entry_thumb':'0x064c4363',
            'export_table':'0x070d59f4/name,0x070d59f8/function',
            'arguments':['r0=EGLDisplay','r1=attribute','r2=output_u32_pointer'],
            'attribute':'0x3fff only', 'output_bytes':4,
            'success_return':1,'failure_return':0,
            'operation':'Select current API draw surface;64daee2 reads its word+a0 and clears that word to0; store prior value at output pointer.',
            'not_a_buffer_descriptor':True,
            'no_query_side_effects':['No pixel-buffer address publication','No swap/present','No allocation','No automatic dirty/completion bit'],
            'validation_errors':{'display_not1_or2':'3008','display_uninitialized':'3001','null_output':'300c','no_API_context':'3006','no_draw_surface':'300d','attribute_not3fff':'3004'},
            'success_EGL_error':'3000',
            'host_mapping_note':'Software display/context/surface handles must be validated by their explicit HLE mapping; original native display integers1/2 cannot consume a software handle directly.'},
        'surface_latch':{'offset':'0xa0','query_read_clear':'0x064daee2','peek':'0x064daef2','set_one':'0x064daefe',
            'identified_setter_call':'0x064e5eac',
            'setting_condition':'GPU wait command kinds1/8/10: after completion indication, readGPU_BASE+58; bit26 set triggers writeGPU_BASE+1214=01000000, readGPU_BASE+1200, pollbit26clear, then set surface latch1.',
            'meaning':'Observed GPU exceptional-condition flag used for lower-quality redraw; exact physical register-bit name and resource cause remain unproven.',
            'software_boundary':'A synchronous software renderer need not invent the original GPU exception. Record an initially zero per-surface latch and read-clear it; any unsupported/failed software operation must still stop or report its actual error rather than being converted into query success.'},
        'internal_mode_control':{'entry_thumb':'0x064c42b9','exported_name':'not established',
            'arguments':['r0=display','r1=mode0or3'],'return':'EGLBoolean',
            'storage':'64daed8 writes mode into current drawsurface+94',
            'getter':'64daecc; direct caller64e06b0',
            'consumer':'64e066c command kinds1/8/10: if peek_latch==1 AND mode==3, branch64e070a, skipping current GPU submission but retaining later completion callback path. With latch0 these two modes use the same submit path.',
            'not_general_noop':True,
            'limits':'Other command kinds do not use this gate. Do not indiscriminately skip every graphics API if the latch is set. The setter also passes through native VG preparation64d2e7c; software synchronous execution must retain its own ordering.'},
        'job_tbb':tbb(0x64E0688,13),'wait_tbb':tbb(0x64E5E64,12),
        'actual_caller_sequence':[
            '7296c56 internalmode(display,3)',
            '7296c62 eglQueryFrameDMP(display,3fff,sp+130), clearing prior latch',
            'Original text draw follows;7296dd2 vgFinish',
            '7296dde second query;7296de2 loads output word',
            'If nonzero: preserve parameter1103, set it to1201, re-run original text drawing, restore parameter1103',
            '7296e4e internalmode(display,0)'],
        'enum_correction':'Parameter1103 is VG_RENDERING_QUALITY; value1201 is FASTER. This is not blend-mode modification.',
        'validation_level':'Exact original code and actual stop ABI; no live execution of these functions by this audit',
    }
    (HERE/'query-frame-contract.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'source_verified':True,'output_bytes':4,'read_clear':True,
                      'guest_executed':False,'setter_call':'0x064e5eac'},indent=2))


if __name__=='__main__':
    main()
