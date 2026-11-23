// Everything that is not written by this page (titles, IDs, versions, file names, log lines, messages) goes through esc()
// before it is put into HTML, numbers through num(), and class names through cls(). No handler is written inline: the
// buttons carry data- attributes and the clicks are handled at the bottom of the page (the CSP forbids inline script).
const $=s=>document.querySelector(s), esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num=v=>Number.isFinite(Number(v))?Number(v):0, cls=v=>String(v??"").replace(/[^\w-]/g,"");
const README="https://github.com/zhangchi198/ps5-pkg-station#keeping-it-private";
const gib=b=>b==null?"—":(b/2**30).toFixed(b>2**40?0:1)+" GiB", ago=t=>{if(!t)return"—";const s=Date.now()/1000-t;return s<90?Math.round(s)+" s":s<5400?Math.round(s/60)+" min":(s/3600).toFixed(1)+" h"};
async function api(p,o={}){const r=await fetch(p,{headers:{"Content-Type":"application/json"},...o});if(r.status===401){location.reload();throw new Error("Log in first.")}if(!r.ok)throw new Error((await r.json().catch(()=>({detail:r.statusText}))).detail);return r.headers.get("content-type")?.includes("json")?r.json():r.text()}
let tab="dash",plan=[],overrides={},libCfgHost=null;
// A path for display: its place on the host when the server knows it (the container path goes into the tooltip), else
// the container path with an "in container" tag, so it is never taken for a path on the NAS. Copy takes what is shown.
function pathHtml(host,container){const shown=host||container||"";if(!shown)return"";
  return `<span class="path"><code title="${esc(host?"in the container: "+container:"a path inside the container: no host location is set for this folder")}">${esc(shown)}</code>${host?"":` <span class="tag small">in container</span>`}<button type="button" class="copy" data-copy="${esc(shown)}" title="Copy this path">copy</button></span>`}
async function copyText(t){try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(t);return true}}catch(e){}
  const a=document.createElement("textarea");a.value=t;a.style.position="fixed";a.style.opacity="0";document.body.appendChild(a);a.select();let ok=false;try{ok=document.execCommand("copy")}catch(e){}a.remove();return ok}   // plain http on a LAN has no clipboard API
document.addEventListener("click",e=>{const b=e.target.closest("[data-copy]");if(!b)return;e.preventDefault();
  copyText(b.dataset.copy).then(ok=>{const t=b.textContent;b.textContent=ok?"copied":"select + Ctrl+C";setTimeout(()=>{b.textContent=t},1500)})});
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{tab=b.dataset.tab;document.querySelectorAll("nav button").forEach(x=>x.classList.toggle("on",x===b));document.querySelectorAll("section").forEach(s=>s.classList.toggle("on",s.id===tab));load()});
function say(t,bad){$("#msg").textContent=t;$("#msg").className="small "+(bad?"err":"muted")}
$("#bRun").onclick=()=>api("/api/run",{method:"POST",body:"{}"}).then(()=>say("Run started")).catch(e=>say(e.message,1)).finally(()=>{refresh();loadSetup()});
$("#bStop").onclick=()=>api("/api/stop",{method:"POST"}).then(()=>say("Stopping after the running builds")).catch(e=>say(e.message,1)).finally(refresh);
function doScan(){return api("/api/scan",{method:"POST"}).then(()=>say("Scanning…")).catch(e=>say(e.message,1)).finally(refresh)}
$("#bScan").onclick=doScan;
function goTab(t){document.querySelector(`nav button[data-tab=${t}]`).click()}
// first-run checklist: the card stays open while something is wrong, then shrinks to one line
let setup=[],setupOpen=false,busy=false,lastFin,wantLog=null;
const setupFails=()=>setup.filter(c=>c.level==="fail");
async function loadSetup(){try{setup=await api("/api/setup")}catch(e){return}renderSetup()}
function syncRun(){const f=setupFails();$("#bRun").disabled=busy||f.length>0;
  $("#runWhy").textContent=f.length?"Run is disabled until the FAIL items under Setup are fixed: "+f.map(c=>c.title).join(", "):""}
function renderSetup(){const el=$("#setupCard"),w=setup.filter(c=>c.level==="warn"),loud=setupFails().length>0||w.some(c=>!c.advisory);syncRun();
  if(!setup.length){el.innerHTML="";return}
  if(!loud&&!setupOpen){el.innerHTML=`<div class="card row small"><b class="lv-ok">Setup OK</b>${w.length?`<span class="muted">· ${w.length} note${w.length>1?"s":""}</span>`:""}<span class="muted" style="margin-left:auto"><a href="#" data-more>Details</a> · <a href="#" data-recheck>Re-check</a></span></div>`;return}
  el.innerHTML=`<div class="card"><div class="row"><h2 style="margin:0">Setup</h2><span class="small muted">${loud?"Fix the FAIL and warn rows, then re-check.":"Everything needed is in place."}</span><span style="margin-left:auto">${loud?"":`<a href="#" data-more class="small">Hide</a> `}<button class="btn" data-recheck>Re-check</button></span></div>
   <table class="setup">${setup.map(c=>`<tr><td class="lv-${cls(c.level)}">${esc(c.level==="fail"?"FAIL":c.level)}</td><td><b>${esc(c.title)}</b></td><td>${esc(c.message)}${c.fix?`<div class="small muted">Fix: ${esc(c.fix)}</div>`:""}</td><td class="small">${c.link?`<a href="#" data-goto="${esc(c.link.field)}">Settings</a>`:""}</td></tr>`).join("")}</table></div>`}
$("#setupCard").addEventListener("click",e=>{const a=e.target.closest("[data-recheck],[data-more],[data-goto]");if(!a)return;e.preventDefault();
  if(a.matches("[data-recheck]"))loadSetup();else if(a.matches("[data-more]")){setupOpen=!setupOpen;renderSetup()}
  else gotoField(a.dataset.goto)});
function gotoField(f){goTab("set");setTimeout(()=>{const i=document.querySelector(`[data-c="${CSS.escape(f)}"]`);if(i){i.scrollIntoView({block:"center"});i.focus()}},500)}
const hostDismissed=()=>{try{return localStorage.getItem("ps5pkg.hostBanner")==="1"}catch(e){return false}};
for(const id of["#folders","#hostBanner"])$(id).addEventListener("click",e=>{const a=e.target.closest("[data-goto],[data-dismiss]");if(!a)return;e.preventDefault();
  if(a.matches("[data-goto]"))gotoField(a.dataset.goto);else{try{localStorage.setItem("ps5pkg.hostBanner","1")}catch(x){}$("#hostBanner").innerHTML=""}});
