import pytest

from dusterscan import safety
from dusterscan.elm327 import ELM327
from dusterscan.safety import WriteBlockedError

ALLOWED = [
    "ATZ", "ATE0", "ATL0", "ATS1", "ATH1", "ATI", "ATRV", "ATDPN", "ATSP0", "ATSP6",
    "ATST32", "ATSH7E0", "ATSH 7DF", "ATCRA7E8", "ATCRA", "ATFCSH740", "ATFCSD300000",
    "ATFCSM1", "ATSH18DB33F1",
    "0100", "0101", "010C", "03", "07", "0A", "020200", "0902", "0600", "06A2",
    "2180", "22F190", "22 F1 87", "1902FF", "17FF00", "1800FF00",
    "1001", "1003", "1081", "10C0", "3E00",
]

BLOCKED = [
    "04",                       # OBD: clear DTCs / turn off MIL
    "08",                       # OBD: control on-board system
    "14FFFFFF",                 # UDS: clear diagnostic information
    "14FF00",                   # KWP: clear diagnostic information
    "2EF19001020304",           # write data by identifier
    "3B0102",                   # write data by local identifier
    "3D01020304",               # write memory
    "310100FF",                 # routine control
    "2701",                     # security access
    "2F010203",                 # IO control
    "30010203",                 # IO control by local id
    "1101",                     # ECU reset
    "1002",                     # programming session
    "1085",                     # KWP programming session
    "3401",                     # request download
    "3601",                     # transfer data
    "8502",                     # control DTC setting
    "2801",                     # communication control
    "23010203",                 # read memory by address (not needed)
    "FF",                       # unknown
    "22F190AABBCCDDEE",         # too long for any read
    "ATPP0CSV23",               # writes adapter EEPROM
    "ATMA",                     # monitor all
    "ATBRD23",                  # baud rate change
    "ATSH1A0",                  # non-diagnostic CAN ID
    "ATSH12345678",             # non-diagnostic 29-bit header
    "AT SD 01",                 # save data byte (adapter EEPROM)
    "hello",
    "",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowed(cmd):
    assert safety.check_command(cmd)


@pytest.mark.parametrize("cmd", BLOCKED)
def test_blocked(cmd):
    with pytest.raises(WriteBlockedError):
        safety.check_command(cmd)


class RecordingPort:
    def __init__(self):
        self.written = []

    in_waiting = 0

    def write(self, data):
        self.written.append(data)

    def read(self, size=1):
        return b">"

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


@pytest.mark.parametrize("cmd", ["04", "14FFFFFF", "2EF19001", "3101FF00", "ATPP0CSV23"])
def test_driver_never_writes_blocked_commands(cmd):
    port = RecordingPort()
    elm = ELM327(port)
    with pytest.raises(WriteBlockedError):
        elm.request(cmd)
    with pytest.raises(WriteBlockedError):
        elm.command(cmd)
    assert port.written == []
