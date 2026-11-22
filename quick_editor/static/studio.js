// quick-editor UI. Plain browser JavaScript; all image processing happens in Python.
const $ = (id) => document.getElementById(id);
const SVGNS = 'http://www.w3.org/2000/svg';

const app = {
  info: null,          // /api/state
  photo: null,         // file name
  photoSize: null,     // {w, h} after rotation, original pixels
  pageSize: null,      // {w, h} of the cleaned page preview
  rotate: 0,
  quad: null,          // 4 corners in original (rotated) pixels
  settings: {},
  gap: 0.0025,
  minSize: 0.02,
  boxes: [],           // {x, y, w, h (0..1 of page), isolate, include, polys?, meta}
  areas: [],           // {mode: 'erase'|'keep', points: [[x, y], …] (0..1 of page)}
  selectedArea: null,
  tool: 'box',
  selected: new Set(),
  step: 1,
  view: 'ink',
  urls: { photo: null, clean: null },
};

// ---------------------------------------------------------------- helpers
let statusTimer;
function say(msg, error = false) {
  const el = $('status');
  el.textContent = msg;
  el.classList.toggle('error', error);
  clearTimeout(statusTimer);
  if (msg && !error) statusTimer = setTimeout(() => (el.textContent = ''), 4000);
}

async function api(path, data, { signal, raw } = {}) {
  const res = await fetch(path, data === undefined ? { signal } : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data), signal,
  });
  if (!res.ok) {
    let msg = `${res.status}`;
    try { msg = (await res.json()).error ?? msg; } catch {}
    throw new Error(msg);
  }
  return raw ? res : res.json();
}

const debounce = (fn, ms) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

const svg = (tag, attrs = {}) => {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
};

function svgPoint(evt) {
  const o = $('overlay');
  const p = o.createSVGPoint();
  p.x = evt.clientX; p.y = evt.clientY;
  return p.matrixTransform(o.getScreenCTM().inverse());
}

/** Overlay units per screen pixel, to keep handles a constant size. */
const unit = () => {
  const o = $('overlay');
  const vb = o.viewBox.baseVal;
  return vb.width / (o.getBoundingClientRect().width || 1);
};

// ---------------------------------------------------------------- session
const sessionData = () => ({
  photo: app.photo, rotate: app.rotate, quad: app.quad, settings: app.settings,
  gap: app.gap, minSize: app.minSize, boxes: app.boxes, areas: app.areas, savedAt: new Date().toISOString(),
});
const saveSession = debounce(() => {
  if (app.photo) api('/api/session', sessionData()).catch((e) => say(`Couldn't save progress: ${e.message}`, true));
}, 800);
const LAST_SETTINGS = 'quick-editor:last-settings';

// ---------------------------------------------------------------- files
async function loadState() {
  app.info = await api('/api/state');
  $('inbox-path').textContent = app.info.inboxPath;
  $('project-name').textContent = app.info.project;
  document.title = `quick-editor · ${app.info.project}`;
  const list = $('file-list');
  list.replaceChildren(...app.info.inbox.map((f) => {
    const li = document.createElement('li');
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = f.name;
    if (f.hasSession) b.insertAdjacentHTML('afterbegin', '<span class="dot" title="Has saved progress">● </span>');
    b.setAttribute('aria-current', String(f.name === app.photo));
    b.addEventListener('click', () => openPhoto(f.name));
    li.append(b);
    return li;
  }));
  if (!app.info.inbox.length) say('The inbox is empty. Add photos to the inbox folder and press ↻.');
  if (!app.info.potrace) say('potrace not found: install it with `brew install potrace`, then restart the studio.', true);
  fillCollections($('default-collection'));
}

function fillCollections(select, value) {
  select.replaceChildren(
    ...app.info.collections.map((c) => new Option(`${c.name} (${c.prefix}-…)`, c.id)),
    new Option('New collection…', '__new'),
  );
  select.value = value ?? app.defaultCollection ?? app.info.collections.at(-1)?.id;
}

