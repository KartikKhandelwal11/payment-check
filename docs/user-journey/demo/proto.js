/* Prototype layer: makes the captured pages click through with demo data.
   Nothing is sent anywhere and nothing is saved. */
(function () {
  var P = window.PROTO || {}, page = P.page || '', isAdmin = page.indexOf('admin') === 0;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var go = function (to, wait) { setTimeout(function () { location.href = to; }, wait || 0); };
  var get = function (k, d) { try { return sessionStorage.getItem(k) || d; } catch (e) { return d; } };
  var set = function (k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} };
  var hasBar = !!$('.bar');

  // ---- toast ----
  var toastEl, toastT;
  function toast(msg, ms) {
    if (!toastEl) { toastEl = document.createElement('div'); toastEl.className = 'proto-toast' + (hasBar ? ' up' : ''); toastEl.setAttribute('role', 'status'); document.body.appendChild(toastEl); }
    toastEl.textContent = msg; toastEl.classList.add('on');
    clearTimeout(toastT); toastT = setTimeout(function () { toastEl.classList.remove('on'); }, ms || 3600);
  }

  // ---- floating "Prototype" pill ----
  var pill = document.createElement('div');
  if (isAdmin) {
    pill.className = 'proto-pill';
    pill.innerHTML = '<div class="proto-menu" hidden><p>Clickable prototype with demo data. Nothing is saved.</p>' +
      '<a href="login.html">Customer wallet</a><a href="admin-login.html">Admin dashboard</a><a href="../index.html#demo">Back to the journey</a></div>' +
      '<button type="button" aria-expanded="false">Prototype</button>';
    document.body.appendChild(pill);
    var menu = $('.proto-menu', pill), pb = $('button', pill);
    pb.addEventListener('click', function () { menu.hidden = !menu.hidden; pb.setAttribute('aria-expanded', String(!menu.hidden)); });
    document.addEventListener('click', function (e) { if (!pill.contains(e.target)) menu.hidden = true; });
  } else {
    pill.className = 'proto-foot' + (hasBar ? ' up' : '');
    pill.innerHTML = '<b>Prototype with demo data</b><span>Nothing is saved.</span><div><a href="admin-login.html">Open the admin dashboard</a><a href="../index.html#demo">Back to the journey</a></div>';
    document.body.appendChild(pill);
  }

  // ---- links that have no page in the prototype ----
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href="#proto"]');
    if (a) { e.preventDefault(); toast('Not part of the prototype. It works in the real app.'); }
  });

  // ---- customer: which screenshot gets uploaded ----
  var SHOTS = [['ok', 'Correct screenshot', 'pay-ready.html'], ['amount', 'Wrong amount', 'pay-wrong-amount.html'], ['noutr', 'UTR cut off', 'pay-no-utr.html'],
               ['pending', 'Payment pending', 'pay-pending.html'], ['old', 'Old screenshot', 'pay-old.html']];
  var proof = $('#proofForm');
  function upload() {
    var k = get('protoShot', 'ok'), t = SHOTS.filter(function (s) { return s[0] === k; })[0] || SHOTS[0];
    var r = $('#reading'); if (r) r.hidden = false;
    go(t[2], 1400);
  }
  if (proof) {
    var box = document.createElement('div'); box.className = 'proto-pick';
    box.innerHTML = '<span><b>Prototype:</b> which screenshot does the customer upload?</span><div></div><small>Then press the upload button. No real file is needed.</small>';
    SHOTS.forEach(function (s) {
      var b = document.createElement('button'); b.type = 'button'; b.textContent = s[1]; b.setAttribute('aria-pressed', String(get('protoShot', 'ok') === s[0]));
      b.addEventListener('click', function () { set('protoShot', s[0]); [].forEach.call(box.querySelectorAll('button'), function (x) { x.setAttribute('aria-pressed', String(x === b)); }); });
      $('div', box).appendChild(b);
    });
    var old = $('.demo'); (old && old.parentNode ? old.parentNode : proof.parentNode).insertBefore(box, old || proof.nextSibling);
    document.addEventListener('click', function (e) {
      var l = e.target.closest && e.target.closest('label[for="proofFile"]');
      if (l) { e.preventDefault(); upload(); }
    }, true);
    proof.addEventListener('submit', function (e) { e.preventDefault(); upload(); });
  }

  // ---- forms ----
  document.addEventListener('submit', function (e) {
    var f = e.target, act = f.getAttribute('action') || '';
    if (f === proof) return;
    e.preventDefault();
    if (/\/wallet\/login$/.test(act)) return go('wallet.html');
    if (/\/wallet\/logout$/.test(act)) return go('login.html');
    if (/\/wallet\/add$/.test(act)) return go('pay.html');
    if (/\/proceed$/.test(act)) return go('pay-waiting.html');
    if (/\/pay\/\w+\/cancel$/.test(act)) return go('wallet.html');
    if (/\/pay\/\w+\/review$/.test(act)) return toast('In the real app this goes to the team, under "Payments to check" in the admin dashboard.', 5000);
    if (/\/admin\/login$/.test(act)) return go('admin.html');
    if (/\/admin\/logout$/.test(act)) return go('admin-login.html');
    if (/\/approve$/.test(act)) return toast('Prototype: in the real app the wallet is credited now and this card disappears.', 5000);
    if (/\/reject$/.test(act)) return toast('Prototype: in the real app the customer now sees the reason and this card disappears.', 5000);
    if (/\/ask$/.test(act)) return toast('Prototype: in the real app the customer is asked to upload again.', 5000);
    if (/simulate-sms$/.test(act)) return toast('Prototype: in the real app this pretends the bank SMS arrived and credits a matching order.', 5000);
    if (/simulate-alert$/.test(act)) return toast('Prototype: in the real app this pretends a UPI app alert arrived. It never adds money.', 5000);
    if (/\/admin\/statements$/.test(act)) return toast('Prototype: in the real app the statement is checked row by row and a result card appears below.', 5000);
    toast('Prototype: this button works in the real app. The demo data here does not change.');
  }, true);

  // ---- customer flow: the bank side happens by itself ----
  if (page === 'wallet.html' || page === 'wallet-after.html') {
    var amt = $('#amount'); if (amt) { amt.value = '500'; amt.addEventListener('input', function () { toast('The prototype always uses ₹500.'); amt.value = '500'; }); }
  }
  if (page === 'login.html') { var n = $('input[name="name"]'), ph = $('input[name="phone"]'); if (n && !n.value) n.value = 'Aman Verma'; if (ph && !ph.value) ph.value = '9876543210'; }
  if (page === 'admin-login.html') { var pw = $('input[name="password"]'); if (pw) pw.value = 'demo123'; }
  if (page === 'pay-waiting.html') { toast('Prototype: in a moment the UPI app alert reaches the receiving phone…', 3600); go('pay-received.html', 4200); }
  if (page === 'pay-received.html') { toast('Prototype: now HDFC\'s SMS arrives with the same UTR and amount…', 3600); go('pay-paid.html', 4200); }
  if (page === 'pay-paid.html') toast('Exact match: the wallet is credited.', 3000);
})();
