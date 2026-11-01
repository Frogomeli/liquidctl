import pytest
from collections import deque

from liquidctl import ExpectationNotMet
from liquidctl.driver.commander_core import CommanderCore, _parse_speeds, _parse_temps
from _testutils import noop, MockRuntimeStorage
from liquidctl.util import u16le_from


def int_to_le(num, length=2, byteorder='little', signed=False):
    return int(num).to_bytes(length=length, byteorder=byteorder, signed=signed)


class MockCommanderCoreDevice:
    def __init__(self):
        self.vendor_id = 0x1b1c
        self.product_id = 0x0c1c
        self.address = 'addr'
        self.path = b'path'
        self.release_number = None
        self.serial_number = None
        self.bus = None
        self.port = None

        self.open = noop
        self.close = noop
        self.clear_enqueued_reports = noop

        self._read = deque()
        self.sent = list()

        self._last_write = bytes()
        self._modes = {}
        self._awake = False

        # The Commander ST uses 64-byte HID reports; Core/Core XT use 96.
        self.response_length = 96
        # Model the ST's small reports as real chunks instead of repeating the
        # whole payload on every read.
        self.chunked_reads = False
        self._read_offset = 0

        self._color_endpoint = False
        self.color_length = 0
        self.color_type = None
        self.color_data = bytearray()
        # Reports that are delivered ahead of the real command response.
        self.stale_reads = deque()

        self.response_prefix = ()
        self.firmware_version = (0x00, 0x00, 0x00)
        self.led_counts = (None, None, None, None, None, None, None)
        # Per-channel LED status byte; None keeps the legacy connected/disconnected
        # convention (2 when a count is set, 3 otherwise).
        self.led_statuses = (None, None, None, None, None, None, None)
        self.speeds_mode = (0, 0, 0, 0, 0, 0, 0)
        self.speeds = (None, None, None, None, None, None, None)
        self.fixed_speeds = (0, 0, 0, 0, 0, 0, 0)
        self.temperatures = (None, None)
        self.curve_points_by_device = [[],[],[],[],[],[],[]]

    def read(self, length):
        if self.stale_reads:
            return list(self.stale_reads.popleft())[:length]

        data = bytearray([0x00, self._last_write[2], 0x00])
        data.extend(self.response_prefix)

        if self._last_write[2] == 0x02:  # Firmware version
            for i in range(0, 3):
                data.append(self.firmware_version[i])
        if self._awake:
            if self._last_write[2] == 0x08 or self._last_write[2] == 0x09:  # Get data
                channel = self._last_write[3]
                mode = self._modes.get(channel)
                if mode[1] == 0x00:
                    if mode[0] == 0x17:  # Get speeds
                        data.extend([0x06, 0x00])
                        data.append(len(self.speeds))
                        for i in self.speeds:
                            if i is None:
                                data.extend([0x00, 0x00])
                            else:
                                data.extend(int_to_le(i))
                    elif mode[0] == 0x1a:  # Speed devices connected
                        data.extend([0x09, 0x00])
                        data.append(len(self.speeds))
                        for i in self.speeds:
                            data.extend([0x01 if i is None else 0x07])
                    elif mode[0] == 0x20:  # LED detect
                        data.extend([0x0f, 0x00])
                        data.append(len(self.led_counts))
                        for i, count in enumerate(self.led_counts):
                            status = self.led_statuses[i]
                            if status is None:
                                status = 2 if count is not None else 3
                            data.extend(int_to_le(status))
                            data.extend(int_to_le(count or 0))
                    elif mode[0] == 0x21:  # Get temperatures
                        data.extend([0x10, 0x00])
                        data.append(len(self.temperatures))
                        for i in self.temperatures:
                            if i is None:
                                data.append(1)
                                data.extend(int_to_le(0))
                            else:
                                data.append(0)
                                data.extend(int_to_le(int(i*10)))
                    else:
                        raise NotImplementedError(f'Read for {mode.hex(":")}')
                elif mode[1] == 0x6d:
                    if mode[0] == 0x60:
                        data.extend([0x03, 0x00])
                        data.append(len(self.speeds_mode))
                        for i in self.speeds_mode:
                            data.append(i)
                    elif mode[0] == 0x61:
                        data.extend([0x04, 0x00])
                        data.append(len(self.fixed_speeds))
                        for i in self.fixed_speeds:
                            data.extend(int_to_le(i))
                    elif mode[0] == 0x62:
                        data.extend([0x05, 0x00]) # data type
                        num_ports = len(self.curve_points_by_device)
                        data.append(num_ports)
                        for i in range(num_ports):
                            data.append(0) # temp sensor
                            data.append(len(self.curve_points_by_device[i])) # num curve points
                            for (temp, duty) in self.curve_points_by_device[i]:
                                data.extend(int_to_le(temp * 10)) # temperature
                                data.extend(int_to_le(duty)) # duty
                    else:
                        raise NotImplementedError(f'Read for {mode.hex(":")}')
                else:
                    raise NotImplementedError(f'Read for {mode.hex(":")}')
            elif self._last_write[2] == 0x09:  # Get more data
                channel = self._last_write[3]
                mode = self._modes.get(channel)
                if mode[1] == 0x6d:
                    if mode[0] == 0x62:
                        for i in range(3, 7):
                            data.append(0) # temp sensor
                            data.append(len(self.curve_points_by_device[i])) # num curve points
                            for (temp, duty) in self.curve_points_by_device[i]:
                                data.extend(int_to_le(temp * 10))
                                data.extend(int_to_le(duty))



        if self.chunked_reads and self._last_write[2] == 0x08:
            payload = bytes(data[3:])
            if self._last_write[4] == 0x01:  # initial read starts a transaction
                capacity = self.response_length - 5
                self._read_offset = 0
            else:
                capacity = self.response_length - 3
            chunk = payload[self._read_offset:self._read_offset + capacity]
            self._read_offset += len(chunk)
            data = bytearray(data[:3]) + chunk

        return list(data)[:min(length, self.response_length)]

    def write(self, data):
        data = bytes(data)  # ensure data is convertible to bytes
        self._last_write = data
        self.sent.append(data)
        if data[0] != 0x00 or data[1] != 0x08:
            raise ValueError('Start of packets going out should be 00:08')
        if data[2] == 0x0d and data[3] == 0x00 and data[4] == 0x22:
            self._color_endpoint = True
            return len(data)
        if data[2] == 0x05 and data[3] == 0x01 and data[4] == 0x01:
            self._color_endpoint = False
            return len(data)
        if self._color_endpoint and data[2] in (0x06, 0x07):
            if data[2] == 0x06:
                self.color_length = u16le_from(data[4:6])
                self.color_type = data[8:10]
                self.color_data = bytearray(data[10:8 + self.color_length])
            else:
                self.color_data.extend(data[4:])
            expected = self.color_length - 2
            if len(self.color_data) > expected:
                self.color_data = self.color_data[:expected]
            return len(data)
        if data[2] == 0x0d:
            channel = data[3]
            self._read_offset = 0
            if  self._modes.get(channel) is None:
                self._modes[channel] = data[4:6]
            else:
                raise ExpectationNotMet('Previous channel was not reset')
        elif data[2] == 0x05 and data[3] == 0x01:
            self._modes[data[4]] = None
        elif data[2] == 0x01 and data[3] == 0x03 and data[4] == 0x00:
            self._awake = data[5] == 0x02
        elif self._awake:
            channel = data[3]
            mode = self._modes.get(channel)

            if data[2] == 0x06:  # Write command
                self.data_length = u16le_from(data[4:6])
                self.written_data_type = data[8:10]
                self.written_data = data[10:8+self.data_length]
                if len(self.written_data) < self.data_length - 2:
                    return
            if data[2] == 0x07: # Write More command
                self.written_data += data[4:]
                if len(self.written_data) < self.data_length - 2:
                    return

            if data[2] == 0x06 or data[2] == 0x07:
                if mode[1] == 0x6d:
                    if mode[0] == 0x60 and list(self.written_data_type) == [0x03, 0x00]:
                        self.speeds_mode = tuple(self.written_data[i+1] for i in range(0, self.written_data[0]))
                    elif mode[0] == 0x61 and list(self.written_data_type) == [0x04, 0x00]:
                        self.fixed_speeds = tuple(u16le_from(self.written_data[i*2+1:i*2+3]) for i in range(0, self.written_data[0]))
                    elif mode[0] == 0x62 and list(self.written_data_type) == [0x05, 0x00]:
                        curve_index = 1
                        for port_index in range(0, self.written_data[0]):
                            self.curve_points_by_device[port_index] = []
                            # get number of curve points
                            num_points = self.written_data[curve_index + 1]

                            # store temperature and duty
                            for i in range(num_points):
                                point_index = curve_index + 2 + i*4
                                cp_temp = u16le_from(self.written_data[point_index: point_index + 2]) / 10
                                cp_duty = u16le_from(self.written_data[point_index + 2: point_index + 4])
                                self.curve_points_by_device[port_index].append((cp_temp, cp_duty))

                            # update index to next curve
                            curve_index += 4 * num_points + 2
                    else:
                        raise NotImplementedError('Invalid Write command')
                else:
                    raise NotImplementedError('Invalid Write command')

        return len(data)



