"""End-to-end tests: a simulated game client against a running server."""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from tw1mp.config import Config
from tw1mp.server import CoreServer
from tw1_client import TestClient

CHANNEL = 'Net_T_01#translateNet_T_01_Channel_01'
FORM = 'TwoWorlds.1.0'


class ServerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(root=self.tmp.name)
        self.cfg.port = 0  # ephemeral
        self.server = CoreServer(self.cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.2},
                                       daemon=True)
        self.thread.start()
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.server.db.close()
        self.tmp.cleanup()

    def connect(self, name, password='pw', serial=None, guid=None,
                register=False, **regfields):
        serial = serial or (name.encode('latin-1') * 8)[:8]
        guid = guid or (name.encode('latin-1') * 16)[:16]
        cli = TestClient(self.port, serial=serial, guid=guid)
        self.clients.append(cli)
        err, info = cli.handshake()
        self.assertEqual(err, 0)
        self.assertIn('TW1MP', info)
        err, _ = cli.login(name, password, register=register, **regfields)
        self.assertEqual(err, 0)
        return cli

    def join_channel(self, cli, pos='1000#2000'):
        cli.send_cmd('/leavegamechannel "1"')
        self.assertIn(b'$gamechannel', cli.wait_for(b'$gamechannel'))
        cli.send_cmd(f'/requestjoingamechannel "{CHANNEL}"')
        self.assertIn(f'/requestjoingamechannel "{CHANNEL}" "1"'.encode(),
                      cli.wait_for(b'/requestjoingamechannel'))
        cli.send_cmd(f'/joingamechannel "{CHANNEL}" "{pos}"')
        got = cli.wait_for(b'/joinchatchannel')
        self.assertIn(f'/joingamechannel "{CHANNEL}"'.encode(), got)
        self.assertIn(b'/joinchatchannel "translateNetCityMainChannel"', got)
        self.assertIn(b'$chatchannel', got)


class TestLogin(ServerFixture):
    def test_autoregister_and_relogin(self):
        cli = self.connect('alice')  # auto-registered
        cli.close()
        cli2 = TestClient(self.port, serial=(b'alice' * 8)[:8],
                          guid=(b'alice' * 16)[:16])
        self.clients.append(cli2)
        cli2.handshake()
        err, res = cli2.login('alice', 'pw')
        self.assertEqual(err, 0)
        self.assertIn(b'Community Multiplayer Server', res)

    def test_wrong_password_rejected(self):
        self.connect('alice').close()
        cli = TestClient(self.port, serial=(b'alice' * 8)[:8],
                         guid=(b'alice' * 16)[:16])
        self.clients.append(cli)
        cli.handshake()
        err, _ = cli.login('alice', 'wrongpw')
        self.assertEqual(err, 1)

    def test_empty_login_gets_error(self):
        # Client sends empty credentials when none are stored; the error
        # makes it open the login prompt.
        cli = TestClient(self.port)
        self.clients.append(cli)
        cli.handshake()
        err, _ = cli.login('', '')
        self.assertEqual(err, 1)

    def test_explicit_registration(self):
        cli = self.connect('bob', register=True, email='b@example.org',
                           location='DE', age=30, description='hi')
        cli.send_cmd('/whois "bob"')
        got = cli.wait_for(b'/whois')
        self.assertIn(b'"b@example.org"', got)
        self.assertIn(b'"DE"', got)

    def test_double_login_rejected(self):
        self.connect('alice')
        cli = TestClient(self.port, serial=(b'alice' * 8)[:8],
                         guid=(b'alice' * 16)[:16])
        self.clients.append(cli)
        cli.handshake()
        err, _ = cli.login('alice', 'pw')
        self.assertEqual(err, 1)

    def test_compat_login_errors(self):
        self.connect('alice').close()
        self.server.config.compat_login_errors = True
        cli = TestClient(self.port, serial=(b'alice' * 8)[:8],
                         guid=(b'alice' * 16)[:16])
        self.clients.append(cli)
        cli.handshake()
        err, res = cli.login('alice', 'wrongpw')
        self.assertEqual(err, 1)
        self.assertIn(b'TESTERROR', res)

    def test_oversized_login_packet_closes_connection(self):
        import socket
        import struct
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        try:
            sock.sendall(struct.pack('<I', 0xFFFFFFF0))
            self.assertEqual(sock.recv(4096), b'')  # server hung up
        finally:
            sock.close()


