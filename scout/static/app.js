const $ = (selector, root = document) => root.querySelector(selector);
const content = $("#content"), modal = $("#dialog"), body = $("#dialog-content");
const S = {page:"jobs", info:null, offset:0, search:"", region:"", latest:false, levels:"relevant", recent:"0", hidden:false,
  companies:[], companySearch:"", source:"", job:null, item:null, people:[], serial:0};
const regions = {india_remote:"Remote from India",india:"India-based",us_remote:"U.S. remote",unclear:"Eligibility unclear",other:"Other location"};
const channels = {linkedin:"LinkedIn",gmail:"Gmail",community:"Referral community"};
const stages = {draft:"Draft",waiting:"Waiting for reply",replied:"Replied",offered:"Referral offered",submitted:"Referral submitted",closed:"Closed"};
const historyLabels = {sent:"Request sent",checked:"Checked, no reply",snoozed:"Snoozed",followed_up:"Follow-up sent",replied:"Reply recorded",offered:"Referral offered",submitted:"Referral submitted",closed:"Closed",undo_send:"Send corrected",reply_detected:"Gmail reply detected"};
const E = (x = "") => String(x ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = x => Number(x ?? 0).toLocaleString();
const date = (x, time=false) => {
  const d = new Date(x);
  return x && Number.isFinite(d.valueOf()) ? d.toLocaleDateString(undefined,{month:"short",day:"numeric",...(time?{hour:"numeric",minute:"2-digit"}:{})}) : "—";
};
const B = (label,action,id="",style="") => `<button type="button" class="button ${style}" data-action="${action}" data-id="${E(id)}">${E(label)}</button>`;
const L = (label,url,style="small") => `<a class="button ${style}" href="${E(url)}" target="_blank" rel="noopener noreferrer">${E(label)} ↗</a>`;
const tag = (label,style="") => `<span class="badge ${style}">${E(label)}</span>`;
const head = (title,text,action="",h="h1") => `<div class="page-head"><div><${h}>${E(title)}</${h}><p>${E(text)}</p></div><div class="actions">${action}</div></div>`;
const empty = (title,text,action="") => `<div class="panel empty"><strong>${E(title)}</strong><p>${E(text)}</p>${action}</div>`;
const select = (values,selected="") => Object.entries(values).map(([id,name])=>`<option value="${id}" ${id===selected?"selected":""}>${E(name)}</option>`).join("");
let messageTimer, pollTimer, searchTimer;

async function api(path,method="GET",data) {
  const response = await fetch(`/api${path}`,{method,headers:data?{"Content-Type":"application/json"}:undefined,
    body:data?JSON.stringify(data):undefined});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail==="string"?result.detail:result.detail?.[0]?.msg||"Please try again.");
  return result;
}
function notice(text,error=false) {
  const box=$("#notice");
  box.textContent=text; box.hidden=false; box.classList.toggle("error",error);
  clearTimeout(messageTimer); messageTimer=setTimeout(()=>{box.hidden=true;},4500);
}
function scanStatus() {
  const {scan,stats}=S.info, running=scan?.state==="running", button=$("#scan-button");
  const retry=scan?.scope==="retry";
  const checked=`${num(scan?.checked)} ${scan?.checked===1?"feed":"feeds"}`;
  button.disabled=running;
  button.innerHTML=running?`<span aria-hidden='true'>↻</span> ${retry?"Retrying feeds…":"Scanning…"}`:"<span aria-hidden='true'>↻</span> Find latest jobs";
  $("#scan-progress").textContent=running ? `${num(scan.checked+scan.failed+scan.unsupported)} of ${num(scan.total)} ${retry?"failed feeds retried":"sources accounted for"}`
    : scan?.state==="completed" ? `${retry?"Last retry":"Last scan"} ${date(scan.finished_at,true)} · ${checked} ${retry?"recovered":"checked"} · ${scan.failed} failed${scan.fresh ? ` · ${num(scan.fresh)} new matches` : ""}`
    : scan ? scan.note||"Previous scan interrupted." : `${num(stats.configured)} feeds ready · no scan yet`;
  $("#nav-due").hidden=!stats.due;
  $("#nav-due").textContent=stats.due;
}
async function refreshState() { S.info=await api("/state"); scanStatus(); }
function pollScan() {
  clearTimeout(pollTimer);
  if (S.info.scan?.state!=="running") return;
  pollTimer=setTimeout(async()=>{
    try {
      await refreshState();
      if (S.info.scan.state==="running") {
        if ($("#coverage-panel")) $("#coverage-panel").innerHTML=coverage();
      } else {
        await render();
        const finished=S.info.scan;
        notice(finished.state!=="completed"?"The scan stopped early.":finished.scope==="retry"?"Failed-feed retry complete.":"Scan complete. Your shortlist is ready.",finished.state!=="completed");
      }
    } catch(error) {notice(error.message,true);}
    pollScan();
  },900);
}
async function startScan(scope="full") {
  if(scope==="full") {
    S.latest=false;
    if ($("#latest")) $("#latest").checked=false;
  }
  S.info.scan=await api(scope==="retry"?"/scans/retry-failed":"/scans","POST");
  if ($("#coverage-panel")) $("#coverage-panel").innerHTML=coverage();
  if(S.page!=="jobs") location.hash="jobs";
  scanStatus(); pollScan();
}
function coverage() {
  const {stats,scan,full_scan}=S.info;
  const full=scan?.scope==="full"&&scan.state==="running"?scan:full_scan;
  return `<p class="eyebrow">SOURCE STATUS</p><h3>Scan coverage</h3><div class="coverage">
    <div>Company watchlist <strong>${num(stats.companies)}</strong></div>
    <div>Feeds configured <strong>${num(stats.configured)}</strong></div>
    <div>Checked in full scan <strong>${full?num(full.checked):"—"}</strong></div>
    <div>Feeds currently failing <strong>${num(stats.failed_feeds)}</strong></div>
    <div>Manual sources in full scan <strong>${full?num(full.unsupported):"—"}</strong></div>
    </div>${stats.failed_feeds&&scan?.state!=="running"?`<div class="actions">${B("Retry failed feeds","retry-feeds","","small")}</div>`:""}
    <a href="#watchlist">View employer watchlist →</a>`;
}
function jobRow(job) {
  const mark=job.company.split(/\s+/).slice(0,2).map(w=>w[0]).join("");
  const freshness=job.archived?tag("No longer on feed","warn"):job.source_status==="failed"?tag("Source stale","warn"):
    job.new_scan&&job.new_scan===S.info.full_scan?.id?tag("New since last scan","good"):"";
  return `<article class="panel job-row"><div class="job-mark" aria-hidden="true">${E(mark)}</div>
    <div><div class="job-company">${E(job.company)} · ${job.posted_at?"Posted":"First seen"} ${date(job.posted_at||job.first_seen)}</div>
    <button class="job-title" data-action="job" data-id="${job.id}">${E(job.title)}</button>
    <div class="meta">${tag(regions[job.region],job.region.startsWith("india")?"good":"")}${freshness}${job.hidden?tag("Hidden from feed"):""}${job.applied_at?tag("Applied","good"):""}</div>
    <div class="micro">${E(job.reason)}</div></div><div class="actions">${B("Review","job",job.id,"small")}${L("Apply",job.apply_url)}</div></article>`;
}
async function refreshJobs() {
  const box=$("#job-results");
  if (!box) return;
  const serial=++S.serial, saved=S.page==="saved", hidden=!saved&&S.hidden;
  const params=new URLSearchParams({view:saved?"saved":hidden?"hidden":"latest",region:S.region,search:S.search,latest:S.latest,
    all_levels:S.levels==="all",early_only:S.levels==="early",recent_days:S.recent,offset:S.offset});
  const {items,more}=await api(`/jobs?${params}`);
  if (!box.isConnected||serial!==S.serial) return;
  box.innerHTML=items.length?`<div class="job-list">${items.map(jobRow).join("")}</div>`:
    saved?empty("Your shortlist is empty","Review a role and save it to come back to it later.",'<a href="#jobs">Browse the job feed →</a>'):
    hidden?empty("No hidden jobs in this view","Roles you hide appear here. Clear your search and location filters to see all of them."):
    !S.info.scan?.finished_at&&!S.search?empty("Ready for your first scan","Start with the employer feeds already configured in your watchlist.",B("Find latest jobs","scan","","primary")):
    empty("No jobs in this view","Try a broader date range, experience level, or location.");
  if (S.offset||more) box.innerHTML+=`<div class="pager">${S.offset?B("← Previous","prev","","small"):"<span></span>"}
    <span class="micro">${S.offset+1}–${S.offset+items.length}</span>${more?B("Next →","next","","small"):"<span></span>"}</div>`;
  box.setAttribute("aria-busy","false");
}
function jobPage() {
  const {stats,scan}=S.info, saved=S.page==="saved", hidden=!saved&&S.hidden;
  const switcher=saved?"":B(hidden?"Back to job feed":`Hidden jobs (${num(stats.hidden)})`,"toggle-hidden","","small");
  const tiles=[[stats.companies,"Companies watched","⌗"],[stats.configured,"Automated feeds","↻"],[scan?.finished_at?stats.matches:null,"Matching openings","◎"]];
  return `${saved?"":`<div class="stats">${tiles.map(([n,label,icon])=>`<div class="stat"><span class="stat-icon" aria-hidden="true">${icon}</span><div><strong>${n===null?"—":num(n)}</strong><small>${label}</small></div></div>`).join("")}</div>`}
    <div class="work-grid"><div>${head(saved?"Saved jobs":hidden?"Hidden jobs":"Latest openings",saved?"Keep track of roles you’re considering and applications you’ve sent.":hidden?"Review roles you’ve hidden and restore any you want to see again.":"Early-career software, data and AI roles from employer feeds.",switcher,saved?"h1":"h2")}
    <div class="filters"><input type="search" id="job-search" aria-label="Search jobs" placeholder="Search roles or companies" value="${E(S.search)}">
    <select id="region" aria-label="Location">${select({"":"All locations",...regions},S.region)}</select></div>
    ${saved||hidden?"":`<div class="filters shortlist-filters">
    <select id="levels" aria-label="Experience level">${select({relevant:"Relevant roles",early:"Explicit early-career roles",all:"All experience levels"},S.levels)}</select>
    <select id="recent" aria-label="Posting age">${select({"0":"Any posting date","7":"Last 7 days","30":"Last 30 days"},S.recent)}</select>
    <label><input type="checkbox" id="latest" ${S.latest?"checked":""}> New since last scan</label></div>`}
    <div id="job-results" aria-busy="true"><div class="panel loading">Loading jobs…</div></div></div>
    <aside class="aside"><div class="panel" id="coverage-panel">${coverage()}</div><div class="panel"><p class="eyebrow">FROM JOB TO REFERRAL</p>
    <h3>What to do next</h3><ol><li>Review a role and its location.</li><li>Find someone relevant to ask.</li><li>Send your message and log it.</li></ol>
    <a href="#outreach">Open referral inbox →</a></div></aside></div>`;
}
const sourceLabels={pending:"Ready to scan",checked:"Checked",failed:"Failed",unsupported:"Manual source",disabled:"Paused"};
function companyRows() {
  const rows=S.companies.filter(row=>row.name.toLowerCase().includes(S.companySearch.toLowerCase().trim())&&(!S.source||S.source===(row.enabled?row.status:"disabled")));
  return rows.length?`<div class="panel">${rows.map(row=>`<div class="company-row"><div><strong>${E(row.name)}</strong>
    <span class="micro">${E(row.provider||"Employer careers page")}${row.checked_at?` · ${date(row.checked_at,true)}`:""}${row.note?` · ${E(row.note)}`:""}</span></div>
    <div class="actions">${tag(sourceLabels[row.enabled?row.status:"disabled"],row.status==="checked"?"good":row.status==="failed"?"warn":"")}${L("Careers",row.url)}${B("Edit","company",row.id,"small")}</div></div>`).join("")}</div>`:
    empty("No companies match","Change the search or source filter.");
}
async function watchlist() {
  S.companies=await api("/companies");
  return `${head("Company watchlist","Manage employer feeds and open career pages for manual sources.",B("＋ Add company","company","","primary"))}
    <div class="filters"><input type="search" id="company-search" aria-label="Search companies" value="${E(S.companySearch)}" placeholder="Search companies">
    <select id="source" aria-label="Source status">${select({"":"All sources",...sourceLabels},S.source)}</select></div><div id="company-results">${companyRows()}</div>`;
}


