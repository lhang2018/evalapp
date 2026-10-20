/** 客户端 ID 生成：URL 安全、满足服务端 event_id 校验（16-64 位）。 */

let counter = 0;

function randomChunk(length: number): string {
  let out = "";
  while (out.length < length) {
    out += Math.floor(Math.random() * 36 ** 4)
      .toString(36)
      .padStart(4, "0");
  }
  return out.slice(0, length);
}

export function createId(prefix: string): string {
  counter = (counter + 1) % 1296;
  return `${prefix}_${Date.now().toString(36)}${counter
    .toString(36)
    .padStart(2, "0")}${randomChunk(10)}`;
}

export function createEventId(): string {
  return createId("evt");
}

export function createLocalTaskId(): string {
  return createId("local");
}

export function createClientRequestId(): string {
  return createId("req");
}
