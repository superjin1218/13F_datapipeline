import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from requests import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '13f_dashboard'))

from config import Quarter
from pipeline import FILING_STATUS_FILES, export, filing_changes
from source import Sec, SourceError, has_published_filing, parse_holdings_xml


def response(status, content, **headers):
    result = Response()
    result.status_code = status
    result._content = content
    result._content_consumed = True
    result.headers.update(headers)
    return result


def filing_hit(period, form='13F-HR'):
    return {'_source': {'form': form, 'period_ending': period, 'ciks': ['0000000123'],
                         'adsh': '0000000123-26-000001', 'file_date': '2026-05-15'}}


def search_page(rows, total=None):
    data = {'hits': {'total': {'value': total if total is not None else len(rows), 'relation': 'eq'}, 'hits': rows}}
    return response(200, json.dumps(data).encode())


def dataset(filers, loaded):
    return {
        'filings': {cik: {'accession': f'{int(cik):010d}-26-000001', 'filed': '2026-05-15'} for cik in filers},
        'names': {cik: f'Filer {cik}' for cik in filers},
        'holdings': {cik: {'CUSIP': {'value': 75}} for cik in loaded},
    }


@patch.dict('os.environ', SEC_USER_AGENT='Test test@example.com')
class SourceTests(unittest.TestCase):
    def test_request_limit_waits_and_resumes(self):
        messages = []
        sec = Sec(notify=lambda **entry: messages.append(entry))
        self.addCleanup(sec.close)
        limited = response(429, b'', **{'Retry-After': '17'})
        with patch.object(sec.session, 'get', side_effect=[limited, response(200, b'ok')]) as request, \
                patch('source.time.sleep') as sleep:
            self.assertEqual(sec.get('https://efts.sec.gov/LATEST/search-index').content, b'ok')
        self.assertEqual(request.call_count, 2)
        sleep.assert_any_call(17)
        self.assertEqual([entry['state'] for entry in messages], ['waiting', 'retrying', 'resumed'])
        self.assertIsNotNone(messages[0]['retry_at'])

    def test_access_denial_requires_contact_or_network_check(self):
        sec = Sec()
        self.addCleanup(sec.close)
        with patch.object(sec.session, 'get', return_value=response(403, b'Access Denied')) as request:
            with self.assertRaisesRegex(SourceError, 'SEC_USER_AGENT'):
                has_published_filing(sec, Quarter.parse('2026Q1'))
        self.assertEqual(request.call_count, 1)

    def test_availability_uses_report_period_and_pages(self):
        sec = Sec()
        self.addCleanup(sec.close)
        first = search_page([filing_hit('2025-12-31'), filing_hit('2026-03-31', '13F-HR/A')], total=3)
        second = search_page([filing_hit('2026-03-31')], total=3)
        with patch.object(sec, 'get', side_effect=[first, second]) as request:
            self.assertTrue(has_published_filing(sec, Quarter.parse('2026Q1')))
        self.assertEqual(request.call_args.kwargs['params']['from'], 2)

    def test_future_quarter_uses_valid_search_window(self):
        sec = Sec()
        self.addCleanup(sec.close)
        with patch('source.edgar_today', return_value=date(2026, 11, 19)), \
                patch.object(sec, 'get', return_value=search_page([filing_hit('2026-09-30')])) as request:
            self.assertFalse(has_published_filing(sec, Quarter.parse('2026Q4')))
        params = request.call_args.kwargs['params']
        self.assertEqual(params['startdt'], '2026-11-19')
        self.assertEqual(params['enddt'], '2026-11-19')

    def test_namespaced_information_table(self):
        xml = b'''<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
            <infoTable><nameOfIssuer>TEST</nameOfIssuer><cusip>000000001</cusip><figi>BBG000000001</figi>
            <value>1500</value><shrsOrPrnAmt><sshPrnamt>3</sshPrnamt></shrsOrPrnAmt><putCall>Call</putCall>
            </infoTable></informationTable>'''
        self.assertEqual(parse_holdings_xml(xml), [
            {'issuer': 'TEST', 'cusip': '000000001', 'value': 1500, 'shares': 3, 'put_call': 'Call'}
        ])


class ExportTests(unittest.TestCase):
    def test_one_sided_filings_and_unreadable_holdings(self):
        before = dataset(['1', '2', '4'], ['1', '2', '4'])
        after = dataset(['1', '3', '4', '5'], ['1', '3'])
        groups = filing_changes(before, after)
        self.assertEqual({key: [row[0] for row in rows] for key, rows in groups.items()},
                         {'target_only': ['3'], 'baseline_only': ['2'], 'missing': ['4', '5']})
        self.assertIsNone(groups['target_only'][0][5])
        self.assertIsNone(groups['missing'][0][-1])

    def test_no_common_filings_still_writes_status_csvs(self):
        before, after = Quarter.parse('2025Q4'), Quarter.parse('2026Q1')
        groups = filing_changes(dataset(['2'], ['2']), dataset(['3'], ['3']))
        source = {'source': 'fixture', 'cutoff': '2026-05-15', 'filings': 1, 'missing': 0, 'url': 'https://www.sec.gov'}
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            root = Path(folder)
            result = export(None, None, after, before, ['holdings'], root, {before.label: source, after.label: source}, groups)
            self.assertEqual(result['overview']['common'], 0)
            self.assertIsNone(result['overview']['change'])
            self.assertEqual(len(result['files']), 5)
            self.assertTrue(all((root / entry['path']).is_file() for entry in result['files']))
            with (root / 'institutions' / FILING_STATUS_FILES['target_only'][0]).open(encoding='utf-8-sig') as file:
                row = next(csv.DictReader(file))
            self.assertEqual(row['2025Q4 보유액 (USD)'], '')
            self.assertEqual(row['2026Q1 보유액 (USD)'], '75')


if __name__ == '__main__':
    unittest.main()
