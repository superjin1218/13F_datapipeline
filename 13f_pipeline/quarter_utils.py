#!/usr/bin/env python3
"""
=============================================================================
분기 유틸리티 (quarter_utils.py)
=============================================================================
"2026Q1" 같은 분기 문자열을 period_ending 날짜, EFTS 검색 범위,
SEC 벌크 ZIP 파일명 등으로 변환합니다.

모든 템플릿 스크립트(collect/bulk_download/analyze)가 공용으로 사용.
=============================================================================
"""

from __future__ import annotations
import io
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path


# ── 전역 경로 ────────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parents[1]
SHARED_DIR = ROOT_DIR / 'shared'
PIPELINE_DIR = ROOT_DIR / '13f_pipeline'


def configure_utf8_io() -> None:
    """Force UTF-8 for console I/O so Korean logs/help text stay readable."""
    os.environ.setdefault('PYTHONUTF8', '1')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
            continue
        except AttributeError:
            pass
        except Exception:
            continue

        buffer = getattr(stream, 'buffer', None)
        if buffer is None:
            continue
        try:
            wrapped = io.TextIOWrapper(buffer, encoding='utf-8', errors='replace', line_buffering=True)
            setattr(sys, name, wrapped)
        except Exception:
            continue


# ── 분기 파싱 / 계산 ─────────────────────────────────────────────────────────
QUARTER_RE = re.compile(r'^\s*(\d{4})\s*[Qq]\s*([1-4])\s*$')

# 각 분기의 마지막 달(월 말일)
_QUARTER_END = {
    1: (3, 31),
    2: (6, 30),
    3: (9, 30),
    4: (12, 31),
}


@dataclass(frozen=True)
class Quarter:
    year: int   # 예: 2026
    q: int      # 1..4

    # ── 파싱 ────────────────────────────────────────────────────────────
    @classmethod
    def parse(cls, s: str) -> 'Quarter':
        """'2026Q1', '2026q1', '2026 Q 1' 등 모두 허용."""
        m = QUARTER_RE.match(s)
        if not m:
            raise ValueError(f'분기 문자열 인식 실패: {s!r} (예: 2026Q1)')
        y, q = int(m.group(1)), int(m.group(2))
        if y < 1990 or y > 2100:
            raise ValueError(f'연도 이상함: {y}')
        return cls(y, q)

    # ── 표현 ────────────────────────────────────────────────────────────
    @property
    def label(self) -> str:
        return f'{self.year}Q{self.q}'

    @property
    def label_lower(self) -> str:
        return f'{self.year}q{self.q}'

    def __str__(self) -> str:
        return self.label

    # ── 주요 날짜 ───────────────────────────────────────────────────────
    @property
    def period_ending(self) -> date:
        """13F cover page의 periodOfReport (period_ending) 값."""
        mo, d = _QUARTER_END[self.q]
        return date(self.year, mo, d)

    @property
    def period_ending_str(self) -> str:
        return self.period_ending.isoformat()  # 'YYYY-MM-DD'

    # ── EFTS 검색 범위 ──────────────────────────────────────────────────
    # 13F 기한은 분기말 +45일. 늦게 내는 곳까지 잡기 위해 +75일 정도로 여유.
    def edgar_search_start(self) -> date:
        """period_ending 다음 날부터 검색 시작 (빠른 제출자)."""
        pe = self.period_ending
        return date(pe.year, pe.month, pe.day)  # = period_ending

    def edgar_search_end(self) -> date:
        """period_ending + 약 75일 (대부분 제출 완료)."""
        # 간단한 계산: 다음 분기 + 1달 후 말일
        nxt_year = self.year + (1 if self.q == 4 else 0)
        nxt_q = 1 if self.q == 4 else self.q + 1
        mo, d = _QUARTER_END[nxt_q]
        return date(nxt_year, mo, d)

    # ── 벌크 파일명 후보 ────────────────────────────────────────────────
    # SEC는 두 가지 패턴으로 올림:
    #   1) 2025q3_form13f.zip
    #   2) 01sep2025-30nov2025_form13f.zip   (filing 날짜 범위)
    def bulk_zip_candidates(self) -> list[str]:
        # 패턴1: 연도+분기
        p1 = f'{self.year}q{self.q}_form13f.zip'

        # 패턴2: filing 날짜 범위 (분기+1개월 ~ 분기+3개월)
        # 예: Q3 → 01sep~30nov, Q4 → 01dec~28feb (다음해)
        month_abbr = ['jan','feb','mar','apr','may','jun',
                      'jul','aug','sep','oct','nov','dec']
        # 분기 시작 달 = 분기의 첫 번째 달
        start_month_idx = (self.q - 1) * 3  # 0, 3, 6, 9
        # filing 기간은 분기가 *끝난 뒤* 3개월. start_month = 분기의 마지막 달
        # SEC 파일명 실례: 01sep2025-30nov2025 → Q3 2025의 filing 기간 Sep~Nov
        # 즉 분기의 last month 부터 그 다음 2개월
        last_month_idx = start_month_idx + 2  # Q1=2(Mar), Q2=5(Jun), Q3=8(Sep), Q4=11(Dec)
        first_fm = month_abbr[last_month_idx]
        second_fm = month_abbr[(last_month_idx + 2) % 12]
        first_year = self.year
        second_year = self.year + (1 if last_month_idx + 2 >= 12 else 0)
        last_day_of_second = {
            'jan': 31, 'feb': 28, 'mar': 31, 'apr': 30, 'may': 31, 'jun': 30,
            'jul': 31, 'aug': 31, 'sep': 30, 'oct': 31, 'nov': 30, 'dec': 31,
        }[second_fm]
        p2 = f'01{first_fm}{first_year}-{last_day_of_second:02d}{second_fm}{second_year}_form13f.zip'

        return [p1, p2]

    def bulk_urls(self) -> list[str]:
        base = 'https://www.sec.gov/files/structureddata/data/form-13f-data-sets/'
        return [base + name for name in self.bulk_zip_candidates()]

    # ── 분기 폴더명 ─────────────────────────────────────────────────────
    @property
    def dir_name(self) -> str:
        """예: '13f_q1_2026' (사용자가 이미 쓰고 있는 네이밍)."""
        return f'13f_q{self.q}_{self.year}'

    @property
    def data_dir(self) -> Path:
        return ROOT_DIR / self.dir_name

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / 'cache'

    @property
    def bulk_dir(self) -> Path:
        return self.data_dir / 'bulk'

    @property
    def efts_index_path(self) -> Path:
        return self.data_dir / 'efts_index.json'

    @property
    def progress_path(self) -> Path:
        return self.data_dir / 'progress.json'

    @property
    def results_dir(self) -> Path:
        return self.data_dir / 'results'


