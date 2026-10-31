"""Optional HTTP status API.

Serves a JSON status document and, if enabled, debug listings and
playerdata downloads. Static files from the Web/ folder are served when
present (index.html for '/').
"""

import collections
import datetime
import http.server
import json
import logging
import os
import re
import socketserver
import time
from urllib.parse import unquote, urlparse

from . import __version__

log = logging.getLogger('tw1mp.web')

# In-memory tail of the server log, exposed at /log for the dashboard.
_LOG_BUFFER = collections.deque(maxlen=500)


class _RingLogHandler(logging.Handler):
    """Keeps the most recent formatted log lines in a bounded deque."""

    def emit(self, record):
        try:
            _LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


def _install_log_capture():
    root = logging.getLogger('tw1mp')
    if not any(isinstance(h, _RingLogHandler) for h in root.handlers):
        handler = _RingLogHandler()
        handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)-7s %(name)s: %(message)s'))
        root.addHandler(handler)

_MIME_BY_EXT = {
    '.js': 'text/javascript',
    '.html': 'text/html',
    '.css': 'text/css',
    '.json': 'application/json',
    '.ico': 'image/x-icon',
    '.png': 'image/png',
    '.txt': 'text/plain',
}
_MIME_JSON = 'application/json'
_MIME_BINARY = 'application/octet-stream'

# Settings the dashboard may read and write, mirroring the desktop panel's
# Settings tab. (section, key, label, type); type is str/int/bool/motd.
# bot_password is deliberately absent - it is a secret, not a tunable.
SETTINGS_SPEC = [
    ('Identität', [
        ('Server', 'name', 'Servername', 'str'),
        ('Server', 'title', 'Titel (für Clients sichtbar)', 'str'),
        ('Server', 'motd', 'Willkommensnachricht (MOTD)', 'motd'),
    ]),
    ('Lobby', [
        ('Server', 'max_channel_users', 'Max. Nutzer pro Kanal', 'int'),
        ('Server', 'channels_per_map', 'Kanäle pro Map (1-20)', 'int'),
        ('Server', 'maps', 'Maps (kommagetrennt)', 'str'),
        ('Server', 'guild_price', 'Goldpreis für Gildengründung (0 = frei)',
         'int'),
        ('Server', 'send_nops', 'Keepalive /nop alle 3s senden', 'bool'),
    ]),
    ('Accounts & Login', [
        ('Server', 'auto_register',
         'Unbekannte Nutzer beim Login automatisch registrieren', 'bool'),
        ('Server', 'allow_registration', 'Neue Accounts zulassen', 'bool'),
        ('Server', 'bind_serial', 'Ein Account pro Seriennummer', 'bool'),
        ('Server', 'allow_any_login',
         'JEDEN Login akzeptieren (nur Debug!)', 'bool'),
        ('Server', 'compat_login_errors',
         'Kompatibler Login-Fehlertext (TESTERROR)', 'bool'),
    ]),
    ('Admin-Bot  (immer in der Lobby, folgt den Spielern)', [
        ('Server', 'bot_enabled', 'Admin-Bot laufen lassen', 'bool'),
        ('Server', 'bot_name', 'Bot-Name', 'str'),
        ('Server', 'bot_website', 'Website, die der Bot nennt', 'str'),
        ('Server', 'bot_discord', 'Discord-Einladung (leer = keine)', 'str'),
        ('Server', 'bot_color',
         'Bot-Chatfarbe <0xAARRGGBB> (leer = keine)', 'str'),
    ]),
    ('Netzwerk  (wirkt erst nach Neustart)', [
        ('Server', 'bind', 'Bind-Adresse (leer = alle Interfaces)', 'str'),
        ('Server', 'port', 'Port', 'int'),
    ]),
    ('Web-Panel', [
        ('Web', 'debug_api', 'Debug-/Status-API', 'bool'),
        ('Web', 'admin_api', 'Admin-Aktionen erlauben', 'bool'),
        ('Web', 'playerdata_download', 'Charakter-Download erlauben', 'bool'),
        ('Web', 'enabled', 'Web-Panel aktiv (Neustart)', 'bool'),
        ('Web', 'port', 'Web-Port (Neustart)', 'int'),
        ('Web', 'public_port',
         'Öffentlicher Datenport, 0 = aus (Neustart)', 'int'),
    ]),
]

