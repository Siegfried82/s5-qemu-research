#!/usr/bin/env python3
"""Bounded original loader3 GIC routing initialization, not full firmware boot."""
import argparse, hashlib, json, signal, struct, sys
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent))
from probe.qemu_arm import QemuArm

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,default=HERE/'results-loader-gic');a=ap.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    source=HERE.parents[1]/'s5-v29-offline/unpacked-v29/02_loader3.bin';raw=source.read_bytes()
    report={'scope':'Original loader3 GIC routes/config function executed alone, no program patches, no IRQ delivery/full boot claim','source':str(source),'source_sha256':hashlib.sha256(raw).hexdigest(),'function_address':'0x10e8','source_loaded_at':'0x0','synthetic_components':['guest LDRB readback helper at 0x00200000; parked-core loop at 0x00201000','initial registers and scratch SP 0x00210000','return-address code breakpoint'],'uses_data_watchpoints':False}
    signal.signal(signal.SIGALRM,lambda *_:(_ for _ in ()).throw(TimeoutError('Outer probe deadline')));signal.alarm(20)
    try:
        with QemuArm(HERE.parent/'toolchain/build/qemu-system-arm',a.output/'qemu',machine='s5-rtos',smp=4,ram='512M',loads=[(source,0)],address_ranges=[(0,0x20000000),(0x70000000,0x10000000)]) as q:
            # Read MMIO through real guest LDRB, not the GDB memory reader (unsafe for GIC).
            words=[];fixups=[]
            def lit(reg,value):fixups.append((len(words),reg,value));words.append(0)
            for source_address,dest_address,count in [(0x70001820,0x220000,416),(0x70001c08,0x2201a0,104)]:
                lit(0,source_address);lit(1,dest_address)
                words.append(0xe3002000|((count&0xf000)<<4)|(count&0xfff))
                words.extend([0xe4d03001,0xe4c13001,0xe2522001,0x1afffffb])
            return_address=0x200000+len(words)*4
            words.append(0xeafffffe)
            for index,reg,value in fixups:
                literal_address=0x200000+len(words)*4
                words[index]=0xe59f0000|(reg<<12)|(literal_address-(0x200000+index*4+8))
                words.append(value)
            q.write_mem(0x200000,struct.pack('<%dI'%len(words),*words))
            q.write_mem(0x201000,struct.pack('<I',0xeafffffe))
            for cpu in range(4):q.select_cpu(cpu);q.write_reg('cpsr',0x1d3);q.write_reg('pc',0x201000)
            q.select_cpu(0);q.write_reg('cpsr',0x1f3);q.write_reg('pc',0x10e8);q.write_reg('sp',0x210000);q.write_reg('lr',0x200000)
            expected_targets=bytes(raw[0x6978+i*4+2] for i in range(416))
            expected_config=bytes(sum(raw[0x6978+(i+j)*4+3]<<(2*j) for j in range(4)) for i in range(0,416,4))
            
            q.breakpoint_add(return_address,4);report['stop_reply']=q.resume_all(timeout=2)
            report['return_pc']=hex(q.read_reg('pc'));report['return_sp']=hex(q.read_reg('sp'))
            actual_targets=q.read_mem(0x220000,416);actual_config=q.read_mem(0x2201a0,104)
            report['expected_targets']=expected_targets.hex();report['actual_targets']=actual_targets.hex();report['expected_config']=expected_config.hex();report['actual_config']=actual_config.hex()
            report['targets_match']=actual_targets==expected_targets;report['config_match']=actual_config==expected_config
            report['passed']=report['return_pc']==hex(return_address) and report['return_sp']=='0x210000' and report['targets_match'] and report['config_match']
        report['qemu_exit_code']=q.process.returncode
    finally:signal.alarm(0)
    (a.output/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({k:v for k,v in report.items() if not k.endswith(('targets','config'))},indent=2))
    return 0 if report['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
