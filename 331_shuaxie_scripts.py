# -*- coding: utf-8 -*-
from __future__ import annotations

import can
import ctypes
import math
import platform
import struct
import sys
import threading
import time
import zlib


# ============================================================
# 配置
# ============================================================
CHANNEL = 0
BITRATE = 500000
DATA_BITRATE = 2000000

TX_PHYS = 0x772
TX_FUNC = 0x7DF
RX_ID = 0x77A
TX_771_SPECIAL = 0x771

PADDING = 0x55

# 对齐 CAPL:
# - 首响应等待 50ms
# - 连续帧等待 128ms
# - pending 等待 _P2Server = 2000ms
P2_SERVER_S = 2.0
RX_FIRST_TIMEOUT_S = 0.05
RX_CF_TIMEOUT_S = 0.128
WAIT_FC_TIMEOUT_S = 0.05

# CAPL: S3server = 4000, timer 周期 = S3server/2 = 2000ms
ENABLE_3E80 = True
KEEPALIVE_PERIOD_S = 2.0

# 你给的 CAPL 报告里，10 02 正响应后不久就出现了 3E80；
# CAPL 原工程里 timer 的启动点不在 Download() 片段内，所以这里保留一个首发延迟参数。
# 如后续你补充了 setTimer(test_online, ...) 的实际启动位置，再微调这个值。
KEEPALIVE_INITIAL_DELAY_S = 0.40
DLL_FUNC_NAME = b"?CALkey@@YAXKQAE0@Z"
DLL_PATH = r"E:\lingpao\Flash2944_LP_ARC_V1.2_0x3E00_CAN_release\CAPL\EXECL\capldll_lingpao.dll"
APP_S19_PATH = r"E:\lingpao\Flash2944_LP_ARC_V1.2_0x3E00_CAN_release\1\ARC3.31BC3_LPB10A_B1.00.01_APP_V2.00.00_CHF0369N_without_boot.s19"
DRIVER_SREC_PATH = r"E:\lingpao\Flash2944_LP_ARC_V1.2_0x3E00_CAN_release\1.00.00\FlashDriver.srec"
VERCHECK_ASC_PATH = r"E:\lingpao\Flash2944_LP_ARC_V1.2_0x3E00_CAN_release\1.00.00\LP-BSD080-BA_V9.99.99_R_RL_20250506.asc"

DRIVER_START_ADDRESS = 0x00000000
DRIVER_LENGTH = 0x00004000

APP_START_ADDRESS = 0x000C0000
APP_LENGTH = 0x00180000
FILE_LEN = APP_LENGTH

SERIAL_DATA = bytes([0x00] * 16)

# ============================================================
# 日志开关
# ============================================================
LOG_VERBOSE_CAN = False
LOG_VERBOSE_ISOTP = False
LOG_PROGRESS_ONLY_FOR_36 = True
LOG_FLUSH_DETAILS = False

COMPARE_MODE = True
COMPARE_LOG_1002 = True
COMPARE_LOG_2712 = True
COMPARE_LOG_31FF00 = True
COMPARE_LOG_36_APP_HEAD = True
COMPARE_LOG_KEEPALIVE = True

BUS_KWARGS = dict(
    interface="vector",
    channel=CHANNEL,
    bitrate=BITRATE,
    data_bitrate=DATA_BITRATE,
    fd=True,
    app_name="CANoe",
)

# ============================================================
# 日志
# ============================================================
def step(title: str):
    print("\n" + "=" * 70)
    print(f"[STEP] {title}")
    print("=" * 70)


def info(msg: str):
    print(f"[INFO] {msg}")


def ok(msg: str):
    print(f"[OK] {msg}")


def warn(msg: str):
    print(f"[WARN] {msg}")


def compare_info(msg: str):
    if COMPARE_MODE:
        print(f"[COMPARE] {msg}")


# ============================================================
# KeyGen
# ============================================================
class KeyGen:
    def __init__(self, dll_path: str, export_name: bytes):
        self.dll = ctypes.WinDLL(dll_path)

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        kernel32.GetProcAddress.restype = ctypes.c_void_p

        addr = kernel32.GetProcAddress(self.dll._handle, export_name)
        if not addr:
            raise RuntimeError(f"GetProcAddress failed for {export_name!r}")

        self._proto = ctypes.CFUNCTYPE(
            None,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.POINTER(ctypes.c_ubyte),
        )
        self.func = self._proto(addr)

        info(f"DLL loaded: {dll_path}")
        # info(f"DLL export: {exp ort_name.decode(errors='ignore')} @ 0x{addr:08X}")



    def calc_key_27_12(self, seed4: bytes) -> bytes:
        if len(seed4) != 4:
            raise RuntimeError(f"27 seed length must be 4, got {len(seed4)}")

        seed_arr = (ctypes.c_ubyte * 4)(*seed4)
        key_arr = (ctypes.c_ubyte * 4)(0, 0, 0, 0)

        # 对齐 CAPL: Cal_key(0x11, seed_L2, Seed_Key)
        self.func(ctypes.c_uint32(0x11), seed_arr, key_arr)

        key = bytes(key_arr)
        if LOG_VERBOSE_ISOTP:
            info(f"27 seed={seed4.hex()} -> key={key.hex()}")
        return key


