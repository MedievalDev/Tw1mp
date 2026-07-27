'use strict';

// TW1MP dashboard - polls the server's JSON API and renders it. No deps.
// Reached through an SSH tunnel to the server's localhost web API.

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const INTERVAL = 3000;
let tick = 0;
let timer = null;

// ---- helpers --------------------------------------------------------
function td(text, cls) {
  const e = document.createElement('td');
  if (cls) e.className = cls;
  e.textContent = (text === null || text === undefined || text === '') ? '—' : String(text);
  return e;
}
function badge(text, cls) {
  const s = document.createElement('span');
  s.className = 'badge ' + cls;
  s.textContent = text;
  return s;
}
function setBody(id, rows) {
  $('#' + id + ' tbody').replaceChildren(...rows);
}
function fmtTime(iso) {                    // ...T00:31:37 -> 00:31:37
  if (!iso) return '—';
  const m = /T(\d\d:\d\d:\d\d)/.exec(iso);
  return m ? m[1] : String(iso);
}
function fmtDateTime(iso) {                // -> 2026-07-23 00:31
  if (!iso) return 'nie';
  return String(iso).replace('T', ' ').slice(0, 16);
}
function fmtUptime(startedIso) {
  const t = Date.parse(startedIso);
  if (isNaN(t)) return '—';
  let s = Math.max(0, (Date.now() - t) / 1000);
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  return m + 'm';
}
function shortTown(name) {                 // Net_T_01#translate..._Channel_01 -> Net_T_01 (Ch 01)
  if (!name) return '—';
  const base = String(name).split('#')[0];
  const ch = /Channel_(\d+)/.exec(name);
  return ch ? base + ' (Ch ' + ch[1] + ')' : base;
}

function setOnline(ok) {
  const dot = $('#dot');
  dot.classList.toggle('on', ok);
  dot.classList.toggle('off', !ok);
}

async function getJSON(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error(path + ' -> ' + r.status);
  return r.json();
}

// Writes carry a custom header; cross-origin pages cannot set it without a
// preflight the server never approves, so a random web page open in the same
// browser cannot drive the panel through the tunnel.
async function postJSON(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-TW1MP-Admin': '1' },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || (path + ' -> ' + r.status));
  return data;
}

let toastTimer = null;
function toast(message, isError) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.toggle('err', !!isError);
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
}

let ADMIN = false;
function button(label, cls, onClick) {
  const b = document.createElement('button');
  b.className = 'btn ' + (cls || '');
  b.textContent = label;
  b.addEventListener('click', onClick);
  return b;
}

async function act(fn, refresh) {
  try {
    const res = await fn();
    toast(res.message || 'OK');
    if (refresh) refresh();
  } catch (e) {
    toast(String(e.message || e), true);
  }
}

// ---- renderers ------------------------------------------------------
function renderStatus(s) {
  $('#title').textContent = s.server || 'TW1MP';
  $('#version').textContent = s.version ? 'v' + s.version : '';
  $('#s-players').textContent = s.players ?? 0;
  $('#s-games').textContent = s.games ?? 0;
  $('#s-uptime').textContent = fmtUptime(s.started);
}

function renderLive(d) {
  const players = d.players || {};
  const names = Object.keys(players);
  setBody('players', names.map((n) => {
    const p = players[n];
    const tr = document.createElement('tr');
    tr.append(td(n, 'name'), td(shortTown(p.town)), td(p.game || '—'),
              td(p.pos || '—'), td(fmtTime(p.loginTime)));
    if (ADMIN) {
      const cell = document.createElement('td');
      cell.className = 'actions admin-only';
      cell.append(
        button('Kick', '', () => {
          if (confirm(n + ' vom Server trennen?')) {
            act(() => postJSON('admin/kick', { name: n }), poll);
          }
        }),
        button('Ban', 'danger', () => banPlayer(n)));
      tr.append(cell);
    }
    return tr;
  }));
  $('#players-empty').style.display = names.length ? 'none' : 'block';

  const towns = d.towns || {};
  setBody('towns', Object.keys(towns).map((name) => {
    const t = towns[name];
    const users = (t.users || []).length;
    const tr = document.createElement('tr');
    tr.append(td(shortTown(name), 'name'),
              td(t.maxUsers ? users + ' / ' + t.maxUsers : String(users)),
              td((t.games || []).length));
    return tr;
  }));

  const games = d.games || [];
  setBody('games', games.map((g) => {
    const tr = document.createElement('tr');
    tr.append(td(g.name, 'name'), td(g.host), td(g.status),
              td((g.users || []).length), td(g.hasPassword ? 'ja' : '—'),
              td(shortTown(g.town)));
    return tr;
  }));
  $('#games-empty').style.display = games.length ? 'none' : 'block';
}

