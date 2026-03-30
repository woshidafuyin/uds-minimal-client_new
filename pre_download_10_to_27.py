import sys
import can
import isotp
import time
import ctypes
import platform
import struct
import zlib

from udsoncan.connections import PythonIsoTpConnection
from udsoncan.client import Client
import udsoncan

print("Python exe   :", sys.executable)
print("Python arch  :", platform.architecture())
print("Pointer size :", struct.calcsize("P") * 8)

# =========================
# 配置
# =========================
CHANNEL = 0
BITRATE = 500000
DATA_BITRATE = 2000000

TX_PHYS = 0x772
RX_PHYS = 0x77A
TX_FUNC = 0x7DF

PADDING = 0x55

DLL_PATH = r"E:\lingpao\Flash2944_LP_ARC_V1.2_0x3E00_CAN_release\CAPL\EXECL\capldll_lingpao.dll"
DLL_FUNC_NAME = b"?CALkey@@YAXKQAE0@Z"

APP_S19_PATH = r"E:\lingpao\Flash2944_LP_ARC_V1.2_0x3E00_CAN_release\1\ARC3.31BC3_LPB10A_B1.00.01_APP_V2.00.00_CHF0369N_without_boot.s19"
DRIVER_SREC_PATH = r"E:\lingpao\Flash2944_LP_ARC_V1.2_0x3E00_CAN_release\1.00.00\FlashDriver.srec"
VERCHECK_ASC_PATH = r"E:\lingpao\Flash2944_LP_ARC_V1.2_0x3E00_CAN_release\1.00.00\LP-BSD080-BA_V9.99.99_R_RL_20250506.asc"

DRIVER_START_ADDRESS = 0x00000000
DRIVER_LENGTH = 0x00004000

APP_START_ADDRESS = 0x000C0000
APP_LENGTH = 0x00180000

# CAPL: FileLen = 0x00180000
FILE_LEN = APP_LENGTH

# 写序列号，先按 CAPL 默认 16 字节 0x00
SERIAL_DATA = bytes([0x00] * 16)


# =========================
# DLL KeyGen
# =========================
class KeyGen:
    def __init__(self):
        self.dll = ctypes.WinDLL(DLL_PATH)

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        kernel32.GetProcAddress.restype = ctypes.c_void_p

        addr = kernel32.GetProcAddress(self.dll._handle, DLL_FUNC_NAME)
        if not addr:
            raise RuntimeError(f"GetProcAddress failed for {DLL_FUNC_NAME!r}")

        print(f"[DLL] loaded: {DLL_PATH}")
        print(f"[DLL] export: {DLL_FUNC_NAME.decode()} @ 0x{addr:08X}")

        self._proto = ctypes.CFUNCTYPE(
            None,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.POINTER(ctypes.c_ubyte),
        )
        self.func = self._proto(addr)

    def gen_key(self, seed: bytes, level=0x11) -> bytes:
        if len(seed) != 4:
            raise RuntimeError(f"seed length must be 4, got {len(seed)} : {seed.hex()}")

        seed_arr = (ctypes.c_ubyte * 4)(*seed)
        key_arr = (ctypes.c_ubyte * 4)(0, 0, 0, 0)

        print(f"[DLL] CALkey input : level=0x{level:02X}, seed={bytes(seed_arr).hex()}")

        self.func(
            ctypes.c_uint32(level),
            seed_arr,
            key_arr
        )

        key = bytes(key_arr)
        print(f"[DLL] CALkey output: key={key.hex()}")
        return key


