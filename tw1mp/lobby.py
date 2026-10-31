"""Lobby state: users, chat channels, game channels and hosted games.

All mutations happen under GameState.lock; message delivery to sockets is
delegated to the server's MessageDistributor so no network I/O happens
while the lock is held.
"""

import datetime
import logging
import random
import threading

from .protocol import em, pretty_guid, guid_is_broken, derive_guid, NUL

log = logging.getLogger('tw1mp.lobby')

# The official server offers three, measured 2026-07-30; this server only
# ever created the first two. Index 0 stays the channel joined on arrival.
DEFAULT_CHATS = ['translateNetCityMainChannel', 'translateNetCityTradeChannel',
                 'translateNetCityChatChannel']

# Upper bound on player-created chat channels per town, so a client cannot
# fill the list without limit.
MAX_CHAT_CHANNELS = 32

# A user id is announced decimal in $gamechanneluser but hex in /updheropos
# — the same number in two bases, inherited from the reference server. Field
# test on 2026-07-31 (three clients seeing each other) showed the client
# copes with it, so the full range is back in use.
MAX_USER_ID = 0x8000


def _without(userlist, con):
    return [c for c in userlist if c is not con]


class User:
    """Per-login state, attached to a ConnectionHandler."""

    def __init__(self, name, con, state):
        self.state = state
        self.herodata = b''
        self.posdata = None
        self.poschanged = False
        self.requestedChannel = None
        self.gamechannel = None
        self.chatchannel = None
        self.requestedGame = None
        self.game = None
        # Guild name from a pending /testcreateguild, so the /joinguild that
        # follows is understood as founding rather than joining.
        self.pending_guild = ''
        # Own guild, sent in the login welcome and as field 2 of the user
        # lists ($gamechanneluser, $chatchanneluser, /whois); the client
        # takes the guild for "accept invitation" from there (exe 0x424940).
        try:
            self.guild = state.server.db.member_guilds().get(name, '')
        except AttributeError:
            self.guild = ''
        self.name = name
        self.loginTime = datetime.datetime.now()
        self.idnum = state.acquire_uid()
        self.connection = con
        # Some installations announce a near-empty id; other clients then
        # never draw a figure for that player (the name and chat still work).
        # Substituting a stable derived id makes them visible again.
        if guid_is_broken(con.guid):
            self.pguid = pretty_guid(derive_guid(name))
            log.info('Replaced unusable client id of %s with %s',
                     name, self.pguid)
        else:
            self.pguid = pretty_guid(con.guid)

    def leaveChannel(self):
        if self.requestedChannel:
            self.requestedChannel.dropRequest(self.connection)
        if self.gamechannel:
            self.gamechannel.leaveChannel(self.connection)  # also leaves chat

    def leaveChat(self):
        if self.chatchannel:
            self.chatchannel.remove(self.connection)

    def stopGame(self):
        if self.requestedGame and self.gamechannel:
            self.gamechannel.gameRequests.pop(self.requestedGame, None)
            self.requestedGame = None
        if self.game:
            self.game.remove(self.connection)

    def disconnect(self, server):
        self.stopGame()
        self.leaveChannel()
        self.leaveChat()  # a guild chat is not tied to a town
        if server.state.activeUsers.get(self.name) is self.connection:
            del server.state.activeUsers[self.name]
        self.state.release_uid(self.idnum)

    def getGCUmsg(self):
        hdl = len(self.herodata)
        if hdl == 0:
            return b''
        # Field 2 is the guild: 0x422f90 reads it with
        # translateENUserGuildFormat.
        return em(f'$gamechanneluser "{self.name}" "{self.guild}" "100" '
                  f'"{self.idnum}" "0" "{self.pguid}" "{self.posdata}" '
                  f'"{hdl}"') + self.herodata

    def getCCUmsg(self):
        # Four fields, as measured off the official server on 2026-07-30:
        #   $chatchanneluser "marco1994222" "" "0" "2d7da871-1d9e-..."
        # The empty field is the guild there too (the measured player had
        # none). This server used to announce chat users by name alone.
        return em(f'$chatchanneluser "{self.name}" "{self.guild}" "0" '
                  f'"{self.pguid}"')


