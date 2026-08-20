const $ = (id) => document.getElementById(id);
const fmtMoney = (v) => v == null ? '—' : `${v < 0 ? '-' : ''}£${Math.abs(v).toLocaleString(undefined,{maximumFractionDigits:0})}`;
const fmt = (v,d=1) => v == null ? '—' : Number(v).toLocaleString(undefined,{maximumFractionDigits:d});
const signed = (v,d=1) => `${v >= 0 ? '+' : ''}${fmt(v,d)}`;

let state = {
  gameId: null,
  phase: 'setup',
  metadata: null,
  forecast: [],
  da: [],
  briefing: null,
  periods: [],
  result: null,
  power: 100,
  autoFinishing: false
};

$('starting_soc_fraction').addEventListener('input', e => $('socLabel').textContent = `${Math.round(+e.target.value*100)}%`);
$('capacity_contract_fraction').addEventListener('input', e => $('capacityLabel').textContent = `${Math.round(+e.target.value*100)}%`);
$('rtHoldback').addEventListener('input', e => $('holdbackLabel').textContent = `${Math.round(+e.target.value*100)}%`);
$('startButton').addEventListener('click', () => state.gameId ? newTradingDay() : startGame());
$('resumeButton').addEventListener('click', resumeSavedGame);
$('autoBidButton').addEventListener('click', autoFillBidBook);
$('submitDaButton').addEventListener('click', submitDayAhead);
$('useDaBidButton').addEventListener('click', useCurrentDaBid);
$('clearPeriodButton').addEventListener('click', () => clearRtPeriod(false));
$('autoFinishButton').addEventListener('click', autoFinish);


function refreshResumeButton(){
  const canResume=Boolean(window.gridvaultApi?.hasSavedGame?.()) && !state.gameId;
  $('resumeButton').classList.toggle('hidden',!canResume);
}

function newTradingDay(){
  window.gridvaultApi?.clearSavedGame?.();
  location.reload();
}

function applySavedSetup(metadata){
  if(!metadata) return;
  $('battery_node').value=metadata.battery_node;
  $('battery_power_mw').value=metadata.battery_power_mw;
  $('battery_duration_hours').value=metadata.battery_energy_mwh/metadata.battery_power_mw;
  $('starting_soc_fraction').value=metadata.starting_soc_mwh/metadata.battery_energy_mwh;
  $('round_trip_efficiency').value=metadata.round_trip_efficiency;
  $('degradation_cost_per_mwh').value=metadata.degradation_cost_per_mwh;
  $('capacity_contract_fraction').value=metadata.capacity_contract_mw/metadata.battery_power_mw;
  $('capacity_payment_per_kw_year').value=metadata.capacity_payment_per_kw_year;
  $('seed').value=metadata.seed;
  $('socLabel').textContent=`${Math.round(+$('starting_soc_fraction').value*100)}%`;
  $('capacityLabel').textContent=`${Math.round(+$('capacity_contract_fraction').value*100)}%`;
}

async function resumeSavedGame(){
  if(!window.gridvaultApi?.resumeLast) return;
  setBusy(true,'Restoring saved browser session…');
  try{
    const data=await window.gridvaultApi.resumeLast();
    applySavedSetup(data.metadata);
    state={...state,gameId:data.game_id,phase:data.phase,metadata:data.metadata,forecast:data.forecast||[],da:data.day_ahead_schedule||[],briefing:data.briefing||null,periods:data.periods||[],result:data.result||null,power:data.metadata.battery_power_mw};
    setSetupLocked(true);
    renderForecast(data);
    if(state.da.length){
      fillBidBookFromSchedule();
      lockBidBook();
      renderDa(data);
    }else{
      autoFillBidBook();
    }
    if(state.periods.length){ renderSettlementRows(); renderProgress(data.progress); }
    if(state.phase==='real_time') renderBriefing();
    if(state.phase==='complete' && state.result){ renderFinal(state.result); $('rtDesk').classList.remove('hidden'); }
    updatePhase(state.phase);
    $('status').textContent=`Saved day restored · ${state.periods.length}/48 RT periods settled`;
    refreshResumeButton();
  }catch(err){ $('status').textContent=err.message; }
  finally{ setBusy(false); }
}

