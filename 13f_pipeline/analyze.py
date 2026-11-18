#!/usr/bin/env python3
"""
=============================================================================
13F 분기 비교 분석기 (analyze.py)
=============================================================================
임의의 두 분기(target vs compare)를 비교 분석해서 CSV 결과를 만듭니다.

사용법:
    # 기본: 2026Q1 vs 2025Q4 비교
    python3 analyze.py --target 2026Q1 --compare 2025Q4

    # 결과를 특정 폴더로
    python3 analyze.py --target 2026Q1 --compare 2025Q4 \\
        --out-dir ./result/2026Q1

각 분기에 대해 다음 순서로 데이터를 찾습니다:
    1) <ROOT>/13f_q{Q}_{YYYY}/bulk/*.tsv          (SEC 벌크)
    2) <ROOT>/13f_q{Q}_{YYYY}/13f_bulk/q{Q}_*/    (기존 Q4 2025 네이밍 호환)
    3) <ROOT>/13f_q{Q}_{YYYY}/cache/*.json        (collect.py 결과)
    4) <ROOT>/13f_q{Q}_{YYYY}/13f_cache/*.json    (기존 Q4 2025 캐시 호환)

공용 캐시:
    shared/market_caps.json

출력 (기본 <target>/results/ 폴더, 파일명 접두사에 분기 라벨 포함):
    01_전체_절대매수_*.csv, 02_전체_절대매도_*.csv,
    03_전체_비율매수_*.csv, 04_전체_비율매도_*.csv,
    05~08_TOP20_*.csv, 09_섹터별_자금흐름.csv, 10_주목_종목.csv,
    11_전체_통계요약.csv, 12_TOP20_펀드_상세.csv,
    14_신규매수_*.csv, 16~18_기관보유수_*.csv,
    옵션_절대증가/절대감소/비율증가/비율감소.csv
=============================================================================
"""

from __future__ import annotations
import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from quarter_utils import (
    Quarter, ROOT_DIR, SHARED_DIR, configure_utf8_io,
    resolve_cache_dir_for_quarter, resolve_bulk_dir_for_quarter,
)  # noqa: E402


configure_utf8_io()