class ChatChannel:
    def __init__(self, parent, name, password=''):
        self.parent = parent  # GameChannel
        self.name = name
        self.password = password
        self.userlist = []

    def join(self, con):
        """Move con into this chat. Returns the response bytes for con."""
        con.user.leaveChat()
        con.server.dist.add({
            'target': list(self.userlist),
            'message': con.user.getCCUmsg()})
        self.userlist.append(con)
        con.user.chatchannel = self
        chunks = [self.joined_reply()]
        for ucon in self.userlist:
            if ucon is not con:
                chunks.append(ucon.user.getCCUmsg())
        return b''.join(chunks)

    def joined_reply(self):
        # Letztes Feld ist die Teilnehmerzahl, nicht die feste 1. Gemessen
        # am Originalserver: /joinchatchannel "...MainChannel" "" "2"
        # Field 2 lands in the same list slot as the "XXX" of $chatchannel
        # (exe 0x424200); an empty one would drop the lock, so the password
        # prompt would not come back on the next join.
        haspass = 'XXX' if self.password else ''
        return em(f'/joinchatchannel "{self.name}" "{haspass}" '
                  f'"{len(self.userlist)}"')

    def enum_entry(self):
        haspass = 'XXX' if self.password else ''
        return em(f'$chatchannel "{self.name}" "{haspass}" '
                  f'"{len(self.userlist)}"')

    def remove(self, con):
        if con in self.userlist:
            self.userlist.remove(con)
            con.server.dist.add({
                'target': list(self.userlist),
                'message': em(f'&chatchanneluser "{con.user.name}"')})
        con.user.chatchannel = None
        if not self.userlist:
            self.emptied(con)

    def emptied(self, con):
        # A player-made channel disappears once the last one leaves; the
        # default channels always stay.
        if self.name not in DEFAULT_CHATS:
            self.parent.chatChannels.pop(self.name, None)
            tg = list(self.parent.userlist)
            if tg:
                con.server.dist.add({
                    'target': tg,
                    'message': em(f'&chatchannel "{self.name}"')})


class GuildChat(ChatChannel):
    """The chat of one guild, one per guild for the whole server (Marco,
    25.10.2026): members in any town who picked the guild channel read
    each other.

    The client enters it with /joinguild "G" "0" "1" (exe 0x5c8fad) and
    expects /joinguildchannel "G" "x" back (0x72fd68 -> 0x4244d0: chat
    index -2, own entry, then the members per $chatchanneluser). Chat
    lines still arrive as plain /send, so routing is the server's job.
    """

    def __init__(self, state, guild):
        super().__init__(None, guild)
        self.state = state

    def joined_reply(self):
        return em(f'/joinguildchannel "{self.name}" "{len(self.userlist)}"')

    def emptied(self, con):
        if self.state.guildChats.get(self.name) is self:
            del self.state.guildChats[self.name]


