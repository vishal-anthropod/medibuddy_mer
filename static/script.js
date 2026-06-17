async function fetchJSON(url){
  const r = await fetch(url);
  if(!r.ok) throw new Error('Failed '+url);
  return await r.json();
}

function getEP(key, fallback){
  try{ return (window.DASHBOARD_CONFIG?.endpoints?.[key]) || fallback; }catch{return fallback;}
}

function paramsEndpointsFromURL(){
  try{
    const url = new URL(window.location.href);
    const rid = url.searchParams.get('rid');
    const call = url.searchParams.get('call');
    if(rid && call){
      const base = `/api/records/${encodeURIComponent(rid)}/calls/${encodeURIComponent(call)}`;
      return {
        metadata: `${base}/metadata`,
        report: `${base}/report`,
        report2: `${base}/report2`,
        transcript: `${base}/transcript`,
        audio: `${base}/audio`,
      };
    }
  }catch{}
  return null;
}

function fmtTime(sec){
  if(!isFinite(sec)) return '0:00';
  const m = Math.floor(sec/60);
  const s = Math.floor(sec%60);
  return `${m}:${s.toString().padStart(2,'0')}`;
}

function renderTop(metrics){
  const metricsEl = document.getElementById('metrics');
  const metaEl = document.getElementById('meta');
  const acc = metrics.top?.accuracy ?? 'N/A';
  const qA = metrics.top?.questions_asked ?? 'N';
  const qT = metrics.top?.total_questions ?? 'A';
  const errs = metrics.top?.documentation_errors ?? 0;
  const crit = metrics.top?.critical_errors ?? 0;
  const dur = metrics.top?.duration ?? '0:00';
  const br = metrics.top?.accuracy_breakdown || {};

  const accColor = (n)=> n>=85? 'style="color:#10B981"': (n>=70? 'style="color:#F59E0B"': 'style="color:#EF4444"');
  const accVal = typeof acc==='number'? `<span class="value" ${accColor(acc)}>${acc}%</span>`: `<span class="value">${acc}</span>`;
  metricsEl.innerHTML = `
    <div class="metric">${accVal} <span class="label">Accuracy</span></div>
    <div class="metric"><span class="value">${qA}/${qT}</span> <span class="label">Questions</span></div>
    <div class="metric" style="color:#EF4444"><span class="value">${errs}</span> <span class="label">Errors</span></div>
    <div class="metric" style="color:#F59E0B"><span class="value">${crit}</span> <span class="label">Critical</span></div>
  `;
  metaEl.textContent = `Duration: ${dur}`;

  // Inject MER PDF link near the player header if available
  try{
    const url = metrics.mer_pdf_url;
    if(url){
      const hdrs = document.querySelectorAll('.player-header');
      if(hdrs && hdrs[0]){
        const link = document.createElement('a');
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = 'Open MER PDF';
        link.className = 'btn';
        hdrs[0].appendChild(link);
      }
      if(hdrs && hdrs[1]){
        const link2 = document.createElement('a');
        link2.href = url;
        link2.target = '_blank';
        link2.rel = 'noopener noreferrer';
        link2.textContent = 'Open MER PDF';
        link2.className = 'btn';
        hdrs[1].appendChild(link2);
      }
    }
  }catch{}
}

function renderAccuracyBreakdown(top){
  const br = top?.accuracy_breakdown || {};
  const rawTotal = top?.raw_total_questions ?? '-';
  const excluded = br.excluded_na ?? top?.excluded_na ?? 0;
  const denominator = br.denominator ?? top?.total_questions ?? '-';
  const correct = br.correct ?? top?.correct_responses ?? '-';
  const paraphrased = br.paraphrased_accepted ?? top?.paraphrased_responses ?? 0;
  const accepted = br.accepted ?? top?.accepted_responses ?? '-';
  const failed = Number(br.incorrect || 0) + Number(br.missing || 0) + Number(br.clubbed || 0);
  const accuracy = top?.accuracy ?? '-';
  return `
    <div class="accuracy-table" aria-label="Accuracy calculation">
      <div><span>Total MER questions</span><b>${rawTotal}</b></div>
      <div><span>Not applicable</span><b>${excluded}</b></div>
      <div><span>Scored questions</span><b>${denominator}</b></div>
      <div><span>Correct</span><b>${correct}</b></div>
      <div><span>Paraphrased accepted</span><b>${paraphrased}</b></div>
      <div><span>Incorrect / incomplete</span><b>${failed}</b></div>
      <div class="formula"><span>Accuracy</span><b>${accepted} / ${denominator} = ${accuracy}%</b></div>
    </div>
  `;
}

