#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${TARGET:-vm}"
case "$target" in
    vm|local) ;;
    *)
        printf 'Unsupported TARGET=%s. Use TARGET=vm or TARGET=local.\n' "$target" >&2
        exit 64
        ;;
esac
vm="${VM:-Win11}"
vboxmanage="${VBOXMANAGE:-}"
if [[ "$target" == "vm" && -z "$vboxmanage" ]]; then
    for candidate in \
        /Applications/VirtualBox.app/Contents/MacOS/VBoxManage
    do
        if [[ -x "$candidate" ]]; then
            vboxmanage="$candidate"
            break
        fi
    done
    if [[ -z "$vboxmanage" ]] && command -v VBoxManage >/dev/null 2>&1; then
        vboxmanage="$(command -v VBoxManage)"
    fi
fi
if [[ "$target" == "vm" && ! -x "$vboxmanage" ]]; then
    printf 'VBoxManage not found. Set VBOXMANAGE=... or use TARGET=local.\n' >&2
    exit 1
fi

duration="${DURATION:-45}"
warmup="${WARMUP:-5}"
start_delay_ms="${START_DELAY_MS:-0}"
instances="${INSTANCES:-512}"
dpr="${DPR:-auto}"
workload="${WORKLOAD:-cubes-fill}"
suite="${SUITE:-}"
suite_print="${SUITE_PRINT:-compact}"
suite_port="${SUITE_PORT:-}"
suite_persistent_server="${SUITE_PERSISTENT_SERVER:-1}"
suite_inter_run_delay="${SUITE_INTER_RUN_DELAY:-}"
suite_reset_vm_between_runs="${SUITE_RESET_VM_BETWEEN_RUNS:-0}"
suite_reset_settle_seconds="${SUITE_RESET_SETTLE_SECONDS:-5}"
external_server="${EXTERNAL_SERVER:-0}"
server_config_file_override="${SERVER_CONFIG_FILE:-}"
baseline="${BASELINE:-${BASELINE_SUITE:-}}"
fps_regression_pct="${FPS_REGRESSION_PCT:-7}"
frame_ms_regression_pct="${FRAME_MS_REGRESSION_PCT:-15}"
cpu_regression_pct="${CPU_REGRESSION_PCT:-25}"
stop_on_alert="${STOP_ON_ALERT:-0}"
fail_on_alert="${FAIL_ON_ALERT:-0}"
shader_iters="${SHADER_ITERS:-64}"
texture_size="${TEXTURE_SIZE:-1024}"
vertices="${VERTICES:-262144}"
dynamic_vertices="${DYNAMIC_VERTICES:-65536}"
offscreen_scale="${OFFSCREEN_SCALE:-0.75}"
passes="${PASSES:-1}"
mode="${MODE:-raf}"
chunk_ms="${CHUNK_MS:-25}"
finish_each_frame="${FINISH:-0}"
sync_every="${SYNC_EVERY:-0}"
browser_fullscreen="${BROWSER_FULLSCREEN:-0}"
guest_browser_maximize="${GUEST_BROWSER_MAXIMIZE:-1}"
if [[ -n "${LAUNCH_METHOD:-}" ]]; then
    launch_method="$LAUNCH_METHOD"
elif [[ "$target" == "vm" ]]; then
    launch_method="keyboard"
else
    launch_method="browser"
fi
guest_browser_exe="${GUEST_BROWSER_EXE:-msedge}"
guest_browser_profile="${GUEST_BROWSER_PROFILE:-}"
guest_browser_flags="${GUEST_BROWSER_FLAGS:---no-first-run --start-maximized --disable-direct-composition --disable-features=DirectCompositionSwapChain,UseDirectCompositionVideoOverlays}"
guest_kill_browser_before_run="${GUEST_KILL_BROWSER_BEFORE_RUN:-0}"
local_browser="${LOCAL_BROWSER:-chrome}"
local_browser_app="${LOCAL_BROWSER_APP:-Google Chrome}"
local_browser_process_pattern="${LOCAL_BROWSER_PROCESS_PATTERN:-Google Chrome}"
local_browser_args="${LOCAL_BROWSER_ARGS:-}"
local_browser_width="${LOCAL_BROWSER_WIDTH:-1600}"
local_browser_height="${LOCAL_BROWSER_HEIGHT:-1000}"
local_browser_isolated="${LOCAL_BROWSER_ISOLATED:-1}"
virtualbox_src="${VIRTUALBOX_SRC:-}"
allow_hazardous="${ALLOW_HAZARDOUS:-0}"
allow_heavy="${ALLOW_HEAVY:-0}"
max_canvas_pixels="${MAX_CANVAS_PIXELS:-4200000}"
release_context="${RELEASE_CONTEXT:-1}"
midrun_screenshot="${MIDRUN_SCREENSHOT:-1}"
midrun_screenshot_delay="${MIDRUN_SCREENSHOT_DELAY:-}"
visual_analysis="${VISUAL_ANALYSIS:-1}"
host_window_screenshot="${HOST_WINDOW_SCREENSHOT:-1}"
focus_screenshot_window="${FOCUS_SCREENSHOT_WINDOW:-1}"
if [[ -n "${CLEANUP_BROWSER:-}" ]]; then
    cleanup_browser="$CLEANUP_BROWSER"
elif [[ "$target" == "local" && "$local_browser_isolated" == "1" ]]; then
    cleanup_browser="1"
elif [[ "$target" == "local" ]]; then
    cleanup_browser="0"
else
    cleanup_browser="0"
fi
if [[ -z "$suite_inter_run_delay" ]]; then
    if [[ "$target" == "vm" ]]; then
        suite_inter_run_delay="3"
    else
        suite_inter_run_delay="0"
    fi
fi
exit_browser_fullscreen="${EXIT_BROWSER_FULLSCREEN:-1}"
show_desktop_after_run="${SHOW_DESKTOP_AFTER_RUN:-0}"
fail_on_graphics_alert="${FAIL_ON_GRAPHICS_ALERT:-1}"
run_start_unix="$(date +%s)"
outroot="${OUTROOT:-$script_dir/dxmtbench-runs}"
outdir="${OUTDIR:-$outroot/$(date +%Y%m%d-%H%M%S)}"
html="$script_dir/dxmtbench.html"
server_py="$script_dir/dxmtbench-server.py"
bench_py="$script_dir/dxmtbench.py"

find_port() {
    python3 "$bench_py" find-port
}

is_heavy_workload() {
    case "$1" in
        gl-cubes-heavy|shader-vortex-heavy|vertex-vortex-heavy|stencil-maze-heavy|d3d11-state-heavy)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

