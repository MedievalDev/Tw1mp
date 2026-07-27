"""Server configuration, backed by an INI file (Config.ini).

A default config file is written on first start so admins can edit it.
"""

import configparser
import logging
import os

log = logging.getLogger('tw1mp.config')

DEFAULTS = {
    'Server': {
        'name': 'TW1MP',
        'title': 'Community Multiplayer Server',
        # \r\n is unescaped on load (INI values cannot hold real newlines)
        'motd': '<0xFF0000FF><F2>Welcome to the community server!<break=10.0>\\r\\n',
        'bind': '',
        'port': '17171',
        # Register unknown usernames automatically on first login.
        'auto_register': 'true',
        # Allow new accounts to be created at all. Turn off to lock the roster
        # once your group is set up; existing accounts keep working.
        'allow_registration': 'true',
        # Accept any login without checking the database (debugging only!).
        'allow_any_login': 'false',
        # Bind accounts to the client serial identifier (one account per key).
        'bind_serial': 'true',
        'max_channel_users': '50',
        # Number of lobby channels created per map (1-20).
        'channels_per_map': '1',
        'maps': 'Net_T_01,Net_T_02,Net_T_03,Net_T_04',
        # Send keepalive /nop to all users every 3 seconds (the original
        # official server did); also flushes out silently dead connections.
        'send_nops': 'true',
        # Always answer failed logins with the reference server's literal
        # 'TESTERROR' string instead of readable messages. Turn on if the
        # real client misbehaves on the readable error texts.
        'compat_login_errors': 'false',
        # Admin bot: an always-connected lobby client that keeps the towns
        # populated, follows the players and answers !help/!players/!uptime.
        'bot_enabled': 'false',
        'bot_name': 'Admin',
        'bot_password': 'tw1mp-bot',
        # Links the bot hands out on !web / !discord. Leave discord empty
        # until there is an invite; the bot then says so instead of lying.
        'bot_website': 'https://twmp.alchemy-fox.de/',
        'bot_discord': '',
        # Colour the bot's chat lines. Two Worlds text markup is <0xAARRGGBB>.
        # Default is a warm gold so admin messages stand out. Empty = no colour.
        'bot_color': '<0xFFFFC83C>',
    },
    'Panel': {
        # The desktop panel opens in this mode. 'client' hides everything that
        # controls or exposes the server; 'server' is the full admin view.
        'mode': 'client',
        # Salted hash of the admin password (set it in the panel, never by
        # hand). Empty = no password set, admin mode is unlocked.
        'admin_password': '',
    },
    'Web': {
        # Optional HTTP status server.
        'enabled': 'false',
        'port': '17071',
        # Bind address for the web server only (the game port uses [Server]
        # bind). Empty = same as the game (all interfaces). Set to 127.0.0.1
        # to keep the dashboard private and reach it through an SSH tunnel.
        'bind': '',
        'debug_api': 'false',
        'playerdata_download': 'false',
    },
}


def _safe(getter, section, key):
    """Read a typed value; fall back to the default on a malformed entry
    instead of aborting startup."""
    try:
        return getter(key)
    except ValueError:
        raw = DEFAULTS[section][key]
        default = (raw.lower() == 'true') if raw.lower() in ('true', 'false') \
            else int(raw)
        log.warning('Invalid value for [%s] %s in Config.ini, '
                    'using default %r', section, key, default)
        return default


