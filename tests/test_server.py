import importlib.util
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dxmtbench_server", ROOT / "dxmtbench-server.py")
SERVER_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER_MODULE)


class PersistentServerRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.run_outdir = root / "current-run"
        self.html = root / "dxmtbench.html"
        self.html.write_text('<script>\n"use strict";\n</script>\n', encoding="utf-8")
        self.config = root / "config.json"
        self.config.write_text(json.dumps({"run": "current", "outdir": str(self.run_outdir)}), encoding="utf-8")
        handler = lambda *args, **kwargs: SERVER_MODULE.BenchmarkHandler(*args, directory=str(root), **kwargs)
        self.server = SERVER_MODULE.ThreadedTCPServer(("127.0.0.1", 0), handler)
        self.server.benchmark_html = self.html
        self.server.config_file = self.config
        self.server.outdir = root / "fallback"
        self.server.run_dirs = {}
        self.server.result_lock = threading.Lock()
        self.server.log_file = root / "server.log"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def test_stale_page_request_is_rejected(self):
        with urllib.request.urlopen(self.base_url + "/bench.html?run=current") as response:
            self.assertEqual(200, response.status)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(self.base_url + "/bench.html?run=stale")
        self.assertEqual(409, raised.exception.code)
        raised.exception.close()

    def test_configured_page_requires_run_id(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(self.base_url + "/bench.html")
        self.assertEqual(409, raised.exception.code)
        raised.exception.close()

    def test_unknown_result_post_is_rejected(self):
        with urllib.request.urlopen(self.base_url + "/bench.html?run=current"):
            pass
        request = urllib.request.Request(
            self.base_url + "/result",
            data=json.dumps({"runId": "unknown"}).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(409, raised.exception.code)
        raised.exception.close()

    def test_empty_config_does_not_serve_benchmark(self):
        self.config.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(self.base_url + "/bench.html?run=current")
        self.assertEqual(409, raised.exception.code)
        raised.exception.close()

    def test_first_terminal_result_is_immutable_and_duplicate_marks_failure(self):
        with urllib.request.urlopen(self.base_url + "/bench.html?run=current"):
            pass

        def post_result(payload):
            request = urllib.request.Request(
                self.base_url + "/result",
                data=json.dumps(payload).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            return urllib.request.urlopen(request)

        first = {"runId": "current", "terminalStatus": "error", "error": "first"}
        with post_result(first) as response:
            self.assertEqual(204, response.status)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            post_result({"runId": "current", "terminalStatus": "ok"})
        self.assertEqual(409, raised.exception.code)
        raised.exception.close()

        committed = json.loads((self.run_outdir / "browser-result.json").read_text(encoding="utf-8"))
        self.assertEqual("first", committed["error"])
        conflicts = (self.run_outdir / "terminal-result-conflicts.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(conflicts))
        self.assertEqual("terminal-result-conflict", json.loads(conflicts[0])["event"])


if __name__ == "__main__":
    unittest.main()
