#!/usr/bin/env python3
import json
import hashlib
import io
import itertools
import math
import pathlib
import shutil
import socket
import subprocess
import sys
import time
from csv import DictReader


SUITE_TSV_COLUMNS = (
    "workload", "status", "mode", "sync_every", "fps_avg", "frame_ms_p95",
    "gpu_timer_usable", "gpu_ms_p95", "gpu_samples", "active_cpu_avg", "canvas",
    "framebuffer_probe_ok", "visual_primary_measure_mid", "visual_primary_signature",
    "baseline_eligible", "alert_count", "draws_per_frame", "vertices_per_frame",
    "triangles_per_frame", "pixels_per_frame", "stencil_pixels_per_frame",
    "texture_samples_per_frame", "fb_binds_per_frame", "state_changes_per_frame",
    "estimated_mib_per_second", "fb_write_mib_per_second", "texture_mib_per_second",
    "clear_mib_per_second", "render_target_mib_per_second", "stencil_mib_per_second",
    "upload_mib_per_second", "outdir",
)


def emit_event(events, latest, event):
    event.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    line = json.dumps(event, sort_keys=True, separators=(",", ":"))
    pathlib.Path(events).open("a", encoding="utf-8").write(line + "\n")
    pathlib.Path(latest).write_text(line + "\n", encoding="utf-8")
    return line


def cmd_find_port(_args):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
    sock.close()


