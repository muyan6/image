"""Offline real-SQLite author rewards: delivery, aliases, recovery and policy."""
import copy
import json
from pathlib import Path
import sqlite3
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_workflow import WorkflowTests,m,OUTPUT,initial_connections
from test_cloud_pipeline import CloudTests
from gateway_async import AsyncImages
from experience_store import store_for
from template_share_rewards import (DEFAULT_TEMPLATE_SHARING,reward_snapshot,
    reward_summary,recover_share_rewards)
from user_store import UserStore


def reviewed(author='template_author',template_id='user_template',revision=1):
    return {'id':template_id,'name':'审核模板','source':'user','author_openid':author,
            'source_revision':revision,'published_at':1,'private':True,'enabled':True,
            'engine':'light','price':40,'prompt':'fixture prompt','text_fields':[]}


class RewardTests(WorkflowTests):
    def setUp(self):
        super().setUp()
        m.users.ensure_user('template_author',welcome_balance=0)
        m.users.admin_adjust_balance('sample_user',balance=1000)
        self.policy = copy.deepcopy(DEFAULT_TEMPLATE_SHARING)

    def submit(self,jid,template=None,policy=None,consumer='sample_user',amount=40,free=False):
        template = template or reviewed(); policy = policy or self.policy
        snapshot = reward_snapshot(template,policy)
        m.users.reserve_job(consumer,jid,amount,{},free,template_snapshot=snapshot)
        m.jobs.create(jid,openid=consumer,status='processing',cloud_pipeline=True,template_id=template['id'],template_snapshot=snapshot)
        m.users.confirm_job(jid,'fixture submission')
        return snapshot

    def complete(self,jid):
        m.jobs.update(jid,status='succeeded',completed_at=time.time())
        m.users.complete_charge(jid)

    def receipt(self,jid):
        cur = m.users._conn.execute('SELECT * FROM template_reward_jobs WHERE job_id=?',(jid,)); row=cur.fetchone()
        return dict(zip([field[0] for field in cur.description],row))

    def link(self,alias,owner):
        m.users.ensure_user(alias,account_type='web',welcome_balance=0)
        with m.users._lock,m.users._conn:
            m.users._conn.execute('INSERT INTO account_aliases VALUES(?,?,?,?,?)',(alias,owner,time.time(),'fixture_union','fixture_app'))

    def test_default_config_validated_detached_and_uploaded_switch_independent(self):
        self.assertEqual(m.settings.template_sharing(),DEFAULT_TEMPLATE_SHARING)
        detached=m.settings.template_sharing();detached['reward_amount']=999
        self.assertEqual(m.settings.template_sharing()['reward_amount'],40)
        m.settings.update({'template_sharing':{'internal_token':'fixture-private-token'}})
        self.assertNotIn('internal_token',m.settings.template_sharing())
        self.assertNotIn('fixture-private-token',json.dumps(self.client.get('/api/config').json()))
        for change in ({'reward_amount':-1},{'reward_amount':10000},{'reward_amount':True},
                       {'reward_mode':'unlimited'},{'reward_enabled':'true'},{'author_daily_cap':-1},
                       {'global_daily_cap':True},{'enabled':1}):
            with self.assertRaises(ValueError):m.settings.update({'template_sharing':change})
        self.submit('switch',policy={**self.policy,'enabled':False})
        self.complete('switch');self.assertEqual(m.users.get_balance('template_author'),40)

    def test_real_success_awards_author_not_consumer_exactly_once(self):
        self.submit('one')
        self.assertEqual(m.users.get_balance('sample_user'),960)
        self.assertEqual(m.users.get_balance('template_author'),0)
        self.complete('one')
        for _ in range(4):m.users.complete_charge('one')
        self.assertEqual(m.users.get_balance('sample_user'),960)
        self.assertEqual(m.users.get_balance('template_author'),40)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM template_reward_awards').fetchone()[0],1)
        self.assertEqual(self.receipt('one')['status'],'awarded')
        self.assertEqual(self.receipt('one')['delivery_confirmed'],1)

    def test_first_user_default_all_success_opt_in_and_revision_not_reset(self):
        for jid,revision in (('first',1),('second',1),('edited',2)):
            self.submit(jid,reviewed(revision=revision));self.complete(jid)
        self.assertEqual(m.users.get_balance('template_author'),40)
        self.assertEqual(self.receipt('edited')['reason'],'not_first_user')
        for jid in ('all1','all2'):
            self.submit(jid,policy={**self.policy,'reward_mode':'all_success'});self.complete(jid)
        self.assertEqual(m.users.get_balance('template_author'),120)

    def test_self_use_and_linked_consumer_author_are_excluded(self):
        m.users.admin_adjust_balance('template_author',balance=100)
        self.link('web_author','template_author')
        self.submit('self',consumer='template_author');self.complete('self')
        self.submit('alias_self',consumer='web_author');self.complete('alias_self')
        self.assertEqual(m.users.get_balance('template_author'),20)
        self.assertEqual(self.receipt('self')['reason'],'self_use')
        self.assertEqual(self.receipt('alias_self')['reason'],'self_use')
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM template_reward_awards').fetchone()[0],0)

    def test_first_user_alias_history_and_author_alias_balance_share(self):
        self.link('web_buyer','sample_user');self.link('web_author','template_author')
        self.submit('alias_first',reviewed(author='web_author'),consumer='web_buyer');self.complete('alias_first')
        self.submit('canonical_second',reviewed(),consumer='sample_user');self.complete('canonical_second')
        self.assertEqual(m.users.get_balance('template_author'),40)
        self.assertEqual(self.receipt('canonical_second')['reason'],'not_first_user')
        self.assertEqual(reward_summary(m.users,'web_author'),reward_summary(m.users,'template_author'))

    def test_new_binding_before_delivery_excludes_self_and_merges_first_use(self):
        m.users.ensure_user('web_buyer',account_type='web',welcome_balance=100)
        self.submit('before_link',consumer='web_buyer')
        self.link('web_buyer','template_author')
        self.complete('before_link')
        self.assertEqual(self.receipt('before_link')['reason'],'self_use')
        self.assertEqual(m.users.get_balance('template_author'),0)

    def test_disabled_zero_free_failed_cancelled_and_banned_no_awards(self):
        for jid,options in (('disabled',{'policy':{**self.policy,'reward_enabled':False}}),
                            ('zero_reward',{'policy':{**self.policy,'reward_amount':0}}),
                            ('zero_charge',{'amount':0}),('free_mode',{'free':True,'amount':40})):
            self.submit(jid,reviewed(template_id=jid),**options);self.complete(jid)
        self.submit('failed',reviewed(template_id='failed'))
        m.jobs.update('failed',status='failed');m.users.refund_job('sample_user','failed')
        self.submit('cancelled',reviewed(template_id='cancelled'));m.users.settle_cancelled_charge('cancelled')
        m.users.complete_charge('cancelled')
        self.assertEqual(m.users._conn.execute("SELECT state FROM job_charges WHERE job_id='cancelled'").fetchone()[0],'cancelled_charged')
        m.users.set_banned('template_author',True)
        self.submit('banned',reviewed(template_id='banned'));self.complete('banned')
        self.assertEqual(m.users.get_balance('template_author'),0)
        self.assertEqual(self.receipt('banned')['reason'],'author_banned')
        self.assertEqual(self.receipt('cancelled')['reason'],'cancelled_charged')
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM template_reward_awards').fetchone()[0],0)

    def test_policy_frozen_and_invalid_snapshot_rolls_back_reservation(self):
        original=reviewed();snapshot=self.submit('frozen',original)
        original['author_openid']='other_user';snapshot['share_reward_policy']['reward_amount']=999
        m.settings.update({'template_sharing':{'reward_enabled':False,'reward_amount':1,'reward_mode':'all_success'}})
        self.complete('frozen');self.assertEqual(m.users.get_balance('template_author'),40)
        saved=json.loads(self.receipt('frozen')['snapshot'])
        self.assertEqual(saved['share_reward_policy']['reward_amount'],40)
        before=m.users.get_balance('sample_user')
        for bad in ({**reviewed(),'author_openid':''},{**reviewed(),'source_revision':0},
                    {**reviewed(),'share_reward_policy':{'reward_amount':40}}):
            with self.assertRaises(ValueError):m.users.reserve_job('sample_user','invalid',40,{},template_snapshot=bad)
            self.assertEqual(m.users.get_balance('sample_user'),before)
            self.assertIsNone(m.users.charged_amount('invalid'))

    def test_author_global_daily_caps_zero_and_no_partial_award(self):
        policy={**self.policy,'reward_mode':'all_success','author_daily_cap':80,'global_daily_cap':120}
        for jid in ('cap1','cap2','cap3'):
            self.submit(jid,policy=policy);self.complete(jid)
        self.assertEqual(m.users.get_balance('template_author'),80)
        self.assertEqual(self.receipt('cap3')['reason'],'author_daily_cap')
        m.users.ensure_user('second_author',welcome_balance=0)
        for jid in ('global1','global2'):
            self.submit(jid,reviewed(author='second_author',template_id='second_template'),policy=policy);self.complete(jid)
        self.assertEqual(m.users.get_balance('second_author'),40)
        self.assertEqual(self.receipt('global2')['reason'],'global_daily_cap')
        self.submit('zero_cap',reviewed(template_id='zero_cap'),policy={**policy,'author_daily_cap':0})
        self.complete('zero_cap');self.assertEqual(self.receipt('zero_cap')['reason'],'author_daily_cap')

    def test_same_consumer_concurrent_first_claim_and_duplicate_completion(self):
        policy={**self.policy,'author_daily_cap':4000,'global_daily_cap':4000}
        for index in range(12):self.submit('concurrent%02d'%index,policy=policy)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(self.complete,['concurrent%02d'%index for index in range(12)]))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(m.users.complete_charge,['concurrent00']*16))
        self.assertEqual(m.users.get_balance('template_author'),40)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM template_reward_awards').fetchone()[0],1)

    def test_two_sqlite_connections_serialize_first_use_and_global_cap(self):
        policy={**self.policy,'reward_mode':'all_success','global_daily_cap':40}
        self.submit('connection1',policy=policy);self.submit('connection2',policy=policy)
        other=UserStore(str(Path(m.users._path).parent))
        try:
            for jid in ('connection1','connection2'):m.jobs.update(jid,status='succeeded',completed_at=time.time())
            with ThreadPoolExecutor(max_workers=2) as pool:
                one=pool.submit(m.users.complete_charge,'connection1');two=pool.submit(other.complete_charge,'connection2')
                one.result();two.result()
            self.assertEqual(m.users.get_balance('template_author'),40)
            self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM template_reward_awards').fetchone()[0],1)
            for jid in ('connection3','connection4'):
                self.submit(jid,reviewed(template_id='first_connections'))
                m.jobs.update(jid,status='succeeded',completed_at=time.time())
            with ThreadPoolExecutor(max_workers=2) as pool:
                one=pool.submit(m.users.complete_charge,'connection3');two=pool.submit(other.complete_charge,'connection4')
                one.result();two.result()
            self.assertEqual(m.users.get_balance('template_author'),80)
            self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM template_reward_awards').fetchone()[0],2)
        finally:other.close()

    def test_reward_failure_atomic_then_restart_reconciles_once(self):
        self.submit('recover')
        m.jobs.update('recover',status='succeeded',completed_at=time.time())
        db=m.users._conn
        db.execute("CREATE TRIGGER fail_reward BEFORE INSERT ON template_reward_awards BEGIN SELECT RAISE(ABORT,'FIXTURE_REWARD_FAILURE'); END;")
        with self.assertRaises(sqlite3.IntegrityError):m.users.complete_charge('recover')
        self.assertEqual(m.users.get_balance('template_author'),0)
        self.assertEqual(db.execute("SELECT state FROM job_charges WHERE job_id='recover'").fetchone()[0],'submitted')
        self.assertEqual(self.receipt('recover')['delivery_confirmed'],0)
        db.execute('DROP TRIGGER fail_reward');db.commit()
        directory=Path(m.users._path).parent;m.users.close();m.users=UserStore(str(directory))
        m.users.reconcile_charges({'recover':'succeeded'});m.users.reconcile_charges({'recover':'succeeded'})
        self.assertEqual(m.users.get_balance('template_author'),40)

    def test_overflow_pending_then_receipt_recovers_after_job_retention(self):
        m.users.admin_adjust_balance('template_author',balance=2**63-1)
        self.submit('overflow');self.complete('overflow')
        self.assertEqual(m.users.get_balance('template_author'),2**63-1)
        self.assertEqual(self.receipt('overflow')['status'],'pending')
        self.assertEqual(self.receipt('overflow')['reason'],'author_balance_overflow')
        m.users.admin_adjust_balance('template_author',balance=100)
        self.assertEqual(recover_share_rewards(m.users,{}),1)
        self.assertEqual(recover_share_rewards(m.users,{}),0)
        self.assertEqual(m.users.get_balance('template_author'),140)

    def test_refund_creates_visible_debt_without_debiting_unrelated_balance(self):
        self.submit('refund');self.complete('refund')
        m.users.admin_adjust_balance('template_author',balance=7)
        before=m.users.get_balance('sample_user')
        m.users.refund_job('sample_user','refund');m.users.refund_job('sample_user','refund')
        self.assertEqual(m.users.get_balance('sample_user'),before+40)
        self.assertEqual(m.users.get_balance('template_author'),7)
        summary=reward_summary(m.users,'template_author')
        self.assertEqual(summary['recovery_due'],40);self.assertTrue(summary['reward_frozen'])
        self.assertEqual(summary['recovery_policy'],'future_author_rewards_only')
        rows=store_for(m.users).credit_records('template_author',limit=100)['items']
        recovery=next(row for row in rows if row['kind']=='template_reward_reversal_pending')
        self.assertEqual((recovery['amount'],recovery['recovery_amount']),(0,40))
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM template_reward_recovery').fetchone()[0],1)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM template_reward_ledger WHERE action=\'reversal_pending\'').fetchone()[0],1)

    def test_future_rewards_offset_debt_and_refunded_offsets_restore_full_entitlement(self):
        policy={**self.policy,'reward_mode':'all_success'}
        self.submit('old',policy=policy);self.complete('old');m.users.refund_job('sample_user','old')
        m.users.admin_adjust_balance('template_author',balance=7)
        self.submit('offset',policy=policy);self.complete('offset')
        self.assertEqual(m.users.get_balance('template_author'),7)
        self.assertEqual(reward_summary(m.users,'template_author')['recovery_due'],0)
        offset=m.users._conn.execute("SELECT credited_amount,offset_amount FROM template_reward_awards WHERE job_id='offset'").fetchone()
        self.assertEqual(offset,(0,40))
        m.users.refund_job('sample_user','offset')
        self.assertEqual(reward_summary(m.users,'template_author')['recovery_due'],40)
        self.submit('partial',policy={**policy,'reward_amount':25});self.complete('partial')
        self.assertEqual(reward_summary(m.users,'template_author')['recovery_due'],15)
        self.assertEqual(m.users.get_balance('template_author'),7)
        self.submit('clear',policy=policy);self.complete('clear')
        self.assertEqual(m.users.get_balance('template_author'),32)
        self.assertEqual(reward_summary(m.users,'template_author')['recovery_due'],0)

    def test_refund_atomic_failure_rolls_back_consumer_credit_and_recovery_metadata(self):
        self.submit('atomic_refund');self.complete('atomic_refund');before=m.users.get_balance('sample_user')
        db=m.users._conn
        db.execute("CREATE TRIGGER fail_recovery BEFORE INSERT ON template_reward_recovery BEGIN SELECT RAISE(ABORT,'FIXTURE_RECOVERY_FAILURE'); END;")
        with self.assertRaises(sqlite3.IntegrityError):m.users.refund_job('sample_user','atomic_refund')
        self.assertEqual(m.users.get_balance('sample_user'),before)
        self.assertEqual(db.execute("SELECT refunded FROM job_charges WHERE job_id='atomic_refund'").fetchone()[0],0)
        self.assertEqual(reward_summary(m.users,'template_author')['recovery_due'],0)
        db.execute('DROP TRIGGER fail_recovery');db.commit()
        m.users.refund_job('sample_user','atomic_refund')
        self.assertEqual(reward_summary(m.users,'template_author')['recovery_due'],40)

    def test_concurrent_completion_refund_has_no_duplicate_or_lost_recovery(self):
        policy={**self.policy,'reward_mode':'all_success','author_daily_cap':4000,'global_daily_cap':4000}
        for index in range(12):
            jid='race%02d'%index;self.submit(jid,policy=policy)
            m.jobs.update(jid,status='succeeded',completed_at=time.time())
        with ThreadPoolExecutor(max_workers=8) as pool:
            work=[]
            for index in range(12):
                jid='race%02d'%index
                work.extend((pool.submit(m.users.complete_charge,jid),pool.submit(m.users.refund_job,'sample_user',jid)))
            for future in work:future.result()
        self.assertEqual(m.users.get_balance('sample_user'),1000)
        summary=reward_summary(m.users,'template_author')
        self.assertEqual(summary['recovery_due'],m.users.get_balance('template_author'))
        self.assertEqual(summary['reward_earned'],summary['reward_reversed'])
        self.assertGreaterEqual(m.users.get_balance('template_author'),0)
        self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM job_charges WHERE refunded=1').fetchone()[0],12)

    def test_credit_count_pagination_author_ownership_and_cache_refresh(self):
        store=store_for(m.users)
        initial=store.credit_record_count('template_author');self.assertEqual(initial,0)
        self.submit('history');self.complete('history')
        self.assertEqual(store.credit_record_count('template_author'),1)
        creator=store.credit_records('template_author')['items'][0]
        self.assertEqual((creator['amount'],creator['reward_amount'],creator['job_id']),(40,40,'history'))
        consumer=store.credit_records('sample_user',limit=100)['items']
        self.assertFalse(any(row['kind'].startswith('template_reward_') for row in consumer))
        m.users.refund_job('sample_user','history')
        self.assertEqual(store.credit_record_count('template_author'),2)
        page=store.credit_records('template_author',limit=1)
        self.assertEqual(page['items'][0]['amount'],0);self.assertTrue(page['has_more'])

    def test_local_pipeline_registration_and_durable_success_reward_hooks(self):
        template=reviewed();source=self.d/'local-source.jpg';source.write_bytes(self.image())
        with patch.object(m.pool,'submit'),patch.object(m,'_get_client',return_value=type('FixtureClient',(),{'configured':True})()):
            jid=m._register_job('sample_user','light','',str(source),'.jpg',template=template)['job_id']
        self.assertEqual(self.receipt(jid)['status'],'pending')
        self.assertEqual(m.jobs.get(jid)['template_snapshot']['share_reward_policy']['reward_amount'],40)
        original_image=self.image()
        class Provider:
            configured=True
            def enhance(self,source,output,**kwargs):Path(output).write_bytes(original_image)
        with patch.object(m.settings,'chain',return_value=['worldcodes']), \
                patch.object(m.settings,'provider_enabled',return_value=True), \
                patch.object(m,'_get_client',return_value=Provider()), \
                patch.object(m,'_moderate_or_reject',return_value=None):
            m._run_pipeline(jid,'light','',template=template)
        self.assertEqual(m.jobs.get(jid)['status'],'succeeded')
        self.assertEqual(m.users.get_balance('template_author'),40)
        m.users.complete_charge(jid);self.assertEqual(m.users.get_balance('template_author'),40)

    def test_personal_api_summary_and_manual_site_restrictions_remain(self):
        self.submit('visible');self.complete('visible');m.users.refund_job('sample_user','visible')
        headers={'Authorization':'Bearer '+m.user_token('template_author')}
        response=self.client.get('/api/me',headers=headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['template_share_rewards']['recovery_due'],40)
        from web_accounts import store_for as site_accounts
        site,token,csrf=site_accounts(m.users).register('author_site_fixture','fixture-password','127.0.0.1')
        self.submit('site_author',reviewed(author=site,template_id='site_template'))
        self.complete('site_author');self.assertEqual(m.users.get_balance(site),40)
        earned=self.client.post('/api/me/earn',json={'kind':'checkin'},headers={
            'Cookie':'site_session='+token,'X-Site-Request':'1','X-Site-CSRF':csrf,'Origin':'https://image.myil.top'})
        self.assertEqual(earned.status_code,403)

    def test_old_system_template_calls_remain_compatible_and_purge_new_records(self):
        m.users.reserve_job('sample_user','ordinary',40,{})
        m.users.confirm_job('ordinary','fixture');m.users.complete_charge('ordinary')
        self.assertIsNone(m.users._conn.execute("SELECT 1 FROM template_reward_jobs WHERE job_id='ordinary'").fetchone())
        self.submit('purge');self.complete('purge');m.users.refund_job('sample_user','purge')
        m.users.begin_purge()
        try:m.users.purge_all_accounts()
        finally:m.users.end_purge()
        for table in ('template_reward_jobs','template_reward_uses','template_reward_awards','template_reward_recovery',
                      'template_reward_offsets','template_reward_ledger'):
            self.assertEqual(m.users._conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)