check_hazardous_config() {
    [[ "$allow_hazardous" == "1" ]] && return 0

    local reasons=()
    if is_heavy_workload "$workload" && [[ "$allow_heavy" != "1" ]]; then
        reasons+=("WORKLOAD=$workload is an intentional low-FPS stress scene; set ALLOW_HEAVY=1")
    fi
    [[ "$mode" == "burst" ]] && reasons+=("MODE=burst queues frames without requestAnimationFrame pacing")
    [[ "$finish_each_frame" == "1" ]] && reasons+=("FINISH=1 forces synchronous GPU flushes every frame")
    (( passes > 2 )) && reasons+=("PASSES=$passes is above the safe default ceiling of 2")
    (( shader_iters > 96 )) && reasons+=("SHADER_ITERS=$shader_iters is above the safe default ceiling of 96")
    (( texture_size > 2048 )) && reasons+=("TEXTURE_SIZE=$texture_size is above the safe default ceiling of 2048")
    (( dynamic_vertices > 262144 )) && reasons+=("DYNAMIC_VERTICES=$dynamic_vertices is above the safe default ceiling of 262144")
    if [[ "$max_canvas_pixels" == "0" && "$dpr" == "auto" ]]; then
        reasons+=("MAX_CANVAS_PIXELS=0 with DPR=auto can drive the full Retina backing size")
    fi

    if ((${#reasons[@]})); then
        {
            printf 'Refusing hazardous benchmark configuration. Set ALLOW_HAZARDOUS=1 only for an intentionally destructive stress run.\n'
            printf 'Reasons:\n'
            printf ' - %s\n' "${reasons[@]}"
        } >&2
        exit 64
    fi
}

check_hazardous_config

reset_vm_for_suite_workload() {
    [[ "$target" == "vm" ]] || return 0
    python3 "$bench_py" reset-vm "$vboxmanage" "$vm" "$suite_reset_settle_seconds"
}

if [[ -n "$suite" && "${RUN_ONE:-0}" != "1" ]]; then
    case "$suite" in
        1|default)
            suite="clear,fill-basic,vortex-shader,texture-sampling,cubes-instanced,vertex-vortex,rtt-postprocess,dynamic-buffer,cubes-fill"
            ;;
        smoke)
            suite="clear,vortex-shader,cubes-instanced,rtt-postprocess,dynamic-buffer"
            ;;
        heavy)
            if [[ "$allow_heavy" != "1" && "$allow_hazardous" != "1" ]]; then
                {
                    printf 'Refusing SUITE=heavy without ALLOW_HEAVY=1.\n'
                    printf 'These workloads are intended to fall well below vsync and can make a broken graphics stack obvious.\n'
                } >&2
                exit 64
            fi
            suite="gl-cubes-heavy,shader-vortex-heavy,vertex-vortex-heavy,stencil-maze-heavy,d3d11-state-heavy"
            ;;
        all)
            if [[ "$allow_heavy" != "1" && "$allow_hazardous" != "1" ]]; then
                {
                    printf 'Refusing SUITE=all without ALLOW_HEAVY=1 because it includes the heavy stress tier.\n'
                } >&2
                exit 64
            fi
            suite="clear,fill-basic,vortex-shader,texture-sampling,cubes-instanced,vertex-vortex,rtt-postprocess,dynamic-buffer,cubes-fill,gl-cubes-heavy,shader-vortex-heavy,vertex-vortex-heavy,stencil-maze-heavy,d3d11-state-heavy"
            ;;
    esac

    suite_root="${OUTDIR:-$outroot/$(date +%Y%m%d-%H%M%S)-suite}"
    mkdir -p "$suite_root"
    suite_port_selected=""
    suite_server_pid=""
    suite_config_file=""
    if [[ "$target" == "vm" ]]; then
        suite_port_selected="${PORT:-$suite_port}"
        if [[ -z "$suite_port_selected" ]]; then
            suite_port_selected="$(find_port)"
        fi
        if [[ "$suite_persistent_server" == "1" ]]; then
            suite_config_file="$suite_root/suite-current-config.json"
            suite_server_dir="$suite_root/server"
            mkdir -p "$suite_server_dir"
            printf '{}\n' > "$suite_config_file"
            python3 "$server_py" --bind "0.0.0.0" --port "$suite_port_selected" --root "$script_dir" --html "$html" --outdir "$suite_server_dir" --config "$suite_config_file" >"$suite_server_dir/server.stdout" 2>"$suite_server_dir/server.stderr" &
            suite_server_pid=$!
            trap 'if [[ -n "${suite_server_pid:-}" ]]; then kill "$suite_server_pid" >/dev/null 2>&1 || true; fi' EXIT
            for _ in {1..20}; do
                [[ -s "$suite_server_dir/server-ready.txt" ]] && break
                sleep 0.25
            done
        fi
    fi
    suite_tsv="$suite_root/suite-summary.tsv"
    suite_jsonl="$suite_root/suite-results.jsonl"
    suite_events="$suite_root/suite-events.jsonl"
    suite_alerts="$suite_root/suite-alerts.jsonl"
    suite_latest="$suite_root/suite-latest.json"
    suite_status="$suite_root/suite-status.txt"
    printf 'workload\tstatus\tmode\tsync_every\tfps_avg\tframe_ms_p95\tgpu_timer_usable\tgpu_ms_p95\tgpu_samples\tactive_cpu_avg\tcanvas\tdraws_per_frame\tvertices_per_frame\ttriangles_per_frame\tpixels_per_frame\tstencil_pixels_per_frame\ttexture_samples_per_frame\tfb_binds_per_frame\tstate_changes_per_frame\testimated_mib_per_second\tfb_write_mib_per_second\ttexture_mib_per_second\tclear_mib_per_second\trender_target_mib_per_second\tstencil_mib_per_second\tupload_mib_per_second\toutdir\n' > "$suite_tsv"
    : > "$suite_jsonl"
    : > "$suite_events"
    : > "$suite_alerts"
    if [[ -n "$suite_port_selected" ]]; then
        printf 'suite_port=%s\n' "$suite_port_selected" > "$suite_root/suite-network.txt"
        if [[ -n "$suite_config_file" ]]; then
            printf 'suite_persistent_server=1\n' >> "$suite_root/suite-network.txt"
            printf 'suite_config_file=%s\n' "$suite_config_file" >> "$suite_root/suite-network.txt"
        fi
    fi

    IFS=',' read -r -a suite_items <<< "$suite"
    total_items=0
    for item in "${suite_items[@]}"; do
        item="${item//[[:space:]]/}"
        [[ -n "$item" ]] && total_items=$((total_items + 1))
    done
    printf 'running suite_root=%s total=%s\n' "$suite_root" "$total_items" > "$suite_status"
    python3 "$bench_py" suite-start "$suite_events" "$suite_latest" "$suite_root" "$total_items" "$mode" "$baseline"
    case "$suite_print" in
        quiet|table) ;;
        events) printf 'suite_event=%s\n' "$(cat "$suite_latest")" ;;
        *) printf 'suite_start total=%s root=%s\n' "$total_items" "$suite_root" ;;
    esac

    index=0
    for item in "${suite_items[@]}"; do
        item="${item//[[:space:]]/}"
        [[ -n "$item" ]] || continue
        index=$((index + 1))
        child_out="$suite_root/$item"
        mkdir -p "$child_out"
        console_log="$child_out/runner.console.log"
        if [[ "$suite_reset_vm_between_runs" == "1" ]]; then
            if ! reset_vm_for_suite_workload > "$child_out/vm-reset.log" 2>&1; then
                printf 'vm reset failed before workload=%s; see %s\n' "$item" "$child_out/vm-reset.log" > "$console_log"
                printf 'suite_stop_on_alert workload=%s reason=vm-reset-failed\n' "$item"
                break
            fi
        fi
        printf 'running workload=%s index=%s/%s outdir=%s\n' "$item" "$index" "$total_items" "$child_out" > "$suite_status"
        python3 "$bench_py" suite-workload-start "$suite_events" "$suite_latest" "$suite_root" "$item" "$index" "$total_items" "$child_out"
        case "$suite_print" in
            quiet|table) ;;
            events) printf 'suite_event=%s\n' "$(cat "$suite_latest")" ;;
            *) printf 'suite_run workload=%s index=%s/%s\n' "$item" "$index" "$total_items" ;;
        esac
        set +e
        if [[ -n "$suite_port_selected" ]]; then
            if [[ -n "$suite_config_file" ]]; then
                TARGET="$target" RUN_ONE=1 WORKLOAD="$item" OUTDIR="$child_out" PORT="$suite_port_selected" EXTERNAL_SERVER=1 SERVER_CONFIG_FILE="$suite_config_file" "$0" >"$console_log" 2>&1
            else
                TARGET="$target" RUN_ONE=1 WORKLOAD="$item" OUTDIR="$child_out" PORT="$suite_port_selected" "$0" >"$console_log" 2>&1
            fi
        else
            TARGET="$target" RUN_ONE=1 WORKLOAD="$item" OUTDIR="$child_out" "$0" >"$console_log" 2>&1
        fi
        rc=$?
        set -e

        python3 "$bench_py" suite-workload-result "$item" "$child_out" "$rc" "$suite_tsv" "$suite_jsonl" "$suite_events" "$suite_alerts" "$suite_latest" "$suite_status" "$suite_root" "$baseline" "$fps_regression_pct" "$frame_ms_regression_pct" "$cpu_regression_pct" "$suite_print" "$index" "$total_items"
        if [[ -s "$child_out/suite-alert.flag" && "$stop_on_alert" == "1" ]]; then
            case "$suite_print" in
                quiet|table) ;;
                *) printf 'suite_stop_on_alert workload=%s\n' "$item" ;;
            esac
            break
        fi
        if [[ "$suite_inter_run_delay" != "0" && "$index" -lt "$total_items" ]]; then
            sleep "$suite_inter_run_delay"
        fi
    done

    alerts_count="$(wc -l < "$suite_alerts" | tr -d ' ')"
    results_count="$(wc -l < "$suite_jsonl" | tr -d ' ')"
    python3 "$bench_py" suite-complete "$suite_events" "$suite_latest" "$suite_status" "$suite_root" "$results_count" "$alerts_count"
    case "$suite_print" in
        quiet) ;;
        table)
            column -t -s $'\t' "$suite_tsv" 2>/dev/null || cat "$suite_tsv"
            printf '%s\n' "$suite_root"
            ;;
        events)
            printf 'suite_event=%s\n' "$(cat "$suite_latest")"
            ;;
        *)
            printf 'suite_complete results=%s alerts=%s root=%s\n' "$results_count" "$alerts_count" "$suite_root"
            ;;
    esac
    if [[ "$fail_on_alert" == "1" && "$alerts_count" != "0" ]]; then
        exit 3
    fi
    exit 0