class TestChannelsAndChat(ServerFixture):
    def test_join_channel_and_chat(self):
        alice = self.connect('alice')
        bob = self.connect('bob')
        self.join_channel(alice)
        self.join_channel(bob)
        alice.drain(0.4)
        bob.send_cmd('/send "hello world"')
        got = alice.wait_for(b'/send')
        self.assertIn(b'/send "bob" "hello world"', got)
        # bob also sees his own message
        self.assertIn(b'/send "bob" "hello world"', bob.wait_for(b'/send'))

    def test_switch_chat_channel(self):
        alice = self.connect('alice')
        self.join_channel(alice)
        alice.send_cmd('/joinchatchannel "translateNetCityTradeChannel" ""')
        got = alice.wait_for(b'/joinchatchannel "translateNetCityTradeChannel"')
        self.assertIn(b'/joinchatchannel "translateNetCityTradeChannel"', got)

    def test_create_chat_channel(self):
        # Third field 1 = "create it". Used to be ignored, so the client
        # waited forever for a reply that never came.
        alice = self.connect('alice')
        self.join_channel(alice)
        alice.drain(0.4)
        alice.send_cmd('/joinchatchannel "Handelsrunde" "" "1"')
        got = alice.wait_for(b'/joinchatchannel "Handelsrunde"')
        self.assertIn(b'/joinchatchannel "Handelsrunde"', got)

    def test_created_channel_is_announced_to_others(self):
        alice = self.connect('alice')
        bob = self.connect('bob')
        self.join_channel(alice)
        self.join_channel(bob)
        alice.drain(0.4)
        bob.send_cmd('/joinchatchannel "Gildenhalle" "" "1"')
        got = alice.wait_for(b'$chatchannel "Gildenhalle"')
        self.assertIn(b'$chatchannel "Gildenhalle"', got)

    def test_created_channel_password_is_enforced(self):
        alice = self.connect('alice')
        bob = self.connect('bob')
        self.join_channel(alice)
        self.join_channel(bob)
        alice.send_cmd('/joinchatchannel "Geheim" "losung" "1"')
        alice.wait_for(b'/joinchatchannel "Geheim"')
        bob.drain(0.4)
        bob.send_cmd('/joinchatchannel "Geheim" "falsch" "0"')
        self.assertIn(b'/error badChatPassword', bob.wait_for(b'/error'))

    def test_unknown_channel_without_create_flag_is_ignored(self):
        alice = self.connect('alice')
        self.join_channel(alice)
        alice.drain(0.4)
        alice.send_cmd('/joinchatchannel "GibtsNicht" "" "0"')
        self.assertNotIn(b'GibtsNicht', alice.drain(0.5))

    def test_leave_broadcasts_removal(self):
        alice = self.connect('alice')
        bob = self.connect('bob')
        self.join_channel(alice)
        self.join_channel(bob)
        alice.drain(0.4)
        bob.send_cmd('/leavegamechannel "1"')
        got = alice.wait_for(b'&gamechanneluser')
        self.assertIn(b'&gamechanneluser "bob"', got)

    def test_herodata_broadcast(self):
        alice = self.connect('alice')
        bob = self.connect('bob')
        self.join_channel(alice)
        alice.drain(0.4)
        hero = b'HERODATA' * 4
        bob.send_cmd(f'/setuserherodata "bob" "{len(hero)}"', blob=hero)
        self.join_channel(bob)
        got = alice.wait_for(b'$gamechanneluser')
        self.assertIn(b'$gamechanneluser "bob"', got)
        self.assertIn(hero, got)

    def test_position_updates(self):
        alice = self.connect('alice')
        bob = self.connect('bob')
        self.join_channel(alice)
        self.join_channel(bob)
        alice.drain(0.4)
        bob.send_cmd('/updheropos "2A4D#30AF"')
        got = alice.wait_for(b'/updheropos', timeout=3)
        self.assertIn(b'#2A4D#30AF', got)

    def test_whois_update(self):
        alice = self.connect('alice')
        alice.send_cmd('/update "alice" "new@example.org" "AT" "33" "1" "servus"')
        alice.send_cmd('/whois "alice"')
        got = alice.wait_for(b'/whois')
        self.assertIn(b'"new@example.org"', got)
        self.assertIn(b'"servus"', got)

    def test_guildrankpoints(self):
        alice = self.connect('alice')
        alice.send_cmd('/getguildrankpoints')
        got = alice.wait_for(b'/getguildrankpoints')
        self.assertIn(b'/getguildrankpoints "1153721648"', got)


