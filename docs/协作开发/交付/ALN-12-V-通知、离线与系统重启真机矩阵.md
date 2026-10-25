# ALN-12-V 通知、离线与系统重启真机矩阵

任务：ALN-12-V / 通知、离线与系统重启真机矩阵
负责人：dsh-lead（本会话执行；原 zcode-next 认领后因设备处于私人使用状态标 blocked，用户明确解锁并授权本任务继续）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`a40ef427d82e92f22f0cfeadcdb6741a1091fb7c`（执行时 `feature/user-direction` HEAD，不是已发布共同基线）
交付物 commit：见台账 `delivered_commit`（本卡为**真机验证卡，不改产品源码**，交付物为证据与报告）
认领状态：blocked → **review**

## 触发场景与行为变化

- 输入/触发：真机上「自然语言任务在线执行 / 断网排队与恢复回放 / 系统重启后状态恢复 / 任务终态通知」四类场景。
- 修改前（证据状态）：
  - 12-A 已交付任务—设备执行/回执协议（accepted），12-A 的未完成项与后续卡未在**真机**上验证通知、离线与重启恢复；此前记录中的设备证据来自 2026-10-24 基线仓（commit `f550d90`），与当前工作树不一致。
- 修改后（本卡不改变产品行为，只产出可核验的真机矩阵结果）：
  - 记录了精确的 APK/JS/设备版本绑定；
  - 正向路径：自然语言任务在线提交→真实执行→后端 SUCCEEDED；
  - 离线路径：断网后任务保存本机（排队 1/500）→ 后端无新任务 → 恢复连接后回放成功（幂等键）；
  - 重启路径：`adb reboot` 后解锁重开 App，本地任务 14/14 保留、状态无变化、队列保留、后端任务完整；
  - 通知路径：**未观测到投递**（权限 allow、后台 headless 同步成功但 dumpsys notification 为 0），按未验证/疑似缺陷记录，见「验收矩阵」。

## 验收矩阵（真机实测，非夹具）

| 场景 | 结果 | 证据 |
|---|---|---|
| 精确版本绑定 | APK sha256 `7b72b799…fdeda2`、JS bundle sha256 `a0eb06f5…57b4e9f`（Metro 工作树，构建时 HEAD `61451f3`）、设备 24018RPACC/Android 16、无障碍服务已启用、通知权限 allow | `device-matrix-evidence.json` |
| 在线任务链 | 输入 `calculate 1+1` → App 结果 `计算结果：2` → 后端任务 `3e1cf341048b` SUCCEEDED | `ui-online2.xml`（已归档哈希）、`online-success.png` |
| 离线排队 | 飞行模式+移除 adb reverse → App 显示「离线 · 任务保存在本机」→ 提交后 `待上传 1`，提示「已离线保存；联网后自动以幂等键提交」→ 后端无新任务 | `ui-offline.xml`/`ui-offline-submitted.xml`、`offline-submitted.png`、持久化键 `offline_state_v1`（14 任务，digest `2947efb5…`） |
| 重连回放 | 恢复 reverse → 立即同步 → `待上传 0`，checkpoint 22→25，任务结果 `计算结果：11`，后端 `a3ee7b879193` SUCCEEDED | `ui-after-sync.xml`、`after-sync.png` |
| 系统重启恢复 | `adb reboot` → 解锁 → 重开 App（Metro 重新加载）→ 14/14 本地任务保留、状态无变化、队列保留、后端 13 个任务完整（含 `b4c98a2c95a0`、`3ee9cd585c67`） | `prereboot-state.json`、`reboot-compare.json`、`RKStorage-{prereboot,postboot}.db`、`postboot.png` |
| 通知投递 | **未观测到**：权限 allow；离线排队→后台 WorkManager SyncWorker（headless JS）→恢复连接后后端新增任务，说明 headless 同步执行成功，但 `dumpsys notification` 中 com.evoagent 计数为 0、logcat 无 `[background-sync]` 行 | 见证据 JSON `notification_probe`（明确标注 not_observed） |

