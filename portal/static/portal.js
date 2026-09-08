/* Client Portal — the dashboard. Data comes from three endpoints on this
   origin (/api/meta, /api/daily, /api/trend); this file only draws.
   No framework, no build step.

   It is one screen at a time: the URL hash names the view (#today,
   #category/<slug>, #growth, #feed), show() hides every other <section
   class="view">, and a view is drawn the first time it is opened for the
   current report day — never all four at once. */
window.Portal = (function () {
  'use strict';

  /* ---------------- helpers ---------------- */
  const $ = (id) => document.getElementById(id);
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const DOW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const DOWL = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
  const MONL = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const parseISO = (s) => { const [y,m,d] = s.split('-').map(Number); return new Date(y, m-1, d); };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate()+n); return x; };
  const fmtD = (d) => `${d.getDate()} ${MON[d.getMonth()]}`;
  const fmtDL = (d) => `${DOW[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]} ${d.getFullYear()}`;
  const fmtN = (n) => n == null ? '—' : Number(n).toLocaleString('en-US');
  const fmtC = (n) => { if (n == null) return '—'; const a = Math.abs(n); if (a >= 1e6) return (n/1e6).toFixed(a >= 1e7 ? 0 : 1)+'M'; if (a >= 1e4) return Math.round(n/1e3)+'K'; if (a >= 1e3) return (n/1e3).toFixed(1)+'K'; return String(Math.round(n)); };
  const esc = (s) => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  const eng = (p) => (p.likes||0)+(p.comments||0)+(p.shares||0);
  const sum = (arr, f) => { let s = 0; for (const x of arr) { const v = f(x); if (v != null) s += v; } return s; };
  const pct = (a, b) => (!b ? null : (a-b)/b*100);
  const deltaHtml = (d) => d == null ? '<b class="flat">—</b>' : `<b class="${d >= 0 ? 'up' : 'down'}">${d >= 0 ? '▲' : '▼'} ${Math.abs(d).toFixed(0)}%</b>`;
  const ago = (isoTs, ref) => { if (!isoTs) return ''; const t = new Date(isoTs); if (isNaN(t)) return ''; const h = Math.max(0, Math.round((ref - t)/36e5)); return h < 1 ? 'just now' : h < 48 ? `${h}h ago` : `${Math.round(h/24)}d ago`; };
  const initials = (s) => (s||'?').replace(/^@/,'').split(/[\s._-]+/).filter(Boolean).slice(0,2).map(w => w[0].toUpperCase()).join('') || '?';
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || getComputedStyle(document.body).getPropertyValue(n).trim();
  const eachDay = (a, b) => { const out = []; for (let d = new Date(a); d <= b; d = addDays(d, 1)) out.push(iso(d)); return out; };
  const slugify = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  function niceTicks(max, n = 4) { if (max <= 0) return [0]; const raw = max/n, mag = Math.pow(10, Math.floor(Math.log10(raw))); const step = [1,2,2.5,5,10].map(m => m*mag).find(s => raw <= s); const out = []; for (let v = 0; v <= max+1e-9; v += step) out.push(+v.toFixed(6)); if (out[out.length-1] < max) out.push(out[out.length-1]+step); return out; }
  let _uid = 0; const uid = () => 'u' + (++_uid).toString(36);

  const PLATS = { facebook:{id:'facebook',name:'Facebook',pill:'FB',cls:'fb',v:'--fb'}, instagram:{id:'instagram',name:'Instagram',pill:'IG',cls:'ig',v:'--ig'}, x:{id:'x',name:'X (Twitter)',pill:'X',cls:'x',v:'--x'} };
  const PLAT_ORDER = ['facebook','instagram','x'];
  const CAT_COLORS = ['--fb','--ig','--x','--c4','--fb','--ig','--x','--c4'];
  const METRIC_NAME = { engagement:'Engagement', likes:'Likes', comments:'Comments', shares:'Shares', views:'Views' };
  const mLabel = (pl, k) => k === 'engagement' ? 'Engagement' : (pl === 'x' ? ({comments:'Replies', shares:'Reposts'}[k] || METRIC_NAME[k]) : METRIC_NAME[k]);
  const NOT_PUBLIC = { 'instagram.shares': 'Shares are not public on Instagram.' };
  const ICON = { heart:'<svg viewBox="0 0 16 16"><path d="M8 13.5S2.5 10 2.5 6.2A2.9 2.9 0 0 1 8 4.6a2.9 2.9 0 0 1 5.5 1.6C13.5 10 8 13.5 8 13.5z"/></svg>', repost:'<svg viewBox="0 0 16 16"><path d="M4 6V4.5A1.5 1.5 0 0 1 5.5 3H12M10 1l2 2-2 2M12 10v1.5A1.5 1.5 0 0 1 10.5 13H4M6 15l-2-2 2-2"/></svg>', comment:'<svg viewBox="0 0 16 16"><path d="M2.5 3.5h11v7h-6L4 13v-2.5H2.5z"/></svg>', eye:'<svg viewBox="0 0 16 16"><path d="M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/></svg>' };

  /* ---------------- state ---------------- */
  let META = null;
  let BASE = '';
  let CATS = [];                                  // META.categories + a url slug each
  const VIEWS = ['today', 'categories', 'growth', 'feed'];
  const state = { view:'today', cat:'', day:'', dur:'7', durFrom:null, durTo:null, metric:'engagement', gDur:'30', gMetric:'engagement', gSplit:'platform', feed:{cat:'',plat:'',sort:'eng',q:'',page:1} };
  const dirty = { today:true, categories:true, growth:true, feed:true };   // needs drawing for the current day
  const cache = { daily:new Map(), trend:new Map() };
  const catLabel = (raw) => { const c = CATS.find(x => x.raw === raw); return c ? c.label : (raw || 'Uncategorised'); };
  const catBySlug = (s) => CATS.find(c => c.slug === s);
  const catOf = (raw) => CATS.find(c => c.raw === raw);

  async function api(url) {
    const r = await fetch(BASE + url, { headers:{ 'Accept':'application/json' }, credentials:'same-origin' });
    if (r.status === 401) { window.location.href = BASE + '/login'; throw new Error('signed out'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
    return data;
  }
  async function daily(day) { if (!cache.daily.has(day)) cache.daily.set(day, api('/api/daily?day=' + encodeURIComponent(day))); return cache.daily.get(day); }
  async function trend(from, to) { const k = from + '..' + to; if (!cache.trend.has(k)) cache.trend.set(k, api(`/api/trend?from=${from}&to=${to}`)); return cache.trend.get(k); }

  /* Trend rows → per-day series for a filter */
  function seriesFrom(t, days, filter, metric) {
    const byDay = new Map(days.map(d => [d, 0]));
    const cnt = new Map(days.map(d => [d, 0]));
    for (const r of t.rows) { if (!filter(r)) continue; if (!byDay.has(r.day)) continue; byDay.set(r.day, byDay.get(r.day) + (r[metric] || 0)); cnt.set(r.day, cnt.get(r.day) + (r.posts || 0)); }
    return { values: days.map(d => byDay.get(d)), counts: days.map(d => cnt.get(d)) };
  }

  /* ---------------- tooltip ---------------- */
  const tip = document.createElement('div'); tip.className = 'tip'; document.body.appendChild(tip);
  const showTip = (html, x, y) => { tip.innerHTML = html; tip.classList.add('show'); tip.style.left = x+'px'; tip.style.top = (y-10)+'px'; };
  const hideTip = () => tip.classList.remove('show');
  document.addEventListener('mousemove', (e) => { const el = e.target.closest('[data-tip]'); if (!el) { if (!e.target.closest('.xh')) hideTip(); return; } showTip(el.dataset.tip, e.pageX, e.pageY); });

  /* ---------------- charts ---------------- */
  function areaChart(days, values, color, opts = {}) {
    const W = opts.w||400, H = opts.h||170, m = {t:16, r:46, b:24, l:40};
    const iw = W-m.l-m.r, ih = H-m.t-m.b, n = days.length;
    const max = Math.max(1, ...values); const ticks = niceTicks(max, 3); const top = ticks[ticks.length-1];
    const x = (i) => m.l + (n === 1 ? iw/2 : i*(iw/(n-1))), y = (v) => m.t + ih - (v/top)*ih;
    const id = uid(); const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.label||'Trend')}"><defs><linearGradient id="${id}g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".28"/><stop offset="1" stop-color="${color}" stop-opacity=".02"/></linearGradient></defs>`;
    s += `<g class="grid">${ticks.map(t => `<line x1="${m.l}" x2="${W-m.r}" y1="${y(t)}" y2="${y(t)}"/>`).join('')}</g>`;
    s += ticks.map(t => `<text x="${m.l-6}" y="${y(t)+3.5}" text-anchor="end" class="num">${fmtC(t)}</text>`).join('');
    const every = Math.max(1, Math.ceil(n/(W > 380 ? 7 : 5)));
    days.forEach((d, i) => { if (i === n-1 || (i % every === 0 && n-1-i >= every/2)) s += `<text x="${x(i)}" y="${H-7}" text-anchor="middle">${fmtD(parseISO(d))}</text>`; });
    s += `<path d="M${pts.join('L')}L${x(n-1)},${y(0)}L${x(0)},${y(0)}Z" fill="url(#${id}g)"/>`;
    s += `<path d="M${pts.join('L')}" fill="none" stroke="${color}" stroke-width="2.25" stroke-linejoin="round" stroke-linecap="round"/>`;
    const li = n-1, peak = values.indexOf(Math.max(...values));
    if (peak !== li && values[peak] > 0) s += `<circle cx="${x(peak)}" cy="${y(values[peak])}" r="3.5" fill="${color}" stroke="var(--surface)" stroke-width="2"/>`;
    s += `<circle cx="${x(li)}" cy="${y(values[li])}" r="4.5" fill="${color}" stroke="var(--surface)" stroke-width="2"/>`;
    s += `<text x="${x(li)+8}" y="${y(values[li])+3.5}" class="v">${fmtC(values[li])}</text>`;
    s += `<line class="base" x1="${m.l}" x2="${W-m.r}" y1="${y(0)}" y2="${y(0)}"/>`;
    s += `<line id="${id}x" x1="0" x2="0" y1="${m.t}" y2="${y(0)}" stroke="var(--axis)" stroke-width="1" style="display:none"/><circle id="${id}c" r="5" fill="${color}" stroke="var(--surface)" stroke-width="2" style="display:none"/>`;
    s += `<rect class="xh hit" x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" data-xh="${id}"/></svg>`;
    queueMicrotask(() => { const r = document.querySelector(`rect[data-xh="${id}"]`); if (!r) return; const svg = r.ownerSVGElement, xl = svg.querySelector('#'+id+'x'), c = svg.querySelector('#'+id+'c');
      r.addEventListener('mousemove', (e) => { const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY; const p = pt.matrixTransform(svg.getScreenCTM().inverse()); const i = Math.max(0, Math.min(n-1, Math.round((p.x-m.l)/(iw/(n-1||1)))));
        xl.setAttribute('x1', x(i)); xl.setAttribute('x2', x(i)); xl.style.display = ''; c.setAttribute('cx', x(i)); c.setAttribute('cy', y(values[i])); c.style.display = '';
        showTip(`<b>${fmtDL(parseISO(days[i]))}</b>${opts.tipRows ? opts.tipRows(i) : `<div class=row><span>${esc(opts.metricName||'Value')}</span><b>${fmtN(values[i])}</b></div>`}`, e.pageX, e.pageY); });
      r.addEventListener('mouseleave', () => { xl.style.display = 'none'; c.style.display = 'none'; hideTip(); }); });
    return s;
  }

  function multiLine(days, series, opts = {}) {
    const W = opts.w||1100, H = opts.h||300, m = {t:16, r:64, b:26, l:48}; const iw = W-m.l-m.r, ih = H-m.t-m.b, n = days.length;
    const max = Math.max(1, ...series.flatMap(s => s.values)); const ticks = niceTicks(max, 4); const top = ticks[ticks.length-1];
    const x = (i) => m.l + (n === 1 ? iw/2 : i*(iw/(n-1))), y = (v) => m.t + ih - (v/top)*ih; const id = uid();
    let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.label||'Growth')}">`;
    s += `<g class="grid">${ticks.map(t => `<line x1="${m.l}" x2="${W-m.r}" y1="${y(t)}" y2="${y(t)}"/>`).join('')}</g>`;
    s += ticks.map(t => `<text x="${m.l-6}" y="${y(t)+3.5}" text-anchor="end" class="num">${fmtC(t)}</text>`).join('');
    const every = Math.max(1, Math.ceil(n/12));
    days.forEach((d, i) => { if (i === n-1 || (i % every === 0 && n-1-i >= every/2)) s += `<text x="${x(i)}" y="${H-7}" text-anchor="middle">${fmtD(parseISO(d))}</text>`; });
    series.forEach(sr => { const pts = sr.values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
      if (series.length === 1) s += `<path d="M${pts.join('L')}L${x(n-1)},${y(0)}L${x(0)},${y(0)}Z" fill="${sr.color}" opacity=".10"/>`;
      s += `<path d="M${pts.join('L')}" fill="none" stroke="${sr.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
      s += `<circle cx="${x(n-1)}" cy="${y(sr.values[n-1])}" r="4" fill="${sr.color}" stroke="var(--surface)" stroke-width="2"/>`; });
    const ends = series.map((sr, k) => ({k, y:y(sr.values[n-1]), v:sr.values[n-1]})).sort((a, b) => a.y-b.y);
    for (let i = 1; i < ends.length; i++) if (ends[i].y-ends[i-1].y < 12) ends[i].y = ends[i-1].y+12;
    ends.forEach(e => { s += `<text x="${x(n-1)+9}" y="${e.y+3.5}" class="v">${fmtC(e.v)}</text>`; });
    s += `<line class="base" x1="${m.l}" x2="${W-m.r}" y1="${y(0)}" y2="${y(0)}"/>`;
    s += `<line id="${id}x" x1="0" x2="0" y1="${m.t}" y2="${y(0)}" stroke="var(--axis)" stroke-width="1" style="display:none"/>` + series.map((sr, k) => `<circle id="${id}c${k}" r="5" fill="${sr.color}" stroke="var(--surface)" stroke-width="2" style="display:none"/>`).join('');
    s += `<rect class="xh hit" x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" data-xh="${id}"/></svg>`;
    queueMicrotask(() => { const r = document.querySelector(`rect[data-xh="${id}"]`); if (!r) return; const svg = r.ownerSVGElement, xl = svg.querySelector('#'+id+'x'), cs = series.map((_, k) => svg.querySelector(`#${id}c${k}`));
      r.addEventListener('mousemove', (e) => { const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY; const p = pt.matrixTransform(svg.getScreenCTM().inverse()); const i = Math.max(0, Math.min(n-1, Math.round((p.x-m.l)/(iw/(n-1||1)))));
        xl.setAttribute('x1', x(i)); xl.setAttribute('x2', x(i)); xl.style.display = ''; series.forEach((sr, k) => { cs[k].setAttribute('cx', x(i)); cs[k].setAttribute('cy', y(sr.values[i])); cs[k].style.display = ''; });
        showTip(`<b>${fmtDL(parseISO(days[i]))}</b>` + series.map(sr => `<div class=row><span><i class="sw" style="background:${sr.color};vertical-align:-1px;margin-right:5px"></i>${esc(sr.name)}</span><b>${fmtN(sr.values[i])}</b></div>`).join(''), e.pageX, e.pageY); });
      r.addEventListener('mouseleave', () => { xl.style.display = 'none'; cs.forEach(c => c.style.display = 'none'); hideTip(); }); });
    return s;
  }

  function sparkline(values, color) { const W = 120, H = 36, n = values.length; const max = Math.max(1, ...values); const x = (i) => n === 1 ? W/2 : i*(W/(n-1)), y = (v) => H-4-(v/max)*(H-10); const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('L');
    return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><path d="M${pts}L${W},${H}L0,${H}Z" fill="${color}" opacity=".12"/><path d="M${pts}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/></svg>`; }

  /* ---------------- Today ---------------- */
  async function renderToday() {
    const d = parseISO(state.day);
    const [cur, prev, week] = await Promise.all([daily(state.day), daily(iso(addDays(d, -1))).catch(() => ({posts:[]})), trend(iso(addDays(d, -6)), state.day)]);
    const posts = cur.posts, pposts = prev.posts || [];
    $('heroDate').textContent = `${DOWL[d.getDay()]}, ${d.getDate()} ${MONL[d.getMonth()]} ${d.getFullYear()}`;
    $('heroLag').textContent = state.day === META.data_through ? `Latest available. Reports are published ${META.lag_days} days after posting, once the counts have settled.` : `Reports go up to ${fmtD(parseISO(META.data_through))} (${META.lag_days}-day delay). Use the day picker to move.`;
    const wdays = week.days.length ? week.days : [state.day];
    const accent = cssVar('--accent');
    const kp = [ {l:'Engagement', f:(a) => sum(a, eng), s:seriesFrom(week, wdays, () => true, 'engagement').values, t:'likes + comments + shares'},
                 {l:'Views', f:(a) => sum(a, p => p.views), s:seriesFrom(week, wdays, () => true, 'views').values},
                 {l:'Posts', f:(a) => a.length, s:seriesFrom(week, wdays, () => true, 'posts').counts} ];
    $('kpis').innerHTML = kp.map(k => { const v = k.f(posts), pv = k.f(pposts), dd = pposts.length ? pct(v, pv) : null;
      return `<div class="kpi"><div class="l" ${k.t ? `title="${k.t}"` : ''}>${k.l}</div><div class="v" title="${fmtN(v)}">${fmtC(v)}</div><div class="d">${deltaHtml(dd)}<span>vs ${fmtD(addDays(d, -1))}</span></div>${sparkline(k.s, accent)}</div>`; }).join('');
    const eTot = Math.max(1, sum(posts, eng));
    $('sharebar').innerHTML = PLAT_ORDER.map(pl => { const e = sum(posts.filter(p => p.platform === pl), eng); return e ? `<i class="${PLATS[pl].cls}" style="width:${(e/eTot*100).toFixed(1)}%" data-tip="<b>${PLATS[pl].name}</b><div class=row><span>Engagement</span><b>${fmtN(e)}</b></div><div class=row><span>Share</span><b>${Math.round(e/eTot*100)}%</b></div>"></i>` : ''; }).join('');
    $('platrows').innerHTML = PLAT_ORDER.map(pl => { const pp = posts.filter(p => p.platform === pl), pv = pposts.filter(p => p.platform === pl); const e = sum(pp, eng), pe = sum(pv, eng), dd = pv.length ? pct(e, pe) : null;
      return `<div class="platrow"><span class="pp ${PLATS[pl].cls}">${PLATS[pl].pill}</span><span class="n">${PLATS[pl].name}<small>${fmtN(pp.length)} post${pp.length === 1 ? '' : 's'} · ${fmtC(sum(pp, p => p.views))} views</small></span><span class="v">${fmtC(e)}<small>${Math.round(e/eTot*100)}% of engagement</small></span><span class="d ${dd == null ? 'flat' : dd >= 0 ? 'up' : 'down'}">${dd == null ? '—' : (dd >= 0 ? '▲' : '▼')+' '+Math.abs(dd).toFixed(0)+'%'}</span></div>`; }).join('');
    $('topgrid').innerHTML = PLAT_ORDER.map(pl => { const best = posts.filter(p => p.platform === pl).sort((a, b) => eng(b)-eng(a))[0];
      if (!best) return `<div class="tp"><div class="media">No ${PLATS[pl].name} posts</div><div class="body"><div class="who"><span class="pp ${PLATS[pl].cls}">${PLATS[pl].pill}</span><b>${PLATS[pl].name}</b></div><div class="cap dash">Nothing posted on ${fmtD(d)}.</div></div></div>`;
      const media = `<div class="media">${best.thumb ? `<img src="${esc(best.thumb)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : (best.media_type ? '' : '<span>No media</span>')}${best.media_type ? `<span class="kind">${best.media_type}</span>` : ''}${best.media_type === 'video' ? '<span class="play"></span>' : ''}<span class="rank">Top ${PLATS[pl].pill}</span></div>`;
      return `<a class="tp" href="${esc(best.url)}" target="_blank" rel="noopener">${media}<div class="body"><div class="who"><span class="ava">${best.avatar ? `<img src="${esc(best.avatar)}" alt="" referrerpolicy="no-referrer">` : initials(best.name)}</span><b>${esc(best.name)}</b><span class="h">${esc(best.handle)}</span><span class="pp ${PLATS[pl].cls}" style="margin-left:auto">${PLATS[pl].pill}</span></div><div class="cap">${esc(best.text) || '<span class="dash">No caption</span>'}</div><div class="catl">${esc(catLabel(best.category))}</div>
        <div class="stat"><span title="Engagement"><b>${fmtC(eng(best))}</b> engagement</span><span title="${mLabel(pl, 'likes')}">${ICON.heart}${fmtC(best.likes)}</span><span title="${mLabel(pl, 'comments')}">${ICON.comment}${fmtC(best.comments)}</span><span title="Views">${ICON.eye}${fmtC(best.views)}</span></div></div></a>`; }).join('');
    // one tile per category — the door into that category's screen
    $('catgrid').innerHTML = CATS.map(c => { const cp = posts.filter(p => p.category === c.raw), pv = pposts.filter(p => p.category === c.raw); const e = sum(cp, eng), pe = sum(pv, eng), dd = pv.length ? pct(e, pe) : null; const et = Math.max(1, e);
      const bar = PLAT_ORDER.map(pl => { const v = sum(cp.filter(p => p.platform === pl), eng); return v ? `<i class="${PLATS[pl].cls}" style="width:${(v/et*100).toFixed(1)}%" data-tip="<b>${PLATS[pl].name}</b><div class=row><span>Engagement</span><b>${fmtN(v)}</b></div>"></i>` : ''; }).join('');
      return `<a class="ct" href="#category/${c.slug}"><div class="n">${esc(c.label)}</div><div class="v">${fmtC(e)}<small>engagement</small></div><div class="s"><span><b>${fmtN(cp.length)}</b> post${cp.length === 1 ? '' : 's'}</span><span><b>${fmtC(sum(cp, p => p.views))}</b> views</span><span>${dd == null ? '' : deltaHtml(dd) + ' vs ' + fmtD(addDays(d, -1))}</span></div><div class="bar">${bar}</div><div class="go">Open trends →</div></a>`; }).join('') || '<div class="empty">No categories on this day.</div>';
  }

  /* ---------------- One category ---------------- */
  function durRange() {
    const end = parseISO(state.day); const lo = parseISO(META.data_from);
    let a, b = end;
    if (state.dur === 'custom') { a = state.durFrom ? parseISO(state.durFrom) : addDays(end, -6); b = state.durTo ? parseISO(state.durTo) : end; if (b > end) b = end; if (a > b) [a, b] = [b, a]; }
    else a = addDays(end, -(Number(state.dur)-1));
    if (a < lo) a = lo;
    return [a, b];
  }
  function markCat() {
    // the chips inside the category screen always show the chosen category; the sidebar only while that screen is open
    document.querySelectorAll('#navCats a, #catnav a').forEach(a => { const on = a.dataset.cat === state.cat && (state.view === 'categories' || a.closest('#catnav')); a.classList.toggle('on', !!on); if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
  }
  async function renderCat() {
    const c = catOf(state.cat) || CATS[0];
    if (!c) { $('catTitle').textContent = 'No categories yet'; $('cats').innerHTML = '<div class="empty">Nothing published for this client yet.</div>'; return; }
    state.cat = c.raw; markCat();
    $('catTitle').textContent = c.label;
    const [a, b] = durRange(); const days = eachDay(a, b); const n = days.length; const met = state.metric;
    const pa = addDays(a, -n), pb = addDays(a, -1);
    const [t, pt] = await Promise.all([trend(iso(a), iso(b)), pb >= parseISO(META.data_from) ? trend(iso(pa < parseISO(META.data_from) ? parseISO(META.data_from) : pa), iso(pb)) : Promise.resolve({days:[], rows:[]})]);
    const pdays = pt.days || [];
    $('durRange').textContent = `${fmtD(a)} – ${fmtD(b)} · ${n} day${n > 1 ? 's' : ''}`;
    $('durCustom').classList.toggle('show', state.dur === 'custom');
    const inCat = (r) => r.category === c.raw;
    const cur = seriesFrom(t, days, inCat, met), old = seriesFrom(pt, pdays, inCat, met);
    const tot = sum(cur.values, v => v), ptot = sum(old.values, v => v); const nposts = sum(cur.counts, v => v);
    const dd = pdays.length ? pct(tot/n, ptot/pdays.length) : null;
    const panels = PLAT_ORDER.map(pl => {
      const f = (r) => r.category === c.raw && r.platform === pl; const color = cssVar(PLATS[pl].v);
      const s = seriesFrom(t, days, f, met), so = seriesFrom(pt, pdays, f, met);
      const np = NOT_PUBLIC[`${pl}.${met}`]; const count = sum(s.counts, v => v);
      let body, head = '';
      if (!count) body = `<div class="empty">No ${PLATS[pl].name} posts in this category in this period.</div>`;
      else if (np) { body = `<div class="empty">${np}</div>`; head = `<div class="tot"><span class="v">—</span><span class="s">${fmtN(count)} posts</span></div>`; }
      else {
        const tt = sum(s.values, v => v), pt2 = sum(so.values, v => v); const d = pdays.length ? pct(tt/n, pt2/pdays.length) : null;
        const peak = Math.max(...s.values), pi = s.values.indexOf(peak);
        head = `<div class="tot"><span class="v" title="${fmtN(tt)}">${fmtC(tt)}</span><span class="d ${d == null ? 'flat' : d >= 0 ? 'up' : 'down'}">${d == null ? '' : (d >= 0 ? '▲' : '▼')+' '+Math.abs(d).toFixed(0)+'%'}</span><span class="s">${fmtN(count)} posts · avg ${fmtC(tt/count)} / post</span></div>`;
        body = areaChart(days, s.values, color, { w:400, h:190, label:`${c.label} · ${PLATS[pl].name} · ${mLabel(pl, met)} per day`, tipRows:(i) => `<div class=row><span>${mLabel(pl, met)}</span><b>${fmtN(s.values[i])}</b></div><div class=row><span>Posts</span><b>${s.counts[i]}</b></div>${s.counts[i] ? `<div class=row><span>Per post</span><b>${fmtN(Math.round(s.values[i]/s.counts[i]))}</b></div>` : ''}${i === pi && peak > 0 ? '<div class=row><span>Best day of the period</span></div>' : ''}` });
      }
      return `<div class="plat"><div class="ph"><span class="pp ${PLATS[pl].cls}">${PLATS[pl].pill}</span>${PLATS[pl].name}<span class="n">${mLabel(pl, met)} per day</span></div>${head}${body}</div>`;
    }).join('');
    $('cats').innerHTML = `<div class="cat"><div class="cat-head"><h3>${esc(c.label)}</h3><div class="stats"><span><b>${fmtN(nposts)}</b> posts</span><span><b title="${fmtN(tot)}">${fmtC(tot)}</b> ${METRIC_NAME[met].toLowerCase()}</span><span title="per-day average vs the ${pdays.length} days before">${dd == null ? '<span class="dash">no earlier period</span>' : `<b class="${dd >= 0 ? 'up' : 'down'}">${dd >= 0 ? '▲' : '▼'} ${Math.abs(dd).toFixed(0)}%</b> vs previous ${pdays.length}d`}</span></div></div><div class="plats">${panels}</div></div>`;
  }

  /* ---------------- Growth ---------------- */
  async function renderGrowth() {
    const n = Number(state.gDur), met = state.gMetric; const end = parseISO(state.day); const lo = parseISO(META.data_from);
    let a = addDays(end, -(n-1)); if (a < lo) a = lo; const days = eachDay(a, end);
    const pa = addDays(a, -days.length), pb = addDays(a, -1);
    const [t, pt] = await Promise.all([trend(iso(a), iso(end)), pb >= lo ? trend(iso(pa < lo ? lo : pa), iso(pb)) : Promise.resolve({days:[], rows:[]})]);
    const pdays = pt.days || [];
    const split = state.gSplit === 'platform' ? PLAT_ORDER.map(pl => ({key:pl, name:PLATS[pl].name, color:cssVar(PLATS[pl].v), f:(r) => r.platform === pl}))
                                              : CATS.map((c, i) => ({key:c.raw, name:c.label, color:cssVar(CAT_COLORS[i % CAT_COLORS.length]), f:(r) => r.category === c.raw}));
    const series = split.map(s => ({...s, ...seriesFrom(t, days, s.f, met), old:seriesFrom(pt, pdays, s.f, met)}));
    const tot = sum(seriesFrom(t, days, () => true, met).values, v => v), ptot = sum(seriesFrom(pt, pdays, () => true, met).values, v => v);
    const dd = pdays.length ? pct(tot/days.length, ptot/pdays.length) : null;
    $('gLabel').textContent = `${METRIC_NAME[met]} · ${fmtD(a)} – ${fmtD(end)} (${days.length} days)`;
    $('gBig').textContent = fmtC(tot); $('gBig').title = fmtN(tot);
    $('gDelta').innerHTML = dd == null ? `<span class="dash">No earlier period to compare — the data starts ${fmtD(lo)}.</span>` : `${deltaHtml(dd)} per day vs the ${pdays.length} days before (${fmtC(ptot)} total)`;
    $('gLegend').innerHTML = series.map(s => `<span><i class="sw line" style="background:${s.color}"></i>${esc(s.name)}</span>`).join('');
    $('gChart').innerHTML = multiLine(days, series, { w:1100, h:300, label:`${METRIC_NAME[met]} per day, ${state.gSplit === 'platform' ? 'by platform' : 'by category'}` });
    $('gTable').innerHTML = series.map(s => { const tt = sum(s.values, v => v), pt2 = sum(s.old.values, v => v); const d = pdays.length ? pct(tt/days.length, pt2/pdays.length) : null; const cnt = sum(s.counts, v => v);
      return `<div class="gi"><div class="n"><i class="sw" style="background:${s.color}"></i>${esc(s.name)}</div><div class="v" title="${fmtN(tt)}">${fmtC(tt)}<small class="${d == null ? 'flat' : d >= 0 ? 'up' : 'down'}">${d == null ? '' : (d >= 0 ? '▲' : '▼')+' '+Math.abs(d).toFixed(0)+'%'}</small></div><div class="s">${fmtN(cnt)} posts · ${fmtC(tt/days.length)} per day${cnt ? ` · ${fmtC(tt/cnt)} per post` : ''}</div></div>`; }).join('');
  }

  /* ---------------- Feed ---------------- */
  const PAGE = 20;
  let FEED = [];
  function feedRows() { const f = state.feed, q = f.q.trim().toLowerCase(); let rows = FEED.filter(p => (!f.cat || p.category === f.cat) && (!f.plat || p.platform === f.plat) && (!q || (p.name+' '+p.handle+' '+p.text).toLowerCase().includes(q)));
    if (f.sort === 'eng') rows.sort((a, b) => eng(b)-eng(a)); else if (f.sort === 'views') rows.sort((a, b) => (b.views||0)-(a.views||0)); else rows.sort((a, b) => (new Date(b.posted_at||0))-(new Date(a.posted_at||0))); return rows; }
  async function renderFeed() {
    const d = parseISO(state.day);
    $('feedTitle').textContent = `Posts on ${fmtD(d)}`;
    FEED = (await daily(state.day)).posts;
    const rows = feedRows(), f = state.feed; const pages = Math.max(1, Math.ceil(rows.length/PAGE)); if (f.page > pages) f.page = pages; const slice = rows.slice((f.page-1)*PAGE, f.page*PAGE);
    $('fCount').textContent = `${fmtN(rows.length)} post${rows.length === 1 ? '' : 's'}`;
    const now = new Date();
    $('cards').innerHTML = slice.map(p => {
      const when = [p.collected_at ? `collected ${ago(p.collected_at, now)}` : '', p.posted_at ? `posted ${ago(p.posted_at, now)}` : ''].filter(Boolean).join(' · ');
      const media = (p.media_type || p.thumb || p.screenshot) ? `<div class="media">${(p.thumb || p.screenshot) ? `<img src="${esc(p.thumb || p.screenshot)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ''}${p.media_type ? `<span class="kind">${p.media_type}</span>` : ''}${p.media_type === 'video' ? '<span class="play"></span>' : ''}</div>` : '';
      const open = {facebook:'Open on Facebook', instagram:'Open on Instagram', x:'Open on X'}[p.platform];
      return `<article class="post ${media ? '' : 'nomedia'}">
        <div class="who"><div class="ava">${p.avatar ? `<img src="${esc(p.avatar)}" alt="" referrerpolicy="no-referrer">` : initials(p.name)}</div><div><span class="name">${esc(p.name)}</span><span class="handle">${esc(p.handle)}</span> <span class="pp ${PLATS[p.platform].cls}">${PLATS[p.platform].pill}</span></div><span class="cat-chip">${esc(catLabel(p.category))}</span></div>
        ${media}
        <div class="body"><div class="text">${esc(p.text) || '<span class="dash">No caption</span>'}</div><div class="when">${when}</div>
          <div class="metrics"><span title="${mLabel(p.platform, 'likes')}">${ICON.heart}${fmtN(p.likes)}</span><span title="${mLabel(p.platform, 'shares')}">${ICON.repost}${p.shares == null ? '<span class=dash>—</span>' : fmtN(p.shares)}</span><span title="${mLabel(p.platform, 'comments')}">${ICON.comment}${fmtN(p.comments)}</span><span title="Views">${ICON.eye}${p.views == null ? '<span class=dash>—</span>' : fmtN(p.views)}</span></div></div>
        <div class="acts"><a class="btn sm" href="${esc(p.url)}" target="_blank" rel="noopener">${open}</a><button class="btn sm" type="button" data-copy="${esc(p.url)}">Copy link</button></div>
      </article>`; }).join('') || `<div class="empty">No posts match these filters on ${fmtD(d)}.</div>`;
    $('pager').innerHTML = pages > 1 ? `<span>Page ${f.page} of ${pages}</span><button class="btn sm" type="button" data-pg="-1" ${f.page <= 1 ? 'disabled' : ''}>Previous</button><button class="btn sm" type="button" data-pg="1" ${f.page >= pages ? 'disabled' : ''}>Next</button>` : '';
  }

  /* ---------------- views + routing ---------------- */
  const RENDER = { today:renderToday, categories:renderCat, growth:renderGrowth, feed:renderFeed };
  function showErr(e) { const b = $('banner'); b.hidden = false; $('bannerText').innerHTML = `<b>Could not load part of the report.</b> ${esc(e.message || e)}`; }
  function markNav() {
    document.querySelectorAll('#nav a[data-view], #strip a[data-view]').forEach(a => { const on = a.dataset.view === state.view; a.classList.toggle('on', on); if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
    markCat();
  }
  async function draw(view) {
    if (!dirty[view]) return;
    dirty[view] = false;
    try { await RENDER[view](); } catch (e) { dirty[view] = true; showErr(e); }
  }
  /* Show one view, hide the rest, draw it if it is stale. */
  function show(view, opts = {}) {
    if (!VIEWS.includes(view)) view = 'today';
    state.view = view;
    document.querySelectorAll('section.view').forEach(s => { s.hidden = s.id !== view; });
    markNav();
    if (!opts.keepScroll) window.scrollTo(0, 0);
    return draw(view);
  }
  function route() {
    const h = decodeURIComponent((location.hash || '#today').slice(1));
    const [head, rest] = h.split('/', 2);
    if (head === 'category' || head === 'categories') {
      const c = rest ? catBySlug(rest) : null;
      if (c && c.raw !== state.cat) { state.cat = c.raw; dirty.categories = true; }
      else if (!state.cat && CATS[0]) state.cat = CATS[0].raw;
      return show('categories');
    }
    return show(head);
  }
  const go = (hash) => { if (location.hash === hash) route(); else location.hash = hash; };
  /* Everything depends on the report day: the active view is redrawn now, the others when they are next opened. */
  function invalidateAll() { for (const v of VIEWS) dirty[v] = true; }
  function redraw(view) { dirty[view] = true; if (state.view === view) draw(view); }

  /* ---------------- reports (download the built report for the day) ---------------- */
  async function renderReports() {
    const el = $('reportDl'); if (!el) return;
    if (!META.show_reports || !state.day) { el.hidden = true; el.innerHTML = ''; return; }
    try {
      const r = await api('/api/reports?day=' + encodeURIComponent(state.day));
      const reps = r.reports || [];
      if (!reps.length) { el.hidden = true; el.innerHTML = ''; return; }
      el.innerHTML = '<span class="repdl-l">Report</span>' + reps.map(x =>
        `<a class="btn sm" href="${BASE}/report/${encodeURIComponent(x.id)}" title="Download the ${esc(x.name)} report for this day">${esc(x.name)}</a>`).join('');
      el.hidden = false;
    } catch (e) { el.hidden = true; el.innerHTML = ''; }
  }

  /* ---------------- day + export ---------------- */
  function exportHref() { const [a, b] = durRange(); const g = Number(state.gDur); const end = parseISO(state.day); return `${BASE}/api/export.xlsx?day=${state.day}&from=${iso(a)}&to=${iso(b)}&gfrom=${iso(addDays(end, -(g-1)))}&gto=${state.day}&metric=${state.gMetric}&split=${state.gSplit}`; }
  function setDay(s) {
    const lo = META.data_from, hi = META.data_through; if (s < lo) s = lo; if (s > hi) s = hi;
    state.day = s; const dp = $('dayPick'); dp.value = s; dp.min = lo; dp.max = hi; $('prevDay').disabled = s <= lo; $('nextDay').disabled = s >= hi; state.feed.page = 1;
    $('lagNote').textContent = s === hi ? `latest · ${META.lag_days}-day delay` : `latest is ${fmtD(parseISO(hi))}`;
    $('exportBtn').href = exportHref();
    renderReports().catch(() => {});
    invalidateAll(); draw(state.view);
  }

  function buildNav() {
    const item = (c, cls) => `<a href="#category/${c.slug}" class="${cls}" data-cat="${esc(c.raw)}"><span>${esc(c.label)}</span></a>`;
    $('navCats').innerHTML = CATS.map(c => item(c, 'sub')).join('') || '<div class="nav-empty">None yet</div>';
    $('catnav').innerHTML = CATS.map(c => item(c, '')).join('');
    $('fCat').innerHTML = '<option value="">All categories</option>' + CATS.map(c => `<option value="${esc(c.raw)}">${esc(c.label)}</option>`).join('');
  }

  function wire() {
    window.addEventListener('hashchange', route);
    $('dayPick').addEventListener('change', () => { if ($('dayPick').value) setDay($('dayPick').value); });
    $('prevDay').addEventListener('click', () => setDay(iso(addDays(parseISO(state.day), -1))));
    $('nextDay').addEventListener('click', () => setDay(iso(addDays(parseISO(state.day), 1))));
    // category screen
    $('durSeg').addEventListener('click', (e) => { const b = e.target.closest('button[data-d]'); if (!b) return; state.dur = b.dataset.d; document.querySelectorAll('#durSeg button').forEach(x => x.classList.toggle('on', x === b));
      if (state.dur === 'custom') { const f = $('durFrom'), t = $('durTo'); f.min = t.min = META.data_from; f.max = t.max = state.day; f.value = state.durFrom || iso(addDays(parseISO(state.day), -6)); t.value = state.durTo || state.day; state.durFrom = f.value; state.durTo = t.value; }
      $('exportBtn').href = exportHref(); redraw('categories'); });
    $('durFrom').addEventListener('change', (e) => { if (e.target.value) { state.durFrom = e.target.value; $('exportBtn').href = exportHref(); redraw('categories'); } });
    $('durTo').addEventListener('change', (e) => { if (e.target.value) { state.durTo = e.target.value; $('exportBtn').href = exportHref(); redraw('categories'); } });
    $('metricSel').addEventListener('change', (e) => { state.metric = e.target.value; redraw('categories'); });
    $('catPosts').addEventListener('click', () => { state.feed.cat = state.cat; state.feed.page = 1; $('fCat').value = state.cat; dirty.feed = true; go('#feed'); });
    // growth
    $('gDurSeg').addEventListener('click', (e) => { const b = e.target.closest('button[data-d]'); if (!b) return; state.gDur = b.dataset.d; document.querySelectorAll('#gDurSeg button').forEach(x => x.classList.toggle('on', x === b)); $('exportBtn').href = exportHref(); redraw('growth'); });
    $('gSplit').addEventListener('click', (e) => { const b = e.target.closest('button[data-s]'); if (!b) return; state.gSplit = b.dataset.s; document.querySelectorAll('#gSplit button').forEach(x => x.classList.toggle('on', x === b)); $('exportBtn').href = exportHref(); redraw('growth'); });
    $('gMetric').addEventListener('change', (e) => { state.gMetric = e.target.value; $('exportBtn').href = exportHref(); redraw('growth'); });
    // feed
    $('cards').addEventListener('click', async (e) => { const b = e.target.closest('button[data-copy]'); if (!b) return; try { await navigator.clipboard.writeText(b.dataset.copy); const t = b.textContent; b.textContent = 'Copied'; setTimeout(() => b.textContent = t, 1200); } catch (err) { b.textContent = 'Copy failed'; } });
    $('pager').addEventListener('click', (e) => { const b = e.target.closest('button[data-pg]'); if (!b) return; state.feed.page += Number(b.dataset.pg); redraw('feed'); window.scrollTo({ top:0 }); });
    $('fCat').addEventListener('change', (e) => { state.feed.cat = e.target.value; state.feed.page = 1; redraw('feed'); });
    $('fPlat').addEventListener('change', (e) => { state.feed.plat = e.target.value; state.feed.page = 1; redraw('feed'); });
    $('fSort').addEventListener('change', (e) => { state.feed.sort = e.target.value; state.feed.page = 1; redraw('feed'); });
    $('fQ').addEventListener('input', (e) => { state.feed.q = e.target.value; state.feed.page = 1; redraw('feed'); });
    const mq = window.matchMedia('(prefers-color-scheme: dark)'); if (mq.addEventListener) mq.addEventListener('change', () => { invalidateAll(); draw(state.view); });
  }

  function boot() {
    META = JSON.parse($('meta').textContent);
    BASE = META.base || '';
    const seen = new Set();
    CATS = (META.categories || []).map((c, i) => { let s = slugify(c.label) || slugify(c.raw) || `cat-${i+1}`; while (seen.has(s)) s = `${s}-${i+1}`; seen.add(s); return { ...c, slug:s }; });
    buildNav();
    if (!META.data_through) {
      $('heroDate').textContent = 'No reports yet'; $('heroLag').textContent = `The first report appears here ${META.lag_days} days after the first day of posts is captured.`;
      ['dayPick','prevDay','nextDay'].forEach(id => $(id).disabled = true);
      $('lagNote').textContent = 'nothing published yet';
      $('catgrid').innerHTML = $('cats').innerHTML = '<div class="empty">Nothing published for this client yet.</div>';
      $('cards').innerHTML = '<div class="empty">No posts yet.</div>';
      $('gChart').innerHTML = '<div class="empty">Nothing to show yet.</div>'; $('exportBtn').removeAttribute('href');
      for (const v of VIEWS) dirty[v] = false;     // nothing to draw; just switch screens
      window.addEventListener('hashchange', route);
      route();
      return;
    }
    wire();
    // setDay draws the current view; route first so it knows which one that is
    state.day = META.default_day || META.data_through;
    const h = decodeURIComponent((location.hash || '#today').slice(1)).split('/', 2);
    state.view = (h[0] === 'category' || h[0] === 'categories') ? 'categories' : (VIEWS.includes(h[0]) ? h[0] : 'today');
    if (h[0] === 'category' && h[1] && catBySlug(h[1])) state.cat = catBySlug(h[1]).raw; else if (CATS[0]) state.cat = CATS[0].raw;
    document.querySelectorAll('section.view').forEach(s => { s.hidden = s.id !== state.view; });
    markNav();
    setDay(state.day);
  }

  // No inline scripts (CSP): boot when the dashboard's meta block is present.
  if (document.getElementById('meta')) { if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot(); }
  return { boot, show, go };
})();
