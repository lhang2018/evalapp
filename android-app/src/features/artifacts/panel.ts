/**
 * 任务成果面板（ALN-30-B）：把服务端 /tasks/{id}/artifacts 的元数据与
 * 预览投影成用户可见卡片，并给出"可打开/仅预览/不可用"的诚实提示。
 *
 * 规则（任务卡验收）：
 * - 无真实文件（bytes<=0 或 sha256 缺失）不显示下载/打开入口；
 * - 预览被截断如实标注，二进制只展示 base64 摘要提示，不冒充文本；
 * - 空结果或失败必须显示为"无成果/加载失败"，不得显示成已交付。
 */

import type {
  TaskArtifactMeta,
  TaskArtifactPreview,
} from "../../api.ts";
import {
  formatBytes,
  toCardView,
  type ArtifactCardView,
} from "./view.ts";

export interface ArtifactRow extends ArtifactCardView {
  trace: string;
  openable: boolean;
  previewNote: string;
}

export function buildArtifactRows(
  metas: TaskArtifactMeta[],
  previews: Record<string, TaskArtifactPreview | null | undefined> = {},
): ArtifactRow[] {
  return metas.map((meta) => {
    const card = toCardView(meta, previews[meta.artifact_id] ?? null);
    const preview = previews[meta.artifact_id] ?? null;
    const openable = card.downloadable;
    return {
      ...card,
      trace: `节点 ${meta.node_id || "-"} · 产物 ${meta.artifact_id}`,
      openable,
      previewNote: previewNotice(preview),
    };
  });
}

export function previewNotice(
  preview: TaskArtifactPreview | null | undefined,
): string {
  if (!preview) return "未打开预览";
  if (preview.text !== null) {
    return preview.truncated
      ? "预览已截断（服务端仅提供前 4KB）；完整内容需后续下载能力"
      : "已打开文本预览";
  }
  if (preview.base64) {
    return "二进制预览（base64 前 4KB），不当作文本展示";
  }
  return "无可显示内容";
}

export function panelHint(state: {
  offline: boolean;
  loading: boolean;
  error: string | null;
  count: number;
}): string | null {
  if (state.offline) return "离线：任务成果需要连接服务端查看";
  if (state.loading) return "正在加载任务成果…";
  if (state.error) return "任务成果加载失败：" + state.error;
  if (state.count === 0) return "该任务暂无可追溯成果（空结果不视为已交付）";
  return null;
}

export function emptyTaskIdHint(taskId: string | null | undefined): string | null {
  return taskId ? null : "本地任务尚未关联服务端任务，无成果可查";
}
