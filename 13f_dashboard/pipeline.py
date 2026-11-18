"""Worker entry point for source preparation, comparison and data exports."""

import csv
import json
import re
import sys
import tempfile
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill

from config import ROOT, SECTIONS, parse_quarter
from source import Sec, SourceError, load_quarter
import analyze


EVENT_PREFIX = '@@DRAKOS@@'
WRITERS = {
    'holdings': (analyze.save_all_fund_size_csvs, analyze.save_notable_csv, analyze.save_new_buys_csv),
    'sectors': (analyze.save_sector_csv,),
    'institutions': (analyze.save_filer_count_csv,),
    'top_managers': (analyze.save_top20_fund_size_csvs, analyze.save_top20_detail_csv),
    'options': (analyze.save_option_csvs,),
}
PRIMARY = {
    'holdings': ('01_전체_절대증가_전체', '02_전체_절대감소_전체', '14_신규편입_절대금액순'),
    'sectors': ('09_섹터별_보유액변화',),
    'institutions': ('17_기관수_증가_상위종목', '18_기관수_감소_상위종목'),
    'top_managers': ('12_TOP20_기관_상세', '05_TOP20_절대증가_전체', '06_TOP20_절대감소_전체'),
    'options': ('옵션_절대증가', '옵션_절대감소'),
}
RELABEL = {
    '절대매수': '절대증가', '절대매도': '절대감소', '비율매수': '비율증가', '비율매도': '비율감소',
    '순매수': '증가', '순매도': '감소', '신규매수': '신규편입', '완전퇴출': '보유내역 없음',
    '매수': '증가', '매도': '감소', '자금흐름': '보유액변화', '유입 ↑': '증가', '유출 ↓': '감소',
    '펀드': '기관',
}
FILING_STATUS_FILES = {
    'target_only': ('19_대상분기만_공시기관.csv', '대상 분기에만 공시 확인'),
    'baseline_only': ('20_기준분기만_공시기관.csv', '기준 분기에만 공시 확인'),
    'missing': ('21_공시_수집누락.csv', '공시 확인 후 수집 누락'),
}


def send(event, **fields):
    print(EVENT_PREFIX + json.dumps({'event': event, **fields}, ensure_ascii=False), flush=True)


def update(stage, fraction, message, done=None, total=None, unit=None):
    send('progress', stage=stage, fraction=fraction, message=message, done=done, total=total, unit=unit)


def split_positions(data):
    stocks, options = {}, {}
    for cik, holdings in data['holdings'].items():
        stock_rows, option_rows = {}, {}
        for key, holding in holdings.items():
            (option_rows if holding['put_call'] else stock_rows)[key] = holding
        stocks[cik] = stock_rows
        options[cik] = option_rows
    return dict(data, holdings=stocks), dict(data, holdings=options)


def relabel(text):
    for original, label in RELABEL.items():
        text = text.replace(original, label)
    return text


def signed_number(text):
    return bool(re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text))


def safe_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) and not signed_number(value):
        return "'" + value
    return value


def clean_csv(path, output, compare_label):
    destination = output / relabel(path.name)
    with path.open(encoding='utf-8-sig', newline='') as source, destination.open('w', encoding='utf-8-sig', newline='') as target:
        reader = csv.reader(source)
        writer = csv.writer(target)
        columns = next(reader)
        writer.writerow([relabel(column) for column in columns])
        base = next((i for i, col in enumerate(columns) if col in (f'{compare_label}보유액($)', f'{compare_label}총액($)')), None)
        percent = columns.index('변동률(%)') if '변동률(%)' in columns else None
        for row in reader:
            if base is not None and percent is not None and row[base] == '0' and row[percent] == '10000.0':
                row[percent] = '신규'
            for idx, value in enumerate(row):
                if columns[idx] == '자금흐름':
                    value = relabel(value) if row[columns.index('변동액($)')] != '0' else '동일'
                if columns[idx] == '주목사유':
                    value = relabel(value).replace('급변동(+10000%)', '신규 편입')
                row[idx] = safe_cell(value)
            writer.writerow(row)
    return destination


def as_cell(sheet, text, *, heading=False):
    if not heading and signed_number(text):
        return float(text) if '.' in text else int(text)
    cell = WriteOnlyCell(sheet, value=text)
    cell.data_type = 's'
    if heading:
        cell.font = Font(name='맑은 고딕', bold=True, color='F5F8FF')
        cell.fill = PatternFill('solid', fgColor='103E87')
        cell.alignment = Alignment(vertical='center', wrap_text=True)
    return cell