# ============================================================
# 文件解析
# ============================================================
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
    total_len = count + 1
    if len(payload) < total_len:
        raise RuntimeError(f"SREC length mismatch: {line}")

    data = payload[:total_len]

    addr_len = int(rectype) + 1
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


# ============================================================
# KeepAlive (对齐 CAPL 的 timer test_online)
# ============================================================
class KeepAliveWorker:
    def __init__(self, send_func, period_s: float = 2.0, initial_delay_s: float = 0.0):
        self._send_func = send_func
        self._period_s = period_s
        self._initial_delay_s = initial_delay_s
        self._stop_evt = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._enabled = False

    def start(self):
        with self._lock:
            if self._enabled:
                return
            self._enabled = True
            self._stop_evt.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="keepalive_3e80",
                daemon=True,
            )
            self._thread.start()
            info(
                f"KeepAliveWorker started: period={self._period_s:.3f}s "
                f"initial_delay={self._initial_delay_s:.3f}s"
            )

    def stop(self):
        with self._lock:
            if not self._enabled:
                return
            self._enabled = False
            self._stop_evt.set()
            t = self._thread

        if t is not None:
            t.join(timeout=1.0)
        info("KeepAliveWorker stopped")

    def _run(self):
        next_ts = time.time() + self._initial_delay_s
        while not self._stop_evt.is_set():
            now = time.time()
            wait_s = next_ts - now
            if wait_s > 0:
                self._stop_evt.wait(wait_s)
                continue

            try:
                self._send_func()
            except Exception as e:
                warn(f"keepalive send failed: {e}")

            next_ts += self._period_s


