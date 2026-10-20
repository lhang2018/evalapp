/** 界面观测与执行校验（PRD 68）：快照、页面变化、权限弹窗、结果证据。 */

export interface DeviceNode {
  text?: string;
  desc?: string;
  role?: string;
  clickable?: boolean;
  enabled?: boolean;
  visible?: boolean;
  bounds?: number[];
  children?: DeviceNode[];
}

export interface DeviceSnapshot {
  package_name: string | null;
  nodes: DeviceNode[];
  node_count: number;
  truncated: boolean;
  captured_at?: number;
}

export interface SnapshotDiff {
  changed: boolean;
  package_changed: boolean;
  package_before: string | null;
  package_after: string | null;
  added: string[];
  removed: string[];
}

export interface PermissionDialogInfo {
  detected: boolean;
  package_name: string | null;
  buttons: string[];
}

export interface VerificationExpectation {
  expect_text?: string;
  expect_package?: string;
}

export interface ActionVerification {
  status: "verified" | "unverified";
  message: string;
  evidence: Record<string, unknown>;
}

const MAX_NODES = 400;
const MAX_DEPTH = 24;
const MAX_DIFF_ITEMS = 20;

const PERMISSION_PACKAGES = ["permissioncontroller", "packageinstaller"];
const PERMISSION_WORDS = [
  "权限",
  "授权",
  "访问",
  "permission",
  "access",
];
const ALLOW_WORDS = [
  "允许",
  "始终允许",
  "仅在使用时允许",
  "仅在使用中允许",
  "allow",
  "while using the app",
  "only this time",
];
const DENY_WORDS = ["拒绝", "不允许", "deny", "don't allow", "dont allow"];

const PACKAGE_RE = /^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$/;

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function asBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function asNumberArray(value: unknown): number[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const numbers = value.filter(
    (item): item is number => typeof item === "number" && Number.isFinite(item),
  );
  return numbers.length === value.length ? numbers : undefined;
}

function normalizeNode(raw: unknown, depth: number): DeviceNode | null {
  if (!raw || typeof raw !== "object" || depth > MAX_DEPTH) return null;
  const source = raw as Record<string, unknown>;
  const node: DeviceNode = {};
  const text = asString(source.text);
  const desc = asString(source.desc);
  const role = asString(source.role);
  if (text) node.text = text;
  if (desc) node.desc = desc;
  if (role) node.role = role;
  const clickable = asBoolean(source.clickable);
  const enabled = asBoolean(source.enabled);
  const visible = asBoolean(source.visible);
  if (clickable !== undefined) node.clickable = clickable;
  if (enabled !== undefined) node.enabled = enabled;
  if (visible !== undefined) node.visible = visible;
  const bounds = asNumberArray(source.bounds);
  if (bounds) node.bounds = bounds;
  if (Array.isArray(source.children)) {
    const children: DeviceNode[] = [];
    for (const child of source.children) {
      const normalized = normalizeNode(child, depth + 1);
      if (normalized) children.push(normalized);
    }
    if (children.length) node.children = children;
  }
  return node;
}

export function normalizeSnapshot(raw: unknown): DeviceSnapshot | null {
  if (!raw || typeof raw !== "object") return null;
  const source = raw as Record<string, unknown>;
  const nodes: DeviceNode[] = [];
  if (Array.isArray(source.nodes)) {
    for (const item of source.nodes.slice(0, MAX_NODES)) {
      const node = normalizeNode(item, 0);
      if (node) nodes.push(node);
    }
  }
  const packageName = asString(source.package_name) ?? null;
  if (!packageName && nodes.length === 0) return null;
  const declared = source.node_count;
  return {
    package_name: packageName,
    nodes,
    node_count:
      typeof declared === "number" && declared >= 0
        ? declared
        : countNodes(nodes),
    truncated: source.truncated === true,
    captured_at:
      typeof source.captured_at === "number" ? source.captured_at : undefined,
  };
}

export function countNodes(nodes: DeviceNode[]): number {
  let total = 0;
  for (const node of nodes) {
    total += 1 + countNodes(node.children ?? []);
  }
  return total;
}

export function snapshotTexts(snapshot: DeviceSnapshot): string[] {
  const out: string[] = [];
  const walk = (nodes: DeviceNode[]) => {
    for (const node of nodes) {
      if (node.text) out.push(node.text);
      if (node.desc) out.push(node.desc);
      if (node.children) walk(node.children);
    }
  };
  walk(snapshot.nodes);
  return out;
}

function countItems(items: string[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const item of items) {
    counts.set(item, (counts.get(item) ?? 0) + 1);
  }
  return counts;
}