# ── 섹터 분류 (기존 q4_analyze에서 그대로) ──────────────────────────────────
SECTOR_KEYWORDS = {
    '기술 (Technology)': [
        'APPLE', 'MICROSOFT', 'NVIDIA', 'GOOGLE', 'ALPHABET', 'META PLATFORMS',
        'BROADCOM', 'ORACLE', 'ADOBE', 'SALESFORCE', 'AMD', 'ADVANCED MICRO',
        'INTEL', 'QUALCOMM', 'TEXAS INSTRUMENT', 'INTUIT', 'SERVICENOW',
        'PALO ALTO', 'CROWDSTRIKE', 'SNOWFLAKE', 'DATADOG', 'PALANTIR',
        'CISCO', 'IBM', 'ACCENTURE', 'MICRON', 'APPLIED MATERIAL',
        'SYNOPSYS', 'CADENCE', 'MARVELL', 'NOKIA', 'SOFI', 'UBER',
        'SHOPIFY', 'TWILIO', 'CLOUDFLARE', 'ROKU', 'SNAP',
        'DELL', 'ARISTA', 'FORTINET', 'ZSCALER', 'MONGODB', 'CONFLUENT',
        'ROBLOX', 'PINTEREST', 'SPOTIFY', 'DOCUSIGN', 'OKTA',
        'WORKDAY', 'VEEVA', 'TRADE DESK', 'UNITY SOFTWARE',
    ],
    '헬스케어 (Healthcare)': [
        'UNITEDHEALTH', 'JOHNSON & JOHNSON', 'ELI LILLY', 'LILLY', 'ABBVIE',
        'MERCK', 'PFIZER', 'AMGEN', 'GILEAD', 'REGENERON', 'VERTEX',
        'MODERNA', 'BIOGEN', 'BRISTOL-MYERS', 'THERMO FISHER', 'ABBOTT',
        'DANAHER', 'MEDTRONIC', 'INTUITIVE SURGICAL', 'STRYKER', 'EDWARDS',
        'HOLOGIC', 'ILLUMINA', 'ELANCO', 'MADRIGAL PHARMA', 'ASTRAZENECA',
        'NOVO NORDISK', 'SANOFI', 'ROCHE', 'NOVARTIS', 'ZIMMER',
        'BECTON', 'AGILENT', 'IQVIA', 'CENTENE', 'HUMANA', 'CIGNA',
        'MOLINA', 'CVS HEALTH', 'MCKESSON', 'CARDINAL HEALTH',
    ],
    '금융 (Financials)': [
        'JPMORGAN', 'BANK OF AMERICA', 'WELLS FARGO', 'GOLDMAN SACHS',
        'MORGAN STANLEY', 'CITIGROUP', 'CHARLES SCHWAB', 'BLACKROCK',
        'VISA', 'MASTERCARD', 'AMERICAN EXPRESS', 'INTERCONTINENTAL EXCHANGE',
        'CME GROUP', 'S&P GLOBAL', 'MOODY', 'MSCI', 'PROGRESSIVE',
        'MARSH', 'AON', 'ROCKET COMPANIES', 'DEUTSCHE BANK',
        'BERKSHIRE', 'CAPITAL ONE', 'PNC FINANCIAL', 'US BANCORP',
        'TRUIST', 'FIFTH THIRD', 'DISCOVER FINANCIAL', 'SYNCHRONY',
        'BLOCK INC', 'PAYPAL', 'SQUARE', 'ROBINHOOD', 'COINBASE',
    ],
    '소비재 (Consumer Discretionary)': [
        'AMAZON', 'TESLA', 'HOME DEPOT', 'MCDONALD', 'NIKE', 'STARBUCKS',
        'COSTCO', 'WALMART', 'TARGET', 'LOWE', 'TJX', 'ROSS STORES',
        'BOOKING', 'AIRBNB', 'CHIPOTLE', 'STELLANTIS', 'FORD', 'GM ',
        'GENERAL MOTORS', 'TOYOTA', 'FERRARI', 'RIVIAN', 'LUCID',
        'DECKERS', 'YETI', 'LULULEMON', 'CROCS', 'CARVANA',
    ],
    '필수소비재 (Consumer Staples)': [
        'PROCTER & GAMBLE', 'COCA-COLA', 'PEPSICO', 'PHILIP MORRIS',
        'ALTRIA', 'COLGATE', 'ESTEE LAUDER', 'KIMBERLY', 'MONDELEZ',
        'GENERAL MILLS', 'KRAFT', 'AMBEV', 'ANHEUSER', 'CONSTELLATION BRAND',
        'HERSHEY', 'CHURCH & DWIGHT', 'CLOROX', 'SYSCO',
    ],
    '에너지 (Energy)': [
        'EXXON', 'CHEVRON', 'CONOCOPHILLIPS', 'SCHLUMBERGER', 'EOG',
        'PIONEER', 'MARATHON', 'TOTALENERGIES', 'SHELL', 'BP PLC',
        'PETROBRAS', 'PETROLEO', 'TC ENERGY', 'ENBRIDGE', 'PLUG POWER',
        'DEVON', 'DIAMONDBACK', 'COTERRA', 'HALLIBURTON', 'BAKER HUGHES',
        'WILLIAMS', 'KINDER MORGAN', 'ONEOK', 'TARGA',
    ],
    '산업재 (Industrials)': [
        'CATERPILLAR', 'DEERE', 'HONEYWELL', 'UNION PACIFIC', 'RTX',
        'RAYTHEON', 'LOCKHEED', 'BOEING', 'GENERAL ELECTRIC', '3M ',
        'DUPONT', 'ILLINOIS TOOL', 'PARKER', 'EMERSON', 'EATON',
        'NORTHROP', 'GENERAL DYNAMICS', 'L3HARRIS', 'TEXTRON',
        'WASTE MANAGEMENT', 'REPUBLIC SERVICES', 'CINTAS', 'FASTENAL',
        'UBER TECHNOLOGIES', 'LYFT', 'DEERE & CO',
    ],
    '소재 (Materials)': [
        'LINDE', 'SHERWIN', 'FREEPORT', 'NEWMONT', 'NUCOR', 'VALE',
        'HECLA', 'BARRICK', 'GOLD ', 'BHP', 'RIO TINTO',
        'AIR PRODUCTS', 'DOW INC', 'INTERNATIONAL PAPER', 'CELANESE',
    ],
    '통신 (Communication)': [
        'NETFLIX', 'WALT DISNEY', 'COMCAST', 'T-MOBILE', 'VERIZON',
        'AT&T', 'CHARTER COMM', 'TELUS', 'WARNER BROS', 'PARAMOUNT',
        'FOX CORP', 'ELECTRONIC ARTS', 'TAKE-TWO', 'ACTIVISION',
    ],
    '유틸리티 (Utilities)': [
        'NEXTERA', 'DUKE ENERGY', 'SOUTHERN COMPANY', 'DOMINION',
        'SEMPRA', 'AMERICAN ELECTRIC', 'CONSTELLATION ENERGY', 'VISTRA',
        'EXELON', 'ENTERGY', 'XCEL', 'WEC ENERGY', 'EVERGY',
    ],
    '부동산 (Real Estate)': [
        'PROLOGIS', 'AMERICAN TOWER', 'CROWN CASTLE', 'EQUINIX',
        'DIGITAL REALTY', 'SIMON PROPERTY', 'REALTY INCOME',
        'WELLTOWER', 'PUBLIC STORAGE', 'IRON MOUNTAIN', 'VICI PROP',
    ],
    'ETF/인덱스 (ETF/Index)': [
        'ISHARES', 'SPDR', 'VANGUARD', 'INVESCO QQQ', 'PROSHARES',
        'ARK ', 'VANECK', 'SCHWAB', 'STATE STREET', 'WISDOMTREE',
        'FIRST TRUST', 'GLOBAL X', 'DIREXION',
    ],
}


def classify_sector(issuer_name: str) -> str:
    nm = (issuer_name or '').upper()
    for sector, kws in SECTOR_KEYWORDS.items():
        for kw in kws:
            if kw in nm:
                return sector
    return '기타 (Other)'


# ── 시가총액 bin ────────────────────────────────────────────────────────────
MCAP_BINS = [
    ('시총1억$미만', 0, 100_000_000),
    ('시총10억$미만', 100_000_000, 1_000_000_000),
    ('시총100억$미만', 1_000_000_000, 10_000_000_000),
    ('시총1000억$미만', 10_000_000_000, 100_000_000_000),
    ('시총1000억$이상', 100_000_000_000, float('inf')),
]
SIZE_LABELS = [l for l, _, _ in MCAP_BINS] + ['미분류', '전체']


def get_mcap_label(cusip: str, mcap_data: dict | None) -> str:
    if not mcap_data or cusip not in mcap_data:
        return '미분류'
    mc = mcap_data[cusip].get('market_cap', 0)
    if mc <= 0:
        return '미분류'
    for label, lo, hi in MCAP_BINS:
        if lo <= mc < hi:
            return label
    return '시총1000억$이상'


