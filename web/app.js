(function () {
  let DOCS = null; const $ = s => document.querySelector(s);
  const { esc, fmtMin } = CRLib;
  const JIRA = 'https://jooservices.atlassian.net', GH = 'https://github.com/jooservices';
  const L = {
    ticket: k => JIRA + '/browse/' + k, board: p => p.board ? JIRA + '/jira/software/projects/' + p.key + '/boards/' + p.board : JIRA + '/jira/software/projects/' + p.key + '/list',
    issues: p => JIRA + '/issues/?jql=' + encodeURIComponent('project = ' + p.key + ' AND statusCategory != Done ORDER BY updated DESC'),
    repo: p => GH + '/' + p.repo, pulls: (p, q) => GH + '/' + p.repo + '/pulls?q=' + encodeURIComponent(q || 'is:pr'), actions: p => GH + '/' + p.repo + '/actions'
  };
  const STATUS_COLOR = { 'Backlog': 'var(--backlog)', 'To Do': 'var(--todo)', 'In Progress': 'var(--run)', 'In Review': 'var(--run)', 'Review passed': 'var(--wait)', 'In QA': 'var(--wait)', 'Human review': 'var(--human)', 'Ready to merge': 'var(--human)', 'Blocked': 'var(--blocked)', 'Done': 'var(--done)', 'Declined': 'var(--backlog)' };
  const ROLE_COLOR = { developer: 'var(--run)', reviewer: 'var(--todo)', qa: 'var(--wait)' };
  let filter = 'all', projectFilter = 'all', sortKey = 'open', sortDir = -1, sel = null, drawerOpener = null;
  const clock = d => new Date(d).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const P = () => (DOCS.projects && DOCS.projects.list) || [];
  const pmap = () => { const m = {}; P().forEach(p => m[p.key] = p); return m; };

  /* ---------- liveness ---------- */
  function liveBadge() {
    const b = CRLib.badge(DOCS && DOCS.meta && DOCS.meta.asOf, Date.now());
    $('#live').className = 'live' + (b.level ? ' ' + b.level : ''); $('#liveText').textContent = b.text;
  }
  setInterval(liveBadge, 1000);
  const SRC = { jira: 'Jira', github: 'GitHub', machine: 'Worker machine' };
  function sources() {
    const ok = (DOCS && DOCS.meta && DOCS.meta.ok) || {}, bad = Object.keys(ok).filter(k => ok[k] === false);
    const el = $('#srcwarn'); if (!el) return;
    el.hidden = !bad.length; el.textContent = bad.length ? 'Last collection failed for ' + bad.map(k => SRC[k] || k).join(', ') + '; its figures may be stale.' : '';
  }

  /* ---------- KPIs ---------- */
  function kpis() {
    const f = DOCS.flow, m = DOCS.machine; const inf = f.inflight; const by = s => inf.filter(t => t.status === s).length;
    const working = (m.workersBusy || 0); const waiting = by('Human review') + by('Ready to merge') + by('Blocked');
    const items = [
      ['Working now', working + (m.workersTotal ? ' / ' + m.workersTotal : ''), working ? 'AI steps holding a worker' : 'workers idle', 'var(--run)'],
      ['In the flow', inf.filter(t => t.status !== 'Blocked').length, 'To Do through Ready to merge', 'var(--todo)'],
      ['Waiting for owner', waiting, by('Human review') + by('Ready to merge') + ' approval · ' + by('Blocked') + ' blocked', 'var(--human)'],
      ['Done, 24 hours', f.doneLast24h, f.doneByAiLast24h + ' worked by an AI, the rest closed by hand', 'var(--done)'],
      ['Merged PRs, 24 hours', f.mergedPRs24h, f.watched.join(' and ') + ' repositories', 'var(--done)'],
      ['Typical time', f.cycle.p50 == null ? '–' : fmtMin(f.cycle.p50), 'In Progress to Done, ' + f.cycle.n + ' tickets (' + fmtMin(f.cycle.p25) + ' to ' + fmtMin(f.cycle.p75) + ')', 'var(--accent)'],
      ['Rework, 24 hours', f.reworkLast24h, 'tickets sent back to To Do', 'var(--wait)'],
      ['Machine load', m.load ? m.load[0].toFixed(1) : '–', 'of ' + m.cpus + ' CPUs · ' + (f.paused ? 'PAUSED' : 'running'), f.paused ? 'var(--blocked)' : 'var(--run)']];
    $('#kpis').innerHTML = items.map(([l, v, s, c]) => '<div class="kpi" data-css="--c:' + c + '"><span>' + l + '</span><b>' + v + '</b><small>' + esc(s) + '</small></div>').join('');
  }

  /* ---------- timeline ---------- */
  function timeline() {
    const tl = DOCS.timeline, runs = tl.runs, merges = tl.merges; const W = 1000, L0 = 90, T = 8, laneH = 40, span = 6 * 3600; const now = tl.now, t0 = now - span; const x = t => L0 + (t - t0) / span * (W - L0 - 8);
    // pack runs into the fewest lanes (one lane per busy worker)
    const laneEnd = []; const placed = runs.map(r => { let i = laneEnd.findIndex(e => e <= r.start); if (i < 0) { i = laneEnd.length; laneEnd.push(0); } laneEnd[i] = r.end; return { r, lane: i }; });
    const lanes = Math.max(2, laneEnd.length); const H = T + (lanes + 1) * (laneH + 4) + 26; $('#tl').setAttribute('viewBox', '0 0 1000 ' + H);
    let h = '';
    for (let i = 0; i < lanes; i++) h += '<rect x="' + L0 + '" y="' + (T + i * (laneH + 4)) + '" width="' + (W - L0 - 8) + '" height="' + laneH + '" rx="4" fill="var(--panel-2)"/><text class="lane-label" x="8" y="' + (T + i * (laneH + 4) + laneH / 2 + 4) + '">Run lane ' + (i + 1) + '</text>';
    const my = T + lanes * (laneH + 4); h += '<rect x="' + L0 + '" y="' + my + '" width="' + (W - L0 - 8) + '" height="' + laneH + '" rx="4" fill="var(--panel-2)"/><text class="lane-label" x="8" y="' + (my + laneH / 2 + 4) + '">Merges</text>';
    const first = Math.ceil(t0 / 1800) * 1800; for (let m = first; m <= now; m += 1800) { const xx = x(m); h += '<line class="tick" x1="' + xx + '" x2="' + xx + '" y1="' + T + '" y2="' + (my + laneH) + '"/><text class="tick-l" x="' + xx + '" y="' + (H - 8) + '" text-anchor="middle">' + clock(m * 1000) + '</text>'; }
    placed.forEach(({ r, lane }) => { const xs = Math.max(L0, x(r.start)), xe = Math.min(W - 8, x(r.end)); if (xe - xs < 1.5) return; const y = T + lane * (laneH + 4) + 5, hh = laneH - 10, wide = xe - xs > 52;
      h += '<g class="seg-bar" data-k="' + r.key + '"><title>' + r.key + ' · ' + r.ai + ' ' + r.role + ' · ' + r.minutes + ' min' + (r.ok ? '' : ' · ended badly') + '</title><rect x="' + xs.toFixed(1) + '" y="' + y + '" width="' + (xe - xs).toFixed(1) + '" height="' + hh + '" rx="3" fill="' + (ROLE_COLOR[r.role] || 'var(--run)') + '"' + (r.ok ? '' : ' stroke="var(--blocked)" stroke-width="2"') + '/>' + (wide ? '<text class="seg-t" x="' + (xs + 4).toFixed(1) + '" y="' + (y + 13) + '">' + r.key + '</text><text class="seg-t" data-css="opacity:.75" x="' + (xs + 4).toFixed(1) + '" y="' + (y + 25) + '">' + r.ai + ' ' + r.role.slice(0, 3) + '</text>' : '') + '</g>'; });
    merges.forEach(m => { const xx = x(new Date(m.at).getTime() / 1000); if (xx < L0) return; h += '<a href="' + m.url + '" target="_blank" rel="noopener noreferrer"><title>PR #' + m.number + ' · ' + esc(m.title) + '</title><path d="M' + xx + ' ' + (my + 6) + 'l7 ' + (laneH / 2 - 6) + 'l-7 ' + (laneH / 2 - 6) + 'l-7 -' + (laneH / 2 - 6) + 'z" fill="var(--done)"/></a>'; });
    h += '<line class="now" x1="' + x(now) + '" x2="' + x(now) + '" y1="' + (T - 4) + '" y2="' + (my + laneH + 2) + '"/>';
    $('#tl').innerHTML = h; $('#tl').querySelectorAll('.seg-bar').forEach(g => g.onclick = () => openTicket(g.dataset.k));
    $('#tlnote').textContent = runs.length ? runs.length + ' AI runs and ' + merges.length + ' merges in the last 6 hours · click a bar' : 'no AI run in the last 6 hours';
  }
  $('#legend').innerHTML = [['Developer', 'var(--run)'], ['Reviewer', 'var(--todo)'], ['QA', 'var(--wait)'], ['Merged pull request', 'var(--done)'], ['Red outline: the run ended badly', 'var(--blocked)']].map(([l, c]) => '<span><i data-css="background:' + c + '"></i>' + l + '</span>').join('');

  /* ---------- tickets now ---------- */
  function funnel() {
    const f = DOCS.flow, c = f.statusCounts; const order = ['Backlog', 'To Do', 'In Progress', 'In Review', 'Review passed', 'In QA', 'Human review', 'Ready to merge', 'Blocked'];
    const rows = order.map(s => [s, s === 'Backlog' ? (c['Backlog'] || 0) : f.inflight.filter(t => t.status === s).length, STATUS_COLOR[s]]).concat([['Done, 24 h', f.doneLast24h, 'var(--done)']]);
    const mx = Math.max(1, ...rows.map(r => r[1])); $('#fun').innerHTML = rows.map(([l, v, col]) => '<div><span>' + l + '</span><span><i data-css="--c:' + col + ';width:' + (v / mx * 100) + '%"></i></span><b>' + v + '</b></div>').join('');
    $('#funnote').textContent = 'Projects on the flow: ' + f.watched.join(', ') + '. Tickets with the hold label are not counted.';
  }

  /* ---------- projects ---------- */
  const etaText = (item, onFlow) => {
    if (!onFlow) return 'not on the flow';
    if (!item.eta) return 'not enough data (' + (item.etaSamples || 0) + ' samples)';
    return fmtMin(item.eta.low) + ' to ' + fmtMin(item.eta.high) + ' (median ' + fmtMin(item.eta.middle) + ', ' + item.etaSamples + ' samples)';
  };
  function projectRows() {
    const rows = P().filter(p => p.total > 0).map(p => { const c = p.counts, declined = c.Declined || 0, tot = p.total - declined, done = c.Done || 0; return { p, tot, done, prog: done / Math.max(1, tot), open: p.total - declined - done }; });
    const f = { name: r => r.p.name.toLowerCase(), prog: r => r.prog, open: r => r.open }[sortKey]; rows.sort((a, b) => (f(a) > f(b) ? 1 : f(a) < f(b) ? -1 : 0) * sortDir * (sortKey === 'name' ? -1 : 1));
    $('#ptable tbody').innerHTML = rows.map(r => { const p = r.p, c = p.counts, seg = [['Done', 'var(--done)'], ['In QA', 'var(--wait)'], ['Human review', 'var(--human)'], ['In Review', 'var(--run)'], ['In Progress', 'var(--run)'], ['To Do', 'var(--todo)'], ['Blocked', 'var(--blocked)'], ['Backlog', 'var(--backlog)']];
      return '<tr class="row" data-p="' + p.key + '"><td><b>' + esc(p.name) + '</b> ' + (p.onFlow ? '<span class="pill" data-css="--c:var(--run)">on the flow</span>' : '') + '<div class="mono" data-css="color:var(--ink-2)">' + p.key + '</div></td><td><div class="stack" title="' + esc(Object.entries(c).map(([k, v]) => k + ' ' + v).join(', ')) + '">' + seg.map(([k, col]) => '<i data-css="width:' + ((c[k] || 0) / p.total * 100) + '%;background:' + col + '"></i>').join('') + '</div></td><td class="num">' + Math.round(r.prog * 100) + '%<div class="meter" data-css="width:70px"><i data-css="width:' + r.prog * 100 + '%"></i></div></td><td class="num">' + r.open + '</td><td class="num">' + esc(etaText(p, p.onFlow)) + '</td><td class="num">' + esc((p.lastUpdate || '').replace('T', ' ')) + '</td><td class="xl"><a href="' + L.board(p) + '" target="_blank" rel="noopener noreferrer">Board</a><a href="' + L.repo(p) + '" target="_blank" rel="noopener noreferrer">Repo</a></td></tr>'; }).join('');
    $('#ptable tbody').querySelectorAll('tr.row').forEach(tr => tr.onclick = e => { if (!e.target.closest('.xl')) openProject(tr.dataset.p); });
  }
  document.querySelectorAll('#ptable th button').forEach(b => b.onclick = () => { if (sortKey === b.dataset.s) sortDir = -sortDir; else { sortKey = b.dataset.s; sortDir = b.dataset.s === 'name' ? 1 : -1; } projectRows(); });

  /* ---------- AI, machine, events ---------- */
  function aiTable() {
    $('#aitable tbody').innerHTML = DOCS.ai.agents.map(a => { const st = !a.usable ? ['out of pool', 'var(--blocked)'] : a.cooling ? ['cooling', 'var(--wait)'] : ['ready', 'var(--done)'];
      return '<tr class="row" data-a="' + a.id + '"><td><b>' + a.name + '</b><div class="mono" data-css="color:var(--ink-2)">' + esc(a.model) + (a.effort !== 'default' ? ' · ' + a.effort : '') + '</div></td><td><span class="pill" data-css="--c:' + st[1] + '">' + st[0] + '</span></td><td class="num">' + a.runs24h + (a.bad24h ? ' (' + a.bad24h + ' bad)' : '') + '</td><td class="num">' + Math.round(a.minutes24h) + '</td></tr>'; }).join('');
    $('#aitable tbody').querySelectorAll('tr.row').forEach(tr => tr.onclick = () => openAI(tr.dataset.a));
    const down = DOCS.ai.agents.filter(a => !a.usable); $('#ainote').textContent = down.length ? down.map(a => a.name + ': ' + a.probe).join(' · ') + ' (replaced by another AI automatically)' : 'All AIs passed the last sign-in probe.';
  }
  function usageTable() {
    const rows = (DOCS.ai && DOCS.ai.usage) || [], cell = (r, w, k) => r[k + w] || 0;
    $('#usagetable tbody').innerHTML = rows.length ? rows.map(r => '<tr><td><b>' + esc(r.ai) + '</b> <span class="mono" data-css="color:var(--ink-2)">' + esc(r.role) + '</span></td>' + ['24h', '7d', '30d'].map(w => '<td class="num">' + cell(r, w, 'runs') + '</td><td class="num">' + cell(r, w, 'minutes').toFixed(1) + '</td><td class="num">' + (cell(r, w, 'bad') ? '<b data-css="color:var(--blocked)">' + cell(r, w, 'bad') + '</b>' : 0) + '</td>').join('') + '</tr>').join('') : '<tr><td colspan="10" class="empty">No AI run recorded yet.</td></tr>';
  }
  function gauge(l, v, mx, u, d) { const p = Math.min(100, v / mx * 100); return '<div class="gauge"><div class="h"><span>' + l + '</span><b>' + v.toFixed(d == null ? 1 : d) + ' ' + u + '</b></div><div class="meter"><i data-css="width:' + p + '%;background:' + (p > 80 ? 'var(--blocked)' : p > 55 ? 'var(--wait)' : 'var(--accent)') + '"></i></div></div>'; }
  function machine() {
    const m = DOCS.machine; $('#machine').textContent = m.host + ' · ' + m.cpus + ' CPUs · ' + (m.mem ? m.mem.total.toFixed(0) : '–') + ' GB';
    if (!m.load) { $('#gauges').innerHTML = '<div class="empty">The worker machine did not answer in the last collection.</div>'; return; }
    const used = m.mem.total - m.mem.avail;
    $('#gauges').innerHTML = gauge('Load average (1 min)', m.load[0], m.cpus, '') + gauge('Load average (15 min)', m.load[2], m.cpus, '') + gauge('Memory used', used, m.mem.total, 'GB') + gauge('Disk used', m.disk.used, m.disk.total, 'GB', 0);
    const hist = m.history || []; if (hist.length > 2) { const n = hist.length; const pts = (key, mx) => hist.map((q, i) => (i / (n - 1) * 300).toFixed(1) + ',' + (52 - Math.min(1, q[key] / mx) * 48).toFixed(1)).join(' '); $('#spark').innerHTML = '<polyline fill="none" stroke="var(--accent)" stroke-width="1.6" points="' + pts('load', m.cpus) + '"/><polyline fill="none" stroke="var(--wait)" stroke-width="1.6" points="' + pts('mem', m.mem.total) + '"/><text x="4" y="10" data-css="font:10px var(--f-mono);fill:var(--accent)">load</text><text x="40" y="10" data-css="font:10px var(--f-mono);fill:var(--wait)">memory</text>'; } else $('#spark').innerHTML = '<text x="4" y="30" data-css="font:10px var(--f-mono);fill:var(--ink-2)">history builds up with every update</text>';
    const hb = m.heartbeatAges || {}; const svc = m.services || {}; const rd = m.redis || {};
    $('#mkv').innerHTML = '<dt>Workers</dt><dd>' + Object.entries(svc).map(([k, v]) => k.replace('aiflow-', '') + ' ' + v).join(' · ') + '</dd><dt>Heartbeats</dt><dd>' + (Object.entries(hb).map(([k, v]) => k.replace('hb:', '') + ' ' + v + ' s ago').join(' · ') || '–') + '</dd><dt>State store</dt><dd>' + (rd.error ? 'error' : rd.ops + ' cmd/s · ' + rd.mem) + '</dd><dt>Shared containers</dt><dd>' + m.containers + ' running</dd><dt>Flow code</dt><dd>' + esc(m.code) + '</dd><dt>Uptime</dt><dd>' + m.uptimeH + ' h</dd><dt>Latest CI runs</dt><dd>' + (m.runs || []).slice(0, 4).map(r => '<a href="' + r.url + '" target="_blank" rel="noopener noreferrer">' + esc(r.name) + ' ' + r.status + ' ' + r.mins + 'm</a>').join(' · ') + '</dd>';
  }
  function feed() {
    const ev = DOCS.events.list.filter(e =>
      (filter === 'all' || (filter === 'run' ? (e.kind === 'run' || e.kind === 'badrun') : e.kind === filter)) &&
      (projectFilter === 'all' || e.project === projectFilter)
    );
    $('#feed').innerHTML = ev.length
      ? ev.map(e => CRLib.renderEvent(e, clock(e.at))).join('')
      : '<div class="empty">Nothing in the last 24 hours for these filters.</div>';
    $('#feed').querySelectorAll('[data-target-type]').forEach(b => {
      b.onclick = () => openTarget(b.dataset.targetType, b.dataset.targetKey);
    });
  }
  function eventFilters() {
    const projects = new Map(P().map(p => [p.key, p.name || p.key]));
    (DOCS.events.list || []).forEach(e => { if (e.project && !projects.has(e.project)) projects.set(e.project, e.project); });
    const select = $('#event-project');
    const options = [...projects.entries()].sort(([a], [b]) => a.localeCompare(b));
    if (projectFilter !== 'all' && !projects.has(projectFilter)) projectFilter = 'all';
    select.innerHTML = '<option value="all">All projects</option>' + options.map(([key, name]) => '<option value="' + esc(key) + '">' + esc(name) + ' · ' + esc(key) + '</option>').join('');
    select.value = projectFilter;
    [...$('#chips').children].forEach(b => b.setAttribute('aria-pressed', b.dataset.f === filter ? 'true' : 'false'));
  }
  $('#chips').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    filter = b.dataset.f; eventFilters(); feed();
  });
  $('#event-project').addEventListener('change', e => { projectFilter = e.target.value; feed(); });

  /* ---------- drawer ---------- */
  const links = a => '<div class="links">' + a.map(l => '<a href="' + l[1] + '" target="_blank" rel="noopener noreferrer">' + l[0] + '</a>').join('') + '</div>';
  function openDrawer() {
    const drawer = $('#drawer');
    if (!drawer.classList.contains('open')) drawerOpener = document.activeElement;
    drawer.removeAttribute('inert'); drawer.classList.add('open'); drawer.setAttribute('aria-hidden', 'false'); $('#scrim').classList.add('open');
    drawer.querySelector('.x').focus();
  }
  function closeDrawer() {
    const drawer = $('#drawer');
    sel = null; drawer.classList.remove('open'); drawer.setAttribute('aria-hidden', 'true'); drawer.setAttribute('inert', ''); $('#scrim').classList.remove('open');
    const target = drawerOpener && drawerOpener.isConnected ? drawerOpener : $('#event-project');
    if (target) target.focus();
    drawerOpener = null;
  }
  $('#scrim').onclick = closeDrawer;
  document.addEventListener('keydown', e => {
    const drawer = $('#drawer');
    if (!sel || !drawer.classList.contains('open')) return;
    if (e.key === 'Escape') { closeDrawer(); return; }
    if (e.key !== 'Tab') return;
    const focusable = [...drawer.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')];
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (!first) { e.preventDefault(); drawer.focus(); }
    else if (!drawer.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  const openTicket = k => { sel = { t: 'ticket', k }; renderDrawer(); openDrawer(); }, openProject = k => { sel = { t: 'project', k }; renderDrawer(); openDrawer(); }, openAI = k => { sel = { t: 'ai', k }; renderDrawer(); openDrawer(); };
  function openTarget(type, key) {
    if (!key) return;
    if (type === 'project') openProject(key);
    else if (type === 'ai') openAI(key);
    else openTicket(key);
  }
  function ticketInfo(k) {
    const pm = pmap(), pk = k.split('-')[0], p = pm[pk]; let t = DOCS.flow.inflight.find(x => x.key === k);
    const act = p && p.active.find(x => x.key === k); const runs = DOCS.timeline.runs.filter(r => r.key === k); const evs = DOCS.events.list.filter(e => e.key === k);
    return { p, t, act, runs, evs };
  }
  function renderDrawer() {
    const d = $('#drawer'); if (!sel) return; let h = '<button class="x" aria-label="Close">×</button>';
    if (sel.t === 'ticket') {
      const { p, t, act, runs, evs } = ticketInfo(sel.k); const status = t ? t.status : act ? act.status : null; const summary = t ? t.summary : act ? act.summary : (evs[0] ? evs[0].text : '');
      h += '<h3>' + sel.k + '</h3><div class="sub">' + esc(t ? t.summary : act ? act.summary : 'Details of this ticket are in Jira') + '</div>' + (status ? '<span class="pill" data-css="--c:' + (STATUS_COLOR[status] || 'var(--line)') + '">' + status + '</span>' : '<span class="pill" data-css="--c:var(--done)">Done or not in the active list</span>');
      h += '<dl class="kv">' + (p ? '<dt>Project</dt><dd>' + esc(p.name) + '</dd>' : '') + (t ? '<dt>Priority</dt><dd>' + t.priority + '</dd><dt>Agent field</dt><dd>' + esc(t.agent || 'not set') + '</dd><dt>Developer AI</dt><dd>' + esc(t.devAi || '–') + '</dd><dt>Reviewer AI</dt><dd>' + esc(t.reviewerAi || '–') + '</dd><dt>Worker</dt><dd>' + esc(t.lease || 'none holds it now') + '</dd><dt>Labels</dt><dd>' + esc((t.labels || []).join(', ') || 'none') + '</dd><dt>Updated</dt><dd>' + esc(t.updated.replace('T', ' ')) + '</dd>' : '') + '</dl>';
      const lk = [['Open in Jira', L.ticket(sel.k)]]; if (t && t.pr) lk.push(['Pull request #' + t.pr.number + (t.pr.draft ? ' (draft)' : ''), t.pr.url]); if (p) { lk.push(['Pull requests', L.pulls(p, sel.k)], ['CI runs', L.actions(p)], ['Repository', L.repo(p)]); } h += links(lk);
      if (runs.length) h += '<h5>AI runs (last 6 hours)</h5><ul class="steps">' + runs.map(r => '<li data-css="--c:' + (ROLE_COLOR[r.role] || 'var(--run)') + '"><span class="mono">' + clock(r.end * 1000) + '</span><span><b>' + r.ai + '</b> · ' + r.role + ' · ' + r.minutes + ' min' + (r.ok ? '' : ' · ended badly') + '</span></li>').join('') + '</ul>';
      if (evs.length) h += '<h5>Events (24 hours)</h5><ul class="steps">' + evs.map(e => '<li data-css="--c:' + (STATUS_COLOR[e.text.split(' → ')[1]] || 'var(--ink-2)') + '"><span class="mono">' + clock(e.at) + '</span><span>' + esc(e.text) + '</span></li>').join('') + '</ul>';
    } else if (sel.t === 'project') {
      const p = pmap()[sel.k];
      if (!p) {
        h += '<h3 id="drawer-title">' + esc(sel.k) + '</h3><div class="sub">Project details are unavailable in the current snapshot.</div>';
        d.innerHTML = h; d.querySelector('.x').onclick = closeDrawer; return;
      }
      const c = p.counts, declined = c.Declined || 0, tot = p.total - declined, done = c.Done || 0;
      h += '<h3>' + esc(p.name) + '</h3><div class="sub">' + p.key + ' · repository ' + p.repo + (p.onFlow ? ' · on the flow' : ' · not on the flow') + '</div><div class="meter"><i data-css="width:' + done / Math.max(1, tot) * 100 + '%"></i></div><b>' + Math.round(done / Math.max(1, tot) * 100) + '%</b> of ' + tot + ' tickets done';
      h += '<dl class="kv"><dt>Jira now</dt><dd>' + esc(Object.entries(c).map(([k, v]) => k + ' ' + v).join(' · ')) + '</dd><dt>Done, 24 h</dt><dd>' + p.doneLast24h + '</dd><dt>Estimated finish</dt><dd>' + esc(etaText(p, p.onFlow)) + '</dd><dt>Last Jira update</dt><dd>' + esc((p.lastUpdate || '').replace('T', ' ')) + '</dd></dl>';
      h += links([['Board', L.board(p)], ['Open issues', L.issues(p)], ['Repository', L.repo(p)], ['Pull requests', L.pulls(p)], ['CI runs', L.actions(p)]]);
      if (p.epics.length) h += '<h5>Epics</h5>' + p.epics.map(e2 => '<div data-css="margin-bottom:6px"><a href="' + L.ticket(e2.key) + '" target="_blank" rel="noopener noreferrer" class="mono">' + e2.key + '</a> ' + esc(e2.summary) + ' <span class="mono" data-css="color:var(--ink-2)">' + e2.status + '</span><div class="meter"><i data-css="width:' + e2.done / Math.max(1, e2.children) * 100 + '%"></i></div><small>Estimated finish: ' + esc(etaText(e2, p.onFlow)) + '</small></div>').join('');
      if ((p.etaExcluded || []).length) h += '<h5>Excluded from ETA</h5><div class="lst">' + p.etaExcluded.map(t2 => '<a data-css="display:block;padding:3px 6px;font-size:12.5px" href="' + L.ticket(t2.key) + '" target="_blank" rel="noopener noreferrer"><span class="mono">' + esc(t2.key) + '</span> ' + esc(t2.summary) + ' <span class="pill" data-css="--c:' + (STATUS_COLOR[t2.status] || 'var(--line)') + '">' + esc(t2.status) + '</span></a>').join('') + '</div>';
      if (p.mergedPRs.length) h += '<h5>Recently merged pull requests</h5><div class="lst">' + p.mergedPRs.map(m => '<a data-css="display:block;padding:3px 6px;font-size:12.5px" href="' + m.url + '" target="_blank" rel="noopener noreferrer">#' + m.number + ' ' + esc(m.title) + '</a>').join('') + '</div>';
      if (p.active.length) h += '<h5>Open tickets (most recent)</h5><div class="lst">' + p.active.slice(0, 14).map(t2 => '<button data-k="' + t2.key + '"><span class="mono">' + t2.key + '</span><span>' + esc(t2.summary.slice(0, 44)) + '</span><span class="pill" data-css="--c:' + (STATUS_COLOR[t2.status] || 'var(--line)') + '">' + t2.status + '</span></button>').join('') + '</div>';
    } else {
      const a = DOCS.ai.agents.find(x => x.id === sel.k || x.name === sel.k);
      if (!a) {
        h += '<h3 id="drawer-title">' + esc(sel.k) + '</h3><div class="sub">This AI is not in the current agent list.</div>';
        d.innerHTML = h; d.querySelector('.x').onclick = closeDrawer; return;
      }
      h += '<h3>' + a.name + '</h3><div class="sub">' + esc(a.model) + (a.effort !== 'default' ? ' · reasoning ' + a.effort : '') + '</div><span class="pill" data-css="--c:' + (a.usable ? 'var(--done)' : 'var(--blocked)') + '">' + (a.usable ? 'ready' : 'out of the pool') + '</span>';
      h += '<dl class="kv"><dt>Used for</dt><dd>' + esc(a.roles) + '</dd><dt>Sign-in probe</dt><dd>' + esc(a.probe) + '</dd><dt>Runs, 24 h</dt><dd>' + a.runs24h + ' (' + a.bad24h + ' ended badly)</dd><dt>Minutes, 24 h</dt><dd>' + a.minutes24h + '</dd><dt>Runs, since logging began</dt><dd>' + (a.runsAll == null ? 'not recorded' : a.runsAll + ' · ' + a.minutesAll + ' min · ' + a.badAll + ' ended badly') + '</dd></dl>';
      const v = DOCS.ai.versions; const mine = Object.values(v)[0]; if (mine && mine[a.id]) h += '<p class="mono" data-css="color:var(--ink-2)">CLI version ' + esc(mine[a.id]) + '</p>';
    }
    d.innerHTML = h;
    const title = d.querySelector('h3'); if (title) title.id = 'drawer-title';
    d.querySelector('.x').onclick = closeDrawer;
    d.querySelectorAll('button[data-k]').forEach(b => b.onclick = () => openTicket(b.dataset.k));
  }

  $('#theme').onclick = () => { const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'); document.documentElement.dataset.theme = cur === 'dark' ? 'light' : 'dark'; };
  function render() {
    [liveBadge, sources, kpis, timeline, funnel, projectRows, aiTable, usageTable, machine, eventFilters, feed].forEach(f => { try { f(); } catch (e) { console.error(f.name, e); } });
    if (sel) { try { const keep = $('#drawer').scrollTop; renderDrawer(); $('#drawer').scrollTop = keep; } catch (e) { console.error('drawer', e); } }
  }
  liveBadge();

  /* ---------- data: /api/snapshot, then /api/stream, polling when the stream is down ---------- */
  new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => n.nodeType === 1 && CRLib.applyCss(n)))).observe(document.body, { childList: true, subtree: true });
  CRLib.createFeed({
    fetchSnapshot: async () => { const r = await fetch(CRLib.SNAPSHOT_URL, { cache: 'no-store' }); if (!r.ok) throw new Error('snapshot ' + r.status); return r.json(); },
    openStream: () => new EventSource(CRLib.STREAM_URL),
    onSnapshot: snap => { DOCS = snap; render(); },
    onStatus: () => liveBadge()
  }).start();
})();