def cmd_reset_vm(args):
    vboxmanage, vm, settle_s = args
    settle = max(0.0, float(settle_s))
    subprocess.run([vboxmanage, "controlvm", vm, "reset"], check=True, timeout=30)
    deadline = time.time() + 180
    while time.time() < deadline:
        info = subprocess.run(
            [vboxmanage, "showvminfo", vm, "--machinereadable"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        ).stdout
        values = {}
        for line in info.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in {"VMState", "GuestAdditionsRunLevel", "VideoMode"}:
                values[key] = value.strip('"')

        props = subprocess.run(
            [vboxmanage, "guestproperty", "enumerate", vm],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).stdout
        net_status = ""
        net_ip = ""
        for line in props.splitlines():
            if "/VirtualBox/GuestInfo/Net/0/Status" in line:
                net_status = line.split("= '", 1)[-1].split("'", 1)[0]
            elif "/VirtualBox/GuestInfo/Net/0/V4/IP" in line:
                net_ip = line.split("= '", 1)[-1].split("'", 1)[0]

        print(" ".join(f"{key}={value}" for key, value in values.items()) + f" net={net_status} ip={net_ip}", flush=True)
        if (
            values.get("VMState") == "running"
            and values.get("GuestAdditionsRunLevel") == "3"
            and net_status == "Up"
            and net_ip.startswith("10.0.2.")
        ):
            if settle:
                time.sleep(settle)
            return
        time.sleep(2)

    print("guest_ready_timeout=1", flush=True)
    raise SystemExit(1)


def cmd_suite_start(args):
    events, latest, root, total, mode, baseline = args
    emit_event(events, latest, {
        "event": "suite-start",
        "suiteRoot": root,
        "total": int(total),
        "mode": mode,
        "baseline": baseline or None,
    })


def cmd_suite_header(_args):
    print("\t".join(SUITE_TSV_COLUMNS))


def cmd_suite_workload_start(args):
    events, latest, root, workload, index, total, outdir = args
    emit_event(events, latest, {
        "event": "suite-workload-start",
        "suiteRoot": root,
        "workload": workload,
        "index": int(index),
        "total": int(total),
        "outdir": outdir,
    })


def parse_float(value):
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def framebuffer_probe_coverage_ratio(probe, ratio_key, sample_key):
    ratio = parse_float(probe.get(ratio_key))
    if ratio is not None and math.isfinite(ratio) and 0.0 <= ratio <= 1.0:
        return ratio

    sample_count = parse_float(probe.get("sampleCount"))
    covered_samples = parse_float(probe.get(sample_key))
    if (
        sample_count is None
        or covered_samples is None
        or not math.isfinite(sample_count)
        or not math.isfinite(covered_samples)
        or sample_count <= 0
        or covered_samples < 0
        or covered_samples > sample_count
    ):
        return None
    return covered_samples / sample_count


def framebuffer_probe_valid(probe):
    if not isinstance(probe, dict):
        return False

    chromatic_ratio = framebuffer_probe_coverage_ratio(
        probe, "chromaticRatio", "chromaticSamples"
    )
    non_dominant_ratio = framebuffer_probe_coverage_ratio(
        probe, "nonDominantRatio", "nonDominantSamples"
    )
    return (
        probe.get("ok") is True
        and probe.get("nonUniform") is True
        and chromatic_ratio is not None
        and chromatic_ratio >= 0.005
        and non_dominant_ratio is not None
        and non_dominant_ratio >= 0.005
        and not bool(probe.get("errors"))
        and probe.get("errorsBefore") == []
        and probe.get("errorsAfter") == []
        and not bool(probe.get("contextLost"))
        and probe.get("contextLostBefore") is False
        and probe.get("contextLostAfter") is False
    )


def load_baseline(path_s):
    if not path_s:
        return {}
    path = pathlib.Path(path_s)
    if path.is_dir():
        if (path / "suite-results.jsonl").exists():
            path = path / "suite-results.jsonl"
        elif (path / "suite-summary.tsv").exists():
            path = path / "suite-summary.tsv"
    if not path.exists():
        return {}
    out = {}
    if path.suffix == ".tsv":
        with path.open(encoding="utf-8") as fh:
            for row in DictReader(fh, delimiter="\t"):
                if (
                    row.get("status") != "ok"
                    or row.get("framebuffer_probe_ok") != "1"
                    or row.get("visual_primary_measure_mid") != "visible-varied"
                    or row.get("visual_primary_signature") != "present"
                    or row.get("baseline_eligible") != "1"
                    or row.get("alert_count") != "0"
                ):
                    continue
                name = row.get("workload")
                if name:
                    out[name] = {
                        "fpsAvg": parse_float(row.get("fps_avg")),
                        "frameMsP95": parse_float(row.get("frame_ms_p95")),
                        "activeCpuAvg": parse_float(row.get("active_cpu_avg")),
                    }
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if item.get("status") != "ok" or item.get("alerts"):
                continue
            name = item.get("workload")
            metrics = item.get("metrics")
            res = item.get("result")
            if not isinstance(metrics, dict):
                continue
            if not isinstance(res, dict):
                res = {}
            probes = (
                metrics.get("framebufferProbe"),
                res.get("framebufferProbe"),
            )
            if (
                not any(framebuffer_probe_valid(probe) for probe in probes)
                or metrics.get("visualPrimaryMeasureMid") != "visible-varied"
                or metrics.get("visualPrimaryMeasureMidSignature") != "present"
            ):
                continue
            if name:
                out[name] = {
                    "fpsAvg": parse_float(metrics.get("fpsAvg", res.get("fpsAvg"))),
                    "frameMsP95": parse_float(metrics.get("frameMsP95", res.get("frameMsP95"))),
                    "activeCpuAvg": parse_float(metrics.get("activeCpuAvg")),
                }
    return out


def value_or_dash(value, fmt=None):
    if value is None or value == "":
        return "-"
    if fmt:
        try:
            return fmt % value
        except TypeError:
            return "-"
    return str(value)


def load_visual_primary(path):
    summary = pathlib.Path(path) / "visual-summary.txt"
    if not summary.exists():
        return {}
    values = {}
    for line in summary.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("visual_primary_") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        name = key.removeprefix("visual_primary_")
        parts = raw.split()
        if not parts:
            continue
        item = {"classification": parts[0], "raw": raw}
        for part in parts[1:]:
            if "=" not in part:
                continue
            pkey, pvalue = part.split("=", 1)
            item[pkey] = pvalue
        values[name] = item
    return values


def assess_visual_primary(path, required=False):
    outdir = pathlib.Path(path)
    visual_primary = load_visual_primary(outdir)
    visual_mid = visual_primary.get("measure_mid", {})
    classification = visual_mid.get("classification")
    signature = visual_mid.get("signature")
    source = visual_mid.get("source")
    alerts = []

    if not required and not (outdir / "visual-summary.txt").exists():
        return alerts, visual_mid
    if not classification:
        alerts.append({
            "kind": "visual-primary-missing",
            "metric": "measure-mid",
            "message": "primary mid-run screenshot was not available",
        })
    elif classification != "visible-varied":
        alerts.append({
            "kind": "visual-primary",
            "metric": "measure-mid",
            "actual": classification,
            "source": source,
            "message": "primary mid-run screenshot did not show varied graphical output",
        })
    if signature != "present":
        alerts.append({
            "kind": "visual-primary-signature",
            "metric": "measure-mid",
            "actual": signature or "missing",
            "source": source,
            "message": "primary mid-run screenshot did not contain the current workload/run visual signature",
        })
    return alerts, visual_mid


def assess_framebuffer_probe(result):
    probe = result.get("framebufferProbe") if isinstance(result, dict) else None
    if not isinstance(probe, dict):
        return [{
            "kind": "framebuffer-probe-missing",
            "metric": "framebufferProbe",
            "message": "browser result did not include a framebuffer correctness probe",
        }]
    if not framebuffer_probe_valid(probe):
        return [{
            "kind": "framebuffer-probe",
            "metric": "framebufferProbe",
            "actual": probe.get("classification") or probe.get("reason") or "invalid",
            "message": "browser-side framebuffer probe did not show varied graphical output",
        }]
    return []


def validate_run_artifacts(path, require_visual=True):
    outdir = pathlib.Path(path)
    errors = []
    expected_run_id = None
    artifact_config = {}
    config_path = outdir / "bench-config.json"
    if config_path.exists():
        try:
            artifact_config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append({"kind": "bench-config-invalid", "message": str(exc)})
        else:
            expected_run_id = str(artifact_config.get("run") or "")
            if not expected_run_id:
                errors.append({
                    "kind": "bench-config-run-missing",
                    "message": "bench-config.json did not identify the current run",
                })
    conflict_path = outdir / "terminal-result-conflicts.jsonl"
    if conflict_path.exists() and conflict_path.stat().st_size > 0:
        errors.append({
            "kind": "terminal-result-conflict",
            "message": "more than one terminal result was posted for this run",
        })
    result_path = outdir / "browser-result.json"
    result = {}
    if not result_path.exists():
        errors.append({"kind": "browser-result-missing", "message": "browser-result.json was not created"})
    else:
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append({"kind": "browser-result-invalid", "message": str(exc)})
        else:
            if expected_run_id and str(result.get("runId") or "") != expected_run_id:
                errors.append({
                    "kind": "browser-result-run-mismatch",
                    "message": (
                        "browser-result.json belongs to run %r, expected %r"
                        % (result.get("runId"), expected_run_id)
                    ),
                })
            terminal_status = result.get("terminalStatus")
            if terminal_status != "ok":
                errors.append({
                    "kind": "browser-terminal-status",
                    "message": "browser result terminal status was %r, expected 'ok'" % terminal_status,
                })
            if result.get("error"):
                errors.append({"kind": "browser-error", "message": str(result["error"])})
            if terminal_status == "ok" and not result.get("error"):
                errors.extend(assess_framebuffer_probe(result))
                frames = parse_float(result.get("frames"))
                fps = parse_float(result.get("fpsAvg"))
                if frames is None or frames <= 0 or fps is None or fps <= 0:
                    errors.append({
                        "kind": "measurement-empty",
                        "message": "browser result did not contain positive measured frames and FPS",
                    })
                config = result.get("config")
                if not isinstance(config, dict):
                    errors.append({
                        "kind": "browser-config-missing",
                        "message": "browser result did not include its effective configuration",
                    })
                    config = {}
                for key in ("expectedCanvasWidth", "expectedCanvasHeight"):
                    if key not in artifact_config:
                        continue
                    authoritative = parse_float(artifact_config.get(key))
                    echoed = parse_float(config.get(key))
                    if authoritative is None or echoed != authoritative:
                        errors.append({
                            "kind": "browser-config-mismatch",
                            "message": (
                                f"browser config {key}={config.get(key)!r} did not match "
                                f"bench-config.json value {artifact_config.get(key)!r}"
                            ),
                        })
                canvas = result.get("canvas") or {}
                expected_width = parse_float(
                    artifact_config.get("expectedCanvasWidth", config.get("expectedCanvasWidth"))
                ) or 0
                expected_height = parse_float(
                    artifact_config.get("expectedCanvasHeight", config.get("expectedCanvasHeight"))
                ) or 0
                actual_width = parse_float(canvas.get("width")) or 0
                actual_height = parse_float(canvas.get("height")) or 0
                if actual_width < expected_width or actual_height < expected_height:
                    errors.append({
                        "kind": "canvas-size",
                        "message": (
                            f"canvas {actual_width:.0f}x{actual_height:.0f} was below required "
                            f"{expected_width:.0f}x{expected_height:.0f}"
                        ),
                    })
    if require_visual:
        visual_errors, _visual_mid = assess_visual_primary(outdir, required=True)
        errors.extend(visual_errors)
    return errors, result


def cmd_validate_run(args):
    outdir = pathlib.Path(args[0])
    require_visual = len(args) < 2 or args[1] == "1"
    errors, result = validate_run_artifacts(outdir, require_visual=require_visual)
    probe = result.get("framebufferProbe", {}) if isinstance(result, dict) else {}
    if errors:
        print(f"functional_validation=failed errors={len(errors)}")
        for error in errors:
            print("functional_error=%s message=%s" % (error.get("kind", "unknown"), error.get("message", "")))
        raise SystemExit(4)
    print(
        "functional_validation=ok framebuffer=%s samples=%s unique_colors=%s luma_range=%s checksum=%s visual_required=%s"
        % (
            probe.get("classification", ""),
            probe.get("sampleCount", ""),
            probe.get("uniqueColors", ""),
            probe.get("lumaRange", ""),
            probe.get("checksum", ""),
            int(require_visual),
        )
    )


def cmd_suite_workload_result(args):
    (
        workload,
        outdir_s,
        rc_s,
        tsv_path_s,
        jsonl_path_s,
        events_path_s,
        alerts_path_s,
        latest_path_s,
        status_path_s,
        suite_root,
        baseline_path_s,
        fps_regression_pct_s,
        frame_ms_regression_pct_s,
        cpu_regression_pct_s,
        suite_print,
        index_s,
        total_s,
    ) = args
    outdir = pathlib.Path(outdir_s)
    rc = int(rc_s)
    tsv_path = pathlib.Path(tsv_path_s)
    jsonl_path = pathlib.Path(jsonl_path_s)
    events_path = pathlib.Path(events_path_s)
    alerts_path = pathlib.Path(alerts_path_s)
    latest_path = pathlib.Path(latest_path_s)
    status_path = pathlib.Path(status_path_s)
    index = int(index_s)
    total = int(total_s)
    fps_regression_pct = float(fps_regression_pct_s)
    frame_ms_regression_pct = float(frame_ms_regression_pct_s)
    cpu_regression_pct = float(cpu_regression_pct_s)

    result_path = outdir / "browser-result.json"
    conflict_path = outdir / "terminal-result-conflicts.jsonl"
    active_path = outdir / "active.cpu"
    status = "ok" if rc == 0 and result_path.exists() else f"failed:{rc}"
    if conflict_path.exists() and conflict_path.stat().st_size > 0:
        status = "terminal-result-conflict"
    result = {}
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("terminalStatus") != "ok" or result.get("error"):
            status = "browser-error"

    visual_required = (outdir / "visual-summary.txt").exists()
    visual_alerts, visual_mid = assess_visual_primary(outdir, required=visual_required)
    framebuffer_alerts = []
    if result and result.get("terminalStatus") == "ok" and not result.get("error"):
        framebuffer_alerts = assess_framebuffer_probe(result)
    functional_alerts = framebuffer_alerts + visual_alerts
    if status == "ok" and functional_alerts:
        status = "functional-failure"

    visual_mid_class = visual_mid.get("classification")
    visual_mid_source = visual_mid.get("source")
    visual_mid_signature = visual_mid.get("signature")

    cpu_avg = ""
    if active_path.exists():
        vals = []
        for line in active_path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                try:
                    vals.append(float(parts[1]))
                except ValueError:
                    pass
        if vals:
            cpu_avg = f"{sum(vals) / len(vals):.1f}"

    estimated = result.get("estimated", {})
    throughput = result.get("throughput", {})
    gpu = result.get("gpuTimer", {})
    canvas = result.get("canvas", {})
    canvas_s = ""
    if canvas:
        canvas_s = f"{canvas.get('width')}x{canvas.get('height')}@{canvas.get('dpr')}"
        if canvas.get("capped"):
            canvas_s += f"(capped:{canvas.get('rawDpr')}->{canvas.get('dpr')})"

    pixels_per_frame = sum(estimated.get(key, 0) or 0 for key in (
        "clearPixelsPerFrame",
        "colorPixelsPerFrame",
        "depthPixelsPerFrame",
    ))
    fps_avg = parse_float(result.get("fpsAvg")) if result else None
    frame_ms_p95 = parse_float(result.get("frameMsP95")) if result else None
    active_cpu_avg = parse_float(cpu_avg)
    mode = str(result.get("config", {}).get("schedulerMode", "-") if result else "-")
    sync_every = str(result.get("config", {}).get("syncEvery", "-") if result else "-")

    alerts = []
    if status != "ok":
        alerts.append({"kind": "status", "message": status})
    alerts.extend(functional_alerts)

    summary_text = ""
    summary_path = outdir / "summary.txt"
    if summary_path.exists():
        summary_text = summary_path.read_text(encoding="utf-8", errors="replace")
    browser_events_path = outdir / "browser-events.jsonl"
    has_browser_events = browser_events_path.exists() and browser_events_path.stat().st_size > 0
    if status != "ok" and "script-start event not observed" in summary_text and not has_browser_events:
        alerts.append({
            "kind": "transport-no-script-start",
            "message": "browser did not load the benchmark page; no script-start event was posted",
        })
    if status != "ok" and "measure-start event not observed" in summary_text:
        alerts.append({
            "kind": "browser-no-measure-start",
            "message": "benchmark page did not reach the measured rendering phase",
        })

    visual_mid_hash = None
    visual_hash_duplicate = None
    if visual_mid_source:
        visual_source_path = outdir / visual_mid_source
        if visual_source_path.exists():
            visual_mid_hash = hashlib.sha256(visual_source_path.read_bytes()).hexdigest()
            hash_path = pathlib.Path(suite_root) / "suite-visual-primary-hashes.tsv"
            if hash_path.exists():
                for line in hash_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 3 and parts[2] == visual_mid_hash and parts[0] != workload:
                        visual_hash_duplicate = {"workload": parts[0], "source": parts[1]}
                        alerts.append({
                            "kind": "visual-primary-duplicate",
                            "metric": "measure-mid",
                            "actual": visual_mid_hash[:16],
                            "source": visual_mid_source,
                            "duplicateOf": parts[0],
                            "message": "primary mid-run screenshot is byte-identical to an earlier workload",
                        })
                        break
            with hash_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{workload}\t{visual_mid_source}\t{visual_mid_hash}\n")

    baseline = load_baseline(baseline_path_s).get(workload)
    if baseline and status == "ok" and not functional_alerts:
        base_fps = baseline.get("fpsAvg")
        if fps_avg is not None and base_fps and fps_avg < base_fps * (1.0 - fps_regression_pct / 100.0):
            alerts.append({
                "kind": "fps-regression",
                "metric": "fpsAvg",
                "actual": fps_avg,
                "baseline": base_fps,
                "thresholdPct": fps_regression_pct,
            })
        base_p95 = baseline.get("frameMsP95")
        if frame_ms_p95 is not None and base_p95 and frame_ms_p95 > base_p95 * (1.0 + frame_ms_regression_pct / 100.0):
            alerts.append({
                "kind": "frame-p95-regression",
                "metric": "frameMsP95",
                "actual": frame_ms_p95,
                "baseline": base_p95,
                "thresholdPct": frame_ms_regression_pct,
            })
        base_cpu = baseline.get("activeCpuAvg")
        if active_cpu_avg is not None and base_cpu and active_cpu_avg > base_cpu * (1.0 + cpu_regression_pct / 100.0):
            alerts.append({
                "kind": "cpu-regression",
                "metric": "activeCpuAvg",
                "actual": active_cpu_avg,
                "baseline": base_cpu,
                "thresholdPct": cpu_regression_pct,
            })

    framebuffer_probe_ok = framebuffer_probe_valid(result.get("framebufferProbe"))
    baseline_eligible = (
        status == "ok"
        and not alerts
        and framebuffer_probe_ok
        and visual_mid_class == "visible-varied"
        and visual_mid_signature == "present"
    )
    row = [
        workload,
        status,
        mode,
        sync_every,
        f"{result.get('fpsAvg', 0):.2f}" if result else "-",
        f"{result.get('frameMsP95', 0):.2f}" if result else "-",
        value_or_dash(gpu.get("usable")),
        value_or_dash(gpu.get("msP95"), "%.2f"),
        value_or_dash(gpu.get("samples")),
        cpu_avg or "-",
        canvas_s or "-",
        "1" if framebuffer_probe_ok else "0",
        visual_mid_class or "-",
        visual_mid_signature or "-",
        "1" if baseline_eligible else "0",
        str(len(alerts)),
        value_or_dash(estimated.get("drawCallsPerFrame")),
        value_or_dash(estimated.get("verticesPerFrame")),
        value_or_dash(estimated.get("trianglesPerFrame")),
        value_or_dash(pixels_per_frame),
        value_or_dash(estimated.get("stencilPixelsPerFrame")),
        value_or_dash(estimated.get("textureSamplesPerFrame")),
        value_or_dash(estimated.get("framebufferBindsPerFrame")),
        value_or_dash(estimated.get("stateChangesPerFrame")),
        value_or_dash(throughput.get("estimatedMiBPerSecond"), "%.2f"),
        value_or_dash(throughput.get("colorMiBPerSecond"), "%.2f"),
        value_or_dash(throughput.get("textureSampleMiBPerSecond"), "%.2f"),
        value_or_dash(throughput.get("clearMiBPerSecond"), "%.2f"),
        value_or_dash(throughput.get("renderTargetMiBPerSecond"), "%.2f"),
        value_or_dash(throughput.get("stencilMiBPerSecond"), "%.2f"),
        value_or_dash(throughput.get("uploadMiBPerSecond"), "%.2f"),
        str(outdir),
    ]
    with tsv_path.open("a", encoding="utf-8") as fh:
        fh.write("\t".join(row) + "\n")

    metrics = {
        "fpsAvg": fps_avg,
        "frameMsP95": frame_ms_p95,
        "activeCpuAvg": active_cpu_avg,
        "mode": mode,
        "syncEvery": sync_every,
        "canvas": canvas_s or None,
        "drawCallsPerFrame": parse_float(estimated.get("drawCallsPerFrame")),
        "verticesPerFrame": parse_float(estimated.get("verticesPerFrame")),
        "trianglesPerFrame": parse_float(estimated.get("trianglesPerFrame")),
        "pixelsPerFrame": parse_float(pixels_per_frame),
        "stencilPixelsPerFrame": parse_float(estimated.get("stencilPixelsPerFrame")),
        "textureSamplesPerFrame": parse_float(estimated.get("textureSamplesPerFrame")),
        "framebufferBindsPerFrame": parse_float(estimated.get("framebufferBindsPerFrame")),
        "stateChangesPerFrame": parse_float(estimated.get("stateChangesPerFrame")),
        "estimatedMiBPerSecond": parse_float(throughput.get("estimatedMiBPerSecond")),
        "colorMiBPerSecond": parse_float(throughput.get("colorMiBPerSecond")),
        "textureSampleMiBPerSecond": parse_float(throughput.get("textureSampleMiBPerSecond")),
        "clearMiBPerSecond": parse_float(throughput.get("clearMiBPerSecond")),
        "renderTargetMiBPerSecond": parse_float(throughput.get("renderTargetMiBPerSecond")),
        "stencilMiBPerSecond": parse_float(throughput.get("stencilMiBPerSecond")),
        "uploadMiBPerSecond": parse_float(throughput.get("uploadMiBPerSecond")),
        "visualPrimaryMeasureMid": visual_mid_class,
        "visualPrimaryMeasureMidSource": visual_mid_source,
        "visualPrimaryMeasureMidSignature": visual_mid_signature,
        "visualPrimaryMeasureMidHash": visual_mid_hash[:16] if visual_mid_hash else None,
        "visualPrimaryDuplicateOf": visual_hash_duplicate,
        "framebufferProbe": result.get("framebufferProbe") if result else None,
    }
    event = {
        "event": "suite-workload-result",
        "suiteRoot": suite_root,
        "workload": workload,
        "index": index,
        "total": total,
        "status": status,
        "returncode": rc,
        "alert": bool(alerts),
        "alerts": alerts,
        "metrics": metrics,
        "baseline": baseline,
        "outdir": str(outdir),
    }
    result_record = {
        "workload": workload,
        "status": status,
        "returncode": rc,
        "outdir": str(outdir),
        "metrics": metrics,
        "baseline": baseline,
        "alerts": alerts,
        "result": result,
    }
    result_line = json.dumps(result_record, sort_keys=True, separators=(",", ":"))
    event_line = json.dumps({**event, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, sort_keys=True, separators=(",", ":"))
    with jsonl_path.open("a", encoding="utf-8") as fh:
        fh.write(result_line + "\n")
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(event_line + "\n")
    latest_path.write_text(event_line + "\n", encoding="utf-8")
    status_path.write_text(
        f"{'alert' if alerts else status} workload={workload} index={index}/{total} fps={fps_avg if fps_avg is not None else '-'} p95={frame_ms_p95 if frame_ms_p95 is not None else '-'} cpu={active_cpu_avg if active_cpu_avg is not None else '-'}\n",
        encoding="utf-8",
    )
    if alerts:
        with alerts_path.open("a", encoding="utf-8") as fh:
            fh.write(event_line + "\n")
        (outdir / "suite-alert.flag").write_text(event_line + "\n", encoding="utf-8")

    if suite_print == "events":
        print("suite_event=" + event_line)
    elif suite_print == "compact":
        alert_text = ",".join(alert["kind"] for alert in alerts) if alerts else "-"
        fps_text = f"{fps_avg:.2f}" if fps_avg is not None else "-"
        p95_text = f"{frame_ms_p95:.2f}" if frame_ms_p95 is not None else "-"
        cpu_text = f"{active_cpu_avg:.1f}" if active_cpu_avg is not None else "-"
        mib = parse_float(throughput.get("estimatedMiBPerSecond"))
        mib_text = f"{mib:.1f}" if mib is not None else "-"
        print(f"suite_result workload={workload} status={status} alert={int(bool(alerts))} fps={fps_text} p95={p95_text} cpu={cpu_text} mib={mib_text} alerts={alert_text}")


def cmd_suite_complete(args):
    events, latest, status_path, root, results, alerts, total = args
    complete = int(results) == int(total)
    event_line = emit_event(events, latest, {
        "event": "suite-complete" if complete else "suite-incomplete",
        "suiteRoot": root,
        "results": int(results),
        "alerts": int(alerts),
        "expected": int(total),
    })
    state = "complete" if complete else "incomplete"
    pathlib.Path(status_path).write_text(
        f"{state} results={results} expected={total} alerts={alerts} suite_root={root}\n",
        encoding="utf-8",
    )
    return event_line


def cmd_write_config(args):
    path = pathlib.Path(args[0])
    pairs = args[1:]
    config = {pairs[i]: pairs[i + 1] for i in range(0, len(pairs), 2)}
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cmd_crash_diagnostics(args):
    start = int(args[0])
    outdir = pathlib.Path(args[1])
    reports = sorted(
        (p for p in pathlib.Path.home().glob("Library/Logs/DiagnosticReports/VirtualBoxVM-*.ips")
         if p.stat().st_mtime >= start - 2),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    summary = outdir / "crash-summary.txt"
    if not reports:
        summary.write_text("crash_report=none\n", encoding="utf-8")
        return

    report = reports[0]
    copy = outdir / report.name
    try:
        shutil.copy2(report, copy)
    except Exception:
        copy = report

    text = report.read_text(errors="replace")
    try:
        _, payload = text.split("\n", 1)
        data = json.loads(payload)
    except Exception as exc:
        summary.write_text(f"crash_report={report}\nparse_error={exc}\n", encoding="utf-8")
        return

    images = data.get("usedImages", [])
    threads = data.get("threads", [])
    idx = data.get("faultingThread")
    thread = threads[idx] if isinstance(idx, int) and idx < len(threads) else next((t for t in threads if t.get("triggered")), {})
    lines = [
        f"crash_report={report}",
        f"crash_report_copy={copy}",
        f"exception={data.get('exception')}",
        f"termination={data.get('termination')}",
        f"faultingThread={idx}",
        f"threadName={thread.get('name')}",
        "top_frames=",
    ]
    for frame in thread.get("frames", [])[:18]:
        image_index = frame.get("imageIndex")
        image = images[image_index].get("name") if isinstance(image_index, int) and image_index < len(images) else ""
        symbol = frame.get("symbol") or ""
        location = frame.get("symbolLocation", "")
        offset = frame.get("imageOffset", "")
        lines.append(f"  {image}: {symbol} loc={location} imageOffset={offset}")
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fnv1a32(text):
    h = 0x811c9dc5
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x01000193) & 0xffffffff
    return h


def signature_colors(workload, run):
    h = fnv1a32(f"{workload}|{run}")
    colors = []
    for _ in range(4):
        h ^= (h << 13) & 0xffffffff
        h &= 0xffffffff
        h ^= h >> 17
        h &= 0xffffffff
        h ^= (h << 5) & 0xffffffff
        h &= 0xffffffff
        r = 56 + ((h >> 0) & 0xbf)
        g = 56 + ((h >> 8) & 0xbf)
        b = 56 + ((h >> 16) & 0xbf)
        if max(r, g, b) - min(r, g, b) < 72:
            r = (r + 112) & 0xff
        colors.append((r, g, b))
    return colors


def screenshot_to_srgb(img):
    profile_bytes = img.info.get("icc_profile")
    if profile_bytes:
        try:
            from PIL import ImageCms
            source_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile_bytes))
            return ImageCms.profileToProfile(
                img,
                source_profile,
                ImageCms.createProfile("sRGB"),
                outputMode="RGB",
            )
        except Exception:
            pass
    return img.convert("RGB")