def _ini_value(key, value):
    """Serialise a form value for the INI file."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if key == 'motd':
        # INI values can't hold real newlines; store the escaped form the
        # loader understands. Collapse any newline style to \r\n.
        text = str(value).replace('\r\n', '\n').replace('\r', '\n')
        return text.replace('\n', '\\r\\n')
    return str(value)


def hash_password(password, salt=None):
    """Salted hash for the panel's admin password.

    This gates the desktop panel's admin view; it is a convenience lock, not
    real security - anyone with file access can read the database directly.
    """
    import hashlib
    import os as _os
    salt = salt or _os.urandom(8).hex()
    digest = hashlib.sha256((salt + password).encode('utf-8')).hexdigest()
    return f'{salt}${digest}'


def check_password(password, stored):
    if not stored or '$' not in stored:
        return False
    salt = stored.split('$', 1)[0]
    return hash_password(password, salt) == stored


def save_settings(path, server=None, web=None, panel=None):
    """Merge the given [Server]/[Web]/[Panel] values into the INI file at
    `path`, preserving everything else."""
    cfg = configparser.ConfigParser()
    cfg.read_dict(DEFAULTS)
    if os.path.exists(path):
        cfg.read(path)
    for section, values in (('Server', server), ('Web', web),
                            ('Panel', panel)):
        if not values:
            continue
        if section not in cfg:
            cfg.add_section(section)
        for key, value in values.items():
            cfg[section][key] = _ini_value(key, value)
    with open(path, 'w') as f:
        cfg.write(f)


class Config:
    def __init__(self, root=None, path=None):
        self.root = root or os.getcwd()
        self.path = path or os.path.join(self.root, 'Config.ini')
        cfg = configparser.ConfigParser()
        cfg.read_dict(DEFAULTS)
        if os.path.exists(self.path):
            cfg.read(self.path)
        else:
            try:
                with open(self.path, 'w') as f:
                    cfg.write(f)
            except OSError:
                pass  # read-only dir: run with defaults
        srv = cfg['Server']
        self.name = srv.get('name')
        self.title = srv.get('title')
        self.motd = srv.get('motd').replace('\\r\\n', '\r\n')
        self.bind = srv.get('bind')
        self.port = _safe(srv.getint, 'Server', 'port')
        self.auto_register = _safe(srv.getboolean, 'Server', 'auto_register')
        self.allow_registration = _safe(srv.getboolean, 'Server',
                                        'allow_registration')
        self.allow_any_login = _safe(srv.getboolean, 'Server',
                                     'allow_any_login')
        self.bind_serial = _safe(srv.getboolean, 'Server', 'bind_serial')
        self.max_channel_users = _safe(srv.getint, 'Server',
                                       'max_channel_users')
        self.channels_per_map = max(1, min(20, _safe(srv.getint, 'Server',
                                                     'channels_per_map')))
        self.maps = [m.strip() for m in srv.get('maps').split(',') if m.strip()]
        self.send_nops = _safe(srv.getboolean, 'Server', 'send_nops')
        self.compat_login_errors = _safe(srv.getboolean, 'Server',
                                         'compat_login_errors')
        self.bot_enabled = _safe(srv.getboolean, 'Server', 'bot_enabled')
        self.bot_name = srv.get('bot_name')
        self.bot_password = srv.get('bot_password')
        self.bot_website = srv.get('bot_website')
        self.bot_discord = srv.get('bot_discord')
        self.bot_color = srv.get('bot_color')
        panel = cfg['Panel'] if 'Panel' in cfg else {}
        self.panel_mode = (panel.get('mode') or 'client').strip().lower()
        if self.panel_mode not in ('client', 'server'):
            self.panel_mode = 'client'
        self.admin_password = panel.get('admin_password') or ''
        web = cfg['Web']
        self.web_enabled = _safe(web.getboolean, 'Web', 'enabled')
        self.web_port = _safe(web.getint, 'Web', 'port')
        self.web_debug_api = _safe(web.getboolean, 'Web', 'debug_api')
        self.web_playerdata_download = _safe(web.getboolean, 'Web',
                                             'playerdata_download')
        self.web_bind = (web.get('bind') or '').strip()

        self.database_path = os.path.join(self.root, 'ServerData.db')
        self.playerdata_path = os.path.join(self.root, 'PlayerData')
        self.web_root = os.path.join(self.root, 'Web')
