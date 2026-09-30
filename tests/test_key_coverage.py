import hashlib
import hmac
import json
import struct
from pathlib import Path
from unittest.mock import patch

import pytest

from wechat_bridge_collector.config import CollectorConfig
from wechat_bridge_collector.key_coverage import check_key_coverage
from wechat_bridge_collector.setup_keys import setup_collector, _extract_macos_keys
from wechat_bridge_collector.wechat_source import WeChatSource, DatabaseSnapshotError
from wechat_bridge_collector.source_runtime import SourceRuntime, SourceNotReady


def write_database(root, name, key='ab' * 32):
    page = bytearray(b'\x19' * 4096)
    mac_key = hashlib.pbkdf2_hmac('sha512', bytes.fromhex(key), bytes(v ^ 0x3a for v in page[:16]), 2, dklen=32)
    page[-64:] = hmac.new(mac_key, page[16:-64] + struct.pack('<I', 1), hashlib.sha512).digest()
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(page)
    return {'enc_key': key}


def ring(root):
    return {name: write_database(root, name) for name in ['contact/contact.db', 'session/session.db', 'message/message_0.db']}


def test_detects_rollover_after_start_and_checks_key_instead_of_presence(tmp_path):
    keys = ring(tmp_path)
    check_key_coverage(str(tmp_path), keys)
    fresh_key = write_database(tmp_path, 'message/message_27.db', 'cd' * 32)
    with pytest.raises(ValueError, match='缺少密钥：message/message_27.db'):
        check_key_coverage(str(tmp_path), keys)
    keys['message/message_27.db'] = {'enc_key': '00' * 32}
    with pytest.raises(ValueError, match='密钥校验失败'):
        check_key_coverage(str(tmp_path), keys)
    keys['message/message_27.db'] = fresh_key
    assert 'message/message_27.db' in check_key_coverage(str(tmp_path), keys)


def test_database_file_list_is_authoritative_and_supports_windows_keys(tmp_path):
    keys = ring(tmp_path)
    keys['message/message_9.db'] = {'enc_key': 'ab' * 32}  # old deleted shard
    (tmp_path / 'message/message_fts.db').write_bytes(b'unrelated')
    (tmp_path / 'message/message_0.db-wal').write_bytes(b'write log')
    assert len(check_key_coverage(str(tmp_path), {k.replace('/', '\\'): v for k, v in keys.items()})) == 3
    write_database(tmp_path, 'message/biz_message_2.db')
    with pytest.raises(ValueError, match='biz_message_2.db'):
        check_key_coverage(str(tmp_path), keys)


def test_truncated_and_changed_key_pages_never_report_ready(tmp_path):
    keys = ring(tmp_path)
    (tmp_path / 'message/message_0.db').write_bytes(b'creating')
    with pytest.raises(ValueError, match='完整页'):
        check_key_coverage(str(tmp_path), keys)
    write_database(tmp_path, 'message/message_0.db', 'cd' * 32)
    with pytest.raises(ValueError, match='密钥校验失败'):
        check_key_coverage(str(tmp_path), keys)


def test_runtime_notices_new_shard_and_blocks_queries_until_reload(tmp_path):
    keys = ring(tmp_path / 'db')
    cfg = CollectorConfig(state_dir=str(tmp_path / 'state'), db_dir=str(tmp_path / 'db'))
    cfg.default_keys_path.parent.mkdir()
    cfg.default_keys_path.write_text(json.dumps(keys))

    class Source:
        def __init__(self, config):
            self.db_dir = config.db_dir
            self.all_keys = json.loads(config.default_keys_path.read_text())
        def assert_source_access(self):
            pass
        assert_complete_coverage = WeChatSource.assert_complete_coverage

    runtime = SourceRuntime(cfg, source_factory=Source)
    assert runtime.initialize()['status'] == 'ready'
    keys['message/message_1.db'] = write_database(tmp_path / 'db', 'message/message_1.db')
    assert runtime.snapshot()['status'] == 'failed'
    with pytest.raises(SourceNotReady, match='message_1.db'):
        runtime.require_source()
    runtime.import_keys({'document': keys})
    assert runtime.snapshot()['status'] == 'ready'
    assert runtime.require_source() is not None


