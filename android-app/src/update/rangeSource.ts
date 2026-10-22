/** HTTP Range 制品源（不依赖 react-native，便于 Node 测试）。 */

import type { ArtifactSource, RangeResponse } from "./artifact.ts";

function parseTotal(contentRange: string | null): number | undefined {
  if (!contentRange) return undefined;
  const match = /bytes\s+\d+-\d+\/(\d+|\*)/i.exec(contentRange);
  if (!match || match[1] === "*") return undefined;
  return Number.parseInt(match[1], 10);
}

export class FetchArtifactSource implements ArtifactSource {
  private readonly timeoutMs: number;

  constructor(timeoutMs = 60000) {
    this.timeoutMs = timeoutMs;
  }

  fetchRange(
    url: string, start: number, endInclusive: number, etag?: string,
  ): Promise<RangeResponse> {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open("GET", url);
      request.responseType = "arraybuffer";
      request.timeout = this.timeoutMs;
      request.setRequestHeader("Range", `bytes=${start}-${endInclusive}`);
      if (etag) request.setRequestHeader("If-Range", etag);
      request.onload = () => {
        if (request.status !== 200 && request.status !== 206) {
          reject(new Error(`制品下载失败: HTTP ${request.status}`));
          return;
        }
        const raw = request.response as ArrayBuffer | null;
        if (!(raw instanceof ArrayBuffer)) {
          reject(new Error("制品下载返回非字节内容"));
          return;
        }
        let bytes = new Uint8Array(raw);
        let total = parseTotal(request.getResponseHeader("content-range"));
        // 服务端忽略 Range 时返回整包；按请求偏移裁剪，保持续传语义。
        if (request.status === 200) {
          total = bytes.length;
          if (start > 0) bytes = bytes.subarray(start);
        }
        resolve({
          bytes,
          totalBytes: total,
          etag: request.getResponseHeader("etag") ?? undefined,
        });
      };
      request.onerror = () => reject(new Error("制品下载网络错误"));
      request.ontimeout = () => reject(new Error("制品下载超时"));
      request.send();
    });
  }
}