function inboxRow(item) {
  return `<div class="inbox-row"><div><strong>${E(item.person)} · ${E(item.company)}</strong>
    <span class="micro">${E(item.job_title)} · ${channels[item.channel]}${item.due_at?` · Check ${date(item.due_at)}`:""}</span></div>
    <div class="actions">${tag(stages[item.state],item.state==="submitted"?"good":"")}${B("Open","conversation",item.id,"small")}</div></div>`;
}
async function inbox() {
  const {items,hints}=await api("/outreach"), now=new Date();
  const groups=[
    ["Check for replies",items.filter(i=>i.state==="waiting"&&new Date(i.due_at)<=now)],
    ["Needs your action",items.filter(i=>["replied","offered"].includes(i.state))],
    ["Ready to send",items.filter(i=>i.state==="draft")],
    ["Waiting",items.filter(i=>i.state==="waiting"&&new Date(i.due_at)>now)],
    ["Completed",items.filter(i=>["submitted","closed"].includes(i.state))],
  ];
  return `${head("Referral inbox","See who you contacted and which conversations need attention.",S.info.mail.account?B("Check Gmail","mail-check","","primary"):'<a class="button" href="#settings">Set up Gmail</a>')}
    ${hints.length?`<section class="section"><h3>Check LinkedIn</h3><div class="panel">${hints.map(h=>`<div class="inbox-row"><div><strong>${E(h.subject)}</strong>
    <span class="micro">Notification email · ${date(h.at,true)} · Open LinkedIn to confirm</span></div><div class="actions">${L("LinkedIn inbox","https://www.linkedin.com/messaging/")}${B("Dismiss","hint-dismiss",h.id,"small")}</div></div>`).join("")}</div></section>`:""}
    ${items.length?groups.filter(([,rows])=>rows.length).map(([name,rows])=>`<section class="section"><h3>${name} <span class="micro">${rows.length}</span></h3>
    <div class="panel">${rows.map(inboxRow).join("")}</div></section>`).join(""):empty("No conversations yet","Open a job, find someone to ask, and prepare a draft.",'<a href="#jobs">Browse the job feed →</a>')}`;
}
function settings() {
  const {mail,search_configured,search_from_env,rewrite_configured}=S.info;
  return `${head("Settings","Connect optional services for public research and reply checks.")}
  <div class="settings-grid">
    <section class="panel padded"><h3>Public contact research</h3><p>Brave searches public sources after you pick a job. Saved contacts and LinkedIn search queries are available immediately.</p>
    ${tag(search_configured?"Brave configured":"Brave key needed",search_configured?"good":"warn")}
    ${search_from_env?"<p class='section'>Managed through the local environment.</p>":`<form class="form section" data-form="brave">
    <label>Brave Search API key<input type="password" name="value" required minlength="8" maxlength="512" autocomplete="off"></label>
    <div class="actions"><button class="button">Save key</button>${search_configured?B("Remove saved key","forget-brave","","small"):""}</div></form>`}</section>
    <section class="panel padded"><h3>Message drafts</h3><p>Templates use the chosen job, contact, and details you enter. A rewrite sends the draft text to the model you configure.</p>
    ${tag(rewrite_configured?"Rewrite available":"Templates active","good")}<p class="section">Optional settings <code>SCOUT_OPENAI_KEY</code> and <code>SCOUT_OPENAI_MODEL</code>.</p></section>
    <section class="panel padded"><h3>Gmail reply checks</h3><p>Job Scout checks message headers for linked conversations every ten minutes while the app runs.</p>
    ${mail.account?`<p><strong>${E(mail.account)}</strong><br>Last check ${date(mail.checked_at,true)}</p>
    <div class="actions">${B("Check Gmail","mail-check","","small")}${B("Disconnect","mail-disconnect","","small danger")}</div>
    <p class="section">Apply a <strong>Job Scout</strong> label in Gmail, then link the thread from a sent Gmail referral here.</p>`:
    `<p>Choose your Google Desktop OAuth client JSON. See the README for Google Cloud setup.</p>
    ${mail.configured?B("Connect Gmail","mail-connect","","primary"):tag("OAuth setup needed","warn")}
    ${mail.from_env?"":`<form class="form section" data-form="google"><label>Google Desktop OAuth client JSON
    <input type="file" name="file" accept=".json,application/json" required></label>
    <div class="actions"><button class="button">Save client</button>${mail.configured?B("Remove saved client","forget-google","","small"):""}</div></form>`}`}
    ${mail.issue?`<p class="evidence">${E(mail.issue)}</p>`:""}</section>
    <section class="panel padded"><h3>LinkedIn notification hints</h3><p>Use a Gmail filter to label LinkedIn message notifications <strong>Job Scout LinkedIn</strong>. Labeled message notifications become reminders to check LinkedIn.</p>
    <label class="checklabel"><input type="checkbox" id="mail-hints" ${mail.linkedin_hints?"checked":""} ${mail.account?"":"disabled"}> Show LinkedIn email hints</label></section>
  </div>`;
}
async function render() {
  const page=S.page,serial=++S.serial;
  $("#hero").hidden=page!=="jobs";
  document.querySelectorAll("nav a").forEach(a=>{
    if(a.getAttribute("href")===`#${page}`) a.setAttribute("aria-current","page"); else a.removeAttribute("aria-current");
  });
  const views={jobs:jobPage,saved:jobPage,watchlist,outreach:inbox,settings};
  const html=await views[page]();
  if(page!==S.page||serial!==S.serial) return;
  content.innerHTML=html;
  content.setAttribute("aria-busy","false");
  if(["jobs","saved"].includes(page)) await refreshJobs();
}
function dialog(title,sub,html) {
  body.innerHTML=`<header class="dialog-head"><div><h2 id="dialog-title">${E(title)}</h2>${sub?`<p>${E(sub)}</p>`:""}</div>
  <button type="button" class="button small" data-action="close" aria-label="Close dialog">✕</button></header><div class="dialog-body">${html}</div>`;
  if(!modal.open) modal.showModal();
}
function jobImport() {
  dialog("Add a job link","Save a role you found outside the scan.",`<form class="form" data-form="import">
  <label>Job posting URL<input type="url" name="url" required></label>
  <span class="hint">Supported watchlist links can be read automatically. For other links, enter the company and role.</span>
  <label>Company<input name="company" maxlength="130"></label><label>Role title<input name="title" maxlength="220"></label>
  <label>Location or workplace policy<input name="location" maxlength="350"></label>
  <label>Job description<textarea name="description" maxlength="30000"></textarea></label><div><button class="button primary">Save job</button></div></form>`);
}
function commands() {
  dialog("Run a command","Type ‘Find latest jobs’ to scan your watchlist.",`<form class="form" data-form="command">
  <label>Command<input name="command" placeholder="Find latest jobs" maxlength="80" required autofocus></label>
  <div><button class="button primary">Run command</button></div></form>`);
}
function employer(id) {
  const row=S.companies.find(x=>x.id===id);
  dialog(row?"Edit employer":"Add an employer","Use a board identifier for automated scans.",`<form class="form" data-form="company" data-id="${row?.id||""}">
  <label>Company<input name="name" value="${E(row?.name)}" maxlength="130" required></label>
  <label>Employer careers URL<input type="url" name="url" value="${E(row?.url)}" required></label>
  <label>Feed provider<select name="provider">${select({"":"Manual careers page",greenhouse:"Greenhouse",lever:"Lever",ashby:"Ashby",smartrecruiters:"SmartRecruiters"},row?.provider)}</select></label>
  <label>Board identifier<input name="board" value="${E(row?.board)}" maxlength="120" placeholder="Example: linear"></label>
  <label class="checklabel"><input type="checkbox" name="enabled" ${!row||row.enabled?"checked":""}> Include on the active watchlist</label>
  <div><button class="button primary">Save employer</button></div></form>`);
}
async function openJob(id) { S.job=await api(`/jobs/${id}`); S.people=[]; showJob(); }
function showJob() {
  const job=S.job;
  dialog(job.title,`${job.company} · ${regions[job.region]}`,`<div class="actions">
    ${L("Apply on company site",job.apply_url,"primary")}${B(job.saved?"Remove from saved":"Save job","save",job.id)}${B(job.applied_at?"Undo applied":"Mark applied","apply",job.id)}
    ${B(job.hidden?"Restore to job feed":"Hide from job feed","hide",job.id)}</div>
    <p class="micro">${job.posted_at?`Employer posted ${date(job.posted_at)} · `:""}First seen ${date(job.first_seen)}${job.archived?" · No longer on feed":job.source_status==="failed"?" · Latest feed check failed":""}</p>
    ${job.evidence?`<div class="evidence"><strong>Location evidence</strong><br>${E(job.evidence)}</div>`:""}
    ${job.description?`<details><summary>View job description</summary><div class="description">${E(job.description)}</div></details>`:""}
    <section class="section"><p class="eyebrow">REFERRAL RESEARCH</p><div class="actions">${B("Find people","find",job.id,"primary")}${B("Add someone I know","person")}</div><div id="people"></div></section>`);
}
async function findPeople(id) {
  const target=$("#people");
  target.textContent="Looking for saved contacts and public evidence…";
  try {
    const result=await api(`/jobs/${id}/people`);
    if(!target.isConnected) return;
    S.people=result.people; S.query=result.query;
    const hint={needs_key:"Set a Brave Search key to enable public research. You can use this LinkedIn query now.",
      unavailable:"Public search is temporarily unavailable. Try the LinkedIn query.",searched:"Verify current employment before sending."}[result.status];
    target.innerHTML=`${S.people.map((p,index)=>`<div class="person"><strong>${E(p.name)}</strong><p>${E(p.title)}</p><p>${E(p.reason)}</p><p>${E(p.notes)}</p>
    <div class="actions">${p.source?L("View source",p.source):""}${B("Use this person","person",index,"small")}</div></div>`).join("")}
    ${hint?`<p class="hint">${E(hint)}</p>`:""}<div class="actions">
    ${L("Search LinkedIn",`https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent(S.query)}`)}${B("Copy search query","copy-query","","small")}</div>`;
  } catch(error) { if(target.isConnected) target.textContent=error.message; throw error; }
}
function person(index) {
  const p=S.people[index]||{};
  S.notes=p.notes||"";
  dialog("Prepare a referral request",`${S.job.company} · ${S.job.title}`,`<form class="form" data-form="draft">
    <label>Person<input name="name" required maxlength="120" value="${E(p.name)}"></label>
    <label>Their role<input name="title" maxlength="200" value="${E(p.title)}"></label>
    <label>Source or profile URL<input type="url" name="source" value="${E(p.source)}"></label>
    <label>Channel<select name="channel">${select(channels)}</select></label>
    <label>One relevant fact about you <span class="hint">Optional. For example, “I built a Python and SQL pipeline for a student project.”</span><input name="evidence" maxlength="350"></label>
    <div class="actions"><button class="button primary">Prepare draft</button>${B("Back to job","job-back")}</div></form>`);
}
async function openConversation(id) { S.item=await api(`/outreach/${id}`); showConversation(); }
function followUpText(item) {
  const first = item.person.split(/\s+/)[0];
  return `Hi ${first}, I wanted to follow up on my referral request for the ${item.job_title} role at ${item.company}. If you’re able to point me in the right direction, I’d appreciate it.\n\nThanks again!`;
}
function handoffLinks(item) {
  let profile = "";
  if (item.channel === "linkedin" && item.source && URL.canParse(item.source)) {
    const source = new URL(item.source);
    if (source.protocol === "https:" && (source.hostname === "linkedin.com" || source.hostname.endsWith(".linkedin.com")) && /^\/in\/[^/]+/.test(source.pathname)) profile = source.href;
  }
  let channel = "";
  if (item.conversation_url) channel = L("Open conversation",item.conversation_url);
  else if (profile) channel = L("Open LinkedIn profile",profile);
  else if (item.channel === "linkedin") {
    const query = encodeURIComponent(`${item.person} ${item.company}`);
    channel = L("Find on LinkedIn",`https://www.linkedin.com/search/results/people/?keywords=${query}`);
  } else if (item.channel === "gmail") channel = L("Open Gmail","https://mail.google.com/");
  return channel + L("View job posting",item.job_url) +
    (item.source && !profile && item.source !== item.conversation_url ? L("View contact source",item.source) : "");
}
function showConversation() {
  const i=S.item, waiting=i.state==="waiting";
  const original = i.events.find(e=>e.kind==="sent"&&e.message)?.message || i.draft;
  const event=(label,action,style="small")=>B(label,`event-${action}`,i.id,style);
  dialog(i.person,`${i.company} · ${channels[i.channel]} · ${stages[i.state]}`,`<p class="micro">${E(i.job_title)}${i.due_at?` · Next check ${date(i.due_at)}`:""}</p>
    <div class="actions" id="handoff-links">${handoffLinks(i)}</div>
    ${i.state==="draft"?`<form class="form" data-form="message" data-id="${i.id}"><label>Editable message<textarea id="draft-text" name="text" maxlength="4000" required>${E(i.draft)}</textarea></label>
    <div class="actions">${B("Copy message","copy-message","","primary")}<button class="button">Save edits</button>${S.info.rewrite_configured?B("AI rewrite","rewrite"):""}</div></form>`:
    `<details><summary>Original request</summary><div class="description">${E(original)}</div></details>`}
    ${waiting?`<section class="section"><p class="eyebrow">FOLLOW-UP</p>
    <form class="form" data-form="follow-up" data-id="${i.id}">
      <label>Follow-up draft<textarea id="follow-up-text" name="text" maxlength="4000" required>${E(i.follow_up_draft || followUpText(i))}</textarea></label>
      <p class="hint">Send it in the original conversation, then record the send here.</p>
      <div class="actions">${B("Copy follow-up","copy-follow-up","","primary")}<button class="button">Save draft</button>
      ${event("Record follow-up sent","followed_up","")}</div>
    </form></section>`:""}
    <section class="section"><p class="eyebrow">CONVERSATION ACTIONS</p><div class="actions">
    ${i.state==="draft"?event("Record as sent","sent","primary"):""}
    ${waiting?event("Reply received","replied","primary")+event("Checked, no reply","checked")+event("Snooze 3 days","snoozed")+event("Undo sent","undo_send"):""}
    ${["waiting","replied"].includes(i.state)?event("Referral offered","offered"):""}
    ${["waiting","replied","offered"].includes(i.state)?event("Referral submitted","submitted"):""}
    ${!["closed","submitted"].includes(i.state)?event("Close conversation","closed"):""}</div>
    <div id="gmail-link">${i.channel==="gmail"&&waiting&&S.info.mail.account?(i.thread_id?'<p class="hint">Gmail thread linked.</p>':B("Link Gmail thread","mail-threads",i.id)):""}</div>
    <label class="form">Optional event note<input id="event-note" maxlength="650" placeholder="What did they say or what’s next?"></label></section>
    <details class="section"><summary id="link-settings-title">${i.conversation_url?"Edit conversation link":"Add conversation link"}</summary>
      <form class="form" data-form="conversation-link" data-id="${i.id}"><label>Conversation URL
      <input name="url" type="url" maxlength="1800" value="${E(i.conversation_url)}" placeholder="Paste the original message thread URL"></label>
      <p class="hint">Use this to reopen the same conversation for reply checks. Clear the field to remove the saved link.</p>
      <div><button class="button small">Save conversation link</button></div></form></details>
    <section><p class="eyebrow">HISTORY</p><ul class="timeline">${i.events.length?i.events.map(e=>`<li><strong>${E(historyLabels[e.kind]||e.kind)}</strong> · ${date(e.at,true)}${e.note?`<br>${E(e.note)}`:""}${e.kind==="followed_up"&&e.message?`<details><summary>View sent follow-up</summary><div class="description">${E(e.message)}</div></details>`:""}</li>`).join(""):"<li>Conversation prepared. Sending is recorded separately.</li>"}</ul></section>`);
}
async function mailThreads(id) {
  const target=$("#gmail-link"), rows=await api("/gmail/threads");
  if(!target.isConnected) return;
  target.innerHTML=rows.length?`<form class="form" data-form="thread" data-id="${id}"><label>Choose a labeled sent conversation
  <select name="thread_id" required>${select(Object.fromEntries(rows.map(row=>[row.id,`${row.subject} · ${row.to}`])))}</select></label>
  <div><button class="button">Link thread</button></div></form>`:'<p class="hint">Apply the Job Scout label to your sent conversation in Gmail, then try again.</p>';
}