@pytest.fixture
def commander_core_device():
    device = MockCommanderCoreDevice()
    core = CommanderCore(device, 'Corsair Commander Core', True)
    core.connect(runtime_storage=MockRuntimeStorage(key_prefixes=['testing']))
    return core


@pytest.fixture
def commander_st_device():
    device = MockCommanderCoreDevice()
    device.product_id = 0x0c32
    device.response_length = 64
    device.chunked_reads = True
    core = CommanderCore(device, 'Corsair Commander ST', True)
    core.connect(runtime_storage=MockRuntimeStorage(key_prefixes=['testing']))
    return core


def test_initialize_commander_st_64_byte_reports(commander_st_device):
    """Initialization must work with the Commander ST's 64-byte HID reports."""
    device = commander_st_device.device
    device.firmware_version = (0x02, 0x00, 0x13)
    device.led_counts = (29, 8, 8, None, None, None, None)
    device.speeds = (2553, 1074, 1051, 1054, None, None, None)
    device.temperatures = (34.8, None)

    res = commander_st_device.initialize()

    assert len(res) == 17
    assert res[0][1] == '2.0.19'

    # LED counts for pump head and the three connected fan ports
    assert res[1][1] == 29
    assert res[2][1] == 8
    assert res[3][1] == 8
    assert res[4][1] is None

    # Connected speed devices
    assert res[8][1] is True   # AIO/EXT port
    assert res[9][1] is True   # fan 1
    assert res[10][1] is True  # fan 2
    assert res[11][1] is True  # fan 3
    assert res[12][1] is False  # fan 4

    # Coolant sensor present, probe absent
    assert res[15][1] is True
    assert res[16][1] is False

    assert not device._awake


