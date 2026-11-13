import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "probe",
    Path(__file__).resolve().parents[1] / "ct_scripts" / "android_proxy_ab_check.py",
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class FakeDriver:
    def __init__(self, insecure=False):
        self.calls = []
        self.insecure = insecure

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/session":
            return {"sessionId": "test", "capabilities": {"acceptInsecureCerts": self.insecure}}
        if method == "POST" and path.endswith("/url"):
            raise probe.WebDriverError("SEC_ERROR_UNKNOWN_ISSUER")
        if path.endswith("/source"):
            return "<html>about:certerror SEC_ERROR_UNKNOWN_ISSUER</html>"
        if path.endswith("/screenshot"):
            return "aW1hZ2U="
        return "about:certerror"


class ProbeTests(unittest.TestCase):
    def test_selects_only_requested_online_device(self):
        devices = "List of devices attached\na\tdevice\nb\tdevice\nc\toffline\n"
        self.assertEqual(probe.select_serial(devices, "b"), "b")
        with self.assertRaises(RuntimeError):
            probe.select_serial(devices)
        with self.assertRaises(RuntimeError):
            probe.select_serial(devices, "c")

    def test_navigation_failure_keeps_pair_and_cleans_sessions(self):
        driver = FakeDriver()
        report = {"phases": []}
        args = SimpleNamespace(
            package="org.mozilla.fenix",
            serial="a",
            activity="activity",
            page_timeout=1,
            rounds=1,
            url=["https://example.org"],
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for label, proxy_type in probe.PHASES:
                probe.probe_phase(driver, args, [], output, 1, label, proxy_type, report)
            self.assertEqual(len(list(output.glob("*.png"))), 3)
            self.assertTrue((output / "results.json").is_file())
        requests = [
            body["capabilities"]["alwaysMatch"]
            for method, path, body in driver.calls
            if method == "POST" and path == "/session"
        ]
        self.assertEqual([r["moz:firefoxOptions"]["prefs"]["network.proxy.type"] for r in requests], [5, 0, 5])
        self.assertTrue(all(r["acceptInsecureCerts"] is False for r in requests))
        self.assertEqual(sum(method == "DELETE" for method, _, _ in driver.calls), 3)
        for phase in report["phases"]:
            self.assertEqual(phase["pages"][0]["navigation"], "error")
            self.assertIn("SEC_ERROR_UNKNOWN_ISSUER", phase["pages"][0]["error_markers"])

    def test_rejects_insecure_driver_before_navigation(self):
        driver = FakeDriver(insecure=True)
        report = {"phases": []}
        args = SimpleNamespace(package="org.mozilla.fenix", serial="a", activity="activity")
        with tempfile.TemporaryDirectory() as directory:
            probe.probe_phase(driver, args, [], Path(directory), 1, "direct", 0, report)
        self.assertIn("acceptInsecureCerts=false", report["phases"][0]["setup_error"])
        self.assertFalse(any(path.endswith("/url") for _, path, _ in driver.calls))
        self.assertEqual(driver.calls[-1][0], "DELETE")


if __name__ == "__main__":
    unittest.main()