function assetPayload(){
  return {
    seed:+$('seed').value,
    battery_power_mw:+$('battery_power_mw').value,
    battery_duration_hours:+$('battery_duration_hours').value,
    starting_soc_fraction:+$('starting_soc_fraction').value,
    battery_node:$('battery_node').value,
    degradation_cost_per_mwh:+$('degradation_cost_per_mwh').value,
    round_trip_efficiency:+$('round_trip_efficiency').value,
    capacity_contract_fraction:+$('capacity_contract_fraction').value,
    capacity_payment_per_kw_year:+$('capacity_payment_per_kw_year').value
  };
}

async function api(url, options={}){
  if(window.gridvaultApi?.request) return window.gridvaultApi.request(url,options);
  const res = await fetch(url,{headers:{'Content-Type':'application/json'},...options});
  const data = await res.json();
  if(!res.ok) throw new Error(data.detail || 'Request failed');
  return data;
}

async function startGame(){
  setBusy(true,'Building day-ahead information set…');
  try{
    const data=await api('/api/game/start',{method:'POST',body:JSON.stringify(assetPayload())});
    state={...state,gameId:data.game_id,phase:data.phase,metadata:data.metadata,forecast:data.forecast,da:[],briefing:null,periods:[],result:null,power:data.metadata.battery_power_mw};
    setSetupLocked(true);
    renderForecast(data);
    autoFillBidBook();
    updatePhase('forecast');
    $('status').textContent=`Scenario ${data.metadata.seed} · DA gate open`;
  }catch(err){ $('status').textContent=err.message; }
  finally{ setBusy(false); refreshResumeButton(); }
}

function setSetupLocked(locked){
  ['battery_node','battery_power_mw','battery_duration_hours','starting_soc_fraction','round_trip_efficiency','degradation_cost_per_mwh','capacity_contract_fraction','capacity_payment_per_kw_year','seed'].forEach(id=>$(id).disabled=locked);
  $('startButton').textContent=locked?'Start a new day':'Open trading day';
  if(locked) $('startButton').disabled=false;
}

function renderForecast(data){
  $('bidPanel').classList.remove('hidden');
  $('carbonPrice').textContent=`£${fmt(data.metadata.carbon_price_per_t,0)}/t`;
  $('gasIndex').textContent=fmt(data.metadata.gas_price_index,2);
  $('forecastRange').textContent=`£${fmt(data.forecast_summary.min_price,0)}–£${fmt(data.forecast_summary.max_price,0)}`;
  $('congestionWatch').textContent=`${data.forecast_summary.high_congestion_risk_periods} periods`;
  $('marketStats').textContent=`${data.forecast_summary.negative_periods} negative-price forecast periods · avg £${fmt(data.forecast_summary.average_price,0)}/MWh`;
  const notices=data.known_events||[];
  $('knownEvents').innerHTML=notices.length
    ? notices.map(e=>`<div class="known-event ${e.severity||'medium'}"><b>KNOWN NOTICE</b><span>${e.text}</span></div>`).join('')
    : '<div class="known-event neutral"><b>KNOWN NOTICE</b><span>No planned generation maintenance in today’s information set.</span></div>';
  $('bidRows').innerHTML=data.forecast.map((p,i)=>{
    return `<tr data-period="${i}">
      <td>${p.label}</td>
      <td class="price-fc">£${fmt(p.node_price_forecast,1)}</td>
      <td>£${fmt(p.dynamic_response_reference_price_per_mw_h,1)}<small>${fmt(p.dynamic_response_requirement_mw,0)} MW req.</small></td>
      <td>${fmt(p.total_demand_forecast_mw,0)} <small>${fmt(p.demand_low_mw,0)}–${fmt(p.demand_high_mw,0)}</small></td>
      <td>${fmt(p.renewable_forecast_mw,0)} <small>${fmt(p.renewable_low_mw,0)}–${fmt(p.renewable_high_mw,0)}</small></td>
      <td><span class="risk ${p.congestion_risk}">${p.congestion_risk}</span></td>
      <td><input class="bid-input charge-price" type="number" step="5"></td>
      <td><input class="bid-input charge-mw" type="number" min="0" max="${state.power}" step="10"></td>
      <td><input class="bid-input discharge-price" type="number" step="5"></td>
      <td><input class="bid-input discharge-mw" type="number" min="0" max="${state.power}" step="10"></td>
      <td><input class="bid-input holdback" type="number" min="0" max="90" value="0" step="5">%</td>
      <td><input class="bid-input reserve-price" type="number" min="0" max="5000" value="5" step="1"></td>
      <td><input class="bid-input dynamic-fraction" type="number" min="0" max="90" value="0" step="5">%</td>
      <td><input class="bid-input dynamic-price" type="number" min="0" max="5000" value="8" step="1"></td>
    </tr>`;
  }).join('');
  drawPriceChart();
  drawBatteryChart();
}

