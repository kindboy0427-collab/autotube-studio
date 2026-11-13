const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const STATUS = {waiting_images:'이미지 기다리는 중', queued:'대기', running:'제작 중', ready:'완성(미업로드)', uploaded_private:'비공개 업로드됨', published:'공개됨', failed:'실패'};
const STEP = {plan:'대본 기획', voice:'내레이션', visuals:'영상·이미지', music:'배경음악', render:'렌더링', thumbnail:'썸네일', upload:'업로드', done:'완료'};
const FEAT = {hook:'도입', retention:'이탈방지', engagement:'참여유도', narration:'내레이션', visual:'화면', emotion:'감정', opens_with:'첫 감정', title:'제목', pace:'말속도', shots:'컷 길이', cta:'구독요청', format:'형식'};
let S = {}, openId = null;

async function api(path, opt = {}) {
  const r = await fetch(path, {...opt, headers: {'Content-Type': 'application/json', 'X-Token': window.TOKEN, ...(opt.headers || {})}});
  const t = await r.text();
  let d; try { d = JSON.parse(t) } catch { d = {detail: t} }
  if (!r.ok) throw new Error(d.detail || d.error || r.status);
  return d;
}
const post = (p, b) => api(p, {method: 'POST', body: JSON.stringify(b || {})});

document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.id === b.dataset.tab));
  if (b.dataset.tab === 'list') loadList();
  if (b.dataset.tab === 'learn') loadLearn();
  if (b.dataset.tab === 'settings') fillSettings();
  if (b.dataset.tab === 'channels' || b.dataset.tab === 'make') loadChannels();
  if (b.dataset.tab === 'usage') loadUsage();
  if (b.dataset.tab === 'voice') loadVoices();
});

let VER = null;
async function checkVersion() {
  try {
    const p = await (await fetch('/api/ping')).json();
    if (VER && p.version !== VER) { location.reload(); return }
    VER = p.version; $('ver').textContent = 'v' + VER;
    const u = await (await fetch('/api/update')).json();
    $('updBar').hidden = !u.message; $('updBar').textContent = u.message || '';
    if ($('updInfo')) $('updInfo').textContent = u.info ? (u.info.available ? `새 버전 v${u.info.latest} 있음` : (u.info.error ? '확인 실패: ' + u.info.error : '최신 버전입니다')) : '';
  } catch (e) { $('updBar').hidden = false; $('updBar').textContent = '프로그램 다시 시작 중… 잠시만 기다리세요' }
}
async function loadStatus() {
  checkVersion();
  S = await api('/api/status');
  const L = S.learning;
  if (!window._imgInit && S.settings) { window._imgInit = 1; $('makeImg').value = (S.settings.production || {}).image_source === 'ai' ? 'ai' : 'manual' }
  if (!$('makeFormat').options.length) $('makeFormat').innerHTML = '<option value="">자동 (주제에 맞게 AI가 선택)</option>' + Object.entries(S.formats || {}).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join('');
  $('kWatched').textContent = L.watched.toLocaleString();
  $('kQueue').textContent = `대기열 ${L.queued} · 오늘 시청 ${L.watch_hours_today}시간`;
  $('kPatterns').textContent = L.patterns;
  $('kState').textContent = L.state;
  $('kToday').textContent = '$' + S.budget.today_usd.toFixed(2);
  $('kMonth').textContent = `이번 달 $${S.budget.month_usd.toFixed(2)} / 한도 $${S.settings.budget.monthly_usd}`;
  $('kQuota').textContent = S.budget.youtube_quota_today.toLocaleString();
  $('kYt').textContent = `/ ${S.settings.youtube_quota_per_day.toLocaleString()} 단위`;
  const k = S.keys, items = [
    [k.GEMINI_API_KEY.set && !(S.gemini_check && !S.gemini_check.ok), 'Gemini API 키 (영상 시청·분석·이미지·영상 생성)' + (S.gemini_check && !S.gemini_check.ok ? ' — ⚠ 키가 맞지 않음: ' + S.gemini_check.message : '')],
    [k.YOUTUBE_API_KEY.set, 'YouTube Data API 키 (검색·통계)'],
    [S.subscriptions.claude_code.installed, 'Claude Pro 대본 (Claude Code 설치·로그인 — SETUP_SUBSCRIPTIONS.cmd)' + (S.subscriptions.claude_code.paused_until ? ' · 한도로 잠시 쉬는 중' : '')],
    [S.subscriptions.codex.installed, 'ChatGPT Plus 대본 (Codex 설치·로그인)' + (S.subscriptions.codex.paused_until ? ' · 한도로 잠시 쉬는 중' : '')],
    [S.voices_mine > 0, '내 목소리 등록 (목소리 탭 — 없으면 Gemini 기본 목소리)'],
    [k.ELEVENLABS_API_KEY.set, 'ElevenLabs (선택: 내 목소리 복제·감정 연기·배경음악)'],
    [S.align_available, '자막 싱크 정밀 맞춤 (선택)' + (S.align_available ? '' : (S.align_install && S.align_install.state === 'running' ? ' — ' + S.align_install.message : (S.align_install && S.align_install.state === 'failed' ? ' — 설치 실패: ' + S.align_install.message : '')))],
    [k.PEXELS_API_KEY.set, 'Pexels 무료 스톡 키 (없으면 AI 이미지로 대체)'],
    [S.youtube_connected, 'YouTube 채널 연결 (채널 탭 — 비공개 자동 업로드)'],
    [S.ffmpeg, 'FFmpeg (영상 렌더링)'],
    [!!S.font, '한글 폰트 (자막·썸네일)'],
    [S.channels_count > 0 || (S.settings.learning.niches || []).length > 0, '채널 만들기 (채널 탭 — 채널 분야로 AI가 학습할 영상을 찾음)'],
  ];
  $('checklist').innerHTML = items.map(([ok, t]) => `<li class="${ok ? 'ok' : 'no'}">${esc(t)}${!ok && t.startsWith('자막 싱크') && !(S.align_install && S.align_install.state === 'running') ? ' <button id="btnAlign">자동 설치</button>' : ''}</li>`).join('');
  const ba = $('btnAlign'); if (ba) ba.onclick = async () => { ba.disabled = true; await post('/api/align/install'); loadStatus() };
  $('events').innerHTML = S.events.map(e => `<div class="${e.level}">${esc(e.created_at.slice(5, 16).replace('T', ' '))} · ${esc(e.message)}</div>`).join('') || '아직 활동 없음';
}

