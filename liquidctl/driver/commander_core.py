"""liquidctl drivers for Corsair Commander Core.

Supported devices:

- Corsair Commander Core

Copyright ParkerMc and contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

import logging
import re
import time
from contextlib import contextmanager

from liquidctl.driver.usb import UsbHidDriver
from liquidctl.error import ExpectationNotMet, NotSupportedByDevice
from liquidctl.keyval import RuntimeStorage
from liquidctl.util import clamp, u16le_from

_LOGGER = logging.getLogger(__name__)

_REPORT_LENGTH = 96

# The Commander ST (1b1c:0c32) advertises 64-byte HID reports.  One MORE packet
# therefore only carries 61 of the 211 bytes needed for the seven-channel fan
# curve, so keep reading until the largest payload fits.
_COMMANDER_ST_PRODUCT_ID = 0x0C32
_COMMANDER_ST_REPORT_LENGTH = 64
_COMMANDER_ST_READ_MORE_PACKETS = 3

_INTERFACE_NUMBER = 0

_FAN_COUNT = 6
_MAX_CHANNELS = _FAN_COUNT + 1

# The Commander ST emits unsolicited reports, and a stale reply can be queued
# between clear_enqueued_reports() and read().  Keep the wait for the actual
# response bounded so a desynchronized device fails cleanly instead of hanging.
_MAX_RESPONSE_RETRIES = 8

# A second process (for example a fan/telemetry daemon) can talk to the same
# hidraw node.  Its replies are indistinguishable from ours when both use the
# same command byte, so the only reliable recovery is to retry the whole
# endpoint transaction until the data type matches.
_READ_ATTEMPTS = 4
_READ_RETRY_DELAY = 0.2

_CMD_WAKE = (0x01, 0x03, 0x00, 0x02)
_CMD_SLEEP = (0x01, 0x03, 0x00, 0x01)
_CMD_GET_FIRMWARE = (0x02, 0x13)
_CMD_CLOSE_ENDPOINT = (0x05, 0x01, 0x00)
_CMD_OPEN_ENDPOINT = (0x0d, 0x00)
_CMD_READ_INITIAL = (0x08, 0x00, 0x01)
_CMD_READ_MORE = (0x08, 0x00, 0x02)
_CMD_READ_FINAL = (0x08, 0x00, 0x03)
_CMD_WRITE = (0x06, 0x00)
_CMD_WRITE_MORE = (0x07, 0x00)

# The color endpoint uses a different close command than the data endpoints.
_CMD_CLOSE_COLOR_ENDPOINT = (0x05, 0x01, 0x01)
_MODE_SET_COLOR = (0x22,)
_DATA_TYPE_SET_COLOR = (0x12, 0x00)
_COLOR_CHUNK_SIZE = 61

_MODE_LED_COUNT = (0x20,)
_MODE_GET_SPEEDS = (0x17,)
_MODE_GET_TEMPS = (0x21,)
_MODE_CONNECTED_SPEEDS = (0x1a,)
_MODE_HW_SPEED_MODE = (0x60, 0x6d)
_MODE_HW_FIXED_PERCENT = (0x61, 0x6d)
_MODE_HW_CURVE_PERCENT = (0x62, 0x6d)

_DATA_TYPE_SPEEDS = (0x06, 0x00)
_DATA_TYPE_LED_COUNT = (0x0f, 0x00)
_DATA_TYPE_TEMPS = (0x10, 0x00)
_DATA_TYPE_CONNECTED_SPEEDS = (0x09, 0x00)
_DATA_TYPE_HW_SPEED_MODE = (0x03, 0x00)
_DATA_TYPE_HW_FIXED_PERCENT = (0x04, 0x00)
_DATA_TYPE_HW_CURVE_PERCENT = (0x05, 0x00)

_FAN_MODE_FIXED_PERCENT = 0x00
_FAN_MODE_CURVE_PERCENT = 0x02


class _DeviceBusy(ExpectationNotMet):
    """The device did not answer us, most likely because another client did."""

    def __str__(self):
        return self.args[0] if self.args else 'device is answering another client'


def _parse_speeds(data):
    """Parse a current-speeds payload, ignoring incomplete trailing records."""
    if not data:
        return []

    # Each record is an u16le; never trust the advertised count beyond the
    # number of complete records actually present.
    count = min(data[0], (len(data) - 1) // 2)
    speeds_data = data[1:1 + count * 2]
    return [u16le_from(speeds_data, offset=i * 2) for i in range(count)]


def _parse_temps(data):
    """Parse a temperatures payload, ignoring incomplete trailing records."""
    if not data:
        return []

    # Each record is a status byte followed by an u16le in tenths of a degree.
    count = min(data[0], (len(data) - 1) // 3)
    temp_data = data[1:1 + count * 3]
    temps = []
    for i in range(count):
        if temp_data[i * 3] == 0x00:
            temps.append(u16le_from(temp_data, offset=i * 3 + 1) / 10)
        else:
            temps.append(None)
    return temps


class CommanderCore(UsbHidDriver):
    """Corsair Commander Core"""

    # For a non-exhaustive list of issues, see: #520, #583, #598, #623, #705
    _MATCHES = [
        (0x1b1c, 0x0c1c, 'Corsair Commander Core (broken)', {"has_pump": True}),
        (0x1b1c, 0x0c2a, 'Corsair Commander Core XT (broken)', {"has_pump": False}),
        (0x1b1c, 0x0c32, 'Corsair Commander ST', {"has_pump": True}),
    ]

    def __init__(self, device, description, has_pump, **kwargs):
        super().__init__(device, description, **kwargs)
        self._has_pump = has_pump
        # initialized in connect()
        self._data = None

    def connect(self, runtime_storage=None, **kwargs):
        """Connect to the device."""
        ret = super().connect(**kwargs)
        if runtime_storage:
            self._data = runtime_storage
        else:
            ids = f'vid{self.vendor_id:04x}_pid{self.product_id:04x}'
            loc = 'loc' + '_'.join(re.findall(r'\d+', self.address))
            self._data = RuntimeStorage(key_prefixes=[ids, loc])
        return ret

    def initialize(self, **kwargs):
        """Initialize the device and get the fan modes."""

        with self._wake_device_context():
            # Get Firmware
            res = self._send_command(_CMD_GET_FIRMWARE)
            if len(res) < 6:
                raise ExpectationNotMet('device returned truncated firmware response')
            fw_version = (res[3], res[4], res[5])

            status = [('Firmware version', '{}.{}.{}'.format(*fw_version), '')]

            # Get LEDs per fan.  The first byte of each record is the connection
            # status (2 = connected); the second byte can carry a device type
            # (the ST reports 0x03 for the pump), so it must not be folded into
            # the status.
            for i, (connected, num_leds) in enumerate(self._get_led_channels()):
                if self._has_pump:
                    label = 'AIO LED count' if i == 0 else f'RGB port {i} LED count'
                else:
                    label = f'RGB port {i+1} LED count'

                status += [(label, num_leds if connected else None, '')]

            # Get what fans are connected
            res = self._read_data(_MODE_CONNECTED_SPEEDS, _DATA_TYPE_CONNECTED_SPEEDS)
            num_devices = min(res[0], _MAX_CHANNELS, len(res) - 1) if res else 0
            for i in range(0, num_devices):
                if self._has_pump:
                    label = 'AIO port connected' if i == 0 else f'Fan port {i} connected'
                else:
                    label = f'Fan port {i+1} connected'

                status += [(label, res[i + 1] == 0x07, '')]

            # Get what temp sensors are connected
            for i, temp in enumerate(self._get_temps()):
                connected = temp is not None
                if self._has_pump:
                    label = 'Water temperature sensor' if i == 0 and self._has_pump else f'Temperature sensor {i}'
                else:
                    label = f'Temperature sensor {i+1}'

                status += [(label, connected, '')]

        return status

    def get_status(self, **kwargs):
        """Get all the fan speeds and temps"""
        status = []

        with self._wake_device_context():
            for i, speed in enumerate(self._get_speeds()):
                if self._has_pump:
                    label = 'Pump speed' if i == 0 else f'Fan speed {i}'
                else:
                    label = f'Fan speed {i+1}'

                status += [(label, speed, 'rpm')]

            for i, temp in enumerate(self._get_temps()):
                if temp is None:
                    continue

                if self._has_pump:
                    label = 'Water temperature' if i == 0 else f'Temperature {i}'
                else:
                    label = f'Temperature {i}'

                status += [(label, temp, '°C')]

        return status

    def set_color(self, channel, mode, colors, **kwargs):
        """Set static colors for the pump ring or a fan LED channel.

        | Channel | Mode        | Colors |
        | ------- | ----------- | ------ |
        | pump    | off         |      0 |
        | pump    | fixed       |      1 |
        | pump    | super-fixed |   0..N |
        | fan1-6  | off         |      0 |
        | fan1-6  | fixed       |      1 |
        | fan1-6  | super-fixed |   0..N |

        `fixed` applies one color to every LED in the channel.  `super-fixed`
        applies one color per LED; LEDs without a color are turned off.

        The device expects a frame containing every LED of every channel, so
        liquidctl remembers the colors it has written per channel (in its
        runtime storage) and merges them into each update.  Channels that have
        not been configured by liquidctl in this session are written as black.
        """
        colors = list(colors)
        self._check_color_args(channel, mode, colors)
        index = self._parse_led_channel(channel)

        with self._wake_device_context():
            led_channels = self._get_led_channels()
            if index >= len(led_channels):
                raise NotSupportedByDevice(f'channel {channel} has no LEDs')
            connected, count = led_channels[index]
            if not connected or count == 0:
                raise NotSupportedByDevice(f'channel {channel} is not connected')

            if mode == 'off':
                channel_colors = [(0, 0, 0)] * count
            elif mode == 'fixed':
                channel_colors = [colors[0]] * count
            else:  # super-fixed
                channel_colors = colors[:count]
                channel_colors += [(0, 0, 0)] * (count - len(channel_colors))

            cached = self._data.load('led_colors', of_type=dict, default={})
            name = self._led_channel_name(index)
            cached[name] = [list(color) for color in channel_colors]

            payload = bytearray()
            for i, (is_connected, num_leds) in enumerate(led_channels):
                if not is_connected or num_leds == 0:
                    continue
                values = cached.get(self._led_channel_name(i), [])
                for led in range(num_leds):
                    rgb = values[led] if led < len(values) else (0, 0, 0)
                    payload.extend(rgb)

            self._write_color_frame(bytes(payload))
            self._data.store('led_colors', cached)
            self._data.store('led_frame', bytes(payload).hex())

    def _check_color_args(self, channel, mode, colors):
        if mode not in ('off', 'fixed', 'super-fixed'):
            raise ValueError(
                f"unknown mode: {mode!r}, should be one of: 'off', 'fixed', 'super-fixed'"
            )
        if mode == 'off':
            if colors:
                raise ValueError('off mode does not take colors')
        elif mode == 'fixed':
            if len(colors) != 1:
                raise ValueError('fixed mode requires exactly one color')
        elif not colors:
            raise ValueError('super-fixed mode requires at least one color')

        for color in colors:
            if len(color) != 3 or not all(0 <= component <= 255 for component in color):
                raise ValueError(f'invalid color: {color!r}')
        self._parse_led_channel(channel)

    def _parse_led_channel(self, channel):
        if self._has_pump and channel == 'pump':
            return 0
        if channel.startswith('fan') and channel[3:].isdigit():
            fan = int(channel[3:])
            if 1 <= fan <= _FAN_COUNT:
                return self._fan_to_channel(fan)

        names = (['pump'] if self._has_pump else []) + [
            f'fan{i}' for i in range(1, _FAN_COUNT + 1)
        ]
        raise ValueError(
            'unknown channel, should be one of: ' + ', '.join(f'"{name}"' for name in names)
        )

    def _led_channel_name(self, index):
        if self._has_pump:
            return 'pump' if index == 0 else f'fan{index}'
        return f'fan{index + 1}'

    def _write_color_frame(self, colors):
        frame = bytearray(2 + 2 + len(_DATA_TYPE_SET_COLOR) + len(colors))
        frame[0:2] = int.to_bytes(
            len(colors) + len(_DATA_TYPE_SET_COLOR), length=2, byteorder='little'
        )
        frame[4:4 + len(_DATA_TYPE_SET_COLOR)] = _DATA_TYPE_SET_COLOR
        frame[4 + len(_DATA_TYPE_SET_COLOR):] = colors

        self._send_command(_CMD_CLOSE_COLOR_ENDPOINT)
        self._send_command(_CMD_OPEN_ENDPOINT, _MODE_SET_COLOR)
        try:
            for offset in range(0, len(frame), _COLOR_CHUNK_SIZE):
                chunk = frame[offset:offset + _COLOR_CHUNK_SIZE]
                command = _CMD_WRITE if offset == 0 else _CMD_WRITE_MORE
                self._send_command(command, chunk)
        finally:
            self._send_command(_CMD_CLOSE_COLOR_ENDPOINT)

    def set_speed_profile(self, channel, profile, **kwargs):
        channels = self._parse_channels(channel)
        curve_points = list(profile)
        if len(curve_points) < 2:
            ValueError('a minimum of 2 speed curve points must be configured.')
        if len(curve_points) > 7:
            ValueError('a maximum of 7 speed curve points may be configured.')

        with self._wake_device_context():
            # Set hardware speed mode
            res = self._read_data(_MODE_HW_SPEED_MODE, _DATA_TYPE_HW_SPEED_MODE)
            device_count = res[0]

            data = bytearray(res[0:device_count + 1])
            for chan in channels:
                data[chan + 1] = _FAN_MODE_CURVE_PERCENT
            self._write_data(_MODE_HW_SPEED_MODE, _DATA_TYPE_HW_SPEED_MODE, data)


            # Read in data and split by device
            res = self._read_data(_MODE_HW_CURVE_PERCENT, _DATA_TYPE_HW_CURVE_PERCENT)
            if not res:
                raise ExpectationNotMet('device returned empty fan curve data')
            device_count = min(res[0], _MAX_CHANNELS)
            data_by_device = []

            i = 1
            for _ in range(0, device_count):
                if i + 1 >= len(res):
                    raise ExpectationNotMet('device returned truncated fan curve data')
                count = res[i+1]
                start = i
                end = i + 4 * count + 2
                if end > len(res):
                    raise ExpectationNotMet('device returned truncated fan curve data')
                i = end
                data_by_device.append(res[start:end])

            # Modify data for channels in channels array
            for chan in channels:
                new_data = []

                # set temperature sensor
                new_data.append(b"\x00")

                # set number of curve points
                new_data.append(int.to_bytes(len(curve_points), length=1, byteorder="big"))

                # set curve points
                for (temp, duty) in curve_points:
                    new_data.append(int.to_bytes(temp*10, length=2, byteorder="little", signed=False))
                    new_data.append(int.to_bytes(clamp(duty, 0, 100), length=2, byteorder="little", signed=False))

                # Update device data
                data_by_device[chan] = b''.join(new_data)

            out = bytes([device_count]) + b''.join(data_by_device)
            self._write_data(_MODE_HW_CURVE_PERCENT, _DATA_TYPE_HW_CURVE_PERCENT, out)

    def set_fixed_speed(self, channel, duty, **kwargs):
        channels = self._parse_channels(channel)

        with self._wake_device_context():
            # Set hardware speed mode
            res = self._read_data(_MODE_HW_SPEED_MODE, _DATA_TYPE_HW_SPEED_MODE)
            device_count = res[0]

            data = bytearray(res[0:device_count + 1])
            for chan in channels:
                data[chan + 1] = _FAN_MODE_FIXED_PERCENT
            self._write_data(_MODE_HW_SPEED_MODE, _DATA_TYPE_HW_SPEED_MODE, data)

            # Set speed
            res = self._read_data(_MODE_HW_FIXED_PERCENT, _DATA_TYPE_HW_FIXED_PERCENT)
            device_count = res[0]
            data = bytearray(res[0:device_count * 2 + 1])
            duty_le = int.to_bytes(clamp(duty, 0, 100), length=2, byteorder="little", signed=False)
            for chan in channels:
                i = chan * 2 + 1
                data[i: i + 2] = duty_le  # Update the device speed
            self._write_data(_MODE_HW_FIXED_PERCENT, _DATA_TYPE_HW_FIXED_PERCENT, data)

    @classmethod
    def probe(cls, handle, **kwargs):
        """Ensure we get the right interface"""

        if handle.hidinfo['interface_number'] != _INTERFACE_NUMBER:
            return

        yield from super().probe(handle, **kwargs)

    def _get_speeds(self):
        return _parse_speeds(self._read_data(_MODE_GET_SPEEDS, _DATA_TYPE_SPEEDS))

    def _get_temps(self):
        return _parse_temps(self._read_data(_MODE_GET_TEMPS, _DATA_TYPE_TEMPS))

    def _get_led_channels(self):
        """Return [(connected, led_count), ...] for every LED channel."""
        res = self._read_data(_MODE_LED_COUNT, _DATA_TYPE_LED_COUNT)
        if not res:
            return []
        num_devices = min(res[0], _MAX_CHANNELS, (len(res) - 1) // 4)
        channels = []
        for i in range(num_devices):
            offset = 1 + i * 4
            connected = res[offset] == 2
            num_leds = u16le_from(res, offset=offset + 2)
            channels.append((connected, num_leds))
        return channels

    def _report_length(self):
        """HID report size advertised by this device."""
        if getattr(self.device, 'product_id', None) == _COMMANDER_ST_PRODUCT_ID:
            return _COMMANDER_ST_REPORT_LENGTH
        return _REPORT_LENGTH

    def _read_more_packets(self):
        if self._report_length() == _COMMANDER_ST_REPORT_LENGTH:
            return _COMMANDER_ST_READ_MORE_PACKETS
        return 1

    def _read_data(self, mode, data_type):
        error = 'device returned no data'
        for attempt in range(_READ_ATTEMPTS):
            if attempt:
                time.sleep(_READ_RETRY_DELAY)

            self._send_command(_CMD_OPEN_ENDPOINT, mode)
            raw_data = self._send_command(_CMD_READ_INITIAL)
            more_raw = [
                self._send_command(_CMD_READ_MORE)
                for _ in range(self._read_more_packets())
            ]
            final_raw_data = self._send_command(_CMD_READ_FINAL)
            self._send_command(_CMD_CLOSE_ENDPOINT)

            if len(raw_data) < 5:
                error = 'device returned truncated data header'
                continue
            if tuple(raw_data[3:5]) != data_type:
                error = 'device returned incorrect data type'
                continue

            data = bytearray(raw_data[5:])
            for packet in more_raw:
                data.extend(packet[3:])
            data.extend(final_raw_data[3:])
            return bytes(data)

        raise _DeviceBusy(
            f'{error}; retried {_READ_ATTEMPTS} times (another program may be using the device)'
        )

    def _send_command(self, command, data=()):
        # self.device.write expects buf[0] to be the report number or 0 if not used
        buf = bytearray(self._report_length() + 1)

        # buf[1] when going out is always 08
        buf[1] = 0x08

        # Indexes for the buffer
        cmd_start = 2
        data_start = cmd_start + len(command)
        data_end = data_start + len(data)

        # Fill in the buffer
        if data_end > len(buf):
            raise ValueError('command and data exceed the HID report size')
        buf[cmd_start:data_start] = command
        buf[data_start:data_end] = data

        self.device.clear_enqueued_reports()
        self.device.write(buf)

        # A report may arrive between clear_enqueued_reports() and read(): the
        # Commander ST in particular sends unsolicited reports and can leave a
        # stale command echo queued.  Skip anything that is not the response to
        # this command, with a bounded budget so a confused device fails with a
        # clear error instead of spinning forever.
        for _ in range(_MAX_RESPONSE_RETRIES):
            res = self.device.read(self._report_length())
            if res and len(res) >= 2 and res[0] == 0x00 and res[1] == command[0]:
                return bytes(res)

        raise ExpectationNotMet('response does not match command')

    @contextmanager
    def _wake_device_context(self):
        try:
            self._send_command(_CMD_WAKE)
            yield
        finally:
            self._restore_led_frame()
            self._send_command(_CMD_SLEEP)

    def _restore_led_frame(self):
        """Re-apply the last lighting frame before the device goes to sleep.

        Waking the device and switching endpoints clears the software lighting
        frame, so a later `status`/`initialize` would otherwise turn the LEDs
        off.  Only replay a frame that this driver has written before.
        """
        if self._data is None:
            return
        frame = self._data.load('led_frame', of_type=str, default=None)
        if not frame:
            return
        try:
            self._write_color_frame(bytes.fromhex(frame))
        except Exception as error:
            _LOGGER.debug('could not restore the LED frame: %s', error)

    def _write_data(self, mode, data_type, data):
        self._read_data(mode, data_type)  # Will ensure we are writing the correct data type to avoid breakage

        self._send_command(_CMD_OPEN_ENDPOINT, mode)

        # Write data
        report_length = self._report_length()
        data_len = len(data)
        data_start_index = 0
        while (data_start_index < data_len):
            if (data_start_index == 0):
                # First 9 bytes are in use
                packet_data_len = report_length - 9

                if (data_len < packet_data_len):
                    packet_data_len = data_len

                # Num Data Length bytes + 0x05 + 0x06 + Num Data Type bytes + Num Data bytes
                buf = bytearray(2 + 2 + len(data_type) + packet_data_len)

                # Data Length value (includes data type length) - 0x03 and 0x04
                buf[0: 2] = int.to_bytes(data_len + len(data_type), length=2, byteorder="little", signed=False)
                # Data Type value - 0x07 and 0x08
                buf[4: 4 + len(data_type)] = data_type
                # Data - 0x09 onwards
                buf[4 + len(data_type):] = data[0:packet_data_len]

                self._send_command(_CMD_WRITE, buf)
                data_start_index += packet_data_len
            else:
                # First 3 bytes are in use
                packet_data_len = report_length - 3
                if data_len - data_start_index < packet_data_len:
                    packet_data_len = data_len - data_start_index

                self._send_command(_CMD_WRITE_MORE, data[data_start_index:data_start_index + packet_data_len])
                data_start_index += packet_data_len

        self._send_command(_CMD_CLOSE_ENDPOINT)

    def _fan_to_channel(self, fan):
        if self._has_pump:
            return fan
        else:
            # On devices without a pump, channel 0 is fan 1
            return fan - 1

    def _parse_channels(self, channel):
        if self._has_pump and channel == 'pump':
            return [0]
        elif channel == "fans":
            return [self._fan_to_channel(x) for x in range(1, _FAN_COUNT + 1)]
        elif channel.startswith("fan") and channel[3:].isnumeric() and 0 < int(channel[3:]) <= _FAN_COUNT:
            return [self._fan_to_channel(int(channel[3:]))]
        else:
            fan_names = ['fan' + str(i) for i in range(1, _FAN_COUNT + 1)]
            fan_names_part = '", "'.join(fan_names)
            if self._has_pump:
                fan_names.insert(0, "pump")
            raise ValueError(f'unknown channel, should be one of: "{fan_names_part}" or "fans"')

    def set_screen(self, channel, mode, value, **kwargs):
        """Not supported by this device."""
        raise NotSupportedByDevice