function percentile(values,p){
  const a=[...values].sort((x,y)=>x-y); const idx=(a.length-1)*p; const lo=Math.floor(idx),hi=Math.ceil(idx);
  return lo===hi?a[lo]:a[lo]*(hi-idx)+a[hi]*(idx-lo);
}

function autoFillBidBook(){
  if(!state.forecast.length) return;
  const prices=state.forecast.map(p=>p.node_price_forecast);
  const low=percentile(prices,.30), high=percentile(prices,.70);
  const baseCharge=Math.min(low+10,high-12);
  const baseDischarge=Math.max(high-10,baseCharge+12);
  [...$('bidRows').querySelectorAll('tr')].forEach((row,i)=>{
    const p=state.forecast[i];
    const charge=p.node_price_forecast<=low ? state.power : 0;
    const discharge=p.node_price_forecast>=high ? state.power : 0;
    row.querySelector('.charge-price').value=Math.round(baseCharge);
    row.querySelector('.charge-mw').value=Math.round(charge);
    row.querySelector('.discharge-price').value=Math.round(baseDischarge);
    row.querySelector('.discharge-mw').value=Math.round(discharge);
    row.querySelector('.holdback').value=(p.congestion_risk==='binding' && discharge>0)?10:20;
    row.querySelector('.reserve-price').value=5;
    row.querySelector('.dynamic-fraction').value=p.dynamic_response_reference_price_per_mw_h>=7?15:5;
    row.querySelector('.dynamic-price').value=Math.max(1,Math.round(p.dynamic_response_reference_price_per_mw_h-1));
  });
  $('status').textContent='Baseline bid book filled. Edit any half-hour before locking.';
}

function readBidBook(){
  return [...$('bidRows').querySelectorAll('tr')].map(row=>({
    max_charge_price_per_mwh:+row.querySelector('.charge-price').value,
    max_charge_mw:+row.querySelector('.charge-mw').value,
    min_discharge_price_per_mwh:+row.querySelector('.discharge-price').value,
    max_discharge_mw:+row.querySelector('.discharge-mw').value,
    reserve_holdback_fraction:+row.querySelector('.holdback').value/100,
    reserve_offer_price_per_mw_h:+row.querySelector('.reserve-price').value,
    dynamic_response_fraction:+row.querySelector('.dynamic-fraction').value/100,
    dynamic_response_offer_price_per_mw_h:+row.querySelector('.dynamic-price').value
  }));
}

async function submitDayAhead(){
  setBusy(true,'ISO clearing your day-ahead book…');
  try{
    const data=await api(`/api/game/${state.gameId}/day-ahead`,{method:'POST',body:JSON.stringify({bids:readBidBook()})});
    state.phase=data.phase; state.da=data.day_ahead_schedule; state.briefing=data.briefing;
    lockBidBook(); renderDa(data); renderBriefing(); updatePhase('real_time');
    $('status').textContent='DA positions locked · real-time desk open';
  }catch(err){ $('status').textContent=err.message; }
  finally{ setBusy(false); }
}

function fillBidBookFromSchedule(){
  [...$('bidRows').querySelectorAll('tr')].forEach((row,i)=>{
    const b=state.da[i]?.bid; if(!b) return;
    row.querySelector('.charge-price').value=b.max_charge_price_per_mwh;
    row.querySelector('.charge-mw').value=b.max_charge_mw ?? state.power;
    row.querySelector('.discharge-price').value=b.min_discharge_price_per_mwh;
    row.querySelector('.discharge-mw').value=b.max_discharge_mw ?? state.power;
    row.querySelector('.holdback').value=(b.reserve_holdback_fraction||0)*100;
    row.querySelector('.reserve-price').value=b.reserve_offer_price_per_mw_h ?? 5;
    row.querySelector('.dynamic-fraction').value=(b.dynamic_response_fraction||0)*100;
    row.querySelector('.dynamic-price').value=b.dynamic_response_offer_price_per_mw_h ?? 8;
  });
}