class TestPlayerdata(ServerFixture):
    def test_roundtrip(self):
        alice = self.connect('alice')
        # no data yet: server answers with size 0, client generates default
        alice.send_cmd(f'/getplayerdata "alice" "{FORM}"')
        got = alice.wait_for(b'/getplayerdata')
        self.assertIn(f'/getplayerdata "alice" "{FORM}" 0'.encode(), got)
        blob = bytes(range(256))
        alice.send_cmd(
            f'/setplayerdata "alice" "{FORM}" "{len(blob)}" "0" "1"', blob=blob)
        alice.send_cmd(f'/getplayerdata "alice" "{FORM}"')
        got = alice.wait_for(blob, timeout=3)
        self.assertIn(f'/getplayerdata "alice" "{FORM}" {len(blob)}'.encode(),
                      got)
        self.assertIn(blob, got)

    def test_cannot_write_other_players_data(self):
        alice = self.connect('alice')
        self.connect('bob')
        blob = b'EVIL'
        alice.send_cmd(
            f'/setplayerdata "bob" "{FORM}" "{len(blob)}" "0" "1"', blob=blob)
        alice.send_cmd(f'/getplayerdata "alice" "{FORM}"')
        alice.wait_for(b'/getplayerdata')
        self.assertEqual(self.server.db.get_playerdata('bob', FORM), b'')


class TestGames(ServerFixture):
    def create_game(self, host, name='MyGame', password='', npj='1'):
        host.send_cmd(f'/requestcreategame "{name}"')
        self.assertIn(f'/creategame "{name}"'.encode(),
                      host.wait_for(b'/creategame'))
        host.send_cmd(
            f'/creategame "{name}" "{password}" "Net_M_01 null 0 1" '
            f'"translateNet_M_01" "{npj}" "0" "0" "8" '
            f'"x-directplay:/1.0/host/127.0.0.1"')

    def test_create_join_start(self):
        alice = self.connect('alice')
        bob = self.connect('bob')
        carol = self.connect('carol')  # spectator stays in the channel
        self.join_channel(alice)
        self.join_channel(bob)
        self.join_channel(carol)
        bob.drain(0.4)
        self.create_game(alice)
        got = bob.wait_for(b'$game')
        self.assertIn(b'$game "MyGame"', got)
        bob.send_cmd('/joingame "MyGame" ""')
        got = bob.wait_for(b'/joingame')
        self.assertIn(b'/joingame "MyGame" "x-directplay:/1.0/host/127.0.0.1"',
                      got)
        carol.drain(0.4)
        alice.send_cmd('/startinggame')
        # spectators see the players and the game leave the lobby lists
        got = carol.wait_for(b'/gamestatus', timeout=3)
        self.assertIn(b'/gamestatus "MyGame" "1"', got)
        self.assertIn(b'/&gamechanneluser "alice"', got)
        # the players themselves get no lobby messages about their own game
        got = alice.drain(0.6) + bob.drain(0.6)
        self.assertNotIn(b'/gamestatus', got)
        self.assertNotIn(b'/&gamechanneluser', got)

    def test_solo_host_cycle_sends_no_lobby_echo(self):
        # Regression for the real-client crash: a solo host repeatedly
        # creating, starting and stopping games must never receive game
        # list messages about its own game (double removes and self-echoes
        # crashed the 1.7 client after 2-3 cycles).
        alice = self.connect('alice')
        self.join_channel(alice)
        alice.drain(0.4)
        for i in range(3):
            self.create_game(alice, name=f'Game{i}', npj='0')
            alice.send_cmd('/startinggame')
            alice.send_cmd('/stopgame')
            got = alice.drain(0.5)
            for token in (b'game', b'chatchanneluser', b'gamechanneluser'):
                self.assertNotIn(b'$' + token, got)
                self.assertNotIn(b'&' + token, got)
            self.assertNotIn(b'/gamestatus', got)

    def test_wrong_game_password(self):
        alice = self.connect('alice')
        bob = self.connect('bob')
        self.join_channel(alice)
        self.join_channel(bob)
        bob.drain(0.4)
        self.create_game(alice, name='Secret', password='geheim')
        bob.wait_for(b'$game')
        bob.send_cmd('/joingame "Secret" "wrong"')
        got = bob.wait_for(b'/error')
        self.assertIn(b'/error badGamePassword "Secret"', got)

    def test_duplicate_game_name_declined_silently(self):
        alice = self.connect('alice')
        bob = self.connect('bob')
        self.join_channel(alice)
        self.join_channel(bob)
        self.create_game(alice)
        bob.wait_for(b'$game')
        bob.drain(0.4)
        bob.send_cmd('/requestcreategame "MyGame"')
        got = bob.drain(0.6)
        # reference-server parity: no bytes at all for a name collision
        self.assertNotIn(b'/creategame', got)
        self.assertNotIn(b'/error', got)

    def test_leave_game_removes_it(self):
        alice = self.connect('alice')
        bob = self.connect('bob')
        self.join_channel(alice)
        self.join_channel(bob)
        bob.drain(0.4)
        self.create_game(alice)
        bob.wait_for(b'$game')
        alice.send_cmd('/leavegame')
        got = bob.wait_for(b'&game')
        self.assertIn(b'&game "MyGame"', got)


