#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
量子随机数稳定采集器 - 最终可用版
建议初始块大小: 4096 字节 (在 macOS 上稳定)
"""

import usb.core
import usb.util
import usb.backend.libusb1
import time
import os
import argparse
import sys
import ctypes
import ctypes.util
import math
from scipy.special import gammaincc

# ---------- 设备参数 ----------
VID = 0x19F5
PID = 0x5750
INITIAL_CHUNK = 4096          # ★ 关键：改为 4096 或 2048
MIN_CHUNK = 512

CMD_MODE_CH1 = 0x01
CMD_MODE_CH2 = 0x02
CMD_MODE_BOTH = 0x03
CMD_START_AUTO = 0x80
CMD_STOP = 0x00

# ---------- 随机性测试 (GM/T 0078-2020) ----------
ALPHA = 0.01
MAX_TEST_BITS = 1_000_000

def bytes_to_bits(data):
    bits = bytearray(len(data) * 8)
    for i, b in enumerate(data):
        bits[i*8+0] = (b >> 7) & 1
        bits[i*8+1] = (b >> 6) & 1
        bits[i*8+2] = (b >> 5) & 1
        bits[i*8+3] = (b >> 4) & 1
        bits[i*8+4] = (b >> 3) & 1
        bits[i*8+5] = (b >> 2) & 1
        bits[i*8+6] = (b >> 1) & 1
        bits[i*8+7] = (b >> 0) & 1
    return bits

def frequency_test(bits):
    n = len(bits)
    ones = sum(bits)
    S = 2 * ones - n
    z = S / math.sqrt(n)
    p = math.erfc(abs(z) / math.sqrt(2))
    return p, p >= ALPHA

def poker_test(bits, m=4):
    n = len(bits)
    blocks = n // m
    if blocks == 0:
        return 0.0, False
    freq = [0] * (1 << m)
    for i in range(blocks):
        val = 0
        for j in range(m):
            val = (val << 1) | bits[i*m + j]
        freq[val] += 1
    sum_sq = sum(f*f for f in freq)
    chi2 = ((1 << m) / blocks) * sum_sq - blocks
    df = (1 << m) - 1
    p = gammaincc(df/2.0, chi2/2.0)
    return p, p >= ALPHA

def runs_test(bits):
    n = len(bits)
    ones = sum(bits)
    if ones == 0 or ones == n:
        return 0.0, False
    runs = 1
    for i in range(1, n):
        if bits[i] != bits[i-1]:
            runs += 1
    pi = ones / n
    n0 = n - ones
    n1 = ones
    expected_runs = 2 * n1 * n0 / n + 1
    numerator = runs - expected_runs
    denominator = 2 * math.sqrt(n1 * n0 * (2 * n1 * n0 - n) / (n * n * (n - 1)))
    if denominator == 0:
        return 0.0, False
    z = numerator / denominator
    p = math.erfc(abs(z) / math.sqrt(2))
    return p, p >= ALPHA

def perform_tests(data_bytes, sample_index, total):
    orig_bits = len(data_bytes) * 8
    if orig_bits > MAX_TEST_BITS:
        print(f"样本 {sample_index}: 数据量 ({orig_bits} 比特) 超过检测上限，仅检测前 {MAX_TEST_BITS} 比特")
        data_bytes = data_bytes[:MAX_TEST_BITS // 8]
    bits = bytes_to_bits(data_bytes)
    n = len(bits)
    if n < 20000:
        print(f"警告: 样本 {sample_index} 比特数 {n} < 20000，不符合GM/T 0078要求")
    print(f"\n样本 {sample_index}/{total} 测试结果 (比特数={n}):")
    p_freq, pass_freq = frequency_test(bits)
    print(f"  单比特频数: p={p_freq:.6f} {'[通过]' if pass_freq else '[失败]'}")
    p_poker, pass_poker = poker_test(bits, 4)
    print(f"  扑克检测(m=4): p={p_poker:.6f} {'[通过]' if pass_poker else '[失败]'}")
    p_runs, pass_runs = runs_test(bits)
    print(f"  游程检测:     p={p_runs:.6f} {'[通过]' if pass_runs else '[失败]'}")
    return pass_freq, pass_poker, pass_runs

# ---------- USB 设备封装 ----------
class QRNGDevice:
    def __init__(self, libusb_path=None):
        self._libusb_path = libusb_path
        self.dev = None
        self._in_ep = None
        self._out_eps = []
        self.chunk_size = INITIAL_CHUNK
        self._connect()

    def _load_libusb(self):
        dll_path = self._libusb_path
        if dll_path is not None:
            lib_path = dll_path
        else:
            if sys.platform == "win32":
                script_dir = os.path.dirname(os.path.abspath(__file__))
                local_dll = os.path.join(script_dir, "libusb-1.0.dll")
                lib_path = local_dll if os.path.exists(local_dll) else "libusb-1.0.dll"
            elif sys.platform == "darwin":
                brew_paths = [
                    "/opt/homebrew/opt/libusb/lib/libusb-1.0.dylib",
                    "/usr/local/opt/libusb/lib/libusb-1.0.dylib",
                    "/usr/local/lib/libusb-1.0.dylib",
                ]
                found = next((p for p in brew_paths if os.path.exists(p)), None)
                lib_path = found or ctypes.util.find_library("libusb-1.0") or "libusb-1.0.dylib"
            else:
                lib_path = "libusb-1.0.so"

        try:
            ctypes.CDLL(lib_path)
            print(f"libusb 加载成功: {lib_path}")
        except Exception as e:
            print(f"警告: 加载库 {lib_path} 失败: {e}")

        backend = usb.backend.libusb1.get_backend(find_library=lambda x: lib_path)
        if backend is None:
            backend = usb.backend.libusb1.get_backend()
        if backend is None:
            raise RuntimeError("无法获取 libusb 后端，请安装 libusb。")
        return backend

    def _connect(self):
        self._out_eps = []
        backend = self._load_libusb()
        self.dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if self.dev is None:
            raise RuntimeError("设备未找到，请检查连接和驱动")

        try:
            self.dev.set_configuration()
        except usb.core.USBError as e:
            if e.errno != 16:
                raise

        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            for ep in intf:
                addr = ep.bEndpointAddress
                if usb.util.endpoint_direction(addr) == usb.util.ENDPOINT_IN:
                    if usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK:
                        self._in_ep = addr
                else:
                    if usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK:
                        self._out_eps.append(addr)

        if self._in_ep is None:
            raise RuntimeError("未找到 Bulk IN 端点")
        print(f"IN 端点: 0x{self._in_ep:02X}")
        if self._out_eps:
            print(f"OUT 端点: {[hex(a) for a in self._out_eps]}")

    def reconnect(self):
        """释放旧资源，重新连接并重新初始化设备"""
        if self.dev is not None:
            try:
                usb.util.dispose_resources(self.dev)
            except:
                pass
            self.dev = None
            self._in_ep = None
            self._out_eps = []
        time.sleep(1)
        try:
            self._connect()
            self.chunk_size = INITIAL_CHUNK
            # 重新初始化设备
            self.send_command(CMD_STOP)
            time.sleep(0.1)
            self.send_command(CMD_MODE_CH1)
            time.sleep(0.2)
            self.send_command(CMD_START_AUTO)
            time.sleep(0.1)
            self.discard(1024*1024)   # 丢弃1MB不稳定的初始数据
            print("重连并重新初始化成功")
            return True
        except Exception as e:
            print(f"重连失败: {e}")
            return False

    def read_chunk(self, timeout=1000):
        try:
            data = self.dev.read(self._in_ep, self.chunk_size, timeout=timeout)
            return bytes(data) if data else None
        except usb.core.USBError as e:
            errno = e.errno
            if errno == 110:
                return None
            elif errno == 32:
                print("  清除端点 halt...")
                try:
                    self.dev.clear_halt(self._in_ep)
                    data = self.dev.read(self._in_ep, self.chunk_size, timeout=timeout)
                    return bytes(data) if data else None
                except:
                    return None
            elif errno == 84:
                if self.chunk_size > MIN_CHUNK:
                    new_size = max(self.chunk_size // 2, MIN_CHUNK)
                    print(f"  Overflow，块大小从 {self.chunk_size} 减至 {new_size}")
                    self.chunk_size = new_size
                    try:
                        data = self.dev.read(self._in_ep, self.chunk_size, timeout=timeout)
                        return bytes(data) if data else None
                    except:
                        return None
                else:
                    print("  块大小已最小，仍溢出，放弃该包")
                    return None
            else:
                raise RuntimeError(f"USB 错误 {errno}: {e}")
        except Exception as e:
            raise RuntimeError(f"读取异常: {e}")

    def write(self, data, timeout=1000):
        if not self._out_eps:
            return False
        for ep in self._out_eps:
            try:
                self.dev.write(ep, data, timeout=timeout)
                return True
            except:
                continue
        return False

    def send_command(self, cmd):
        ok = self.write([cmd])
        if ok:
            print(f"命令 0x{cmd:02X} 已发送")
        return ok

    def discard(self, num_bytes):
        discarded = 0
        while discarded < num_bytes:
            chunk = self.read_chunk(timeout=100)
            if chunk is None:
                break
            discarded += len(chunk)
        print(f"已丢弃 {discarded} 字节")

# ---------- 采集函数 ----------
def stream_sample(device, sample_size, progress_interval=1.0):
    buf = bytearray(sample_size)
    offset = 0
    start_time = time.time()
    last_print = start_time
    timeouts = 0
    max_timeouts = 15
    reconnect_attempts = 0
    max_reconnects = 3

    while offset < sample_size:
        need = sample_size - offset
        req_len = min(device.chunk_size, need)

        try:
            chunk = device.read_chunk(timeout=1000)
        except RuntimeError as e:
            print(f"严重错误: {e}")
            if reconnect_attempts >= max_reconnects:
                raise RuntimeError("重连次数上限，采集终止")
            if device.reconnect():
                reconnect_attempts += 1
                offset = 0
                timeouts = 0
                print("已重置缓冲区，重新采集该样本")
                continue
            else:
                raise RuntimeError("重连失败")

        if chunk is None:
            timeouts += 1
            if timeouts >= max_timeouts:
                print(f"连续 {max_timeouts} 次超时，尝试重连...")
                if reconnect_attempts >= max_reconnects:
                    raise RuntimeError("重连次数上限")
                if device.reconnect():
                    reconnect_attempts += 1
                    offset = 0
                    timeouts = 0
                    print("已重置缓冲区，重新采集该样本")
                    continue
                else:
                    raise RuntimeError("重连失败")
            time.sleep(0.005)
            continue

        timeouts = 0
        n = len(chunk)
        if offset + n > sample_size:
            n = sample_size - offset
            chunk = chunk[:n]
        buf[offset:offset+n] = chunk
        offset += n

        now = time.time()
        if now - last_print >= progress_interval:
            elapsed = now - start_time
            speed = offset / elapsed if elapsed > 0 else 0
            pct = (offset / sample_size) * 100
            eta = (sample_size - offset) / speed if speed > 0 else 0
            print(f"  已读: {offset:,}/{sample_size:,} 字节 ({pct:.1f}%) | "
                  f"速率 {speed/1024:.1f} KB/s | ETA {eta:.1f}s")
            last_print = now

    elapsed = time.time() - start_time
    speed = sample_size / elapsed if elapsed > 0 else 0
    print(f"  完成: {sample_size:,} 字节 | 总耗时 {elapsed:.1f}s | "
          f"平均速率 {speed/1024:.1f} KB/s")
    return bytes(buf)

# ---------- 保存与测试 ----------
def hexdump(data, idx, bytes_per_line=16):
    lines = [f"Sample {idx}  (len={len(data)} bytes)\n"]
    for i in range(0, len(data), bytes_per_line):
        chunk = data[i:i+bytes_per_line]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f"{i:08x}  {hex_part:<{bytes_per_line*3}}  {ascii_part}")
    return '\n'.join(lines)

def save_sample(sample, idx, output_dir, fmt):
    if fmt == "bin":
        path = os.path.join(output_dir, f"sample_{idx:04d}.bin")
        with open(path, "wb") as f:
            f.write(sample)
        print(f"  已保存: {path}")
    elif fmt == "txt":
        path = os.path.join(output_dir, f"sample_{idx:04d}.txt")
        with open(path, "w") as f:
            f.write(hexdump(sample, idx))
        print(f"  已保存: {path}")
    elif fmt == "csv":
        path = os.path.join(output_dir, "samples.csv")
        with open(path, "a") as f:
            if idx == 1:
                f.write("sample_index,data_hex\n")
            f.write(f"{idx},{sample.hex().upper()}\n")
        print(f"  已追加到: {path}")

def test_existing_samples(output_dir, sample_count=None, sample_size=125000):
    if sample_count is None:
        import glob
        files = sorted(glob.glob(os.path.join(output_dir, "sample_*.bin")))
        if not files:
            print("未找到样本文件")
            return
        sample_count = len(files)
        print(f"自动检测到 {sample_count} 个样本")

    total_pass = [0, 0, 0]
    for idx in range(1, sample_count+1):
        path = os.path.join(output_dir, f"sample_{idx:04d}.bin")
        if not os.path.exists(path):
            print(f"文件 {path} 不存在，跳过")
            continue
        with open(path, "rb") as f:
            data = f.read(sample_size)
        if len(data) < sample_size:
            print(f"文件 {path} 大小不足，跳过")
            continue
        results = perform_tests(data, idx, sample_count)
        for i, r in enumerate(results):
            if r:
                total_pass[i] += 1

    print("\n" + "="*60)
    print(f"随机性检测汇总（共 {sample_count} 个样本）")
    print("-"*60)
    names = ["单比特频数", "扑克检测(m=4)", "游程检测"]
    fails = [sample_count - p for p in total_pass]
    critical = int(ALPHA * sample_count + 3 * math.sqrt(ALPHA * (1 - ALPHA) * sample_count)) + 1
    all_pass = all(f <= critical for f in fails)
    for name, p, f in zip(names, total_pass, fails):
        print(f"{name}: 通过 {p} 个，失败 {f} 个")
    print("\n整体判定:", "全部通过，随机性合格。" if all_pass else "存在未通过项目，随机性不合格。")
    print("="*60)

# ---------- 交互输入 ----------
def interactive_count():
    while True:
        try:
            v = input("请输入样本数量 (>0): ").strip()
            if v:
                n = int(v)
                if n > 0:
                    return n
        except ValueError:
            pass
        print("请输入正整数。")

def interactive_size():
    presets = [
        (125000, "1 Mb (125,000 字节)"),
        (1250000, "10 Mb (1,250,000 字节)"),
        (12500000, "100 Mb (12,500,000 字节)"),
        (125000000, "1 Gb (125,000,000 字节)"),
    ]
    print("\n选择样本长度:")
    for i, (b, desc) in enumerate(presets, 1):
        print(f"  {i}. {desc}")
    print("  5. 自定义")
    while True:
        c = input("选项 (1-5): ").strip()
        if c in '1234':
            return presets[int(c)-1][0]
        if c == '5':
            try:
                b = int(input("字节数: ").strip())
                if b > 0:
                    return b
            except ValueError:
                pass
        print("无效选项。")

# ---------- 主程序 ----------
def main():
    parser = argparse.ArgumentParser(description="量子随机数稳定采集器 (最终可用版)")
    parser.add_argument("-c", "--count", type=int, default=1, help="样本数量")
    parser.add_argument("-s", "--sample-size", type=int, default=None, help="每个样本字节数")
    parser.add_argument("-f", "--format", choices=["bin", "csv", "txt"], default="bin", help="输出格式")
    parser.add_argument("-o", "--output-dir", default="./samples", help="保存目录")
    parser.add_argument("--discard", type=int, default=1048576, help="初始丢弃字节数 (默认1MB)")
    parser.add_argument("--skip-commands", action="store_true", help="跳过命令发送")
    parser.add_argument("--test-only", action="store_true", help="仅测试已有样本")
    parser.add_argument("--no-test", action="store_true", help="跳过随机性检测")
    parser.add_argument("--progress", type=float, default=1.0, help="进度显示间隔(秒)")
    parser.add_argument("--libusb-path", default=None, help="手动指定 libusb 路径")
    args = parser.parse_args()

    if args.count <= 0:
        print("错误: --count 必须大于 0")
        sys.exit(1)

    if args.test_only:
        test_existing_samples(args.output_dir, args.count, args.sample_size or 125000)
        return

    if not any(a.startswith(('--count', '-c')) for a in sys.argv):
        args.count = interactive_count()
    if args.sample_size is None and not any(a.startswith(('--sample-size', '-s')) for a in sys.argv):
        args.sample_size = interactive_size()

    args.output_dir = os.path.abspath(args.output_dir)
    os.makedirs(args.output_dir, exist_ok=True)

    print("="*60)
    print("量子随机数稳定采集器 (最终可用版)")
    print(f"初始块大小: {INITIAL_CHUNK} 字节 (自适应)")
    print("="*60)

    try:
        device = QRNGDevice(libusb_path=args.libusb_path)
    except Exception as e:
        print(f"设备初始化失败: {e}")
        sys.exit(1)

    # 检测自动发送
    print("\n检测设备是否自动发送数据...")
    test_data = device.read_chunk(timeout=100)
    auto_send = test_data is not None and len(test_data) > 0

    if auto_send:
        print(f"设备自动发送数据 (首包 {len(test_data)} 字节)，跳过命令。")
        device.discard(args.discard)
    else:
        print("未检测到自动发送，发送命令...")
        if args.skip_commands:
            print("--skip-commands 已设置，退出")
            sys.exit(1)
        device.send_command(CMD_STOP)
        time.sleep(0.1)
        device.send_command(CMD_MODE_CH1)
        time.sleep(0.2)
        device.send_command(CMD_START_AUTO)
        time.sleep(0.1)
        device.discard(args.discard)

    print(f"\n开始采集 {args.count} 个样本，每个 {args.sample_size:,} 字节...")
    valid_samples = 0
    pass_freq_total = pass_poker_total = pass_runs_total = 0

    for idx in range(1, args.count+1):
        print(f"\n--- 样本 {idx}/{args.count} ---")
        try:
            sample = stream_sample(device, args.sample_size, progress_interval=args.progress)
        except Exception as e:
            print(f"样本 {idx} 采集失败: {e}")
            continue

        save_sample(sample, idx, args.output_dir, args.format)

        if not args.no_test:
            pass_freq, pass_poker, pass_runs = perform_tests(sample, idx, args.count)
            valid_samples += 1
            if pass_freq: pass_freq_total += 1
            if pass_poker: pass_poker_total += 1
            if pass_runs: pass_runs_total += 1

    if not auto_send:
        device.send_command(CMD_STOP)

    if not args.no_test and valid_samples > 0:
        print("\n" + "="*60)
        print(f"随机性检测汇总 (有效样本数: {valid_samples})")
        print("-"*60)
        names = ["单比特频数", "扑克检测(m=4)", "游程检测"]
        fails = [valid_samples - p for p in (pass_freq_total, pass_poker_total, pass_runs_total)]
        critical = int(ALPHA * valid_samples + 3 * math.sqrt(ALPHA * (1 - ALPHA) * valid_samples)) + 1
        for name, p, f in zip(names, (pass_freq_total, pass_poker_total, pass_runs_total), fails):
            print(f"{name}: 通过 {p} 个，失败 {f} 个")
        all_pass = all(f <= critical for f in fails)
        print("\n整体判定:", "全部通过，随机性合格。" if all_pass else "存在未通过项目，随机性不合格。")
        print("="*60)
    elif not args.no_test and valid_samples == 0:
        print("\n没有成功采集到任何样本，无法进行检测汇总。")

    print("\n全部完成")

if __name__ == "__main__":
    main()