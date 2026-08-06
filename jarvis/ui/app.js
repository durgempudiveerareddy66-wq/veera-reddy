/* JARVIS HUD.

   Everything animated here runs on setInterval, never requestAnimationFrame.
   rAF stops dead in a backgrounded tab — the reactor would freeze the moment you
   alt-tabbed, and a frozen reactor looks exactly like a crash (§9.5, §9.6).

   The page receives state over SSE and never touches getUserMedia. The microphone
   lives in Python (§5); when voice lands in step 4, nothing in this file changes
   except that the listening state starts getting real RMS. */

(function () {
  'use strict';

  const FRAME_MS = 33;          // ~30fps, and it keeps running in a background tab
  const $ = function (id) { return document.getElementById(id); };

  const S = {
    state: 'idle',
    pending: null,               // {id, code, needs, tier}
    left: 0,
    rms: 0,
    tick: 0,
    cards: new Map(),
    files: 0
  };

  /* ── reactor ────────────────────────────────────────────────────── */

  const R = {
    outer: $('r-outer'), mid: $('r-mid'), inner: $('r-inner'),
    ticks: $('ticks'), bars: $('bars'),
    countdown: $('countdown'), blockarc: $('blockarc'),
    core: $('core'), glow: $('core-glow'),
    num: $('r-num'), label: $('r-state')
  };

  const TICKS = 48, BARS = 40;
  (function buildReactor() {
    const NS = 'http://www.w3.org/2000/svg';
    for (let i = 0; i < TICKS; i++) {
      const a = (i / TICKS) * Math.PI * 2 - Math.PI / 2;
      const l = document.createElementNS(NS, 'line');
      l.setAttribute('x1', 200 + Math.cos(a) * 118);
      l.setAttribute('y1', 200 + Math.sin(a) * 118);
      l.setAttribute('x2', 200 + Math.cos(a) * 126);
      l.setAttribute('y2', 200 + Math.sin(a) * 126);
      R.ticks.appendChild(l);
    }
    for (let i = 0; i < BARS; i++) {
      const l = document.createElementNS(NS, 'line');
      R.bars.appendChild(l);
    }
  })();

  const CIRC = 2 * Math.PI * 158;
  R.countdown.setAttribute('stroke-dasharray', CIRC);
  R.blockarc.setAttribute('stroke-dasharray', CIRC);

  let rot = 0;
  function frame() {
    S.tick++;
    const st = S.state;

    // Rings rotate at different speeds, and the speed says what JARVIS is doing.
    const speed = st === 'thinking' ? 2.4 : st === 'listening' ? 1.2
                : st === 'speaking' ? 1.6 : st === 'awaiting' ? 0 : 0.35;
    rot += speed;
    R.outer.setAttribute('transform', 'rotate(' + (rot * 0.6) + ' 200 200)');
    R.mid.setAttribute('transform', 'rotate(' + (-rot * 0.9) + ' 200 200)');
    R.inner.setAttribute('transform', 'rotate(' + (rot * 1.7) + ' 200 200)');

    // idle — one ring breathing, ~40% brightness
    const breathe = 0.4 + Math.sin(S.tick / 26) * 0.09;
    R.outer.style.opacity = st === 'idle' ? breathe : 1;

    // thinking — tick marks resolve one at a time
    const ticks = R.ticks.children;
    for (let i = 0; i < ticks.length; i++) {
      let on = false;
      if (st === 'thinking') on = ((S.tick >> 1) % TICKS) >= i;
      else if (st === 'idle') on = false;
      else on = i % 6 === 0;
      ticks[i].classList.toggle('on', on);
    }

    // listening — outer segments driven by real mic RMS from the server
    const bars = R.bars.children;
    for (let i = 0; i < bars.length; i++) {
      const a = (i / BARS) * Math.PI * 2 - Math.PI / 2;
      const amp = st === 'listening'
        ? 6 + S.rms * 46 * (0.55 + 0.45 * Math.sin(i * 1.7 + S.tick / 3))
        : 0;
      const r0 = 176, r1 = r0 + amp;
      bars[i].setAttribute('x1', 200 + Math.cos(a) * r0);
      bars[i].setAttribute('y1', 200 + Math.sin(a) * r0);
      bars[i].setAttribute('x2', 200 + Math.cos(a) * r1);
      bars[i].setAttribute('y2', 200 + Math.sin(a) * r1);
      bars[i].style.opacity = amp > 0.5 ? 0.85 : 0;
    }

    // speaking — core pulses; idle — steady
    const pulse = st === 'speaking' ? 34 + Math.sin(S.tick / 2.2) * 7
                : st === 'thinking' ? 34 + Math.sin(S.tick / 5) * 3 : 34;
    R.core.setAttribute('r', pulse);
    R.glow.setAttribute('r', pulse + 28);
    R.glow.style.opacity = st === 'idle' ? 0.32 : 0.55;

    // awaiting — everything freezes, an amber arc closes clockwise
    if (st === 'awaiting' && S.pending) {
      const frac = Math.max(0, Math.min(1, S.left / 20));
      R.countdown.classList.add('on');
      R.countdown.setAttribute('stroke-dashoffset', CIRC * (1 - frac));
      R.num.textContent = S.left.toFixed(1);
    } else {
      R.countdown.classList.remove('on');
    }

    // blocked — a red arc, held, until acknowledged
    R.blockarc.classList.toggle('on', st === 'blocked');
    if (st === 'blocked') R.blockarc.setAttribute('stroke-dashoffset', CIRC * 0.72);

    if (st !== 'awaiting') R.num.textContent = String(S.files).padStart(2, '0');
    R.label.textContent = st.toUpperCase();
  }
  setInterval(frame, FRAME_MS);

  function setState(s) {
    S.state = s;
    $('t-state').textContent = s.toUpperCase();
    const strip = $('strip');
    strip.classList.toggle('awaiting', s === 'awaiting');
    strip.classList.toggle('blocked', s === 'blocked');
  }

  /* ── action stack ───────────────────────────────────────────────── */

  function cardFor(action) {
    let el = S.cards.get(action.id);
    if (el) return el;
    el = document.createElement('div');
    el.className = 'card ' + action.risk;
    el.innerHTML =
      '<div class="card-top">' +
        '<span class="card-verb"></span>' +
        '<span class="card-state">QUEUED</span>' +
      '</div>' +
      '<div class="card-prev"></div>' +
      '<div class="card-why"></div>';
    el.querySelector('.card-verb').textContent = action.verb;
    el.querySelector('.card-prev').textContent = action.preview;
    if (action.reasons && action.reasons.length) {
      el.querySelector('.card-why').textContent = action.reasons.join(' · ');
    }
    const box = $('cards');
    box.insertBefore(el, box.firstChild);
    S.cards.set(action.id, el);
    return el;
  }

  function cardState(id, text, done) {
    const el = S.cards.get(id);
    if (!el) return;
    el.querySelector('.card-state').textContent = text;
    if (done) el.classList.add('done');
  }

  /* ── confirmation overlay ───────────────────────────────────────── */

  function askConfirm(action, code, needs) {
    S.pending = { id: action.id, code: code, needs: needs, tier: action.risk };
    S.left = 20;
    const red = action.risk === 'RED';
    $('conf').classList.toggle('red', red);
    $('conf-tier').textContent = red
      ? 'RED — THIS CANNOT BE RELEASED BY VOICE'
      : 'AMBER — ONE WORD RELEASES THIS';
    $('conf-preview').textContent = action.preview;
    const ul = $('conf-reasons');
    ul.innerHTML = '';
    (action.reasons || []).forEach(function (r) {
      const li = document.createElement('li');
      li.textContent = r;
      ul.appendChild(li);
    });
    $('conf-code').hidden = !red;
    if (red) {
      $('conf-code-val').textContent = code;
      $('conf-code-in').value = '';
      setTimeout(function () { $('conf-code-in').focus(); }, 30);
    }
    $('overlay').hidden = false;
    setState('awaiting');
    cardState(action.id, 'CONFIRMING');
  }

  function closeConfirm() {
    $('overlay').hidden = true;
    S.pending = null;
  }

  $('conf-yes').onclick = function () {
    if (!S.pending) return;
    post('/api/confirm', {
      id: S.pending.id, approve: true, code: $('conf-code-in').value
    });
    closeConfirm();
  };
  $('conf-no').onclick = function () {
    if (!S.pending) return;
    post('/api/confirm', { id: S.pending.id, approve: false });
    closeConfirm();
  };
  $('conf-code-in').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') $('conf-yes').click();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && S.pending) $('conf-no').click();
  });

  /* ── transport ──────────────────────────────────────────────────── */

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); }).catch(function (e) {
      // Degrade loudly — a silent failure here is the worst kind (§9.7).
      $('caption').textContent = 'LOST THE SERVER: ' + e;
      setState('blocked');
    });
  }

  const ES = new EventSource('/api/events');
  ES.onmessage = function (ev) {
    const m = JSON.parse(ev.data);
    switch (m.kind) {
      case 'state':      if (m.state !== 'awaiting') setState(m.state); break;
      case 'transcript': $('caption').textContent = '“' + m.text + '”'; break;
      case 'action':     cardFor(m.action); break;
      case 'plan':       (m.actions || []).forEach(cardFor); break;
      case 'confirm':    cardFor(m.action); askConfirm(m.action, m.code, m.needs); break;
      case 'countdown':  if (S.pending && m.id === S.pending.id) S.left = m.left; break;
      case 'cancelled':  closeConfirm(); cardState(m.id, 'CANCELLED', true);
                         $('caption').textContent = (m.reason || 'cancelled').toUpperCase();
                         setState('idle'); break;
      case 'blocked':    cardState(m.id, 'REFUSED', true);
                         $('caption').textContent = (m.reason || 'refused').toUpperCase();
                         setState('blocked'); break;
      case 'done':       cardState(m.id, m.ok ? 'DONE' : 'FAILED', true); break;
      case 'rms':        S.rms = m.value; break;
      case 'error':      $('caption').textContent = 'ERROR: ' + m.error;
                         setState('blocked'); break;
    }
  };
  ES.onerror = function () {
    $('caption').textContent = 'EVENT STREAM DOWN — THE SERVER MAY HAVE STOPPED';
    setState('blocked');
  };

  /* ── telemetry ──────────────────────────────────────────────────── */

  function health() {
    fetch('/api/health').then(function (r) { return r.json(); }).then(function (h) {
      $('t-brain').textContent = h.brain === 'none' ? 'NO MODEL' : h.brain;
      $('t-brain').style.color = h.brain === 'none' ? 'var(--red)' : '';
      $('t-stt').textContent = h.stt;
      $('t-tts').textContent = h.tts;
      $('t-wake').textContent = h.wake;
      $('t-dry').textContent = h.dry_run ? 'ON' : 'OFF';
      $('t-dry').style.color = h.dry_run ? '' : 'var(--amber)';
      $('t-chrome').textContent = h.chrome;
      $('t-verbs').textContent = h.attached_verbs + '/' + h.verbs;
      const j = h.journal || {};
      $('t-acts').textContent = (j.executed || 0);
      $('yolo').hidden = !h.yolo;

      const notes = $('notes');
      if (notes.childElementCount !== (h.notes || []).length) {
        notes.innerHTML = '';
        (h.notes || []).forEach(function (n) {
          const d = document.createElement('div');
          d.className = 'note';
          d.textContent = n;
          notes.appendChild(d);
        });
      }
    }).catch(function () {
      $('t-brain').textContent = 'SERVER DOWN';
      $('t-brain').style.color = 'var(--red)';
    });
  }
  health();
  setInterval(health, 4000);

  /* ── graph ──────────────────────────────────────────────────────── */

  const g = new VaultGraph($('graph'), {
    onSelect: function (nd, pathLen) {
      const ins = $('inspector');
      if (!nd) { ins.hidden = true; return; }
      ins.hidden = false;
      ins.innerHTML = '<h3></h3><p class="meta"></p><p class="path"></p>';
      ins.querySelector('h3').textContent = nd.name;
      ins.querySelector('.meta').textContent =
        nd.type.toUpperCase() + ' · ' + nd.degree + ' LINKS' +
        (nd.unindexed ? ' · UNINDEXED (no readable text)' : '') +
        (pathLen ? ' · PATH OF ' + pathLen + ' NODES' : '');
      ins.querySelector('.path').textContent = nd.path;
    }
  });

  fetch('/api/graph').then(function (r) { return r.json(); }).then(function (d) {
    g.load(d);
    S.files = d.stats.files;
    $('g-nodes').textContent = d.stats.files;
    $('g-links').textContent = d.stats.links;
    $('t-files').textContent = d.stats.files;
  });

  setInterval(function () { g.step(); g.draw(); }, FRAME_MS);

  /* Hydrate the stack from the journal, so a reloaded HUD shows what already
     happened instead of an empty panel that looks like nothing ever ran. */
  fetch('/api/journal').then(function (r) { return r.json(); }).then(function (d) {
    (d.entries || []).slice(-14).forEach(function (e) {
      cardFor(e.action);
      cardState(e.action.id,
        e.outcome === 'executed' ? 'DONE'
        : e.outcome === 'refused' ? 'REFUSED'
        : e.outcome.toUpperCase(), true);
    });
  });

  /* ── command bar ────────────────────────────────────────────────── */

  function send() {
    const box = $('cmd');
    const text = box.value.trim();
    if (!text) return;
    box.value = '';
    post('/api/command', { text: text });
  }
  $('send-btn').onclick = send;
  $('cmd').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') send();
  });
  $('undo-btn').onclick = function () {
    post('/api/undo').then(function (r) {
      if (r) $('caption').textContent = (r.message || '').toUpperCase();
    });
  };
  $('journal-btn').onclick = function () { $('cmd').value = 'journal'; send(); };
  $('brief-btn').onclick = function () { $('cmd').value = 'brief'; send(); };
  $('mic-btn').onclick = function () {
    $('caption').textContent = 'THE MICROPHONE ARRIVES IN STEP 4 — IT LIVES IN PYTHON, NOT THE BROWSER';
  };

  $('cmd').focus();
})();