function lockBidBook(){
  $('bidPanel').querySelectorAll('input').forEach(x=>x.disabled=true);
  $('autoBidButton').disabled=true; $('submitDaButton').disabled=true;
}

function renderDa(data){
  $('daPanel').classList.remove('hidden');
  $('rtDesk').classList.remove('hidden');
  $('daSummary').textContent=`${data.day_ahead_summary.charge_periods} charge · ${data.day_ahead_summary.discharge_periods} discharge · reserve ${data.day_ahead_summary.battery_reserve_periods} periods · dynamic response ${data.day_ahead_summary.dynamic_response_periods} periods · ${data.day_ahead_summary.thermal_startups} thermal starts · uplift ${fmtMoney(data.day_ahead_summary.system_uplift)}`;
  $('daRows').innerHTML=state.da.map(p=>`<tr>
    <td>${p.label}</td><td>£${fmt(p.price,1)}</td><td>${fmt(p.charge_mw,1)} MW</td><td>${fmt(p.discharge_mw,1)} MW</td>
    <td class="${p.net_mw>=0?'positive':'negative'}">${signed(p.net_mw,1)} MW</td><td>${fmt(p.soc_end_mwh,1)} MWh</td>
    <td>${p.startup_units?.length ? p.startup_units.join(', ') : '—'}</td><td>${fmt(p.battery_reserve_mw,1)} MW</td><td>${fmt(p.reserve_requirement_mw,0)} MW</td><td>£${fmt(p.reserve_price_per_mw_h,1)}</td><td>${fmt(p.dynamic_response_mw,1)} MW</td><td>£${fmt(p.dynamic_response_price_per_mw_h,1)}</td>
  </tr>`).join('');
  drawPriceChart(); drawBatteryChart();
}

function renderBriefing(){
  const b=state.briefing; if(!b) return;
  $('rtTime').textContent=b.label;
  $('rtProgress').textContent=`Period ${b.period+1} of 48`;
  $('indicativePrice').textContent=`~£${fmt(b.indicative_price,0)}`;
  $('briefSoc').textContent=`${fmt(b.soc_mwh,1)} MWh`;
  $('briefDaPosition').textContent=b.day_ahead_net_mw>0.05?`Sell ${fmt(b.day_ahead_net_mw,0)} MW`:b.day_ahead_net_mw<-0.05?`Buy ${fmt(-b.day_ahead_net_mw,0)} MW`:'Flat';
  $('briefDaPrice').textContent=`£${fmt(b.day_ahead_price,1)}`;
  $('briefCongestion').textContent=b.congestion_risk;
  $('briefCongestion').className=`risk ${b.congestion_risk}`;
  $('demandRevision').textContent=`${signed(b.demand_revision_pct,1)}%`;
  $('renewRevision').textContent=`${signed(b.renewable_revision_pct,1)}%`;
  $('reserveRequirement').textContent=`${fmt(b.reserve_requirement_mw,0)} MW`;
  $('reservePrice').textContent=`~£${fmt(b.indicative_reserve_price_per_mw_h,1)}/MW/h`;
  $('dynamicResponseInfo').textContent=b.dynamic_response_mw>0.05 ? `${fmt(b.dynamic_response_mw,0)} MW @ £${fmt(b.dynamic_response_price_per_mw_h,1)}/MW/h` : 'not awarded';
  $('startupInfo').textContent=b.startup_units?.length ? b.startup_units.join(', ') : 'none';
  $('capacityInfo').textContent=b.capacity_contract_mw>0 ? `${fmt(b.capacity_contract_mw,0)} MW${b.capacity_stress_watch?' · STRESS WATCH':''}` : 'none';
  const outlook=b.lookahead||[];
  $('lookaheadMeta').textContent=`${fmt(b.rt_lookahead_hours||outlook.length*.5,1)}h · continuation £${fmt(b.lookahead_continuation_price,0)}`;
  $('lookaheadStrip').innerHTML=outlook.length ? outlook.map((x,i)=>{
    const net=x.battery_discharge_mw-x.battery_charge_mw;
    const action=net>0.05?`+${fmt(net,0)} MW`:net<-0.05?`${fmt(net,0)} MW`:'hold';
    const cls=i===0?' current':'';
    const dyn=x.dynamic_response_mw>0.05?` · dyn ${fmt(x.dynamic_response_mw,0)}`:'';
    return `<div class="lookahead-card${cls}"><span>${x.label}</span><b>£${fmt(x.price,0)}</b><small>${action} · SOC ${fmt(x.projected_soc_mwh,0)}${dyn}</small><em class="risk ${x.congestion_risk}">${x.congestion_risk}</em></div>`;
  }).join('') : '<div class="lookahead-empty">No remaining look-ahead periods.</div>';
  const commitmentAlerts=[...b.alerts];
  if(b.startup_units?.length) commitmentAlerts.unshift({severity:'low',text:`DA commitment start: ${b.startup_units.join(', ')}`});
  if(b.reserve_shortfall_mw>0.1) commitmentAlerts.unshift({severity:'high',text:`DA reserve shortfall ${fmt(b.reserve_shortfall_mw,0)} MW`});
  $('alerts').innerHTML=commitmentAlerts.map(a=>`<div class="alert ${a.severity}">${a.text}</div>`).join('');
  useCurrentDaBid();
  $('marketClock').textContent=b.label;
}

