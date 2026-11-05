"""Invented message/account/record fixtures; no provider calls."""
import pytest
from coord_mesh.attention import AttentionStore


def note(kind='message'):
    return {'v':1,'mid':'synthetic-message','to_user':'synthetic-recipient','to':'worker','kind':kind,'slug':'synthetic-question','body':'Is a directory available?'}

@pytest.mark.parametrize('kind', ['message','query','directive','request'])
def test_read_cursor_does_not_dispose_of_a_request(tmp_path, kind):
    p=tmp_path/'attention.db'
    with AttentionStore(p) as s:
        assert s.ingest('synthetic-peer','synthetic-record',note(kind),'synthetic-recipient')
        s.acknowledge('synthetic-peer','synthetic-record','synthetic-native-receipt')
    with AttentionStore(p) as s:
        assert len(s.pending()) == 1
        assert s.pending()[0]['receiver_evidence']=='synthetic-native-receipt'
        assert not s.ingest('synthetic-peer','synthetic-record',note(kind),'synthetic-recipient')
        assert len(s.pending())==1

@pytest.mark.parametrize('kind',['response','claim','unknown'])
def test_non_requests_do_not_wake_workers(tmp_path,kind):
    with AttentionStore(tmp_path/'attention.db') as s:
        assert not s.ingest('synthetic-peer','synthetic-record',note(kind),'synthetic-recipient')
        assert s.pending()==[]

def test_wrong_recipient_and_retracted_records_are_not_attention(tmp_path):
    with AttentionStore(tmp_path/'attention.db') as s:
        assert not s.ingest('synthetic-peer','synthetic-record',note(),'other-recipient')
        assert not s.ingest('synthetic-peer','synthetic-record',note(),'synthetic-recipient',retracted=True)
        assert s.pending()==[]

def test_changed_record_fails_closed_without_overwriting_original(tmp_path):
    with AttentionStore(tmp_path/'attention.db') as s:
        s.ingest('synthetic-peer','synthetic-record',note(),'synthetic-recipient')
        changed={**note(),'body':'Changed question'}
        with pytest.raises(ValueError,match='conflicting'):
            s.ingest('synthetic-peer','synthetic-record',changed,'synthetic-recipient')
        assert s.pending()[0]['envelope']==note()

def test_reply_claim_survives_restart_and_cannot_be_sent_again(tmp_path):
    p=tmp_path/'attention.db'
    reply={'v':1,'mid':'synthetic-reply','kind':'response','slug':'synthetic-reply-slug','to_user':'synthetic-peer','in_reply_to':'synthetic-message','body':'Verified answer'}
    with AttentionStore(p) as s:
        s.ingest('synthetic-peer','synthetic-record',note(),'synthetic-recipient')
        s.prepare_reply('synthetic-peer','synthetic-record',reply)
        assert s.claim_reply('synthetic-peer','synthetic-record')==reply
    with AttentionStore(p) as s:
        with pytest.raises(ValueError,match='reconciliation'):
            s.claim_reply('synthetic-peer','synthetic-record')
        with pytest.raises(ValueError,match='readback'):
            s.verify_reply('synthetic-peer','synthetic-record',{**reply,'body':'wrong'},'synthetic-outbox-record')
        assert len(s.pending())==1
        s.verify_reply('synthetic-peer','synthetic-record',reply,'synthetic-outbox-record')
        assert s.pending()==[]
        assert s.snapshot()['entries'][0]['reply_record_id']=='synthetic-outbox-record'
        assert not s.ingest('synthetic-peer','synthetic-record',note(),'synthetic-recipient')
        assert s.pending()==[]

def test_reply_cannot_be_prepared_for_another_account_or_message(tmp_path):
    with AttentionStore(tmp_path/'attention.db') as s:
        s.ingest('synthetic-peer','synthetic-record',note(),'synthetic-recipient')
        for reply in [{'to_user':'other-peer','in_reply_to':'synthetic-message'}, {'to_user':'synthetic-peer','in_reply_to':'other-message'}]:
            with pytest.raises(ValueError):
                s.prepare_reply('synthetic-peer','synthetic-record',{'mid':'synthetic-reply','kind':'response',**reply})

def test_missing_table_is_unreadable_not_an_empty_queue(tmp_path):
    import sqlite3
    p=tmp_path/'attention.db'
    with AttentionStore(p) as s:
        s.ingest('synthetic-peer','synthetic-record',note(),'synthetic-recipient')
    with sqlite3.connect(p) as db:
        db.execute('DROP TABLE attention')
    with pytest.raises(ValueError, match='schema'):
        AttentionStore(p)

