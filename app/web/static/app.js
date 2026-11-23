// Everything that is not written by this page (titles, IDs, versions, file names, log lines, messages) goes through esc()
// before it is put into HTML, numbers through num(), and class names through cls(). No handler is written inline: the
// buttons carry data- attributes and the clicks are handled at the bottom of the page (the CSP forbids inline script).
const $=s=>document.querySelector(s), esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num=v=>Number.isFinite(Number(v))?Number(v):0, cls=v=>String(v??"").replace(/[^\w-]/g,"");
const README="https://github.com/zhangchi198/ps5-pkg-station#keeping-it-private";
const gib=b=>b==null?"—":(b/2**30).toFixed(b>2**40?0:1)+" GiB", ago=t=>{if(!t)return"—";const s=Date.now()/1000-t;return s<90?Math.round(s)+" s":s<5400?Math.round(s/60)+" min":(s/3600).toFixed(1)+" h"};
async function api(p,o={}){const r=await fetch(p,{headers:{"Content-Type":"application/json"},...o});if(r.status===401){location.reload();throw new Error("Log in first.")}if(!r.ok){const d=await r.json().catch(()=>({detail:r.statusText})),e=new Error(d.detail||r.statusText);e.errors=d.errors;throw e}return r.headers.get("content-type")?.includes("json")?r.json():r.text()}
let tab="dash",plan=[],overrides={},libCfgHost=null;
// A path for display: its place on the host when the server knows it (the container path goes into the tooltip), else
// the container path with an "in container" tag, so it is never taken for a path on the NAS. Copy takes what is shown.
function pathHtml(host,container){const shown=host||container||"";if(!shown)return"";
  return `<span class="path"><code title="${esc(host?"in the container: "+container:"a path inside the container: no host location is set for this folder")}">${esc(shown)}</code>${host?"":` <span class="tag small">in container</span>`}<button type="button" class="copy" data-copy="${esc(shown)}" title="Copy this path">copy</button></span>`}
async function copyText(t){try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(t);return true}}catch(e){}
  const a=document.createElement("textarea");a.value=t;a.style.position="fixed";a.style.opacity="0";document.body.appendChild(a);a.select();let ok=false;try{ok=document.execCommand("copy")}catch(e){}a.remove();return ok}   // plain http on a LAN has no clipboard API
document.addEventListener("click",e=>{const b=e.target.closest("[data-copy]");if(!b)return;e.preventDefault();
  copyText(b.dataset.copy).then(ok=>{const t=b.textContent;b.textContent=ok?"copied":"select + Ctrl+C";setTimeout(()=>{b.textContent=t},1500)})});
document.querySelectorAll("nav button").forEach(b=>b.onclick=async()=>{if(tab==="set"&&b.dataset.tab!=="set"&&settingsDirty()&&!(await askConfirm({title:"Discard your changes?",html:"<p>You have unsaved changes on the Settings page.</p>",ok:"Discard them",cancel:"Keep editing",danger:true})))return;tab=b.dataset.tab;if(tab!=="logs"&&location.hash.startsWith("#logs"))try{history.replaceState(null,"",location.pathname+location.search)}catch(x){}document.querySelectorAll("nav button").forEach(x=>x.classList.toggle("on",x===b));document.querySelectorAll("section").forEach(s=>s.classList.toggle("on",s.id===tab));load()});
function say(t,bad){$("#msg").textContent=t;$("#msg").className="small "+(bad?"err":"muted")}
// A question in the page, not window.confirm: a browser dialog can block an automation, and on a phone it is small and ugly. Everything in `html` is
// escaped by the caller. Resolves {key, checked} for the button pressed, or null for Escape / a click outside.
function showDialog({title,html,buttons,check}){return new Promise(resolve=>{
  if(dlgDone)dlgDone(null);
  const back=$("#dlg"),box=back.querySelector(".dlg"),ck=$("#dlgCheck"),prev=document.activeElement;
  $("#dlgTitle").textContent=title;$("#dlgBody").innerHTML=html||"";$("#dlgCheckRow").hidden=!check;ck.checked=false;if(check)$("#dlgCheckText").textContent=check;
  $("#dlgBtns").innerHTML=buttons.map(b=>`<button type="button" class="btn ${b.kind==="pri"?"pri":b.kind==="danger"?"dng":""}" data-dlg="${esc(b.key)}">${esc(b.label)}</button>`).join("");
  const done=r=>{back.hidden=true;document.body.classList.remove("dlgopen");back.removeEventListener("click",onClick);document.removeEventListener("keydown",onKey,true);dlgDone=null;
    try{if(prev&&prev.focus)prev.focus()}catch(e){}resolve(r)};
  const onClick=e=>{const b=e.target.closest("[data-dlg]");if(b)return done({key:b.dataset.dlg,checked:ck.checked});if(e.target===back)done(null)};
  const onKey=e=>{if(e.key==="Escape"){e.preventDefault();e.stopPropagation();done(null)}
    else if(e.key==="Tab"){const els=[...box.querySelectorAll("button,input")].filter(x=>!x.disabled&&!x.closest("[hidden]"));if(!els.length)return;
      const i=els.indexOf(document.activeElement);if(i===-1||(e.shiftKey&&i===0)||(!e.shiftKey&&i===els.length-1)){e.preventDefault();els[e.shiftKey?els.length-1:0].focus()}}};
  back.addEventListener("click",onClick);document.addEventListener("keydown",onKey,true);dlgDone=done;
  back.hidden=false;document.body.classList.add("dlgopen");(box.querySelector("button.pri,button.dng")||box.querySelector("button")||box).focus()})}
let dlgDone=null;
function askConfirm({title,html,ok="OK",cancel="Cancel",danger}){return showDialog({title,html,buttons:[{key:"ok",label:ok,kind:danger?"danger":"pri"},{key:"no",label:cancel}]}).then(r=>!!r&&r.key==="ok")}
const plural=(n,one,many)=>`${n} ${n===1?one:(many||one+"s")}`;
const bytesText=b=>b>=2**40?(b/2**40).toFixed(1)+" TiB":gib(b);
// Run now: a summary first (how many games, how big, about how long, how much room), unless this browser was told not to ask
const dontAskRun=()=>{try{return localStorage.getItem("ps5pkg.askRun")==="0"}catch(e){return false}};
function renderAskAgain(){$("#askAgain").hidden=!dontAskRun()}
document.addEventListener("click",e=>{if(!e.target.closest("[data-askagain]"))return;e.preventDefault();try{localStorage.removeItem("ps5pkg.askRun")}catch(x){}renderAskAgain()});
async function startRun(){const s=lastS||{},t=s.todo||{},free=s.disks&&s.disks.temp?s.disks.temp.free:null;
  if(!dontAskRun()){
    const nothing=!num(t.builds)&&!num(t.copies);
    const what=nothing?`<p>${s.plan_time?"Nothing is waiting to be built. The run starts by looking for new games anyway.":"No scan has been done yet: the run starts by looking for new games."}</p>`
      :`<p><b>${num(t.builds)?`Build ${plural(num(t.builds),"game")}`:""}${num(t.builds)&&num(t.copies)?" and copy ":num(t.copies)?"Copy ":""}${num(t.copies)?plural(num(t.copies),"PS4 title"):""}</b> (${esc(bytesText(num(t.bytes)))} of sources, ${t.seconds!=null?`about ${esc(dur(t.seconds))}`:"no time estimate yet: no build has finished on this machine"}).</p>`;
    const r=await showDialog({title:"Start a run now?",html:what+`<p class="small muted">Free on the work drive: ${free!=null?esc(bytesText(free)):"not known"}. You can stop the run at any time from the Dashboard.</p>`,
      buttons:[{key:"go",label:"Start run",kind:"pri"},{key:"no",label:"Cancel"}],check:"Don't ask me again in this browser"});
    if(!r||r.key!=="go")return;
    if(r.checked){try{localStorage.setItem("ps5pkg.askRun","0")}catch(e){}renderAskAgain()}}
  try{await api("/api/run",{method:"POST",body:"{}"});say("Run started")}catch(e){say(e.message,1)}finally{refresh();loadSetup()}}
$("#bRun").onclick=startRun;
// Stop: finish the current games (and take that back), or stop now (after saying what is lost)
const runCounts=s=>{const st=s.status||{},P=st.preparing||[];return{building:(st.builds||[]).length,preparing:P.filter(x=>!x.ps4).length,ps4:P.filter(x=>x.ps4).length,ready:(st.queue||[]).filter(x=>x.ready).length}};
function currentLeft(s){const st=s.status||{},rows=[...(st.builds||[]),...(st.preparing||[])];if(!rows.length)return 0;if(rows.some(x=>x.remaining==null))return null;return Math.max(...rows.map(x=>num(x.remaining)))}
async function stopDialog(){const s=lastS||{};if(!s.engine_running||s.stop_mode==="now")return;
  const c=runCounts(s),left=currentLeft(s),after=s.stop_mode==="after_current";
  const wait=left===null?" (time not known yet)":left>0?` (about ${dur(left)})`:"";
  const r=await showDialog({title:"Stop the run",
    html:after?`<p>The run is stopping: nothing new is started, and the games in progress finish${esc(wait)}. You can carry on with the queue until the last one ends.</p>`
      :`<p><b>Finish current games, then stop</b> starts nothing new and lets what is running finish${esc(wait)}. You can take it back until the last one ends.</p><p><b>Stop now</b> ends everything that is running (you are asked to confirm).</p>`,
    buttons:[after?{key:"resume",label:"Resume: carry on with the queue",kind:"pri"}:{key:"after",label:"Finish current games, then stop"+wait,kind:"pri"},{key:"now",label:"Stop now…",kind:"danger"},{key:"no",label:after?"Close":"Cancel"}]});
  if(!r||r.key==="no")return;
  if(r.key==="now"&&!(await askConfirm({title:"Stop now?",html:nowLoss(c),ok:"Stop now and discard",cancel:"Back",danger:true})))return;
  await doStop(r.key==="resume"?"resume":r.key==="now"?"now":"after_current")}
function nowLoss(c){const lost=[];if(c.building)lost.push(plural(c.building,"build")+" in progress");if(c.preparing)lost.push(plural(c.preparing,"game")+" being prepared");if(c.ps4)lost.push(plural(c.ps4,"PS4 copy","PS4 copies")+" in progress");
  return `<p>${lost.length?esc(lost.join(", ").replace(/, ([^,]*)$/," and $1"))+" will be discarded.":"Nothing is running at this moment."}</p>${c.ready?`<p>${esc(plural(c.ready,"prepared copy","prepared copies"))} ${c.ready===1?"is":"are"} kept for next time.</p>`:""}<p class="small muted">Games that have not started stay in the library and are built by the next run. Finished packages are never touched.</p>`}
async function doStop(mode){try{if(mode==="resume"){await api("/api/stop",{method:"DELETE"});say("Resumed: the run carries on")}else{await api("/api/stop",{method:"POST",body:JSON.stringify({mode})});say(mode==="now"?"Stopping now…":"Stopping after the running games")}}catch(e){say(e.message,1)}finally{refresh()}}
$("#bStop").onclick=stopDialog;
// Cancel one game: a build or a prepare (asked first), or take a queued game out of the run
async function cancelGame(tid,name,phase){
  if(phase!=="queue"&&!(await askConfirm({title:`Cancel ${name}?`,html:`<p>${phase==="building"?"The build in progress is discarded.":"What has been prepared so far is discarded."}</p><p class="small muted">The game stays in the library and is built again by the next run. Finished packages are never touched.</p>`,ok:"Cancel this game",cancel:"Keep going",danger:true})))return;
  try{await api(`/api/builds/${encodeURIComponent(tid)}/cancel`,{method:"POST",body:"{}"});say(phase==="queue"?"Taken out of this run":"Cancelling…")}catch(e){say(e.message,1)}finally{refresh()}}