function useCurrentDaBid(){
  if(!state.briefing || !state.da.length) return;
  const d=state.da[state.briefing.period]; const b=d.bid;
  $('rtChargePrice').value=Math.round(b.max_charge_price_per_mwh);
  $('rtDischargePrice').value=Math.round(b.min_discharge_price_per_mwh);
  $('rtChargeMw').value=Math.round(b.max_charge_mw ?? state.power);
  $('rtDischargeMw').value=Math.round(b.max_discharge_mw ?? state.power);
  $('rtHoldback').value=b.reserve_holdback_fraction || 0;
  $('rtReservePrice').value=b.reserve_offer_price_per_mw_h ?? 5;
  $('holdbackLabel').textContent=`${Math.round((b.reserve_holdback_fraction||0)*100)}%`;
}

function rtBidPayload(){
  let cp=+$('rtChargePrice').value, dp=+$('rtDischargePrice').value;
  if(cp>=dp) dp=cp+1;
  return {bid:{
    max_charge_price_per_mwh:cp,
    min_discharge_price_per_mwh:dp,
    max_charge_mw:+$('rtChargeMw').value,
    max_discharge_mw:+$('rtDischargeMw').value,
    reserve_holdback_fraction:+$('rtHoldback').value,
    reserve_offer_price_per_mw_h:+$('rtReservePrice').value,
    dynamic_response_fraction:0,
    dynamic_response_offer_price_per_mw_h:0
  }};
}

async function clearRtPeriod(silent=false){
  if(!silent) setBusy(true,`Clearing ${state.briefing?.label || 'RT'}…`);
  try{
    const data=await api(`/api/game/${state.gameId}/real-time`,{method:'POST',body:JSON.stringify(rtBidPayload())});
    state.phase=data.phase; state.periods.push(data.cleared_period); state.briefing=data.briefing;
    renderProgress(data.progress); renderSettlementRows(); drawPriceChart(); drawBatteryChart();
    if(data.phase==='complete'){
      state.result=data.result; renderFinal(data.result); updatePhase('complete');
      $('status').textContent='Trading day complete · IC scorecard ready';
    } else {
      renderBriefing();
      if(!silent) $('status').textContent=`${data.cleared_period.label} settled at £${fmt(data.cleared_period.real_time_prices[state.metadata.battery_node],1)}/MWh`;
    }
    return data;
  }catch(err){ $('status').textContent=err.message; throw err; }
  finally{ if(!silent) setBusy(false); }
}