let accountsCache = [];
function renderAccounts(a) {
  accountsCache = a.accounts || [];
  const n = a.count ?? accountsCache.length;
  $('#s-accounts').textContent = n;
  $('#acc-count').textContent = n;
  applyAccountFilter();

  const bans = a.bans || [];
  setBody('bans', bans.map((b) => {
    const tr = document.createElement('tr');
    tr.append(td(b.kind), td(b.value), td(fmtDateTime(b.ts)), td(b.reason || '—'));
    if (ADMIN) {
      const cell = document.createElement('td');
      cell.className = 'actions admin-only';
      cell.appendChild(button('Entbannen', '', () => act(
        () => postJSON('admin/unban', { kind: b.kind, value: b.value }),
        loadAccounts)));
      tr.append(cell);
    }
    return tr;
  }));
  $('#bans-empty').style.display = bans.length ? 'none' : 'block';
}
function applyAccountFilter() {
  const q = $('#acc-filter').value.trim().toLowerCase();
  setBody('accounts', accountsCache
    .filter((x) => !q || x.name.toLowerCase().includes(q))
    .map((x) => {
      const tr = document.createElement('tr');
      const st = document.createElement('td');
      st.appendChild(badge(x.online ? 'online' : 'offline', x.online ? 'online' : 'offline'));
      if (x.banned) { st.append(' '); st.appendChild(badge('gebannt', 'banned')); }
      tr.append(td(x.name, 'name'), td(fmtDateTime(x.lastLogin)), st);
      if (ADMIN) {
        const cell = document.createElement('td');
        cell.className = 'actions admin-only';
        cell.append(button('Charakter', '', () => showCharacter(x.name)));
        if (!x.banned) cell.append(button('Ban', 'danger', () => banPlayer(x.name)));
        tr.append(cell);
      }
      return tr;
    }));
}

function banPlayer(name) {
  const reason = prompt('Grund für den Bann von ' + name + ' (optional):');
  if (reason === null) return;               // cancelled
  act(() => postJSON('admin/ban', { name: name, reason: reason }), () => {
    poll(); loadAccounts();
  });
}

async function loadAccounts() {
  try { renderAccounts(await getJSON('accounts')); } catch (e) {}
}

// ---- guilds ---------------------------------------------------------
let guildNames = [];

async function loadGuilds() {
  let data;
  try { data = await getJSON('guilds'); } catch (e) { return; }
  const guilds = data.guilds || [];
  guildNames = guilds.map((g) => g.name);

  setBody('guilds', guilds.map((g) => {
    const tr = document.createElement('tr');
    tr.append(td(g.name, 'name'), td(g.tag || '—'),
              td(g.members.length), td(g.online || 0),
              td(fmtDateTime(g.founded)));
    if (ADMIN) {
      const cell = document.createElement('td');
      cell.className = 'actions admin-only';
      cell.appendChild(button('Auflösen', 'danger', () => {
        if (confirm('Gilde "' + g.name + '" wirklich auflösen?')) {
          act(() => postJSON('admin/guild/delete', { guild: g.name }), loadGuilds);
        }
      }));
      tr.append(cell);
    }
    return tr;
  }));
  $('#guilds-empty').style.display = guilds.length ? 'none' : 'block';

  // one row per player with a guild dropdown
  const assigned = [];
  guilds.forEach((g) => g.members.forEach((m) => assigned.push([m, g.name])));
  (data.unassigned || []).forEach((m) => assigned.push([m, '']));
  assigned.sort((a, b) => a[0].localeCompare(b[0]));

  setBody('members', assigned.map(([player, guild]) => {
    const tr = document.createElement('tr');
    const cell = document.createElement('td');
    if (ADMIN) {
      const sel = document.createElement('select');
      sel.className = 'qty';
      sel.style.width = '160px';
      const none = document.createElement('option');
      none.value = ''; none.textContent = '— keine —';
      sel.appendChild(none);
      guildNames.forEach((n) => {
        const o = document.createElement('option');
        o.value = n; o.textContent = n;
        sel.appendChild(o);
      });
      sel.value = guild;
      sel.addEventListener('change', () => act(
        () => postJSON('admin/guild/member', { name: player, guild: sel.value }),
        loadGuilds));
      cell.appendChild(sel);
    } else {
      cell.textContent = guild || '—';
    }
    tr.append(td(player, 'name'), cell);
    return tr;
  }));
}

function createGuild() {
  const name = $('#g-name').value.trim();
  if (!name) { toast('Gildenname fehlt.', true); return; }
  act(() => postJSON('admin/guild/create', {
    guild: name, tag: $('#g-tag').value.trim(),
  }), () => { $('#g-name').value = ''; $('#g-tag').value = ''; loadGuilds(); });
}

// ---- character view / item editor -----------------------------------
let charState = { name: null, form: null, edits: {} };