$('btnMake').onclick = async () => {
  const topic = $('topic').value.trim();
  if (topic.length < 2) return;
  $('btnMake').disabled = true;
  try {
    const r = await post('/api/productions', {topic, instructions: $('instr').value, minutes: +$('minutes').value || 10, channel_id: $('makeChannel').value || null, script_format: $('makeFormat').value || null, image_source: $('makeImg').value});
    $('makeMsg').innerHTML = `<div class="msg">제작을 시작했습니다. '제작 목록'에서 진행 상황을 볼 수 있습니다. (${esc(r.id)})</div>`;
    $('topic').value = ''; $('instr').value = '';
  } catch (e) { $('makeMsg').innerHTML = `<div class="msg err">${esc(e.message)}</div>` }
  $('btnMake').disabled = false;
};

async function loadList() {
  const list = await api('/api/productions');
  $('prodList').innerHTML = list.map(p => `<div class="prod" data-id="${esc(p.id)}">
    <div><b>${esc(p.title || p.topic)}</b><br><small>${esc(p.created_at.slice(0, 16).replace('T', ' '))} · ${esc(STEP[p.step] || p.step)} · $${(p.cost_usd || 0).toFixed(2)}${p.error ? ' · ' + esc(p.error.slice(0, 80)) : ''}</small></div>
    <div><span class="badge ${esc(p.status)}">${esc(STATUS[p.status] || p.status)}</span></div>
    <div class="bar"><i style="width:${Math.round(p.progress || 0)}%"></i></div></div>`).join('') || '<p class="hint">아직 제작한 영상이 없습니다.</p>';
  document.querySelectorAll('.prod').forEach(el => el.onclick = () => showDetail(el.dataset.id));
  // re-draw the open detail only when it actually changed (re-drawing reloads the video = flicker)
  const cur = openId && list.find(p => p.id === openId);
  const sig = cur ? [cur.status, cur.step, Math.round(cur.progress || 0), cur.youtube_id, cur.error].join('|') : '';
  if (openId && sig !== window._lastSig) { window._lastSig = sig; showDetail(openId, true) }
}

async function showDetail(id, quiet) {
  if (!quiet) window._lastSig = null;
  openId = id;
  const p = await api('/api/productions/' + id), plan = p.plan || {};
  const d = $('detail'); d.hidden = false;
  const vid = p.video_path ? `<video controls src="/media/${esc(id)}/video.mp4?t=${Date.now()}"></video>` : '';
  const th = p.thumbnail_path ? `<img class="thumb" src="/media/${esc(id)}/thumbnail.jpg?t=${Date.now()}">` : '';
  const yt = p.youtube_id ? `<p>YouTube: <a href="https://youtu.be/${esc(p.youtube_id)}" target="_blank">https://youtu.be/${esc(p.youtube_id)}</a> (${esc(p.privacy)})</p>` : '';
  d.innerHTML = `<h3>${esc(plan.title || p.topic)}</h3>
    <p class="hint">상태: ${esc(STATUS[p.status] || p.status)} · 단계: ${esc(STEP[p.step] || p.step)} · 비용(추정) $${(p.cost_usd || 0).toFixed(2)}</p>
    ${vid}${th}${yt}
    <div class="row">
      ${p.status === 'failed' ? '<button id="dRetry" class="primary">이어서 다시 시도</button>' : ''}
      ${p.video_path && !p.youtube_id ? '<button id="dUpload" class="primary">비공개 업로드</button>' : ''}
      ${p.youtube_id && p.privacy !== 'public' ? '<button id="dPublish" class="primary">검토 완료 → 공개</button>' : ''}
      ${(plan.scenes || []).length ? '<button id="dRevoice">내레이션만 다시 만들기</button>' : ''}
      <button id="dReplan">대본부터 새로 만들기</button>
    </div>
    ${p.status === 'waiting_images' ? '<div id="imgPanel" class="panel"></div>' : ''}
    ${(plan.titles || []).length ? `<h4>제목 후보</h4><div>${plan.titles.map(t => `<div>· ${esc(t)}</div>`).join('')}</div>` : ''}
    ${(plan.features_used || []).length ? `<h4>반영한 학습 패턴</h4><div>${plan.features_used.map(f => `<span class="feat">${esc(featLabel(f))}</span>`).join('')}</div>` : ''}
    ${p.fact_check && p.fact_check !== '(없음)' ? `<h4>⚠ 공개 전 사실 확인할 것</h4><div class="pre">${esc(p.fact_check)}</div>` : ''}
    ${usageBlock(p.usage)}
    ${p.channel_id ? `<h4>이 영상 피드백 → 채널 제작 규칙에 반영</h4><textarea id="fbText" rows="2" placeholder="예) 배경음악이 너무 크다 / 도입 질문이 약하다 / 수식은 화면 글자로 보여줘"></textarea><button id="fbSend">규칙에 반영</button>` : ''}
    ${p.description ? `<h4>설명</h4><div class="pre">${esc(p.description)}</div>` : ''}
    ${p.pinned_comment ? `<h4>고정 댓글</h4><div class="pre">${esc(p.pinned_comment)}</div>` : ''}
    ${(plan.scenes || []).length ? `<h4>장면 (${plan.scenes.length})</h4><div class="scenes">${plan.scenes.map((s, i) => `<div><small>${i + 1} · ${esc(s.chapter || '')} · ${esc((s.visual || {}).type || '')}</small><br>${esc(s.narration)}</div>`).join('')}</div>` : ''}
    <h4>작업 기록</h4><div class="log pre">${esc(p.log || '')}</div>`;
  const bind = (bid, fn) => { const b = $(bid); if (b) b.onclick = async () => { b.disabled = true; try { await fn() } catch (e) { alert(e.message) } loadList() } };
  bind('dRetry', () => post(`/api/productions/${id}/retry`));
  bind('dUpload', () => post(`/api/productions/${id}/upload`));
  bind('dPublish', () => confirm('이 영상을 공개할까요?') && post(`/api/productions/${id}/publish`, {privacy: 'public'}));
  bind('fbSend', async () => { const t = $('fbText').value.trim(); if (t) { const r = await post(`/api/channels/${p.channel_id}/feedback`, {feedback: t}); alert('채널 규칙이 갱신되었습니다:\n' + r.style_guide) } });
  bind('dRevoice', () => confirm('대본·이미지·영상은 그대로 두고 내레이션만 다시 만든 뒤 다시 렌더링할까요? (몇 십 원 수준)') && post(`/api/productions/${id}/revoice`));
  bind('dReplan', () => confirm('대본과 작업 파일을 지우고 새로 만들까요?') && post(`/api/productions/${id}/replan`));
  if (p.status === 'waiting_images') imagePanel(id);
  if (!quiet) d.scrollIntoView({behavior: 'smooth'});
}