function renderSpeakerStats(stats){
  const el = document.getElementById('speaker-stats');
  const agentW = stats.agent_pct || 0;
  const custW = stats.customer_pct || 0;
  const deadW = stats.dead_air_pct || 0;
  const doctorWpm = stats.doctor_wpm ?? 0;
  const customerWpm = stats.customer_wpm ?? 0;
  el.innerHTML = `
    <div class="speaker-bar">
      <div class="agent" style="width:${agentW}%;"></div>
      <div class="customer" style="width:${custW}%;"></div>
      <div class="dead" style="width:${deadW}%;"></div>
    </div>
    <div style="display:flex;gap:12px;font-size:12px;color:#6B7280;flex-wrap:wrap">
      <div>Doctor: ${agentW}%</div>
      <div>Customer: ${custW}%</div>
      <div>Dead Air: ${deadW}%</div>
      <div>Doctor WPM: ${doctorWpm}</div>
      <div>Customer WPM: ${customerWpm}</div>
    </div>
  `;
}

function createQuestionCard(item){
  const id = item.question_id || 'N/A';
  const status = (item.status || 'Unknown').toLowerCase();
  const timestamp = item.timestamp || null;
  const q = item.question_text || '';
  const captured = item.captured_response || '';
  const expected = item.expected_response || '';
  const suggestion = item.suggested_correction || '';

  const badgeClass = status.includes('incorrect') ? 'incorrect'
                   : status.includes('correct') ? 'correct'
                   : status.includes('missing') ? 'missing'
                   : 'paraphrased';
  const btn = timestamp? `<button class="timestamp" data-ts="${timestamp}">⏱ ${timestamp}</button>`: '';

  const div = document.createElement('div');
  div.className = 'question-card';
  div.innerHTML = `
    <div class="q-header">
      <div class="q-id">${id}</div>
      <div class="q-text">${q}</div>
      <span class="badge ${badgeClass}">${item.status || 'Unknown'}</span>
      ${btn}
    </div>
    <div class="two-col">
      <div>
        <div style="font-weight:600;color:#374151">Customer Said</div>
        <div>${captured || '<i>Not asked</i>'}</div>
      </div>
      <div>
        <div style="font-weight:600;color:#374151">Doctor Documented</div>
        <div>${expected}</div>
      </div>
    </div>
    ${suggestion? `<div style="margin-top:8px;color:#92400e">⚠️ ${suggestion}</div>`: ''}
  `;
  return div;
}

function renderCritical(report){
  const el = document.getElementById('critical');
  const issues = report.summary?.critical_issues || [];
  if(!issues.length){ el.style.display='none'; return; }
  const list = issues.map(i=> `<li>❌ ${i}</li>`).join('');
  el.innerHTML = `
    <h3 style="margin:0 0 8px 0">Critical Errors</h3>
    <ul style="margin:0 0 0 16px; padding:0">${list}</ul>
  `;
}