class CloudRewardTests(CloudTests):
    def test_cloud_approved_user_template_snapshots_then_success_awards_once(self):
        m.users.ensure_user('template_author',welcome_balance=0)
        jid=self.new(template=reviewed())
        job=m.jobs.get(jid)
        self.assertEqual(job['template_snapshot']['share_reward_policy']['reward_amount'],40)
        self.step(jid,'prepare')
        with patch.object(AsyncImages,'submit',return_value='fixture_share_task'):self.step(jid,'submit')
        m.settings.update({'template_sharing':{'reward_enabled':False,'reward_amount':1}})
        job=self.completed(jid);self.assertEqual(job['status'],'succeeded',job.get('error'))
        self.assertEqual(m.users.get_balance('template_author'),40)
        m.users.complete_charge(jid);self.assertEqual(m.users.get_balance('template_author'),40)


if __name__ == '__main__':
    cases=[RewardTests(name) for name in RewardTests.__dict__ if name.startswith('test_')]
    cases += [CloudRewardTests(name) for name in CloudRewardTests.__dict__ if name.startswith('test_')]
    names=[case._testMethodName for case in cases]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(cases))
    failed={test._testMethodName for test,_ in result.failures+result.errors}
    (OUTPUT/'template_share_reward_results.json').write_text(json.dumps({'cases':[{'case':name,'passed':name not in failed} for name in names]},
        ensure_ascii=False,indent=2),encoding='utf-8')
    for connection in initial_connections:
        try:connection.close()
        except Exception:pass
    print('TEMPLATE_SHARE_REWARDS_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
