# ALN-12-E 自然语言任务提议与回执投影

原实现07edc6ba5ce4f035d0a3f3bdba8b67245c8e485f；审查修复a1b3a3183b057b4e72a50677a703555a05ba4acf。此文补齐台账原先引用但缺失的交付文件，代码状态review，新增修复待独立复核。

## 当前实现

- classifyPipeline复用聊天/工作分类，仅精确的单一“打开设置”请求且有任务身份时产生launch_intent，目标为com.android.settings，附期望包名。
- 其他工作/复合请求与缺身份返回null提议和planningReason，交给任务规划或用户澄清；不生成空目标或随意点击。
- resultView展示设备动作回执；传入任务时额外展示权威taskLabel，单动作成功不合成整个任务成功；任务/运行错配拒绝，未知或矛盾回执禁止自动重放。
- uploadPayload复用receiptForUpload最小投影，不包含文本参数、消息或证据正文。

## 验证与边界

新旧相关客户端20项通过；固定a1b3a31客户端353项与类型检查通过。包含真实NativeDeviceTransport适配器调度到替身NativeDeviceModule的探针，验证launchIntent目标，不使用真实Android设备。

本模块仍为提议准备与结果投影函数，未挂载App.tsx，未串接生产broker独立授权、执行台账、完整任务生命周期或产物生成。原“完整链路”测试仅组合逻辑投影，不能当生产主链/真机证据。整包ALN-12不关闭。

详见[第三批审查](../验收/2026-10-25-第三批交叉审查与真实性修复.md)与.planning/2026-10-25-developed-review-3/committed-evidence.json。

## 2026-10-26 接续复核

设备执行闸门的 `077d293` 修复确认回调改写动作、非布尔真值误授权/误报原生成功、异步等待跨越安全停止三类问题。固定提交客户端384/384、TypeScript退出码0；07-C相关后端112项通过。见[主链接续复核](../验收/2026-10-26-07C与12E主链接续复核.md)。本轮仍未挂载App或使用真机，新增修复待其他审阅人复核，整卡保持review。
