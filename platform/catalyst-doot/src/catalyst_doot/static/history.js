/* Actual earnings releases and deterministic moves read from accepted publications. */
(() => {
  'use strict';
  const escape = value => String(value ?? 'Not stated').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const source = value => {try {const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password?`<a class="text-link" href="${escape(u.href)}" target="_blank" rel="noopener noreferrer">SEC release evidence</a>`:'Source unavailable';}catch{return 'Source unavailable';}};
  const reason = value => escape((value || 'Inputs unavailable').replaceAll('_',' '));
  let view=null, controller=null, pending=false;
  function render(data,ticker) {
    const available=data.reactions.filter(r=>r.state==='AVAILABLE');
    if(!ticker)return `<h2>Actual earnings history coverage</h2><p class="reading-text">${data.events.length} source-backed historical earnings events across ${new Set(data.events.map(e=>e.cik)).size} issuers · ${available.length} calculated listing reactions.</p><p class="quiet">${escape(data.history_scope)}</p>`;
    const moves=new Map(data.reactions.map(r=>[r.historical_event_id,r]));
    const events=data.events.slice().sort((a,b)=>b.actual_release_date.localeCompare(a.actual_release_date));
    return `<h2>Past earnings and price moves · ${escape(ticker)}</h2><p class="reading-text">${events.length} actual releases · ${available.length} calculated price moves</p><p class="quiet">Adjusted daily closes. Unknown release sessions use the preceding-to-following-session bracket. These moves also include other market information.</p>${events.map(e=>{const r=moves.get(e.historical_event_id),p=e.source_assertions[0];return `<details class="record-details" data-historical-event="${escape(e.historical_event_id)}" open><summary>${escape(e.actual_release_date)} · ${escape(e.fiscal_period || 'Reporting period not stated')} · ${r?.state==='AVAILABLE'?`${escape(r.return_percent)}% adjusted-price move`:'Price move unavailable'}</summary><dl class="detail-list"><div><dt>Actual release day</dt><dd>${escape(e.actual_release_date)} · ${escape(e.session)}</dd></div><div><dt>Evidence support</dt><dd>${escape(e.support_state)} · ${escape(e.release_kind.replaceAll('_',' '))}</dd></div>${r?.state==='AVAILABLE'?`<div><dt>Measurement</dt><dd>${escape(r.window_kind.replaceAll('_',' '))}</dd></div><div><dt>Baseline close</dt><dd>${escape(r.baseline_date)} · USD ${escape(r.baseline_close)}</dd></div><div><dt>Measurement close</dt><dd>${escape(r.measurement_date)} · USD ${escape(r.measurement_close)}</dd></div><div><dt>Adjusted-price move</dt><dd data-reaction-value>${escape(r.return_percent)}%</dd></div>`:`<div><dt>Missing input</dt><dd>${reason(r?.reason_code)}</dd></div>`}<div><dt>Source observation</dt><dd>${escape(p.observed_at)}</dd></div></dl><blockquote class="reading-text">${escape(p.excerpt)}</blockquote><p>${source(p.source_url)}</p><small class="market-hash">Document SHA-256 ${escape(p.document_sha256)}</small></details>`;}).join('') || '<p class="quiet">No supported actual release statement was found in the retained native SEC history for this issuer.</p>'}<details class="record-details"><summary>Method and publication</summary><p class="quiet">BMO: preceding close to release-day close. AMC: release-day close to following close. Unknown session or closed release day: enclosing sessions. Return = (measurement close / baseline close − 1) × 100. Source clocks are preserved; computation time is separate.</p><dl class="detail-list"><div><dt>Method</dt><dd>${escape(data.method_version)}</dd></div><div><dt>History publication</dt><dd>${escape(data.history_publication_id)}</dd></div><div><dt>Market publication</dt><dd>${escape(data.market_publication_id)}</dd></div><div><dt>Computed</dt><dd>${escape(data.computed_at)}</dd></div></dl><p class="quiet">${escape(data.history_scope)}</p></details>`;
  }
  async function refresh(detail) {
    if(!['company','event','coverage'].includes(detail.route)||(detail.route!=='coverage'&&!detail.ticker)){controller?.abort();return;}
    const main=document.getElementById('main');let panel=main.querySelector('[data-earnings-history-panel]');
    if(!panel){panel=document.createElement('section');panel.className='panel market-context';panel.dataset.earningsHistoryPanel='true';main.append(panel);}
    const binding=`${detail.publication_id}:${detail.ticker || ''}`;
    if(pending&&panel.dataset.binding===binding)return;
    panel.innerHTML='<h2>Past earnings and price moves</h2><p class="quiet">Reading accepted actual-release evidence…</p>';
    controller?.abort();controller=new AbortController();const current=controller;
    pending=true;panel.dataset.binding=binding;
    const timeout=setTimeout(()=>current.abort(),120000);
    try {
      const params=new URLSearchParams({publication_id:detail.publication_id});if(detail.ticker)params.set('ticker',detail.ticker);
      const response=await fetch(`/v1/earnings/history?${params}`,{cache:'no-store',signal:current.signal});
      if(!response.ok)throw new Error('Actual-release history could not be verified for this snapshot.');
      const result=await response.json();
      if(!panel.isConnected||current!==controller||panel.dataset.binding!==binding)return;
      if(result.contract_version!=='earnings-history-0.1.0'||result.calendar_publication_id!==detail.publication_id)throw new Error('Historical publication binding could not be verified.');
      if(!result.is_current||!result.data)throw new Error(result.reason || 'No current actual-release history publication is available.');
      const data=result.data;if(!['events','reactions','coverage'].every(k=>Array.isArray(data[k])))throw new Error('Historical response is incomplete.');
      panel.innerHTML=render(data,detail.ticker);
      main.querySelector('[data-reaction-placeholder]')?.remove();
      main.querySelector('[data-reaction-availability]')?.replaceChildren(document.createTextNode(`${data.reactions.filter(r=>r.state==='AVAILABLE').length} calculated listing reactions`));
    } catch(error){if(panel.isConnected&&current===controller&&panel.dataset.binding===binding){panel.innerHTML=`<h2>Past earnings and price moves unavailable</h2><p class="reading-text">${escape(error.name==='AbortError'?'Evidence read timed out.':error.message)}</p>`;main.querySelector('[data-reaction-availability]')?.replaceChildren(document.createTextNode('Unavailable for this snapshot'));}}
    finally{clearTimeout(timeout);if(current===controller)pending=false;}
  }
  document.addEventListener('wtf:market-view',event=>{view=event.detail;void refresh(view);});
  setInterval(()=>{if(view&&!document.hidden)void refresh(view);},60000);
  document.addEventListener('visibilitychange',()=>{if(view&&!document.hidden)void refresh(view);});
})();
