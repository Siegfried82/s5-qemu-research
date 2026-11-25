#!/usr/bin/env zsh
set -euo pipefail

# Task A reproducible execution script.
# Run from project root: /Users/lixiuqi/Documents/ChatGPT/相机/s5-emulator
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
FOCUSED_OUT="qa/task-a/focused-vfp-${TAG}"
BASELINE_OUT="qa/task-a/baseline-${TAG}"
LENS_HIGH_OUT="qa/task-a/lens-release-high-${TAG}"
VERIFY_OUT="qa/task-a/verification-${TAG}.json"
REPORT_OUT="qa/task-a/report-${TAG}.md"

echo "[1/4] Focused SPI3 0x044e input verification -> ${FOCUSED_OUT}"
python3 -B qa/task-a/run_task_a_focused.py --output "${FOCUSED_OUT}"

COMMON_ARGS=(
  --ram 2G
  --storage-hle
  --capture-gfx
  --graphics-hle
  --spi0-open-bus
  --spi6-open-bus
  --tx-serial-hle
  --adc11-sample 0x300
  --isolate-display
  --isolate-display-ticks 600000
  --linux-peer
  --service-gui-once
  --probe-ui-mode35
  --display-clock-test
  --ui-service-loop
  --ui-service-postinit
  --frame-irq-test-pairs 128
  --frame-test-stage-seconds 10
  --lcd-refresh-test-pulses 8
  --key-event KEY_CENTER
  --key-click
  --seconds 120
)

echo "[2/4] Full SMP baseline run (lens-release-input-high OFF) -> ${BASELINE_OUT}"
python3 -B menu-smp/boot.py "${COMMON_ARGS[@]}" --output "${BASELINE_OUT}"

echo "[3/4] Full SMP controlled run (lens-release-input-high ON) -> ${LENS_HIGH_OUT}"
python3 -B menu-smp/boot.py "${COMMON_ARGS[@]}" --lens-release-input-high --output "${LENS_HIGH_OUT}"

echo "[4/4] Automated comparison & verification -> ${VERIFY_OUT}"
python3 -B qa/task-a/compare_task_a.py \
  --focused "${FOCUSED_OUT}" \
  --baseline "${BASELINE_OUT}" \
  --candidate "${LENS_HIGH_OUT}" \
  --output-json "${VERIFY_OUT}" \
  --output-md "${REPORT_OUT}"