def test_initialize_commander_st_pump_led_count(commander_st_device):
    """The ST reports the pump status as 0x0302, with 24 LEDs."""
    device = commander_st_device.device
    device.firmware_version = (0x02, 0x00, 0x13)
    device.led_counts = (24, 8, 8, None, None, None, None)
    device.led_statuses = (0x0302, None, None, None, None, None, None)
    device.speeds = (2500, 1000, 1000, None, None, None, None)
    device.temperatures = (30.0, None)

    res = commander_st_device.initialize()

    assert res[1] == ('AIO LED count', 24, '')
    assert res[2] == ('RGB port 1 LED count', 8, '')


def test_set_color_commander_st_pump_fixed(commander_st_device):
    """A fixed pump color is expanded to all 24 pump LEDs."""
    device = commander_st_device.device
    device.led_counts = (24, 8, 8, None, None, None, None)

    commander_st_device.set_color('pump', 'fixed', [(255, 0, 0)])

    assert list(device.color_type) == [0x12, 0x00]
    assert len(device.color_data) == (24 + 8 + 8) * 3
    assert device.color_data[0:3] == b'\xff\x00\x00'
    assert device.color_data[24 * 3:24 * 3 + 3] == b'\x00\x00\x00'
    assert device.color_data[-3:] == b'\x00\x00\x00'