@pytest.mark.parametrize('change', [{'kind':'response'}, {'to_user':'other-account'}])
def test_changed_binding_of_retained_record_is_a_conflict(tmp_path, change):
    with AttentionStore(tmp_path/'attention.db') as s:
        s.ingest('synthetic-peer','synthetic-record',note(),'synthetic-recipient')
        with pytest.raises(ValueError, match='conflicting'):
            s.ingest('synthetic-peer','synthetic-record',{**note(),**change},'synthetic-recipient')

@pytest.mark.parametrize('operation', ['prepare', 'verify'])
def test_reply_read_check_write_holds_writer_lock(tmp_path, operation):
    import sqlite3
    reply={'v':1,'mid':'synthetic-reply','kind':'response','to_user':'synthetic-peer','in_reply_to':'synthetic-message'}
    with AttentionStore(tmp_path/'attention.db') as s, AttentionStore(tmp_path/'attention.db') as other:
        s.ingest('synthetic-peer','synthetic-record',note(),'synthetic-recipient')
        if operation=='verify':
            s.prepare_reply('synthetic-peer','synthetic-record',reply)
            s.claim_reply('synthetic-peer','synthetic-record')
        other.db.execute('PRAGMA busy_timeout=0')
        original=s._entry
        def entry(*args):
            row=original(*args)
            # A competing writer must not be able to change the row after this read.
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.db.execute("UPDATE attention SET reply_state='prepared' WHERE record_id='synthetic-record'")
            other.db.rollback()
            return row
        s._entry=entry
        if operation=='prepare':
            s.prepare_reply('synthetic-peer','synthetic-record',reply)
        else:
            s.verify_reply('synthetic-peer','synthetic-record',reply,'synthetic-outbox')


def test_existing_legacy_ack_reconciles_without_resend(tmp_path):
    with AttentionStore(tmp_path/'attention.db') as s:
        s.ingest('synthetic-peer','synthetic-record',note('request'),'synthetic-recipient')
        actual={'v':1,'mid':'legacy-mid','kind':'response','to_user':'synthetic-peer','slug':'synthetic-question-ack','body':'In reply to synthetic-message: Received.'}
        s.verify_legacy_reply('synthetic-peer','synthetic-record',actual,'legacy-record','independent authorized source readback')
        s.verify_legacy_reply('synthetic-peer','synthetic-record',actual,'legacy-record','independent authorized source readback')
        assert len(s.pending())==1
        assert s.pending()[0]['reply_state'] is None
        assert 'legacy-record' in s.pending()[0]['receiver_evidence']
        reply={'v':1,'mid':'answer-mid','kind':'response','to_user':'synthetic-peer','in_reply_to':'synthetic-message','body':'Here is the directory.'}
        s.prepare_reply('synthetic-peer','synthetic-record',reply)
        s.claim_reply('synthetic-peer','synthetic-record')
        s.verify_reply('synthetic-peer','synthetic-record',reply,'answer-record')
        assert s.pending()==[]


@pytest.mark.parametrize('change',[{'to_user':'wrong-peer'},{'slug':'other-ack'},{'body':'Unrelated receipt'},{'kind':'request'}])
def test_legacy_ack_cannot_close_an_unrelated_request(tmp_path,change):
    with AttentionStore(tmp_path/'attention.db') as s:
        s.ingest('synthetic-peer','synthetic-record',note('request'),'synthetic-recipient')
        actual={'v':1,'mid':'legacy-mid','kind':'response','to_user':'synthetic-peer','slug':'synthetic-question-ack','body':'In reply to synthetic-message: Received.',**change}
        with pytest.raises(ValueError):s.verify_legacy_reply('synthetic-peer','synthetic-record',actual,'legacy-record','source readback')
        assert len(s.pending())==1


def test_legacy_ack_cannot_overwrite_an_uncertain_send(tmp_path):
    with AttentionStore(tmp_path/'attention.db') as s:
        s.ingest('synthetic-peer','synthetic-record',note('request'),'synthetic-recipient')
        reply={'v':1,'mid':'pending-mid','kind':'response','to_user':'synthetic-peer','slug':'synthetic-question-ack','in_reply_to':'synthetic-message','body':'Pending answer'}
        s.prepare_reply('synthetic-peer','synthetic-record',reply)
        s.claim_reply('synthetic-peer','synthetic-record')
        actual={**reply,'mid':'legacy-mid','body':'In reply to synthetic-message: Received.'}
        del actual['in_reply_to']
        with pytest.raises(ValueError,match='uncertain'):
            s.verify_legacy_reply('synthetic-peer','synthetic-record',actual,'legacy-record','source readback')
        assert s.pending()[0]['reply_state']=='claimed'


