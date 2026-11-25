#!/usr/bin/env python3
"""Apply the idempotent QEMU 9.2 SP804 extensions used by the S5 board.

Output 0 retains its original combined-IRQ behavior. Outputs 1 and 2 expose
individual timer levels needed by the S5 firmware's explicit IRQ map.
An opt-in property adds the inferred S5 control-bit-4 pulse output; default
SP804 behavior and the latched raw interrupt status remain unchanged.
"""
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('qemu_source', type=Path)
    args = parser.parse_args()
    path = args.qemu_source / 'hw/timer/arm_timer.c'
    text = path.read_text()
    changes = [
        ('    int level[2];\n    qemu_irq irq;\n',
         '    int level[2];\n    qemu_irq irq;\n'
         '    qemu_irq individual_irq[2]; /* S5: outputs 1/2; output 0 unchanged. */\n'),
        ('    s->level[irq] = level;\n'
         '    qemu_set_irq(s->irq, s->level[0] || s->level[1]);\n',
         '    s->level[irq] = level;\n'
         '    qemu_set_irq(s->irq, s->level[0] || s->level[1]);\n'
         '    qemu_set_irq(s->individual_irq[irq], level);\n'),
        ('    sysbus_init_irq(sbd, &s->irq);\n'
         '    memory_region_init_io(&s->iomem, obj, &sp804_ops, s,\n',
         '    sysbus_init_irq(sbd, &s->irq);\n'
         '    sysbus_init_irq(sbd, &s->individual_irq[0]);\n'
         '    sysbus_init_irq(sbd, &s->individual_irq[1]);\n'
         '    memory_region_init_io(&s->iomem, obj, &sp804_ops, s,\n'),
        ('    int freq;\n    int int_level;\n    qemu_irq irq;\n',
         '    int freq;\n    int int_level;\n    qemu_irq irq;\n'
         '    bool inferred_pulse_bit4; /* S5-only, disabled by default. */\n'),
        ('    if (s->int_level && (s->control & TIMER_CTRL_IE)) {\n',
         '    if (s->int_level && (s->control & TIMER_CTRL_IE) &&\n'
         '        !(s->inferred_pulse_bit4 && (s->control & 0x10))) {\n'),
        ('static void arm_timer_tick(void *opaque)\n'
         '{\n    arm_timer_state *s = (arm_timer_state *)opaque;\n'
         '    s->int_level = 1;\n    arm_timer_update(s);\n}\n',
         'static void arm_timer_tick(void *opaque)\n'
         '{\n    arm_timer_state *s = (arm_timer_state *)opaque;\n'
         '    s->int_level = 1;\n'
         '    if (s->inferred_pulse_bit4 && (s->control & 0x10) &&\n'
         '        (s->control & TIMER_CTRL_IE)) {\n'
         '        /* Original S5 handlers never clear INTCLR in this mode.\n'
         '         * Keep RIS/MIS latched but emit a fresh hardware IRQ edge\n'
         '         * on every actual timer expiry. This is an inferred model.\n'
         '         */\n'
         '        qemu_irq_pulse(s->irq);\n'
         '    } else {\n        arm_timer_update(s);\n    }\n}\n'),
        ('    uint32_t freq0, freq1;\n    int level[2];\n',
         '    uint32_t freq0, freq1;\n    bool inferred_pulse_bit4;\n'
         '    int level[2];\n'),
        ('    s->timer[1] = arm_timer_init(s->freq1);\n'
         '    s->timer[0]->irq = qemu_allocate_irq(sp804_set_irq, s, 0);\n',
         '    s->timer[1] = arm_timer_init(s->freq1);\n'
         '    s->timer[0]->inferred_pulse_bit4 = s->inferred_pulse_bit4;\n'
         '    s->timer[1]->inferred_pulse_bit4 = s->inferred_pulse_bit4;\n'
         '    s->timer[0]->irq = qemu_allocate_irq(sp804_set_irq, s, 0);\n'),
        ('static Property sp804_properties[] = {\n',
         'static Property sp804_properties[] = {\n'
         '    DEFINE_PROP_BOOL("s5-inferred-pulse-bit4", SP804State,\n'
         '                     inferred_pulse_bit4, false),\n'),
        ('    bool inferred_pulse_bit4; /* S5-only, disabled by default. */\n',
         '    bool inferred_pulse_bit4; /* S5-only, disabled by default. */\n'
         '    bool load_rearms_oneshot; /* Opt-in SP804 TRM reload behavior. */\n'),
        ('    uint32_t freq0, freq1;\n',
         '    bool load_rearms_oneshot;\n    uint32_t freq0, freq1;\n'),
        ('        arm_timer_recalibrate(s, 1);\n'
         '        ptimer_transaction_commit(s->timer);\n',
         '        arm_timer_recalibrate(s, 1);\n'
         '        /* ARM DDI 0271D 2.2.6: LOAD retriggers an enabled one-shot.\n'
         '         * A completed ptimer stops internally, even though ENABLE\n'
         '         * remains set in the guest control register.\n'
         '         * Opt-in preserves the existing QEMU behavior elsewhere. */\n'
         '        if (s->load_rearms_oneshot &&\n'
         '            (s->control & TIMER_CTRL_ENABLE) &&\n'
         '            (s->control & TIMER_CTRL_ONESHOT)) {\n'
         '            ptimer_run(s->timer, 1);\n'
         '        }\n'
         '        ptimer_transaction_commit(s->timer);\n'),
        ('    s->timer[0]->irq = qemu_allocate_irq(sp804_set_irq, s, 0);\n',
         '    s->timer[0]->irq = qemu_allocate_irq(sp804_set_irq, s, 0);\n'
         '    s->timer[0]->load_rearms_oneshot = s->load_rearms_oneshot;\n'
         '    s->timer[1]->load_rearms_oneshot = s->load_rearms_oneshot;\n'),
        ('                     inferred_pulse_bit4, false),\n',
         '                     inferred_pulse_bit4, false),\n'
         '    DEFINE_PROP_BOOL("load-rearms-oneshot", SP804State,\n'
         '                     load_rearms_oneshot, false),\n'),
    ]
    original = text
    for old, new in changes:
        if new in text:
            continue
        if text.count(old) != 1:
            raise SystemExit('Unsupported SP804 source; patch anchor is not unique')
        text = text.replace(old, new, 1)
    if text != original:
        path.write_text(text)
    print('SP804 S5 extensions: ' +
          ('applied' if text != original else 'already present'))


if __name__ == '__main__':
    main()