async function newCollection(select) {
  const name = prompt('Name of the new collection (e.g. "Squares IV"):');
  if (!name) { fillCollections(select); return null; }
  const prefix = prompt('Short id prefix for its assets (e.g. "sq4"):', name.split(/\s+/).map((w) => w[0]).join('').toLowerCase());
  try {
    const c = await api('/api/collections', { name, prefix });
    app.info.collections.push(c);
    document.querySelectorAll('select[name="collection"], #default-collection').forEach((s) => fillCollections(s, s === select ? c.id : s.value));
    return c.id;
  } catch (e) {
    say(e.message, true);
    fillCollections(select);
    return null;
  }
}

async function openPhoto(name) {
  app.photo = name;
  app.selected.clear();
  document.querySelectorAll('#file-list button').forEach((b) => b.setAttribute('aria-current', String(b.textContent.endsWith(name))));
  const saved = await api(`/api/session/${encodeURIComponent(name)}`);
  app.rotate = saved?.rotate ?? 0;
  app.settings = { ...app.info.defaults, ...(saved?.settings ?? {}) };
  app.gap = saved?.gap ?? 0.0025;
  app.minSize = saved?.minSize ?? 0.02;
  app.boxes = saved?.boxes ?? [];
  app.areas = saved?.areas ?? [];
  app.selectedArea = null;
  draft = null;
  app.quad = saved?.quad ?? null;
  await loadPhoto();
  if (!app.quad) await autoFrame(true);
  syncControls();
  goto(saved?.boxes?.length ? 3 : 1);
  say(saved ? 'Restored your progress on this photo.' : 'Drag the corners onto the page.');
}

async function loadPhoto() {
  const res = await api(`/api/photo/${encodeURIComponent(app.photo)}?rotate=${app.rotate}`, undefined, { raw: true });
  app.photoSize = { w: +res.headers.get('X-Width'), h: +res.headers.get('X-Height') };
  if (app.urls.photo) URL.revokeObjectURL(app.urls.photo);
  app.urls.photo = URL.createObjectURL(await res.blob());
}

// ---------------------------------------------------------------- steps
function goto(step) {
  if (!app.photo) return;
  app.step = step;
  draft = null;
  document.querySelectorAll('#steps [data-step]').forEach((b) => b.setAttribute('aria-current', +b.dataset.step === step ? 'step' : 'false'));
  document.querySelectorAll('#controls [data-panel]').forEach((p) => p.classList.toggle('active', +p.dataset.panel === step));
  $('empty').hidden = true;
  $('stage').hidden = step === 4;
  $('review').hidden = step !== 4;
  if (step === 1) showFrame();
  if (step === 2) refreshClean();
  if (step === 3) refreshClean().then(drawBoxes);
  if (step === 4) buildReview();
}

// ---------------------------------------------------------------- step 1: frame
async function setImage(url, w, h) {
  const img = $('img');
  $('overlay').setAttribute('viewBox', `0 0 ${w} ${h}`);
  if (img.src !== url) {
    img.src = url;
    await img.decode().catch(() => {});
  }
}

async function showFrame() {
  const { w, h } = app.photoSize;
  await setImage(app.urls.photo, w, h);
  drawQuad();
}

function drawQuad() {
  const o = $('overlay');
  o.replaceChildren();
  const poly = svg('polygon', { class: 'quad', points: app.quad.map((p) => p.join(',')).join(' ') });
  o.append(poly);
  const r = 9 * unit();
  app.quad.forEach((p, i) => {
    const c = svg('circle', { class: 'handle', cx: p[0], cy: p[1], r });
    c.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      c.setPointerCapture(e.pointerId);
      const move = (ev) => {
        const pt = svgPoint(ev);
        app.quad[i] = [Math.max(0, Math.min(app.photoSize.w, pt.x)), Math.max(0, Math.min(app.photoSize.h, pt.y))];
        c.setAttribute('cx', app.quad[i][0]);
        c.setAttribute('cy', app.quad[i][1]);
        poly.setAttribute('points', app.quad.map((q) => q.join(',')).join(' '));
      };
      const up = () => {
        c.removeEventListener('pointermove', move);
        c.removeEventListener('pointerup', up);
        frameChanged();
      };
      c.addEventListener('pointermove', move);
      c.addEventListener('pointerup', up);
    });
    o.append(c);
  });
}