def test_set_color_preserves_cached_channels(commander_st_device):
    """Configuring one channel must not clear the others configured earlier."""
    device = commander_st_device.device
    device.led_counts = (24, 8, 8, None, None, None, None)

    commander_st_device.set_color('pump', 'fixed', [(255, 0, 0)])
    commander_st_device.set_color('fan1', 'fixed', [(0, 255, 0)])

    assert len(device.color_data) == (24 + 8 + 8) * 3
    assert device.color_data[0:3] == b'\xff\x00\x00'
    assert device.color_data[24 * 3:24 * 3 + 3] == b'\x00\xff\x00'
    assert device.color_data[32 * 3:32 * 3 + 3] == b'\x00\x00\x00'


def test_set_color_off_commander_st_pump(commander_st_device):
    device = commander_st_device.device
    device.led_counts = (24, 8, None, None, None, None, None)

    commander_st_device.set_color('pump', 'fixed', [(10, 20, 30)])
    commander_st_device.set_color('pump', 'off', [])

    assert len(device.color_data) == (24 + 8) * 3
    assert device.color_data[0:3] == b'\x00\x00\x00'


def test_set_color_super_fixed_commander_st(commander_st_device):
    device = commander_st_device.device
    device.led_counts = (4, None, None, None, None, None, None)

    commander_st_device.set_color('pump', 'super-fixed', [(1, 2, 3), (4, 5, 6)])

    assert device.color_data == bytes([1, 2, 3, 4, 5, 6, 0, 0, 0, 0, 0, 0])


def test_set_color_rejects_bad_channel(commander_st_device):
    with pytest.raises(ValueError, match='unknown channel'):
        commander_st_device.set_color('led1', 'fixed', [(1, 2, 3)])


def test_set_color_rejects_bad_mode(commander_st_device):
    device = commander_st_device.device
    device.led_counts = (4, None, None, None, None, None, None)
    with pytest.raises(ValueError, match='unknown mode'):
        commander_st_device.set_color('pump', 'rainbow', [])


def test_status_commander_st_64_byte_reports(commander_st_device):
    """Status must return coolant temperature, pump RPM and fan RPM on the ST."""
    device = commander_st_device.device
    device.speeds = (2553, 1074, 1051, 1054, 0, 0, 0)
    device.temperatures = (34.8, None)

    res = commander_st_device.get_status()

    assert res == [
        ('Pump speed', 2553, 'rpm'),
        ('Fan speed 1', 1074, 'rpm'),
        ('Fan speed 2', 1051, 'rpm'),
        ('Fan speed 3', 1054, 'rpm'),
        ('Fan speed 4', 0, 'rpm'),
        ('Fan speed 5', 0, 'rpm'),
        ('Fan speed 6', 0, 'rpm'),
        ('Water temperature', 34.8, '°C'),
    ]

    assert not device._awake


def test_status_skips_stale_report_commander_st(commander_st_device):
    """An unsolicited/stale report must not be mistaken for the command reply."""
    device = commander_st_device.device
    device.speeds = (2400, 900, 901, 902, 0, 0, 0)
    device.temperatures = (31.5, None)
    # This is what the ST can leave queued between clear_enqueued_reports()
    # and the first read of a new transaction.
    device.stale_reads.append([0x00, 0x05, 0x00] + [0x00] * 61)

    res = commander_st_device.get_status()

    assert res[0] == ('Pump speed', 2400, 'rpm')
    assert res[-1] == ('Water temperature', 31.5, '°C')
    assert not device._awake


def test_set_speed_profile_commander_st(commander_st_device):
    """The ST needs several MORE packets for its 211-byte fan curve payload."""
    device = commander_st_device.device
    original = [(25 + i, 20 + i * 10) for i in range(7)]
    device.curve_points_by_device = [list(original) for _ in range(7)]

    commander_st_device.set_speed_profile('fan1', [(30, 40), (50, 80)])

    assert device.curve_points_by_device[1] == [(30, 40), (50, 80)]
    assert device.curve_points_by_device[0] == original
    assert device.curve_points_by_device[6] == original
    # Every outgoing report must fit the ST's 64-byte HID descriptor.
    assert device.sent
    assert all(len(packet) <= 65 for packet in device.sent)