fi

mkdir -p "$outdir"

pid_for_vm() {
    pgrep -nf '/VirtualBoxVM.app/.*/VirtualBoxVM .*--startvm' || true
}

browser_window_id() {
    [[ "$target" == "local" ]] || return 1
    swift - "$local_browser_app" "$run_id" <<'SWIFT' 2>/dev/null || true
import CoreGraphics
import Foundation

let args = Array(CommandLine.arguments.dropFirst())
let appName = args.first ?? "Google Chrome"
let runId = args.count > 1 ? args[1] : ""
let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
let candidates = windows.compactMap { window -> (Int, Int, CGFloat)? in
    let owner = window[kCGWindowOwnerName as String] as? String ?? ""
    guard owner == appName || owner.contains(appName) else { return nil }
    let title = window[kCGWindowName as String] as? String ?? ""
    guard title.contains("DXMTBench") else { return nil }
    if !runId.isEmpty && !title.contains(runId) { return nil }
    guard let number = window[kCGWindowNumber as String] as? Int else { return nil }
    let layer = window[kCGWindowLayer as String] as? Int ?? 0
    let bounds = window[kCGWindowBounds as String] as? [String: Any] ?? [:]
    let width = bounds["Width"] as? CGFloat ?? 0
    let height = bounds["Height"] as? CGFloat ?? 0
    return (number, layer, width * height)
}
if let best = candidates.sorted(by: { lhs, rhs in
    if lhs.1 != rhs.1 { return lhs.1 < rhs.1 }
    return lhs.2 > rhs.2
}).first {
    print(best.0)
}
SWIFT
}

vm_window_id() {
    [[ "$target" == "vm" ]] || return 1
    swift - "$vm" <<'SWIFT' 2>/dev/null || true
import CoreGraphics
import Foundation

let vmName = CommandLine.arguments.dropFirst().first ?? ""
let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
let candidates = windows.compactMap { window -> (Int, CGFloat)? in
    let owner = window[kCGWindowOwnerName as String] as? String ?? ""
    guard owner.contains("VirtualBox") else { return nil }
    let title = window[kCGWindowName as String] as? String ?? ""
    if !vmName.isEmpty && !title.contains(vmName) { return nil }
    guard let number = window[kCGWindowNumber as String] as? Int else { return nil }
    let bounds = window[kCGWindowBounds as String] as? [String: Any] ?? [:]
    let width = bounds["Width"] as? CGFloat ?? 0
    let height = bounds["Height"] as? CGFloat ?? 0
    return (number, width * height)
}
if let best = candidates.sorted(by: { $0.1 > $1.1 }).first {
    print(best.0)
}
SWIFT
}

