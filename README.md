# 🚗 UDS Python 刷写工具（CAPL 对齐版）

基于 Python 实现的 UDS 刷写脚本，目标不是“只要能刷写成功”，而是：

👉 **尽量复现 CANoe / CAPL Download() 的执行流程、报文行为与关键时序**

---

# 🎯 项目目标

本项目用于验证并复现某 ECU 刷写流程的 Python 实现，重点包括：

* 功能寻址（0x7DF）与物理寻址（0x772）分离
* 严格按 UDS 标准服务流程执行刷写
* 对齐 CAPL Download() 行为与 CANoe 报文日志
* 支持刷写全过程的日志对比与问题定位

---

# ⚙️ 当前支持的 UDS 流程

已完整覆盖以下关键服务：

```
10 01   默认会话（功能）
22 F197 / F150 / F189
10 03   扩展会话（功能）
85 02   关闭 DTC（功能）
28 03 01 通信控制（功能）

10 02   编程会话（物理）
27 11 / 27 12 安全访问

31 6000 / 6001 RoutineControl
2E F198 / F199 写入数据

34      请求下载
36      数据传输
37      传输结束

31 0202 / FF00 / 0203 / FF01

11 01   ECU Reset
14 FFFFFF 清 DTC
3E 80    KeepAlive（周期发送）
```

---

# 🚀 技术特性

* 基于 `python-can` + Vector 后端
* 自实现 ISO-TP（单帧 / 多帧）
* 支持：

  * FlowControl / ConsecutiveFrame
  * NRC 0x78（ResponsePending）
* 支持刷写期间：

  * 周期性 `3E80` 保活
* 支持：

  * DLL Seed/Key 计算（27 服务）
  * SREC 文件解析
  * ASC VerCheck 文件解析
  * CRC32 校验
* 日志包含 CAPL 对齐信息，方便抓包对比

---

# 🧠 核心设计思想

本项目**不是通用刷写工具**，而是：

👉 **CAPL Download() 行为复刻工程**

重点在于：

* 报文一致
* 顺序一致
* 时序一致
* KeepAlive 时机一致
* Pending 行为一致

---

# 🖥️ 运行环境

* Windows
* Python 3.x
* Vector 驱动（如 VN1630A）
* CANoe / Vector 通道可正常使用

---

# 📦 依赖安装

```bash
pip install python-can
```

---

# 🔧 运行前配置

修改脚本顶部参数：

```python
CHANNEL = 0
BITRATE = 500000
DATA_BITRATE = 2000000

TX_PHYS = 0x772
TX_FUNC = 0x7DF
RX_ID = 0x77A

DLL_PATH = r"..."
APP_S19_PATH = r"..."
DRIVER_SREC_PATH = r"..."
VERCHECK_ASC_PATH = r"..."
```

---

# ▶️ 运行方式

```bash
python 331_shuaxie_scripts.py
```

---

# 📂 代码结构说明

脚本主要模块：

* **KeyGen**

  * DLL 加载
  * Seed → Key 计算

* **文件处理**

  * SREC 解析
  * VerCheck 解析
  * CRC32

* **KeepAliveWorker**

  * 周期发送 `3E80`

* **UDS**

  * CAN / ISO-TP 收发
  * NRC / Pending 处理

* **Service**

  * UDS 服务封装

* **main()**

  * 串联完整 Download 流程

---

# ⚠️ 已知限制

当前版本属于“工程验证版”：

* 参数强依赖项目
* 文件路径为本地路径
* DLL 接口固定
* 未模块化 / 未封装为库
* 未支持多 ECU / 多 profile

---

# 🔮 后续优化方向

* 配置文件化（JSON / YAML）
* 抽象 Transport / UDS 层
* 拆分模块结构
* 增加 CLI 工具入口
* 支持多项目 profile
* 完整日志系统（文件输出）
* 自动化测试支持

---

# 📌 适用场景

* UDS 刷写流程分析
* CAPL 行为对齐验证
* ECU 刷写调试
* Python 替代 CANoe 实验

---

# ⚠️ 免责声明

本项目仅用于：

👉 刷写流程研究 / 调试 / 验证

请勿直接用于生产环境，避免对 ECU 造成不可逆影响。