$("#bSetupSet").onclick=()=>{setupOpen=true;goTab("dash");loadSetup()};
// a scan or run that ended badly: say so, with the end of its log
$("#failBox").addEventListener("click",e=>{const a=e.target.closest("[data-log]");if(!a)return;e.preventDefault();wantLog=a.dataset.log;goTab("logs")});
const FOLDER_LABEL={library:"Game library",downloads:"Downloads",output:"Finished .pkg files",temp:"Work / scratch",config:"Settings and history"};
function hostCell(f){let h;
  if(f.source==="env"||f.source==="settings")h=`<code>${esc(f.host)}</code>`;
  else{h=f.source==="detected"?`<code>${esc(f.host)}</code> <span class="muted small">(detected, volume prefix unknown)</span>`:`<span class="muted">(not set)</span>`;
    h+=` <a href="#" class="small" data-goto="host_paths.${esc(f.key)}">Set location</a>`}
  return h+`<div class="small muted">in the container: ${esc(f.container)}</div>`+(f.mismatch?`<div class="small" style="color:var(--warn)">The mount reports …${esc(f.detected)}, which doesn't match. Check the compose file (only a hint: some set-ups add prefixes).</div>`:"")}
function renderFolders(s){const F=s.folders||{},d=s.disks||{};
  $("#folders").innerHTML=`<table>${Object.keys(FOLDER_LABEL).filter(k=>F[k]).map(k=>{const x=d[k]||{},f=F[k];
    return `<tr><td>${FOLDER_LABEL[k]}${f.mode?`<div class="small muted">${f.mode==="ro"?"read-only":"writable"}</div>`:""}</td><td>${hostCell(f)}</td><td>${x.error?esc(x.error):gib(x.free)+" free"}</td></tr>`}).join("")}</table>`;
  const any=Object.values(F).some(f=>f.source==="env"||f.source==="settings");
  $("#hostBanner").innerHTML=Object.keys(F).length&&!any&&!hostDismissed()?`<div class="banner warn">Folder locations are shown as paths inside the container. <a href="#" data-goto="host_paths.library">Tell the app where they are on your NAS</a>.<a href="#" data-dismiss class="small" style="float:right">Dismiss</a></div>`:""}
// ---- games that failed or wait for space: explained, with what can be done (see /api/failures)
let failures=[],lastFailRev,engineBusy=false;
function toast(m,bad){const t=$("#toast");t.textContent=m;t.hidden=false;t.style.background=bad?"var(--bad)":"";clearTimeout(toast.h);toast.h=setTimeout(()=>{t.hidden=true},6000)}
const failTag=f=>f.state==="waiting"?`<span class="tag t-waiting">waiting for space</span>`:`<span class="tag t-failed">failed</span>`;
function failureButtons(f,dismiss){const t=esc(f.title_id);
  return `<div class="btnrow">${(f.actions||[]).map(a=>a.kind==="retry"?`<button class="btn sm pri" data-fa="retry" data-tid="${t}">Retry</button>`
    :a.kind==="api"?`<button class="btn sm" data-fa="api" data-act="${esc(a.id)}" data-tid="${t}" ${a.disabled||(a.id==="delete_staged"&&engineBusy)?`disabled title="${esc(a.disabled||"A run is active and may be using it")}"`:""} ${a.disabled?'data-blocked="1"':""}>${esc(a.label)}</button>`
    :a.kind==="goto"?`<button class="btn sm" data-fa="goto" data-field="${esc(a.field)}">${esc(a.label)}</button>`
    :`<button class="btn sm" data-fa="log" data-log="${esc(a.log)}">${esc(a.label)}</button>`).join("")}${dismiss?`<button class="btn sm" data-fa="dismiss" data-tid="${t}">Dismiss</button>`:""}</div>`}
