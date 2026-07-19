"""The admin bot as seen by a real client: presence, chat and greeting."""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from tw1mp.config import Config
from tw1mp.server import CoreServer
from tw1_client import TestClient

CHANNEL = 'Net_T_01#translateNet_T_01_Channel_01'
BOT = 'Admin'


class TestAdminBot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(root=self.tmp.name)
        self.cfg.port = 0                 # ephemeral
        self.cfg.bot_enabled = True
        self.cfg.bot_name = BOT
        self.cfg.send_nops = False
        self.server = CoreServer(self.cfg)
        self.port = self.server.server_address[1]
        # the bot connects to the port it reads from the config
        self.cfg.port = self.port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.2},
                                       daemon=True)
        self.thread.start()
        self.assertTrue(self._wait(lambda: BOT in self.server.state.activeUsers),
                        'the admin bot never logged in')

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.server.db.close()
        self.tmp.cleanup()

    @staticmethod
    def _wait(predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return False

    def _join(self, name='alice'):
        cli = TestClient(self.port, serial=(name.encode() * 8)[:8],
                         guid=(name.encode() * 16)[:16])
        self.addCleanup(cli.close)
        cli.handshake()
        cli.login(name, 'pw', register=True)
        cli.send_cmd('/leavegamechannel "1"')
        cli.wait_for(b'$gamechannel')
        cli.send_cmd(f'/requestjoingamechannel "{CHANNEL}"')
        cli.wait_for(b'/requestjoingamechannel')
        cli.send_cmd(f'/joingamechannel "{CHANNEL}" "1000#2000"')
        cli.wait_for(b'/joinchatchannel')
        return cli

    def _ask(self, cli, message, marker):
        cli.drain(0.4)
        cli.send_cmd(f'/send "{message}"')
        return cli.wait_for(marker, timeout=6)

    def test_bot_is_in_a_town(self):
        con = self.server.state.activeUsers[BOT]
        self.assertTrue(self._wait(lambda: con.user.gamechannel is not None))
        self.assertTrue(con.user.gamechannel.name.startswith('Net_T_01'))

    def test_greets_a_player_joining_its_town(self):
        cli = self._join()
        self.assertIn(b'Willkommen, alice!',
                      cli.wait_for(b'Willkommen', timeout=8))
        # ...and does not keep repeating it
        time.sleep(2.5)
        self.assertNotIn(b'Willkommen', cli.drain(1.0))

    def test_answers_chat_commands(self):
        cli = self._join()
        cli.wait_for(b'Willkommen', timeout=8)
        self.assertIn(b'Befehle:', self._ask(cli, '!help', b'Befehle:'))
        self.assertIn(b'Online (1): alice',
                      self._ask(cli, '!players', b'Online ('))
        self.assertIn(b'Server laeuft seit',
                      self._ask(cli, '!uptime', b'Server laeuft'))

    def test_announces_website_and_info_commands(self):
        cli = self._join()
        # the greeting already advertises the site
        self.assertIn(b'twmp.alchemy-fox.de',
                      cli.wait_for(b'Infos', timeout=8))
        checks = [
            ('!web', b'twmp.alchemy-fox.de'),
            ('!commands', b'buglord'),          # in-game console command list
            ('!settings', b'FarPlane'),         # set.txt view-distance tuning
            ('!server', b'Spieler online'),
        ]
        for message, marker in checks:
            self.assertIn(marker, self._ask(cli, message, marker), message)

    def test_discord_is_honest_when_unset(self):
        cli = self._join()
        cli.wait_for(b'Infos', timeout=8)
        reply = self._ask(cli, '!discord', b'Discord')
        self.assertIn(b'noch nicht eingerichtet', reply)

    def test_stays_silent_on_ordinary_chat(self):
        cli = self._join()
        cli.wait_for(b'Welcome', timeout=8)
        cli.drain(0.4)
        cli.send_cmd('/send "just chatting"')
        time.sleep(1.5)
        replies = [line for line in cli.drain(1.0).split(b'\0')
                   if line.startswith(b'/send "%s"' % BOT.encode())]
        self.assertEqual(replies, [])

    def test_bot_becomes_visible_once_herodata_is_known(self):
        """A user with no herodata is drawn for nobody (getGCUmsg is empty),
        so the bot has to replay a captured appearance blob."""
        bot = self.server.state.activeUsers[BOT].user
        self.assertEqual(bot.getGCUmsg(), b'', 'bot should start invisible')

        cli = self._join()
        hero = b'\xDE\xAD\xBE\xEF' * 40
        cli.send_cmd(f'/setuserherodata "alice" "{len(hero)}"', blob=hero)
        self.assertTrue(self._wait(
            lambda: self.server.load_herodata_sample()[0] != b''),
            'server never captured a herodata sample')

        blob, posdata = self.server.load_herodata_sample()
        self.assertEqual(blob, hero)
        self.assertEqual(posdata, '1000#2000')   # the position _join used

        # the bot picks the sample up on its next town join
        self.assertTrue(self._wait(
            lambda: self.server.state.activeUsers[BOT].user.herodata != b'',
            timeout=20), 'bot never sent its herodata')
        bot = self.server.state.activeUsers[BOT].user
        self.assertEqual(bot.herodata, hero)
        self.assertNotEqual(bot.getGCUmsg(), b'', 'bot still invisible')

    def test_bot_does_not_count_as_company(self):
        """A lone player must still count as alone, so the modified-character
        rule keeps working while the bot is connected."""
        self._join()
        self.assertIn(BOT, self.server.state.activeUsers)
        humans = [n for n in self.server.state.activeUsers
                  if n != self.cfg.bot_name]
        self.assertEqual(humans, ['alice'])


if __name__ == '__main__':
    unittest.main()