async function jobFlag(id,kind) {
  const title=$("#dialog-title");
  const changes={save:{saved:!S.job.saved},apply:{applied:!S.job.applied_at},hide:{hidden:!S.job.hidden}};
  const job=await api(`/jobs/${id}`,"PATCH",changes[kind]);
  if(modal.open&&title.isConnected) {S.job=job;showJob();}
  if(kind==="hide") {
    await refreshState(); await render();
    notice(job.hidden?"Job hidden from the feed.":"Job restored to the feed.");
  } else await refreshJobs();
}
async function eventAction(id,type) {
  const title=$("#dialog-title"), data={action:type,note:$("#event-note").value};
  if(type==="sent") data.message=$("#draft-text").value;
  if(type==="followed_up") {
    data.message=$("#follow-up-text").value;
    data.expected_due_at=S.item.due_at;
  }
  const item=await api(`/outreach/${id}/events`,"POST",data);
  if(modal.open&&title.isConnected) {S.item=item;showConversation();}
  await refreshState();
  if(S.page==="outreach") await render();
}
async function copy(text) {
  await navigator.clipboard.writeText(text);
  notice("Copied to clipboard.");
}
async function act(action,id) {
  if(action.startsWith("event-")) return eventAction(id,action.slice(6));
  const handlers={
    close:()=>modal.close(), command:commands, import:jobImport, scan:startScan, "retry-feeds":()=>startScan("retry"), company:()=>employer(Number(id)), job:()=>openJob(id),
    prev:()=>{S.offset=Math.max(0,S.offset-30);return refreshJobs();},
    next:()=>{S.offset+=30;return refreshJobs();},
    "toggle-hidden":()=>{S.hidden=!S.hidden;S.offset=0;return render();},
    save:()=>jobFlag(id,"save"),apply:()=>jobFlag(id,"apply"),hide:()=>jobFlag(id,"hide"),"job-back":showJob,find:()=>findPeople(id),
    person:()=>person(id===""?-1:Number(id)),conversation:()=>openConversation(id),
    "copy-query":()=>copy(S.query),"copy-message":()=>copy($("#draft-text").value),
    "copy-follow-up":()=>copy($("#follow-up-text").value),
    rewrite:async()=>{
      const input=$("#draft-text"),result=await api("/rewrite","POST",{text:input.value});
      if(input.isConnected) input.value=result.text;
      notice("Review the rewrite before saving.");
    },
    "mail-connect":async()=>{location.href=(await api("/gmail/authorize","POST")).url;},
    "forget-brave":async()=>{await saveIntegration("brave","");},
    "forget-google":async()=>{await saveIntegration("google","");},
    "mail-disconnect":async()=>{await api("/gmail/disconnect","POST");await refreshState();await render();notice("Gmail disconnected.");},
    "mail-check":async()=>{
      try {const result=await api("/gmail/check","POST");notice(result.replies?`${result.replies} Gmail replies detected.`:"Gmail checked.");}
      finally {await refreshState();await render();}
    },
    "mail-threads":()=>mailThreads(id),
    "hint-dismiss":async()=>{await api(`/gmail/hints/${encodeURIComponent(id)}/dismiss`,"POST");await render();},
  };
  await handlers[action]?.();
}
async function withButton(button,callback) {
  button.disabled=true;
  try {await callback();}
  catch(error) {notice(error.message,true);}
  finally {if(button.isConnected&&(button.id!=="scan-button"||S.info?.scan?.state!=="running")) button.disabled=false;}
}
async function saveIntegration(name,value) {
  await api(`/integrations/${name}`,"PUT",{value});
  await refreshState();await render();
  notice(value?"Saved in the local credential vault.":"Saved integration removed.");
}
document.addEventListener("click",event=>{
  const button=event.target.closest("[data-action], #scan-button");
  if(button) withButton(button,()=>act(button.dataset.action||"scan",button.dataset.id||""));
});
document.addEventListener("submit",event=>{
  const form=event.target.closest("[data-form]");
  if(!form) return;
  event.preventDefault();
  const data=Object.fromEntries(new FormData(form)),id=form.dataset.id;
  withButton($("button:not([type=button])",form),async()=>{
    switch(form.dataset.form) {
      case "brave":
        await saveIntegration("brave",data.value);
        break;
      case "google":
        if(data.file.size>16000) throw new Error("Choose the downloaded Desktop OAuth JSON file.");
        await saveIntegration("google",await data.file.text());
        break;
      case "command":
        if(!["find latest jobs","find jobs latest","find jobs","scan"].includes(data.command.trim().toLowerCase())) throw new Error("Try ‘Find latest jobs’.");
        modal.close();await startScan();
        break;
      case "import": {
        const result=await api("/jobs","POST",data);
        await refreshState();await openJob(result.id);await refreshJobs();notice("Job saved.");
        break;
      }
      case "company":
        await api(id?`/companies/${id}`:"/companies",id?"PATCH":"POST",{...data,enabled:data.enabled==="on"});
        modal.close();await refreshState();await render();notice("Watchlist updated.");
        break;
      case "draft": {
        const {channel,evidence,...contact}=data;
        const item=await api(`/jobs/${S.job.id}/draft`,"POST",{channel,evidence,contact:{...contact,notes:S.notes}});
        await openConversation(item.id);notice("Draft ready to review.");
        break;
      }
      case "message":
        S.item=await api(`/outreach/${id}`,"PATCH",data);notice("Draft saved.");
        break;
      case "follow-up":
        await api(`/outreach/${id}/follow-up`,"PATCH",data);notice("Follow-up draft saved.");
        break;
      case "conversation-link": {
        const item = await api(`/outreach/${id}/conversation-link`,"PATCH",data);
        if (form.isConnected && modal.open) {
          S.item = item;
          $("#handoff-links").innerHTML = handoffLinks(item);
          $("#link-settings-title").textContent = item.conversation_url ? "Edit conversation link" : "Add conversation link";
        }
        notice("Conversation link updated.");
        break;
      }
      case "thread":
        await api(`/outreach/${id}/gmail`,"POST",data);await openConversation(id);notice("Gmail thread linked.");
        break;
    }
  });
});
document.addEventListener("input",event=>{
  const input=event.target;
  if(input.id==="job-search") {
    S.search=input.value;S.offset=0;
    clearTimeout(searchTimer);searchTimer=setTimeout(()=>refreshJobs().catch(error=>notice(error.message,true)),160);
  } else if(input.id==="company-search") {
    S.companySearch=input.value;$("#company-results").innerHTML=companyRows();
  }
});
document.addEventListener("change",async event=>{
  const input=event.target;
  try {
    if(["region","latest","levels","recent"].includes(input.id)) {
      S[input.id]=input.type==="checkbox"?input.checked:input.value;S.offset=0;await refreshJobs();
    } else if(input.id==="source") {
      S.source=input.value;$("#company-results").innerHTML=companyRows();
    } else if(input.id==="mail-hints") {
      await api("/gmail/options","PATCH",{linkedin_hints:input.checked});await refreshState();notice("Setting saved.");
    }
  } catch(error) {notice(error.message,true);}
});
async function navigate() {
  const page=location.hash.slice(1);
  S.page=["jobs","saved","outreach","watchlist","settings"].includes(page)?page:"jobs";
  S.offset=0;
  if(modal.open) modal.close();
  try {await render();}
  catch(error) {content.innerHTML=empty("Page could not load",error.message);}
}
window.addEventListener("hashchange",navigate);
$(".skip").addEventListener("click",event=>{event.preventDefault();$("#main").focus();});
document.addEventListener("keydown",event=>{
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==="k"&&!modal.open) {event.preventDefault();commands();}
});
try {await refreshState();await navigate();pollScan();}
catch(error) {content.innerHTML=empty("Job Scout could not connect",error.message);}

