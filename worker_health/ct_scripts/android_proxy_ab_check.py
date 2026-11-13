#!/usr/bin/env python3
"""Compare Fenix system/direct/system proxy paths; stdlib only, Python 3.9+."""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/task"
DEFAULT_DRIVER = f"{ROOT}/EZVHUtEMQ5aUbAHjoEmGCg/artifacts/public/build/geckodriver.tar.gz"
DEFAULT_APK = f"{ROOT}/KdQvF3YDRkuDPEyCzyM05Q/artifacts/public/build/target.arm64-v8a.apk"
URLS = [
    "https://mozilla-necko.github.io/tests/dns/trr_multi_domain.html",
    (
        "https://prod.youtube-test.prod.webservices.mozgcp.net/main.html?"
        "test_type=playbackperf-sfr-av1-test&raptor=true&exclude=1,2&muted=true&command=run"
    ),
]
PHASES = [("system-before", 5), ("direct", 0), ("system-after", 5)]
PROBE_ERRORS = (OSError, RuntimeError, ValueError, subprocess.SubprocessError)


def utc():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def run(command, timeout=30, check=True):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=check)


def select_serial(devices, requested=None):
    online = [line.split()[0] for line in devices.splitlines() if line.endswith("\tdevice")]
    if requested:
        if requested not in online:
            raise RuntimeError(f"Requested device {requested!r} is not online")
        return requested
    if len(online) != 1:
        raise RuntimeError(f"Expected one online device; got {online}. Specify --serial.")
    return online[0]


