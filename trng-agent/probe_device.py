#!/usr/bin/env python3
"""HS-TRNG-01 设备探测：尝试常见波特率读取数据流，分析字节分布。"""
import serial
import time
import collections
import sys

PORT = "/dev/cu.usbmodemN32G45x1"
BAUDS = [115200, 9600, 57600, 38400, 230400, 460800, 921600]

def probe(baud):
    try:
        ser = serial.Serial(PORT, baud, timeout=0.5)
    except Exception as e:
        return f"open fail: {e}"
    ser.reset_input_buffer()
    time.sleep(0.3)
    data = ser.read(2048)
    ser.close()
    if not data:
        return "no data (可能需主机命令或固件未输出)"
    counter = collections.Counter(data)
    n = len(data)
    # 简单均匀性评估
    expected = n / 256.0
    chi2 = sum((c - expected) ** 2 / expected for c in counter.values())
    # 熵估计
    import math
    entropy = -sum((c / n) * math.log2(c / n) for c in counter.values())
    return (f"OK  {n} bytes | entropy={entropy:.3f} bits/byte | "
            f"unique={len(counter)}/256 | chi2≈{chi2:.0f} (期望~255) | "
            f"sample={data[:16].hex(' ')}")

if __name__ == "__main__":
    print(f"设备: {PORT}")
    for b in BAUDS:
        print(f"波特率 {b:>7}: {probe(b)}")