function renderPersonal(report){
  const el = document.getElementById('personal');
  const p = report.personal_particulars;
  if(!el || !p){ el && (el.style.display='none'); return; }
  const ids = (p.id_proofs||[]).map(i=> `${i.type}: ${i.value} ${i.present_in_mer===false?'(not in MER)':''}`).join(', ');
  el.innerHTML = `
    <h3 style="margin:0 0 8px 0">Personal Particulars</h3>
    <div><b>Name</b>: ${p.name ?? '-'}</div>
    <div><b>DOB</b>: ${p.dob ?? '-'}</div>
    <div><b>ID Proofs</b>: ${ids || '-'}</div>
    <div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;margin-top:8px">
      <div><b>Nominee Name</b>: ${p.nominee_name ?? '-'}</div>
      <div><b>Nominee DOB</b>: ${p.nominee_dob ?? '-'}</div>
    </div>
  `;
}

function renderProcess(report){
  const el = document.getElementById('process');
  const pc = report.process_compliance;
  if(!el || !pc){ el && (el.style.display='none'); return; }
  const d = pc.disclaimer||{}; const l = pc.language_preference||{};
  el.innerHTML = `
    <h3 style="margin:0 0 8px 0">Disclaimer & Language Check</h3>
    <div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px">
      <div>
        <div><b>Disclaimer Read</b>: ${d.stated? 'Yes' : 'No'}</div>
        <div><b>Insurer</b>: ${d.insurer_name || '-'}</div>
        <div><b>Time</b>: ${d.timestamp || '-'}</div>
      </div>
      <div>
        <div><b>Language Asked</b>: ${l.asked? 'Yes' : 'No'}</div>
        <div><b>Selected</b>: ${l.selected_language || '-'}</div>
        <div><b>Time</b>: ${l.timestamp || '-'}</div>
      </div>
    </div>
  `;
}

function renderMatrix(report){
  const el = document.getElementById('matrix');
  const items = report.qa_matrix || [];
  const container = document.createElement('div');
  items.forEach(it => container.appendChild(createQuestionCard(it)));
  el.innerHTML = '<h3 style="margin:0 0 8px 0">Question Analysis</h3>';
  el.appendChild(container);
}

function renderRecs(report){
  const el = document.getElementById('recs');
  const recs = report.summary?.recommendations || [];
  if(!recs.length){ el.style.display='none'; return; }
  const list = recs.map(r=> `<li>✅ ${r}</li>`).join('');
  el.innerHTML = `
    <h3 style="margin:0 0 8px 0">Recommendations</h3>
    <ul style="margin:0 0 0 16px; padding:0">${list}</ul>
  `;
}

function bindAudioControls(){
  const audio = document.getElementById('audio');
  const back = document.getElementById('back10');
  const fwd = document.getElementById('fwd10');
  const pp = document.getElementById('playpause');
  const vol = document.getElementById('volume');
  const time = document.getElementById('time-display');
  const cfg = window.DASHBOARD_CONFIG || {skipInterval:10, highlightDuration:2000};

  back.onclick = ()=> { audio.currentTime = Math.max(0, audio.currentTime - cfg.skipInterval); };
  fwd.onclick = ()=> { audio.currentTime = Math.min(audio.duration||1e9, audio.currentTime + cfg.skipInterval); };
  pp.onclick = ()=> { if(audio.paused){ audio.play(); pp.textContent='⏸'; } else { audio.pause(); pp.textContent='▶️'; } };
  vol.oninput = ()=> { audio.volume = parseFloat(vol.value || '0.8'); };

  audio.addEventListener('timeupdate', ()=>{
    const cur = fmtTime(audio.currentTime||0);
    const dur = fmtTime(isFinite(audio.duration)? audio.duration : 0);
    time.textContent = `${cur} / ${dur}`;
  });
}

function parseTS(ts){
  if(!ts) return 0;
  const clean = String(ts).trim();
  const p = clean.split(':').map(x=>parseInt(x, 10));
  if(p.length===3) return (p[0]||0)*3600 + (p[1]||0)*60 + (p[2]||0);
  if(p.length===2) return (p[0]||0)*60 + (p[1]||0);
  return p[0] || 0;
}

function visibleAudio(){
  const part2Visible = document.querySelector('main[data-view="part2"]')?.style.display !== 'none';
  return (part2Visible ? document.getElementById('audio2') : document.getElementById('audio')) || document.getElementById('audio');
}

