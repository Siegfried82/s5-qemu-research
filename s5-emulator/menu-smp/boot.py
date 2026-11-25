#!/usr/bin/env python3
"""Boot original S5 RTOS cores with coherent shared RAM and all resident modules."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from probe.qemu_arm import QemuArm
from qemu.generate_board_setup import code_from_object
from s5 import verify as verify_firmware

COMPONENTS = [("02_loader3.bin",0),("03_program.bin",0x06280000),("05_postboot1.bin",0x07267000),
              ("06_postboot2.bin",0x076e7000),("07_postboot3.bin",0x07707000),
              ("08_postboot4.bin",0x07ee7000),("09_postboot5.bin",0x07f07000),
              ("21_osddata.bin",0x07fe7000)]
POINTS = {0x063b0124:"primary_shared_barrier",0x0642c43c:"serial_transfer_entry",
          0x063c8b26:"late_startup_interrupt_setup",0x0647e83e:"avio_task_entry",
          0x063c8b94:"activate_startup_tasks",0x063c7eb0:"activate_avio_task_18",
          0x06495ab6:"avio_osd_callback_registration",
          0x06759490:"original_timer_sequence_ready_store_completed",
          0x068f9c1c:"original_irq_context_entry",0x065dd080:"original_irq_dispatcher",
          0x07395fcc:"original_egl_initialization",0x07391388:"original_display_layout_initialization"}
ARM_POINTS = {0x068f9c1c}
EXCEPTIONS = {4:"undefined",8:"svc",12:"prefetch_abort",16:"data_abort"}
STORAGE_READ = 0x068609b6
GFX_SOURCE = 0x06495b96
NATIVE_WINDOW = 0x064deeba
PEER_WAIT = 0x065491ee
ORIGINAL_DELAY = 0x066c23e8
DELAY_RETURN = 0x0001bf00


def sha(data):
    return hashlib.sha256(data).hexdigest()


def capture(emu):
    result=[]
    for index in range(4):
        emu.select_cpu(index)
        result.append({k:hex(v) for k,v in emu.registers().items()})
    emu.select_cpu(0)
    return result


class GuestRamView:
    """Bounded read-only slice interface for the existing printk decoder."""
    def __init__(self,emu,base,size):
        self.emu,self.base,self.size=emu,base,size
    def __len__(self):return self.size
    def __getitem__(self,key):
        if not isinstance(key,slice) or key.step not in (None,1):raise ValueError("Only contiguous memory slices are supported")
        start,stop,_=key.indices(self.size)
        return self.emu.read_mem(self.base+start,stop-start)


def fault_registers(emu):
    """Read fault/MMU diagnostics through actual target XML register numbers."""
    target=emu.rsp.read_feature("target.xml")
    number=0;result={}
    for filename in re.findall(r'<xi:include\s+href="([^"]+)"',target):
        for reg in ET.fromstring(emu.rsp.read_feature(filename)).iter("reg"):
            number=int(reg.get("regnum",str(number)),0)
            name=reg.get("name","")
            if any(part in name.upper() for part in ("DFSR","DFAR","IFSR","IFAR","TTBR","TTBCR","SCTLR")):
                raw=emu.rsp.request("p%x"%number)
                value={"number":number,"bits":int(reg.get("bitsize")),"raw_le":raw}
                if re.fullmatch("[0-9a-fA-F]+",raw) and len(raw)%2==0:
                    value["value"]=hex(int.from_bytes(bytes.fromhex(raw),"little"))
                result[name]=value
            number+=1
    return result


def sample_timer_registers(emu):
    """Read MMIO with actual aligned guest LDR instructions, then restore CPU0."""
    addresses=[0x7f020000+i for i in (0,4,8,16,20,32,36,40,48,52)]
    code=[".syntax unified", ".arm", ".text", ".global _start", "_start:", "ldr r1, =0x19000"]
    for index,address in enumerate(addresses):
        code += ["ldr r0, =0x%x"%address, "ldr r2, [r0]", "str r2, [r1, #%d]"%(index*4)]
    code += ["bx lr"]
    with tempfile.TemporaryDirectory(prefix="s5-mmio-sample-") as td:
        asm,obj=Path(td)/"sample.S",Path(td)/"sample.o"
        asm.write_text("\n".join(code)+"\n")
        subprocess.run(["clang","--target=arm-none-eabi","-march=armv7-a","-marm","-c",str(asm),"-o",str(obj)],check=True)
        helper=code_from_object(obj.read_bytes())
    emu.select_cpu(0);saved=emu.registers()
    if saved["cpsr"]&0x20 and emu.read_mem(saved["r15"]-2,2)==bytes.fromhex("30bf"):
        return {"unavailable":"CPU0 is halted after WFI; changing PC does not wake it, so no probe was executed"}
    emu.write_mem(0x18000,helper)
    emu.write_reg("cpsr",0x1d3)
    emu.write_reg("pc",0x18000);emu.write_reg("lr",0x18f00)
    emu.breakpoint_add(0x18f00,kind=4)
    try:
        reply=emu.resume_cpu(0,timeout=1)
        if emu.read_reg("pc")!=0x18f00:raise RuntimeError("MMIO sampler stopped unexpectedly: "+reply)
        values=struct.unpack("<10I",emu.read_mem(0x19000,40))
        return {hex(address):hex(value) for address,value in zip(addresses,values)}
    finally:
        emu.breakpoint_remove(0x18f00,kind=4)
        for name,value in saved.items():emu.write_reg(name,value)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path)
    p.add_argument("--seconds",type=float,default=5)
    p.add_argument("--stop-at",type=lambda v:int(v,0))
    p.add_argument("--keep-ram",action="store_true")
    p.add_argument("--ram",choices=("512M","2G"),default="512M",help="explicit DRAM backing size; 2G supports the original high-end image arena, without claiming measured physical capacity")
    p.add_argument("--storage-hle",action="store_true",help="supply verified package bytes at the original sector-read I/O boundary")
    p.add_argument("--capture-gfx",action="store_true",help="record original AVIO graphics source descriptors")
    p.add_argument("--startup-inputs",choices=("inactive","active"),default="inactive",help="explicit experimental levels for the two observed startup GPIO bit4 inputs")
    p.add_argument("--graphics-hle",action="store_true",help="replace observed EGL/OpenVG driver calls with explicit software rendering; unsupported APIs stop")
    p.add_argument("--stop-after-swaps",type=int,default=0,help="stop after this many actual EGL swaps (0: use time bound); a swap alone does not prove a menu")
    p.add_argument("--key-event",choices=("KEY_CENTER","KEY_UP","KEY_DOWN","KEY_LEFT","KEY_RIGHT","KEY_PLAY","KEY_Q_MENU","KEY_SET"),help="after the first observed swap, send one explicit software key through the original input-task queue")
    p.add_argument("--key-action",type=int,choices=(0,1),default=1,help="software key action: 0 release, 1 press")
    p.add_argument("--key-click",action="store_true",help="after the original press is consumed, send its release through the same original input-task queue")
    p.add_argument("--key-sequence",nargs="+",choices=("KEY_CENTER","KEY_UP","KEY_DOWN","KEY_LEFT","KEY_RIGHT","KEY_PLAY","KEY_Q_MENU","KEY_SET"),help="up to16 explicit clicks; each subsequent press waits for a new actual EGL swap and original input readiness")
    p.add_argument("--spi0-open-bus",action="store_true",help="experimental disconnected SPI0 MISO=0, no success ACK or coprocessor execution")
    p.add_argument("--spi6-open-bus",action="store_true",help="experimental disconnected SPI6 MISO=0 for original display initialization; no panel response invented")
    p.add_argument("--tx-serial-hle",action="store_true",help="explicit TX-only controller completion model for 0x7b193400; captures bytes, no peripheral acknowledgement or image generation")
    p.add_argument("--lens-release-input-high",action="store_true",help="explicit inactive lens-release input: SPI3 register044e bit0 reads high; no lens presence or peer acknowledgement is invented")
    p.add_argument("--adc11-sample",type=lambda v:int(v,0),default=0,help="explicit raw ADC11 environment sample, no calibrated voltage claim (default: 0)")
    p.add_argument("--isolate-display",action="store_true",help="experimental local execution: call original delay API for task66 at its unsupported peer wait; not a full boot")
    p.add_argument("--isolate-display-ticks",type=int,default=60000,help="explicit task66 delay length for local display experiment (1..600000, default 60000)")
    p.add_argument("--linux-peer",action="store_true",help="start CPU3 in the original loader wait; only original RTOS mailbox/SGI may release it into original Linux")
    p.add_argument("--service-gui-once",action="store_true",help="explicit function-level UI experiment: after a 2000-tick yield, let original task66 APIs consume one exact pending GUI mode request; DSP init remains unfinished")
    p.add_argument("--probe-ui-mode35",action="store_true",help="after the validated mode36 service, execute the next exact original mode35 handler as a bounded diagnostic; asynchronous hardware dependencies remain unverified")
    p.add_argument("--ui-service-loop",action="store_true",help="explicit partial execution of the original task66 receive/dispatch loop after mode35; DSP and remaining pre-loop initialization are not completed")
    p.add_argument("--ui-service-postinit",action="store_true",help="with --ui-service-loop, execute the two original post-DSP initialization calls before the loop; the DSP initialization itself remains unfinished")
    p.add_argument("--display-clock-test",action="store_true",help="explicit bounded TEST display status and GIC pulses; not a recovered display timing model")
    p.add_argument("--frame-irq-test-pairs",type=int,default=0,help="explicit 1..128 pairs of image-pipeline IRQ192/193 TEST inputs after original readiness; timing metadata only, no synthetic pixel ABI")
    p.add_argument("--frame-test-stage-seconds",type=float,default=2,help="bounded TEST watchdog for each original ISR/consumer stage (0..10 seconds); does not change guest timeout logic")
    p.add_argument("--lcd-refresh-test-pulses",type=int,default=0,help="up to 1..32 explicit IRQ232 TEST inputs, only after the original GUI sets its refresh event; no automatic frame timing")
    a=p.parse_args()
    if a.key_sequence:
        if len(a.key_sequence)>16:p.error("--key-sequence accepts at most16 clicks")
        if a.key_event or a.key_click or a.key_action!=1:
            p.error("--key-sequence supplies its own presses/releases; do not combine it with --key-event/--key-click/--key-action")
        a.key_event=a.key_sequence[0];a.key_click=True
    if not 0 < a.seconds <= 600:
        p.error("--seconds must be within (0,600]")
    if not 0 <= a.stop_after_swaps <= 100:p.error("--stop-after-swaps must be within 0..100")
    if a.stop_after_swaps and not a.graphics_hle:p.error("--stop-after-swaps requires --graphics-hle")
    if a.key_event and not a.graphics_hle:p.error("--key-event requires --graphics-hle")
    if a.key_click and (not a.key_event or a.key_action!=1):p.error("--key-click requires --key-event and press action1")
    if a.key_event and a.stop_after_swaps==1:p.error("Use at least two swaps or only a time bound when requesting a key event")
    if a.service_gui_once and not a.isolate_display:p.error("--service-gui-once requires --isolate-display")
    if a.probe_ui_mode35 and not a.service_gui_once:p.error("--probe-ui-mode35 requires --service-gui-once")
    if (a.ui_service_loop or a.display_clock_test) and not a.probe_ui_mode35:
        p.error("--ui-service-loop and --display-clock-test require --probe-ui-mode35")
    if a.ui_service_postinit and not a.ui_service_loop:p.error("--ui-service-postinit requires --ui-service-loop")
    if not 0 <= a.frame_irq_test_pairs <= 128:p.error("--frame-irq-test-pairs must be within 0..128")
    if not 0 < a.frame_test_stage_seconds <= 10:p.error("--frame-test-stage-seconds must be within (0,10]")
    if a.frame_irq_test_pairs and not (a.ui_service_postinit and a.display_clock_test):
        p.error("--frame-irq-test-pairs requires --ui-service-postinit and --display-clock-test")
    if not 0 <= a.lcd_refresh_test_pulses <= 32:p.error("--lcd-refresh-test-pulses must be within 0..32")
    if a.lcd_refresh_test_pulses and not a.frame_irq_test_pairs:
        p.error("--lcd-refresh-test-pulses requires --frame-irq-test-pairs")
    if not 0 <= a.adc11_sample <= 0xfff:p.error("--adc11-sample must be within 0..4095")
    if not 1 <= a.isolate_display_ticks <= 600000:p.error("--isolate-display-ticks must be within 1..600000")
    a.output=(a.output or ROOT/"menu-smp/results"/(time.strftime("boot-%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6])).resolve()
    a.output.mkdir(parents=True,exist_ok=False)
    print("正在运行 S5 离线启动实验；客体执行上限 %.0f 秒。结果目录：%s"%(a.seconds,a.output),flush=True)
    ram_bytes=0x80000000 if a.ram=="2G" else 0x20000000
    low_ram_bytes=min(ram_bytes,0x40000000)
    source=ROOT.parent/"s5-v29-offline/unpacked-v29"
    manifest=verify_firmware()
    (a.output/"firmware-verification.json").write_text(json.dumps(manifest,indent=2)+"\n")
    expected={"%02d_%s.bin"%(index,item["name"]):item["sha256"] for index,item in enumerate(manifest["components"])}
    loads=[]; identities=[]
    for name,address in COMPONENTS:
        path=source/name;data=path.read_bytes()
        if sha(data)!=expected[name]:
            raise RuntimeError("Component identity mismatch: "+name)
        loads.append((path,address))
        identities.append({"name":name,"address":hex(address),"bytes":len(data),"sha256":sha(data)})
    with tempfile.TemporaryDirectory(prefix="s5-smp-helper-") as td:
        obj=Path(td)/"helper.o"
        subprocess.run(["clang","--target=arm-none-eabi","-march=armv7-a","-marm","-c",
                        str(Path(__file__).with_name("boot-state.S")),"-o",str(obj)],check=True)
        helper=code_from_object(obj.read_bytes())
    helper_path=a.output/"boot-state.bin";helper_path.write_bytes(helper)
    table=struct.pack("<4096I",*[(index<<20)|(0xc02 if 0x700<=index<0x800 else 0x11c02) for index in range(4096)])
    table_path=a.output/"page-table.bin";table_path.write_bytes(table)
    loads.extend([(helper_path,0x10000),(table_path,0x100000)])
    report={"components":identities,"original_firmware_instructions_modified":False,
            "synthetic_state":["RTOS VFP enabled","RTOS identity Normal-RAM page table",
                               "CPU3 original loader wait, MMU off" if a.linux_peer else "CPU3 parked, no Linux peer"],
            "reconstructed_loader_state":["original loader3 resident bytes at address zero restore the interrupt-vector chain",
                                          "original loader3 function 0x10e8 sets 416 IRQ routes/configurations; original GICD enable and per-core GICC enable/PMR values restored",
                                          "timer1 control writes 0, 6, 0x86, verified against original loader2 and loader3"],
            "startup_inputs":{"profile":a.startup_inputs,"addresses":["0x7f080410","0x7f080094"],"bit":4,
                              "level":1 if a.startup_inputs=="inactive" else 0,"physical_button_names_verified":False},
            "peer_flags_manually_cleared":False,"events":[],"menu_rendered":False}
    report["ram_model"]={"bytes":ram_bytes,"high_base":"0x80000000","low_alias_bytes":low_ram_bytes,
                         "physical_camera_capacity_measured":False,
                         "scope":"Explicit backing model; original image arena constant spans0x0fc00000..0x7ffe0000 and CPU accessor sets bit31"}
    if a.service_gui_once:
        report["synthetic_state"].append("Explicit local UI service: execute one original task66 mode-request handler while its DSP initialization remains unfinished")
    storage = None
    if a.storage_hle:
        from display.storage import StorageMap
        storage=StorageMap()
        (a.output/"storage-map.json").write_text(json.dumps(storage.manifest(),indent=2)+"\n")
        report["storage_model"]="Read-only HLE of original sector I/O; packaged bytes only, missing ranges fail"
    report["storage_reads"]=[]
    report["graphics_sources"]=[]
    qemu=ROOT/"toolchain/build/qemu-system-arm"
    report["qemu_sha256"]=sha(qemu.read_bytes())
    with QemuArm(qemu,a.output/"qemu",machine="s5-rtos",smp=4,ram=a.ram,loads=loads,
                 address_ranges=[(0,low_ram_bytes),(0x70000000,0x10000000),(0x80000000,ram_bytes)],
                 log_mask="unimp,guest_errors") as emu:
        emu.qmp.command("qom-set",{"path":"/machine","property":"inactive-inputs","value":a.startup_inputs=="inactive"})
        if a.spi0_open_bus:
            emu.qmp.command("qom-set",{"path":"/machine","property":"spi0-open-bus","value":True})
            report["spi0_model"]="Explicit disconnected peer experiment: every sampled MISO byte is zero; no success ACK and no uploaded code execution"
        if a.spi6_open_bus:
            emu.qmp.command("qom-set",{"path":"/machine","property":"spi6-open-bus","value":True})
            report["spi6_model"]="Explicit disconnected peer experiment: sampled MISO is zero; no panel reply invented"
        if a.tx_serial_hle:
            emu.qmp.command("qom-set",{"path":"/machine","property":"tx-serial-hle","value":True})
            report["tx_serial_model"]="Explicit TX-sink controller lifecycle; captures original outgoing bytes and clears completion on original ACK; no RX, peripheral acceptance, or pixels"
        if a.lens_release_input_high:
            emu.qmp.command("qom-set",{"path":"/machine/rtc-peer","property":"lens-release-input-high","value":True})
        report["lens_release_input"]={"register":"SPI3 peer044e","bit":0,"forced_high":a.lens_release_input_high,
            "scope":"Explicit external input level interpreted by original firmware as release button inactive; no physical measurement, lens presence, UI flag, or device ACK supplied"}
        if a.adc11_sample:
            emu.qmp.command("qom-set",{"path":"/machine","property":"adc11-sample","value":a.adc11_sample})
        report["adc_environment"]={"channel":11,"raw_sample":a.adc11_sample,"voltage_calibrated":False,"firmware_thresholds_modified":False}
        ids=emu.threads();report["threads"]=ids
        if len(ids)!=4:raise RuntimeError("Expected four QEMU CPU threads")
        emu.write_mem(0x10800,bytes.fromhex("feffffea"))
        for index in range(4):
            emu.select_cpu(index)
            for register in range(16):emu.write_reg(register,0)
            emu.write_reg("cpsr",0x600001d3)
            emu.write_reg("pc",0x10000 if index<3 else (0x10400 if a.linux_peer else 0x10800))
        emu.select_cpu(0)
        points=dict(POINTS)
        arm_points=set(ARM_POINTS)
        if a.linux_peer:
            linux_points={0x330:"original_cpu3_loader_wait_entry",0x80120000:"original_cpu3_zboot_entry",
                          0x82300000:"original_cpu3_zimage_entry",0x80208000:"original_cpu3_decompressed_kernel_entry"}
            points.update(linux_points);arm_points.update(linux_points)
            report["linux_peer_model"]="CPU3 GICC enable/PMR reconstructed; original loader3 wait and actual RTOS SGI2/mailbox release; original Linux images and DTB"
        if a.stop_at is not None:points[a.stop_at]="requested_checkpoint"
        if storage:emu.breakpoint_add(STORAGE_READ)
        if a.capture_gfx:emu.breakpoint_add(GFX_SOURCE)
        graphics=None;window_returns={}
        key_probe=None;key_armed=False;key_release_armed=False
        if a.key_event:
            from key_probe import KeyProbe,KeyProbeError
            key_probe=KeyProbe(emu,consumption_timeout=30 if a.service_gui_once else 5)
        service_probe=None;service_started=False;service_phase=0;service_probes=[]
        if a.service_gui_once:
            from ui_service_probe import UIServiceProbe,UIServiceProbeError
            service_probe=UIServiceProbe(emu)
            service_probes.append(service_probe)
        service_loop=None;display_clock_probe=None;frame_probe=None;frame_probe_started=False
        lcd_refresh_probe=None;lcd_refresh_started=False
        if a.ui_service_loop:
            from ui_service_loop import UIServiceLoopProbe,UIServiceLoopProbeError
            service_loop=(UIServiceLoopProbe(emu,run_postinit=True) if a.ui_service_postinit else UIServiceLoopProbe(emu))
        if a.display_clock_test:
            from display_clock_probe import DisplayClockProbe,DisplayClockProbeError
            display_clock_probe=DisplayClockProbe(emu)
        if a.frame_irq_test_pairs:
            from frame_irq_probe import FrameIRQProbe,FrameIRQProbeError
            frame_probe=FrameIRQProbe(emu,pairs=a.frame_irq_test_pairs,stage_timeout=a.frame_test_stage_seconds)
            frame_probe.report["requested_stage_timeout_seconds"]=a.frame_test_stage_seconds
        if a.lcd_refresh_test_pulses:
            from lcd_refresh_probe import LCDRefreshProbe,LCDRefreshProbeError
            lcd_refresh_probe=LCDRefreshProbe(emu,max_pulses=a.lcd_refresh_test_pulses,stage_timeout=10)
        delayed_context=None
        if a.isolate_display:
            emu.breakpoint_add(PEER_WAIT)
            report["display_isolation"]={"scope":"Local scheduling experiment, not a completed original boot",
                                         "method":"Call original dly_tsk-like API at task66's peer wait; no TCB/event/firmware instruction patch",
                                         "requested_ticks":a.isolate_display_ticks,
                                         "delay_entry":hex(ORIGINAL_DELAY),"calls":[]}
        if a.graphics_hle:
            from display.graphics_hle import GraphicsHLE
            graphics=GraphicsHLE(emu,a.output/"graphics")
            for address in graphics.breakpoints:emu.breakpoint_add(address)
            emu.breakpoint_add(NATIVE_WINDOW)
            report["graphics_model"]="Opt-in software EGL/OpenVG at original API boundaries; no invented menu content; unsupported calls stop"
        for address in points:emu.breakpoint_add(address,kind=4 if address in arm_points else 2)
        for address in EXCEPTIONS:emu.breakpoint_add(address,kind=4)
        report["initial_registers"]=capture(emu)
        def key_input_ready():
            if not graphics or not graphics.backend.swaps:return False
            if service_probe:
                # The key probe observes actual state changes at the input
                # task's loop even when no further graphics calls arrive.
                return (service_probe.completed and
                        (not service_loop or emu.read_mem(0x08aa1103,1)[0]&0xf==0xf))
            return True
        deadline=time.monotonic()+a.seconds
        scripted_keys=a.key_sequence or ([a.key_event] if a.key_event else [])
        key_sequence_index=0;key_sequence_completed=0;key_release_recorded=False;key_next_swap_floor=None
        while time.monotonic()<deadline:
            if key_probe and not key_armed and key_input_ready():
                key_probe.arm(a.key_event,a.key_action,swaps_observed=graphics.backend.swaps,
                              wait_for_ui_active=bool(service_probe),wait_for_mode35_ack=bool(service_loop),wait_for_transition_complete=bool(service_loop),wait_for_input_route_ready=bool(service_loop))
                key_armed=True
            if (a.key_click and key_armed and not key_release_armed and not key_probe.active
                    and key_probe.report["requests"][-1].get("consumed")):
                # Release is valid even if the press changed the active scene.
                key_probe.arm(scripted_keys[key_sequence_index],0,swaps_observed=graphics.backend.swaps)
                key_release_armed=True
            if (a.key_sequence and key_release_armed and not key_release_recorded and not key_probe.active
                    and key_probe.report["requests"][-1].get("consumed")):
                key_release_recorded=True
                key_sequence_completed+=1
                # A new frame may arrive before the original release is
                # consumed. Count from the actual press dispatch, after all
                # readiness waits, rather than from arm/release time.
                key_next_swap_floor=key_probe.report["requests"][-2].get(
                    "egl_swaps_at_original_dispatch",graphics.backend.swaps)
            if (a.key_sequence and key_release_recorded and key_sequence_index+1<len(scripted_keys)
                    and graphics.backend.swaps>key_next_swap_floor):
                key_sequence_index+=1
                key_probe.arm(scripted_keys[key_sequence_index],1,swaps_observed=graphics.backend.swaps,
                              wait_for_input_route_ready=True)
                key_release_armed=False;key_release_recorded=False
            if (frame_probe and not frame_probe_started and display_clock_probe.completed
                    and service_loop.report.get("postinit_672060c_executed_by_helper")
                    and service_loop.report.get("postinit_65b9fcc_executed_by_helper")
                    and emu.read_mem(0x08aa7f19,1)==b"\x01"
                    and emu.read_mem(0x08aac2f9,1)==b"\x01"):
                try:
                    frame_probe.start()
                    frame_probe_started=True
                except FrameIRQProbeError as exc:
                    report["frame_irq_test_error"]=str(exc)
                    report["stop_reason"]="frame_irq_test_stopped"
                    break
            if (lcd_refresh_probe and not lcd_refresh_started and display_clock_probe.completed
                    and frame_probe.completed):
                try:
                    lcd_refresh_probe.start()
                    lcd_refresh_started=True
                except LCDRefreshProbeError as exc:
                    report["lcd_refresh_test_error"]=str(exc)
                    report["stop_reason"]="lcd_refresh_test_stopped"
                    break
            try:
                timeout=max(.001,deadline-time.monotonic())
                if key_probe and key_probe.active:timeout=min(timeout,key_probe.timeout_hint())
                if service_probe and service_probe.active:timeout=min(timeout,service_probe.timeout_hint())
                if display_clock_probe and display_clock_probe.active:timeout=min(timeout,display_clock_probe.timeout_hint())
                if frame_probe and frame_probe.active:timeout=min(timeout,frame_probe.timeout_hint())
                if lcd_refresh_probe and lcd_refresh_probe.active:timeout=min(timeout,lcd_refresh_probe.timeout_hint())
                reply=emu.resume_all(timeout=timeout)
            except TimeoutError:
                if lcd_refresh_probe and lcd_refresh_probe.active:
                    try:lcd_refresh_probe.check_timeout()
                    except LCDRefreshProbeError as exc:
                        report["lcd_refresh_test_error"]=str(exc)
                        report["stop_reason"]="lcd_refresh_test_stopped"
                        break
                if frame_probe and frame_probe.active:
                    try:frame_probe.check_timeout()
                    except FrameIRQProbeError as exc:
                        report["frame_irq_test_error"]=str(exc)
                        report["stop_reason"]="frame_irq_test_stopped"
                        break
                if display_clock_probe and display_clock_probe.active:
                    try:display_clock_probe.check_timeout()
                    except DisplayClockProbeError as exc:
                        report["display_clock_test_error"]=str(exc)
                        report["stop_reason"]="display_clock_test_stopped"
                        break
                if service_probe and service_probe.active:
                    try:service_probe.check_timeout()
                    except UIServiceProbeError as exc:
                        report["ui_service_error"]=str(exc)
                        report["stop_reason"]="original_ui_service_probe_stopped"
                        break
                if key_probe and key_probe.active:
                    try:key_probe.check_timeout()
                    except KeyProbeError as exc:
                        report["key_error"]=str(exc)
                        report["stop_reason"]="software_key_probe_stopped"
                        break
                report["stop_reason"]="time_bound"
                break
            match=re.search(r"thread:([^;]+)",reply)
            index=ids.index(match.group(1)) if match and match.group(1) in ids else 0
            emu.select_cpu(index);pc=emu.read_reg("pc")
            if lcd_refresh_probe:
                try:
                    lcd_refresh_probe.check_timeout()
                    if lcd_refresh_probe.handle(pc,index):continue
                except LCDRefreshProbeError as exc:
                    report["lcd_refresh_test_error"]=str(exc)
                    report["stop_reason"]="lcd_refresh_test_stopped"
                    break
            if frame_probe:
                try:
                    frame_probe.check_timeout()
                    if frame_probe.handle(pc,index):continue
                except FrameIRQProbeError as exc:
                    report["frame_irq_test_error"]=str(exc)
                    report["stop_reason"]="frame_irq_test_stopped"
                    break
            if service_probe:
                try:
                    service_probe.check_timeout()
                    if service_probe.handle(pc,index):
                        if service_probe.completed:
                            if a.probe_ui_mode35 and service_phase==0:
                                service_probe.close()
                                service_probe=UIServiceProbe(emu,command=0x35)
                                service_probes.append(service_probe)
                                service_started=False;service_phase=1
                            if service_probe.command==0x35 and service_probe.completed:
                                if display_clock_probe:
                                    try:display_clock_probe.start()
                                    except DisplayClockProbeError as exc:
                                        report["display_clock_test_error"]=str(exc)
                                        report["stop_reason"]="display_clock_test_stopped"
                                        break
                                if service_loop:
                                    try:service_loop.start(index)
                                    except UIServiceLoopProbeError as exc:
                                        report["ui_service_loop_error"]=str(exc)
                                        report["stop_reason"]="original_ui_service_loop_stopped"
                                        break
                                else:emu.breakpoint_add(PEER_WAIT)
                            else:emu.breakpoint_add(PEER_WAIT)
                            if key_probe and not key_armed and key_input_ready():
                                key_probe.arm(a.key_event,a.key_action,swaps_observed=graphics.backend.swaps,wait_for_ui_active=bool(service_probe),wait_for_mode35_ack=bool(service_loop),wait_for_transition_complete=bool(service_loop),wait_for_input_route_ready=bool(service_loop))
                                key_armed=True
                        continue
                except UIServiceProbeError as exc:
                    report["ui_service_error"]=str(exc)
                    report["stop_reason"]="original_ui_service_probe_stopped"
                    break
            if display_clock_probe:
                try:
                    display_clock_probe.check_timeout()
                    if display_clock_probe.handle(pc,index):continue
                except DisplayClockProbeError as exc:
                    report["display_clock_test_error"]=str(exc)
                    report["stop_reason"]="display_clock_test_stopped"
                    break
            if service_loop:
                try:
                    if service_loop.handle(pc,index):continue
                except UIServiceLoopProbeError as exc:
                    report["ui_service_loop_error"]=str(exc)
                    report["stop_reason"]="original_ui_service_loop_stopped"
                    break
            if key_probe:
                try:
                    key_probe.check_timeout()
                    if key_probe.handle(pc,index):
                        request=key_probe.report["requests"][-1]
                        events=request.get("events",[])
                        if request["action"]==1 and events and events[-1]["kind"]=="original_key_dispatch":
                            request.setdefault("egl_swaps_at_original_dispatch",graphics.backend.swaps)
                        continue
                except KeyProbeError as exc:
                    report["key_error"]=str(exc)
                    report["stop_reason"]="software_key_probe_stopped"
                    break
            if a.isolate_display and pc==PEER_WAIT:
                regs=emu.registers()
                u32=lambda address:struct.unpack("<I",emu.read_mem(address,4))[0]
                tcb=u32(0x20040+index*4)
                guards={hex(address):u32(address) for address in (0x08ab4e80,0x08ab4e7c,0x08ab4e68,0x209c0+index*4)}
                if (regs["cpsr"]&0x3f)!=0x33 or regs["cpsr"]&0x0600fc00 or not tcb or u32(tcb+0xc0)!=66 or any(guards.values()):
                    report["stop_reason"]="display_isolation_guard_rejected"
                    report["display_isolation"]["guard_failure"]={"cpu":index,"tcb":hex(tcb),"registers":regs,"guards":guards}
                    break
                delayed_context={"registers":regs,"tcb":tcb,"cpu_at_call":index}
                delay_ticks=(100 if service_phase else 2000) if service_probe and not service_started else a.isolate_display_ticks
                report["display_isolation"]["calls"].append({"cpu":index,"tcb":hex(tcb),"task_id":66,"pc":hex(pc),"guards":guards,
                                                            "saved_core_registers":{k:hex(v) for k,v in regs.items()},"requested_ticks":delay_ticks,"returned":False})
                emu.breakpoint_remove(PEER_WAIT)
                emu.breakpoint_add(DELAY_RETURN)
                emu.write_reg("r0",delay_ticks);emu.write_reg("lr",DELAY_RETURN|1);emu.write_reg("pc",ORIGINAL_DELAY)
                continue
            if delayed_context and pc==DELAY_RETURN:
                tcb=struct.unpack("<I",emu.read_mem(0x20040+index*4,4))[0]
                if tcb!=delayed_context["tcb"]:raise RuntimeError("Isolated delay returned in an unexpected task context")
                result=emu.read_reg("r0")
                report["display_isolation"]["calls"][-1].update(returned=True,return_value=hex(result),cpu_at_return=index)
                emu.breakpoint_remove(DELAY_RETURN)
                for name,value in delayed_context["registers"].items():emu.write_reg(name,value)
                delayed_context=None
                if result:
                    report["stop_reason"]="original_delay_api_rejected_isolation"
                    break
                if service_probe and not service_started:
                    try:
                        service_probe.start(index)
                        service_started=True
                    except UIServiceProbeError as exc:
                        report["ui_service_error"]=str(exc)
                        report["stop_reason"]="original_ui_service_probe_stopped"
                        break
                continue
            if graphics and pc in graphics.breakpoints:
                try:
                    graphics.handle(pc,emu.registers())
                except (ValueError,RuntimeError,KeyError) as exc:
                    report["graphics_error"]={"pc":hex(pc),"cpu":index,"error":str(exc),"exception":type(exc).__name__}
                    report["stop_reason"]="graphics_unsupported_or_invalid"
                    break
                if key_probe and not key_armed and key_input_ready():
                    key_probe.arm(a.key_event,a.key_action,swaps_observed=graphics.backend.swaps,wait_for_ui_active=bool(service_probe),wait_for_mode35_ack=bool(service_loop),wait_for_transition_complete=bool(service_loop),wait_for_input_route_ready=bool(service_loop))
                    key_armed=True
                if a.stop_after_swaps and graphics.backend.swaps >= a.stop_after_swaps:
                    report["stop_reason"]="requested_egl_swap_count"
                    break
                continue
            if graphics and pc==NATIVE_WINDOW:
                regs=emu.registers();return_pc=regs["r14"]&~1
                call={"cpu":index,"sp":regs["r13"],"arguments":graphics.native_window_arguments(regs)}
                window_returns.setdefault(return_pc,[]).append(call)
                emu.breakpoint_add(return_pc,kind=2 if regs["r14"]&1 else 4)
                emu.breakpoint_remove(NATIVE_WINDOW);emu.step(timeout=1);emu.breakpoint_add(NATIVE_WINDOW)
                continue
            if graphics and pc in window_returns:
                regs=emu.registers();calls=window_returns[pc]
                call=next((item for item in calls if item["cpu"]==index and item["sp"]==regs["r13"]),None)
                if call:
                    if regs["r0"]:graphics.register_native_window(regs["r0"],**call["arguments"])
                    else:graphics.backend.log("native_window_failed",cpu=index,**call["arguments"])
                    calls.remove(call)
                kind=2 if regs["cpsr"]&0x20 else 4
                emu.breakpoint_remove(pc,kind=kind)
                if calls:
                    emu.step(timeout=1);emu.breakpoint_add(pc,kind=kind)
                else:del window_returns[pc]
                continue
            if storage and pc == STORAGE_READ:
                regs=emu.registers()
                address,count,destination=0x40000000+regs["r0"]*512,regs["r1"]*512,regs["r2"]
                access={"cpu":index,"logical_address":hex(address),"destination":hex(destination),"bytes":count,"flag":regs["r3"]}
                try:
                    if count>64*1024*1024:raise ValueError("sector request exceeds bounded model")
                    data=storage.read(address,count)
                    if not (0 <= destination <= low_ram_bytes-count or 0x80000000 <= destination <= 0x80000000+ram_bytes-count):
                        raise ValueError("storage destination is outside mapped RAM")
                    emu.write_mem(destination,data)
                    access.update(result=0,sha256=sha(data))
                except ValueError as exc:
                    access.update(result=0xffffffff,error=str(exc))
                report["storage_reads"].append(access)
                emu.write_reg("r0",access["result"])
                emu.write_reg("cpsr",(regs["cpsr"]&~0x20)|(0x20 if regs["r14"]&1 else 0))
                emu.write_reg("pc",regs["r14"]&~1)
                continue
            if a.capture_gfx and pc==GFX_SOURCE:
                descriptor=emu.read_reg("r0")
                try:
                    raw=emu.read_mem(descriptor,0x24)
                    words=struct.unpack("<9I",raw)
                    graphic={"cpu":index,"descriptor":hex(descriptor),"words":[hex(v) for v in words]}
                    (a.output/("gfx-%03d-descriptor.bin"%len(report["graphics_sources"]))).write_bytes(raw)
                except (ValueError,RuntimeError) as exc:
                    graphic={"cpu":index,"descriptor":hex(descriptor),"error":str(exc)}
                report["graphics_sources"].append(graphic)
                emu.breakpoint_remove(GFX_SOURCE)
                emu.step(timeout=1)
                emu.breakpoint_add(GFX_SOURCE)
                continue
            event={"cpu":index,"pc":hex(pc),"reply":reply,"label":points.get(pc,EXCEPTIONS.get(pc,"unexpected_stop")),
                   "registers":capture(emu),"shared_words":[hex(x) for x in struct.unpack("<4I",emu.read_mem(0x209c0,16))]}
            if pc in EXCEPTIONS:
                emu.select_cpu(index)
                try:event["fault_system_registers"]=fault_registers(emu)
                except (ValueError,RuntimeError) as exc:event["fault_registers_unavailable"]=str(exc)
                emu.select_cpu(0)
            report["events"].append(event)
            if pc not in points or pc==a.stop_at:
                report["stop_reason"]=event["label"]
                break
            emu.breakpoint_remove(pc,kind=4 if pc in arm_points else 2)
            del points[pc]
        report.setdefault("stop_reason","time_bound")
        if lcd_refresh_probe:
            lcd_refresh_probe.close()
            report["lcd_refresh_test_probe"]=lcd_refresh_probe.report
            report["lcd_refresh_test_probe"]["started"]=lcd_refresh_started
        if frame_probe:
            frame_probe.close()
            report["frame_irq_test_probe"]=frame_probe.report
            report["frame_irq_test_probe"]["started"]=frame_probe_started
        if display_clock_probe:
            display_clock_probe.close()
            report["display_clock_test_probe"]=display_clock_probe.report
        if service_loop:
            service_loop.close()
            report["original_ui_service_loop_probe"]=service_loop.report
        if key_probe:
            report["software_key_probe"]=key_probe.report
            report["software_key_probe"]["armed"]=key_armed
            report["software_key_probe"]["click_release_armed"]=key_release_armed
            if a.key_sequence:
                report["software_key_sequence"]={"keys":scripted_keys,"completed_clicks":key_sequence_completed,
                    "current_index":key_sequence_index,"waiting_for_swap_after":key_next_swap_floor,
                    "next_press_requires_new_egl_swap":True,"complete":key_sequence_completed==len(scripted_keys)}
            if not key_armed:
                report["software_key_probe"]["pending_reason"]="Waiting for a real swap and, in local UI service mode, original nonzero UI state outside mode6"
            key_probe.close()
        if service_probe:
            report["original_ui_service_probe"]=service_probes[0].report
            report["original_ui_service_probes"]=[probe.report for probe in service_probes]
            for probe in service_probes:probe.close()
        report["final_registers"]=capture(emu)
        report["original_ui_state_at_stop"]={
            "control_words_08a928b0":[hex(value) for value in struct.unpack("<4I",emu.read_mem(0x08a928b0,16))],
            "transition_step_08cb0ee0":hex(struct.unpack("<I",emu.read_mem(0x08cb0ee0,4))[0]),
            "secondary_transition_stage_08aa090c":struct.unpack("<I",emu.read_mem(0x08aa090c,4))[0],
            "modal_id_08a92898":hex(struct.unpack("<H",emu.read_mem(0x08a92898,2))[0]),
            "mode35_confirmation_mask_08aa1103":hex(emu.read_mem(0x08aa1103,1)[0]),
            "confirmation_mask_08aa1104":hex(emu.read_mem(0x08aa1104,1)[0]),
            "source_scene_08cb10d0":hex(struct.unpack("<H",emu.read_mem(0x08cb10d0,2))[0]),
            "render_scene_0b392e98":hex(struct.unpack("<H",emu.read_mem(0x0b392e98,2))[0]),
            "source_layout_08cb10c8_hex":emu.read_mem(0x08cb10c8,16).hex(),
            "render_layout_0b392e90_hex":emu.read_mem(0x0b392e90,16).hex(),
            "lcd_clock_state_08ab488c":hex(struct.unpack("<I",emu.read_mem(0x08ab488c,4))[0]),
            "avio_clock_completion_08a9c7f8":hex(struct.unpack("<I",emu.read_mem(0x08a9c7f8,4))[0])}
        startup_sequence=emu.read_mem(0x08aa7f18,8)
        report["original_timed_startup_sequence"]={"base":"0x08aa7f18","bytes_hex":startup_sequence.hex(),
            "ready_byte_1":startup_sequence[1],"callback_index_byte_6":startup_sequence[6],
            "frame_service_gate_08aa02c8":emu.read_mem(0x08aa02c8,5).hex(),
            "mode35_frame_poll_count_08aac26c":struct.unpack("<H",emu.read_mem(0x08aac26c,2))[0]}
        if a.linux_peer:
            from analysis.read_kmsg import read_kmsg
            try:
                kernel=read_kmsg(GuestRamView(emu,0x80000000,0x20000000),0x80000000)
                (a.output/"linux-kmsg.json").write_text(json.dumps(kernel,indent=2)+"\n")
                lines=[record["text"] for record in kernel["records"]]
                (a.output/"linux-kmsg.txt").write_text("".join("[%12.6f] %s\n"%(row["timestamp_ns"]/1e9,row["text"]) for row in kernel["records"]))
                report["linux_observed"]={"printk_records":len(lines),
                    "physical_cpu3":any("Booting Linux on physical CPU 0x3" in line for line in lines),
                    "root_mounted":any("VFS: Mounted root" in line for line in lines),
                    "icc_loaded":any("icc driver version" in line for line in lines),
                    "icsm_loaded":any("icsm driver version" in line for line in lines),
                    "panic_seen":any("Kernel panic" in line for line in lines)}
            except (ValueError,RuntimeError) as exc:
                report["linux_observed"]={"unavailable":str(exc)}
        report["system_tick_counters"]={name:struct.unpack("<I",emu.read_mem(address,4))[0]
            for name,address in [("callback_modulo10",0x08aaa5f4),("software_timers",0x08aaa7b4),
                                 ("os_ticks",0x08ab6394),("clock64_low",0x08ab18f8)]}
        if delayed_context:
            tcb=delayed_context["tcb"]
            report["display_isolation"]["task66_after_stop"]={name:struct.unpack("<I",emu.read_mem(tcb+offset,4))[0]
                for name,offset in [("thread_state",0x30),("remaining_ticks",0x4c),("wait_type",0x100)]}
        if graphics:
            report["graphics_draws"]=graphics.backend.draw_count
            report["graphics_swaps"]=graphics.backend.swaps
            report["graphics_frames"]=[str(path) for path in sorted((a.output/"graphics").glob("frame-*.png"))]
            report["graphics_frame_scope"]="Host software EGL/OpenVG output from original guest calls; guest native scanout buffers are not synchronized"
            report["graphics_surface_diagnostics"]=[]
            for handle,canvas in graphics.backend.surfaces.items():
                path=a.output/"graphics"/("incomplete-surface-%08x.png"%handle)
                canvas.image().save(path)
                report["graphics_surface_diagnostics"].append({"surface":hex(handle),"path":str(path),
                    "scope":"Diagnostic pixels at stop; not an EGL-presented frame or proof of a menu"})
            graphics.close()
        report["shared_words"]=[hex(x) for x in struct.unpack("<4I",emu.read_mem(0x209c0,16))]
        (a.output/"result.json").write_text(json.dumps(report,indent=2)+"\n")
        if a.keep_ram:
            emu.dump_memory(0,0x20000000,a.output/"ram.bin")
            report["ram_snapshots"]=[{"file":"ram.bin","physical_base":"0x0","bytes":0x20000000}]
            if ram_bytes>0x20000000:
                emu.dump_memory(0xf0000000,0x10000000,a.output/"upper-ram.bin")
                report["ram_snapshots"].append({"file":"upper-ram.bin","physical_base":"0xf0000000","bytes":0x10000000,
                                              "scope":"Upper256MiB diagnostic slice, not a complete2GiB dump"})
            emu.dump_memory(0x7b000000,0x30000,a.output/"peer-sram.bin")
            peer=(a.output/"peer-sram.bin").read_bytes()
            report["peer_sram_snapshot"]={"address":"0x7b000000","bytes":len(peer),"sha256":sha(peer),
                "ready_word":hex(struct.unpack_from("<I",peer,0x4000)[0]),
                "data_sha256":sha(peer[:0x20000]),"code_sha256":sha(peer[0x20000:]),
                "peer_instruction_execution_implemented":False}
            original_program=(source/"03_program.bin").read_bytes()
            report["peer_sram_snapshot"]["matches_original_code_payload"]=peer[0x20000:]==original_program[0x6a2d918-0x6280000:0x6a3d918-0x6280000]
            report["peer_sram_snapshot"]["matches_original_data_payload"]=peer[:0x20000]==original_program[0x6a3d918-0x6280000:0x6a5d918-0x6280000]
        try:
            report["timer_registers_after_stop"]=sample_timer_registers(emu)
        except (RuntimeError,TimeoutError) as exc:
            report["timer_registers_after_stop"]={"unavailable":str(exc)}
        for label,path,property_name in [("rtc","/machine/rtc-peer","state-json"),
                                         ("ram_layout","/machine","ram-layout-json"),
                                         ("gic","/machine","gic-state-json"),
                                         ("spi0","/machine","spi0-state-json"),
                                         ("spi6","/machine","spi6-state-json"),
                                         ("spi_channels","/machine","spi-state-json"),
                                         ("adc","/machine","adc-state-json"),
                                         ("i2c","/machine","i2c-state-json"),
                                         ("display_test_inputs","/machine","display-test-state-json"),
                                         ("frame_test_inputs","/machine","frame-test-state-json"),
                                         ("tx_serial","/machine","tx-serial-state-json"),
                                         ("software_status","/machine","software-status-state-json"),
                                         ("unmodeled_mmio","/machine","unmodeled-mmio-json"),
                                         ("module_handshakes","/machine","module-state-json")]:
            try:
                value=emu.qmp.command("qom-get",{"path":path,"property":property_name})
                report[label]=json.loads(value) if isinstance(value,str) else value
            except RuntimeError as exc:
                report[label]={"unavailable":str(exc)}
        if report.get("spi0",{}).get("strict_stops") and report.get("stop_reason")=="unexpected_stop":
            report["stop_reason"]="spi0_unimplemented_peer"
            report["events"][-1]["label"]="spi0_unimplemented_peer"
        if report.get("spi6",{}).get("strict_stops") and report.get("stop_reason")=="unexpected_stop":
            report["stop_reason"]="spi6_unimplemented_peer"
            report["events"][-1]["label"]="spi6_unimplemented_peer"
        if report.get("tx_serial",{}).get("unsupported") and report.get("stop_reason")=="unexpected_stop":
            report["stop_reason"]="tx_serial_unsupported_transaction"
            report["events"][-1]["label"]="tx_serial_unsupported_transaction"
        if report.get("stop_reason")=="unexpected_stop":
            for state in report.get("spi_channels",{}).get("channels",[]):
                if state.get("strict_stops"):
                    label="spi%d_unimplemented_peer"%state["channel"]
                    report["stop_reason"]=label;report["events"][-1]["label"]=label
                    break
        report["uart_transmissions"]=[]
        for channel in range(5):
            try:
                encoded=emu.qmp.command("ringbuf-read",{"device":"s5-uart%d"%channel,"size":65536,"format":"base64"})
                data=base64.b64decode(encoded)
                (a.output/("uart%d-tx.bin"%channel)).write_bytes(data)
                report["uart_transmissions"].append({"channel":channel,"bytes":len(data),"sha256":sha(data),"first_128_bytes":data[:128].hex()})
            except RuntimeError as exc:
                report["uart_transmissions"].append({"channel":channel,"unavailable":str(exc)})
    report["qemu_exit_code"]=emu.process.returncode
    (a.output/"result.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({"events":[{k:e[k] for k in ("cpu","pc","label","shared_words")} for e in report["events"]],
                      "final_pcs":[r["r15"] for r in report["final_registers"]],"shared_words":report["shared_words"],
                      "stop_reason":report.get("stop_reason"),"storage_reads":len(report["storage_reads"]),
                      "graphics_sources":len(report["graphics_sources"]),
                      "graphics_draws":report.get("graphics_draws"),"graphics_swaps":report.get("graphics_swaps"),
                      "menu_rendered":report["menu_rendered"],"output":str(a.output)},indent=2))


if __name__=="__main__":main()
