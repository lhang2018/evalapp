"""为真机更新联调生成一个带真实制品 envelope 的发布。

私钥只在本机签名时读取，不写入数据库、日志或响应；客户端只拿公钥。
用法：
  python tools/seed_update_release.py --data-dir .evoagent-update \
      --bundle path/to/bundle.json --key path/to/private.pem \
      --port 8300 --version v9.1.0
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from evoagent.app import App

SCHEMA = "evoagent.release.signing/1"


def canonical_message(release_id: str, version: str, digest: str,
                      size: int, key_id: str) -> bytes:
    return "\n".join([
        "EVOAGENT-ARTIFACT-V1", SCHEMA, release_id, version,
        digest.lower().removeprefix("sha256:"), str(size), key_id,
    ]).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--key-id", default="dev-release-1")
    parser.add_argument("--release-id", default="rel_update_1")
    parser.add_argument("--version", default="v9.1.0")
    parser.add_argument("--min-app-version", default="0.1.0")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8300)
    parser.add_argument("--gray", type=int, default=100)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    bundle_bytes = Path(args.bundle).read_bytes()
    digest = hashlib.sha256(bundle_bytes).hexdigest()
    key = serialization.load_pem_private_key(
        Path(args.key).read_bytes(), password=None)
    signature = key.sign(
        canonical_message(args.release_id, args.version, digest,
                          len(bundle_bytes), args.key_id),
        padding.PKCS1v15(), hashes.SHA256()).hex()

    app = App(data_dir=str(data_dir))
    try:
        artifact_dir = Path(app.releases.artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / f"{args.release_id}.bundle").write_bytes(bundle_bytes)
        manifest = app.releases.build_manifest(
            release_id=args.release_id,
            version=args.version,
            channel="stable",
            target_platforms=["android"],
            min_app_version=args.min_app_version,
            bundle_kind="workflow",
            permission_requirements=[],
            required_tool_contracts=["open_app"],
            artifact_url=(f"http://{args.host}:{args.port}"
                          f"/artifacts/{args.release_id}"),
            artifact_sha256=digest,
            artifact_size=len(bundle_bytes),
            artifact_signature=signature,
            artifact_signing_key_id=args.key_id,
            signature_schema=SCHEMA,
        )
        deployed = app.releases.deploy(
            manifest.release_id, gate_passed=True,
            gray_percent=max(0, min(100, args.gray)))
        if not deployed.get("ok"):
            print(json.dumps(deployed, ensure_ascii=False))
            return 2
        print(json.dumps({
            "release_id": manifest.release_id,
            "version": manifest.version,
            "artifact_url": f"http://{args.host}:{args.port}"
                            f"/artifacts/{manifest.release_id}",
            "artifact_sha256": digest,
            "artifact_size": len(bundle_bytes),
            "signing_key_id": args.key_id,
            "deployed": True,
        }, ensure_ascii=False))
    finally:
        app.db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
