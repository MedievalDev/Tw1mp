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
      return tr;
    }));
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
    if (tick % 5 === 0) { try { renderAccounts(await getJSON('accounts')); } catch (e) {} }
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
}));
$('#acc-filter').addEventListener('input', applyAccountFilter);
$('#autorefresh').addEventListener('change', (e) => (e.target.checked ? start() : stop()));

start();