def download(url, destination):
    print(f"{utc()} Downloading {url}", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


class WebDriverError(RuntimeError):
    pass


class Driver:
    def __init__(self, endpoint, timeout):
        self.endpoint = endpoint
        self.timeout = timeout
        # Local WebDriver requests must never inherit an HTTP(S)_PROXY variable.
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, body=None):
        request = urllib.request.Request(
            self.endpoint + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with self.http.open(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read())
        value = payload.get("value", {})
        if isinstance(value, dict) and value.get("error"):
            raise WebDriverError(json.dumps(value))
        return value


def capabilities(package, serial, proxy_type, activity):
    options = {
        "androidPackage": package,
        "androidDeviceSerial": serial,
        "prefs": {"network.proxy.type": proxy_type},
    }
    if activity:
        options["androidActivity"] = activity
    return {
        "capabilities": {
            "alwaysMatch": {
                "browserName": "firefox",
                "acceptInsecureCerts": False,
                "pageLoadStrategy": "normal",
                "moz:firefoxOptions": options,
            },
        },
    }


def inspect_page(driver, session, result, prefix, adb):
    for name, suffix in [("url", "/url"), ("title", "/title"), ("source", "/source")]:
        try:
            value = driver.call("GET", session + suffix)
            if name == "source":
                prefix.with_suffix(".html").write_text(value)
                result["error_markers"] = sorted(
                    set(
                        re.findall(
                            r"(?:SEC_ERROR_|SSL_ERROR_|MOZILLA_PKIX_ERROR_|ERR_CERT_)[A-Z_]+"
                            r"|about:(?:certerror|neterror)|Secure Connection Failed|Potential Security Risk",
                            value,
                        ),
                    ),
                )
            else:
                result[name] = value
        except PROBE_ERRORS as error:
            result[f"{name}_capture_error"] = str(error)
    try:
        image = driver.call("GET", session + "/screenshot")
        prefix.with_suffix(".png").write_bytes(base64.b64decode(image))
    except PROBE_ERRORS as error:
        result["screenshot_capture_error"] = str(error)
        try:
            image = subprocess.run(
                adb + ["exec-out", "screencap", "-p"],
                capture_output=True,
                timeout=20,
                check=True,
            ).stdout
            prefix.with_suffix(".png").write_bytes(image)
        except PROBE_ERRORS as fallback_error:
            result["adb_screenshot_error"] = str(fallback_error)


def probe_phase(driver, args, adb, output, round_number, label, proxy_type, report):
    phase = {"round": round_number, "phase": label, "requested_proxy_type": proxy_type, "started": utc(), "pages": []}
    report["phases"].append(phase)
    write_json(output / "results.json", report)
    session = None
    try:
        created = driver.call("POST", "/session", capabilities(args.package, args.serial, proxy_type, args.activity))
        session = "/session/" + created["sessionId"]
        phase["capabilities"] = created["capabilities"]
        # A permissive driver would invalidate a certificate-trust comparison.
        if phase["capabilities"].get("acceptInsecureCerts") is not False:
            raise RuntimeError("Driver did not confirm acceptInsecureCerts=false")
        driver.call("POST", session + "/timeouts", {"pageLoad": args.page_timeout * 1000})
        for index, url in enumerate(args.url or URLS, 1):
            result = {"requested_url": url, "started": utc()}
            phase["pages"].append(result)
            start = time.monotonic()
            print(f"{utc()} Round {round_number}/{args.rounds} {label}: {url}", flush=True)
            try:
                driver.call("POST", session + "/url", {"url": url})
                result["navigation"] = "completed"
            except PROBE_ERRORS as error:
                result["navigation"] = "error"
                result["error"] = str(error)
            result["navigation_seconds"] = round(time.monotonic() - start, 3)
            prefix = output / f"round-{round_number}-{label}-page-{index}"
            inspect_page(driver, session, result, prefix, adb)
            result["finished"] = utc()
            print(f"{utc()} {label}: {result['navigation']}; markers={result.get('error_markers', [])}", flush=True)
            write_json(output / "results.json", report)
    except PROBE_ERRORS as error:
        phase["setup_error"] = str(error)
        print(f"{utc()} {label} setup error: {error}", flush=True)
    finally:
        if session:
            try:
                driver.call("DELETE", session)
            except PROBE_ERRORS as error:
                phase["cleanup_error"] = str(error)
        phase["finished"] = utc()
        write_json(output / "results.json", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=os.environ.get("DEVICE_SERIAL") or os.environ.get("ANDROID_SERIAL"))
    parser.add_argument("--package", default="org.mozilla.fenix")
    parser.add_argument("--activity", default="org.mozilla.fenix.IntentReceiverActivity")
    parser.add_argument("--geckodriver", help="Existing host binary; otherwise download the pinned CI driver")
    parser.add_argument("--geckodriver-url", default=DEFAULT_DRIVER)
    parser.add_argument("--apk-url", help="Optionally install/replace Fenix with this APK before checking")
    parser.add_argument("--ci-apk", action="store_true", help="Install the exact APK from comment 10's job")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--interval", type=float, default=60, help="Seconds between complete paired rounds")
    parser.add_argument("--page-timeout", type=int, default=45)
    parser.add_argument("--url", action="append", help="Override URLs (repeatable)")
    parser.add_argument("--output-dir", type=Path, default=Path("out/proxy-ab"))
    args = parser.parse_args()
    if args.rounds < 1 or args.interval < 0 or args.page_timeout < 1:
        parser.error("rounds/page-timeout must be positive; interval must be nonnegative")
    if args.ci_apk and args.apk_url:
        parser.error("Choose --ci-apk or --apk-url")
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "started": utc(),
        "task_id": os.environ.get("TASK_ID"),
        "phases": [],
        "scope": "Page navigation/TLS check; not DNS correctness or playback performance",
    }
    write_json(output / "results.json", report)
    adb_start = None
    adb = None
    try:
        devices = run(["adb", "devices"]).stdout
        args.serial = select_serial(devices, args.serial)
        adb = ["adb", "-s", args.serial]
        report["serial"] = args.serial
        report["package"] = args.package
        report["device"] = {
            key: run(adb + ["shell", "getprop", key]).stdout.strip()
            for key in ["ro.product.model", "ro.build.version.release", "ro.build.fingerprint"]
        }
        # Read selected settings only; do not dump Wi-Fi credentials or change system proxy state.
        report["global_proxy_settings"] = {
            key: run(adb + ["shell", "settings", "get", "global", key]).stdout.strip()
            for key in ["http_proxy", "global_http_proxy_host", "global_http_proxy_port", "global_proxy_pac_url"]
        }
        report["proxy_settings_limit"] = "Global settings may omit per-Wi-Fi or vendor proxy configuration."
        adb_start = run(adb + ["shell", "date", "+%m-%d %H:%M:%S.000"]).stdout.strip()
        with tempfile.TemporaryDirectory(prefix="proxy-ab-") as scratch:
            scratch = Path(scratch)
            apk_url = DEFAULT_APK if args.ci_apk else args.apk_url
            if apk_url:
                report["apk"] = {"url": apk_url, "sha256": download(apk_url, scratch / "fenix.apk")}
                installed = run(adb + ["install", "-r", str(scratch / "fenix.apk")], timeout=180)
                report["apk"]["install_output"] = installed.stdout
                if "Success" not in installed.stdout:
                    raise RuntimeError("APK install did not report Success")
            if not run(adb + ["shell", "pm", "path", args.package]).stdout.strip().startswith("package:"):
                raise RuntimeError("Fenix not installed; supply --ci-apk or --apk-url")
            package_info = run(adb + ["shell", "dumpsys", "package", args.package]).stdout
            report["package_version"] = [
                line.strip() for line in package_info.splitlines() if "versionName=" in line or "versionCode=" in line
            ]
            driver_path = args.geckodriver
            if not driver_path:
                archive = scratch / "geckodriver.tar.gz"
                report["driver"] = {"url": args.geckodriver_url, "sha256": download(args.geckodriver_url, archive)}
                # Extract only the regular executable, without trusting archive paths or links.
                with tarfile.open(archive) as bundle:
                    member = next(item for item in bundle if Path(item.name).name == "geckodriver" and item.isfile())
                    driver_path = str(scratch / "geckodriver")
                    with bundle.extractfile(member) as source, open(driver_path, "wb") as target:
                        shutil.copyfileobj(source, target)
                Path(driver_path).chmod(0o755)
            report["driver_version"] = run([driver_path, "--version"]).stdout
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            with (output / "geckodriver.log").open("w") as log:
                process = subprocess.Popen(
                    [driver_path, "--host", "127.0.0.1", "--port", str(port), "--log", "debug"],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                driver = Driver(f"http://127.0.0.1:{port}", args.page_timeout + 30)
                try:
                    for _ in range(100):
                        if process.poll() is not None:
                            raise RuntimeError("geckodriver exited; see geckodriver.log")
                        try:
                            if driver.call("GET", "/status").get("ready"):
                                break
                        except (OSError, urllib.error.URLError):
                            pass
                        time.sleep(0.1)
                    else:
                        raise RuntimeError("geckodriver did not become ready")
                    for number in range(1, args.rounds + 1):
                        for label, proxy_type in PHASES:
                            probe_phase(driver, args, adb, output, number, label, proxy_type, report)
                        if number < args.rounds:
                            time.sleep(args.interval)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
    except PROBE_ERRORS as error:
        report["fatal_error"] = str(error)
        print(f"{utc()} Fatal: {error}", flush=True)
    finally:
        if adb and adb_start:
            try:
                logs = run(adb + ["logcat", "-d", "-v", "threadtime", "-T", adb_start], timeout=30).stdout
                selected = [
                    line
                    for line in logs.splitlines()
                    if re.search(
                        r"SEC_ERROR|SSL_ERROR|PKIX|CERT_|certificat|proxy|certerror|neterror",
                        line,
                        re.IGNORECASE,
                    )
                ]
                (output / "device-network-errors.log").write_text("\n".join(selected) + "\n")
            except PROBE_ERRORS as error:
                report["logcat_error"] = str(error)
        report["finished"] = utc()
        write_json(output / "results.json", report)
    setup_failed = report.get("fatal_error") or any(
        phase.get("setup_error") or phase.get("cleanup_error") for phase in report["phases"]
    )
    # Navigation failures are observations, not broken diagnostic infrastructure.
    print(f"{utc()} Results: {output / 'results.json'}", flush=True)
    return 2 if setup_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
