import threading
import sqlite3
from unittest.mock import Mock

import pytest
from jsonschema import validate

from wechat_bridge_collector.app import drain_contact_events, load_complete_contact_snapshot
from wechat_bridge_collector.bridge import BridgeResponse, CONTACT_EVENT_PAYLOAD_SCHEMA
from wechat_bridge_collector.contact_sync import ContactSync, MAX_PAYLOAD_BYTES, encode
from wechat_bridge_collector.wechat_source import DatabaseSnapshotError


def snapshot(count=2, account_id="account-a"):
    return {
        "account": {"accountId": account_id, "source": "wechat-local-db", "platform": "darwin"},
        "snapshotToken": "file-time:size", "total": count, "offset": 0, "hasMore": False,
        "contacts": [{"username": f"contact-{i:05}", "displayName": f"用户 {i}",
                      "nickName": f"用户 {i}", "remark": "", "isDeleted": False}
                     for i in range(count)],
    }


def take_all(sync):
    result = []
    while event := sync.pending():
        validate(event["payload"], CONTACT_EVENT_PAYLOAD_SCHEMA)
        assert len(encode(event["payload"]).encode("utf-8")) <= MAX_PAYLOAD_BYTES
        result.append(event)
        sync.acknowledge(event["eventId"])
    return result


@pytest.fixture
def sync(tmp_path):
    result = ContactSync(tmp_path / "contacts.sqlite3")
    yield result
    result.close()


def test_ten_thousand_contacts_then_one_remark_is_one_change(sync):
    observed = snapshot(10_000)
    assert sync.prepare(observed) == 50
    initial = take_all(sync)
    assert all(e["payload"]["mode"] == "initial" for e in initial)
    assert [e["payload"]["batchIndex"] for e in initial] == list(range(50))
    assert len({e["payload"]["syncId"] for e in initial}) == 1
    assert sum(len(e["payload"]["contacts"]) for e in initial) == 10_000
    observed["snapshotToken"] = "another-file-time:size"
    assert sync.prepare(observed) == 0
    observed["contacts"][5000]["remark"] = "新备注"
    assert sync.prepare(observed) == 1
    delta = take_all(sync)
    assert len(delta) == 1
    assert delta[0]["payload"]["mode"] == "delta"
    assert delta[0]["payload"]["sequence"] == 51
    assert len(delta[0]["payload"]["contacts"]) == 1
    assert delta[0]["payload"]["contacts"][0]["contactId"] == "contact-05000"


def test_restart_resumes_exact_unacknowledged_bytes(tmp_path):
    path = tmp_path / "contacts.sqlite3"
    first = ContactSync(path)
    first.prepare(snapshot(401))
    first.acknowledge(first.pending()["eventId"])
    pending = first.pending()
    first.close()
    restarted = ContactSync(path)
    try:
        assert restarted.pending() == pending
        events = take_all(restarted)
        assert [e["payload"]["batchIndex"] for e in events] == [1, 2]
        assert restarted.prepare(snapshot(401)) == 0
    finally:
        restarted.close()


def test_lost_ack_retries_same_id_and_does_not_append_more_work(sync):
    sync.prepare(snapshot())
    original = sync.pending()
    bridge = Mock()
    bridge.emit_event.return_value = BridgeResponse(False, 0, "lost response")
    assert not drain_contact_events(sync, bridge, False, threading.Event())
    assert sync.pending() == original
    with pytest.raises(ValueError, match="pending"):
        sync.prepare(snapshot(100))
    bridge.emit_event.return_value = BridgeResponse(True, 200, "{}")
    assert drain_contact_events(sync, bridge, False, threading.Event())
    assert bridge.emit_event.call_args_list[0] == bridge.emit_event.call_args_list[1]
    assert sync.prepare(snapshot()) == 0


def test_commit_failure_rolls_back_both_baseline_and_outbox(sync):
    sync.db.execute("CREATE TRIGGER reject_event BEFORE INSERT ON outbox WHEN (SELECT COUNT(*) FROM outbox)>0 BEGIN SELECT RAISE(ABORT,'disk failure'); END")
    with pytest.raises(Exception, match="disk failure"):
        sync.prepare(snapshot(401))
    assert sync.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    assert sync.db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
    assert sync.pending() is None
    sync.db.execute("DROP TRIGGER reject_event")
    assert sync.prepare(snapshot()) == 1
    assert take_all(sync)[0]["payload"]["mode"] == "initial"


def test_a_to_b_to_a_has_distinct_revision_and_event_id(sync):
    observed = snapshot(1)
    events = []
    for remark in ("A", "B", "A"):
        observed["contacts"][0]["remark"] = remark
        sync.prepare(observed)
        events.extend(take_all(sync))
    assert len({e["eventId"] for e in events}) == 3
    assert [e["payload"]["sequence"] for e in events] == [1, 2, 3]


