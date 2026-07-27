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
import socketserver
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
            for form in ([want] if want else forms):
                blob = core.db.get_playerdata(name, form)
                if blob:
                    used = form
                    break
            if not blob:
                self._send_json({'name': name, 'forms': forms, 'form': None,
                                 'items': [], 'note': 'no character stored'})
                return
            from . import chardata
            try:
                raw = chardata.decompress(blob)
                items = [{'name': s.name, 'quantity': s.quantity,
                          'kind': s.kind, 'editable': s.editable,
                          'category': chardata.category(s.name)}
                         for s in chardata.parse_items(raw)]
            except chardata.CharDataError as exc:
                self._send_error_json(f'unreadable character: {exc}', 500)
                return
            self._send_json({'name': name, 'forms': forms, 'form': used,
                             'size': len(blob), 'items': items})
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

        The API is meant to be reached through an SSH tunnel, so the tunnel
        (and the key behind it) is the authentication. What that does not
        cover is a web page open in the same browser POSTing to localhost,
        so require a custom header - which cross-origin JavaScript cannot
        set without a preflight this server never approves - and reject any
        foreign Origin outright.
        """
        if self.headers.get('X-TW1MP-Admin') != '1':
            self._send_error_json('missing admin header', 403)
            return False
        origin = self.headers.get('Origin')
        if origin and urlparse(origin).hostname not in ('localhost',
                                                        '127.0.0.1', '::1'):
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

        self._send_error_json('not found', 404)

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