document.addEventListener("click",e=>{const b=e.target.closest("[data-cancel]");if(!b)return;e.preventDefault();b.disabled=true;cancelGame(b.dataset.cancel,b.dataset.name||b.dataset.cancel,b.dataset.phase).finally(()=>{b.disabled=false})});
function doScan(){return api("/api/scan",{method:"POST"}).then(()=>say("Scanning…")).catch(e=>say(e.message,1)).finally(refresh)}
$("#bScan").onclick=doScan;
function goTab(t){document.querySelector(`nav button[data-tab=${t}]`).click()}
// first-run checklist: the card stays open while something is wrong, then shrinks to one line
let setup=[],setupOpen=false,lastFin,wantLog=null,lastS=null;
const setupFails=()=>setup.filter(c=>c.level==="fail");
async function loadSetup(){try{setup=await api("/api/setup")}catch(e){return}renderSetup()}
// why a button can't be pressed, in words (also its tooltip), so a disabled button never leaves a person guessing
function whyNot(s,what){const f=setupFails();
  if(what==="stop")return!s.engine_running?"No run is active.":s.stop_mode==="now"?"Stopping now.":"";
  if(s.engine_running)return"A run is active.";
  if(s.scan&&s.scan.running)return"A scan is running.";
  if(what==="run"&&f.length)return"Run is disabled until the FAIL items under Setup are fixed: "+f.map(c=>c.title).join(", ");
  return""}
function syncRun(){const s=lastS||{},r=whyNot(s,"run"),sc=whyNot(s,"scan"),st=whyNot(s,"stop");
  $("#bRun").disabled=!!r;$("#bScan").disabled=!!sc;$("#bStop").disabled=!!st;
  $("#bRun").title=r;$("#bScan").title=sc;$("#bStop").title=st;
  $("#bStop").textContent=s.stop_mode==="after_current"?"Stopping after current…":s.stop_mode==="now"?"Stopping now…":"Stop…";
  const why=s.engine_running?"Run now and Scan are off while a run is active.":s.scan&&s.scan.running?"Run now and Scan are off while a scan is running.":r;
  $("#runWhy").textContent=why}
// the schedule can be paused with one click (optionally until a time on the server's clock); the header says so
const WD=["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
function pauseLabel(p,now){if(!p)return"";if(p.until==null)return"paused";const a=srvAt(p.until),b=srvAt(now||Date.now()/1000),hm=`${p2(a.h)}:${p2(a.mi)}`;
  if(a.y===b.y&&a.mo===b.mo&&a.d===b.d)return`paused until ${hm}`;
  const days=(Date.UTC(a.y,a.mo,a.d)-Date.UTC(b.y,b.mo,b.d))/86400000;
  return days>0&&days<7?`paused until ${WD[new Date(Date.UTC(a.y,a.mo,a.d)).getUTCDay()]} ${hm}`:`paused until ${a.d} ${MON[a.mo]} ${hm}`}
function renderPause(s){const p=s.schedule_pause,on=s.schedule&&s.schedule.enabled,u=$("#pauseUntil").value;
  $("#pauseBox").hidden=!(on||p);$("#pauseUntilLbl").hidden=!!p;
  $("#bPause").textContent=p?"Resume schedule":u?"Pause until "+u.replace("T"," "):"Pause schedule";
  $("#bPause").title=p?"The schedule starts runs again":"No run starts by itself until you resume"+(u?" or until "+u.replace("T"," "):"")}
$("#pauseUntil").oninput=()=>{if(lastS)renderPause(lastS)};
$("#bPause").onclick=async()=>{const paused=lastS&&lastS.schedule_pause;
  try{if(paused)await api("/api/schedule/pause",{method:"DELETE"});else{const u=$("#pauseUntil").value;await api("/api/schedule/pause",{method:"POST",body:JSON.stringify({until:u||null})});$("#pauseUntil").value=""}
    say(paused?"Schedule resumed":"Schedule paused")}catch(e){say(e.message,1)}finally{refresh()}};
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
let cfgRev,cfgLoadedAt=0,bannerHtml="";   // cfgRev: version of config.json the Settings form shows
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
async function poll(){const t0=performance.now(),s=await api("/api/status");banners(s);renderAccess(s.access);refreshLibIfChanged(s);
  if(s.clock){const m=/^([+-])(\d\d):(\d\d)$/.exec(s.clock.utc_offset||"");if(m)srvOff=(m[1]==="-"?-1:1)*(num(m[2])*3600+num(m[3])*60)}if(s.version)$("#ver").textContent=verText(s.version);engineBusy=!!s.engine_running;syncDeleteButtons();
  // config.json was edited since the form was loaded (a poll that started before the last load says nothing new)
  if(tab==="set"&&!s.config_error&&cfgRev!==undefined&&t0>cfgLoadedAt&&s.config_rev!==cfgRev){
    if(settingsDirty())$("#cfgChanged").hidden=false;else loadSet("Settings file changed on disk: reloaded.")}
  renderRescan(s);
  if(s.clock&&$("#nextNote"))$("#nextNote").textContent=`Server time now ${s.clock.time} (${s.clock.tz}, UTC${s.clock.utc_offset}); times use this clock.${s.next_run?` Next run: ${s.next_run.label}.`:""}`;
  return s}
// ---- the Dashboard: the run first (state, how far, how long), then what it is doing, then the library and the folders
const MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],p2=n=>String(n).padStart(2,"0");
const srvAt=ts=>{const d=new Date((ts+srvOff)*1000);return{y:d.getUTCFullYear(),mo:d.getUTCMonth(),d:d.getUTCDate(),h:d.getUTCHours(),mi:d.getUTCMinutes()}};
// a time on the server's clock: 03:00 today, "22 Nov 03:00" another day
function clk(ts,now){if(!ts)return"—";const a=srvAt(ts),b=srvAt(now||Date.now()/1000),hm=`${p2(a.h)}:${p2(a.mi)}`;return a.y===b.y&&a.mo===b.mo&&a.d===b.d?hm:`${a.d} ${MON[a.mo]} ${hm}`}
function dur(s){s=Math.max(0,Math.round(num(s)));if(s<90)return s+" s";const m=Math.round(s/60);if(m<60)return m+" min";const h=Math.floor(m/60),r=m%60;return r?`${h} h ${p2(r)} min`:`${h} h`}
const mibs=r=>{const v=num(r)/2**20;return v.toFixed(v>=10?0:1)+" MiB/s"},games=n=>`${num(n)} game${num(n)===1?"":"s"}`;
const countsText=r=>{const p=[];if(num(r.built))p.push(`${num(r.built)} built`);if(num(r.copied))p.push(`${num(r.copied)} copied`);if(num(r.failed))p.push(`${num(r.failed)} failed`);if(num(r.deferred))p.push(`${num(r.deferred)} waiting for space`);if(num(r.cancelled))p.push(`${num(r.cancelled)} cancelled`);return p.join(", ")||"nothing to do"};
const bar=pct=>`<div class="bar"><i style="width:${Math.max(0,Math.min(100,num(pct)))}%"></i></div>`;
const jobName=x=>esc(String(x.name||x.title||x.title_id||"").replace(/\.pkg$/,""));
// the run being shown: the engine is up and its status is of this run (the last run's finished status is not)
const liveRun=s=>s.engine_running&&s.status&&s.status.run&&!s.status.finished?s.status.run:null;
function headerText(s){const run=liveRun(s);let t;
  const paused=s.schedule_pause&&s.schedule&&s.schedule.enabled?` · schedule ${pauseLabel(s.schedule_pause,s.now)}`:"";
  if(s.engine_running){t=s.stop_mode==="now"?"Stopping now":s.stop_requested?"Stopping after current":"Running";if(run&&run.phase!=="scanning")t+=` · ${num(run.done)} of ${num(run.total)} done${run.remaining!=null?` · about ${dur(run.remaining)} left`:""}`;t+=paused}
  else if(s.scan.running)t="Scanning…"+paused;
  else{t="Idle";if(paused)t+=paused;else if(s.next_run)t+=` · next run ${s.next_run.label}`;const lr=s.last_run;if(lr){const c=countsText(lr);t+=lr.interrupted?" · the last run did not finish":` · last run ${c==="nothing to do"?"had nothing to do":c}`}}
  return t+(s.clock?` · server time ${s.clock.time} ${s.clock.tz}`:"")}
function renderRun(s){const run=liveRun(s),now=s.now;let line,sub="",pct=null;
  if(s.engine_running){
    if(!run){line="Starting…";sub="a run has just been started"}
    else if(run.phase==="scanning"){line="Running · scanning the library…";sub=`started ${clk(run.started,now)} (${esc(run.trigger||"manual")}) · what to build is worked out first`}
    else{line=`${s.stop_mode==="now"?"Stopping now":s.stop_requested?"Stopping after the current games":"Running"} · ${num(run.done)} of ${num(run.total)} done${num(run.failed)?` · <span class="err">${num(run.failed)} failed</span>`:""}`;pct=Math.round(num(run.progress)*100);
      sub=`started ${clk(run.started,now)} (${esc(run.trigger||"manual")}) · ${run.remaining!=null?`about ${dur(run.remaining)} left`:"estimating the time left…"}`}}
  else if(s.scan.running){line="Scanning for new games…"}
  else{const lr=s.last_run;
    line=lr?(lr.interrupted?`Idle · the last run did not finish (${num(lr.done)} of ${num(lr.total)} done)`:`Idle · last run ${clk(lr.finished,now)}: ${games(lr.total)}, ${esc(countsText(lr))}`):"Idle · nothing has run yet";
    sub=s.schedule_pause&&s.schedule&&s.schedule.enabled?`Schedule ${esc(pauseLabel(s.schedule_pause,now))}: no run starts by itself. Press Resume schedule, or Run now.`
      :s.next_run?`Next run ${esc(s.next_run.label)}${lr&&lr.trigger?` · the last one was ${esc(lr.trigger)}`:""}`:"Not scheduled: press Run now, or set times under Settings."}
  const r=$("#runLine");const html=line;if(r.innerHTML!==html)r.innerHTML=html;
  $("#runBar").hidden=pct==null;if(pct!=null){$("#runBar i").style.width=pct+"%";$("#runPct").textContent=pct+"%"}
  $("#runSub").innerHTML=sub}
const cancelBtn=(x,canc,phase,label)=>canc.has(x.title_id)?`<span class="muted small">${phase==="queue"?"Removing…":"Cancelling…"}</span>`
  :`<button class="btn sm" data-cancel="${esc(x.title_id)}" data-name="${jobName(x)}" data-phase="${phase}">${label||"Cancel"}</button>`;
function buildRow(x,now,canc=new Set()){const stages=[1,3,5].includes(num(x.stage))&&x.part;
  const what=x.phase==="building"?`<span title="${esc(x.stage_text?"UltraPack: "+x.stage_text:"")}">${esc(x.stage_name||"Building")}</span> (${num(x.stage)} of 5)${stages?` · ${num(x.stage_pct)}%`:""}`
    :`${esc(x.stage_name)}${x.total_bytes?` · ${gib(x.bytes)} of ${gib(x.total_bytes)} · ${num(x.stage_pct)}%`:""}`;
  const left=x.remaining!=null?`about ${dur(x.remaining)} left`:"estimating…";
  return `<div class="job"><div class="row"><b>${jobName(x)}</b><span class="jobbtn small">${x.log?`<a href="#" data-log="pkg/${esc(x.log)}">log</a>`:""}${cancelBtn(x,canc,"building")}</span></div>
   <div class="small">${what} · ${dur(now-num(x.since))} elapsed · ${left}${x.rate?` · ${mibs(x.rate)}`:""}</div>${x.progress!=null?bar(num(x.progress)*100):""}</div>`}