focus_vm_window() {
    [[ "$target" == "vm" ]] || return 0
    [[ "$focus_screenshot_window" == "1" ]] || return 0
    osascript <<'OSA' >/dev/null 2>&1 || true
tell application "System Events"
    repeat with proc in application processes
        set procName to name of proc
        if procName contains "VirtualBox" then
            set frontmost of proc to true
            exit repeat
        end if
    end repeat
end tell
OSA
    sleep 0.2
}

sample_cpu() {
    local seconds="$1" file="$2" pid="$3"
    : > "$file"
    for ((i=0; i<seconds; i++)); do
        local ts
        ts="$(date +%H:%M:%S)"
        ps -axo pcpu,pmem,pid,comm | awk -v pid="$pid" -v ts="$ts" '$3 == pid {print ts, $1, $2}' >> "$file"
        sleep 1
    done
}

summarize_cpu() {
    awk '{sum+=$2; n+=1; if(min==""||$2<min) min=$2; if($2>max) max=$2} END {if(n) printf "avg=%.1f min=%.1f max=%.1f n=%d", sum/n, min, max, n; else printf "no samples"}' "$1"
}

descendant_pids() {
    local root="$1"
    local frontier=("$root")
    local all=("$root")
    local child
    while ((${#frontier[@]})); do
        local next=()
        for child in "${frontier[@]}"; do
            while IFS= read -r grandchild; do
                [[ -n "$grandchild" ]] || continue
                all+=("$grandchild")
                next+=("$grandchild")
            done < <(pgrep -P "$child" 2>/dev/null || true)
        done
        frontier=("${next[@]}")
    done
    printf '%s\n' "${all[@]}" | awk 'NF && !seen[$1]++'
}

sample_cpu_tree() {
    local seconds="$1" file="$2" root_pid="$3"
    : > "$file"
    for ((i=0; i<seconds; i++)); do
        local ts pids
        ts="$(date +%H:%M:%S)"
        pids="$(descendant_pids "$root_pid" | paste -sd, -)"
        if [[ -n "$pids" ]]; then
            ps -axo pcpu,pmem,pid | awk -v pids="$pids" -v ts="$ts" '
                BEGIN {
                    split(pids, ids, ",")
                    for (i in ids)
                        wanted[ids[i]] = 1
                }
                $3 in wanted {
                    cpu += $1
                    mem += $2
                    n += 1
                }
                END {
                    if (n)
                        printf "%s %.1f %.1f %d\n", ts, cpu, mem, n
                }' >> "$file"
        fi
        sleep 1
    done
}

sample_cpu_matching() {
    local seconds="$1" file="$2" pattern="$3"
    : > "$file"
    for ((i=0; i<seconds; i++)); do
        local ts
        ts="$(date +%H:%M:%S)"
        ps -axo pcpu,pmem,pid,command | awk -v pattern="$pattern" -v ts="$ts" '
            index($0, pattern) && $0 !~ /awk -v pattern/ {
                cpu += $1
                mem += $2
                n += 1
            }
            END {
                if (n)
                    printf "%s %.1f %.1f %d\n", ts, cpu, mem, n
            }' >> "$file"
        sleep 1
    done
}

open_url_in_local_browser() {
    local url="$1"
    local before_pids after_pids newest_pid
    printf 'local_browser=%s\n' "$local_browser" | tee -a "$outdir/run-config.txt"
    printf 'local_browser_app=%s\n' "$local_browser_app" | tee -a "$outdir/run-config.txt"
    printf 'local_browser_process_pattern=%s\n' "$local_browser_process_pattern" | tee -a "$outdir/run-config.txt"
    before_pids="$(pgrep -f "$local_browser_process_pattern" 2>/dev/null | sort -n | paste -sd, - || true)"
    if [[ "$local_browser" == "chrome" && "$local_browser_isolated" == "1" ]]; then
        local chrome_bin profile_dir
        chrome_bin="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        profile_dir="$outdir/chrome-profile"
        mkdir -p "$profile_dir"
        "$chrome_bin" \
            --user-data-dir="$profile_dir" \
            --no-first-run \
            --disable-default-apps \
            --window-position=80,80 \
            --window-size="${local_browser_width},${local_browser_height}" \
            --new-window "$url" \
            >"$outdir/local-browser.stdout" 2>"$outdir/local-browser.stderr" &
        local_browser_pid=$!
        printf 'local_browser_profile=%s\n' "$profile_dir" >> "$outdir/run-config.txt"
    elif [[ "$local_browser" == "chrome" ]]; then
        URL="$url" WIDTH="$local_browser_width" HEIGHT="$local_browser_height" \
            osascript <<'OSA' >"$outdir/local-browser.stdout" 2>"$outdir/local-browser.stderr"
set benchUrl to system attribute "URL"
set winWidth to (system attribute "WIDTH") as integer
set winHeight to (system attribute "HEIGHT") as integer
tell application "Google Chrome"
    activate
    if (count of windows) = 0 then
        make new window
    end if
    set targetWindow to front window
    set bounds of targetWindow to {80, 80, 80 + winWidth, 80 + winHeight}
    set targetTabIndex to active tab index of targetWindow
    set URL of tab targetTabIndex of targetWindow to benchUrl
end tell
OSA
    elif [[ -x "$local_browser" ]]; then
        "$local_browser" ${local_browser_args:+$local_browser_args} "$url" >"$outdir/local-browser.stdout" 2>"$outdir/local-browser.stderr" &
        local_browser_pid=$!
    else
        open -a "$local_browser_app" "$url" >"$outdir/local-browser.stdout" 2>"$outdir/local-browser.stderr"
    fi
    sleep 2
    after_pids="$(pgrep -f "$local_browser_process_pattern" 2>/dev/null | sort -n | paste -sd, - || true)"
    newest_pid="$(pgrep -nf "$local_browser_process_pattern" 2>/dev/null || true)"
    printf 'local_browser_pids_before=%s\n' "$before_pids" >> "$outdir/run-config.txt"
    printf 'local_browser_pids_after=%s\n' "$after_pids" >> "$outdir/run-config.txt"
    printf 'local_browser_newest_pid=%s\n' "$newest_pid" >> "$outdir/run-config.txt"
    if [[ -n "${local_browser_pid:-$newest_pid}" ]]; then
        printf '%s\n' "${local_browser_pid:-$newest_pid}" > "$outdir/local-browser.pid"
    fi
    sleep 2
}

local_cleanup_after_run() {
    [[ "$cleanup_browser" == "1" ]] || return 0
    if [[ -n "${local_browser_pid:-}" ]] && kill -0 "$local_browser_pid" >/dev/null 2>&1; then
        descendant_pids "$local_browser_pid" | sort -rn | xargs kill >/dev/null 2>&1 || true
        sleep 1
        descendant_pids "$local_browser_pid" | sort -rn | xargs kill -9 >/dev/null 2>&1 || true
        return 0
    fi
    if [[ "$local_browser" == "chrome" ]]; then
        open -a "$local_browser_app" "about:blank" >/dev/null 2>&1 || true
    fi
}

key_scancodes() {
    "$vboxmanage" controlvm "$vm" keyboardputscancode "$@" >/dev/null 2>&1 || true
}

maximize_guest_browser_window() {
    [[ "$guest_browser_maximize" == "1" ]] || return 0
    [[ "$browser_fullscreen" == "1" ]] && return 0

    # Alt+Space, X asks the active window's system menu to maximize without entering
    # browser fullscreen or cycling through Win+Up snap states on repeated runs.
    key_scancodes 38 39 b9 b8
    sleep 0.2
    key_scancodes 2d ad
    sleep 0.8
}

open_url_in_guest() {
    local url="$1"
    local mode="${2:-$launch_method}"
    key_scancodes 01 81
    sleep 0.3
    if [[ "$mode" == "run" ]]; then
        local command
        command="cmd /c start \"\" $guest_browser_exe --user-data-dir=\"$guest_browser_profile\" $guest_browser_flags --new-window \"$url\""
        key_scancodes e0 5b 13 93 e0 db
        sleep 0.8
        "$vboxmanage" controlvm "$vm" keyboardputstring "$command" >/dev/null 2>&1 || true
    elif [[ "$mode" == "browser" ]]; then
        key_scancodes 1d 26 a6 9d
        sleep 0.3
        "$vboxmanage" controlvm "$vm" keyboardputstring "$url" >/dev/null 2>&1 || true
    else
        key_scancodes e0 5b 13 93 e0 db
        sleep 1.0
    fi
    if [[ "$mode" == "clipboard" ]]; then
        printf '%s' "$url" | pbcopy
        "$vboxmanage" controlvm "$vm" clipboard mode bidirectional >/dev/null 2>&1 || true
        sleep 0.5
        key_scancodes 1d 2f af 9d
    elif [[ "$mode" == "keyboard" ]]; then
        "$vboxmanage" controlvm "$vm" keyboardputstring "$url" >/dev/null 2>&1 || true
    fi
    sleep 0.3
    key_scancodes 1c 9c
    sleep 5.0
    maximize_guest_browser_window
    if [[ "$browser_fullscreen" == "1" ]]; then
        key_scancodes 57 d7
    fi
}

wait_for_event() {
    local event="$1"
    local timeout_seconds="$2"
    local file="$outdir/browser-events.jsonl"
    local deadline=$((SECONDS + timeout_seconds))
    while (( SECONDS < deadline )); do
        if [[ -s "$file" ]] && rg -q "\"event\": \"$event\"" "$file"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

guest_cleanup_after_run() {
    [[ "$cleanup_browser" == "1" ]] || return 0

    if [[ "$exit_browser_fullscreen" == "1" && "$browser_fullscreen" == "1" ]]; then
        key_scancodes 57 d7
        sleep 0.5
    fi

    key_scancodes 01 81
    sleep 0.2
    key_scancodes 1d 26 a6 9d
    sleep 0.2
    "$vboxmanage" controlvm "$vm" keyboardputstring "about:blank" >/dev/null 2>&1 || true
    sleep 0.2
    key_scancodes 1c 9c
    sleep 0.8

    if [[ "$show_desktop_after_run" == "1" ]]; then
        key_scancodes e0 5b 20 a0 e0 db
        sleep 0.5
    fi
}

vm_log_file_from_info() {
    local info="$1"
    local log_fldr cfg_file
    log_fldr="$(printf '%s\n' "$info" | awk -F= '$1=="LogFldr"{gsub(/^"|"$/,"",$2); print $2; exit}')"
    if [[ -n "$log_fldr" ]]; then
        printf '%s/VBox.log\n' "$log_fldr"
        return 0
    fi
    cfg_file="$(printf '%s\n' "$info" | awk -F= '$1=="CfgFile"{gsub(/^"|"$/,"",$2); print $2; exit}')"
    if [[ -n "$cfg_file" ]]; then
        printf '%s/Logs/VBox.log\n' "$(dirname "$cfg_file")"
    fi
}

capture_graphics_log_start() {
    vm_log_file="$(vm_log_file_from_info "$vm_info_mr")"
    if [[ -n "$vm_log_file" && -r "$vm_log_file" ]]; then
        vm_log_start_line="$(wc -l < "$vm_log_file" | tr -d ' ')"
    else
        vm_log_file=""
        vm_log_start_line="0"
    fi
}

scan_graphics_log_delta() {
    [[ -n "${vm_log_file:-}" && -r "$vm_log_file" ]] || return 0

    local from_line graphics_log graphics_summary
    from_line=$((vm_log_start_line + 1))
    graphics_log="$outdir/graphics-alerts.log"
    graphics_summary="$outdir/graphics-alerts-summary.txt"
    tail -n +"$from_line" "$vm_log_file" \
        | rg 'cpu argument heap overflow|MTLCommandBufferErrorDomain|kIOGPUCommandBufferCallbackErrorOutOfMemory|No rendering attachment or uav is bounded|VMSVGA: unknown sid|KERN_INVALID_ADDRESS|SIGSEGV|EXC_BAD_ACCESS' \
        > "$graphics_log" || true

    if [[ ! -s "$graphics_log" ]]; then
        printf 'graphics_alert=none\n' > "$graphics_summary"
        return 0
    fi

    {
        printf 'graphics_alert=1\n'
        printf 'vm_log_file=%s\n' "$vm_log_file"
        printf 'vm_log_start_line=%s\n' "$vm_log_start_line"
        printf 'matches=%s\n' "$(wc -l < "$graphics_log" | tr -d ' ')"
        printf 'first_matches=\n'
        sed -n '1,20p' "$graphics_log"
    } > "$graphics_summary"
    return 5
}

port="${PORT:-$(find_port)}"
if [[ -n "${SERVER_BIND:-}" ]]; then
    server_bind="$SERVER_BIND"
elif [[ "$target" == "local" ]]; then
    server_bind="127.0.0.1"
else
    server_bind="0.0.0.0"
fi
if [[ -n "${URL_HOST:-}" ]]; then
    url_host="$URL_HOST"
elif [[ "$target" == "local" ]]; then
    url_host="127.0.0.1"
else
    url_host="10.0.2.2"
fi
run_id="$(date +%Y%m%d-%H%M%S)"
if [[ -z "$guest_browser_profile" ]]; then
    guest_browser_profile="%TEMP%\\dxmtbench-$run_id"
fi
host_cpus="$(sysctl -n hw.ncpu 2>/dev/null || true)"
host_memory_mb="$(sysctl -n hw.memsize 2>/dev/null | awk '{printf "%.0f", $1 / 1048576}' || true)"
vm_info_mr=""
vm_cpus=""
vm_memory_mb=""
vm_vram_mb=""
vm_graphics=""
vm_3d=""
vm_log_file=""
vm_log_start_line="0"
if [[ "$target" == "vm" ]]; then
    vm_info_mr="$("$vboxmanage" showvminfo "$vm" --machinereadable 2>/dev/null || true)"
    vm_cpus="$(printf '%s\n' "$vm_info_mr" | awk -F= '$1=="cpus"{gsub(/"/,"",$2); print $2; exit}')"
    vm_memory_mb="$(printf '%s\n' "$vm_info_mr" | awk -F= '$1=="memory"{gsub(/"/,"",$2); print $2; exit}')"
    vm_vram_mb="$(printf '%s\n' "$vm_info_mr" | awk -F= '$1=="vram"{gsub(/"/,"",$2); print $2; exit}')"
    vm_graphics="$(printf '%s\n' "$vm_info_mr" | awk -F= '$1=="graphicscontroller"{gsub(/"/,"",$2); print $2; exit}')"
    vm_3d="$(printf '%s\n' "$vm_info_mr" | awk -F= '$1=="accelerate3d"{gsub(/"/,"",$2); print $2; exit}')"
    capture_graphics_log_start