class TestGuilds(ServerFixture):
    """The in-game guild commands.

    Wire formats measured off the official server on 2026-07-30; before this
    the server answered all of them with 'Unknown command', so the guild
    screens in the game did nothing at all.
    """

    def test_ladder_lists_guilds(self):
        self.server.db.create_guild('Nekromanten')
        self.server.db.create_guild('Seelenfaenger')
        alice = self.connect('alice')
        self.join_channel(alice)
        alice.drain(0.4)
        alice.send_cmd('/guildsladder "1"')
        got = alice.wait_for(b'/guildsladder')
        self.assertIn(b'/guildsladder "1"', got)
        self.assertIn(b'"Nekromanten"', got)
        self.assertIn(b'"Seelenfaenger"', got)

    def test_ladder_on_empty_server(self):
        alice = self.connect('alice')
        self.join_channel(alice)
        alice.drain(0.4)
        alice.send_cmd('/guildsladder "1"')
        self.assertIn(b'/guildsladder "1"', alice.wait_for(b'/guildsladder'))

    def test_testcreateguild_answers_with_price(self):
        alice = self.connect('alice')
        self.join_channel(alice)
        alice.drain(0.4)
        alice.send_cmd('/testcreateguild "AlchemyFox"')
        got = alice.wait_for(b'/testcreateguild')
        price = self.server.config.guild_price
        self.assertIn(f'/testcreateguild "AlchemyFox" "{price}"'
                      .encode('latin-1'), got)

    def test_guild_price_is_configurable(self):
        self.server.config.guild_price = 1234
        alice = self.connect('alice')
        self.join_channel(alice)
        alice.drain(0.4)
        alice.send_cmd('/testcreateguild "Teuer"')
        self.assertIn(b'/testcreateguild "Teuer" "1234"',
                      alice.wait_for(b'/testcreateguild'))

    def test_create_then_join_reports_membership(self):
        alice = self.connect('alice')
        bob = self.connect('bob')
        self.join_channel(alice)
        self.join_channel(bob)
        alice.drain(0.4)
        alice.send_cmd('/createguild "Fuchsbau"')
        self.assertIn(b'/userguild "alice" "Fuchsbau"',
                      alice.wait_for(b'/userguild'))
        bob.drain(0.4)
        bob.send_cmd('/joinguild "Fuchsbau" "1" "1"')
        self.assertIn(b'/userguild "bob" "Fuchsbau"',
                      bob.wait_for(b'/userguild'))
        self.assertEqual(self.server.db.member_guilds().get('bob'), 'Fuchsbau')

    def test_founding_via_client_dialog(self):
        # What the real client does: ask the price, then send /joinguild
        # with the new name. Nothing exists in the database beforehand.
        alice = self.connect('alice')
        self.join_channel(alice)
        alice.drain(0.4)
        alice.send_cmd('/testcreateguild "AlchemyFox"')
        alice.wait_for(b'/testcreateguild')
        alice.send_cmd('/joinguild "AlchemyFox" "1" "1"')
        self.assertIn(b'/userguild "alice" "AlchemyFox"',
                      alice.wait_for(b'/userguild'))
        self.assertIn('AlchemyFox',
                      [g['name'] for g in self.server.db.guild_standings()])
        # and it shows up in the ladder afterwards
        alice.send_cmd('/guildsladder "1"')
        self.assertIn(b'"AlchemyFox"', alice.wait_for(b'/guildsladder'))

    def test_join_without_price_query_does_not_create(self):
        # Guards against a typo in the join dialog founding a guild.
        alice = self.connect('alice')
        self.join_channel(alice)
        alice.drain(0.4)
        alice.send_cmd('/joinguild "Vertipper" "1" "1"')
        self.assertIn(b'/userguild "alice" ""', alice.wait_for(b'/userguild'))
        self.assertEqual([g['name'] for g in self.server.db.guild_standings()],
                         [])

    def test_join_unknown_guild_reports_no_membership(self):
        alice = self.connect('alice')
        self.join_channel(alice)
        alice.drain(0.4)
        alice.send_cmd('/joinguild "GibtsNicht" "1" "1"')
        self.assertIn(b'/userguild "alice" ""', alice.wait_for(b'/userguild'))

    def test_leave_guild(self):
        self.server.db.create_guild('Fuchsbau')
        alice = self.connect('alice')
        self.join_channel(alice)
        alice.send_cmd('/joinguild "Fuchsbau" "1" "1"')
        alice.wait_for(b'/userguild')
        alice.send_cmd('/leaveguild')
        self.assertIn(b'/userguild "alice" ""', alice.wait_for(b'/userguild'))
        self.assertIsNone(self.server.db.member_guilds().get('alice'))


