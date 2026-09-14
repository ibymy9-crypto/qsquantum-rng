# 启熵量子 真随机数生成 Agent（跨平台版）

基于 U 盘量子随机数发生器（启熵量子，N32G45x + WT-QRNG300）构建的本地随机数生成工具。
所有数字与密钥的熵全部直接取自硬件物理熵源，**不使用任何软件伪随机数**。

支持 **Windows / macOS / Linux / 鸿蒙** 四大平台，自动识别并连接硬件。

---

## 两种使用模式

### 模式一：Web Serial 纯前端版（推荐，跨平台通用）

**无需安装 Python，浏览器直接读取硬件。** 支持 Windows / macOS / Linux 的 Chrome、Edge、Opera 浏览器；鸿蒙系统若浏览器基于 Chromium 内核且支持 Web Serial API，也可直接使用。

```bash
# 方式 A：直接用浏览器打开文件
open web-serial-app.html          # macOS
start web-serial-app.html          # Windows
xdg-open web-serial-app.html       # Linux

# 方式 B：通过本地服务访问（推荐，避免 file:// 限制）
python3 server.py
# 浏览器打开 http://127.0.0.1:8080/web-serial
```

打开后点击「连接设备」，在弹出的设备选择框中选择启熵量子 U 盘即可。

> **浏览器要求**：Chrome 89+ / Edge 89+ / Opera。Safari 和 Firefox 暂不支持 Web Serial API，请使用 Chrome 或 Edge。

### 模式二：Python 后端版（功能最全，含 Excel 导出）

需要安装 Python 3.8+ 和依赖，提供完整的 Excel 导出、服务端 API 等功能。

```bash
pip3 install pyserial numpy matplotlib flask openpyxl
python3 server.py
# 浏览器打开 http://127.0.0.1:8080
```

---

## 各平台使用指南

### Windows

1. 插入启熵量子 U 盘，系统自动识别为 USB 串行设备（COM 端口）
2. **Web Serial 版**：用 Chrome/Edge 打开 `web-serial-app.html`，点击「连接设备」选择 COM 端口
3. **Python 版**：安装 Python 3.8+，运行 `pip install pyserial numpy matplotlib flask openpyxl`，然后 `python server.py`
4. 设备自动发现：程序会枚举所有 COM 端口，验证随机数据后自动连接，无需手动指定端口号

### macOS

1. 插入启熵量子 U 盘，设备自动枚举为 `/dev/cu.usbmodemN32G45x1`
2. **Web Serial 版**：用 Chrome/Edge 打开 `web-serial-app.html`
3. **Python 版**：`pip3 install pyserial numpy matplotlib flask openpyxl && python3 server.py`
4. 设备自动发现：自动枚举所有串口，无需手动指定路径

### Linux

1. 插入启熵量子 U 盘，设备通常枚举为 `/dev/ttyACM0` 或 `/dev/ttyUSB0`
2. **权限设置**（首次使用）：
   ```bash
   # 将当前用户加入 dialout 组（Debian/Ubuntu）
   sudo usermod -aG dialout $USER
   # 或临时赋予权限
   sudo chmod 666 /dev/ttyACM0
   # 重新登录后生效
   ```
3. **Web Serial 版**：用 Chrome/Edge 打开 `web-serial-app.html`
4. **Python 版**：`pip3 install pyserial numpy matplotlib flask openpyxl && python3 server.py`
5. 设备自动发现：自动枚举 `/dev/ttyACM*` 和 `/dev/ttyUSB*`，验证随机数据后自动连接

### 鸿蒙（HarmonyOS）

鸿蒙系统不支持 Python 运行时，因此**只能使用 Web Serial 纯前端版**：

1. 用 OTG 转接线将启熵量子 U 盘连接到鸿蒙设备（手机/平板）
2. 用鸿蒙浏览器打开 `web-serial-app.html`（需先将文件传输到设备，或通过局域网访问 `http://<电脑IP>:8080/web-serial`）
3. 点击「连接设备」，在弹出的权限对话框中授权 USB 设备访问
4. 若鸿蒙浏览器不支持 Web Serial API，请使用电脑端操作，或等待鸿蒙浏览器后续版本支持

> **注意**：鸿蒙系统对 USB Host 权限有严格管控，首次连接需要在系统弹窗中授权。部分鸿蒙设备可能需要在「设置 → 系统 → 开发者选项」中开启 USB 调试。

---

## 跨平台自动发现机制

程序不再硬编码串口路径，而是采用**自动发现 + 随机数据验证**的策略：

1. **枚举所有串口**：使用 pyserial `list_ports` 跨平台枚举，并补充 glob 匹配（macOS 的 `cu.usbmodem*`、Linux 的 `ttyACM*`/`ttyUSB*`）
2. **逐个验证**：对每个候选端口，打开并读取 256 字节，通过香农熵检测（>5.0 bits/byte）判断是否为 TRNG 设备
3. **自动连接**：第一个通过验证的端口即为目标设备
4. **失败提示**：未找到设备时，列出所有可用串口，提示用户检查连接或手动指定端口

手动指定端口（自动发现失败时）：
```python
from trng_core import TRNGDevice
with TRNGDevice(port="COM3").open() as dev:   # Windows
    ...
with TRNGDevice(port="/dev/ttyACM0").open() as dev:   # Linux
    ...
```

---

## 功能

| 功能 | 说明 |
|---|---|
| 均匀分布 | 任意范围、任意数量的浮点数/整数 |
| 正态分布 | Box-Muller 变换 + 截断采样，保证硬边界；σ 可自定义（默认区间/6） |
| 指数分布 | 逆变换截断采样 |
| HEX 密钥 | 任意字节长度（1 字节 = 2 HEX 字符），如 32 字节 = 256 位 AES 密钥 |
| **四项质量检测** | 密钥生成后自动执行：单比特频率 / 游程 / 字节分布卡方 / 香农熵，不通过则停止生成 |
| 图表 | 交互式直方图 + 正态拟合曲线、QQ 图（R² 评估） |
| 导出 | CSV / JSON / TXT / Excel(xlsx) / 直方图 PNG / QQ 图 PNG |
| 清空 | 两页均有清空按钮，清除数据并重新加载页面 |

---

## 文件结构

- `server.py` — Web 服务（Flask，端口 8080），含 `/web-serial` 路由
- `trng_core.py` — 核心引擎：跨平台设备自动发现 / 分布变换 / 密钥 / 导出 / 熵源健康检查 / 四项质量检测
- `index.html` — Python 后端版前端界面（ECharts 交互图表）
- `web-serial-app.html` — **跨平台纯前端版**（Web Serial API，无需 Python，Win/Mac/Linux/鸿蒙通用）
- `probe_device.py` — 设备探测脚本（波特率 / 熵质量诊断）

---

## 设备接口

- 协议：USB CDC 虚拟串口，上电持续输出随机字节流，无需主机命令
- 默认波特率：115200（虚拟串口波特率不影响实际速率）
- 质量实测：熵 ≈ 7.91 bits/byte，256/256 字节全覆盖，卡方检验正常
- 跨平台端口：macOS `/dev/cu.usbmodem*`、Linux `/dev/ttyACM*`、Windows `COM*`

---

## 程序化调用（可选）

```python
from trng_core import TRNGDevice, make_numbers

# 自动发现设备（跨平台）
with TRNGDevice().open() as dev:
    vals, st = make_numbers("normal", 100, 100.0, 200.0, 2, device=dev)
    key = dev.key_hex(32)   # 256 位 HEX 密钥

# 手动指定端口（自动发现失败时）
with TRNGDevice(port="COM3").open() as dev:
    ...
```
