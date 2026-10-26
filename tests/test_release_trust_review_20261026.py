"""Use real key export, client verification and SQLite rollback receipts."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from evoagent.core.persistence import Database
from evoagent.release.delivery import ReleaseDelivery
from evoagent.release.identity import IdentityRegistry, PrivateIdentityKey, SCHEMA
from evoagent.release.manager import ReleaseManager

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def keys():
    # Generated only for this test process; no production credentials are read.
    return (PrivateIdentityKey.generate(key_id="synthetic-old"),
            PrivateIdentityKey.generate(key_id="synthetic-new"))


def envelope(key, release, version, body):
    digest = hashlib.sha256(body).hexdigest()
    fields = dict(release_id=release, version=version, channel="stable",
                  target_platforms=["android"], min_app_version="0.1.0",
                  bundle_kind="workflow", artifact_url="local-synthetic-artifact",
                  artifact_sha256=digest, artifact_size=len(body),
                  artifact_signature=key.sign_artifact_envelope(
                      release_id=release, version=version, artifact_sha256=digest,
                      artifact_size=len(body)), artifact_signing_key_id=key.key_id,
                  signature_schema=SCHEMA)
    wire = dict(schema=SCHEMA, release_id=release, version=version,
                artifact_sha256=digest, artifact_size=len(body),
                signing_key_id=key.key_id, signature=fields["artifact_signature"])
    return fields, wire


def client_verify(records, wire, body):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for real client key-ring verification")
    trust_url = (ROOT/"android-app/src/update/trust.ts").as_uri()
    script = ("import { readFileSync } from 'node:fs';\n"
              "import { TrustedKeyRing } from " + json.dumps(trust_url) + ";\n"
              "const p=JSON.parse(readFileSync(0,'utf8'));\n"
              "const ring=new TrustedKeyRing(p.keys);\n"
              "console.log(JSON.stringify({usable:ring.hasUsableKey(p.envelope.signing_key_id),"
              "verified:ring.verifyArtifact(p.envelope,new Uint8Array(p.bytes))}));")
    result = subprocess.run([node,"--input-type=module","-e",script],
                            input=json.dumps({"keys":records,"envelope":wire,"bytes":list(body)}),
                            capture_output=True,text=True,encoding="utf-8",timeout=15,check=True)
    return json.loads(result.stdout)


def test_revoked_public_export_stays_revoked_in_actual_client_ring(keys, tmp_path):
    old, new = keys
    registry = IdentityRegistry(tmp_path/"identity.json")
    registry.register_from_private(old, activate=True, actor="test", reason="synthetic setup")
    registry.register_from_private(new, actor="test", reason="synthetic rotation")
    body = b"synthetic artifact"
    _, wire = envelope(old,"old-release","v1.0.0",body)
    before = client_verify(registry.public_records(include_revoked=True),wire,body)
    assert before["usable"] and before["verified"]["ok"]
    registry.rotate(new.key_id, actor="test", reason="synthetic rotation")
    registry.revoke(old.key_id, actor="test", reason="synthetic revocation")
    reopened = IdentityRegistry(tmp_path/"identity.json")
    after = client_verify(reopened.public_records(include_revoked=True),wire,body)
    assert after["usable"] is False
    assert after["verified"]["ok"] is False
    assert old.key_id not in {record["key_id"] for record in reopened.public_records()}
    exported = next(record for record in reopened.public_records(include_revoked=True) if record["key_id"]==old.key_id)
    assert exported["revoked"] is True
    assert not ({"revoked_reason","private_key","d","p","q"} & set(exported))


@pytest.fixture
def chain(keys, tmp_path):
    old, new = keys
    registry = IdentityRegistry(tmp_path/"identity.json")
    registry.register_from_private(old, activate=True, actor="test", reason="synthetic setup")
    registry.register_from_private(new, activate=True, actor="test", reason="synthetic setup")
    db = Database(str(tmp_path/"db.sqlite"))
    releases = ReleaseManager(store=db)
    old_fields, _ = envelope(old,"old-release","v1.0.0",b"old synthetic artifact")
    new_fields, _ = envelope(new,"new-release","v2.0.0",b"new synthetic artifact")
    releases.build_manifest(**old_fields)
    releases.build_manifest(**new_fields)
    releases.current_release_id = "new-release"
    delivery = ReleaseDelivery(releases,db,identity_registry=registry)
    delivery.reconcile(installation_id="inst-test",device_id="dev-test",app_version="0.2.0")
    assert delivery.record_apply(installation_id="inst-test",action="activated",release_id="new-release")["ok"]
    yield delivery, db, releases, registry, old, new
    db.close()


@pytest.mark.parametrize("invalid", ["revoked","expired","future","unregistered","retired"])
def test_rollback_to_untrusted_or_retired_destination_is_refused_without_receipt(chain, invalid):
    delivery, db, releases, registry, old, _ = chain
    if invalid == "revoked":
        registry.revoke(old.key_id,actor="test",reason="synthetic revocation")
    elif invalid == "expired":
        registry.get(old.key_id).not_after = time.time()-60
    elif invalid == "future":
        registry.get(old.key_id).not_before = time.time()+3600
    elif invalid == "unregistered":
        other = IdentityRegistry()
        other.register_from_private(chain[-1],activate=True,actor="test",reason="synthetic only new")
        delivery.set_identity_registry(other)
    else:
        releases.retire("old-release",actor="test",reason="synthetic retirement")
    before = db.get_installation("inst-test")
    count = db._conn.execute("SELECT COUNT(*) FROM device_deployments").fetchone()[0]
    result = delivery.record_apply(installation_id="inst-test",action="rolled_back",
        release_id="new-release",to_release_id="old-release",apply_id="rollback-denied-01")
    assert result["ok"] is False
    assert db.get_installation("inst-test") == before
    assert db._conn.execute("SELECT COUNT(*) FROM device_deployments").fetchone()[0] == count


@pytest.mark.parametrize("reported_release", ["new-release","old-release"])
def test_valid_rollback_checks_destination_and_records_its_actual_trust(chain, reported_release):
    delivery, db, releases, registry, old, new = chain
    registry.rotate(old.key_id,actor="test",reason="recover to trusted previous")
    registry.revoke(new.key_id,actor="test",reason="synthetic bad current key")
    result = delivery.record_apply(installation_id="inst-test",action="rolled_back",
        release_id=reported_release,to_release_id="old-release",signing_key_id=old.key_id)
    assert result["ok"] is True
    assert result["installation"]["current_release_id"] == "old-release"
    receipt = result["deployment"]
    assert receipt["release_id"] == "old-release"
    assert receipt["previous_release_id"] == "new-release"
    assert receipt["signing_key_id"] == old.key_id
    assert receipt["artifact_digest"] == releases.manifests["old-release"].artifact_sha256
    assert receipt["signing_identity_status"] == "usable"


def test_rollback_claimed_wrong_destination_key_is_rejected(chain):
    delivery, db, _, _, _, new = chain
    before = db.get_installation("inst-test")
    result = delivery.record_apply(installation_id="inst-test",action="rolled_back",
        release_id="new-release",to_release_id="old-release",signing_key_id=new.key_id)
    assert result["ok"] is False
    assert db.get_installation("inst-test") == before
