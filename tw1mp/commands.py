"""Lobby text command dispatch.

Commands arrive as NUL-terminated strings like:
    /joingamechannel "Net_T_01#translateNet_T_01_Channel_01" "80A2#7F11"
Some are followed by a raw binary blob whose size is given as an argument.

Handlers run on the connection's thread. Handlers that carry a blob must
read it from the socket *before* taking the state lock.
"""

import logging
import re

from .lobby import _without
from .protocol import em

log = logging.getLogger('tw1mp.cmd')

_RE_CMD = re.compile(r'(?:"([^"]*)")|([^\s]+)')

_RE_NAME_OK = re.compile(r'^[^"\s]{1,32}$')


def _arg(res, i, default=''):
    return res[i] if len(res) > i else default


class CommandParser:
    def __init__(self, server):
        self.server = server
        self.commandlist = {
            '/nop': self._nop,
            '/leavegamechannel': self._leavegamechannel,
            '/requestjoingamechannel': self._requestjoingamechannel,
            '/joingamechannel': self._joingamechannel,
            '/joinchatchannel': self._joinchatchannel,
            '/leavechatchannel': self._leavechatchannel,
            '/updheropos': self._updheropos,
            '/send': self._send,
            '/whois': self._whois,
            '/update': self._update,
            '/nick': self._nick,
            '/getguildrankpoints': self._getguildrankpoints,
            '/guildsladder': self._guildsladder,
            '/ladder': self._guildsladder,
            '/testcreateguild': self._testcreateguild,
            '/creatguild': self._createguild,
            '/createguild': self._createguild,
            '/joinguild': self._joinguild,
            '/leaveguild': self._leaveguild,
            '/requestcreategame': self._requestcreategame,
            '/creategame': self._creategame,
            '/stopgame': self._stopgame,
            '/leavegame': self._stopgame,
            '/startinggame': self._startinggame,
            '/startgame': self._startgame,
            '/getplayerdata': self._getplayerdata,
            '/setplayerdata': self._setplayerdata,
            '/setuserherodata': self._setuserherodata,
            '/gamecommandtouser': self._gamecommandtouser,
            '/joingame': self._joingame,
        }

    def parse(self, data, con):
        res = [itm[0] + itm[1] for itm in _RE_CMD.findall(data)]
        if res and res[0] in self.commandlist:
            return self.commandlist[res[0]](con, res)
        log.info('Unknown command from %s: %r', con.user.name, res)
        return None

    # -- simple ---------------------------------------------------------

    def _nop(self, con, res):
        return None

    def _getguildrankpoints(self, con, res):
        from .protocol import guild_rank_points
        (a, b, c, d) = guild_rank_points()
        return em(f'/getguildrankpoints "{a}" "{b}" "{c}" "{d}"')

    # -- channels -------------------------------------------------------

    def _leavegamechannel(self, con, res):
        with self.server.state.lock:
            chnl = con.user.gamechannel
            if chnl:
                chnl.leaveChannel(con)
            return self.server.state.enumerateGC()

    def _requestjoingamechannel(self, con, res):
        name = _arg(res, 1)
        with self.server.state.lock:
            chnl = self.server.state.gameChannels.get(name)
            if chnl and chnl.requestJoin(con):
                return em(f'/requestjoingamechannel "{name}" "1"')
            return em(f'/requestjoingamechannel "{name}" "0"')

    def _joingamechannel(self, con, res):
        name = _arg(res, 1)
        with self.server.state.lock:
            chnl = self.server.state.gameChannels.get(name)
            if chnl is None:
                return None
            con.user.posdata = _arg(res, 2)
            return chnl.joinChannel(con, name)

    # -- guilds ---------------------------------------------------------
    #
    # Formats measured off the official server on 2026-07-30:
    #   C->S /guildsladder "1"
    #   S->C /guildsladder "1" "~EDELWEIS~" "1" "10400" "1632" "22" "12808"
    #        "582" "1356618870" "50" "Nekromancers" "2" ...
    #        i.e. the page number, then per guild a name plus eight numbers.
    #   C->S /testcreateguild "AlchemyFox"
    #   S->C /testcreateguild "AlchemyFox" "5000"      (price to found one)
    #   C->S /joinguild "" "1" "1"
    #   S->C /userguild "marco19942" ""                ("" = in no guild)
    # Only the first two numbers per guild have a known meaning (rank and
    # points); the rest are filled with member counts and zeroes, which the
    # client accepts.

    GUILD_PAGE = 20

    def _guild_of(self, name):
        return self.server.db.member_guilds().get(name, '')

    def _guildsladder(self, con, res):
        try:
            page = max(1, int(_arg(res, 1, '1')))
        except ValueError:
            page = 1
        standings = self.server.db.guild_standings()
        # Rank by member count, the only score this server tracks so far.
        standings.sort(key=lambda g: (-len(g.get('members') or ()),
                                      g['name'].lower()))
        first = (page - 1) * self.GUILD_PAGE
        chunk = standings[first:first + self.GUILD_PAGE]
        parts = [f'/guildsladder "{page}"']
        for i, g in enumerate(chunk, start=first + 1):
            members = len(g.get('members') or ())
            parts.append(f'"{g["name"]}" "{i}" "{members}" "0" '
                         f'"{members}" "0" "0" "0" "50"')
        return em(' '.join(parts))

    def _testcreateguild(self, con, res):
        # The client asks what founding costs. The official server answered
        # 5000; guild_price in Config.ini decides here, 0 = free.
        # This is also how the founding dialog announces itself: the actual
        # creation arrives afterwards as /joinguild with the same name, so
        # remember the intent to tell it apart from joining someone else's.
        name = _arg(res, 1).strip()
        con.user.pending_guild = name
        return em(f'/testcreateguild "{name}" '
                  f'"{self.server.config.guild_price}"')

    def _createguild(self, con, res):
        name = _arg(res, 1).strip()
        if not name:
            return None
        created = self.server.db.create_guild(name)
        if created:
            log.info('%s founded guild %r', con.user.name, name)
        # Joining is what the client cares about; founding implies membership.
        self.server.db.set_member_guild(con.user.name, name)
        return em(f'/userguild "{con.user.name}" "{name}"')

    def _joinguild(self, con, res):
        # Carries the guild name in one of the first arguments. It is sent
        # both for joining an existing guild and, right after
        # /testcreateguild, for founding a new one.
        known = {g['name'] for g in self.server.db.guild_standings()}
        pending = getattr(con.user, 'pending_guild', '')
        wanted = ''
        for i in (1, 2, 3):
            cand = _arg(res, i).strip()
            if not cand:
                continue
            if cand in known:
                wanted = cand
                break
            if cand == pending:
                if self.server.db.create_guild(cand):
                    log.info('%s founded guild %r', con.user.name, cand)
                wanted = cand
                break
        con.user.pending_guild = ''
        if wanted:
            self.server.db.set_member_guild(con.user.name, wanted)
        current = self._guild_of(con.user.name)
        return em(f'/userguild "{con.user.name}" "{current}"')

    def _leaveguild(self, con, res):
        self.server.db.set_member_guild(con.user.name, '')
        return em(f'/userguild "{con.user.name}" ""')

    def _joinchatchannel(self, con, res):
        # /joinchatchannel "name" "password" "create"
        # The last field is 1 when the client wants the channel created (the
        # "new channel" dialog) and 0 when picking an existing one from the
        # list. It used to be ignored, so creating a channel did nothing at
        # all - the client waited for a reply that never came.
        name = _arg(res, 1)
        password = _arg(res, 2)
        create = _arg(res, 3, '0') == '1'
        with self.server.state.lock:
            chnl = con.user.gamechannel
            if chnl is None:
                return None
            ret = chnl.joinChat(con, name, password, create=create)
            return ret or None

    def _leavechatchannel(self, con, res):
        with self.server.state.lock:
            con.user.leaveChat()
        return None

    def _updheropos(self, con, res):
        with self.server.state.lock:
            con.user.posdata = _arg(res, 1)
            if con.user.gamechannel:
                con.user.gamechannel.dirty = True
                con.user.poschanged = True
        return None

    # -- chat / social --------------------------------------------------

    def _send(self, con, res):
        text = _arg(res, 1)
        with self.server.state.lock:
            chat = con.user.chatchannel
            if not chat:
                return None
            self.server.dist.add({
                'target': list(chat.userlist),
                'message': em(f'/send "{con.user.name}" "{text}"')})
        return None

    def _whois(self, con, res):
        name = _arg(res, 1)
        info = self.server.db.get_userinfo(name)
        if info is None:
            return em(f'/admin Unknown user: {name}')
        (email, location, age, gender, description) = info
        town = ''
        chatname = ''
        with self.server.state.lock:
            tcon = self.server.state.activeUsers.get(name)
            if tcon and tcon.user:
                if tcon.user.gamechannel:
                    town = tcon.user.gamechannel.name
                if tcon.user.chatchannel:
                    chatname = tcon.user.chatchannel.name
        return em(f'/whois "{name}" "" "{town}" "{chatname}" "{email}" '
                  f'"{location}" {age} {gender} "{description}"')

    def _update(self, con, res):
        # /update "name" "email" "location" "age" "gender" "description"
        name = _arg(res, 1)
        if name != con.user.name:
            return em('/admin You can only update your own details')
        self.server.db.update_userinfo(name, _arg(res, 2), _arg(res, 3),
                                       _arg(res, 4), _arg(res, 5), _arg(res, 6))
        return None

    def _nick(self, con, res):
        name = _arg(res, 1)
        if not _RE_NAME_OK.match(name):
            return em('/admin Invalid username')
        return em('/admin Nickname changes are not supported on this server')

    # -- playerdata -----------------------------------------------------

    def _getplayerdata(self, con, res):
        name = _arg(res, 1)
        form = _arg(res, 2)
        if name != con.user.name:
            log.info('%s tried to fetch playerdata of %s', con.user.name, name)
            return None
        # A modified character variant is only ever served to a player who
        # is alone on the server; the choice then sticks for the session so
        # a later save can never overwrite the original. The admin bot is
        # not a real player, so it must not make everyone look accompanied.
        bot = self.server.config.bot_name
        with self.server.state.lock:
            humans = [n for n in self.server.state.activeUsers if n != bot]
        alone = len(humans) == 1
        use_modded = alone and \
            self.server.db.has_modded_playerdata(name, form)
        con.use_modded = use_modded
        if use_modded:
            log.info('Serving modified character to %s (solo session)', name)
        pd = self.server.db.get_playerdata(name, form, modded=use_modded)
        return em(f'/getplayerdata "{name}" "{form}" {len(pd)}') + pd

    def _setplayerdata(self, con, res):
        # /setplayerdata "name" "form" "blobsize" "unk" "unk2"\0(PLAYERDATA)
        pd = con.readBlob(_arg(res, 3, '0'))
        name = _arg(res, 1)
        if name == con.user.name:
            self.server.db.set_playerdata(name, _arg(res, 2), pd,
                                          modded=getattr(con, 'use_modded',
                                                         False))
        else:
            log.info('%s tried to store playerdata of %s', con.user.name, name)
        return None

    def _setuserherodata(self, con, res):
        pd = con.readBlob(_arg(res, 2, '0'))
        with self.server.state.lock:
            con.user.herodata = pd
            posdata = con.user.posdata
            if con.user.gamechannel:
                msg = con.user.getGCUmsg()
                tg = _without(con.user.gamechannel.userlist, con)
                self.server.dist.add({'target': tg, 'message': msg})
        # Keep one real blob on disk. A user without herodata is invisible to
        # everyone (getGCUmsg returns nothing), so the admin bot needs a
        # genuine sample to appear as a character rather than a ghost.
        if pd:
            self.server.save_herodata_sample(pd, posdata)
        return None

    # -- games ----------------------------------------------------------

    def _requestcreategame(self, con, res):
        with self.server.state.lock:
            if not con.user.gamechannel:
                return None
            return con.user.gamechannel.requestCreateGame(con, _arg(res, 1))

    def _creategame(self, con, res):
        # /creategame "name" "pass" "mapPar" "mapTranslate" npj un1 status
        #             maxplayers "directplay-url"
        if len(res) < 10:
            return None
        with self.server.state.lock:
            if not con.user.gamechannel:
                return None
            return con.user.gamechannel.createGame(
                res[1], con, res[2], res[3], res[4], res[5], res[6], res[7],
                res[8], res[9])

    def _joingame(self, con, res):
        with self.server.state.lock:
            if not con.user.gamechannel:
                return None
            gm = con.user.gamechannel.games.get(_arg(res, 1))
            if gm is None:
                return None
            return gm.addUser(con, _arg(res, 2))

    def _stopgame(self, con, res):
        with self.server.state.lock:
            if con.user.game:
                return con.user.game.remove(con)
        return None

    def _startinggame(self, con, res):
        with self.server.state.lock:
            if con.user.game:
                return con.user.game.startGame(con)
        return None

    def _startgame(self, con, res):
        # /startgame "Countdown" "Host?" "GameStatus" - informational only;
        # the actual state transition happens via /startinggame.
        return None

    def _gamecommandtouser(self, con, res):
        dat = con.readBlob(_arg(res, 2, '0'))
        target = _arg(res, 1)
        # Relay to any connected player regardless of state, to support
        # modded use cases.
        with self.server.state.lock:
            tcon = self.server.state.activeUsers.get(target)
            if not tcon:
                return None
            fulmsg = em(f'/gamecommandtouser "{con.user.name}" "{len(dat)}"') + dat
            self.server.dist.add({'target': (tcon,), 'message': fulmsg})
        return None
