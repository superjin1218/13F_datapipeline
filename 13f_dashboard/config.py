"""Paths and section names shared by the local dashboard and its worker."""

import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
PIPELINE = ROOT / '13f_pipeline'
STATE = Path(os.environ.get('DRAKOS_STATE_DIR', APP_DIR / '.state')).expanduser().resolve()
EXPORTS = ROOT / 'result'

sys.path.insert(0, str(PIPELINE))
from quarter_utils import Quarter  # noqa: E402


SECTIONS = {
    'holdings': '보유액 변화',
    'sectors': '섹터',
    'institutions': '보유기관 수',
    'top_managers': '상위 기관',
    'options': '풋·콜',
}
STAGES = {'source': 'SEC 공시 수집', 'analysis': '두 분기 비교', 'export': '결과 파일 저장'}


def edgar_today() -> date:
    return datetime.now(ZoneInfo('America/New_York')).date()


def parse_quarter(label: str) -> Quarter:
    quarter = Quarter.parse(label)
    if not date(2013, 9, 30) <= quarter.period_ending <= date(edgar_today().year, 12, 31):
        raise ValueError('2013년 3분기부터 올해 4분기까지 선택할 수 있습니다.')
    return quarter


def atomic_write(path: Path, content: str) -> None:
    """Publish a completed metadata or cache record with a single rename."""
    temporary = path.with_name(f'.{path.name}.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.replace(path)