function showMediaView(target, view){
  const suffix = target === 'part2' ? '-2' : '';
  const right = document.querySelector(`.media-tab[data-target="${target}"]`)?.closest('.right-panel');
  if(!right) return;
  right.querySelectorAll('.media-tab').forEach(btn => btn.classList.toggle('active', btn.dataset.mediaView === view));
  const player = right.querySelector('.player');
  const transcript = target === 'part2' ? document.getElementById('transcript2') : document.getElementById('transcript');
  const mer = document.getElementById(`mer-viewer${suffix}`);
  if(player) player.style.display = view === 'recording' ? '' : 'none';
  if(transcript) transcript.style.display = view === 'recording' ? '' : 'none';
  if(mer) mer.style.display = view === 'mer' ? '' : 'none';
}

function findTranscriptTarget(container, sec){
  const entries = Array.from(container.querySelectorAll('.entry'));
  if(!entries.length) return null;
  return entries.find(x => {
    const start = Number(x.dataset.startSec || 0);
    const end = Number(x.dataset.endSec || start);
    return sec >= start && sec <= end;
  }) || entries.reduce((best, item) => {
    const dist = Math.abs(Number(item.dataset.startSec || 0) - sec);
    return !best || dist < best.dist ? {item, dist} : best;
  }, null)?.item || null;
}

function seekToTimestamp(ts, part='part1'){
  const sec = parseTS(ts);
  showMediaView(part, 'recording');
  const audio = part === 'part2' ? document.getElementById('audio2') : visibleAudio();
  if(audio){
    audio.currentTime = sec;
    audio.play().catch(()=>{});
  }
  const transcript = part === 'part2' ? document.getElementById('transcript2') : document.getElementById('transcript');
  if(!transcript) return;
  const tEntries = transcript.querySelectorAll('.entry');
  tEntries.forEach(x=> x.classList.remove('active'));
  const target = findTranscriptTarget(transcript, sec);
  if(target){
    target.classList.add('active');
    target.scrollIntoView({behavior:'smooth', block:'center'});
    setTimeout(()=> target.classList.remove('active'), (window.DASHBOARD_CONFIG?.highlightDuration)||2000);
  }
}

function bindMediaTabs(meta){
  const merUrl = meta?.mer_pdf_url;
  if(merUrl){
    const frame = document.getElementById('mer-frame');
    const frame2 = document.getElementById('mer-frame-2');
    if(frame) frame.src = merUrl;
    if(frame2) frame2.src = merUrl;
  } else {
    document.querySelectorAll('.media-tab[data-media-view="mer"]').forEach(btn => {
      btn.disabled = true;
      btn.title = 'MER PDF unavailable';
    });
  }
  document.querySelectorAll('.media-tab').forEach(btn => {
    btn.addEventListener('click', () => showMediaView(btn.dataset.target, btn.dataset.mediaView));
  });
}

function bindTimestampClicks(){
  const matrix = document.getElementById('matrix');
  if(!matrix) return;
  matrix.addEventListener('click', (e)=>{
    const btn = e.target.closest('button.timestamp');
    if(!btn) return;
    seekToTimestamp(btn.dataset.ts, 'part1');
  });
}

function bindAudioControls2(){
  const audio = document.getElementById('audio2');
  if(!audio) return;
  const back = document.getElementById('back10-2');
  const fwd = document.getElementById('fwd10-2');
  const pp = document.getElementById('playpause-2');
  const vol = document.getElementById('volume-2');
  const time = document.getElementById('time-display-2');
  const cfg = window.DASHBOARD_CONFIG || {skipInterval:10, highlightDuration:2000};
  back.onclick = ()=> { audio.currentTime = Math.max(0, audio.currentTime - cfg.skipInterval); };
  fwd.onclick = ()=> { audio.currentTime = Math.min(audio.duration||1e9, audio.currentTime + cfg.skipInterval); };
  pp.onclick = ()=> { if(audio.paused){ audio.play(); pp.textContent='⏸'; } else { audio.pause(); pp.textContent='▶️'; } };
  vol.oninput = ()=> { audio.volume = parseFloat(vol.value || '0.8'); };
  audio.addEventListener('timeupdate', ()=>{
    const cur = fmtTime(audio.currentTime||0);
    const dur = fmtTime(isFinite(audio.duration)? audio.duration : 0);
    time.textContent = `${cur} / ${dur}`;
  });
}