async function imagePanel(id) {
  const box = $('imgPanel'); if (!box) return;
  const d = await api(`/api/productions/${id}/images`);
  const empty = d.slots.filter(s => !s.file).length;
  box.innerHTML = `<h3>이미지 ${d.slots.length}장 넣기 <small class="hint">(남은 칸 ${empty})</small></h3>
    <p class="hint">① [프롬프트 전체 복사] → ChatGPT에 붙여넣고 번호 순서대로 만들기 → ② 받은 이미지들을 아래 [여러 장 한 번에 넣기]로 선택
    (다운로드한 순서대로 빈 칸에 채워짐) → ③ 칸을 눌러 한 장씩 바꿀 수도 있음 → ④ [이미지 넣었어요 → 계속]. 빈 칸은 무료 실제 자료·스톡 화면으로 채웁니다.</p>
    <div class="row"><button id="ipCopy" class="primary">프롬프트 전체 복사</button>
      <label class="inline">여러 장 한 번에 넣기 <input type="file" id="ipBulk" accept="image/*" multiple></label>
      <button id="ipGo" class="primary">이미지 넣었어요 → 계속</button></div>
    <div class="slots">${d.slots.map(s => `<div class="slot" title="${esc(s.prompt)}"><b>${String(s.n).padStart(2, '0')}</b> <small>${esc(s.what || '')}</small><br>
      ${s.file ? `<img src="/media/${esc(id)}/my/${esc(s.file)}?t=${Date.now()}">` : '<div class="noimg">비어 있음</div>'}
      <label class="inline"><input type="file" accept="image/*" data-slot="${s.n}"> 바꾸기</label></div>`).join('')}</div>`;
  $('ipCopy').onclick = async () => { await navigator.clipboard.writeText(d.prompts); $('ipCopy').textContent = '복사됨 ✓ ChatGPT에 붙여넣으세요' };
  const send = async items => { await post(`/api/productions/${id}/images`, {items}); imagePanel(id) };
  const toItem = async (f, n) => ({n, b64: await readB64(f), ext: (f.name.split('.').pop() || 'png')});
  $('ipBulk').onchange = async e => {
    const files = [...e.target.files].sort((a, b) => a.lastModified - b.lastModified || a.name.localeCompare(b.name, undefined, {numeric: true}));
    const named = files.filter(f => /^\d{1,3}\./.test(f.name));
    let items = [];
    if (named.length === files.length) items = await Promise.all(files.map(f => toItem(f, parseInt(f.name))));
    else { const free = d.slots.filter(s => !s.file).map(s => s.n); items = await Promise.all(files.slice(0, free.length).map((f, i) => toItem(f, free[i]))) }
    for (let i = 0; i < items.length; i += 5) await post(`/api/productions/${id}/images`, {items: items.slice(i, i + 5)});
    imagePanel(id);
  };
  box.querySelectorAll('[data-slot]').forEach(inp => inp.onchange = async e => { if (e.target.files[0]) await send([await toItem(e.target.files[0], +inp.dataset.slot)]) });
  $('ipGo').onclick = async () => { if (empty && !confirm(`빈 칸 ${empty}개는 무료 실제 자료·스톡 화면으로 채웁니다. 계속할까요?`)) return; await post(`/api/productions/${id}/images/continue`); loadList() };
}

const featLabel = f => { const [c, v] = f.split(':'); return (FEAT[c] || c) + ': ' + v };

