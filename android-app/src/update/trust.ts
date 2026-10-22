/** 客户端受信公钥与制品签名验证（ALN-11）。 */

import { sha256Hex } from "../offline/sha256.ts";
import {
  decodeSignature,
  rsaSha256Verify,
  utf8Encode,
  type RsaPublicKey,
} from "./rsa.ts";

export interface TrustedPublicKey extends RsaPublicKey {
  key_id: string;
  algorithm?: "rsa-sha256";
  not_before?: number;
  not_after?: number;
  revoked?: boolean;
}

export interface ArtifactEnvelope {
  schema: string;
  release_id: string;
  version: string;
  artifact_sha256: string;
  artifact_size: number;
  signing_key_id: string;
  signature: string;
}

export interface VerifyResult {
  ok: boolean;
  reasons: string[];
  key_id: string;
  digest: string;
}

export function canonicalArtifactMessage(envelope: ArtifactEnvelope): Uint8Array {
  return utf8Encode([
    "EVOAGENT-ARTIFACT-V1",
    envelope.schema,
    envelope.release_id,
    envelope.version,
    envelope.artifact_sha256.toLowerCase().replace(/^sha256:/, ""),
    String(envelope.artifact_size),
    envelope.signing_key_id,
  ].join("\n"));
}

export class TrustedKeyRing {
  private readonly keys: Map<string, TrustedPublicKey>;
  private readonly now: () => number;

  constructor(keys: TrustedPublicKey[], now: () => number = () => Date.now()) {
    this.keys = new Map(keys.map((key) => [key.key_id, key]));
    this.now = now;
  }

  verifyArtifact(envelope: ArtifactEnvelope, bytes: Uint8Array): VerifyResult {
    return this.verifyDigest(
      envelope, sha256Hex(bytes), bytes.length);
  }

  /** 已下载并算过摘要时直接验签，避免再次读入大制品。 */
  verifyDigest(envelope: ArtifactEnvelope, digest: string,
               size: number): VerifyResult {
    const reasons: string[] = [];
    const declared = envelope.artifact_sha256
      .toLowerCase().replace(/^sha256:/, "");
    if (declared !== digest) reasons.push("制品摘要与声明不符");
    if (envelope.artifact_size !== size) {
      reasons.push("制品大小与声明不符");
    }
    const key = this.keys.get(envelope.signing_key_id);
    if (!key) {
      reasons.push(`未知签名密钥: ${envelope.signing_key_id}`);
    } else if (key.revoked) {
      reasons.push(`签名密钥已撤销: ${envelope.signing_key_id}`);
    } else {
      const now = this.now();
      if (key.not_before !== undefined && now < key.not_before) {
        reasons.push("签名密钥尚未生效");
      }
      if (key.not_after !== undefined && now >= key.not_after) {
        reasons.push("签名密钥已过期");
      }
      if (reasons.length === 0) {
        const message = canonicalArtifactMessage({
          ...envelope, artifact_sha256: digest,
        });
        let signature: Uint8Array;
        try {
          signature = decodeSignature(envelope.signature);
        } catch {
          reasons.push("签名编码非法");
          signature = new Uint8Array();
        }
        if (!signature.length
            || !rsaSha256Verify(key, message, signature)) {
          reasons.push("签名验证失败");
        }
      }
    }
    return {
      ok: reasons.length === 0,
      reasons,
      key_id: envelope.signing_key_id,
      digest,
    };
  }
}
