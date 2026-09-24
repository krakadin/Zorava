(() => {
  const root = document.querySelector('#app');
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const params = new URLSearchParams(location.search);
  const page = params.get('page') || 'overview';
  const jobId = params.get('id');
  const navPage = jobId ? 'jobs' : page;
  document.querySelectorAll('[data-nav]').forEach(link => {
    if (link.dataset.nav === navPage) link.classList.add('active');
  });

  const node = (tag, text, cls) => {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (cls) item.className = cls;
    return item;
  };
  const title = (name, sub) => {
    root.replaceChildren();
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
  async function get(url) {
    const response = await fetch(url, {cache:'no-store', credentials:'same-origin'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || 'Local request failed.');
    return data;
  }
  async function post(url, body={}) {
    const response = await fetch(url, {method:'POST', credentials:'same-origin', cache:'no-store',
      headers:{'Content-Type':'application/json','X-AI-Worker-CSRF':csrf},body:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || 'Local action failed.');
    return data;
  }
  const flash = message => {
    let target = document.querySelector('#flash');
    if (!target) { target=node('div'); target.id='flash'; root.prepend(target); }
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
    if (includeActions && job.status === 'running') {
      const cancel=node('button','Cancel','danger'); cancel.dataset.action='cancel'; cancel.dataset.id=job.id; action.append(cancel);
    }
    tr.append(action); return tr;
  }
  function providerCard(info) {
    const card=node('article',undefined,'card');
    const head=node('div',undefined,'card-head');
    head.append(node('h2',(info.worker || '').toUpperCase())); head.append(badge(info.status)); card.append(head);
    const dl=node('dl',undefined,'kv');
    addKV(dl,'Model',info.requested_model,true); addKV(dl,'Provider',info.provider);
    addKV(dl,'Authentication',info.authentication); addKV(dl,'Routing',info.routing || 'Worker CLI direct');
    if (info.endpoint_url) addKV(dl,'Base URL',info.endpoint_url,true);
    else if (info.endpoint_host) addKV(dl,'Endpoint host',info.endpoint_host,true);
    if (info.version) addKV(dl,'CLI version',info.version,true);
    addKV(dl,'Last test',info.last_test_at ? `${displayTime(info.last_test_at)} · ${info.last_test_status}` : 'Not tested');
    card.append(dl);
    if (info.worker === 'qwen' || info.worker === 'kimi') {
      const actions=node('div',undefined,'provider-actions');
      actions.append(node('small',`Makes a small live ${info.worker === 'qwen' ? 'Token Plan' : 'provider'} request.`));
      const button=node('button',`Test ${info.worker[0].toUpperCase()+info.worker.slice(1)}`,'primary');
      button.dataset.action='test'; button.dataset.worker=info.worker; actions.append(button); card.append(actions);
    }
    return card;
  }
  async function overview() {
    title('Overview','Local worker health, active jobs, and recent outcomes.');
    root.append(node('div','Claude stays direct to Anthropic. Delegated workers are separate CLI processes.','banner'));
    const data=await get('/api/v1/status');
    const cards=node('div',undefined,'cards');
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
    addKV(dl,'ai-router secrets',data.security.credentials_in_ai_router_config?'review required':'none configured');
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
    root.prepend(node('a','← Back to jobs','crumb')).href='/?page=jobs';
    const data=await get('/api/v1/jobs/'+encodeURIComponent(jobId));const job=data.job;
    const summary=panel('SUMMARY'); const dl=node('dl',undefined,'kv');
    [['Job ID',job.id],['Parent/orchestrator','Claude'],['Worker',job.role+' ('+job.worker+')'],['Requested model',job.requested_model],['Reported model',job.reported_model||'Not provided'],['CLI version',job.worker_version],['Project',job.cwd],['Mode',job.mode],['Status',job.status],['Created',displayTime(job.created_at)],['Started',displayTime(job.started_at)],['Completed',displayTime(job.completed_at)],['Duration',duration(job.duration_ms)],['Exit code',job.exit_code],['Usage',job.usage?JSON.stringify(job.usage):'Not reported by worker']].forEach(([a,b])=>addKV(dl,a,b,true));
    summary.append(dl); if(job.status==='running'){const button=node('button','Cancel job','danger');button.dataset.action='cancel';button.dataset.id=job.id;summary.append(button)}root.append(summary);
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
    const data=await get('/api/v1/providers');const cards=node('div',undefined,'cards');
    ['claude','qwen','kimi'].forEach(name=>cards.append(providerCard(data.providers[name])));root.append(cards);
    const note=node('p','Credentials remain in their provider-owned CLI configuration. This page never displays tokens or performs automatic health polling.','notice');root.append(note);
    const config=panel('MODEL AND ROUTING');
    config.append(node('p','The dashboard currently reports the verified model profile. Changing models or provider URLs is not enabled here; only locally verified model/endpoint combinations should be added. Qwen’s Token Plan key must not be sent to its separate pay-as-you-go endpoints.','prose'));
    root.append(config);
  }
  async function permissionsPage() {
    title('Permissions','Worker permissions are independent of Claude’s local permissions.');
    const data=await get('/api/v1/permissions');const cards=node('div',undefined,'grid-two');
    for(const name of ['qwen','kimi']){const p=data[name],box=panel(name==='qwen'?'QWEN RESEARCHER':'KIMI CODER');const dl=node('dl',undefined,'kv');
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
    title('Settings','Read-only operational configuration. Secrets and provider credential values are never shown.');
    const data=await get('/api/v1/settings');const box=panel('WORKER SETTINGS');const dl=node('dl',undefined,'kv');
    addKV(dl,'Allowed project root',data.allowed_roots.join(', '),true);addKV(dl,'Qwen default timeout',`${data.qwen_timeout_seconds}s`);addKV(dl,'Kimi default timeout',`${data.kimi_timeout_seconds}s`);addKV(dl,'Maximum timeout',`${data.max_timeout_seconds}s`);addKV(dl,'Concurrency',`Qwen ${data.concurrency.qwen} · Kimi ${data.concurrency.kimi}`);addKV(dl,'Log/result retention',`${data.retention_days} days`);addKV(dl,'Dashboard bind',`${data.dashboard_bind}:${data.dashboard_port}`,true);addKV(dl,'Runtime state',data.runtime_state,true);box.append(dl);root.append(box);
    const cleanup=panel('RETENTION CLEANUP');cleanup.append(node('p','Expired ai-router job records, results, and event logs older than 30 days can be purged. Active jobs and retained Kimi isolated-edit worktrees are protected. Provider CLI histories are never touched.','notice'));
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
        const result=await post('/api/v1/test/'+button.dataset.worker);
        flash(`Started ${button.dataset.worker} provider test · job ${result.job_id}`);
        setTimeout(()=>{if(page==='overview'||page==='providers')location.reload();},1500);
      } else if(action==='cancel'){
        if(!window.confirm('Cancel this worker job?')) return;
        const result=await post(`/api/v1/jobs/${encodeURIComponent(button.dataset.id)}/cancel`);
        flash(`Cancellation requested for ${result.job_id}.`);setTimeout(()=>location.reload(),1000);
      } else if(action==='cleanup-preview'){
        const result=await post('/api/v1/cleanup/preview');const target=document.querySelector('#cleanup-preview');target.replaceChildren();
        target.append(node('p',`${result.eligible_count} expired record(s) eligible; ${result.protected_count} protected.`));
        const confirm=node('button',`Purge ${result.eligible_count} expired record(s)`,'danger');confirm.dataset.action='cleanup-confirm';confirm.disabled=result.eligible_count===0;target.append(confirm);
      } else if(action==='cleanup-confirm'){
        if(!window.confirm('Permanently remove expired ai-router job records, results, and events? Active jobs and retained Kimi worktrees are excluded.')) return;
        const result=await post('/api/v1/cleanup/confirm',{confirm:true});flash(`Purged ${result.deleted_count} expired record(s).`);await settingsPage();
      }
    } catch(error){flash(error.message)}
    finally {button.disabled=false}
  });
  render().then(() => {
    if (page === 'overview') setInterval(() => overview().catch(error => flash(error.message)), 4000);
  });
})();
