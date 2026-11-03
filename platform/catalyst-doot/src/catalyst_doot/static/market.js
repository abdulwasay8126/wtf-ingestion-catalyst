/* Read accepted market publications only. Browser requests never reach a provider. */
(() => {
  'use strict';
  const escape = value => String(value ?? 'Not supplied').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const label = key => escape(key.replaceAll('_',' '));
  const link = (value, text) => {
    try { const url = new URL(value); if(url.protocol!=='https:' || url.username || url.password) return escape(text);
      return `<a class="text-link" href="${escape(url.href)}" target="_blank" rel="noopener noreferrer">${escape(text)}</a>`;
    } catch { return escape(text); }
  };
  const table = (columns, rows) => `<div class="market-scroll"><table class="fact-table"><thead><tr>${columns.map(c=>`<th>${escape(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(v=>`<td>${escape(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  let view = null, controller = null;
  function unavailable(panel, reason) {
    panel.innerHTML = `<h2>Market context unavailable</h2><p class="reading-text">${escape(reason)}</p><p class="quiet">Accepted earnings facts remain available. Historical prices and news have their own publication and freshness.</p>`;
  }
  function content(result, ticker) {
    const data = result.data;
    const evidence = new Map(data.evidence.map(item=>[item.id,item]));
    const covered = new Set(data.prices.map(item=>item.ticker));
    if(!ticker) return `<h2>Market data coverage</h2><p class="reading-text">${covered.size} of ${data.scope.length} listings have price observations in this window.</p><p class="quiet">${escape(data.observed_from)}–${escape(data.observed_through)} · ${escape(result.market_state)} · separate from earnings date coverage</p><p>${data.news.length} recent news items · ${data.corporate_actions.length} corporate actions · ${data.trading_calendar.length} market sessions</p><p class="quiet">${escape(data.news_scope)}</p>`;
    const prices=data.prices.filter(item=>item.ticker===ticker);
    const missing=data.coverage.find(item=>item.ticker===ticker)?.missing_trading_dates || [];
    const source=prices.length?evidence.get(prices[prices.length-1].evidence_id):null;
    const priceTable=prices.length?table(['Trading day','Open','High','Low','Close','Volume','VWAP','Trades'],prices.map(item=>[item.trading_date,item.open,item.high,item.low,item.close,item.volume,item.vwap,item.trade_count])):'<p class="reading-text">No price observations were returned for this listing in the requested window.</p>';
    const articles=data.news.map(item=>`<article class="market-news"><h3>${link(item.url,item.headline)}</h3><p class="quiet">${escape(item.publisher)} · published ${escape(item.published_at)} · updated ${escape(item.updated_at)}</p><small>Provider ID ${escape(item.id)} · ${escape(item.tickers.join(', '))} · discovery only</small></article>`).join('') || '<p class="quiet">No item for this listing appeared in the bounded news sample.</p>';
    const actions=data.corporate_actions.map(item=>`<details class="record-details"><summary>${label(item.type)} · processed ${escape(item.process_date)}</summary><dl class="detail-list">${Object.entries(item.source_fields).map(([k,v])=>`<div><dt>${label(k)}</dt><dd>${escape(typeof v==='object'&&v!==null?JSON.stringify(v):v)}</dd></div>`).join('')}</dl></details>`).join('') || '<p class="quiet">No corporate action was returned for the queried process-date window.</p>';
    return `<div class="panel-head"><div><h2>Market context · ${escape(ticker)}</h2><p>Historical SIP · USD · all provider adjustments</p></div><span class="badge">${escape(result.market_state)}</span></div><p class="quiet">Source observations ${escape(data.observed_from)}–${escape(data.observed_through)}. Window ${escape(data.window.start)}–${escape(data.window.end)}.</p><p class="reading-text">Provider-mapped listing history. Continuous historical issuer identity is not yet verified.</p><details class="record-details" open><summary>Daily price observations · ${prices.length} rows</summary>${priceTable}${source?`<p>${link(source.source_url,'Price source')} · observed ${escape(source.observed_at)}</p>`:''}${missing.length?`<p class="quiet">Missing trading dates: ${escape(missing.join(', '))}</p>`:''}</details><details class="record-details"><summary>Recent news · ${data.news.length} items</summary><p class="quiet">${escape(data.news_scope)} Headlines are discovery leads; they do not confirm earnings facts.</p>${articles}</details><details class="record-details"><summary>Corporate actions · ${data.corporate_actions.length} records</summary><p class="quiet">Process dates and effective/ex-dates are distinct. Provider reporting may be delayed.</p>${actions}</details><details class="record-details"><summary>Trading calendar</summary>${table(['Date','Open','Close','Timezone'],data.trading_calendar.map(item=>[item.date,item.open,item.close,item.timezone]))}</details><details class="record-details"><summary>Publication and evidence</summary><dl class="detail-list"><div><dt>Market publication</dt><dd>${escape(data.market_publication_id)}</dd></div><div><dt>Calendar publication</dt><dd>${escape(data.calendar_publication_id)}</dd></div><div><dt>Trust deadline</dt><dd>${escape(data.trust_deadline)}</dd></div></dl>${data.evidence.map(item=>`<p>${link(item.source_url,item.dataset)} · ${escape(item.observed_at)}<small class="market-hash">SHA-256 ${escape(item.sha256)}</small></p>`).join('')}</details>`;
  }
  async function refresh(detail) {
    if(!['company','event','coverage'].includes(detail.route) || (detail.route!=='coverage'&&!detail.ticker)){controller?.abort();return;}
    const main=document.getElementById('main');
    let panel=main.querySelector('[data-market-panel]');
    if(!panel){panel=document.createElement('section');panel.className='panel market-context';panel.dataset.marketPanel='true';panel.innerHTML='<h2>Market context</h2><p class="quiet">Reading accepted market evidence…</p>';main.append(panel);}
    const binding=`${detail.publication_id}:${detail.ticker || ''}`;
    panel.dataset.binding=binding;
    controller?.abort();controller=new AbortController();
    const current=controller;
    const timeout=setTimeout(()=>current.abort(),60000);
    try {
      const params=new URLSearchParams({publication_id:detail.publication_id});if(detail.ticker)params.set('ticker',detail.ticker);
      const response=await fetch(`/v1/earnings/market?${params}`,{cache:'no-store',signal:current.signal});
      if(!response.ok)throw new Error('Market evidence could not be read for the selected snapshot.');
      const result=await response.json();
      if(!panel.isConnected || panel.dataset.binding!==binding || current!==controller)return;
      if(result.contract_version!=='earnings-market-0.1.0' || result.calendar_publication_id!==detail.publication_id)throw new Error('Market publication binding could not be verified.');
      if(!result.is_current || !result.data){unavailable(panel,result.reason || 'No current market publication is available.');return;}
      if(!['scope','prices','news','corporate_actions','trading_calendar','evidence','coverage'].every(k=>Array.isArray(result.data[k])))throw new Error('Market response is incomplete.');
      panel.innerHTML=content(result,detail.ticker);
    } catch(error) {
      if(panel.isConnected && panel.dataset.binding===binding && current===controller)unavailable(panel,error.name==='AbortError'?'Market evidence read timed out.':error.message);
    } finally {clearTimeout(timeout);}
  }
  document.addEventListener('wtf:market-view',event=>{view=event.detail;void refresh(view);});
  setInterval(()=>{if(view&&!document.hidden)void refresh(view);},60000);
  document.addEventListener('visibilitychange',()=>{if(view&&!document.hidden)void refresh(view);});
})();