# =========================
# ISO-TP client
# =========================
def create_client(bus):
    addr = isotp.Address(
        isotp.AddressingMode.Normal_11bits,
        txid=TX_PHYS,
        rxid=RX_PHYS
    )

    params = {
        "stmin": 0,
        "blocksize": 0,
        "wftmax": 0,
        "tx_data_length": 8,
        "tx_data_min_length": 8,
        "tx_padding": PADDING,
        "rx_flowcontrol_timeout": 1000,
        "rx_consecutive_frame_timeout": 1000,
    }

    stack = isotp.CanStack(bus=bus, address=addr, params=params)
    conn = PythonIsoTpConnection(stack)

    config = udsoncan.configs.default_client_config.copy()
    config["use_server_timing"] = False
    config["data_identifiers"] = {}
    config["exception_on_negative_response"] = True
    config["exception_on_invalid_response"] = True
    config["exception_on_unexpected_response"] = True

    return Client(conn, config=config)


# =========================
# 通用收发
# =========================
def send_10_01(bus):
    msg = can.Message(
        arbitration_id=TX_FUNC,
        data=[0x02, 0x10, 0x01, 0x55, 0x55, 0x55, 0x55, 0x55],
        is_extended_id=False
    )
    bus.send(msg)
    print("[RAW] 7DF 10 01 sent")


def flush_bus_queue(bus, duration=0.30):
    print(f"[BUS] flushing rx queue for {duration:.2f}s ...")
    end_t = time.time() + duration
    count = 0

    while time.time() < end_t:
        msg = bus.recv(timeout=0.01)
        if msg is None:
            continue
        count += 1
        print(f"[BUS] flushed id=0x{msg.arbitration_id:X} data={bytes(msg.data).hex()}")

    print(f"[BUS] flush done, removed {count} frame(s)")


def uds_request_raw(client, payload: bytes, timeout: float = 2.0) -> bytes:
    print(f"[UDS RAW TX] {payload.hex()}")

    client.conn.send(payload)
    resp = client.conn.wait_frame(timeout=timeout)

    if resp is None:
        raise RuntimeError(f"No response for payload: {payload.hex()}")

    resp = bytes(resp)
    print(f"[UDS RAW RX] {resp.hex()}")
    return resp


def read_did_raw(client, did: int) -> bytes:
    req = bytes([0x22, (did >> 8) & 0xFF, did & 0xFF])
    resp = uds_request_raw(client, req, timeout=2.0)

    if len(resp) < 3:
        raise RuntimeError(f"22 {did:04X} invalid response: {resp.hex()}")

    if resp[0] == 0x7F:
        raise RuntimeError(f"22 {did:04X} NRC=0x{resp[2]:02X}, raw={resp.hex()}")

    if resp[0] != 0x62:
        raise RuntimeError(f"22 {did:04X} unexpected SID: raw={resp.hex()}")

    resp_did = (resp[1] << 8) | resp[2]
    if resp_did != did:
        raise RuntimeError(f"22 {did:04X} DID mismatch: got 0x{resp_did:04X}, raw={resp.hex()}")

    return resp[3:]


# =========================
# 27
# =========================
def do_27(client):
    kg = KeyGen()

    print("==== 27 11 ====")
    seed_resp = client.request_seed(level=0x11)

    if seed_resp is None:
        raise RuntimeError("27 11 no response")

    raw = bytes(seed_resp.data or b"")
    print(f"[27 11] raw payload: {raw.hex()}")

    if len(raw) < 1:
        raise RuntimeError(f"27 11 invalid payload: {raw.hex()}")

    subfunc = raw[0]
    if subfunc != 0x11:
        raise RuntimeError(f"27 11 unexpected subfunction in payload: 0x{subfunc:02X}, raw={raw.hex()}")

    seed = raw[1:]
    print("SEED:", seed.hex())

    key = kg.gen_key(seed, level=0x11)
    print("KEY :", key.hex())

    print("==== 27 12 ====")
    key_resp = client.send_key(level=0x12, key=key)
    print("[27 12] response:", key_resp)

    print("✅ 27 解锁成功")


# =========================
# CAPL 对齐：文件解析
# =========================
def file_checksum_capl(data: bytes) -> int:
    return (0xFF - (sum(data) & 0xFF)) & 0xFF