def test_status_retries_when_another_client_corrupts_reply(commander_st_device):
    """A concurrent hidraw client can make the reply carry the wrong data type."""
    device = commander_st_device.device
    device.speeds = (2400, 900, 901, 902, 0, 0, 0)
    device.temperatures = (31.5, None)

    real_read = device.read
    corrupted = {'done': False}

    def flaky_read(length):
        res = list(real_read(length))
        if not corrupted['done'] and device._last_write[2:5] == b'\x08\x00\x01':
            res[3:5] = [0x00, 0x00]
            corrupted['done'] = True
        return res[:length]

    device.read = flaky_read

    res = commander_st_device.get_status()

    assert corrupted['done']
    assert res[0] == ('Pump speed', 2400, 'rpm')
    assert res[-1] == ('Water temperature', 31.5, '°C')


def test_status_reports_concurrent_client_after_retries(commander_st_device):
    """Fail with an actionable message if the device keeps returning other data."""
    device = commander_st_device.device
    real_read = device.read

    def always_wrong(length):
        res = list(real_read(length))
        if device._last_write[2:5] == b'\x08\x00\x01':
            res[3:5] = [0x00, 0x00]
        return res[:length]

    device.read = always_wrong

    with pytest.raises(ExpectationNotMet, match='another program may be using the device'):
        commander_st_device.get_status()


def test_send_command_rejects_repeated_stale_reports(commander_core_device):
    """Give up with a driver error if the device never answers the command."""
    device = commander_core_device.device
    for _ in range(8):
        device.stale_reads.append([0x00, 0x05, 0x00] + [0x00] * 61)

    with pytest.raises(ExpectationNotMet):
        commander_core_device._send_command((0x01, 0x03, 0x00, 0x02))


def test_parse_speeds_ignores_incomplete_record():
    # Advertised count 3, but only two complete u16le records are present.
    data = bytes([0x03, 0x84, 0x09, 0xE9, 0x01, 0x0D])
    assert _parse_speeds(data) == [2436, 489]


def test_parse_temps_ignores_incomplete_record():
    # count=3; two complete records, the third is cut short.
    data = bytes([0x03, 0x00, 0x30, 0x01, 0x00, 0x35, 0x01])
    assert _parse_temps(data) == [30.4, 30.9]


def test_parse_temps_reports_disconnected_sensor():
    data = bytes([0x02, 0x01, 0x00, 0x00, 0x00, 0x30, 0x01])
    assert _parse_temps(data) == [None, 30.4]


def test_parse_empty_payloads():
    assert _parse_speeds(bytes()) == []
    assert _parse_temps(bytes()) == []


def test_initialize_commander_core(commander_core_device):
    commander_core_device.device.firmware_version = (0x01, 0x02, 0x21)
    commander_core_device.device.speeds = (None, 104, None, None, None, None, 918)
    commander_core_device.device.led_counts = (27, None, 1, 2, 4, 8, 16)
    commander_core_device.device.temperatures = (None, 45.6)
    res = commander_core_device.initialize()

    assert len(res) == 17

    assert res[0][1] == '1.2.33'  # Firmware

    # LED counts
    assert res[1][1] == 27
    assert res[2][1] is None
    assert res[3][1] == 1
    assert res[4][1] == 2
    assert res[5][1] == 4
    assert res[6][1] == 8
    assert res[7][1] == 16

    # Speed devices connected
    assert not res[8][1]
    assert res[9][1]
    assert not res[10][1]
    assert not res[11][1]
    assert not res[12][1]
    assert not res[13][1]
    assert res[14][1]

    # Temperature sensors connected
    assert not res[15][1]
    assert res[16][1]

    # Ensure device is asleep at end
    assert not commander_core_device.device._awake


def test_initialize_error_commander_core(commander_core_device):
    """This tests sends invalid data to ensure the device gets set back to hardware mode on error"""
    commander_core_device.device.response_prefix = (0x00, 0x00)

    with pytest.raises(ExpectationNotMet):
        commander_core_device.initialize()

    # Ensure device is asleep at end
    assert not commander_core_device.device._awake


