#!/usr/bin/env bash
#
# check_power_meter_single.sh
# Diagnostic for a setup with exactly one AVHzy CT-3 USB power meter.
#
# This script intentionally does not use a serial-number argument. It reports
# all visible meters and tests the first one in deterministic bus/address order.

set -o pipefail

echo "=== AVHzy CT-3 Power Meter Diagnostic ==="
echo "Date: $(date)"
echo "Host: $(hostname)"
echo "User: $(id)"
echo

echo "=== USB Device Nodes (/dev/bus/usb) ==="
if [ -d /dev/bus/usb ]; then
    find /dev/bus/usb -type c -ls
else
    echo "ERROR: /dev/bus/usb is not available"
    exit 1
fi
echo

echo "=== USB Devices (lsusb) ==="
if command -v lsusb >/dev/null 2>&1; then
    lsusb
else
    echo "lsusb not installed, installing usbutils..."
    sudo apt-get install -y -q usbutils 2>&1 | tail -2
    lsusb 2>&1 || {
        echo "ERROR: lsusb is still not available after installation"
        exit 1
    }
fi
echo

PYTHON="$(command -v python3 || command -v python || true)"
if [ -z "$PYTHON" ]; then
    echo "ERROR: python3 not found"
    exit 1
fi
echo "Python: $PYTHON ($($PYTHON --version))"

if ! "$PYTHON" -c "import usb.core, usb.util" 2>/dev/null; then
    echo "pyusb not installed, installing dependencies..."
    sudo apt-get install -y -q libusb-1.0-0 2>&1 | tail -3
    pip3 install pyusb
fi

if ! "$PYTHON" -c "import usb.core, usb.util" 2>&1; then
    echo "ERROR: pyusb still not importable after install attempt - cannot continue"
    exit 1
fi
echo "pyusb: OK"
echo

"$PYTHON" -u - <<'PYEOF'
import struct
import os
import sys

import usb.core
import usb.util

VENDOR_ID = 0x0483
PRODUCT_IDS = (0xFFFE, 0xFFFF, 0x374B)

devices = []
for product_id in PRODUCT_IDS:
    found = usb.core.find(find_all=True, idVendor=VENDOR_ID, idProduct=product_id)
    if found:
        devices.extend(found)

print(f"Found {len(devices)} supported AVHzy/Shizuku device(s):")
for device in devices:
    print(
        f"  Bus {device.bus:03d} Device {device.address:03d}: "
        f"VID:PID {device.idVendor:04x}:{device.idProduct:04x}"
    )

if len(devices) == 0:
    print("FAIL: no AVHzy power meter visible on USB bus")
    sys.exit(1)

devices.sort(key=lambda device: (device.bus, device.address))
target_serial = os.environ.get("PowerMeterSerial") or os.environ.get("USB_POWER_METER_SERIAL_NUMBER")
if target_serial:
    matching_devices = []
    for candidate in devices:
        try:
            serial = usb.util.get_string(candidate, candidate.iSerialNumber) if candidate.iSerialNumber else None
        except Exception:
            serial = None
        if serial == target_serial:
            matching_devices.append(candidate)

    if not matching_devices:
        print(f"FAIL: no power meter found with serial number {target_serial!r}")
        sys.exit(1)
    device = matching_devices[0]
    print(
        f"Selected power meter by serial {target_serial!r}: "
        f"Bus {device.bus:03d} Device {device.address:03d}"
    )
elif len(devices) > 1:
    print(f"WARNING: {len(devices)} AVHzy power meters visible; testing the first one")
    device = devices[0]
else:
    device = devices[0]

interface_number = None
try:
    device.set_configuration()
    configuration = device.get_active_configuration()
    endpoint_in = endpoint_out = None

    for interface in configuration:
        candidate_in = usb.util.find_descriptor(
            interface,
            custom_match=lambda endpoint: (
                usb.util.endpoint_direction(endpoint.bEndpointAddress) == usb.util.ENDPOINT_IN
                and usb.util.endpoint_type(endpoint.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK
            ),
        )
        candidate_out = usb.util.find_descriptor(
            interface,
            custom_match=lambda endpoint: (
                usb.util.endpoint_direction(endpoint.bEndpointAddress) == usb.util.ENDPOINT_OUT
                and usb.util.endpoint_type(endpoint.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK
            ),
        )
        if candidate_in and candidate_out:
            endpoint_in = candidate_in
            endpoint_out = candidate_out
            interface_number = interface.bInterfaceNumber
            break

    if not endpoint_in or not endpoint_out:
        print("FAIL: no interface has both bulk IN and OUT endpoints")
        sys.exit(1)

    print(
        f"Bulk endpoints: IN=0x{endpoint_in.bEndpointAddress:02x} "
        f"OUT=0x{endpoint_out.bEndpointAddress:02x} "
        f"interface={interface_number}"
    )

    begin = 0xA5
    end = 0x5A

    def make_frame(command, args=(), request_id=0):
        payload = bytes([0x01, command, request_id, 0x00] + list(args))
        checksum = 0
        for byte in payload:
            checksum ^= byte
        return bytes([begin]) + struct.pack("<I", len(payload)) + payload + bytes([checksum, end])

    def read_frame(timeout_ms=3000):
        buffer = bytearray()
        while True:
            try:
                buffer.extend(endpoint_in.read(512, timeout=timeout_ms))
            except usb.core.USBTimeoutError:
                return None
            if begin not in buffer:
                continue
            buffer = buffer[buffer.index(begin) :]
            if len(buffer) < 6:
                continue
            payload_length = struct.unpack_from("<I", buffer, 1)[0]
            frame_length = 1 + 4 + payload_length + 1 + 1
            if len(buffer) >= frame_length:
                return bytes(buffer[:frame_length])

    endpoint_out.write(make_frame(0x07), timeout=2000)
    read_frame(timeout_ms=2000)
    endpoint_out.write(make_frame(0x09, struct.pack("<I", 1), request_id=1), timeout=2000)
    sample = read_frame(timeout_ms=3000)

    endpoint_out.write(make_frame(0x07, request_id=2), timeout=2000)
    if sample:
        print(f"PASS: received a sample frame ({len(sample)} bytes)")
        print("RESULT: PASS - power meter connected, accessible, and returning samples")
    else:
        print("RESULT: UNCERTAIN - device opened, but no sample frame was received")
        sys.exit(1)
finally:
    if interface_number is not None:
        usb.util.release_interface(device, interface_number)
PYEOF

EXIT=$?
echo
if [ "$EXIT" -eq 0 ]; then
    echo "=== OVERALL RESULT: PASS ==="
else
    echo "=== OVERALL RESULT: FAIL ==="
fi
exit "$EXIT"