class GameEntry:
    def __init__(self, parent, name, host, pasw, mapp, mapt, npj, un1,
                 status, maxplayers, url):
        if host.user.game:
            host.user.game.remove(host)
        self.parent = parent  # GameChannel
        self.gname = name
        self.host = host
        self.password = pasw
        self.mapPar = mapp            # "Net_M_01 null 0 1"
        self.mapTranslate = mapt      # "translateNet_M_01"
        self.npj = int(npj)           # allow new players to join running game
        self.un1 = int(un1)           # unknown (guild game?)
        self.status = int(status)     # 1 once started
        self.maxplayers = int(maxplayers)
        self.url = url                # x-directplay url of the hosting client
        self.params = ('', '')        # last /setgameparams of the host
        self.userlist = [host]
        self.parent.games[self.gname] = self
        host.user.game = self
        # Advertise to the rest of the channel only. The field-tested solo
        # server never echoed game-list messages back at the acting client;
        # doing so (as TW1CS did, untested) crashed the real client after a
        # few create/stop cycles.
        msg = self.getGameString()
        tg = _without(self.parent.userlist, host)
        if msg and tg:
            self.parent.server.dist.add({'target': tg, 'message': msg})

    def addUser(self, con, pasw):
        # The client's /error table (exe 0xa84888, 21 tokens) has a
        # translated message for these; the reference server declined
        # silently and left the player waiting.
        if len(self.userlist) >= self.maxplayers:
            return em(f'/error cantJoinGame "{self.gname}"')
        if self.status and not self.npj:
            return em(f'/error cantJoinGame "{self.gname}"')
        if self.password != pasw:
            return em(f'/error badGamePassword "{self.gname}"')
        self.userlist.append(con)
        con.user.game = self
        joined = em(f'$gameuser "{self.gname}" "{con.user.name}" "" "100" "0"')
        if self.npj:
            tg = _without(self.parent.userlist, con)
            if tg:
                con.server.dist.add({'target': tg, 'message': joined})
        return em(f'/joingame "{self.gname}" "{self.url}" "{self.status}"')

    def remove(self, con):
        if con in self.userlist:
            self.userlist.remove(con)
        con.user.game = None
        # Only channel members outside this game get the removal; players
        # of the game itself never receive lobby messages about it.
        tg = [c for c in self.parent.userlist
              if c is not con and c not in self.userlist]
        if len(self.userlist) == 0:
            leavemsg = em(f'&game "{self.gname}"')
            self.parent.games.pop(self.gname, None)
        else:
            leavemsg = em(f'&gameuser "{con.user.name}"')
        if self.status and not self.npj:
            # A started closed game was already removed from everyone's
            # lists via /&game at start; a second remove for the now
            # nonexistent entry crashed the real client.
            leavemsg = None
        if leavemsg and tg:
            self.parent.server.dist.add({'target': tg, 'message': leavemsg})

    def startGame(self, con=None):
        if not (con and self.host is con):
            return None  # only the host may start
        self.status = 1
        # The players entering the game get no lobby messages about it
        # (matching the field-tested solo server); only spectators in the
        # channel see the players and the game leave the lists.
        tg = [c for c in self.parent.userlist if c not in self.userlist]
        if not tg:
            return None
        for c in self.userlist:
            un = c.user.name
            self.parent.server.dist.add({
                'target': tg,
                'message': em(f'/&chatchanneluser "{un}"') +
                           em(f'/&gamechanneluser "{un}"')})
        if self.npj:
            umsg = em(f'/gamestatus "{self.gname}" "1"')
        else:
            umsg = em(f'/&game "{self.gname}"')
        self.parent.server.dist.add({'target': tg, 'message': umsg})
        return None

    def _getUserlist(self):
        return ' '.join(f'"{c.user.name}" "" "100" "0"' for c in self.userlist)

    def getGameString(self):
        if self.status and not self.npj:
            return None  # running closed games are not advertised
        pasw = 'XXX' if self.password else ''
        return em(f'$game "{self.gname}" "{pasw}" "{self.mapPar}" '
                  f'"{self.mapTranslate}" "{self.un1}" "{self.status}" '
                  f'"{self.maxplayers}" {self._getUserlist()}')

    def debug_dict(self):
        return {
            'name': self.gname,
            'host': self.host.user.name,
            'status': self.status,
            'hasPassword': 1 if self.password else 0,
            'users': tuple(c.user.name for c in self.userlist),
            'town': self.parent.name,
            'parameters': self.mapPar,
            'mapName': self.mapTranslate,
            'canJoinRunning': self.npj,
        }


