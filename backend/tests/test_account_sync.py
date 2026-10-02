"""Offline real-SQLite account-union invariants, no live accounts or network."""
from pathlib import Path
import json, os, sys, tempfile, time, unittest
from concurrent.futures import ThreadPoolExecutor

ROOT=Path(os.environ.get('REVIEW_ROOT',Path(__file__).resolve().parents[2])).resolve()
OUTPUT=Path(os.environ.get('REVIEW_OUTPUT',ROOT/'audit/account-sync-tests')).resolve();OUTPUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT/'backend'))
from user_store import UserStore
from web_accounts import WebAccounts
from account_links import bind_verified, register_verified, mini_for_unionid, owns, aliases, canonical
from experience_store import ExperienceStore
from community_submissions import SubmissionStore
from community_interactions import InteractionStore
from payment_store import PaymentStore


class AccountSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.users=UserStore(self.tmp.name)
        self.site=WebAccounts(self.users)
        self.web,_,_=self.site.register('registered_web','fixture-password-2026','127.0.0.1')
        self.wx='mini_verified_user';self.other='unrelated_user'
        self.users.ensure_user(self.wx,account_type='wechat',app_id='mini_app',welcome_balance=75)
        self.users.ensure_user(self.other,account_type='wechat',welcome_balance=11)
        register_verified(self.users,'mini_app',self.wx,'same_verified_union','mini')
    def tearDown(self):
        self.users.close();self.tmp.cleanup()
    def bind(self):return bind_verified(self.users,self.web,self.wx,'same_verified_union','official_app')
    def raw_balance(self,identity):return self.users._conn.execute('SELECT balance FROM users WHERE openid=?',(identity,)).fetchone()[0]
    def photo_job(self,jid):return {'id':jid,'result_cos':'results/'+jid+'.jpg','orig_cos':'origins/'+jid+'.jpg','template_id':'template','template_name':'风格','quality':'light'}
    def content(self,title='我的作品'):return {'title':title,'story':'真实使用故事','category':'all','share_original':False,'author_name':'创作者'}
    def test_explicit_registered_only_link_no_guess_no_gifts(self):
        self.assertEqual(self.users.get_balance(self.web),0)
        with self.assertRaises(ValueError):bind_verified(self.users,self.web,self.wx,'different_union','official_app')
        self.users.ensure_user('web-legacy-visitor',account_type='web',welcome_balance=999)
        with self.assertRaises(ValueError):bind_verified(self.users,'web-legacy-visitor',self.wx,'same_verified_union','official_app')
        self.assertEqual(self.users.get_balance(self.wx),75);self.assertEqual(aliases(self.users,self.web),(self.web,))
    def test_balance_transfer_atomic_repeat_once_and_alias_ownership(self):
        self.users.admin_adjust_balance(self.web,delta=22)
        result=self.bind();self.assertEqual(result['transferred_amount'],22)
        self.assertEqual(self.users.get_balance(self.wx),97);self.assertEqual(self.users.get_balance(self.web),97)
        self.assertEqual(self.raw_balance(self.web),0)
        self.assertTrue(owns(self.users,self.web,self.wx));self.assertFalse(owns(self.users,self.other,self.wx))
        self.assertEqual(self.bind()['transferred_amount'],0);self.assertEqual(self.users.get_balance(self.wx),97)
        self.assertEqual(self.users._conn.execute("SELECT COUNT(*) FROM audit WHERE action='account_balance_transfer'").fetchone()[0],2)
    def test_concurrent_binding_transfers_exactly_once(self):
        self.users.admin_adjust_balance(self.web,delta=31)
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(lambda _:self.bind(),range(16)))
        self.assertEqual(sum(row['transferred_amount'] for row in results),31)
        self.assertEqual(self.users.get_balance(self.wx),106);self.assertEqual(self.raw_balance(self.web),0)
    def test_conflicting_binding_and_bans_preserve_money(self):
        self.users.set_banned(self.web,True)
        with self.assertRaises(ValueError):self.bind()
        self.users.set_banned(self.web,False);self.bind()
        second,_,_=self.site.register('second_web','fixture-password-2026','127.0.0.2')
        with self.assertRaises(ValueError):bind_verified(self.users,second,self.wx,'same_verified_union','official_app')
        self.assertEqual(self.users.get_balance(second),0);self.assertEqual(self.users.get_balance(self.wx),75)
    def test_verified_identity_conflicts_do_not_overwrite_proofs(self):
        with self.assertRaises(ValueError):register_verified(self.users,'mini_app',self.wx,'other_union','mini')
        self.assertEqual(mini_for_unionid(self.users,'same_verified_union','mini_app'),self.wx)
        self.assertIsNone(mini_for_unionid(self.users,'same_verified_union','another_app'))
    def test_cross_alias_debit_old_charge_refund_and_manual_adjustment(self):
        self.users.admin_adjust_balance(self.web,delta=60)
        self.users.reserve_job(self.web,'old_web_job',20,{'per_minute':0,'daily':0},False)
        self.users.confirm_job('old_web_job','before binding')
        self.bind();self.assertEqual(self.users.get_balance(self.wx),115)
        self.users.refund_job(self.web,'old_web_job');self.assertEqual(self.users.get_balance(self.wx),135)
        self.users.refund_job(self.wx,'old_web_job');self.assertEqual(self.users.get_balance(self.wx),135)
        self.users.reserve_job(self.web,'new_shared_job',10,{'per_minute':0,'daily':0},False)
        self.assertEqual(self.users.get_balance(self.wx),125);self.assertEqual(self.raw_balance(self.web),0)
        self.users.admin_adjust_balance(self.web,balance=130);self.assertEqual(self.users.get_balance(self.wx),130)
        self.assertEqual(self.users._conn.execute("SELECT detail FROM audit WHERE action='admin_adjust_balance' ORDER BY id DESC LIMIT 1").fetchone()[0],'delta=5 balance=130')
    def test_shared_credit_history_is_complete_and_read_only(self):
        store=ExperienceStore(self.users)
        self.users.admin_adjust_balance(self.web,delta=50)
        self.users.reserve_job(self.web,'web_charge',7,{'per_minute':0,'daily':0},False)
        self.users.reserve_job(self.wx,'wx_charge',9,{'per_minute':0,'daily':0},False)
        self.bind();self.users.refund_job(self.web,'web_charge')
        before=self.users._conn.total_changes
        web=store.credit_records(self.web);wx=store.credit_records(self.wx)
        self.assertEqual(web['items'],wx['items']);self.assertEqual(self.users._conn.total_changes,before)
        self.assertEqual(sorted(row['amount'] for row in wx['items']),[-9,-7,7,50])
        self.assertFalse(any(row.get('job_id')=='unrelated' for row in wx['items']))
    def test_preferences_merge_and_unfavorite_removes_all_alias_flags(self):
        store=ExperienceStore(self.users)
        store.preference(self.web,'same_template',True,recent=True);store.preference(self.wx,'same_template',True,recent=True)
        store.preference(self.web,'web_only',True);self.bind()
        self.assertEqual(set(store.preferences(self.wx)['template_favorites']),{'same_template','web_only'})
        store.preference(self.wx,'same_template',False)
        self.assertEqual(store.preferences(self.web)['template_favorites'],['web_only'])
        self.assertEqual(store.preferences(self.wx)['recent_templates'],['same_template'])
    def test_submission_receipt_and_settlement_remain_findable_after_link(self):
        store=ExperienceStore(self.users)
        row,_=store.claim(self.web,'original_request',{'prompt':'fixture'},'text')
        self.users.reserve_job(self.web,row['job_id'],0,{'per_minute':0,'daily':0},False)
        self.bind();store.settle(self.wx,'original_request','accepted')
        self.assertEqual(store.submission(self.wx,'original_request')['job_id'],row['job_id'])
        self.assertEqual(store.settlement(self.wx,row['job_id'])['charged_amount'],0)
        duplicate,fresh=store.claim(self.wx,'original_request',{'prompt':'fixture'},'text')
        self.assertFalse(fresh);self.assertEqual(duplicate['job_id'],row['job_id'])
    def test_community_submissions_rewards_and_withdrawals_share_owner(self):
        store=SubmissionStore(self.users)
        row,_=store.reserve(self.web,self.photo_job('photo_job'),self.content());store.finish(row['id'],row['revision'])
        self.bind();self.assertEqual(store.list(owner=self.wx)[1],1)
        store.review(row['id'],row['revision'],'approve')
        store.review(row['id'],row['revision'],'feature',reward=8)
        self.assertEqual(self.users.get_balance(self.wx),83);self.assertEqual(self.raw_balance(self.web),0)
        store.review(row['id'],row['revision'],'feature',reward=8);self.assertEqual(self.users.get_balance(self.web),83)
        store.withdraw(row['id'],self.wx,row['revision']);self.assertEqual(store.get(row['id'])['status'],'withdrawn')
    def test_duplicate_prelink_post_likes_count_once_and_toggle_both(self):
        store=SubmissionStore(self.users)
        store.set_like('post',self.web,True);store.set_like('post',self.wx,True)
        self.assertEqual(store.likes(['post'])["post"][0],2)
        self.bind();self.assertEqual(store.likes(['post'],self.wx)['post'],(1,True))
        store.set_like('post',self.wx,False);self.assertNotIn('post',store.likes(['post'],self.web))
    def test_old_comments_and_likes_remain_owned_with_alias_rate_limits(self):
        store=InteractionStore(self.users)
        comment,_=store.reserve_comment('post',self.web,'original_comment','旧网页留言');store.finish_comment(comment['id'],'published','fixture:pass')
        store.like(comment['id'],self.web,True);store.like(comment['id'],self.wx,True)
        self.bind();data=store.comments(post_id='post',viewer=self.wx)
        self.assertTrue(data['items'][0]['mine']);self.assertEqual(data['items'][0]['likes'],1)
        self.assertTrue(data['items'][0]['liked'])
        same,new=store.reserve_comment('post',self.wx,'original_comment','旧网页留言');self.assertFalse(new);self.assertEqual(same['id'],comment['id'])
        store.delete_comment(comment['id'],self.wx);self.assertEqual(store.comments(post_id='post')['total'],0)
    def test_report_deduplication_survives_binding(self):
        store=InteractionStore(self.users)
        old=store.report('post','post','post',self.web,'privacy','fixture snapshot');self.bind()
        new=store.report('post','post','post',self.wx,'privacy','fixture snapshot')
        self.assertTrue(new['duplicate']);self.assertEqual(new['id'],old['id'])
    def test_orders_delivery_and_refund_use_shared_balance_and_histories(self):
        store=PaymentStore(self.users);package={'id':'fixture','price':1,'points':20,'amount_fen':100}
        web_order,_=store.create(self.web,'web_order_key','app','offer',0,package)
        wx_order,_=store.create(self.wx,'wx_order_key','app','offer',0,package)
        other,_=store.create(self.other,'other_order_key','app','offer',0,package)
        self.bind();self.assertEqual(len(store.list(self.wx)),2);self.assertIsNone(store.get(other['id'],self.web))
        store.apply_verified(web_order['id'],'verified-order',True);self.assertEqual(self.users.get_balance(self.wx),95)
        self.assertEqual(self.raw_balance(self.web),0)
        store.apply_verified(web_order['id'],'verified-order',True,100);self.assertEqual(self.users.get_balance(self.web),75)
        self.assertEqual(ExperienceStore(self.users).credit_records(self.wx)['total'],2)
    def test_links_and_all_history_access_survive_restart(self):
        self.users.admin_adjust_balance(self.web,delta=15);self.bind();self.users.close()
        self.users=UserStore(self.tmp.name)
        self.assertEqual(canonical(self.users,self.web),self.wx);self.assertEqual(self.users.get_balance(self.web),90)
        self.assertEqual(ExperienceStore(self.users).credit_records(self.web)['items'][0]['amount'],15)
    def test_prelink_submission_and_order_key_conflicts_do_not_guess_or_redebit(self):
        experience=ExperienceStore(self.users);a,_=experience.claim(self.web,'conflicting_request',{},'text');b,_=experience.claim(self.wx,'conflicting_request',{},'text')
        payments=PaymentStore(self.users);package={'id':'fixture','price':1,'points':20,'amount_fen':100}
        p,_=payments.create(self.web,'conflicting_order','app','offer',0,package);q,_=payments.create(self.wx,'conflicting_order','app','offer',0,package)
        self.bind();before=self.users.get_balance(self.wx)
        with self.assertRaises(ValueError):experience.submission(self.wx,'conflicting_request')
        with self.assertRaises(ValueError):experience.claim(self.wx,'conflicting_request',{},'text')
        with self.assertRaises(ValueError):payments.find(self.wx,'conflicting_order')
        with self.assertRaises(ValueError):payments.create(self.wx,'conflicting_order','app','offer',0,package)
        self.assertEqual(self.users.get_balance(self.wx),before)
        self.assertEqual(len(payments.list(self.wx)),2)
        self.assertIsNotNone(payments.get(p['id'],self.wx));self.assertIsNotNone(payments.get(q['id'],self.web))
    def test_prelink_comment_nonce_collision_preserves_both_and_never_guesses(self):
        store=InteractionStore(self.users)
        first,_=store.reserve_comment('post',self.web,'same_comment_nonce','原网页留言');store.finish_comment(first['id'],'published','fixture:pass')
        second,_=store.reserve_comment('post',self.wx,'same_comment_nonce','原微信留言');store.finish_comment(second['id'],'published','fixture:pass')
        self.bind()
        with self.assertRaises(ValueError):store.reserve_comment('post',self.wx,'same_comment_nonce','原微信留言')
        self.assertEqual(store.comments(post_id='post',viewer=self.wx)['total'],2)
    def test_proof_from_another_mini_app_never_claims_a_recorded_account(self):
        register_verified(self.users,'other_mini_app',self.wx,'wrong_app_union','mini')
        self.assertIsNone(mini_for_unionid(self.users,'wrong_app_union','other_mini_app'))
        with self.assertRaises(ValueError):bind_verified(self.users,self.web,self.wx,'wrong_app_union','official_app')
        self.assertEqual(self.users.get_balance(self.wx),75);self.assertEqual(aliases(self.users,self.web),(self.web,))
    def test_payment_credit_overflow_rolls_back_balance_and_delivery_ledger(self):
        store=PaymentStore(self.users);order,_=store.create(self.wx,'max_fixture_order','app','offer',0,{'id':'fixture','price':1,'points':20,'amount_fen':100})
        self.users.admin_adjust_balance(self.wx,balance=2**63-1)
        with self.assertRaises(ValueError):store.apply_verified(order['id'],'verified-max-order',True)
        self.assertEqual(self.raw_balance(self.wx),2**63-1)
        self.assertEqual(self.users._conn.execute('SELECT typeof(balance) FROM users WHERE openid=?',(self.wx,)).fetchone()[0],'integer')
        self.assertEqual(store.get(order['id'])['credited'],0)
        self.assertEqual(self.users._conn.execute('SELECT COUNT(*) FROM payment_ledger').fetchone()[0],0)
    def test_feature_reward_overflow_keeps_unawarded_record_and_exact_balance(self):
        store=SubmissionStore(self.users);row,_=store.reserve(self.wx,self.photo_job('max_feature'),self.content());store.finish(row['id'],row['revision']);store.review(row['id'],row['revision'],'approve')
        self.users.admin_adjust_balance(self.wx,balance=2**63-1)
        with self.assertRaises(ValueError):store.review(row['id'],row['revision'],'feature',reward=8)
        self.assertEqual(self.raw_balance(self.wx),2**63-1);self.assertFalse(store.rewarded(row['id']));self.assertFalse(store.get(row['id'])['featured'])


if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(AccountSyncTests);names=[t._testMethodName for t in suite]
    result=unittest.TextTestRunner(verbosity=2).run(suite);failed={t._testMethodName for t,_ in result.failures+result.errors}
    (OUTPUT/'account_sync_results.json').write_text(json.dumps({'cases':[{'case':name,'passed':name not in failed} for name in names]},ensure_ascii=False,indent=2),encoding='utf-8')
    print('ACCOUNT_SYNC_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
