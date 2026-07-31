"""Lobby state: users, chat channels, game channels and hosted games.

All mutations happen under GameState.lock; message delivery to sockets is
delegated to the server's MessageDistributor so no network I/O happens
while the lock is held.
"""

import datetime
import logging
import random
import threading

from .protocol import em, pretty_guid, NUL

log = logging.getLogger('tw1mp.lobby')

# The official server offers three, measured 2026-07-30; this server only
# ever created the first two. Index 0 stays the channel joined on arrival.
DEFAULT_CHATS = ['translateNetCityMainChannel', 'translateNetCityTradeChannel',
                 'translateNetCityChatChannel']

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
        self.name = name
        self.loginTime = datetime.datetime.now()
        self.idnum = state.acquire_uid()
        self.connection = con
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
        if server.state.activeUsers.get(self.name) is self.connection:
            del server.state.activeUsers[self.name]
        self.state.release_uid(self.idnum)

    def getGCUmsg(self):
        hdl = len(self.herodata)
        if hdl == 0:
            return b''
        return em(f'$gamechanneluser "{self.name}" "" "100" "{self.idnum}" '
                  f'"0" "{self.pguid}" "{self.posdata}" "{hdl}"') + self.herodata

    def getCCUmsg(self):
        # Four fields, as measured off the official server on 2026-07-30:
        #   $chatchanneluser "marco1994222" "" "0" "2d7da871-1d9e-..."
        # This server used to announce chat users by name alone.
        return em(f'$chatchanneluser "{self.name}" "" "0" "{self.pguid}"')


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
        # Letztes Feld ist die Teilnehmerzahl, nicht die feste 1. Gemessen
        # am Originalserver: /joinchatchannel "...MainChannel" "" "2"
        chunks = [em(f'/joinchatchannel "{self.name}" "" "{len(self.userlist)}"')]
        for ucon in self.userlist:
            if ucon is not con:
                chunks.append(ucon.user.getCCUmsg())
        return b''.join(chunks)

    def remove(self, con):
        if con in self.userlist:
            self.userlist.remove(con)
            con.server.dist.add({
                'target': list(self.userlist),
                'message': em(f'&chatchanneluser "{con.user.name}"')})
        con.user.chatchannel = None


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
        # Full/already-running games are declined silently, matching the
        # reference server; 'badGamePassword' is the only /error token the
        # real client is known to accept.
        if len(self.userlist) >= self.maxplayers:
            return None
        if self.status and not self.npj:
            return None
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
        # Name collisions are declined silently, matching the reference
        # server (an unknown /error token could confuse the real client).
        if gameName in self.gameRequests and self.gameRequests[gameName] is not con:
            return None
        if gameName in self.games:
            return None
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

    def joinChat(self, con, nam, pas=''):
        chat = self.chatChannels.get(nam)
        if chat is None:
            return b''
        if chat.password and chat.password != pas:
            return em(f'/error badChatPassword "{nam}"')
        return chat.join(con)

    def enumChats(self):
        chunks = []
        for chat in self.chatChannels.values():
            haspass = 'XXX' if chat.password else ''
            chunks.append(
                f'$chatchannel "{chat.name}" "{haspass}" "{len(chat.userlist)}"'
                .encode('latin-1'))
        return NUL.join(chunks) + NUL

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
