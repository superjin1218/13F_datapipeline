'use strict';

const $ = (id) => document.getElementById(id);
const escapeText = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[char]);
const icon = (name) => `<svg aria-hidden="true"><use href="/static/icons.svg#${name}"/></svg>`;
const number = new Intl.NumberFormat('ko-KR');
const compactDollars = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 2});
const money = (value) => value == null ? '—' : compactDollars.format(value);
const quarterText = (value) => String(value).replace(/^(\d{4})Q([1-4])$/, '$1 Q$2');
const timeText = (value) => new Date(value).toLocaleTimeString('ko-KR');
const stages = {source: 'cloud-arrow-down', analysis: 'chart-bar', export: 'folder-open'};
const sectionIcons = {holdings: 'chart-line-up', sectors: 'chart-pie', institutions: 'users', top_managers: 'stack', options: 'chart-bar'};
const statusNames = {queued: '실행 준비', running: '실행 중', complete: '완료', cancelled: '취소됨', failed: '실패', interrupted: '중단됨'};
const sides = ['target', 'compare'];
const state = {config: null, checks: {}, job: null, active: null, source: null, submitting: false,
  result: null, resultSection: null, autoResults: false, folder: null, folderRequest: 0, liveLost: false};
const isActive = (run) => ['queued', 'running'].includes(run?.status);