# ============================================================
# UDS / ISO-TP
# ============================================================
class UDS:
    def __init__(self, bus: can.BusABC, tx_id: int, rx_id: int = RX_ID, tx_lock: threading.RLock | None = None):
        self.bus = bus
        self.tx_id = tx_id
        self.rx_id = rx_id
        self.tx_lock = tx_lock if tx_lock is not None else threading.RLock()

    def explain_nrc(self, sid: int, nrc: int) -> str:
        if sid == 0x27:
            if nrc == 0x35:
                return "InvalidKey（Key错误 → DLL/算法问题）"
            elif nrc == 0x36:
                return "ExceededAttempts（错误次数过多 → ECU已锁）"
            elif nrc == 0x37:
                return "RequiredTimeDelayNotExpired（ECU锁定中 → 冷却时间未到）"

        if nrc == 0x78:
            return "ResponsePending（ECU处理中）"

        return "Unknown NRC"  # ← 必须加
    def flush_rx(self, duration_s: float = 0.2):
        info(f"flushing rx queue for {duration_s:.2f}s ...")
        end = time.time() + duration_s
        n = 0
        hit_rx = 0

        while time.time() < end:
            msg = self.bus.recv(0.01)
            if msg is None:
                continue
            n += 1

            if msg.arbitration_id == self.rx_id:
                hit_rx += 1

            if LOG_FLUSH_DETAILS:
                info(f"flushed id=0x{msg.arbitration_id:X} data={bytes(msg.data).hex()}")

        info(f"flush done, removed {n} frame(s), rx_0x{self.rx_id:X}={hit_rx}")

    def _send_can(self, arbitration_id: int, data8: bytes, log: bool = True):
        if len(data8) != 8:
            raise RuntimeError(f"CAN frame len must be 8, got {len(data8)}")

        msg = can.Message(
            arbitration_id=arbitration_id,
            data=data8,
            is_extended_id=False,
            is_fd=False,
            bitrate_switch=False,
        )
        self.bus.send(msg)

        if log and LOG_VERBOSE_CAN:
            info(f"TX {hex(arbitration_id)} {data8.hex()}")

    def _recv_can(self, timeout_s: float) -> can.Message:
        end = time.time() + timeout_s
        while time.time() < end:
            msg = self.bus.recv(0.01)
            if msg is None:
                continue
            if msg.arbitration_id != self.rx_id:
                continue

            if LOG_VERBOSE_CAN:
                info(f"RX {hex(msg.arbitration_id)} {bytes(msg.data).hex()}")
            return msg

        raise RuntimeError("timeout")

    def send_sf(self, uds_payload: bytes, padding_byte: int | None = None):
        if len(uds_payload) > 7:
            raise RuntimeError(f"SF payload too long: {len(uds_payload)}")

        pad = PADDING if padding_byte is None else (padding_byte & 0xFF)

        frame = bytes([len(uds_payload)]) + uds_payload
        frame += bytes([pad] * (8 - len(frame)))

        with self.tx_lock:
            self._send_can(self.tx_id, frame)

        if LOG_VERBOSE_CAN:
            info(
                f"SF tx_id=0x{self.tx_id:X} "
                f"payload={uds_payload.hex()} frame={frame.hex()} pad=0x{pad:02X}"
            )

    def send_mf(
        self,
        uds_payload: bytes,
        suppress_cf_log: bool = False,
        trace_name: str | None = None,
    ):
        total_len = len(uds_payload)
        if total_len <= 7:
            self.send_sf(uds_payload)
            return

        with self.tx_lock:
            ff = bytearray(8)
            ff[0] = 0x10 | ((total_len >> 8) & 0x0F)
            ff[1] = total_len & 0xFF
            ff[2:8] = uds_payload[:6]
            self._send_can(self.tx_id, bytes(ff))

            if trace_name:
                info(f"[ISOTP][{trace_name}] FF={bytes(ff).hex()} total_len={total_len}")

            if LOG_VERBOSE_ISOTP:
                info(f"FF total_len={total_len}")

            fc_msg = self._recv_can(WAIT_FC_TIMEOUT_S)
            fc = bytes(fc_msg.data)

            if trace_name:
                info(f"[ISOTP][{trace_name}] FC={fc.hex()} block_size={fc[1]} stmin=0x{fc[2]:02X}")

            if (fc[0] >> 4) != 0x3:
                raise RuntimeError(f"expect FC after FF, got {fc.hex()}")

            if (fc[0] & 0x0F) != 0x0:
                raise RuntimeError(f"FC not CTS: {fc.hex()}")

            block_size = fc[1]
            stmin = fc[2]

            if LOG_VERBOSE_ISOTP:
                info(f"FC block_size={block_size} stmin=0x{stmin:02X}")

            remain = uds_payload[6:]
            sn = 1
            sent_in_block = 0
            cf_count = 0

            while remain:
                cf = bytearray(8)
                cf[0] = 0x20 | (sn & 0x0F)

                take = min(7, len(remain))
                cf[1:1 + take] = remain[:take]

                if take < 7:
                    for i in range(1 + take, 8):
                        cf[i] = PADDING

                self._send_can(self.tx_id, bytes(cf), log=(LOG_VERBOSE_CAN and not suppress_cf_log))

                if trace_name and cf_count < 5:
                    info(f"[ISOTP][{trace_name}] CF#{cf_count + 1}={bytes(cf).hex()}")

                remain = remain[take:]
                cf_count += 1
                sent_in_block += 1
                sn = (sn + 1) & 0x0F

                if stmin > 0 and stmin <= 0x7F:
                    time.sleep(stmin / 1000.0)

                if block_size != 0 and sent_in_block >= block_size and remain:
                    fc_msg = self._recv_can(1.0)
                    fc = bytes(fc_msg.data)

                    if (fc[0] >> 4) != 0x3:
                        raise RuntimeError(f"expect next FC, got {fc.hex()}")

                    if (fc[0] & 0x0F) != 0x0:
                        raise RuntimeError(f"next FC not CTS: {fc.hex()}")

                    block_size = fc[1]
                    stmin = fc[2]
                    sent_in_block = 0

                    if LOG_VERBOSE_ISOTP:
                        info(f"NEXT FC block_size={block_size} stmin=0x{stmin:02X}")

        # 保持你原来的轻微节拍，不做大改
        if total_len >= 2050:
            time.sleep(0.08)
        elif cf_count > 0:
            time.sleep(min(0.05, 0.0003 * cf_count + 0.01))

    def output_fc(self):
        fc = bytes([0x30, 0x00, 0x00, 0x55, 0x55, 0x55, 0x55, 0x55])
        with self.tx_lock:
            self._send_can(self.tx_id, fc)

    def recv_uds(self, timeout_s: float = RX_FIRST_TIMEOUT_S) -> bytes:
        msg = self._recv_can(timeout_s)
        data = bytes(msg.data)

        pci_type = (data[0] >> 4) & 0x0F

        if pci_type == 0x0:
            sf_len = data[0] & 0x0F
            return data[1:1 + sf_len]

        if pci_type == 0x1:
            total_len = ((data[0] & 0x0F) << 8) | data[1]
            payload = bytearray(data[2:8])

            if LOG_VERBOSE_ISOTP:
                info(f"RX FF total_len={total_len}")

            self.output_fc()

            expect_sn = 1

            while len(payload) < total_len:
                cf_msg = self._recv_can(RX_CF_TIMEOUT_S)
                cf_data = bytes(cf_msg.data)

                cf_type = (cf_data[0] >> 4) & 0x0F
                sn = cf_data[0] & 0x0F

                if cf_type != 0x2:
                    raise RuntimeError(f"not CF: {cf_data.hex()}")

                if sn != (expect_sn & 0x0F):
                    raise RuntimeError(f"CF SN mismatch: expect={expect_sn & 0x0F}, got={sn}")

                payload.extend(cf_data[1:8])
                expect_sn = (expect_sn + 1) & 0x0F

            return bytes(payload[:total_len])

        raise RuntimeError(f"unsupported PCI: {data.hex()}")

    def judge(self, service_id: int, expect_sid: int, trace: bool = True) -> bytes:
        start_ts = time.time()
        pending_count = 0

        resp = self.recv_uds()

        if resp[0] == expect_sid:
            if COMPARE_MODE and trace:
                compare_info(
                    f"judge sid=0x{service_id:02X} direct positive "
                    f"resp={resp.hex()} elapsed_ms={(time.time() - start_ts) * 1000:.1f}"
                )
            return resp

        if len(resp) >= 3 and resp[0] == 0x7F and resp[1] == service_id and resp[2] == 0x78:
            pending_count += 1
            if COMPARE_MODE and trace:
                compare_info(
                    f"judge sid=0x{service_id:02X} pending#{pending_count} "
                    f"resp={resp.hex()} elapsed_ms={(time.time() - start_ts) * 1000:.1f}"
                )

            while True:
                resp = self.recv_uds(P2_SERVER_S)

                if resp[0] == expect_sid:
                    if COMPARE_MODE and trace:
                        compare_info(
                            f"judge sid=0x{service_id:02X} final positive after pending "
                            f"pending_count={pending_count} resp={resp.hex()} "
                            f"elapsed_ms={(time.time() - start_ts) * 1000:.1f}"
                        )
                    return resp

                if len(resp) >= 3 and resp[0] == 0x7F and resp[1] == service_id and resp[2] == 0x78:
                    pending_count += 1
                    if COMPARE_MODE and trace:
                        compare_info(
                            f"judge sid=0x{service_id:02X} pending#{pending_count} "
                            f"resp={resp.hex()} elapsed_ms={(time.time() - start_ts) * 1000:.1f}"
                        )
                    continue

                if len(resp) >= 3 and resp[0] == 0x7F:
                    raise RuntimeError(
                        f"NRC after pending sid=0x{service_id:02X} "
                        f"nrc=0x{resp[2]:02X} resp={resp.hex()}"
                    )

                raise RuntimeError(f"unexpected after pending: {resp.hex()}")

        if len(resp) >= 3 and resp[0] == 0x7F:
            nrc = resp[2]
            reason = self.explain_nrc(service_id, nrc)

            raise RuntimeError(
                f"NRC sid=0x{service_id:02X} "
                f"nrc=0x{nrc:02X} ({reason}) "
                f"resp={resp.hex()}"
            )

        raise RuntimeError(f"unexpected {resp.hex()}")


