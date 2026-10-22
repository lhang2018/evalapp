/**
 * 调试构建使用的发布验签公钥（仅公钥；私钥只在本地签名脚本中）。
 *
 * 生产构建应由发布侧把受信公钥随安装包固化或经独立信任渠道下发，
 * 不能从制品同一通道获取公钥，否则验签失去意义。
 */

import type { TrustedPublicKey } from "./trust.ts";

export const DEV_RELEASE_KEY: TrustedPublicKey = {
  key_id: "dev-release-1",
  n: "nVE1epQmAZqXD3Km9ETuuTLnxIVtiQJtTpUcVNvzI9eaYknQTQXeaL8skcEbfC42GhF6S3BPZdxeT6XeLSQ6-cMAy0J4FOw2QbAcGj_WSFNQ0RKBRDEgtPJ6eZrAJvY9Vn2DSI-2Zkf1IcTN3oq6x8j0qFk8d67QdBOHumqsmvwBrPz0QGUOiodviS-CocOnC4K2x7BZJYKOj-e23_j0S3PKOhwcK7AIFPqftbQGmf-PwTrvHv8GNXE3ZLWvtzd9Tzf7nnqNDModnTURr4-JNoOjA5NBANFDHDK8A2CSy9scQdzhXo6VgvBXIVwzPMw9sae8r98Jd8Kxuy5RRqD8_w",
  e: "AQAB",
  algorithm: "rsa-sha256",
};