async function autoFinish(){
  if(state.autoFinishing || state.phase!=='real_time') return;
  state.autoFinishing=true; setBusy(true,'Settling remaining periods using the locked DA strategy…');
  try{
    while(state.phase==='real_time'){
      useCurrentDaBid();
      await clearRtPeriod(true);
    }
  }catch(err){ /* status already set */ }
  finally{ state.autoFinishing=false; setBusy(false); }
}

function renderProgress(p){
  $('cashPnl').textContent=fmtMoney(p.cash_pnl);
  $('cashPnl').className=p.cash_pnl>=0?'positive':'negative';
  $('cashSub').textContent=`${p.periods_cleared}/48 settled · reserve ${fmtMoney(p.reserve_settlement||0)} · dynamic ${fmtMoney(p.dynamic_response_revenue||0)}${p.capacity_net ? ` · capacity ${fmtMoney(p.capacity_net)}` : ''}`;
  $('cycles').textContent=fmt(p.equivalent_cycles,2);
  $('finalSoc').textContent=`SOC ${fmt(p.soc_mwh,1)} MWh`;
}

function renderSettlementRows(){
  if(!state.periods.length){ $('periodRows').innerHTML='<tr><td colspan="12" class="empty">No real-time periods have cleared.</td></tr>'; return; }
  $('periodRows').innerHTML=state.periods.map(p=>{
    const node=state.metadata.battery_node;
    const d=p.battery_rt_discharge_mw-p.battery_rt_charge_mw;
    const caps=p.line_capacities_mw||state.metadata.line_capacities_mw;
    const congested=Object.entries(p.line_flows_mw).some(([k,v])=>Math.abs(v)>=(caps[k]||Infinity)*0.97);
    const dtext=d>0.05?`+${fmt(d)} MW discharge`:d<-0.05?`${fmt(d)} MW charge`:'hold';
    const reserveText=p.battery_rt_reserve_mw>0.05?` · ${fmt(p.battery_rt_reserve_mw,0)} MW reserve`:'';
    const dyn=p.dynamic_response_mw>0.05?`${fmt(p.dynamic_response_mw,0)} MW @ £${fmt(p.dynamic_response_price_per_mw_h,1)}<small>${fmtMoney(p.dynamic_response_revenue)}</small>`:'—';
    return `<tr><td>${p.label}</td><td>£${fmt(p.day_ahead_prices[node],1)}</td><td>£${fmt(p.real_time_prices[node],1)}</td><td>£${fmt(p.reserve_price_per_mw_h,1)}</td><td>${dyn}</td><td>${dtext}${reserveText}</td><td>${fmt(p.soc_end_mwh,1)} MWh</td><td>${fmtMoney(p.da_settlement+p.da_reserve_settlement)}</td><td class="${p.rt_deviation_settlement+p.rt_reserve_deviation_settlement>=0?'positive':'negative'}">${fmtMoney(p.rt_deviation_settlement+p.rt_reserve_deviation_settlement)}</td><td class="negative">${fmtMoney(-p.degradation_cost)}</td><td class="${p.net_cashflow>=0?'positive':'negative'}">${fmtMoney(p.net_cashflow)}</td><td>${congested?'<span class="warn">CONGESTED</span>':'normal'}</td></tr>`;
  }).join('');
  const wrap=document.querySelector('.settlement-wrap'); wrap.scrollTop=wrap.scrollHeight;
}