def detect_visual_signature(img, expected_signature, tolerance=32):
    if not expected_signature:
        return {"available": False}

    from PIL import Image, ImageChops

    width, height = img.size
    region_box = (
        max(0, round(width * 0.50)),
        max(0, round(height * 0.55)),
        width,
        height,
    )
    region = img.crop(region_box).convert("RGB")
    region_width, region_height = region.size
    scale = min(1.0, 720.0 / max(region_width, region_height, 1))
    mask_width = max(1, round(region_width * scale))
    mask_height = max(1, round(region_height * scale))
    red, green, blue = region.split()
    channel_luts = {
        value: [255 if abs(sample - value) <= tolerance else 0 for sample in range(256)]
        for color in expected_signature
        for value in color
    }
    resampling = Image.Resampling.BOX if hasattr(Image, "Resampling") else Image.BOX

    def components_for(color):
        masks = [
            channel.point(channel_luts[value])
            for channel, value in zip((red, green, blue), color)
        ]
        mask = ImageChops.multiply(ImageChops.multiply(masks[0], masks[1]), masks[2])
        if scale < 1.0:
            mask = mask.resize((mask_width, mask_height), resampling)
        mask = mask.point(lambda value: 255 if value >= 160 else 0)
        pixels = mask.tobytes()
        visited = bytearray(len(pixels))
        components = []
        minimum_side = max(5, round(min(mask_width, mask_height) * 0.012))
        maximum_side = max(minimum_side, round(min(mask_width, mask_height) * 0.35))

        for start, value in enumerate(pixels):
            if value == 0 or visited[start]:
                continue
            visited[start] = 1
            stack = [start]
            area = 0
            min_x = mask_width
            min_y = mask_height
            max_x = 0
            max_y = 0
            while stack:
                index = stack.pop()
                y, x = divmod(index, mask_width)
                area += 1
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)
                if x > 0:
                    neighbor = index - 1
                    if pixels[neighbor] and not visited[neighbor]:
                        visited[neighbor] = 1
                        stack.append(neighbor)
                if x + 1 < mask_width:
                    neighbor = index + 1
                    if pixels[neighbor] and not visited[neighbor]:
                        visited[neighbor] = 1
                        stack.append(neighbor)
                if y > 0:
                    neighbor = index - mask_width
                    if pixels[neighbor] and not visited[neighbor]:
                        visited[neighbor] = 1
                        stack.append(neighbor)
                if y + 1 < mask_height:
                    neighbor = index + mask_width
                    if pixels[neighbor] and not visited[neighbor]:
                        visited[neighbor] = 1
                        stack.append(neighbor)

            component_width = max_x - min_x + 1
            component_height = max_y - min_y + 1
            fill_ratio = area / (component_width * component_height)
            aspect = component_width / component_height
            if (
                component_width < minimum_side
                or component_height < minimum_side
                or component_width > maximum_side
                or component_height > maximum_side
                or not 0.65 <= aspect <= 1.55
                or fill_ratio < 0.55
            ):
                continue
            components.append({
                "area": area,
                "width": component_width,
                "height": component_height,
                "centerX": (min_x + max_x) / 2,
                "centerY": (min_y + max_y) / 2,
                "right": max_x,
                "bottom": max_y,
                "fillRatio": fill_ratio,
            })

        components.sort(
            key=lambda component: component["area"]
            * (1.0 + component["centerX"] / mask_width + component["centerY"] / mask_height),
            reverse=True,
        )
        # State-churn scenes can contain many large rectangles close to a signature
        # color. Keep a bounded pool large enough that those tiles cannot crowd the
        # smaller DOM cells out before the ordered-row geometry check runs.
        return components[:32]

    candidates = [components_for(color) for color in expected_signature]
    selected = None
    selected_score = None
    if all(candidates):
        for combination in itertools.product(*candidates):
            centers_x = [component["centerX"] for component in combination]
            if centers_x != sorted(centers_x) or len(set(centers_x)) != 4:
                continue
            sides = [(component["width"] + component["height"]) / 2 for component in combination]
            typical_side = sum(sides) / len(sides)
            if max(sides) / min(sides) > 1.6:
                continue
            centers_y = [component["centerY"] for component in combination]
            if max(centers_y) - min(centers_y) > typical_side * 0.35:
                continue
            gaps = [centers_x[i + 1] - centers_x[i] for i in range(3)]
            if min(gaps) < typical_side * 0.90 or max(gaps) > typical_side * 1.80:
                continue
            if max(gaps) / min(gaps) > 1.25:
                continue
            # A fullscreen VM can letterbox a fixed 4K guest inside a larger host
            # display. Keep the row in the bottom-right half, but allow the host-side
            # margin to be substantially wider than a signature cell.
            if mask_width - combination[-1]["right"] > typical_side * 20.0:
                continue
            if mask_height - max(component["bottom"] for component in combination) > typical_side * 14.0:
                continue
            score = sum(component["area"] for component in combination)
            if selected_score is None or score > selected_score:
                selected = combination
                selected_score = score

    counts = [0, 0, 0, 0]
    geometry = None
    if selected:
        counts = [round(component["area"] / (scale * scale)) for component in selected]
        geometry = [{
            "centerX": round(region_box[0] + component["centerX"] / scale, 1),
            "centerY": round(region_box[1] + component["centerY"] / scale, 1),
            "width": round(component["width"] / scale, 1),
            "height": round(component["height"] / scale, 1),
        } for component in selected]
    return {
        "available": True,
        "present": selected is not None,
        "hits": 4 if selected else 0,
        "total": 4,
        "counts": counts,
        "source": "bottom-right-geometry",
        "expected": [list(color) for color in expected_signature],
        "tolerance": tolerance,
        "candidateColors": sum(1 for items in candidates if items),
        "geometry": geometry,
    }


