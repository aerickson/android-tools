#!/usr/bin/env python3

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import json
import logging
import os
import subprocess
import sys
from glob import glob

from mozdevice import ADBDevice, ADBError, ADBHost, ADBTimeoutError

MAX_NETWORK_ATTEMPTS = 3
ADB_COMMAND_TIMEOUT = 10


def fatal(message, exception=None, retry=True):
    """Emit an error message and exit the process with status
    TBPL_RETRY_EXIT_STATUS this will cause the job to be retried.

    """
    TBPL_RETRY_EXIT_STATUS = 4
    if retry:
        exit_code = TBPL_RETRY_EXIT_STATUS
    else:
        exit_code = 1
    print("TEST-UNEXPECTED-FAIL | bitbar | {}".format(message))
    if exception:
        print("{}: {}".format(exception.__class__.__name__, exception))
    sys.exit(exit_code)


def show_df():
    try:
        print("\ndf -h\n%s\n\n" % subprocess.check_output(["df", "-h"], stderr=subprocess.STDOUT).decode())
    except subprocess.CalledProcessError as e:
        print("{} attempting df".format(e))


# TODO: rename to ensure_known_device or similar
def get_device_type(device):
    device_type = device.shell_output("getprop ro.product.model", timeout=ADB_COMMAND_TIMEOUT)
    if device_type == "Pixel 2" or device_type == "Pixel 5":
        pass
    elif device_type == "Moto G (5)":
        pass
    elif device_type == "SM-G930F" or device_type == "SM-A515F":
        # samsung s7 galaxy (exynos) or samsung A51
        pass
    elif device_type == "Android SDK built for x86":
        pass
    else:
        fatal("Unknown device ('%s')! Contact Android Relops immediately." % device_type, retry=False)
    return device_type


def enable_charging(device, device_type):
    p2_path = "/sys/class/power_supply/battery/input_suspend"
    p5_path = "/sys/class/power_supply/sm7250_bms/charge_disable"
    g5_path = "/sys/class/power_supply/battery/charging_enabled"
    s7_path = "/sys/class/power_supply/battery/batt_slate_mode"
    # a51 uses same paths as s7

    if not device.is_rooted:
        print("enable_charging: device is not rooted, not enabling.")
        return

    try:
        print("script.py: enabling charging for device '%s' ('%s')..." % (device_type, device.get_info("id")["id"]))
        if device_type == "Pixel 2":
            p2_charging_disabled = (
                device.shell_output("cat %s 2>/dev/null" % p2_path, timeout=ADB_COMMAND_TIMEOUT).strip() == "1"
            )
            if p2_charging_disabled:
                print("Enabling charging...")
                device.shell_bool("echo %s > %s" % (0, p2_path), timeout=ADB_COMMAND_TIMEOUT)
        elif device_type == "Pixel 5":
            p5_charging_disabled = (
                device.shell_output("cat %s 2>/dev/null" % p5_path, timeout=ADB_COMMAND_TIMEOUT).strip() == "1"
            )
            if p5_charging_disabled:
                print("Enabling charging...")
                device.shell_bool("echo %s > %s" % (0, p5_path), timeout=ADB_COMMAND_TIMEOUT)
        elif device_type == "Moto G (5)":
            g5_charging_disabled = (
                device.shell_output("cat %s 2>/dev/null" % g5_path, timeout=ADB_COMMAND_TIMEOUT).strip() == "0"
            )
            if g5_charging_disabled:
                print("Enabling charging...")
                device.shell_bool("echo %s > %s" % (1, g5_path), timeout=ADB_COMMAND_TIMEOUT)
        elif device_type == "SM-G930F" or device_type == "SM-A515F":
            s7_charging_disabled = (
                device.shell_output("cat %s 2>/dev/null" % s7_path, timeout=ADB_COMMAND_TIMEOUT).strip() == "1"
            )
            if s7_charging_disabled:
                print("Enabling charging...")
                device.shell_bool("echo %s > %s" % (0, s7_path), root=True, timeout=ADB_COMMAND_TIMEOUT)
        elif device_type == "Android SDK built for x86":
            pass
        else:
            fatal("Unknown device ('%s')! Contact Android Relops immediately." % device_type, retry=False)
    except (ADBError, ADBTimeoutError) as e:
        print("TEST-WARNING | bitbar | Error while attempting to enable charging.")
        print("{}: {}".format(e.__class__.__name__, e))


def _monitor_readline(process, q):
    while True:
        bail = True
        if process.poll() is None:
            bail = False
        out = process.stdout.readline().decode()
        q.put(out)
        if q.empty() and bail:
            break


