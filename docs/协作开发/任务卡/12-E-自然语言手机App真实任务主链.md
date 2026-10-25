# ALN-12-E｜自然语言手机App真实任务主链

来源批次：PRODUCT-2026-10-25；阶段2；父包ALN-12。
需求：F02 / F03 / G0 / G1；来源：[分期需求清单](../需求拆分与分期实施_2026-10-25.json)。

## 目标

从自然语言到真实设备动作、授权、回执、成果的完整链路

复用已有实现，先读当前接口与交付记录；不重新实现已接受切片。

## 依赖与开工条件

- 卡片依赖：ALN-23-C, ALN-07-C, ALN-06-R
- 环境：可连接且授权的Android设备
- 包依赖约束整包关闭；待审接口可用于独立实现，但不能替代真实集成验收。

## 写入边界

- android-app/src/
- android-app/android/
- android-app/App.tsx
- evoagent/api/

共享文件按workboard.shared_files登记；多人认领时先声明精确修改点，同文件串行合入。新增迁移需集成排号。

## 验收

明确授权后真机执行；task/run/effect一致；夹具transport不能算真机动作

- 记录正向、失败与恢复/拒绝路径；Mock/夹具明确标注，不能冒充真机或生产实测。
- 绑定精确源码commit、命令、结果、日志及必要环境；无证据保持未测试。

## 交付

- 在交付目录提供本卡报告、兼容/迁移影响与保留缺口，填写台账evidence和delivered_commit。
- 开发完成转review；审阅人未定保留null，不自批为独立验收；发布决定另行处理。
- owner/branch/base_commit以workboard为准；未认领卡不预填他人身份。
