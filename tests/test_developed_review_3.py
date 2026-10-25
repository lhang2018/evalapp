"""Negative review probes for 18-G and 28-A; all I/O stays in tmp_path."""
import hashlib
import io
import math
from pathlib import Path
import pytest
from evoagent.tools.coding_consumption import (CodingPackRuntime,ConsumptionError,
    ProjectSnapshot,snapshot_project,restore_snapshot)
from evoagent.tools.registry import ToolRegistry
from evoagent.delivery.artifacts import ArtifactStore
from evoagent.verification import Claim,FactCrossChecker,SourceRecord,TrustedDelivery,fixture_source,fetch_source,SourceFetchError
from evoagent.verification.crosscheck import Verdict
from evoagent.verification.delivery import DeliveryError


def test_snapshot_restores_deleted_file(tmp_path):
    p=tmp_path/'a.txt';p.write_bytes(b'original')
    snap=snapshot_project(str(tmp_path));p.unlink()
    assert restore_snapshot(snap)==['a.txt']
    assert p.read_bytes()==b'original'


def test_snapshot_rejects_traversal_before_any_write(tmp_path):
    root=tmp_path/'root';root.mkdir()
    (root/'good').write_bytes(b'current');outside=tmp_path/'outside';outside.write_bytes(b'current')
    snap=ProjectSnapshot(str(root),1,{'good':b'old','../outside':b'old'})
    with pytest.raises(ConsumptionError):restore_snapshot(snap)
    assert (root/'good').read_bytes()==b'current' and outside.read_bytes()==b'current'


def test_snapshot_freezes_absolute_root(tmp_path,monkeypatch):
    root=tmp_path/'project';root.mkdir();(root/'a').write_bytes(b'original')
    monkeypatch.chdir(tmp_path);snap=snapshot_project('project')
    other=tmp_path/'elsewhere';other.mkdir();monkeypatch.chdir(other)
    assert Path(snap.root).is_absolute()
    (root/'a').unlink();restore_snapshot(snap)
    assert (root/'a').read_bytes()==b'original'


def test_changed_and_archive_preserve_exclusions(tmp_path):
    root=tmp_path/'project';root.mkdir();(root/'a').write_bytes(b'a')
    snap=snapshot_project(str(root))
    ignored=root/'node_modules';ignored.mkdir();(ignored/'secret').write_bytes(b'dependency secret')
    assert snap.changed_paths(str(root))==[]
    store=ArtifactStore(str(tmp_path/'artifacts'))
    assert CodingPackRuntime(ToolRegistry(),store).archive_modified('task',str(root),snap)['archived']==[]


def test_archive_rejects_different_project(tmp_path):
    a=tmp_path/'a';a.mkdir();(a/'file').write_bytes(b'first')
    b=tmp_path/'b';b.mkdir();(b/'file').write_bytes(b'private')
    snap=snapshot_project(str(a));store=ArtifactStore(str(tmp_path/'artifacts'))
    with pytest.raises(ConsumptionError):CodingPackRuntime(ToolRegistry(),store).archive_modified('t',str(b),snap)
    assert store.list_for_task([])==[]


def test_snapshot_has_byte_budget(tmp_path):
    (tmp_path/'big').write_bytes(b'12345')
    with pytest.raises(ConsumptionError):snapshot_project(str(tmp_path),max_bytes=4)


def test_archive_file_ids_do_not_collide(tmp_path):
    root=tmp_path/'project';root.mkdir();snap=snapshot_project(str(root))
    (root/'a').mkdir();(root/'a'/'b.txt').write_bytes(b'same');(root/'a_b.txt').write_bytes(b'same')
    store=ArtifactStore(str(tmp_path/'artifacts'));out=CodingPackRuntime(ToolRegistry(),store).archive_modified('t',str(root),snap)
    assert len(out['archived'])==2
    assert len({a['artifact_id'] for a in out['archived']})==2


def test_second_install_cannot_redirect_first_project(tmp_path):
    a=tmp_path/'a';a.mkdir();b=tmp_path/'b';b.mkdir();registry=ToolRegistry()
    first=CodingPackRuntime(registry).install_for_task('one',str(a))
    assert CodingPackRuntime(registry).install_for_task('two',str(a))['root']==first['root']
    with pytest.raises(ConsumptionError):CodingPackRuntime(registry).install_for_task('two',str(b))


def test_snapshot_rejects_external_symlink(tmp_path):
    root=tmp_path/'root';root.mkdir();outside=tmp_path/'private';outside.write_bytes(b'private')
    try:(root/'link').symlink_to(outside)
    except OSError:pytest.skip('symbolic links unavailable')
    with pytest.raises(ConsumptionError):snapshot_project(str(root))