function prepRow(x,now,canc=new Set()){const known=num(x.size)>0;
  return `<div class="job"><div class="row"><b>${jobName(x)}</b><span class="jobbtn small">${cancelBtn(x,canc,"preparing")}</span></div><div class="small">${x.ps4?"copying PS4 packages":"copying &amp; verifying"}${known?` · ${gib(x.bytes)} of ${gib(x.size)} · ${num(x.pct)}%`:` · ${gib(x.bytes)}`}${x.rate?` · ${mibs(x.rate)}`:""}${x.remaining!=null?` · about ${dur(x.remaining)} left`:""} · ${dur(now-num(x.since))} elapsed</div>${known?bar(num(x.pct)):""}</div>`}
let resOpen=false,healthOpen=false,qOpen=false;
document.addEventListener("toggle",e=>{if(e.target.id==="resDetails")resOpen=e.target.open;if(e.target.id==="healthBox")healthOpen=e.target.open;if(e.target.id==="qMore")qOpen=e.target.open},true);
function resultsTable(r,now){return `<table>${r.map(x=>`<tr><td>${esc(x.name||x.title)}</td><td><span class="tag t-${cls(x.status)}">${esc(x.status==="deferred"?"waiting for space":x.status)}</span></td><td class="small muted" title="${esc(x.why_host||x.why||"")}">${esc(x.summary||x.why_host||x.why||x.validate||"")} ${x.build_min?`· build ${esc(x.build_min)} min`:""}${x.at?` · ${esc(clk(x.at,now))}`:""}</td></tr>`).join("")}</table>`}
function renderLive(s){const run=liveRun(s),st=s.status||{},now=s.now,r=st.results||[];let h="";
  if(run&&run.phase!=="scanning"){
    const B=st.builds||[],P=st.preparing||[],Q=st.queue||[],canc=new Set(s.cancelling||[]);
    const qRow=x=>`<div class="row small" style="padding:2px 0"><span>${jobName(x)} <span class="muted">${x.size?gib(x.size):""}${x.ps4?" · PS4":""}${x.ready?" · prepared, waiting for a free slot":""}</span></span><span class="jobbtn">${cancelBtn(x,canc,"queue","Remove from this run")}</span></div>`;
    if(B.length)h+=`<div class="card sec"><h2>Building (${B.length})</h2>${B.map(x=>buildRow(x,now,canc)).join("")}</div>`;
    if(P.length)h+=`<div class="card sec"><h2>Preparing (${P.length})</h2>${P.map(x=>prepRow(x,now,canc)).join("")}</div>`;
    if(Q.length)h+=`<div class="card sec"><h2>Up next (${Q.length})${num(run.queue_bytes)?` · ${gib(run.queue_bytes)}`:""}</h2>${Q.slice(0,6).map(qRow).join("")}${Q.length>6?`<details id="qMore" ${qOpen?"open":""}><summary class="small muted">and ${Q.length-6} more</summary>${Q.slice(6).map(qRow).join("")}</details>`:""}</div>`;
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
  lastS=s;$("#state").textContent=headerText(s);
  syncRun();renderPause(s);renderAskAgain();failBox(s);
  if(s.failures_rev!==lastFailRev){lastFailRev=s.failures_rev;loadFailures()}
  const fin=[s.scan.finished,s.run?.finished].join("|");if(fin!==lastFin){const first=lastFin===undefined;lastFin=fin;if(!first)loadSetup()}   // a scan changes what the biggest game is
  renderRun(s);renderLive(s);renderFolders(s);renderHealth(s);
  const c=s.plan_counts;
  $("#kpis").innerHTML=[["To build",c.build],...(c.copy?[["PS4 to copy",c.copy]]:[]),["Built",c.done],["Failed",c.failed,c.failed>0],["Last scan",s.plan_time?ago(s.plan_time):"never"]].map(([k,v,bad])=>`<div class="kpi${bad?" bad":""}"><b>${esc(v??"—")}</b><span>${esc(k)}</span></div>`).join("");
}
// under the patch folder input: where that folder is on the NAS, or where to put the files
function patchHint(o,tid,cfgHost){
  if(o.patch)return o.patch_host?`On your NAS: ${pathHtml(o.patch_host,o.patch)}`:`${pathHtml(null,o.patch)} (no host location is set for this folder)`;
  return cfgHost?`Put the files in <code>${esc(cfgHost.replace(/[\\/]+$/,"")+"/patches/"+tid)}</code> on your NAS and enter <code>/config/patches/${esc(tid)}</code> here (a path on the NAS works too).`
    :`Inside the container. Say where /config is on your NAS (Settings > Folders) to see the real location.`}
// ---- Library: every game, why this copy was chosen, what its options do. A fetch builds the whole table once; filtering only hides rows
// (no request, nothing re-rendered, so an open panel and an unsaved edit stay); a re-render for new data puts open panels and edits back.
let libData={plan:[],overrides:{},status:{},unrec:[]},libKey=null,libUi={open:new Set(),edits:{}},libBusy=false;
const VERDICT={chosen:"used",verifies:"checks the image",older:"older",packed:"packed",same_version:"same version",not_used:"not used"};
const README_BASE="https://github.com/zhangchi198/ps5-pkg-station";
const libKeyOf=s=>[s.plan_time,JSON.stringify(s.plan_counts||{}),s.failures_rev,s.config_rev].join("|");   // what the Library shows changes when one of these does
async function fetchLib(){
  const [pl,ov,s,un]=await Promise.all([api("/api/plan"),api("/api/overrides"),api("/api/status"),api("/api/unrecognised")]);
  plan=pl;overrides=ov;libData={plan:pl,overrides:ov,status:s,unrec:un};libKey=libKeyOf(s)}
async function loadLib(){await fetchLib();renderLib()}
// a title's firmware against the console's; the copy that will be used decides, as the server worked out for the settings of the moment
function fwLine(p){const f=p.fw_check;if(!f)return"";
  if(f.ok===false)return `needs fw ${esc(f.need)} · <span class="tag t-failed">above your console (${esc(f.console)}): won't run</span>`;
  if(f.ok===true)return `needs fw ${esc(f.need)} · your console ${esc(f.console)}: ok`;
  return `needs fw ${esc(f.need)} · <a href="#" data-goto="console_fw">set your console firmware</a>`}
function candList(p){const c=p.candidates||[];if(!c.length)return"";
  return `<div class="small"><b>Copies found (${c.length})</b></div><ul class="cands">${c.map(x=>`<li><span class="tag v-${cls(x.verdict||"")}">${esc(VERDICT[x.verdict]||x.kind||"")}</span> ${pathHtml(x.source_host,x.source)}
    <div class="small muted">${esc(x.kind_label||x.kind||"")} · v${esc(x.version||"?")}${x.fw?` · needs fw ${esc(x.fw)}`:""}${x.size?` · ${esc(gib(x.size))}`:""}${x.packed?` · ${esc(x.packed)}-packed`:""}</div>${x.reason?`<div class="small">${esc(x.reason)}</div>`:""}</li>`).join("")}</ul>`}
function backportText(p){const b=p.backport,out=[];
  if(b){const con=b.console_fw?`your console (${esc(b.console_fw)})`:"your console";
    if(b.present)out.push(`<b>Backport:</b> the image needs fw ${esc(b.image_fw)}; the original archive needs fw ${esc(b.original_fw)}. `+(b.decision==="original"?`${b.console_fw?`Your console (${esc(b.console_fw)})`:"Your console"} runs the original, so the original will be used (${esc(b.decided_by)}).`
      :b.decision==="image"?`The original needs newer firmware than ${con}, so the image's modified executables are kept (${esc(b.decided_by)}).`:`Not decided: ${esc(b.decided_by)}. Set your console firmware under Settings, or choose under Options.`)
      +` <span class="muted">The files are compared when the build starts; what it did is shown here afterwards.</span>`);
    else out.push(`<b>Checked against the original:</b> the image is compared file by file with the original archive (${b.image_fw&&b.image_fw===b.original_fw?`both ask for fw ${esc(b.image_fw)}`:"no lower-firmware backport"}).${p.patched?` The package itself carries fw ${esc(p.fw||"?")}, from the patch's param.json.`:""}`)}
  const l=p.last_backport;if(l)out.push(`<b>Last build:</b> ${l.differs===false?"the image and the original archive matched.":`the image differed from the original in ${num(l.files)} file(s): the ${esc(l.used)} was used (${esc(l.decided_by||"")}).`}`);
  return out.map(t=>`<div class="small" style="margin:4px 0">${t}</div>`).join("")}
function optionsForm(p,o,cfgHost){const c=p.candidates||[];
  return `<div class="ovr" data-tid="${esc(p.title_id)}"><div class="small"><b>Options</b></div>
     <label class="small">Copy to use <select data-k="source"><option value="">automatic</option>${c.map(x=>`<option value="${esc(x.source)}" ${o.source===x.source?"selected":""}>${esc(x.kind_label||x.kind)} v${esc(x.version)}${x.packed?" ["+esc(x.packed)+"]":""} · ${esc(x.source_host||x.source+" (container path)")}</option>`).join("")}</select></label>
     <label class="small">If a backport exists <select data-k="modified_exec"><option value="">decide by console firmware</option><option value="image" ${o.modified_exec==="image"?"selected":""}>keep the backport (the image)</option><option value="original" ${o.modified_exec==="original"?"selected":""}>use the original</option></select></label>
     <label class="small">Extra files folder (copied over the game) <input type="text" data-k="patch" value="${esc(o.patch||"")}" placeholder="/config/patches/…"></label>
     <div class="small muted" data-patchhint>${patchHint(o,p.title_id,cfgHost)}</div>
     <label class="small">Note <input type="text" data-k="note" value="${esc(o.note||"")}"></label>
     <label class="small"><input type="checkbox" data-k="skip_verify" ${o.skip_verify?"checked":""}> don't check against the original archive (keep this copy anyway)</label>
     <label class="small"><input type="checkbox" data-k="skip" ${o.skip?"checked":""}> never build this game</label>
     <div class="row"><button class="btn" data-do="saveov">Save options</button>${p.action==="build"?`<button class="btn" data-do="buildone" data-tid="${esc(p.title_id)}">Build only this…</button>`:""}<span class="small muted"></span></div></div>`}
function libRow(p,cfgHost){const o=libData.overrides[p.title_id]||{},tid=esc(p.title_id),search=[p.title,p.title_id,p.source,...(p.candidates||[]).map(x=>x.source)].join(" ").toLowerCase();
  const why=p.help==="packed"?`<div class="small" style="margin-top:4px">Why this blocks the build, and what to do: <a href="${README_BASE}#packed-copies" target="_blank" rel="noopener noreferrer">Packed copies</a>.</div>`:"";
  return `<tr data-tid="${tid}" data-action="${esc(cls(p.action))}" data-search="${esc(search)}"><td><b>${esc(p.title||p.title_id)}</b><div class="small muted">${tid} · ${p.version?"v"+esc(p.version):"version read after extraction"} · ${fwLine(p)||"firmware not known"}</div>
    ${p.patched?`<div class="small muted">Named after the patch's param.json: fw ${esc(p.fw||"?")} (the source has ${esc(p.source_fw||"?")})</div>`:""}
    <details class="why" data-tid="${tid}"><summary class="small">Why this copy · Options${Object.keys(o).length?" (changed)":""}</summary>${candList(p)}${backportText(p)}${optionsForm(p,o,cfgHost)}</details></td>
    <td>${esc(p.version)}</td><td class="small">${esc(((p.candidates||[]).find(x=>x.source===p.source)||{}).kind_label||p.kind||"")}<div>${pathHtml(p.source_host,p.source)}</div></td>
    <td>${p.failure?failTag(p.failure.state==="waiting"||p.action==="failed"?p.failure:{state:"failed"}):`<span class="tag t-${cls(p.action)}">${esc(p.action)}</span>`}${p.failure&&p.failure.state==="failed"&&p.action==="build"?` <span class="tag t-build">will retry</span>`:""}
     <div class="small muted">${esc(p.why_host||p.why)}</div><div class="small"><a href="#logs?game=${tid}">Logs</a></div>${why&&p.action==="skip"?why:""}${p.ps4&&p.files?ps4Files(p):""}${p.failure?`${failureButtons(p.failure,false)}<details><summary class="small">Why, and the log</summary>${failureBody(p.failure)}</details>`:""}</td></tr>`}
function unrecBox(un,s){if(!un.length){$("#libUnrec").innerHTML="";return}
  $("#libUnrec").innerHTML=`<details class="card" id="unrecDetails"><summary><b>Not recognised (${un.length})</b> <span class="muted small">looked at, but not usable</span></summary>
    <div class="small muted" style="margin:6px 0">These files were scanned and could not be identified as a game. A patch, an add-on or an unrelated archive is not listed here, only what failed. Fix or remove the file and scan again.</div>
    <table>${un.map(x=>`<tr><td>${pathHtml(x.source_host,x.source)}<div class="small muted">${esc(x.origin==="library"?"in the game library":x.origin==="download"?"in downloads":x.origin||"")}</div></td><td class="small err">${esc(x.error)}</td></tr>`).join("")}</table></details>`}
function libNote(s){const r=s.rescan||{needed:[]},busy=s.engine_running||(s.scan&&s.scan.running),btn=`<button class="btn" data-do="rescan" ${busy?"disabled":""}>Rescan now</button>${busy?` <span class="small">A run or scan is active: rescan when it ends.</span>`:""}`;
  $("#libNote").innerHTML=r.stale?`<div class="banner warn"><b>This list was made by an older version of the app.</b> What it shows about firmware and backports is not judged until it is refreshed. ${btn}</div>`
    :r.needed.length?`<div class="banner warn"><b>Settings changed since the last scan:</b> ${r.needed.map(esc).join(", ")}. The list below shows them after a rescan. ${btn}</div>`:""}
function renderLib(){const {plan:pl,status:s,unrec:un}=libData,cf=s.folders?.config,cfgHost=cf&&cf.host&&!cf.partial?cf.host:null;libCfgHost=cfgHost;
  const n=a=>pl.filter(p=>p.action===a).length,bits=[[`${pl.length} titles`,""],[`${n("build")} to build`,"build"],...(n("copy")?[[`${n("copy")} PS4 to copy`,"copy"]]:[]),[`${n("done")} done`,"done"],...(n("failed")?[[`${n("failed")} failed`,"failed"]]:[]),
    ...(n("skip")?[[`${n("skip")} skipped`,"skip"]]:[]),...(n("error")?[[`${n("error")} problem${n("error")===1?"":"s"}`,"error"]]:[]),...(un.length?[[`${un.length} not recognised`,"unrec"]]:[])];
  $("#libSum").innerHTML=bits.map(([t,k])=>k==="unrec"?`<a href="#" data-libunrec>${esc(t)}</a>`:k?`<a href="#" data-libfilter="${k}" class="${$("#libAct").value===k?"on":""}">${esc(t)}</a>`:`<b>${esc(t)}</b>`).join(" · ");
  const empty=t=>`<tr><td colspan="4">${t}</td></tr>`;
  $("#libRows").innerHTML=!pl.length?empty(!s.plan_time?`<b>No scan yet.</b> <span class="muted">Press Scan to look through your game library for games to build.</span> <button class="btn" data-do="scan">Scan for new games</button>`
      :`<b>The last scan found no games.</b> <span class="muted">Nothing usable was found in <code>${esc(s.disks.library?.path||"")}</code>${s.disks.downloads?.error?"":` or <code>${esc(s.disks.downloads?.path||"")}</code>`}. Check the folders on the Dashboard, then scan again.</span>${un.length?` <span class="muted">${un.length} file${un.length===1?"":"s"} could not be identified: see below.</span>`:""}`)
    :pl.map(p=>libRow(p,cfgHost)).join("")+`<tr id="libNone" hidden><td colspan="4"><span class="muted">No titles match the filter.</span></td></tr>`;
  unrecBox(un,s);libNote(s);
  $("#libTime").textContent=s.plan_time?`scanned ${ago(s.plan_time)} ago`:"not scanned yet";
  restoreLibUi();applyLibFilter()}
// what the user had open or typed survives a re-render
function restoreLibUi(){document.querySelectorAll("#libRows details.why").forEach(d=>{if(libUi.open.has(d.dataset.tid))d.open=true});
  for(const [tid,ed] of Object.entries(libUi.edits)){const box=document.querySelector(`#libRows .ovr[data-tid="${CSS.escape(tid)}"]`);if(!box)continue;
    for(const [k,v] of Object.entries(ed)){const e=box.querySelector(`[data-k="${CSS.escape(k)}"]`);if(!e)continue;if(e.type==="checkbox")e.checked=v;else e.value=v}}}
function applyLibFilter(){const f=$("#libFilter").value.toLowerCase().trim(),a=$("#libAct").value;let shown=0,all=0;
  document.querySelectorAll("#libRows tr[data-tid]").forEach(tr=>{all++;const show=(!a||tr.dataset.action===a)&&(!f||tr.dataset.search.includes(f));tr.hidden=!show;if(show)shown++});
  const none=$("#libNone");if(none)none.hidden=shown>0;
  $("#libShown").textContent=(f||a)&&all?`showing ${shown} of ${all} titles`:"";
  document.querySelectorAll("#libSum a[data-libfilter]").forEach(x=>x.classList.toggle("on",x.dataset.libfilter===a))}
$("#libFilter").oninput=applyLibFilter;$("#libAct").onchange=applyLibFilter;
$("#libSum").addEventListener("click",e=>{const u=e.target.closest("[data-libunrec]");if(u){e.preventDefault();const d=$("#unrecDetails");if(d){d.open=true;d.scrollIntoView({block:"start"})}return}
  const a=e.target.closest("[data-libfilter]");if(!a)return;e.preventDefault();$("#libAct").value=$("#libAct").value===a.dataset.libfilter?"":a.dataset.libfilter;applyLibFilter()});
$("#libRows").addEventListener("toggle",e=>{const d=e.target;if(d.classList&&d.classList.contains("why")){if(d.open)libUi.open.add(d.dataset.tid);else libUi.open.delete(d.dataset.tid)}},true);
const noteEdit=e=>{const k=e.target.dataset&&e.target.dataset.k,box=e.target.closest&&e.target.closest(".ovr");if(!k||!box)return;(libUi.edits[box.dataset.tid]=libUi.edits[box.dataset.tid]||{})[k]=e.target.type==="checkbox"?e.target.checked:e.target.value};
$("#libRows").addEventListener("input",noteEdit);$("#libRows").addEventListener("change",noteEdit);
$("#libRows").addEventListener("click",e=>{const a=e.target.closest("[data-goto]");if(!a)return;e.preventDefault();gotoField(a.dataset.goto)});
// the Library follows what changes (a build finishing, a scan, a saved option) without a visit to the tab; not while someone is typing in it
async function refreshLibIfChanged(s){if(tab!=="lib"||libKey===null||libBusy||libKeyOf(s)===libKey)return;
  const ae=document.activeElement;if(ae&&ae.closest&&ae.closest("#libRows .ovr"))return;
  libBusy=true;try{await fetchLib();renderLib()}catch(e){}finally{libBusy=false}}
async function saveOv(btn){const box=btn.closest(".ovr"),tid=box.dataset.tid,o={};box.querySelectorAll("[data-k]").forEach(e=>o[e.dataset.k]=e.type==="checkbox"?e.checked:e.value);
  const m=btn.parentElement.querySelector("span"),prev=libData.status.plan_time;
  try{const r=await api("/api/overrides/"+encodeURIComponent(tid),{method:"PUT",body:JSON.stringify(o)});delete libUi.edits[tid];m.className="small muted";
    m.textContent=r.replan==="now"?"Saved. Planning again…":r.replan==="after_run"?"Saved. A run is active: this applies from the next run, and the list updates when the run ends.":"Saved. A scan is running: the list updates when it ends.";
    const pi=box.querySelector('[data-k="patch"]');if(pi)pi.value=r.patch||"";   // a path typed as on the NAS is stored as the container path
    const h=box.querySelector("[data-patchhint]");if(h)h.innerHTML=patchHint(r,tid,libCfgHost);
    if(r.replan==="now")afterReplan(tid,prev)}catch(e){m.textContent=e.message;m.className="small err"}}
// a scan is about a second with the cache warm: look for the new plan until it is there
async function afterReplan(tid,prev){for(let i=0;i<30;i++){await new Promise(r=>setTimeout(r,1000));let s;try{s=await api("/api/status")}catch(e){return}
  if(s.plan_time!==prev&&!s.scan.running){try{await fetchLib();renderLib()}catch(e){}toast("Saved and applied: "+tid);return}}}
function buildEstimate(p){const pace=libData.status.pace||{},gibs=num(p.size)/2**30;
  if(!pace.build_per_gib||!gibs)return"no estimate yet: no build has finished on this machine";
  return "about "+dur((pace.build_per_gib+(pace.prep_per_gib||0))*gibs)+", from the builds this machine has done"}
async function buildOne(t){const p=libData.plan.find(x=>x.title_id===t)||{},free=libData.status.disks&&libData.status.disks.temp?libData.status.disks.temp.free:null;
  if(!(await askConfirm({title:`Build ${p.title||t} now?`,html:`<p>${num(p.size)?esc(gib(p.size))+" to read, ":""}${esc(buildEstimate(p))}.</p><p class="small muted">${free!=null?`Free on the work drive: ${esc(bytesText(free))}. `:""}It runs in the background; Stop on the Dashboard ends it.</p>`,ok:"Build it"})))return;
  try{await api("/api/run",{method:"POST",body:JSON.stringify({only:[t]})});say("Started "+t);tab="dash";document.querySelector('nav button[data-tab=dash]').click()}catch(e){alertBox(e.message)}}
function alertBox(m){say(m,1)}
document.addEventListener("click",e=>{const b=e.target.closest("[data-do]");if(!b)return;e.preventDefault();
  if(b.dataset.do==="scan"){doScan();goTab("dash")}else if(b.dataset.do==="rescan")doScan();else if(b.dataset.do==="saveov")saveOv(b);else if(b.dataset.do==="buildone")buildOne(b.dataset.tid)});
// ---- History: what each run did, and the packages it made. Two views of the same facts; the Runs view is history.jsonl, the Packages
// view is the ledger (one row per game: the latest package) with what each entry says normalised, and "not recorded" where it doesn't.
const whenText=ts=>{if(!ts)return"not recorded";const a=srvAt(ts),dt=new Date(Date.UTC(a.y,a.mo,a.d));return`${WD[dt.getUTCDay()]} ${a.d} ${MON[a.mo]} ${p2(a.h)}:${p2(a.mi)}`};
const minText=m=>m==null?"not recorded":m<1?"under a minute":dur(m*60);
const sizeText=b=>b==null?"not recorded":b<1024?b+" B":b<2**20?Math.round(b/1024)+" KiB":b<2**30?(b/2**20).toFixed(1)+" MiB":bytesText(b);
const JOB_TAG={built:"built",copied:"copied",failed:"failed",cancelled:"cancelled",deferred:"waiting for space"};
const runCountsText=c=>{const o=[];for(const k of["built","copied","failed","cancelled","deferred"])if(num((c||{})[k]))o.push(`${num(c[k])} ${k==="deferred"?"waiting for space":k}`);return o.join(" · ")||"nothing to do"};
const checksText=(c,raw)=>c?`${num(c.passed)} checks passed${num(c.warned)?`, ${num(c.warned)} warned`:""}${num(c.failed)?`, <b class="err">${num(c.failed)} failed</b>`:""}`:raw?esc(raw):"";
let histView="runs",runList=[],runsTotal=0,runOpen=new Set(),runJobs={},pkgData=null,failData=[];
try{histView=localStorage.getItem("ps5pkg.histView")==="pkgs"?"pkgs":"runs"}catch(e){}
function setHistView(v){histView=v;try{localStorage.setItem("ps5pkg.histView",v)}catch(e){}
  document.querySelectorAll("[data-hv]").forEach(b=>b.classList.toggle("on",b.dataset.hv===v));$("#histRuns").hidden=v!=="runs";$("#histPkgs").hidden=v!=="pkgs"}
document.querySelectorAll("[data-hv]").forEach(b=>b.onclick=()=>{setHistView(b.dataset.hv);loadHist().catch(e=>say(e.message,1))});
function jobRow(j){const t=j.title||j.name||j.tid,ok=j.status==="built"||j.status==="copied";
  const what=ok?[j.bytes?sizeText(j.bytes):null,j.prep_min!=null?`prepared in ${minText(j.prep_min)}`:null,j.build_min!=null?`built in ${minText(j.build_min)}`:null,j.level!=null?`compression level ${j.level}`:null].filter(Boolean).map(esc).join(" · ")+(checksText(j.checks,j.validate)?` · ${checksText(j.checks,j.validate)}`:"")
    :esc(j.summary||j.why_host||j.why||"");
  return `<tr><td><b>${esc(t)}</b><div class="small muted">${esc(j.tid||"")}${j.name?` · ${esc(j.name)}`:""}</div></td><td><span class="tag t-${cls(j.status)}">${esc(JOB_TAG[j.status]||j.status||"?")}</span><div class="small muted">${esc(whenText(j.at))}</div></td>
    <td class="small">${what}${j.status==="failed"&&j.code?`<div class="muted">${esc(j.code)}${j.phase?` · while ${esc(j.phase)}`:""}</div>`:""}</td><td class="small">${j.log?`<a href="#logs/${esc(j.log)}">Build log</a>`:""}</td></tr>`}
function runBody(r){const jobs=runJobs[r.run];if(!jobs)return`<span class="muted small">Loading…</span>`;
  return `${jobs.length?`<table><thead><tr><th>Game</th><th>Result</th><th>What happened</th><th></th></tr></thead><tbody>${jobs.map(jobRow).join("")}</tbody></table>`:`<div class="muted small">This run did not build anything.</div>`}${r.output_log?`<div class="small" style="margin-top:6px"><a href="#logs/${esc(r.output_log)}">Raw output of this run</a></div>`:""}`}
const runCard=r=>`<details class="card run" data-run="${esc(r.run)}" ${runOpen.has(r.run)?"open":""}><summary><b>${esc(whenText(r.started))}</b><span class="muted small">${esc(r.trigger||"run")} · ${r.seconds!=null?esc(dur(r.seconds)):"duration not recorded"}${r.interrupted?" · did not finish":""}</span>
  <span class="small">${esc(runCountsText(r.counts))}</span>${r.bytes?`<span class="muted small">${esc(sizeText(r.bytes))}</span>`:""}</summary><div class="runbody">${runOpen.has(r.run)?runBody(r):""}</div></details>`;
function renderRuns(){const el=$("#histRuns");
  el.innerHTML=!runList.length?`<div class="card"><b>No run has been recorded yet.</b> <span class="muted">Every run from now on is listed here with its games, including failures and cancellations. Packages built before that are under Packages.</span></div>`
    :runList.map(runCard).join("")+(runList.length<runsTotal?`<div class="card"><button class="btn" id="runsMore">Show older runs (${runsTotal-runList.length} more)</button></div>`:"")}
async function loadRuns(more){const r=await api(`/api/history/runs?limit=30&offset=${more?runList.length:0}`);runsTotal=r.total;runList=more?runList.concat(r.runs):r.runs;
  renderRuns();for(const id of runOpen)if(!runJobs[id])fetchRun(id)}
async function fetchRun(id){try{runJobs[id]=(await api("/api/history/runs/"+encodeURIComponent(id))).jobs}catch(e){runJobs[id]=[]}
  const d=document.querySelector(`#histRuns [data-run="${CSS.escape(id)}"]`),r=runList.find(x=>x.run===id);if(d&&r)d.querySelector(".runbody").innerHTML=runBody(r)}
$("#histRuns").addEventListener("toggle",e=>{const d=e.target.closest&&e.target.closest("[data-run]");if(!d||e.target!==d)return;const id=d.dataset.run;
  if(d.open){runOpen.add(id);if(runJobs[id])d.querySelector(".runbody").innerHTML=runBody(runList.find(x=>x.run===id));else{d.querySelector(".runbody").innerHTML=`<span class="muted small">Loading…</span>`;fetchRun(id)}}else runOpen.delete(id)},true);
$("#histRuns").addEventListener("click",e=>{if(e.target.id==="runsMore")loadRuns(true).catch(err=>say(err.message,1))});
// packages
function pkgRow(p){const tid=esc(p.tid),bits=p.ps4?[`copied as they are${p.files?` · ${plural(p.files.length,"package")}`:""}`]
    :[p.prep_min!=null?`prepared in ${minText(p.prep_min)}`:"preparation time not recorded",p.build_min!=null?`built in ${minText(p.build_min)}`:p.total_min!=null?`took ${minText(p.total_min)} in total (an older record)`:"build time not recorded",p.level!=null?`compression level ${p.level}`:"compression level not recorded"];
  const tags=[p.ps4?`<span class="tag">PS4</span>`:"",!p.exists?`<span class="tag t-waiting" title="The package is not where the history says it is: the next scan plans it again">file missing</span>`:"",p.marked?`<span class="tag t-build">rebuild requested</span>`:""].filter(Boolean).join(" ");
  return `<div class="pkg" data-tid="${tid}"><div class="row"><b>${esc(p.title||p.tid)}</b><span class="muted small">${tid}${p.version?` · v${esc(p.version)}`:""}</span>${tags}<span class="small" style="margin-left:auto">${p.bytes!=null?esc(sizeText(p.bytes)):"size not recorded"} · built ${esc(whenText(p.built_ts))}</span></div>
    <div class="small muted">${bits.map(esc).join(" · ")} · ${checksText(p.checks,p.validate)||"checks not recorded"}</div>
    ${p.output?`<div class="small">${p.ps4?"Folder":"Package"}: ${pathHtml(p.output_host,p.output)}</div>`:`<div class="small muted">Location not recorded</div>`}
    <div class="small muted">Source: ${p.source?pathHtml(p.source_host,p.source):"not recorded"}</div>
    ${p.backport&&p.backport.differs?`<div class="small muted">The image differed from the original in ${num(p.backport.files)} file(s): the ${esc(p.backport.used)} was used.</div>`:""}
    <div class="btnrow">${p.log?`<a class="btn sm" href="#logs/${esc(p.log)}">Build log${p.log_guessed?" (probably)":""}</a>${p.ps4?"":`<a class="btn sm" href="#logs/${esc(p.log)}?find=validate">Validation report</a>`}`:""}
      ${p.sha256?`<span class="small muted"><code>${esc(p.sha256.slice(0,16))}…</code></span><button class="btn sm" data-copy="${esc(p.sha256)}" title="Copy the whole SHA-256">Copy SHA-256</button>`:""}
      ${p.marked?`<button class="btn sm" data-unmark="${tid}">Undo rebuild</button>`:`<button class="btn sm" data-rebuild="${tid}">Rebuild</button>`}<button class="btn sm" data-forget="${tid}">Forget</button></div></div>`}
function failedPkgRow(f){return `<div class="pkg"><div class="row"><b>${esc(f.name||f.title||f.title_id)}</b><span class="muted small">${esc(f.title_id)}</span>${failTag(f)}<span class="small" style="margin-left:auto">${esc(f.at_text||"")}</span></div>
    <div class="small">${esc(f.summary)}</div><div class="small muted">${esc(f.next||"")} · attempt ${esc(f.attempts||1)} · ${esc(f.phase||"")}</div>${failureButtons(f,false)}<details><summary class="small">Details and the log of this attempt</summary>${failureBody(f)}</details></div>`}
function renderPkgs(){if(!pkgData)return;const q=$("#pkgFind").value.trim().toLowerCase(),st=$("#pkgStatus").value,so=$("#pkgSort").value,all=pkgData.packages||[];
  const hay=p=>[p.title,p.tid,p.name].join(" ").toLowerCase();
  let pk=all.filter(p=>(!q||hay(p).includes(q))&&(!st||(st==="ok"?p.exists&&!p.ps4:st==="ps4"?p.ps4:st==="missing"?!p.exists:st==="marked"?p.marked:false)));
  let fl=failData.filter(f=>(!st||st==="failed")&&(!q||[f.title,f.title_id,f.name].join(" ").toLowerCase().includes(q)));
  const key={new:p=>-(p.built_ts||0),old:p=>p.built_ts||0,size:p=>-(p.bytes||0),time:p=>-(p.minutes||0),name:p=>(p.title||p.tid).toLowerCase()}[so];
  pk=pk.slice().sort((a,b)=>{const x=key(a),y=key(b);return typeof x==="string"?x.localeCompare(y):x-y});
  const t=pkgData.totals||{};
  $("#pkgSum").textContent=`Packages (${num(t.count)}${t.bytes?` · ${sizeText(t.bytes)}`:""})`;
  $("#pkgTotals").textContent=`${plural(num(t.present),"package")} on disk${num(t.count)>num(t.present)?`, ${num(t.count)-num(t.present)} missing`:""}${t.minutes_known?` · ${dur(num(t.minutes)*60)} of work${t.minutes_known<t.count?` (${t.minutes_known} of ${t.count} have a time recorded)`:""}`:""}${failData.length?` · ${plural(failData.length,"game")} failing`:""}${pk.length+fl.length!==num(t.count)+failData.length?` · ${pk.length+fl.length} shown`:""}`;
  $("#pkgList").innerHTML=(fl.length&&(!st||st==="failed")&&so!=="size"?fl.map(failedPkgRow).join(""):"")+pk.map(pkgRow).join("")+(!pk.length&&!fl.length?`<div class="muted"><b>${all.length||failData.length?"Nothing matches.":"Nothing built yet."}</b> Finished packages are listed here, with their checks, once a run has built one; games that failed are listed with the reason.</div>`:"");
  const r=pkgData.replaced||{items:[]};
  $("#replCard").innerHTML=r.items.length?`<h2>Replaced packages (${r.count} · ${esc(sizeText(r.bytes))})</h2><div class="small muted" style="margin-bottom:6px">When a game is built again its older package stays here as "(replaced …)". Settings > Cleanup can keep fewer.</div>
    <table><tbody>${r.items.map(x=>`<tr><td>${esc(x.name)}<div class="small">${pathHtml(x.path_host,x.path)}</div></td><td class="small">${esc(sizeText(x.bytes))}</td><td class="small">replaced ${esc(whenText(x.replaced_at))}</td><td><button class="btn sm" data-repl-del="${esc(x.name)}">Delete</button></td></tr>`).join("")}</tbody></table>`
    :`<div class="small muted">No replaced packages: a package that a newer build replaces is kept here until you delete it (Settings > Cleanup).</div>`}
async function loadPkgs(){const[d,fl]=await Promise.all([api("/api/history/packages"),api("/api/failures")]);pkgData=d;failData=Array.isArray(fl)?fl:[];renderPkgs()}
$("#pkgFind").oninput=renderPkgs;$("#pkgStatus").onchange=renderPkgs;$("#pkgSort").onchange=renderPkgs;
async function loadHist(){setHistView(histView);if(histView==="runs")await loadRuns(false);else await loadPkgs()}
async function rebuildGame(tid){const p=(pkgData.packages||[]).find(x=>x.tid===tid)||{};
  if(!(await askConfirm({title:`Build ${p.title||tid} again?`,html:`<p>The next run builds it again, from the copy the Library shows. The package you have now is replaced only when the new one is complete and checked.</p><p class="small muted">Nothing starts now: press Run now on the Dashboard, or wait for the schedule. Settings > Cleanup says what happens to the old file.</p>`,ok:"Mark for rebuild"})))return;
  try{const r=await api(`/api/history/${encodeURIComponent(tid)}/rebuild`,{method:"POST",body:"{}"});toast(r.message||"Marked for rebuild");await loadPkgs()}catch(e){toast(e.message,1)}}
async function forgetGame(tid){const p=(pkgData.packages||[]).find(x=>x.tid===tid)||{};
  const r=await showDialog({title:`Forget ${p.title||tid}?`,html:`<p>Remove it from the history. The package file stays where it is.</p><p>The planner then treats the game as never built, so the next run builds it again, unless you tick the box.</p>`,
    buttons:[{key:"go",label:"Forget it",kind:"danger"},{key:"no",label:"Cancel"}],check:"Also never build this game again"});
  if(!r||r.key!=="go")return;
  try{await api(`/api/history/${encodeURIComponent(tid)}/forget`,{method:"POST",body:JSON.stringify({skip:r.checked})});toast(r.checked?"Forgotten, and it will not be built again":"Forgotten");await loadPkgs()}catch(e){toast(e.message,1)}}
async function deleteReplaced(name){
  if(!(await askConfirm({title:"Delete this package?",html:`<p><code>${esc(name)}</code></p><p>It was replaced by a newer build of the game. Deleting it frees the space and can't be undone.</p>`,ok:"Delete it",danger:true})))return;
  try{const r=await api("/api/replaced/"+encodeURIComponent(name),{method:"DELETE"});toast(`Deleted, ${sizeText(r.freed)} freed`);await loadPkgs()}catch(e){toast(e.message,1)}}
$("#histPkgs").addEventListener("click",e=>{const b=e.target.closest("[data-rebuild],[data-forget],[data-unmark],[data-repl-del]");if(!b)return;
  if(b.dataset.rebuild)rebuildGame(b.dataset.rebuild);else if(b.dataset.forget)forgetGame(b.dataset.forget);else if(b.dataset.replDel)deleteReplaced(b.dataset.replDel);
  else api(`/api/history/${encodeURIComponent(b.dataset.unmark)}/rebuild`,{method:"DELETE"}).then(()=>{toast("The rebuild request is withdrawn");return loadPkgs()}).catch(err=>toast(err.message,1))});
// ---- Settings: built from the server's own definition of every setting (label, help, default, range), checked as you type
// by the server, saved all at once from a bar that appears when something changed. Nothing is dropped silently.
let cfg={},setBase="",setMsgTimer=null,valTimer=null,setErrors=[];
const NICE_NOTE="Lower priority = builds give way to other programs.";
const valueOf=(c,key)=>key.split(".").reduce((o,x)=>o?.[x],c);
function fieldHtml(f){const v=valueOf(cfg,f.key),id="f_"+f.key.replace(/\W/g,"_"),k=f.key;let ctl="";
  if(f.kind==="int")ctl=`<input type="number" id="${id}" data-c="${esc(k)}" data-t="n" min="${num(f.min)}" max="${num(f.max)}" step="${num(f.step)||1}" value="${esc(v)}"> <span class="small muted">${esc(f.unit||"")}${f.min!=null?` (${num(f.min)} to ${num(f.max)})`:""}</span>`;
  else if(f.kind==="bool")ctl=`<label class="small"><input type="checkbox" id="${id}" data-c="${esc(k)}" data-t="b" ${v?"checked":""}> on</label>`;
  else if(f.kind==="choice"||f.kind==="nice"){const ch=f.choices||[],has=ch.some(c=>String(c[0])===String(v));
    ctl=`<select id="${id}" data-c="${esc(k)}" data-t="${f.kind==="nice"?"n":"s"}">${ch.map(c=>`<option value="${esc(c[0])}" ${String(c[0])===String(v)?"selected":""}>${esc(c[1])}</option>`).join("")}${has?"":`<option value="${esc(v)}" selected>Custom (${esc(v)})</option>`}</select>`}
  else if(f.kind==="lines"||f.kind==="regex")ctl=`<textarea id="${id}" rows="3" data-c="${esc(k)}" data-t="lines" spellcheck="false" placeholder="${esc(f.example||"")}">${esc((v||[]).join("\n"))}</textarea>`
    +(f.kind==="regex"?`<div class="ptest"><button type="button" class="btn sm" data-ptest="${esc(k)}">Test against the last scan</button><div class="small" data-pout="${esc(k)}"></div></div>`:"");
  else if(f.kind==="times")ctl=`<div class="times" id="${id}" data-c="${esc(k)}" data-t="times">${(v||[]).map(timeRow).join("")}</div><button type="button" class="btn sm" data-addtime>Add a time</button> <span class="small muted" id="nextNote"></span>`;
  else ctl=`<input type="text" id="${id}" data-c="${esc(k)}" data-t="s" value="${esc(v)}" placeholder="${esc(f.example||"")}">`;
  const def=f.kind==="lines"||f.kind==="regex"?(f.default||[]).join(", ")||"none":f.kind==="times"?(f.default||[]).join(", ")||"none":f.kind==="choice"||f.kind==="nice"?((f.choices||[]).find(c=>String(c[0])===String(f.default))||[0,String(f.default)])[1]:f.kind==="bool"?(f.default?"on":"off"):f.default===""?"empty":String(f.default);
  return `<div class="fld" data-f="${esc(k)}"><label class="t" for="${id}">${esc(f.label)}</label>${ctl}<div class="small muted help">${esc(f.help)}${f.kind==="nice"?" "+NICE_NOTE:""}</div>
    <div class="small"><span class="muted def">Default: ${esc(def)}</span><a href="#" data-reset="${esc(k)}" hidden>Reset to default</a></div><div class="small err" data-err="${esc(k)}"></div><div class="small warn" data-warn="${esc(k)}"></div></div>`}
const timeRow=t=>`<div class="row"><input type="text" inputmode="numeric" placeholder="03:00" maxlength="5" value="${esc(t)}" data-time> <button type="button" class="btn sm" data-rmtime title="Remove this time">Remove</button></div>`;
function renderGroups(){const sc=cfg.schema||[],threads=cfg.machine&&cfg.machine.cpus;
  $("#setGroups").innerHTML=(cfg.groups||[]).filter(g=>sc.some(f=>f.group===g[0])).map(([gk,gl])=>`<div class="card" data-g="${esc(gk)}"><h2>${esc(gl)}</h2>
    ${gk==="performance"?`<div class="small" id="perfCtx" style="margin-bottom:6px"></div>`:""}${sc.filter(f=>f.group===gk).map(fieldHtml).join("")}</div>`).join("");
  perfContext()}
function perfContext(){const el=$("#perfCtx");if(!el)return;const m=cfg.machine||{},mc=num($("#f_max_compress")?.value),cc=num($("#f_cpu_count")?.value);
  el.innerHTML=m.cpus?`This machine: <b>${num(m.cpus)} CPU threads</b>. With these values up to ${mc} × ${cc} = <b>${mc*cc} threads</b> compress at once${mc*cc>m.cpus?`, <span class="err">more than the machine has</span>`:""}.`:""}
// one control's value as the server wants it
function readCtl(e){const t=e.dataset.t;
  if(t==="b")return e.checked;
  if(t==="n")return e.value===""||!/^-?\d+$/.test(e.value.trim())?(e.value===""?null:e.value.trim()):Number(e.value);   // a blank or odd number is sent as it is: the server says what is wrong
  if(t==="lines")return e.value.split("\n").map(x=>x.trim()).filter(Boolean);
  if(t==="times")return [...e.querySelectorAll("[data-time]")].map(x=>x.value.trim()).filter(Boolean);
  return e.value.trim()}
function collectForm(){const out={};document.querySelectorAll("#set [data-c]").forEach(e=>{const v=readCtl(e),[a,b]=e.dataset.c.split(".");if(b){out[a]=out[a]||{...cfg[a]};out[a][b]=v}else out[a]=v});return out}
const settingsDirty=()=>!!setBase&&tab==="set"&&JSON.stringify(collectForm())!==setBase;
function showSetBar(){const dirty=settingsDirty(),bad=setErrors.length;$("#saveBar").hidden=!(dirty||$("#setMsg").textContent);
  $("#barText").innerHTML=dirty?(bad?`<b class="err">Unsaved changes · ${bad} problem${bad===1?"":"s"} to fix</b>`:"<b>Unsaved changes</b>"):"";
  $("#bSave").disabled=!dirty||bad>0;$("#bDiscard").hidden=!dirty;
  document.querySelectorAll("#set [data-reset]").forEach(a=>{const f=(cfg.schema||[]).find(x=>x.key===a.dataset.reset),e=document.querySelector(`#set [data-c="${CSS.escape(a.dataset.reset)}"]`);
    if(!f||!e)return;const v=JSON.stringify(readCtl(e)),d=f.kind==="nice"||f.kind==="int"?Number(f.default):f.default;a.hidden=v===JSON.stringify(d)})}
function setMessage(t,bad,keep){const m=$("#setMsg");clearTimeout(setMsgTimer);m.textContent=t;m.className="small "+(bad?"err":"");showSetBar();
  if(t&&!bad&&!keep)setMsgTimer=setTimeout(()=>{m.textContent="";showSetBar()},7000)}
// the server's verdict, next to the fields
function showFieldMessages(errors,warnings){setErrors=errors||[];
  document.querySelectorAll("#set [data-err]").forEach(el=>el.textContent="");document.querySelectorAll("#set [data-warn]").forEach(el=>el.textContent="");document.querySelectorAll("#set .fld.bad").forEach(el=>el.classList.remove("bad"));
  const put=(sel,list,bad)=>{const by={};list.forEach(x=>{(by[x.field]=by[x.field]||[]).push(x.message)});
    for(const [field,msgs] of Object.entries(by)){const slot=document.querySelector(`#set [${sel}="${CSS.escape(field)}"]`)||document.querySelector(`#set [${sel}="${CSS.escape(field.split(".")[0])}"]`);
      if(slot){slot.innerHTML=msgs.map(esc).join("<br>");if(bad)slot.closest(".fld")?.classList.add("bad")}}};
  put("data-err",errors||[],true);put("data-warn",warnings||[],false);
  const loose=(errors||[]).filter(x=>!document.querySelector(`#set [data-err="${CSS.escape(x.field)}"]`)&&!document.querySelector(`#set [data-err="${CSS.escape(x.field.split(".")[0])}"]`));
  if(loose.length)setMessage(loose.map(x=>x.message).join(" "),true)}
function validateSoon(){clearTimeout(valTimer);valTimer=setTimeout(async()=>{try{const r=await api("/api/config/validate",{method:"POST",body:JSON.stringify(collectForm())});showFieldMessages(r.errors,r.warnings);showSetBar()}catch(e){}},350)}
$("#set").addEventListener("input",e=>{if(!e.target.closest("#setGroups,#fFolders"))return;perfContext();showSetBar();validateSoon()});
$("#set").addEventListener("change",e=>{if(!e.target.closest("#setGroups,#fFolders"))return;showSetBar();validateSoon()});
$("#setGroups").addEventListener("click",e=>{
  const r=e.target.closest("[data-reset]");if(r){e.preventDefault();const f=cfg.schema.find(x=>x.key===r.dataset.reset),el=document.querySelector(`#set [data-c="${CSS.escape(f.key)}"]`),d=f.default;
    if(el.dataset.t==="b")el.checked=!!d;else if(el.dataset.t==="lines")el.value=(d||[]).join("\n");else if(el.dataset.t==="times")el.innerHTML=(d||[]).map(timeRow).join("");else el.value=d;
    el.dispatchEvent(new Event("input",{bubbles:true}));return}
  if(e.target.closest("[data-addtime]")){const box=document.querySelector('#set [data-c="schedule.times"]');box.insertAdjacentHTML("beforeend",timeRow(""));box.lastElementChild.querySelector("input").focus();showSetBar();return}
  const rm=e.target.closest("[data-rmtime]");if(rm){const box=rm.closest(".times");rm.closest(".row").remove();box.dispatchEvent(new Event("input",{bubbles:true}));return}
  const t=e.target.closest("[data-ptest]");if(t)testPatterns(t.dataset.ptest)});
async function testPatterns(key){const f=cfg.schema.find(x=>x.key===key),out=document.querySelector(`#set [data-pout="${CSS.escape(key)}"]`),ta=document.querySelector(`#set [data-c="${CSS.escape(key)}"]`);
  out.textContent="Testing…";
  try{const r=await api("/api/config/preview-patterns",{method:"POST",body:JSON.stringify({kind:f.subject,patterns:readCtl(ta)})});
    if(r.errors&&r.errors.length){out.innerHTML=r.errors.map(x=>`<span class="err">${esc(x.message)}</span>`).join("<br>");return}
    out.innerHTML=`<b>${num(r.total)} match${r.total===1?"":"es"}</b>${r.total>r.matches.length?` (first ${r.matches.length} shown)`:""}. <span class="muted">${esc(r.note)}</span>${r.matches.length?`<ul>${r.matches.map(m=>`<li><code>${esc(m.text)}</code>${m.after!=null?` → <code>${esc(m.after)}</code>`:""}</li>`).join("")}</ul>`:""}`}
  catch(e){out.innerHTML=`<span class="err">${esc(e.message)}</span>`}}
// what only shows in the Library after another scan, with a button for it
function renderRescan(s){const r=s.rescan||{needed:[]},box=$("#rescanBox");if(!box)return;
  const busy=s.engine_running||(s.scan&&s.scan.running);
  box.innerHTML=r.needed.length?`<div class="banner warn" style="margin:8px 0 0"><b>Changed since the last scan:</b> ${r.needed.map(esc).join(", ")}. The Library shows this after a rescan. <button class="btn" data-do="rescan" ${busy?"disabled":""}>Rescan now</button>${busy?` <span class="small">A run or scan is active: rescan when it ends.</span>`:""}</div>`:"";
}
function foldersForm(){const F=cfg.folders||{},hp=cfg.host_paths||{};
  const intro=cfg.in_docker?`Where each folder is <i>inside</i> the container is fixed by the <code>volumes:</code> list in docker-compose.yml. Tell the app where it is on your NAS, so paths are shown the way you know them. This only changes what is displayed.`
    :`Tell the app where each folder is on the machine you think of as the host, so paths are shown the way you know them. This only changes what is displayed.`;
  const rows=Object.keys(FOLDER_LABEL).filter(k=>F[k]).map(k=>{const f=F[k];
    const val=f.source==="env"?`<span><code>${esc(f.host)}</code> <span class="muted small">set in docker-compose (${esc(f.via)}); Settings can't change it</span></span>`
      :`<div><input type="text" data-c="host_paths.${k}" data-t="s" value="${esc(hp[k]||"")}" placeholder="${esc(f.source==="detected"?f.host:"/path/on/your/nas")}"><div class="small err" data-err="host_paths.${k}"></div></div>`;
    return `<label class="f"><span>${esc(FOLDER_LABEL[k])} is shown as<div class="small">in the container: ${esc(f.container)}</div></span>${val}</label>`}).join("");
  const adv=cfg.in_docker?"":`<details><summary class="small">Advanced: folder paths as the app uses them</summary>${["library","downloads","output","temp"].map(k=>`<label class="f"><span>${esc(FOLDER_LABEL[k])}</span><input type="text" data-c="${k}" data-t="s" value="${esc(cfg[k])}"></label>`).join("")}</details>`;
  $("#fFolders").innerHTML=`<p class="small muted" style="margin-top:0">${intro}</p>${rows}${adv}`}
async function loadSet(note){loadStorage();cfgLoadedAt=performance.now();cfg=await api("/api/config");cfgRev=cfg.config_rev;$("#cfgChanged").hidden=true;
  $("#rescanNote").textContent=cfg.rescan_note?" "+cfg.rescan_note:"";renderGroups();foldersForm();setBase=JSON.stringify(collectForm());setErrors=[];showFieldMessages(cfg.problems||[],cfg.warnings||[]);
  if(typeof note==="string")setMessage(note,false);else showSetBar()}
async function saveSettings(){const body=collectForm();$("#bSave").disabled=true;
  try{const r=await api("/api/config",{method:"PUT",body:JSON.stringify(body)});
    await loadSet();setMessage("Saved."+(r.run_active?" Applies to the next run.":""),false);toast("Saved."+(r.run_active?" Applies to the next run.":""));loadSetup();
    showFieldMessages([],r.warnings||[]);showSetBar();poll().catch(()=>{})}
  catch(e){showFieldMessages(e.errors||[{field:"",message:e.message}],[]);setMessage("Not saved: "+(e.errors&&e.errors.length>1?`${e.errors.length} problems, shown next to the fields.`:e.message),true);
    const first=document.querySelector("#set .fld.bad");if(first)first.scrollIntoView({block:"center"})}
  showSetBar()}
$("#bSave").onclick=saveSettings;$("#bDiscard").onclick=()=>loadSet("Changes discarded.");$("#bReload").onclick=()=>loadSet("Reloaded from the file.");
window.addEventListener("beforeunload",e=>{if(settingsDirty()){e.preventDefault();e.returnValue=""}});
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
  else if(e.target.closest("[data-bkrestore]")){if(!(await askConfirm({title:"Restore this backup?",html:`<p>Make the backup <code>${esc(name)}</code> the current settings? The current file is backed up first.</p>`,ok:"Restore it"})))return;const r=await postJson(`/api/backups/${encodeURIComponent(name)}/restore`,"{}");
    if(r.ok){toast(`Restored. ${r.data.backup?"The replaced settings were saved as "+r.data.backup+".":""}`);loadSet("Restored from "+name)}else out.innerHTML=`<span class="err">${esc(r.data.detail||"failed")}</span>`}});
function renderOverrides(){const o=storage.overrides;
  $("#ovrCard").innerHTML=`<h2>Per-game overrides</h2>${o.length?`<table><thead><tr><th>Game</th><th>What is overridden</th><th>Note</th><th></th></tr></thead><tbody>${o.map(x=>`<tr><td><b>${esc(x.title||x.title_id)}</b><div class="small muted">${esc(x.title_id)}${x.in_library?"":` <span class="tag t-waiting">not in library</span>`}</div></td>
     <td class="small">${esc(x.what.join(" · ")||"(empty)")}${x.patch?`<div>${pathHtml(x.patch_host,x.patch)}</div>`:""}</td><td class="small">${esc(x.note)}</td>
     <td><button class="btn sm" data-ovr-remove="${esc(x.title_id)}">Remove</button></td></tr>`).join("")}</tbody></table>`:`<div class="muted small">No game has options of its own. Set them from a game's Options on the Library page.</div>`}`}
$("#ovrCard").addEventListener("click",async e=>{const b=e.target.closest("[data-ovr-remove]");if(!b)return;const t=b.dataset.ovrRemove;
  if(!(await askConfirm({title:"Remove this override?",html:`<p>Remove the override for ${esc(t)}? Only this entry is deleted.</p>`,ok:"Remove it",danger:true})))return;
  try{await api("/api/overrides/"+encodeURIComponent(t),{method:"DELETE"});toast("Removed the override for "+t);loadStorage()}catch(err){toast(err.message,1)}});
// ---- Logs: a list grouped by what it is (and by game), and a viewer that loads what it needs: the end first, earlier on request, new lines by
// offset when following (so what you scrolled to stays where it is), search with highlights, and a download of the file as it is.
let logList=[],logInfo=null,logSt={name:null,lines:[],start:0,next:0,size:0,q:"",hit:-1,busy:false,trimmed:false},wantGame=null,wantFind=null;
const encPath=n=>encodeURIComponent(n).replace(/%2F/g,"/");
const WARN_RE=/\b(errors?|warn(?:ing|ed)?|fail(?:ed|ure|s)?|fatal|traceback|exception|mismatch|killed)\b|\[(?:ERROR|WARN\w*)\]/i;
const PROG_RE=/^\s*\[[#\-=. ]{8,}\]\s*\d+%|\d+% [^@]*@ [\d.]+ MB\/s/;
const OUTCOME={built:"built",copied:"copied",failed:"failed",cancelled:"cancelled",deferred:"waiting for space"};
const logLabel=d=>d.kind==="build"?`${d.title||d.tid}`:d.kind==="activity"?"Activity log":d.kind==="activity_old"?"Activity log, earlier part":d.kind==="scan"?"Last scan":d.kind==="run"?"Raw output of a run":d.name;
function logRow(d){const when=d.started||d.mtime;
  return `<a class="lg ${d.name===logSt.name?"on":""}" href="#logs/${esc(d.name)}" data-lg="${esc(d.name)}">${d.kind==="build"?`<b>${esc(logLabel(d))}</b> `:`${esc(logLabel(d))} `}<span class="small muted">${esc(whenText(when))} · ${esc(sizeText(d.size))}</span>${d.outcome?` <span class="tag t-${cls(d.outcome)}">${esc(OUTCOME[d.outcome]||d.outcome)}</span>`:""}</a>`}
function renderLogList(){const q=$("#logFind").value.trim().toLowerCase(),ok=d=>!q||[d.name,d.title,d.tid,logLabel(d)].join(" ").toLowerCase().includes(q),g=k=>logList.filter(d=>d.kind===k&&ok(d));
  const act=[...g("activity"),...g("activity_old")],scan=g("scan"),runs=g("run"),builds=g("build"),allBuilds=logList.filter(d=>d.kind==="build").length;
  const grp=(t,note,rows)=>rows.length?`<div class="lgh">${t}</div><div class="small muted">${note}</div>${rows.map(logRow).join("")}`:"";
  $("#logGroups").innerHTML=grp("Activity log","What the engine did: each scan, the plan, every game's steps and the run's summary. The raw output below holds the same lines.",act)
    +grp("Scans","The output of the last scan.",scan)
    +grp(`Builds (${builds.length}${q?` of ${allBuilds}`:""})`,"One UltraPack log per attempt, by game. Search a title or ID above.",builds)
    +grp(`Raw output of runs (${runs.length})`,"What each run printed, line by line, as it ran.",runs)
    ||`<div class="muted small" style="margin-top:8px">${logList.length?"Nothing matches.":"There are no logs yet."}</div>`;
  const u=logInfo&&logInfo.usage,r=logInfo&&logInfo.retention;
  if(u){$("#logUsage").textContent=`${plural(u.files,"log")} use ${sizeText(u.bytes)}. `+(r.days||r.gb?`Old logs are removed ${r.days?`after ${plural(r.days,"day")}`:""}${r.days&&r.gb?" and ":""}${r.gb?`above ${r.gb} GB`:""} (Settings > Cleanup).`:"They are kept for ever; Settings > Cleanup can remove old ones.");$("#bLogPrune").hidden=!(r.days||r.gb)}}
async function loadLogs(){const r=await api("/api/logs?info=1");logList=r.items;logInfo=r;
  if(wantGame!==null){$("#logFind").value=wantGame;wantGame=null}
  renderLogList();
  const name=wantLog||(logSt.name?null:(logList.find(d=>d.kind==="activity")||logList[0]||{}).name),find=wantFind;wantLog=null;wantFind=null;
  if(name)await openLog(name,{find})}
$("#bLog").onclick=()=>loadLogs().catch(e=>say(e.message,1));$("#logFind").oninput=renderLogList;
async function logGet(name,q){const r=await fetch(`/api/logs/${encPath(name)}?${q}${$("#logHost").checked?"&host=1":""}`,{headers:{"Content-Type":"application/json"}});
  if(r.status===401){location.reload();throw new Error("Log in first.")}
  if(!r.ok)throw new Error(r.status===404?"That log is not there (any more): old logs are removed by the retention setting.":"The log could not be read.");
  return{text:await r.text(),size:num(r.headers.get("X-Log-Size")),start:num(r.headers.get("X-Log-Start")),next:num(r.headers.get("X-Log-Next")),reset:r.headers.get("X-Log-Reset")==="1"}}
const linesOf=t=>t?t.replace(/\n$/,"").split("\n"):[];
function logHtml(){const L=logSt.lines,q=logSt.q,warn=$("#logWarn").checked,col=$("#logCollapse").checked,rows=[];
  if(warn){for(const l of L)if(WARN_RE.test(l))rows.push({t:l,c:"lvl"})}
  else if(col){for(let i=0;i<L.length;){if(!PROG_RE.test(L[i])){rows.push({t:L[i],c:""});i++;continue}
    let j=i;while(j<L.length&&PROG_RE.test(L[j]))j++;
    if(j-i>=3){rows.push({t:`… ${j-i-1} progress lines hidden …`,c:"pg"});rows.push({t:L[j-1],c:"pg"})}else for(let k=i;k<j;k++)rows.push({t:L[k],c:"pg"});i=j}}
  else for(const l of L)rows.push({t:l,c:""});
  const hl=t=>{if(!q)return esc(t);const lo=t.toLowerCase(),ql=q.toLowerCase();let out="",i=0,j;while((j=lo.indexOf(ql,i))>=0){out+=esc(t.slice(i,j))+`<mark>${esc(t.slice(j,j+q.length))}</mark>`;i=j+q.length}return out+esc(t.slice(i))};
  return{html:rows.map(r=>r.c?`<span class="${r.c}">${hl(r.t)}</span>`:hl(r.t)).join("\n"),shown:rows.length}}
// draw the log; `how`: "bottom" (go to the end), "anchor" (more text was put above: stay where you were), "keep" (stay)
function renderLog(how="keep"){const p=$("#logTxt"),h0=p.scrollHeight,t0=p.scrollTop,atEnd=h0-p.clientHeight-t0<40,v=logHtml();
  p.innerHTML=v.html;p.classList.toggle("nowrap",!$("#logWrap").checked);
  if(how==="bottom"||(how==="keep"&&atEnd&&$("#logFollow").checked))p.scrollTop=p.scrollHeight;else if(how==="anchor")p.scrollTop=t0+(p.scrollHeight-h0);else p.scrollTop=t0;
  const marks=p.querySelectorAll("mark");
  $("#logHits").textContent=logSt.q?`${marks.length} match${marks.length===1?"":"es"}${logSt.start>0?" in the part loaded":""}`:"";
  $("#logEarlier").hidden=!(logSt.start>0&&!logSt.trimmed);$("#logAll").hidden=!(logSt.start>0&&!logSt.trimmed);
  $("#logNote").textContent=$("#logWarn").checked?`${v.shown} of ${logSt.lines.length} lines`:logSt.start>0?"Showing the end of the log.":"";}
function gotoHit(d){const marks=[...$("#logTxt").querySelectorAll("mark")];if(!marks.length)return;logSt.hit=(logSt.hit+d+marks.length)%marks.length;
  marks.forEach(m=>m.classList.remove("cur"));const m=marks[logSt.hit];m.classList.add("cur");m.scrollIntoView({block:"center"})}
async function openLog(name,o={}){logSt={...logSt,name,lines:[],start:0,next:0,size:0,hit:-1,trimmed:false};
  try{history.replaceState(null,"","#logs/"+name+(o.find?"?find="+encodeURIComponent(o.find):""))}catch(e){}
  const meta=logList.find(d=>d.name===name)||{};
  $("#logTitle").textContent=logLabel(meta.name?meta:{name,kind:"other"});$("#logMeta").textContent=meta.name?`${name} · ${sizeText(meta.size)}`:name;
  const dl=$("#logDl");dl.hidden=false;dl.href=`/api/logs/${encPath(name)}?download=1`;dl.setAttribute("download",name.split("/").pop());
  renderLogList();
  try{const r=await logGet(name,"lines=600");logSt.lines=linesOf(r.text);logSt.start=r.start;logSt.next=r.next;logSt.size=r.size}
  catch(e){$("#logTxt").textContent=e.message;return}
  if(o.find){const word=o.find==="validate"?"==== --fpkg-validate ====":o.find;
    if(!logSt.lines.some(l=>l.includes(word))&&logSt.start>0)await loadAllLog(true);
    $("#logSearch").value=word;logSt.q=word;renderLog("bottom");gotoHit(1);return}
  logSt.q=$("#logSearch").value.trim();renderLog("bottom")}
async function loadAllLog(quiet){if(!logSt.name)return;$("#logNote").textContent="Loading the whole file…";
  try{let off=0,lines=[],last=null;for(let i=0;i<4;i++){const r=await logGet(logSt.name,`offset=${off}&limit=8388608`);last=r;lines=lines.concat(linesOf(r.text));if(r.next>=r.size||r.next===off)break;off=r.next}
    logSt.lines=lines;logSt.start=0;logSt.next=last.next;logSt.size=last.size;logSt.trimmed=false;
    if(!quiet)renderLog("keep");if(last.next<last.size)$("#logNote").textContent="Only the first 32 MiB are loaded: download the file for the rest."}
  catch(e){$("#logNote").textContent=e.message}}
async function loadEarlierLog(){if(!logSt.name||logSt.start<=0)return;
  try{const r=await logGet(logSt.name,`before=${logSt.start}&lines=1000`);logSt.lines=linesOf(r.text).concat(logSt.lines);logSt.start=r.start;renderLog("anchor")}catch(e){$("#logNote").textContent=e.message}}
async function followLog(){if(!logSt.name||!$("#logFollow").checked||logSt.busy||!logSt.next)return;logSt.busy=true;
  try{const r=await logGet(logSt.name,`offset=${logSt.next}&limit=1048576`);
    if(r.reset){logSt.lines=linesOf(r.text);logSt.start=r.start;logSt.next=r.next;logSt.size=r.size;renderLog("bottom");return}
    logSt.size=r.size;logSt.next=r.next;
    if(r.text){logSt.lines=logSt.lines.concat(linesOf(r.text));if(logSt.lines.length>60000){logSt.lines.splice(0,logSt.lines.length-60000);logSt.trimmed=true}renderLog("keep")}}   // appended below: what you scrolled to stays put
  catch(e){}finally{logSt.busy=false}}
$("#logSearch").oninput=()=>{logSt.q=$("#logSearch").value;logSt.hit=-1;renderLog("keep");if(logSt.q)gotoHit(1)};
$("#logSearch").onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();gotoHit(e.shiftKey?-1:1)}};
$("#logNext").onclick=()=>gotoHit(1);$("#logPrev").onclick=()=>gotoHit(-1);
for(const id of["#logWarn","#logCollapse","#logWrap"])$(id).onchange=()=>{try{localStorage.setItem("ps5pkg."+id.slice(1),$(id).checked?"1":"0")}catch(e){}renderLog("keep")};
try{for(const id of["#logCollapse","#logWrap"]){const v=localStorage.getItem("ps5pkg."+id.slice(1));if(v!==null)$(id).checked=v==="1"}}catch(e){}
$("#logEarlier").onclick=loadEarlierLog;$("#logAll").onclick=()=>loadAllLog(false);
$("#logHost").onchange=()=>{try{localStorage.setItem("ps5pkg.logHost",$("#logHost").checked?"1":"0")}catch(e){}if(logSt.name)openLog(logSt.name).catch(()=>{})};
try{if(localStorage.getItem("ps5pkg.logHost")==="0")$("#logHost").checked=false}catch(e){}
$("#bLogPrune").onclick=async()=>{try{const dry=await api("/api/logs/prune",{method:"POST",body:JSON.stringify({dry_run:true})});
  if(!dry.removed.length){toast(dry.note||"Nothing is old enough to remove");return}
  if(!(await askConfirm({title:"Remove old logs?",html:`<p>${esc(plural(dry.removed.length,"log"))} (${esc(sizeText(dry.freed))}) are older than the retention allows. Removing them can't be undone.</p><p class="small muted">The log of a game that is still failing is kept.</p>`,ok:"Remove them",danger:true})))return;
  const r=await api("/api/logs/prune",{method:"POST",body:"{}"});toast(`Removed ${plural(r.removed.length,"log")}, ${sizeText(r.freed)} freed`);await loadLogs()}catch(e){toast(e.message,1)}};
// a link to a log, or to a game's logs, works from anywhere: #logs/pkg/<file>[?find=validate] and #logs?game=<title ID>
function routeHash(){const m=/^#logs(?:\/([^?]*))?(?:\?(.*))?$/.exec(location.hash);if(!m)return false;
  const qs=new URLSearchParams(m[2]||"");wantLog=m[1]?decodeURIComponent(m[1]):null;wantGame=qs.get("game");wantFind=qs.get("find");
  if(tab!=="logs")goTab("logs");else loadLogs().catch(e=>say(e.message,1));return true}
window.addEventListener("hashchange",routeHash);
document.addEventListener("click",e=>{const a=e.target.closest('a[href^="#logs"]');if(!a)return;e.preventDefault();try{history.replaceState(null,"",a.getAttribute("href"))}catch(x){}routeHash()});
function load(){({dash:refresh,lib:loadLib,hist:loadHist,set:loadSet,logs:loadLogs})[tab]().catch(e=>say(e.message,1));if(tab==="dash")loadSetup();if(tab!=="dash"&&tab!=="set")poll().catch(()=>{})}
if(!routeHash())load();setInterval(()=>{(tab==="dash"?refresh():poll()).catch(()=>{});if(tab==="logs")followLog()},5000);
