#!/usr/bin/env python3
import argparse
import http.server
import json
import pathlib
import socketserver
import sys
import threading
import time
import urllib.parse


class BenchmarkHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "DXMTBench/1.0"

    def log_message(self, fmt, *args):
        with self.server.log_file.open("a", encoding="utf-8") as fh:
            fh.write("%0.3f %s\n" % (time.time(), fmt % args))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        request_path = urllib.parse.urlsplit(self.path).path
        if request_path == "/" or request_path.startswith("/bench"):
            self.serve_benchmark_html()
            return
        return super().do_GET()

    def serve_benchmark_html(self):
        config = {}
        if self.server.config_file:
            if not self.server.config_file.exists():
                self.send_error(409, "benchmark config is not armed")
                return
            try:
                config = json.loads(self.server.config_file.read_text(encoding="utf-8"))
            except Exception as exc:
                self.send_error(500, "invalid benchmark config: %s" % exc)
                return
        run_id = str(config.get("run") or "")
        run_outdir = str(config.get("outdir") or "")
        requested_run = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("run", [""])[0]
        if self.server.config_file and (not run_id or not run_outdir):
            self.send_error(409, "benchmark config is not armed")
            return
        if self.server.config_file and requested_run != run_id:
            self.send_error(409, "stale benchmark request: requested run does not match current config")
            return
        if run_id and run_outdir:
            self.server.run_dirs[run_id] = pathlib.Path(run_outdir).resolve()

        html = self.server.benchmark_html.read_text(encoding="utf-8")
        injection = "<script>window.DXMTBENCH_CONFIG = %s;</script>\n" % json.dumps(config, separators=(",", ":"))
        marker = '<script>\n"use strict";'
        if marker in html:
            html = html.replace(marker, injection + marker, 1)
        else:
            html = injection + html
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            cb = int(self.headers.get("content-length", "0"))
        except ValueError:
            cb = 0
        raw = self.rfile.read(cb)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self.send_error(400, "invalid json: %s" % exc)
            return

        payload["_hostReceiveUnix"] = time.time()
        run_id = str(payload.get("runId") or "")
        if self.server.config_file and run_id not in self.server.run_dirs:
            self.send_error(409, "unknown benchmark run id")
            return
        outdir = self.server.run_dirs.get(run_id, self.server.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        events_file = outdir / "browser-events.jsonl"
        result_file = outdir / "browser-result.json"
        if self.path == "/event":
            with events_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, sort_keys=True) + "\n")
            self.send_response(204)
            self.end_headers()
            return

        if self.path == "/result":
            result_body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
            try:
                with result_file.open("x", encoding="utf-8") as fh:
                    fh.write(result_body)
            except FileExistsError:
                conflict = {
                    "event": "terminal-result-conflict",
                    "runId": run_id,
                    "_hostReceiveUnix": time.time(),
                    "payload": payload,
                }
                with self.server.result_lock:
                    with (outdir / "terminal-result-conflicts.jsonl").open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(conflict, sort_keys=True) + "\n")
                    with events_file.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(conflict, sort_keys=True) + "\n")
                self.send_error(409, "terminal result already committed")
                return
            with self.server.result_lock:
                with events_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "event": "result",
                        "runId": run_id,
                        "_hostReceiveUnix": time.time(),
                    }, sort_keys=True) + "\n")
            self.send_response(204)
            self.end_headers()
            return

        self.send_error(404, "unknown POST target")


class ThreadedTCPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--html", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--config")
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    outdir = pathlib.Path(args.outdir).resolve()
    html = pathlib.Path(args.html).resolve()
    if not html.exists():
        raise SystemExit("benchmark html not found: %s" % html)

    outdir.mkdir(parents=True, exist_ok=True)
    handler = lambda *handler_args, **handler_kwargs: BenchmarkHandler(*handler_args, directory=str(root), **handler_kwargs)
    with ThreadedTCPServer((args.bind, args.port), handler) as server:
        server.benchmark_html = html
        server.config_file = pathlib.Path(args.config).resolve() if args.config else None
        server.outdir = outdir
        server.run_dirs = {}
        server.result_lock = threading.Lock()
        server.log_file = outdir / "http-server.log"
        (outdir / "server-ready.txt").write_text("%s:%s\n" % (args.bind, args.port), encoding="utf-8")
        print("serving %s on %s:%s" % (html, args.bind, args.port), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
