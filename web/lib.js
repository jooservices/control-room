/* Pure helpers of the Flow Control Room page. No access to the page state, so they can be read and tested on their own. */
const CRLib = (function () {
  const SNAPSHOT_URL = '/api/snapshot', STREAM_URL = '/api/stream', POLL_MS = 30000;
  const STALE_AFTER_S = 180, OLD_AFTER_S = 900; // amber after 3 minutes, red after 15

  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmtMin = m => m == null ? '–' : m < 1 ? '<1 min' : m < 60 ? Math.round(m) + ' min' : (m / 60).toFixed(1) + ' h';
  const ago = seconds => { const s = Math.max(0, seconds); return s < 90 ? Math.round(s) + ' s' : s < 5400 ? Math.round(s / 60) + ' min' : s < 172800 ? (s / 3600).toFixed(1) + ' h' : Math.round(s / 86400) + ' d'; };

  /* The 'updated N seconds ago' badge, from meta.asOf. level is '' (live), 'stale' (amber) or 'old' (red). */
  function badge(asOfIso, nowMs) {
    const t = asOfIso ? new Date(asOfIso).getTime() : NaN;
    if (isNaN(t)) return { level: 'old', text: 'Waiting for the first snapshot' };
    const age = Math.max(0, (nowMs - t) / 1000);
    const level = age > OLD_AFTER_S ? 'old' : age > STALE_AFTER_S ? 'stale' : '';
    return { level, age, text: (level ? 'Last update ' : 'Live · updated ') + ago(age) + ' ago · Jira, GitHub, worker machine' };
  }

  /* The server's Content-Security-Policy forbids inline style attributes, so generated markup carries
     data-css="--c:red;width:40%" and applyCss turns it into CSSOM calls, which the policy allows. */
  function parseCss(text) {
    return String(text || '').split(';').map(d => { const i = d.indexOf(':'); return i < 0 ? null : [d.slice(0, i).trim(), d.slice(i + 1).trim()]; }).filter(p => p && p[0]);
  }
  function applyCss(root) {
    const nodes = root.querySelectorAll ? [...root.querySelectorAll('[data-css]')] : [];
    if (root.dataset && root.dataset.css) nodes.push(root);
    nodes.forEach(n => { parseCss(n.dataset.css).forEach(([k, v]) => n.style.setProperty(k, v)); n.removeAttribute('data-css'); });
  }

  /* A snapshot has every section the page reads. */
  const SECTIONS = ['meta', 'flow', 'projects', 'ai', 'timeline', 'events', 'machine'];
  const isSnapshot = s => !!s && typeof s === 'object' && SECTIONS.every(k => s[k] && typeof s[k] === 'object');

  /* Loads the snapshot once, then follows the stream. When the stream fails or does not exist, it polls the
     snapshot every pollMs and tries the stream again after each poll, so the page recovers by itself.
     A stream message that carries a whole snapshot is used as is; any other message means 'changed, fetch it'. */
  function createFeed(o) {
    const pollMs = o.pollMs || POLL_MS; let stream = null, timer = null, stopped = false;
    async function load() {
      try { const s = await o.fetchSnapshot(); if (isSnapshot(s)) { o.onSnapshot(s); o.onStatus('ok'); } else o.onStatus('empty'); }
      catch (e) { o.onStatus('error'); }
    }
    function closeStream() { if (stream) { stream.onopen = stream.onmessage = stream.onerror = null; try { stream.close(); } catch (e) { /* already closed */ } stream = null; } }
    function schedule() { if (!stopped && !timer) timer = setTimeout(async () => { timer = null; await load(); connect(); }, pollMs); }
    function onMessage(ev) {
      let data = null; try { data = JSON.parse(ev.data); } catch (e) { data = null; }
      if (isSnapshot(data)) { o.onSnapshot(data); o.onStatus('ok'); } else load();
    }
    function connect() {
      if (stopped || stream) return;
      try { stream = o.openStream(); } catch (e) { stream = null; schedule(); return; }
      stream.onopen = () => o.onStatus('stream');
      stream.onmessage = onMessage;
      if (stream.addEventListener) stream.addEventListener('snapshot', onMessage);
      stream.onerror = () => { closeStream(); o.onStatus('polling'); schedule(); };
    }
    return {
      async start() { await load(); connect(); },
      stop() { stopped = true; closeStream(); clearTimeout(timer); timer = null; }
    };
  }

  return { SNAPSHOT_URL, STREAM_URL, POLL_MS, STALE_AFTER_S, OLD_AFTER_S, esc, fmtMin, ago, badge, parseCss, applyCss, isSnapshot, createFeed };
})();
