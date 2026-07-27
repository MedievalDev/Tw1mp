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
