import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wechat_bridge_collector.app import build_parser, cmd_run
from wechat_bridge_collector.bridge import BridgeResponse
from wechat_bridge_collector.config import CollectorConfig
from wechat_bridge_collector.state import CollectorState, Cursor
from wechat_bridge_collector.wechat_source import DatabaseSnapshotError, WeChatSource


TABLE = 'Msg_' + 'a' * 32
KEY = 'message/message_0.db'
CURSOR_KEY = KEY + '#' + TABLE
CHAT = 'room@chatroom'


def append_rows(path, first, last, sender=2):
    with sqlite3.connect(path) as conn:
        conn.executemany(f'INSERT INTO [{TABLE}] VALUES (?,1,100,?,?)',
                         [(i, sender, f'message-{i}') for i in range(first, last + 1)])


def source_fixture(tmp_path, count, include_outgoing=True):
    path = tmp_path / 'messages.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE Name2Id(user_name TEXT)')
        conn.executemany('INSERT INTO Name2Id VALUES (?)', [('self',), ('peer',)])
        conn.execute(f'CREATE TABLE [{TABLE}] (local_id INTEGER PRIMARY KEY, local_type INTEGER, '
                     'create_time INTEGER, real_sender_id INTEGER, message_content TEXT)')
    append_rows(path, 1, count)
    source = object.__new__(WeChatSource)
    source.cache = SimpleNamespace(get=lambda _: str(path))
    source.config = SimpleNamespace(include_outgoing=include_outgoing, include_text=True)
    source.db_dir = '/fixture/account/db_storage'
    source.contact_names = lambda: {}
    source.self_sender = lambda: 'self'
    source._message_tables_for_username = lambda _: [(KEY, TABLE)]
    source.assert_complete_coverage = lambda: None
    source.changed_usernames = lambda state: ({CHAT: 100}, [CHAT] if state.sessions.get(CHAT, 0) < 100 else [])
    source.contact_snapshot = lambda **kwargs: {
        'account': {'accountId': 'account', 'source': 'wechat-local-db', 'platform': 'darwin'},
        'snapshotToken': 'snapshot', 'hasMore': False, 'offset': 0, 'total': 0, 'contacts': [],
    }
    return source, path


def existing_state():
    state = CollectorState(sessions={CHAT: 50})
    state.set_cursor(CURSOR_KEY, 100, 1)
    return state


def accept_all(source, state, size=200):
    accepted = []
    for candidate in source.iter_new_messages(state, [CHAT], size):
        accepted.append(candidate.payload['localId'])
        state.set_cursor(candidate.cursor_key, candidate.cursor.create_time, candidate.cursor.local_id)
    return accepted


def test_missing_cursor_skips_history_persists_tail_and_accepts_future_message(tmp_path):
    source, path = source_fixture(tmp_path, 450)
    state = CollectorState(sessions={CHAT: 50})
    assert accept_all(source, state) == []
    assert state.cursor_for(CURSOR_KEY) == Cursor(100, 450)
    state.save(tmp_path / 'state.json')
    append_rows(path, 451, 451)
    restarted = CollectorState.load(tmp_path / 'state.json')
    assert accept_all(source, restarted) == [451]


def test_empty_new_table_accepts_its_first_future_message(tmp_path):
    source, path = source_fixture(tmp_path, 0)
    state = CollectorState()
    assert accept_all(source, state) == []
    assert state.cursor_for(CURSOR_KEY) == Cursor()
    append_rows(path, 1, 1)
    assert accept_all(source, state) == [1]


@pytest.mark.parametrize('count', [201, 401, 452])
def test_existing_cursor_drains_all_pages_with_same_timestamp(tmp_path, count):
    source, _ = source_fixture(tmp_path, count)
    state = existing_state()
    assert accept_all(source, state) == list(range(2, count + 1))
    assert state.cursor_for(CURSOR_KEY) == Cursor(100, count)
    assert accept_all(source, state) == []


def test_filtered_pages_do_not_hide_later_incoming_messages(tmp_path):
    source, path = source_fixture(tmp_path, 1, include_outgoing=False)
    append_rows(path, 2, 401, sender=1)
    append_rows(path, 402, 402)
    state = existing_state()
    assert accept_all(source, state) == [402]
    assert state.cursor_for(CURSOR_KEY) == Cursor(100, 402)
    append_rows(path, 403, 410, sender=1)
    assert accept_all(source, state) == []
    assert state.cursor_for(CURSOR_KEY) == Cursor(100, 410)


def test_poll_is_bounded_when_new_messages_arrive_during_delivery(tmp_path):
    source, path = source_fixture(tmp_path, 5)
    state = existing_state()
    accepted = []
    for event in source.iter_new_messages(state, [CHAT], 2):
        accepted.append(event.payload['localId'])
        if len(accepted) == 1:
            append_rows(path, 6, 8)
        state.set_cursor(event.cursor_key, event.cursor.create_time, event.cursor.local_id)
    assert accepted == [2, 3, 4, 5]
    assert accept_all(source, state) == [6, 7, 8]


def test_unavailable_snapshot_does_not_create_a_zero_cursor(tmp_path):
    source, _ = source_fixture(tmp_path, 3)
    source.cache.get = lambda _: None
    state = CollectorState()
    with pytest.raises(DatabaseSnapshotError):
        accept_all(source, state)
    assert state.cursor_for(CURSOR_KEY) is None


def test_loop_does_not_advance_session_until_all_pages_are_accepted(tmp_path):
    source, _ = source_fixture(tmp_path, 405)
    cfg = CollectorConfig(state_dir=str(tmp_path))
    existing_state().save(cfg.state_path)
    accepted = []

    def emit(payload, *_args):
        local_id = payload['localId']
        if local_id == 205:
            return BridgeResponse(False, 503, 'retry')
        accepted.append(local_id)
        return BridgeResponse(True, 202, '{}')

    with patch('wechat_bridge_collector.app._load_config', return_value=cfg), \
         patch('wechat_bridge_collector.app.SourceRuntime') as runtime, \
         patch('wechat_bridge_collector.app.QueryMethodServer'), \
         patch('wechat_bridge_collector.app.BridgeClient') as bridge:
        runtime.return_value.source_or_none.return_value = source
        bridge.return_value.emit_message.side_effect = emit
        bridge.return_value.emit_event.return_value = BridgeResponse(True, 202, '{}')
        assert cmd_run(build_parser().parse_args(['run', '--once'])) == 1
        saved = CollectorState.load(cfg.state_path)
        assert saved.sessions == {CHAT: 50}
        assert saved.cursor_for(CURSOR_KEY) == Cursor(100, 204)
        assert accepted == list(range(2, 205))
        bridge.return_value.emit_message.side_effect = lambda payload, *_: (
            accepted.append(payload['localId']) or BridgeResponse(True, 202, '{}'))
        assert cmd_run(build_parser().parse_args(['run', '--once'])) == 0
        saved = CollectorState.load(cfg.state_path)
        assert saved.sessions == {CHAT: 100}
        assert saved.cursor_for(CURSOR_KEY) == Cursor(100, 405)
        assert accepted == list(range(2, 406))
