#!/usr/bin/env bash
# Run the explicit x86_64/4 KB test target and retain device/GPU evidence.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REPORTS="$ROOT/mobile/android/app/build/reports/emulator"
mkdir -p "$REPORTS"
COLLECTOR_PID=""
LOGCAT_PID=""

record_host_state() {
    {
        date -u '+%Y-%m-%dT%H:%M:%SZ'
        printf 'collector_pid=%s shell_pid=%s\n' "$COLLECTOR_PID" "$BASHPID"
        free -m
        ps -eo pid,ppid,pgid,rss,vsz,stat,comm --sort=-rss | sed -n '1,25p'
        for control in memory.max memory.current memory.events; do
            if [[ -r "/sys/fs/cgroup/$control" ]]; then
                printf '%s\n' "$control"
                cat "/sys/fs/cgroup/$control"
            fi
        done
    } >> "$REPORTS/host-memory-processes.txt" 2>&1
}

record_device_state() {
    {
        date -u '+%Y-%m-%dT%H:%M:%SZ'
        timeout 5s adb devices -l
        timeout 5s adb shell cat /proc/meminfo
    } >> "$REPORTS/device-memory-timeline.txt" 2>&1 || true
}

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
    record_host_state
    record_device_state
    timeout 5s sudo -n dmesg --ctime > "$REPORTS/host-kernel.txt" 2>&1
    timeout 5s sudo -n journalctl --no-pager --since '-10 minutes' \
        _COMM=systemd-coredump > "$REPORTS/host-coredumps.txt" 2>&1
    # The emulator catches signals itself, so a host crash may leave no kernel
    # segfault line. Decode its own minidumps to text, without uploading memory.
    local crashreport="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}/emulator/crashreport"
    local dump count=0
    if [[ -x "$crashreport" ]]; then
        timeout 5s "$crashreport" -l > "$REPORTS/emulator-crash-list.txt" 2>&1
        for dump in /tmp/android-"$(id -un)"/emu-crash-*.db/{pending,completed}/*.dmp; do
            [[ -f "$dump" ]] || continue
            timeout 15s "$crashreport" -d "$dump" \
                > "$REPORTS/emulator-crash-$(basename "$dump").txt" 2>&1
            count=$((count + 1))
            (( count >= 3 )) && break
        done
    fi
    local device_state
    device_state="$(timeout 3s adb get-state 2>/dev/null)"
    if [[ "$device_state" == device ]]; then
        collect_test_screenshots
        timeout 20s adb shell am start -W -n io.github.qqrm.lastphone/org.lastphone.app.MainActivity \
            > "$REPORTS/final-launch.txt" 2>&1
        timeout 30s adb shell uiautomator dump /sdcard/lastphone-ci-window.xml \
            > "$REPORTS/ui-dump.txt" 2>&1
        timeout 10s adb exec-out cat /sdcard/lastphone-ci-window.xml > "$REPORTS/final-ui.xml" 2>/dev/null
        timeout 10s adb exec-out screencap -p > "$REPORTS/final-screen.png" 2>/dev/null
        timeout 15s adb logcat -d -v threadtime > "$REPORTS/logcat.txt" 2>&1
    else
        printf 'Device unavailable after test run; use logcat-live.txt and host diagnostics.\n' > "$REPORTS/logcat.txt"
    fi
    if [[ -f "$REPORTS/screenshots.tar" ]]; then
        mkdir -p "$REPORTS/screenshots"
        tar --no-same-owner --no-same-permissions -xf "$REPORTS/screenshots.tar" \
            --strip-components=2 -C "$REPORTS/screenshots"
    fi
    if [[ -n "$LOGCAT_PID" ]]; then
        kill "$LOGCAT_PID" 2>/dev/null
        wait "$LOGCAT_PID" 2>/dev/null
    fi
    exit "$status"
}
trap collect_logs EXIT

timeout 60s adb wait-for-device
record_host_state
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

# Preserve crashes even if the guest or its adb transport subsequently vanishes.
adb logcat -v threadtime -T 1000 > "$REPORTS/logcat-live.txt" 2>&1 &
LOGCAT_PID=$!
record_device_state

# Keep evidence before any test-runner cleanup can uninstall the test application.
(
    trap - EXIT
    iteration=0
    while true; do
        collect_test_screenshots || true
        if (( iteration % 5 == 0 )); then
            record_host_state || true
            record_device_state
        fi
        iteration=$((iteration + 1))
        sleep 2
    done
) &
COLLECTOR_PID=$!

cd "$ROOT/mobile/android"
./gradlew --no-daemon --max-workers=1 \
    '-Dorg.gradle.jvmargs=-Xmx1536m -Dfile.encoding=UTF-8' connectedEmulatorDebugAndroidTest
