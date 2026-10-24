# ALN-00-B 新候选与最新版本真机回归（2026-10-24）

新候选为 **f550d9063f7773f5b18893decde668fbc001f7ae**，tree为 **6b9bd53adb72c95676fc14d657c438a19687e550**，包含07466fb设备协议/执行/能力修复及f550d90证据校验修复。候选准备与约定设备模块回归完成，00-B从blocked转review；**baseline仍candidate，published_commit为空**。用户已同意推进候选和真机工作；原规则中基线发布的独立审阅条件仍单列，准备人没有把自己的验证冒充第三方接受。

## 固定源码与依赖验证

- 在当前项目outputs中用精确Git归档验证，未新建工作树；源文件不取自脏工作区。所有102个已跟踪Android文件与该Git对象逐字节一致，测试入口单列，不改产品index.js或业务模块。
- 后端 **1661 passed、1 skipped**（424.00s）；线程异常警告升级错误。客户端 **228 passed、0 failed**；声明/台账/认领/缺口/89主题覆盖五检查通过。[全量记录](../../../.planning/2026-10-24-baseline-device/candidate-validation.json)。
- 精确候选中执行npm ci，按已提交package-lock安装599包，没有复用工作区node_modules；TypeScript **5.9.3**通过，符合package.json的^5.5.0。Python使用本机已装requirements.txt环境，JDK17/SDK/Gradle缓存复用，不称空机部署验收。
- Android ARM64 debug APK构建成功，71任务；覆盖安装成功、保留原数据。APK SHA256为`7b72b7997e842ba806841352dfc2e1494617a73f4dacbeb7d30ee85599fdeda2`，与设备已安装base.apk摘要一致。debug APK不内嵌JS，最新JS来自独立Metro8084的精确候选入口；原工作区Metro8081未用于本次验证。[构建记录](../../../.planning/2026-10-24-baseline-device/build-evidence.json)、[源码/安装/JS绑定](../../../.planning/2026-10-24-baseline-device/device-attestation.json)。

## 真机实测

设备5748d823 / 小米24018RPACC / Android16；实际引擎Hermes。本轮重新安装新APK并运行新JS，非沿用10-24较早截图充当修复后证据。

| 实测项 | 结果 |
|---|---|
| 原生模块与权限边界 | Device/Update原生模块存在；无障碍未开启，真实broker返回unavailable，没有绕过系统授权 |
| 同动作并发 | 同一AsyncStorage/key的两个真实台账实例，两个broker并发调用，只进入一次transport；最终verified回执落盘 |
| 身份替换与坏历史 | 改任务身份和损坏持久记录均拒绝，transport调用0次 |
| 健康包安装 | HTTP下载、RSA公钥验签、原生文件存储、真实装载与探针执行通过，晋级known-good |
| 坏包健康失败 | 真实执行但期望不符→unhealthy→UpdateManager回滚→从原生旧制品重新装载并执行succeeded，版本/digest与失败包成对留痕 |
| 工具硬超时 | 150ms预算，169ms返回timeout；不重试、不执行下游，迟到完成亦不继续；健康探针unknown，不伪标healthy |
| 过时探针 | 不属于当前制品的健康结果拒绝晋级 |
| 实际强杀重启 | 主机确认进程存在后force-stop，确认PID为空，再启动；已完成动作和已落提议但无回执动作均不重放，未知动作保持unverified |
| 重启后旧包能力 | 回滚关联、活动制品与旧包执行结果真实从AsyncStorage恢复；旧能力执行succeeded |

**9/9通过**。[强杀前](../../../.planning/2026-10-24-baseline-device/device-before-restart.json)、[强杀后](../../../.planning/2026-10-24-baseline-device/device-after-restart.json)、[PID证据](../../../.planning/2026-10-24-baseline-device/force-stop-evidence.json)、[设备截图](../../../.planning/2026-10-24-baseline-device/harness-after-restart.png)。原始设备DB只存outputs私有备份，不提交；提交记录只投影本轮隔离测试命名空间。

测试入口deviceReview.tsx直接导入候选真实模块，使用真实AsyncStorage、NativeArtifactFiles、HTTP下载和RSA验签。动作transport仅用显式计数夹具，更新reconcile/applyResult服务为显式夹具；不是自然语言App主链、真实无障碍点击、生产信任根或生产服务联调，也不是物理重启。测试结束已恢复正式index.js入口并核对任务中心正常显示；8334测试反向端口已移除，原8300服务及历史任务数据未清空。

首轮测试入口误把固定action_id传给会自行生成ID的createProposal，实际变成两个不同动作，导致计数探针失败。保留首轮截图/XML；修正为构造后固定提议身份后，在独立v2命名空间重新执行9项全通过，没有据此修改产品。Metro首轮配置的绝对目录require也已改为按App上下文解析exports；这两项均为测试配置问题。

## 交接与发布状态

旧07f11e5候选及证据完整保存在prepared_candidate_history，不覆盖历史测试结果。新候选已解除“不含07466fb”和“新修复未上设备”的技术问题；无障碍端到端与自然语言主链仍是父包后续范围，不能据9项模块测试宣布整包完成。

按[共享规则](../协作规则与共享文件.md)当前文字“此例外不扩展到基线发布”，正式写入published_commit前需要独立接受，或者用户明确授权本次以非独立复验发布**共享开发基线**。本报告将完整候选、测试和设备材料先交付，未替用户选择例外，未生产上线、推送远端或清理其他会话修改。

接手者使用f550d90的精确源码；后续优先补ALN-07副作用完成到绑定落盘的崩溃窗口、精确授权与对账恢复，再推进ALN-12自然语言手机任务主链。