def workbook(path, tables):
    book = Workbook(write_only=True)
    for label, source in tables:
        sheet = book.create_sheet(label[:31])
        sheet.freeze_panes = 'A2'
        sheet.sheet_format.defaultColWidth = 23
        sheet.column_dimensions['A'].width = 28
        with source.open(encoding='utf-8-sig', newline='') as stream:
            rows = csv.reader(stream)
            sheet.append([as_cell(sheet, text, heading=True) for text in next(rows)])
            for row in rows:
                sheet.append([as_cell(sheet, text) for text in row])
    book.save(path)


def record_file(path, section, root):
    return {'name': path.name, 'path': path.relative_to(root).as_posix(), 'section': section,
            'format': path.suffix[1:].upper(), 'size': path.stat().st_size}


def preview(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        rows = []
        fields = reader.fieldnames or []
        for i, row in enumerate(reader, 1):
            rows.append(dict(row))
            if i == 8:
                break
    return {'title': re.sub(r'^\d{2}_?', '', path.stem).replace('_', ' '), 'columns': fields, 'rows': rows}


def write_overview(path, overview, sources, target, compare):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['항목', '값'])
        writer.writerows([
            ['대상 분기', target.label], ['기준 분기', compare.label],
            ['공통 기관 수', overview['common']], ['보유 종목 수', overview['securities']],
            [f'{compare.label} 보유액 ($)', overview['compare_value']],
            [f'{target.label} 보유액 ($)', overview['target_value']], ['보유액 변화 ($)', overview['change']],
        ])
        for label, source in sources.items():
            writer.writerows([[f'{label} 출처', source['source']], [f'{label} 공시 기준일', source['cutoff']],
                              [f'{label} 확보 공시 수', source['filings']], [f'{label} 누락 공시 수', source['missing']],
                              [f'{label} SEC 주소', source.get('archive') or source['url']]])
        for key, (_, title) in FILING_STATUS_FILES.items():
            writer.writerow([f'{title} 기관 수', overview['filing_counts'][key]])
        writer.writerow(['비교 기준', '두 분기 공통 기관의 공시 보유액을 USD로 비교. 옵션은 풋·콜 항목에 별도 집계.'])
        writer.writerow(['해석', '보유액 변화에는 수량과 가격 변동이 함께 반영됨.'])


def filing_cells(data, cik):
    filing = data['filings'].get(cik)
    if filing is None:
        return ('공시 미확인', '', '', '', None)
    holdings = data['holdings'].get(cik)
    amount = sum(row['value'] for row in holdings.values()) if holdings is not None else None
    return ('확보' if holdings is not None else '수집 누락', safe_cell(data['names'].get(cik, '')),
            filing['accession'], filing['filed'], amount)


def filing_changes(baseline, latest):
    groups = {key: [] for key in FILING_STATUS_FILES}
    for cik in sorted(baseline['filings'].keys() | latest['filings'].keys(), key=int):
        before, after = filing_cells(baseline, cik), filing_cells(latest, cik)
        if '수집 누락' in (before[0], after[0]):
            group = 'missing'
        elif before[0] == '공시 미확인':
            group = 'target_only'
        elif after[0] == '공시 미확인':
            group = 'baseline_only'
        else:
            continue
        groups[group].append((cik, *before, *after))
    return groups


def write_filing_changes(destination, groups, baseline, latest):
    folder = destination / 'institutions'
    folder.mkdir(exist_ok=True)
    columns = ['SEC CIK']
    for label in (baseline.label, latest.label):
        columns.extend(f'{label} {name}' for name in ('공시 상태', '기관명', '공시 번호', '제출일', '보유액 (USD)'))
    for key, (filename, title) in FILING_STATUS_FILES.items():
        path = folder / filename
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(groups[key])
        yield title, path