def parse_srec_record(line: str):
    line = line.strip()
    if not line or not line.startswith("S"):
        return None
    if line[1] not in ("1", "2", "3"):
        return None

    rectype = line[1]
    payload = bytes.fromhex(line[2:])

    if len(payload) < 2:
        return None

    count = payload[0]
    total_len = count + 1  # CAPL: TempLen = count + 1
    if len(payload) < total_len:
        raise RuntimeError(f"SREC length mismatch: {line}")

    data = payload[:total_len]

    addr_len = int(rectype) + 1  # S1->2, S2->3, S3->4
    checksum = data[total_len - 1]
    calc = file_checksum_capl(data[:total_len - 1])
    if checksum != calc:
        raise RuntimeError(f"SREC checksum error: {line}")

    addr = 0
    for i in range(addr_len):
        addr += data[i + 1] << (8 * (addr_len - i - 1))

    data_len = total_len - (addr_len + 2)
    rec_data = bytes(data[addr_len + 1: addr_len + 1 + data_len])
    return rectype, addr, rec_data


def load_srec_to_buffer(file_path: str, start_addr: int, total_len: int) -> bytes:
    buf = bytearray([0xFF] * total_len)

    with open(file_path, "r", encoding="ascii", errors="ignore") as f:
        for line in f:
            parsed = parse_srec_record(line)
            if parsed is None:
                continue

            _, addr, rec_data = parsed

            if addr < start_addr:
                continue

            off = addr - start_addr
            if off >= total_len:
                continue

            copy_len = min(len(rec_data), total_len - off)
            buf[off:off + copy_len] = rec_data[:copy_len]

    return bytes(buf)


