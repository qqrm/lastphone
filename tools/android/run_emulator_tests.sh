#!/usr/bin/env bash
# Run the explicit x86_64/4 KB test target and retain device/GPU evidence.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REPORTS="$ROOT/mobile/android/app/build/reports/emulator"
mkdir -p "$REPORTS"
COLLECTOR_PID=""

collect_test_screenshots() {
    local candidate entries
    candidate="$(mktemp "$REPORTS/evidence-XXXXXX.tar")"
    if timeout 10s adb exec-out run-as io.github.qqrm.lastphone tar -cf - files/test-evidence \
        > "$candidate" 2>/dev/null && entries="$(tar -tf "$candidate" 2>/dev/null)" && [[ "$entries" == *".png"* ]]; then
        mv "$candidate" "$REPORTS/screenshots.tar"
    fi
    rm -f "$candidate"
}

collect_logs() {
    local status=$?
    trap - EXIT
    set +e
    if [[ -n "$COLLECTOR_PID" ]]; then
        kill "$COLLECTOR_PID" 2>/dev/null
        wait "$COLLECTOR_PID" 2>/dev/null
    fi
    collect_test_screenshots
    if [[ -f "$REPORTS/screenshots.tar" ]]; then
        mkdir -p "$REPORTS/screenshots"
        tar --no-same-owner --no-same-permissions -xf "$REPORTS/screenshots.tar" \
            --strip-components=2 -C "$REPORTS/screenshots"
    fi
    timeout 20s adb shell am start -W -n io.github.qqrm.lastphone/org.lastphone.app.MainActivity \
        > "$REPORTS/final-launch.txt" 2>&1
    timeout 30s adb shell uiautomator dump /sdcard/lastphone-ci-window.xml \
        > "$REPORTS/ui-dump.txt" 2>&1
    timeout 10s adb exec-out cat /sdcard/lastphone-ci-window.xml > "$REPORTS/final-ui.xml" 2>/dev/null
    timeout 10s adb exec-out screencap -p > "$REPORTS/final-screen.png" 2>/dev/null
    adb logcat -d -v threadtime > "$REPORTS/logcat.txt" 2>&1 || true
    exit "$status"
}
trap collect_logs EXIT

adb wait-for-device
adb shell getprop > "$REPORTS/device-properties.txt"
adb shell cat /proc/meminfo > "$REPORTS/memory.txt"
PAGE_SIZE="$(adb shell getconf PAGE_SIZE | tr -d '\r')"
ABI="$(adb shell getprop ro.product.cpu.abi | tr -d '\r')"
printf 'ABI=%s\nPAGE_SIZE=%s\n' "$ABI" "$PAGE_SIZE" | tee "$REPORTS/test-target.txt"
if [[ "$ABI" != x86_64 || "$PAGE_SIZE" != 4096 ]]; then
    echo "This test variant requires the standard x86_64, 4 KB emulator image." >&2
    exit 1
fi

adb shell cmd connectivity airplane-mode enable
adb shell svc wifi disable
adb shell svc data disable
AIRPLANE_MODE="$(adb shell settings get global airplane_mode_on | tr -d '\r')"
printf 'AIRPLANE_MODE=%s\n' "$AIRPLANE_MODE" | tee "$REPORTS/network-state.txt"
if [[ "$AIRPLANE_MODE" != 1 ]]; then
    echo "Airplane mode was not enabled; offline instrumentation cannot proceed." >&2
    exit 1
fi

# Keep evidence before any test-runner cleanup can uninstall the test application.
(
    trap - EXIT
    while true; do
        collect_test_screenshots || true
        sleep 2
    done
) &
COLLECTOR_PID=$!

cd "$ROOT/mobile/android"
./gradlew --no-daemon --max-workers=2 connectedEmulatorDebugAndroidTest