function frameChanged() {
  const has = app.boxes.length || app.areas.length;
  if (has && !confirm('The frame changed. Keep your areas and pieces? (They may no longer line up.)')) { app.boxes = []; app.areas = []; }
  saveSession();
}

async function autoFrame(quiet = false) {
  const r = await api('/api/detect', { name: app.photo, rotate: app.rotate });
  app.quad = r.quad;
  if (!quiet) say(r.confidence > 0.6 ? 'Found the page. Check the corners.' : 'Not sure where the page is; adjust the corners.');
  if (app.step === 1) drawQuad();
  saveSession();
}

function wholeFrame() {
  const { w, h } = app.photoSize;
  app.quad = [[0, 0], [w, 0], [w, h], [0, h]];
  drawQuad();
  saveSession();
}

async function rotateBy(deg) {
  app.rotate = (app.rotate + deg + 360) % 360;
  app.boxes = [];
  await loadPhoto();
  await showFrame();
  wholeFrame();
}

// ---------------------------------------------------------------- step 2: clean
let cleanCtl;
async function refreshClean() {
  if (app.view === 'photo' && app.step === 2) {
    // straightened photo isn't served separately; show the framed original
    const { w, h } = app.photoSize;
    await setImage(app.urls.photo, w, h);
    $('overlay').replaceChildren(svg('polygon', { class: 'quad', points: app.quad.map((p) => p.join(',')).join(' ') }));
    return;
  }
  cleanCtl?.abort();
  cleanCtl = new AbortController();
  say('Cleaning…');
  try {
    const view = app.step === 3 ? 'ink' : app.view;
    const res = await api('/api/clean', { name: app.photo, quad: app.quad, settings: app.settings, areas: app.areas, view }, { signal: cleanCtl.signal, raw: true });
    app.pageSize = { w: +res.headers.get('X-Width'), h: +res.headers.get('X-Height') };
    if (app.urls.clean) URL.revokeObjectURL(app.urls.clean);
    app.urls.clean = URL.createObjectURL(await res.blob());
    await setImage(app.urls.clean, app.pageSize.w, app.pageSize.h);
    $('overlay').replaceChildren();
    if (app.step === 2) drawAreas();
    say('');
  } catch (e) {
    if (e.name !== 'AbortError') say(e.message, true);
  }
}
const refreshCleanSoon = debounce(refreshClean, 250);

function syncControls() {
  document.querySelectorAll('[data-setting]').forEach((el) => {
    const v = app.settings[el.dataset.setting];
    if (el.type === 'checkbox') el.checked = Boolean(v);
    else el.value = v;
  });
  $('gap').value = app.gap;
  $('min-size').value = app.minSize;
}

// ---------------------------------------------------------------- free-form polygons
// Used for areas (step 2) and lasso pieces (step 3). Points are normalised to the page.
let draft = null; // { points, hover, onDone }

function startPolygon(onDone) {
  draft = { points: [], hover: null, onDone };
  say('Click to add points. Click the first point, double-click or press Enter to close. Esc cancels.');
  renderDraft();
}

function renderDraft() {
  const o = $('overlay');
  o.querySelector('.draft-poly')?.remove();
  if (!draft || !app.pageSize) return;
  const { w: W, h: H } = app.pageSize;
  const pts = [...draft.points, ...(draft.hover ? [draft.hover] : [])].map(([x, y]) => `${x * W},${y * H}`).join(' ');
  const g = svg('g', { class: 'draft-poly' });
  g.append(svg('polygon', { class: 'draft', points: pts }));
  draft.points.forEach(([x, y], i) => g.append(svg('circle', { class: i === 0 ? 'handle first' : 'handle', cx: x * W, cy: y * H, r: (i === 0 ? 7 : 4) * unit() })));
  o.append(g);
}

function finishDraft() {
  const d = draft;
  draft = null;
  renderDraft();
  if (d && d.points.length >= 3) d.onDone(d.points);
  else say('');
}

