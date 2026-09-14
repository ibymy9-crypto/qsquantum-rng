#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
密码学安全随机数采集器 - CDC 串口版
适配固件：N32G45x 硬件 TRNG + ADC 熵源，HMAC-SM3 DRBG 输出
VID=0x0483 PID=0x5740 (Windows 自动加载 usbser.sys)
支持流式采集、随机性检测 (GM/T 0078-2020)
"""

import serial
import serial.tools.list_ports
import time
import os
import argparse
import sys
import math
from scipy.special import gammaincc

# ---------- 设备参数 ----------
VID = 0x0483
PID = 0x5740
INITIAL_CHUNK = 65536   # 从 4096 改为 64KB

# ---------- 随机性测试 ----------
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


# ---------- 串口设备类 ----------
class RNGDevice:
    def __init__(self, port=None, baudrate=115200, timeout=1.0):
        self.ser = None
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.chunk_size = INITIAL_CHUNK
        self._connect()

    def _find_port(self):
        ports = list(serial.tools.list_ports.comports())
        print("\n可用串口列表:")
        for p in ports:
            print(f"  {p.device}: vid={p.vid}, pid={p.pid}, hwid={p.hwid}, desc={p.description}")

        if self.port is not None:
            if not any(p.device == self.port for p in ports):
                print(f"警告: 指定端口 {self.port} 不在列表中，仍尝试打开")
            return self.port

        for p in ports:
            if p.vid == VID and p.pid == PID:
                return p.device
            if p.hwid and f"VID_{VID:04X}&PID_{PID:04X}" in p.hwid.upper():
                return p.device

        print(f"\n未自动匹配到 VID=0x{VID:04X} PID=0x{PID:04X} 的设备。")
        print("请从上方列表中选择串口，输入设备名（如 COM5）：")
        choice = input("设备名: ").strip()
        if choice:
            return choice
        raise RuntimeError("未选择串口，退出。")

    def _connect(self):
        self.port = self._find_port()
        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=8,
            parity='N',
            stopbits=1,
            timeout=self.timeout,
            write_timeout=self.timeout
        )
        self.ser.dtr = True
        self.ser.rts = True
        print(f"已打开串口: {self.port}")

    def reconnect(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
        time.sleep(1)
        try:
            self._connect()
            self.chunk_size = INITIAL_CHUNK
            self.discard(1024*1024)
            print("重连并重新初始化成功")
            return True
        except Exception as e:
            print(f"重连失败: {e}")
            return False

    def read_chunk(self, timeout=None):
        orig_timeout = self.ser.timeout
        if timeout is not None:
            self.ser.timeout = timeout
        try:
            data = self.ser.read(self.chunk_size)
            return data if data else None
        except serial.SerialException as e:
            print(f"串口读取异常: {e}")
            return None
        finally:
            if timeout is not None:
                self.ser.timeout = orig_timeout

    def discard(self, num_bytes):
        discarded = 0
        while discarded < num_bytes:
            chunk = self.read_chunk(timeout=0.1)
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
    max_timeouts = 5
    reconnect_attempts = 0
    max_reconnects = 3

    while offset < sample_size:
        try:
            chunk = device.read_chunk(timeout=3.0)
        except Exception as e:
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
    parser = argparse.ArgumentParser(description="密码学安全随机数采集器 (TRNG+DRBG CDC 串口版)")
    parser.add_argument("-c", "--count", type=int, default=1, help="样本数量")
    parser.add_argument("-s", "--sample-size", type=int, default=None, help="每个样本字节数")
    parser.add_argument("-f", "--format", choices=["bin", "csv", "txt"], default="bin", help="输出格式")
    parser.add_argument("-o", "--output-dir", default="./samples", help="保存目录")
    parser.add_argument("--discard", type=int, default=1048576, help="初始丢弃字节数 (默认1MB)")
    parser.add_argument("--test-only", action="store_true", help="仅测试已有样本")
    parser.add_argument("--no-test", action="store_true", help="跳过随机性检测")
    parser.add_argument("--progress", type=float, default=1.0, help="进度显示间隔(秒)")
    parser.add_argument("--baudrate", type=int, default=115200, help="串口波特率")
    parser.add_argument("--port", type=str, default=None, help="指定串口号，如COM5")
    args = parser.parse_args()

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
    print("密码学安全随机数采集器 (TRNG+DRBG CDC 串口版)")
    print(f"串口波特率: {args.baudrate}")
    if args.port:
        print(f"指定端口: {args.port}")
    print("="*60)

    try:
        device = RNGDevice(port=args.port, baudrate=args.baudrate, timeout=1.0)
    except Exception as e:
        print(f"设备初始化失败: {e}")
        sys.exit(1)

    print("\n初始化设备，丢弃预热数据...")
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