fi
bench_config_file="$outdir/bench-config.json"
server_config_file="${server_config_file_override:-$bench_config_file}"
python3 "$bench_py" write-config "$bench_config_file" \
    run "$run_id" \
    outdir "$outdir" \
    target "$target" \
    duration "$((duration * 1000))" \
    warmup "$((warmup * 1000))" \
    startDelay "$start_delay_ms" \
    instances "$instances" \
    dpr "$dpr" \
    workload "$workload" \
    shaderIters "$shader_iters" \
    textureSize "$texture_size" \
    vertices "$vertices" \
    dynamicVertices "$dynamic_vertices" \
    offscreenScale "$offscreen_scale" \
    passes "$passes" \
    mode "$mode" \
    chunkMs "$chunk_ms" \
    finish "$finish_each_frame" \
    syncEvery "$sync_every" \
    maxCanvasPixels "$max_canvas_pixels" \
    releaseContext "$release_context" \
    hostCpus "$host_cpus" \
    hostMemoryMb "$host_memory_mb" \
    localBrowser "$local_browser" \
    vmCpus "$vm_cpus" \
    vmMemoryMb "$vm_memory_mb" \
    vmVramMb "$vm_vram_mb" \
    vmGraphics "$vm_graphics" \
    vm3d "$vm_3d"
