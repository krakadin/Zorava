(() => {
  const root = document.querySelector('#app');
  let csrf = document.querySelector('meta[name="csrf-token"]').content;
  const params = new URLSearchParams(location.search);
  const page = params.get('page') || 'overview';
  const jobId = params.get('id');
  const pendingTests = new Set();
  // Workers with an operator-requested CLI version check in flight. Like the
  // test set, it only drives label/disabled state; it never starts a check.
  const pendingUpdates = new Set();
  const navPage = jobId ? 'jobs' : page;
  document.querySelectorAll('[data-nav]').forEach(link => {
    if (link.dataset.nav === navPage) link.classList.add('active');
  });

  // Scroll memory. This page renders asynchronously, so neither the browser nor
  // a reload can put the operator back where they were. Only the vertical
  // offset of this exact URL is kept in sessionStorage - never job, provider,
  // or credential material - and every storage failure stays harmless.
  const scrollKey = `ai-router:scroll:${location.pathname}${location.search}`;
  const readStoredScroll = () => {
    try {
      const stored = Number(window.sessionStorage.getItem(scrollKey));
      return Number.isFinite(stored) && stored > 0 ? Math.round(stored) : 0;
    } catch (error) { return 0; }
  };
  const storeScroll = offset => {
    try {
      if (offset > 0) window.sessionStorage.setItem(scrollKey, String(offset));
      else window.sessionStorage.removeItem(scrollKey);
    } catch (error) { /* blocked or full storage: a refresh then starts at top */ }
  };
  const navigationEntry = (() => {
    try { return performance.getEntriesByType('navigation')[0] || null; } catch (error) { return null; }
  })();
  // A reload (browser refresh or the cancel action below) and back/forward reuse
  // the remembered offset; following a dashboard link still starts at the top.
  const reusesScroll = !navigationEntry || navigationEntry.type === 'reload' || navigationEntry.type === 'back_forward';
  let savedScroll = 0;
  if (reusesScroll) savedScroll = readStoredScroll();
  else storeScroll(0);
  const rememberScroll = () => storeScroll(Math.round(window.scrollY || 0));
  window.addEventListener('pagehide', rememberScroll);
  window.addEventListener('beforeunload', rememberScroll);
  try {
    if ('scrollRestoration' in window.history) window.history.scrollRestoration = 'manual';
  } catch (error) { /* unsupported: the explicit restore below still runs */ }
  const restoreScroll = () => {
    if (savedScroll <= 0) return;
    const apply = () => window.scrollTo(0, savedScroll);
    // Deferred one frame so the freshly rendered content already has layout.
    if (typeof window.requestAnimationFrame === 'function') window.requestAnimationFrame(apply);
    else setTimeout(apply, 0);
  };
  // Timer- and action-driven re-renders rebuild the page from scratch; without
  // this the operator would be snapped back to the top seconds after a restore.
  async function rerenderKeepingScroll(view) {
    const offset = Math.round(window.scrollY || 0);
    await view();
    if (offset > 0 && (window.scrollY || 0) < offset) window.scrollTo(0, offset);
  }

  const node = (tag, text, cls) => {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (cls) item.className = cls;
    return item;
  };
  const title = (name, sub) => {
    const notification = document.querySelector('#flash');
    root.replaceChildren();
    if (notification) root.append(notification);
    root.append(node('h1', name, 'page-title'));
    if (sub) root.append(node('p', sub, 'subtitle'));
  };
  const panel = (heading) => {
    const box = node('section', undefined, 'panel');
    box.append(node('h2', heading));
    return box;
  };
  const addKV = (dl, label, value, mono=false) => {
    dl.append(node('dt', label));
    dl.append(node('dd', value ?? '—', mono ? 'mono' : ''));
  };
  const displayTime = value => {
    if (!value) return 'Not recorded';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  };
  const duration = value => {
    if (value == null) return '—';
    const s = Math.max(0, Math.floor(value / 1000));
    return `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;
  };
  const badge = status => node('span', (status || 'unknown').replaceAll('_',' '), `status status-${String(status||'unknown').toLowerCase()}`);
  // CLI version metadata is read-only: the dashboard reports a pending upgrade
  // but never performs one. Labels come from this fixed map and are written with
  // textContent, so no fetched string is interpreted as markup.
  const updateLabels = {
    'up-to-date': 'Up to date',
    'update-available': 'Upgrade pending',
    'checking': 'Checking…',
    'stale': 'Installed CLI is newer than the published source',
    'unknown': 'Unknown'
  };
  const updateLabel = state => updateLabels[state] || 'Unknown';
  async function get(url) {
    const response = await fetch(url, {cache:'no-store', credentials:'same-origin'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || 'Local request failed.');
    return data;
  }
  async function post(url, body={}) {
    // An open tab can outlive a dashboard restart. Refresh its action token.
    const session = await get('/api/v1/session');
    csrf = session.csrf_token;
    const response = await fetch(url, {method:'POST', credentials:'same-origin', cache:'no-store',
      headers:{'Content-Type':'application/json','X-AI-Worker-CSRF':csrf},body:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || 'Local action failed.');
    return data;
  }
  const flash = message => {
    let target = document.querySelector('#flash');
    if (!target) { target=node('div'); target.id='flash'; target.setAttribute('role','status'); root.prepend(target); }
    target.textContent = message || '';
  };
  function table(headers, rows) {
    const wrap=node('div',undefined,'table-wrap');
    const table=node('table');
    const thead=node('thead'), head=node('tr');
    headers.forEach(item => head.append(node('th',item)));
    thead.append(head); table.append(thead);
    const tbody=node('tbody');
    rows.forEach(row => tbody.append(row));
    if (!rows.length) {
      const tr=node('tr'), td=node('td','No matching records.','empty'); td.colSpan=headers.length; tr.append(td); tbody.append(tr);
    }
    table.append(tbody); wrap.append(table); return wrap;
  }
  function jobRow(job, includeActions=true) {
    const tr=node('tr');
    const worker=node('td'); worker.append(node('strong',job.role || job.worker)); worker.append(node('div',job.worker,'muted')); tr.append(worker);
    const project=node('td',job.cwd || '—','mono'); tr.append(project);
    const task=node('td',job.task_summary || '(provider test)','task-cell'); tr.append(task);
    const elapsed=node('td',duration(job.duration_ms),'mono'); tr.append(elapsed);
    const state=node('td'); state.append(badge(job.status)); tr.append(state);
    const when=node('td',displayTime(job.created_at)); tr.append(when);
    const action=node('td');
    const view=node('a','View','button'); view.href=`/?page=jobs&id=${encodeURIComponent(job.id)}`; action.append(view);
    if (includeActions && (job.status === 'running' || job.status === 'queued')) {
      const cancel=node('button','Cancel','danger'); cancel.dataset.action='cancel'; cancel.dataset.id=job.id; action.append(cancel);
    }
    tr.append(action); return tr;
  }
  function providerCard(info) {
    const testing = info.status === 'TESTING' || pendingTests.has(info.worker);
    const checking = info.update_checking === true || pendingUpdates.has(info.worker);
    const card=node('article',undefined,'card');
    const head=node('div',undefined,'card-head');
    head.append(node('h2',(info.worker || '').toUpperCase())); head.append(badge(testing ? 'TESTING' : info.status));
    if (info.update_state === 'update-available') head.append(node('span',`Upgrade pending · ${info.latest_version}`,'pill upgrade-pill'));
    else if (checking) head.append(node('span','Checking for upgrades…','pill'));
    card.append(head);
    const dl=node('dl',undefined,'kv');
    addKV(dl,'Model',info.requested_model,true); addKV(dl,'Provider',info.provider);
    if (info.role) addKV(dl,'Role',`${info.role} · separate worktree`);
    if (info.concurrency != null) {
      addKV(dl,'Capacity',`${info.concurrency} concurrent job${info.concurrency===1?'':'s'}`);
      addKV(dl,'Active jobs',`running ${info.running_jobs ?? 0} · queued ${info.queued_jobs ?? 0}`);
    }
    addKV(dl,'Authentication',info.authentication); addKV(dl,'Routing',info.routing || 'Worker CLI direct');
    if (info.endpoint_url) addKV(dl,'Base URL',info.endpoint_url,true);
    else if (info.endpoint_host) addKV(dl,'Endpoint host',info.endpoint_host,true);
    if (info.update_state) {
      addKV(dl,'Installed CLI',info.installed_version || info.version || 'Unknown',true);
      addKV(dl,'Latest available',info.latest_version || (checking ? 'Checking…' : 'Unknown'),true);
      addKV(dl,'Upgrade status',updateLabel(info.update_state));
      if (info.update_state === 'update-available') addKV(dl,'Pending upgrade',`${info.installed_version || 'Unknown'} → ${info.latest_version}`,true);
      addKV(dl,'Last version check',info.last_update_check_at ? displayTime(info.last_update_check_at) : 'Not checked');
      if (info.last_update_error) addKV(dl,'Check error',info.last_update_error);
      if (info.update_source) addKV(dl,'Version source',info.update_source,true);
    } else if (info.version) addKV(dl,'CLI version',info.version,true);
    addKV(dl,'Last test',info.last_test_at ? `${displayTime(info.last_test_at)} · ${info.last_test_status}` : 'Not tested');
    if (info.last_success_at) addKV(dl,'Last passed',displayTime(info.last_success_at));
    if (info.last_test_error) addKV(dl,'Test error',info.last_test_error);
    card.append(dl);
    if (info.worker === 'qwen' || info.worker === 'kimi') {
      const actions=node('div',undefined,'provider-actions');
      actions.append(node('small',`Makes a small live ${info.worker === 'qwen' ? 'Token Plan' : 'provider'} request.`));
      const button=node('button',testing ? 'Testing…' : `Test ${info.worker[0].toUpperCase()+info.worker.slice(1)}`,'primary');
      button.disabled=testing;
      button.dataset.action='test'; button.dataset.worker=info.worker; actions.append(button); card.append(actions);
      if (info.last_test_id) {
        const view=node('a','View test result','button');
        view.href=`/?page=jobs&id=${encodeURIComponent(info.last_test_id)}`;actions.append(view);
      }
      // Explicit, read-only version check. The dashboard reports a pending
      // upgrade; installing one stays an operator action outside ai-router.
      const versionActions=node('div',undefined,'provider-actions');
      versionActions.append(node('small','Reads this CLI’s fixed official version source. Read-only: nothing is downloaded, installed, or upgraded.'));
      const checkButton=node('button',checking ? 'Checking…' : 'Check for upgrades');
      checkButton.disabled=checking;
      checkButton.dataset.action='check-updates'; checkButton.dataset.worker=info.worker;
      versionActions.append(checkButton); card.append(versionActions);
    }
    return card;
  }
  async function refreshProviderCards() {
    const data=await get('/api/v1/providers');
    const cards=document.querySelector('#provider-cards');
    if (cards) cards.replaceChildren(...['claude','qwen','kimi'].map(name=>providerCard(data.providers[name])));
  }
  async function followTest(worker, jobId) {
    const deadline=Date.now()+160000;
    while (Date.now()<deadline) {
      const response=await fetch('/api/v1/jobs/'+encodeURIComponent(jobId),{cache:'no-store',credentials:'same-origin'});
      if (response.ok) {
        const {job}=await response.json();
        if (!['queued','running'].includes(job.status)) {
          pendingTests.delete(worker);
          await refreshProviderCards();
          const outcome=job.status==='completed'?'passed':`failed (${job.error_code || job.status})`;
          flash(`${worker.toUpperCase()} test ${outcome} at ${displayTime(job.completed_at)}.`);
          return;
        }
      } else if (response.status!==404) {
        throw new Error('Could not read this test result. Open Jobs to check its status.');
      }
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
    throw new Error('Test result is still pending. Open Jobs to check its status.');
  }
  // The check itself runs in a bounded server-side thread. This loop only reads
  // the cache-only endpoint until it finishes, so polling never triggers a
  // request to a version source and never installs anything.
  async function followUpdateCheck(worker) {
    const deadline=Date.now()+45000;
    while (Date.now()<deadline) {
      const data=await get('/api/v1/updates');
      const entry=(data.updates||{})[worker]||{};
      await refreshProviderCards();
      // The cache-only endpoint reports the flag as update_checking.
      if (!entry.update_checking) {
        pendingUpdates.delete(worker);
        const detail=entry.update_state==='update-available'
          ? `upgrade pending (${entry.installed_version || 'unknown'} → ${entry.latest_version})`
          : updateLabel(entry.update_state).toLowerCase();
        const suffix=entry.last_update_error?` · ${entry.last_update_error}`:'';
        flash(`${worker.toUpperCase()} CLI version check finished: ${detail}${suffix}.`);
        return;
      }
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
    pendingUpdates.delete(worker);
    throw new Error('The CLI version check is still running. Refresh Providers to see its result.');
  }
  async function overview() {
    title('Overview','Local worker health, active jobs, and recent outcomes.');
    root.append(node('div','Claude stays direct to Anthropic. Delegated workers are separate CLI processes.','banner'));
    const data=await get('/api/v1/status');
    const cards=node('div',undefined,'cards');cards.id='provider-cards';
    ['claude','qwen','kimi'].forEach(name => cards.append(providerCard(data.providers[name])));
    root.append(cards);
    const counts=data.counts?.today || {};
    const metrics=node('div',undefined,'metrics');
    [['Jobs today',Object.values(counts).reduce((a,b)=>a+b,0)],['Completed',counts.completed||0],['Failed',counts.failed||0],['Cancelled',counts.cancelled||0]].forEach(([label,value])=>{
      const item=node('div',undefined,'metric'); item.append(node('strong',value)); item.append(node('span',label)); metrics.append(item);
    });
    root.append(metrics);
    const sec=panel('SECURITY STATUS');
    const dl=node('dl',undefined,'kv');
    addKV(dl,'Dashboard bind',data.security.dashboard_loopback?'127.0.0.1':'check required',true);
    addKV(dl,'Runtime state',data.security.runtime_private?`${data.security.runtime_mode} · private`:'missing or unsafe',true);
    addKV(dl,'Worker policy',data.security.worker_mode);
    addKV(dl,'Zorava secrets',data.security.credentials_in_ai_router_config?'review required':'none configured');
    sec.append(dl); root.append(sec);
    const active=panel('ACTIVE JOBS');
    active.append(table(['Worker','Project','Task summary','Duration','Status','Started','Actions'],data.active_jobs.map(j=>jobRow(j)))); root.append(active);
    const recent=panel('RECENT JOBS');
    recent.append(table(['Worker','Project','Task summary','Duration','Status','Created','Actions'],data.recent_jobs.map(j=>jobRow(j,false)))); root.append(recent);
    if (data.recent_failures.length) {
      const failures=panel('RECENT FAILURES'); failures.append(table(['Worker','Project','Task summary','Duration','Status','Created','Actions'],data.recent_failures.map(j=>jobRow(j,false)))); root.append(failures);
    }
  }
  async function jobsPage() {
    if (jobId) return jobDetail();
    title('Jobs','Delegated tasks and provider test history.');
    const filters=node('form',undefined,'filters'); filters.id='job-filters';
    const workerLabel=node('label','Worker'); const worker=document.createElement('select'); worker.name='worker';
    [['','All workers'],['qwen','Qwen'],['kimi','Kimi']].forEach(([v,l])=>{const o=node('option',l);o.value=v;worker.append(o)});workerLabel.append(worker);filters.append(workerLabel);
    const statusLabel=node('label','Status'); const status=document.createElement('select'); status.name='status';
    [['','All statuses'],['running','Running'],['completed','Completed'],['failed','Failed'],['auth_error','Auth error'],['rate_limited','Rate limited'],['timed_out','Timed out'],['cancelled','Cancelled'],['invalid_output','Invalid output']].forEach(([v,l])=>{const o=node('option',l);o.value=v;status.append(o)});statusLabel.append(status);filters.append(statusLabel);
    const dateLabel=node('label','Date (UTC)');const date=document.createElement('input');date.type='date';date.name='date';dateLabel.append(date);filters.append(dateLabel);
    const searchLabel=node('label','Project or task');const search=node('input');search.type='search';search.name='q';searchLabel.append(search);filters.append(searchLabel);
    const submit=node('button','Filter','primary');submit.type='submit';filters.append(submit);root.append(filters);
    const results=node('div');results.id='job-results';root.append(results);
    const load=async()=>{const q=new URLSearchParams(new FormData(filters));const data=await get('/api/v1/jobs?'+q.toString());results.replaceChildren(table(['Worker','Project','Task summary','Duration','Status','Created','Actions'],data.jobs.map(j=>jobRow(j))))};
    filters.addEventListener('submit',event=>{event.preventDefault();load().catch(err=>flash(err.message))});
    await load();
  }
  async function jobDetail() {
    title('Job detail','Sanitized local record · worker output is advisory.');
    const back=node('a','← Back to jobs','crumb');back.href='/?page=jobs';root.prepend(back);
    const data=await get('/api/v1/jobs/'+encodeURIComponent(jobId));const job=data.job;
    const summary=panel('SUMMARY'); const dl=node('dl',undefined,'kv');
    [['Job ID',job.id],['Parent/orchestrator','Claude'],['Worker',job.role+' ('+job.worker+')'],['Requested model',job.requested_model],['Reported model',job.reported_model||'Not provided'],['CLI version',job.worker_version],['Project',job.cwd],['Mode',job.mode],['Status',job.status],['Created',displayTime(job.created_at)],['Started',displayTime(job.started_at)],['Completed',displayTime(job.completed_at)],['Duration',duration(job.duration_ms)],['Exit code',job.exit_code],['Usage',job.usage?JSON.stringify(job.usage):'Not reported by worker']].forEach(([a,b])=>addKV(dl,a,b,true));
    summary.append(dl); if(job.status==='running'||job.status==='queued'){const button=node('button','Cancel job','danger');button.dataset.action='cancel';button.dataset.id=job.id;summary.append(button)}root.append(summary);
    const task=panel('TASK SUMMARY');task.append(node('pre',job.task_summary||'(Provider test)', 'prose'));root.append(task);
    if(job.result){const output=panel('WORKER OUTPUT');output.append(node('pre',job.result,'prose'));root.append(output)}
    if(job.partial_result){const output=panel('PARTIAL RESULT');output.append(node('pre',job.partial_result,'prose'));root.append(output)}
    if(job.error_code||job.error_message){const errors=panel('ERRORS');errors.append(node('p',`${job.error_code||'ERROR'}: ${job.error_message||''}`,'error-text'));root.append(errors)}
    const logs=panel('SANITIZED EVENT LOG');const list=node('ol',undefined,'event-list');
    (job.events||[]).forEach(event=>{const li=node('li');li.append(node('time',displayTime(event.timestamp),'mono'),node('strong',event.event),node('span',event.detail||''));list.append(li)});
    logs.append(list);root.append(logs);
  }
  async function providersPage() {
    title('Providers','Configured provider routes and cached test status. Opening this page makes no provider calls.');
    const data=await get('/api/v1/providers');const cards=node('div',undefined,'cards');cards.id='provider-cards';
    ['claude','qwen','kimi'].forEach(name=>cards.append(providerCard(data.providers[name])));root.append(cards);
    const note=node('p','Credentials remain in their provider-owned CLI configuration. Test status refreshes automatically from local records; provider calls only run when you click Test. CLI version metadata is cached: a version source is read only when you click Check for upgrades, and this dashboard never installs an upgrade.','notice');root.append(note);
    const config=panel('MODEL AND ROUTING');
    config.append(node('p','The dashboard currently reports the verified model profile. Changing models or provider URLs is not enabled here; only locally verified model/endpoint combinations should be added. Qwen’s Token Plan key must not be sent to its separate pay-as-you-go endpoints.','prose'));
    root.append(config);
  }
  async function permissionsPage() {
    title('Permissions','Worker permissions are independent of Claude’s local permissions.');
    const data=await get('/api/v1/permissions');const cards=node('div',undefined,'grid-two');
    for(const name of ['qwen','kimi']){const p=data[name],box=panel(name==='qwen'?'QWEN CODER':'KIMI CODER');const dl=node('dl',undefined,'kv');
      addKV(dl,'Filesystem',p.filesystem);addKV(dl,'Shell',p.shell);addKV(dl,'Git writes',p.git_writes);addKV(dl,'Push',p.push);addKV(dl,'Deploy',p.deploy);box.append(dl);cards.append(box)}
    root.append(cards);root.append(node('p',data.os_sandbox,'notice'));
  }
  async function logsPage() {
    title('Logs','Sanitized operational events stored with local job history.');
    const filters=node('div',undefined,'filters');
    const workerLabel=node('label','Worker');const worker=document.createElement('select');worker.id='log-worker';[['','All'],['qwen','Qwen'],['kimi','Kimi']].forEach(([v,l])=>{const o=node('option',l);o.value=v;worker.append(o)});workerLabel.append(worker);filters.append(workerLabel);
    const errorLabel=node('label','View');const errors=document.createElement('select');errors.id='log-errors';[['0','All events'],['1','Errors only']].forEach(([v,l])=>{const o=node('option',l);o.value=v;errors.append(o)});errorLabel.append(errors);filters.append(errorLabel);root.append(filters);
    const output=node('div');output.id='log-results';root.append(output);
    const load=async()=>{const q=new URLSearchParams();if(worker.value)q.set('worker',worker.value);if(errors.value==='1')q.set('errors','1');const data=await get('/api/v1/logs?'+q.toString());const rows=data.events.map(e=>{const tr=node('tr');[displayTime(e.timestamp),e.worker,e.event,e.error_code||'',e.cwd,e.detail].forEach((v,i)=>tr.append(node('td',v,i===4?'mono':'')));return tr});output.replaceChildren(table(['Time','Worker','Event','Error','Project','Detail'],rows))};
    worker.addEventListener('change',()=>load().catch(e=>flash(e.message)));errors.addEventListener('change',()=>load().catch(e=>flash(e.message)));await load();
  }
  async function settingsPage() {
    title('Settings','Operational settings for the local workers. Secrets and provider credential values are never shown.');
    const data=await get('/api/v1/settings');const box=panel('WORKER SETTINGS');const dl=node('dl',undefined,'kv');
    addKV(dl,'Default worker mode','Coding in separate Git worktrees');addKV(dl,'Allowed project root',data.allowed_roots.join(', '),true);addKV(dl,'Qwen default timeout',`${data.qwen_timeout_seconds}s`);addKV(dl,'Kimi default timeout',`${data.kimi_timeout_seconds}s`);addKV(dl,'Maximum timeout',`${data.max_timeout_seconds}s`);addKV(dl,'Concurrency',`Qwen ${data.concurrency.qwen} · Kimi ${data.concurrency.kimi}`);addKV(dl,'Log/result retention',`${data.retention_days} days`);addKV(dl,'Dashboard bind',`${data.dashboard_bind}:${data.dashboard_port}`,true);addKV(dl,'Runtime state',data.runtime_state,true);box.append(dl);root.append(box);
    const limits=data.concurrency_limits||{minimum:1,maximum:4};
    const capacity=panel('WORKER CONCURRENCY');
    capacity.append(node('p',`Each worker runs up to its slot capacity at once; extra jobs wait queued and start when a slot frees. Changes apply to future jobs only and are saved to the same private settings.json as the CLI.`,'notice'));
    for(const worker of ['qwen','kimi']) {
      const label=worker==='qwen'?'Qwen':'Kimi';
      const row=node('div',undefined,'setting-row');
      const field=node('label',`${label} concurrency`);
      const select=document.createElement('select');select.id=`concurrency-${worker}`;select.name=`concurrency-${worker}`;
      for(let value=limits.minimum;value<=limits.maximum;value++) {
        const option=node('option',`${value} concurrent job${value===1?'':'s'}`);option.value=String(value);
        if(value===data.concurrency[worker]) option.selected=true;
        select.append(option);
      }
      field.append(select);row.append(field);
      row.append(node('small',`Current: ${data.concurrency[worker]} · allowed ${limits.minimum}–${limits.maximum} · extra jobs queue`));
      const save=node('button','Save','primary');save.dataset.action='save-concurrency';save.dataset.worker=worker;row.append(save);
      capacity.append(row);
    }
    root.append(capacity);
    const cleanup=panel('RETENTION CLEANUP');cleanup.append(node('p','Expired Zorava job records, results, and event logs older than 30 days can be purged. Active jobs and retained Kimi isolated-edit worktrees are protected. Provider CLI histories are never touched.','notice'));
    const actions=node('div',undefined,'actions-bar');const preview=node('button','Preview expired records');preview.dataset.action='cleanup-preview';actions.append(preview);cleanup.append(actions);const output=node('div');output.id='cleanup-preview';cleanup.append(output);root.append(cleanup);
  }
  async function render() {
    try {
      if(page==='overview') await overview();
      else if(page==='jobs') await jobsPage();
      else if(page==='providers') await providersPage();
      else if(page==='permissions') await permissionsPage();
      else if(page==='logs') await logsPage();
      else if(page==='settings') await settingsPage();
      else {title('Not found','Choose a dashboard section from the navigation.');}
    } catch(error) { root.replaceChildren(node('h1','Dashboard error','page-title'),node('p',error.message,'error-text')); }
  }
  root.addEventListener('click',async event=>{
    const button=event.target.closest('button[data-action]');if(!button)return;
    const action=button.dataset.action;button.disabled=true;
    try {
      if(action==='test'){
        pendingTests.add(button.dataset.worker);button.textContent='Testing…';
        const result=await post('/api/v1/test/'+button.dataset.worker);
        flash(`Started ${button.dataset.worker} provider test · job ${result.job_id}`);
        await refreshProviderCards();
        await followTest(button.dataset.worker,result.job_id);
      } else if(action==='check-updates'){
        const worker=button.dataset.worker;
        pendingUpdates.add(worker);button.textContent='Checking…';
        await post('/api/v1/updates/check',{workers:[worker]});
        flash(`Requested the ${worker.toUpperCase()} CLI version check. Nothing is downloaded or installed.`);
        await refreshProviderCards();
        await followUpdateCheck(worker);
      } else if(action==='cancel'){
        if(!window.confirm('Cancel this worker job?')) return;
        const result=await post(`/api/v1/jobs/${encodeURIComponent(button.dataset.id)}/cancel`);
        flash(`Cancellation requested for ${result.job_id}.`);setTimeout(()=>location.reload(),1000);
      } else if(action==='save-concurrency'){
        const worker=button.dataset.worker;
        const select=document.querySelector(`#concurrency-${worker}`);
        if(!select) throw new Error('Concurrency selector is not available on this page.');
        const result=await post('/api/v1/settings/concurrency',{worker:worker,concurrency:Number.parseInt(select.value,10)});
        flash(result.note || `Saved ${worker} concurrency.`);
        await rerenderKeepingScroll(settingsPage);
      } else if(action==='cleanup-preview'){
        const result=await post('/api/v1/cleanup/preview');const target=document.querySelector('#cleanup-preview');target.replaceChildren();
        target.append(node('p',`${result.eligible_count} expired record(s) eligible; ${result.protected_count} protected.`));
        const confirm=node('button',`Purge ${result.eligible_count} expired record(s)`,'danger');confirm.dataset.action='cleanup-confirm';confirm.disabled=result.eligible_count===0;target.append(confirm);
      } else if(action==='cleanup-confirm'){
        if(!window.confirm('Permanently remove expired Zorava job records, results, and events? Active jobs and retained Kimi worktrees are excluded.')) return;
        const result=await post('/api/v1/cleanup/confirm',{confirm:true});flash(`Purged ${result.deleted_count} expired record(s).`);await rerenderKeepingScroll(settingsPage);
      }
    } catch(error){flash(error.message)}
    finally {
      if(action==='test'){
        pendingTests.delete(button.dataset.worker);
        await refreshProviderCards().catch(error=>flash(error.message));
      } else if(action==='check-updates'){
        pendingUpdates.delete(button.dataset.worker);
        await refreshProviderCards().catch(error=>flash(error.message));
      } else button.disabled=false;
    }
  });
  render().then(() => {
    restoreScroll();
    if (page === 'overview') setInterval(() => rerenderKeepingScroll(overview).catch(error => flash(error.message)), 4000);
    if (page === 'providers') setInterval(() => refreshProviderCards().catch(error => flash(error.message)), 4000);
  });
})();