function setupPolygonDrawing() {
  const o = $('overlay');
  const norm01 = (e) => {
    const pt = svgPoint(e);
    return [clamp01(pt.x / app.pageSize.w), clamp01(pt.y / app.pageSize.h)];
  };
  // Registered before the box editor; while a polygon is being drawn it takes all clicks.
  o.addEventListener('pointerdown', (e) => {
    if (!draft || !app.pageSize) return;
    e.stopImmediatePropagation();
    e.preventDefault();
    const p = norm01(e);
    const first = draft.points[0];
    const px = (a, b) => Math.hypot((a[0] - b[0]) * app.pageSize.w, (a[1] - b[1]) * app.pageSize.h) / unit();
    if (e.detail >= 2 || (first && draft.points.length >= 3 && px(p, first) < 12)) { finishDraft(); return; }
    draft.points.push(p);
    renderDraft();
  }, true);
  o.addEventListener('pointermove', (e) => {
    if (!draft || !app.pageSize) return;
    draft.hover = norm01(e);
    renderDraft();
  });
}

function drawAreas() {
  if (app.step !== 2 || app.view === 'photo' || !app.pageSize) return;
  const { w: W, h: H } = app.pageSize;
  const o = $('overlay');
  o.querySelectorAll('.area').forEach((el) => el.remove());
  const keeps = app.areas.filter((a) => a.mode === 'keep');
  if (keeps.length) {
    // dim everything outside the keep areas
    const d = `M0 0H${W}V${H}H0Z ` + keeps.map((a) => 'M' + a.points.map(([x, y]) => `${x * W} ${y * H}`).join('L') + 'Z').join(' ');
    o.append(svg('path', { class: 'area area-keep-shade', d }));
  }
  app.areas.forEach((a, i) => {
    const poly = svg('polygon', {
      class: `area area-${a.mode}${app.selectedArea === i ? ' selected' : ''}`,
      points: a.points.map(([x, y]) => `${x * W},${y * H}`).join(' '),
    });
    poly.addEventListener('pointerdown', (e) => {
      if (draft) return;
      e.stopPropagation();
      app.selectedArea = app.selectedArea === i ? null : i;
      drawAreas();
    });
    o.append(poly);
  });
}

function addArea(mode) {
  if (app.view === 'photo') { document.querySelector('input[name="view"][value="ink"]').click(); }
  startPolygon((points) => {
    app.areas.push({ mode, points });
    app.selectedArea = null;
    saveSession();
    refreshClean();
    say(mode === 'erase' ? 'Area erased.' : 'Only ink inside the area is kept.');
  });
}

// ---------------------------------------------------------------- step 3: pieces
async function detect() {
  if (app.boxes.length && !confirm('Replace your current boxes with detected ones?')) return;
  say('Detecting…');
  const r = await api('/api/pieces', { name: app.photo, quad: app.quad, settings: app.settings, areas: app.areas, gap: app.gap, minSize: app.minSize });
  app.boxes = r.boxes.map((b) => ({ ...b, include: true, meta: {} }));
  app.selected.clear();
  drawBoxes();
  say(`${app.boxes.length} pieces found.`);
  saveSession();
}

function drawBoxes() {
  if (app.step !== 3 || !app.pageSize) return;
  const { w: W, h: H } = app.pageSize;
  const o = $('overlay');
  o.replaceChildren();
  const u = unit();
  app.boxes.forEach((b, i) => {
    const sel = app.selected.has(i);
    const cls = `box${sel ? ' selected' : ''}${b.include === false ? ' off' : ''}`;
    const shapes = b.polys
      ? b.polys.map((poly) => svg('polygon', { class: cls, points: poly.map(([x, y]) => `${x * W},${y * H}`).join(' ') }))
      : [svg('rect', { class: cls, x: b.x * W, y: b.y * H, width: b.w * W, height: b.h * H })];
    for (const r of shapes) {
      r.dataset.index = i;
      o.append(r);
    }
    const t = svg('text', { class: 'box-label', x: b.x * W + 4 * u, y: b.y * H + 13 * u, 'font-size': 12 * u });
    t.textContent = i + 1;
    o.append(t);
  });
  if (app.selected.size === 1 && !app.boxes[[...app.selected][0]].polys) {
    const b = app.boxes[[...app.selected][0]];
    const corners = [[b.x, b.y], [b.x + b.w, b.y], [b.x + b.w, b.y + b.h], [b.x, b.y + b.h]];
    corners.forEach(([x, y], k) => {
      const c = svg('circle', { class: 'handle', cx: x * W, cy: y * H, r: 7 * u });
      c.dataset.corner = k;
      o.append(c);
    });
  }
  $('merge').disabled = app.selected.size < 2;
  $('remove').disabled = app.selected.size === 0;
  const isolate = $('isolate');
  isolate.disabled = app.selected.size === 0;
  isolate.checked = app.selected.size > 0 && [...app.selected].every((i) => app.boxes[i].isolate);
  $('box-count').textContent = `${app.boxes.length} boxes${app.selected.size ? `, ${app.selected.size} selected` : ''}.`;
}