def detect_visual_signature_variants(variants, expected_signature):
    results = []
    for representation, image in variants:
        result = detect_visual_signature(image, expected_signature)
        result["representation"] = representation
        results.append(result)
    if not results:
        return {"available": False}
    return max(
        results,
        key=lambda result: (
            1 if result.get("present") else 0,
            result.get("candidateColors", 0),
            sum(result.get("counts", [])),
        ),
    )


def luma(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def classify(mean_luma, std_luma, white_ratio, black_ratio, gray_ratio, chromatic_ratio):
    if white_ratio >= 0.90:
        return "blank-white"
    if black_ratio >= 0.90 and std_luma < 8:
        return "blank-black"
    if gray_ratio >= 0.90 and std_luma < 14:
        return "blank-gray"
    if chromatic_ratio < 0.001:
        return "achromatic-varied"
    if chromatic_ratio >= 0.01 and std_luma >= 6:
        return "visible-varied"
    if white_ratio >= 0.72:
        return "mostly-white"
    if black_ratio >= 0.72:
        if std_luma >= 10:
            return "visible-varied"
        return "mostly-black"
    if gray_ratio >= 0.72 and std_luma < 22:
        return "mostly-gray"
    if mean_luma < 20 and std_luma < 25:
        return "low-contrast-black"
    if std_luma < 6:
        if mean_luma < 28:
            return "blank-black"
        if mean_luma > 228:
            return "blank-white"
        return "blank-gray"
    if std_luma < 16:
        if mean_luma < 45:
            return "low-contrast-black"
        if mean_luma > 210:
            return "low-contrast-white"
        return "low-contrast-gray"
    return "visible-varied"


def content_region_box(width, height):
    left = min(width - 1, max(0, round(width * 0.35)))
    top = min(height - 1, max(0, round(height * 0.52)))
    right = max(left + 1, min(width, round(width * 0.95)))
    bottom = max(top + 1, min(height, round(height * 0.80)))
    return left, top, right, bottom


def cmd_analyze_visuals(args):
    outdir = pathlib.Path(args[0])
    target = args[1] if len(args) > 1 else "vm"
    summary_txt = outdir / "visual-summary.txt"
    summary_json = outdir / "visual-summary.json"

    try:
        from PIL import Image, ImageStat
    except Exception as exc:
        summary_txt.write_text(f"visual_analysis=unavailable reason={exc}\n", encoding="utf-8")
        summary_json.write_text(json.dumps({"available": False, "reason": str(exc)}, indent=2) + "\n", encoding="utf-8")
        return

    expected_signature = []
    config_path = outdir / "bench-config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8", errors="replace"))
            if cfg.get("workload") and cfg.get("run"):
                expected_signature = signature_colors(str(cfg["workload"]), str(cfg["run"]))
        except Exception:
            expected_signature = []

    def stats_for(img):
        stat = ImageStat.Stat(img)
        mean_rgb = tuple(float(v) for v in stat.mean[:3])
        std_rgb = tuple(float(v) for v in stat.stddev[:3])
        mean_luma = luma(mean_rgb)
        std_luma = math.sqrt(
            (0.2126 * std_rgb[0]) ** 2 +
            (0.7152 * std_rgb[1]) ** 2 +
            (0.0722 * std_rgb[2]) ** 2
        )
        sample = img.copy()
        sample.thumbnail((256, 256))
        colors = sample.getcolors(maxcolors=65536)
        unique_sampled = None if colors is None else len(colors)
        sample_pixels = list(sample.get_flattened_data() if hasattr(sample, "get_flattened_data") else sample.getdata())
        sample_lumas = [luma(pixel) for pixel in sample_pixels]
        sample_count = max(len(sample_lumas), 1)
        white_ratio = sum(1 for value in sample_lumas if value >= 245) / sample_count
        black_ratio = sum(1 for value in sample_lumas if value <= 12) / sample_count
        gray_ratio = sum(1 for value in sample_lumas if 170 <= value <= 235) / sample_count
        chromatic_ratio = sum(1 for r, g, b in sample_pixels if max(r, g, b) - min(r, g, b) >= 12) / sample_count
        return {
            "mean_rgb": [round(v, 2) for v in mean_rgb],
            "stddev_rgb": [round(v, 2) for v in std_rgb],
            "mean_luma": round(mean_luma, 2),
            "stddev_luma": round(std_luma, 2),
            "white_ratio": round(white_ratio, 4),
            "black_ratio": round(black_ratio, 4),
            "gray_ratio": round(gray_ratio, 4),
            "chromatic_ratio": round(chromatic_ratio, 4),
            "unique_colors_sampled": unique_sampled,
            "classification": classify(mean_luma, std_luma, white_ratio, black_ratio, gray_ratio, chromatic_ratio),
        }

    def content_region(img):
        return img.crop(content_region_box(*img.size))

    entries = []
    for name in (
        "before.png",
        "measure-mid.png",
        "after.png",
        "no-measure-start.png",
        "host-before.png",
        "host-measure-mid.png",
        "host-after.png",
        "host-no-measure-start.png",
    ):
        path = outdir / name
        if not path.exists():
            continue
        try:
            with Image.open(path) as source:
                encoded_img = source.convert("RGB")
                img = screenshot_to_srgb(source)
        except Exception as exc:
            entries.append({"file": name, "error": str(exc)})
            continue
        region = content_region(img)
        entries.append({
            "file": name,
            "width": img.width,
            "height": img.height,
            "full": stats_for(img),
            "content": stats_for(region),
            "signature": detect_visual_signature_variants(
                (("encoded-rgb", encoded_img), ("srgb", img)),
                expected_signature,
            ),
        })

    if not entries:
        summary_txt.write_text("visual_analysis=no_screenshots\n", encoding="utf-8")
        summary_json.write_text(json.dumps({"available": True, "screenshots": []}, indent=2) + "\n", encoding="utf-8")
        return

    lines = ["visual_analysis=ok"]
    by_file = {}
    for entry in entries:
        if "error" in entry:
            lines.append(f"visual_{entry['file']}=error reason={entry['error']}")
            continue
        by_file[entry["file"]] = entry
        key = entry["file"].removesuffix(".png").replace("-", "_")
        content = entry["content"]
        full = entry["full"]
        signature = entry.get("signature", {})
        signature_text = ""
        if signature.get("available"):
            signature_state = "present" if signature.get("present") else "absent"
            counts = ",".join(str(v) for v in signature.get("counts", []))
            signature_text = " signature=%s signature_hits=%s/%s signature_counts=%s" % (
                signature_state,
                signature.get("hits", 0),
                signature.get("total", 0),
                counts,
            )
            if signature.get("representation"):
                signature_text += " signature_space=%s" % signature["representation"]
        lines.append(
            "visual_%s=%s %sx%s content_luma=%.2f content_std=%.2f full=%s%s" % (
                key,
                content["classification"],
                entry["width"],
                entry["height"],
                content["mean_luma"],
                content["stddev_luma"],
                full["classification"],
                signature_text,
            )
        )

    primary = {}
    for stage in ("before", "measure-mid", "after", "no-measure-start"):
        candidates = [f"{stage}.png"]
        if target == "vm":
            candidates = [f"host-{stage}.png", f"{stage}.png"]
        for name in candidates:
            entry = by_file.get(name)
            if not entry:
                continue
            key = stage.replace("-", "_")
            content = entry["content"]
            signature = entry.get("signature", {})
            signature_state = None
            if signature.get("available"):
                signature_state = "present" if signature.get("present") else "absent"
            primary[key] = {
                "source": name,
                "classification": content["classification"],
                "meanLuma": content["mean_luma"],
                "stddevLuma": content["stddev_luma"],
                "signature": signature_state,
            }
            signature_text = ""
            if signature_state:
                signature_text = " signature=%s signature_hits=%s/%s" % (
                    signature_state,
                    signature.get("hits", 0),
                    signature.get("total", 0),
                )
            lines.append(
                "visual_primary_%s=%s source=%s content_luma=%.2f content_std=%.2f%s" % (
                    key,
                    content["classification"],
                    name,
                    content["mean_luma"],
                    content["stddev_luma"],
                    signature_text,
                )
            )
            break

    summary_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary_json.write_text(json.dumps({"available": True, "target": target, "primary": primary, "screenshots": entries}, indent=2) + "\n", encoding="utf-8")