async function showCharacter(name) {
  $$('nav button').forEach((x) => x.classList.remove('active'));
  $$('.tab').forEach((x) => x.classList.remove('active'));
  $('#tab-char').classList.add('active');
  $('#char-name').textContent = name;
  $('#char-meta').textContent = 'lädt…';
  setBody('charitems', []);
  $('#char-empty').style.display = 'none';
  charState = { name: name, form: null, edits: {} };
  try {
    const c = await getJSON('character?name=' + encodeURIComponent(name));
    const items = c.items || [];
    charState.form = c.form;
    $('#char-meta').textContent = c.form
      ? `Map: ${c.form} · ${(c.size / 1024).toFixed(1)} KB · ${items.length} Einträge`
        + (c.modded ? ' · bearbeitete Kopie aktiv' : '')
      : (c.note || 'kein Charakter');
    setBody('charitems', items.map((it) => {
      const tr = document.createElement('tr');
      const value = document.createElement('td');
      if (ADMIN && it.editable) {
        const input = document.createElement('input');
        input.className = 'qty';
        input.type = 'number';
        input.min = '0';
        input.max = String(it.max);
        input.value = it.quantity;
        input.addEventListener('input', () => {
          const v = parseInt(input.value, 10);
          const changed = !isNaN(v) && v !== it.quantity;
          input.classList.toggle('dirty', changed);
          if (changed) charState.edits[it.offset] = v;
          else delete charState.edits[it.offset];
        });
        value.appendChild(input);
      } else {
        value.textContent = it.quantity;
      }
      tr.append(td(it.name, 'name'), td(it.category), value,
                td(it.kind === 'class' ? 'Klasse' : 'Anzahl'));
      return tr;
    }));
    $('#char-empty').style.display = items.length ? 'none' : 'block';
  } catch (e) {
    $('#char-meta').textContent = 'Fehler: ' + (e.message || e);
  }
}

function saveCharacter() {
  const n = Object.keys(charState.edits).length;
  if (!charState.name || !charState.form) return;
  if (!n) { toast('Nichts geändert.'); return; }
  act(() => postJSON('admin/item', {
    name: charState.name, form: charState.form, changes: charState.edits,
  }), () => showCharacter(charState.name));
}

function revertCharacter() {
  if (!charState.name || !charState.form) return;
  if (!confirm('Alle Änderungen an ' + charState.name
               + ' verwerfen und das Original wieder aktivieren?')) return;
  act(() => postJSON('admin/character/revert', {
    name: charState.name, form: charState.form,
  }), () => showCharacter(charState.name));
}

function renderLog(d) {
  const el = $('#log');
  const follow = $('#log-follow').checked;
  const frag = document.createDocumentFragment();
  (d.lines || []).forEach((ln) => {
    const div = document.createElement('div');
    const m = /\s(DEBUG|INFO|WARNING|ERROR)\s/.exec(ln);
    if (m) div.className = 'lvl-' + m[1];
    div.textContent = ln;
    frag.appendChild(div);
  });
  el.replaceChildren(frag);
  if (follow) el.scrollTop = el.scrollHeight;
}

// ---- poll loop ------------------------------------------------------
async function poll() {
  let ok = false;
  try { renderStatus(await getJSON('status')); ok = true; } catch (e) { /* offline */ }
  setOnline(ok);
  if (ok) {
    try { renderLive(await getJSON('debug?lists=player+town+game')); } catch (e) {}
    try { renderLog(await getJSON('log')); } catch (e) {}
    if (tick % 5 === 0) {
      await loadAccounts();
      if ($('#tab-guilds').classList.contains('active')) await loadGuilds();
    }
    $('#updated').textContent = 'Aktualisiert ' + new Date().toLocaleTimeString();
  } else {
    $('#updated').textContent = 'Keine Verbindung – läuft der SSH-Tunnel?';
  }
  tick++;
}

function start() { if (!timer) { poll(); timer = setInterval(poll, INTERVAL); } }
function stop() { if (timer) { clearInterval(timer); timer = null; } }

// ---- ui wiring ------------------------------------------------------
$$('nav button').forEach((b) => b.addEventListener('click', () => {
  $$('nav button').forEach((x) => x.classList.remove('active'));
  $$('.tab').forEach((x) => x.classList.remove('active'));
  b.classList.add('active');
  $('#tab-' + b.dataset.tab).classList.add('active');
  if (b.dataset.tab === 'guilds') loadGuilds();
}));
$('#g-create').addEventListener('click', createGuild);
$('#g-name').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') createGuild();
});
$('#acc-filter').addEventListener('input', applyAccountFilter);
$('#autorefresh').addEventListener('change', (e) => (e.target.checked ? start() : stop()));

$('#char-save').addEventListener('click', saveCharacter);
$('#char-revert').addEventListener('click', revertCharacter);
$('#char-back').addEventListener('click', () => {
  $('#tab-char').classList.remove('active');
  const btn = document.querySelector('nav button[data-tab="accounts"]');
  btn.click();
});

function sendBroadcast() {
  const input = $('#bc-text');
  const text = input.value.trim();
  if (!text) return;
  act(() => postJSON('admin/broadcast', { text: text }).then((r) => ({
    message: `Broadcast an ${r.recipients} Spieler gesendet.`,
  })));
  input.value = '';
}
$('#bc-send').addEventListener('click', sendBroadcast);
$('#bc-text').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendBroadcast();
});

// Ask the server what it allows, then show the admin controls if enabled.
getJSON('capabilities').then((c) => {
  ADMIN = !!c.admin;
  document.body.classList.toggle('admin', ADMIN);
}).catch(() => {}).finally(start);
