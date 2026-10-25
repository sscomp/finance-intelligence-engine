"""Offline rejection at the production collector/persistence and strategy seams."""
from unittest.mock import Mock, patch
import test_yfinance as base


class CalendarAdmissionTests(base.unittest.TestCase):
    setUp=base.YFinanceTests.setUp
    tearDown=base.YFinanceTests.tearDown
    save=base.YFinanceTests.save
    payload=base.YFinanceTests.payload

    def collect(self,payload,persist=False):
        y=base.y;meta={'fetched_at':base.NOW.isoformat(),'sha256':'a'*64}
        m=y.research(self.r);writer=Mock(return_value={'rows':18})
        with patch.object(y,'fetch',return_value=(payload,meta)),patch.object(y,'research',return_value={**m,'persist':writer}):
            result=y.run(base.NOW,self.r) if persist else y.collect(base.NOW,self.r)
        return result,writer

    def test_closed_days_rejected_before_any_p3_call_regardless_of_volume(self):
        y=base.y
        for day in [23,26]:
            for volume in [0,1234]:
                payload=self.payload(f'2026-10-{day}T00:00:00+08:00')
                payload['series'][0]['bars'][0]['Volume']=volume
                m=y.research(self.r);writer=Mock(side_effect=AssertionError('P3 must not be reached'))
                with patch.object(y,'fetch',return_value=(payload,{'fetched_at':base.NOW.isoformat()})),patch.object(y,'research',return_value={**m,'persist':writer}):
                    with self.assertRaisesRegex(y.DataError,f'BAR_CALENDAR_REJECTED:2026-10-{day}:CLOSED'):y.run(base.NOW,self.r)
                writer.assert_not_called()

    def test_valid_day_zero_volume_keeps_provenance_and_reaches_writer(self):
        payload=self.payload('2026-10-22T00:00:00+08:00')
        for series in payload['series']:series['bars'][0]['Volume']=0
        batch,_=self.collect(payload)
        self.assertEqual(len(batch['rows']),18)
        self.assertTrue(all(row['fields']['volume']==0 for row in batch['rows']))
        self.assertEqual(batch['responses'][0]['sha256'],'a'*64)
        _,writer=self.collect(payload,persist=True);writer.assert_called_once()
        quality=base.y.assess(batch['rows'],base.NOW,self.r)
        self.assertEqual(quality['status'],'DATA_DELAYED')
        self.assertEqual(base.y.strategy(quality,{})['status'],'BLOCKED')

    def test_exchange_date_and_stale_or_unknown_calendar_fail_closed(self):
        # Taipei midnight is the previous UTC date; admission uses Taipei date.
        batch,_=self.collect(self.payload('2026-10-22T00:00:00+08:00'))
        self.assertTrue(all(row['source_date']=='2026-10-22' for row in batch['rows']))
        with self.assertRaisesRegex(base.y.DataError,'EXCHANGE_TIMEZONE_MISMATCH'):
            self.collect(self.payload('2026-10-21T16:00:00+00:00'))
        with self.assertRaisesRegex(base.y.DataError,'HISTORY_TOO_OLD'):
            self.collect(self.payload('2026-08-01T00:00:00+08:00'))
        cal=base.json.loads((self.r/'config/calendar.json').read_text());cal['verified_utc']='2026-08-01T00:00:00+00:00';self.save('config/calendar.json',cal)
        with self.assertRaisesRegex(base.y.DataError,'CALENDAR_UNAVAILABLE_OR_EXPIRED'):self.collect(self.payload())

    def test_preexisting_closed_rows_block_strategy_even_with_current_rows(self):
        rows=base.y.normalize(self.payload(),base.NOW,self.c)
        rows+=base.y.normalize(self.payload('2026-10-23T00:00:00+08:00'),base.NOW,self.c)
        quality=base.y.assess(rows,base.NOW,self.r)
        self.assertEqual(quality['quality'],'DEGRADED');self.assertEqual(quality['rows'],[])
        self.assertEqual(base.y.strategy(quality,{})['observations'],[])