def cmd_browser_summary(args):
    data = json.load(open(args[0], encoding="utf-8"))
    probe = data.get("framebufferProbe", {})
    if probe:
        print(
            "browser_framebuffer_probe=ok:%s class:%s samples:%s bins:%s chromatic:%s luma_range:%s checksum:%s"
            % (
                probe.get("ok", False),
                probe.get("classification", ""),
                probe.get("sampleCount", 0),
                probe.get("uniqueColorBins", probe.get("uniqueColors", 0)),
                probe.get("chromaticSamples", 0),
                probe.get("lumaRange", ""),
                probe.get("checksum", ""),
            )
        )
    if "error" in data:
        print("browser_error=%s" % data["error"])
        return

    print("browser_workload=%s" % data.get("workload", ""))
    print("browser_workload_description=%s" % data.get("workloadDescription", ""))
    config = data.get("config", {})
    print("browser_mode=%s" % config.get("schedulerMode", ""))
    print("browser_burst_chunk_ms=%s" % config.get("burstChunkMs", ""))
    print("browser_sync_every=%s" % config.get("syncEvery", ""))
    print("browser_fps_avg=%.1f" % data.get("fpsAvg", 0))
    print("browser_frame_ms_p50=%.2f" % data.get("frameMsP50", 0))
    print("browser_frame_ms_p95=%.2f" % data.get("frameMsP95", 0))
    print("browser_frame_ms_p99=%.2f" % data.get("frameMsP99", 0))
    gpu = data.get("gpuTimer", {})
    if gpu.get("available"):
        print("browser_gpu_timer_usable=%s" % gpu.get("usable", False))
        print("browser_gpu_timer_samples=%s" % gpu.get("samples", 0))
        print("browser_gpu_timer_nonzero_samples=%s" % gpu.get("nonZeroSamples", 0))
        print("browser_gpu_timer_zero_result_count=%s" % gpu.get("zeroResultCount", 0))
        print("browser_gpu_timer_ms_avg=%.2f" % gpu.get("msAvg", 0))
        print("browser_gpu_timer_ms_p50=%.2f" % gpu.get("msP50", 0))
        print("browser_gpu_timer_ms_p95=%.2f" % gpu.get("msP95", 0))
        print("browser_gpu_timer_ms_p99=%.2f" % gpu.get("msP99", 0))
        print("browser_gpu_timer_disjoint_count=%s" % gpu.get("disjointCount", 0))
    canvas = data.get("canvas", {})
    print("browser_canvas=%sx%s dpr=%s raw_dpr=%s capped=%s max_pixels=%s" % (
        canvas.get("width"),
        canvas.get("height"),
        canvas.get("dpr"),
        canvas.get("rawDpr"),
        canvas.get("capped"),
        canvas.get("maxCanvasPixels"),
    ))
    estimated = data.get("estimated", {})
    throughput = data.get("throughput", {})
    for key in (
        "drawCallsPerFrame",
        "verticesPerFrame",
        "trianglesPerFrame",
        "colorPixelsPerFrame",
        "depthPixelsPerFrame",
        "textureSamplesPerFrame",
        "fragmentShaderIterationsPerFrame",
        "vertexShaderIterationsPerFrame",
        "stencilPixelsPerFrame",
        "framebufferBindsPerFrame",
        "stateChangesPerFrame",
        "uploadBytesPerFrame",
        "renderTargetPixelsPerFrame",
    ):
        if key in estimated:
            print("browser_est_%s=%s" % (key, estimated[key]))
    for key in (
        "drawCallsPerSecond",
        "verticesPerSecond",
        "trianglesPerSecond",
        "colorPixelsPerSecond",
        "textureSamplesPerSecond",
        "fragmentShaderIterationsPerSecond",
        "vertexShaderIterationsPerSecond",
        "stencilPixelsPerSecond",
        "framebufferBindsPerSecond",
        "stateChangesPerSecond",
        "estimatedMiBPerSecond",
        "colorMiBPerSecond",
        "clearMiBPerSecond",
        "textureSampleMiBPerSecond",
        "renderTargetMiBPerSecond",
        "stencilMiBPerSecond",
        "uploadMiBPerSecond",
    ):
        if key in throughput:
            print("browser_throughput_%s=%.2f" % (key, throughput[key]))
    gl = data.get("gl", {})
    print("browser_gl_renderer=%s" % (gl.get("unmaskedRenderer") or gl.get("renderer") or ""))
    print("browser_timer_query_available=%s" % gl.get("timerQueryAvailable", ""))
    runtime = data.get("runtime", {})
    host = runtime.get("host", {})
    if runtime:
        print("browser_target=%s" % runtime.get("target", ""))
    if host:
        print("browser_host=%sCPU %sMB_RAM local_browser=%s" % (
            host.get("cpus", ""),
            host.get("memoryMb", ""),
            host.get("localBrowser", ""),
        ))
    vm = runtime.get("vm", {})
    if vm and any(vm.get(key) for key in ("cpus", "memoryMb", "vramMb", "graphics", "accelerate3d")):
        print("browser_vm=%svcpu %sMB_RAM %sMB_VRAM graphics=%s 3d=%s" % (
            vm.get("cpus", ""),
            vm.get("memoryMb", ""),
            vm.get("vramMb", ""),
            vm.get("graphics", ""),
            vm.get("accelerate3d", ""),
        ))
    if runtime:
        print("browser_runtime=browser_cpus=%s device_memory_gb=%s" % (
            runtime.get("browserCpus", ""),
            runtime.get("browserDeviceMemoryGb", ""),
        ))


COMMANDS = {
    "find-port": cmd_find_port,
    "reset-vm": cmd_reset_vm,
    "suite-start": cmd_suite_start,
    "suite-header": cmd_suite_header,
    "suite-workload-start": cmd_suite_workload_start,
    "suite-workload-result": cmd_suite_workload_result,
    "suite-complete": cmd_suite_complete,
    "write-config": cmd_write_config,
    "crash-diagnostics": cmd_crash_diagnostics,
    "analyze-visuals": cmd_analyze_visuals,
    "validate-run": cmd_validate_run,
    "browser-summary": cmd_browser_summary,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("usage: dxmtbench.py <command> [args...]", file=sys.stderr)
        print("commands: " + ", ".join(sorted(COMMANDS)), file=sys.stderr)
        raise SystemExit(64)
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
