/** 最小 ZIP 写入器：仅 STORE（不压缩），避免 RN 侧引入原生依赖。 */

import { utf8Encode } from "./bytes.ts";

export interface ZipEntry {
  name: string;
  data: Uint8Array;
}

const CRC_TABLE: Uint32Array = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i += 1) {
    let value = i;
    for (let bit = 0; bit < 8; bit += 1) {
      value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    }
    table[i] = value >>> 0;
  }
  return table;
})();

export function crc32(data: Uint8Array): number {
  let crc = 0xffffffff;
  for (const byte of data) {
    crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

class ByteWriter {
  private chunks: Uint8Array[] = [];
  private length = 0;

  get size(): number {
    return this.length;
  }

  push(bytes: Uint8Array): void {
    this.chunks.push(bytes);
    this.length += bytes.length;
  }

  u16(value: number): void {
    const bytes = new Uint8Array(2);
    new DataView(bytes.buffer).setUint16(0, value & 0xffff, true);
    this.push(bytes);
  }

  u32(value: number): void {
    const bytes = new Uint8Array(4);
    new DataView(bytes.buffer).setUint32(0, value >>> 0, true);
    this.push(bytes);
  }

  finish(): Uint8Array {
    const out = new Uint8Array(this.length);
    let offset = 0;
    for (const chunk of this.chunks) {
      out.set(chunk, offset);
      offset += chunk.length;
    }
    return out;
  }
}

function dosTime(date: Date): { time: number; day: number } {
  const year = Math.min(2107, Math.max(1980, date.getFullYear()));
  const month = Math.min(12, Math.max(1, date.getMonth() + 1));
  const day = Math.min(31, Math.max(1, date.getDate()));
  return {
    time: (date.getHours() << 11) | (date.getMinutes() << 5) |
      (date.getSeconds() >> 1),
    day: ((year - 1980) << 9) | (month << 5) | day,
  };
}

export function zipStore(entries: ZipEntry[], mtime = new Date()): Uint8Array {
  if (entries.length > 0xffff) {
    throw new Error("ZIP 条目过多");
  }
  const stamp = dosTime(mtime);
  const local = new ByteWriter();
  const central = new ByteWriter();

  for (const entry of entries) {
    const name = utf8Encode(entry.name);
    const crc = crc32(entry.data);
    const offset = local.size;
    local.u32(0x04034b50);
    local.u16(20);
    local.u16(0x0800);
    local.u16(0);
    local.u16(stamp.time);
    local.u16(stamp.day);
    local.u32(crc);
    local.u32(entry.data.length);
    local.u32(entry.data.length);
    local.u16(name.length);
    local.u16(0);
    local.push(name);
    local.push(entry.data);

    central.u32(0x02014b50);
    central.u16(20);
    central.u16(20);
    central.u16(0x0800);
    central.u16(0);
    central.u16(stamp.time);
    central.u16(stamp.day);
    central.u32(crc);
    central.u32(entry.data.length);
    central.u32(entry.data.length);
    central.u16(name.length);
    central.u16(0);
    central.u16(0);
    central.u16(0);
    central.u16(0);
    central.u32(0);
    central.u32(offset);
    central.push(name);
  }

  const localBytes = local.finish();
  const centralBytes = central.finish();
  const end = new ByteWriter();
  end.u32(0x06054b50);
  end.u16(0);
  end.u16(0);
  end.u16(entries.length);
  end.u16(entries.length);
  end.u32(centralBytes.length);
  end.u32(localBytes.length);
  end.u16(0);

  const endBytes = end.finish();
  const out = new Uint8Array(
    localBytes.length + centralBytes.length + endBytes.length,
  );
  out.set(localBytes, 0);
  out.set(centralBytes, localBytes.length);
  out.set(endBytes, localBytes.length + centralBytes.length);
  return out;
}
