#!/usr/bin/env python3
import argparse
import http.server
import json
import pathlib
import socketserver
import sys
import time


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
        if self.path == "/" or self.path.startswith("/bench"):
            self.path = "/" + self.server.benchmark_html.name
        return super().do_GET()

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
        if self.path == "/event":
            with self.server.events_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, sort_keys=True) + "\n")
            self.send_response(204)
            self.end_headers()
            return

        if self.path == "/result":
            self.server.result_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.server.events_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"event": "result", "_hostReceiveUnix": time.time()}, sort_keys=True) + "\n")
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
        server.events_file = outdir / "browser-events.jsonl"
        server.result_file = outdir / "browser-result.json"
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