function setupBoxEditing() {
  const o = $('overlay');
  let drag = null;
  o.addEventListener('pointerdown', (e) => {
    if (app.step !== 3 || !app.pageSize) return;
    const { w: W, h: H } = app.pageSize;
    const pt = svgPoint(e);
    const p = { x: pt.x / W, y: pt.y / H };
    const target = e.target;
    o.setPointerCapture(e.pointerId);
    if (target.dataset.corner !== undefined) {
      const i = [...app.selected][0];
      drag = { kind: 'resize', i, corner: +target.dataset.corner, start: { ...app.boxes[i] } };
    } else if (target.dataset.index !== undefined) {
      const i = +target.dataset.index;
      if (e.shiftKey) {
        app.selected.has(i) ? app.selected.delete(i) : app.selected.add(i);
        drawBoxes();
        return;
      }
      if (!app.selected.has(i)) { app.selected = new Set([i]); drawBoxes(); }
      drag = { kind: 'move', from: p, starts: [...app.selected].map((k) => [k, structuredClone(app.boxes[k])]) };
    } else if (app.tool === 'lasso') {
      o.releasePointerCapture(e.pointerId);
      if (!e.shiftKey) app.selected.clear();
      drawBoxes();
      startPolygon((points) => {
        app.boxes.push({ ...bbox([points]), polys: [points], isolate: false, include: true, meta: {} });
        app.selected = new Set([app.boxes.length - 1]);
        drawBoxes();
        saveSession();
        say('Piece added.');
      });
      // this click is the first point
      draft.points.push([p.x, p.y]);
      renderDraft();
    } else {
      if (!e.shiftKey) app.selected.clear();
      drag = { kind: 'draw', from: p, el: svg('rect', { class: 'draft' }) };
      o.append(drag.el);
      drawBoxesKeepDraft(drag.el);
    }
  });
  o.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const { w: W, h: H } = app.pageSize;
    const pt = svgPoint(e);
    const p = { x: clamp01(pt.x / W), y: clamp01(pt.y / H) };
    if (drag.kind === 'draw') {
      const r = norm(drag.from, p);
      Object.entries({ x: r.x * W, y: r.y * H, width: r.w * W, height: r.h * H }).forEach(([k, v]) => drag.el.setAttribute(k, v));
    } else if (drag.kind === 'move') {
      const dx = p.x - drag.from.x, dy = p.y - drag.from.y;
      for (const [k, s] of drag.starts) {
        const b = app.boxes[k];
        b.x = clamp01(s.x + dx, 1 - s.w);
        b.y = clamp01(s.y + dy, 1 - s.h);
        if (s.polys) {
          const mx = b.x - s.x, my = b.y - s.y;
          b.polys = s.polys.map((poly) => poly.map(([x, y]) => [x + mx, y + my]));
        }
      }
      drawBoxes();
    } else if (drag.kind === 'resize') {
      const s = drag.start;
      const corners = [[s.x, s.y], [s.x + s.w, s.y], [s.x + s.w, s.y + s.h], [s.x, s.y + s.h]];
      const opposite = corners[(drag.corner + 2) % 4];
      Object.assign(app.boxes[drag.i], norm({ x: opposite[0], y: opposite[1] }, p));
      drawBoxes();
    }
  });
  const end = (e) => {
    if (!drag) return;
    if (drag.kind === 'draw') {
      const { w: W, h: H } = app.pageSize;
      const pt = svgPoint(e);
      const r = norm(drag.from, { x: clamp01(pt.x / W), y: clamp01(pt.y / H) });
      drag.el.remove();
      if (r.w * W > 6 && r.h * H > 6) {
        app.boxes.push({ ...r, isolate: false, include: true, meta: {} });
        app.selected = new Set([app.boxes.length - 1]);
      }
      drawBoxes();
    }
    drag = null;
    saveSession();
  };
  o.addEventListener('pointerup', end);
  o.addEventListener('pointercancel', end);
}

