"""跨会话用户事实测试（T-P2-02）。"""
from __future__ import annotations

from langgraph.store.memory import InMemoryStore

from memory.context_store import ContextStore, Fact


def test_put_and_get_roundtrip():
    store = ContextStore()
    store.put_fact("u1", "ticket", "TK-20261106-ABC123")
    facts = store.get_facts("u1")
    assert len(facts) == 1
    assert facts[0].kind == "ticket"
    assert "TK-20261106-ABC123" in facts[0].as_text()


def test_isolation_between_users_and_orgs():
    store = ContextStore()
    store.put_fact("u1", "ticket", "TK-A", org_id="o1")
    store.put_fact("u2", "ticket", "TK-B", org_id="o1")
    store.put_fact("u1", "ticket", "TK-C", org_id="o2")

    assert [f.value for f in store.get_facts("u1", org_id="o1")] == ["TK-A"]
    assert [f.value for f in store.get_facts("u2", org_id="o1")] == ["TK-B"]
    assert [f.value for f in store.get_facts("u1", org_id="o2")] == ["TK-C"]


def test_dedup_updates_timestamp():
    store = ContextStore()
    store.put_fact("u1", "ticket", "TK-1")
    store.put_fact("u1", "ticket", "TK-1")
    assert len(store.get_facts("u1")) == 1


def test_max_facts_trimmed():
    store = ContextStore(max_facts=3)
    for i in range(5):
        store.put_fact("u1", "ticket", f"TK-{i}")
    values = [f.value for f in store.get_facts("u1", limit=10)]
    assert values == ["TK-2", "TK-3", "TK-4"]


def test_prompt_formatting_empty_and_populated():
    store = ContextStore()
    assert store.format_for_prompt("nobody") == ""
    store.put_fact("u1", "ticket", "TK-1")
    store.put_fact("u1", "product", "理财产品A")
    text = store.format_for_prompt("u1")
    assert "该用户近期记录" in text and "TK-1" in text and "理财产品A" in text


def test_backed_by_langgraph_store():
    backing = InMemoryStore()
    store = ContextStore(store=backing)
    store.put_fact("u1", "ticket", "TK-X")
    item = backing.get(("org", "default", "user", "u1", "facts"), "facts")
    assert item is not None
    assert item.value["items"][0]["value"] == "TK-X"
    # 另一个 ContextStore 实例读同一 backing → 跨进程/跨会话可见
    assert ContextStore(store=backing).get_facts("u1")[0].value == "TK-X"


def test_clear():
    store = ContextStore()
    store.put_fact("u1", "ticket", "TK-1")
    store.clear("u1")
    assert store.get_facts("u1") == []


def test_fact_as_text_labels():
    assert Fact("ticket", "TK-1", 0).as_text().startswith("工单：")
    assert Fact("unknown", "x", 0).as_text().startswith("unknown：")