function renderTranscript(data){
  const el = document.getElementById('transcript');
  const segs = data.segments || [];
  el.innerHTML = '<h3 style="margin:0 0 8px 0">Transcript</h3>';
  const container = document.createElement('div');
  segs.forEach(s=>{
    const div = document.createElement('div');
    const rawSpk = (s.speaker||'').toLowerCase();
    const cssSpk = rawSpk === 'doctor' ? 'doctor' : rawSpk;
    const label = rawSpk ? rawSpk.charAt(0).toUpperCase() + rawSpk.slice(1) : 'Speaker';
    div.className = `entry ${cssSpk}`;
    div.dataset.start = s.start_timestamp || '';
    div.dataset.startSec = parseTS(s.start_timestamp || '');
    div.dataset.endSec = parseTS(s.end_timestamp || s.start_timestamp || '');
    const time = s.start_timestamp || '';
    div.innerHTML = `
      <div class="meta">
        <div class="speaker">${label}</div>
        <div class="time">${time}</div>
      </div>
      <div>${(s.text || '').replace(/</g,'&lt;')}</div>
    `;
    container.appendChild(div);
  });
  el.appendChild(container);
}

function renderTranscript2(data){
  const el = document.getElementById('transcript2');
  if(!el) return;
  const segs = data.segments || [];
  el.innerHTML = '<h3 style="margin:0 0 8px 0">Transcript</h3>';
  const container = document.createElement('div');
  segs.forEach(s=>{
    const div = document.createElement('div');
    const rawSpk = (s.speaker||'').toLowerCase();
    const cssSpk = rawSpk === 'doctor' ? 'doctor' : rawSpk;
    const label = rawSpk ? rawSpk.charAt(0).toUpperCase() + rawSpk.slice(1) : 'Speaker';
    div.className = `entry ${cssSpk}`;
    div.dataset.start = s.start_timestamp || '';
    div.dataset.startSec = parseTS(s.start_timestamp || '');
    div.dataset.endSec = parseTS(s.end_timestamp || s.start_timestamp || '');
    const time = s.start_timestamp || '';
    div.innerHTML = `
      <div class="meta">
        <div class="speaker">${label}</div>
        <div class="time">${time}</div>
      </div>
      <div>${(s.text || '').replace(/</g,'&lt;')}</div>
    `;
    container.appendChild(div);
  });
  el.appendChild(container);
}