function failureBody(f){return `${f.detail?.length?`<ul class="small">${f.detail.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`:""}`
  +(f.staged?`<div class="small">Staged package: ${pathHtml(f.staged.path_host,f.staged.path)} · ${gib(f.staged.bytes)}</div>`:"")
  +((f.history||[]).length>1?`<div class="small muted">${num(f.history.length)} attempts, last: ${f.history.slice(-3).map(h=>esc(new Date(h.at*1000).toLocaleString())+" "+esc(h.code)).join(" · ")}</div>`:"")
  +(f.excerpt?.length?`<pre class="small">${esc((f.excerpt_host||f.excerpt).join("\n"))}</pre>`:"")
  +`<div class="small muted">Raw message: ${esc(f.message)}</div>`}
function renderAttention(){const f=failures.filter(x=>!x.dismissed);
  $("#attention").innerHTML=f.length?`<div class="card"><h2>Needs attention</h2>${f.map(x=>`<div class="att"><b>${esc(x.name||x.title||x.title_id)}</b> ${failTag(x)} <span class="muted small">${esc(x.at_text||"")} · attempt ${esc(x.attempts||1)}</span>
    <div>${esc(x.summary)}</div><div class="small muted">${esc(x.next||"")}</div>${failureButtons(x,true)}
    <details><summary class="small">Details and the log of this attempt</summary>${failureBody(x)}</details></div>`).join("")}</div>`:""}
async function loadFailures(){try{failures=await api("/api/failures")}catch(e){return}renderAttention()}
document.addEventListener("click",async e=>{const b=e.target.closest("[data-fa]");if(!b)return;e.preventDefault();const a=b.dataset.fa,tid=b.dataset.tid;
  if(a==="log"){wantLog=b.dataset.log;goTab("logs");return}
  if(a==="goto"){gotoField(b.dataset.field);return}
  b.disabled=true;
  try{const r=a==="retry"?await api(`/api/failures/${encodeURIComponent(tid)}/retry`,{method:"POST"}):a==="dismiss"?await api(`/api/failures/${encodeURIComponent(tid)}/dismiss`,{method:"POST"})
      :await api(`/api/failures/${encodeURIComponent(tid)}/action`,{method:"POST",body:JSON.stringify({action:b.dataset.act})});
    toast(r?.message||"Done")}catch(err){toast(err.message,1)}
  b.disabled=false;loadFailures();if(tab==="lib")loadLib();if(tab==="hist")loadHist();refresh().catch(()=>{})});
// ---- PS4 titles: their packages are copied as they are (see the plan's `files`)
function ps4Files(p){const f=p.files||[],dest=p.dest_dir_host||p.dest_dir;
  return `<details><summary class="small">PS4 packages (${f.length}) → ${esc(p.dest_dir_host?"your NAS":"the output folder")}</summary>
   <div class="small">Copied to ${pathHtml(p.dest_dir_host,p.dest_dir)}</div>
   ${p.title_in_package?`<div class="small muted">Title in package: ${esc(p.title_in_package)}${p.title_source&&p.title_source!=="package"?` · named after the ${esc(p.title_source)}`:""}</div>`:""}
   <table class="small"><tbody>${f.map(x=>`<tr><td>${esc(x.name)}${x.via?` <span class="muted">(from ${esc(x.via.split("/").pop())})</span>`:""}</td><td>${esc(x.kind)}</td><td>${x.size?gib(x.size):""}</td></tr>`).join("")}</tbody></table>
   ${(p.notes||[]).map(n=>`<div class="small muted">${esc(n)}</div>`).join("")}</details>`}
function failBox(s){const ev=[["scan",s.scan],["run",s.run]].filter(([k,x])=>x&&x.finished).sort((a,b)=>b[1].finished-a[1].finished)[0];
  $("#failBox").innerHTML=ev&&ev[1].error&&!s.engine_running&&!s.scan.running?`<div class="banner bad"><b>The ${ev[0]==="scan"?"scan":"run"} failed.</b> ${esc(ev[1].error)}${ev[1].log_tail?.length?`<pre class="small">${esc((ev[1].log_tail_host||ev[1].log_tail).join("\n"))}</pre>`:""}<a href="#" data-log="${esc(ev[1].log)}">Open the log</a></div>`:""}
let cfgRev,cfgLoadedAt=0,setDirty=false,bannerHtml="";   // cfgRev: version of config.json the Settings form shows
function banners(s){const e=s.config_error,p=s.config_problems||[];let h="";
  if(e)h+=`<div class="banner bad"><b>Settings file could not be read</b> (${e.line?`line ${esc(e.line)}: `:""}${esc(e.message)}). ${e.using==="last_good"?"Using the last working settings.":"Using the default settings."} Fix the file or restore a backup.</div>`;
  if(p.length)h+=`<div class="banner warn"><b>Some settings in the settings file are not valid.</b> The defaults are used for them; the file itself is unchanged.<ul>${p.map(x=>`<li>${esc(x)}</li>`).join("")}</ul></div>`;
  if(s.host_problems?.length)h+=`<div class="banner warn"><b>Folder locations in the compose file are not valid.</b> They are ignored until fixed.<ul>${s.host_problems.map(x=>`<li>${esc(x)}</li>`).join("")}</ul></div>`;
  if(h!==bannerHtml){bannerHtml=h;$("#cfgBanner").innerHTML=h}}
// ---- who can reach this page: there is no login unless PS5PKG_PASSWORD is set (see the README), so say so, and shout when it looks open to the world
let accessHtml="";
function renderAccess(a){if(!a)return;let ban="";
  if(a.warn)ban=`<div class="banner bad"><b>This page has no login and appears to be reachable from outside your network.</b> ${a.reason==="remote"?`This request came from ${esc(a.client)}, which is not a home-network address.`:`This request came through a reverse proxy.`}
    Anyone who can open it can start and stop builds and change settings. Put it behind a VPN or set a password (<code>PS5PKG_PASSWORD</code>): <a href="${README}" target="_blank" rel="noopener noreferrer">how</a>.</div>`;
  const note=a.login?`Password protected · <a href="#" data-logout>Log out</a>`:`No login: anyone who can open this page can use it. Keep it on your home network or behind a VPN. <a href="${README}" target="_blank" rel="noopener noreferrer">More</a>`;
  if(ban+note!==accessHtml){accessHtml=ban+note;$("#accessBanner").innerHTML=ban;$("#accessNote").innerHTML=note}}
document.addEventListener("click",e=>{if(!e.target.closest("[data-logout]"))return;e.preventDefault();
  fetch("/api/logout",{method:"POST",headers:{"Content-Type":"application/json"}}).finally(()=>location.reload())});
const verText=v=>`PS5 PKG Station ${v.app} · ${v.ultrapack?"UltraPack "+v.ultrapack:"UltraPack version not known"}`;
function syncDeleteButtons(){document.querySelectorAll('[data-act="delete_staged"]').forEach(b=>{const blocked=engineBusy||b.dataset.blocked==="1";b.disabled=blocked;if(engineBusy)b.title="A run is active and may be using it"})}
let srvOff=0;   // seconds the server's clock is ahead of UTC: times the server speaks of (a schedule, when a run started) are shown on its clock
async function poll(){const t0=performance.now(),s=await api("/api/status");banners(s);renderAccess(s.access);
  if(s.clock){const m=/^([+-])(\d\d):(\d\d)$/.exec(s.clock.utc_offset||"");if(m)srvOff=(m[1]==="-"?-1:1)*(num(m[2])*3600+num(m[3])*60)}if(s.version)$("#ver").textContent=verText(s.version);engineBusy=!!s.engine_running;syncDeleteButtons();
  // config.json was edited since the form was loaded (a poll that started before the last load says nothing new)
  if(tab==="set"&&!s.config_error&&cfgRev!==undefined&&t0>cfgLoadedAt&&s.config_rev!==cfgRev){
    if(setDirty)$("#cfgChanged").hidden=false;else loadSet("Settings file changed on disk: reloaded.")}
  if(s.clock)$("#clockNote").textContent=`Server time now ${s.clock.time} (${s.clock.tz}, UTC${s.clock.utc_offset}). Schedule times use this clock.`;
  return s}
// ---- the Dashboard: the run first (state, how far, how long), then what it is doing, then the library and the folders
const MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],p2=n=>String(n).padStart(2,"0");
const srvAt=ts=>{const d=new Date((ts+srvOff)*1000);return{y:d.getUTCFullYear(),mo:d.getUTCMonth(),d:d.getUTCDate(),h:d.getUTCHours(),mi:d.getUTCMinutes()}};
// a time on the server's clock: 03:00 today, "22 Nov 03:00" another day
function clk(ts,now){if(!ts)return"—";const a=srvAt(ts),b=srvAt(now||Date.now()/1000),hm=`${p2(a.h)}:${p2(a.mi)}`;return a.y===b.y&&a.mo===b.mo&&a.d===b.d?hm:`${a.d} ${MON[a.mo]} ${hm}`}
function dur(s){s=Math.max(0,Math.round(num(s)));if(s<90)return s+" s";const m=Math.round(s/60);if(m<60)return m+" min";const h=Math.floor(m/60),r=m%60;return r?`${h} h ${p2(r)} min`:`${h} h`}
const mibs=r=>{const v=num(r)/2**20;return v.toFixed(v>=10?0:1)+" MiB/s"},games=n=>`${num(n)} game${num(n)===1?"":"s"}`;
const countsText=r=>{const p=[];if(num(r.built))p.push(`${num(r.built)} built`);if(num(r.copied))p.push(`${num(r.copied)} copied`);if(num(r.failed))p.push(`${num(r.failed)} failed`);if(num(r.deferred))p.push(`${num(r.deferred)} waiting for space`);return p.join(", ")||"nothing to do"};
const bar=pct=>`<div class="bar"><i style="width:${Math.max(0,Math.min(100,num(pct)))}%"></i></div>`;
const jobName=x=>esc(String(x.name||x.title||x.title_id||"").replace(/\.pkg$/,""));
// the run being shown: the engine is up and its status is of this run (the last run's finished status is not)
const liveRun=s=>s.engine_running&&s.status&&s.status.run&&!s.status.finished?s.status.run:null;
function headerText(s){const run=liveRun(s);let t;
  if(s.engine_running){t=s.stop_requested?"Stopping after current":"Running";if(run&&run.phase!=="scanning")t+=` · ${num(run.done)} of ${num(run.total)} done${run.remaining!=null?` · about ${dur(run.remaining)} left`:""}`}
  else if(s.scan.running)t="Scanning…";
  else{t="Idle";if(s.next_run)t+=` · next run ${s.next_run.label}`;const lr=s.last_run;if(lr){const c=countsText(lr);t+=lr.interrupted?" · the last run did not finish":` · last run ${c==="nothing to do"?"had nothing to do":c}`}}
  return t+(s.clock?` · server time ${s.clock.time} ${s.clock.tz}`:"")}