# ============================================================
# 服务层
# ============================================================
class Service:
    def __init__(self, bus: can.BusABC, phys: UDS, func: UDS, keygen: KeyGen, tx_lock: threading.RLock):
        self.bus = bus
        self.phys = phys
        self.func = func
        self.keygen = keygen
        self.tx_lock = tx_lock
        self.keepalive = KeepAliveWorker(
            self.send_3e80_once,
            period_s=KEEPALIVE_PERIOD_S,
            initial_delay_s=KEEPALIVE_INITIAL_DELAY_S,
        )

    def start_keepalive(self):
        if ENABLE_3E80:
            self.keepalive.start()

    def stop_keepalive(self):
        if ENABLE_3E80:
            self.keepalive.stop()

    def send_3e80_once(self):
        # 对齐 CAPL server_3e(): [02 3E 80 00 00 00 00 00]
        self.func.send_sf(bytes([0x3E, 0x80]), padding_byte=0x00)
        if COMPARE_MODE and COMPARE_LOG_KEEPALIVE:
            compare_info("3E80 timer tick | frame should be: 02 3E 80 00 00 00 00 00")

    def s10(self, mode: str, sub: int, reset_flag: bool = False):
        step(f"10 {sub:02X} {'functional' if mode == 'func' else 'physical'}")
        uds = bytes([0x10, sub])
        target = self.func if mode == "func" else self.phys

        if sub == 0x02 and COMPARE_LOG_1002:
            compare_info(
                f"10 02 start | mode={mode} tx_id=0x{target.tx_id:X} expect=50 02 | "
                f"CAPL关键点: 先7F 10 78, 再50 02"
            )

        target.send_sf(uds)
        resp = target.judge(0x10, 0x50)
        ok(resp.hex())

        if sub == 0x02 and COMPARE_LOG_1002:
            compare_info(f"10 02 final resp={resp.hex()}")

        # 对齐 CAPL server_10(): ResetFlag == 1 时等待 2000ms
        if reset_flag:
            time.sleep(2.0)

        return resp

    def s11(self, sub: int):
        step(f"11 {sub:02X}")
        self.phys.send_sf(bytes([0x11, sub]))
        resp = self.phys.judge(0x11, 0x51)
        ok(resp.hex())
        # 对齐 CAPL server_11(): testWaitForTimeout(6000)
        time.sleep(6.0)
        return resp

    def s22(self, did: int) -> bytes:
        step(f"22 {did:04X}")
        uds = bytes([0x22, (did >> 8) & 0xFF, did & 0xFF])
        self.phys.send_sf(uds)
        resp = self.phys.judge(0x22, 0x62)

        got_did = (resp[1] << 8) | resp[2]
        if got_did != did:
            raise RuntimeError(f"22 DID mismatch, expect={did:04X}, got={got_did:04X}")

        data = resp[3:]
        ok(data.hex())
        return data

    def s85(self, sub: int):
        step(f"85 {sub:02X}")
        self.func.send_sf(bytes([0x85, sub]))
        resp = self.func.judge(0x85, 0xC5)
        ok(resp.hex())
        return resp

    def s28(self, a: int, b: int):
        step(f"28 {a:02X} {b:02X}")
        self.func.send_sf(bytes([0x28, a, b]))
        resp = self.func.judge(0x28, 0x68)
        ok(resp.hex())
        return resp

    def s14(self, group: int = 0xFFFFFF):
        step(f"14 {group:06X} functional")
        uds = bytes([0x14, (group >> 16) & 0xFF, (group >> 8) & 0xFF, group & 0xFF])
        self.func.send_sf(uds)
        resp = self.func.judge(0x14, 0x54)
        ok(resp.hex())
        return resp

    # def s27_unlock(self):
    #     for attempt in range(2):  # 最多尝试2次（CAPL一般不会超过2）
    #         try:
    #             step("27 11")
    #             self.phys.send_sf(bytes([0x27, 0x11]))
    #             resp = self.phys.judge(0x27, 0x67)
    #
    #             seed = resp[2:6]
    #             ok(f"seed={seed.hex()}")
    #
    #             key = self.keygen.calc_key_27_12(seed)
    #
    #             step("27 12")
    #             self.phys.send_sf(bytes([0x27, 0x12]) + key)
    #             resp = self.phys.judge(0x27, 0x67)
    #
    #             ok(resp.hex())
    #             return resp
    #
    #         except RuntimeError as e:
    #             msg = str(e)
    #
    #             # 👇 关键判断
    #             if "0x36" in msg or "0x37" in msg:
    #                 warn("27 被锁，重新执行 10 02 解锁 ECU")
    #
    #                 # 🔥 重新进入编程会话（核心）
    #                 self.stop_keepalive()
    #                 self.s10("phys", 0x02, reset_flag=True)
    #                 self.start_keepalive()
    #                 self.send_771_fb_a5()
    #
    #                 continue  # 再来一次 27
    #
    #             raise
    def s27_unlock(self) -> bytes:
        """
        27 安全访问解锁逻辑（对齐你当前项目经验）:
        - 0x37: 等 10 秒后，重新 27 11 请求种子
        - 0x36: ECU 因错误次数过多锁定，重新执行 10 02 + 0x771 FB A5，再重新 27 11
        - 0x35: key 错，直接失败（说明 keygen 仍有问题）
        - 只有 67 12 才算真正解锁成功
        """
        max_attempts = 5

        for attempt in range(1, max_attempts + 1):
            try:
                # ============================================================
                # STEP 1: 请求种子 27 11
                # ============================================================
                step("27 11")
                self.phys.send_sf(bytes([0x27, 0x11]))
                resp = self.phys.judge(0x27, 0x67)

                if len(resp) < 6 or resp[1] != 0x11:
                    raise RuntimeError(f"27 11 invalid resp: {resp.hex()}")

                seed = resp[2:6]
                ok(f"seed={seed.hex()}")

                # ============================================================
                # STEP 2: 计算 key
                # CAPL 逻辑是: Cal_key(0x11, seed_L2, Seed_Key)
                # ============================================================
                key = self.keygen.calc_key_27_12(seed)

                if COMPARE_LOG_2712:
                    compare_info(f"27 11 seed={seed.hex()}")
                    compare_info(f"27 12 key={key.hex()}")
                    compare_info("27 12 CAPL关键点: 常见行为是先7F 27 78，再67 12")

                # 保险：全 0 key 直接判失败
                if key == b"\x00\x00\x00\x00":
                    raise RuntimeError("keygen returned all-zero key")

                # ============================================================
                # STEP 3: 发送 key 27 12
                # ============================================================
                step("27 12")
                self.phys.send_sf(bytes([0x27, 0x12]) + key)
                resp = self.phys.judge(0x27, 0x67)

                if len(resp) < 2 or resp[1] != 0x12:
                    raise RuntimeError(f"27 12 invalid resp: {resp.hex()}")

                ok(resp.hex())

                if COMPARE_LOG_2712:
                    compare_info(f"27 12 final resp={resp.hex()}")

                # 成功，直接返回
                return resp

            except RuntimeError as e:
                msg = str(e)
                warn(f"s27_unlock attempt={attempt}/{max_attempts} failed: {msg}")

                # ------------------------------------------------------------
                # 情况1: 0x37 RequiredTimeDelayNotExpired
                # 处理: 等 10 秒，再重新 27 11 请求种子
                # ------------------------------------------------------------
                if "nrc=0x37" in msg or "7f2737" in msg.lower():
                    warn("27 收到 0x37，等待 10 秒后重新请求种子")
                    time.sleep(10.0)
                    continue

                # ------------------------------------------------------------
                # 情况2: 0x36 ExceededAttempts
                # 处理: 重新 10 02 -> 771 FB A5 -> 再重新 27 11
                # ------------------------------------------------------------
                if "nrc=0x36" in msg or "7f2736" in msg.lower():
                    warn("27 收到 0x36，重新执行 10 02 + 0x771 FB A5 以恢复解锁状态")
                    self.stop_keepalive()
                    self.s10("phys", 0x02, reset_flag=True)
                    self.start_keepalive()
                    self.send_771_fb_a5()
                    continue

                # ------------------------------------------------------------
                # 情况3: 0x35 InvalidKey
                # 处理: 直接失败，不要继续打 ECU
                # ------------------------------------------------------------
                if "nrc=0x35" in msg or "7f2735" in msg.lower():
                    raise RuntimeError(
                        "27 12 InvalidKey (0x35): 当前 keygen 结果不正确，停止流程"
                    ) from e

                # 其他异常：直接抛出
                raise

        raise RuntimeError(f"27 解锁失败：重试 {max_attempts} 次后仍未成功")

    def s2e(self, did: int, data_record: bytes):
        step(f"2E {did:04X}")
        uds = bytes([0x2E, (did >> 8) & 0xFF, did & 0xFF]) + data_record

        if len(uds) <= 7:
            self.phys.send_sf(uds)
        else:
            self.phys.send_mf(uds)

        resp = self.phys.judge(0x2E, 0x6E)
        ok(resp.hex())
        return resp

    def s31(self, subfunction: int, rid: int, data: int, flag: int, file_valid_10: bytes, vercheck_1322: bytes):
        step(f"31 sub=0x{subfunction:02X} rid=0x{rid:04X} flag={flag}")

        tx = bytearray()
        tx.append(0x31)
        tx.append(subfunction & 0xFF)

        if flag == 0:
            tx.append((rid >> 8) & 0xFF)
            tx.append(rid & 0xFF)
            tx.append(0x44)
            tx.extend(APP_START_ADDRESS.to_bytes(4, "big"))
            tx.extend(APP_LENGTH.to_bytes(4, "big"))

        elif flag == 1:
            tx.append((rid >> 8) & 0xFF)
            tx.append(rid & 0xFF)
            tx.extend(data.to_bytes(4, "big"))

        elif flag == 2:
            tx.append((rid >> 8) & 0xFF)
            tx.append(rid & 0xFF)

        elif flag == 3:
            if len(file_valid_10) != 10:
                raise RuntimeError(f"file_valid_10 len must be 10, got {len(file_valid_10)}")
            tx.append((rid >> 8) & 0xFF)
            tx.append(rid & 0xFF)
            tx.extend(file_valid_10)

        elif flag == 4:
            if len(vercheck_1322) != 1322:
                raise RuntimeError(f"vercheck_1322 len must be 1322, got {len(vercheck_1322)}")
            tx.append((rid >> 8) & 0xFF)
            tx.append(rid & 0xFF)
            tx.extend(vercheck_1322)

        else:
            raise RuntimeError(f"unknown 31 flag={flag}")

        if rid == 0xFF00 and COMPARE_LOG_31FF00:
            compare_info("31 FF00 start | CAPL关键点: 会出现多次7F 31 78，期间伴随3E80")
            compare_info(f"31 FF00 payload={bytes(tx).hex()}")

        if len(tx) <= 7:
            self.phys.send_sf(bytes(tx))
        else:
            self.phys.send_mf(bytes(tx), suppress_cf_log=True)

        resp = self.phys.judge(0x31, 0x71)
        ok(resp.hex())

        # 对齐 CAPL server_31(): g_Rx.byte(5) 应为 0x04
        if len(resp) >= 5 and resp[4] != 0x04:
            raise RuntimeError(f"31 routine completed unsuccessfully, resp={resp.hex()}")

        if rid == 0xFF00 and COMPARE_LOG_31FF00:
            compare_info(f"31 FF00 final resp={resp.hex()}")

        return resp

    def s34(self, start_addr: int, data_len: int) -> int:
        step(f"34 addr=0x{start_addr:08X} len=0x{data_len:08X}")

        tx = bytes([
            0x34,
            0x00,
            0x44,
            (start_addr >> 24) & 0xFF,
            (start_addr >> 16) & 0xFF,
            (start_addr >> 8) & 0xFF,
            start_addr & 0xFF,
            (data_len >> 24) & 0xFF,
            (data_len >> 16) & 0xFF,
            (data_len >> 8) & 0xFF,
            data_len & 0xFF,
        ])

        self.phys.send_mf(tx)
        resp = self.phys.judge(0x34, 0x74)
        ok(resp.hex())

        block_length = (resp[2] << 8) | resp[3]
        ok(f"block_length=0x{block_length:04X} ({block_length})")
        return block_length

    def s36(self, all_data: bytes, block_len: int, is_app: bool) -> int:
        step(f"36 {'APP' if is_app else 'DRIVER'}")

        chunk_size = block_len - 2
        total = len(all_data)
        block_num = math.ceil(total / chunk_size)

        last_print_bucket = -1

        for block_index in range(1, block_num + 1):
            start = (block_index - 1) * chunk_size
            end = min(start + chunk_size, total)
            chunk = all_data[start:end]

            uds = bytes([0x36, block_index & 0xFF]) + chunk

            percent = end * 100.0 / total
            bucket = int(percent) // 10

            if not is_app:
                info(f"[36][DRIVER] progress={percent:.2f}%")
            else:
                if block_index == 1 or bucket > last_print_bucket:
                    info(f"[36][APP] progress={percent:.2f}%")
                    last_print_bucket = bucket

            compare_this_block = (is_app and COMPARE_LOG_36_APP_HEAD and block_index <= 3)

            if compare_this_block:
                compare_info(
                    f"36 APP block={block_index} start | "
                    f"seq=0x{block_index:02X} len={len(chunk)} "
                    f"payload_head={uds[:16].hex()}"
                )
                compare_info("36 APP CAPL关键点: 每块典型行为是 FC -> 7F 36 78 -> 76 xx")

            # 对齐 CAPL: 第一个 APP block 可保留特殊节点
            if is_app and block_index == 1:
                time.sleep(0.002)

            trace_name = None
            if compare_this_block:
                trace_name = f"36_APP_BLK_{block_index}"

            self.phys.send_mf(
                uds,
                suppress_cf_log=(LOG_PROGRESS_ONLY_FOR_36 and not compare_this_block),
                trace_name=trace_name,
            )
            resp = self.phys.judge(0x36, 0x76, trace=compare_this_block)

            if len(resp) < 2 or resp[1] != (block_index & 0xFF):
                raise RuntimeError(
                    f"36 seq mismatch, expect=0x{block_index:02X}, resp={resp.hex()}"
                )

            if compare_this_block:
                compare_info(f"36 APP block={block_index} final resp={resp.hex()}")

        checksum = crc32_capl_style(all_data)
        ok(f"36 done crc32=0x{checksum:08X}")
        return checksum

    def s37(self):
        step("37")
        self.phys.send_sf(bytes([0x37]))
        resp = self.phys.judge(0x37, 0x77)
        ok(resp.hex())
        return resp

    def send_771_fb_a5(self):
        step("0x771 FB A5")
        time.sleep(0.010)

        data = bytes([0x03, 0xFB, 0xA5, 0x00, PADDING, PADDING, PADDING, PADDING])
        msg = can.Message(
            arbitration_id=TX_771_SPECIAL,
            data=data,
            is_extended_id=False,
            is_fd=False,
            bitrate_switch=False,
        )

        with self.tx_lock:
            self.bus.send(msg)

        if LOG_VERBOSE_CAN:
            info(f"TX {hex(TX_771_SPECIAL)} {data.hex()}")

        ok("0x771 FB A5 sent")
        if COMPARE_MODE:
            compare_info("TX [771] [03 FB A5 00 55 55 55 55] | after 10ms")
        time.sleep(0.030)

    def enter_programming_and_unlock(self):
        # 对齐 CAPL Download():
        # 10 02 -> (ResetFlag wait 2000ms) -> 0x771 FB A5 -> 27 11/12
        self.s10("phys", 0x02, reset_flag=True)
        self.start_keepalive()
        self.send_771_fb_a5()
        self.s27_unlock()


