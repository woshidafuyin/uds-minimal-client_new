# 🚗 UDS Python 刷写工具（CAPL 对齐版）

> 🔬 基于 CANoe CAPL Download() 行为反向实现

这是一个使用 Python 实现的 UDS 刷写工具，目标不是“能刷写”，而是：

👉 **严格复现 CAPL Download() 的执行逻辑和 CANoe 日志行为**

---

## ✨ 功能特性

- ✅ 完整 UDS 刷写流程（Download）
- ✅ 严格对齐 CAPL 行为（不是普通 demo）
- ✅ 功能寻址 / 物理寻址分离
- ✅ 支持 NRC 0x78（响应挂起）
- ✅ 支持 ISO-TP 多帧
- ✅ 支持 DLL 安全访问（27服务）
- ✅ 支持 SREC / ASC 文件解析
- ✅ 支持 CRC / 校验计算

---

## 🧠 设计目标

> 🎯 目标不是“刷写成功”，而是：

👉 **和 CAPL Download() 一模一样**

包括：

- 请求顺序一致
- 时间行为一致
- 报文格式一致
- 错误处理一致（尤其是 0x78）

---

## 🏗️ 整体架构

```mermaid
flowchart TD

    A[脚本入口<br/>pre_download_10_to_27.py]
    B[刷写流程控制<br/>run_download_flow]
    C[UDS层<br/>udsoncan + 原始UDS]
    D[ISO-TP层<br/>isotp]
    E[CAN层<br/>python-can]
    F[硬件<br/>Vector设备]

    A --> B
    B --> C
    C --> D
    D --> E
    E --> F

    B --> G[DLL解密<br/>27服务]
    B --> H[文件解析<br/>SREC/ASC]
