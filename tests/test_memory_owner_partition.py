"""Independent pre-integration negative probes using existing public memory APIs."""
import json
import pytest
from evoagent.memory.manifest import MemoryIdentity
from evoagent.memory.store import MemoryStore,MemoryItem,Scope,MemoryKind,Credibility
from evoagent.memory.context import ContextAssembler

@pytest.mark.parametrize("changed",[dict(user_id="bob"),dict(project_id="p2"),dict(session_id="s2")])
def test_same_expert_key_does_not_merge_or_reveal_other_owners(tmp_path,changed):
    store=MemoryStore(str(tmp_path/"memory.db"))
    try:
        alice=MemoryIdentity("alice","p1","s1","t1","general")
        other=MemoryIdentity(**{**alice.to_dict(),**changed})
        def write(identity,text):
            return store.write(MemoryItem(text,scope=Scope.EXPERT,kind=MemoryKind.DOMAIN_KNOWLEDGE,
                status=Credibility.VERIFIED,evidence={"ok":True},applicable_domains=["general"]),identity=identity,fact_key="fact")
        mine=write(alice,"alpha same")
        theirs=write(other,"alpha same")
        assert mine.memory_id!=theirs.memory_id
        changed_item=write(other,"alpha changed")
        assert not store.get(mine.memory_id).has_conflict
        assert changed_item.memory_id not in {m.memory_id for m in store.search("alpha",identity=alice)}
    finally:store.close()


def test_context_manifest_contains_references_instead_of_constraint_and_acceptance_body():
    ctx=ContextAssembler().assemble("goal",constraints=["private_constraint_body"],
        acceptance=[{"type":"result_nonempty","value":"private_acceptance_body"}])
    payload=json.dumps(ctx.manifest.to_dict())
    assert "private_constraint_body" not in payload
    assert "private_acceptance_body" not in payload
