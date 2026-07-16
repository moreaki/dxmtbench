import importlib.util
import json
import pathlib
import tempfile
import unittest
from csv import DictReader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dxmtbench", ROOT / "dxmtbench.py")
DXMTBENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DXMTBENCH)


class FunctionalValidationTests(unittest.TestCase):
    def valid_probe(self, **updates):
        probe = {
            "ok": True,
            "nonUniform": True,
            "classification": "visible-varied",
            "sampleCount": 4096,
            "uniqueColors": 128,
            "minimumCoverageRatio": 0.005,
            "chromaticRatio": 0.25,
            "nonDominantRatio": 0.30,
            "errorsBefore": [],
            "errorsAfter": [],
            "contextLostBefore": False,
            "contextLostAfter": False,
            "lumaRange": 90.0,
            "checksum": "1234abcd",
        }
        probe.update(updates)
        return probe

    def write_visual(self, outdir, classification, signature):
        (outdir / "visual-summary.txt").write_text(
            "visual_analysis=ok\n"
            f"visual_primary_measure_mid={classification} source=measure-mid.png signature={signature}\n",
            encoding="utf-8",
        )

    def write_result(self, outdir, **updates):
        result = {
            "frames": 60,
            "fpsAvg": 60.0,
            "terminalStatus": "ok",
            "config": {},
            "framebufferProbe": self.valid_probe(),
        }
        result.update(updates)
        (outdir / "browser-result.json").write_text(json.dumps(result), encoding="utf-8")

    def test_blank_pixels_fail_even_when_signature_is_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = pathlib.Path(tmp)
            self.write_visual(outdir, "blank-white", "present")
            alerts, _visual = DXMTBENCH.assess_visual_primary(outdir, required=True)
            self.assertIn("visual-primary", {alert["kind"] for alert in alerts})
            self.assertNotIn("visual-primary-signature", {alert["kind"] for alert in alerts})

    def test_missing_signature_fails_even_when_pixels_are_varied(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = pathlib.Path(tmp)
            self.write_visual(outdir, "visible-varied", "absent")
            alerts, _visual = DXMTBENCH.assess_visual_primary(outdir, required=True)
            self.assertEqual(["visual-primary-signature"], [alert["kind"] for alert in alerts])

    def test_varied_pixels_and_current_signature_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = pathlib.Path(tmp)
            self.write_visual(outdir, "visible-varied", "present")
            alerts, _visual = DXMTBENCH.assess_visual_primary(outdir, required=True)
            self.assertEqual([], alerts)

    def test_achromatic_variation_is_not_accepted_as_rendered_scene(self):
        classification = DXMTBENCH.classify(
            mean_luma=128,
            std_luma=55,
            white_ratio=0.15,
            black_ratio=0.15,
            gray_ratio=0.25,
            chromatic_ratio=0.0,
        )
        self.assertEqual("achromatic-varied", classification)

    def test_chromatic_low_luma_variation_is_visible(self):
        classification = DXMTBENCH.classify(
            mean_luma=94,
            std_luma=9.5,
            white_ratio=0.0,
            black_ratio=0.0,
            gray_ratio=0.0,
            chromatic_ratio=1.0,
        )
        self.assertEqual("visible-varied", classification)

    def test_content_crop_remains_meaningful_at_target_resolutions(self):
        for width, height in ((1280, 720), (1920, 1080), (3840, 2160)):
            with self.subTest(size=(width, height)):
                left, top, right, bottom = DXMTBENCH.content_region_box(width, height)
                self.assertGreaterEqual(right - left, round(width * 0.59))
                self.assertGreaterEqual(bottom - top, round(height * 0.27))
                self.assertGreaterEqual(top, round(height * 0.51))
                self.assertLessEqual(bottom, round(height * 0.81))

    def test_signature_detector_requires_correct_ordered_cells(self):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow unavailable")
        image = Image.new("RGB", (1280, 720), (12, 18, 28))
        draw = ImageDraw.Draw(image)
        palette = ((30, 80, 140), (120, 45, 90), (40, 135, 75), (150, 110, 35))
        for y in range(0, 720, 48):
            for x in range(0, 1280, 64):
                draw.rectangle((x, y, x + 63, y + 47), fill=palette[(x // 64 + y // 48) % len(palette)])
        expected = DXMTBENCH.signature_colors("clear", "current-run")
        start_x = 1280 - 20 - (4 * 56 + 3 * 10)
        start_y = 720 - 20 - 56
        for index, color in enumerate(expected):
            left = start_x + index * 66
            draw.rectangle((left, start_y, left + 55, start_y + 55), fill=color)

        current = DXMTBENCH.detect_visual_signature(image, expected)
        stale = DXMTBENCH.detect_visual_signature(
            image,
            DXMTBENCH.signature_colors("clear", "stale-run"),
        )
        self.assertTrue(current["present"])
        self.assertFalse(stale["present"])

    def test_signature_detector_accepts_color_shifted_letterboxed_vm_view(self):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow unavailable")
        image = Image.new("RGB", (1600, 1000), (245, 245, 245))
        draw = ImageDraw.Draw(image)
        draw.rectangle((260, 100, 1340, 820), fill=(8, 14, 24))
        expected = DXMTBENCH.signature_colors("clear", "letterboxed-run")
        start_x = 1340 - 20 - (4 * 40 + 3 * 8)
        start_y = 820 - 20 - 40
        for index, color in enumerate(expected):
            shifted = tuple(max(0, min(255, value + delta)) for value, delta in zip(color, (28, -20, 16)))
            left = start_x + index * 48
            draw.rectangle((left, start_y, left + 39, start_y + 39), fill=shifted)

        current = DXMTBENCH.detect_visual_signature(image, expected)
        stale = DXMTBENCH.detect_visual_signature(
            image,
            DXMTBENCH.signature_colors("clear", "another-run"),
        )
        self.assertTrue(current["present"])
        self.assertFalse(stale["present"])

    def test_signature_detector_prefers_exact_encoded_colors_over_clipped_profile_colors(self):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow unavailable")
        encoded = Image.new("RGB", (1280, 720), (10, 16, 24))
        clipped = encoded.copy()
        encoded_draw = ImageDraw.Draw(encoded)
        clipped_draw = ImageDraw.Draw(clipped)
        expected = DXMTBENCH.signature_colors("dynamic-buffer", "encoded-host-run")
        start_x = 1280 - 20 - (4 * 56 + 3 * 10)
        start_y = 720 - 20 - 56
        for index, color in enumerate(expected):
            left = start_x + index * 66
            encoded_draw.rectangle((left, start_y, left + 55, start_y + 55), fill=color)
            shifted = tuple(max(0, value - 80) for value in color)
            clipped_draw.rectangle((left, start_y, left + 55, start_y + 55), fill=shifted)

        result = DXMTBENCH.detect_visual_signature_variants(
            (("srgb", clipped), ("encoded-rgb", encoded)),
            expected,
        )
        self.assertTrue(result["present"])
        self.assertEqual("encoded-rgb", result["representation"])

    def test_signature_detector_keeps_cells_behind_larger_color_distractors(self):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow unavailable")
        image = Image.new("RGB", (1600, 1000), (18, 24, 34))
        draw = ImageDraw.Draw(image)
        expected = DXMTBENCH.signature_colors("d3d11-state-heavy", "distractor-run")
        for index in range(13):
            column = index % 5
            row = index // 5
            left = 820 + column * 130
            top = 570 + row * 100
            draw.rectangle((left, top, left + 47, top + 47), fill=expected[0])

        cell_side = 30
        cell_gap = 7
        start_x = 1600 - 20 - (4 * cell_side + 3 * cell_gap)
        start_y = 1000 - 20 - cell_side
        for index, color in enumerate(expected):
            left = start_x + index * (cell_side + cell_gap)
            draw.rectangle((left, start_y, left + cell_side - 1, start_y + cell_side - 1), fill=color)

        result = DXMTBENCH.detect_visual_signature(image, expected)
        self.assertTrue(result["present"])
        self.assertEqual(4, result["hits"])

    def test_valid_browser_probe_and_visual_evidence_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = pathlib.Path(tmp)
            self.write_result(outdir)
            self.write_visual(outdir, "visible-varied", "present")
            errors, _result = DXMTBENCH.validate_run_artifacts(outdir, require_visual=True)
            self.assertEqual([], errors)

    def test_uniform_browser_probe_fails_without_visual_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = pathlib.Path(tmp)
            self.write_result(outdir, framebufferProbe={
                "ok": False,
                "nonUniform": False,
                "classification": "uniform",
                "reason": "uniform-framebuffer",
            })
            errors, _result = DXMTBENCH.validate_run_artifacts(outdir, require_visual=False)
            self.assertIn("framebuffer-probe", {error["kind"] for error in errors})

    def test_four_colored_speckles_do_not_pass_framebuffer_probe(self):
        result = {
            "framebufferProbe": {
                "ok": True,
                "nonUniform": True,
                "classification": "visible-varied",
                "minimumCoverageRatio": 0.005,
                "chromaticRatio": 4 / 262144,
                "nonDominantRatio": 4 / 262144,
            }
        }
        alerts = DXMTBENCH.assess_framebuffer_probe(result)
        self.assertEqual(["framebuffer-probe"], [alert["kind"] for alert in alerts])

    def test_framebuffer_probe_accepts_sample_count_coverage_fallback(self):
        probe = self.valid_probe(
            chromaticRatio=None,
            nonDominantRatio=None,
            sampleCount=10000,
            chromaticSamples=50,
            nonDominantSamples=51,
        )
        self.assertTrue(DXMTBENCH.framebuffer_probe_valid(probe))
        probe["chromaticSamples"] = 49
        self.assertFalse(DXMTBENCH.framebuffer_probe_valid(probe))

    def test_framebuffer_probe_requires_explicit_clean_error_and_context_state(self):
        probe = self.valid_probe()
        del probe["errorsAfter"]
        self.assertFalse(DXMTBENCH.framebuffer_probe_valid(probe))
        probe["errorsAfter"] = []
        probe["contextLostAfter"] = True
        self.assertFalse(DXMTBENCH.framebuffer_probe_valid(probe))

    def test_missing_browser_probe_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = pathlib.Path(tmp)
            self.write_result(outdir, framebufferProbe=None)
            errors, _result = DXMTBENCH.validate_run_artifacts(outdir, require_visual=False)
            self.assertIn("framebuffer-probe-missing", {error["kind"] for error in errors})

    def test_required_canvas_size_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = pathlib.Path(tmp)
            self.write_result(
                outdir,
                config={"expectedCanvasWidth": 3840, "expectedCanvasHeight": 2160},
                canvas={"width": 2712, "height": 1548},
            )
            (outdir / "bench-config.json").write_text(
                json.dumps({
                    "run": "current-run",
                    "expectedCanvasWidth": "3840",
                    "expectedCanvasHeight": "2160",
                }) + "\n",
                encoding="utf-8",
            )
            result = json.loads((outdir / "browser-result.json").read_text(encoding="utf-8"))
            result["runId"] = "current-run"
            (outdir / "browser-result.json").write_text(json.dumps(result), encoding="utf-8")
            errors, _result = DXMTBENCH.validate_run_artifacts(outdir, require_visual=False)
            self.assertIn("canvas-size", {error["kind"] for error in errors})

    def test_authoritative_canvas_requirement_cannot_be_understated_by_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = pathlib.Path(tmp)
            self.write_result(
                outdir,
                runId="current-run",
                config={"expectedCanvasWidth": 0, "expectedCanvasHeight": 0},
                canvas={"width": 1280, "height": 720},
            )
            (outdir / "bench-config.json").write_text(
                json.dumps({
                    "run": "current-run",
                    "expectedCanvasWidth": "3840",
                    "expectedCanvasHeight": "2160",
                }) + "\n",
                encoding="utf-8",
            )
            errors, _result = DXMTBENCH.validate_run_artifacts(outdir, require_visual=False)
            kinds = {error["kind"] for error in errors}
            self.assertIn("browser-config-mismatch", kinds)
            self.assertIn("canvas-size", kinds)

    def test_explicit_terminal_error_status_fails_without_error_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = pathlib.Path(tmp)
            self.write_result(outdir, terminalStatus="error")
            errors, _result = DXMTBENCH.validate_run_artifacts(outdir, require_visual=False)
            self.assertIn("browser-terminal-status", {error["kind"] for error in errors})

    def test_browser_runtime_error_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = pathlib.Path(tmp)
            self.write_result(
                outdir,
                terminalStatus="error",
                error="unexpected WebGL context loss",
            )
            errors, _result = DXMTBENCH.validate_run_artifacts(outdir, require_visual=False)
            self.assertIn("browser-error", {error["kind"] for error in errors})

    def test_duplicate_terminal_result_marker_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = pathlib.Path(tmp)
            self.write_result(outdir)
            (outdir / "terminal-result-conflicts.jsonl").write_text(
                json.dumps({"event": "terminal-result-conflict", "runId": "current"}) + "\n",
                encoding="utf-8",
            )
            errors, _result = DXMTBENCH.validate_run_artifacts(outdir, require_visual=False)
            self.assertIn("terminal-result-conflict", {error["kind"] for error in errors})

    def test_stale_browser_result_run_id_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = pathlib.Path(tmp)
            self.write_result(outdir, runId="old-run")
            (outdir / "bench-config.json").write_text(
                json.dumps({"run": "current-run"}) + "\n",
                encoding="utf-8",
            )
            errors, _result = DXMTBENCH.validate_run_artifacts(outdir, require_visual=False)
            self.assertIn("browser-result-run-mismatch", {error["kind"] for error in errors})

    def test_failed_or_alerted_rows_are_not_loaded_as_baselines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "suite-results.jsonl"
            valid_metrics = {
                "fpsAvg": 60,
                "framebufferProbe": self.valid_probe(),
                "visualPrimaryMeasureMid": "visible-varied",
                "visualPrimaryMeasureMidSignature": "present",
            }
            rows = [
                {"workload": "good", "status": "ok", "alerts": [], "metrics": valid_metrics},
                {
                    "workload": "result-probe-good",
                    "status": "ok",
                    "alerts": [],
                    "metrics": {
                        "fpsAvg": 60,
                        "visualPrimaryMeasureMid": "visible-varied",
                        "visualPrimaryMeasureMidSignature": "present",
                    },
                    "result": {"framebufferProbe": self.valid_probe()},
                },
                {"workload": "failed", "status": "functional-failure", "alerts": [], "metrics": valid_metrics},
                {
                    "workload": "alerted",
                    "status": "ok",
                    "alerts": [{"kind": "visual-primary"}],
                    "metrics": valid_metrics,
                },
                {"workload": "legacy-weak", "status": "ok", "alerts": [], "metrics": {"fpsAvg": 63}},
                {
                    "workload": "bad-visual",
                    "status": "ok",
                    "alerts": [],
                    "metrics": {**valid_metrics, "visualPrimaryMeasureMidSignature": "absent"},
                },
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            baseline = DXMTBENCH.load_baseline(str(path))
            self.assertEqual({"good", "result-probe-good"}, set(baseline))

    def test_legacy_tsv_without_functional_evidence_is_not_a_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "suite-summary.tsv"
            path.write_text(
                "workload\tstatus\tfps_avg\tframe_ms_p95\tactive_cpu_avg\n"
                "legacy\tok\t60\t16.7\t10\n",
                encoding="utf-8",
            )
            self.assertEqual({}, DXMTBENCH.load_baseline(str(path)))

    def test_tsv_requires_all_functional_evidence_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "suite-summary.tsv"
            path.write_text(
                "workload\tstatus\tfps_avg\tframe_ms_p95\tactive_cpu_avg\t"
                "framebuffer_probe_ok\tvisual_primary_measure_mid\tvisual_primary_signature\t"
                "baseline_eligible\talert_count\n"
                "good\tok\t60\t16.7\t10\t1\tvisible-varied\tpresent\t1\t0\n"
                "no-probe\tok\t61\t16.6\t11\t0\tvisible-varied\tpresent\t0\t0\n"
                "bad-class\tok\t62\t16.5\t12\t1\tblank-gray\tpresent\t0\t0\n"
                "bad-signature\tok\t63\t16.4\t13\t1\tvisible-varied\tabsent\t0\t0\n"
                "alerted\tok\t64\t16.3\t14\t1\tvisible-varied\tpresent\t0\t1\n"
                "claimed-eligible-alerted\tok\t65\t16.2\t15\t1\tvisible-varied\tpresent\t1\t1\n",
                encoding="utf-8",
            )
            baseline = DXMTBENCH.load_baseline(str(path))
            self.assertEqual({"good"}, set(baseline))
            self.assertEqual(60.0, baseline["good"]["fpsAvg"])

    def test_emitted_tsv_eligibility_reflects_final_regression_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite_root = pathlib.Path(tmp) / "suite"
            suite_root.mkdir()
            tsv_path = suite_root / "suite-summary.tsv"
            columns = list(DXMTBENCH.SUITE_TSV_COLUMNS)
            tsv_path.write_text("\t".join(columns) + "\n", encoding="utf-8")

            baseline_path = suite_root / "baseline.jsonl"
            baseline_path.write_text(json.dumps({
                "workload": "regressed",
                "status": "ok",
                "alerts": [],
                "metrics": {
                    "fpsAvg": 100,
                    "framebufferProbe": self.valid_probe(),
                    "visualPrimaryMeasureMid": "visible-varied",
                    "visualPrimaryMeasureMidSignature": "present",
                },
            }) + "\n", encoding="utf-8")

            def emit(workload, baseline, screenshot_bytes):
                outdir = suite_root / workload
                outdir.mkdir()
                self.write_result(outdir, fpsAvg=60.0, frameMsP95=16.0)
                self.write_visual(outdir, "visible-varied", "present")
                (outdir / "measure-mid.png").write_bytes(screenshot_bytes)
                DXMTBENCH.cmd_suite_workload_result([
                    workload,
                    str(outdir),
                    "0",
                    str(tsv_path),
                    str(suite_root / "suite-results.jsonl"),
                    str(suite_root / "suite-events.jsonl"),
                    str(suite_root / "suite-alerts.jsonl"),
                    str(suite_root / "suite-latest.json"),
                    str(suite_root / "suite-status.txt"),
                    str(suite_root),
                    str(baseline) if baseline else "",
                    "5",
                    "10",
                    "15",
                    "none",
                    "1",
                    "2",
                ])

            emit("regressed", baseline_path, b"regressed-render")
            emit("good", None, b"good-render")

            for line in tsv_path.read_text(encoding="utf-8").splitlines():
                self.assertEqual(len(DXMTBENCH.SUITE_TSV_COLUMNS), len(line.split("\t")))
            with tsv_path.open(encoding="utf-8") as fh:
                rows = {row["workload"]: row for row in DictReader(fh, delimiter="\t")}
            self.assertEqual("0", rows["regressed"]["baseline_eligible"])
            self.assertEqual("1", rows["regressed"]["alert_count"])
            self.assertEqual("1", rows["good"]["baseline_eligible"])
            self.assertEqual("0", rows["good"]["alert_count"])
            self.assertEqual({"good"}, set(DXMTBENCH.load_baseline(str(tsv_path))))


if __name__ == "__main__":
    unittest.main()
