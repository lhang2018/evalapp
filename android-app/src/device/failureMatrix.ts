/**
 * 服务端/模型/网络/同步/进程/资源/权限 故障统一降级矩阵（ALN-12-A）。
 *
 * 单一权威：离线队列、同步引擎、后台同步、设备闸门与任务链路的降级
 * 语义都以这里为准；各模块实现与矩阵条目不一致时按矩阵整改或显式登记。
 * 条目内容与既有实现对齐（见 android-app/src/offline/* 与 device/broker.ts），
 * 本模块不引入新行为，只把分散的规则收拢为可检查的数据。
 *
 * 隐私基线（全矩阵通用，各条目 local_retention 只列增量）：
 * - 仅 standard 级事件上传；sensitive（设备动作、位置、记忆写入）本地保留。
 * - 含密码/令牌/证件号的载荷在入队前直接拒绝。
 * - 设备动作上传载荷只含引用与状态（taskProtocol 的上传投影），
 *   参数、文本目标、消息与证据内容默认不出设备。
 */

export type FailureKind =
  | "server_unreachable"
  | "model_timeout"
  | "model_rate_limited"
  | "network_switch"
  | "offline"
  | "sync_conflict"
  | "process_killed"
  | "app_restart"
  | "low_battery"
  | "low_storage"
  | "accessibility_unauthorized";

export interface FailureRetry {
  policy: "exponential_backoff" | "manual_only" | "none" | "deferred";
  /** 退避区间或触发时机说明 */
  detail?: string;
}

export interface FailureHandling {
  kind: FailureKind;
  /** 用户可见状态（界面语义，不暴露内部细节） */
  user_visible_state: string;
  /** 降级动作：故障期间功能如何继续 */
  degradation: string;
  /** 重试策略 */
  retry: FailureRetry;
  /** 停止条件：何时不再自动尝试 */
  stop_condition: string;
  /** 对账方式：恢复后如何证明本地与服务端一致 */
  reconciliation: string;
  /** 已下发未验证设备动作的处置（未知副作用永不自动重放） */
  device_action_rule: string;
  /** 本地保留字段（不进入上传载荷；叠加隐私基线） */
  local_retention: string[];
}

export const FAILURE_KINDS: FailureKind[] = [
  "server_unreachable",
  "model_timeout",
  "model_rate_limited",
  "network_switch",
  "offline",
  "sync_conflict",
  "process_killed",
  "app_restart",
  "low_battery",
  "low_storage",
  "accessibility_unauthorized",
];

