"""Single-user SEC 13F pipeline served on loopback."""

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import secrets
import shutil
import signal
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from config import APP_DIR, EXPORTS, ROOT, SECTIONS, STAGES, STATE, Quarter, edgar_today, parse_quarter
from source import Sec, SourceError, has_published_filing


LOG = logging.getLogger('drakos')
EVENT_PREFIX = '@@DRAKOS@@'
ACTIVE = {'queued', 'running'}
TERMINAL = {'complete', 'failed', 'cancelled', 'interrupted'}
DEFAULTS = {'target': '2026Q1', 'compare': '2025Q4', 'sections': list(SECTIONS), 'output_dir': str(EXPORTS)}


class RunOptions(BaseModel):
    target: str = Field(max_length=12)
    compare: str = Field(max_length=12)
    sections: list[str] = Field(min_length=1, max_length=len(SECTIONS))
    output_dir: str = Field(default=str(EXPORTS), min_length=1, max_length=4096)


def clock():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def checked_quarter(label):
    try:
        return parse_quarter(label)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class Availability:
    def __init__(self):
        self.cache = {}
        self.lock = asyncio.Lock()

    @staticmethod
    def probe(quarter):
        sec = Sec()
        try:
            return has_published_filing(sec, quarter)
        finally:
            sec.close()

    async def check(self, quarter):
        async with self.lock:
            cached = self.cache.get(quarter.label)
            if cached and time.monotonic() < cached[0]:
                return cached[1]
            try:
                available = await asyncio.to_thread(self.probe, quarter)
            except SourceError as exc:
                raise HTTPException(503, f'SEC 공시 확인에 실패했습니다. {exc}') from exc
            result = {'quarter': quarter.label, 'status': 'available' if available else 'unpublished',
                      'checked_at': clock()}
            self.cache[quarter.label] = (time.monotonic() + 300, result)
            return result


def resolve_folder(raw, *, create_default=False):
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError
        if path == EXPORTS and create_default:
            path.mkdir(exist_ok=True)
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise ValueError
        return path
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(422, '열 수 있는 로컬 폴더의 전체 경로를 입력해 주세요.') from exc


def staged_output(run):
    return Path(run['output_dir']) / f".13f-{run['id']}.partial"