def main():
    parser = argparse.ArgumentParser(
        usage="%(prog)s [options] <test command> (<test command option> ...)",
        description="Wrapper script for tests run on physical Android devices at Bitbar. Runs the provided command "
        "wrapped with required setup and teardown.",
    )
    _args, extra_args = parser.parse_known_args()
    logging.basicConfig(format="%(asctime)-15s %(levelname)s %(message)s", level=logging.INFO, stream=sys.stdout)

    print("""
⠀⠀⠀⠀⢀⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⡀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠙⢷⣤⣤⣴⣶⣶⣦⣤⣤⡾⠋⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦⠀⠀⠀⠀⠀
⠀⠀⠀⠀⣼⣿⣿⣉⣹⣿⣿⣿⣿⣏⣉⣿⣿⣧⠀⠀⠀⠀
⠀⠀⠀⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠀⠀⠀
⣠⣄⠀⢠⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⡄⠀⣠⣄
⣿⣿⡇⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⢸⣿⣿
⣿⣿⡇⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⢸⣿⣿
⣿⣿⡇⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⢸⣿⣿
⣿⣿⡇⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⢸⣿⣿
⠻⠟⠁⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠈⠻⠟
⠀⠀⠀⠀⠉⠉⣿⣿⣿⡏⠉⠉⢹⣿⣿⣿⠉⠉⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⣿⣿⣿⡇⠀⠀⢸⣿⣿⣿⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⣿⣿⣿⡇⠀⠀⢸⣿⣿⣿⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠈⠉⠉⠀⠀⠀⠀⠉⠉⠁⠀⠀⠀⠀⠀⠀
""")
    print("\nscript.py: starting")
    with open("/builds/worker/version") as versionfile:
        version = versionfile.read().strip()
    print("\nDockerfile version {}".format(version))

    taskcluster_debug = "*"

    task_cwd = os.getcwd()
    print("Current working directory: {}".format(task_cwd))

    with open("/builds/taskcluster/scriptvars.json") as scriptvars:
        scriptvarsenv = json.loads(scriptvars.read())
        print(
            "Bitbar test run: https://mozilla.testdroid.com/#testing/device-session/{}/{}/{}".format(
                scriptvarsenv["TESTDROID_PROJECT_ID"],
                scriptvarsenv["TESTDROID_BUILD_ID"],
                scriptvarsenv["TESTDROID_RUN_ID"],
            ),
        )

    env = dict(os.environ)

    # to handle https://issuetracker.google.com/issues/327026299 issues for now
    #   symptom: `adb devices` issues like:
    #     usb_libusb.cpp:944 failed to register inotify watch on '/dev/bus/usb/006/', falling back to sleep: No such file or directory
    #
    # set for this script (to cover mozdevice calling adb)
    os.environ["ADB_LIBUSB"] = "0"
    # set for the script run below that uses `env`
    env["ADB_LIBUSB"] = "0"

    if "PATH" in os.environ:
        path = os.environ["PATH"]
    else:
        path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin"

    path += ":/builds/worker/android-sdk-linux/tools/bin:/builds/worker/android-sdk-linux/platform-tools"

    env["PATH"] = os.environ["PATH"] = path
    env["NEED_XVFB"] = "false"
    env["DEVICE_NAME"] = scriptvarsenv["DEVICE_NAME"]
    env["ANDROID_DEVICE"] = scriptvarsenv["ANDROID_DEVICE"]
    env["DEVICE_SERIAL"] = scriptvarsenv["DEVICE_SERIAL"]
    env["HOST_IP"] = scriptvarsenv["HOST_IP"]
    env["DEVICE_IP"] = scriptvarsenv["DEVICE_IP"]
    env["DOCKER_IMAGE_VERSION"] = scriptvarsenv["DOCKER_IMAGE_VERSION"]

    if "HOME" not in env:
        env["HOME"] = "/builds/worker"
        print("setting HOME to {}".format(env["HOME"]))

    show_df()

    # If we are running normal tests we will be connected via usb and
    # there should be only one device connected.  If we are running
    # power tests, the framework will have already called adb tcpip
    # 5555 on the device before it disconnected usb. There should be
    # no devices connected and we will need to perform an adb connect
    # to connect to the device. DEVICE_SERIAL will be set to either
    # the device's serial number or its ipaddress:5555 by the framework.
    try:
        adbhost = ADBHost(verbose=True)
        if env["DEVICE_SERIAL"].endswith(":5555"):
            # Power testing with adb over wifi.
            adbhost.command_output(["connect", env["DEVICE_SERIAL"]])
        devices = adbhost.devices()
        print(json.dumps(devices, indent=4))
        if len(devices) != 1:
            fatal("Must have exactly one connected device. {} found.".format(len(devices)), retry=True)
    except (ADBError, ADBTimeoutError) as e:
        fatal("{} Unable to obtain attached devices".format(e), retry=True)

    try:
        for f in glob("/tmp/adb.*.log"):
            print("\n{}:\n".format(f))
            with open(f) as afile:
                print(afile.read())
    except Exception as e:
        print("{} while reading adb logs".format(e))

    print("Connecting to Android device {}".format(env["DEVICE_SERIAL"]))
    try:
        device = ADBDevice(device=env["DEVICE_SERIAL"])
        android_version = device.get_prop("ro.build.version.release")
        print("Android device version (ro.build.version.release):  {}".format(android_version))
        print("SDK: {}".format(device.get_prop("ro.build.version.sdk")))
        print("Build fingerprint: {}".format(device.get_prop("ro.build.fingerprint")))

        # power testing
        #   - 10/23 (aje): disabled as per Sparky we're not going to be doing power testing
        #     - they will let us know if this changes
        #
        # this can explode if an unknown device, explode now vs in an hour...
        # device_type = get_device_type(device)

        # set device to UTC
        if device.is_rooted:
            device.shell_output('setprop persist.sys.timezone "UTC"', timeout=ADB_COMMAND_TIMEOUT)
        # show date for visual confirmation
        device_datetime = device.shell_output("date", timeout=ADB_COMMAND_TIMEOUT)
        print("Android device datetime:  {}".format(device_datetime))

        # clean up the device.
        device.rm("/data/local/tests", recursive=True, force=True)
        device.rm("/data/local/tmp/*", recursive=True, force=True)
        device.rm("/data/local/tmp/xpcb", recursive=True, force=True)
        device.rm("/sdcard/tests", recursive=True, force=True)
        device.rm("/sdcard/raptor-profile", recursive=True, force=True)
    except (ADBError, ADBTimeoutError) as e:
        fatal("{} attempting to clean up device".format(e), retry=True)

    if taskcluster_debug:
        env["DEBUG"] = taskcluster_debug

    print("environment = {}".format(json.dumps(env, indent=4)))

    print("do things here!!!!")

    rc = 0
    print("Checking root and su approval...")
    try:
        su_exists = device.shell_bool("command -v su >/dev/null 2>&1", timeout=ADB_COMMAND_TIMEOUT)
        if su_exists:
            su_ok = device.shell_bool("su -c 'setenforce 0' >/dev/null 2>&1", timeout=ADB_COMMAND_TIMEOUT)
            if su_ok:
                print("PASS: device is rooted and su permissions are approved")
                print("SELinux mode:")
                print(device.shell_output("getenforce", timeout=ADB_COMMAND_TIMEOUT))
            else:
                print("FAIL: su exists, but root permission was denied or setenforce failed")
                rc = 1
        else:
            print("FAIL: su binary not found, device does not appear rooted")
            rc = 1
    except (ADBError, ADBTimeoutError) as e:
        fatal("{} while checking root and su approval".format(e), retry=True)

    print("Checking Magisk version...")
    try:
        magisk_version = device.shell_output("magisk -v", timeout=ADB_COMMAND_TIMEOUT).strip()
        magisk_versioncode = device.shell_output("magisk -V", timeout=ADB_COMMAND_TIMEOUT).strip()
        if magisk_version:
            print("Magisk version: {} (code: {})".format(magisk_version, magisk_versioncode))
        else:
            print("Magisk not found (may be using a different root method)")
    except (ADBError, ADBTimeoutError) as e:
        print("WARNING: {} while checking Magisk version".format(e))

    # power testing
    #   - 10/23 (aje): disabled as per Sparky we're not going to be doing power testing
    #     - they will let us know if this changes
    #
    # enable charging on device if it is disabled
    #   see https://bugzilla.mozilla.org/show_bug.cgi?id=1565324
    # enable_charging(device, device_type)

    try:
        if env["DEVICE_SERIAL"].endswith(":5555"):
            device.command_output(["usb"])
            adbhost.command_output(["disconnect", env["DEVICE_SERIAL"]])
        adbhost.kill_server()
    except (ADBError, ADBTimeoutError) as e:
        print("{} attempting adb kill-server".format(e))

    # try:
    #     print('\nnetstat -aop\n%s\n\n' % subprocess.check_output(
    #         ['netstat', '-aop'],
    #         stderr=subprocess.STDOUT).decode())
    # except subprocess.CalledProcessError as e:
    #     print('{} attempting netstat'.format(e))

    # show_df()

    print("script.py: exiting with exitcode {}.".format(rc))
    return rc


if __name__ == "__main__":
    sys.exit(main())
