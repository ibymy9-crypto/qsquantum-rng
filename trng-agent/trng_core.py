#!/usr/bin/env python3
"""
启熵量子 真随机数核心引擎（方案 A · MacOS 串口通信版）
========================================================
从 USB 串口读取物理随机字节（量子 TRNG 熵源），提供：
  - 均匀分布 / 正态分布 / 指数分布（截断）浮点数生成
  - HEX 密钥生成
  - 四项随机数质量检测（频率/游程/卡方/熵），不通过则停止
  - 熵源健康检查（全零/全同/字节多样性）
  - 导出：CSV / JSON / TXT / XLSX / PNG 图表

用法（模块方式）：
    from trng_core import TRNGDevice, make_numbers, keys_hex
"""
import collections
import csv
import io
import json
import logging
import math
import os
import time

import serial
import numpy as np

# 屏蔽 matplotlib 字体警告噪音
logging.getLogger("matplotlib").setLevel(logging.ERROR)
logging.getLogger("PIL").setLevel(logging.ERROR)

DEFAULT_PORT = "/dev/cu.usbmodemN32G45x1"
DEFAULT_BAUD = 115200
MAX_GUARD = 20000  # 截断采样最大尝试次数


class TRNGDevice:
    """启熵量子 USB 串口随机字节读取器（带健康检查）。"""

    def __init__(self, port=DEFAULT_PORT, baud=DEFAULT_BAUD, timeout=2.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None

    def open(self):
        if not os.path.exists(self.port):
            raise RuntimeError(
                f"设备未连接：{self.port} 不存在。请插入启熵量子 U 盘后重试。"
            )
        self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        self.ser.reset_input_buffer()
        return self

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            finally:
                self.ser = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def read_bytes(self, n: int) -> bytes:
        """读取 n 个原始随机字节，循环补足并做健康检查。"""
        if n <= 0:
            return b""
        buf = bytearray()
        idle = 0
        while len(buf) < n:
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
                idle = 0
            else:
                idle += 1
                if idle > 100:
                    raise RuntimeError("熵源无数据输出：请确认设备固件在持续发送随机字节。")
                time.sleep(0.05)
        self._health_check(bytes(buf))
        return bytes(buf)

    @staticmethod
    def _health_check(data: bytes):
        """熵源健康检查：全零/全同字节直接报错。"""
        if len(data) == 0:
            raise RuntimeError("熵源返回空数据。")
        if len(set(data)) == 1:
            raise RuntimeError(
                f"熵源异常：所有字节均为 0x{data[0]:02X}，疑似硬件故障或固件异常。"
            )

    def keys_hex(self, count: int, nbytes: int) -> list:
        """生成 count 个 nbytes 字节的 HEX 密钥。"""
        raw = self.read_bytes(count * nbytes)
        keys = []
        for i in range(count):
            chunk = raw[i * nbytes:(i + 1) * nbytes]
            keys.append(chunk.hex().upper())
        return keys


# ============================================================
# 四项随机数质量检测（NIST 标准）
# ============================================================

def _erf(x: float) -> float:
    """误差函数（Abramowitz & Stegun 近似，精度 ~1e-7）。"""
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
                - 0.284496736) * t + 0.254829592) * t * math.exp(-x * x)
    return sign * y


def _erfc(x: float) -> float:
    return 1.0 - _erf(x)


def _bytes_to_bits(data: bytes) -> list:
    bits = []
    for b in data:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)
    return bits


