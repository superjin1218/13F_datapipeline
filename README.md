# Drakos 13F 데이터 파이프라인

미국 SEC에 제출된 두 분기의 기관 보유내역을 수집하고 CSV와 통합 XLSX로 저장하는 로컬 도구입니다. 한 화면에서 저장 위치를 정하고, 수집 진행률과 재시도 상태를 보고, 결과를 다운로드할 수 있습니다.

Form 13F는 일정 규모 이상의 기관 투자운용사가 SEC에 제출하는 보유내역 공시입니다. 이 도구는 제출일과 보고기간을 구분해 선택한 분기 말의 보유내역을 비교합니다. [SEC Form 13F 안내](https://www.sec.gov/rules-regulations/staff-guidance/frequently-asked-questions-about-form-13f)

## 처음 실행하기

Linux 또는 macOS에서 Git, Python 3.11 이상, 인터넷 연결이 필요합니다. 첫 실행에는 SEC 벌크 데이터를 내려받으므로 디스크 여유 공간을 1GB 이상 확보해 주세요.

```bash
git clone https://github.com/superjin1218/13F_datapipeline.git
cd 13F_datapipeline
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r 13f_dashboard/requirements.txt
export SEC_USER_AGENT='Your Name you@your-domain.com'
python 13f_dashboard/run.py
```

`SEC_USER_AGENT` 예시의 이름과 이메일을 본인의 연락처로 바꿉니다. SEC가 자동 수집 요청자를 확인할 때 쓰는 값입니다. Ubuntu나 Debian에서 `venv` 설치 오류가 나면 `python3-venv` 패키지를 설치한 뒤 다시 실행합니다. `uv`가 설치된 환경에서는 `uv venv .venv`와 `uv pip install --python .venv/bin/python -r 13f_dashboard/requirements.txt`로 환경을 준비할 수도 있습니다. [SEC 데이터 접근 안내](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)

브라우저에서 [로컬 파이프라인 열기](http://127.0.0.1:8787/)를 선택합니다. 포트가 사용 중이면 `python 13f_dashboard/run.py --port 8788`로 실행하고 주소의 포트를 `8788`로 바꿉니다.

1. **저장 폴더**에 전체 경로를 입력하거나 **찾아보기**로 고릅니다. 기본값은 프로젝트의 `result/`입니다.
2. **대상 분기**와 **기준 분기**를 고릅니다. 첫 실행은 기본값인 `2026 Q1`과 `2025 Q4`로 시험할 수 있습니다.
3. 두 분기에 **SEC 공시 확인됨**이 표시되면 분석 항목을 고르고 **START**를 누릅니다.
4. **진행 상황**에서 수집·비교·저장 단계와 처리 건수를 확인합니다. 저장이 끝나면 **결과**에 폴더 경로, CSV 목록, 미리보기, 다운로드가 표시됩니다.

## 결과 파일 읽기

각 실행은 선택한 경로 아래에 고유한 결과 폴더를 만듭니다.

```text
result/
└── 13F_2026Q1_vs_2025Q4_<실행시각>_<ID>/
    ├── 00_비교_요약.csv
    ├── 13F_2026Q1_vs_2025Q4.xlsx
    ├── holdings/        보유액 변화, 주목 종목, 신규 편입
    ├── sectors/         섹터별 보유액과 종목 상세
    ├── institutions/    보유기관 수와 분기별 기관 공시 상태
    ├── top_managers/    상위 20개 기관의 보유액과 종목
    └── options/         풋·콜 보유액 변화
```

- CSV는 UTF-8 BOM으로 저장합니다. `00_비교_요약.csv`에서 출처, 공시 기준일, 공시 확보·누락 건수와 두 분기의 공통 기관 수를 확인할 수 있습니다.
- 기관은 SEC 식별 번호인 **CIK**로 연결합니다. 양쪽 분기의 정보표가 확보된 기관으로 보유액 변화를 계산합니다. 한쪽 분기에서만 확인된 기관은 `19_대상분기만_공시기관.csv` 또는 `20_기준분기만_공시기관.csv`에 기록합니다. 공시가 있지만 정보표를 확보하지 못한 기관은 `21_공시_수집누락.csv`에 기록합니다. 공시나 보유액을 확인할 수 없는 칸은 비워 둡니다.
- 금액은 USD입니다. 보유액 차이에는 가격과 보유량 변화가 함께 반영됩니다. 공시의 출처와 조회 기준일에 따라 한쪽에서만 관측되는 기관이 달라질 수 있습니다.
- 선택적으로 프로젝트의 `shared/market_caps.json`에 CUSIP별 `market_cap`, `ticker`, `issuer`를 제공할 수 있습니다. 시가총액이 없는 종목은 `미분류`에 집계되며, 데이터가 없는 시총 구간의 CSV에는 헤더만 들어갑니다.

## 기존에 저장한 CSV

아래에는 이전 파이프라인 실행에서 저장한 CSV 208개를 비교 분기와 항목별로 정리했습니다. 당시 파일명과 값은 보관한 그대로입니다. 최신 수집 결과는 위 실행 절차로 다시 만들 수 있습니다.

| 비교 분기 | 폴더 | 파일 수 |
| --- | --- | ---: |
| 2026 Q1과 2025 Q4 | [기존 결과](examples/2026Q1_vs_2025Q4/) | 76 |
| 2025 Q4와 2025 Q3 | [첫 번째 결과](examples/2025Q4_vs_2025Q3/v1/) | 79 |
| 2025 Q4와 2025 Q3 | [두 번째 결과](examples/2025Q4_vs_2025Q3/v2/) | 53 |

`institutions/`의 대용량 통합 CSV 두 개는 `.csv.xz`로 보관했습니다. 터미널에서 해당 폴더로 이동해 `xz -dk 파일명.csv.xz`를 실행하면 옆에 원본 CSV가 생깁니다.

## SEC 공시 확인과 재시도

- 분기를 선택하면 EDGAR 검색에서 보고기간이 일치하는 원본 `13F-HR`을 확인합니다. **아직 안 올라왔습니다**는 검색을 마쳤지만 일치하는 공시가 없다는 뜻입니다. **SEC 공시 확인에 실패했습니다**가 표시되면 연락처 설정과 연결을 확인하고 **SEC 공시 다시 확인**을 누릅니다. [EDGAR 검색 안내](https://www.sec.gov/edgar/search/efts-faq.html)
- 내려받을 수 있는 SEC 벌크가 있으면 우선 사용합니다. 벌크가 아직 공개되지 않으면 개별 공시의 XML 정보표를 수집합니다. 완료한 다운로드와 개별 공시는 로컬 `.state/` 캐시에 저장해 다시 실행할 때 재사용합니다. [SEC 13F 벌크 데이터](https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets)
- 실행 중 SEC가 요청을 제한하거나 연결이 일시 중단되면, 다음 재시도 시각까지 기다린 뒤 자동으로 수집을 다시 시도합니다. 진행 화면에 대기 사유, 남은 시간, 재연결 시도와 수집 재개가 표시됩니다. 브라우저를 닫았다가 다시 열어도 서버가 실행 중이면 진행 상황을 볼 수 있습니다.
- **실행 취소** 후에는 **다시 실행**으로 이어서 작업할 수 있습니다. 결과 폴더는 CSV와 XLSX 저장이 모두 끝난 시점에 생성됩니다. 실행 중인 터미널을 닫으면 서버와 작업이 함께 종료됩니다.

## 개발 확인

```bash
.venv/bin/python -m unittest discover -s tests
```

화면은 Manrope 글꼴과 Phosphor 아이콘을 사용하며 라이선스는 `13f_dashboard/static/`에 포함되어 있습니다.
