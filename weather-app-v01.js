const el = id => document.getElementById(id);
const HOUR = 3600;
const clock = new Intl.DateTimeFormat("it-IT", {timeZone:"Europe/Rome", hour:"2-digit", minute:"2-digit"});
const dateHour = new Intl.DateTimeFormat("it-IT", {timeZone:"Europe/Rome", day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit", timeZoneName:"short"});
const percent = new Intl.NumberFormat("it-IT", {style:"percent", minimumFractionDigits:1, maximumFractionDigits:1});
let lat=42.5, lon=12.5, activeModels=[], forecastRows=[], generation=0, pending, config;
let map, marker;
if (window.L) {
  map=L.map("map").setView([lat,lon],6);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{attribution:"© OpenStreetMap contributors",maxZoom:18}).addTo(map);
  marker=L.marker([lat,lon]).addTo(map);
  map.on("click",e=>select(e.latlng));
} else {
  el("map").hidden=true;
}

function resetForecast() {
  activeModels=[]; forecastRows=[];
  el("hourSelect").replaceChildren(new Option("Calcola prima una previsione",""));
  el("hourSelect").disabled=true;
  for (const id of ["temp","rain","wind","trange","prange","wrange","enduroVotes","enduroConsensus","enduroProbability"]) el(id).textContent="—";
  for (const id of ["comparison","hours","charts","agreement","modelErrors","enduroSource"]) el(id).replaceChildren();
  el("models").textContent="In attesa dei modelli";
  el("enduroInterval").textContent="Calcola la previsione per questa posizione.";
  el("updated").textContent="In attesa del calcolo per questa posizione.";
}
function select(p) {
  generation++; pending?.abort();
  lat=p.lat; lon=p.lng; marker?.setLatLng(p);
  resetForecast(); el("run").disabled=false;
  el("location").textContent="Lat "+lat.toFixed(4)+" · Lon "+lon.toFixed(4);
  el("status").textContent="Posizione selezionata. Premi Calcola previsione.";
}
async function fetchJSON(url, signal) {
  const response=await fetch(url,{signal,cache:"no-store"});
  if (!response.ok) throw Error("HTTP "+response.status);
  return response.json();
}
async function getModel(model, latitude, longitude, signal) {
  const q=new URLSearchParams({latitude,longitude,hourly:"temperature_2m,precipitation,wind_speed_10m",models:model,
    forecast_days: "3", timezone:"GMT", timeformat:"unixtime", wind_speed_unit:"kmh"});
  const data=await fetchJSON("https://api.open-meteo.com/v1/forecast?"+q,signal);
  const hourly=data.hourly;
  if (!Array.isArray(hourly?.time) || !hourly.time.length) throw Error("Serie oraria assente");
  const points=new Map();
  hourly.time.forEach((time,i)=>{
    const mm=hourly.precipitation?.[i];
    points.set(time,{t:hourly.temperature_2m?.[i],p:Number.isFinite(mm)&&mm>=0?mm:null,w:hourly.wind_speed_10m?.[i]});
  });
  return {model,points};
}
const cachePrefix="enduro:rain:0.1:";
function readHistory(key) {
  try {return JSON.parse(localStorage.getItem(key)) || {};} catch {return {};}
}
function saveHistory(key, records, now) {
  try {
    for (const k of Object.keys(localStorage)) {
      if (k.startsWith(cachePrefix) && !Object.keys(readHistory(k)).some(t=>Number(t)>now)) localStorage.removeItem(k);
    }
    const merged=readHistory(key);
    for (const [end,entry] of Object.entries(records)) {
      if (!merged[end] || merged[end].at<entry.at) merged[end]=entry;
    }
    localStorage.setItem(key,JSON.stringify(Object.fromEntries(Object.entries(merged).filter(([end])=>Number(end)>now))));
  } catch { /* Rendering remains available with browser storage disabled. */ }
}
function eligible(at,start) {return Number.isFinite(at) && at<start && start-at<=24*HOUR;}
function probability(votes) {return (config.wet[votes]+1)/(config.total[votes]+2);}
function renderHour(time) {
  const row=forecastRows.find(r=>r.time===time);
  if (!row) return;
  const {values,signal}=row;
  const v=signal?.votes;
  el("enduroConsensus").textContent=signal?(v>=5?"Pioggia":"Asciutto"):"Dati insufficienti";
  el("enduroVotes").textContent=signal?v+"/8":"—";
  el("enduroProbability").textContent=signal?percent.format(probability(v)):"—";
  el("enduroInterval").textContent="Da "+dateHour.format((time-HOUR)*1000)+" a "+dateHour.format(time*1000)+".";
  el("enduroSource").textContent=signal?(row.fallback?"Ultimo calcolo completo salvato":"Dati acquisiti")+" il "+dateHour.format(signal.at*1000)+"."
    :row.n+"/8 previsioni di pioggia valide. Servono otto modelli dello stesso calcolo.";
  el("temp").textContent=format(row.t)+"°";el("rain").textContent=format(row.p)+" mm";el("wind").textContent=format(row.w,0);
  for (const [id,k,unit] of [["trange","t","°"],["prange","p"," mm"],["wrange","w"," km/h"]]) el(id).textContent=range(values.map(a=>a?.[k]),unit);
  el("agreement").textContent="Minimo e massimo previsti nell’ultimo calcolo per l’ora selezionata.";
  el("comparison").replaceChildren();
  config.models.forEach((m,i)=>{const a=values[i];addRow(el("comparison"),a?[m,format(a.t),format(a.p),format(a.w,0)]:[m,"Non disponibile","—","—"]);});
}
async function calculate() {
  pending?.abort();
  const request=++generation, ctl=new AbortController();pending=ctl;
  const timeout=setTimeout(()=>ctl.abort(),30000);
  const latitude=lat, longitude=lon, key=cachePrefix+lat+","+lon;
  resetForecast();el("run").disabled=true;el("status").textContent="Caricamento delle previsioni…";
  try {
    if (!config) config=await fetchJSON("./rain-v01.json",ctl.signal);
    if (request!==generation) return;
    const results=await Promise.allSettled(config.models.map(m=>getModel(m,latitude,longitude,ctl.signal)));
    if (request!==generation) return;
    const at=Date.now()/1000;
    const history=readHistory(key), additions={};
    const firstStart=(Math.floor(at/HOUR)+1)*HOUR;
    activeModels=results.map(x=>x.status==="fulfilled"?x.value:null);
    el("modelErrors").textContent=results.flatMap((x,i)=>x.status==="rejected"?[config.models[i]+" · "+String(x.reason?.message??"indisponibile")]:[]).join(" | ");
    for (let i=0;i<24;i++) {
      const start=firstStart+i*HOUR, time=start+HOUR;
      const values=activeModels.map(m=>m?.points.get(time));
      const rain=values.map(a=>a?.p).filter(Number.isFinite);
      let signal=null, fallback=false;
      if (rain.length===8 && eligible(at,start)) {
        signal={votes:rain.filter(p=>p>=config.threshold_mm).length,at}; additions[time]=signal;
      } else {
        const old=history[time];
        if (old && Number.isInteger(old.votes) && old.votes>=0 && old.votes<=8 && old.at<=at && eligible(old.at,start)) {
          signal=old; fallback=true;
        }
      }
      forecastRows.push({time,values,signal,fallback,n:rain.length,t:average(values.map(a=>a?.t)),p:average(rain),w:average(values.map(a=>a?.w))});
    }
    if (!activeModels.some(Boolean) && !forecastRows.some(r=>r.signal)) throw Error("Nessun modello disponibile. Riprova tra poco.");
    saveHistory(key,additions,at);
    const choices=el("hourSelect");choices.replaceChildren();
    for (const r of forecastRows) {
      choices.add(new Option(dateHour.format((r.time-HOUR)*1000)+" – "+clock.format(r.time*1000),String(r.time)));
      addRow(el("hours"),[dateHour.format(r.time*1000),format(r.t),format(r.p),format(r.w,0),
        r.signal?(r.signal.votes>=5?"Pioggia":"Asciutto")+(r.fallback?" · salvato":""):"Dati insufficienti",r.signal?percent.format(probability(r.signal.votes)):"—"]);
    }
    choices.disabled=false;renderHour(forecastRows[0].time);renderCharts();
    el("models").textContent=activeModels.filter(Boolean).length+"/8 modelli disponibili nell’ultimo calcolo";
    el("status").textContent="Previsione elaborata per la posizione selezionata.";
    el("updated").textContent="Ultimo calcolo sul dispositivo · "+dateHour.format(at*1000);
  } catch(err) {
    if(request===generation) el("status").textContent="Previsione non disponibile. "+(err.message??"Riprova.");
  } finally {
    clearTimeout(timeout);
    if(request===generation) el("run").disabled=false;
  }
}
select({lat,lng:lon});
el("locate").addEventListener("click",locateMe);
el("run").addEventListener("click",calculate);
el("hourSelect").addEventListener("change",e=>renderHour(Number(e.target.value)));
function locateMe(){const button=el("locate");if(!navigator.geolocation){el("status").textContent="Geolocalizzazione non supportata dal browser.";return}button.disabled=true;el("status").textContent="Ricerca della posizione GPS...";navigator.geolocation.getCurrentPosition(position=>{const p={lat:position.coords.latitude,lng:position.coords.longitude};select(p);map?.setView([p.lat,p.lng],14);el("status").textContent="Posizione GPS rilevata (precisione circa "+Math.round(position.coords.accuracy)+" m). Premi Calcola previsione.";button.disabled=false},error=>{const messages={1:"Permesso posizione negato. Abilita la localizzazione per il browser nelle impostazioni iPhone.",2:"Posizione non disponibile. Controlla GPS e rete.",3:"Tempo scaduto nella ricerca GPS. Riprova."};el("status").textContent=messages[error.code]||"Impossibile ottenere la posizione.";button.disabled=false},{enableHighAccuracy:true,timeout:15000,maximumAge:60000})}
function average(values){const v=values.filter(Number.isFinite);return v.length?v.reduce((a,b)=>a+b,0)/v.length:null}function format(n,d=1){return Number.isFinite(n)?n.toFixed(d):"—"}function range(values,unit){const v=values.filter(Number.isFinite);return v.length?format(Math.min(...v))+"–"+format(Math.max(...v))+unit:"—"}function addRow(body,values){const tr=document.createElement("tr");for(const value of values){const td=document.createElement("td");td.textContent=value;tr.appendChild(td)}body.appendChild(tr)}
function makeChart(title,key,unit,color){const rows=forecastRows.filter(r=>Number.isFinite(r[key]));const wrap=document.createElement("div");wrap.className="chartWrap";const heading=document.createElement("div");heading.className="chartTitle";heading.textContent=title;wrap.appendChild(heading);if(rows.length<2){const p=document.createElement("p");p.className="empty";p.textContent="Dati insufficienti";wrap.appendChild(p);return wrap}const values=rows.map(r=>r[key]);let lo=Math.min(...values),hi=Math.max(...values);if(hi===lo){lo-=1;hi+=1}const pad=(hi-lo)*.12;lo-=pad;hi+=pad;const w=680,h=180,left=48,right=12,top=16,bottom=32,plotW=w-left-right,plotH=h-top-bottom;const x=i=>left+i*plotW/(rows.length-1),y=v=>top+(hi-v)/(hi-lo)*plotH;const ns="http://www.w3.org/2000/svg";const svg=document.createElementNS(ns,"svg");svg.setAttribute("viewBox",`0 0 ${w} ${h}`);svg.setAttribute("class","chart");svg.setAttribute("role","img");svg.setAttribute("aria-label",title+" nelle prossime 24 ore");function node(name,attrs,txt){const n=document.createElementNS(ns,name);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,String(v));if(txt!==undefined)n.textContent=txt;svg.appendChild(n);return n}for(let i=0;i<=3;i++){const val=lo+(hi-lo)*i/3;node("line",{x1:left,x2:w-right,y1:y(val),y2:y(val),stroke:"#526563","stroke-width":.7});node("text",{x:4,y:y(val)+4,fill:"#bed0cc","font-size":11},format(val,1))}const points=rows.map((r,i)=>`${x(i)},${y(r[key])}`).join(" ");node("polyline",{points,fill:"none",stroke:color,"stroke-width":3,"stroke-linejoin":"round","stroke-linecap":"round"});for(const i of [0,6,12,18,23]){if(i>=rows.length)continue;node("text",{x:x(i),y:h-8,fill:"#bed0cc","font-size":11,"text-anchor":i===0?"start":i===rows.length-1?"end":"middle"},clock.format(rows[i].time*1000))}wrap.appendChild(svg);const legend=document.createElement("div");legend.className="legend";legend.textContent=`Min ${format(Math.min(...values))} ${unit} · Max ${format(Math.max(...values))} ${unit} · Orari italiani`;wrap.appendChild(legend);return wrap}
function renderCharts(){const c=el("charts");c.innerHTML="";c.appendChild(makeChart("Temperatura","t","°C","#ffb85c"));c.appendChild(makeChart("Precipitazioni","p","mm","#69bfff"));c.appendChild(makeChart("Vento","w","km/h","#63d4b3"))}
async function loadProjectStatus(){const status=el("projectStatus"),releases=el("projectReleases");status.textContent="Controllo esecuzioni GitHub...";releases.textContent="";const root="https://api.github.com/repos/lcricc/enduro-weather-engine";try{const paths=["meteohub_collect.yml","enduro_validation.yml"];const results=await Promise.allSettled(paths.map(async name=>{const response=await fetch(root+"/actions/workflows/"+name+"/runs?per_page=1",{cache:"no-store"});if(!response.ok)throw Error("HTTP "+response.status);const data=await response.json();return{name,run:data.workflow_runs?.[0]}}));status.replaceChildren();for(let i=0;i<results.length;i++){const line=document.createElement("p"),result=results[i],label=i===0?"Raccolta MeteoHub":"Validazione scientifica";if(result.status==="fulfilled"&&result.value.run){const run=result.value.run;line.append(document.createTextNode(label+": "+(run.conclusion||run.status)+" · "+new Intl.DateTimeFormat("it-IT",{timeZone:"Europe/Rome",dateStyle:"short",timeStyle:"short"}).format(new Date(run.created_at))+" · "));const a=document.createElement("a");a.href=run.html_url;a.textContent="Vedi esecuzione";a.target="_blank";a.rel="noopener noreferrer";a.style.color="#9ed9ce";line.append(a)}else line.textContent=label+": stato non disponibile";status.append(line)}const resp=await fetch(root+"/releases?per_page=30",{cache:"no-store"});if(!resp.ok)throw Error("Releases HTTP "+resp.status);const data=await resp.json();const groups=[["Osservazioni MeteoHub","meteohub-observations-"],["Previsioni storiche","meteohub-forecasts-"]];for(const[label,prefix]of groups){const item=data.find(x=>x.tag_name?.startsWith(prefix));const p=document.createElement("p");if(item){p.append(document.createTextNode(label+": archivio "+new Intl.DateTimeFormat("it-IT",{timeZone:"Europe/Rome",dateStyle:"short",timeStyle:"short"}).format(new Date(item.published_at||item.created_at))+" · "));const a=document.createElement("a");a.href=item.html_url;a.textContent="Apri archivio";a.target="_blank";a.rel="noopener noreferrer";a.style.color="#9ed9ce";p.append(a)}else p.textContent=label+": nessun archivio trovato nelle ultime 30 releases";releases.append(p)}const p=document.createElement("p");p.textContent="Il report di validazione include enduro_rain_v01.json con gli esiti delle previsioni ENDURO archiviate prima dell’ora osservata.";releases.append(p)}catch(e){releases.textContent="Impossibile consultare gli archivi GitHub: "+e.message+". I dati non vengono sostituiti con valori simulati."}}
el("refreshProject").addEventListener("click",loadProjectStatus);loadProjectStatus();
