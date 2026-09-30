import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wechat_bridge_collector.app import build_parser, cmd_run
from wechat_bridge_collector.collection import IndependentLoops
from wechat_bridge_collector.config import CollectorConfig
from wechat_bridge_collector.bridge import BridgeResponse
from wechat_bridge_collector.state import CollectorState, Cursor
from wechat_bridge_collector.wechat_source import DatabaseSnapshotError


class Source:
    def __init__(self):
        self.bootstraps = 0

    def bootstrap_state(self, state, backfill_seconds):
        self.bootstraps += 1
        state.set_cursor('db#table', 10, 1)

    def changed_usernames(self, state):
        return {'chat': 20}, ['chat']

    def iter_new_messages(self, state, changed, batch_size):
        yield SimpleNamespace(
            payload={'text': 'test'}, event_id='message-2', occurred_at=None,
            cursor_key='db#table', cursor=Cursor(20, 2),
        )

    def contact_snapshot(self, **kwargs):
        return {
            'account': {'accountId': 'test', 'source': 'wechat-local-db', 'platform': 'windows'},
            'snapshotToken': 'snapshot-1', 'hasMore': False,
            'contacts': [{'username': 'contact-1', 'displayName': 'Test', 'nickName': '', 'remark': ''}],
        }


def run_once(tmp_path, source, handler):
    cfg = CollectorConfig(state_dir=str(tmp_path))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    cfg.bridge_base_url = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    args = build_parser().parse_args(['run', '--once'])
    try:
        with patch('wechat_bridge_collector.app._load_config', return_value=cfg), \
             patch('wechat_bridge_collector.app.SourceRuntime') as runtime, \
             patch('wechat_bridge_collector.app.QueryMethodServer') as methods:
            runtime.return_value.source_or_none.return_value = source
            result = cmd_run(args)
            methods.return_value.stop.assert_called_once()
        return result, CollectorState.load(cfg.state_path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_blocked_contact_http_does_not_block_message_and_does_not_advance_contact_state(tmp_path):
    contact_started = threading.Event()
    message_delivered = threading.Event()
    ordering = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if body['event'] == 'contactSnapshotChanged':
                ordering.append('contact waiting')
                contact_started.set()
                delivered = message_delivered.wait(3)
                ordering.append('contact failed')
                self.send_response(503 if delivered else 504)
            else:
                if not contact_started.wait(3):
                    self.send_response(504)
                else:
                    ordering.append('message delivered')
                    message_delivered.set()
                    self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, *args):
            pass

    result, state = run_once(tmp_path, Source(), Handler)
    assert result == 1
    assert ordering == ['contact waiting', 'message delivered', 'contact failed']
    assert state.cursor_for('db#table') == Cursor(20, 2)
    assert state.sessions == {'chat': 20}
    assert state.contact_snapshot_token == ''


def test_message_failure_does_not_block_contact_sequence_or_overwrite_its_checkpoint(tmp_path):
    message_started = threading.Event()
    contacts_completed = threading.Event()
    phases = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if body['event'] == 'messageReceived':
                message_started.set()
                assert contacts_completed.wait(3)
                self.send_response(503)
            else:
                assert message_started.wait(3)
                phases.append(body['payload']['phase'])
                if phases[-1] == 'completed':
                    contacts_completed.set()
                self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, *args):
            pass

    result, state = run_once(tmp_path, Source(), Handler)
    assert result == 1
    assert phases == ['started', 'contact', 'completed']
    assert state.contact_snapshot_token == 'snapshot-1'
    assert state.cursor_for('db#table') == Cursor(10, 1)
    assert state.sessions == {}


def test_contact_database_failure_does_not_skip_message_poll(tmp_path):
    source = Source()
    source.contact_snapshot = lambda **kwargs: (_ for _ in ()).throw(DatabaseSnapshotError('busy'))

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    result, state = run_once(tmp_path, source, Handler)
    assert result == 1
    assert state.cursor_for('db#table') == Cursor(20, 2)
    assert state.contact_snapshot_token == ''


def test_unexpected_worker_failure_stops_and_joins_the_other_loop():
    loops = IndependentLoops()
    waiting = threading.Event()
    stopped = threading.Event()

    def messages():
        waiting.set()
        loops.stop.wait(3)
        stopped.set()
        return 0

    def contacts():
        assert waiting.wait(3)
        raise ValueError('broken invariant')

    with pytest.raises(ValueError, match='broken invariant'):
        loops.run(messages, contacts)
    assert stopped.is_set()
    assert not any(t.name in {'wechat-messages', 'wechat-contacts'} for t in threading.enumerate())