def load_market_caps(path: Path) -> dict:
    if not path.exists():
        print(f"[WARNING] 시가총액 파일 없음: {path}")
        return {}
    with open(path, encoding='utf-8') as f:
        raw = json.load(f)
    data: dict = {}
    for cusip, info in raw.items():
        key = cusip.upper()
        if key not in data or info.get('market_cap', 0) > data[key].get('market_cap', 0):
            data[key] = info
    with_mc = sum(1 for v in data.values() if v.get('market_cap', 0) > 0)
    print(f"[시가총액] {len(raw)}개 CUSIP → {len(data)}개 정규화, 시가총액 있음: {with_mc}개")
    return data


# ── 벌크 TSV 로드 ───────────────────────────────────────────────────────────
def load_bulk(bulk_dir: Path) -> dict | None:
    """SEC 벌크 TSV 폴더에서 {holdings, names, aum} 반환. 없으면 None."""
    info_path = bulk_dir / 'INFOTABLE.tsv'
    sub_path = bulk_dir / 'SUBMISSION.tsv'
    cover_path = bulk_dir / 'COVERPAGE.tsv'
    summary_path = bulk_dir / 'SUMMARYPAGE.tsv'
    if not info_path.exists():
        return None

    print(f"[벌크] 로딩: {bulk_dir}")

    acc_to_cik: dict = {}
    with open(sub_path, encoding='utf-8', errors='replace') as f:
        for row in csv.DictReader(f, delimiter='\t'):
            acc = row.get('ACCESSION_NUMBER', '').strip()
            cik = row.get('CIK', '').strip().lstrip('0') or '0'
            acc_to_cik[acc] = cik

    cik_to_name: dict = {}
    with open(cover_path, encoding='utf-8', errors='replace') as f:
        for row in csv.DictReader(f, delimiter='\t'):
            acc = row.get('ACCESSION_NUMBER', '').strip()
            name = row.get('FILINGMANAGER_NAME', '').strip()
            cik = acc_to_cik.get(acc, '')
            if cik and name:
                cik_to_name[cik] = name

    cik_to_aum: dict = {}
    with open(summary_path, encoding='utf-8', errors='replace') as f:
        for row in csv.DictReader(f, delimiter='\t'):
            acc = row.get('ACCESSION_NUMBER', '').strip()
            total_str = row.get('TABLEVALUETOTAL', '0').strip()
            cik = acc_to_cik.get(acc, '')
            try:
                aum = int(float(total_str))
            except ValueError:
                aum = 0
            if cik:
                cik_to_aum[cik] = max(cik_to_aum.get(cik, 0), aum)

    holdings: dict = defaultdict(dict)
    count = 0
    with open(info_path, encoding='utf-8', errors='replace') as f:
        for row in csv.DictReader(f, delimiter='\t'):
            acc = row.get('ACCESSION_NUMBER', '').strip()
            cik = acc_to_cik.get(acc, '')
            if not cik:
                continue
            issuer = row.get('NAMEOFISSUER', '').strip().upper()
            cusip = row.get('CUSIP', '').strip().upper()
            put_call = row.get('PUTCALL', '').strip()
            try:
                value = int(row.get('VALUE', '0').strip())
            except ValueError:
                value = 0
            try:
                shares = int(row.get('SSHPRNAMT', '0').strip())
            except ValueError:
                shares = 0

            base_key = cusip if cusip else issuer
            key = f'{base_key}_{put_call}' if put_call in ('Call', 'Put') else base_key
            if key in holdings[cik]:
                holdings[cik][key]['value'] += value
                holdings[cik][key]['shares'] += shares
            else:
                holdings[cik][key] = {
                    'issuer': issuer, 'cusip': cusip,
                    'value': value, 'shares': shares, 'put_call': put_call,
                }
            count += 1

    print(f"[벌크] 완료: {len(holdings)} 펀드, {count:,} 보유종목")
    return {'holdings': dict(holdings), 'names': cik_to_name, 'aum': cik_to_aum}


# ── 캐시 (per-filing JSON) 로드 ─────────────────────────────────────────────
def load_cache(cache_dir: Path, quarter: Quarter, divide_by_1000: bool = True) -> dict:
    """collect.py가 만든 개별 파일링 JSON들을 병합.
    raw XML value가 $1000 단위인 필러가 많아 기본은 ÷1000 보정.
    """
    data: dict = {}
    names: dict = {}

    # EFTS 인덱스로 펀드명 보강
    idx = quarter.efts_index_path
    if idx.exists():
        try:
            with open(idx, encoding='utf-8') as f:
                for e in json.load(f):
                    cik = e.get('cik', '')
                    if cik:
                        names[cik] = e.get('company', '')
        except Exception:
            pass

    total_holdings = 0
    if not cache_dir.exists():
        print(f"[캐시] 경로 없음: {cache_dir}")
        return {'holdings': {}, 'names': names}

    for fn in sorted(cache_dir.iterdir()):
        if not fn.name.endswith('.json') or fn.stat().st_size < 10:
            continue
        parts = fn.stem.split('_')
        if len(parts) < 2:
            continue
        cik = parts[1]

        try:
            with open(fn, encoding='utf-8') as f:
                holdings = json.load(f)
        except Exception:
            continue
        if not holdings:
            continue

        merged: dict = {}
        for h in holdings:
            cusip = (h.get('cusip') or '').upper()
            issuer = (h.get('issuer') or '').upper()
            put_call = (h.get('put_call') or '').strip()
            base_key = cusip if cusip else issuer
            key = f'{base_key}_{put_call}' if put_call in ('Call', 'Put') else base_key
            raw_val = h.get('value', 0) or 0
            if divide_by_1000 and raw_val > 0:
                val = raw_val // 1000
            else:
                val = raw_val
            if key in merged:
                merged[key]['value'] += val
                merged[key]['shares'] += h.get('shares', 0) or 0
            else:
                merged[key] = {
                    'issuer': issuer, 'cusip': cusip, 'value': val,
                    'shares': h.get('shares', 0) or 0, 'put_call': put_call,
                }
        data[cik] = merged
        total_holdings += len(merged)

    print(f"[캐시] {cache_dir}: {len(data)} 펀드, {total_holdings:,} 보유종목")
    return {'holdings': data, 'names': names}


