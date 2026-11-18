"""Load SEC data for the requested reporting period and normalize it to USD."""

import csv
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from itertools import count
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from lxml import etree, html

from config import STATE, Quarter, atomic_write, edgar_today
from quarter_utils import resolve_bulk_dir_for_quarter, resolve_cache_dir_for_quarter


CATALOG = 'https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets'
EFTS = 'https://efts.sec.gov/LATEST/search-index'
ARCHIVES = 'https://www.sec.gov/Archives/edgar/data/'
SEC_HOSTS = {'www.sec.gov', 'efts.sec.gov', 'data.sec.gov'}
BULK_FILES = ('SUBMISSION.tsv', 'COVERPAGE.tsv', 'SUMMARYPAGE.tsv', 'INFOTABLE.tsv')
USD_SINCE = date(2023, 1, 3)
ACCESSION = re.compile(r'^\d{10}-\d{2}-\d{6}$')
MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
          'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}


class SourceError(Exception):
    pass


TRANSIENT_NETWORK_ERRORS = (
    requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError,
)


def retry_after_seconds(value):
    if not value:
        return None
    if value.isdecimal():
        return int(value)
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, OverflowError, TypeError):
        return None


class Sec:
    def __init__(self, notify=None):
        user_agent = os.environ.get('SEC_USER_AGENT', '').strip()
        if not user_agent:
            raise SourceError('SEC_USER_AGENT에 이름과 연락처 이메일을 설정한 뒤 서버를 다시 실행해 주세요.')
        self.session = requests.Session()
        self.session.headers['User-Agent'] = user_agent
        self.next_request = 0.0
        self.notify = notify

    def connection_status(self, state, reason, attempt, retry_at=None):
        if self.notify:
            self.notify(state=state, reason=reason, attempt=attempt, retry_at=retry_at)

    def wait_to_retry(self, reason, attempt, delay=None):
        delay = max(1, delay) if delay is not None else min(120, 2 ** min(attempt, 7))
        if not self.notify and (attempt >= 3 or delay > 8):
            raise SourceError(reason)
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec='seconds')
        self.connection_status('waiting', reason, attempt, retry_at)
        time.sleep(delay)
        self.connection_status('retrying', reason, attempt)

    def get(self, url, **options):
        address = urlsplit(url)
        if address.scheme != 'https' or address.netloc not in SEC_HOSTS:
            raise SourceError('검증된 SEC 주소가 아닙니다.')
        for attempt in count(1):
            time.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + 0.35
            try:
                result = self.session.get(url, timeout=(10, 60), allow_redirects=False, **options)
            except requests.exceptions.SSLError as exc:
                raise SourceError('SEC 보안 연결을 확인할 수 없습니다.') from exc
            except TRANSIENT_NETWORK_ERRORS:
                self.wait_to_retry('SEC 연결이 일시적으로 끊겼습니다.', attempt)
                continue
            if result.ok and result.status_code < 300:
                if attempt > 1:
                    self.connection_status('resumed', 'SEC 연결이 복구되어 수집을 재개합니다.', attempt - 1)
                return result

            with result:
                status = result.status_code
                delay = retry_after_seconds(result.headers.get('Retry-After'))
                rate_warning = status == 403 and 'request rate threshold' in result.text.lower()
            limited = status == 429 or (status == 403 and (rate_warning or delay is not None))
            if limited:
                self.wait_to_retry(f'SEC 요청 제한 (HTTP {status})', attempt, delay if delay is not None else 600)
            elif status in (408, 500, 502, 503, 504):
                self.wait_to_retry(f'SEC 서버 일시 오류 (HTTP {status})', attempt, delay)
            elif status == 403:
                raise SourceError('SEC가 요청을 거부했습니다. SEC_USER_AGENT와 네트워크 접근 상태를 확인해 주세요.')
            else:
                raise SourceError(f'SEC 응답 HTTP {status}')

    def close(self):
        self.session.close()


def sec_date(raw):
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip().lower()
    try:
        return date.fromisoformat(text)
    except ValueError:
        match = re.fullmatch(r'(\d{1,2})-?([a-z]{3})-?(\d{4})', text)
        if match and match[2] in MONTHS:
            try:
                return date(int(match[3]), MONTHS[match[2]], int(match[1]))
            except ValueError:
                return None
        return None