async function api(path, options = {}) {
  const {body, ...init} = options;
  const response = await fetch(`/api${path}`, {...init, headers: body ? {'Content-Type': 'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined});
  const data = await response.json();
  if (!response.ok) {
    throw new Error(typeof data.detail === 'string' ? data.detail : '입력 내용을 확인하고 다시 시도해 주세요.');
  }
  return data;
}

function errorAt(id, message) {
  $(id).hidden = !message;
  $(id).textContent = message || '';
}

let toastTimer;
function toast(message) {
  errorAt('toast', message);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => errorAt('toast'), 4200);
}

function draftQuarter(side) {
  return `${$(`${side}-year`).value}Q${$(`${side}-quarter`).value}`;
}

function listQuarters(side, preferred) {
  const year = Number($(`${side}-year`).value);
  const choices = state.config.quarters.filter((entry) => entry.year === year).sort((a, b) => a.q - b.q);
  const select = $(`${side}-quarter`);
  select.innerHTML = choices.map(({q}) => `<option value="${q}">Q${q}</option>`).join('');
  if (choices.some(({q}) => String(q) === preferred)) select.value = preferred;
}

function setQuarter(side, label) {
  const [year, quarter] = label.split('Q');
  $(`${side}-year`).value = year;
  listQuarters(side, quarter);
}

function chosenSections() {
  return Array.from(document.querySelectorAll('input[name="section"]:checked'), (entry) => entry.value);
}

function renderControls() {
  if (!state.config) return;
  const checked = chosenSections();
  $('toggle-sections').textContent = checked.length === Object.keys(state.config.sections).length ? '전체 해제' : '전체 선택';
  for (const side of sides) {
    const check = state.checks[side];
    const hint = $(`${side}-hint`);
    const message = check?.message ?? 'SEC 공시 확인 중…';
    if (hint.textContent !== message) hint.textContent = message;
    hint.dataset.status = check?.status ?? 'checking';
  }
  $('check-again').hidden = !sides.some((side) => ['unpublished', 'error'].includes(state.checks[side]?.status));
  const ready = sides.every((side) => state.checks[side]?.status === 'available');
  $('start-run').disabled = state.submitting || Boolean(state.active) || !ready || !checked.length ||
    !$('output-dir').value.trim() || draftQuarter('target') === draftQuarter('compare');
  renderHero();
}

async function checkQuarter(side) {
  const label = draftQuarter(side);
  const check = {status: 'checking', message: 'SEC 공시 확인 중…'};
  state.checks[side] = check;
  renderControls();
  try {
    const result = await api(`/quarters/${encodeURIComponent(label)}/availability`);
    check.status = result.status;
    check.message = result.status === 'available' ? 'SEC 공시 확인됨' : '아직 안 올라왔습니다';
  } catch (error) {
    check.status = 'error';
    check.message = error.message;
  }
  if (state.checks[side] === check) renderControls();
}

function renderHero() {
  const run = state.job;
  const target = run?.target || draftQuarter('target');
  const compare = run?.compare || draftQuarter('compare');
  const percent = run?.percent ?? 0;
  $('hero-pair').innerHTML = `${escapeText(quarterText(target))} <span class="versus">vs</span> ${escapeText(quarterText(compare))}`;
  $('percent').innerHTML = `${percent}<small>%</small>`;
  $('hero-state').textContent = run?.connection?.state === 'waiting' ? 'SEC 재시도 대기' : statusNames[run?.status] ?? '실행 준비';
  $('meter').setAttribute('aria-valuenow', percent);
  $('meter').dataset.running = isActive(run);
  $('meter-fill').style.transform = `scaleX(${percent / 100})`;
}

function renderConnection(run) {
  const connection = run?.connection;
  const notice = $('connection-notice');
  notice.hidden = !connection && !state.liveLost;
  notice.dataset.state = connection?.state ?? 'retrying';
  const title = connection?.state === 'waiting' ? `${connection.reason} · 재시도 대기` :
    connection?.state === 'retrying' ? `SEC에 다시 연결하는 중 · ${connection.attempt}회` :
    connection?.reason ?? '실시간 상태를 다시 연결하는 중입니다.';
  if (!notice.hidden && $('connection-title').textContent !== title) $('connection-title').textContent = title;
  renderCountdown();
}

function renderCountdown() {
  const connection = state.job?.connection;
  const element = $('connection-countdown');
  if (connection?.state !== 'waiting') {
    element.textContent = '';
    return;
  }
  const remaining = Math.max(0, Math.ceil((new Date(connection.retry_at) - Date.now()) / 1000));
  element.textContent = `${timeText(connection.retry_at)} 재시도 · ${Math.floor(remaining / 60)}분 ${String(remaining % 60).padStart(2, '0')}초 남음`;
}

function progressDetail(run) {
  if (run.done == null) return '';
  const units = {bytes: 'B', filings: '건', steps: '단계', institutions: '곳'};
  const fmt = run.unit === 'bytes' ? (value) => `${number.format(Math.round(value / 1048576))} MB` :
    (value) => `${number.format(value)}${units[run.unit] ? ` ${units[run.unit]}` : ''}`;
  return run.total ? `${fmt(run.done)} / ${fmt(run.total)}` : fmt(run.done);
}

function renderProgress(run) {
  const message = run ? [run.message, isActive(run) && progressDetail(run)].filter(Boolean).join(' · ') :
    '실행을 시작하면 이곳에 진행 상황이 표시됩니다.';
  if ($('progress-message').textContent !== message) $('progress-message').textContent = message;
  $('cancel-run').hidden = !isActive(run);
  $('retry-run').hidden = !run || Boolean(state.active) || run.status === 'complete';
  errorAt('progress-error', run?.error);
  renderConnection(run);
  const activity = run?.activity ?? [];
  $('run-activity').hidden = !activity.length;
  $('run-activity').innerHTML = activity.map(({time, message}) =>
    `<li><time datetime="${escapeText(time)}">${timeText(time)}</time><span>${escapeText(message)}</span></li>`).join('');
  for (const node of $('stages').children) {
    const stage = node.dataset.stage;
    const fraction = run?.progress[stage] ?? 0;
    const active = isActive(run) && run.stage === stage;
    node.classList.toggle('active', active);
    node.classList.toggle('complete', fraction === 1);
    node.querySelector('small').textContent = active ? `${Math.floor(fraction * 100)}%` : fraction === 1 ? '완료' : '대기';
  }
}

function fileRow(run, file) {
  const url = `/api/runs/${encodeURIComponent(run.id)}/files/${encodeURIComponent(file.name)}`;
  const size = file.size >= 1048576 ? `${(file.size / 1048576).toFixed(1)} MB` : `${Math.ceil(file.size / 1024)} KB`;
  return `<div class="file-row"><span class="format-tag">${escapeText(file.format)}</span>
    <div><strong>${escapeText(file.name)}</strong><small>${size}</small></div>
    <a class="button neutral" href="${url}" download aria-label="${escapeText(file.name)} 다운로드">${icon('download')} <span>다운로드</span></a></div>`;
}

function renderTables() {
  const tables = state.result?.result.tables[state.resultSection] ?? [];
  document.querySelectorAll('[data-result-section]').forEach((button) => {
    const selected = button.dataset.resultSection === state.resultSection;
    button.classList.toggle('current', selected);
    button.setAttribute('aria-pressed', selected);
  });
  const panel = $('result-tables');
  if (!panel) return;
  panel.innerHTML = tables.map(({title, columns, rows}) => {
    const heading = `<h4 class="table-label">${escapeText(title)}</h4>`;
    if (!rows.length) return `${heading}<p class="empty-result">해당 항목에 행이 없습니다.</p>`;
    const header = columns.map((col) => `<th scope="col">${escapeText(col)}</th>`).join('');
    const values = rows.map((row) => `<tr>${columns.map((col) => `<td>${escapeText(row[col])}</td>`).join('')}</tr>`).join('');
    return `${heading}<div class="table-scroll" tabindex="0" aria-label="${escapeText(title)}"><table class="data-table"><thead><tr>${header}</tr></thead><tbody>${values}</tbody></table></div>`;
  }).join('');
}

function renderResult(run) {
  if (!run?.result || state.result?.id === run.id) return;
  state.result = run;
  const {overview, sources, files, tables} = run.result;
  const availableSections = Object.keys(state.config.sections).filter((key) => files.some((file) => file.section === key));
  state.resultSection = availableSections.find((key) => tables[key]);
  const sourceCards = Object.entries(sources).map(([label, source]) =>
    `<div class="source-card"><strong>${quarterText(label)}</strong> · ${escapeText(source.source)}
      <p>${number.format(source.filings)} / ${number.format(source.expected)}건 확보 · 누락 ${number.format(source.missing)}건</p>
      <p>공시 기준일 ${escapeText(source.cutoff)}</p></div>`).join('');
  const counts = overview.filing_counts;
  const groups = availableSections.map((key) => {
    const items = files.filter((file) => file.section === key).sort((a, b) => a.name.localeCompare(b.name, 'ko'));
    return `<details open><summary>${escapeText(state.config.sections[key])} <span>${key}/ · ${items.length}개</span></summary>${items.map((file) => fileRow(run, file)).join('')}</details>`;
  }).join('');
  $('result-content').innerHTML = `<div class="result-heading"><div><h2 id="results-title">결과</h2>
    <p>${quarterText(run.target)} vs ${quarterText(run.compare)}</p></div>
    <button type="button" class="button neutral" data-copy-path>${icon('folder')} 경로 복사</button></div>
    <p class="path-note">${escapeText(run.destination)}</p>
    <div class="result-kpis"><div class="kpi"><span>공통 기관</span><strong>${number.format(overview.common)}</strong></div>
      <div class="kpi"><span>${quarterText(run.target)} 보유액 (USD)</span><strong>${money(overview.target_value)}</strong></div>
      <div class="kpi"><span>보유액 변화 (USD)</span><strong>${money(overview.change)}</strong></div></div>
    <div class="result-sources">${sourceCards}</div>
    <p class="filing-summary">대상 분기에만 확인 ${number.format(counts.target_only)}곳 · 기준 분기에만 확인 ${number.format(counts.baseline_only)}곳 · 수집 누락 ${number.format(counts.missing)}곳</p>
    <div class="grid-layout result-grid"><div class="files"><h3>저장된 파일</h3>
      ${files.filter((file) => file.section === 'overview').map((file) => fileRow(run, file)).join('')}${groups}</div>
      <div class="result-preview"><h3>CSV 미리보기</h3><div class="result-tabs">
      ${availableSections.filter((key) => tables[key]).map((key) =>
        `<button class="result-tab" type="button" data-result-section="${key}">${escapeText(state.config.sections[key])}</button>`).join('')}
      </div><div id="result-tables"></div></div></div>`;
  renderTables();
}

function acceptJob(run) {
  state.job = run;
  if (isActive(run)) state.active = run.id;
  else if (state.active === run.id) state.active = null;
  renderControls();
  renderProgress(run);
  if (run.status === 'complete') renderResult(run);
}

function monitor(run) {
  state.source?.close();
  if (!isActive(run)) return;
  const source = new EventSource(`/api/runs/${run.id}/events`);
  state.source = source;
  source.onmessage = (event) => {
    state.liveLost = false;
    const snapshot = JSON.parse(event.data);
    acceptJob(snapshot);
    if (!isActive(snapshot)) {
      source.close();
      if (snapshot.status === 'complete' && state.autoResults) $('results').scrollIntoView();
      state.autoResults = false;
    }
  };
  source.onerror = () => {
    state.liveLost = true;
    renderConnection(state.job);
  };
}

function loadJob(run, showProgress = false) {
  state.liveLost = false;
  acceptJob(run);
  monitor(run);
  if (showProgress) $(run.status === 'complete' ? 'results' : 'progress').scrollIntoView();
}

async function loadFolders(path, fallback = false) {
  const request = ++state.folderRequest;
  $('folder-list').innerHTML = '<p>폴더를 읽는 중…</p>';
  $('folder-select').disabled = true;
  try {
    const data = await api(path ? `/folders?path=${encodeURIComponent(path)}` : '/folders');
    if (state.folderRequest !== request) return;
    state.folder = data;
    $('folder-path').value = data.path;
    $('folder-select').disabled = false;
    $('folder-list').innerHTML = data.folders.map(({name, path}) =>
      `<button type="button" data-folder="${escapeText(path)}">${icon('folder')} ${escapeText(name)}</button>`).join('') ||
      '<p>하위 폴더가 없습니다.</p>';
  } catch (error) {
    if (state.folderRequest !== request) return;
    if (fallback) return loadFolders();
    $('folder-list').innerHTML = `<p>${escapeText(error.message)}</p>`;
  }
}

async function openHistory() {
  $('history-dialog').showModal();
  const list = $('history-list');
  list.innerHTML = '<p>실행 기록을 읽는 중…</p>';
  try {
    const {runs} = await api('/runs');
    list.innerHTML = runs.map((run) => `<button type="button" data-run-id="${run.id}">
      <span><strong>${quarterText(run.target)} vs ${quarterText(run.compare)}</strong>
      <small>${new Date(run.created_at).toLocaleString('ko-KR')}</small></span><span>${statusNames[run.status]} · ${run.percent}%</span></button>`).join('') ||
      '<p>아직 실행 기록이 없습니다.</p>';
  } catch (error) {
    list.innerHTML = `<p>${escapeText(error.message)}</p>`;
  }
}

function prepareForm() {
  const {quarters, sections, defaults} = state.config;
  const years = [...new Set(quarters.map(({year}) => year))];
  for (const side of sides) {
    $(`${side}-year`).innerHTML = years.map((year) => `<option value="${year}">${year}</option>`).join('');
    setQuarter(side, quarters.some(({label}) => label === defaults[side]) ? defaults[side] : quarters[0].label);
  }
  $('section-choices').innerHTML = Object.entries(sections).map(([key, title]) => `
    <label class="dataset">${icon(sectionIcons[key])}<span>${escapeText(title)}</span><input type="checkbox" name="section" value="${key}" checked></label>`).join('');
  $('stages').innerHTML = Object.entries(state.config.stages).map(([key, title]) =>
    `<div class="stage" data-stage="${key}"><span class="stage-icon">${icon(stages[key])}</span><div><strong>${escapeText(title)}</strong><small>대기</small></div></div>`).join('');
  $('output-dir').value = defaults.output_dir;
  sides.forEach(checkQuarter);
}

async function init() {
  try {
    const [config, history] = await Promise.all([api('/config'), api('/runs')]);
    state.config = config;
    state.active = history.active;
    prepareForm();
    const latest = history.active || history.runs[0]?.id;
    if (latest) loadJob(await api(`/runs/${latest}`));
  } catch (error) {
    errorAt('form-feedback', error.message);
  }
}

for (const side of sides) {
  $(`${side}-year`).addEventListener('change', () => {
    listQuarters(side, $(`${side}-quarter`).value);
    checkQuarter(side);
  });
  $(`${side}-quarter`).addEventListener('change', () => checkQuarter(side));
}
$('swap-quarters').addEventListener('click', () => {
  const target = draftQuarter('target');
  setQuarter('target', draftQuarter('compare'));
  setQuarter('compare', target);
  sides.forEach(checkQuarter);
});
$('check-again').addEventListener('click', () => sides.forEach(checkQuarter));
$('output-dir').addEventListener('input', renderControls);
$('section-choices').addEventListener('change', renderControls);
$('toggle-sections').addEventListener('click', () => {
  const list = [...document.querySelectorAll('input[name="section"]')];
  const checked = list.some((item) => !item.checked);
  for (const item of list) item.checked = checked;
  renderControls();
});
$('run-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if ($('start-run').disabled) return;
  state.submitting = true;
  renderControls();
  errorAt('form-feedback');
  try {
    const run = await api('/runs', {method: 'POST', body: {
      target: draftQuarter('target'), compare: draftQuarter('compare'), sections: chosenSections(),
      output_dir: $('output-dir').value.trim(),
    }});
    state.autoResults = true;
    loadJob(run, true);
  } catch (error) {
    errorAt('form-feedback', error.message);
    if (error.message.includes('공시')) sides.forEach(checkQuarter);
  } finally {
    state.submitting = false;
    renderControls();
  }
});
for (const action of ['cancel', 'retry']) {
  $(`${action}-run`).addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const run = await api(`/runs/${state.job.id}/${action}`, {method: 'POST'});
      state.autoResults = action === 'retry';
      loadJob(run, true);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });
}
$('history-button').addEventListener('click', openHistory);
$('browse-button').addEventListener('click', () => {
  $('folder-dialog').showModal();
  loadFolders($('output-dir').value, true);
});
$('folder-home').addEventListener('click', () => loadFolders(state.folder?.home));
$('folder-parent').addEventListener('click', () => loadFolders(state.folder?.parent));
$('folder-go').addEventListener('click', () => loadFolders($('folder-path').value));
$('folder-path').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') loadFolders(event.target.value);
});
$('folder-select').addEventListener('click', () => {
  if (state.folder) $('output-dir').value = state.folder.path;
  $('folder-dialog').close();
  renderControls();
});

document.addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-close], button[data-folder], button[data-run-id], button[data-result-section], button[data-copy-path]');
  if (!button) return;
  const {close, folder, runId, resultSection, copyPath} = button.dataset;
  try {
    if (close) $(close).close();
    else if (folder) await loadFolders(folder);
    else if (runId) {
      const run = await api(`/runs/${runId}`);
      $('history-dialog').close();
      if (state.active && state.active !== run.id) {
        if (run.status === 'complete') {
          renderResult(run);
          $('results').scrollIntoView();
        } else toast('현재 실행 상태는 진행 상황에서 볼 수 있습니다.');
      } else loadJob(run, true);
    } else if (resultSection) {
      state.resultSection = resultSection;
      renderTables();
    } else if (copyPath !== undefined) {
      await navigator.clipboard.writeText(state.result.destination);
      toast('저장 경로를 복사했습니다.');
    }
  } catch (error) {
    toast(error.message);
  }
});

if ('IntersectionObserver' in window) {
  const cloud = document.querySelector('.hero-art');
  new IntersectionObserver(([entry]) => cloud.classList.toggle('is-visible', entry.isIntersecting)).observe(cloud);
}
setInterval(renderCountdown, 1000);
init();