class Jobs:
    def __init__(self):
        self.db = sqlite3.connect(STATE / 'jobs.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        self.active_id = self.task = None
        for job in self.list(None):
            if job['status'] in ACTIVE:
                if job['result'] and Path(job['destination']).is_dir():
                    job.update(status='complete', percent=100, message='결과 파일 저장 완료')
                else:
                    job.update(status='interrupted', result=None, message='서버가 다시 시작되어 작업이 중단됐습니다.',
                               error='다시 실행하면 저장된 SEC 자료를 사용합니다.')
                self.clean_stage(job)
                job['ended_at'] = clock()
                self.save(job)

    def save(self, run):
        run['seq'] = run.get('seq', 0) + 1
        self.db.execute('INSERT OR REPLACE INTO jobs VALUES (?, ?)', (run['id'], json.dumps(run, ensure_ascii=False)))
        self.db.commit()

    def list(self, limit=30):
        if limit is None:
            rows = self.db.execute('SELECT payload FROM jobs ORDER BY rowid DESC')
        else:
            rows = self.db.execute('SELECT payload FROM jobs ORDER BY rowid DESC LIMIT ?', (limit,))
        return [json.loads(row[0]) for row in rows]

    def read(self, identifier):
        row = self.db.execute('SELECT payload FROM jobs WHERE id = ?', (identifier,)).fetchone()
        if row is None:
            raise HTTPException(404, '실행 기록을 찾을 수 없습니다.')
        return json.loads(row[0])

    def available(self):
        if self.task and not self.task.done():
            raise HTTPException(409, '이미 실행 중인 작업이 있습니다.')

    def clean_stage(self, run):
        try:
            shutil.rmtree(staged_output(run))
        except FileNotFoundError:
            pass
        except OSError:
            LOG.warning('Could not clean staging for run %s', run['id'], exc_info=True)

    def start(self, options, target, compare):
        self.available()
        sections = [section for section in SECTIONS if section in options.sections]
        base = resolve_folder(options.output_dir, create_default=True)
        identifier = secrets.token_hex(8)
        destination = base / f'13F_{target.label}_vs_{compare.label}_{datetime.now():%Y%m%d-%H%M%S}_{identifier[:8]}'
        stages = list(STAGES)
        job = {'id': identifier, 'target': target.label, 'compare': compare.label, 'sections': sections,
               'output_dir': str(base), 'destination': str(destination), 'status': 'queued',
               'created_at': clock(), 'ended_at': None, 'result': None, 'error': None, 'connection': None,
               'stage': stages[0], 'stages': stages, 'percent': 0, 'progress': dict.fromkeys(stages, 0.0),
               'done': None, 'total': None, 'unit': None, 'message': '실행 준비 중', 'activity': []}
        try:
            staged_output(job).mkdir(mode=0o700)
        except OSError as exc:
            raise HTTPException(422, '이 폴더에 결과를 저장할 수 없습니다. 쓰기 가능한 폴더를 선택해 주세요.') from exc
        try:
            self.save(job)
        except sqlite3.Error:
            self.clean_stage(job)
            raise
        self.active_id = identifier
        self.task = asyncio.create_task(self.execute(job))
        return job

    def note(self, run, stage, message):
        if not run['activity'] or run['activity'][0]['message'] != message:
            run['activity'] = [{'time': clock(), 'stage': stage, 'message': message}, *run['activity'][:19]]

    def update(self, run, stage, fraction, message, done=None, total=None, unit=None):
        progress = run['progress']
        progress[stage] = max(progress[stage], min(1, fraction))
        percent = min(99, int(100 * sum(progress.values()) / len(progress)))
        run.update(stage=stage, percent=percent, done=done, total=total, unit=unit, message=message)
        if stage != 'source':
            run['connection'] = None
        self.note(run, stage, message)
        self.save(run)

    def update_connection(self, run, status):
        run['connection'] = status
        state, reason, attempt = status['state'], status['reason'], status['attempt']
        if state == 'waiting':
            local_time = datetime.fromisoformat(status['retry_at']).astimezone().strftime('%H:%M:%S')
            message = f'{reason} · {local_time}에 재시도 예정'
        elif state == 'retrying':
            message = f'SEC 재시도 중 · {attempt}회'
        else:
            message = reason
        self.note(run, 'source', message)
        self.save(run)

    async def generate_data(self, run):
        spec = {key: run[key] for key in ('target', 'compare', 'sections')}
        spec['destination'] = str(staged_output(run))
        worker = await asyncio.create_subprocess_exec(
            sys.executable, '-u', str(APP_DIR / 'pipeline.py'), cwd=ROOT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True, limit=2 * 1024 * 1024,
        )
        try:
            worker.stdin.write(json.dumps(spec).encode())
            await worker.stdin.drain()
            worker.stdin.close()
            result, error = None, None
            tail = []
            while line := await worker.stdout.readline():
                record = line.decode('utf-8', errors='replace')
                if not record.startswith(EVENT_PREFIX):
                    tail = [*tail[-14:], record.rstrip()]
                    continue
                payload = json.loads(record[len(EVENT_PREFIX):])
                event = payload.pop('event')
                if event == 'progress':
                    self.update(run, **payload)
                elif event == 'connection':
                    self.update_connection(run, payload)
                elif event == 'result':
                    result = payload
                elif event == 'error':
                    error = payload['message']
            status = await worker.wait()
            if status or not result:
                if not error:
                    LOG.error('Data worker failed: %s', '\n'.join(tail))
                raise RuntimeError(error or '13F 자료를 처리하지 못했습니다. 저장 공간과 SEC 연결을 확인해 주세요.')
            return result
        finally:
            if worker.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(worker.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(worker.wait(), 3)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(worker.pid, signal.SIGKILL)
                    await worker.wait()

    async def execute(self, run):
        run.update(status='running', error=None, ended_at=None)
        self.save(run)
        try:
            run['result'] = await self.generate_data(run)
            destination = Path(run['destination'])
            if destination.exists():
                raise RuntimeError('같은 이름의 저장 폴더가 이미 있습니다. 다시 실행해 주세요.')
            self.save(run)
            staged_output(run).rename(destination)
            run.update(status='complete', percent=100, message='결과 파일 저장 완료')
        except asyncio.CancelledError:
            run.update(status='cancelled', result=None, message='실행을 취소했습니다.')
        except RuntimeError as exc:
            run.update(status='failed', result=None, error=str(exc), message='분기 비교를 완료하지 못했습니다.')
        except Exception:
            LOG.exception('Job failed in stage %s', run['stage'])
            run.update(status='failed', result=None, error='실행 중 오류가 발생했습니다. 연결과 저장 폴더를 확인해 주세요.',
                       message='작업이 중단되었습니다.')
        finally:
            self.clean_stage(run)
            run.update(ended_at=clock(), connection=None)
            self.note(run, run['stage'], run['message'])
            self.save(run)
            self.active_id = None

    async def cancel(self, identifier):
        if self.active_id != identifier or not self.task or self.task.done():
            raise HTTPException(409, '현재 실행 중인 작업만 취소할 수 있습니다.')
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        run = self.read(identifier)
        if run['status'] in ACTIVE:
            self.clean_stage(run)
            run.update(status='cancelled', message='실행을 취소했습니다.', ended_at=clock())
            self.save(run)
            self.active_id = None
        return run


@asynccontextmanager
async def lifespan(app):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / 'server.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('동일한 상태 폴더를 사용 중인 파이프라인이 있습니다.') from exc
        app.state.jobs = Jobs()
        app.state.availability = Availability()
        try:
            yield
        finally:
            jobs = app.state.jobs
            if jobs.active_id:
                with contextlib.suppress(HTTPException):
                    await jobs.cancel(jobs.active_id)
            jobs.db.close()


app = FastAPI(title='Drakos 13F 데이터 파이프라인', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost'])
app.mount('/static', StaticFiles(directory=APP_DIR / 'static'), name='static')


@app.middleware('http')
async def local_only(request, call_next):
    if not request.client or request.client.host not in ('127.0.0.1', '::1'):
        return JSONResponse({'detail': '이 파이프라인은 로컬 컴퓨터에서 열 수 있습니다.'}, status_code=403)
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('origin') != f'http://{request.url.netloc}':
        return JSONResponse({'detail': '파이프라인 화면에서 다시 시도해 주세요.'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
    if request.url.path.startswith('/api/') or request.url.path == '/':
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/')
async def index():
    return FileResponse(APP_DIR / 'static' / 'index.html')


@app.get('/api/config')
async def ui_config():
    quarters = []
    for year in range(edgar_today().year, 2012, -1):
        for q in (4, 3, 2, 1):
            quarter = Quarter(year, q)
            if quarter.period_ending >= date(2013, 9, 30):
                quarters.append({'label': quarter.label, 'year': year, 'q': q, 'end': quarter.period_ending_str})
    return {'quarters': quarters, 'sections': SECTIONS, 'stages': STAGES, 'defaults': DEFAULTS}


@app.get('/api/quarters/{label}/availability')
async def quarter_availability(label: str, request: Request):
    return await request.app.state.availability.check(checked_quarter(label))


@app.get('/api/folders')
async def browse_folders(path: str | None = None):
    current = resolve_folder(path or str(ROOT))
    try:
        children = sorted((child for child in current.iterdir() if not child.name.startswith('.') and child.is_dir()),
                          key=lambda child: child.name.casefold())
    except OSError as exc:
        raise HTTPException(422, '이 폴더의 목록을 읽을 수 없습니다.') from exc
    return {'path': str(current), 'parent': str(current.parent), 'home': str(Path.home()),
            'folders': [{'name': child.name, 'path': str(child)} for child in children[:250]]}


async def start_run(state, options):
    state.jobs.available()
    target, compare = checked_quarter(options.target), checked_quarter(options.compare)
    if target == compare:
        raise HTTPException(422, '대상 분기와 기준 분기를 다르게 선택해 주세요.')
    if any(section not in SECTIONS for section in options.sections):
        raise HTTPException(422, '지원하지 않는 분석 항목이 있습니다.')
    for quarter in (target, compare):
        result = await state.availability.check(quarter)
        if result['status'] != 'available':
            raise HTTPException(422, f'{quarter.label} 공시가 아직 안 올라왔습니다.')
    return state.jobs.start(options, target, compare)


@app.get('/api/runs')
async def list_runs(request: Request):
    jobs = request.app.state.jobs
    fields = ['id', 'target', 'compare', 'status', 'percent', 'created_at', 'destination']
    return {'active': jobs.active_id, 'runs': [{key: item[key] for key in fields} for item in jobs.list()]}


@app.post('/api/runs', status_code=201)
async def new_run(options: RunOptions, request: Request):
    return await start_run(request.app.state, options)


@app.get('/api/runs/{identifier}')
async def get_run(identifier: str, request: Request):
    return request.app.state.jobs.read(identifier)


@app.get('/api/runs/{identifier}/events')
async def run_events(identifier: str, request: Request):
    jobs = request.app.state.jobs
    jobs.read(identifier)

    async def snapshots():
        seq, seen_at = None, time.monotonic()
        while not await request.is_disconnected():
            job = jobs.read(identifier)
            if job['seq'] != seq:
                yield f'id: {job["seq"]}\ndata: {json.dumps(job, ensure_ascii=False)}\n\n'
                seq, seen_at = job['seq'], time.monotonic()
            elif time.monotonic() - seen_at > 12:
                yield ': heartbeat\n\n'
                seen_at = time.monotonic()
            if job['status'] in TERMINAL:
                return
            await asyncio.sleep(0.35)

    return StreamingResponse(snapshots(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})


@app.post('/api/runs/{identifier}/cancel')
async def cancel_run(identifier: str, request: Request):
    return await request.app.state.jobs.cancel(identifier)


@app.post('/api/runs/{identifier}/retry', status_code=201)
async def retry_run(identifier: str, request: Request):
    state = request.app.state
    run = state.jobs.read(identifier)
    if run['status'] not in TERMINAL - {'complete'}:
        raise HTTPException(409, '중단되었거나 실패한 작업을 다시 실행할 수 있습니다.')
    return await start_run(state, RunOptions.model_validate(run))


@app.get('/api/runs/{identifier}/files/{filename}')
async def run_file(identifier: str, filename: str, request: Request):
    run = request.app.state.jobs.read(identifier)
    files = run['result']['files'] if run['status'] == 'complete' and run['result'] else []
    match = next((entry for entry in files if entry['name'] == filename), None)
    if not match or Path(filename).name != filename:
        raise HTTPException(404, '이 실행에서 만든 파일을 찾을 수 없습니다.')
    folder = Path(run['destination']).resolve()
    path = (folder / match['path']).resolve()
    if path.name != filename or not path.is_relative_to(folder) or not path.is_file():
        raise HTTPException(404, '저장 폴더에서 결과 파일을 찾을 수 없습니다.')
    return FileResponse(path, filename=filename)