if [[ "$server_config_file" != "$bench_config_file" ]]; then
    server_config_tmp="${server_config_file}.tmp.$$"
    cp "$bench_config_file" "$server_config_tmp"
    mv "$server_config_tmp" "$server_config_file"
fi
url="http://${url_host}:${port}/bench.html?run=${run_id}&cfg=1"

{
    printf 'target=%s\n' "$target"
    printf 'vm=%s\n' "$vm"
    printf 'vboxmanage=%s\n' "$vboxmanage"
    printf 'server_bind=%s\n' "$server_bind"
    printf 'url_host=%s\n' "$url_host"
    printf 'url=%s\n' "$url"
    printf 'bench_config_file=%s\n' "$bench_config_file"
    printf 'server_config_file=%s\n' "$server_config_file"
    printf 'external_server=%s\n' "$external_server"
    printf 'duration=%ss\n' "$duration"
    printf 'warmup=%ss\n' "$warmup"
    printf 'start_delay_ms=%s\n' "$start_delay_ms"
    printf 'instances=%s\n' "$instances"
    printf 'dpr=%s\n' "$dpr"
    printf 'workload=%s\n' "$workload"
    printf 'shader_iters=%s\n' "$shader_iters"
    printf 'texture_size=%s\n' "$texture_size"
    printf 'vertices=%s\n' "$vertices"
    printf 'dynamic_vertices=%s\n' "$dynamic_vertices"
    printf 'offscreen_scale=%s\n' "$offscreen_scale"
    printf 'passes=%s\n' "$passes"
    printf 'mode=%s\n' "$mode"
    printf 'chunk_ms=%s\n' "$chunk_ms"
    printf 'finish_each_frame=%s\n' "$finish_each_frame"
    printf 'sync_every=%s\n' "$sync_every"
    printf 'allow_hazardous=%s\n' "$allow_hazardous"
    printf 'allow_heavy=%s\n' "$allow_heavy"
    printf 'max_canvas_pixels=%s\n' "$max_canvas_pixels"
    printf 'release_context=%s\n' "$release_context"
    printf 'midrun_screenshot=%s\n' "$midrun_screenshot"
    printf 'midrun_screenshot_delay=%s\n' "$midrun_screenshot_delay"
    printf 'visual_analysis=%s\n' "$visual_analysis"
    printf 'host_window_screenshot=%s\n' "$host_window_screenshot"
    printf 'focus_screenshot_window=%s\n' "$focus_screenshot_window"
    printf 'cleanup_browser=%s\n' "$cleanup_browser"
    printf 'exit_browser_fullscreen=%s\n' "$exit_browser_fullscreen"
    printf 'show_desktop_after_run=%s\n' "$show_desktop_after_run"
    printf 'fail_on_graphics_alert=%s\n' "$fail_on_graphics_alert"
    printf 'vm_log_file=%s\n' "${vm_log_file:-}"
    printf 'vm_log_start_line=%s\n' "${vm_log_start_line:-0}"
    printf 'host_cpus=%s\n' "$host_cpus"
    printf 'host_memory_mb=%s\n' "$host_memory_mb"
    printf 'vm_cpus=%s\n' "$vm_cpus"
    printf 'vm_memory_mb=%s\n' "$vm_memory_mb"
    printf 'vm_vram_mb=%s\n' "$vm_vram_mb"
    printf 'vm_graphics=%s\n' "$vm_graphics"
    printf 'vm_3d=%s\n' "$vm_3d"
    printf 'browser_fullscreen=%s\n' "$browser_fullscreen"
    printf 'guest_browser_maximize=%s\n' "$guest_browser_maximize"
    printf 'launch_method=%s\n' "$launch_method"
    printf 'guest_browser_exe=%s\n' "$guest_browser_exe"
    printf 'guest_browser_profile=%s\n' "$guest_browser_profile"
    printf 'guest_browser_flags=%s\n' "$guest_browser_flags"
    printf 'guest_kill_browser_before_run=%s\n' "$guest_kill_browser_before_run"
    printf 'local_browser=%s\n' "$local_browser"
    printf 'local_browser_app=%s\n' "$local_browser_app"
    printf 'local_browser_process_pattern=%s\n' "$local_browser_process_pattern"
    printf 'local_browser_width=%s\n' "$local_browser_width"
    printf 'local_browser_height=%s\n' "$local_browser_height"
    printf 'local_browser_isolated=%s\n' "$local_browser_isolated"
    printf 'virtualbox_src=%s\n' "$virtualbox_src"
    printf 'run_start_unix=%s\n' "$run_start_unix"
} | tee "$outdir/run-config.txt"