async function loadStudies() {
  const d = await api('/api/study');
  $('stMsg').textContent = d.running ? '학습 중… ' + d.message : (d.message || '');
  $('stList').innerHTML = d.studies.map(x => { const r = x.report || {}; return `<div class="chcard"><b>${esc(x.channel.title)}</b>
    <small class="hint">구독자 ${x.channel.subscribers ? x.channel.subscribers.toLocaleString() : '비공개'} · 롱폼 ${x.studied}편 비교 · 평소 조회 ${x.median_views.toLocaleString()} · 정밀 분석 ${x.queued}편${x.favorite ? ' · 취향 레퍼런스' : ''}</small>
    <div>${esc(r.one_line || '')}</div>
    ${(r.title_patterns || []).length ? `<small class="hint">터진 제목 패턴: ${esc(r.title_patterns.join(' / '))}</small><br>` : ''}
    ${(r.borrow || []).length ? `<small class="hint">가져올 원리: ${esc(r.borrow.join(' / '))}</small><br>` : ''}
    ${(r.gaps || []).length ? `<small class="hint">빈자리 주제: ${esc(r.gaps.join(' / '))}</small><br>` : ''}
    <details><summary>터진 영상 TOP 10</summary>${(x.top || []).map(t => `<div><a href="https://youtu.be/${esc(t.id)}" target="_blank">${esc(t.title)}</a> <small class="hint">평소의 ${t.ratio}배 · ${(t.views || 0).toLocaleString()}회</small></div>`).join('')}</details></div>` }).join('');
  if (d.running) setTimeout(loadStudies, 4000);
}
$('btnStudy').onclick = async () => {
  const url = $('stUrl').value.trim(); if (!url) return;
  try { await post('/api/study', {url, channel_id: $('stCh').value, favorite: $('stFav').checked}); $('stUrl').value = ''; loadStudies() }
  catch (e) { $('stMsg').textContent = e.message }
};

async function loadLearn() {
  api('/api/channels').then(d => { $('stCh').innerHTML = '<option value="">(모든 채널 공통)</option>' + d.channels.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('') });
  loadStudies();
  const L = await api('/api/learning'), pb = L.playbook;
  $('pbMeta').textContent = `분석 영상 ${pb.watched_videos}편 · 내 영상 성과 반영 ${pb.own_videos_measured}편 · 표본 3편 이상 패턴만 표시`;
  const block = (o, neg) => Object.entries(o).filter(([, v]) => v.length).map(([c, v]) =>
    `<div><small class="hint">${esc(FEAT[c] || c)}</small><br>${v.map(x => `<span class="feat ${neg ? 'neg' : ''}" title="표본 ${x.n}">${esc(x.feature.split(':')[1])} ${x.lift > 0 ? '+' : ''}${x.lift}</span>`).join('')}</div>`).join('');
  $('pbDo').innerHTML = block(pb.do) || '<p class="hint">아직 학습 데이터가 부족합니다. 학습 분야를 설정하고 기다리거나 영상을 직접 추가하세요.</p>';
  $('pbAvoid').innerHTML = block(pb.avoid, true) || '<p class="hint">-</p>';
  $('pbNarr').textContent = pb.narrative || '분석 영상이 10편 이상 쌓이면 매주 자동으로 작성됩니다.';
  $('recent').innerHTML = L.recent.map(v => `<div class="vid"><a href="https://youtu.be/${esc(v.id)}" target="_blank">${esc(v.title)}</a><br>
    <small class="hint">${esc(v.channel_title)} · 조회 ${(v.views || 0).toLocaleString()} · 성과 상위 ${v.perf_percentile != null ? Math.round((1 - v.perf_percentile) * 100) + '%' : '-'}</small><br>${esc((v.summary || '').slice(0, 160))}</div>`).join('') || '<p class="hint">아직 없음</p>';
}
$('btnLearn').onclick = async () => { await post('/api/learning/run'); $('learnMsg').textContent = '학습을 시작했습니다. 몇 분 뒤 새로고침하세요.' };
$('btnAdd').onclick = async () => {
  try { const r = await post('/api/learning/add', {url: $('addUrl').value, favorite: $('addFav').checked}); $('learnMsg').textContent = ($('addFav').checked ? '취향 레퍼런스로 ' : '') + '대기열 맨 앞에 추가: ' + r.title; $('addUrl').value = '' }
  catch (e) { $('learnMsg').textContent = e.message }
};