def load_quarter_data(q: Quarter, divide_by_1000: bool = True) -> dict | None:
    """분기 하나에 대해 가장 신선한 소스(벌크 > 캐시)로 로드."""
    bulk_dir = resolve_bulk_dir_for_quarter(q)
    if bulk_dir and (bulk_dir / 'INFOTABLE.tsv').exists():
        out = load_bulk(bulk_dir)
        if out:
            out.setdefault('aum', {})
            return out

    cache_dir = resolve_cache_dir_for_quarter(q)
    if cache_dir and cache_dir.exists():
        out = load_cache(cache_dir, q, divide_by_1000=divide_by_1000)
        if out and out['holdings']:
            out.setdefault('aum', {})
            return out

    print(f"[ERROR] {q.label} 데이터 없음 (bulk/cache 둘 다 못 찾음)")
    return None


# ── 비교 ────────────────────────────────────────────────────────────────────
def compare(compare_data: dict, target_data: dict, mcap_data: dict | None, *, progress=None):
    c_ciks = set(compare_data['holdings'].keys())
    t_ciks = set(target_data['holdings'].keys())
    common = c_ciks & t_ciks
    print(f"\n[비교] compare: {len(c_ciks)}, target: {len(t_ciks)}, 공통: {len(common)}")
    if len(common) == 0:
        print("[WARNING] 공통 펀드 없음")
        return None

    stock_changes: dict = defaultdict(lambda: {
        'issuer': '', 'c_value': 0, 't_value': 0,
        'c_filers': 0, 't_filers': 0, 'sector': '',
    })

    fund_list: list = []

    for processed, cik in enumerate(sorted(common), 1):
        c_aum = compare_data.get('aum', {}).get(cik, 0)
        t_aum_sum = sum(h['value'] for h in target_data['holdings'][cik].values())
        c_aum_sum = sum(h['value'] for h in compare_data['holdings'][cik].values())
        fund_aum = max(c_aum, t_aum_sum, c_aum_sum)
        fund_name = (compare_data['names'].get(cik)
                     or target_data['names'].get(cik)
                     or f'CIK-{cik}')

        ch = compare_data['holdings'].get(cik, {})
        th = target_data['holdings'].get(cik, {})
        all_keys = set(ch.keys()) | set(th.keys())

        fund_buys = []
        fund_sells = []

        for key in sorted(all_keys):
            c_val = ch.get(key, {}).get('value', 0)
            t_val = th.get(key, {}).get('value', 0)
            change = t_val - c_val
            issuer = th.get(key, ch.get(key, {})).get('issuer', key)

            sc = stock_changes[key]
            if mcap_data and key in mcap_data and mcap_data[key].get('issuer'):
                sc['issuer'] = mcap_data[key]['issuer']
            elif not sc['issuer']:
                sc['issuer'] = issuer
            sc['c_value'] += c_val
            sc['t_value'] += t_val
            if c_val > 0: sc['c_filers'] += 1
            if t_val > 0: sc['t_filers'] += 1
            sc['sector'] = classify_sector(issuer)

            if change > 0:
                fund_buys.append((issuer, change, c_val, t_val))
            elif change < 0:
                fund_sells.append((issuer, change, c_val, t_val))

        fund_buys.sort(key=lambda x: -x[1])
        fund_sells.sort(key=lambda x: x[1])
        fund_list.append({
            'cik': cik, 'name': fund_name, 'aum': fund_aum,
            'c_total': c_aum_sum, 't_total': t_aum_sum,
            'top_buys': fund_buys[:10], 'top_sells': fund_sells[:10],
        })
        if progress and (processed % 100 == 0 or processed == len(common)):
            progress(processed, len(common))

    fund_list.sort(key=lambda x: -x['aum'])

    size_changes: dict = {sl: {} for sl in SIZE_LABELS}
    for key, data in stock_changes.items():
        sl = get_mcap_label(key, mcap_data)
        entry = {'issuer': data['issuer'], 'c_value': data['c_value'], 't_value': data['t_value']}
        size_changes[sl][key] = entry
        size_changes['전체'][key] = entry.copy()

    top20_changes: dict = defaultdict(lambda: {'issuer': '', 'c_value': 0, 't_value': 0})
    for fund in fund_list[:20]:
        cik = fund['cik']
        ch = compare_data['holdings'].get(cik, {})
        th = target_data['holdings'].get(cik, {})
        for key in sorted(ch.keys() | th.keys()):
            c_val = ch.get(key, {}).get('value', 0)
            t_val = th.get(key, {}).get('value', 0)
            issuer = th.get(key, ch.get(key, {})).get('issuer', key)
            top20_changes[key]['issuer'] = issuer
            top20_changes[key]['c_value'] += c_val
            top20_changes[key]['t_value'] += t_val

    top20_size: dict = {sl: {} for sl in SIZE_LABELS}
    for key, data in top20_changes.items():
        sl = get_mcap_label(key, mcap_data)
        top20_size[sl][key] = dict(data)
        top20_size['전체'][key] = dict(data)

    return {
        'stock_changes': dict(stock_changes),
        'size_changes': size_changes,
        'top20_size_changes': top20_size,
        'fund_list': fund_list,
        'common_count': len(common),
        'mcap_data': mcap_data,
    }


