import { test } from "node:test";
import assert from "node:assert/strict";

import { base64Encode, utf8Encode } from "../../src/offline/bytes.ts";
import {
  decodePickedPackage,
  headerFileName,
  MAX_IMPORT_BYTES,
} from "../../src/offline/packageImport.ts";

test("picked package decodes bytes and keeps the file name", () => {
  const raw = utf8Encode("PK diagnostics");
  const picked = decodePickedPackage({
    ok: true,
    file_name: "device.zip",
    size: raw.length,
    base64: base64Encode(raw),
  });
  assert.equal(picked.fileName, "device.zip");
  assert.deepEqual([...picked.bytes], [...raw]);
});

test("cancelled or missing picks surface the native message", () => {
  assert.throws(() => decodePickedPackage(null), /已取消选择/);
  assert.throws(
    () => decodePickedPackage({ ok: false, message: "没有可用界面" }),
    /没有可用界面/,
  );
});

test("invalid, empty, and oversized packages are rejected", () => {
  assert.throws(
    () => decodePickedPackage({ ok: true, base64: "" }),
    /为空或无法解码/,
  );
  assert.throws(
    () => decodePickedPackage({ ok: true, base64: "!!!!" }),
    /为空或无法解码/,
  );
  assert.throws(
    () => decodePickedPackage({
      ok: true,
      size: MAX_IMPORT_BYTES + 1,
      base64: base64Encode(new Uint8Array([1])),
    }),
    /超过 20 MiB/,
  );
});

test("HTTP header file names are ASCII and path-free", () => {
  const header = headerFileName("../诊断/包.zip");
  assert.match(header, /^[\x20-\x7e]+$/);
  assert.doesNotMatch(header, /[\\/"]/);
  assert.ok(header.endsWith(".zip"));
  assert.equal(headerFileName(""), "diagnostics.zip");
});
