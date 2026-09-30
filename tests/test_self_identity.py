from contextlib import contextmanager
from types import SimpleNamespace
import sqlite3
import pytest
from wechat_bridge_collector.self_identity import resolve_self_sender, SELF_TRANSFER_TABLE
from wechat_bridge_collector.wechat_source import WeChatSource, direction_for


def database(path, senders, *, missing_sender=False):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY)')
        db.execute(f'CREATE TABLE [{SELF_TRANSFER_TABLE}](local_type INTEGER,real_sender_id INTEGER)')
        for i, sender in enumerate(senders, 10):
            db.execute('INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)',(i,sender))
            db.execute(f'INSERT INTO [{SELF_TRANSFER_TABLE}] VALUES(1,?)',(i,))
        if missing_sender:
            db.execute(f'INSERT INTO [{SELF_TRANSFER_TABLE}] VALUES(1,999)')
    return path


def snapshots(paths):
    @contextmanager
    def snapshot(key):
        yield paths[key]
    return snapshot


def test_resolves_same_identity_across_shards_without_fixed_row_id(tmp_path):
    paths={str(i):database(tmp_path/f'{i}.db',['self-user']) for i in range(3)}
    assert resolve_self_sender(snapshots(paths),list(paths))=='self-user'


@pytest.mark.parametrize('senders,missing',[([],False),(['self-user','other-user'],False),(['filehelper'],False),(['room@chatroom'],False),(['self-user'],True)])
def test_incomplete_or_conflicting_evidence_is_unknown(tmp_path,senders,missing):
    path=database(tmp_path/'db',senders,missing_sender=missing)
    assert resolve_self_sender(snapshots({'db':path}),['db']) is None


def test_conflict_in_another_shard_and_unreadable_shard_are_unknown(tmp_path):
    paths={'a':database(tmp_path/'a',['self-a']),'b':database(tmp_path/'b',['self-b'])}
    assert resolve_self_sender(snapshots(paths),list(paths)) is None
    paths['b']=None
    assert resolve_self_sender(snapshots(paths),list(paths)) is None


def test_identity_recomputed_after_account_switch(tmp_path):
    paths={'account':database(tmp_path/'a',['self-a'])}
    snapshot=snapshots(paths)
    assert resolve_self_sender(snapshot,['account'])=='self-a'
    paths['account']=database(tmp_path/'b',['self-b'])
    assert resolve_self_sender(snapshot,['account'])=='self-b'


def test_direction_compares_exact_resolved_identity():
    assert direction_for(True,'room@chatroom','self','self')=='outgoing'
    assert direction_for(True,'room@chatroom','peer','self')=='incoming'
    assert direction_for(True,'room@chatroom','peer',None)=='unknown'
    assert direction_for(True,'room@chatroom','','self')=='unknown'


def candidate(sender_id, content, include_outgoing=True):
    source=object.__new__(WeChatSource)
    source.db_dir='/test/account/db_storage'
    source.config=SimpleNamespace(include_outgoing=include_outgoing,include_text=True)
    return source._build_candidate((1,1,1700000000,sender_id,content,None),
        'message/message_0.db','Msg_'+'0'*32,'room@chatroom',{}, {10:'self',11:'peer'},'self')


def test_group_outgoing_can_be_filtered_and_peer_is_incoming():
    assert candidate(10,'hello').payload['direction']=='outgoing'
    assert candidate(10,'hello',False) is None
    assert candidate(11,'hello').payload['direction']=='incoming'


def test_content_prefix_is_not_sender_authority():
    assert candidate(0,'peer:\nhello').payload['direction']=='unknown'
    assert candidate(10,'peer:\nhello').payload['direction']=='outgoing'


def test_group_system_messages_never_become_incoming():
    source=object.__new__(WeChatSource)
    source.db_dir='/test/account/db_storage'
    source.config=SimpleNamespace(include_outgoing=True,include_text=True)
    for kind in [10000,10002]:
        event=source._build_candidate((1,kind,1700000000,11,'system text',None),
            'message/message_0.db','Msg_'+'0'*32,'room@chatroom',{}, {11:'peer'},'self')
        assert event.payload['direction']=='unknown'