def test_historical_midless_directives_have_a_stable_source_bound_reply_target(tmp_path):
    incoming = note('directive')
    del incoming['mid']
    p=tmp_path/'attention.db'
    with AttentionStore(p) as s:
        s.ingest('synthetic-peer','synthetic-record',incoming,'synthetic-recipient')
        reply_to=s.pending()[0]['reply_to']
        s.ingest('other-peer','synthetic-record',incoming,'synthetic-recipient')
        assert s.pending()[0]['reply_to'] != s.pending()[1]['reply_to']
    with AttentionStore(p) as s:
        entry=next(e for e in s.pending() if e['source_user_id']=='synthetic-peer')
        assert entry['reply_to']==reply_to
        assert entry['envelope']==incoming
        reply={'v':1,'mid':'answer-mid','kind':'response','to_user':'synthetic-peer','in_reply_to':reply_to}
        s.prepare_reply('synthetic-peer','synthetic-record',reply)
        s.claim_reply('synthetic-peer','synthetic-record')
        s.verify_reply('synthetic-peer','synthetic-record',reply,'answer-record')
        assert [e['source_user_id'] for e in s.pending()]==['other-peer']


def test_v1_legacy_receipt_disposition_reopens_without_losing_source_evidence(tmp_path):
    import json
    p=tmp_path/'attention.db'
    actual={'v':1,'mid':'legacy-mid','kind':'response','to_user':'synthetic-peer','slug':'synthetic-question-ack','body':'In reply to synthetic-message: Received.'}
    with AttentionStore(p) as s:
        s.ingest('synthetic-peer','synthetic-record',note('request'),'synthetic-recipient')
        s.db.execute("UPDATE attention SET state='responded',receiver_evidence='old-source-readback',reply_json=?,reply_state='verified',reply_record_id='legacy-record'", (json.dumps(actual),))
        s.db.execute('PRAGMA user_version=1')
        s.db.commit()
    with AttentionStore(p) as s:
        entry=s.pending()[0]
        assert entry['reply_state'] is None
        evidence=json.loads(entry['receiver_evidence'])
        assert evidence['envelope']==actual
        assert evidence['record_id']=='legacy-record'
        assert evidence['evidence']=='old-source-readback'


@pytest.mark.parametrize('anchor', ['synthetic-message', '', None])
def test_v1_migration_does_not_reclassify_explicitly_anchored_modern_answers(tmp_path, anchor):
    import json
    p=tmp_path/'attention.db'
    # prepare_reply permits any slug/body: an -ack suffix is not receipt provenance.
    actual={'v':1,'mid':'answer-mid','kind':'response','to_user':'synthetic-peer','slug':'synthetic-question-ack','in_reply_to':anchor,'body':'In reply to synthetic-message: Here is the requested directory.'}
    with AttentionStore(p) as s:
        s.ingest('synthetic-peer','synthetic-record',note('request'),'synthetic-recipient')
        if anchor == 'synthetic-message':
            s.prepare_reply('synthetic-peer','synthetic-record',actual)
            s.claim_reply('synthetic-peer','synthetic-record')
            s.verify_reply('synthetic-peer','synthetic-record',actual,'answer-record')
        else:
            # Even malformed explicit anchors are not evidence of the legacy path.
            s.db.execute("UPDATE attention SET state='responded',reply_json=?,reply_state='verified',reply_record_id='answer-record'", (json.dumps(actual),))
        s.db.execute('PRAGMA user_version=1')
        s.db.commit()
    with AttentionStore(p) as s:
        assert s.pending()==[]
        assert s.snapshot()['entries'][0]['reply']==actual
        assert s.snapshot()['entries'][0]['reply_record_id']=='answer-record'


@pytest.mark.parametrize('version',[1,2])
def test_schema_upgrade_and_reopen_keep_pending_and_claimed_answers_unchanged(tmp_path,version):
    p=tmp_path/'attention.db'
    with AttentionStore(p) as s:
        s.ingest('synthetic-peer','synthetic-record',note('request'),'synthetic-recipient')
        reply={'v':1,'mid':'answer-mid','kind':'response','to_user':'synthetic-peer','slug':'synthetic-question-ack','in_reply_to':'synthetic-message','body':'In reply to synthetic-message: An answer.'}
        s.prepare_reply('synthetic-peer','synthetic-record',reply)
        s.claim_reply('synthetic-peer','synthetic-record')
        s.ingest('synthetic-peer','pending-record',note('query'),'synthetic-recipient')
        s.db.execute(f'PRAGMA user_version={version}')
        s.db.commit()
        before=s.snapshot()
    for _ in range(2):
        with AttentionStore(p) as s:
            assert s.snapshot()==before
            with pytest.raises(ValueError,match='reconciliation'):
                s.claim_reply('synthetic-peer','synthetic-record')