def read_vercheck_asc(file_path: str) -> bytes:
    out = bytearray()
    with open(file_path, "r", encoding="ascii", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\r\n")
            groups = (len(line) + 1) // 3
            for i in range(groups):
                p0 = i * 3
                if p0 + 1 >= len(line):
                    continue
                token = line[p0:p0 + 2]
                try:
                    out.append(int(token, 16))
                except ValueError:
                    pass
    return bytes(out)


def calc_fingerdata_4bytes() -> bytes:
    t = time.localtime()
    year = t.tm_year
    month = t.tm_mon
    day = t.tm_mday

    b0 = ((year // 1000) * 16) + ((year // 100) % 10)
    b1 = (((year % 100) // 10) * 16) + (year % 10)
    b2 = ((month // 10) * 16) + (month % 10)
    b3 = ((day // 10) * 16) + (day % 10)
    return bytes([b0 & 0xFF, b1 & 0xFF, b2 & 0xFF, b3 & 0xFF])


def crc32_capl_style(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF

def routine_control_raw(client, subfunction: int, rid: int, extra: bytes = b"", timeout: float = 5.0) -> bytes:
    req = bytes([
        0x31,
        subfunction & 0xFF,
        (rid >> 8) & 0xFF,
        rid & 0xFF
    ]) + extra

    resp = uds_request_raw(client, req, timeout=timeout)

    # CAPL 对齐：遇到 NRC 0x78 继续等正式响应
    if len(resp) >= 3 and resp[0] == 0x7F and resp[1] == 0x31 and resp[2] == 0x78:
        print("[31] NRC 0x78 pending, wait for final response...")
        while True:
            resp = client.conn.wait_frame(timeout=2.0)   # 对齐 CAPL _P2Server=2000ms
            if resp is None:
                raise RuntimeError("31 pending timeout after NRC 0x78")

            resp = bytes(resp)
            print(f"[UDS RAW RX] {resp.hex()}")

            if len(resp) >= 3 and resp[0] == 0x7F and resp[1] == 0x31 and resp[2] == 0x78:
                print("[31] still pending...")
                continue
            break

    if resp[0] == 0x7F:
        raise RuntimeError(f"31 NRC=0x{resp[2]:02X}, raw={resp.hex()}")
    if resp[0] != 0x71:
        raise RuntimeError(f"31 unexpected SID: {resp.hex()}")

    return resp

def write_did_raw(client, did: int, data: bytes, timeout: float = 3.0) -> bytes:
    req = bytes([
        0x2E,
        (did >> 8) & 0xFF,
        did & 0xFF
    ]) + data

    resp = uds_request_raw(client, req, timeout=timeout)

    # CAPL 对齐：遇到 NRC 0x78 继续等正式响应
    if len(resp) >= 3 and resp[0] == 0x7F and resp[1] == 0x2E and resp[2] == 0x78:
        print("[2E] NRC 0x78 pending, wait for final response...")
        while True:
            resp = client.conn.wait_frame(timeout=2.0)
            if resp is None:
                raise RuntimeError("2E pending timeout after NRC 0x78")

            resp = bytes(resp)
            print(f"[UDS RAW RX] {resp.hex()}")

            if len(resp) >= 3 and resp[0] == 0x7F and resp[1] == 0x2E and resp[2] == 0x78:
                print("[2E] still pending...")
                continue
            break

    if resp[0] == 0x7F:
        raise RuntimeError(f"2E NRC=0x{resp[2]:02X}, raw={resp.hex()}")
    if resp[0] != 0x6E:
        raise RuntimeError(f"2E unexpected SID: {resp.hex()}")

    return resp

def request_download_raw(client, address: int, size: int) -> int:
    req = bytes([
        0x34,
        0x00,
        0x44,
        (address >> 24) & 0xFF,
        (address >> 16) & 0xFF,
        (address >> 8) & 0xFF,
        address & 0xFF,
        (size >> 24) & 0xFF,
        (size >> 16) & 0xFF,
        (size >> 8) & 0xFF,
        size & 0xFF,
    ])

    resp = uds_request_raw(client, req, timeout=5.0)

    if resp[0] == 0x7F:
        raise RuntimeError(f"34 NRC=0x{resp[2]:02X}, raw={resp.hex()}")
    if resp[0] != 0x74:
        raise RuntimeError(f"34 failed: {resp.hex()}")

    # ECU返回: 74 20 08 02
    # 0x20 = lengthFormatIdentifier
    # 0x08 0x02 = maxNumberOfBlockLength
    if len(resp) < 4:
        raise RuntimeError(f"34 response too short: {resp.hex()}")

    lfid = resp[1]
    block_len_bytes = lfid >> 4
    if block_len_bytes <= 0:
        raise RuntimeError(f"34 invalid lengthFormatIdentifier: {resp.hex()}")

    if len(resp) < 2 + block_len_bytes:
        raise RuntimeError(f"34 response length mismatch: {resp.hex()}")

    block_len = int.from_bytes(resp[2:2 + block_len_bytes], "big")
    print(f"[34] block_len={block_len}")
    return block_len

def transfer_data_raw(client, data: bytes, block_len: int, app_mode: bool):
    payload_size = block_len - 2
    total = len(data)

    if payload_size <= 0:
        raise RuntimeError(f"invalid block_len={block_len}")

    offset = 0
    seq = 1

    while offset < total:
        chunk = data[offset:offset + payload_size]
        offset += len(chunk)

        req = bytes([0x36, seq & 0xFF]) + chunk

        if app_mode and seq == 1:
            time.sleep(0.002)  # CAPL: 第一个 APP block 前等待 2ms

        resp = uds_request_raw(client, req, timeout=5.0)

        # CAPL 对齐：36 遇到 NRC 0x78 继续等最终响应
        if len(resp) >= 3 and resp[0] == 0x7F and resp[1] == 0x36 and resp[2] == 0x78:
            print(f"[36] seq={seq} NRC 0x78 pending, wait for final response...")
            while True:
                resp = client.conn.wait_frame(timeout=2.0)   # 先沿用你31/2E的2秒
                if resp is None:
                    raise RuntimeError(f"36 seq={seq} pending timeout after NRC 0x78")

                resp = bytes(resp)
                print(f"[UDS RAW RX] {resp.hex()}")

                if len(resp) >= 3 and resp[0] == 0x7F and resp[1] == 0x36 and resp[2] == 0x78:
                    print(f"[36] seq={seq} still pending...")
                    continue
                break

        if resp[0] == 0x7F:
            raise RuntimeError(f"36 seq={seq} NRC=0x{resp[2]:02X}, raw={resp.hex()}")

        if resp[0] != 0x76:
            raise RuntimeError(f"36 seq={seq} unexpected SID: {resp.hex()}")

        if len(resp) < 2 or resp[1] != (seq & 0xFF):
            raise RuntimeError(f"36 seq mismatch: expect=0x{seq:02X}, raw={resp.hex()}")

        print(f"[36] seq={seq} OK len={len(chunk)} offset={offset}/{total}")

        seq += 1
        if seq > 0xFF:
            seq = 1

def request_transfer_exit_raw(client, extra_data: bytes = b""):
    req = bytes([0x37]) + extra_data
    resp = uds_request_raw(client, req, timeout=5.0)

    if resp[0] == 0x7F:
        raise RuntimeError(f"37 NRC=0x{resp[2]:02X}, raw={resp.hex()}")
    if resp[0] != 0x77:
        raise RuntimeError(f"37 failed: {resp.hex()}")

    print("[37] OK")
    return resp


def ecu_reset_raw(client, subfunction: int = 0x01, timeout: float = 8.0):
    req = bytes([0x11, subfunction & 0xFF])
    resp = uds_request_raw(client, req, timeout=timeout)

    if resp[0] == 0x7F:
        raise RuntimeError(f"11 NRC=0x{resp[2]:02X}, raw={resp.hex()}")
    if resp[0] != 0x51:
        raise RuntimeError(f"11 failed: {resp.hex()}")

    print("[11] reset OK")
    return resp


def clear_dtc_raw(client, group: int = 0xFFFFFF, timeout: float = 3.0):
    req = bytes([
        0x14,
        (group >> 16) & 0xFF,
        (group >> 8) & 0xFF,
        group & 0xFF,
    ])
    resp = uds_request_raw(client, req, timeout=timeout)

    if resp[0] == 0x7F:
        raise RuntimeError(f"14 NRC=0x{resp[2]:02X}, raw={resp.hex()}")
    if resp[0] != 0x54:
        raise RuntimeError(f"14 failed: {resp.hex()}")

    print("[14] clear DTC OK")
    return resp


# =========================
# Download 主流程
# =========================
def run_download_flow(client, bus):
    print("==== FILE INIT ====")
    driver_data = load_srec_to_buffer(DRIVER_SREC_PATH, DRIVER_START_ADDRESS, DRIVER_LENGTH)
    app_data = load_srec_to_buffer(APP_S19_PATH, APP_START_ADDRESS, APP_LENGTH)
    vercheck = read_vercheck_asc(VERCHECK_ASC_PATH)

    print(f"[FILE] driver size={len(driver_data)}")
    print(f"[FILE] app    size={len(app_data)}")
    print(f"[FILE] verchk size={len(vercheck)}")

    if len(vercheck) < 1322:
        raise RuntimeError(f"VerCheck length too short: {len(vercheck)}")
    vercheck = vercheck[:1322]

    print("==== 31 6000 ====")
    routine_control_raw(client, 0x01, 0x6000, vercheck, timeout=10.0)

    print("==== 31 6001 ====")
    routine_control_raw(client, 0x01, 0x6001, b"", timeout=8.0)

    print("==== 2E F198 ====")
    write_did_raw(client, 0xF198, SERIAL_DATA, timeout=5.0)

    print("==== 2E F199 ====")
    write_did_raw(client, 0xF199, calc_fingerdata_4bytes(), timeout=5.0)

    print("==== DRIVER 34 ====")
    block_len = request_download_raw(client, DRIVER_START_ADDRESS, DRIVER_LENGTH)

    print("==== DRIVER 36 ====")
    transfer_data_raw(client, driver_data, block_len, app_mode=False)

    print("==== DRIVER 37 ====")
    request_transfer_exit_raw(client)

    print("==== 31 0202 ====")
    driver_crc = crc32_capl_style(driver_data)
    routine_control_raw(
        client,
        0x01,
        0x0202,
        bytes([
            (driver_crc >> 24) & 0xFF,
            (driver_crc >> 16) & 0xFF,
            (driver_crc >> 8) & 0xFF,
            driver_crc & 0xFF
        ]),
        timeout=10.0
    )

    print("==== 31 FF00 ====")
    erase_extra = bytes([
        0x44,
        (APP_START_ADDRESS >> 24) & 0xFF,
        (APP_START_ADDRESS >> 16) & 0xFF,
        (APP_START_ADDRESS >> 8) & 0xFF,
        APP_START_ADDRESS & 0xFF,
        (APP_LENGTH >> 24) & 0xFF,
        (APP_LENGTH >> 16) & 0xFF,
        (APP_LENGTH >> 8) & 0xFF,
        APP_LENGTH & 0xFF,
    ])
    routine_control_raw(client, 0x01, 0xFF00, erase_extra, timeout=20.0)

    print("==== APP 34 ====")
    block_len = request_download_raw(client, APP_START_ADDRESS, FILE_LEN)

    print("==== APP 36 ====")
    transfer_data_raw(client, app_data, block_len, app_mode=True)

    print("==== APP 37 ====")
    request_transfer_exit_raw(client)

    print("==== 31 0203 ====")
    routine_control_raw(client, 0x01, 0x0203, b"", timeout=10.0)

    print("==== 31 FF01 ====")
    routine_control_raw(client, 0x01, 0xFF01, b"", timeout=10.0)

    print("==== 11 01 ====")
    ecu_reset_raw(client, 0x01, timeout=8.0)

    time.sleep(2.0)

    print("==== POST 10 03 ====")
    r = client.change_session(3)
    print("10 03 OK:", r)

    print("==== POST 28 00 01 ====")
    r = client.communication_control(0, 1)
    print("28 OK:", r)

    print("==== POST 85 01 ====")
    r = client.control_dtc_setting(1)
    print("85 OK:", r)

    print("==== POST 14 FFFFFF ====")
    clear_dtc_raw(client, 0xFFFFFF)

    print("==== POST 10 01 ====")
    send_10_01(bus)
    time.sleep(0.2)
    flush_bus_queue(bus, duration=0.30)


# =========================
# main
# =========================
def main():
    print("[BUS] opening...")

    bus = can.Bus(
        interface="vector",
        channel=CHANNEL,
        bitrate=BITRATE,
        data_bitrate=DATA_BITRATE,
        fd=True,
        app_name="CANoe",
    )

    client = create_client(bus)

    try:
        print("==== 10 01 ====")
        send_10_01(bus)
        time.sleep(0.20)
        flush_bus_queue(bus, duration=0.30)

        with client as c:
            print("==== 22 F197 ====")
            f197 = read_did_raw(c, 0xF197)
            print("F197:", f197.hex())

            print("==== 22 F150 ====")
            f150 = read_did_raw(c, 0xF150)
            print("F150:", f150.hex())

            print("==== 22 F189 ====")
            f189 = read_did_raw(c, 0xF189)
            print("F189:", f189.hex())

            print("==== 10 03 ====")
            r = c.change_session(3)
            print("10 03 OK:", r)

            print("==== 85 ====")
            r = c.control_dtc_setting(2)
            print("85 OK:", r)

            print("==== 28 ====")
            r = c.communication_control(3, 1)
            print("28 OK:", r)

            print("==== 10 02 ====")
            r = c.change_session(2)
            print("10 02 OK:", r)

            print("==== 27 ====")
            do_27(c)

            print("==== DOWNLOAD FLOW ====")
            run_download_flow(c, bus)

    finally:
        bus.shutdown()
        print("[BUS] closed")


if __name__ == "__main__":
    main()