def test_contact_retry_backoff_does_not_pause_successive_message_polls(tmp_path):
    cfg = CollectorConfig(state_dir=str(tmp_path), poll_interval_secs=0.01)
    loops = IndependentLoops()
    contact_failed = threading.Event()
    contact_attempts = []
    message_ids = []

    class AdvancingSource(Source):
        def changed_usernames(self, state):
            assert contact_failed.wait(3)
            return super().changed_usernames(state)

        def iter_new_messages(self, state, changed, batch_size):
            next_id = state.cursor_for('db#table').local_id + 1
            yield SimpleNamespace(
                payload={}, event_id=f'message-{next_id}', occurred_at=None,
                cursor_key='db#table', cursor=Cursor(20, next_id),
            )

    def fail_contact(*args):
        contact_attempts.append(1)
        contact_failed.set()
        return BridgeResponse(False, 503, 'unavailable')

    def accept_message(payload, event_id, occurred_at):
        message_ids.append(event_id)
        if len(message_ids) == 3:
            loops.stop.set()
        return BridgeResponse(True, 200, '{}')

    # The real 2-second contact backoff must remain interruptible. All three
    # message polls complete while the contact worker is waiting to retry.
    watchdog = threading.Timer(5, loops.stop.set)
    watchdog.start()
    try:
        with patch('wechat_bridge_collector.app._load_config', return_value=cfg), \
             patch('wechat_bridge_collector.app.SourceRuntime') as runtime, \
             patch('wechat_bridge_collector.app.QueryMethodServer'), \
             patch('wechat_bridge_collector.app.IndependentLoops', return_value=loops), \
             patch('wechat_bridge_collector.app.BridgeClient') as bridge:
            runtime.return_value.source_or_none.return_value = AdvancingSource()
            bridge.return_value.emit_event.side_effect = fail_contact
            bridge.return_value.emit_message.side_effect = accept_message
            assert cmd_run(build_parser().parse_args(['run'])) == 0
    finally:
        watchdog.cancel()
        watchdog.join()
    assert message_ids == ['message-2', 'message-3', 'message-4']
    assert contact_attempts == [1]
    assert CollectorState.load(cfg.state_path).cursor_for('db#table') == Cursor(20, 4)


def test_restart_retries_unacknowledged_message_without_bootstrapping(tmp_path):
    cfg = CollectorConfig(state_dir=str(tmp_path))
    saved = CollectorState(contact_snapshot_token='snapshot-1')
    saved.set_cursor('db#table', 10, 1)
    saved.save(cfg.state_path)
    source = Source()
    with patch('wechat_bridge_collector.app._load_config', return_value=cfg), \
         patch('wechat_bridge_collector.app.SourceRuntime') as runtime, \
         patch('wechat_bridge_collector.app.QueryMethodServer'), \
         patch('wechat_bridge_collector.app.BridgeClient') as bridge:
        runtime.return_value.source_or_none.return_value = source
        bridge.return_value.emit_message.return_value = BridgeResponse(False, 503, 'unavailable')
        args = build_parser().parse_args(['run', '--once'])
        assert cmd_run(args) == 1
        assert CollectorState.load(cfg.state_path).cursor_for('db#table') == Cursor(10, 1)
        bridge.return_value.emit_message.return_value = BridgeResponse(True, 200, '{}')
        assert cmd_run(args) == 0
        assert source.bootstraps == 0
        assert bridge.return_value.emit_message.call_count == 2
        bridge.return_value.emit_event.assert_not_called()
    assert CollectorState.load(cfg.state_path).cursor_for('db#table') == Cursor(20, 2)


@pytest.mark.parametrize('source_ready', [False, True])
def test_incomplete_initialization_exits_once_without_persisting_empty_state(tmp_path, source_ready):
    cfg = CollectorConfig(state_dir=str(tmp_path))
    source = Source()
    source.bootstrap_state = lambda *args, **kwargs: (_ for _ in ()).throw(DatabaseSnapshotError('busy'))
    with patch('wechat_bridge_collector.app._load_config', return_value=cfg), \
         patch('wechat_bridge_collector.app.SourceRuntime') as runtime, \
         patch('wechat_bridge_collector.app.QueryMethodServer'), \
         patch('wechat_bridge_collector.app.BridgeClient') as bridge:
        runtime.return_value.source_or_none.return_value = source if source_ready else None
        assert cmd_run(build_parser().parse_args(['run', '--once'])) == 1
        bridge.return_value.emit_event.assert_not_called()
        bridge.return_value.emit_message.assert_not_called()
    assert not cfg.state_path.exists()