async function init(){
  // Override audio src if supplied via config
  try{
    // Build endpoints from URL if provided
    const urlEP = paramsEndpointsFromURL();
    if(urlEP){
      window.DASHBOARD_CONFIG = window.DASHBOARD_CONFIG || {};
      window.DASHBOARD_CONFIG.endpoints = Object.assign({}, window.DASHBOARD_CONFIG.endpoints||{}, urlEP);
    }
    const epAudio = getEP('audio', null);
    if(epAudio){ const a=document.getElementById('audio'); if(a) a.src = epAudio; const a2=document.getElementById('audio2'); if(a2) a2.src = epAudio; }
  }catch{}

  const [meta, report, transcript] = await Promise.all([
    fetchJSON(getEP('metadata','/api/metadata')),
    fetchJSON(getEP('report','/api/report')),
    fetchJSON(getEP('transcript','/api/transcript')),
  ]);
  // Load Part 2 report using rid/call-specific endpoint first
  let report2 = {};
  let recordDetails = {};
  let qcScore = {};
  try{
    const url = new URL(window.location.href);
    const rid = url.searchParams.get('rid');
    const call = url.searchParams.get('call') || '1';
    if(rid){
      report2 = await fetchJSON(`/api/records/${encodeURIComponent(rid)}/calls/${encodeURIComponent(call)}/report2`);
    } else {
      report2 = await fetchJSON(getEP('report2','/api/report2'));
    }
  }catch{}
  try{
    const u = new URL(window.location.href);
    const rid = u.searchParams.get('rid');
    if(rid){
      recordDetails = await fetchJSON(`/api/records/${encodeURIComponent(rid)}`);
      qcScore = await fetchJSON(`/api/records/${encodeURIComponent(rid)}/qcscore`);
    }
  }catch{}
  // Fallback: if Part 2 missing, pull from merged record endpoint
  try{
    if(!report2 || !report2.qc_parameters){
      const u = new URL(window.location.href);
      const rid = u.searchParams.get('rid');
      if(rid){
        const rec = await fetchJSON(`/api/records/${encodeURIComponent(rid)}`);
        if(rec && rec.merged && rec.merged.qc && rec.merged.qc.qc_parameters){
          report2 = rec.merged.qc;
        }
      }
    }
  }catch{}

  renderTop(meta);
  bindMediaTabs(meta);
  renderSpeakerStats(meta.speaker||{});
  renderOverview(meta, report);
  renderCritical(report);
  renderProcess(report); // Section 1
  renderPersonal(report); // Section 2
  renderMatrix(report); // Section 3
  renderRecs(report);
  renderAggregate(meta, report, recordDetails, qcScore);
  renderTranscript(transcript);
  renderTranscript2(transcript);
  bindAudioControls();
  bindAudioControls2();
  bindTimestampClicks();
  enableDividerResize();
  const btn = document.getElementById('export-pdf');
  if(btn){ btn.addEventListener('click', exportPDF); }
  setupTabs(report2);
}

init().catch(err=>{
  console.error(err);
  alert('Failed to load dashboard');
});

// Resizable divider logic
function enableDividerResize(){
  const layout = document.querySelector('.layout');
  const divider = document.getElementById('divider');
  const left = document.querySelector('.left-panel');
  const right = document.querySelector('.right-panel');
  if(!layout || !divider || !left || !right) return;

  // Initial columns: left | divider | right
  let isDragging = false;
  let startX = 0;
  let startLeftWidth = left.getBoundingClientRect().width;
  let startRightWidth = right.getBoundingClientRect().width;

  const min = 320; // px

  function onDown(e){
    isDragging = true;
    startX = (e.touches? e.touches[0].clientX : e.clientX);
    const rect = left.getBoundingClientRect();
    startLeftWidth = rect.width;
    startRightWidth = right.getBoundingClientRect().width;
    document.body.style.cursor = 'col-resize';
    e.preventDefault();
  }

  function onMove(e){
    if(!isDragging) return;
    const x = (e.touches? e.touches[0].clientX : e.clientX);
    const dx = x - startX;
    let newLeft = Math.max(min, startLeftWidth + dx);
    let newRight = Math.max(min, startRightWidth - dx);
    const total = newLeft + newRight;
    layout.style.gridTemplateColumns = `${newLeft}px 6px ${newRight}px`;
  }

  function onUp(){
    if(!isDragging) return;
    isDragging = false;
    document.body.style.cursor = '';
  }

  divider.addEventListener('mousedown', onDown);
  divider.addEventListener('touchstart', onDown, {passive:false});
  window.addEventListener('mousemove', onMove);
  window.addEventListener('touchmove', onMove, {passive:false});
  window.addEventListener('mouseup', onUp);
  window.addEventListener('touchend', onUp);
}

// Export to PDF by invoking print; landscape handled by print CSS
function exportPDF(){
  setTimeout(()=> window.print(), 50);
}

