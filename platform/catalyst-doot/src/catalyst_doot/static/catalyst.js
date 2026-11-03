/* Catalyst Doot: accepted SEC earnings only. No source fetches, model calls, or collection controls. */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const paths = {
    home:'M3 10 12 3l9 7M5 9v12h5v-7h4v7h5V9', calendar:'M8 2v4m8-4v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14H3V6a2 2 0 0 1 2-2Z',
    database:'M20 6c0 2-4 4-8 4S4 8 4 6s4-4 8-4 8 2 8 4Zm0 0v12c0 2-4 4-8 4s-8-2-8-4V6m0 6c0 2 4 4 8 4s8-2 8-4',
    settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm-2-5h4l1 3 3 1 3-1 2 4-2 2v3l2 2-2 4-3-1-3 1-1 3h-4l-1-3-3-1-3 1-2-4 2-2v-3l-2-2 2-4 3 1 3-1Z',
    search:'M21 21l-5-5M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14Z', globe:'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20ZM2 12h20M12 2c6 5 6 15 0 20-6-5-6-15 0-20Z',
    moon:'M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11Z', sun:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM12 2v2m0 16v2M2 12h2m16 0h2M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2',
    menu:'M4 6h16M4 12h16M4 18h16', arrow:'M4 12h16m-6-6 6 6-6 6', chevron:'M9 5l7 7-7 7', left:'M15 5l-7 7 7 7', close:'M6 6l12 12M18 6 6 18',
    external:'M14 3h7v7m0-7L10 14M10 3H3v18h18v-7', clock:'M12 8v5l3 2M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Z',
    document:'M14 2H5v20h14V7l-5-5Zm0 0v6h5M8 12h8M8 16h8', check:'M8 12l3 3 5-6M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Z',
    info:'M12 11v6m0-10v.1M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Z', chart:'M4 20V10m8 10V4m8 16v-8', phone:'M8 3 4 5c-1 5 9 15 14 14l2-4-5-3-2 2-3-3 2-2-4-6Z',
    shield:'M12 2 3 6v6c0 5 5 8 9 10 4-2 9-5 9-10V6l-9-4Zm-5 10 3 3 7-7', filter:'M3 5h18l-7 8v7l-4-2v-5L3 5Z', copy:'M8 8h13v13H8ZM3 16V3h13',
  };
  const icon = name => `<svg aria-hidden="true" viewBox="0 0 24 24"><path d="${paths[name] || paths.info}"/></svg>`;
  function decorate(root = document) { root.querySelectorAll('[data-icon]').forEach(el => { el.classList.add('icon'); el.innerHTML = icon(el.dataset.icon); }); }
  const today = () => localDate(new Date());
  function localDate(date) { return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`; }
  const parseDate = date => new Date(`${date}T12:00:00`);
  function addDays(date, days) { const result = parseDate(date); result.setDate(result.getDate()+days); return localDate(result); }
  function prettyDate(date, options = {}) { return date ? parseDate(date).toLocaleDateString('en-GB', {day:'numeric',month:'short',...options}) : 'No supported date'; }
  function sourceClock(value, sourceZone) {
    if (!value) return 'Not stated';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Not stated';
    const timeZone = prefs.timezone === 'source' ? (sourceZone || 'UTC') : prefs.timezone;
    try { return new Intl.DateTimeFormat('en-GB',{dateStyle:'medium',timeStyle:'short',timeZone}).format(date) + ` · ${timeZone}`; }
    catch { return date.toISOString().replace('T',' ').replace('.000Z',' UTC'); }
  }
  const sessionLabel = value => ({BMO:'Before market open',AMC:'After market close',DURING_MARKET:'During market hours',UNKNOWN:'Time not stated'}[value] || 'Time not stated');
  const sessionIcon = value => value === 'BMO' ? 'sun' : value === 'AMC' ? 'moon' : 'clock';
  const isEstimate = row => row.event.date_basis === 'estimated';
  const countAnnounced = rows => rows.filter(row => row.event.date_basis === 'announced').length;
  const eventURL = row => `/events/${encodeURIComponent(row.event.event_id)}`;
  const companyURL = ticker => `/companies/${encodeURIComponent(ticker || '')}`;
  function publicURL(value) { try { const url = new URL(value); return ['https:','http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null; } catch { return null; } }
  function sourceLink(value, label = 'View source', cls = 'text-link') { const url = publicURL(value); return url ? `<a class="${cls}" href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(label)} ${icon('external')}</a>` : '<span class="quiet">Source unavailable</span>'; }
  function badge(row) { const e = row.event; const label = e.status === 'cancelled' ? 'Cancelled' : e.status === 'moved' ? 'Moved · announced' : isEstimate(row) ? 'Estimated' : e.date_basis === 'announced' ? 'Announced' : e.status; return `<span class="badge ${isEstimate(row)?'estimated':e.status==='cancelled'?'cancelled':e.date_basis==='announced'?'':'unknown'}">${escape(label)}</span>`; }
  function avatar(ticker, large = false) { return `<span class="avatar ${large?'large':''}" aria-hidden="true">${escape(ticker || 'SEC')}</span>`; }
  const storage = {get(key){try{return localStorage.getItem(key);}catch{return null;}},set(key,value){try{localStorage.setItem(key,value);}catch{}}};
  let prefs;
  try { prefs = {...{theme:'dark',timezone:'source',density:'comfortable',motion:'system'},...JSON.parse(storage.get('catalyst.preferences') || '{}')}; } catch { prefs = {theme:'dark',timezone:'source',density:'comfortable',motion:'system'}; }
  const allowedPrefs = {theme:['dark','light','system'],timezone:['source','UTC','Asia/Karachi','America/New_York'],density:['comfortable','compact'],motion:['system','reduced']};
  for (const key of Object.keys(allowedPrefs)) if (!allowedPrefs[key].includes(prefs[key])) prefs[key] = allowedPrefs[key][0];
  function applyPrefs() {
    document.documentElement.dataset.theme = prefs.theme === 'system' ? (matchMedia('(prefers-color-scheme: light)').matches?'light':'dark') : prefs.theme;
    document.documentElement.dataset.density = prefs.density;
    document.documentElement.dataset.motion = prefs.motion;
    $('#zone-label').textContent = prefs.timezone === 'source' ? 'Source dates' : prefs.timezone;
    const light = document.documentElement.dataset.theme === 'light';
    $('#theme-toggle').innerHTML = icon(light?'moon':'sun');
    $('#theme-toggle').setAttribute('aria-label',`Switch to ${light?'dark':'light'} theme`);
    storage.set('catalyst.preferences',JSON.stringify(prefs));
    configureGlow();
  }
  const state = {feed:null,rows:[],pendingFeed:null,error:null,window:7,anchor:today(),miniMonth:today().slice(0,7),view:'agenda',filter:'',basis:'all',session:'all',page:0,tab:'summary',evidence:new Map(),history:new Map(),loading:false};
  const finePointer=matchMedia('(hover: hover) and (pointer: fine)');
  const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)');
  let glowEnabled=false,glowFrame=0,spotlit=null,pointer={x:0,y:0,target:null};
  function configureGlow() {
    glowEnabled=finePointer.matches&&prefs.motion!=='reduced'&&!(prefs.motion==='system'&&reducedMotion.matches);
    document.documentElement.dataset.glow=glowEnabled?'enabled':'disabled';
    if(!glowEnabled){$('#ambient-light').classList.remove('active');if(spotlit)spotlit.removeAttribute('data-spotlight');spotlit=null;}
  }
  document.addEventListener('pointermove',event=>{
    if(!glowEnabled||event.pointerType==='touch')return;
    pointer={x:event.clientX,y:event.clientY,target:event.target};
    if(glowFrame)return;
    glowFrame=requestAnimationFrame(()=>{
      glowFrame=0;if(!glowEnabled)return;
      const ambient=$('#ambient-light');ambient.style.setProperty('--pointer-x',`${pointer.x}px`);ambient.style.setProperty('--pointer-y',`${pointer.y}px`);ambient.classList.add('active');
      const panel=pointer.target.closest('.panel,.preview-dialog');
      if(spotlit!==panel){if(spotlit)spotlit.removeAttribute('data-spotlight');spotlit=panel;}
      if(panel){const box=panel.getBoundingClientRect();panel.style.setProperty('--spot-x',`${pointer.x-box.left}px`);panel.style.setProperty('--spot-y',`${pointer.y-box.top}px`);panel.setAttribute('data-spotlight','');}
    });
  },{passive:true});
  document.documentElement.addEventListener('pointerleave',()=>{$('#ambient-light').classList.remove('active');if(spotlit)spotlit.removeAttribute('data-spotlight');spotlit=null;});
  finePointer.addEventListener('change',configureGlow);reducedMotion.addEventListener('change',configureGlow);
  let toastTimer;
  function toast(message) { $('#toast').textContent=message;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('#toast').hidden=true;},4500); }
  function route() { const path=location.pathname; return path.startsWith('/events/')?'event':path.startsWith('/companies/')?'company':path==='/earnings'?'calendar':path==='/coverage'?'coverage':path==='/preferences'?'preferences':'overview'; }
  function updateNav() { const selected = ['event','company'].includes(route())?'calendar':route(); document.querySelectorAll('[data-route]').forEach(a=>{const active=a.dataset.route===selected;a.classList.toggle('active',active);active?a.setAttribute('aria-current','page'):a.removeAttribute('aria-current');}); }
  function manageNavigation() {const mobile=matchMedia('(max-width: 760px)').matches;if(!mobile){document.body.classList.remove('nav-open');$('#menu-toggle').setAttribute('aria-expanded','false');}$('#sidebar').inert=mobile&&!document.body.classList.contains('nav-open');}
  function navigate(url, push=true) {
    if (push) history.pushState({},'',url);
    if(state.pendingFeed&&route()!=='event'){acceptFeed(state.pendingFeed);toast('Updated to the latest accepted earnings.');}
    const params=new URLSearchParams(location.search);
    state.filter=params.get('ticker') || params.get('q') || '';
    state.basis=['announced','estimated'].includes(params.get('basis'))?params.get('basis'):'all';
    state.session=['BMO','AMC','DURING_MARKET','UNKNOWN'].includes(params.get('session'))?params.get('session'):'all';
    state.anchor=/^\d{4}-\d{2}-\d{2}$/.test(params.get('date') || '') && !Number.isNaN(parseDate(params.get('date')).getTime()) ? params.get('date') : today();
    state.view=['agenda','week','month'].includes(params.get('view'))?params.get('view'):'agenda';
    state.tab='summary';state.page=0;
    if($('#event-preview').open)$('#event-preview').close();
    $('#search-results').hidden=true;$('#global-search').value='';$('#global-search').setAttribute('aria-expanded','false');
    document.body.classList.remove('nav-open');$('#menu-toggle').setAttribute('aria-expanded','false');
    manageNavigation();
    render();window.scrollTo(0,0);
    $('#main').focus({preventScroll:true});
  }
  function calendarQuery() { const params=new URLSearchParams(); if(state.filter)params.set('q',state.filter);if(state.basis!=='all')params.set('basis',state.basis);if(state.session!=='all')params.set('session',state.session);if(state.view!=='agenda')params.set('view',state.view);if(state.anchor!==today())params.set('date',state.anchor); const query=params.toString();return `/earnings${query?'?'+query:''}`; }
  async function getJSON(url) {
    const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),60000);
    try{const response=await fetch(url,{cache:'no-store',signal:controller.signal,headers:{Accept:'application/json'}});let data;try{data=await response.json();}catch{throw new Error('The data service returned an unreadable response.');}if(!response.ok)throw new Error(data.detail || 'The accepted data could not be read.');return data;}
    finally{clearTimeout(timeout);}
  }
  function validateFeed(feed) {
    if(!Array.isArray(feed.data)||!Array.isArray(feed.missing_issuers)||feed.pagination?.total!==feed.data.length||typeof feed.publication_id!=='string'||typeof feed.is_current!=='boolean')throw new Error('The accepted snapshot is incomplete. Reload after the data service recovers.');
  }
  function acceptFeed(feed) {
    validateFeed(feed);state.feed=feed;state.pendingFeed=null;
    state.rows=feed.data.filter(row=>row.event.event_type==='earnings_release').sort((a,b)=>(a.event.event_date || '9999').localeCompare(b.event.event_date || '9999') || (a.event.ticker || '').localeCompare(b.event.ticker || ''));
    state.evidence.clear();state.history.clear();
  }
  function renderPreservingContext() {
    const active=document.activeElement,id=active?.id,selection=active?.selectionStart,scroll={x:scrollX,y:scrollY};render();
    if(id){const next=document.getElementById(id);if(next){next.focus({preventScroll:true});if(typeof selection==='number')try{next.setSelectionRange(selection,selection);}catch{}}}
    window.scrollTo(scroll.x,scroll.y);if($('#global-search').value.trim())updateSearch();
  }
  async function loadFeed() {
    if(state.loading)return;state.loading=true;state.error=null;render();
    try { const feed=await getJSON('/v1/earnings?retained=true&limit=1000');
      acceptFeed(feed);
    } catch(error) {state.error=error.message;state.feed=null;state.rows=[];}
    finally{state.loading=false;render();if($('#global-search').value.trim())updateSearch();}
  }
  // Read only atomically accepted publications. Pin an open event, preview or active input.
  let trustReading=false;
  async function checkSnapshotTrust() {
    if(document.hidden||trustReading||state.loading)return;
    if(!state.feed){loadFeed();return;}
    trustReading=true;
    try {
      const latest=await getJSON('/v1/earnings?retained=true&limit=1000');
      validateFeed(latest);
      const changed=latest.publication_id!==state.feed.publication_id;
      const pinned=route()==='event'||$('#event-preview').open||document.activeElement.matches('input,textarea,select')||document.body.classList.contains('nav-open');
      if(changed&&!pinned){acceptFeed(latest);renderPreservingContext();toast('Updated to the latest accepted earnings.');}
      else if(changed){state.pendingFeed=latest;if(state.feed.calendar_state!=='NEW_PUBLICATION_AVAILABLE'){state.feed.is_current=false;state.feed.calendar_state='NEW_PUBLICATION_AVAILABLE';renderPreservingContext();toast('New earnings data is ready. Your open event stays on its selected snapshot.');}}
      else if(latest.is_current!==state.feed.is_current||latest.calendar_state!==state.feed.calendar_state){state.feed.is_current=latest.is_current;state.feed.calendar_state=latest.calendar_state;state.pendingFeed=null;renderPreservingContext();if(!latest.is_current)toast('Current data trust changed. Showing the retained snapshot.');}
    } catch {if(state.feed.calendar_state!=='READ_UNAVAILABLE'){state.feed.is_current=false;state.feed.calendar_state='READ_UNAVAILABLE';renderPreservingContext();}}
    finally{trustReading=false;}
  }
  function trustStrip() {
    if(!state.feed)return '';
    const feed=state.feed;const fixture=document.body.dataset.evidenceMode!=='live';
    const status=fixture?'Fixture evidence':feed.is_current?'Accepted snapshot current':'Retained snapshot';
    const pending=feed.calendar_state==='NEW_PUBLICATION_AVAILABLE';
    return `<div class="trust-strip"><span class="trust-status ${fixture?'fixture-note':''}">${icon(feed.is_current?'shield':'info')} ${status}${feed.is_current?'<span class="auto-update-label">Auto updates</span>':''}</span><span>Published ${escape(sourceClock(feed.publication_generated_at,'UTC'))} · Source dates stay on their original day</span></div>${!feed.is_current?`<div class="trust-banner">${icon('info')}<div><strong>Retained data · ${escape(feed.calendar_state)}</strong><p>${pending?'New accepted earnings are ready. This view keeps its selected snapshot until you reload or return to the calendar.':'This snapshot is available for reference. Its counts and dates are not presented as current. Reload after a successful publication.'}</p></div><button class="button compact" data-action="reload">${pending?'View latest':'Reload'}</button></div>`:''}`;
  }
  function heading(title, subtitle, actions='') { return `<div class="page-heading"><div><div class="eyebrow">SEC earnings workspace</div><h1>${escape(title)}</h1><p>${escape(subtitle)}</p></div>${actions?`<div class="heading-actions">${actions}</div>`:''}</div>`; }
  function empty(title, detail, action='') { return `<div class="empty-state">${icon('calendar')}<h2>${escape(title)}</h2><p>${escape(detail)}</p>${action}</div>`; }
  function kpi(label,value,detail,type='calendar',amber=false) { return `<div class="panel kpi"><div class="kpi-top"><span>${escape(label)}</span><span class="kpi-icon ${amber?'amber':''}">${icon(type)}</span></div><div class="kpi-value">${escape(value)}</div><small>${escape(detail)}</small></div>`; }
  const activeRows = () => state.rows.filter(row=>row.event.status!=='cancelled' && row.event.event_date);
  function matches(row,query) {const text=[row.event.company_name,row.event.ticker,...row.listings.map(l=>l.ticker)].join(' ').toLowerCase();return !query || text.includes(query.toLowerCase().trim());}
  function unavailable() { return `<div class="unavailable-grid"><div class="unavailable-card">${icon('document')}<h3>Research brief unavailable</h3><p>Source-backed calendar facts are available. A validated narrative brief has not been published.</p></div><div class="unavailable-card" data-reaction-placeholder>${icon('chart')}<h3>Historical reaction unavailable</h3><p>Qualified price data and actual historical earnings releases are required before a reaction can be shown.</p></div></div>`; }
  function overview() {
    const current=state.feed.is_current;const start=today(),end=addDays(start,state.window-1);
    const rows=activeRows().filter(row=>row.event.event_date>=start&&row.event.event_date<=end);
    const nearest=activeRows().find(row=>row.event.event_date>=start);const countLabel=`Next ${state.window} days`;
    const options=[7,30,90].map(n=>`<option value="${n}" ${state.window===n?'selected':''}>Next ${n} days</option>`).join('');
    const actions=`<label class="filter-field"><span class="sr-only">Overview date window</span><select id="overview-window" aria-label="Overview date window">${options}</select></label><a class="button primary" data-nav href="/earnings">${icon('calendar')} Open calendar</a>`;
    const feature=nearest?`<section class="panel feature-card"><div class="feature-orbit"></div><div class="eyebrow">${current?'Next upcoming earnings':'Next event in retained snapshot'}</div><div class="feature-title">${avatar(nearest.event.ticker,true)}<div><h2>${escape(nearest.event.company_name || nearest.event.ticker)}</h2><small class="quiet">${escape(nearest.event.ticker)} · ${escape(nearest.event.exchange || 'Exchange not stated')}</small></div></div><div class="feature-bottom"><div><span class="feature-date">${escape(prettyDate(nearest.event.event_date))}</span>${badge(nearest)}<div class="meta-row"><span>${icon(sessionIcon(nearest.event.session))}${sessionLabel(nearest.event.session)}</span><span>${icon('document')}${escape(nearest.event.fiscal_period || 'Period not stated')}</span></div></div><a class="button primary" data-nav href="${eventURL(nearest)}">Open event ${icon('arrow')}</a></div>${isEstimate(nearest)?'<p class="quiet" style="font-size:11px;margin-top:16px">Low-confidence filing-date projection · not company-announced</p>':''}</section>`:empty('No upcoming supported date','Search a company to see its coverage and accepted records.');
    return `${heading('Your earnings overview','Upcoming earnings, with the evidence behind them.',actions)}${trustStrip()}<div class="kpi-grid">${kpi('Events in view',current?rows.length:'—',current?countLabel:'Current counts unavailable')}${kpi('Announced',current?countAnnounced(rows):'—',countLabel,'check')}${kpi('Estimated',current?rows.filter(isEstimate).length:'—',countLabel,'clock',true)}${kpi('No supported date',current?state.feed.missing_issuers.length:'—','Across full company scope','document')}</div><div class="dashboard-grid"><div class="stack">${feature}<section class="panel"><div class="panel-head"><div><h2>Earnings in view</h2><p>${escape(prettyDate(start))}–${escape(prettyDate(end,{year:'numeric'}))}</p></div><a class="text-link" href="/earnings" data-nav>View all ${icon('arrow')}</a></div>${rows.length?agendaTable(rows.slice(0,6)):empty('No earnings in this window','Try the next 30 or 90 days.')}<div class="table-foot"><span>${rows.length} accepted events · ${countLabel.toLowerCase()}</span><span>SEC evidence</span></div></section></div><div class="stack"><section class="panel">${miniCalendar()}</section><section class="panel"><div class="panel-head"><div><h2>Earnings events by day</h2><p>Accepted event counts · next 7 days</p></div></div>${countChart(start)}</section></div></div><section class="panel info-strip">${icon('shield')}<div><h3>Evidence behind every date</h3><p>Open a source for an announced date, or inspect the historical filing basis behind an estimate.</p></div><a class="text-link" data-nav href="/coverage">Learn about our data ${icon('arrow')}</a></section>`;
  }
  function agendaTable(rows) {return `<div class="agenda-wrap"><table class="agenda"><thead><tr><th>Company</th><th>Date</th><th class="date-basis-col">Date basis</th><th class="timing-col">Release timing</th><th class="period-col">Period</th><th><span class="sr-only">Preview</span></th></tr></thead><tbody>${rows.map(row=>`<tr><td><div class="company-cell"><span class="company-avatar">${avatar(row.event.ticker)}</span><div><a data-nav href="${companyURL(row.event.ticker)}">${escape(row.event.company_name || row.event.ticker)}</a><small>${escape(row.event.ticker)} · ${escape(row.event.exchange || 'Exchange not stated')}</small></div></div></td><td class="number">${escape(prettyDate(row.event.event_date))}<span class="date-mobile-badge">${badge(row)}</span></td><td class="date-basis-col">${badge(row)}</td><td class="timing-col">${sessionLabel(row.event.session)}</td><td class="period-col">${escape(row.event.fiscal_period || 'Not stated')}</td><td><button class="row-open" data-preview="${escape(row.event.event_id)}" aria-label="Preview ${escape(row.event.ticker)} earnings">${icon('chevron')}</button></td></tr>`).join('')}</tbody></table></div>`;}
  function miniCalendar() {
    const [year,month]=state.miniMonth.split('-').map(Number);const first=new Date(year,month-1,1,12);const offset=(first.getDay()+6)%7;const start=new Date(year,month-1,1-offset,12);const windowEnd=addDays(today(),state.window-1);
    return `<div class="mini-month-head"><h2>${first.toLocaleDateString('en-GB',{month:'long',year:'numeric'})}</h2><div class="button-row"><button class="icon-button" data-action="mini-prev" aria-label="Previous month">${icon('left')}</button><button class="icon-button" data-action="mini-next" aria-label="Next month">${icon('chevron')}</button></div></div><div class="mini-calendar">${['M','T','W','T','F','S','S'].map(d=>`<span class="weekday">${d}</span>`).join('')}${Array.from({length:42},(_,i)=>{const date=new Date(start);date.setDate(start.getDate()+i);const iso=localDate(date);const rows=activeRows().filter(r=>r.event.event_date===iso);return `<button class="day-button ${date.getMonth()!==month-1?'off-month':''} ${iso===today()?'today':''} ${iso>=today()&&iso<=windowEnd?'in-range':''}" data-date="${iso}" aria-label="${escape(prettyDate(iso,{month:'long',year:'numeric'}))}, ${rows.length} earnings">${date.getDate()}<span class="day-dots">${rows.some(r=>!isEstimate(r))?'<i></i>':''}${rows.some(isEstimate)?'<i class="estimated"></i>':''}</span></button>`;}).join('')}</div>`;
  }
  function countChart(start) {
    if(!state.feed.is_current)return empty('Current counts unavailable','This is a retained snapshot.');
    const days=Array.from({length:7},(_,i)=>{const date=addDays(start,i);const rows=activeRows().filter(r=>r.event.event_date===date);return {date,announced:countAnnounced(rows),estimated:rows.filter(isEstimate).length};});
    const max=Math.max(1,...days.map(d=>d.announced+d.estimated)),ceiling=max<=2?max:Math.ceil(max/2)*2,total=days.reduce((sum,d)=>sum+d.announced+d.estimated,0);
    const peak=days.find(d=>d.announced+d.estimated===max),base=155,height=120;
    const grid=[...new Set([0,Math.ceil(ceiling/2),ceiling])].map(n=>{const y=base-n/ceiling*height;return `<line class="chart-gridline" x1="28" y1="${y}" x2="350" y2="${y}"/><text class="chart-axis" x="18" y="${y+3}" text-anchor="end">${n}</text>`;}).join('');
    const columns=days.map((d,i)=>{
      const x=39+i*46,announcedHeight=d.announced/ceiling*height,estimatedHeight=d.estimated/ceiling*height,sum=d.announced+d.estimated;
      const detail=`${prettyDate(d.date,{weekday:'short'})} · ${d.announced} announced · ${d.estimated} estimated`;
      return `<g class="chart-day" tabindex="0" role="img" aria-label="${escape(detail)}" data-chart-detail="${escape(detail)}"><title>${escape(detail)}</title><rect class="chart-hit" x="${x-10}" y="23" width="44" height="136" rx="8"/>${d.announced?`<rect class="chart-announced" x="${x}" y="${base-announcedHeight}" width="24" height="${announcedHeight}" rx="5" fill="url(#chart-mint)"/>`:''}${d.estimated?`<rect class="chart-estimated" x="${x}" y="${base-announcedHeight-estimatedHeight}" width="24" height="${estimatedHeight}" rx="5" fill="url(#chart-gold)"/><rect class="chart-hatching" x="${x}" y="${base-announcedHeight-estimatedHeight}" width="24" height="${estimatedHeight}" rx="5" fill="url(#chart-stripes)"/>`:''}${!sum?`<circle class="chart-zero" cx="${x+12}" cy="${base}" r="2"/>`:''}<text class="chart-total" x="${x+12}" y="${base-announcedHeight-estimatedHeight-9}" text-anchor="middle">${sum}</text><text class="chart-day-label" x="${x+12}" y="177" text-anchor="middle">${prettyDate(d.date,{weekday:'short',day:undefined,month:undefined})}</text><text class="chart-date-label" x="${x+12}" y="193" text-anchor="middle">${prettyDate(d.date)}</text></g>`;
    }).join('');
    return `<div class="chart-highlights"><span><strong>${total}</strong> scheduled</span><span>${peak?`Peak <strong>${max}</strong> · ${prettyDate(peak.date)}`:'No events in this window'}</span></div><svg class="count-chart-svg" viewBox="0 0 360 205" aria-labelledby="chart-title chart-description"><title id="chart-title">Earnings events by day</title><desc id="chart-description">Exact announced and estimated event counts for the next seven days. Each day is keyboard-focusable; a data table follows.</desc><defs><linearGradient id="chart-mint" x2="0" y2="1"><stop stop-color="var(--chart-mint)"/><stop offset="1" stop-color="var(--chart-mint-deep)"/></linearGradient><linearGradient id="chart-gold" x2="0" y2="1"><stop stop-color="var(--chart-gold)"/><stop offset="1" stop-color="var(--chart-gold-deep)"/></linearGradient><pattern id="chart-stripes" width="7" height="7" patternUnits="userSpaceOnUse"><path d="M-2 2 2-2M0 7 7 0M5 9 9 5" class="chart-stripe"/></pattern></defs>${grid}${columns}</svg><div class="chart-detail" aria-live="polite">Hover or focus a day to explore its counts</div><div class="legend"><span><i></i>Announced</span><span><i class="estimated"></i>Estimated</span></div><details class="record-details"><summary>View chart data</summary><table class="agenda"><caption class="sr-only">Earnings counts for the next seven days</caption><thead><tr><th>Date</th><th>Announced</th><th>Estimated</th></tr></thead><tbody>${days.map(d=>`<tr><td>${prettyDate(d.date)}</td><td>${d.announced}</td><td>${d.estimated}</td></tr>`).join('')}</tbody></table></details>`;
  }
  function calendarBounds() {
    if(state.view==='week'){const date=parseDate(state.anchor);const offset=(date.getDay()+6)%7;const start=addDays(state.anchor,-offset);return [start,addDays(start,6)];}
    if(state.view==='month'){const date=parseDate(state.anchor);return [localDate(new Date(date.getFullYear(),date.getMonth(),1,12)),localDate(new Date(date.getFullYear(),date.getMonth()+1,0,12))];}
    return [state.anchor,addDays(state.anchor,89)];
  }
  function filteredCalendarRows() { const [start,end]=calendarBounds();return activeRows().filter(row=>row.event.event_date>=start&&row.event.event_date<=end&&matches(row,state.filter)&&(state.basis==='all'||row.event.date_basis===state.basis)&&(state.session==='all'||row.event.session===state.session)); }
  function calendar() {
    const [start,end]=calendarBounds();const rows=filteredCalendarRows();const title=state.view==='month'?prettyDate(start,{day:undefined,month:'long',year:'numeric'}):`${prettyDate(start)}–${prettyDate(end,{year:'numeric'})}`;
    const basisOptions=[['all','All date bases'],['announced','Announced only'],['estimated','Estimated only']].map(([value,label])=>`<option value="${value}" ${state.basis===value?'selected':''}>${label}</option>`).join('');
    const sessionOptions=[['all','All sessions'],['BMO','Before market open'],['AMC','After market close'],['DURING_MARKET','During market hours'],['UNKNOWN','Time not stated']].map(([value,label])=>`<option value="${value}" ${state.session===value?'selected':''}>${label}</option>`).join('');
    return `${heading('Earnings calendar','Dates, reporting periods, and the evidence behind them.')} ${trustStrip()}<div class="toolbar"><div class="date-controls"><button class="button compact" data-action="calendar-today">Today</button><button class="icon-button" data-action="calendar-prev" aria-label="Previous ${state.view==='month'?'month':state.view==='week'?'week':'90 days'}">${icon('left')}</button><button class="icon-button" data-action="calendar-next" aria-label="Next ${state.view==='month'?'month':state.view==='week'?'week':'90 days'}">${icon('chevron')}</button><strong>${escape(title)}</strong></div><div class="segmented" aria-label="Calendar view">${['agenda','week','month'].map(view=>`<button data-view="${view}" aria-pressed="${state.view===view}">${view[0].toUpperCase()+view.slice(1)}</button>`).join('')}</div></div><div class="filters"><div class="filter-field"><label for="company-filter">Company or ticker</label><input id="company-filter" type="search" placeholder="Search accepted companies" value="${escape(state.filter)}"></div><div class="filter-field"><label for="basis-filter">Date basis</label><select id="basis-filter">${basisOptions}</select></div><div class="filter-field"><label for="session-filter">Market session</label><select id="session-filter">${sessionOptions}</select></div><button class="button compact" data-action="clear-filters">Reset filters</button><span class="filter-count" aria-live="polite">${rows.length} ${state.feed.is_current?'accepted':'retained'} events</span></div>${state.view==='agenda'?calendarAgenda(rows):state.view==='week'?weekCalendar(rows,start):monthCalendar(rows,start)}<p class="calendar-note">${icon('info')} Estimated dates are low-confidence historical filing projections, not company announcements. No exact clock is inferred.</p>`;
  }
  function calendarAgenda(rows) {
    if(!rows.length)return `<section class="panel">${empty('No matching earnings','Try another date window or reset the company, date basis, and session filters.', '<button class="button" data-action="clear-filters">Reset filters</button>')}</section>`;
    const perPage=matchMedia('(max-width: 760px)').matches?12:30;const maxPage=Math.max(0,Math.ceil(rows.length/perPage)-1);state.page=Math.min(state.page,maxPage);const slice=rows.slice(state.page*perPage,(state.page+1)*perPage);let date='';
    const content=slice.map(row=>{let header='';if(row.event.event_date!==date){date=row.event.event_date;header=`<div class="agenda-date-header">${escape(prettyDate(date,{weekday:'long',month:'long'}))}</div>`;}return `${header}<div class="agenda-item">${avatar(row.event.ticker)}<div><a class="company-name" href="${companyURL(row.event.ticker)}" data-nav><strong>${escape(row.event.company_name || row.event.ticker)}</strong></a><small>${escape(row.event.ticker)} · ${escape(row.event.exchange || 'Exchange not stated')}</small></div><div class="basis">${badge(row)}</div><div class="timing">${sessionLabel(row.event.session)}</div><div class="period">${escape(row.event.fiscal_period || 'Period not stated')}</div><button class="row-open" data-preview="${escape(row.event.event_id)}" aria-label="Preview ${escape(row.event.ticker)} earnings">${icon('chevron')}</button></div>`;}).join('');
    return `<section class="panel">${content}<div class="pagination"><span>Showing ${state.page*perPage+1}–${Math.min(rows.length,(state.page+1)*perPage)} of ${rows.length}</span><div class="button-row"><button class="button compact" data-action="page-prev" ${state.page===0?'disabled':''}>Previous</button><button class="button compact" data-action="page-next" ${state.page===maxPage?'disabled':''}>Next</button></div></div></section>`;
  }
  function weekCalendar(rows,start) {return `<div class="calendar-scroll"><div class="week-grid">${Array.from({length:7},(_,i)=>{const date=addDays(start,i);const events=rows.filter(r=>r.event.event_date===date);return `<section class="week-day ${date===today()?'is-today':''} ${!events.length?'empty-day':''}" aria-label="${escape(prettyDate(date,{weekday:'long',month:'long'}))}"><div class="week-header">${prettyDate(date,{weekday:'short',day:undefined,month:undefined})}<strong>${prettyDate(date,{month:'long'})}</strong></div>${events.map(calendarCard).join('')}${!events.length?'<small class="quiet">No events</small>':''}</section>`;}).join('')}</div></div>${!rows.length?'<div class="panel section-gap">'+empty('No matching earnings this week','Try another week or reset your filters.')+'</div>':''}`;}
  function calendarCard(row) {return `<button class="calendar-event ${isEstimate(row)?'estimated':''}" data-preview="${escape(row.event.event_id)}" aria-label="Preview ${escape(row.event.ticker)} on ${escape(prettyDate(row.event.event_date))}">${badge(row)}<strong>${escape(row.event.ticker)}</strong><small class="event-name">${escape(row.event.company_name)}</small><small>${sessionLabel(row.event.session)}</small><small>${escape(row.event.fiscal_period || 'Period not stated')}</small></button>`;}
  function monthCalendar(rows,first) {
    const date=parseDate(first);const offset=(date.getDay()+6)%7;const start=addDays(first,-offset);const last=new Date(date.getFullYear(),date.getMonth()+1,0,12);const cells=Math.ceil((offset+last.getDate())/7)*7;
    return `<div class="calendar-scroll"><div class="month-grid">${['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'].map(d=>`<span class="month-weekday">${d}</span>`).join('')}${Array.from({length:cells},(_,i)=>{const iso=addDays(start,i);const events=rows.filter(r=>r.event.event_date===iso);return `<section class="month-cell ${iso.slice(0,7)!==first.slice(0,7)?'off-month':''} ${!events.length?'empty-day':''} ${iso===today()?'is-today':''}"><strong><span class="desktop-day">${parseDate(iso).getDate()}</span><span class="mobile-month-date">${prettyDate(iso,{weekday:'long',month:'long'})}</span></strong>${events.slice(0,3).map(row=>`<button class="month-event ${isEstimate(row)?'estimated':''}" data-preview="${escape(row.event.event_id)}" aria-label="Preview ${escape(row.event.ticker)} earnings"><strong>${escape(row.event.ticker)}</strong><span>${escape(row.event.company_name)}</span></button>`).join('')}${events.length>3?`<button class="more-events" data-date="${iso}">+${events.length-3} more</button>`:''}</section>`;}).join('')}</div></div>${!rows.length?'<div class="panel section-gap">'+empty('No matching earnings this month','Try another month or reset your filters.')+'</div>':''}`;
  }
  function preview(eventId) {
    const row=state.rows.find(r=>r.event.event_id===eventId);if(!row)return;
    const e=row.event;
    $('#preview-content').innerHTML=`<div class="preview-header"><div><div class="eyebrow">Earnings preview</div><h2 id="preview-title">${escape(e.company_name || e.ticker)}</h2><small class="quiet">${escape(e.ticker)} · ${escape(e.exchange || 'Exchange not stated')}</small></div><button class="icon-button" data-action="close-preview" aria-label="Close preview">${icon('close')}</button></div><div class="preview-date">${prettyDate(e.event_date,{month:'long',year:'numeric'})}</div>${badge(row)}${!state.feed.is_current?'<p class="calendar-note">Retained snapshot · not current</p>':''}<div class="preview-facts">${factRow('Market session',sessionLabel(e.session),sessionIcon(e.session))}${factRow('Fiscal period',e.fiscal_period || 'Not stated','document')}${factRow('Exact release time',sourceClock(e.scheduled_start,e.source_timezone),'clock')}${factRow('Conference call',callClock(row),'phone')}</div>${isEstimate(row)?`<div class="note-card"><h3>Low-confidence estimate</h3><p>${escape(prettyDate(e.estimate_basis_date,{year:'numeric'}))} filing day + 52 weeks. This is not an actual historical earnings date or a company announcement.</p>${sourceLink(e.estimate_basis_url,'View filing basis')}</div>`:`<div class="note-card"><h3>SEC source</h3><p>Source-backed forward earnings statement.</p>${sourceLink(e.source_url,'View source')}</div>`}<div class="note-card"><h3>Research brief unavailable</h3><p>Calendar facts and their evidence are available. A validated narrative brief has not been published.</p></div><div class="preview-footer"><a class="button primary" href="${eventURL(row)}" data-nav>Open full event ${icon('arrow')}</a><a class="button" href="${companyURL(e.ticker)}" data-nav>View company</a></div>`;
    $('#event-preview').showModal();
  }
  function callClock(row) {const call=row.conference_call;return call?.state==='available'?sourceClock(call.at,call.timezone):'Not stated';}
  function factRow(label,value,type) {return `<div class="fact-row">${icon(type)}<div><small>${escape(label)}</small><strong>${escape(value)}</strong></div></div>`;}
  function eventPage() {
    const eventId=decodeURIComponent(location.pathname.split('/').pop());const row=state.rows.find(r=>r.event.event_id===eventId);
    if(!row)return heading('Event unavailable','This event is not part of the selected accepted snapshot.')+trustStrip()+`<section class="panel">${empty('Event not in this snapshot','Use the calendar to open an event from the accepted publication.', '<a class="button primary" href="/earnings" data-nav>Open calendar</a>')}</section>`;
    const e=row.event;
    const facts=[['Release date',prettyDate(e.event_date,{year:'numeric'}),'calendar'],['Market session',sessionLabel(e.session),sessionIcon(e.session)],['Fiscal period',e.fiscal_period || 'Not stated','document'],['Originally checked',sourceClock(e.collected_at,'UTC'),'clock']];
    return `<div class="breadcrumb"><a data-nav href="/earnings">Calendar</a><span>/</span><a data-nav href="${companyURL(e.ticker)}">${escape(e.ticker)}</a><span>/</span><span>Earnings release</span></div><div class="page-heading"><div><div class="eyebrow">Earnings event</div><h1>${escape(e.company_name || e.ticker)} earnings</h1><div class="identity-line"><span>${escape(e.ticker)}</span><span>·</span><span>${escape(e.exchange || 'Exchange not stated')}</span>${badge(row)}<span>Revision ${e.revision_number}</span></div></div>${sourceLink(e.source_url,isEstimate(row)?'View filing basis':'View source','button primary')}</div>${trustStrip()}<section class="panel facts-ribbon">${facts.map(([label,value,type])=>`<div class="ribbon-item">${icon(type)}<div><small>${escape(label)}</small><strong>${escape(value)}</strong></div></div>`).join('')}</section><div class="detail-grid"><section class="panel"><div class="tab-list" role="tablist" aria-label="Event sections">${['summary','evidence','history'].map(tab=>`<button role="tab" id="tab-${tab}" aria-controls="event-tab-content" aria-selected="${state.tab===tab}" tabindex="${state.tab===tab?'0':'-1'}" data-tab="${tab}">${tab[0].toUpperCase()+tab.slice(1)}</button>`).join('')}</div><div id="event-tab-content" role="tabpanel" aria-labelledby="tab-${state.tab}">${state.tab==='history'?historyContent(row):state.tab==='evidence'?factEvidence(row):eventSummary(row)}</div>${unavailable()}${recordDetails(row)}</section><aside class="panel"><div class="panel-head"><h2>Evidence</h2><span class="badge unknown">SEC</span></div>${evidencePanel(row)}</aside></div>`;
  }
  function eventSummary(row) {
    const e=row.event;
    const text=isEstimate(row)?`${e.company_name || e.ticker} has a low-confidence estimated date of ${prettyDate(e.event_date,{year:'numeric'})}. It is calculated from the ${prettyDate(e.estimate_basis_date,{year:'numeric'})} SEC Item 2.02 filing day plus 52 weeks. The company has not announced this projected date.`:`${e.company_name || e.ticker} has a source-backed announced earnings date of ${prettyDate(e.event_date,{year:'numeric'})}${e.fiscal_period?' for '+e.fiscal_period:''}. ${e.session==='UNKNOWN'?'The release session has not been stated.':'The stated release session is '+sessionLabel(e.session).toLowerCase()+'.'}`;
    return `<h2>What is known</h2><p class="reading-text">${escape(text)}</p><p class="reading-text">${escape(e.title)}</p><table class="fact-table"><tbody><tr><td>Exact release time</td><td>${escape(sourceClock(e.scheduled_start,e.source_timezone))}<small>${escape(row.fields.release_time.reason || 'Source-supported exact clock')}</small></td></tr><tr><td>Release precision</td><td>${escape(e.time_precision.replaceAll('_',' '))}</td></tr><tr><td>Source timezone</td><td>${escape(e.source_timezone || 'Not stated')}</td></tr>${isEstimate(row)?`<tr><td>Estimate confidence</td><td>Low<small>Filing timing proxy; not accuracy-tested.</small></td></tr>`:''}</tbody></table>${callPanel(row)}`;
  }
  function callPanel(row) {return `<div class="call-panel">${icon('phone')}<div><h3>Conference call</h3><strong>${escape(callClock(row))}</strong><p>Separate from the earnings release. A call clock never fills an unknown release clock.</p></div></div>`;}
  function factEvidence(row) {
    const e=row.event;return `<h2>Source-backed event facts</h2><p class="reading-text">${isEstimate(row)?'This date is a calculation from retained SEC filing metadata.':'Facts below are bound to this accepted event revision and its SEC source.'}</p><table class="fact-table"><thead><tr><th>Fact</th><th>Value and support</th></tr></thead><tbody>${[['event_date','Release date',prettyDate(e.event_date,{year:'numeric'})],['fiscal_period','Fiscal period',e.fiscal_period || 'Not stated'],['session','Market session',sessionLabel(e.session)],['release_time','Exact release time',sourceClock(e.scheduled_start,e.source_timezone)]].map(([key,label,value])=>`<tr><td>${label}</td><td>${escape(value)}<small>${escape(row.fields[key].value===null?row.fields[key].reason:row.fields[key].state==='estimated'?'Calculated estimate · filing day + 52 weeks':'Source-supported fact')}</small></td></tr>`).join('')}</tbody></table>${callPanel(row)}<div class="section-gap">${sourceLink(e.source_url,isEstimate(row)?'Open estimate basis':'Open source statement')}</div>`;
  }
  const fieldLabels = {schema_version:'Record format',event_id:'Event identity',revision_number:'Accepted revision',event_type:'Event type',ticker:'Ticker',exchange:'Exchange',company_name:'Company',cik:'SEC CIK',title:'Event description',event_date:'Source-local date',scheduled_start:'Exact release clock (UTC)',source_timezone:'Source timezone',session:'Release session',fiscal_period:'Fiscal period',status:'Event lifecycle',time_precision:'Time precision',source_id:'Source publisher key',source_event_id:'Source record reference',source_url:'Public source',collected_at:'Originally checked',first_seen_at:'First accepted observation',last_seen_at:'Last accepted observation',content_hash:'Normalized record checksum',parser_version:'Interpretation version',date_basis:'Date basis',estimate_method:'Estimate method',estimate_basis_date:'Historical filing basis day',estimate_basis_url:'Historical filing basis source',estimate_confidence:'Estimate confidence'};
  function recordDetails(row) {return `<details class="record-details"><summary>Record details · all feed fields</summary><p class="quiet" style="font-size:11px;margin-bottom:10px">Reproducibility details for this exact accepted record. A record checksum is distinct from a source document checksum.</p><dl class="record-table">${Object.entries(fieldLabels).map(([key,label])=>`<div class="record-field"><dt>${escape(label)} <span class="quiet">(${key})</span></dt><dd>${key.endsWith('_url')||key==='source_url'?sourceLink(row.event[key],row.event[key] || 'Not available'):escape(row.event[key] ?? 'Not stated')}</dd></div>`).join('')}<div class="record-field"><dt>Accepted publication</dt><dd>${escape(row.publication_id)}</dd></div></dl></details>`;}
  function evidencePanel(row) {
    const data=state.evidence.get(row.event.event_id);const e=row.event;
    let proof=`<p class="reading-text" style="font-size:12px">Verify the retained source for this selected event to view its exact excerpt or calculation proof.</p><button class="button section-gap" data-load-evidence="${escape(e.event_id)}">${icon('shield')} Verify source evidence</button>`;
    if(data?.loading)proof='<div class="inline-loading"><span class="loader"></span>Verifying retained source bytes…</div><p class="quiet" style="font-size:11px">This reads existing evidence. It does not collect new data.</p>';
    else if(data?.error)proof=`<div class="note-card"><h3>Evidence verification unavailable</h3><p>${escape(data.error)}</p><button class="button compact section-gap" data-load-evidence="${escape(e.event_id)}">Retry verification</button></div>`;
    else if(data?.record){const record=data.record;const evidence=record.evidence[0];proof=`<div class="proof-label">${evidence.calculation?'Verified calculation proof':'Verified source excerpt'}</div>${evidence.excerpt?`<blockquote class="evidence-excerpt">${escape(evidence.excerpt)}</blockquote>`:evidence.calculation?`<blockquote class="evidence-excerpt">${escape(evidence.calculation.input_filing_date)} filing day + ${evidence.calculation.weeks_added} weeks<br><strong>${escape(evidence.calculation.output_estimated_date)} estimated date</strong><br><small>This filing day is not an actual historical earnings release.</small></blockquote>`:'<p class="reading-text">No excerpt is available for this evidence kind.</p>'}<dl class="detail-list"><div><dt>Support</dt><dd>${evidence.calculation?'Calculated estimate':'Forward source statement'}</dd></div><div><dt>Filing day</dt><dd>${escape(evidence.filed_on || 'Not stated')}</dd></div><div><dt>Source publication time</dt><dd>Not stated</dd></div><div><dt>Locator</dt><dd>${escape(typeof evidence.locator==='object'?JSON.stringify(evidence.locator):evidence.locator || 'Not available')}</dd></div></dl><details class="record-details"><summary>Evidence reproduction details</summary><dl class="record-table">${[['Evidence identity',evidence.evidence_id],['Document checksum',evidence.document_sha256],['Parser input checksum',evidence.parser_input_sha256],['Normalized text checksum',evidence.normalized_text_sha256],['Evidence pack identity',record.evidence_pack_id]].map(([label,value])=>`<div class="record-field"><dt>${label}</dt><dd>${escape(value || 'Not applicable')}</dd></div>`).join('')}</dl></details>`;}
    return `<div class="evidence-source">${icon('document')}<div><strong>${isEstimate(row)?'SEC filing basis':'SEC forward statement'}</strong><small>${isEstimate(row)?'Historical Item 2.02 filing metadata':'Accepted announcement evidence'}</small></div></div>${proof}<div class="section-gap">${sourceLink(e.source_url,'Open public source')}</div><dl class="detail-list"><div><dt>Originally observed</dt><dd>${escape(sourceClock(row.evidence[0]?.observed_at,'UTC'))}</dd></div><div><dt>Exact release clock</dt><dd>${escape(sourceClock(e.scheduled_start,e.source_timezone))}</dd></div></dl>`;
  }
  async function loadEvidence(eventId) {
    if(state.evidence.get(eventId)?.loading)return;const publication=state.feed.publication_id;state.evidence.set(eventId,{loading:true});render();
    try{const data=await getJSON(`/v1/earnings/${encodeURIComponent(eventId)}/evidence?publication_id=${encodeURIComponent(publication)}`);if(state.feed?.publication_id===publication)state.evidence.set(eventId,data);}
    catch(error){if(state.feed?.publication_id===publication)state.evidence.set(eventId,{error:error.message});}
    if(route()==='event'&&location.pathname.endsWith(eventId))render();
  }
  function historyContent(row) {
    const data=state.history.get(row.event.event_id);
    if(!data||data.loading)return '<h2>Accepted revision history</h2><div class="inline-loading"><span class="loader"></span>Loading retained revisions…</div>';
    if(data.error)return `<h2>History unavailable</h2><p class="reading-text">${escape(data.error)}</p><button class="button section-gap" data-load-history="${escape(row.event.event_id)}">Retry history</button>`;
    return `<h2>Accepted revision history</h2><p class="reading-text" style="margin-bottom:24px">Retained versions through revision ${row.event_revision}. First observation is not the original company announcement time.</p>${data.versions.map(v=>`<article class="history-item"><h3>Revision ${v.revision}${v.revision===row.event_revision?' · shown in this publication':''}</h3><small>Recorded ${escape(sourceClock(v.recorded_at,'UTC'))}</small><div class="history-facts"><span>${escape(v.facts.event_date || 'No supported date')}</span><span>${escape(v.facts.status)}</span><span>${sessionLabel(v.facts.session)}</span><span>${escape(v.facts.fiscal_period || 'Period not stated')}</span></div><p>${sourceLink(v.facts.source_url,'Version source')}</p></article>`).join('')}`;
  }
  async function loadHistory(eventId) {
    if(state.history.get(eventId)?.loading)return;const publication=state.feed.publication_id;state.history.set(eventId,{loading:true});render();
    try{const data=await getJSON(`/v1/earnings/${encodeURIComponent(eventId)}/history?publication_id=${encodeURIComponent(publication)}`);if(state.feed?.publication_id===publication)state.history.set(eventId,data);}
    catch(error){if(state.feed?.publication_id===publication)state.history.set(eventId,{error:error.message});}
    if(route()==='event'&&location.pathname.endsWith(eventId))render();
  }
  function companyPage() {
    const ticker=decodeURIComponent(location.pathname.split('/').pop()).toUpperCase();
    const match=state.rows.find(row=>(row.event.ticker || '').toUpperCase()===ticker || row.listings.some(l=>l.ticker.toUpperCase()===ticker));
    const missing=state.feed.missing_issuers.find(row=>row.listings.some(l=>l.ticker.toUpperCase()===ticker));
    if(!match&&!missing)return heading('Company not found','Search a company in the accepted company scope.')+`<section class="panel">${empty('Company outside this snapshot','Use search to select a company or listing in the accepted scope.','<a class="button" data-nav href="/coverage">View data coverage</a>')}</section>`;
    const name=match?.event.company_name || missing.company_name;const cik=match?.event.cik || missing.cik;const listings=match?.listings || missing.listings;
    const rows=state.rows.filter(row=>row.event.cik===cik);const upcoming=rows.find(row=>row.event.event_date>=today()&&row.event.status!=='cancelled');
    return `<div class="breadcrumb"><a href="/earnings" data-nav>Calendar</a><span>/</span><span>${escape(ticker)}</span></div><div class="company-hero">${avatar(ticker,true)}<div><div class="eyebrow">Company research</div><h1>${escape(name)}</h1><div class="listing-chips">${listings.map(l=>`<a href="${companyURL(l.ticker)}" data-nav class="listing-chip">${escape(l.ticker)} · ${escape(l.exchange || 'Exchange not stated')}</a>`).join('')}</div></div></div>${trustStrip()}<div class="detail-grid"><div class="stack"><section class="panel"><div class="panel-head"><h2>Next accepted earnings</h2><span class="quiet" style="font-size:11px">SEC only</span></div>${upcoming?`<div class="feature-title"><div><span class="feature-date">${prettyDate(upcoming.event.event_date,{year:'numeric'})}</span>${badge(upcoming)}<div class="meta-row"><span>${icon(sessionIcon(upcoming.event.session))}${sessionLabel(upcoming.event.session)}</span><span>${icon('document')}${escape(upcoming.event.fiscal_period || 'Period not stated')}</span></div></div></div>${isEstimate(upcoming)?'<p class="reading-text">Low-confidence filing-date projection. A company announcement has not been established for this date.</p>':''}<div class="button-row section-gap"><a class="button primary" data-nav href="${eventURL(upcoming)}">Open earnings event ${icon('arrow')}</a>${sourceLink(upcoming.event.source_url,isEstimate(upcoming)?'View filing basis':'View source')}</div>`:empty('No supported upcoming date',missing?.reason || 'No upcoming event is present in this accepted snapshot.','<a class="button" href="/coverage" data-nav>Understand date coverage</a>')}</section><section class="panel"><div class="panel-head"><h2>Events in this publication</h2></div>${rows.length?agendaTable(rows):'<p class="quiet">No accepted event for this company.</p>'}<p class="calendar-note">Retained history accumulates from collection. This list is not a complete two-year earnings backfill.</p></section>${unavailable()}</div><aside class="panel"><h2>Company details</h2><dl class="detail-list"><div><dt>SEC CIK</dt><dd>${escape(cik)}</dd></div><div><dt>Listings</dt><dd>${listings.length}</dd></div><div><dt>Selected ticker</dt><dd>${escape(ticker)}</dd></div><div><dt>Date coverage</dt><dd>${upcoming?isEstimate(upcoming)?'Estimated':'Announced':'No supported date'}</dd></div></dl><div class="note-card"><h3>Shared company identity</h3><p>Share-class listings belong to the same SEC issuer. Events count once per accepted event; selecting a listing does not duplicate it.</p></div><div class="section-gap"><a class="text-link" href="/earnings?q=${encodeURIComponent(ticker)}" data-nav>View in calendar ${icon('arrow')}</a></div></aside></div>`;
  }
  function coveragePage() {
    const issuers=new Map();for(const row of state.rows){const key=row.event.cik || row.company_id;if(!issuers.has(key)||!isEstimate(row))issuers.set(key,row);}
    const announced=[...issuers.values()].filter(r=>r.event.date_basis==='announced').length;const estimated=[...issuers.values()].filter(isEstimate).length;const missing=state.feed.missing_issuers.length;const total=issuers.size+missing;const listingCount=[...issuers.values()].reduce((sum,row)=>sum+row.listings.length,0)+state.feed.missing_issuers.reduce((sum,row)=>sum+row.listings.length,0);const current=state.feed.is_current;
    const query=state.filter.toLowerCase().trim();const missingRows=state.feed.missing_issuers.filter(row=>!query||[row.company_name,...row.listings.map(l=>l.ticker)].join(' ').toLowerCase().includes(query));
    return `${heading('Understand your data','What this earnings snapshot supports, and where information is missing.','<button class="button" data-action="reload">'+icon('clock')+' Reload snapshot</button>')}${trustStrip()}<div class="kpi-grid">${kpi('Companies in scope',total,'Accepted publication scope','database')}${kpi('Announced dates',current?announced:'—','Distinct companies','check')}${kpi('Estimated dates',current?estimated:'—','Distinct companies','clock',true)}${kpi('No supported date',current?missing:'—','Distinct companies','document')}</div><div class="detail-grid"><div class="stack"><section class="panel"><h2>${current?'Date coverage':'Retained date coverage'}</h2><p class="reading-text" style="font-size:13px">${total} SEC issuers · ${listingCount} listings · ${state.rows.length} accepted events. These denominators differ because a company can have multiple listings.</p><div class="coverage-bar" aria-label="${announced} announced companies, ${estimated} estimated companies, ${missing} without supported date"><span style="width:${total?announced/total*100:0}%"></span><span class="estimated" style="width:${total?estimated/total*100:0}%"></span><span class="missing" style="width:${total?missing/total*100:0}%"></span></div><div class="coverage-legend"><span><i></i>Announced ${announced}</span><span class="estimated"><i></i>Estimated ${estimated}</span><span class="missing"><i></i>No date ${missing}</span></div>${!current?'<p class="calendar-note">This breakdown describes retained records, not current coverage.</p>':''}</section><section class="panel"><div class="panel-head"><div><h2>Companies without a supported date</h2><p>Missing support stays visible. No event date is fabricated.</p></div></div><div class="filter-field"><label for="coverage-filter">Search missing companies</label><input id="coverage-filter" type="search" placeholder="Company or ticker" value="${escape(state.filter)}"></div><div class="coverage-companies">${missingRows.map(row=>`<div class="coverage-company"><div><a data-nav href="${companyURL(row.listings[0]?.ticker)}">${escape(row.company_name)}</a><small>${escape(row.listings.map(l=>l.ticker).join(' · '))}</small></div><span class="badge unknown">No date</span></div>`).join('') || '<p class="reading-text">No companies match this search.</p>'}</div><p class="calendar-note">No accepted announced date or unambiguous approved forecast basis.</p></section></div><div class="stack"><section class="panel"><h2>Source coverage</h2><div class="evidence-source section-gap">${icon('document')}<div><strong>SEC EDGAR</strong><small>Current collector</small></div></div><p class="reading-text" style="font-size:13px">Forward statements in company filings support announced dates. Prior-year Item 2.02 filing dates can support clearly labeled estimates.</p><div class="note-card"><h3>SEC coverage has limits</h3><p>A company may publish a schedule on another channel before it appears in the SEC filings this collector checks. Calendar absence is not proof that no announcement exists.</p></div></section><section class="panel"><h2>Availability</h2><dl class="detail-list"><div><dt>Calendar facts</dt><dd>${current?'Current accepted snapshot':'Retained snapshot'}</dd></div><div><dt>Exact release clocks</dt><dd>${state.rows.filter(r=>r.event.scheduled_start).length} supported</dd></div><div><dt>Separate call clocks</dt><dd>${state.rows.filter(r=>r.conference_call?.state==='available').length} supported</dd></div><div><dt>Narrative briefs</dt><dd>Unavailable</dd></div><div><dt>Price reactions</dt><dd data-reaction-availability>Reading accepted history…</dd></div></dl></section></div></div><details class="panel record-details section-gap"><summary>Snapshot details</summary><dl class="detail-list"><div><dt>Publication identity</dt><dd>${escape(state.feed.publication_id)}</dd></div><div><dt>Published</dt><dd>${escape(sourceClock(state.feed.publication_generated_at,'UTC'))}</dd></div><div><dt>Current trust</dt><dd>${escape(state.feed.calendar_state)}</dd></div><div><dt>Reader format</dt><dd>${escape(state.feed.contract_version)}</dd></div></dl></details>`;
  }
  function preferenceSelect(key,options) {return `<select data-preference="${key}" aria-label="${key}">${options.map(([value,label])=>`<option value="${value}" ${prefs[key]===value?'selected':''}>${label}</option>`).join('')}</select>`;}
  function preferencesPage() {
    const setting=(title,detail,control)=>`<div class="setting-row"><div><strong>${title}</strong><p>${detail}</p></div><div class="setting-control">${control}</div></div>`;
    const help=[['What does “Announced” mean?','An explicit forward statement in accepted source evidence supports the date. It does not guarantee that a company will keep the schedule.'],['How are estimated dates calculated?','The approved SEC estimate projects an unambiguous prior-year original Item 2.02 filing day forward 52 weeks. It has low confidence and is not an actual historical earnings-release date or a company-announced date.'],['Why is the release time missing?','An exact release clock requires explicit source support and timezone. Date-only records stay on their source-local day. A conference-call clock is a separate fact.'],['What is a retained snapshot?','A previously accepted publication remains readable for reference when its freshness, source trust, or scope no longer supports current use. The app shows its original publication time and withholds current KPI counts.'],['How are historical reactions sourced?','Past earnings panels show actual release statements from retained SEC filings and adjusted daily price moves where identity, timing and prices support them. Unknown release sessions use a wider date bracket. A validated narrative brief has not been published.'],['What company scope is covered?','This interface uses the company and listing scope attached to its accepted publication. The current provisional scope is visible on Data coverage; it is not an exhaustive global calendar.']];
    return `${heading('Preferences & help','Make the workspace comfortable and understand the data.')}<div class="detail-grid"><section class="panel"><h2>Your workspace</h2><p class="reading-text" style="font-size:12px">Preferences are saved in this browser.</p>${setting('Appearance','Choose the palette that fits your workspace.',preferenceSelect('theme',[['dark','Midnight'],['light','Daylight'],['system','Follow system']]))}${setting('Display timezone','Converts known timestamps only. Source-local event dates never shift.',preferenceSelect('timezone',[['source','Source timezone'],['UTC','UTC'],['Asia/Karachi','Asia/Karachi'],['America/New_York','America/New_York']]))}${setting('Information density','Adjust spacing in agenda rows.',preferenceSelect('density',[['comfortable','Comfortable'],['compact','Compact']]))}${setting('Motion','Reduce lift and loading animations when preferred.',preferenceSelect('motion',[['system','Follow system'],['reduced','Reduced motion']]))}<div class="button-row section-gap"><button class="button" data-action="reset-preferences">Reset preferences</button></div></section><section class="panel"><h2>Calendar & evidence guide</h2><div class="section-gap">${help.map(([title,text])=>`<details class="help-topic"><summary>${title}</summary><p>${text}</p></details>`).join('')}</div><div class="section-gap"><a href="/coverage" data-nav class="text-link">Explore data coverage ${icon('arrow')}</a></div></section></div>`;
  }
  function render() {
    updateNav();const main=$('#main');let content;
    if(route()==='preferences')content=preferencesPage();
    else if(state.loading&&!state.feed)content='<div class="loading-state"><span class="loader"></span><h1>Opening your earnings workspace</h1><p>Checking the accepted publication. No source collection is triggered.</p></div>';
    else if(state.error)content=heading('Data temporarily unavailable','The accepted snapshot could not be read.')+`<section class="panel">${empty('Unable to load accepted earnings',state.error,'<button class="button primary" data-action="reload">Retry data read</button>')}</section>`;
    else if(!state.feed)content='<div class="loading-state"><span class="loader"></span><h1>Opening your earnings workspace</h1></div>';
    else content=({overview,calendar,event:eventPage,company:companyPage,coverage:coveragePage}[route()] || overview)();
    main.innerHTML=content;main.setAttribute('aria-busy',String(state.loading));decorate(main);
    $('.count-chart-svg',main)?.closest('.panel').classList.add('chart-panel');
    if(state.feed&&!state.loading&&!state.error){
      const selected=route()==='event'?state.rows.find(row=>row.event.event_id===location.pathname.split('/').pop()):null;
      const ticker=route()==='company'?decodeURIComponent(location.pathname.split('/').pop()).toUpperCase():selected?.event.ticker;
      document.dispatchEvent(new CustomEvent('wtf:market-view',{detail:{route:route(),ticker,publication_id:state.feed.publication_id}}));
    }
    const status=$('#feed-status');const waiting=Boolean(state.pendingFeed);status.dataset.state=state.error?'unavailable':waiting?'pending':state.feed?.is_current?'current':state.feed?'retained':'connecting';
    $('span',status).textContent=state.error?'Data unavailable':waiting?'New data ready':state.feed?.is_current?'Auto updates on':state.feed?'Retained snapshot':'Connecting…';
    status.title=waiting?'Your open view keeps its selected snapshot. View latest data or return to the calendar.':'New accepted earnings update automatically. An open event or preview keeps its selected snapshot.';
    const title=$('h1',main)?.textContent || 'Earnings';document.title=`${title} · Catalyst Doot`;
  }
  function updateCalendarURL() {history.replaceState({},'',calendarQuery());}
  function shiftCalendar(direction) {
    if(state.view==='month'){const date=parseDate(state.anchor);state.anchor=localDate(new Date(date.getFullYear(),date.getMonth()+direction,1,12));}
    else state.anchor=addDays(state.anchor,direction*(state.view==='week'?7:90));
    state.page=0;updateCalendarURL();render();
  }
  function updateSearch() {
    const query=$('#global-search').value.trim().toLowerCase();const results=$('#search-results');
    if(!query){results.hidden=true;$('#global-search').setAttribute('aria-expanded','false');return;}
    const unique=new Map();
    for(const row of state.rows)if(matches(row,query)&&!unique.has(row.event.cik))unique.set(row.event.cik,{ticker:row.event.ticker,name:row.event.company_name,date:row.event.event_date,estimated:isEstimate(row)});
    for(const row of state.feed?.missing_issuers || [])if([row.company_name,...row.listings.map(l=>l.ticker)].join(' ').toLowerCase().includes(query))unique.set(row.cik,{ticker:row.listings[0]?.ticker,name:row.company_name,date:null});
    results.innerHTML=unique.size?[...unique.values()].slice(0,8).map(row=>`<a class="search-result" data-nav href="${companyURL(row.ticker)}">${avatar(row.ticker)}<span><strong>${escape(row.name)}</strong><small>${escape(row.ticker)} · ${row.date?escape(prettyDate(row.date))+' · '+(row.estimated?'Estimated':'Announced'):'No supported date'}</small></span></a>`).join(''):`<div class="search-empty">${state.loading?'Loading accepted companies…':state.error?'Data is unavailable. Retry the snapshot read.':'No matching company in the accepted scope.'}</div>`;
    results.hidden=false;$('#global-search').setAttribute('aria-expanded','true');
  }
  document.addEventListener('click',event=>{
    if(document.body.classList.contains('nav-open')&&!event.target.closest('#sidebar,#menu-toggle')){document.body.classList.remove('nav-open');$('#menu-toggle').setAttribute('aria-expanded','false');manageNavigation();$('#menu-toggle').focus();return;}
    const link=event.target.closest('a[data-nav]');
    if(link&&!event.ctrlKey&&!event.metaKey&&!event.shiftKey&&event.button===0){event.preventDefault();navigate(link.getAttribute('href'));return;}
    const previewButton=event.target.closest('[data-preview]');if(previewButton){preview(previewButton.dataset.preview);return;}
    const dateButton=event.target.closest('[data-date]');if(dateButton){navigate(`/earnings?date=${dateButton.dataset.date}`);return;}
    const viewButton=event.target.closest('[data-view]');if(viewButton){state.view=viewButton.dataset.view;state.page=0;updateCalendarURL();render();return;}
    const tabButton=event.target.closest('[data-tab]');if(tabButton){state.tab=tabButton.dataset.tab;render();$(`#tab-${state.tab}`).focus();if(state.tab==='history'){const id=location.pathname.split('/').pop();if(!state.history.has(id))loadHistory(id);}return;}
    const evidenceButton=event.target.closest('[data-load-evidence]');if(evidenceButton){loadEvidence(evidenceButton.dataset.loadEvidence);return;}
    const historyButton=event.target.closest('[data-load-history]');if(historyButton){loadHistory(historyButton.dataset.loadHistory);return;}
    const action=event.target.closest('[data-action]')?.dataset.action;
    if(action==='reload')loadFeed();
    if(action==='close-preview')$('#event-preview').close();
    if(action==='clear-filters'){state.filter='';state.basis='all';state.session='all';state.page=0;updateCalendarURL();render();}
    if(action==='calendar-today'){state.anchor=today();state.page=0;updateCalendarURL();render();}
    if(action==='calendar-prev')shiftCalendar(-1);if(action==='calendar-next')shiftCalendar(1);
    if(action==='page-prev'||action==='page-next'){state.page+=action==='page-next'?1:-1;render();window.scrollTo(0,140);}
    if(action==='mini-prev'||action==='mini-next'){const [y,m]=state.miniMonth.split('-').map(Number);state.miniMonth=localDate(new Date(y,m-1+(action==='mini-next'?1:-1),1,12)).slice(0,7);render();}
    if(action==='reset-preferences'){prefs={theme:'dark',timezone:'source',density:'comfortable',motion:'system'};applyPrefs();render();toast('Workspace preferences reset.');}
    if(!event.target.closest('.global-search')){$('#search-results').hidden=true;$('#global-search').setAttribute('aria-expanded','false');}
  });
  function chartDetail(event) {const day=event.target.closest('[data-chart-detail]');if(day){const detail=$('.chart-detail',day.closest('.panel'));if(detail)detail.textContent=day.dataset.chartDetail;}}
  document.addEventListener('pointerover',chartDetail);document.addEventListener('focusin',chartDetail);
  document.addEventListener('change',event=>{
    if(event.target.id==='overview-window'){state.window=Number(event.target.value);render();}
    if(event.target.id==='basis-filter'||event.target.id==='session-filter'){state[event.target.id==='basis-filter'?'basis':'session']=event.target.value;state.page=0;updateCalendarURL();render();}
    if(event.target.dataset.preference){prefs[event.target.dataset.preference]=event.target.value;applyPrefs();toast('Preference saved in this browser.');}
  });
  let filterTimer;
  document.addEventListener('input',event=>{
    if(event.target.id==='global-search'){updateSearch();return;}
    if(['company-filter','coverage-filter'].includes(event.target.id)){const target=event.target;state.filter=target.value;state.page=0;clearTimeout(filterTimer);filterTimer=setTimeout(()=>{const id=target.id;const selection=target.selectionStart;if(route()==='calendar')updateCalendarURL();render();const input=$(`#${id}`);if(input){input.focus();try{input.setSelectionRange(selection,selection);}catch{}}},140);}
  });
  document.addEventListener('keydown',event=>{
    const typing=event.target.matches('input,textarea,select');
    if(event.key==='/'&&!typing&&!$('#event-preview').open){event.preventDefault();$('#global-search').focus();}
    if(event.key==='Escape'){$('#search-results').hidden=true;$('#global-search').setAttribute('aria-expanded','false');const navOpen=document.body.classList.contains('nav-open');document.body.classList.remove('nav-open');$('#menu-toggle').setAttribute('aria-expanded','false');manageNavigation();if(navOpen)$('#menu-toggle').focus();}
    if(event.key==='Tab'&&document.body.classList.contains('nav-open')){const links=[...$('#sidebar').querySelectorAll('a')];if(event.shiftKey&&document.activeElement===links[0]){event.preventDefault();links.at(-1).focus();}else if(!event.shiftKey&&document.activeElement===links.at(-1)){event.preventDefault();links[0].focus();}}
    if(event.key==='ArrowDown'&&event.target.id==='global-search'&&!$('#search-results').hidden){event.preventDefault();$('.search-result')?.focus();}
    if(event.target.closest('.search-results')&&['ArrowDown','ArrowUp'].includes(event.key)){const links=[...$('#search-results').querySelectorAll('a')];const index=links.indexOf(event.target.closest('a'));event.preventDefault();links[Math.max(0,Math.min(links.length-1,index+(event.key==='ArrowDown'?1:-1)))]?.focus();}
    if(event.key==='Enter'&&event.target.id==='global-search'){const first=$('.search-result');if(first){event.preventDefault();navigate(first.getAttribute('href'));}}
    if(event.target.dataset.tab&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){const tabs=['summary','evidence','history'];let index=tabs.indexOf(event.target.dataset.tab);index=event.key==='Home'?0:event.key==='End'?2:(index+(event.key==='ArrowRight'?1:2))%3;event.preventDefault();$(`[data-tab="${tabs[index]}"]`).click();}
  });
  $('#theme-toggle').addEventListener('click',()=>{prefs.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';applyPrefs();if(route()==='preferences')render();});
  $('#menu-toggle').addEventListener('click',()=>{document.body.classList.toggle('nav-open');const open=document.body.classList.contains('nav-open');$('#menu-toggle').setAttribute('aria-expanded',String(open));manageNavigation();if(open)$('#sidebar a').focus();});
  $('#event-preview').addEventListener('click',event=>{if(event.target===$('#event-preview')){const rect=event.target.getBoundingClientRect();if(event.clientX<rect.left)event.target.close();}});
  window.addEventListener('popstate',()=>navigate(location.pathname+location.search,false));
  window.addEventListener('resize',manageNavigation);
  setInterval(checkSnapshotTrust,60000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)checkSnapshotTrust();});
  window.addEventListener('focus',checkSnapshotTrust);
  matchMedia('(prefers-color-scheme: light)').addEventListener('change',()=>{if(prefs.theme==='system')applyPrefs();});
  decorate();applyPrefs();navigate(location.pathname+location.search,false);loadFeed();
})();