# Keys that only take effect after a full restart (bound sockets / start-up
# wiring); the rest apply immediately via config.reload().
_RESTART_KEYS = {('Server', 'bind'), ('Server', 'port'),
                 ('Server', 'channels_per_map'), ('Server', 'maps'),
                 ('Server', 'bot_enabled'), ('Server', 'bot_name'),
                 ('Web', 'enabled'), ('Web', 'port'), ('Web', 'public_port')}


def _iso_utc(ts):
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    return dt.replace(tzinfo=None).isoformat() + 'Z'


class WebServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, core):
        _install_log_capture()
        host = core.config.web_bind or core.config.bind
        log.info('Web status server starting on %s port %s',
                 host or '*', core.config.web_port)
        super().__init__((host, core.config.web_port), WebApiServe)
        self.core = core


class WebApiServe(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        log.debug('%s ' + fmt, self.client_address[0], *args)

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', _MIME_JSON)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message, code=404):
        self._send_json({'error': message}, code)

    def _send_json_public(self, obj):
        """A public, read-only payload: adds CORS so the community website can
        fetch it. Only ever used for the whitelisted /public/* endpoints -
        never for account, character or admin data."""
        body = json.dumps(obj).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', _MIME_JSON)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        core = self.server.core
        cfg = core.config
        pres = urlparse(self.path)
        path = unquote(pres.path).strip('/').lower()
        qprops = {}
        if pres.query:
            for prp in pres.query.split('&'):
                (k, _, v) = prp.partition('=')
                qprops[unquote(k)] = unquote(v)

        # -- public, read-only endpoints (for the community website) ------
        # Whitelisted subset only: guilds and ranking. No account roster, no
        # character data, no last-login/ban info. Safe to expose publicly.
        if path == 'public/guilds':
            online = set(core.debug_dict_players())
            guilds = [{'name': g['name'], 'tag': g['tag'],
                       'founded': g['founded'], 'members': g['members'],
                       'memberCount': len(g['members']),
                       'online': sum(1 for m in g['members'] if m in online)}
                      for g in core.db.guild_standings()]
            self._send_json_public({'guilds': guilds,
                                    'generated': _iso_utc(time.time())})
            return

        if path == 'public/ranking':
            # The character blob has no readable XP/gold field yet, so there
            # is nothing real to rank on. Kept as a stable shape the website
            # can already consume; filled once the fields are identified.
            self._send_json_public({'available': False,
                                    'note': 'Rangliste wird vorbereitet.',
                                    'players': [],
                                    'generated': _iso_utc(time.time())})
            return

        if path == 'public/summary':
            with core.state.lock:
                players = sum(1 for n in core.state.activeUsers
                              if n != cfg.bot_name)
            self._send_json_public({
                'server': cfg.title,
                'playersOnline': players,
                'guilds': len(core.db.guild_standings()),
                'generated': _iso_utc(time.time())})
            return

        if path == 'status':
            with core.state.lock:
                players = len(core.state.activeUsers)
                games = sum(len(c.games)
                            for c in core.state.gameChannels.values())
            self._send_json({
                'server': cfg.title,
                'version': __version__,
                'started': _iso_utc(core.startTime),
                'players': players,
                'games': games,
            })
            return

        if path == 'debug':
            if not cfg.web_debug_api:
                self._send_error_json('Debug API is disabled', 403)
                return
            message = {'version': __version__,
                       'started': _iso_utc(core.startTime)}
            rqlst = qprops.get('lists', '').lower().split('+')
            if 'player' in rqlst:
                message['players'] = core.debug_dict_players()
            if 'town' in rqlst:
                message['towns'] = core.debug_dict_towns()
            if 'game' in rqlst:
                message['games'] = core.debug_arr_games()
            self._send_json(message)
            return

        if path == 'accounts':
            if not cfg.web_debug_api:
                self._send_error_json('Accounts API is disabled', 403)
                return
            db = core.db
            bans = db.list_bans()
            banned_names = {v for (k, v, *_) in bans if k == 'name'}
            online = set(core.debug_dict_players())
            accounts = []
            for name, last in db.list_users():
                accounts.append({
                    'name': name,
                    'lastLogin': last.isoformat() if hasattr(last, 'isoformat')
                    else (last or None),
                    'online': name in online,
                    'banned': name in banned_names,
                })
            self._send_json({
                'count': len(accounts),
                'accounts': accounts,
                'bans': [{'kind': k, 'value': v,
                          'ts': ts.isoformat() if hasattr(ts, 'isoformat')
                          else str(ts),
                          'reason': r} for (k, v, ts, r) in bans],
            })
            return

        if path == 'log':
            if not cfg.web_debug_api:
                self._send_error_json('Log API is disabled', 403)
                return
            self._send_json({'lines': list(_LOG_BUFFER)})
            return

        if path == 'guilds':
            if not cfg.web_debug_api:
                self._send_error_json('Guild API is disabled', 403)
                return
            online = set(core.debug_dict_players())
            guilds = core.db.guild_standings()
            for g in guilds:
                g['online'] = sum(1 for m in g['members'] if m in online)
            self._send_json({'guilds': guilds,
                             'unassigned': sorted(
                                 name for name, _ in core.db.list_users()
                                 if name not in core.db.member_guilds()
                                 and name != cfg.bot_name)})
            return

        if path == 'capabilities':
            self._send_json({'debug': bool(cfg.web_debug_api),
                             'admin': bool(cfg.web_admin_api)})
            return

        if path == 'character':
            if not cfg.web_admin_api:
                self._send_error_json('Admin API is disabled', 403)
                return
            name = qprops.get('name')
            if not name:
                self._send_error_json('name parameter required', 400)
                return
            forms = core.db.list_forms()
            want = qprops.get('form')
            blob = b''
            used = None
            modded = False
            for form in ([want] if want else forms):
                # Show (and edit) the modified variant when one exists, the
                # way the desktop panel does.
                mod = core.db.has_modded_playerdata(name, form)
                blob = core.db.get_playerdata(name, form, modded=mod)
                if blob:
                    used, modded = form, mod
                    break
            if not blob:
                self._send_json({'name': name, 'forms': forms, 'form': None,
                                 'items': [], 'modded': False,
                                 'note': 'no character stored'})
                return
            from . import chardata
            try:
                raw = chardata.decompress(blob)
                items = [{'name': s.name, 'quantity': s.quantity,
                          'offset': s.qty_offset,
                          'kind': s.kind, 'editable': s.editable,
                          'max': s.max_value,
                          'category': chardata.category(s.name)}
                         for s in chardata.parse_items(raw)]
            except chardata.CharDataError as exc:
                self._send_error_json(f'unreadable character: {exc}', 500)
                return
            self._send_json({'name': name, 'forms': forms, 'form': used,
                             'size': len(blob), 'modded': modded,
                             'items': items})
            return

        if path == 'config':
            if not cfg.web_admin_api:
                self._send_error_json('Admin API is disabled', 403)
                return
            self._send_json(self._config_document())
            return

        if path == 'snapshots':
            if not cfg.web_admin_api:
                self._send_error_json('Admin API is disabled', 403)
                return
            name = qprops.get('name')
            if not name:
                self._send_error_json('name parameter required', 400)
                return
            from . import savegame
            active = core.db.get_playerdata(name, savegame.DEFAULT_FORM)
            snaps = [{'name': sn, 'size': sz, 'mtime': _iso_utc(mt)}
                     for sn, sz, mt in core.db.list_snapshots(name)]
            self._send_json({'name': name, 'activeSize': len(active or b''),
                             'snapshots': snaps})
            return

        if path == 'character/download':
            if not cfg.web_admin_api:
                self._send_error_json('Admin API is disabled', 403)
                return
            name = qprops.get('name')
            form = qprops.get('form')
            if not name:
                self._send_error_json('name parameter required', 400)
                return
            from . import savegame
            form = form or savegame.DEFAULT_FORM
            blob = core.db.get_playerdata(name, form)
            if not blob:
                self._send_error_json('no character stored', 404)
                return
            self._send_bytes(blob, f'{name}_Playerdata.bin')
            return

        if path == 'snapshot/download':
            if not cfg.web_admin_api:
                self._send_error_json('Admin API is disabled', 403)
                return
            name = qprops.get('name')
            snap = qprops.get('snap')
            if not name or not snap:
                self._send_error_json('name and snap parameters required', 400)
                return
            blob = core.db.get_snapshot(name, snap)
            if not blob:
                self._send_error_json('empty or missing snapshot', 404)
                return
            self._send_bytes(blob, f'{name}_{snap}.bin')
            return

        if path == 'playerdata':
            if not cfg.web_playerdata_download:
                self._send_error_json('Playerdata download is disabled', 403)
                return
            name = qprops.get('name')
            form = qprops.get('form')
            if not name or not form:
                self._send_error_json('name and form parameters required', 400)
                return
            pdat = core.db.get_playerdata(name, form)
            if not pdat:
                self._send_error_json('no data', 404)
                return
            self.send_response(200)
            self.send_header('Content-Type', _MIME_BINARY)
            self.send_header('Content-Length', str(len(pdat)))
            self.send_header('Content-Disposition',
                             'attachment; filename="Playerdata.bin"')
            self.end_headers()
            self.wfile.write(pdat)
            return

        if not self._send_file(path or 'index.html'):
            self._send_error_json('not found', 404)

    # -- admin actions --------------------------------------------------

    def _admin_allowed(self):
        """Gate write actions.

        Access is authenticated by whatever sits in front of this port: an
        SSH tunnel (the key) or the reverse proxy's basic-auth login. The
        CSRF defence is the custom header below - cross-origin JavaScript
        cannot set it without a preflight this server never approves. The
        Origin (when the browser sends one) must be localhost or one of the
        configured proxy host names ([Web] panel_hosts). It is deliberately
        not compared with the Host header: a DNS-rebinding page is
        same-origin with its own Host, so it could set the custom header and
        reach the panel through a local SSH tunnel.
        """
        if self.headers.get('X-TW1MP-Admin') != '1':
            self._send_error_json('missing admin header', 403)
            return False
        origin = self.headers.get('Origin')
        if origin:
            oh = urlparse(origin).hostname
            allowed = {'localhost', '127.0.0.1', '::1'}
            allowed |= getattr(self.server.core.config, 'web_panel_hosts',
                               set())
            if oh not in allowed:
                self._send_error_json('bad origin', 403)
                return False
        return True

    def do_POST(self):
        core = self.server.core
        cfg = core.config
        path = unquote(urlparse(self.path).path).strip('/').lower()

        if not cfg.web_admin_api:
            self._send_error_json('Admin API is disabled', 403)
            return
        if not self._admin_allowed():
            return

        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length < 0 or length > 65536:
            self._send_error_json('payload too large', 413)
            return
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(body, dict):
                raise ValueError('expected an object')
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_error_json(f'bad json: {exc}', 400)
            return

        name = str(body.get('name') or '')
        reason = str(body.get('reason') or '')

        if path == 'admin/broadcast':
            text = str(body.get('text') or '').strip()
            if not text:
                self._send_error_json('text required', 400)
                return
            count = core.broadcast(text)
            self._send_json({'ok': True, 'recipients': count})
            return

        if path == 'admin/kick':
            if not name:
                self._send_error_json('name required', 400)
                return
            ok = core.kick(name)
            self._send_json({'ok': ok,
                             'message': f'{name} gekickt.' if ok else
                             f'{name} ist nicht online.'})
            return

        if path == 'admin/ban':
            if not name:
                self._send_error_json('name required', 400)
                return
            # Ban the account and its serial key, then drop the session -
            # same two-part ban the desktop panel applies.
            core.db.add_ban('name', name, reason)
            serial = core.db.get_serial(name)
            if serial:
                core.db.add_ban('serial', bytes(serial).hex(), reason)
            kicked = core.kick(name, 'You have been banned from this server')
            log.info('Banned %s via the dashboard (reason: %s)',
                     name, reason or '-')
            self._send_json({'ok': True, 'kicked': kicked,
                             'message': f'{name} gebannt (Name + Serial)'
                             + (' und gekickt.' if kicked else '.')})
            return

        if path == 'admin/item':
            # Edits land in the modified variant next to the original, which
            # the server only ever serves to a player alone on it - the
            # untouched character can never be lost to an edit.
            form = str(body.get('form') or '')
            changes = body.get('changes') or {}
            if not name or not form or not isinstance(changes, dict) \
                    or not changes:
                self._send_error_json('name, form and changes required', 400)
                return
            from . import chardata
            db = core.db
            was_modded = db.has_modded_playerdata(name, form)
            blob = db.get_playerdata(name, form, modded=was_modded)
            if not blob:
                self._send_error_json('no character stored', 404)
                return
            try:
                parsed = {int(k): int(v) for k, v in changes.items()}
                newblob = chardata.edit_quantities(blob, parsed)
            except (ValueError, TypeError) as exc:
                self._send_error_json(f'bad changes: {exc}', 400)
                return
            except chardata.CharDataError as exc:
                self._send_error_json(str(exc), 400)
                return
            if not db.set_playerdata(name, form, newblob, modded=True):
                self._send_error_json('could not store the character', 500)
                return
            log.info('Edited %d item(s) of %s via the dashboard',
                     len(parsed), name)
            self._send_json({'ok': True, 'changed': len(parsed),
                             'message': f'{len(parsed)} Eintrag(e) geändert '
                                        '(Original bleibt unangetastet).'})
            return

        if path == 'admin/character/revert':
            form = str(body.get('form') or '')
            if not name or not form:
                self._send_error_json('name and form required', 400)
                return
            ok = core.db.delete_modded_playerdata(name, form)
            log.info('Reverted the modified character of %s (%s)', name,
                     'removed' if ok else 'nothing to remove')
            self._send_json({'ok': ok,
                             'message': 'Änderungen verworfen, Original ist '
                             'wieder aktiv.' if ok else
                             'Es gab keine geänderte Variante.'})
            return

        if path == 'admin/guild/create':
            guild = str(body.get('guild') or '').strip()
            if not guild:
                self._send_error_json('guild required', 400)
                return
            ok = core.db.create_guild(guild, body.get('tag') or '',
                                      body.get('note') or '')
            self._send_json({'ok': ok,
                             'message': f'Gilde "{guild}" angelegt.' if ok else
                             'Diesen Gildennamen gibt es schon.'})
            return

        if path == 'admin/guild/delete':
            guild = str(body.get('guild') or '').strip()
            if not guild:
                self._send_error_json('guild required', 400)
                return
            ok = core.db.delete_guild(guild)
            self._send_json({'ok': ok,
                             'message': f'Gilde "{guild}" aufgelöst.' if ok
                             else 'Gilde nicht gefunden.'})
            return

        if path == 'admin/guild/member':
            guild = str(body.get('guild') or '').strip()
            if not name:
                self._send_error_json('name required', 400)
                return
            ok = core.db.set_member_guild(name, guild)
            if not ok:
                self._send_error_json('unknown guild', 400)
                return
            self._send_json({'ok': True,
                             'message': f'{name} ist jetzt in "{guild}".'
                             if guild else f'{name} aus der Gilde entfernt.'})
            return

        if path == 'admin/unban':
            kind = str(body.get('kind') or '')
            value = str(body.get('value') or '')
            if kind not in ('name', 'serial') or not value:
                self._send_error_json('kind and value required', 400)
                return
            ok = core.db.remove_ban(kind, value)
            log.info('Unbanned %s %s via the dashboard', kind, value)
            self._send_json({'ok': ok,
                             'message': 'Ban aufgehoben.' if ok else
                             'Kein passender Ban gefunden.'})
            return

        # -- settings ---------------------------------------------------
        if path == 'admin/config':
            self._save_config(core, body)
            return

        if path == 'admin/restart':
            self._restart_service()
            return

        # -- character / account maintenance ----------------------------
        if path == 'admin/account/delete':
            if not name:
                self._send_error_json('name required', 400)
                return
            if name in core.debug_dict_players():
                self._send_error_json(
                    f'{name} ist online - erst kicken.', 409)
                return
            ok = core.db.delete_user(name)
            log.info('Deleted account %s via the dashboard', name)
            self._send_json({'ok': ok,
                             'message': f'Account "{name}" gelöscht.' if ok
                             else f'{name} konnte nicht gelöscht werden.'})
            return

        if path == 'admin/character/delete':
            from . import savegame
            form = str(body.get('form') or savegame.DEFAULT_FORM)
            if not name:
                self._send_error_json('name required', 400)
                return
            ok = core.db.delete_playerdata(name, form)
            log.info('Deleted character of %s via the dashboard', name)
            self._send_json({'ok': ok,
                             'message': f'Charakter von {name} gelöscht.' if ok
                             else f'{name} hatte keinen Charakter.'})
            return

        if path == 'admin/character/import':
            import base64
            from . import savegame
            data = body.get('data')
            if not name or not isinstance(data, str) or not data:
                self._send_error_json('name and data (base64) required', 400)
                return
            try:
                blob = base64.b64decode(data)
            except (ValueError, TypeError) as exc:
                self._send_error_json(f'bad base64: {exc}', 400)
                return
            try:
                savegame.import_playerdata(core.db, name, blob)
            except savegame.SavegameError as exc:
                self._send_error_json(str(exc), 400)
                return
            log.info('Imported %d bytes for %s via the dashboard',
                     len(blob), name)
            self._send_json({'ok': True,
                             'message': f'{len(blob)} Bytes für {name} '
                                        'importiert.'})
            return

        # -- savegame snapshots -----------------------------------------
        if path.startswith('admin/snapshot/'):
            self._snapshot_action(core, path.rsplit('/', 1)[-1], body, name)
            return

        self._send_error_json('not found', 404)

    # -- settings / restart helpers -------------------------------------

    def _save_config(self, core, body):
        from .config import save_settings, hash_password
        settings = body.get('settings')
        if not isinstance(settings, dict):
            self._send_error_json('settings object required', 400)
            return
        types = {(s, k): t
                 for _title, fields in SETTINGS_SPEC
                 for (s, k, _label, t) in fields}
        server, web = {}, {}
        restart_needed = False
        for flat, value in settings.items():
            section, _, key = str(flat).partition('.')
            typ = types.get((section, key))
            if typ is None:          # ignore anything not in the known spec
                continue
            if typ == 'int':
                raw = str(value).strip()
                if not raw.lstrip('-').isdigit():
                    self._send_error_json(
                        f'"{key}" muss eine ganze Zahl sein.', 400)
                    return
                value = int(raw)
            elif typ == 'bool':
                value = bool(value)
            else:
                value = str(value)
            (server if section == 'Server' else web)[key] = value
            if (section, key) in _RESTART_KEYS:
                restart_needed = True
        panel = {}
        pmode = (body.get('panel') or {}).get('mode')
        if pmode in ('client', 'server'):
            panel['mode'] = pmode
        pw = body.get('adminPassword')
        if isinstance(pw, str):      # '' clears it, a value sets a new hash
            panel['admin_password'] = hash_password(pw) if pw else ''
        try:
            save_settings(core.config.path, server=server, web=web,
                          panel=panel or None)
        except OSError as exc:
            self._send_error_json(f'Speichern fehlgeschlagen: {exc}', 500)
            return
        core.config.reload()         # live-effect settings apply at once
        log.info('Settings saved via the dashboard (%d Server, %d Web keys)',
                 len(server), len(web))
        msg = 'Einstellungen gespeichert.'
        if restart_needed:
            msg += ' Port-/Netzwerkänderungen wirken erst nach einem Neustart.'
        self._send_json({'ok': True, 'restartNeeded': restart_needed,
                         'message': msg})

    def _restart_service(self):
        """Restart the whole server process so bound-socket settings apply.

        The VPS unit is hardened with NoNewPrivileges (so sudo cannot
        escalate) and runs with Restart=always, so the reliable path is to
        terminate ourselves cleanly and let systemd bring the service back.
        `systemctl restart` is still tried first for setups that permit it;
        it fails fast under NoNewPrivileges and we fall through to SIGTERM.
        """
        import signal
        import subprocess
        import threading

        def worker():
            try:
                rc = subprocess.run(
                    ['sudo', '-n', 'systemctl', 'restart', 'tw1mp'],
                    timeout=6).returncode
            except Exception:
                rc = 1
            if rc != 0:              # sudo blocked: rely on Restart=always
                try:
                    os.kill(os.getpid(), signal.SIGTERM)
                except OSError:
                    log.exception('Self-restart failed')

        log.info('Restart requested via the dashboard')
        # Answer first, then restart, so this response still reaches the page.
        self._send_json({'ok': True,
                         'message': 'Server wird neu gestartet - das Panel '
                                    'verbindet sich gleich neu.'})
        try:
            self.wfile.flush()
        except OSError:
            pass
        t = threading.Timer(0.6, worker)
        t.daemon = True
        t.start()

    def _snapshot_action(self, core, action, body, name):
        from . import savegame
        if not name:
            self._send_error_json('name required', 400)
            return
        db = core.db
        snap = str(body.get('snap') or '').strip()

        if action == 'save':          # store the current active character
            blob = db.get_playerdata(name, savegame.DEFAULT_FORM)
            if not blob:
                self._send_error_json(f'{name} hat keinen aktiven Charakter.',
                                      404)
                return
            ok = db.save_snapshot(name, snap, blob)
            self._send_json({'ok': ok,
                             'message': f'Als "{snap}" gespeichert.' if ok
                             else 'Ungültiger Name.'})
            return

        if action == 'new':           # empty slot => fresh character on use
            ok = db.save_snapshot(name, snap, b'')
            self._send_json({'ok': ok,
                             'message': f'Leerer Spielstand "{snap}" angelegt.'
                             if ok else 'Ungültiger Name.'})
            return

        if action == 'activate':
            blob = db.get_snapshot(name, snap)
            if blob is None:
                self._send_error_json('Spielstand nicht lesbar.', 404)
                return
            db.set_playerdata(name, savegame.DEFAULT_FORM, blob, modded=False)
            fresh = len(blob) == 0
            self._send_json({'ok': True,
                             'message': f'"{snap}" aktiviert - {name} startet '
                             'einen neuen Charakter.' if fresh else
                             f'"{snap}" ist jetzt aktiv für {name}.'})
            return

        if action == 'rename':
            newname = str(body.get('newname') or '').strip()
            if not newname:
                self._send_error_json('newname required', 400)
                return
            ok = db.rename_snapshot(name, snap, newname)
            self._send_json({'ok': ok,
                             'message': f'Umbenannt in "{newname}".' if ok
                             else 'Name ungültig oder schon vergeben.'})
            return

        if action == 'delete':
            ok = db.delete_snapshot(name, snap)
            self._send_json({'ok': ok,
                             'message': f'Spielstand "{snap}" gelöscht.' if ok
                             else 'Nicht gefunden.'})
            return

        if action == 'import':
            import base64
            data = body.get('data')
            if not isinstance(data, str) or not snap:
                self._send_error_json('snap and data (base64) required', 400)
                return
            try:
                blob = base64.b64decode(data)
            except (ValueError, TypeError) as exc:
                self._send_error_json(f'bad base64: {exc}', 400)
                return
            ok = db.save_snapshot(name, snap, blob)
            self._send_json({'ok': ok,
                             'message': f'{len(blob)} Bytes als "{snap}" '
                             'importiert.' if ok else 'Ungültiger Name.'})
            return

        self._send_error_json('unknown snapshot action', 404)

    def _read_ini(self):
        """Config.ini merged onto the defaults (raw string values)."""
        import configparser
        from .config import DEFAULTS
        cp = configparser.ConfigParser()
        cp.read_dict(DEFAULTS)
        path = self.server.core.config.path
        if os.path.exists(path):
            cp.read(path)
        return cp

    def _config_document(self):
        """The settings form the dashboard renders: grouped fields with their
        current values, plus the desktop-panel access settings."""
        cur = self._read_ini()
        groups = []
        for title, fields in SETTINGS_SPEC:
            out = []
            for section, key, label, typ in fields:
                if typ == 'bool':
                    value = cur.getboolean(section, key)
                elif typ == 'motd':
                    value = cur.get(section, key).replace('\\r\\n', '\n')
                else:
                    value = cur.get(section, key)
                out.append({'section': section, 'key': key, 'label': label,
                            'type': typ, 'value': value,
                            'restart': [section, key] in
                            [list(k) for k in _RESTART_KEYS]})
            groups.append({'title': title, 'fields': out})
        return {'groups': groups,
                'panel': {'mode': cur.get('Panel', 'mode', fallback='client'),
                          'hasPassword': bool(cur.get('Panel',
                                                      'admin_password',
                                                      fallback=''))}}

    def _send_bytes(self, data, filename):
        # names come from the query string: keep the header to safe characters
        filename = re.sub(r'[^A-Za-z0-9._-]', '_', filename)
        self.send_response(200)
        self.send_header('Content-Type', _MIME_BINARY)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Content-Disposition',
                         f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, relpath):
        root = os.path.abspath(self.server.core.config.web_root)
        fpath = os.path.abspath(os.path.join(root, relpath))
        # keep requests inside the web root
        if not fpath.startswith(root + os.sep):
            return False
        ext = os.path.splitext(fpath)[1].lower()
        mime = _MIME_BY_EXT.get(ext)
        if mime is None or not os.path.isfile(fpath):
            return False
        try:
            with open(fpath, 'rb') as f:
                body = f.read()
        except OSError:
            return False
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True


class PublicApiServe(WebApiServe):
    """Internet-facing handler: serves only the whitelisted read-only
    /public/* endpoints and refuses everything else. The admin, account and
    character surface stays on the private (localhost) [Web] port."""

    _PUBLIC = ('public/guilds', 'public/ranking', 'public/summary')

    def do_GET(self):
        path = unquote(urlparse(self.path).path).strip('/').lower()
        if path in self._PUBLIC:
            return super().do_GET()
        self._send_error_json('not found', 404)

    def do_POST(self):
        self._send_error_json('method not allowed', 405)


class PublicWebServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, core):
        _install_log_capture()
        log.info('Public data server starting on 0.0.0.0 port %s',
                 core.config.public_port)
        super().__init__(('0.0.0.0', core.config.public_port), PublicApiServe)
        self.core = core
