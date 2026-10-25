/**
 * 任务产物用户视图（ALN-24-A）：把服务端 /tasks/{id}/artifacts 的返回
 * 投影为用户可见的产物卡片——预览、下载入口、局部修改提示。
 *
 * 规则（任务卡验收）：
 * - 无文件不显示下载（服务端已保证列表只含有效产物，客户端同守卫）；
 * - 变更只重做受影响产物（rebuild 提示列出受影响项与保留项）；
 * - 截断预览如实标注（truncated=true 时提示"预览截断，下载查看全文"）。
 */

export interface ServerArtifactMeta {
  artifact_id: string;
  node_id: string;
  kind: string;
  sha256: string;
  bytes: number;
  description?: string;
  created_at?: number;
}

export interface ArtifactPreviewPayload {
  artifact_id: string;
  kind: string;
  bytes: number;
  truncated: boolean;
  text: string | null;
  base64: string | null;
}

export interface ArtifactCardView {
  artifact_id: string;
  node_id: string;
  /** 用户可见类型标签 */
  kindLabel: string;
  /** 大小（用户可读格式） */
  sizeLabel: string;
  description: string;
  digestLabel: string;
  /** 可下载（bytes>0 且有 sha256） */
  downloadable: boolean;
  /** 预览文本（如有） */
  previewText: string | null;
  /** 预览截断提示 */
  truncated: boolean;
}

export interface RebuildPlanView {
  /** 需要重做的产物（用户可见） */
  rebuild: string[];
  /** 保留不变的产物 */
  keep: string[];
  /** 用户可见摘要 */
  summary: string;
}

const KIND_LABELS: Record<string, string> = {
  markdown: "Markdown 文档",
  json: "JSON 数据",
  text: "文本文件",
  report: "报告",
  file: "文件",
  binary: "二进制文件",
};

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function toCardView(
  meta: ServerArtifactMeta,
  preview?: ArtifactPreviewPayload | null,
): ArtifactCardView {
  const downloadable = meta.bytes > 0 && Boolean(meta.sha256);
  return {
    artifact_id: meta.artifact_id,
    node_id: meta.node_id,
    kindLabel: KIND_LABELS[meta.kind] ?? meta.kind,
    sizeLabel: formatBytes(meta.bytes),
    description: meta.description ?? "",
    digestLabel: `sha256:${meta.sha256.slice(0, 12)}…`,
    downloadable,
    previewText: preview?.text ?? null,
    truncated: preview?.truncated ?? false,
  };
}

export function rebuildPlanView(
  rebuild: string[],
  keep: string[],
): RebuildPlanView {
  const parts: string[] = [];
  if (rebuild.length) {
    parts.push(`需要重新生成 ${rebuild.length} 项：${rebuild.join("、")}`);
  }
  if (keep.length) {
    parts.push(`保留不变 ${keep.length} 项`);
  }
  return {
    rebuild,
    keep,
    summary: parts.join("；") || "无变更",
  };
}

/** 截断提示：预览被截断时显示。 */
export function truncationHint(card: ArtifactCardView): string | null {
  return card.truncated
    ? "预览已截断，下载查看全文"
    : null;
}