def test_future_claim_is_not_delivered():
    claim=Claim('c','future assertion','k',valid_from=2000)
    out=TrustedDelivery().build(topic='future',claims=[claim],sources=[fixture_source(claim_key='k',quote='support')],now=1000)
    assert not out['delivered'] and out['unverified'][0]['state']=='unknown'


@pytest.mark.parametrize('start,end',[(math.nan,None),(None,math.inf),(20,10)])
def test_invalid_claim_window_rejected(start,end):
    with pytest.raises(ValueError):Claim('c','assertion','k',valid_from=start,valid_until=end)


@pytest.mark.parametrize('change',[{'quote':''},{'content_digest':''},{'accessible':'false'},{'url':''}])
def test_incomplete_or_mistyped_source_never_supports(change):
    raw=fixture_source(claim_key='k',quote='support').to_dict();raw.update(change)
    source=SourceRecord.from_dict(raw)
    assert FactCrossChecker().verify(Claim('c','assertion','k'),[source]).state!='supported'


def test_build_enforces_delivery_guard():
    class ForgedChecker:
        def verify(self,*a,**k):
            return Verdict('c','model claim','supported',.9,model_only=True,supporting=[{'url':'u'}])
    with pytest.raises(DeliveryError):TrustedDelivery(checker=ForgedChecker()).build(topic='t',claims=[Claim('c','claim','k')],sources=[])


def fake_http(monkeypatch,body):
    class Response(io.BytesIO):status=200
    monkeypatch.setattr('urllib.request.urlopen',lambda *a,**k:Response(body))


def test_fetch_rejects_quote_absent_from_body(monkeypatch):
    fake_http(monkeypatch,b'the actual page')
    with pytest.raises(SourceFetchError):fetch_source('https://source.test',quote='invented claim')


def test_source_digest_covers_raw_bytes(monkeypatch):
    body=b'content\xff';fake_http(monkeypatch,body)
    assert fetch_source('https://source.test').content_digest==hashlib.sha256(body).hexdigest()


def test_fetch_is_not_automatic_semantic_support(monkeypatch):
    fake_http(monkeypatch,b'actual page mentions the subject')
    source=fetch_source('https://source.test',claim_key='k')
    verdict=FactCrossChecker().verify(Claim('c','unrelated assertion','k'),[source])
    assert verdict.state=='unknown' and verdict.mentions


def test_fetch_bounds_source_body(monkeypatch):
    fake_http(monkeypatch,b'12345')
    with pytest.raises(SourceFetchError):fetch_source('https://source.test',max_bytes=4)


def test_empty_file_skip_is_explicit(tmp_path):
    root=tmp_path/'root';root.mkdir();file=root/'a';file.write_bytes(b'old')
    snap=snapshot_project(str(root));file.write_bytes(b'')
    store=ArtifactStore(str(tmp_path/'artifacts'))
    out=CodingPackRuntime(ToolRegistry(),store).archive_modified('t',str(root),snap)
    assert not out['archived'] and out['skipped']==['a:empty']


def test_restore_refuses_link_swapped_after_snapshot(tmp_path):
    root=tmp_path/'root';root.mkdir();file=root/'a';file.write_bytes(b'old')
    snap=snapshot_project(str(root));file.unlink();outside=tmp_path/'outside';outside.write_bytes(b'private')
    try:file.symlink_to(outside)
    except OSError:pytest.skip('symbolic links unavailable')
    with pytest.raises(ConsumptionError):restore_snapshot(snap)
    assert outside.read_bytes()==b'private'


def test_registry_mutation_invalidates_consumption_binding(tmp_path):
    from evoagent.tools.contract import ToolContract,ToolResult
    registry=ToolRegistry();runtime=CodingPackRuntime(registry)
    runtime.install_for_task('first',str(tmp_path))
    registry.register(ToolContract(tool_id='coding_symbols'),lambda args:ToolResult(ok=True))
    with pytest.raises(ConsumptionError):runtime.install_for_task('second',str(tmp_path))


def test_restore_rejects_project_root_replaced_by_link(tmp_path):
    root=tmp_path/'project';root.mkdir();(root/'a').write_bytes(b'old')
    snap=snapshot_project(str(root));root.rename(tmp_path/'original')
    outside=tmp_path/'outside';outside.mkdir();(outside/'a').write_bytes(b'private')
    try:root.symlink_to(outside,target_is_directory=True)
    except OSError:pytest.skip('symbolic links unavailable')
    with pytest.raises(ConsumptionError):restore_snapshot(snap)
    assert (outside/'a').read_bytes()==b'private'
