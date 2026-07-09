#!/usr/bin/env python3
import json
import hashlib
import math
import pathlib
import shutil
import socket
import subprocess
import sys
import time
from csv import DictReader


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
            name = item.get("workload")
            metrics = item.get("metrics") or {}
            res = item.get("result") or {}
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
    active_path = outdir / "active.cpu"
    status = "ok" if rc == 0 and result_path.exists() else f"failed:{rc}"
    result = {}
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("error"):
            status = "browser-error"

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

    alerts = []
    if status != "ok":
        alerts.append({"kind": "status", "message": status})

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

    visual_primary = load_visual_primary(outdir)
    visual_mid = visual_primary.get("measure_mid", {})
    visual_mid_class = visual_mid.get("classification")
    visual_mid_source = visual_mid.get("source")
    visual_mid_signature = visual_mid.get("signature")
    visual_mid_hash = None
    visual_hash_duplicate = None
    if status == "ok" and (outdir / "visual-summary.txt").exists() and not visual_mid_class:
        alerts.append({
            "kind": "visual-primary-missing",
            "metric": "measure-mid",
            "message": "primary mid-run screenshot was not available",
        })
    elif (
        visual_mid_class
        and visual_mid_class != "visible-varied"
        and visual_mid_signature != "present"
    ):
        alerts.append({
            "kind": "visual-primary",
            "metric": "measure-mid",
            "actual": visual_mid_class,
            "source": visual_mid_source,
            "message": "primary mid-run screenshot did not show varied graphical output or the current workload/run visual signature",
        })
    if visual_mid_signature and visual_mid_signature != "present":
        alerts.append({
            "kind": "visual-primary-signature",
            "metric": "measure-mid",
            "actual": visual_mid_signature,
            "source": visual_mid_source,
            "message": "primary mid-run screenshot did not contain the current workload/run visual signature",
        })
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
    if baseline:
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
    events, latest, status_path, root, results, alerts = args
    event_line = emit_event(events, latest, {
        "event": "suite-complete",
        "suiteRoot": root,
        "results": int(results),
        "alerts": int(alerts),
    })
    pathlib.Path(status_path).write_text(f"complete results={results} alerts={alerts} suite_root={root}\n", encoding="utf-8")
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


def luma(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def classify(mean_luma, std_luma, white_ratio, black_ratio, gray_ratio):
    if white_ratio >= 0.90:
        return "blank-white"
    if black_ratio >= 0.90 and std_luma < 8:
        return "blank-black"
    if gray_ratio >= 0.90 and std_luma < 14:
        return "blank-gray"
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
        sample_pixels = sample.get_flattened_data() if hasattr(sample, "get_flattened_data") else sample.getdata()
        sample_lumas = [luma(pixel) for pixel in sample_pixels]
        sample_count = max(len(sample_lumas), 1)
        white_ratio = sum(1 for value in sample_lumas if value >= 245) / sample_count
        black_ratio = sum(1 for value in sample_lumas if value <= 12) / sample_count
        gray_ratio = sum(1 for value in sample_lumas if 170 <= value <= 235) / sample_count
        return {
            "mean_rgb": [round(v, 2) for v in mean_rgb],
            "stddev_rgb": [round(v, 2) for v in std_rgb],
            "mean_luma": round(mean_luma, 2),
            "stddev_luma": round(std_luma, 2),
            "white_ratio": round(white_ratio, 4),
            "black_ratio": round(black_ratio, 4),
            "gray_ratio": round(gray_ratio, 4),
            "unique_colors_sampled": unique_sampled,
            "classification": classify(mean_luma, std_luma, white_ratio, black_ratio, gray_ratio),
        }

    def content_region(img):
        w, h = img.size
        if w >= 1000 and h >= 700:
            left = min(max(700, w // 3), max(w - 1, 0))
            top = min(max(380, h // 3), max(h - 1, 0))
            right = max(left + 1, w - max(40, w // 32))
            bottom = max(top + 1, h - max(320, h // 4))
            return img.crop((left, top, right, bottom))
        return img.crop((w // 4, h // 4, max(w // 4 + 1, 3 * w // 4), max(h // 4 + 1, 3 * h // 4)))

    def signature_for(img):
        if not expected_signature:
            return {"available": False}

        def count_in(sample_img):
            sample = sample_img.copy()
            sample.thumbnail((1280, 1280))
            counts = [0 for _ in expected_signature]
            max_distance_sq = 72 * 72
            pixels = sample.get_flattened_data() if hasattr(sample, "get_flattened_data") else sample.getdata()
            for r, g, b in pixels:
                for i, (er, eg, eb) in enumerate(expected_signature):
                    if (r - er) * (r - er) + (g - eg) * (g - eg) + (b - eb) * (b - eb) <= max_distance_sq:
                        counts[i] += 1
            return counts

        w, h = img.size
        region = img.crop((
            max(0, w - max(520, w // 3)),
            max(0, h - max(360, h // 3)),
            w,
            h,
        ))
        counts = count_in(region)
        min_count = 180
        hits = sum(1 for count in counts if count >= min_count)
        source = "bottom-right"
        if hits < 3:
            full_counts = count_in(img)
            full_hits = sum(1 for count in full_counts if count >= min_count)
            if full_hits > hits:
                counts = full_counts
                hits = full_hits
                source = "full"
        return {
            "available": True,
            "present": hits >= 3,
            "hits": hits,
            "total": len(expected_signature),
            "counts": counts,
            "source": source,
            "expected": [list(color) for color in expected_signature],
        }

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
            img = Image.open(path).convert("RGB")
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
            "signature": signature_for(img),
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
    "suite-workload-start": cmd_suite_workload_start,
    "suite-workload-result": cmd_suite_workload_result,
    "suite-complete": cmd_suite_complete,
    "write-config": cmd_write_config,
    "crash-diagnostics": cmd_crash_diagnostics,
    "analyze-visuals": cmd_analyze_visuals,
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