function renderRun(s){const run=liveRun(s),now=s.now;let line,sub="",pct=null;
  if(s.engine_running){
    if(!run){line="Starting…";sub="a run has just been started"}
    else if(run.phase==="scanning"){line="Running · scanning the library…";sub=`started ${clk(run.started,now)} (${esc(run.trigger||"manual")}) · what to build is worked out first`}
    else{line=`${s.stop_requested?"Stopping after the current games":"Running"} · ${num(run.done)} of ${num(run.total)} done${num(run.failed)?` · <span class="err">${num(run.failed)} failed</span>`:""}`;pct=Math.round(num(run.progress)*100);
      sub=`started ${clk(run.started,now)} (${esc(run.trigger||"manual")}) · ${run.remaining!=null?`about ${dur(run.remaining)} left`:"estimating the time left…"}`}}
  else if(s.scan.running){line="Scanning for new games…"}
  else{const lr=s.last_run;
    line=lr?(lr.interrupted?`Idle · the last run did not finish (${num(lr.done)} of ${num(lr.total)} done)`:`Idle · last run ${clk(lr.finished,now)}: ${games(lr.total)}, ${esc(countsText(lr))}`):"Idle · nothing has run yet";
    sub=s.next_run?`Next run ${esc(s.next_run.label)}${lr&&lr.trigger?` · the last one was ${esc(lr.trigger)}`:""}`:"Not scheduled: press Run now, or set times under Settings."}
  const r=$("#runLine");const html=line;if(r.innerHTML!==html)r.innerHTML=html;
  $("#runBar").hidden=pct==null;if(pct!=null){$("#runBar i").style.width=pct+"%";$("#runPct").textContent=pct+"%"}
  $("#runSub").innerHTML=sub}
function buildRow(x,now){const stages=[1,3,5].includes(num(x.stage))&&x.part;
  const what=x.phase==="building"?`<span title="${esc(x.stage_text?"UltraPack: "+x.stage_text:"")}">${esc(x.stage_name||"Building")}</span> (${num(x.stage)} of 5)${stages?` · ${num(x.stage_pct)}%`:""}`
    :`${esc(x.stage_name)}${x.total_bytes?` · ${gib(x.bytes)} of ${gib(x.total_bytes)} · ${num(x.stage_pct)}%`:""}`;
  const left=x.remaining!=null?`about ${dur(x.remaining)} left`:"estimating…";
  return `<div class="job"><div class="row"><b>${jobName(x)}</b>${x.log?`<span class="small" style="margin-left:auto"><a href="#" data-log="pkg/${esc(x.log)}">log</a></span>`:""}</div>
   <div class="small">${what} · ${dur(now-num(x.since))} elapsed · ${left}${x.rate?` · ${mibs(x.rate)}`:""}</div>${x.progress!=null?bar(num(x.progress)*100):""}</div>`}
function prepRow(x,now){const known=num(x.size)>0;
  return `<div class="job"><div class="row"><b>${jobName(x)}</b></div><div class="small">${x.ps4?"copying PS4 packages":"copying &amp; verifying"}${known?` · ${gib(x.bytes)} of ${gib(x.size)} · ${num(x.pct)}%`:` · ${gib(x.bytes)}`}${x.rate?` · ${mibs(x.rate)}`:""}${x.remaining!=null?` · about ${dur(x.remaining)} left`:""} · ${dur(now-num(x.since))} elapsed</div>${known?bar(num(x.pct)):""}</div>`}
let resOpen=false,healthOpen=false;
document.addEventListener("toggle",e=>{if(e.target.id==="resDetails")resOpen=e.target.open;if(e.target.id==="healthBox")healthOpen=e.target.open},true);
function resultsTable(r,now){return `<table>${r.map(x=>`<tr><td>${esc(x.name||x.title)}</td><td><span class="tag t-${cls(x.status)}">${esc(x.status==="deferred"?"waiting for space":x.status)}</span></td><td class="small muted" title="${esc(x.why_host||x.why||"")}">${esc(x.summary||x.why_host||x.why||x.validate||"")} ${x.build_min?`· build ${esc(x.build_min)} min`:""}${x.at?` · ${esc(clk(x.at,now))}`:""}</td></tr>`).join("")}</table>`}
function renderLive(s){const run=liveRun(s),st=s.status||{},now=s.now,r=st.results||[];let h="";
  if(run&&run.phase!=="scanning"){
    const B=st.builds||[],P=st.preparing||[],Q=st.queue||[];
    if(B.length)h+=`<div class="card sec"><h2>Building (${B.length})</h2>${B.map(x=>buildRow(x,now)).join("")}</div>`;
    if(P.length)h+=`<div class="card sec"><h2>Preparing (${P.length})</h2>${P.map(x=>prepRow(x,now)).join("")}</div>`;
    if(Q.length)h+=`<div class="card sec"><h2>Up next (${Q.length})${num(run.queue_bytes)?` · ${gib(run.queue_bytes)}`:""}</h2>${Q.slice(0,6).map(x=>`<div class="small">${jobName(x)} <span class="muted">${x.size?gib(x.size):""}${x.ps4?" · PS4":""}</span></div>`).join("")}${Q.length>6?`<div class="small muted">and ${Q.length-6} more</div>`:""}</div>`;
    if(r.length)h+=`<details class="card sec" id="resDetails" ${resOpen?"open":""}><summary><b>Finished this run (${r.length})</b> <span class="muted small">${esc(countsText(run))}</span></summary>${resultsTable(r,now)}</details>`}
  else if(!s.engine_running&&r.length&&st.finished&&s.last_run)h=`<details class="card sec" id="resDetails" ${resOpen?"open":""}><summary><b>Last run (${r.length})</b> <span class="muted small">${esc(clk(s.last_run.finished,now))} · ${esc(countsText(s.last_run))}</span></summary>${resultsTable(r,now)}</details>`;
  const box=$("#liveBox");if(box.dataset.h!==h){box.dataset.h=h;box.innerHTML=h}}