function drawBoxesKeepDraft(el) { drawBoxes(); $('overlay').append(el); }
const clamp01 = (v, max = 1) => Math.max(0, Math.min(max, v));
const norm = (a, b) => ({ x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) });
const bbox = (polys) => {
  const xs = polys.flat().map((p) => p[0]), ys = polys.flat().map((p) => p[1]);
  const x = Math.min(...xs), y = Math.min(...ys);
  return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y };
};

function mergeSelected() {
  const idx = [...app.selected].sort((a, b) => a - b);
  const bs = idx.map((i) => app.boxes[i]);
  const x0 = Math.min(...bs.map((b) => b.x)), y0 = Math.min(...bs.map((b) => b.y));
  const x1 = Math.max(...bs.map((b) => b.x + b.w)), y1 = Math.max(...bs.map((b) => b.y + b.h));
  const anyPoly = bs.some((b) => b.polys);
  const rect = (b) => [[b.x, b.y], [b.x + b.w, b.y], [b.x + b.w, b.y + b.h], [b.x, b.y + b.h]];
  const merged = {
    x: x0, y: y0, w: x1 - x0, h: y1 - y0, isolate: false, include: true, meta: { ...bs[0].meta },
    ...(anyPoly ? { polys: bs.flatMap((b) => b.polys ?? [rect(b)]) } : {}),
  };
  app.boxes = app.boxes.filter((_, i) => !app.selected.has(i));
  app.boxes.splice(idx[0], 0, merged);
  app.selected = new Set([idx[0]]);
  drawBoxes();
  saveSession();
}

function removeSelected() {
  app.boxes = app.boxes.filter((_, i) => !app.selected.has(i));
  app.selected.clear();
  drawBoxes();
  saveSession();
}

// ---------------------------------------------------------------- step 4: review
let traceQueue = Promise.resolve();
const cards = new Map(); // box -> card element

function buildReview() {
  const wrap = $('review');
  wrap.replaceChildren();
  cards.clear();
  if (!app.boxes.length) {
    wrap.innerHTML = '<p class="hint">No pieces yet. Go back to step 3 and detect or draw some.</p>';
    return;
  }
  app.boxes.forEach((box, i) => {
    const card = $('card').content.firstElementChild.cloneNode(true);
    const f = (n) => card.querySelector(`[name="${n}"]`);
    box.meta ??= {};
    f('include').checked = box.include !== false;
    f('name').value = box.meta.name ?? '';
    f('tags').value = box.meta.tags ?? '';
    // "Kind" only exists when the project lists categories in quick-editor.toml
    card.querySelector('.kind').hidden = !app.info.categories.length;
    f('category').replaceChildren(...app.info.categories.map((c) => new Option(c, c)));
    if (app.info.categories.length) f('category').value = box.meta.category ?? app.info.categories[0];
    fillCollections(f('collection'), box.meta.collection ?? $('default-collection').value);
    f('replaces').append(...app.info.assets.map((a) => new Option(`${a.id} · ${a.name}${a.status !== 'published' ? ` (${a.status})` : ''}`, a.id)));
    f('replaces').value = box.meta.replaces ?? '';
    f('backing').checked = Boolean(box.meta.backing);
    card.classList.toggle('off', box.include === false);
    card.querySelector('.meta').textContent = `#${i + 1}${box.meta.savedAs ? ` · saved as ${box.meta.savedAs}` : ''}`;

    card.addEventListener('input', async (e) => {
      const el = e.target;
      if (el.name === 'include') { box.include = el.checked; card.classList.toggle('off', !el.checked); }
      else if (el.name === 'backing') { box.meta.backing = el.checked; traceCard(box, card); }
      else if (el.name === 'collection' && el.value === '__new') { box.meta.collection = await newCollection(el); }
      else if (el.name) box.meta[el.name] = el.value;
      if (el.name === 'replaces') f('collection').disabled = Boolean(el.value);
      saveSession();
    });
    f('collection').disabled = Boolean(box.meta.replaces);
    cards.set(box, card);
    wrap.append(card);
    traceCard(box, card);
  });
}

