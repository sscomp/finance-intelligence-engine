"""Calendar/checkpoint contracts using offline payloads and a private SQLite sink."""
import copy
import sqlite3
from unittest.mock import Mock, patch
import test_yfinance as base

y=base.y

class CalendarBatchTests(base.unittest.TestCase):
    setUp=base.YFinanceTests.setUp
    tearDown=base.YFinanceTests.tearDown
    save=base.YFinanceTests.save
    payload=base.YFinanceTests.payload

    def mixed(self):
        payload=self.payload()
        for series in payload['series']:
            closed=copy.deepcopy(series['bars'][0]);closed['timestamp']='2026-10-23T00:00:00+08:00'
            series['bars'].insert(0,closed)
        return payload

    def state(self):
        return base.json.loads((self.r/'state/research-yfinance_daily.json').read_text())

    def seed(self):
        prior={'batch_id':'prior','last_success':(base.NOW-base.dt.timedelta(days=2)).isoformat()}
        self.save('state/research-yfinance_daily.json',prior)
        return prior

    def run_payload(self,payload,writer,now=base.NOW,meta=None):
        m=y.research(self.r)
        meta=meta or {'fetched_at':base.NOW.isoformat(),'sha256':'a'*64}
        with patch.object(y,'fetch',return_value=(payload,meta)),patch.object(y,'research',return_value={**m,'persist':writer}):
            return y.run(now,self.r)

    def sink(self):
        db=sqlite3.connect(self.r/'state/fixture.sqlite3')
        db.executescript('CREATE TABLE batches(id TEXT PRIMARY KEY,payload TEXT); CREATE TABLE versions(key TEXT,version TEXT,PRIMARY KEY(key,version));')
        def persist(batch,root):
            # Fixture transaction is durable before the collector sees a receipt.
            with db:
                db.execute('INSERT OR IGNORE INTO batches VALUES (?,?)',(batch['batch_id'],base.json.dumps(batch)))
                db.executemany('INSERT OR IGNORE INTO versions VALUES (?,?)',[(r['key'],r['version']) for r in batch['rows']])
            return {'batch_id':batch['batch_id'],'rows':len(batch['rows'])}
        self.addCleanup(db.close)
        return db,Mock(side_effect=persist)

    def test_mixed_filters_closed_before_durable_sink_and_strategy(self):
        db,writer=self.sink();result=self.run_payload(self.mixed(),writer)
        batch=base.json.loads(db.execute('SELECT payload FROM batches').fetchone()[0])
        self.assertEqual(result['result'],'PASS');self.assertEqual(result['rows'],18)
        self.assertEqual(batch['coverage']['calendar_admission']['input_rows'],36)
        self.assertEqual(len(batch['coverage']['calendar_admission']['rejected_rows']),18)
        self.assertTrue(all(r['source_date']=='2026-10-27' for r in batch['rows']))
        self.assertTrue(all(r['latest_trade_at'] is None for r in batch['rows']))
        self.assertEqual(batch['responses'][0]['sha256'],'a'*64)
        rejected=list((self.r/'state/yfinance-calendar-rejections').glob('*.json'))
        self.assertEqual(len(rejected),1)
        evidence=base.json.loads(rejected[0].read_text())
        self.assertEqual(len(evidence['rejected_rows']),18)
        self.assertEqual(evidence['responses'],batch['responses'])
        observations=y.strategy(y.assess(batch['rows'],base.NOW,self.r),{})['observations']
        self.assertEqual(len(observations),17)
        self.assertTrue(all(o['bar_timestamp'].startswith('2026-10-27') for o in observations))

    def test_all_closed_keeps_checkpoint_and_quarantine_without_writer(self):
        prior=self.seed();writer=Mock()
        with self.assertRaisesRegex(y.DataError,'NO_ACCEPTED_ROWS'):
            self.run_payload(self.payload('2026-10-23T00:00:00+08:00'),writer)
        writer.assert_not_called();self.assertEqual(self.state()['last_success'],prior['last_success'])
        self.assertEqual(self.state()['batch_id'],'prior')
        evidence=base.json.loads(next((self.r/'state/yfinance-calendar-rejections').glob('*.json')).read_text())
        self.assertEqual(evidence['calendar_admission']['accepted_rows'],0)

    def test_unknown_date_with_open_rows_rejects_whole_batch(self):
        cal=base.json.loads((self.r/'config/calendar.json').read_text());cal['valid_from']='2026-10-24';self.save('config/calendar.json',cal)
        prior=self.seed();writer=Mock()
        with self.assertRaisesRegex(y.DataError,'CALENDAR_UNAVAILABLE_OR_EXPIRED'):self.run_payload(self.mixed(),writer)
        writer.assert_not_called();self.assertEqual(self.state()['last_success'],prior['last_success'])

    def test_missing_expired_and_tampered_calendar_reject_whole_batch(self):
        original=base.json.loads((self.r/'config/calendar.json').read_text())
        for variant in ['missing','expired','evidence']:
            cal=copy.deepcopy(original)
            if variant=='expired':cal['verified_utc']='2026-08-01T00:00:00+00:00'
            if variant=='evidence':cal['evidence']=[{'path':'config/yfinance.json','sha256':'0'*64}]
            self.save('config/calendar.json',cal)
            if variant=='missing':(self.r/'config/calendar.json').unlink()
            writer=Mock()
            with self.assertRaisesRegex(y.DataError,'CALENDAR_UNAVAILABLE_OR_EXPIRED'):self.run_payload(self.mixed(),writer)
            writer.assert_not_called()

    def test_verified_open_zero_volume_is_accepted_but_current_strategy_stays_limited(self):
        payload=self.mixed()
        for series in payload['series']:series['bars'][-1]['Volume']=0
        db,writer=self.sink();self.run_payload(payload,writer)
        rows=writer.call_args.args[0]['rows']
        self.assertTrue(all(r['fields']['volume']==0 for r in rows))
        quality=y.assess(rows,base.NOW,self.r)
        self.assertEqual(quality['reason'],'CURRENT_SESSION_VOLUME_ABSENT')
        self.assertNotEqual(quality['status'],'MARKET_CLOSED')
        self.assertEqual(y.strategy(quality,{})['observations'],[])

    def test_provider_failure_does_not_advance_or_use_old_data(self):
        for exc in [y.DataError('YFINANCE_FETCH_FAILED_OR_OVERSIZED'),base.subprocess.TimeoutExpired('worker',420)]:
            prior=self.seed();writer=Mock();m=y.research(self.r)
            with patch.object(y,'fetch',side_effect=exc),patch.object(y,'research',return_value={**m,'persist':writer}):
                with self.assertRaises(type(exc)):y.run(base.NOW,self.r)
            writer.assert_not_called();self.assertEqual(self.state()['last_success'],prior['last_success'])
            self.assertEqual(self.state()['result'],'FAILED')

    def test_invalid_closed_bar_is_not_hidden_by_filter(self):
        payload=self.mixed();payload['series'][0]['bars'][0]['Low']=200
        writer=Mock()
        with self.assertRaisesRegex(y.DataError,'INVALID_OHLC_RANGE'):self.run_payload(payload,writer)
        writer.assert_not_called()

    def test_partial_series_coverage_is_reported_and_blocks_current_strategy(self):
        payload=self.mixed();payload['series'][0]['bars'].pop()
        db,writer=self.sink();result=self.run_payload(payload,writer)
        self.assertEqual(result['rows'],17);self.assertEqual(result['coverage']['series'],17)
        quality=y.assess(writer.call_args.args[0]['rows'],base.NOW,self.r)
        self.assertEqual(quality['reason'],'INCOMPLETE_SYMBOL_COVERAGE')
        self.assertEqual(y.strategy(quality,{})['observations'],[])

    def test_persistence_failure_and_bad_receipt_preserve_checkpoint(self):
        for failure in [RuntimeError('fixture persistence failure'),{'rows':18,'batch_id':'wrong'}]:
            prior=self.seed();writer=Mock(side_effect=failure) if isinstance(failure,Exception) else Mock(return_value=failure)
            with self.assertRaises(Exception):self.run_payload(self.mixed(),writer)
            self.assertEqual(self.state()['last_success'],prior['last_success'])
            self.assertEqual(self.state()['result'],'FAILED')

    def test_quarantine_failure_prevents_persistence_and_checkpoint(self):
        prior=self.seed();writer=Mock();m=y.research(self.r);atomic=m['atomic']
        def fail_quarantine(path,value):
            if path.parent.name=='yfinance-calendar-rejections':raise OSError('fixture disk failure')
            return atomic(path,value)
        with patch.object(y,'fetch',return_value=(self.mixed(),{'fetched_at':base.NOW.isoformat(),'sha256':'a'*64})),patch.object(y,'research',return_value={**m,'persist':writer,'atomic':fail_quarantine}):
            with self.assertRaises(OSError):y.run(base.NOW,self.r)
        writer.assert_not_called();self.assertEqual(self.state()['last_success'],prior['last_success'])

    def test_idempotent_rerun_refetch_and_crash_after_commit(self):
        db,writer=self.sink();payload=self.mixed();self.seed()
        m=y.research(self.r);atomic=m['atomic']
        def fail_checkpoint(path,value):
            if path.name=='research-yfinance_daily.json' and value.get('result')=='PASS':raise OSError('fixture crash')
            return atomic(path,value)
        with patch.object(y,'research',return_value={**m,'atomic':fail_checkpoint,'persist':writer}):
            with self.assertRaises(OSError):self.run_payload(payload,writer)
        self.assertEqual(self.state()['batch_id'],'prior')
        a=self.run_payload(payload,writer)
        b=self.run_payload(payload,writer,base.NOW+base.dt.timedelta(minutes=1))
        self.assertEqual(a['batch_id'],b['batch_id']);self.assertEqual(a['last_success'],b['last_success'])
        meta={'fetched_at':(base.NOW+base.dt.timedelta(minutes=2)).isoformat(),'sha256':'b'*64}
        c=self.run_payload(payload,writer,base.NOW+base.dt.timedelta(minutes=2),meta)
        self.assertEqual(a['batch_id'],c['batch_id']);self.assertEqual(c['last_success'],meta['fetched_at'])
        self.assertEqual(db.execute('SELECT count(*) FROM batches').fetchone()[0],1)
        self.assertEqual(db.execute('SELECT count(*) FROM versions').fetchone()[0],18)
        self.assertEqual(len(list((self.r/'state/yfinance-calendar-rejections').glob('*.json'))),2)

    def test_checkpoint_freshness_uses_fetch_time_after_durable_partial_acceptance(self):
        db,writer=self.sink();result=self.run_payload(self.mixed(),writer)
        m=y.research(self.r)
        def raw_sql(*args):return db.execute('SELECT payload FROM batches').fetchone()[0].encode()
        with patch.object(y,'research',return_value={**m,'source_state':lambda *a:{**self.state(),'usable':True},'ingestion':lambda:{'sql':raw_sql,'json_sql':lambda x:x}}):
            fresh=y.status(base.NOW+base.dt.timedelta(seconds=4499),self.r)
            stale=y.status(base.NOW+base.dt.timedelta(seconds=4500),self.r)
        self.assertTrue(fresh['usable'],fresh);self.assertEqual(len(fresh['rows']),18)
        self.assertFalse(stale['usable']);self.assertEqual(stale['rows'],[])
        self.assertEqual(stale['reason'],'YFINANCE_CHECKPOINT_EXPIRED')
        self.assertEqual(result['last_success'],base.NOW.isoformat())

    def test_readback_containing_closed_rows_still_blocks_entire_strategy(self):
        rows=y.normalize(self.mixed(),base.NOW,self.c)
        quality=y.assess(rows,base.NOW,self.r)
        self.assertEqual(quality['quality'],'DEGRADED')
        self.assertEqual(y.strategy(quality,{})['observations'],[])

    def test_tampered_quarantine_prevents_rerun_checkpoint(self):
        db,writer=self.sink();self.run_payload(self.mixed(),writer)
        path=next((self.r/'state/yfinance-calendar-rejections').glob('*.json'))
        path.write_text('{}');writer.reset_mock();prior=self.state()
        with self.assertRaisesRegex(y.DataError,'CALENDAR_REJECTION_EVIDENCE_MISMATCH'):
            self.run_payload(self.mixed(),writer)
        writer.assert_not_called();self.assertEqual(self.state()['last_success'],prior['last_success'])