$("#liveBox").addEventListener("click",e=>{const a=e.target.closest("[data-log]");if(!a)return;e.preventDefault();wantLog=a.dataset.log;goTab("logs")});
// what is wrong with a folder: missing, read-only, or too small for the biggest game still to build (the rules of the Setup card: the
// output needs room for it, the work drive for 110 % of it on top of what is kept free; a big drive is never "low" for being nearly full)
function folderProblems(s){const F=s.folders||{},d=s.disks||{},sp=s.space||{},big=num(sp.biggest&&sp.biggest.size),res=num(sp.reserve),out=[];
  for(const k of Object.keys(FOLDER_LABEL)){const f=F[k];if(!f)continue;const x=d[k]||{},L=FOLDER_LABEL[k];
    if(x.error)out.push(`${L}: not available`);
    else if(["output","temp","config"].includes(k)&&f.mode==="ro")out.push(`${L}: read-only`);
    else if(k==="output"&&big&&num(x.free)<big)out.push(`${L}: ${gib(x.free)} free, the biggest game (${gib(big)}) may not fit`);
    else if(k==="temp"&&(num(x.free)-res<=0||(big&&big*1.1>num(x.free)-res)))out.push(`${L}: ${gib(x.free)} free, the biggest game needs about ${gib(big*1.1+res)} with what is kept free`)}
  return out}
// one line about the library and the folders; the folders table opens from it, and moves to the top of the page when something is wrong with them
function renderHealth(s){const c=s.plan_counts||{},d=s.disks||{},total=Object.values(c).reduce((a,b)=>a+num(b),0),probs=folderProblems(s),box=$("#healthBox");
  $("#healthSum").innerHTML=`Library ${total} games · ${num(c.build)} to build${num(c.copy)?` · ${num(c.copy)} PS4 to copy`:""} · ${probs.length?`<b class="err">Folders: ${esc(probs.join("; "))}</b>`:`Folders ok <span class="muted">(work ${esc(gib(d.temp?.free))} free · output ${esc(gib(d.output?.free))} free)</span>`}`;
  box.classList.toggle("warn",probs.length>0);
  const to=probs.length?$("#healthTop"):$("#healthBottom");if(box.parentNode!==to)to.appendChild(box);
  box.open=healthOpen}
async function refresh(){
  const s=await poll();
  $("#state").textContent=headerText(s);
  busy=s.engine_running||s.scan.running;syncRun();failBox(s);
  if(s.failures_rev!==lastFailRev){lastFailRev=s.failures_rev;loadFailures()}
  const fin=[s.scan.finished,s.run?.finished].join("|");if(fin!==lastFin){const first=lastFin===undefined;lastFin=fin;if(!first)loadSetup()}   // a scan changes what the biggest game is
  $("#bScan").disabled=s.engine_running||s.scan.running;$("#bStop").disabled=!s.engine_running||s.stop_requested;
  renderRun(s);renderLive(s);renderFolders(s);renderHealth(s);
  const c=s.plan_counts;
  $("#kpis").innerHTML=[["To build",c.build],...(c.copy?[["PS4 to copy",c.copy]]:[]),["Built",c.done],["Failed",c.failed,c.failed>0],["Last scan",s.plan_time?ago(s.plan_time):"never"]].map(([k,v,bad])=>`<div class="kpi${bad?" bad":""}"><b>${esc(v??"—")}</b><span>${esc(k)}</span></div>`).join("");
}
// under the patch folder input: where that folder is on the NAS, or where to put the files
function patchHint(o,tid,cfgHost){
  if(o.patch)return o.patch_host?`On your NAS: ${pathHtml(o.patch_host,o.patch)}`:`${pathHtml(null,o.patch)} (no host location is set for this folder)`;
  return cfgHost?`Put the files in <code>${esc(cfgHost.replace(/[\\/]+$/,"")+"/patches/"+tid)}</code> on your NAS and enter <code>/config/patches/${esc(tid)}</code> here (a path on the NAS works too).`
    :`Inside the container. Say where /config is on your NAS (Settings > Folders) to see the real location.`}
