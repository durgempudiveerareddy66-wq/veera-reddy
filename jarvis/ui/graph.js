/* The vault graph — canvas, not SVG.
   SVG needs a DOM node per element and stalls past ~1,500 nodes; a canvas draws
   the same graph in one pass. Repulsion uses a spatial grid with a distance
   cutoff so cost stays near-linear instead of O(n²) — without it, a few thousand
   notes turn the layout into a slideshow. */

(function (global) {
  'use strict';

  const TYPE_COLOR = {
    invoice: '#00E5FF', client: '#4FD6FF', project: '#7FE9F5',
    meeting: '#2FB8CE', person: '#9AF0F9', people: '#9AF0F9',
    idea:    '#00C2D6', admin:   '#0A7C8C', note: '#5FD9E8'
  };
  const DIM = 0.10;              // everything not in focus drops to ~10%
  const CUTOFF = 150;            // repulsion distance cutoff, in world units
  const CELL = CUTOFF;

  function VaultGraph(canvas, opts) {
    this.c = canvas;
    this.ctx = canvas.getContext('2d');
    this.nodes = [];
    this.edges = [];
    this.adj = new Map();
    this.view = { x: 0, y: 0, k: 1 };
    this.hover = null;
    this.selected = null;
    this.pathSet = null;
    this.onSelect = (opts && opts.onSelect) || function () {};
    this._drag = null;
    this._bind();
  }

  VaultGraph.prototype.load = function (data) {
    const n = data.nodes.length || 1;
    const R = Math.sqrt(n) * 42;
    this.nodes = data.nodes.map(function (d, i) {
      // Golden-angle seeding — an even spread beats random clumps, and the
      // simulation converges faster from it.
      const a = i * 2.399963, r = R * Math.sqrt(i / n);
      return {
        id: d.id, name: d.name, path: d.path, type: d.type,
        degree: d.degree, unindexed: d.unindexed,
        x: Math.cos(a) * r, y: Math.sin(a) * r, vx: 0, vy: 0,
        r: 3 + Math.min(11, Math.sqrt(d.degree) * 2.6)
      };
    });
    const byId = new Map(this.nodes.map(function (nd) { return [nd.id, nd]; }));
    this.edges = data.edges
      .map(function (e) { return { s: byId.get(e.source), t: byId.get(e.target) }; })
      .filter(function (e) { return e.s && e.t; });

    this.adj = new Map();
    const self = this;
    this.nodes.forEach(function (nd) { self.adj.set(nd.id, []); });
    this.edges.forEach(function (e) {
      self.adj.get(e.s.id).push(e.t.id);
      self.adj.get(e.t.id).push(e.s.id);
    });
    this.alpha = 1;
    this._fitted = false;
    this.view = { x: 0, y: 0, k: 1 };
  };

  /* ── layout ─────────────────────────────────────────────────────── */

  VaultGraph.prototype.step = function () {
    if (!this.nodes.length || this.alpha < 0.005) return;
    const nodes = this.nodes, a = this.alpha;

    // Spatial grid: only nodes within CUTOFF can repel, so each node compares
    // against its own cell and the eight around it, not all n.
    const grid = new Map();
    for (let i = 0; i < nodes.length; i++) {
      const nd = nodes[i];
      const key = ((nd.x / CELL) | 0) + ':' + ((nd.y / CELL) | 0);
      let cell = grid.get(key);
      if (!cell) { cell = []; grid.set(key, cell); }
      cell.push(nd);
    }

    for (let i = 0; i < nodes.length; i++) {
      const nd = nodes[i];
      const cx = (nd.x / CELL) | 0, cy = (nd.y / CELL) | 0;
      for (let gx = cx - 1; gx <= cx + 1; gx++) {
        for (let gy = cy - 1; gy <= cy + 1; gy++) {
          const cell = grid.get(gx + ':' + gy);
          if (!cell) continue;
          for (let j = 0; j < cell.length; j++) {
            const o = cell[j];
            if (o === nd) continue;
            let dx = nd.x - o.x, dy = nd.y - o.y;
            let d2 = dx * dx + dy * dy;
            if (d2 > CUTOFF * CUTOFF || d2 === 0) continue;
            const d = Math.sqrt(d2);
            const f = (900 * a) / d2;
            nd.vx += (dx / d) * f;
            nd.vy += (dy / d) * f;
          }
        }
      }
    }

    for (let i = 0; i < this.edges.length; i++) {
      const e = this.edges[i];
      const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - 64) * 0.012 * a;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      e.s.vx += fx; e.s.vy += fy;
      e.t.vx -= fx; e.t.vy -= fy;
    }

    for (let i = 0; i < nodes.length; i++) {
      const nd = nodes[i];
      nd.vx -= nd.x * 0.0016 * a;      // gentle pull to centre
      nd.vy -= nd.y * 0.0016 * a;
      nd.x += nd.vx; nd.y += nd.vy;
      nd.vx *= 0.82; nd.vy *= 0.82;
    }
    this.alpha *= 0.994;
    // Fit once, when the layout has stopped moving enough to be worth framing.
    if (!this._fitted && this.alpha < 0.35) { this._fitted = true; this.fit(); }
  };

  /* ── focus sets ─────────────────────────────────────────────────── */

  VaultGraph.prototype._focus = function () {
    if (this.pathSet) return this.pathSet;
    const id = this.hover || this.selected;
    if (!id) return null;
    const s = new Set([id]);
    (this.adj.get(id) || []).forEach(function (n) { s.add(n); });
    return s;
  };

  VaultGraph.prototype.shortestPath = function (aId, bId) {
    // Plain BFS. The graph is unweighted, so this is the shortest path, and it
    // stays fast enough that a click feels instant.
    const prev = new Map([[aId, null]]);
    const q = [aId];
    while (q.length) {
      const cur = q.shift();
      if (cur === bId) break;
      const nbrs = this.adj.get(cur) || [];
      for (let i = 0; i < nbrs.length; i++) {
        if (!prev.has(nbrs[i])) { prev.set(nbrs[i], cur); q.push(nbrs[i]); }
      }
    }
    if (!prev.has(bId)) return null;
    const out = new Set();
    let cur = bId;
    while (cur) { out.add(cur); cur = prev.get(cur); }
    return out;
  };

  /* ── draw ───────────────────────────────────────────────────────── */

  VaultGraph.prototype.draw = function () {
    const c = this.c, ctx = this.ctx;
    const dpr = global.devicePixelRatio || 1;
    const w = c.clientWidth, h = c.clientHeight;
    if (c.width !== w * dpr || c.height !== h * dpr) {
      c.width = w * dpr; c.height = h * dpr;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.save();
    ctx.translate(w / 2 + this.view.x, h / 2 + this.view.y);
    ctx.scale(this.view.k, this.view.k);

    const focus = this._focus();
    const inFocus = function (id) { return !focus || focus.has(id); };

    ctx.lineWidth = 1 / this.view.k;
    for (let i = 0; i < this.edges.length; i++) {
      const e = this.edges[i];
      const lit = !focus || (focus.has(e.s.id) && focus.has(e.t.id));
      ctx.strokeStyle = lit ? 'rgba(0,229,255,.34)' : 'rgba(10,124,140,' + DIM + ')';
      ctx.beginPath();
      ctx.moveTo(e.s.x, e.s.y);
      ctx.lineTo(e.t.x, e.t.y);
      ctx.stroke();
    }

    for (let i = 0; i < this.nodes.length; i++) {
      const nd = this.nodes[i];
      const lit = inFocus(nd.id);
      const col = TYPE_COLOR[nd.type] || TYPE_COLOR.note;
      ctx.globalAlpha = lit ? 1 : DIM;
      ctx.beginPath();
      ctx.arc(nd.x, nd.y, nd.r, 0, 6.28318);
      if (nd.unindexed) {
        // Ring only — an unindexed node is a file we could not read, and it
        // should not look like one we understood.
        ctx.strokeStyle = col; ctx.lineWidth = 1.4 / this.view.k; ctx.stroke();
      } else {
        ctx.fillStyle = col; ctx.fill();
      }
      if (nd.id === this.selected) {
        ctx.strokeStyle = '#CFFAFF';
        ctx.lineWidth = 2 / this.view.k;
        ctx.beginPath(); ctx.arc(nd.x, nd.y, nd.r + 4, 0, 6.28318); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }

    // Labels, most-connected first. A label whose box collides with one already
    // placed is skipped — otherwise the hub cluster turns to mush.
    const order = this.nodes.slice().sort(function (a, b) { return b.degree - a.degree; });
    const placed = [];
    ctx.font = (11 / this.view.k) + 'px Bahnschrift, "Segoe UI", sans-serif';
    ctx.textBaseline = 'middle';
    for (let i = 0; i < order.length && placed.length < 90; i++) {
      const nd = order[i];
      if (focus && !focus.has(nd.id)) continue;
      if (nd.degree === 0 && !focus) continue;
      const tw = ctx.measureText(nd.name).width;
      const bx = nd.x + nd.r + 4 / this.view.k, by = nd.y;
      const box = { x: bx, y: by - 7 / this.view.k, w: tw, h: 14 / this.view.k };
      let hit = false;
      for (let j = 0; j < placed.length; j++) {
        const p = placed[j];
        if (box.x < p.x + p.w && box.x + box.w > p.x &&
            box.y < p.y + p.h && box.y + box.h > p.y) { hit = true; break; }
      }
      if (hit) continue;
      placed.push(box);
      ctx.fillStyle = (nd.id === this.selected) ? '#CFFAFF' : 'rgba(127,233,245,.82)';
      ctx.fillText(nd.name, bx, by);
    }

    ctx.restore();
  };

  /* ── interaction ────────────────────────────────────────────────── */

  VaultGraph.prototype._world = function (ev) {
    const rect = this.c.getBoundingClientRect();
    return {
      x: (ev.clientX - rect.left - rect.width / 2 - this.view.x) / this.view.k,
      y: (ev.clientY - rect.top - rect.height / 2 - this.view.y) / this.view.k
    };
  };

  VaultGraph.prototype._at = function (p) {
    let best = null, bd = 14 / this.view.k;
    for (let i = 0; i < this.nodes.length; i++) {
      const nd = this.nodes[i];
      const d = Math.hypot(nd.x - p.x, nd.y - p.y);
      if (d < Math.max(nd.r + 3, bd)) { bd = d; best = nd; }
    }
    return best;
  };

  VaultGraph.prototype._bind = function () {
    const self = this;

    this.c.addEventListener('mousemove', function (ev) {
      if (self._drag) {
        self.view.x = self._drag.vx + (ev.clientX - self._drag.x);
        self.view.y = self._drag.vy + (ev.clientY - self._drag.y);
        return;
      }
      const nd = self._at(self._world(ev));
      self.hover = nd ? nd.id : null;
      self.c.style.cursor = nd ? 'pointer' : 'grab';
    });

    this.c.addEventListener('mousedown', function (ev) {
      const nd = self._at(self._world(ev));
      if (nd) return;
      self._drag = { x: ev.clientX, y: ev.clientY, vx: self.view.x, vy: self.view.y };
    });
    global.addEventListener('mouseup', function () { self._drag = null; });

    this.c.addEventListener('click', function (ev) {
      const nd = self._at(self._world(ev));
      if (!nd) { self.selected = null; self.pathSet = null; self.onSelect(null); return; }
      if (ev.shiftKey && self.selected && self.selected !== nd.id) {
        self.pathSet = self.shortestPath(self.selected, nd.id);
        self.onSelect(nd, self.pathSet ? self.pathSet.size : 0);
        return;
      }
      self.pathSet = null;
      self.selected = nd.id;
      self.onSelect(nd);
    });

    this.c.addEventListener('wheel', function (ev) {
      ev.preventDefault();
      const k = self.view.k * (ev.deltaY < 0 ? 1.12 : 0.89);
      self.view.k = Math.max(0.15, Math.min(6, k));
    }, { passive: false });
  };

  /* Fit the settled layout to the panel. Without this the cloud drifts wherever
     the simulation leaves it and the left-hand nodes clip off the edge — the
     centre pull is deliberately weak, so "centred at the origin" is not the same
     as "centred on screen". */
  VaultGraph.prototype.fit = function () {
    if (!this.nodes.length) { this.view = { x: 0, y: 0, k: 1 }; return; }
    // Fit to the CONNECTED structure. Unlinked nodes have repulsion pushing them
    // out and no edge pulling them back, so they end up far from everything —
    // fitting to them shrinks the actual graph to a dot in the middle.
    let pool = this.nodes.filter(function (n) { return n.degree > 0; });
    if (pool.length < 3) pool = this.nodes;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (let i = 0; i < pool.length; i++) {
      const n = pool[i];
      if (n.x < x0) x0 = n.x; if (n.x > x1) x1 = n.x;
      if (n.y < y0) y0 = n.y; if (n.y > y1) y1 = n.y;
    }
    const pad = 70;              // room for the labels, which sit to the right
    const w = this.c.clientWidth || 400, h = this.c.clientHeight || 400;
    const k = Math.min((w - pad) / Math.max(1, x1 - x0),
                       (h - pad) / Math.max(1, y1 - y0));
    this.view.k = Math.max(0.4, Math.min(2.2, k));
    this.view.x = -((x0 + x1) / 2) * this.view.k;
    this.view.y = -((y0 + y1) / 2) * this.view.k;
  };

  global.VaultGraph = VaultGraph;
})(window);