function fillSettings() {
  const s = S.settings, k = S.keys;
  $('keys').innerHTML = Object.entries(k).map(([name, v]) => `<label>${esc(v.label)} ${v.set ? (v.from_env ? '✓ 환경변수' : '✓ 저장됨') : '— 없음'}</label>
    <input type="password" data-key="${esc(name)}" placeholder="${esc(name)}" autocomplete="off">`).join('');
  $('niches').value = (s.learning.niches || []).join(', ');
  $('maxWatch').value = s.learning.max_watch_per_day;
  $('paidTier').value = String(!!s.learning.gemini_paid_tier);
  $('maxVideo').value = s.production.max_ai_video_clips;
  $('imgRatio').value = s.production.ai_image_ratio;
  $('perVideo').value = s.production.max_cost_per_video_usd;
  $('daily').value = s.budget.daily_usd; $('monthly').value = s.budget.monthly_usd;
  $('autoUp').value = String(!!s.production.auto_upload_private);
  $('autoUp') && ($('autoUpd').value = String((s.update || {}).auto !== false));
  $('scriptP').value = s.providers.script; $('ttsP').value = s.providers.tts; $('voiceId').value = s.providers.elevenlabs_voice_id; $('gVoice').value = s.providers.gemini_tts_voice;
  $('ytBox').innerHTML = S.youtube_connected ? '<p class="hint">✓ 연결됨</p>' :
    `<p class="hint">${S.client_secret_present ? 'client_secret.json 확인됨.' : '1) Google Cloud 콘솔에서 OAuth 클라이언트(데스크톱 앱)를 만들고 JSON을 <b>data/client_secret.json</b> 으로 저장하세요.'}</p>
     <button id="btnYt">채널 연결하기 (브라우저 열림)</button>`;
  const b = $('btnYt'); if (b) b.onclick = async () => { try { alert((await post('/api/youtube/connect')).message) } catch (e) { alert(e.message) } };
}
$('btnKeys').onclick = async () => {
  const vals = {}; document.querySelectorAll('[data-key]').forEach(i => { if (i.value.trim()) vals[i.dataset.key] = i.value.trim() });
  const r = await post('/api/secrets', vals); await loadStatus(); fillSettings();
  alert(r.gemini_check ? (r.gemini_check.ok ? '저장했습니다. Gemini 키 확인: 정상 ✓' : 'Gemini 키가 올바르지 않습니다: ' + r.gemini_check.message + '\n\naistudio.google.com/apikey 에서 키를 다시 복사해 붙여넣으세요.') : '저장했습니다');
};
$('btnSettings').onclick = async () => {
  await post('/api/settings', {
    learning: {niches: $('niches').value.split(',').map(x => x.trim()).filter(Boolean), max_watch_per_day: +$('maxWatch').value, gemini_paid_tier: $('paidTier').value === 'true'},
    production: {max_ai_video_clips: +$('maxVideo').value, ai_image_ratio: +$('imgRatio').value, max_cost_per_video_usd: +$('perVideo').value, auto_upload_private: $('autoUp').value === 'true'},
    budget: {daily_usd: +$('daily').value, monthly_usd: +$('monthly').value},
    update: {auto: $('autoUpd').value === 'true'},
    providers: {script: $('scriptP').value, tts: $('ttsP').value, elevenlabs_voice_id: $('voiceId').value.trim(), gemini_tts_voice: $('gVoice').value.trim() || 'Kore'}
  });
  await loadStatus(); alert('저장했습니다');
};

post('/api/secrets/test').catch(() => {}).finally(loadStatus);
setInterval(() => { loadStatus(); if ($('list').classList.contains('on')) loadList() }, 8000);


function usageBlock(u) {
  if (!u || !u.items || !u.items.length) return '';
  const fmtS = s => s == null ? '-' : (s >= 3600 ? (s / 3600).toFixed(1) + '시간' : Math.round(s / 60) + '분');
  return `<h4>이 영상의 사용량</h4>
  <p class="hint">총 비용(추정) <b>$${u.cost_usd.toFixed(3)}</b> · 영상 ${u.video_minutes ?? '-'}분 · 분당 $${u.cost_per_minute ?? '-'} · 제작 소요 ${fmtS(u.total_seconds)}</p>
  <table class="t"><tr><th>항목</th><th>사용량</th><th>호출</th><th>비용</th></tr>
  ${u.items.map(i => `<tr><td>${esc(i.label)}</td><td>${Math.round(i.units).toLocaleString()} ${esc(i.unit)}</td><td>${i.calls}</td><td>$${(i.cost || 0).toFixed(3)}</td></tr>`).join('')}</table>
  ${u.steps.length ? `<p class="hint">단계별 시간: ${u.steps.map(s => esc(STEP[s.step] || s.step) + ' ' + fmtS(s.seconds)).join(' · ')}</p>` : ''}`;
}