function renderFinal(r){
  $('resultPanels').classList.remove('hidden');
  $('netPnl').textContent=fmtMoney(r.economic_pnl); $('netPnl').className=r.economic_pnl>=0?'positive':'negative';
  $('pnlSub').textContent=`includes ${fmtMoney(r.inventory_mark)} terminal SOC mark`;
  $('cashPnl').textContent=fmtMoney(r.progress.cash_pnl);
  $('capture').textContent=r.opportunity_capture==null?'n/a':`${fmt(r.opportunity_capture*100,0)}%`;
  $('captureSub').textContent=`oracle ${fmtMoney(r.perfect_foresight.economic_pnl)}`;
  const a=r.attribution;
  const energyWear=(a.degradation||0)-(a.dynamic_response_degradation||0);
  $('attribution').innerHTML=[
    ['Day-ahead energy',a.day_ahead_settlement],['RT energy deviation',a.real_time_deviation],['Day-ahead reserve',a.day_ahead_reserve],['RT reserve deviation',a.real_time_reserve_deviation],['Dynamic response',a.dynamic_response_revenue],['Dynamic response wear',a.dynamic_response_degradation],['Energy degradation',energyWear],['Capacity revenue',a.capacity_revenue],['Capacity penalties',a.capacity_penalties],['Terminal inventory',a.inventory_mark],['Congestion value*',a.congestion_value]
  ].map(([name,v])=>`<div><span>${name}</span><b class="${v>=0?'positive':'negative'}">${fmtMoney(v)}</b></div>`).join('')+
    `<p class="muted mini">*Congestion value is the RT physical dispatch value attributable to your local LMP versus the simple mean nodal price.</p>`;
  const score=r.opportunity_capture==null?'Unscored':r.opportunity_capture>=.8?'Excellent':r.opportunity_capture>=.6?'Strong':r.opportunity_capture>=.4?'Mixed':'Needs work';
  $('scorecard').innerHTML=`
    <div class="score-big"><span>Energy opportunity capture</span><strong>${r.opportunity_capture==null?'—':fmt(r.opportunity_capture*100,0)+'%'}</strong><em>${score}</em></div>
    <div class="score-row"><span>Perfect-foresight economic P&L</span><b>${fmtMoney(r.perfect_foresight.economic_pnl)}</b></div>
    <div class="score-row"><span>DA vs RT price MAE</span><b>£${fmt(a.price_forecast_mae,1)}/MWh</b></div>
    <div class="score-row"><span>Congested periods</span><b>${a.congested_periods} / 48</b></div>
    <div class="score-row"><span>Reserve scarcity periods</span><b>${a.reserve_scarcity_periods} / 48</b></div>
    <div class="score-row"><span>Dynamic response awards</span><b>${a.dynamic_response_awarded_periods} / 48 · ${fmt(a.dynamic_response_throughput_mwh,1)} MWh wear-throughput</b></div>
    <div class="score-row"><span>Capacity calls</span><b>${a.capacity_calls} · shortfall ${fmt(a.capacity_shortfall_mwh,1)} MWh</b></div>
    <div class="score-row"><span>Average reserve price</span><b>£${fmt(a.average_reserve_price_per_mw_h,1)}/MW/h</b></div>
    <div class="score-row"><span>DA system uplift*</span><b>${fmtMoney(r.market_context?.day_ahead_uplift?.total_uplift||0)}</b></div>
    <div class="score-row"><span>Final SOC</span><b>${fmt(r.progress.soc_mwh,1)} MWh</b></div>
    <p class="muted mini">*System uplift is offer-based generator make-whole for DA startup/no-load non-convexities; it is market context and is not allocated to your BESS in this build. Benchmark uses a no-player-dispatch RT price path under the cleared DA commitment.</p>`;
  drawPriceChart(); drawBatteryChart();
}

function updatePhase(phase){
  state.phase=phase;
  const map={
    setup:['SETUP','Asset setup','Choose your mandate and open the trading day.','T−1'],
    forecast:['DAY-AHEAD','Build the day-ahead position','You have forecasts and congestion risk, but not realised conditions. Submit a 48-period bid book.','DA'],
    real_time:['REAL-TIME','Operate through real time','The DA financial position is locked. Use updated system information to rebid physical charge/discharge each half-hour.',state.briefing?.label||'RT'],
    complete:['CLOSED','Trading day complete','Review attribution, benchmark performance and what the market did around your asset.','EOD']
  };
  const [pill,title,text,clock]=map[phase]; $('phasePill').textContent=pill; $('phaseTitle').textContent=title; $('phaseText').textContent=text; $('marketClock').textContent=clock;
}

function setBusy(busy,text){
  if(text) $('status').textContent=text;
  ['startButton','resumeButton','submitDaButton','clearPeriodButton','autoFinishButton','useDaBidButton'].forEach(id=>{ if($(id)) $(id).disabled=busy; });
}