class GameChannel:
    """One lobby 'town' instance (map + channel index)."""

    def __init__(self, server, chnName):
        self.server = server
        self.name = chnName
        self.maxuser = server.config.max_channel_users
        self.userlist = []
        self.chatChannels = {}
        self.games = {}
        self.requested = []
        self.gameRequests = {}
        self.dirty = False
        for cn in DEFAULT_CHATS:
            self.chatChannels[cn] = ChatChannel(self, cn)

    def requestJoin(self, con):
        con.user.leaveChannel()
        elen = len(self.userlist) + len(self.requested)
        if elen < self.maxuser:
            self.requested.append(con)
            con.user.requestedChannel = self
            return True
        return False

    def dropRequest(self, con):
        if con in self.requested:
            self.requested.remove(con)
        con.user.requestedChannel = None

    def requestCreateGame(self, con, gameName):
        if con.user.requestedGame or con.user.game:
            con.user.stopGame()
        # Name collisions: gameAlreadyExist is in the client's /error table
        # (exe 0xa84888), so it shows a message instead of waiting.
        if gameName in self.gameRequests and self.gameRequests[gameName] is not con:
            return em(f'/error gameAlreadyExist "{gameName}"')
        if gameName in self.games:
            return em(f'/error gameAlreadyExist "{gameName}"')
        self.gameRequests[gameName] = con
        con.user.requestedGame = gameName
        return em(f'/creategame "{gameName}"')

    def createGame(self, gameName, host, pasw, mapp, mapt, npj, un1, un2,
                   un3, url):
        reqHost = self.gameRequests.get(gameName)
        if reqHost is None or reqHost is not host:
            return None
        GameEntry(self, gameName, host, pasw, mapp, mapt, npj, un1, un2,
                  un3, url)
        host.user.requestedGame = None
        del self.gameRequests[gameName]
        return None

    def leaveChannel(self, con):
        if con in self.userlist:
            con.user.stopGame()
            con.user.leaveChat()
            self.userlist.remove(con)
            leavemsg = em(f'&gamechanneluser "{con.user.name}"')
            con.server.dist.add({'target': list(self.userlist),
                                 'message': leavemsg})
            con.user.gamechannel = None

    def joinChannel(self, con, nam):
        """Move con from the request queue into the channel."""
        if con not in self.requested:
            return None
        self.userlist.append(con)
        con.user.gamechannel = self
        self.requested.remove(con)
        con.user.requestedChannel = None
        retmsg = em(f'/joingamechannel "{nam}" "{len(self.userlist)}"')
        # enumerate herodata of present users
        for user in self.userlist:
            if user is not con:
                retmsg += user.user.getGCUmsg()
        retmsg += self.joinChat(con, DEFAULT_CHATS[0])
        retmsg += self.enumChats()
        retmsg += self.enumGames()
        # broadcast the newcomer's herodata to everyone else
        con.server.dist.add({
            'target': _without(self.userlist, con),
            'message': con.user.getGCUmsg()})
        return retmsg

    def joinChat(self, con, nam, pas='', create=False):
        # Error tokens from the client's table (exe 0xa84888); the old
        # 'badChatPassword' is not in it and showed as raw text.
        chat = self.chatChannels.get(nam)
        if chat is None:
            if not nam:
                return b''
            if not create:
                return em(f'/error channelNotFound "{nam}"')
            if len(self.chatChannels) >= MAX_CHAT_CHANNELS:
                return em(f'/error notAvailable "{nam}"')
            chat = ChatChannel(self, nam, pas)
            self.chatChannels[nam] = chat
            log.info('%s created chat channel %r in %s%s',
                     con.user.name, nam, self.name,
                     ' (password protected)' if pas else '')
            # Everyone else in the town gets the new channel in their list.
            tg = _without(self.userlist, con)
            if tg:
                self.server.dist.add({'target': tg,
                                      'message': chat.enum_entry()})
        elif create and nam not in DEFAULT_CHATS:
            return em(f'/error channelAlreadyExist "{nam}"')
        elif chat.password and chat.password != pas:
            return em(f'/error invalidPasswordChannel "{nam}"')
        return chat.join(con)

    def enumChats(self):
        return b''.join(chat.enum_entry()
                        for chat in self.chatChannels.values())

    def enumGames(self):
        chunks = []
        for game in self.games.values():
            gamestr = game.getGameString()
            if gamestr:
                chunks.append(gamestr)
        return b''.join(chunks)

    def updatePos(self, md):
        if not self.dirty:
            return
        self.dirty = False
        movers = [u for u in self.userlist if u.user.poschanged]
        for ucon in movers:
            ucon.user.poschanged = False
        if not movers:
            return
        # Everyone in the channel gets every mover's position, the mover
        # included. Measured off the official server on 2026-07-30 with a
        # single player in the channel:
        #     C->S /updheropos "1E38#345F"
        #     S->C /updheropos 8925#1E38#345F
        # It echoes, and unquoted. Suppressing that echo (as this server
        # did since 2026-07-18) plausibly leaves the client without any way
        # to learn which id is its own.
        chunks = [f'{m.user.idnum:x}#{m.user.posdata}' for m in movers]
        md.add({'target': list(self.userlist),
                'message': em('/updheropos ' + ' '.join(chunks))})

    def debug_arr_games(self):
        return [g.debug_dict() for g in self.games.values()]

    def debug_dict(self):
        return {
            'users': tuple(c.user.name for c in self.userlist),
            'maxUsers': self.maxuser,
            'games': tuple(self.games),
        }


class GameState:
    def __init__(self, server):
        self.server = server
        self.lock = threading.RLock()
        self.activeUsers = {}
        self.gameChannels = {}
        self.guildChats = {}  # guild name -> GuildChat, while someone is in it
        self._usedUids = set()
        for name in server.config.maps:
            for i in range(server.config.channels_per_map):
                chnName = f'{name}#translate{name}_Channel_{1 + i:02d}'
                self.gameChannels[chnName] = GameChannel(server, chnName)

    def acquire_uid(self):
        with self.lock:
            for num in range(1, MAX_USER_ID + 1):
                if num not in self._usedUids:
                    self._usedUids.add(num)
                    return num
            num = max(self._usedUids) + 1
            self._usedUids.add(num)
            return num

    def guildChat(self, guild):
        chat = self.guildChats.get(guild)
        if chat is None:
            chat = self.guildChats[guild] = GuildChat(self, guild)
        return chat

    def release_uid(self, num):
        with self.lock:
            self._usedUids.discard(num)

    def enumerateGC(self):
        chns = []
        for chnName, chn in self.gameChannels.items():
            chns.append(f'$gamechannel "{chnName}" "{len(chn.userlist)}" '
                        f'"{chn.maxuser}" "0" "0"'.encode('latin-1'))
        return NUL.join(chns) + NUL

    def updatePos(self):
        md = self.server.dist
        with self.lock:
            for chn in self.gameChannels.values():
                chn.updatePos(md)