function difference(a: string[], b: string[]): string[] {
  const counts = countItems(b);
  const out: string[] = [];
  for (const item of a) {
    const remaining = counts.get(item) ?? 0;
    if (remaining > 0) {
      counts.set(item, remaining - 1);
    } else if (!out.includes(item)) {
      out.push(item);
    }
  }
  return out;
}

export function diffSnapshots(
  before: DeviceSnapshot,
  after: DeviceSnapshot,
): SnapshotDiff {
  const beforeTexts = snapshotTexts(before);
  const afterTexts = snapshotTexts(after);
  const added = difference(afterTexts, beforeTexts).slice(0, MAX_DIFF_ITEMS);
  const removed = difference(beforeTexts, afterTexts).slice(0, MAX_DIFF_ITEMS);
  const packageChanged =
    (before.package_name ?? "") !== (after.package_name ?? "");
  return {
    changed: packageChanged || added.length > 0 || removed.length > 0,
    package_changed: packageChanged,
    package_before: before.package_name,
    package_after: after.package_name,
    added,
    removed,
  };
}

function isPermissionPackage(packageName: string): boolean {
  const lower = packageName.toLowerCase();
  return PERMISSION_PACKAGES.some((prefix) => lower.includes(prefix));
}

function matchesAny(text: string, words: string[]): boolean {
  const lower = text.toLowerCase();
  return words.some((word) => lower.includes(word));
}

function matchesExactLabel(text: string, words: string[]): boolean {
  const lower = text.trim().toLowerCase();
  return words.some((word) => lower === word);
}

export function detectPermissionDialog(
  snapshot: DeviceSnapshot,
): PermissionDialogInfo {
  const texts = snapshotTexts(snapshot);
  const allow = texts.filter((text) => matchesExactLabel(text, ALLOW_WORDS));
  const deny = texts.filter((text) => matchesExactLabel(text, DENY_WORDS));
  const permission = texts.some((text) => matchesAny(text, PERMISSION_WORDS));
  const packageName = snapshot.package_name;
  const knownPackage = packageName ? isPermissionPackage(packageName) : false;
  const detected =
    knownPackage || (allow.length > 0 && deny.length > 0 && permission);
  const buttons: string[] = [];
  if (detected) {
    for (const label of [...allow, ...deny]) {
      if (!buttons.includes(label) && buttons.length < 4) buttons.push(label);
    }
  }
  return { detected, package_name: packageName, buttons };
}

export function isPackageName(target: string): boolean {
  return PACKAGE_RE.test(target);
}

export function verifyAction(
  before: DeviceSnapshot | null,
  after: DeviceSnapshot | null,
  expectation: VerificationExpectation = {},
): ActionVerification {
  const base: Record<string, unknown> = {};
  if (!before || !after) {
    return {
      status: "unverified",
      message: "缺少操作前后界面快照，无法验证执行结果",
      evidence: base,
    };
  }

  const dialog = detectPermissionDialog(after);
  const diff = diffSnapshots(before, after);
  const beforeTexts = new Set(snapshotTexts(before));
  const afterTexts = new Set(snapshotTexts(after));
  Object.assign(base, {
    package_before: diff.package_before,
    package_after: diff.package_after,
    changed: diff.changed,
    added: diff.added,
    removed: diff.removed,
    node_count_before: before.node_count,
    node_count_after: after.node_count,
    permission_dialog: dialog,
  });

  if (dialog.detected) {
    return {
      status: "unverified",
      message: "检测到权限弹窗，操作结果未确认，需用户处理",
      evidence: base,
    };
  }

  if (expectation.expect_text) {
    const expected = expectation.expect_text;
    if (afterTexts.has(expected) && !beforeTexts.has(expected)) {
      return {
        status: "verified",
        message: `已确认界面出现「${expected}」`,
        evidence: base,
      };
    }
    if (!afterTexts.has(expected)) {
      return {
        status: "unverified",
        message: `未观察到预期文本「${expected}」`,
        evidence: base,
      };
    }
  }

  if (expectation.expect_package) {
    if (after.package_name === expectation.expect_package) {
      return {
        status: "verified",
        message: `已确认前台为 ${expectation.expect_package}`,
        evidence: base,
      };
    }
    return {
      status: "unverified",
      message: `前台应用不是 ${expectation.expect_package}`,
      evidence: base,
    };
  }

  if (diff.changed) {
    return {
      status: "verified",
      message: diff.package_changed
        ? "已确认前台界面切换"
        : "已确认界面发生变化",
      evidence: base,
    };
  }

  return {
    status: "unverified",
    message: "界面未发生可观察变化（操作可能未生效）",
    evidence: base,
  };
}
