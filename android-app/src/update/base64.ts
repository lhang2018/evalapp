/** 无 Buffer 依赖的 base64 编解码（Hermes/RN 通用）。 */

const ALPHABET =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

export function bytesToBase64(bytes: Uint8Array): string {
  let out = "";
  let index = 0;
  while (index + 3 <= bytes.length) {
    const chunk = (bytes[index] << 16) | (bytes[index + 1] << 8)
      | bytes[index + 2];
    out += ALPHABET[(chunk >> 18) & 63] + ALPHABET[(chunk >> 12) & 63]
      + ALPHABET[(chunk >> 6) & 63] + ALPHABET[chunk & 63];
    index += 3;
  }
  const left = bytes.length - index;
  if (left === 1) {
    const chunk = bytes[index] << 16;
    out += ALPHABET[(chunk >> 18) & 63] + ALPHABET[(chunk >> 12) & 63] + "==";
  } else if (left === 2) {
    const chunk = (bytes[index] << 16) | (bytes[index + 1] << 8);
    out += ALPHABET[(chunk >> 18) & 63] + ALPHABET[(chunk >> 12) & 63]
      + ALPHABET[(chunk >> 6) & 63] + "=";
  }
  return out;
}

function decodeChar(char: string): number {
  const value = ALPHABET.indexOf(char);
  if (value >= 0) return value;
  if (char === "-") return 62;
  if (char === "_") return 63;
  return -1;
}

export function base64ToBytes(value: string): Uint8Array {
  const clean = value.replace(/[\r\n\s]/g, "");
  let length = clean.length;
  while (length > 0 && clean[length - 1] === "=") length -= 1;
  const bytes = new Uint8Array(Math.floor((length * 6) / 8));
  let accumulator = 0;
  let bits = 0;
  let offset = 0;
  for (let index = 0; index < length; index += 1) {
    const decoded = decodeChar(clean[index]);
    if (decoded < 0) throw new Error("非法 base64 字符");
    accumulator = (accumulator << 6) | decoded;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      bytes[offset] = (accumulator >> bits) & 0xff;
      offset += 1;
      accumulator &= bits > 0 ? (1 << bits) - 1 : 0;
    }
  }
  return bytes;
}

/** Hermes 不保证有 TextDecoder；只处理明确传入的 UTF-8 字节。 */
export function utf8Decode(bytes: Uint8Array): string {
  let out = "";
  let index = 0;
  while (index < bytes.length) {
    const first = bytes[index];
    index += 1;
    if (first < 0x80) {
      out += String.fromCharCode(first);
      continue;
    }
    let codePoint = 0;
    let remaining = 0;
    if ((first & 0xe0) === 0xc0) {
      codePoint = first & 0x1f;
      remaining = 1;
    } else if ((first & 0xf0) === 0xe0) {
      codePoint = first & 0x0f;
      remaining = 2;
    } else if ((first & 0xf8) === 0xf0) {
      codePoint = first & 0x07;
      remaining = 3;
    } else {
      throw new Error("非法 UTF-8 字节");
    }
    while (remaining > 0) {
      if (index >= bytes.length) throw new Error("UTF-8 字节被截断");
      const next = bytes[index];
      if ((next & 0xc0) !== 0x80) throw new Error("非法 UTF-8 续字节");
      codePoint = (codePoint << 6) | (next & 0x3f);
      index += 1;
      remaining -= 1;
    }
    out += String.fromCodePoint(codePoint);
  }
  return out;
}
