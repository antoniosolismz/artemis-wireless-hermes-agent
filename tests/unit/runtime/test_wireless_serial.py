# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ADB-over-Wi-Fi devices (serial ``ip:port``) must be first-class task targets.

These tests pin the behaviour that makes cable-free operation possible: the
stack is transport-agnostic and never filters on USB. They cover the device
pool parser, the adbutils driver layer, and the task-submission gate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from adbutils import AdbClient

from artemis.core.diagnostics.probes.adb_probe import AdbDeviceProbe
from artemis.core.diagnostics.schema import ProbeStatus
from artemis.runtime.adb_endpoint import AdbEndpoint, AdbSession
from artemis.runtime.device_pool import DevicePool

WIFI_SERIAL = "192.168.1.50:5555"
WIFI_ADB_LINE = (
    f"List of devices attached\n"
    f"{WIFI_SERIAL}       device product:panther model:Pixel_7_device transport_id:3\n"
)
USB_ADB_LINE = (
    "List of devices attached\n"
    "1A2B3C4D5E       device product:panther model:Pixel_7_device transport_id:1\n"
)


def test_device_pool_keeps_wireless_serial_verbatim() -> None:
    """A `host:port` serial is parsed exactly like a USB serial."""
    parsed = DevicePool._parse_device_lines(WIFI_ADB_LINE.splitlines())

    assert len(parsed) == 1
    serial, state, model, _product = parsed[0]
    assert serial == WIFI_SERIAL
    assert state == "device"
    assert model == "Pixel 7 device"


def test_device_pool_mixes_wireless_and_usb_devices() -> None:
    """Wireless and USB devices enumerate together with no transport filter."""
    parsed = DevicePool._parse_device_lines(
        (USB_ADB_LINE.rstrip("\n") + f"\n{WIFI_SERIAL}       device model:Pixel_7_device\n").splitlines()
    )

    serials = [row[0] for row in parsed]
    assert serials == ["1A2B3C4D5E", WIFI_SERIAL]


def test_adbutils_accepts_colon_serial() -> None:
    """The driver layer (adbutils) constructs a device handle from `ip:port`."""
    device = AdbClient(host="127.0.0.1", port=5037).device(serial=WIFI_SERIAL)

    assert device.serial == WIFI_SERIAL


@pytest.mark.asyncio
async def test_submission_gate_accepts_wireless_device(monkeypatch) -> None:
    """A wi-fi target passes the bounded pre-submission readiness gate."""
    probe = AdbDeviceProbe()
    monkeypatch.setattr(
        "artemis.core.diagnostics.probes.adb_probe.toolchain.resolve",
        lambda name: "adb",
    )
    monkeypatch.setattr(probe, "_get_device_states", AsyncMock(return_value=[(WIFI_SERIAL, "device")]))
    monkeypatch.setattr(probe, "_get_confirmed_device_lock_state", AsyncMock(return_value=False))

    result = await probe.probe_submission_readiness(target_serial=WIFI_SERIAL)

    assert result.status == ProbeStatus.PASS
    assert result.metadata["active_device"]["serial"] == WIFI_SERIAL
    assert result.metadata["active_device"]["is_locked"] is False


@pytest.mark.asyncio
async def test_submission_gate_can_target_wireless_among_usb(monkeypatch) -> None:
    """An explicit `device_serial` selects the wireless device over a USB one."""
    probe = AdbDeviceProbe()
    monkeypatch.setattr(
        "artemis.core.diagnostics.probes.adb_probe.toolchain.resolve",
        lambda name: "adb",
    )
    monkeypatch.setattr(
        probe,
        "_get_device_states",
        AsyncMock(return_value=[("1A2B3C4D5E", "device"), (WIFI_SERIAL, "device")]),
    )
    monkeypatch.setattr(probe, "_get_confirmed_device_lock_state", AsyncMock(return_value=False))

    result = await probe.probe_submission_readiness(target_serial=WIFI_SERIAL)

    assert result.metadata["active_device"]["serial"] == WIFI_SERIAL


def test_wireless_connect_targets_the_local_adb_server() -> None:
    """`adb connect ip:port` runs against the local server socket."""
    command = AdbSession(AdbEndpoint.local()).command(["connect", WIFI_SERIAL])

    assert command[command.index("-H") + 1] == "127.0.0.1"
    assert command[command.index("-P") + 1] == "5037"
    assert command[-1] == WIFI_SERIAL