async function loadLib(){
  let s;[plan,overrides,s]=await Promise.all([api("/api/plan"),api("/api/overrides"),api("/api/status")]);
  const f=$("#libFilter").value.toLowerCase(),a=$("#libAct").value;
  const rows=plan.filter(p=>(!a||p.action===a)&&(!f||(p.title+p.title_id).toLowerCase().includes(f)));
  $("#libSum").textContent=`${plan.length} titles · ${plan.filter(p=>p.action==="build").length} to build`;
  const empty=t=>`<tr><td colspan="4">${t}</td></tr>`,cf=s.folders?.config,cfgHost=cf&&cf.host&&!cf.partial?cf.host:null;libCfgHost=cfgHost;
  $("#libRows").innerHTML=!plan.length?empty(!s.plan_time?`<b>No scan yet.</b> <span class="muted">Press Scan to look through your game library for games to build.</span> <button class="btn" data-do="scan">Scan for new games</button>`
      :`<b>The last scan found no games.</b> <span class="muted">Nothing usable was found in <code>${esc(s.disks.library?.path||"")}</code>${s.disks.downloads?.error?"":` or <code>${esc(s.disks.downloads?.path||"")}</code>`}. Check the folders on the Dashboard, then scan again.</span>`)
    :!rows.length?empty('<span class="muted">No titles match the filter.</span>'):rows.map(p=>{const o=overrides[p.title_id]||{};const c=p.candidates||[];
    return `<tr><td><b>${esc(p.title||p.title_id)}</b><div class="small muted">${esc(p.title_id)}${p.fw?" · fw "+esc(p.fw):""}</div>${p.patched?`<div class="small muted">Named after the patch's param.json: fw ${esc(p.fw||"?")} (the source has ${esc(p.source_fw||"?")})</div>`:""}
    <details><summary class="small">Options${Object.keys(o).length?" (overridden)":""}</summary><div class="ovr" data-tid="${esc(p.title_id)}">
     <label class="small">Source <select data-k="source"><option value="">automatic</option>${c.map(x=>`<option value="${esc(x.source)}" ${o.source===x.source?"selected":""}>${esc(x.kind)} v${esc(x.version)}${x.packed?" ["+esc(x.packed)+"]":""} · ${esc(x.source_host||x.source+" (container path)")}</option>`).join("")}</select></label>
     <label class="small">Modified executables <select data-k="modified_exec"><option value="">by console firmware</option><option value="image" ${o.modified_exec==="image"?"selected":""}>keep image (backport)</option><option value="original" ${o.modified_exec==="original"?"selected":""}>use original</option></select></label>
     <label class="small">Patch folder (files overlaid) <input type="text" data-k="patch" value="${esc(o.patch||"")}" placeholder="/config/patches/…"></label>
     <div class="small muted" data-patchhint>${patchHint(o,p.title_id,cfgHost)}</div>
     <label class="small">Note <input type="text" data-k="note" value="${esc(o.note||"")}"></label>
     <label class="small"><input type="checkbox" data-k="skip_verify" ${o.skip_verify?"checked":""}> don't check against the original archive (keep this copy anyway)</label>
     <label class="small"><input type="checkbox" data-k="skip" ${o.skip?"checked":""}> skip this title</label>
     <div class="row"><button class="btn" data-do="saveov">Save options</button>${p.action==="build"?`<button class="btn" data-do="buildone" data-tid="${esc(p.title_id)}">Build only this</button>`:""}<span class="small muted"></span></div></div></details></td>
    <td>${esc(p.version)}</td><td class="small">${esc(p.kind||"")}<div>${pathHtml(p.source_host,p.source)}</div></td>
    <td>${p.failure?failTag(p.failure.state==="waiting"||p.action==="failed"?p.failure:{state:"failed"}):`<span class="tag t-${cls(p.action)}">${esc(p.action)}</span>`}${p.failure&&p.failure.state==="failed"&&p.action==="build"?` <span class="tag t-build">will retry</span>`:""}
     <div class="small muted">${esc(p.why_host||p.why)}</div>${p.ps4&&p.files?ps4Files(p):""}${p.failure?`${failureButtons(p.failure,false)}<details><summary class="small">Why, and the log</summary>${failureBody(p.failure)}</details>`:""}</td></tr>`}).join("");
  $("#libTime").textContent=s.plan_time?`scanned ${ago(s.plan_time)} ago`:"not scanned yet";
}
$("#libFilter").oninput=loadLib;$("#libAct").onchange=loadLib;
async function saveOv(btn){const box=btn.closest(".ovr"),o={};box.querySelectorAll("[data-k]").forEach(e=>o[e.dataset.k]=e.type==="checkbox"?e.checked:e.value);
  const m=btn.parentElement.querySelector("span");try{const r=await api("/api/overrides/"+encodeURIComponent(box.dataset.tid),{method:"PUT",body:JSON.stringify(o)});m.textContent="Saved — rescan to apply";m.className="small muted";
    const pi=box.querySelector('[data-k="patch"]');if(pi)pi.value=r.patch||"";   // a path typed as on the NAS is stored as the container path
    const h=box.querySelector("[data-patchhint]");if(h)h.innerHTML=patchHint(r,box.dataset.tid,libCfgHost)}catch(e){m.textContent=e.message;m.className="small err"}}
async function buildOne(t){try{await api("/api/run",{method:"POST",body:JSON.stringify({only:[t]})});say("Started "+t);tab="dash";document.querySelector('nav button[data-tab=dash]').click()}catch(e){alertBox(e.message)}}
function alertBox(m){say(m,1)}
document.addEventListener("click",e=>{const b=e.target.closest("[data-do]");if(!b)return;e.preventDefault();
  if(b.dataset.do==="scan"){doScan();goTab("dash")}else if(b.dataset.do==="saveov")saveOv(b);else if(b.dataset.do==="buildone")buildOne(b.dataset.tid)});
function builtRow(t,v){return `<tr><td><b>${esc(String(v.output||t).split(/[\\/]/).pop())}</b>${v.ps4?` <span class="tag">PS4</span>${Array.isArray(v.files)?` <span class="muted small">${num(v.files.length)} package${v.files.length===1?"":"s"}</span>`:""}`:""}${v.output?`<div class="small">Location: ${pathHtml(v.output_host,v.output)}</div>`:""}<div class="small muted">Source: ${v.source?pathHtml(v.source_host,v.source):"—"}</div></td><td class="small">${esc(v.built||"")}</td><td>${esc(gib(v.bytes))}</td><td class="small">${v.ps4?"copied as they are":v.prep_min==null&&v.build_min==null&&v.seconds!=null?`total ${esc((num(v.seconds)/60).toFixed(1))} min`:`prep ${esc(v.prep_min??"—")} · build ${esc(v.build_min??"—")} min`}${v.level!=null?` · L${esc(v.level)}`:""}</td><td class="small">${esc(v.validate??v.validated??"")}${typeof v.sha256==="string"?`<div class="muted">sha256 ${esc(v.sha256.slice(0,16))}…</div>`:""}</td></tr>`}
function failedRow(f){return `<tr><td><b>${esc(f.name||f.title||f.title_id)}</b> ${failTag(f)}<div class="small">${esc(f.summary)}</div><div class="small muted">${esc(f.next||"")}</div>${failureButtons(f,false)}<details><summary class="small">Details and the log of this attempt</summary>${failureBody(f)}</details></td><td class="small">${esc(f.at_text||"")}</td><td>—</td><td class="small">attempt ${esc(f.attempts||1)} · ${esc(f.phase||"")}</td><td class="small">${esc(f.code||"")}</td></tr>`}
async function loadHist(){const [l,fl]=await Promise.all([api("/api/ledger"),api("/api/failures")]);   // ledger entries from older versions have other field names and gaps
  const flt=$("#histFilter").value;
  const rows=[...Object.entries(l||{}).filter(([t,v])=>v&&typeof v==="object").map(([t,v])=>({k:"built",t,v,when:String(v.built||"")})),
    ...(fl||[]).map(f=>({k:"failed",f,when:String(f.at_text||"")}))].filter(r=>!flt||r.k===flt).sort((a,b)=>b.when.localeCompare(a.when));
  $("#histRows").innerHTML=!rows.length?`<tr><td colspan="5"><b>${flt==="failed"?"No failed attempts.":"Nothing built yet."}</b> <span class="muted">Finished packages are listed here, with their checks, once a run has built one; games that failed are listed with the reason.</span></td></tr>`:rows.map(r=>r.k==="failed"?failedRow(r.f):builtRow(r.t,r.v)).join("")}
$("#histFilter").onchange=loadHist;
const OPTS={ps4_mode:{copy:"copy their ready-made packages as they are (no conversion)",skip:"skip them (listed as skipped)"}};
const FIELDS={fConv:[["compression_level","Kraken level (1-9; 4 = fast, 7 = smaller)","n"],["cpu_count","Workers per build","n"],["max_compress","Builds compressing at once","n"],["max_builds","Builds alive at once","n"],["nice","CPU priority (nice 0-19)","n"],["ssd_reserve_gb","Keep free on work drive (GB)","n"],["build_on_ssd","Write package on work drive, then copy","b"],["console_fw","Console firmware (picks original vs backport)"],["skip_download_prefixes","Skip download folders starting with (comma separated)","l"],["junk_file_patterns","Delete files whose name matches (regex, comma separated)","l"],["junk_title_patterns","Cut from game titles (regex, comma separated)","l"],["ps4_mode","PS4 titles (CUSA...)","m"]],
 fSched:[["schedule.enabled","Run automatically","b"],["schedule.times","At (HH:MM, comma separated)","l"],["notify_url","Notification URL (ntfy topic, optional)"]]};
let cfg={};
$("#set").addEventListener("input",()=>{setDirty=true});$("#bReload").onclick=()=>loadSet("Reloaded from the file.");
function foldersForm(){const F=cfg.folders||{},hp=cfg.host_paths||{};
  const intro=cfg.in_docker?`Where each folder is <i>inside</i> the container is fixed by the <code>volumes:</code> list in docker-compose.yml. Tell the app where it is on your NAS, so paths are shown the way you know them. This only changes what is displayed.`
    :`Tell the app where each folder is on the machine you think of as the host, so paths are shown the way you know them. This only changes what is displayed.`;
  const rows=Object.keys(FOLDER_LABEL).filter(k=>F[k]).map(k=>{const f=F[k];
    const val=f.source==="env"?`<span><code>${esc(f.host)}</code> <span class="muted small">set in docker-compose (${esc(f.via)}); Settings can't change it</span></span>`
      :`<input type="text" data-c="host_paths.${k}" data-t="s" value="${esc(hp[k]||"")}" placeholder="${esc(f.source==="detected"?f.host:"/path/on/your/nas")}">`;
    return `<label class="f"><span>${esc(FOLDER_LABEL[k])} is shown as<div class="small">in the container: ${esc(f.container)}</div></span>${val}</label>`}).join("");
  const adv=cfg.in_docker?"":`<details><summary class="small">Advanced: folder paths as the app uses them</summary>${["library","downloads","output","temp"].map(k=>`<label class="f"><span>${esc(FOLDER_LABEL[k])}</span><input type="text" data-c="${k}" data-t="s" value="${esc(cfg[k])}"></label>`).join("")}</details>`;
  $("#fFolders").innerHTML=`<p class="small muted" style="margin-top:0">${intro}</p>${rows}${adv}`}