function drawPriceChart(){
  const forecast=state.forecast.map(x=>x.node_price_forecast);
  const da=state.da.map(x=>x.price);
  const rt=state.periods.map(x=>x.real_time_prices[state.metadata?.battery_node || 'Central']);
  const series=[];
  if(forecast.length) series.push({values:forecast,cls:'forecast'});
  if(da.length) series.push({values:da,cls:'da'});
  if(rt.length) series.push({values:rt,cls:'rt',partial:true});
  drawLineChart($('priceChart'),series);
  if(state.metadata) $('priceChartSub').textContent=`${state.metadata.battery_node} node · forecast, cleared DA and revealed RT`;
}

function drawBatteryChart(){
  const energy=state.metadata?.battery_energy_mwh || (+$('battery_power_mw').value * +$('battery_duration_hours').value);
  const starting=state.metadata?.starting_soc_mwh ?? energy*(+$('starting_soc_fraction').value);
  const da=[starting,...state.da.map(x=>x.soc_end_mwh)];
  const rt=[starting,...state.periods.map(x=>x.soc_end_mwh)];
  drawSocChart($('batteryChart'),da,rt,energy);
}

function drawLineChart(svg,series){
  if(!series.length){svg.innerHTML='<text x="30" y="150" fill="#7890a2" font-size="15">Open a trading day to load market intelligence.</text>';return;}
  const w=900,h=300,pad=38;
  const vals=series.flatMap(s=>s.values); let min=Math.min(...vals,0),max=Math.max(...vals,0); if(max===min)max=min+1;
  const n=Math.max(...series.map(s=>s.values.length),48); const x=i=>pad+i*(w-2*pad)/(n-1); const y=v=>h-pad-(v-min)*(h-2*pad)/(max-min);
  const colors={forecast:'#8c9dab',da:'#72b7ff',rt:'#79e0b9'}; let html='';
  for(let j=0;j<5;j++){const yy=pad+j*(h-2*pad)/4; html+=`<line x1="${pad}" y1="${yy}" x2="${w-pad}" y2="${yy}" stroke="#1f2e39"/>`;}
  if(min<0&&max>0)html+=`<line x1="${pad}" y1="${y(0)}" x2="${w-pad}" y2="${y(0)}" stroke="#644950"/>`;
  series.forEach(s=>{ if(s.values.length<2)return; const pts=s.values.map((v,i)=>`${x(i)},${y(v)}`).join(' '); html+=`<polyline points="${pts}" fill="none" stroke="${colors[s.cls]}" stroke-width="${s.cls==='forecast'?1.5:2.5}" ${s.cls==='forecast'?'stroke-dasharray="5 5"':''} vector-effect="non-scaling-stroke"/>`; });
  html+=`<text x="7" y="${pad+3}" fill="#8fa3b4" font-size="11">£${fmt(max,0)}</text><text x="7" y="${h-pad}" fill="#8fa3b4" font-size="11">£${fmt(min,0)}</text>`; svg.innerHTML=html;
}

function drawSocChart(svg,da,rt,energy){
  const w=900,h=300,pad=38,n=49; const x=i=>pad+i*(w-2*pad)/(n-1),y=v=>h-pad-v*(h-2*pad)/energy; let html='';
  for(let j=0;j<5;j++){const yy=pad+j*(h-2*pad)/4; html+=`<line x1="${pad}" y1="${yy}" x2="${w-pad}" y2="${yy}" stroke="#1f2e39"/>`;}
  if(da.length>1){const pts=da.map((v,i)=>`${x(i)},${y(v)}`).join(' ');html+=`<polyline points="${pts}" fill="none" stroke="#f0c36a" stroke-width="1.8" stroke-dasharray="5 4" vector-effect="non-scaling-stroke"/>`;}
  if(rt.length>1){const pts=rt.map((v,i)=>`${x(i)},${y(v)}`).join(' ');html+=`<polyline points="${pts}" fill="none" stroke="#edf4f7" stroke-width="2.4" vector-effect="non-scaling-stroke"/>`;}
  html+=`<text x="7" y="${pad+3}" fill="#8fa3b4" font-size="11">${fmt(energy,0)} MWh</text><text x="7" y="${h-pad}" fill="#8fa3b4" font-size="11">0</text>`;svg.innerHTML=html;
}

drawPriceChart(); drawBatteryChart(); updatePhase('setup'); refreshResumeButton();
window.addEventListener('gridvault-save-updated',refreshResumeButton);