function traceCard(box, card) {
  const ink = card.querySelector('img.ink');
  const backing = card.querySelector('img.backing');
  const loading = card.querySelector('.loading');
  loading.hidden = false;
  loading.textContent = 'Tracing…';
  traceQueue = traceQueue.then(async () => {
    try {
      const r = await api('/api/trace', {
        name: app.photo, quad: app.quad, settings: app.settings, areas: app.areas, gap: app.gap, box, backing: Boolean(box.meta.backing),
      });
      ink.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(r.svg)}`;
      backing.hidden = !r.backing;
      if (r.backing) backing.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(r.backing)}`;
      loading.hidden = true;
      const kb = (new Blob([r.svg]).size / 1024).toFixed(0);
      card.querySelector('.meta').textContent = `${card.querySelector('.meta').textContent.split(' · ')[0]} · ${r.sourceWidth}×${r.sourceHeight}px source · ${kb} KB SVG${box.meta.savedAs ? ` · saved as ${box.meta.savedAs}` : ''}`;
      // quality: too few pixels and fine lines merge, edges go blocky
      const px = Math.max(r.sourceWidth, r.sourceHeight);
      const warn = card.querySelector('.quality');
      warn.hidden = px >= app.info.minPixels;
      warn.textContent = `Only ${px} px across (aim for ${app.info.minPixels}+). It will lose detail: rescan or photograph this drawing closer, at a higher resolution. You can still save it as a draft.`;
    } catch (e) {
      loading.textContent = e.message;
    }
  });
}

const retraceAll = debounce(() => { for (const [box, card] of cards) traceCard(box, card); }, 400);

async function save() {
  const pieces = app.boxes.filter((b) => b.include !== false);
  if (!pieces.length) { say('Nothing checked to save.'); return; }
  for (const b of pieces) b.meta.collection ??= $('default-collection').value;
  say(`Saving ${pieces.length} piece(s)… full resolution, this can take a few seconds.`);
  $('save').disabled = true;
  try {
    const r = await api('/api/save', { name: app.photo, quad: app.quad, settings: app.settings, areas: app.areas, gap: app.gap, pieces });
    const list = $('results');
    list.replaceChildren(...r.results.map((res, k) => {
      const li = document.createElement('li');
      if (res.ok) {
        pieces[k].meta.savedAs = res.id;
        li.textContent = `${res.updated ? 'Updated' : 'Added'} ${res.id} · ${res.name} (${res.status})${res.small ? ` · only ${res.pixels} px: the site won't publish it until it's re-extracted from a sharper source` : ''}`;
        if (res.small) li.className = 'warn';
      } else {
        li.className = 'bad';
        li.textContent = `Failed ${res.name || `#${k + 1}`}: ${res.error}`;
      }
      return li;
    }));
    const ok = r.results.filter((x) => x.ok).length;
    say(`Saved ${ok} of ${pieces.length}.`, ok < pieces.length);
    localStorage.setItem(LAST_SETTINGS, JSON.stringify(app.settings));
    saveSession();
    app.info = await api('/api/state');
    buildReview();
  } catch (e) {
    say(e.message, true);
  } finally {
    $('save').disabled = false;
  }
}