function renderQCPart2(qc){
  const el = document.getElementById('qc2');
  if(!el || !qc || !qc.qc_parameters){ el && (el.innerHTML = '<h3>QA Part 2</h3><div>No data</div>'); return; }
  const p = qc.qc_parameters;
  const row = (title, obj)=>{
    const value = obj?.value ?? '-';
    const exp = obj?.explanation ?? '';
    const tsVal = (obj?.timestamps ?? obj?.timestamp ?? {});
    const tsList = Array.isArray(tsVal)? tsVal : (typeof tsVal==='string'? [tsVal] : Object.values(tsVal||{}));
    const tsLinks = tsList.filter(Boolean).map(ts=> `<a href="#" class="qc-ts" data-ts="${ts}">${ts}</a>`).join(', ');
    return `<div class="question-card"><div style="display:flex;justify-content:space-between"><b>${title}</b><span class="badge ${String(value).toLowerCase()}">${value}</span></div><div style="color:#374151;margin-top:4px">${exp}</div><div style="color:#6B7280;margin-top:4px">${tsLinks||'-'}</div></div>`;
  };
  el.innerHTML = '<h3 style="margin:0 0 8px 0">QA Part 2 - QC Parameters</h3>' +
    [
      row('Greetings', p.greetings),
      row("Call Opening", p.call_opening),
      row("Language Preference", p.language_preference),
      row("ID Validation", p.id_validation),
      row("Disclaimer", p.disclaimer),
      row("Politeness", p.politeness),
      row("Empathy", p.empathy),
      row("Communication Skills", p.communication_skills),
      row("Probing", p.probing),
      row("Observations", p.observations),
      row("Call Closure", p.call_closure)
    ].join('');
}

function setupTabs(report2){
  const tabs = document.querySelectorAll('.tab');
  const views = document.querySelectorAll('main[data-view]');
  renderQCPart2(report2);
  tabs.forEach(t=> t.addEventListener('click', ()=>{
    tabs.forEach(x=> x.classList.remove('active'));
    t.classList.add('active');
    const name = t.dataset.tab;
    views.forEach(v=> {
      if(v.dataset.view !== name){
        v.style.display = 'none';
      } else {
        v.style.display = v.classList.contains('layout') ? 'grid' : 'block';
      }
    });
    // When switching to Part 2 tab, refresh QC2 from per-record endpoint
    if(name === 'part2'){
      try{
        const u = new URL(window.location.href);
        const rid = u.searchParams.get('rid');
        const call = u.searchParams.get('call') || '1';
        if(rid){
          fetchJSON(`/api/records/${encodeURIComponent(rid)}/calls/${encodeURIComponent(call)}/report2`).then(r=>{
            if(r && r.qc_parameters){ renderQCPart2(r); }
            else{ return fetchJSON(`/api/records/${encodeURIComponent(rid)}`); }
          }).then(rec=>{
            if(rec && rec.merged && rec.merged.qc && rec.merged.qc.qc_parameters){ renderQCPart2(rec.merged.qc); }
          }).catch(()=>{});
        }
      }catch{}
    }
  }));
  // Clickable timestamps (seek both players)
  document.addEventListener('click', (e)=>{
    const a = e.target.closest('a.qc-ts');
    if(!a) return;
    e.preventDefault();
    const ts = a.dataset.ts;
    seekToTimestamp(ts, 'part2');
  });
}

function renderOverview(meta, report){
  const el = document.getElementById('overview');
  if(!el) return;
  const t = meta.top || {};
  el.innerHTML = `
    <h3 style="margin:0 0 8px 0">Overview</h3>
    <div style="display:flex;gap:12px;flex-wrap:wrap;color:#374151">
      <div><b>${(t.accuracy ?? 'N/A')}%</b> Accuracy</div>
      <div><b>${t.questions_asked ?? '-'} / ${t.total_questions ?? '-'}</b> Questions</div>
      <div><b>${t.documentation_errors ?? 0}</b> Errors</div>
      <div><b>${t.critical_errors ?? 0}</b> Critical</div>
    </div>
    ${renderAccuracyBreakdown(t)}
  `;
}