def randomness_quality(data: bytes) -> dict:
    """四项随机数质量检测。

    返回 {passed, sample_bytes, tests: {monobit, runs, chi2, entropy}}
    每项含 name/passed/p_value 或 value/threshold；样本不足时 skipped=True。
    任一有效项不通过则整体 passed=False。
    """
    n = len(data)
    nbits = n * 8
    bits = _bytes_to_bits(data)
    ones = sum(bits)

    # 1. 单比特频率检验（NIST SP800-22 Monobit）
    if nbits >= 100:
        s_obs = abs(2 * ones - nbits) / math.sqrt(nbits)
        p_mono = _erfc(s_obs / math.sqrt(2))
        mono = {"name": "单比特频率检验", "passed": p_mono >= 0.01,
                "p_value": round(p_mono, 6), "ones_ratio": round(ones / nbits, 4)}
    else:
        mono = {"name": "单比特频率检验", "passed": True, "skipped": True,
                "note": f"样本不足（{nbits}<100 bit），跳过"}

    # 2. 游程检验（NIST Runs）
    if nbits >= 100:
        runs = 1
        for i in range(1, nbits):
            if bits[i] != bits[i - 1]:
                runs += 1
        pi = ones / nbits
        tau = 2.0 / math.sqrt(nbits)
        if abs(pi - 0.5) >= tau:
            p_runs = 0.0
        else:
            numerator = abs(runs - 2 * nbits * pi * (1 - pi))
            denominator = 2 * math.sqrt(2 * nbits) * pi * (1 - pi)
            p_runs = _erfc(numerator / denominator) if denominator > 0 else 0.0
        runs_t = {"name": "游程检验", "passed": p_runs >= 0.01,
                  "p_value": round(p_runs, 6), "runs": runs}
    else:
        runs_t = {"name": "游程检验", "passed": True, "skipped": True,
                  "note": "样本不足，跳过"}

    # 3. 字节分布卡方检验（255 df；样本不足自动跳过）
    counter = collections.Counter(data)
    expected = n / 256.0
    chi2 = sum((counter.get(i, 0) - expected) ** 2 / expected for i in range(256))
    if n >= 256:
        # 用正态近似卡方 CDF（df=255 时均值=255，方差=510）
        z = (chi2 - 255) / math.sqrt(510)
        p_chi = _erfc(z / math.sqrt(2))
        chi_t = {"name": "字节分布卡方检验", "passed": p_chi >= 0.01,
                 "p_value": round(p_chi, 6), "chi2": round(chi2, 2)}
    else:
        chi_t = {"name": "字节分布卡方检验", "passed": True, "skipped": True,
                 "note": f"样本不足（{n}<256 字节），跳过", "chi2": round(chi2, 2)}

    # 4. 香农熵检验（动态阈值 = min(8, log2(n)) * 0.9）
    entropy = 0.0
    for cnt in counter.values():
        if cnt > 0:
            p = cnt / n
            entropy -= p * math.log2(p)
    ent_threshold = min(8.0, math.log2(n)) * 0.9 if n > 1 else 0.0
    ent_t = {"name": "香农熵检验", "passed": entropy >= ent_threshold,
             "value": round(entropy, 4), "threshold": round(ent_threshold, 4),
             "unit": "bits/byte"}

    passed = all(t.get("passed", True) for t in [mono, runs_t, chi_t, ent_t])
    return {
        "passed": passed,
        "sample_bytes": n,
        "tests": {"monobit": mono, "runs": runs_t, "chi2": chi_t, "entropy": ent_t}
    }


# ============================================================
# 分布随机数生成
# ============================================================

def _uniform01(dev: TRNGDevice) -> float:
    """8 字节 → (0, 1] 均匀浮点数。"""
    b = dev.read_bytes(8)
    v = int.from_bytes(b, "big")
    return (v + 1) / 18446744073709551616.0


def _box_muller(dev: TRNGDevice):
    u1 = _uniform01(dev)
    u2 = _uniform01(dev)
    z0 = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
    z1 = math.sqrt(-2 * math.log(u1)) * math.sin(2 * math.pi * u2)
    return z0, z1


def make_numbers(dist: str, count: int, lo: float, hi: float,
                 decimals: int = 2, sigma: float = None,
                 device: TRNGDevice = None):
    """生成分布随机数。

    dist: normal / uniform / exponential
    返回 (values_list, stats_dict)
    """
    if hi <= lo:
        raise ValueError("上限必须大于下限")
    if count <= 0:
        raise ValueError("数量必须大于 0")
    if decimals < 0:
        raise ValueError("小数位数不能为负")

    dev = device
    own_dev = False
    if dev is None:
        dev = TRNGDevice().open()
        own_dev = True

    try:
        vals = []
        mu = (lo + hi) / 2.0
        if sigma is None:
            sigma = (hi - lo) / 6.0

        guard = 0
        while len(vals) < count:
            guard += 1
            if guard > MAX_GUARD:
                raise RuntimeError(
                    "截断采样失败：σ 过大导致拒绝率过高，请减小 σ 或扩大范围。"
                )
            if dist == "normal":
                z0, z1 = _box_muller(dev)
                for z in (z0, z1):
                    x = mu + sigma * z
                    if lo <= x <= hi and len(vals) < count:
                        vals.append(round(x, decimals))
            elif dist == "uniform":
                u = _uniform01(dev)
                x = lo + (hi - lo) * u
                vals.append(round(x, decimals))
            elif dist == "exponential":
                lam = 2.0 / (hi - lo)
                u = _uniform01(dev)
                x = lo - math.log(u) / lam
                if lo <= x <= hi:
                    vals.append(round(x, decimals))
            else:
                raise ValueError(f"不支持的分布：{dist}")
        st = stats(vals)
        return vals, st
    finally:
        if own_dev:
            dev.close()


def stats(vals: list) -> dict:
    n = len(vals)
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / n
    sd = math.sqrt(var)
    srt = sorted(vals)
    median = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
    return {"count": n, "mean": round(mean, 6), "std": round(sd, 6),
            "min": min(vals), "max": max(vals), "median": round(median, 6)}


