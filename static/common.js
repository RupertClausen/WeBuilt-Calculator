// Shared helpers and the top bar for every page.
const $ = s => document.querySelector(s);

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function money(n, cur = 'THB') {
  const sign = n < 0 ? '-' : '';
  return sign + (cur === 'THB' ? '฿' : cur + ' ') + Math.abs(+n || 0).toLocaleString('en', {
    minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function bangkokDate(iso, withTime = true) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleString('en-GB', { timeZone: 'Asia/Bangkok', day: '2-digit', month: 'short',
    year: 'numeric', ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}) });
}

// Every change sent from these pages carries X-WeBuilt: 1. The team server
// refuses changes without it, which stops other websites from posting to the
// app using your login.
(function () {
  const real = window.fetch;
  window.fetch = (url, opts = {}) => {
    const method = (opts.method || 'GET').toUpperCase();
    const same = typeof url === 'string' && url.startsWith('/');
    if (same && method !== 'GET' && method !== 'HEAD') {
      opts.headers = new Headers(opts.headers || {});
      if (!opts.headers.has('X-WeBuilt')) opts.headers.set('X-WeBuilt', '1');
    }
    return real(url, opts).then(r => {
      if (r.status === 401 && same && !url.startsWith('/api/login')) {
        location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
      }
      return r;
    });
  };
})();

async function api(url, opts = {}) {
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    delete opts.json;
  }
  const r = await fetch(url, opts);
  const data = r.headers.get('content-type')?.includes('json') ? await r.json() : null;
  if (!r.ok) throw new Error(data && typeof data.detail === 'string' ? data.detail
    : `Request failed (${r.status})`);
  return { data, res: r };
}

let ME = null;
async function loadMe() {
  if (!ME) ME = (await api('/api/me')).data;
  return ME;
}

async function renderNav(active) {
  const me = await loadMe();
  const nav = document.createElement('nav');
  nav.className = 'topnav';
  const link = (href, label, key) =>
    `<a class="link${active === key ? ' on' : ''}" href="${href}">${label}</a>`;
  const isAdmin = !me.server_mode || me.user?.role === 'admin';
  nav.innerHTML = `<a class="brand" href="/">WeBuilt Quote</a>
    ${link('/', 'Quote', 'quote')}${link('/orders', 'Orders', 'orders')}
    ${isAdmin ? link('/admin', 'Admin', 'admin') : ''}
    ${me.user ? `<span class="me"><span>${esc(me.user.display_name || me.user.username)}</span>
      <a href="/account">Account</a><button id="logout">Log out</button></span>` : ''}`;
  document.body.prepend(nav);
  const lo = nav.querySelector('#logout');
  if (lo) lo.onclick = async () => { await api('/api/logout', { method: 'POST' }); location.href = '/login'; };
  return me;
}