def test_status_commander_core(commander_core_device):
    commander_core_device.device.speeds = (2357, 918, 903, 501, 1104, 1824, 104)
    commander_core_device.device.temperatures = (12.3, 45.6)
    res = commander_core_device.get_status()

    assert len(res) == 9

    # Speeds of pump and fans
    assert res[0][1] == 2357
    assert res[1][1] == 918
    assert res[2][1] == 903
    assert res[3][1] == 501
    assert res[4][1] == 1104
    assert res[5][1] == 1824
    assert res[6][1] == 104

    # Temperatures
    assert res[7][1] == 12.3
    assert res[8][1] == 45.6

    # Ensure device is asleep at end
    assert not commander_core_device.device._awake


def test_status_error_commander_core(commander_core_device):
    """This tests sends invalid data to ensure the device gets set back to hardware mode on error"""
    commander_core_device.device.response_prefix = (0x00, 0x00)

    with pytest.raises(ExpectationNotMet):
        commander_core_device.initialize()

    # Ensure device is asleep at end
    assert not commander_core_device.device._awake


def test_set_fixed_speed_fan2_commander_core(commander_core_device):
    """This tests setting the speed of a single channel"""
    commander_core_device.device.speeds_mode = (1, 2, 3, 4, 5, 6, 7)
    commander_core_device.device.fixed_speeds = (8, 9, 10, 11, 12, 13, 14)

    commander_core_device.set_fixed_speed('fan2', 95)

    assert commander_core_device.device.speeds_mode == (1, 2, 0, 4, 5, 6, 7)
    assert commander_core_device.device.fixed_speeds == (8, 9, 95, 11, 12, 13, 14)
    # Ensure device is asleep at end
    assert not commander_core_device.device._awake


def test_set_fixed_speed_fans_commander_core(commander_core_device):
    """This tests setting the speed of all the fans"""
    commander_core_device.device.speeds_mode = (1, 2, 3, 4, 5, 6, 7)
    commander_core_device.device.fixed_speeds = (8, 9, 10, 11, 12, 13, 14)

    commander_core_device.set_fixed_speed('fans', 61)

    assert commander_core_device.device.speeds_mode == (1, 0, 0, 0, 0, 0, 0)
    assert commander_core_device.device.fixed_speeds == (8, 61, 61, 61, 61, 61, 61)
    # Ensure device is asleep at end
    assert not commander_core_device.device._awake


def test_set_fixed_speed_error_commander_core(commander_core_device):
    """This tests sends invalid data to ensure the device gets set back to hardware mode on error"""
    commander_core_device.device.response_prefix = (0x00, 0x00)

    with pytest.raises(ExpectationNotMet):
        commander_core_device.set_fixed_speed('fan1', 95)

    # Ensure device is asleep at end
    assert not commander_core_device.device._awake


def test_set_speed_profile_fans_commander_core(commander_core_device):
    """This tests setting the speed of all the fans"""
    commander_core_device.device.speeds_mode = (0, 0, 0, 0, 0, 0, 0)
    commander_core_device.device.fixed_speeds = (8, 9, 10, 11, 12, 13, 14)

    commander_core_device.set_speed_profile('fans', [
        (22, 0), (32, 25), (34, 50), (36, 75), (38,85), (40,95), (42, 100)
    ])

    assert commander_core_device.device.speeds_mode == (0, 2, 2, 2, 2, 2, 2)
    assert commander_core_device.device.curve_points_by_device[0] == []
    assert commander_core_device.device.curve_points_by_device[1] == [(22, 0), (32, 25), (34, 50), (36, 75), (38, 85), (40, 95), (42, 100)]
    assert commander_core_device.device.curve_points_by_device[2] == [(22, 0), (32, 25), (34, 50), (36, 75), (38, 85), (40, 95), (42, 100)]
    assert commander_core_device.device.curve_points_by_device[3] == [(22, 0), (32, 25), (34, 50), (36, 75), (38, 85), (40, 95), (42, 100)]
    assert commander_core_device.device.curve_points_by_device[4] == [(22, 0), (32, 25), (34, 50), (36, 75), (38, 85), (40, 95), (42, 100)]
    assert commander_core_device.device.curve_points_by_device[5] == [(22, 0), (32, 25), (34, 50), (36, 75), (38, 85), (40, 95), (42, 100)]
    assert commander_core_device.device.curve_points_by_device[6] == [(22, 0), (32, 25), (34, 50), (36, 75), (38, 85), (40, 95), (42, 100)]

    # Ensure device is asleep at end
    assert not commander_core_device.device._awake