# ── CSV 유틸 ────────────────────────────────────────────────────────────────
def _write_csv(path: Path, rows: list, fields: list) -> int:
    try:
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)
    except PermissionError:
        alt = path.with_name(path.stem + '_new' + path.suffix)
        with open(alt, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        print(f"    (잠김→{alt.name}에 저장)")
        return len(rows)


def _make_items(changes: dict, c_label: str, t_label: str, mcap_data: dict | None):
    items = []
    for key, data in changes.items():
        cv = data['c_value']
        tv = data['t_value']
        change = tv - cv
        if cv > 0:
            pct = change / cv * 100
        elif tv > 0:
            pct = 10000.0  # 신규
        else:
            pct = 0.0

        ticker = ''
        market_cap = 0
        if mcap_data and key in mcap_data:
            ticker = mcap_data[key].get('ticker', '')
            market_cap = mcap_data[key].get('market_cap', 0)

        items.append({
            '종목': data['issuer'],
            '티커': ticker,
            '시가총액($)': market_cap,
            f'{c_label}보유액($)': cv,
            f'{t_label}보유액($)': tv,
            '변동액($)': change,
            '변동률(%)': round(pct, 2),
            '섹터': classify_sector(data['issuer']),
        })
    return items


# ── 저장 함수들 (기존 q4_analyze 동일 로직, 컬럼/파일명에 분기 라벨) ─────────
def save_all_fund_size_csvs(analysis, c_label, t_label, results_dir, top_n=200):
    fields = ['순위', '종목', '티커', '시가총액($)', f'{c_label}보유액($)',
              f'{t_label}보유액($)', '변동액($)', '변동률(%)', '섹터']
    total_files = 0
    for sl in SIZE_LABELS:
        changes = analysis['size_changes'].get(sl, {})
        items = _make_items(changes, c_label, t_label, analysis.get('mcap_data'))
        buys = [i for i in items if i['변동액($)'] > 0]
        sells = [i for i in items if i['변동액($)'] < 0]

        for num, label_name, rows, key, reverse in [
            (1, '절대매수', sorted(buys, key=lambda x: -x['변동액($)']), None, False),
            (2, '절대매도', sorted(sells, key=lambda x: x['변동액($)']), None, False),
            (3, '비율매수', sorted([i for i in buys if i[f'{c_label}보유액($)'] > 0],
                                   key=lambda x: -x['변동률(%)']), None, False),
            (4, '비율매도', sorted([i for i in sells if i[f'{c_label}보유액($)'] > 0],
                                   key=lambda x: x['변동률(%)']), None, False),
        ]:
            for idx, r in enumerate(rows):
                r['순위'] = idx + 1
            fn = f'{num:02d}_전체_{label_name}_{sl}.csv'
            _write_csv(results_dir / fn, rows, fields)
            print(f"  → {fn} ({len(rows)}행)")
            total_files += 1
    print(f"  ▶ 전체 펀드 규모별 CSV: {total_files}개")
    return total_files


def save_top20_fund_size_csvs(analysis, c_label, t_label, results_dir, top_n=200):
    fields = ['순위', '종목', '티커', '시가총액($)', f'{c_label}보유액($)',
              f'{t_label}보유액($)', '변동액($)', '변동률(%)', '섹터']
    total_files = 0
    for sl in SIZE_LABELS:
        changes = analysis['top20_size_changes'].get(sl, {})
        items = _make_items(changes, c_label, t_label, analysis.get('mcap_data'))
        buys = [i for i in items if i['변동액($)'] > 0]
        sells = [i for i in items if i['변동액($)'] < 0]
        for num, label_name, rows in [
            (5, '절대매수', sorted(buys, key=lambda x: -x['변동액($)'])),
            (6, '절대매도', sorted(sells, key=lambda x: x['변동액($)'])),
            (7, '비율매수', sorted([i for i in buys if i[f'{c_label}보유액($)'] > 0],
                                   key=lambda x: -x['변동률(%)'])),
            (8, '비율매도', sorted([i for i in sells if i[f'{c_label}보유액($)'] > 0],
                                   key=lambda x: x['변동률(%)'])),
        ]:
            for idx, r in enumerate(rows):
                r['순위'] = idx + 1
            fn = f'{num:02d}_TOP20_{label_name}_{sl}.csv'
            _write_csv(results_dir / fn, rows, fields)
            print(f"  → {fn} ({len(rows)}행)")
            total_files += 1
    print(f"  ▶ TOP20 펀드 규모별 CSV: {total_files}개")
    return total_files


def save_sector_csv(analysis, c_label, t_label, results_dir):
    sector_totals = defaultdict(lambda: {'c': 0, 't': 0, 'buy_count': 0, 'sell_count': 0})
    sector_stocks: dict = defaultdict(list)

    for key, data in analysis['stock_changes'].items():
        sector = data['sector']
        cv = data['c_value']
        tv = data['t_value']
        change = tv - cv
        pct = (change / cv * 100) if cv > 0 else (10000.0 if tv > 0 else 0)

        sector_totals[sector]['c'] += cv
        sector_totals[sector]['t'] += tv
        if change > 0:
            sector_totals[sector]['buy_count'] += 1
        elif change < 0:
            sector_totals[sector]['sell_count'] += 1

        sector_stocks[sector].append({
            '종목': data['issuer'], '변동액($)': change, '변동률(%)': round(pct, 2),
            f'{c_label}보유액($)': cv, f'{t_label}보유액($)': tv,
        })

    rows = []
    for sector, d in sorted(sector_totals.items(), key=lambda x: -(x[1]['t'] - x[1]['c'])):
        change = d['t'] - d['c']
        pct = (change / d['c'] * 100) if d['c'] > 0 else 0
        rows.append({
            '섹터': sector,
            f'{c_label}총액($)': d['c'], f'{t_label}총액($)': d['t'],
            '변동액($)': change, '변동률(%)': round(pct, 2),
            '순매수종목수': d['buy_count'], '순매도종목수': d['sell_count'],
            '자금흐름': '유입 ↑' if change > 0 else '유출 ↓',
        })
    _write_csv(results_dir / '09_섹터별_자금흐름.csv', rows,
               ['섹터', f'{c_label}총액($)', f'{t_label}총액($)', '변동액($)',
                '변동률(%)', '순매수종목수', '순매도종목수', '자금흐름'])
    print(f"  → 09_섹터별_자금흐름.csv ({len(rows)}행)")

    detail_rows = []
    for sector in sorted(sector_stocks.keys()):
        stocks = sorted(sector_stocks[sector], key=lambda x: -abs(x['변동액($)']))
        for rank, s in enumerate(stocks, 1):
            detail_rows.append({
                '섹터': sector, '순위': rank,
                '종목': s['종목'], '변동액($)': s['변동액($)'], '변동률(%)': s['변동률(%)'],
                f'{c_label}보유액($)': s[f'{c_label}보유액($)'],
                f'{t_label}보유액($)': s[f'{t_label}보유액($)'],
            })
    _write_csv(results_dir / '09b_섹터별_종목상세.csv', detail_rows,
               ['섹터', '순위', '종목', f'{c_label}보유액($)', f'{t_label}보유액($)',
                '변동액($)', '변동률(%)'])
    print(f"  → 09b_섹터별_종목상세.csv ({len(detail_rows)}행)")


def save_notable_csv(analysis, c_label, t_label, results_dir):
    rows = []
    for key, data in analysis['stock_changes'].items():
        cv = data['c_value']
        tv = data['t_value']
        change = tv - cv
        pct = (change / cv * 100) if cv > 0 else (10000.0 if tv > 0 else 0)
        filer_change = data['t_filers'] - data['c_filers']
        reasons = []
        if cv == 0 and tv > 500_000:
            reasons.append('신규진입')
        if tv == 0 and cv > 500_000:
            reasons.append('완전퇴출')
        if abs(pct) > 50 and abs(change) > 5_000_000:
            reasons.append(f'급변동({pct:+.0f}%)')
        if abs(filer_change) >= 2:
            reasons.append(f'펀드수변동({filer_change:+d})')
        if abs(change) > 50_000_000:
            reasons.append('대규모변동')
        if reasons:
            rows.append({
                '순위': 0,
                '종목': data['issuer'], '섹터': data['sector'],
                f'{c_label}보유액($)': cv, f'{t_label}보유액($)': tv,
                '변동액($)': change, '변동률(%)': round(pct, 2),
                f'{c_label}펀드수': data['c_filers'],
                f'{t_label}펀드수': data['t_filers'],
                '펀드수변동': filer_change, '주목사유': ', '.join(reasons),
            })
    rows.sort(key=lambda x: -abs(x['변동액($)']))
    for idx, r in enumerate(rows):
        r['순위'] = idx + 1
    _write_csv(results_dir / '10_주목_종목.csv', rows,
               ['순위', '종목', '섹터', f'{c_label}보유액($)', f'{t_label}보유액($)',
                '변동액($)', '변동률(%)', f'{c_label}펀드수', f'{t_label}펀드수',
                '펀드수변동', '주목사유'])
    print(f"  → 10_주목_종목.csv ({len(rows)}행)")


def save_summary_csv(analysis, c_label, t_label, c_period, t_period, results_dir):
    sc = analysis['stock_changes']
    total_c = sum(d['c_value'] for d in sc.values())
    total_t = sum(d['t_value'] for d in sc.values())
    total_change = total_t - total_c
    buys = sorted(sc.items(), key=lambda x: -(x[1]['t_value'] - x[1]['c_value']))
    sells = sorted(sc.items(), key=lambda x: (x[1]['t_value'] - x[1]['c_value']))
    rows = [
        {'항목': '분석기간', '값': f'{t_label} ({t_period}) vs {c_label} ({c_period})'},
        {'항목': '공통 펀드 수', '값': str(analysis['common_count'])},
        {'항목': f'{c_label} 전체 보유액', '값': f'${total_c:,.0f}'},
        {'항목': f'{t_label} 전체 보유액', '값': f'${total_t:,.0f}'},
        {'항목': '전체 변동액', '값': f'${total_change:+,.0f}'},
        {'항목': '전체 변동률',
         '값': f'{total_change/total_c*100:+.2f}%' if total_c else 'N/A'},
        {'항목': '분석 종목 수', '값': str(len(sc))},
        {'항목': '순매수 종목 수',
         '값': str(sum(1 for d in sc.values() if d['t_value'] > d['c_value']))},
        {'항목': '순매도 종목 수',
         '값': str(sum(1 for d in sc.values() if d['t_value'] < d['c_value']))},
        {'항목': '---', '값': '--- 최다 순매수 TOP 20 ---'},
    ]
    for key, d in buys[:20]:
        ch = d['t_value'] - d['c_value']
        rows.append({'항목': f'매수 {d["issuer"]}', '값': f'+${ch:,.0f}'})
    rows.append({'항목': '---', '값': '--- 최다 순매도 TOP 20 ---'})
    for key, d in sells[:20]:
        ch = d['t_value'] - d['c_value']
        rows.append({'항목': f'매도 {d["issuer"]}', '값': f'-${abs(ch):,.0f}'})
    _write_csv(results_dir / '11_전체_통계요약.csv', rows, ['항목', '값'])
    print(f"  → 11_전체_통계요약.csv ({len(rows)}행)")


def save_top20_detail_csv(analysis, c_label, t_label, results_dir):
    rows = []
    for rank, fund in enumerate(analysis['fund_list'][:20], 1):
        total_change = fund['t_total'] - fund['c_total']
        pct = (total_change / fund['c_total'] * 100) if fund['c_total'] > 0 else 0
        buy_str = '; '.join([f"{b[0]}(+${b[1]:,.0f})" for b in fund['top_buys'][:5]])
        sell_str = '; '.join([f"{s[0]}(-${abs(s[1]):,.0f})" for s in fund['top_sells'][:5]])
        rows.append({
            '순위': rank, '펀드명': fund['name'], 'AUM($)': fund['aum'],
            f'{c_label}총액($)': fund['c_total'], f'{t_label}총액($)': fund['t_total'],
            '변동액($)': total_change, '변동률(%)': round(pct, 2),
            '상위매수종목': buy_str, '상위매도종목': sell_str,
        })
    _write_csv(results_dir / '12_TOP20_펀드_상세.csv', rows,
               ['순위', '펀드명', 'AUM($)', f'{c_label}총액($)', f'{t_label}총액($)',
                '변동액($)', '변동률(%)', '상위매수종목', '상위매도종목'])
    print(f"  → 12_TOP20_펀드_상세.csv ({len(rows)}행)")


def save_new_buys_csv(analysis, c_label, t_label, results_dir):
    mcap_data = analysis.get('mcap_data', {}) or {}
    sc = analysis['stock_changes']
    new_buys = []
    for key, data in sc.items():
        if data['c_value'] == 0 and data['t_value'] > 0:
            ticker = ''
            market_cap = 0
            if key in mcap_data:
                ticker = mcap_data[key].get('ticker', '')
                market_cap = mcap_data[key].get('market_cap', 0)
            mcap_ratio = round(data['t_value'] / market_cap * 100, 4) if market_cap > 0 else 0
            new_buys.append({
                '순위': 0,
                '종목': data['issuer'], '티커': ticker,
                '시가총액($)': market_cap,
                f'{t_label}매수액($)': data['t_value'],
                '시총대비매수비율(%)': mcap_ratio,
                f'{t_label}보유기관수': data['t_filers'],
                '섹터': data['sector'],
                '시총구간': get_mcap_label(key, mcap_data),
            })
    fields = ['순위', '종목', '티커', '시가총액($)', f'{t_label}매수액($)',
              '시총대비매수비율(%)', f'{t_label}보유기관수', '섹터', '시총구간']
    by_abs = sorted(new_buys, key=lambda x: -x[f'{t_label}매수액($)'])
    for idx, r in enumerate(by_abs):
        r['순위'] = idx + 1
    _write_csv(results_dir / '14_신규매수_절대금액순.csv', by_abs, fields)
    print(f"  → 14_신규매수_절대금액순.csv ({len(by_abs)}행)")

    by_ratio = sorted([r for r in new_buys if r['시가총액($)'] > 0],
                      key=lambda x: -x['시총대비매수비율(%)'])
    for idx, r in enumerate(by_ratio):
        r['순위'] = idx + 1
    _write_csv(results_dir / '15_신규매수_시총대비비율순.csv', by_ratio, fields)
    print(f"  → 15_신규매수_시총대비비율순.csv ({len(by_ratio)}행)")

    for sl in (label for label in SIZE_LABELS if label != '전체'):
        subset = [r for r in new_buys if r['시총구간'] == sl]
        subset_abs = sorted(subset, key=lambda x: -x[f'{t_label}매수액($)'])
        for idx, r in enumerate(subset_abs):
            r['순위'] = idx + 1
        fn = f'14_신규매수_절대금액순_{sl}.csv'
        _write_csv(results_dir / fn, subset_abs, fields)
        print(f"  → {fn} ({len(subset_abs)}행)")
    print(f"  ▶ 신규매수: 전체 {len(new_buys)}개")


def save_filer_count_csv(analysis, c_label, t_label, results_dir):
    mcap_data = analysis.get('mcap_data', {}) or {}
    sc = analysis['stock_changes']
    rows = []
    for key, data in sc.items():
        if data['t_filers'] == 0:
            continue
        ticker = ''
        market_cap = 0
        if key in mcap_data:
            ticker = mcap_data[key].get('ticker', '')
            market_cap = mcap_data[key].get('market_cap', 0)
        filer_change = data['t_filers'] - data['c_filers']
        rows.append({
            '순위': 0,
            '종목': data['issuer'], '티커': ticker, '시가총액($)': market_cap,
            f'{t_label}보유기관수': data['t_filers'],
            f'{c_label}보유기관수': data['c_filers'],
            '기관수변동': filer_change,
            f'{t_label}보유액($)': data['t_value'],
            f'{c_label}보유액($)': data['c_value'],
            '변동액($)': data['t_value'] - data['c_value'],
            '섹터': data['sector'],
        })
    fields = ['순위', '종목', '티커', '시가총액($)',
              f'{t_label}보유기관수', f'{c_label}보유기관수', '기관수변동',
              f'{t_label}보유액($)', f'{c_label}보유액($)', '변동액($)', '섹터']
    rows.sort(key=lambda x: -x[f'{t_label}보유기관수'])
    for idx, r in enumerate(rows):
        r['순위'] = idx + 1
    _write_csv(results_dir / '16_기관보유수_상위종목.csv', rows, fields)
    print(f"  → 16_기관보유수_상위종목.csv ({len(rows)}행)")

    increased = sorted([r for r in rows if r['기관수변동'] > 0],
                       key=lambda x: -x['기관수변동'])
    for idx, r in enumerate(increased):
        r['순위'] = idx + 1
    _write_csv(results_dir / '17_기관수_증가_상위종목.csv', increased, fields)
    print(f"  → 17_기관수_증가_상위종목.csv ({len(increased)}행)")

    decreased = sorted([r for r in rows if r['기관수변동'] < 0],
                       key=lambda x: x['기관수변동'])
    for idx, r in enumerate(decreased):
        r['순위'] = idx + 1
    _write_csv(results_dir / '18_기관수_감소_상위종목.csv', decreased, fields)
    print(f"  → 18_기관수_감소_상위종목.csv ({len(decreased)}행)")


def save_option_csvs(analysis, c_label, t_label, results_dir):
    """옵션(put/call) 포지션만 필터해서 4개 CSV 생성."""
    mcap_data = analysis.get('mcap_data', {}) or {}
    sc = analysis['stock_changes']
    items = []
    for key, data in sc.items():
        if key.endswith('_Call'):
            kind = 'CALL'
            base_key = key[:-5]
        elif key.endswith('_Put'):
            kind = 'PUT'
            base_key = key[:-4]
        else:
            continue

        cv = data['c_value']
        tv = data['t_value']
        change = tv - cv
        if cv > 0:
            pct = change / cv * 100
        elif tv > 0:
            pct = 10000.0
        else:
            pct = 0.0

        ticker = ''
        if base_key in mcap_data:
            ticker = mcap_data[base_key].get('ticker', '')

        items.append({
            '순위': 0,
            '종목': f"{data['issuer']}({kind})",
            '티커': ticker,
            '섹터': data['sector'] or classify_sector(data['issuer']),
            f'{c_label}보유액($)': cv,
            f'{t_label}보유액($)': tv,
            '변동액($)': change,
            '변동률(%)': round(pct, 2),
        })

    fields = ['순위', '종목', '티커', '섹터',
              f'{c_label}보유액($)', f'{t_label}보유액($)', '변동액($)', '변동률(%)']

    def _emit(filename, rows):
        for idx, r in enumerate(rows):
            r['순위'] = idx + 1
        _write_csv(results_dir / filename, rows, fields)
        print(f"  → {filename} ({len(rows)}행)")

    abs_up = sorted([dict(i) for i in items if i['변동액($)'] > 0],
                    key=lambda x: -x['변동액($)'])
    abs_dn = sorted([dict(i) for i in items if i['변동액($)'] < 0],
                    key=lambda x: x['변동액($)'])
    pct_up = sorted([dict(i) for i in items
                     if i['변동액($)'] > 0 and i[f'{c_label}보유액($)'] > 0],
                    key=lambda x: -x['변동률(%)'])
    pct_dn = sorted([dict(i) for i in items
                     if i['변동액($)'] < 0 and i[f'{c_label}보유액($)'] > 0],
                    key=lambda x: x['변동률(%)'])

    _emit('옵션_절대증가.csv', abs_up)
    _emit('옵션_절대감소.csv', abs_dn)
    _emit('옵션_비율증가.csv', pct_up)
    _emit('옵션_비율감소.csv', pct_dn)


# ── 메인 ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='13F 분기 비교 분석 (템플릿)')
    parser.add_argument('--target', required=True, help='최신 분기 (예: 2026Q1)')
    parser.add_argument('--compare', required=True, help='비교 기준 분기 (예: 2025Q4)')
    parser.add_argument('--out-dir', default=None, help='결과 폴더 (기본: <target>/results)')
    parser.add_argument('--mcap-file', default=None,
                        help='시가총액 JSON (기본: shared/market_caps.json)')
    parser.add_argument('--top', type=int, default=200, help='카테고리당 상위 N개')
    parser.add_argument('--target-no-divide', action='store_true',
                        help='target 캐시 값을 ÷1000 하지 않음 (값이 이미 달러일 때)')
    parser.add_argument('--compare-no-divide', action='store_true',
                        help='compare 캐시 값을 ÷1000 하지 않음')
    args = parser.parse_args()

    target_q = Quarter.parse(args.target)
    compare_q = Quarter.parse(args.compare)

    results_dir = Path(args.out_dir) if args.out_dir else (target_q.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    mcap_path = Path(args.mcap_file) if args.mcap_file else (SHARED_DIR / 'market_caps.json')

    print("=" * 70)
    print(f"13F 비교 분석: {target_q.label} vs {compare_q.label}")
    print(f"실행: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"결과 폴더: {results_dir}")
    print("=" * 70)

    mcap_data = load_market_caps(mcap_path)

    print(f"\n[로드] {compare_q.label}")
    compare_data = load_quarter_data(compare_q, divide_by_1000=not args.compare_no_divide)
    if not compare_data:
        return

    print(f"\n[로드] {target_q.label}")
    target_data = load_quarter_data(target_q, divide_by_1000=not args.target_no_divide)
    if not target_data:
        return

    print(f"\n[분석] {target_q.label} vs {compare_q.label}")
    analysis = compare(compare_data, target_data, mcap_data)
    if not analysis:
        return

    c_label = compare_q.label  # 예: '2025Q4'
    t_label = target_q.label   # 예: '2026Q1'

    print("\n[CSV] 시가총액별 전체 펀드...")
    save_all_fund_size_csvs(analysis, c_label, t_label, results_dir, top_n=args.top)

    print("\n[CSV] 시가총액별 TOP20 펀드...")
    save_top20_fund_size_csvs(analysis, c_label, t_label, results_dir, top_n=args.top)

    print("\n[CSV] 섹터/주목종목/요약...")
    save_sector_csv(analysis, c_label, t_label, results_dir)
    save_notable_csv(analysis, c_label, t_label, results_dir)
    save_summary_csv(analysis, c_label, t_label,
                     compare_q.period_ending_str, target_q.period_ending_str,
                     results_dir)
    save_top20_detail_csv(analysis, c_label, t_label, results_dir)

    print("\n[CSV] 신규매수...")
    save_new_buys_csv(analysis, c_label, t_label, results_dir)

    print("\n[CSV] 기관보유수...")
    save_filer_count_csv(analysis, c_label, t_label, results_dir)

    print("\n[CSV] 옵션(put/call)...")
    save_option_csvs(analysis, c_label, t_label, results_dir)

    csv_count = len(list(results_dir.glob('*.csv')))
    print(f"\n{'='*70}")
    print(f"분석 완료! 총 {csv_count}개 CSV")
    print(f"결과: {results_dir}")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