class TestBrokenClientId(ServerFixture):
    """A near-empty client id is replaced by a stable derived one.

    Field-observed: such a player shows up in the name list and can chat,
    but other clients never draw a figure for them.
    """

    BROKEN = bytes([0, 0x16] + [0] * 14)

    def test_broken_id_is_replaced(self):
        alice = self.connect('alice', guid=self.BROKEN)
        bob = self.connect('bob')
        self.join_channel(bob)
        bob.drain(0.4)
        hero = b'HERODATA' * 4
        alice.send_cmd(f'/setuserherodata "alice" "{len(hero)}"', blob=hero)
        self.join_channel(alice)
        got = bob.wait_for(b'$gamechanneluser')
        self.assertIn(b'$gamechanneluser "alice"', got)
        self.assertNotIn(b'00001600-0000-0000-0000-000000000000', got)

    def test_replacement_is_stable_per_account(self):
        from tw1mp.protocol import derive_guid, pretty_guid
        first = pretty_guid(derive_guid('alice'))
        self.assertEqual(first, pretty_guid(derive_guid('alice')))
        self.assertNotEqual(first, pretty_guid(derive_guid('bob')))

    def test_valid_id_is_left_alone(self):
        good = bytes(range(16))
        alice = self.connect('alice', guid=good)
        bob = self.connect('bob')
        self.join_channel(bob)
        bob.drain(0.4)
        hero = b'HERODATA' * 4
        alice.send_cmd(f'/setuserherodata "alice" "{len(hero)}"', blob=hero)
        self.join_channel(alice)
        from tw1mp.protocol import pretty_guid
        self.assertIn(pretty_guid(good).encode('latin-1'),
                      bob.wait_for(b'$gamechanneluser'))


if __name__ == '__main__':
    unittest.main()