def export(analysis, option_analysis, target, compare, sections, destination, sources, changes):
    items = analysis['stock_changes'].values() if analysis else ()
    baseline = sum(item['c_value'] for item in items) if analysis else None
    latest = sum(item['t_value'] for item in items) if analysis else None
    change = latest - baseline if analysis else None
    overview = {'common': analysis['common_count'] if analysis else 0, 'securities': len(items),
                'compare_value': baseline, 'target_value': latest, 'change': change,
                'change_pct': round(change / baseline * 100, 2) if baseline else None,
                'filing_counts': {key: len(rows) for key, rows in changes.items()}}
    summary = destination / '00_비교_요약.csv'
    write_overview(summary, overview, sources, target, compare)
    files = [record_file(summary, 'overview', destination)]
    sheets = [('비교 요약', summary)]
    chart_tables = {}

    status_tables = list(write_filing_changes(destination, changes, compare, target))
    selected = sections if analysis else []
    total = len(selected) + 1
    with tempfile.TemporaryDirectory(prefix='.csv-', dir=destination) as staging:
        for i, section in enumerate(selected):
            section_analysis = option_analysis if section == 'options' else analysis
            if section_analysis is None:
                continue
            update('export', i / total, f'{SECTIONS[section]} CSV 작성 중', i, total, 'steps')
            raw = Path(staging) / section
            raw.mkdir()
            output = destination / section
            output.mkdir(exist_ok=True)
            for writer in WRITERS[section]:
                writer(section_analysis, compare.label, target.label, raw)
            csvs = {}
            for path in sorted(raw.glob('*.csv')):
                clean = clean_csv(path, output, compare.label)
                csvs[clean.stem] = clean
                files.append(record_file(clean, section, destination))
            primary = [csvs[name] for name in PRIMARY[section] if name in csvs]
            if not primary:
                primary = list(csvs.values())[:1]
            chart_tables[section] = [preview(path) for path in primary]
            for ordinal, path in enumerate(primary, 1):
                sheets.append((f'{SECTIONS[section]} {ordinal}' if len(primary) > 1 else SECTIONS[section], path))
    for _, path in status_tables:
        files.append(record_file(path, 'institutions', destination))
        chart_tables.setdefault('institutions', []).append(preview(path))
    sheets.extend(status_tables)
    update('export', len(selected) / total, '엑셀 통합문서 작성 중', len(selected), total, 'steps')
    excel = destination / f'13F_{target.label}_vs_{compare.label}.xlsx'
    workbook(excel, sheets)
    files.insert(1, record_file(excel, 'overview', destination))
    update('export', 1, 'CSV와 엑셀 저장 완료', total, total, 'steps')
    return {'overview': overview, 'sources': sources, 'tables': chart_tables, 'files': files}


def run(spec):
    target, compare = parse_quarter(spec['target']), parse_quarter(spec['compare'])
    requested = spec['sections']
    datasets, coverage = {}, {}
    sec = Sec(notify=lambda **status: send('connection', **status))
    try:
        for i, quarter in enumerate((compare, target)):
            def saving(message, done, total, unit, base, span):
                ratio = min(1, done / total) if total and done is not None else 0
                update('source', (i + base + span * ratio) / 2, f'{quarter.label} · {message}', done, total, unit)

            data, meta = load_quarter(sec, quarter, saving)
            datasets[quarter.label] = data
            coverage[quarter.label] = meta
            update('source', (i + 1) / 2, f'{quarter.label} · SEC 자료 준비 완료', meta['filings'], meta['expected'], 'filings')
    finally:
        sec.close()

    update('analysis', 0, '공통 기관과 종목을 연결하는 중')
    mcap = analyze.load_market_caps(ROOT / 'shared' / 'market_caps.json')
    changes = filing_changes(datasets[compare.label], datasets[target.label])
    compare_stock, compare_opt = split_positions(datasets.pop(compare.label))
    target_stock, target_opt = split_positions(datasets.pop(target.label))
    with_options = 'options' in requested

    def compared(done, total):
        update('analysis', 0.08 + 0.82 * done / total * (0.8 if with_options else 1),
               '공통 기관의 보유액 비교 중', done, total, 'institutions')

    analysis = analyze.compare(compare_stock, target_stock, mcap, progress=compared)
    opt = None
    if analysis and with_options:
        def compared_options(done, total):
            update('analysis', 0.74 + 0.16 * done / total, '풋·콜 보유액 비교 중', done, total, 'institutions')

        opt = analyze.compare(compare_opt, target_opt, mcap, progress=compared_options)
    common = analysis['common_count'] if analysis else 0
    update('analysis', 1, '분기 비교 완료', common, common, 'institutions')
    send('result', **export(analysis, opt, target, compare, requested, Path(spec['destination']), coverage, changes))


if __name__ == '__main__':
    try:
        run(json.load(sys.stdin))
    except SourceError as error:
        send('error', message=str(error))
        sys.exit(1)