function escHtml(value){
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

function renderFinalDecision(decision){
  const cats = [
    ['ASSIGNBACK', 'Assignback'],
    ['OPS_ATTENTION', 'Ops Attention'],
    ['FLAGS', 'Flags'],
    ['TECH_ISSUES', 'Tech Issues'],
  ];
  return cats.map(([key, label]) => {
    const items = Array.isArray(decision?.[key]) ? decision[key] : [];
    const body = items.length
      ? `<ul>${items.map((it) => {
          const details = it?.details;
          const detailText = details ? (typeof details === 'string' ? details : JSON.stringify(details)) : '';
          return `<li><b>${escHtml(it?.issue || '-')}</b>${detailText ? `<div class="subtext">${escHtml(detailText)}</div>` : ''}</li>`;
        }).join('')}</ul>`
      : '<div class="subtext">No issues in this category.</div>';
    return `<div class="decision-card"><h4>${label} <span class="pill">${items.length}</span></h4>${body}</div>`;
  }).join('');
}

function renderScoreBreakdown(score){
  const br = score?.breakdown || {};
  const rows = Object.keys(br).sort().map((key) => `
    <tr>
      <td>${escHtml(key.replaceAll('_', ' '))}</td>
      <td><b>${escHtml(br[key])}</b> / 100</td>
    </tr>
  `).join('');
  return `
    <table class="score-table">
      <thead><tr><th>Scoring Parameter</th><th>Score</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="2">No score breakdown available.</td></tr>'}</tbody>
    </table>
  `;
}

function renderAggregate(meta, report, recordDetails, qcScore){
  const el = document.getElementById('aggregate');
  if(!el) return;
  const top = meta.top || {};
  const decision = recordDetails?.final_decision || {};
  const score = qcScore || {};
  const decisionCategory = (decision.ASSIGNBACK || []).length ? 'Assignback'
    : (decision.OPS_ATTENTION || []).length ? 'Ops Attention'
    : (decision.TECH_ISSUES || []).length ? 'Tech Issues'
    : (decision.FLAGS || []).length ? 'Flags'
    : 'Pass';
  el.innerHTML = `
    <h2 style="margin:0 0 12px 0">Aggregated</h2>
    <div class="score-grid">
      <div class="score-tile"><div class="value">${escHtml(score.total_score ?? '-')} / ${escHtml(score.max_score ?? '-')}</div><div class="label">QC Score</div></div>
      <div class="score-tile"><div class="value">${escHtml(score.percentage ?? '-')}%</div><div class="label">QC Percentage</div></div>
      <div class="score-tile"><div class="value">${escHtml(score.category ?? '-')}</div><div class="label">QC Category</div></div>
      <div class="score-tile"><div class="value">${escHtml(decisionCategory)}</div><div class="label">Final Decision</div></div>
    </div>

    <h3 style="margin:18px 0 8px">Accuracy Calculation</h3>
    ${renderAccuracyBreakdown(top)}

    <h3 style="margin:18px 0 8px">QC Scoring Table</h3>
    ${renderScoreBreakdown(score)}

    <h3 style="margin:18px 0 8px">Final Decision Details</h3>
    <div class="decision-grid">${renderFinalDecision(decision)}</div>
  `;
}

function renderOverviewPart2(meta){
  const el = document.getElementById('qc2');
  if(!el) return;
  const t = meta.top || {};
  const header = `
    <div class="question-card">
      <div style="display:flex;gap:12px;flex-wrap:wrap;color:#374151;align-items:baseline">
        <div><b>${(t.accuracy ?? 'N/A')}%</b> Accuracy</div>
        <div><b>${t.questions_asked ?? '-'} / ${t.total_questions ?? '-'}</b> Questions</div>
        <div><b>${t.documentation_errors ?? 0}</b> Errors</div>
        <div><b>${t.critical_errors ?? 0}</b> Critical</div>
      </div>
    </div>`;
  el.insertAdjacentHTML('afterbegin', header);
}