def test_acquire_failure_or_partial_scan_preserves_existing_key_ring(tmp_path):
    keys = ring(tmp_path / 'db')
    cfg = CollectorConfig(state_dir=str(tmp_path / 'state'), db_dir=str(tmp_path / 'db'))
    cfg.default_keys_path.parent.mkdir()
    original = json.dumps(keys).encode()
    cfg.default_keys_path.write_bytes(original)
    write_database(tmp_path / 'db', 'message/message_1.db', 'cd' * 32)

    def partial(_cfg, output):
        assert output != cfg.default_keys_path
        output.write_text(json.dumps({'message/message_0.db': keys['message/message_0.db']}))
    with patch('wechat_bridge_collector.setup_keys.extract_wechat_keys', partial):
        with pytest.raises(ValueError, match='message_1.db'):
            setup_collector(cfg, force=True)
    assert cfg.default_keys_path.read_bytes() == original

    def complete(_cfg, output):
        output.write_text(json.dumps({'message/message_1.db': {'enc_key': 'cd' * 32}}))
    with patch('wechat_bridge_collector.setup_keys.extract_wechat_keys', complete):
        setup_collector(cfg, force=True)
    saved = json.loads(cfg.default_keys_path.read_text())
    assert len(saved) == 4
    check_key_coverage(cfg.db_dir, saved)
    assert cfg.default_keys_path.stat().st_mode & 0o777 == 0o600


def test_macos_permission_failure_does_not_resign_even_as_admin(tmp_path):
    from types import SimpleNamespace
    (tmp_path / 'find_all_keys_macos.c').write_text('')
    cfg = CollectorConfig(wechat_decrypt_dir=str(tmp_path), db_dir=str(tmp_path / 'selected-account'))
    with patch('wechat_bridge_collector.setup_keys._compile_macos_scanner'), \
         patch('wechat_bridge_collector.setup_keys.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='task_for_pid failed: 5')) as run:
        with pytest.raises(RuntimeError, match='未修改签名'):
            _extract_macos_keys(cfg, tmp_path / 'output/all_keys.json')
    assert run.call_count == 1
    assert run.call_args.args[0][-1] == cfg.db_dir
    assert not (tmp_path / 'output/all_keys.json').exists()


def test_http_does_not_return_partial_success_when_shard_appears_during_query(tmp_path):
    import urllib.request
    import urllib.error
    from wechat_bridge_collector.query_server import QueryMethodServer
    keys = ring(tmp_path / 'db')
    cfg = CollectorConfig(state_dir=str(tmp_path / 'state'), db_dir=str(tmp_path / 'db'), method_port=0)
    cfg.default_keys_path.parent.mkdir()
    cfg.default_keys_path.write_text(json.dumps(keys))

    class Source:
        def __init__(self, config):
            self.db_dir = config.db_dir
            self.all_keys = keys
        def assert_source_access(self):
            pass
        assert_complete_coverage = WeChatSource.assert_complete_coverage
        def get_chat_history(self, *args, **kwargs):
            write_database(tmp_path / 'db', 'message/message_1.db')
            return {'messages': [{'text': 'partial old data'}]}

    runtime = SourceRuntime(cfg, source_factory=Source)
    runtime.initialize()
    server = QueryMethodServer(cfg, source_runtime=runtime)
    server.start()
    try:
        request = urllib.request.Request(server.base_url + '/invoke/getChatHistory', data=b'{"conversationId":"test"}')
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        assert raised.value.code == 503
        response = json.loads(raised.value.read())
        assert response['errorCode'] == 'SOURCE_NOT_READY'
        assert response['data'] is None
        assert 'message_1.db' in response['value']
    finally:
        server.stop()
