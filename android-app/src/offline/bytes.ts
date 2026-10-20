/** 跨 RN/Node 的字节工具：不依赖 TextEncoder/atob 是否可用。 */

export function utf8Encode(text: string): Uint8Array {
  const out: number[] = [];
  for (const char of text) {
    let cp = char.codePointAt(0) ?? 0xfffd;
    if (cp >= 0xd800 && cp <= 0xdfff) cp = 0xfffd;
    if (cp < 0x80) {
      out.push(cp);
    } else if (cp < 0x800) {
      out.push(0xc0 | (cp >> 6), 0x80 | (cp & 0x3f));
    } else if (cp < 0x10000) {
      out.push(
        0xe0 | (cp >> 12),
        0x80 | ((cp >> 6) & 0x3f),
        0x80 | (cp & 0x3f),
      );
    } else {
      out.push(
        0xf0 | (cp >> 18),
        0x80 | ((cp >> 12) & 0x3f),
        0x80 | ((cp >> 6) & 0x3f),
        0x80 | (cp & 0x3f),
      );
    }
  }
  return new Uint8Array(out);
}

export function utf8Decode(bytes: Uint8Array): string {
  let out = "";
  let index = 0;
  while (index < bytes.length) {
    const first = bytes[index];
    index += 1;
    let cp = 0xfffd;
    if (first < 0x80) {
      cp = first;
    } else if (first >= 0xc2 && first < 0xe0 && index < bytes.length) {
      cp = ((first & 0x1f) << 6) | (bytes[index] & 0x3f);
      index += 1;
    } else if (first >= 0xe0 && first < 0xf0 && index + 1 < bytes.length) {
      cp = ((first & 0x0f) << 12) |
        ((bytes[index] & 0x3f) << 6) |
        (bytes[index + 1] & 0x3f);
      index += 2;
    } else if (first >= 0xf0 && first < 0xf5 && index + 2 < bytes.length) {
      cp = ((first & 0x07) << 18) |
        ((bytes[index] & 0x3f) << 12) |
        ((bytes[index + 1] & 0x3f) << 6) |
        (bytes[index + 2] & 0x3f);
      index += 3;
    }
    if (cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) cp = 0xfffd;
    out += String.fromCodePoint(cp);
  }
  return out;
}

const HEX = "0123456789abcdef";

export function toHex(bytes: Uint8Array): string {
  let out = "";
  for (const byte of bytes) {
    out += HEX[byte >> 4] + HEX[byte & 0x0f];
  }
  return out;
}

const B64 =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const B64_LOOKUP: Record<string, number> = (() => {
  const table: Record<string, number> = {};
  for (let i = 0; i < B64.length; i += 1) table[B64[i]] = i;
  table["-"] = 62;
  table["_"] = 63;
  return table;
})();

export function base64Encode(bytes: Uint8Array): string {
  let out = "";
  let index = 0;
  for (; index + 2 < bytes.length; index += 3) {
    const value = (bytes[index] << 16) | (bytes[index + 1] << 8) |
      bytes[index + 2];
    out += B64[(value >> 18) & 63] + B64[(value >> 12) & 63] +
      B64[(value >> 6) & 63] + B64[value & 63];
  }
  const remaining = bytes.length - index;
  if (remaining === 1) {
    const value = bytes[index] << 16;
    out += B64[(value >> 18) & 63] + B64[(value >> 12) & 63] + "==";
  } else if (remaining === 2) {
    const value = (bytes[index] << 16) | (bytes[index + 1] << 8);
    out += B64[(value >> 18) & 63] + B64[(value >> 12) & 63] +
      B64[(value >> 6) & 63] + "=";
  }
  return out;
}

/** 解码 base64（允许 data URI 前缀/空白/URL-safe）；非法输入返回 null。 */
export function base64Decode(input: string): Uint8Array | null {
  let text = input.trim();
  const comma = text.indexOf(",");
  if (text.startsWith("data:") && comma >= 0) text = text.slice(comma + 1);
  text = text.replace(/\s+/g, "").replace(/=+$/, "");
  if (!text) return new Uint8Array(0);
  const out: number[] = [];
  let buffer = 0;
  let bits = 0;
  for (const char of text) {
    const value = B64_LOOKUP[char];
    if (value === undefined) return null;
    buffer = (buffer << 6) | value;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out.push((buffer >> bits) & 0xff);
    }
  }
  return new Uint8Array(out);
}
