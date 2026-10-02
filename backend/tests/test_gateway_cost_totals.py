"""Pure SQLite reference-cost accounting, no credentials/media/provider requests."""
from pathlib import Path
import json,os,sqlite3,sys,tempfile,threading,unittest
from types import SimpleNamespace

ROOT=Path(os.environ.get('REVIEW_ROOT',Path(__file__).resolve().parents[2]))
OUT=Path(os.environ.get('REVIEW_OUTPUT',ROOT/'audit/gateway-cost-totals'));OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT/'backend'))
from gateway_costs import init_cost_ledger,record_cost,gateway_statistics


class TotalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='cost_totals_',dir=OUT)
        self.path=Path(self.temp.name)/'jobs.db';self.db=sqlite3.connect(self.path)
        self.db.execute('''CREATE TABLE jobs(id TEXT PRIMARY KEY,provider TEXT,cost_cny REAL,cost_usd REAL,
            created_at REAL,updated_at REAL,extra_json TEXT,status TEXT,deleted_at REAL)''')
        init_cost_ledger(self.db);self.db.commit()
        self.store=SimpleNamespace(_conn=self.db,_lock=threading.Lock(),list_recent=lambda *a:(_ for _ in ()).throw(AssertionError('NO_TASK_CLONES')))
    def tearDown(self):self.db.close();self.temp.cleanup()
    def add(self,jid,**fields):
        job={'id':jid,'provider':'gw_'+'a'*32,'provider_name':'自定义甲','created_at':100,'updated_at':101,'status':'succeeded',**fields}
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                (jid,job['provider'],job.get('cost_cny'),job.get('cost_usd'),job['created_at'],job['updated_at'],
                 json.dumps({k:v for k,v in job.items() if k not in ('id','cost_cny','cost_usd')},ensure_ascii=False),job['status'],job.get('deleted_at')))
            record_cost(self.db,job)
        return job
    def stats(self):return gateway_statistics(self.store,50)

    def test_total_and_today_aggregate_all_gateways_but_never_add_usd_to_cny(self):
        self.add('a',cost_cny=.04);self.add('b',provider='gw_'+'b'*32,provider_name='自定义乙',cost_cny=.15)
        self.add('old',provider='fal',provider_name='历史来源',cost_usd=.125,created_at=10)
        stats=self.stats();self.assertAlmostEqual(stats['total_cost_cny'],.19);self.assertEqual(stats['total_cost_usd'],.125)
        self.assertAlmostEqual(stats['today_cost_cny'],.19);self.assertEqual(stats['today_cost_usd'],0)
        self.assertEqual(len(stats['provider_costs']),3);self.assertEqual(stats['costs_recorded_total'],3)

    def test_repeat_status_updates_and_restart_do_not_double_count_one_job(self):
        job=self.add('once',cost_cny=.07)
        for _ in range(5):
            with self.db:record_cost(self.db,{**job,'status':'failed','updated_at':1000})
        init_cost_ledger(self.db);self.db.commit();self.db.close();self.db=sqlite3.connect(self.path);self.store._conn=self.db
        init_cost_ledger(self.db);self.db.commit()
        self.assertEqual(self.stats()['costs_recorded_total'],1);self.assertEqual(self.stats()['total_cost_cny'],.07)

    def test_expiring_or_deleting_job_rows_does_not_reduce_recorded_total(self):
        self.add('expired',cost_cny=.39);self.add('deleted',cost_cny=.07,deleted_at=101)
        with self.db:self.db.execute('DELETE FROM jobs')
        stats=self.stats();self.assertEqual(stats['total'],0);self.assertAlmostEqual(stats['total_cost_cny'],.46)
        init_cost_ledger(self.db);self.db.commit();self.assertEqual(self.stats()['costs_recorded_total'],2)

    def test_refunding_failed_delivery_does_not_erase_provider_expense(self):
        job=self.add('generated',cost_cny=.23,status='processing')
        self.add('generated',**{k:v for k,v in job.items() if k!='id'},cost_usd=None)
        with self.db:
            self.db.execute("UPDATE jobs SET status='failed' WHERE id='generated'")
            record_cost(self.db,{**job,'status':'failed','price':0,'charged_amount':0,'cost_cny':None})
        stats=self.stats();self.assertEqual(stats['today_failed'],1);self.assertEqual(stats['total_cost_cny'],.23)

    def test_unknown_cost_is_not_fabricated_from_photons_or_model_price(self):
        self.add('unknown',price=999,charged_amount=999,cost_cny=None)
        stats=self.stats();self.assertEqual(stats['total_cost_cny'],0);self.assertEqual(stats['costs_recorded_total'],0)
        self.assertEqual(stats['costs_missing_total'],1)

    def test_zero_is_known_and_invalid_negative_nan_infinite_bool_amounts_are_ignored(self):
        self.add('free',cost_cny=0)
        for index,value in enumerate([-1,float('nan'),float('inf'),True,1e13,10**400,'10']):
            # Invalid supplier values are tested at the record boundary, not by
            # letting SQLite coerce them into a different valid storage type.
            with self.db:record_cost(self.db,{'id':'invalid'+str(index),'cost_cny':value})
        stats=self.stats();self.assertEqual(stats['total_cost_cny'],0);self.assertEqual(stats['costs_recorded_total'],1)

    def test_oversized_timestamp_is_ignored_without_leaving_partial_transaction(self):
        with self.db:record_cost(self.db,{'id':'bad_stamp','cost_cny':.04,'created_at':10**400,'updated_at':10**400})
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.db.execute("SELECT created_at,recorded_at FROM gateway_cost_ledger WHERE job_id='bad_stamp'").fetchone(),(0,0))

    def test_cost_write_is_in_same_transaction_and_failure_rolls_back_job_too(self):
        self.db.execute("CREATE TRIGGER fail_expense BEFORE INSERT ON gateway_cost_ledger BEGIN SELECT RAISE(ABORT,'fixture'); END")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):self.add('atomic',cost_cny=.1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE id='atomic'").fetchone()[0],0)
        self.assertEqual(self.stats()['costs_recorded_total'],0)

    def test_historical_known_cost_seed_is_idempotent_without_credentials_or_story_columns(self):
        with self.db:self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
            ('legacy','worldcodes',.04,None,10,11,json.dumps({'provider_name':'原名称','cost_estimated':True,'api_key':'SECRET_FIXTURE','prompt':'PRIVATE_FIXTURE'}),'failed',None))
        init_cost_ledger(self.db);self.db.commit();init_cost_ledger(self.db);self.db.commit()
        row=self.db.execute('SELECT * FROM gateway_cost_ledger').fetchone();text=json.dumps(row,ensure_ascii=False)
        self.assertNotIn('SECRET_FIXTURE',text);self.assertNotIn('PRIVATE_FIXTURE',text)
        self.assertEqual(self.stats()['total_cost_cny'],.04);self.assertEqual(self.stats()['provider_costs'][0]['provider_name'],'原名称')

    def test_gateway_renaming_keeps_id_grouped_and_task_name_snapshot_not_live_config(self):
        self.add('old',cost_cny=.04,provider_name='原名称',provider_completed_at=100)
        self.add('new',cost_cny=.15,provider_name='新名称',provider_completed_at=200)
        rows=self.stats()['provider_costs'];self.assertEqual(len(rows),1);self.assertEqual(rows[0]['provider_name'],'新名称')
        self.assertAlmostEqual(rows[0]['total_cost_cny'],.19)

    def test_stats_use_sql_aggregates_not_full_task_payload_copies(self):
        for index in range(1000):self.add('scalar'+str(index),cost_cny=.01)
        statements=[];self.db.set_trace_callback(statements.append)
        stats=self.stats();self.db.set_trace_callback(None)
        self.assertEqual(stats['total'],1000);self.assertAlmostEqual(stats['total_cost_cny'],10)
        self.assertFalse(any('extra_json' in q or 'SELECT *' in q for q in statements))
        self.assertEqual(stats['cost_basis'],'recorded_reference')

    def test_statistics_respect_existing_outer_transaction_without_committing_it(self):
        self.db.execute("INSERT INTO jobs VALUES('pending','worldcodes',NULL,NULL,100,101,'{}','processing',NULL)")
        self.assertTrue(self.db.in_transaction);self.assertEqual(self.stats()['running'],1)
        self.assertTrue(self.db.in_transaction);self.db.rollback();self.assertEqual(self.stats()['running'],0)


if __name__=='__main__':
    names=[n for n in TotalTests.__dict__ if n.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(TotalTests(n) for n in names))
    failed={t._testMethodName for t,_ in result.failures+result.errors}
    (OUT/'gateway_cost_totals_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    print('GATEWAY_COST_TOTALS_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