let CH = [];
async function loadChannels() {
  const d = await api('/api/channels'); CH = d.channels;
  $('makeChannel').innerHTML = '<option value="">(채널 없이)</option>' + CH.filter(c => c.active).map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  const have = new Set(CH.map(c => c.id));
  $('presetBox').innerHTML = d.presets.filter(p => !have.has(p.id)).map(p => `<button data-preset="${esc(p.id)}">+ ${esc(p.name)}</button>`).join('') + '<button id="chNew">+ 직접 만들기</button>';
  document.querySelectorAll('[data-preset]').forEach(b => b.onclick = async () => { await post('/api/channels', {preset: b.dataset.preset}); loadChannels() });
  $('chNew').onclick = () => editChannel({id: '', name: '', niches: [], audience: '', tone: '', voice_id: '', target_minutes: 10, monthly_videos: 8, style_guide: '', active: 1});
  $('chList').innerHTML = CH.map(c => `<div class="chcard"><b>${esc(c.name)}</b> <span class="badge">${c.active ? '운영' : '중지'}</span>
    <span class="badge ${c.connected ? 'ready' : ''}">${c.connected ? 'YouTube 연결됨' : 'YouTube 미연결'}</span>
    ${c.yt_channel ? `<span class="hint">→ 업로드 채널: ${c.yt_channel.url ? `<a href="${esc(c.yt_channel.url)}" target="_blank"><b>${esc(c.yt_channel.title)}</b></a>` : esc(c.yt_channel.title)}</span>` : ''}<br>
    <small class="hint">분야: ${esc(c.niches.join(', '))} · 학습한 영상 ${c.watched} · 제작 ${c.videos} · 목표 월 ${c.monthly_videos}편</small>
    <div class="row" style="margin-top:8px"><button data-edit="${esc(c.id)}">설정·제작 규칙</button><button data-conn="${esc(c.id)}">YouTube 연결</button>
    <button data-radar="${esc(c.id)}">최신 영상 검색</button><button data-sug="${esc(c.id)}" class="primary">지금 뜰 주제 추천</button>
    <button data-syn="${esc(c.id)}" class="primary">참고 채널 종합 → 빈자리 주제</button></div>
    <div>${synthHtml(c.id, c.synth)}</div>
    <div id="rad-${esc(c.id)}">${radarHtml(c.id, c.radar)}</div></div>`).join('') || '<p class="hint">위 버튼으로 과학·교육 분야 채널을 추가하세요.</p>';
  document.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => editChannel(CH.find(c => c.id === b.dataset.edit)));
  document.querySelectorAll('[data-conn]').forEach(b => b.onclick = async () => { try { alert((await post('/api/youtube/connect?channel_id=' + b.dataset.conn)).message) } catch (e) { alert(e.message) } });
  document.querySelectorAll('[data-radar]').forEach(b => b.onclick = async () => { b.disabled = true; try { const r = await post(`/api/channels/${b.dataset.radar}/radar/refresh`); alert(`최근 2주 영상 ${r.added}개를 찾았습니다 (검색 할당량 사용)`) } catch (e) { alert(e.message) } b.disabled = false });
  document.querySelectorAll('[data-sug]').forEach(b => b.onclick = async () => { b.disabled = true; try { const r = await post(`/api/channels/${b.dataset.sug}/radar/suggest`); if (r.message) alert(r.message); loadChannels() } catch (e) { alert(e.message) } b.disabled = false });
  document.querySelectorAll('[data-syn]').forEach(b => b.onclick = async () => { b.disabled = true; b.textContent = '종합 분석 중… (1~2분)'; try { await post(`/api/channels/${b.dataset.syn}/synth`); loadChannels() } catch (e) { alert(e.message); b.disabled = false; b.textContent = '참고 채널 종합 → 빈자리 주제' } });
  document.querySelectorAll('[data-use]').forEach(b => b.onclick = () => { $('topic').value = b.dataset.use; $('makeChannel').value = b.dataset.ch; if (b.dataset.fmt) $('makeFormat').value = b.dataset.fmt; document.querySelector('[data-tab=make]').click() });
}
function synthHtml(cid, x) {
  if (!x) return '';
  const li = a => (a || []).map(t => `<div>· ${esc(t)}</div>`).join('');
  return `<details open><summary><b>참고 채널 종합 분석</b> <small class="hint">(${esc((x.based_on || []).join(', '))} · ${esc((x.at || '').slice(0, 16).replace('T', ' '))})</small></summary>
    ${x.feel ? `<p class="hint">${esc(x.feel)}</p>` : ''}
    <div class="cols"><div><h4>잘 된 것</h4>${li(x.works)}</div><div><h4>안 된 것</h4>${li(x.fails)}</div></div>
    <h4>빈자리 주제</h4>${(x.gap_topics || []).map(g => `<div class="idea"><b>${esc(g.topic)}</b><br>
      <small class="hint">왜 빈자리: ${esc(g.why_gap || '')}${(g.evidence || []).length ? ' · 근거: ' + esc(g.evidence.join(' / ')) : ''}</small><br>
      <button data-use="${esc(g.topic)}" data-ch="${esc(cid)}" data-fmt="${esc(g.format || '')}">이 주제로 만들기</button></div>`).join('')}</details>`;
}
function radarHtml(cid, r) {
  if (!r || !r.ideas || !r.ideas.length) return '';
  return `<h4>추천 주제 (${esc(r.at.slice(0, 16).replace('T', ' '))})</h4>` + r.ideas.map(i => `<div class="idea"><b>${esc(i.topic)}</b><br>
    <small class="hint">왜 지금: ${esc(i.why_now)} · 감정: ${esc(i.entry_emotion)} → ${esc(i.exit_emotion)}</small><br>
    <button data-use="${esc(i.topic)}" data-ch="${esc(cid)}">이 주제로 만들기</button></div>`).join('');
}
function editChannel(c) {
  const d = $('chEdit'); d.hidden = false;
  d.innerHTML = `<h3>${c.id ? esc(c.name) + ' 설정' : '새 채널'}</h3>
    <div class="row"><div class="grow"><label>이름</label><input id="ceName" value="${esc(c.name)}"></div>
    <div><label>ID(영문)</label><input id="ceId" value="${esc(c.id)}" ${c.id ? 'disabled' : ''}></div></div>
    <label>분야 키워드 (쉼표 구분 — AI가 이 키워드로 영상을 찾아 학습)</label><input id="ceNiches" value="${esc(c.niches.join(', '))}">
    <label>시청자</label><input id="ceAud" value="${esc(c.audience)}">
    <label>말투·톤</label><input id="ceTone" value="${esc(c.tone)}">
    <div class="row"><div class="grow"><label>내레이션 목소리</label><select id="ceVoice"></select></div>
    <div class="grow"><label>강조 장면(훅·반전)만 다른 목소리 (선택)</label><select id="ceAccent"></select></div></div>
    <div class="row">
    <div><label>기본 길이(분)</label><input id="ceMin" type="number" value="${c.target_minutes}"></div>
    <div><label>목표 월 제작 수</label><input id="ceMonthly" type="number" value="${c.monthly_videos}"></div>
    <div><label>상태</label><select id="ceActive"><option value="1">운영</option><option value="0">중지</option></select></div></div>
    <label>제작 규칙 (스킬) — 짧게, 피드백을 주면 AI가 자동으로 갱신</label><textarea id="ceGuide" rows="8">${esc(c.style_guide)}</textarea>
    <button id="ceSave" class="primary">저장</button>`;
  $('ceActive').value = String(c.active ?? 1);
  let cur = {}; try { cur = c.voice ? JSON.parse(c.voice) : {} } catch {}
  if (!cur.engine && c.voice_id) cur = {engine: 'elevenlabs', ref: c.voice_id};
  voiceOptions().then(opts => {
    const key = v => v && v.engine ? v.engine + '|' + (v.ref || '') : '';
    const html = first => `<option value="">${first}</option>` + opts.map(o => `<option value="${esc(key(o.spec))}">${esc(o.name)}</option>`).join('');
    $('ceVoice').innerHTML = html('(기본 설정 따름)'); $('ceAccent').innerHTML = html('(사용 안 함)');
    $('ceVoice').value = key(cur); $('ceAccent').value = key(cur.accent);
    if (key(cur) && !$('ceVoice').value) { $('ceVoice').insertAdjacentHTML('beforeend', `<option value="${esc(key(cur))}">${esc(key(cur))}</option>`); $('ceVoice').value = key(cur) }
  });
  $('ceSave').onclick = async () => {
    const parse = v => { if (!v) return null; const [engine, ...r] = v.split('|'); return {engine, ref: r.join('|')} };
    const main = parse($('ceVoice').value), acc = parse($('ceAccent').value);
    const voice = main ? JSON.stringify(acc ? {...main, accent: acc} : main) : '';
    await post('/api/channels', {id: c.id || $('ceId').value.trim(), name: $('ceName').value, niches: $('ceNiches').value, audience: $('ceAud').value,
      tone: $('ceTone').value, voice, voice_id: '', target_minutes: +$('ceMin').value, monthly_videos: +$('ceMonthly').value,
      active: +$('ceActive').value, style_guide: $('ceGuide').value});
    d.hidden = true; loadChannels();
  };
  d.scrollIntoView({behavior: 'smooth'});
}

