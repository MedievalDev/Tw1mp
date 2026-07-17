"""Server configuration, backed by an INI file (Config.ini).

A default config file is written on first start so admins can edit it.
"""

import configparser
import os

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
        # Accept any login without checking the database (debugging only!).
        'allow_any_login': 'false',
        # Bind accounts to the client serial identifier (one account per key).
        'bind_serial': 'true',
        'max_channel_users': '50',
        # Number of lobby channels created per map (1-20).
        'channels_per_map': '1',
        'maps': 'Net_T_01,Net_T_02,Net_T_03,Net_T_04',
        # Send keepalive /nop to all users every 3 seconds.
        'send_nops': 'false',
    },
    'Web': {
        # Optional HTTP status server.
        'enabled': 'false',
        'port': '17071',
        'debug_api': 'false',
        'playerdata_download': 'false',
    },
}


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
        self.port = srv.getint('port')
        self.auto_register = srv.getboolean('auto_register')
        self.allow_any_login = srv.getboolean('allow_any_login')
        self.bind_serial = srv.getboolean('bind_serial')
        self.max_channel_users = srv.getint('max_channel_users')
        self.channels_per_map = max(1, min(20, srv.getint('channels_per_map')))
        self.maps = [m.strip() for m in srv.get('maps').split(',') if m.strip()]
        self.send_nops = srv.getboolean('send_nops')
        web = cfg['Web']
        self.web_enabled = web.getboolean('enabled')
        self.web_port = web.getint('port')
        self.web_debug_api = web.getboolean('debug_api')
        self.web_playerdata_download = web.getboolean('playerdata_download')

        self.database_path = os.path.join(self.root, 'ServerData.db')
        self.playerdata_path = os.path.join(self.root, 'PlayerData')
        self.web_root = os.path.join(self.root, 'Web')