# ============================================================
# 导出
# ============================================================

def to_csv(vals, header="value") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([header])
    for v in vals:
        w.writerow([v])
    return buf.getvalue().encode("utf-8")


def to_json(vals) -> bytes:
    return json.dumps(vals, ensure_ascii=False, indent=2).encode("utf-8")


def to_txt(vals, header="value") -> bytes:
    lines = [header] + [str(v) for v in vals]
    return ("\n".join(lines) + "\n").encode("utf-8")


def to_xlsx(vals, header="value") -> bytes:
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "data"
        ws.append([header])
        for v in vals:
            ws.append([v])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except ImportError:
        raise RuntimeError("需要 openpyxl：pip install openpyxl")


# ============================================================
# 图表（matplotlib，中文字体修复）
# ============================================================

def _setup_matplotlib():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    for f in ["/System/Library/Fonts/PingFang.ttc",
              "/System/Library/Fonts/STHeiti Light.ttc",
              "/Library/Fonts/Arial Unicode.ttf",
              "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"]:
        if os.path.exists(f):
            from matplotlib.font_manager import FontProperties
            plt.rcParams["font.family"] = FontProperties(fname=f).get_name()
            break
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def chart_png(vals, title="分布直方图") -> bytes:
    plt = _setup_matplotlib()
    fig, ax = plt.subplots(figsize=(8, 5))
    n_bins = min(30, max(10, int(len(vals) / 4)))
    ax.hist(vals, bins=n_bins, density=True, alpha=0.7, color="#8BC8EA",
            edgecolor="white", label="样本")
    mu = sum(vals) / len(vals)
    sd = math.sqrt(sum((x - mu) ** 2 for x in vals) / len(vals))
    if sd > 0:
        xs = np.linspace(min(vals), max(vals), 200)
        ys = (1 / (sd * math.sqrt(2 * math.pi))) * np.exp(-0.5 * ((xs - mu) / sd) ** 2)
        ax.plot(xs, ys, color="#1A1B1C", linewidth=1.8, label="正态拟合")
    ax.set_title(title, fontsize=14)
    ax.set_xlabel("值")
    ax.set_ylabel("密度")
    ax.legend()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    return buf.getvalue()


def qq_png(vals) -> bytes:
    plt = _setup_matplotlib()
    fig, ax = plt.subplots(figsize=(8, 5))
    n = len(vals)
    srt = sorted(vals)
    theory = [_inv_norm((i + 0.5) / n) for i in range(n)]
    ax.scatter(theory, srt, s=12, alpha=0.7, color="#94D4D0", label="样本")
    mt = sum(theory) / n
    ms = sum(srt) / n
    stt = sum((t - mt) ** 2 for t in theory)
    sts = sum((s - ms) ** 2 for s in srt)
    stst = sum((theory[i] - mt) * (srt[i] - ms) for i in range(n))
    slope = stst / stt if stt > 0 else 1
    intercept = ms - slope * mt
    r2 = (stst ** 2 / (stt * sts)) if stt > 0 and sts > 0 else 0
    xs = [min(theory), max(theory)]
    ys = [slope * x + intercept for x in xs]
    ax.plot(xs, ys, color="#1A1B1C", linewidth=1.5, label=f"拟合 R²={r2:.4f}")
    ax.set_title("正态 QQ 图", fontsize=14)
    ax.set_xlabel("理论分位数")
    ax.set_ylabel("样本分位数")
    ax.legend()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    return buf.getvalue()


def _inv_norm(p: float) -> float:
    """标准正态分布逆 CDF（Beasley-Springer-Moro 近似）。"""
    if p <= 0:
        return -float("inf")
    if p >= 1:
        return float("inf")
    a = [-39.6968302866538, 220.946098424521, -275.928510446969,
         138.357751867269, -30.6647980661472, 2.50662827745924]
    b = [-54.4760987982241, 161.585836858041, -155.698979859887,
         66.8013118877197, -13.2806815528857]
    c = [-0.00778489400243029, -0.322396458041136, -2.40075827716184,
         -2.54973253934373, 4.37466414146497, 2.93816398269878]
    d = [0.00778469570904146, 0.32246712907004, 2.445134137143, 3.75440866190742]
    q = p - 0.5
    if abs(q) <= 0.02425:
        r = q * q
        x = q * (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) \
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    else:
        r = p if q > 0 else 1 - p
        r = math.sqrt(-math.log(r))
        x = (((((c[0] * r + c[1]) * r + c[2]) * r + c[3]) * r + c[4]) * r + c[5]) \
            / ((((d[0] * r + d[1]) * r + d[2]) * r + d[3]) * r + 1)
        if q < 0:
            x = -x
    return x