def number(raw):
    try:
        return int(str(raw).replace(',', '').strip())
    except (TypeError, ValueError):
        return 0


def usd(raw, filed):
    return number(raw) * (1000 if filed < USD_SINCE else 1)


def windows_from_catalog(page):
    links = html.fromstring(page).xpath('//a/@href')
    for link in links:
        address = urljoin(CATALOG, link)
        parts = urlsplit(address)
        if parts.scheme != 'https' or parts.netloc != 'www.sec.gov':
            continue
        name = Path(parts.path).name.lower()
        dated = re.fullmatch(r'(\d{2}[a-z]{3}\d{4})-(\d{2}[a-z]{3}\d{4})_form13f\.zip', name)
        calendar = re.fullmatch(r'(\d{4})q([1-4])_form13f\.zip', name)
        if dated:
            start, end = map(sec_date, dated.groups())
        elif calendar:
            end = Quarter(int(calendar[1]), int(calendar[2])).period_ending
            start = date(end.year, end.month - 2, 1)
        else:
            continue
        if start and end:
            yield start, end, address


def bulk_urls(sec, quarter):
    due = quarter.period_ending + timedelta(days=45)
    try:
        windows = list(windows_from_catalog(sec.get(CATALOG).content))
    except (SourceError, etree.ParserError):
        windows = []
    matching = sorted({(end, link) for start, end, link in windows if start <= due <= end})
    if matching:
        return [link for _, link in matching]
    if not windows:
        # The dated filenames are published by filing window in recent years.
        if quarter.year >= 2024:
            return quarter.bulk_urls()[1:]
        next_quarter = Quarter(due.year, (due.month - 1) // 3 + 1)
        return next_quarter.bulk_urls()[:1]
    return []


def download_bulk(sec, url, directory, progress):
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.download-', dir=directory.parent) as temporary:
        work = Path(temporary)
        archive = work / 'download.zip'
        for attempt in count(1):
            try:
                with sec.get(url, stream=True) as response, archive.open('wb') as output:
                    total = number(response.headers.get('Content-Length'))
                    received = 0
                    for block in response.iter_content(1024 * 1024):
                        output.write(block)
                        received += len(block)
                        progress('SEC 벌크 내려받는 중', received, total or None, 'bytes', 0.04, 0.44)
            except TRANSIENT_NETWORK_ERRORS:
                sec.wait_to_retry('SEC 벌크 전송이 중단됐습니다.', attempt)
                continue
            if attempt > 1:
                sec.connection_status('resumed', 'SEC 벌크 전송이 완료되었습니다.', attempt - 1)
            break
        unpacked = work / 'tables'
        unpacked.mkdir()
        try:
            with zipfile.ZipFile(archive) as source:
                members = {Path(i.filename).name.upper(): i for i in source.infolist()}
                if not all(name.upper() in members for name in BULK_FILES):
                    raise SourceError('SEC 벌크에 필수 TSV가 없습니다.')
                for index, name in enumerate(BULK_FILES, start=1):
                    progress('SEC 벌크 압축 푸는 중', index - 1, len(BULK_FILES), 'steps', 0.48, 0.02)
                    item = members[name.upper()]
                    if item.file_size > 3 * 1024 ** 3:
                        raise SourceError('SEC 벌크 파일이 지원 크기를 초과합니다.')
                    with source.open(item) as stream, (unpacked / name).open('wb') as output:
                        shutil.copyfileobj(stream, output)
        except (zipfile.BadZipFile, EOFError) as exc:
            raise SourceError('SEC 벌크 파일의 압축을 풀 수 없습니다.') from exc
        atomic_write(unpacked / 'source.json', json.dumps({'url': url}))
        unpacked.replace(directory)


def tsv(path, required):
    with path.open(encoding='utf-8-sig', errors='replace', newline='') as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        if not set(required).issubset(reader.fieldnames or ()):
            raise SourceError(f'{path.name}에 필요한 SEC 열이 없습니다.')
        yield from reader


def add_position(positions, *, issuer, cusip, value, shares, put_call):
    if not issuer or (value <= 0 and shares <= 0):
        return
    issuer, cusip = issuer.strip().upper(), cusip.strip().upper()
    kind = {'PUT': 'Put', 'CALL': 'Call'}.get(put_call.strip().upper(), '')
    key = cusip or issuer
    if kind:
        key = f'{key}_{kind}'
    existing = positions.get(key)
    if existing:
        existing['value'] += value
        existing['shares'] += shares
    else:
        positions[key] = {'issuer': issuer, 'cusip': cusip, 'value': value, 'shares': shares, 'put_call': kind}


def read_bulk(directory, quarter, label, progress):
    progress('보고 기간과 원본 13F-HR 확인 중', None, None, None, 0.5, 0)
    by_cik = {}
    for row in tsv(directory / 'SUBMISSION.tsv', ('ACCESSION_NUMBER', 'CIK', 'SUBMISSIONTYPE', 'PERIODOFREPORT', 'FILING_DATE')):
        if row['SUBMISSIONTYPE'] != '13F-HR' or sec_date(row['PERIODOFREPORT']) != quarter.period_ending:
            continue
        cik = (row['CIK'] or '').lstrip('0')
        filed = sec_date(row['FILING_DATE'])
        acc = row['ACCESSION_NUMBER']
        if cik.isdecimal() and filed and ACCESSION.fullmatch(acc or ''):
            by_cik[cik] = max(by_cik.get(cik, (filed, acc)), (filed, acc))
    if not by_cik:
        raise SourceError(f'{quarter.label}에 해당하는 SEC 벌크 공시가 없습니다.')
    by_acc = {acc: (cik, filed) for cik, (filed, acc) in by_cik.items()}
    names = {by_acc[r['ACCESSION_NUMBER']][0]: r['FILINGMANAGER_NAME'] for r in
             tsv(directory / 'COVERPAGE.tsv', ('ACCESSION_NUMBER', 'FILINGMANAGER_NAME'))
             if r['ACCESSION_NUMBER'] in by_acc}
    aum = {by_acc[r['ACCESSION_NUMBER']][0]: usd(r['TABLEVALUETOTAL'], by_acc[r['ACCESSION_NUMBER']][1]) for r in
           tsv(directory / 'SUMMARYPAGE.tsv', ('ACCESSION_NUMBER', 'TABLEVALUETOTAL'))
           if r['ACCESSION_NUMBER'] in by_acc}
    holdings = {}
    for row in tsv(directory / 'INFOTABLE.tsv', ('ACCESSION_NUMBER', 'NAMEOFISSUER', 'CUSIP', 'VALUE', 'SSHPRNAMT')):
        filing = by_acc.get(row['ACCESSION_NUMBER'])
        if not filing:
            continue
        cik, filed = filing
        positions = holdings.get(cik)
        if positions is None:
            positions = holdings[cik] = {}
            if len(holdings) % 100 == 0:
                progress('벌크 보유내역 읽는 중', len(holdings), len(by_acc), 'filings', 0.53, 0.46)
        add_position(positions, issuer=row['NAMEOFISSUER'] or '', cusip=row['CUSIP'] or '',
                     value=usd(row['VALUE'], filed), shares=number(row['SSHPRNAMT']), put_call=row.get('PUTCALL') or '')
    holdings = {cik: h for cik, h in holdings.items() if h}
    if not holdings:
        raise SourceError(f'{quarter.label} 벌크에서 보유종목을 읽지 못했습니다.')
    metadata = {'source': label, 'url': CATALOG, 'expected': len(by_acc), 'filings': len(holdings),
                'missing': len(by_acc) - len(holdings), 'cutoff': max(filed for _, filed in by_acc.values()).isoformat()}
    if (directory / 'source.json').is_file():
        metadata['archive'] = json.loads((directory / 'source.json').read_text())['url']
    filings = {cik: {'accession': acc, 'filed': filed.isoformat()} for cik, (filed, acc) in by_cik.items()}
    return {'holdings': holdings, 'names': names, 'aum': aum, 'filings': filings}, metadata


def matching_filings(sec, quarter, start, end):
    offset = 0
    while True:
        params = {'forms': '13F-HR', 'dateRange': 'custom', 'startdt': start.isoformat(),
                  'enddt': end.isoformat(), 'size': 100, 'from': offset}
        try:
            with sec.get(EFTS, params=params) as response:
                page = response.json()['hits']
            total = page['total']['value']
            rows = page['hits']
            if type(total) is not int or total < 0 or not isinstance(rows, list):
                raise ValueError('Invalid search page')
            if not rows and offset < total:
                raise ValueError('Missing search page')
            for hit in rows:
                row = hit['_source']
                if row['form'] != '13F-HR' or sec_date(row['period_ending']) != quarter.period_ending:
                    continue
                cik = row['ciks'][0].lstrip('0')
                acc, filed = row['adsh'], sec_date(row['file_date'])
                if not re.fullmatch(r'[0-9]{1,10}', cik) or not ACCESSION.fullmatch(acc) or not filed:
                    raise ValueError('Invalid filing metadata')
                name = (row.get('display_names') or [f'CIK {cik}'])[0]
                yield cik, acc, filed, re.sub(r'\s*\(CIK \d+\)$', '', name)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            raise SourceError('SEC 공시 목록 응답 형식이 달라졌습니다.') from exc
        if total >= 10000 or page['total'].get('relation') == 'gte':
            if start == end:
                raise SourceError('하루 공시가 SEC 검색 상한을 초과했습니다.')
            middle = start + (end - start) // 2
            yield from matching_filings(sec, quarter, start, middle)
            yield from matching_filings(sec, quarter, middle + timedelta(days=1), end)
            return
        offset += len(rows)
        if offset >= total:
            return


def has_published_filing(sec, quarter):
    today = edgar_today()
    # For a future quarter use a valid search window and still ask EDGAR.
    start = min(quarter.period_ending, today)
    cutoff = min(today, quarter.edgar_search_end())
    return next(matching_filings(sec, quarter, start, cutoff), None) is not None


def search_edgar(sec, quarter):
    cutoff = min(edgar_today(), quarter.edgar_search_end())
    by_cik = {}
    for record in matching_filings(sec, quarter, min(quarter.period_ending, cutoff), cutoff):
        cik, acc, filed, _ = record
        previous = by_cik.get(cik)
        if previous is None or (filed, acc) > (previous[2], previous[1]):
            by_cik[cik] = record
    return list(by_cik.values()), cutoff


def verified_legacy(rows, total, count):
    if not rows or count != len(rows) or total <= 0:
        return None
    recorded = sum(number(row.get('value')) for row in rows)
    if recorded == total:
        return rows
    if recorded == total * 1000 and all(number(row.get('value')) % 1000 == 0 for row in rows):
        return [dict(row, value=number(row.get('value')) // 1000) for row in rows]
    return None


def filing_documents(sec, base, accession):
    page = html.fromstring(sec.get(f'{base}{accession}-index.htm').content)
    tables, cover = [], None
    for row in page.xpath('//tr'):
        description = ' '.join(row.itertext()).upper()
        for href in row.xpath('.//a/@href'):
            # SEC also links to an XSL display version. Use the original XML file.
            path = urlsplit(href).path
            if not path.lower().endswith('.xml'):
                continue
            full = urljoin(base, Path(path).name)
            if 'INFORMATION TABLE' in description or 'INFOTABLE' in description:
                if full not in tables:
                    tables.append(full)
            elif '13F-HR' in description:
                cover = full
    return tables, cover


def parse_holdings_xml(data):
    root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
    return [
        {'issuer': row.findtext('{*}nameOfIssuer', ''), 'cusip': row.findtext('{*}cusip', ''),
         'value': number(row.findtext('{*}value')), 'shares': number(row.findtext('.//{*}sshPrnamt')),
         'put_call': row.findtext('{*}putCall', '')}
        for row in root.iterfind('.//{*}infoTable')
    ]


def filing_rows(sec, cik, acc, legacy_file):
    base = f'{ARCHIVES}{cik}/{acc.replace("-", "")}/'
    tables, cover = filing_documents(sec, base, acc)
    if legacy_file and legacy_file.is_file() and cover:
        try:
            cover_xml = etree.fromstring(sec.get(cover).content, etree.XMLParser(resolve_entities=False, no_network=True))
            total = number(cover_xml.xpath('string(//*[local-name()="tableValueTotal"])'))
            count = number(cover_xml.xpath('string(//*[local-name()="tableEntryTotal"])'))
            verified = verified_legacy(json.loads(legacy_file.read_text(encoding='utf-8')), total, count)
            if verified is not None:
                return verified, True
        except (SourceError, ValueError, OSError, AttributeError, TypeError, etree.XMLSyntaxError):
            pass
    if tables:
        rows = []
        for url in tables:
            rows.extend(parse_holdings_xml(sec.get(url).content))
        return rows, False
    for name in ('infotable.xml', 'information_table.xml'):
        try:
            rows = parse_holdings_xml(sec.get(urljoin(base, name)).content)
            if rows:
                return rows, False
        except (SourceError, etree.XMLSyntaxError):
            continue
    return [], False


def read_xml(sec, quarter, progress):
    progress('SEC에서 해당 분기 공시 목록 확인 중', None, None, None, 0.04, 0)
    cache = STATE / 'sec' / 'xml' / quarter.label
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    index_path = cache / 'index.json'
    # Refresh discovery for quarters still receiving filings; older lists are reusable.
    if index_path.is_file() and quarter.edgar_search_end() < edgar_today():
        index = json.loads(index_path.read_text(encoding='utf-8'))
        filings, cutoff = index['filings'], sec_date(index['cutoff'])
    else:
        discovered, cutoff = search_edgar(sec, quarter)
        filings = [(cik, acc, filed.isoformat(), name) for cik, acc, filed, name in discovered]
        if filings:
            atomic_write(index_path, json.dumps({'filings': filings, 'cutoff': cutoff.isoformat()}))
    if not filings:
        raise SourceError(f'{quarter.label}에 해당하는 SEC 보유 공시가 아직 없습니다.')

    legacy = resolve_cache_dir_for_quarter(quarter)
    holdings = {}
    names = {cik: name for cik, _, _, name in filings}
    manifest = {cik: {'accession': acc, 'filed': filed} for cik, acc, filed, _ in filings}
    reused, failures_in_a_row = 0, 0
    for done, (cik, acc, filed, name) in enumerate(filings, start=1):
        progress(f'{name} · 공시 보유내역 확인 중', done - 1, len(filings), 'filings', 0.07, 0.92)
        cached = cache / f'{cik}_{acc}.json'
        try:
            if cached.is_file():
                entry = json.loads(cached.read_text(encoding='utf-8'))
                rows, from_legacy = entry['rows'], entry['verified_legacy']
            else:
                candidate = legacy / f'q{quarter.q}_{cik}_{acc.replace("-", "")}.json' if legacy else None
                rows, from_legacy = filing_rows(sec, cik, acc, candidate)
                if rows:
                    atomic_write(cached, json.dumps({'rows': rows, 'verified_legacy': from_legacy}, ensure_ascii=False))
        except (SourceError, ValueError, etree.LxmlError):
            failures_in_a_row += 1
            if failures_in_a_row >= 8:
                raise SourceError('SEC 공시 연결이 연속으로 실패했습니다. 성공한 공시는 재시도할 때 다시 사용합니다.')
            continue
        failures_in_a_row = 0
        if not rows:
            continue
        positions = {}
        for row in rows:
            add_position(positions, issuer=row.get('issuer', ''), cusip=row.get('cusip', ''),
                         value=usd(row.get('value'), sec_date(filed)), shares=number(row.get('shares')),
                         put_call=row.get('put_call', ''))
        if positions:
            holdings[cik] = positions
            reused += int(from_legacy)
    if not holdings:
        raise SourceError(f'{quarter.label} 공시에서 보유내역을 수집하지 못했습니다.')
    metadata = {'source': 'SEC 개별 공시', 'url': 'https://www.sec.gov/edgar/search/', 'expected': len(filings),
                'filings': len(holdings), 'missing': len(filings) - len(holdings), 'cutoff': cutoff.isoformat(),
                'verified_legacy': reused}
    return {'holdings': holdings, 'names': names, 'aum': {}, 'filings': manifest}, metadata


def load_quarter(sec, quarter, progress):
    local = resolve_bulk_dir_for_quarter(quarter)
    managed = STATE / 'sec' / 'bulk' / quarter.label
    for directory, label in ((local, '로컬 SEC 벌크'), (managed, '저장된 SEC 벌크')):
        if directory and all((directory / name).is_file() for name in BULK_FILES):
            try:
                return read_bulk(directory, quarter, label, progress)
            except SourceError:
                continue
    progress('SEC 벌크 공개 여부 확인 중', None, None, None, 0, 0)
    if not managed.exists():
        for url in bulk_urls(sec, quarter):
            try:
                download_bulk(sec, url, managed, progress)
                return read_bulk(managed, quarter, 'SEC 벌크', progress)
            except (SourceError, requests.RequestException):
                if managed.exists():
                    break
                continue  # An unavailable archive can be replaced by another published window.
    return read_xml(sec, quarter, progress)
