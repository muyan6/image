"""Anonymous, idempotent provider reference expenses; separate from user credits.

The ledger has no owner, prompt, image or credential fields. Job expiry/purge must
not erase a recorded generation expense. Missing historical expenses stay unknown.
Call init_cost_ledger/record_cost within the caller's existing SQLite transaction.
"""
import json
import math

TABLE = 'gateway_cost_ledger'


def _amount(value):
    if type(value) not in (int, float) or not 0 <= value <= 1e12 or not math.isfinite(value):
        return None
    return float(value)


def _stamp(value):
    return float(value) if type(value) in (int, float) and 0 <= value <= 1e12 and math.isfinite(value) else 0.0


def record_cost(conn, job):
    """Upsert one expense per job, without committing or erasing known amounts."""
    cny, usd = _amount(job.get('cost_cny')), _amount(job.get('cost_usd'))
    if cny is None and usd is None:
        return
    provider = str(job.get('provider') or 'unknown')[:80]
    packet = job.get('cloud_request') if isinstance(job.get('cloud_request'), dict) else {}
    name = str(job.get('provider_name') or packet.get('gateway_name') or provider)[:80]
    conn.execute('''INSERT INTO gateway_cost_ledger
        (job_id,provider,provider_name,created_at,recorded_at,cost_cny,cost_usd,cost_estimated,cost_source)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
        provider=excluded.provider,provider_name=excluded.provider_name,
        cost_cny=COALESCE(excluded.cost_cny,gateway_cost_ledger.cost_cny),
        cost_usd=COALESCE(excluded.cost_usd,gateway_cost_ledger.cost_usd),
        cost_estimated=excluded.cost_estimated,cost_source=excluded.cost_source''',
        (job['id'],provider,name,_stamp(job.get('created_at')),
         _stamp(job.get('provider_completed_at') or job.get('completed_at') or job.get('updated_at') or job.get('created_at')),
         cny,usd,int(bool(job.get('cost_estimated'))),str(job.get('cost_source') or '')[:80]))


def init_cost_ledger(conn):
    """Seed only available, known historical costs; restarting is idempotent."""
    conn.execute('''CREATE TABLE IF NOT EXISTS gateway_cost_ledger(
        job_id TEXT PRIMARY KEY,provider TEXT NOT NULL,provider_name TEXT NOT NULL,
        created_at REAL NOT NULL,recorded_at REAL NOT NULL,cost_cny REAL,cost_usd REAL,
        cost_estimated INTEGER NOT NULL,cost_source TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gateway_cost_created ON gateway_cost_ledger(created_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gateway_cost_provider_recorded ON gateway_cost_ledger(provider,recorded_at DESC,job_id DESC)')
    cursor=conn.execute('''SELECT j.id,j.provider,j.cost_cny,j.cost_usd,j.created_at,j.updated_at,j.extra_json
        FROM jobs j LEFT JOIN gateway_cost_ledger c ON c.job_id=j.id
        WHERE c.job_id IS NULL AND (j.cost_cny IS NOT NULL OR j.cost_usd IS NOT NULL)''')
    while True:
        batch=cursor.fetchmany(200)
        if not batch:break
        for jid,provider,cny,usd,created,updated,extra in batch:
            try:fields=json.loads(extra or '{}')
            except (ValueError,TypeError):fields={}
            if not isinstance(fields,dict):fields={}
            record_cost(conn,{'id':jid,'provider':provider,'cost_cny':cny,'cost_usd':usd,
                'created_at':created,'updated_at':updated,'provider_name':fields.get('provider_name'),
                'cost_estimated':fields.get('cost_estimated',False),'cost_source':fields.get('cost_source','')})


def gateway_statistics(jobs, midnight):
    """SQL scalar aggregates in one snapshot; never clone retained task payloads."""
    with jobs._lock:
        db=jobs._conn
        db.execute('SAVEPOINT gateway_stats_snapshot')
        try:
            counts=db.execute('''SELECT COUNT(*),
                COALESCE(SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN created_at>=? AND status='succeeded' THEN 1 ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN created_at>=? AND status='failed' THEN 1 ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN status='processing' AND NOT COALESCE(deleted_at,0) THEN 1 ELSE 0 END),0)
                FROM jobs''',(midnight,midnight,midnight)).fetchone()
            totals=db.execute('''SELECT COUNT(*),COALESCE(SUM(cost_cny),0),COALESCE(SUM(cost_usd),0),
                COALESCE(SUM(CASE WHEN created_at>=? THEN cost_cny ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN created_at>=? THEN cost_usd ELSE 0 END),0)
                FROM gateway_cost_ledger''',(midnight,midnight)).fetchone()
            by_provider=db.execute('''SELECT c.provider,
                (SELECT n.provider_name FROM gateway_cost_ledger n WHERE n.provider=c.provider
                    ORDER BY n.recorded_at DESC,n.job_id DESC LIMIT 1),
                COUNT(*),COALESCE(SUM(c.cost_cny),0),COALESCE(SUM(c.cost_usd),0),
                COALESCE(SUM(CASE WHEN c.created_at>=? THEN c.cost_cny ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN c.created_at>=? THEN c.cost_usd ELSE 0 END),0)
                FROM gateway_cost_ledger c GROUP BY c.provider ORDER BY c.provider''',(midnight,midnight)).fetchall()
            missing=db.execute('''SELECT COUNT(*) FROM jobs j LEFT JOIN gateway_cost_ledger c ON c.job_id=j.id
                WHERE c.job_id IS NULL AND j.status IN ('succeeded','failed')''').fetchone()[0]
            db.execute('RELEASE gateway_stats_snapshot')
        except Exception:
            db.execute('ROLLBACK TO gateway_stats_snapshot');db.execute('RELEASE gateway_stats_snapshot')
            raise
    return {'total':counts[0],'today_total':counts[1],'today_succeeded':counts[2],
        'today_failed':counts[3],'running':counts[4],
        'total_cost_cny':totals[1],'total_cost_usd':totals[2],
        'today_cost_cny':totals[3],'today_cost_usd':totals[4],
        'costs_recorded_total':totals[0],'costs_missing_total':missing,'cost_basis':'recorded_reference',
        'provider_costs':[{'provider':p,'provider_name':name,'jobs_with_cost':n,
            'total_cost_cny':cny,'total_cost_usd':usd,'today_cost_cny':day_cny,'today_cost_usd':day_usd}
            for p,name,n,cny,usd,day_cny,day_usd in by_provider]}