# ============================================================
# 主流程
# ============================================================
def main():
    print("Python exe   :", sys.executable)
    print("Python arch  :", platform.architecture())
    print("Pointer size :", struct.calcsize("P") * 8)

    bus = can.Bus(**BUS_KWARGS)
    tx_lock = threading.RLock()

    phys = UDS(bus, TX_PHYS, RX_ID, tx_lock=tx_lock)
    func = UDS(bus, TX_FUNC, RX_ID, tx_lock=tx_lock)
    keygen = KeyGen(DLL_PATH, DLL_FUNC_NAME)
    svc = Service(bus, phys, func, keygen, tx_lock=tx_lock)

    try:
        step("BUS OPEN")
        phys.flush_rx(0.2)

        step("FILE INIT")
        driver_data = load_srec_to_buffer(DRIVER_SREC_PATH, DRIVER_START_ADDRESS, DRIVER_LENGTH)
        app_data = load_srec_to_buffer(APP_S19_PATH, APP_START_ADDRESS, APP_LENGTH)
        vercheck = read_vercheck_asc(VERCHECK_ASC_PATH)
        if len(vercheck) < 1322:
            raise RuntimeError(f"VerCheck too short: {len(vercheck)}")
        vercheck = vercheck[:1322]

        info(f"driver size={len(driver_data)}")
        info(f"app size={len(app_data)}")
        info(f"vercheck size={len(vercheck)}")

        # ============================================================
        # 对齐 CAPL Download()
        # ============================================================
        svc.s10("func", 0x01, reset_flag=True)

        f197 = svc.s22(0xF197)
        svc.s22(0xF150)
        f189 = svc.s22(0xF189)

        file_valid_10 = f189[:10]

        svc.s10("func", 0x03, reset_flag=False)
        svc.s85(0x02)
        svc.s28(0x03, 0x01)

        svc.enter_programming_and_unlock()

        svc.s31(0x01, 0x6000, 0, 4, file_valid_10, vercheck)
        svc.s31(0x01, 0x6001, 0, 2, file_valid_10, vercheck)

        svc.s2e(0xF198, SERIAL_DATA)
        svc.s2e(0xF199, calc_fingerdata_4bytes())

        driver_block_len = svc.s34(DRIVER_START_ADDRESS, DRIVER_LENGTH)
        driver_crc = svc.s36(driver_data, driver_block_len, is_app=False)
        svc.s37()

        svc.s31(0x01, 0x0202, driver_crc, 1, file_valid_10, vercheck)

        svc.s31(0x01, 0xFF00, 0, 0, file_valid_10, vercheck)

        app_block_len = svc.s34(APP_START_ADDRESS, FILE_LEN)
        _app_crc = svc.s36(app_data, app_block_len, is_app=True)
        svc.s37()

        svc.s31(0x01, 0x0203, 0, 2, file_valid_10, vercheck)
        svc.s31(0x01, 0xFF01, 0, 2, file_valid_10, vercheck)

        svc.stop_keepalive()

        svc.s11(0x01)

        svc.s10("func", 0x03, reset_flag=False)
        svc.s28(0x00, 0x01)
        svc.s85(0x01)
        svc.s14(0xFFFFFF)
        svc.s10("func", 0x01, reset_flag=True)

        step("DOWNLOAD FINISH")
        ok("Download complete")

    finally:
        try:
            svc.stop_keepalive()
        except Exception:
            pass
        bus.shutdown()
        info("BUS closed")


if __name__ == "__main__":
    main()