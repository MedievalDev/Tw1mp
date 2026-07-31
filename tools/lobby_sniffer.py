"""Mitschneidender Zwischenserver fuer eine echte Lobby-Sitzung.

Der Spiel-Client verbindet sich hierher, dieses Skript reicht alles an den
echten Server weiter und protokolliert beide Richtungen im Klartext. So laesst
sich sehen, wie der Originalserver seine Nachrichten wirklich formatiert.

WICHTIG - Zugangsdaten: Die Login-Pakete werden NICHT protokolliert. Der
Mitschnitt beginnt je Richtung erst, sobald das erste Textkommando ueber die
Leitung geht; die zlib-Pakete der Anmeldung (mit Benutzername und Passwort)
werden nur durchgereicht.

Aufruf:
    python tools/lobby_sniffer.py
    python tools/lobby_sniffer.py --upstream netserver.2-worlds.com
"""

import argparse
import datetime
import os
import re
import socket
import socketserver
import threading

DEFAULT_UPSTREAM = 'warnet.2-worlds.com'
LOBBY_PORT = 17171

# Ein Textkommando: '/name' oder '$name', danach nur DRUCKBARE Zeichen.
# Wichtig: auf '$gamechanneluser ... "168"' folgen direkt die binaeren
# Heldendaten. Ein Muster wie [^\x00]* liest in diesen Block hinein und der
# Treffer sieht dann aus wie Binaermuell - so ist mir genau diese Nachricht
# im ersten Anlauf komplett durchgerutscht.
CMD = re.compile(rb'[/$&][A-Za-z][A-Za-z0-9_]{2,}[\x20-\x7e]*')
# Ab hier gilt die Verbindung als angemeldet (Login-Pakete sind zlib-Binaer).
LOGIN_DONE = re.compile(rb'[/$&][A-Za-z][A-Za-z0-9_]{4,} ')

_write_lock = threading.Lock()
_logfile = None


def note(direction, cid, data):
    """Erkennbare Textkommandos aus einem Datenblock protokollieren."""
    stamp = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
    lines = []
    for m in CMD.finditer(data):
        raw = m.group(0)
        text = raw.decode('latin-1', 'replace')
        if len(text) > 600:
            text = text[:600] + f'... [+{len(text) - 600} Zeichen]'
        lines.append(f'{stamp} [{cid}] {direction} {text}')
    if not lines:
        return
    block = '\n'.join(lines) + '\n'
    with _write_lock:
        _logfile.write(block)
        _logfile.flush()
    # Die Windows-Konsole kann nicht jedes Zeichen darstellen; ein
    # Kodierfehler hier darf die Verbindung nicht abreissen lassen.
    try:
        print(block.encode('ascii', 'replace').decode('ascii'), end='')
    except Exception:
        pass


class Handler(socketserver.BaseRequestHandler):
    counter = 0

    def handle(self):
        Handler.counter += 1
        cid = Handler.counter
        peer = self.client_address[0]
        print(f'--- [{cid}] Client {peer} verbunden, verbinde zu '
              f'{self.server.upstream_host}:{self.server.upstream_port}')
        try:
            up = socket.create_connection(
                (self.server.upstream_host, self.server.upstream_port), 15)
        except OSError as exc:
            print(f'--- [{cid}] Upstream nicht erreichbar: {exc}')
            return
        with _write_lock:
            _logfile.write(f'=== [{cid}] neue Sitzung von {peer} ===\n')
            _logfile.flush()

        # Rohstrom je Richtung zusaetzlich vollstaendig sichern. Die
        # Textauswertung oben kann nur finden, wonach sie sucht - der
        # Rohmitschnitt laesst sich hinterher beliebig neu auswerten.
        raws = {}
        for key in ('c2s', 's2c'):
            raws[key] = open(f'{self.server.raw_prefix}-{cid}-{key}.bin', 'wb')

        state = {'c2s': False, 's2c': False}

        def pump(src, dst, direction, key):
            try:
                while True:
                    chunk = src.recv(8192)
                    if not chunk:
                        break
                    dst.sendall(chunk)
                    if not state[key]:
                        # Login-Phase: nur durchreichen, nichts mitschreiben.
                        if LOGIN_DONE.search(chunk):
                            state[key] = True
                        else:
                            continue
                    # Ab der Anmeldung: Rohstrom vollstaendig sichern.
                    raws[key].write(chunk)
                    raws[key].flush()
                    try:
                        note(direction, cid, chunk)
                    except Exception as exc:
                        # Mitschreiben ist Nebensache - die Sitzung laeuft
                        # weiter, egal was beim Protokollieren schiefgeht.
                        print(f'--- [{cid}] Logfehler ignoriert: {exc!r}')
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        t = threading.Thread(target=pump,
                             args=(self.request, up, 'C->S', 'c2s'),
                             daemon=True)
        t.start()
        pump(up, self.request, 'S->C', 's2c')
        t.join(timeout=2)
        up.close()
        for f in raws.values():
            f.close()
        print(f'--- [{cid}] Sitzung beendet')


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global _logfile
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--upstream', default=DEFAULT_UPSTREAM)
    ap.add_argument('--upstream-port', type=int, default=LOBBY_PORT)
    ap.add_argument('--listen-port', type=int, default=LOBBY_PORT)
    ap.add_argument('--out', default='official-session.log')
    args = ap.parse_args()

    _logfile = open(args.out, 'a', encoding='utf-8')
    _logfile.write(f'\n===== Start {datetime.datetime.now():%Y-%m-%d %H:%M:%S}'
                   f' -> {args.upstream}:{args.upstream_port} =====\n')
    _logfile.flush()

    srv = Server(('0.0.0.0', args.listen_port), Handler)
    srv.upstream_host = args.upstream
    srv.upstream_port = args.upstream_port
    srv.raw_prefix = os.path.splitext(args.out)[0] + '-raw'
    print(f'Sniffer laeuft auf Port {args.listen_port}, leitet weiter an '
          f'{args.upstream}:{args.upstream_port}')
    print(f'Mitschnitt: {args.out}   (Login-Pakete werden ausgelassen)')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nBeendet.')
    finally:
        _logfile.close()


if __name__ == '__main__':
    main()