export const FAILURE_MATRIX: Record<FailureKind, FailureHandling> = {
  server_unreachable: {
    kind: "server_unreachable",
    user_visible_state: "离线待同步：任务与事件已本地保留，恢复后自动上传",
    degradation:
      "任务提交与事件入本地队列（容量 500，满则拒新）；提交任务与入队事件同一次落盘",
    retry: { policy: "exponential_backoff", detail: "2s→5min 指数退避；in-flight 批次持久化" },
    stop_condition: "永久校验错误转隔离区；连续失败进死信并提供手动重试，不静默丢弃",
    reconciliation:
      "重连后按 client_request_id / event_id 幂等对账：服务端命中即返回原 task_id（202 deduplicated），批次回执按 confirmed/unconfirmed 明细清理",
    device_action_rule:
      "动作执行与回执本地照常记录；服务端确认前事件不删除；失联不当作未执行",
    local_retention: ["任务目标与本地任务记录", "事件载荷", "队列批次与回执历史（最近20条）"],
  },
  model_timeout: {
    kind: "model_timeout",
    user_visible_state: "模型响应超时：任务可重试，已产生费用仍计入账单",
    degradation:
      "任务保持当前状态可重试；不本地伪造结果；硬预算（单任务/单次调用）程序化截断",
    retry: { policy: "exponential_backoff", detail: "受硬预算与并发上限约束" },
    stop_condition: "硬预算耗尽、并发上限或用户取消即停止，不无限重试",
    reconciliation:
      "任务状态以服务端 task/run 为准；模型调用的预估与实际费用以 provider 回执为准，未知计费进预算对账（ALN-15）",
    device_action_rule:
      "等待模型产出的动作提议保持待定，不抢先执行；已下发未验证动作按原规则禁止自动重放",
    local_retention: ["提示词片段与响应摘要"],
  },
  model_rate_limited: {
    kind: "model_rate_limited",
    user_visible_state: "模型限流：稍后自动重试，或切换可用模型",
    degradation: "路由按质量优先评分切换可用模型；无可用品时任务等待",
    retry: { policy: "exponential_backoff", detail: "限流退避；预算内可换模型重试" },
    stop_condition: "限流持续且预算耗尽，任务转失败/死信并提示",
    reconciliation: "调用计量与费用证据随事件留痕，重连后对账",
    device_action_rule: "同 model_timeout：提议待定，不抢先执行",
    local_retention: ["限流响应摘要"],
  },
  network_switch: {
    kind: "network_switch",
    user_visible_state: "网络切换：同步暂停后自动恢复，无需操作",
    degradation:
      "in-flight 批次保持持久化，切换后以相同批次号重发；队列序号与检查点不变",
    retry: { policy: "exponential_backoff", detail: "重连后立即尝试，失败回到退避序列" },
    stop_condition: "无（切换本身不产生失败终态）",
    reconciliation:
      "回执协议 confirmed/unconfirmed 明确列表，部分成功不误删；seq 单调递增，服务端按序号还原因果顺序",
    device_action_rule:
      "设备动作在本机执行，不依赖网络；其事件上传随队列恢复，回执以本地记录为准",
    local_retention: ["in-flight 批次内容"],
  },
  offline: {
    kind: "offline",
    user_visible_state: "离线模式：本地任务与本地能力包可用，联网功能降级",
    degradation:
      "任务离线创建入队；已装载能力包的 workflow 可本地执行（bundleRuntime）；需服务端/模型的能力等待重连",
    retry: { policy: "exponential_backoff", detail: "同 server_unreachable" },
    stop_condition: "同 server_unreachable；本地能力执行不因断网停止",
    reconciliation:
      "重连后幂等同步 + 安装实例版本对账（平台/最低App版本/工具契约/制品完整性/灰度范围），客户端另做签名与回滚路径复核",
    device_action_rule:
      "本地能力包设备动作照常走授权与验证；服务端协作动作（审批等）等待重连，不本地代答；已下发未验证动作仍不自动重放",
    local_retention: ["离线期间全部事件", "能力包执行记录"],
  },
  sync_conflict: {
    kind: "sync_conflict",
    user_visible_state: "同步冲突：数据已隔离保存，需处理后重试",
    degradation:
      "校验失败批次整批转隔离区，保留原始证据；不静默丢弃、不部分应用",
    retry: { policy: "manual_only", detail: "隔离区提供人工重新入队或丢弃" },
    stop_condition: "校验错误属永久性，不自动重发同一载荷",
    reconciliation:
      "隔离区条目人工审查后重新入队；服务端 duplicates 计数与回执明细核对；诊断包导入按 SHA-256 幂等入隔离区",
    device_action_rule:
      "回执冲突以真实回执为准：失联或对账分歧不推断执行结果，等待证据",
    local_retention: ["隔离区原始载荷与校验错误"],
  },
  process_killed: {
    kind: "process_killed",
    user_visible_state: "应用被终止：恢复后从断点继续，需确认的会提示",
    degradation:
      "队列/批次/执行记录/台账均已持久化；重启回收 stale in-flight 批次继续发送",
    retry: { policy: "deferred", detail: "重启后自动继续；SUBMITTING 状态除外" },
    stop_condition:
      "崩溃时处于 SUBMITTING 的任务转 NEEDS_RECONCILE，需用户确认后重提，不自动重放",
    reconciliation:
      "seq 单调递增 + checkpoint 只推进连续已确认前缀；执行/回执台账从 KV 重建，版本关联不丢失",
    device_action_rule:
      "已下发但未验证的设备动作重启后仍禁止自动重放：等待真实回执或人工新提议",
    local_retention: ["崩溃前队列与台账快照"],
  },
  app_restart: {
    kind: "app_restart",
    user_visible_state: "应用重启：本地状态与版本关联已恢复",
    degradation:
      "任务库、同步回执历史、能力包运行时记录（bundle_runtime_v1）与设备动作台账从持久化重建",
    retry: { policy: "deferred", detail: "重启后按既有调度继续（后台同步≥15分钟约束不变）" },
    stop_condition: "无（正常重启不产生失败终态）",
    reconciliation:
      "重启后以安装实例 ID 与服务端对账；执行记录/探针/恢复记录保留完整版本绑定（release/version/digest）",
    device_action_rule: "同 process_killed：未验证动作不自动重放",
    local_retention: ["重启前全部本地状态（按各保留期）"],
  },
  low_battery: {
    kind: "low_battery",
    user_visible_state: "电量不足：后台同步推迟，前台操作不受限",
    degradation:
      "WorkManager 后台同步带电量不低约束，推迟至电量恢复；前台任务与设备操作照常",
    retry: { policy: "deferred", detail: "充电或电量恢复后自动运行" },
    stop_condition: "电量恢复前不再尝试后台同步",
    reconciliation: "下次同步时按幂等键对账积压；连续失败或积压超时已有告警",
    device_action_rule:
      "高风险确认与安全停止规则不因电量放宽；连续未验证仍自动安全停止；已下发未验证动作仍不自动重放",
    local_retention: ["推迟期间的积压事件"],
  },
  low_storage: {
    kind: "low_storage",
    user_visible_state: "空间不足：已按等级清理可重建数据，重要证据保留",
    degradation:
      "达 80% 预警；先清理可重建的低等级事件（normal 7 天 / debug 1 天），并写入含原因与事件清单的损失标记",
    retry: { policy: "none", detail: "清理立即生效；critical 永不自动清理" },
    stop_condition: "critical 事件（安全相关）永不自动清理",
    reconciliation:
      "损失标记界面显式提示并含事件清单；不静默删除；空间恢复后新数据正常累积",
    device_action_rule:
      "设备动作日志与回执属执行证据，优先于低等级事件保留；已下发未验证动作仍不自动重放",
    local_retention: ["损失标记", "critical/normal 级事件"],
  },
  accessibility_unauthorized: {
    kind: "accessibility_unauthorized",
    user_visible_state: "无障碍服务未授权：设备操作不可用，请到系统设置开启",
    degradation:
      "DeviceBroker 返回 unavailable 并引导系统设置授权；任务其余部分（规划/本地能力）继续",
    retry: { policy: "manual_only", detail: "用户授权后重新发起；同一 action_id 不自动重发" },
    stop_condition: "授权前不再尝试；不绕过系统权限、不使用坐标注入",
    reconciliation:
      "授权后以新提议（新 action_id）重新执行；原 unavailable 回执保留为历史，幂等重放不产生副作用",
    device_action_rule:
      "服务未启用时不产生任何下发；不存在\"失败后自动重试\"路径",
    local_retention: ["unavailable 回执（引用与状态）"],
  },
};

export function handlingFor(kind: FailureKind): FailureHandling {
  return FAILURE_MATRIX[kind];
}

/** 任务链路故障时的权威查询入口：未知类别按服务端不可达降级。 */
export function handlingForKind(kind: string): FailureHandling {
  return FAILURE_MATRIX[kind as FailureKind] ??
    FAILURE_MATRIX.server_unreachable;
}