def test_set_speed_curve_profile_single_channel_commander_core(commander_core_device):
    """This tests setting the speed curve profile of a single fan"""
    commander_core_device.device.speeds_mode = (0, 0, 0, 0, 0, 0, 0)
    commander_core_device.device.fixed_speeds = (8, 9, 10, 11, 12, 13, 14)
    commander_core_device.device.curve_points_by_device[0] = []
    commander_core_device.device.curve_points_by_device[1] = []
    commander_core_device.device.curve_points_by_device[2] = []
    commander_core_device.device.curve_points_by_device[3] = []
    commander_core_device.device.curve_points_by_device[4] = []
    commander_core_device.device.curve_points_by_device[5] = []
    commander_core_device.device.curve_points_by_device[6] = []

    commander_core_device.set_speed_profile('pump', [(22, 0), (42, 100)])
    commander_core_device.set_speed_profile('fan1', [(22, 1), (42, 10)])
    commander_core_device.set_speed_profile('fan2', [(22, 2), (42, 20)])
    commander_core_device.set_speed_profile('fan3', [(22, 3), (42, 30)])
    commander_core_device.set_speed_profile('fan4', [(22, 4), (42, 40)])
    commander_core_device.set_speed_profile('fan5', [(22, 5), (42, 50)])
    commander_core_device.set_speed_profile('fan6', [(22, 6), (42, 60)])

    assert commander_core_device.device.speeds_mode == (2, 2, 2, 2, 2, 2, 2)
    assert commander_core_device.device.curve_points_by_device[0] == [(22, 0), (42, 100)]
    assert commander_core_device.device.curve_points_by_device[1] == [(22, 1), (42, 10)]
    assert commander_core_device.device.curve_points_by_device[2] == [(22, 2), (42, 20)]
    assert commander_core_device.device.curve_points_by_device[3] == [(22, 3), (42, 30)]
    assert commander_core_device.device.curve_points_by_device[4] == [(22, 4), (42, 40)]
    assert commander_core_device.device.curve_points_by_device[5] == [(22, 5), (42, 50)]
    assert commander_core_device.device.curve_points_by_device[6] == [(22, 6), (42, 60)]

    # Ensure device is asleep at end
    assert not commander_core_device.device._awake


def test_parse_channels_commander_core():
    """This test will go through and thoroughly test CommanderCore._parse_channels so we don't have to in other tests"""
    core = CommanderCore(MockCommanderCoreDevice(), 'Corsair Commander Core', True)
    tests = [
        ('pump', [0]), ('fans', [1, 2, 3, 4, 5, 6]),
        ('fan1', [1]), ('fan2', [2]), ('fan3', [3]), ('fan4', [4]), ('fan5', [5]), ('fan6', [6])
    ]

    for (val, answer) in tests:
        assert list(core._parse_channels(val)) == answer



def test_parse_channels_commander_core_xt():
    """Test Core XT-specific channel layout"""
    corext = CommanderCore(MockCommanderCoreDevice(), 'Corsair Commander Core', False)
    tests = [
        ('fans', [0, 1, 2, 3, 4, 5]),
        ('fan1', [0]), ('fan2', [1]), ('fan3', [2]), ('fan4', [3]), ('fan5', [4]), ('fan6', [5])
    ]

    for (val, answer) in tests:
        assert list(corext._parse_channels(val)) == answer


def test_parse_channels_error_commander_core():
    """This tests to make sure we get an error with an invalid channel"""
    core = CommanderCore(MockCommanderCoreDevice(), 'Corsair Commander Core', True)
    with pytest.raises(ValueError):
        core._parse_channels('fan')