async function loadSet(note){loadStorage();cfgLoadedAt=performance.now();cfg=await api("/api/config");cfgRev=cfg.config_rev;setDirty=false;$("#cfgChanged").hidden=true;
  if(typeof note==="string"){$("#setMsg").textContent=note;$("#setMsg").className="small muted"}
  foldersForm();
  for(const [box,fs] of Object.entries(FIELDS)){$("#"+box).innerHTML=fs.map(([k,l,t])=>{const v=k.split(".").reduce((o,x)=>o?.[x],cfg);
  return `<label class="f"><span>${esc(l)}</span>${t==="m"?`<select data-c="${k}" data-t="s">${Object.entries(OPTS[k]).map(([o,x])=>`<option value="${o}" ${v===o?"selected":""}>${esc(x)}</option>`).join("")}</select>`:t==="b"?`<input type="checkbox" data-c="${k}" data-t="b" ${v?"checked":""}>`:`<input type="${t==="n"?"number":"text"}" data-c="${k}" data-t="${t||"s"}" value="${esc(Array.isArray(v)?v.join(", "):v)}">`}</label>`}).join("")}}
$("#bSave").onclick=async()=>{const out={};document.querySelectorAll("[data-c]").forEach(e=>{let v=e.dataset.t==="b"?e.checked:e.dataset.t==="n"?Number(e.value):e.dataset.t==="l"?e.value.split(",").map(x=>x.trim()).filter(Boolean):e.value.trim();
  const [a,b]=e.dataset.c.split(".");if(b){out[a]=out[a]||{...cfg[a]};out[a][b]=v}else out[a]=v});
  try{await api("/api/config",{method:"PUT",body:JSON.stringify(out)});$("#setMsg").textContent="Saved to "+(storage?(storage.settings.host||storage.settings.path):"the settings file");$("#setMsg").className="small muted";loadSet();loadSetup()}catch(e){$("#setMsg").textContent=e.message;$("#setMsg").className="small err"}};