async function loadUsage() {
  const b = $('uBudget').value;
  const d = await api('/api/usage' + (b ? '?monthly_budget=' + b : '')), c = d.cadence;
  if (!b) $('uBudget').value = c.monthly_budget;
  if (!c.measured_videos) { $('uSummary').innerHTML = `<p class="hint">${esc(c.message)} (최근 30일 학습 비용 $${c.learning_cost_30d})</p>`; $('uChannels').innerHTML = ''; $('uRecent').innerHTML = ''; return }
  $('uSummary').innerHTML = `<div class="grid4">
    <div class="card"><small>1편 평균 비용</small><b>$${c.avg_cost_usd}</b><em>측정 ${c.measured_videos}편</em></div>
    <div class="card"><small>1편 평균 제작 시간</small><b>${c.avg_production_minutes ?? '-'}분</b><em>PC가 켜져 있는 동안 자동</em></div>
    <div class="card"><small>최근 30일 학습 비용</small><b>$${c.learning_cost_30d}</b><em>AI 시청·분석 (채널 공통)</em></div>
    <div class="card"><small>월 예산으로 가능한 편수</small><b>${c.affordable_videos_per_month ?? '-'}편</b><em>원하는 합계 ${c.wanted_videos_per_month ?? '-'}편</em></div></div>
    <p class="hint">${esc(c.youtube_quota_note)}. 비용은 설정의 단가로 계산한 추정치입니다 — 각 서비스 콘솔의 실제 청구액과 비교해 단가를 맞추세요.</p>`;
  $('uChannels').innerHTML = (c.per_channel || []).map(x => `<div class="chcard"><b>${esc(x.name)}</b><br>
    <small class="hint">1편 $${x.avg_cost_usd} (${x.measured ? '실측 ' + x.measured + '편' : '전체 평균'}) · 목표 월 ${x.wanted_per_month}편 → 예산상 <b>월 ${x.budget_allows_per_month}편</b> · 권장 간격 <b>${x.suggested_interval_days ?? '-'}일</b></small></div>`).join('') || '<p class="hint">채널을 만들면 채널별로 계산합니다.</p>';
  $('uRecent').innerHTML = `<table class="t"><tr><th>영상</th><th>길이</th><th>비용</th><th>제작 시간</th></tr>${c.recent.map(r => `<tr><td>${esc((r.title || '').slice(0, 30))}</td><td>${r.video_minutes ?? '-'}분</td><td>$${r.cost_usd}</td><td>${r.total_seconds ? Math.round(r.total_seconds / 60) + '분' : '-'}</td></tr>`).join('')}</table>`;
}
$('btnCalc').onclick = loadUsage;


// ---------------- voices ----------------
let VO = null;
async function voiceOptions() {
  VO = await api('/api/voices');
  return [...VO.mine.map(v => ({...v, name: '★ ' + v.name})), ...VO.prebuilt];
}
const readB64 = f => new Promise((ok, no) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = no; r.readAsDataURL(f) });
const extOf = f => (f.name.match(/\.[A-Za-z0-9]+$/) || ['.wav'])[0];

