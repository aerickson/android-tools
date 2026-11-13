# LT Android proxy comparison (bug 2039991)

This checks actual Fenix navigation, not host curl connectivity. Each round uses
three fresh WebDriver sessions: system proxy (type 5), direct (type 0), system
proxy again. Certificate validation stays enabled (`acceptInsecureCerts=false`).
The default is three rounds, 60 seconds apart, against the DNS page and YTP page
from the bug. It checks page navigation/TLS, not playback performance or DNS
correctness. A loaded page does not prove all subresources succeeded.

Run from `worker_health` (preview only):

```sh
pipenv run python create_tc_task.py \
  --queue proj-autophone/gecko-t-lambda-test-1 --count 1 \
  --script-file ct_scripts/android_proxy_ab_check.py \
  --script-args '--ci-apk --rounds 3 --interval 60 --page-timeout 45' \
  --command-timeout 2400 --dry-run
```

After approval, replace `--dry-run` with `--wait`. This submits one task to the
pool; it does not select a particular device. Do not change host overrides,
worker pools, system proxy settings, or Puppet. Record the actual device model,
serial, Taskcluster worker, and installed versions before drawing conclusions
about A55 coverage. Three rounds sample a short window; an all-pass run cannot
exclude failures at other times or on other devices.

`--ci-apk` installs/replaces `org.mozilla.fenix` with the exact APK used by task
HJoM7rkqTe6EjJOmJ1lIpg (November 12, 2026). The APK remains installed afterward.
The script does not uninstall an existing app to work around signature/version
conflicts; installation failure stops the check. Omit `--ci-apk` to use an
already-installed Fenix, or supply `--apk-url`. It downloads the pinned job's
Linux geckodriver to a temporary directory; `--geckodriver` uses a local binary.
Artifact URLs and downloaded SHA-256 values are recorded in results.json.

ADB selects DEVICE_SERIAL/ANDROID_SERIAL, an explicit --serial, or the sole
online device. Multiple unselected devices fail closed. Each browser session is
deleted at the end of its phase, including on navigation errors. Sessions use
temporary profiles; global Android proxy settings are only read. The script
does not clear app data or logcat. A hard task termination may prevent cleanup.

Task artifacts under `public/out/proxy-ab/` contain incrementally saved
results.json, per-page HTML/screenshots, geckodriver debug logs, and filtered
device logcat lines. These are public: use only public test URLs and a test
device. Logcat contains only matching lines since the check started; it may
contain messages from other apps and may omit relevant messages.

System-global proxy settings may be empty even when Wi-Fi/vendor configuration
supplies a proxy. Requested proxy types are recorded; this does not independently
prove which proxy endpoint was used. A system failure, direct success, and second
system failure is stronger path evidence than one isolated successful run.
Certificate error codes strengthen the TLS hypothesis; a timeout alone does not
identify its cause. Screenshots/source/logs may expose a certificate error, but
this tool does not retrieve the peer certificate chain or issuer.

Exit 0 means the diagnostic completed, including observed navigation failures.
Exit 2 means setup/cleanup failed and results may be incomplete. All-pass means
only that no navigation failure was observed in this sample.

Local verification (no device or network):

```sh
pipenv run python tests/test_android_proxy_ab_check.py
```