## 范围

- 独占模块：无（**未改任何产品源码**；`git status` 中本卡只新增 `.planning/2026-10-25-aln12v/` 证据与交付报告）。
- 共享文件补丁：无（12 个 shared_files 全部未动）。
- 新增/修改契约：无代码契约变更；本卡输出「真机矩阵结果 + 精确版本绑定 + 通知缺口」。
- 环境准备（可复用）：后端 8300（他人在跑，auth open，mock provider）、Metro 8081 提供工作树 JS、`adb reverse 8081/8300`、`debug_http_host=127.0.0.1:8081`（仅为让 debug APK 连上 Metro，验证后已还原原始 prefs）、无障碍服务经设备授权启用。

## 验证证据

- 命令与结果：
  - 在线：`adb shell input text "calculate 1+1"` + 提交 → 后端 `/tasks` 出现 `3e1cf341048b`/SUCCEEDED；
  - 离线：`cmd connectivity airplane-mode enable` + `adb reverse --remove tcp:8300` → 提交 `calculate 5+6` → `待上传 1`；
  - 回放：`adb reverse tcp:8300 tcp:8300` + 点「立即同步」→ `待上传 0`、checkpoint +3、后端 `a3ee7b879193` SUCCEEDED；
  - 重启：`adb reboot` → 解锁（用户提供的 PIN）→ 重开 App → RKStorage `offline_state_v1` 前后对比；
  - 通知：离线排队 + 立即同步（enqueue WorkManager）→ 后台化 → 恢复 reverse → `cmd jobscheduler run -f com.evoagent 58` → 30s 后 `dumpsys notification` 计数 0。
- 结果与日志文件：`.planning/2026-10-25-aln12v/device-matrix-evidence.json`（含 29 个证据文件 sha256）、`reboot-compare.json`、`prereboot-state.json`、UI dump XML、截图、RKStorage 前后快照。
- Mock / 本地组件 / 真机 / 真实供应商边界：
  - 本轮为**真实设备**（非模拟器）上的真实 APK + 真实后端；任务执行结果由 mock provider 产生（后端 provider=mock），但设备端渲染、离线存储、WorkManager/headless 同步、系统重启均为真机行为；
  - JS 由 **Metro 开发服务器**提供（debug APK），**不是发布包**；发布包/OTA 渠道未验；
  - 通知子项为真实设备上未观测到投递，未用任何方式补造；`adb reboot` 不等于真实断电；
  - 设备为用户私人手机：本次未清除任何用户数据、未重置系统；结束后已还原 `debug_http_host` 原始 prefs、停止 Metro、恢复飞行模式为关闭、屏幕关闭；设备当前解锁状态由用户自行处理。

## 回滚与未完成事项

- 回滚触发、操作及验证：无产品代码变更，无需回滚；如需清除证据：删除 `.planning/2026-10-25-aln12v/` 与交付文档。
- 保留的限制、缺口与后续：
  - **通知投递未验证（疑似缺陷）**：后台 headless 同步成功但系统通知未出现；需要 12-A/通知实现方（ALN-12 系）排查 `TaskNotifier.notifyTransitions` 在 headless 上下文的可观测性，或在发布包上复验；
  - debug APK + Metro 工作树 JS ≠ 发布包：发布包、OTA 更新后的 JS 行为未验；
  - MIUI 省电/Doze 对后台同步的影响未系统测；`adb reboot` ≠ 物理断电；
  - 设备为私人手机，后续复验需用户在场解锁；
  - 本轮未覆盖 TTS/语音、无障碍设备动作（属 12-E/12-V 其他子项）。
- 影响的主题/实体/ADR/契约/系统验收条目：无契约变更；为 12-V 的「通知、离线、重启」矩阵提供真机证据与一个明确缺口。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-12：通知投递缺口待排查；发布包与 12-E 主链仍未验；本卡交付真机矩阵证据）