{
    sw_vers 2>/dev/null || true
    uname -a
    sysctl -n hw.model hw.ncpu hw.memsize 2>/dev/null | awk 'NR==1{print "hw.model="$0} NR==2{print "hw.ncpu="$0} NR==3{print "hw.memsize="$0}'
    if [[ "$target" == "vm" ]]; then
        "$vboxmanage" --version 2>/dev/null | sed 's/^/VBoxManage.version=/'
    fi
    if [[ "$target" == "local" ]]; then
        printf 'local_browser=%s\n' "$local_browser"
        printf 'local_browser_app=%s\n' "$local_browser_app"
        printf 'local_browser_process_pattern=%s\n' "$local_browser_process_pattern"
        /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --version 2>/dev/null | sed 's/^/chrome.version=/' || true
    fi
    if [[ -n "$virtualbox_src" ]]; then
        git -C "$virtualbox_src" rev-parse --short HEAD 2>/dev/null | sed 's/^/virtualbox.git=/'
    fi
} > "$outdir/host-info.txt"

capture_crash_diagnostics() {
    local reason="$1"
    {
        printf 'reason=%s\n' "$reason"
        printf 'date=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'processes=\n'
        pgrep -fl '/VirtualBoxVM.app/.*/VirtualBoxVM|VBoxSVC|VBoxXPCOMIPCD' || true
        printf 'vminfo=\n'
        "$vboxmanage" showvminfo "$vm" --machinereadable 2>&1 \
            | rg '^(VMState|VMStateChangeTime|VideoMode|GuestAdditionsRunLevel|accelerate3d|vram|graphicscontroller)=' || true
    } > "$outdir/crash-diagnostics.txt"
    python3 "$bench_py" crash-diagnostics "$run_start_unix" "$outdir"
}

capture_run_screenshot() {
    local path="$1"
    if [[ "$target" == "vm" ]]; then
        if "$vboxmanage" showvminfo "$vm" --machinereadable 2>/dev/null | rg -q '^VMState="running"'; then
            "$vboxmanage" controlvm "$vm" screenshotpng "$path" >/dev/null 2>&1 || true
        fi
    else
        local window_id
        window_id="$(browser_window_id | head -n 1)"
        if [[ -n "$window_id" ]]; then
            screencapture -x -l "$window_id" "$path" >/dev/null 2>&1 || true
        else
            printf 'screenshot_skipped=%s reason=local_benchmark_window_not_found\n' "$path" >> "$outdir/run-config.txt"
        fi
    fi
}

capture_host_window_screenshot() {
    local path="$1"
    [[ "$target" == "vm" ]] || return 0
    [[ "$host_window_screenshot" == "1" ]] || return 0

    local window_id
    focus_vm_window
    window_id="$(vm_window_id | head -n 1)"
    if [[ -n "$window_id" ]]; then
        screencapture -x -l "$window_id" "$path" >/dev/null 2>&1 || true
    fi
}

analyze_visuals() {
    [[ "$visual_analysis" == "1" ]] || return 0

    if ! python3 "$bench_py" analyze-visuals "$outdir" "$target"
    then
        printf 'visual_analysis=failed\n' > "$outdir/visual-summary.txt"
    fi
}

pid=""
if [[ "$target" == "vm" ]]; then
    pid="$(pid_for_vm)"
    if [[ -z "$pid" ]]; then
        echo "VirtualBoxVM pid not found" >&2
        exit 1
    fi
    printf 'pid=%s\n' "$pid" | tee -a "$outdir/run-config.txt"

    "$vboxmanage" showvminfo "$vm" --machinereadable \
        | rg '^(VMState|VideoMode|accelerate3d|vram|graphicscontroller|clipboard|GuestAdditionsRunLevel)=' \
        | tee "$outdir/vminfo-before.txt"
fi

server_pid=""
if [[ "$external_server" != "1" ]]; then
    python3 "$server_py" --bind "$server_bind" --port "$port" --root "$script_dir" --html "$html" --outdir "$outdir" --config "$server_config_file" >"$outdir/server.stdout" 2>"$outdir/server.stderr" &
    server_pid=$!
    cleanup() {
        kill "$server_pid" >/dev/null 2>&1 || true
    }
    trap cleanup EXIT

    for _ in {1..20}; do
        [[ -s "$outdir/server-ready.txt" ]] && break
        sleep 0.25
    done
fi

if [[ "$target" == "vm" ]]; then
    "$vboxmanage" controlvm "$vm" screenshotpng "$outdir/before.png" >/dev/null 2>&1 || true
    capture_host_window_screenshot "$outdir/host-before.png"
    if [[ "$guest_kill_browser_before_run" == "1" ]]; then
        open_url_in_guest "cmd /c taskkill /IM msedge.exe /F >NUL 2>&1" "keyboard"
        sleep 1
    fi
    open_url_in_guest "$url"
else
    open_url_in_local_browser "$url"
    capture_run_screenshot "$outdir/before.png"
    pid="$(cat "$outdir/local-browser.pid" 2>/dev/null || true)"
    printf 'pid=%s\n' "$pid" | tee -a "$outdir/run-config.txt"
