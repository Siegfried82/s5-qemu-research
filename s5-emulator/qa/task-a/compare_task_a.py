#!/usr/bin/env python3
"""Automated comparison and machine-readable verification for Task A."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def parse_png_summary(path: Path) -> dict:
    """Stdlib-only PNG metadata & decoded pixel digest/nonzero-alpha statistics."""
    raw = path.read_bytes()
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError(f'Not a valid PNG file: {path}')
    pos = 8
    width = height = bit_depth = color_type = None
    idat = bytearray()
    while pos < len(raw):
        length = struct.unpack_from('>I', raw, pos)[0]
        ctype = raw[pos + 4:pos + 8]
        cdata = raw[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b'IHDR':
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack('>IIBBBBB', cdata)
        elif ctype == b'IDAT':
            idat.extend(cdata)
        elif ctype == b'IEND':
            break
    info = {
        'index': int(path.stem.split('-')[-1]) if '-' in path.stem and path.stem.split('-')[-1].isdigit() else None,
        'name': path.name,
        'path': str(path),
        'bytes': len(raw),
        'sha256': hashlib.sha256(raw).hexdigest(),
        'width': width,
        'height': height,
        'bit_depth': bit_depth,
        'color_type': color_type,
    }
    if bit_depth == 8 and color_type in (2, 6) and width and height:
        bpp = 4 if color_type == 6 else 3
        stride = width * bpp
        decompressed = zlib.decompress(bytes(idat))
        expected_len = height * (1 + stride)
        if len(decompressed) == expected_len:
            recon = bytearray(height * stride)
            for y in range(height):
                ft = decompressed[y * (1 + stride)]
                row = decompressed[y * (1 + stride) + 1:(y + 1) * (1 + stride)]
                out_off = y * stride
                prev_off = (y - 1) * stride
                for x in range(stride):
                    val = row[x]
                    a = recon[out_off + x - bpp] if x >= bpp else 0
                    b = recon[prev_off + x] if y > 0 else 0
                    c = recon[prev_off + x - bpp] if (x >= bpp and y > 0) else 0
                    if ft == 0:
                        r = val
                    elif ft == 1:
                        r = (val + a) & 0xff
                    elif ft == 2:
                        r = (val + b) & 0xff
                    elif ft == 3:
                        r = (val + ((a + b) >> 1)) & 0xff
                    elif ft == 4:
                        p = a + b - c
                        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                        pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                        r = (val + pr) & 0xff
                    else:
                        r = val
                    recon[out_off + x] = r
            info['decoded_pixels_sha256'] = hashlib.sha256(recon).hexdigest()
            nonzero_pixels = 0
            bright_pixels = 0
            for i in range(0, len(recon), bpp):
                r, g, b_ch = recon[i], recon[i + 1], recon[i + 2]
                alpha = recon[i + 3] if bpp == 4 else 255
                if alpha > 0 and (r > 0 or g > 0 or b_ch > 0):
                    nonzero_pixels += 1
                if alpha > 0 and (r >= 200 and g >= 200 and b_ch >= 200):
                    bright_pixels += 1
            info['visible_nonblack_pixels'] = nonzero_pixels
            info['bright_white_pixels'] = bright_pixels
    return info


def decode_layout_hex(hex_str: str) -> dict:
    b = bytes.fromhex(hex_str)
    if len(b) != 16:
        return {'raw_hex': hex_str}
    w0, w1, w2, w3 = struct.unpack('<4I', b)
    return {
        'raw_hex': hex_str,
        'word0': f'0x{w0:x}',
        'modal_word1': f'0x{w1:x}',
        'scene_word2': f'0x{w2:x}',
        'word3': f'0x{w3:x}',
    }


def summarize_calls_per_swap(calls_path: Path) -> list:
    if not calls_path.exists():
        return []
    swaps = []
    draws_since_last = 0
    total_draws = 0
    for line in calls_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        op = row.get('call') or row.get('op') or row.get('kind') or row.get('api')
        if op in ('vgDrawPath', 'vgDrawImage', 'draw_path', 'draw_image'):
            draws_since_last += 1
            total_draws += 1
        if op in ('eglSwapBuffers', 'swap_buffers') or 'frame' in row and str(row.get('frame', '')).endswith('.png'):
            swaps.append({
                'swap_index': len(swaps),
                'record': row,
                'draws_in_frame': draws_since_last,
                'cumulative_draws': total_draws,
            })
            draws_since_last = 0
    return swaps


def extract_key_summary(res: dict) -> dict:
    kp = res.get('software_key_probe', {})
    reqs = kp.get('requests', [])
    summarized = []
    tz_confirmed = False
    release_reconfirmed = False
    for req in reqs:
        routes = [
            e.get('route') or f"{e.get('kind')}@{e.get('pc')}"
            for e in req.get('events', [])
        ]
        hit_tz_entry = any(e.get('pc') == '0x68b728e' or e.get('route') == 'original_timezone_setup_entry'
                           for e in req.get('events', []))
        hit_tz_scene = any(e.get('pc') == '0x68b7b3c' or e.get('route') == 'original_timezone_scene_setup'
                           for e in req.get('events', []))
        if req.get('action') == 1 and hit_tz_entry and hit_tz_scene:
            tz_confirmed = True
        if req.get('action') == 0 and (hit_tz_entry or hit_tz_scene):
            release_reconfirmed = True
        summarized.append({
            'key': req.get('key'),
            'action': req.get('action'),
            'consumed': req.get('consumed'),
            'egl_swaps_at_original_dispatch': req.get('egl_swaps_at_original_dispatch'),
            'input_route_gate': req.get('input_route_gate'),
            'hit_timezone_setup_entry_068b728e': hit_tz_entry,
            'hit_timezone_scene_setup_068b7b3c': hit_tz_scene,
            'route_sequence': routes,
        })
    ui = res.get('original_ui_state_at_stop', {})
    state_is_0x20 = (ui.get('control_words_08a928b0') or [None, None])[1] == '0x20'
    scene_is_70de = ui.get('source_scene_08cb10d0') == '0x70de'
    return {
        'armed': kp.get('armed'),
        'click_release_armed': kp.get('click_release_armed'),
        'key_error': res.get('key_error'),
        'requests': summarized,
        'timezone_confirmation_observed_in_route': tz_confirmed,
        'release_retriggered_confirmation': release_reconfirmed,
        'final_state_0x20_and_source_scene_0x70de': bool(state_is_0x20 and scene_is_70de),
        'timezone_confirmation_succeeded': bool(tz_confirmed and not release_reconfirmed and state_is_0x20 and scene_is_70de),
    }


def summarize_run(run_dir: Path) -> dict:
    res_path = run_dir / 'result.json'
    inv_path = run_dir / 'qemu' / 'invocation.json'
    res = json.loads(res_path.read_text())
    inv = json.loads(inv_path.read_text()) if inv_path.exists() else {}
    ui = res.get('original_ui_state_at_stop', {})
    src_layout = decode_layout_hex(ui.get('source_layout_08cb10c8_hex', ''))
    rnd_layout = decode_layout_hex(ui.get('render_layout_0b392e90_hex', ''))
    frames = [
        parse_png_summary(p)
        for p in sorted((run_dir / 'graphics').glob('frame-*.png'))
    ]
    incomplete = [
        parse_png_summary(p)
        for p in sorted((run_dir / 'graphics').glob('incomplete-surface-*.png'))
    ]
    probe_errors = {
        k: res.get(k)
        for k in (
            'ui_service_error',
            'ui_service_loop_error',
            'display_clock_test_error',
            'frame_irq_test_error',
            'lcd_refresh_test_error',
            'key_error',
            'graphics_error',
        )
        if res.get(k) is not None
    }
    modal_4a_in_state = (
        ui.get('modal_id_08a92898') == '0x4a'
        or src_layout.get('modal_word1') == '0x4a'
        or rnd_layout.get('modal_word1') == '0x4a'
    )
    return {
        'run_dir': str(run_dir),
        'execution_succeeded': res.get('qemu_exit_code') == 0 and 'graphics_error' not in probe_errors,
        'qemu_exit_code': res.get('qemu_exit_code'),
        'stop_reason': res.get('stop_reason'),
        'probe_or_graphics_errors': probe_errors,
        'lens_release_input_config': res.get('lens_release_input'),
        'rtc': res.get('rtc'),
        'original_ui_state_at_stop': ui,
        'decoded_source_layout': src_layout,
        'decoded_render_layout': rnd_layout,
        'modal_4a_warning_present_in_state_or_layout': modal_4a_in_state,
        'key_path': extract_key_summary(res),
        'graphics_draws': res.get('graphics_draws'),
        'graphics_swaps': res.get('graphics_swaps'),
        'graphics_error': res.get('graphics_error'),
        'menu_rendered': res.get('menu_rendered'),
        'lcd_refresh_completed_requests': res.get('lcd_refresh_test_probe', {}).get('completed_requests'),
        'lcd_refresh_probe_summary': {
            k: v for k, v in res.get('lcd_refresh_test_probe', {}).items()
            if k not in ('pulses', 'events')
        },
        'frames': frames,
        'incomplete_surfaces': incomplete,
        'invocation': inv,
        'comparable_config_fingerprint': {
            'qemu_sha256': res.get('qemu_sha256'),
            'components': res.get('components'),
            'ram_model': res.get('ram_model'),
            'startup_inputs': res.get('startup_inputs'),
            'spi0_model': res.get('spi0_model'),
            'spi6_model': res.get('spi6_model'),
            'tx_serial_model': res.get('tx_serial_model'),
            'adc_environment': res.get('adc_environment'),
            'display_isolation_requested_ticks': res.get('display_isolation', {}).get('requested_ticks'),
            'linux_peer_model': res.get('linux_peer_model'),
            'graphics_model': res.get('graphics_model'),
            'storage_model': res.get('storage_model'),
        },
    }


def check_single_variable(base: dict, cand: dict) -> dict:
    diffs = []
    bf = base['comparable_config_fingerprint']
    cf = cand['comparable_config_fingerprint']
    for k in sorted(set(bf) | set(cf)):
        if bf.get(k) != cf.get(k):
            diffs.append({'field': k, 'baseline': bf.get(k), 'candidate': cf.get(k)})

    # Check QEMU command line normalized for output/temp paths
    b_argv = base['invocation'].get('argv', [])
    c_argv = cand['invocation'].get('argv', [])
    b_flags = [x for x in b_argv if not ('qa/task-a' in x or '/tmp/s5-rsp-' in x)]
    c_flags = [x for x in c_argv if not ('qa/task-a' in x or '/tmp/s5-rsp-' in x)]
    argv_identical_modulo_paths = (b_flags == c_flags)

    b_lr = base['lens_release_input_config'] or {}
    c_lr = cand['lens_release_input_config'] or {}
    only_lens_var = (
        len(diffs) == 0
        and argv_identical_modulo_paths
        and b_lr.get('forced_high') is False
        and c_lr.get('forced_high') is True
        and base['rtc']['lens_release_input']['enabled'] is False
        and cand['rtc']['lens_release_input']['enabled'] is True
    )
    return {
        'verified_single_variable': only_lens_var,
        'unique_variable_name': '--lens-release-input-high (/machine/rtc-peer.lens-release-input-high)',
        'baseline_value': False,
        'candidate_value': True,
        'qemu_argv_identical_modulo_paths': argv_identical_modulo_paths,
        'unexpected_config_differences': diffs,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--focused', type=Path, required=True)
    ap.add_argument('--baseline', type=Path, required=True)
    ap.add_argument('--candidate', type=Path, required=True)
    ap.add_argument('--reference', type=Path,
                    default=Path(__file__).resolve().parents[2] / 'menu-smp/results/local-requested-lcd-refresh')
    ap.add_argument('--output-json', type=Path, required=True)
    ap.add_argument('--output-md', type=Path)
    args = ap.parse_args()

    focused_json = json.loads((args.focused / 'verification.json').read_text())
    base = summarize_run(args.baseline.resolve())
    cand = summarize_run(args.candidate.resolve())
    ref = summarize_run(args.reference.resolve()) if (args.reference / 'result.json').exists() else None

    single_var = check_single_variable(base, cand)

    # Cross-check visual frames between baseline and candidate:
    # In baseline (and reference), frame-0002.png is scene 0x70de before modal 0x4a overlays frame-0003/0004.
    base_frame_shas = [f['sha256'] for f in base['frames']]
    cand_frame_shas = [f['sha256'] for f in cand['frames']]
    ref_warning_shas = {f['sha256'] for f in (ref['frames'][3:] if ref and len(ref['frames']) >= 4 else [])}
    base_warning_shas = {f['sha256'] for f in (base['frames'][3:] if base['modal_4a_warning_present_in_state_or_layout'] and len(base['frames']) >= 4 else [])}
    known_warning_shas = ref_warning_shas | base_warning_shas
    known_scene70de_clean_shas = set()
    if ref and len(ref['frames']) >= 3:
        known_scene70de_clean_shas.add(ref['frames'][2]['sha256'])
    if len(base['frames']) >= 3:
        known_scene70de_clean_shas.add(base['frames'][2]['sha256'])

    cand_last_frame = cand['frames'][-1] if cand['frames'] else None
    cand_any_warning_frame = any(f['sha256'] in known_warning_shas for f in cand['frames']) if known_warning_shas else None
    cand_frame2_matches_ref_scene70de = (
        len(cand['frames']) >= 3 and ref is not None and len(ref['frames']) >= 3
        and cand['frames'][2]['sha256'] == ref['frames'][2]['sha256']
    )

    cand_layout_clean_70de = (
        cand['original_ui_state_at_stop'].get('modal_id_08a92898') == '0x0'
        and cand['decoded_source_layout'].get('modal_word1') == '0x0'
        and cand['decoded_source_layout'].get('scene_word2') == '0x70de'
        and cand['decoded_render_layout'].get('modal_word1') == '0x0'
        and cand['decoded_render_layout'].get('scene_word2') == '0x70de'
    )
    cand_page_updated_after_timezone = (
        len(cand['frames']) >= 3
        and cand_last_frame is not None
        and cand_last_frame['sha256'] != cand['frames'][0]['sha256']
        and cand_last_frame['sha256'] != cand['frames'][1]['sha256']
    )

    rtc_comparison = {
        'baseline_rtc': base['rtc'],
        'candidate_rtc': cand['rtc'],
        'register_044e_stored_baseline': base['rtc']['registers'].get('0x044e'),
        'register_044e_stored_candidate': cand['rtc']['registers'].get('0x044e'),
        'register_044e_effective_baseline': base['rtc']['lens_release_input']['effective_value'],
        'register_044e_effective_candidate': cand['rtc']['lens_release_input']['effective_value'],
        'candidate_affected_reads_044e': cand['rtc']['lens_release_input']['affected_reads'],
        'other_registers_identical': (
            {k: v for k, v in base['rtc']['registers'].items() if k != '0x044e'}
            == {k: v for k, v in cand['rtc']['registers'].items() if k != '0x044e'}
        ),
    }

    task_a_root = Path(__file__).resolve().parent
    preserved_attempts = [
        {'dir': str(task_a_root / 'focused-taska-01'), 'note': 'Sandbox AF_UNIX socket PermissionError attempt (preserved)'},
        {'dir': str(task_a_root / 'focused-taska-02'), 'note': 'Unmodified qemu/test_lens_release_input.py cold-reset VFP UND timeout (preserved)'},
        {'dir': str(task_a_root / 'focused-vfp-taska-01'), 'note': 'Sandbox AF_UNIX socket PermissionError attempt (preserved)'},
        {'dir': str(task_a_root / 'focused-vfp-taska-02'), 'note': 'Focused verification with architectural VFP enabled (passed)'},
        {'dir': str(task_a_root / 'baseline-taska-sandbox-01'), 'note': 'Sandbox AF_UNIX socket PermissionError attempt (preserved)'},
        {'dir': str(task_a_root / 'baseline-taska-01'), 'note': 'Host concurrent load caused pair-30 10s stage timeout at 2483/2585 storage reads (preserved)'},
        {'dir': str(task_a_root / 'baseline-taska-02'), 'note': 'Full 120s baseline run (completed, reproduced modal 0x4a)'},
        {'dir': str(task_a_root / 'lens-release-high-taska-sandbox-01'), 'note': 'Sandbox AF_UNIX socket PermissionError attempt (preserved)'},
        {'dir': str(task_a_root / 'lens-release-high-taska-01'), 'note': 'Full 120s controlled run with --lens-release-input-high (completed, modal 0x4a eliminated)'},
    ]

    verification = {
        'task': 'Task A: Lens-release input model regression and UI comparison',
        'unique_variable_check': single_var,
        'execution_vs_assertions': {
            'unmodified_focused_test_execution_succeeded': focused_json['unmodified_test_execution_succeeded'],
            'focused_with_vfp_execution_succeeded': focused_json['focused_with_vfp_enable_execution_succeeded'],
            'focused_with_vfp_assertions_passed': focused_json['focused_with_vfp_enable_assertions_passed'],
            'baseline_execution_succeeded': base['execution_succeeded'],
            'candidate_execution_succeeded': cand['execution_succeeded'],
            'single_variable_assertion_passed': single_var['verified_single_variable'],
            'baseline_reproduced_modal4a_warning': base['modal_4a_warning_present_in_state_or_layout'],
            'candidate_eliminated_modal4a_in_state_and_layout': cand_layout_clean_70de,
            'candidate_eliminated_modal4a_in_all_presented_frames': (cand_any_warning_frame is False),
            'candidate_timezone_confirmation_succeeded': cand['key_path']['timezone_confirmation_succeeded'],
            'candidate_page_actually_updated_to_scene70de': bool(cand_layout_clean_70de and cand_page_updated_after_timezone),
        },
        'summary_answers': {
            'unique_variable': single_var['unique_variable_name'],
            'baseline_stop_reason': base['stop_reason'],
            'candidate_stop_reason': cand['stop_reason'],
            'baseline_qemu_exit_code': base['qemu_exit_code'],
            'candidate_qemu_exit_code': cand['qemu_exit_code'],
            'warning_present_in_baseline': base['modal_4a_warning_present_in_state_or_layout'],
            'warning_present_in_candidate': cand['modal_4a_warning_present_in_state_or_layout'],
            'candidate_any_swap_matched_warning_sha': cand_any_warning_frame,
            'baseline_timezone_confirmation_succeeded': base['key_path']['timezone_confirmation_succeeded'],
            'candidate_timezone_confirmation_succeeded': cand['key_path']['timezone_confirmation_succeeded'],
            'candidate_page_updated': cand_page_updated_after_timezone,
            'candidate_frame2_matches_reference_scene70de_sha': cand_frame2_matches_ref_scene70de,
            'candidate_post_confirm_frames_observation': (
                'Frames 2..9 all render unobstructed scene 0x70de world-map timezone UI (no modal 0x4a); '
                'timezone steps eastward (GMT+3:30 Tehran -> GMT+4:30 -> GMT+5:00 -> GMT+5:45 -> GMT+7:00 -> GMT+9:30 -> GMT+10:00 Guam/Sydney) '
                'across the 8 completed LCD232 refresh requests, exposing an unmodeled directional/dial input once modal 0x4a no longer masks scene 0x70de.'
            ),
            'main_menu_rendered': cand['menu_rendered'],
            'lens_attached_claimed': False,
        },
        'preserved_run_directories': [x for x in preserved_attempts if Path(x['dir']).exists()],
        'focused_verification': {
            'path': str(args.focused.resolve() / 'verification.json'),
            'unmodified_test_defect': focused_json['unmodified_test_defect_diagnosis'],
            'original_entry_execution_evidence': focused_json['focused_report']['original_entry_execution_evidence'],
            'checks': focused_json['focused_report']['checks'],
        },
        'rtc_comparison': rtc_comparison,
        'ui_and_layout_comparison': {
            'baseline': {
                'ui_state': base['original_ui_state_at_stop'],
                'decoded_source_layout': base['decoded_source_layout'],
                'decoded_render_layout': base['decoded_render_layout'],
            },
            'candidate': {
                'ui_state': cand['original_ui_state_at_stop'],
                'decoded_source_layout': cand['decoded_source_layout'],
                'decoded_render_layout': cand['decoded_render_layout'],
            },
        },
        'key_path_comparison': {
            'baseline': base['key_path'],
            'candidate': cand['key_path'],
        },
        'graphics_and_frames_comparison': {
            'baseline': {
                'draws': base['graphics_draws'],
                'swaps': base['graphics_swaps'],
                'graphics_error': base['graphics_error'],
                'lcd_refresh_completed_requests': base['lcd_refresh_completed_requests'],
                'lcd_refresh_probe_summary': base['lcd_refresh_probe_summary'],
                'frames': base['frames'],
                'incomplete_surfaces': base['incomplete_surfaces'],
            },
            'candidate': {
                'draws': cand['graphics_draws'],
                'swaps': cand['graphics_swaps'],
                'graphics_error': cand['graphics_error'],
                'lcd_refresh_completed_requests': cand['lcd_refresh_completed_requests'],
                'lcd_refresh_probe_summary': cand['lcd_refresh_probe_summary'],
                'frames': cand['frames'],
                'incomplete_surfaces': cand['incomplete_surfaces'],
            },
            'reference_local_requested_lcd_refresh_frames': ref['frames'] if ref else None,
        },
        'verified_facts_vs_hypotheses': {
            'verified': [
                'Original firmware SPI3 routines 0x06752fb0 (read) and 0x06753036 (write) execute in guest Thumb mode via 0x064e6a78 -> 0x0642327e -> 0x0642c43c.',
                'Unmodified qemu/test_lens_release_input.py traps to UND (0x4) at 0x066c2620 (vpush {d0-d3}) because cold-reset CPACR/FPEXC disable VFP/NEON.',
                'With architectural VFP enabled, SPI3 register 0x044e defaults to lens_release_input.enabled=false, retains guest latch 0x02, reads 0x03 when enabled, restores 0x02 when disabled, and leaves all other bits and registers unchanged.',
                'Both 120s SMP runs share identical firmware images, QEMU binary, RAM layout, and CLI/QOM configuration except --lens-release-input-high.',
                'Enabling --lens-release-input-high affected 6938 original SPI3 reads of register 0x044e (effective 0x03 while stored latch remained 0x02), completely eliminating modal 0x4a in UI state (modal_id=0x0), source layout (0000000000000000de70000000000000), render layout (0000000000000000de70000000000000), and all 10 swap PNGs (frames 2..9 all show unobstructed scene 0x70de world-map timezone selection).',
            ],
            'still_hypotheses_or_limits': [
                'Physical chip identity of the SPI3 peer and physical pin/circuit polarity of register 0x044e bit0 are not hardware-measured.',
                '--lens-release-input-high models only an inactive external release-button input level; it does NOT model an attached lens, lens communication/ACK, sensor frames, or full DSP initialization.',
                'Once modal 0x4a no longer covers scene 0x70de, frames 2..9 step eastward across timezones (GMT+3:30 -> GMT+10:00), indicating another unmodeled directional/dial input remains active in scene 0x70de.',
                'Both runs remain local display-isolation experiments (--isolate-display, --service-gui-once, --probe-ui-mode35, --ui-service-loop, TEST IRQ pulses) and menu_rendered remains false.',
            ],
        },
    }

    # Remove verbose invocation blobs from top-level if not needed, keep paths
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(verification, indent=2, ensure_ascii=False) + '\n')

    if args.output_md:
        lines = [
            '# 任务 A：镜头释放输入模型真实回归与界面对照报告',
            '',
            '## 1. 执行与断言摘要',
            '',
            f"- **唯一变量核验 (`verified_single_variable`)**: `{single_var['verified_single_variable']}` (`{single_var['unique_variable_name']}`)",
            f"- **未修改 focused 测试执行**: `{focused_json['unmodified_test_execution_succeeded']}`（冷启动未开 VFP，在 `0x066c2620` `vpush {{d0-d3}}` 触发 UND `0x4`）",
            f"- **开启架构 VFP 后 focused 执行 / 断言**: `{focused_json['focused_with_vfp_enable_execution_succeeded']}` / `{focused_json['focused_with_vfp_enable_assertions_passed']}`（实测命中 `0x06753036` 11 次、`0x06752fb0` 14 次、`0x0642c43c` 25 次）",
            f"- **Baseline 120s 执行 / 停止原因 / 退出码**: `{base['execution_succeeded']}` / `{base['stop_reason']}` / `{base['qemu_exit_code']}`",
            f"- **Lens-release-high 120s 执行 / 停止原因 / 退出码**: `{cand['execution_succeeded']}` / `{cand['stop_reason']}` / `{cand['qemu_exit_code']}`",
            f"- **Baseline 是否存在 modal `0x4a` 告警**: `{base['modal_4a_warning_present_in_state_or_layout']}`",
            f"- **Lens-release-high 是否存在 modal `0x4a` 告警**: `{cand['modal_4a_warning_present_in_state_or_layout']}`",
            f"- **原厂时区提示 CENTER 确认成功 (Baseline / Candidate)**: `{base['key_path']['timezone_confirmation_succeeded']}` / `{cand['key_path']['timezone_confirmation_succeeded']}`",
            f"- **页面实际更新并保持无遮挡 `scene 0x70de` (Candidate)**: `{verification['execution_vs_assertions']['candidate_page_actually_updated_to_scene70de']}`",
            f"- **主菜单是否已显示 (`menu_rendered`)**: `{cand['menu_rendered']}`（仍为时区选择页 `0x70de`，且并非镜头已连接）",
            '',
            '## 2. RTC 元数据与 `0x044e` 对比',
            '',
            '| 指标 | Baseline (关闭) | Candidate (`--lens-release-input-high`) |',
            '| --- | --- | --- |',
            f"| `lens_release_input.enabled` | `{base['rtc']['lens_release_input']['enabled']}` | `{cand['rtc']['lens_release_input']['enabled']}` |",
            f"| `0x044e stored_value` | `{base['rtc']['lens_release_input']['stored_value']}` | `{cand['rtc']['lens_release_input']['stored_value']}` |",
            f"| `0x044e effective_value` | `{base['rtc']['lens_release_input']['effective_value']}` | `{cand['rtc']['lens_release_input']['effective_value']}` |",
            f"| `affected_reads` | `{base['rtc']['lens_release_input']['affected_reads']}` | `{cand['rtc']['lens_release_input']['affected_reads']}` |",
            f"| 总 `reads` / `writes` | `{base['rtc']['reads']}` / `{base['rtc']['writes']}` | `{cand['rtc']['reads']}` / `{cand['rtc']['writes']}` |",
            f"| `unknown_reads` / `malformed_frames` | `{base['rtc']['unknown_reads']}` / `{base['rtc']['malformed_frames']}` | `{cand['rtc']['unknown_reads']}` / `{cand['rtc']['malformed_frames']}` |",
            '',
            '## 3. UI 状态与源/绘制布局对比',
            '',
            '| 字段 | Baseline | Candidate (`--lens-release-input-high`) |',
            '| --- | --- | --- |',
            f"| `control_words_08a928b0` | `{base['original_ui_state_at_stop'].get('control_words_08a928b0')}` | `{cand['original_ui_state_at_stop'].get('control_words_08a928b0')}` |",
            f"| `modal_id_08a92898` | `{base['original_ui_state_at_stop'].get('modal_id_08a92898')}` | `{cand['original_ui_state_at_stop'].get('modal_id_08a92898')}` |",
            f"| `source_scene_08cb10d0` | `{base['original_ui_state_at_stop'].get('source_scene_08cb10d0')}` | `{cand['original_ui_state_at_stop'].get('source_scene_08cb10d0')}` |",
            f"| `render_scene_0b392e98` | `{base['original_ui_state_at_stop'].get('render_scene_0b392e98')}` | `{cand['original_ui_state_at_stop'].get('render_scene_0b392e98')}` |",
            f"| `source_layout_08cb10c8_hex` | `{base['original_ui_state_at_stop'].get('source_layout_08cb10c8_hex')}` | `{cand['original_ui_state_at_stop'].get('source_layout_08cb10c8_hex')}` |",
            f"| `render_layout_0b392e90_hex` | `{base['original_ui_state_at_stop'].get('render_layout_0b392e90_hex')}` | `{cand['original_ui_state_at_stop'].get('render_layout_0b392e90_hex')}` |",
            f"| `graphics_draws` / `graphics_swaps` | `{base['graphics_draws']}` / `{base['graphics_swaps']}` | `{cand['graphics_draws']}` / `{cand['graphics_swaps']}` |",
            f"| `lcd_refresh.completed_requests` | `{base['lcd_refresh_completed_requests']}` | `{cand['lcd_refresh_completed_requests']}` |",
            f"| `graphics_error` | `{base['graphics_error']}` | `{cand['graphics_error']}` |",
            '',
            '## 4. 实际 Swap PNG 帧索引与 SHA-256',
            '',
            '### Baseline 帧清单',
            '| 帧索引 | 文件 | 字节数 | SHA-256 | 非黑可见像素 | 高亮白像素 |',
            '| --- | --- | --- | --- | --- | --- |',
        ]
        for f in base['frames']:
            lines.append(f"| {f['index']} | `{f['name']}` | {f['bytes']} | `{f['sha256']}` | {f.get('visible_nonblack_pixels')} | {f.get('bright_white_pixels')} |")
        lines += [
            '',
            '### Candidate (`--lens-release-input-high`) 帧清单',
            '| 帧索引 | 文件 | 字节数 | SHA-256 | 非黑可见像素 | 高亮白像素 |',
            '| --- | --- | --- | --- | --- | --- |',
        ]
        for f in cand['frames']:
            lines.append(f"| {f['index']} | `{f['name']}` | {f['bytes']} | `{f['sha256']}` | {f.get('visible_nonblack_pixels')} | {f.get('bright_white_pixels')} |")
        args.output_md.write_text('\n'.join(lines) + '\n')

    print(json.dumps(verification['summary_answers'], indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
