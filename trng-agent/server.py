#!/usr/bin/env python3
"""
启熵量子 真随机数生成 Agent · Web 服务（方案 A）
启动：python3 server.py  （默认 http://127.0.0.1:8080）
"""
import base64
import io
import json
import os

from flask import Flask, jsonify, request, send_file, Response

from trng_core import (TRNGDevice, make_numbers, stats, to_csv, to_json,
                       to_txt, to_xlsx, chart_png, qq_png, randomness_quality)

BASE = os.path.dirname(os.path.abspath(__file__))
# 兼容两种目录结构：index.html 与 server.py 同目录，或放在 templates/ 子目录
INDEX_CANDIDATES = [
    os.path.join(BASE, "index.html"),
    os.path.join(BASE, "templates", "index.html"),
]

app = Flask(__name__)

# 最近一次生成结果缓存（供导出）
LAST = {"mode": None, "values": None, "keys": None, "params": None,
        "chart": None, "qq": None, "entropy": 0}


@app.errorhandler(Exception)
def _on_error(e):
    """全局兜底：任何未捕获异常都返回带具体信息的 JSON，不再裸 500。"""
    return jsonify({"ok": False, "error": f"服务器内部错误：{e}"}), 500


def _device():
    return TRNGDevice().open()


@app.route("/")
def index():
    for path in INDEX_CANDIDATES:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return Response(f.read(), mimetype="text/html")
    return Response(
        "<h3>未找到 index.html</h3><p>请确认 index.html 与 server.py 在同一目录，"
        "或位于 templates/ 子目录。</p>",
        status=500, mimetype="text/html")


@app.route("/api/status")
def status():
    try:
        with _device() as dev:
            dev.read_bytes(64)
            return jsonify({"ok": True, "device": dev.port,
                            "message": "设备在线，物理熵源就绪"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e),
                        "message": "设备离线：请插入启熵量子 U 盘后重试"})


