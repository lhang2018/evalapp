# ALN-23-F 交付：语音输入与文字任务共用入口

任务：ALN-23-F（PRODUCT-2026-10-25，父包 ALN-23，阶段3）
负责人：zcode-main；审阅人：待定（须独立复核）
基线 commit：`957369be958691748430db8b6a1c4c5b9c7f5d9e`（接手锚点，非已发布基线）
分支与最终 commit：feature/user-direction（源码见工作区；提交待用户授权后落库）
认领状态：delivered（review，待独立复核）

## 触发场景与行为变化

- 输入/触发：用户点击"语音"，系统 SpeechRecognizer 转写为文本。
- 修改前：转写文本直接 `setGoal(result.transcript)`，无来源标记、无修正追踪、
  不声明离线范围；语音与文字的关系只是"恰好填进同一个输入框"。
- 修改后：新增 `features/voice/entry.ts` 把一次识别转成**可编辑草稿**，仅当
  `transcribed` 且确认后非空才产出提交目标；转写完成只填输入框、不建任务、
  不提交；用户编辑即记录 `voice_edited` 并保留原始 `transcript`；范围声明
  （识别/播报/麦克风/离线）随草稿与设置页展示。

## 范围

- 独占模块：`android-app/src/features/voice/entry.ts`（纯 TS，新增）。
- 测试：`android-app/test/device/voiceEntry.test.ts`（11 项）。
- 共享文件最小接线：`android-app/App.tsx`（导入桥接、`voiceEntry` 状态、
  `updateGoal` 编辑钩子、语音范围提示行、设置页范围声明）。App.tsx 为
  workboard.shared_files 已登记项，改动为最小接线，不改执行内核与提交语义。
- 复用既有：`src/device/voice.ts`（识别/TTS/降级六态）、`VoiceModule.kt`
  （系统 SpeechRecognizer/TTS）。不重新实现已交付切片。
- 不改契约与迁移：不新增表、不改 `TaskContractInput`；语音经既有 `goal` →
  `services.tasks.submit(...)` 路径进入同一任务契约与动作授权。

## 验收对齐

1. **转写完成 ≠ 执行成功**：`applyTranscription` 只产出草稿；`buildVoiceTaskInput`
   只返回 `goal` 文本，绝无副作用；App 中语音仍走与文字相同的 `submit`。
   测试断言桥接输出仅含 `ok/goal/reason`，不含 intent/risk/权限字段。
2. **转写错误可修正**：`editVoiceEntry` 保留 `transcript`、提交只取 `text`；
   清空文本即不可提交，不退回原始转写；改回原文恢复 `voice` 来源。
3. **高风险外部动作仍走动作授权**：入口不产出/覆盖风险等级，不新增第二条
   提交通道；语音与文字共用 `submit`，高风险动作照常由统一授权判定。
4. **声明识别/播报及离线范围**：`describeVoiceScope` 由原生能力投影
   recognition/speech/microphone/offline，缺依赖时明确显示不可用，识别可用时
   如实标注"离线依赖系统识别引擎"（不夸大断网可用）。

## 验证证据

- 正常路径：`test/device/voiceEntry.test.ts` 11 项全过——转写→草稿→提交目标、
  非识别成功状态不可提交、空文本按空结果处理、修正保留原文、清空不回退、
  范围声明可用/缺依赖/未授权三态、重复录音整体替换。
- 拒绝/失败路径：denied/unavailable/cancelled/failed/empty 均不可提交且不保留
  文本；转写称成功但文本为空归一为 empty。
- 命令与结果：
  - `node --test test/device/voiceEntry.test.ts` → 11 passed / 0 failed。
  - `node --test "test/offline/*.test.ts" "test/device/*.test.ts" "test/update/*.test.ts"`
    → **302 passed / 0 failed**（含既有回归与并行在途新增）。
  - `npx tsc --noEmit` → 0 错误。
  - 日志：`.planning/2026-10-25-aln23f/client-tests.txt`、`typecheck.txt`。
- 验证边界：以上均为本地组件与夹具；**真机麦克风/权限/断网语音路径未采证**，
  归属 ALN-12-V 真机矩阵另验。

## 回滚与未完成事项

- 回滚：删除新增模块与测试、还原 App.tsx 语音接线即可；不影响其他卡。
- 保留限制：真机语音输入与离线降级实测未做；App 语音草稿的来源标记为
  会话内状态，未持久化到 LocalTask（`transcript` 仅为本地修正追溯）。
- 影响主题：UX-02；父包 ALN-23 关闭另按整包依赖（23-C/24-A/12-E）推进。
- 未提交：源码未落库，`delivered_commit` 待用户授权后填写。

审阅结论：待审
整包结论：仍实现中