async function loadVoices() {
  const opts = await voiceOptions();
  $('vSample').textContent = VO.sample_script;
  $('vConsent').textContent = VO.consent['ko-KR'];
  $('vList').innerHTML = VO.mine.map(v => `<div class="chcard"><b>${esc(v.name)}</b> <span class="badge">${esc(v.engine)}</span>
    <small class="hint">${esc(v.note || '')} · ${esc((v.created_at || '').slice(0, 10))}</small>
    <button data-vdel="${esc(v.id)}">삭제</button></div>`).join('') || '<p class="hint">아직 없습니다. 등록 전에는 Gemini 기본 목소리로 제작됩니다.</p>';
  document.querySelectorAll('[data-vdel]').forEach(b => b.onclick = async () => {
    if (!confirm('이 목소리를 삭제할까요? (서비스에 저장된 복제 목소리도 함께 삭제)')) return;
    const r = await post(`/api/voices/${b.dataset.vdel}/delete`); if (r.note) alert(r.note); loadVoices();
  });
  $('aCands').innerHTML = opts.map((o, i) => `<label class="inline"><input type="checkbox" data-cand="${i}" ${o.kind !== 'prebuilt' || i < 0 ? 'checked' : ''}> ${esc(o.name)}</label>`).join('<br>');
  const anyChecked = [...document.querySelectorAll('[data-cand]')].some(x => x.checked);
  if (!anyChecked) { const g = opts.findIndex(o => o.engine === 'gemini'); const e = opts.findIndex(o => o.engine === 'edge');
    [g, e].forEach(i => { if (i >= 0) document.querySelector(`[data-cand="${i}"]`).checked = true }) }
  $('aRun').onclick = async () => {
    const chosen = [...document.querySelectorAll('[data-cand]')].filter(x => x.checked).map(x => opts[+x.dataset.cand]).slice(0, 6);
    if (!chosen.length) return;
    $('aRun').disabled = true; $('aOut').innerHTML = '<p class="hint">만드는 중… (목소리당 몇 초)</p>';
    try {
      const r = await post('/api/voices/audition', {text: $('aText').value, tone: $('aTone').value, candidates: chosen.map(o => ({label: o.name, spec: o.spec}))});
      $('aOut').innerHTML = r.results.map(x => `<div class="chcard"><b>${esc(x.label)}</b><br>${x.file ? `<audio controls src="/audition/${esc(x.file)}"></audio>` : `<span class="err">${esc(x.error)}</span>`}</div>`).join('');
    } catch (e) { $('aOut').innerHTML = `<div class="msg err">${esc(e.message)}</div>` }
    $('aRun').disabled = false;
  };
}
$('vReg').onclick = async () => {
  const src = $('vSrc').files[0], con = $('vCon').files[0];
  const engines = [$('vG').checked && 'gemini', $('vE').checked && 'elevenlabs'].filter(Boolean);
  if (!src || !engines.length) return alert('목소리 샘플 파일과 등록할 곳을 선택하세요');
  if (engines.includes('gemini') && !con) return alert('Gemini 등록에는 동의 녹음 파일이 필요합니다');
  $('vReg').disabled = true; $('vMsg').textContent = '등록 중…';
  try {
    const r = await post('/api/voices/register', {name: $('vName').value || '내 목소리', engines, source_b64: await readB64(src), source_ext: extOf(src),
      consent_b64: con ? await readB64(con) : '', consent_ext: con ? extOf(con) : '.wav'});
    $('vMsg').textContent = (r.registered.length ? '등록 완료: ' + r.registered.map(v => v.name).join(', ') + ' ' : '') + (r.errors.length ? '실패: ' + r.errors.join(' / ') : '');
    loadVoices();
  } catch (e) { $('vMsg').textContent = e.message }
  $('vReg').disabled = false;
};
$('mAdd').onclick = async () => {
  try { await post('/api/voices/manual', {name: $('mName').value || '직접 입력', engine: $('mEng').value, ref: $('mRef').value.trim()}); $('mRef').value = ''; loadVoices() }
  catch (e) { alert(e.message) }
};

$('btnUpd').onclick = async () => { await post('/api/update/check'); $('updInfo').textContent = '확인 중… 새 버전이 있으면 자동으로 적용하고 다시 시작합니다'; setTimeout(checkVersion, 8000) };
$('btnQuit').onclick = async () => {
  if (!confirm('프로그램을 완전히 끌까요? (AI 학습·제작이 멈춥니다. 창만 닫으면 백그라운드에서 계속 학습합니다)')) return;
  try { await post('/api/shutdown') } catch {}
  document.body.innerHTML = '<p style="padding:40px;font-size:18px">AutoTube Studio를 종료했습니다. 이 창을 닫으세요. 다시 시작하려면 바탕화면의 AutoTube Studio 아이콘을 누르세요.</p>';
};

$('vRemote').onclick = async () => {
  $('vRemoteList').innerHTML = '<p class="hint">불러오는 중…</p>';
  try {
    const r = await api('/api/voices/remote');
    $('vRemoteList').innerHTML = r.voices.length ? r.voices.map((v, i) => `<div class="chcard"><b>${esc(v.name)}</b>
      <small class="hint">${esc(v.type)} · ${esc(v.ref)} ${v.created ? '· ' + esc(v.created.slice(0, 10)) : ''}</small>
      ${v.imported ? '<span class="badge ready">가져옴</span>' : `<button data-imp="${i}">이 목소리 가져오기</button>`}</div>`).join('')
      : '<p class="hint">이 키의 프로젝트에 저장된 목소리가 없습니다. 다른 프로젝트에서 만들었다면 AI Studio에서 voice_… ID를 복사해 아래 "직접 넣기"에 붙여넣으세요.</p>';
    document.querySelectorAll('[data-imp]').forEach(b => b.onclick = async () => {
      const v = r.voices[+b.dataset.imp];
      await post('/api/voices/manual', {name: v.name, engine: 'gemini', ref: v.ref}); loadVoices(); $('vRemote').click();
    });
  } catch (e) { $('vRemoteList').innerHTML = `<div class="msg err">${esc(e.message)}</div>` }
};