// ---- Storage & backup, per-game overrides (see /api/storage)
let storage=null,impText=null;
const when=t=>t?new Date(t*1000).toLocaleString():"never";
const short=v=>{const t=JSON.stringify(v);return t===undefined?"(not set)":t.length>60?t.slice(0,57)+"…":t};
async function loadStorage(){try{storage=await api("/api/storage")}catch(e){return}renderStorage();renderOverrides()}
function fileRow(label,f,extra){return `<tr><td>${label}</td><td>${pathHtml(f.host,f.path)}<div class="small muted">${f.host?`in the container: ${esc(f.path)} · `:""}${f.exists?`last changed ${esc(when(f.changed))}`:"not there yet"}${extra||""}</div></td></tr>`}
function renderStorage(){const s=storage,b=s.backups;
  $("#storageCard").innerHTML=`<h2>Storage &amp; backup</h2>
   <table>${fileRow("Settings file",s.settings)}${fileRow("Build history",s.history,` · ${num(s.history.packages)} package${s.history.packages===1?"":"s"}`)}${fileRow("Failed games",s.failures,` · ${num(s.failures.count)} stored`)}</table>
   ${s.settings.error?`<div class="small err">The settings file can't be read right now: ${esc(s.settings.error.message)}.</div>`:""}
   <p class="small muted">You can edit the settings file by hand; changes are picked up automatically. ${esc(s.state_note)}</p>
   <div class="row"><a class="btn" id="bExport" href="/api/export?notify_url=1" download>Export settings</a>
     <label class="small"><input type="checkbox" id="expNotify" checked> include the notification URL (it may be a private topic: anyone with the file can post to it)</label></div>
   <div class="row" style="margin-top:8px"><a class="btn" id="bFull" href="/api/backup.zip" download>Download full backup (.zip)</a>
     <label class="small"><input type="checkbox" id="fullFailures"> also the failed-game records</label><span class="small muted">settings + build history + the list of patch folders (not their files)</span></div>
   <div style="margin-top:12px"><b class="small">Import</b>
     <div class="row"><input type="file" id="impFile" accept=".json,application/json">
       <label class="small"><input type="checkbox" id="impSettings" checked> settings</label><label class="small"><input type="checkbox" id="impOvr" checked> per-game overrides</label>
       <button class="btn" id="bImpPreview">Preview</button><button class="btn pri" id="bImpApply" disabled>Import</button></div>
     <div id="impOut" class="small"></div></div>
   <div style="margin-top:12px"><b class="small">Automatic backups</b>
     <div class="small muted">${b.count?`${num(b.count)} of ${num(b.keep)} kept · last ${esc(when(b.last))}`:"None yet: one is made each time the settings file is changed."} <a href="#" id="bkToggle">${b.count?"Restore…":""}</a>${b.error?` <span class="err">${esc(b.error)}</span>`:""}</div>
     <div id="bkList" hidden>${b.items.map(i=>`<div class="row small" data-bk="${esc(i.name)}"><span>${esc(when(i.at))}</span><span class="muted">${esc(i.name)} · ${num(i.size)} bytes</span><button class="btn sm" data-bkpreview>Preview</button><button class="btn sm" data-bkrestore>Restore</button></div>`).join("")}<div id="bkOut" class="small"></div></div></div>
   <p class="small muted" style="margin-bottom:0">${esc(verText(s.versions))}</p>`;
  $("#expNotify").onchange=$("#fullFailures").onchange=()=>{$("#bExport").href="/api/export?notify_url="+($("#expNotify").checked?1:0);$("#bFull").href="/api/backup.zip"+($("#fullFailures").checked?"?failures=1":"")};
  $("#bImpPreview").onclick=()=>impRun(true);$("#bImpApply").onclick=()=>impRun(false);$("#impFile").onchange=()=>{impText=null;$("#bImpApply").disabled=true;$("#impOut").textContent=""};
  $("#bkToggle").onclick=e=>{e.preventDefault();$("#bkList").hidden=!$("#bkList").hidden}}
const parts=()=>[$("#impSettings").checked?"settings":"",$("#impOvr").checked?"overrides":""].filter(Boolean).join(",");
async function postJson(path,body){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body});if(r.status===401){location.reload();return{ok:false,data:{detail:"Log in first."}}}let d={};try{d=await r.json()}catch(e){}return{ok:r.ok,data:d}}
function diffHtml(d){const row=(k,t)=>`<li><code>${esc(k)}</code> ${t}</li>`;
  return `<b>${esc(d.summary)}</b><ul>${(d.settings?.changed||[]).map(c=>row(c.key,`${esc(short(c.from))} → ${esc(short(c.to))}${c.note?` <span class="muted">(${esc(c.note)})</span>`:""}`)).join("")}`
   +(d.overrides?.added||[]).map(c=>row(c.title_id,`override added: ${esc(short(c.to))}`)).join("")+(d.overrides?.replaced||[]).map(c=>row(c.title_id,`override replaced: ${esc(short(c.from))} → ${esc(short(c.to))}`)).join("")
   +(d.overrides?.removed||[]).map(c=>row(c.title_id,`override removed (${esc(short(c.from))})`)).join("")+`</ul>`}
async function impRun(dry){const out=$("#impOut");
  if(dry){const f=$("#impFile").files[0];if(!f){out.innerHTML=`<span class="err">Choose a file first.</span>`;return}impText=await f.text()}
  if(!impText)return;if(!parts()){out.innerHTML=`<span class="err">Choose settings, overrides or both.</span>`;return}
  const r=await postJson(`/api/import?parts=${parts()}${dry?"&dry_run=1":""}`,impText);
  if(!r.ok){$("#bImpApply").disabled=true;out.innerHTML=`<span class="err"><b>${esc(r.data.detail||"Not imported")}</b></span>${(r.data.problems||[]).length>1?`<ul>${r.data.problems.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`:""}<div class="muted">Nothing was changed.</div>`;return}
  if(dry){out.innerHTML=diffHtml(r.data)+(r.data.changes?`<div class="muted">Nothing is written until you press Import.</div>`:"");$("#bImpApply").disabled=!r.data.changes;return}
  $("#bImpApply").disabled=true;impText=null;out.innerHTML=`<b>Imported: ${esc(r.data.summary)}.</b>${r.data.backup?` The previous settings were saved as <code>${esc(r.data.backup)}</code>.`:""}`;loadSet("Imported.")}
document.addEventListener("click",async e=>{const bk=e.target.closest("[data-bk]");if(!bk)return;const name=bk.dataset.bk,out=$("#bkOut");
  if(e.target.closest("[data-bkpreview]")){const r=await postJson(`/api/backups/${encodeURIComponent(name)}/restore?dry_run=1`,"{}");out.innerHTML=r.ok?diffHtml(r.data)+(r.data.problems?.length?`<div class="muted">The backup has values that are not valid now: ${r.data.problems.map(esc).join("; ")}</div>`:""):`<span class="err">${esc(r.data.detail||"failed")}</span>`}
  else if(e.target.closest("[data-bkrestore]")){if(!confirm(`Make the backup ${name} the current settings? The current file is backed up first.`))return;const r=await postJson(`/api/backups/${encodeURIComponent(name)}/restore`,"{}");
    if(r.ok){toast(`Restored. ${r.data.backup?"The replaced settings were saved as "+r.data.backup+".":""}`);loadSet("Restored from "+name)}else out.innerHTML=`<span class="err">${esc(r.data.detail||"failed")}</span>`}});
function renderOverrides(){const o=storage.overrides;
  $("#ovrCard").innerHTML=`<h2>Per-game overrides</h2>${o.length?`<table><thead><tr><th>Game</th><th>What is overridden</th><th>Note</th><th></th></tr></thead><tbody>${o.map(x=>`<tr><td><b>${esc(x.title||x.title_id)}</b><div class="small muted">${esc(x.title_id)}${x.in_library?"":` <span class="tag t-waiting">not in library</span>`}</div></td>
     <td class="small">${esc(x.what.join(" · ")||"(empty)")}${x.patch?`<div>${pathHtml(x.patch_host,x.patch)}</div>`:""}</td><td class="small">${esc(x.note)}</td>
     <td><button class="btn sm" data-ovr-remove="${esc(x.title_id)}">Remove</button></td></tr>`).join("")}</tbody></table>`:`<div class="muted small">No game has options of its own. Set them from a game's Options on the Library page.</div>`}`}
$("#ovrCard").addEventListener("click",async e=>{const b=e.target.closest("[data-ovr-remove]");if(!b)return;const t=b.dataset.ovrRemove;
  if(!confirm(`Remove the override for ${t}? Only this entry is deleted.`))return;
  try{await api("/api/overrides/"+encodeURIComponent(t),{method:"DELETE"});toast("Removed the override for "+t);loadStorage()}catch(err){toast(err.message,1)}});
async function loadLogs(){const l=await api("/api/logs");const cur=wantLog||$("#logSel").value;wantLog=null;if(cur&&!l.some(x=>x.name===cur))l.unshift({name:cur});$("#logSel").innerHTML=l.map(x=>`<option ${x.name===cur?"selected":""}>${esc(x.name)}</option>`).join("");showLog()}
async function showLog(){const n=$("#logSel").value;if(!n)return;const t=await api("/api/logs/"+encodeURIComponent(n).replace(/%2F/g,"/")+"?lines=600"+($("#logHost").checked?"&host=1":""));const p=$("#logTxt");p.textContent=t;if($("#logFollow").checked)p.scrollTop=p.scrollHeight}
$("#logSel").onchange=showLog;$("#logHost").onchange=()=>{try{localStorage.setItem("ps5pkg.logHost",$("#logHost").checked?"1":"0")}catch(e){}showLog().catch(()=>{})};
try{if(localStorage.getItem("ps5pkg.logHost")==="0")$("#logHost").checked=false}catch(e){}
$("#bLog").onclick=loadLogs;
function load(){({dash:refresh,lib:loadLib,hist:loadHist,set:loadSet,logs:loadLogs})[tab]().catch(e=>say(e.message,1));if(tab==="dash")loadSetup();if(tab!=="dash"&&tab!=="set")poll().catch(()=>{})}
load();setInterval(()=>{(tab==="dash"?refresh():poll()).catch(()=>{});if(tab==="logs"&&$("#logFollow").checked)showLog().catch(()=>{})},5000);