fi

if ! wait_for_event "script-start" 10; then
    if [[ "$target" == "vm" ]]; then
        retry_method="browser"
        if [[ "$launch_method" == "browser" ]]; then
            retry_method="keyboard"
        fi
        echo "script-start event not observed after initial launch; retrying through $retry_method launch" | tee -a "$outdir/summary.txt"
        open_url_in_guest "$url" "$retry_method"
    else
        echo "script-start event not observed after local browser launch" | tee -a "$outdir/summary.txt"
    fi
fi

if ! wait_for_event "measure-start" 45; then
    echo "measure-start event not observed; collecting diagnostic screenshot and exiting" | tee -a "$outdir/summary.txt"
    if [[ "$target" == "vm" ]]; then
        capture_crash_diagnostics "measure-start-timeout"
        if "$vboxmanage" showvminfo "$vm" --machinereadable 2>/dev/null | rg -q '^VMState="running"'; then
            "$vboxmanage" controlvm "$vm" screenshotpng "$outdir/no-measure-start.png" >/dev/null 2>&1 || true
            capture_host_window_screenshot "$outdir/host-no-measure-start.png"
        fi
        cat "$outdir/crash-summary.txt" >> "$outdir/summary.txt" 2>/dev/null || true
    else
        screencapture -x "$outdir/no-measure-start.png" >/dev/null 2>&1 || true
    fi
    analyze_visuals
    cat "$outdir/visual-summary.txt" >> "$outdir/summary.txt" 2>/dev/null || true
    exit 2
fi

if [[ "$target" == "vm" ]]; then
    "$vboxmanage" debugvm "$vm" statistics --reset --pattern='*/VMSVGA/*' >/dev/null 2>&1 || true
fi
midrun_screenshot_pid=""
if [[ "$midrun_screenshot" == "1" ]]; then
    if [[ -z "$midrun_screenshot_delay" ]]; then
        midrun_screenshot_delay=1
    fi
    (
        sleep "$midrun_screenshot_delay"
        capture_run_screenshot "$outdir/measure-mid.png"
        capture_host_window_screenshot "$outdir/host-measure-mid.png"
    ) &
    midrun_screenshot_pid=$!
fi
sample_seconds="$duration"
sample_file="$outdir/VirtualBoxVM.sample.txt"
if [[ "$target" == "local" ]]; then
    sample_file="$outdir/local-browser.sample.txt"
fi
if [[ -n "$pid" ]]; then
    sample "$pid" "$sample_seconds" 5 -file "$sample_file" >/dev/null 2>"$outdir/sample.err" &
    sample_pid=$!
else
    sample_pid=""
    printf 'sample skipped: no pid\n' > "$outdir/sample.err"
fi
if [[ "$target" == "local" && -n "$pid" ]]; then
    sample_cpu_tree "$sample_seconds" "$outdir/active.cpu" "$pid" &
elif [[ "$target" == "local" ]]; then
    sample_cpu_matching "$sample_seconds" "$outdir/active.cpu" "$local_browser_process_pattern" &
else
    sample_cpu "$sample_seconds" "$outdir/active.cpu" "$pid" &
fi
cpu_pid=$!

wait "$cpu_pid" || true
if [[ -n "$sample_pid" ]]; then
    wait "$sample_pid" || true
fi
if [[ -n "$midrun_screenshot_pid" ]]; then
    wait "$midrun_screenshot_pid" || true
fi

if [[ "$target" == "vm" ]] && ! "$vboxmanage" showvminfo "$vm" --machinereadable 2>/dev/null | rg -q '^VMState="running"'; then
    capture_crash_diagnostics "measurement-vm-not-running"
fi

if ! wait_for_event "result" 15; then
    echo "result event not observed before timeout" >> "$outdir/summary.txt"
fi

if [[ "$target" == "vm" ]]; then
    "$vboxmanage" debugvm "$vm" statistics \
        --pattern='*/VMSVGA/Cmd/*|*/VMSVGA/DX/*|*/VMSVGA/Fifo*|*/VMSVGA/Reg/Command*|*/VMSVGA/Reg/DevCap*|*/VMSVGA/Reg/Cursor*' \
        > "$outdir/vmsvga-stats.xml" 2>"$outdir/vmsvga-stats.err" || true
    "$vboxmanage" controlvm "$vm" screenshotpng "$outdir/after.png" >/dev/null 2>&1 || true
    capture_host_window_screenshot "$outdir/host-after.png"
    "$vboxmanage" showvminfo "$vm" --machinereadable \
        | rg '^(VMState|VideoMode|accelerate3d|vram|graphicscontroller|clipboard|GuestAdditionsRunLevel)=' \
        | tee "$outdir/vminfo-after.txt"
else
    capture_run_screenshot "$outdir/after.png"
fi

graphics_alert_rc=0
if [[ "$target" == "vm" ]]; then
    scan_graphics_log_delta || graphics_alert_rc=$?
    guest_cleanup_after_run
else
    local_cleanup_after_run
fi
analyze_visuals

{
    printf 'outdir=%s\n' "$outdir"
    printf 'active_cpu=%s\n' "$(summarize_cpu "$outdir/active.cpu")"
    if [[ -s "$outdir/browser-result.json" ]]; then
        python3 "$bench_py" browser-summary "$outdir/browser-result.json"
    else
        printf 'browser_result=missing\n'
    fi
    if [[ -s "$sample_file" ]]; then
        printf 'sample_file=%s\n' "$sample_file"
    else
        printf 'sample_failed=%s\n' "$(tr '\n' ' ' < "$outdir/sample.err" 2>/dev/null || true)"
    fi
    if [[ -s "$outdir/vmsvga-stats.xml" ]]; then
        rg '3dBlitSurfaceToScreenProf|3dSurfaceScreen|DefineScreen|Update|FifoWatchdogWakeUps|CommandHighWrite|CommandLowWrite|OutputTarget|ProcessPendingUpdates|Readback|StartScreenReadback' "$outdir/vmsvga-stats.xml" || true
    fi
    if [[ -s "$outdir/graphics-alerts-summary.txt" ]]; then
        cat "$outdir/graphics-alerts-summary.txt"
    fi
    if [[ -s "$outdir/visual-summary.txt" ]]; then
        cat "$outdir/visual-summary.txt"
    fi
} | tee -a "$outdir/summary.txt"

echo "$outdir"

if [[ "$graphics_alert_rc" != "0" && "$fail_on_graphics_alert" == "1" ]]; then
    exit "$graphics_alert_rc"
fi
