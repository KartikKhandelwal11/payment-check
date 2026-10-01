// Behaviour shared by the admin pages: drawer, motion, copy/share, forms, auto-refresh.
(function () {
  const M = window.Motion;  // motion.dev (vanilla); pages work without it
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let soft = false;         // true after an auto-refresh: don't replay the entrance
  try { soft = sessionStorage.getItem('pcSoft') === location.pathname + location.search; sessionStorage.removeItem('pcSoft'); } catch (e) {}
  const animateOk = M && !reduce && !soft;
  const ease = [0.22, 1, 0.36, 1];
  // Motion runs on animation frames. Where those are throttled (battery saver, a tab
  // opened in the background) an entrance could stall half way, so everything is
  // forced to its end state after a moment.
  const running = [];
  const finishers = [];
  const run = (...args) => { const c = M.animate(...args); running.push(c); return c; };
  setTimeout(() => {
    running.forEach((c) => { try { c.complete(); } catch (e) {} });
    finishers.forEach((f) => f());
  }, 1600);

  // ---------- entrance motion ----------
  if (animateOk) {
    const items = document.querySelectorAll('[data-animate]');
    if (items.length) {
      run(items, { opacity: [0, 1], transform: ['translateY(14px)', 'translateY(0)'] },
                { delay: M.stagger(0.05), duration: 0.5, easing: ease });
    }
    const bars = document.querySelectorAll('.bars .bar i');
    if (bars.length) {
      run(bars, { transform: ['scaleY(0)', 'scaleY(1)'] }, { delay: M.stagger(0.05, { start: 0.25 }), duration: 0.6, easing: ease });
    }
    const rows = document.querySelectorAll('[data-rows] tbody tr, [data-rows] > li');
    if (rows.length && rows.length < 60) {
      run(rows, { opacity: [0, 1] }, { delay: M.stagger(0.025, { start: 0.15 }), duration: 0.35 });
    }
  }
  document.documentElement.classList.remove('pre-anim');

  // ---------- numbers count up ----------
  const inr = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
  document.querySelectorAll('[data-count]').forEach((el) => {
    const target = Number(el.dataset.count);
    const money = el.dataset.money !== undefined;
    const fmt = (v) => (money ? '₹' : '') + inr.format(Math.round(v));
    if (!animateOk || !target) { el.textContent = fmt(target); return; }
    el.textContent = fmt(0);
    let finished = false;
    run(0, target, { duration: 1.1, easing: ease, onUpdate: (v) => { if (!finished) el.textContent = fmt(v); } });
    finishers.push(() => { finished = true; el.textContent = fmt(target); });
  });

  // ---------- drawer (phones) ----------
  const openBtn = document.getElementById('drawerOpen');
  const setDrawer = (open) => {
    document.body.classList.toggle('drawer-open', open);
    if (openBtn) openBtn.setAttribute('aria-expanded', String(open));
    if (open) { const first = document.querySelector('.sidebar a'); if (first) first.focus({ preventScroll: true }); }
  };
  if (openBtn) openBtn.addEventListener('click', () => setDrawer(true));
  ['drawerClose', 'scrim'].forEach((id) => { const el = document.getElementById(id); if (el) el.addEventListener('click', () => setDrawer(false)); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setDrawer(false); });

  // ---------- top bar gets a glass background once scrolled ----------
  const topbar = document.getElementById('topbar');
  const onScroll = () => topbar && topbar.classList.toggle('scrolled', window.scrollY > 8);
  window.addEventListener('scroll', onScroll, { passive: true }); onScroll();

  // ---------- segmented tabs: sliding indicator ----------
  document.querySelectorAll('.tabs').forEach((tabs) => {
    const ind = document.createElement('span'); ind.className = 'ind'; tabs.prepend(ind);
    const place = (el, instant) => {
      if (!el) return;
      if (instant) ind.style.transition = 'none';
      ind.style.width = el.offsetWidth + 'px';
      ind.style.transform = 'translateX(' + (el.offsetLeft - 4) + 'px)';
      if (instant) { ind.offsetWidth; ind.style.transition = ''; }
    };
    place(tabs.querySelector('.on'), true);
    window.addEventListener('resize', () => place(tabs.querySelector('.on'), true));
    tabs.addEventListener('click', (e) => {
      const t = e.target.closest('a, button'); if (!t || !tabs.contains(t)) return;
      tabs.querySelectorAll('.on').forEach((x) => x.classList.remove('on'));
      t.classList.add('on'); place(t);
    });
  });

  // ---------- copy / share ----------
  function flash(btn, text) {
    if (!btn.dataset.label) btn.dataset.label = btn.innerHTML;
    btn.textContent = text;
    setTimeout(() => { btn.innerHTML = btn.dataset.label; }, 1500);
  }
  // navigator.clipboard only works on https/localhost, so fall back to execCommand
  // when the dashboard is opened over the local network (http://192.168…).
  function copy(value, btn) {
    const done = () => flash(btn, btn.classList.contains('icon-btn') ? '✓' : 'Copied');
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(value).then(done); return; }
    const ta = document.createElement('textarea');
    ta.value = value; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } finally { ta.remove(); }
  }
  document.querySelectorAll('[data-copy]').forEach((b) =>
    b.addEventListener('click', (e) => { e.stopPropagation(); copy(b.dataset.copy, b); }));
  document.querySelectorAll('.copy-field input').forEach((i) => i.addEventListener('focus', () => i.select()));
  document.querySelectorAll('[data-share]').forEach((b) => {
    if (!navigator.share) { b.hidden = true; return; }
    b.addEventListener('click', () => navigator.share({ text: b.dataset.shareText || '', url: b.dataset.share }).catch(() => {}));
  });

  // Whole table row opens the order.
  document.querySelectorAll('tr[data-href]').forEach((tr) => tr.addEventListener('click', (e) => {
    if (e.target.closest('a, button, input, form')) return;
    location.href = tr.dataset.href;
  }));

  // ---------- new payment link form ----------
  document.querySelectorAll('form.order-form').forEach((form) => {
    const amount = form.querySelector('input[name=amount]');
    const chips = form.querySelectorAll('.chip');
    const sync = () => chips.forEach((c) => c.setAttribute('aria-pressed', String(c.dataset.amount === amount.value.replace(/,/g, ''))));
    chips.forEach((c) => c.addEventListener('click', () => {
      amount.value = c.dataset.amount; sync(); amount.focus();
      if (M && !reduce) M.animate(amount, { transform: ['scale(1.02)', 'scale(1)'] }, { duration: 0.25 });
    }));
    amount.addEventListener('input', () => { amount.value = amount.value.replace(/[^\d.,]/g, ''); sync(); });
    sync();

    // Remember the last member used, so the usual one is already selected.
    const radios = form.querySelectorAll('input[name=member_id][type=radio]');
    if (radios.length && !form.querySelector('input[name=member_id]:checked')) {
      let last = null;
      try { last = localStorage.getItem('lastMember'); } catch (e) {}
      const pick = [...radios].find((r) => r.value === last) || [...radios].find((r) => r.dataset.online === '1') || radios[0];
      pick.checked = true;
    }
    form.addEventListener('submit', () => {
      const m = form.querySelector('input[name=member_id]:checked');
      if (m) { try { localStorage.setItem('lastMember', m.value); } catch (e) {} }
      const btn = form.querySelector('button[type=submit]');
      if (btn) { btn.disabled = true; btn.textContent = 'Creating…'; }
    });
  });

  // ---------- auto-refresh ----------
  // Reload every 20 s on plain GET pages, never while someone is typing or has a form half filled.
  if (window.PC_AUTO_REFRESH) {
    setInterval(() => {
      if (document.visibilityState !== 'visible' || document.body.classList.contains('drawer-open')) return;
      if (document.querySelector('dialog[open]')) return;  // never reload under an open dialog
      const a = document.activeElement;
      if (a && /INPUT|SELECT|TEXTAREA/.test(a.tagName) && !a.readOnly) return;
      const typed = [...document.querySelectorAll('input:not([type=hidden]):not([type=radio]):not([readonly]):not([data-keep])')].some((i) => i.value && i.defaultValue !== i.value);
      if (typed) return;
      try { sessionStorage.setItem('pcSoft', location.pathname + location.search); } catch (e) {}
      location.reload();
    }, 20000);
  }
})();