def resolve_cache_dir_for_quarter(q: Quarter) -> Path | None:
    """분기별 per-filing 캐시 폴더를 찾는다.

    우선순위:
      1) <ROOT>/13f_q{q}_{year}/cache/
      2) <ROOT>/13f_q{q}_{year}/13f_cache/   (기존 Q4 2025 네이밍 호환)
    """
    p1 = q.data_dir / 'cache'
    p2 = q.data_dir / '13f_cache'
    if p1.exists() and any(p1.iterdir()):
        return p1
    if p2.exists() and any(p2.iterdir()):
        return p2
    if p1.exists():
        return p1
    if p2.exists():
        return p2
    return None


def resolve_bulk_dir_for_quarter(q: Quarter) -> Path | None:
    """분기별 벌크 TSV 폴더.

    우선순위:
      1) <ROOT>/13f_q{q}_{year}/bulk/
      2) <ROOT>/13f_q{q}_{year}/13f_bulk/q{Q}_{YYYY}/
      3) <ROOT>/13f_q*_*/13f_bulk/q{Q}_{year}/          (다른 분기 폴더에 저장된 compare 데이터)
      4) <ROOT>/13f_q*_*/bulk/                           (단, 해당 폴더가 q의 분기일 때만)
    """
    p1 = q.bulk_dir
    if (p1 / 'INFOTABLE.tsv').exists():
        return p1

    legacy = q.data_dir / '13f_bulk' / f'q{q.q}_{q.year}'
    if (legacy / 'INFOTABLE.tsv').exists():
        return legacy

    # 전체 스캔: 다른 13f_q* 폴더에 compare용으로 들어가 있는 벌크
    target_name = f'q{q.q}_{q.year}'
    for peer in sorted(ROOT_DIR.glob('13f_q*')):
        if not peer.is_dir():
            continue
        # 13f_bulk/qN_YYYY/ 하위 폴더 검색
        cand = peer / '13f_bulk' / target_name
        if (cand / 'INFOTABLE.tsv').exists():
            return cand

    return p1  # 아직 없어도 기본 경로 반환


if __name__ == '__main__':
    # 간단 테스트
    import sys
    args = sys.argv[1:] or ['2026Q1', '2025Q4', '2025Q3']
    for a in args:
        q = Quarter.parse(a)
        print(f'{q.label}')
        print(f'  period_ending = {q.period_ending_str}')
        print(f'  EFTS range    = {q.edgar_search_start()} ~ {q.edgar_search_end()}')
        print(f'  data_dir      = {q.data_dir}')
        print(f'  bulk zips     = {q.bulk_zip_candidates()}')