// ---------------------------------------------------------------- wiring
function wire() {
  $('refresh').addEventListener('click', loadState);
  document.querySelectorAll('#steps [data-step]').forEach((b) => b.addEventListener('click', () => goto(+b.dataset.step)));
  document.querySelectorAll('[data-goto]').forEach((b) => b.addEventListener('click', () => goto(+b.dataset.goto)));

  $('rot-left').addEventListener('click', () => rotateBy(-90));
  $('rot-right').addEventListener('click', () => rotateBy(90));
  $('auto-frame').addEventListener('click', () => autoFrame());
  $('whole-frame').addEventListener('click', wholeFrame);

  document.querySelectorAll('input[name="view"]').forEach((r) => r.addEventListener('change', () => { app.view = r.value; refreshClean(); }));
  document.querySelectorAll('[data-setting]').forEach((el) => el.addEventListener('input', () => {
    app.settings[el.dataset.setting] = el.type === 'checkbox' ? el.checked : Number(el.value);
    saveSession();
    if (el.dataset.setting === 'curves') retraceAll();
    else refreshCleanSoon();
  }));
  $('area-erase').addEventListener('click', () => addArea('erase'));
  $('area-keep').addEventListener('click', () => addArea('keep'));
  $('area-clear').addEventListener('click', () => {
    if (app.areas.length && !confirm('Remove all areas?')) return;
    app.areas = []; app.selectedArea = null; saveSession(); refreshClean();
  });
  document.querySelectorAll('input[name="tool"]').forEach((r) => r.addEventListener('change', () => { app.tool = r.value; draft = null; renderDraft(); }));
  setupPolygonDrawing();
  $('reset-clean').addEventListener('click', () => {
    app.settings = { ...app.info.defaults, rotate: app.rotate, curves: app.settings.curves };
    syncControls(); refreshClean(); saveSession();
  });
  $('copy-prev').addEventListener('click', () => {
    const prev = JSON.parse(localStorage.getItem(LAST_SETTINGS) ?? 'null');
    if (!prev) { say('No saved settings yet.'); return; }
    app.settings = { ...prev, rotate: app.rotate };
    syncControls(); refreshClean(); saveSession();
  });

  $('gap').addEventListener('input', (e) => { app.gap = +e.target.value; saveSession(); });
  $('min-size').addEventListener('input', (e) => { app.minSize = +e.target.value; saveSession(); });
  $('detect').addEventListener('click', () => detect().catch((e) => say(e.message, true)));
  $('merge').addEventListener('click', mergeSelected);
  $('remove').addEventListener('click', removeSelected);
  $('isolate').addEventListener('change', (e) => { for (const i of app.selected) app.boxes[i].isolate = e.target.checked; saveSession(); });
  setupBoxEditing();

  document.querySelectorAll('input[name="bg"]').forEach((r) => r.addEventListener('change', () => {
    $('review').classList.toggle('dark', r.value === 'dark' && r.checked);
    $('review').classList.toggle('cutout', r.value === 'cutout' && r.checked);
  }));
  $('default-collection').addEventListener('change', async (e) => {
    if (e.target.value === '__new') await newCollection(e.target);
    app.defaultCollection = e.target.value;
  });
  $('save').addEventListener('click', save);

  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, select, textarea')) return;
    if (draft) {
      if (e.key === 'Escape') { draft = null; renderDraft(); say('Cancelled.'); }
      if (e.key === 'Enter') finishDraft();
      if (e.key === 'Backspace') { e.preventDefault(); draft.points.pop(); renderDraft(); }
      return;
    }
    if (app.step === 2 && (e.key === 'Delete' || e.key === 'Backspace') && app.selectedArea !== null) {
      e.preventDefault();
      app.areas.splice(app.selectedArea, 1);
      app.selectedArea = null;
      saveSession();
      refreshClean();
      return;
    }
    if (app.step === 3 && (e.key === 'Delete' || e.key === 'Backspace') && app.selected.size) { e.preventDefault(); removeSelected(); }
    if (app.step === 3 && e.key === 'Escape') { app.selected.clear(); drawBoxes(); }
  });
  window.addEventListener('resize', () => { if (app.step === 1) drawQuad(); if (app.step === 3) drawBoxes(); });
}

wire();
loadState().catch((e) => say(`Couldn't reach the studio server: ${e.message}`, true));