@app.route("/api/generate", methods=["POST"])
def generate():
    try:
        req = request.get_json(force=True)
        mode = req.get("mode", "numbers")

        if mode == "keys":
            count = int(req.get("count", 1))
            nbytes = int(req.get("key_bytes", 32))
            if not (1 <= count <= 100000):
                return jsonify({"ok": False, "error": "密钥数量需在 1~100000 之间"})
            if not (1 <= nbytes <= 4096):
                return jsonify({"ok": False, "error": "密钥长度需在 1~4096 字节之间"})

            with _device() as dev:
                keys = dev.keys_hex(count, nbytes)

            # 四项随机数质量检测：不通过则停止生成，不返回密钥
            raw = bytes.fromhex("".join(keys))
            qa = randomness_quality(raw)
            if not qa["passed"]:
                failed = [t["name"] for t in qa["tests"].values()
                          if not t["passed"] and not t.get("skipped")]
                return jsonify({"ok": False,
                                "error": "随机数质量检测未通过，已停止生成（未通过项："
                                         + "、".join(failed) + "），请重试或检查设备",
                                "quality": qa})

            LAST.update(mode="keys", keys=keys, values=None,
                        params=req, entropy=count * nbytes,
                        chart=None, qq=None)
            return jsonify({"ok": True, "mode": "keys", "keys": keys,
                            "key_bytes": nbytes, "hex_chars": nbytes * 2,
                            "entropy_bytes": count * nbytes, "quality": qa,
                            "note": "密钥字节直接取自物理熵源（启熵量子），未使用软件伪随机数"})

        # numbers 模式
        dist = req.get("dist", "normal")
        count = int(req.get("count", 100))
        lo = float(req.get("lo", 0.0))
        hi = float(req.get("hi", 1.0))
        decimals = int(req.get("decimals", 2))
        sigma = req.get("sigma")
        sigma = float(sigma) if sigma not in (None, "", 0) else None

        if not (1 <= count <= 100000):
            return jsonify({"ok": False, "error": "数量需在 1~100000 之间"})
        if dist not in ("uniform", "normal", "exponential"):
            return jsonify({"ok": False, "error": f"不支持的分布：{dist}"})
        if hi <= lo:
            return jsonify({"ok": False, "error": "上限必须大于下限"})
        if not (0 <= decimals <= 10):
            return jsonify({"ok": False, "error": "小数位数需在 0~10 之间"})

        with _device() as dev:
            vals, st = make_numbers(dist, count, lo, hi, decimals,
                                    sigma=sigma, device=dev)
            entropy = count * 8
            chart = base64.b64encode(
                chart_png(vals, f"{dist} 分布 · {count} 个样本 · 物理熵源")).decode()
            qq = base64.b64encode(qq_png(vals)).decode()

        LAST.update(mode="numbers", values=vals, keys=None,
                    params=req, entropy=entropy, chart=chart, qq=qq)
        return jsonify({"ok": True, "mode": "numbers", "values": vals,
                        "stats": st, "entropy_bytes": entropy,
                        "chart_png": chart, "qq_png": qq,
                        "note": "全部数值直接取自物理熵源（启熵量子），未使用软件伪随机数"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/clear", methods=["POST"])
def clear():
    """清空服务端最近结果缓存。"""
    LAST.update(mode=None, values=None, keys=None, params=None,
                chart=None, qq=None, entropy=0)
    return jsonify({"ok": True})


@app.route("/api/export")
def export():
    try:
        return _export_impl()
    except Exception as e:
        return jsonify({"ok": False, "error": f"导出失败：{e}"}), 400


def _export_impl():
    fmt = request.args.get("fmt", "csv")
    if LAST["mode"] == "numbers":
        values = LAST["values"]
        header = "value"
        stem = f"trng_{LAST['params'].get('dist', 'numbers')}_{len(values)}"
    elif LAST["mode"] == "keys":
        values = LAST["keys"]
        header = "key"
        stem = f"trng_hexkeys_{len(values)}"
    else:
        return jsonify({"ok": False, "error": "尚未生成任何数据"}), 400

    mimetypes = {"csv": "text/csv", "json": "application/json",
                 "txt": "text/plain",
                 "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 "png": "image/png", "qq": "image/png",
                 "bin": "application/octet-stream"}
    if fmt == "bin":
        if LAST["mode"] != "keys":
            return jsonify({"ok": False, "error": "bin 格式仅支持密钥导出"}), 400
        # 密钥原始字节：HEX 字符串转回二进制
        data = bytes.fromhex("".join(values))
        name = f"{stem}.bin"
    elif fmt == "csv":
        data, name = to_csv(values, header), f"{stem}.csv"
    elif fmt == "json":
        data, name = to_json(values), f"{stem}.json"
    elif fmt == "txt":
        data, name = to_txt(values, header), f"{stem}.txt"
    elif fmt == "xlsx":
        data, name = to_xlsx(values, header), f"{stem}.xlsx"
    elif fmt == "png":
        if not LAST["chart"]:
            return jsonify({"ok": False, "error": "当前结果无图表"}), 400
        data, name = base64.b64decode(LAST["chart"]), f"{stem}_hist.png"
    elif fmt == "qq":
        if not LAST["qq"]:
            return jsonify({"ok": False, "error": "当前结果无 QQ 图"}), 400
        data, name = base64.b64decode(LAST["qq"]), f"{stem}_qq.png"
    else:
        return jsonify({"ok": False, "error": f"不支持的格式：{fmt}"}), 400

    if isinstance(data, str):
        data = data.encode("utf-8")
    return send_file(io.BytesIO(data),
                     mimetype=mimetypes.get(fmt, "application/octet-stream"),
                     as_attachment=True, download_name=name)


if __name__ == "__main__":
    print("启熵量子 真随机数生成 Agent（方案 A）")
    print("请在浏览器打开： http://127.0.0.1:8080")
    app.run(host="127.0.0.1", port=8080, debug=False, threaded=True)