def test_missing_contact_does_not_infer_deletion_but_explicit_source_flag_changes(sync):
    original = snapshot(2)
    sync.prepare(original)
    take_all(sync)
    assert sync.prepare(snapshot(1)) == 0
    assert sync.prepare(original) == 0
    original["contacts"][1]["isDeleted"] = True
    sync.prepare(original)
    changed = take_all(sync)[0]["payload"]["contacts"]
    assert len(changed) == 1
    assert changed[0]["sourceDeleted"] is True


def test_accounts_are_isolated_and_switch_back_does_not_reinitialize(sync):
    sync.prepare(snapshot(account_id="first"))
    first = take_all(sync)[0]
    sync.prepare(snapshot(account_id="second"))
    second = take_all(sync)[0]
    assert first["payload"]["streamId"] != second["payload"]["streamId"]
    assert sync.prepare(snapshot(account_id="first")) == 0


def test_unicode_payload_limit_and_oversize_contact(sync):
    observed = snapshot(20)
    for item in observed["contacts"]:
        item["remark"] = "中" * 3000
    assert sync.prepare(observed) > 1
    assert sum(len(e["payload"]["contacts"]) for e in take_all(sync)) == 20
    observed["contacts"][0]["remark"] = "中" * MAX_PAYLOAD_BYTES
    with pytest.raises(ValueError, match="size limit"):
        sync.prepare(observed)
    assert sync.pending() is None


def test_empty_initialization_and_later_new_contact(sync):
    sync.prepare(snapshot(0))
    event = take_all(sync)[0]
    assert event["payload"]["mode"] == "initial"
    assert event["payload"]["contacts"] == []
    assert sync.prepare(snapshot(0)) == 0
    sync.prepare(snapshot(1))
    assert take_all(sync)[0]["payload"]["mode"] == "delta"


def test_reordering_and_file_changes_are_not_contact_changes(sync):
    observed = snapshot(3)
    sync.prepare(observed)
    take_all(sync)
    observed["contacts"].reverse()
    observed["snapshotToken"] = "new-token"
    observed["contacts"][0]["memberCount"] = 123
    assert sync.prepare(observed) == 0


@pytest.mark.parametrize("kind", ["count", "duplicate", "partial"])
def test_invalid_observation_does_not_establish_baseline(sync, kind):
    observed = snapshot(2)
    if kind == "count":
        observed["total"] = 3
    elif kind == "duplicate":
        observed["contacts"][1] = observed["contacts"][0]
    else:
        observed["hasMore"] = True
    with pytest.raises(ValueError):
        sync.prepare(observed)
    assert sync.pending() is None
    assert sync.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["empty_page", "changed_token", "changed_account", "short", "duplicate", "missing"])
def test_paginated_read_rejects_incomplete_or_mixed_observations(kind):
    observed = snapshot(2)
    first = {**observed, "contacts": observed["contacts"][:1], "hasMore": True}
    second = {**observed, "contacts": observed["contacts"][1:], "offset": 1}
    if kind == "empty_page":
        second.update(contacts=[], hasMore=True)
    elif kind == "changed_token":
        second["snapshotToken"] = "changed"
    elif kind == "changed_account":
        second["account"] = {**observed["account"], "accountId": "different"}
    elif kind == "short":
        second["contacts"] = []
    elif kind == "duplicate":
        second["contacts"] = first["contacts"]
    elif kind == "missing":
        first["snapshotToken"] = "missing"
    source = Mock(spec=["assert_complete_coverage", "contact_snapshot"])
    source.contact_snapshot.side_effect = [first, second]
    with pytest.raises(DatabaseSnapshotError):
        load_complete_contact_snapshot(source, page_size=1)


def test_dry_run_does_not_touch_real_contact_state(tmp_path):
    path = tmp_path / "contact-sync.sqlite3"
    sync = ContactSync(":memory:")
    try:
        sync.prepare(snapshot())
        bridge = Mock()
        assert drain_contact_events(sync, bridge, True, threading.Event())
        bridge.emit_event.assert_not_called()
        assert not path.exists()
    finally:
        sync.close()


def test_stop_retains_next_event(sync):
    sync.prepare(snapshot())
    original = sync.pending()
    stop = threading.Event()
    stop.set()
    assert not drain_contact_events(sync, Mock(), False, stop)
    assert sync.pending() == original


def test_missing_state_table_is_reported_not_reconstructed(tmp_path):
    path = tmp_path / 'contacts.sqlite3'
    sync = ContactSync(path)
    sync.prepare(snapshot())
    sync.db.execute('DROP TABLE contacts')
    pending = sync.pending()
    sync.close()
    with pytest.raises(sqlite3.DatabaseError, match='contacts'):
        ContactSync(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='contacts'").fetchone()[0] == 0
        assert db.execute('SELECT event_id FROM outbox').fetchone()[0] == pending['eventId']
