/** RSA PKCS#1 v1.5 + SHA-256 验签（纯 TypeScript，RN 与 Node 测试共用）。 */

import { sha256 } from "../offline/sha256.ts";

export interface RsaPublicKey {
  n: string;
  e?: string;
}

const DIGEST_INFO_SHA256 = "3031300d060960864801650304020105000420";
const DEFAULT_EXPONENT = "010001";

function decodeBase64Url(value: string): Uint8Array {
  const text = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = text + "=".repeat((4 - (text.length % 4)) % 4);
  const alphabet =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  const bytes: number[] = [];
  let buffer = 0;
  let bits = 0;
  for (const char of padded) {
    if (char === "=") break;
    const index = alphabet.indexOf(char);
    if (index < 0) throw new Error("公钥不是合法 base64url");
    buffer = (buffer << 6) | index;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      bytes.push((buffer >> bits) & 0xff);
    }
  }
  return Uint8Array.from(bytes);
}

function toBigInt(value: string): bigint {
  const trimmed = value.trim();
  if (!trimmed) return 0n;
  if (/^[0-9a-fA-F]+$/.test(trimmed) && trimmed.length % 2 === 0) {
    return BigInt("0x" + trimmed);
  }
  const bytes = decodeBase64Url(trimmed);
  let result = 0n;
  for (const byte of bytes) result = (result << 8n) | BigInt(byte);
  return result;
}

function bytesToBigInt(bytes: Uint8Array): bigint {
  let result = 0n;
  for (const byte of bytes) result = (result << 8n) | BigInt(byte);
  return result;
}

function modPow(base: bigint, exponent: bigint, modulus: bigint): bigint {
  let result = 1n;
  let power = base % modulus;
  let exp = exponent;
  while (exp > 0n) {
    if (exp & 1n) result = (result * power) % modulus;
    power = (power * power) % modulus;
    exp >>= 1n;
  }
  return result;
}

function paddedBytes(value: bigint, length: number): Uint8Array {
  const out = new Uint8Array(length);
  let rest = value;
  for (let index = length - 1; index >= 0; index -= 1) {
    out[index] = Number(rest & 0xffn);
    rest >>= 8n;
  }
  return out;
}

export function rsaSha256Verify(
  key: RsaPublicKey,
  message: Uint8Array,
  signature: Uint8Array,
): boolean {
  try {
    const modulus = toBigInt(key.n);
    const exponent = toBigInt(key.e || DEFAULT_EXPONENT);
    if (modulus <= 0n || exponent <= 1n) return false;
    const size = Math.ceil(modulus.toString(16).length / 2);
    if (signature.length !== size) return false;
    const value = bytesToBigInt(signature);
    if (value >= modulus) return false;
    const encoded = paddedBytes(modPow(value, exponent, modulus), size);
    const digestInfo = DIGEST_INFO_SHA256 + Array.from(sha256(message))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
    const tail = Array.from(decodeHex(digestInfo));
    const padding = size - tail.length - 3;
    if (padding < 8) return false;
    const expected = new Uint8Array(size);
    expected[0] = 0x00;
    expected[1] = 0x01;
    for (let index = 2; index < 2 + padding; index += 1) expected[index] = 0xff;
    expected[2 + padding] = 0x00;
    expected.set(Uint8Array.from(tail), 3 + padding);
    let diff = 0;
    for (let index = 0; index < size; index += 1) {
      diff |= expected[index] ^ encoded[index];
    }
    return diff === 0;
  } catch {
    return false;
  }
}

function decodeHex(value: string): Uint8Array {
  const pairs = value.match(/../g) || [];
  return Uint8Array.from(pairs.map((pair) => parseInt(pair, 16)));
}

export function decodeSignature(value: string): Uint8Array {
  const trimmed = value.trim();
  if (/^[0-9a-fA-F]+$/.test(trimmed) && trimmed.length % 2 === 0) {
    return decodeHex(trimmed);
  }
  return decodeBase64Url(trimmed);
}

export function utf8Encode(value: string): Uint8Array {
  if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(value);
  const out: number[] = [];
  for (const char of value) {
    const code = char.codePointAt(0) as number;
    if (code < 0x80) out.push(code);
    else if (code < 0x800) {
      out.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f));
    } else if (code < 0x10000) {
      out.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f),
        0x80 | (code & 0x3f));
    } else {
      out.push(0xf0 | (code >> 18), 0x80 | ((code >> 12) & 0x3f),
        0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
    }
  }
  return Uint8Array.from(out);
}
