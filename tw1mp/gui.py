"""Desktop front-end for the lobby server.

Runs the server in-process on a worker thread, mirrors its log into the
window, lists who is connected, and manages characters: pull an account
from a live (e.g. official) server and import it into local storage.

Uses only tkinter from the standard library, plus tw1mp.tray for the
notification area, so the "install Python, done" property still holds.
"""

import datetime
import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, chardata, gamelang, savegame, tray
from .config import Config
from .database import Database
from .server import CoreServer

log = logging.getLogger('tw1mp.gui')

_POLL_MS = 150
_REFRESH_MS = 1000
_MAX_LOG_LINES = 2000

_COL_OK = '#2e9e4f'
_COL_OFF = '#8a8a8a'
_COL_ERR = '#c0392b'


class QueueLogHandler(logging.Handler):
    """Feeds formatted log records into a queue for the UI thread."""

    def __init__(self, sink):
        super().__init__()
        self.sink = sink
        self.setFormatter(logging.Formatter(
            '%(asctime)s  %(levelname)-7s %(message)s', '%H:%M:%S'))

    def emit(self, record):
        try:
            self.sink.put((record.levelno, self.format(record)))
        except Exception:  # never let logging break the server
            pass


class ServerController:
    """Owns the CoreServer instance and its thread."""

    def __init__(self, root_dir):
        self.root_dir = root_dir
        self.server = None
        self.thread = None
        self.started_at = None

    @property
    def running(self):
        return self.server is not None and self.thread is not None \
            and self.thread.is_alive()

    def start(self):
        """Start the server. Raises OSError if the port is unavailable."""
        if self.running:
            return
        config = Config(root=self.root_dir)
        server = CoreServer(config)  # binds here, so port errors surface now
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever,
                                       kwargs={'poll_interval': 0.5},
                                       daemon=True, name='tw1mp-server')
        self.thread.start()
        self.started_at = datetime.datetime.now()

    def stop(self):
        if not self.server:
            return
        try:
            # Let each connected client's lobby loop break out on its next
            # recv timeout (server.py checks _is_closing) so its handler
            # disconnects before the shared DB is closed.
            self.server._is_closing = True
            self.server.shutdown()
            if self.thread:
                self.thread.join(timeout=5)
            # Per-client handler threads are daemons the server never
            # joins; give them a moment to unwind before closing the DB.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and \
                    self.server.debug_dict_players():
                time.sleep(0.05)
            self.server.server_close()
            self.server.db.close()
        except Exception:
            log.exception('Error while stopping the server')
        finally:
            self.server = None
            self.thread = None
            self.started_at = None

    def players(self):
        if not self.running:
            return {}
        try:
            return self.server.debug_dict_players()
        except Exception:
            return {}

    def uptime(self):
        if not self.started_at:
            return ''
        delta = datetime.datetime.now() - self.started_at
        total = int(delta.total_seconds())
        return f'{total // 3600:d}:{(total // 60) % 60:02d}:{total % 60:02d}'

    def open_database(self):
        """Database handle for maintenance work.

        Returns (db, needs_close): the running server's handle is reused so
        no second writer touches the files; otherwise a temporary one.
        """
        if self.running:
            return self.server.db, False
        return Database(Config(root=self.root_dir)), True


class MainWindow:
    def __init__(self, root_dir=None, start_server=True, minimised=False):
        self.root_dir = root_dir or os.getcwd()
        self.controller = ServerController(self.root_dir)
        self.logq = queue.Queue()
        # Cross-thread work is queued and drained on the UI thread; calling
        # root.after() from another thread can block that thread while the
        # main thread is busy (e.g. joining the server during stop).
        self.callq = queue.Queue()
        self.busy = False
        self._quitting = False

        self.root = tk.Tk()
        self.root.title(f'TW1MP Lobby Server {__version__}')
        self.root.geometry('900x620')
        self.root.minsize(720, 480)
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)

        self._build()
        self._attach_logging()

        self.tray = tray.TrayIcon(
            'TW1MP Lobby Server',
            on_show=lambda: self._call_soon(self.show_window),
            on_quit=lambda: self._call_soon(self.quit_app),
            on_toggle=lambda: self._call_soon(self.toggle_server))
        self.has_tray = self.tray.start()
        if not self.has_tray:
            self.btn_tray.state(['disabled'])

        self.root.after(_POLL_MS, self._drain_log)
        self.root.after(_POLL_MS, self._drain_calls)
        self.root.after(_REFRESH_MS, self._refresh)

        if start_server:
            self.start_server()
        self.refresh_accounts()
        if minimised and self.has_tray:
            self.root.after(200, self.hide_window)

    # -- construction ---------------------------------------------------

    def _build(self):
        bar = ttk.Frame(self.root, padding=(10, 8))
        bar.pack(fill='x')

        self.lbl_dot = tk.Label(bar, text='●', fg=_COL_OFF,
                                font=('Segoe UI', 14))
        self.lbl_dot.pack(side='left')
        self.lbl_status = ttk.Label(bar, text='Stopped',
                                    font=('Segoe UI', 10, 'bold'))
        self.lbl_status.pack(side='left', padx=(6, 16))
        self.lbl_info = ttk.Label(bar, text='')
        self.lbl_info.pack(side='left')

        self.btn_tray = ttk.Button(bar, text='To tray',
                                   command=self.hide_window)
        self.btn_tray.pack(side='right')
        self.btn_toggle = ttk.Button(bar, text='Start server',
                                     command=self.toggle_server)
        self.btn_toggle.pack(side='right', padx=6)

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        notebook.add(self._build_server_tab(notebook), text='Server')
        notebook.add(self._build_saves_tab(notebook), text='Characters')

    def _build_server_tab(self, parent):
        frame = ttk.Frame(parent, padding=8)
        panes = ttk.PanedWindow(frame, orient='horizontal')
        panes.pack(fill='both', expand=True)

        left = ttk.Frame(panes)
        ttk.Label(left, text='Log').pack(anchor='w')
        wrap = ttk.Frame(left)
        wrap.pack(fill='both', expand=True)
        self.txt_log = tk.Text(wrap, wrap='none', height=10,
                               font=('Consolas', 9), state='disabled',
                               background='#1e1e1e', foreground='#dcdcdc',
                               insertbackground='#dcdcdc')
        scroll = ttk.Scrollbar(wrap, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.txt_log.pack(side='left', fill='both', expand=True)
        self.txt_log.tag_configure('warn', foreground='#e0b050')
        self.txt_log.tag_configure('err', foreground='#e06c60')
        self.txt_log.tag_configure('dbg', foreground='#8a8a8a')
        panes.add(left, weight=3)

        right = ttk.Frame(panes)
        ttk.Label(right, text='Connected players').pack(anchor='w')
        self.tree_players = ttk.Treeview(
            right, columns=('town', 'since'), show='tree headings', height=10)
        self.tree_players.heading('#0', text='Player')
        self.tree_players.heading('town', text='Town')
        self.tree_players.heading('since', text='Since')
        self.tree_players.column('#0', width=130)
        self.tree_players.column('town', width=110)
        self.tree_players.column('since', width=70, anchor='center')
        self.tree_players.pack(fill='both', expand=True)
        panes.add(right, weight=2)
        return frame

    def _build_saves_tab(self, parent):
        frame = ttk.Frame(parent, padding=8)

        ttk.Label(frame, text='Local accounts and their stored characters'
                  ).pack(anchor='w')
        self.tree_saves = ttk.Treeview(
            frame, columns=('size', 'changed'), show='tree headings',
            height=9, selectmode='browse')
        self.tree_saves.heading('#0', text='Account')
        self.tree_saves.heading('size', text='Character')
        self.tree_saves.heading('changed', text='Last change')
        self.tree_saves.column('#0', width=200)
        self.tree_saves.column('size', width=120, anchor='e')
        self.tree_saves.column('changed', width=160, anchor='center')
        self.tree_saves.pack(fill='both', expand=True, pady=(2, 8))

        box = ttk.LabelFrame(
            frame, text='Import from a live server', padding=8)
        box.pack(fill='x')

        row = ttk.Frame(box)
        row.pack(fill='x', pady=2)
        ttk.Label(row, text='Server:', width=9).pack(side='left')
        self.cmb_server = ttk.Combobox(row, width=32, state='readonly')
        self.cmb_server.pack(side='left', padx=(0, 12))
        ttk.Label(row, text='Profile:', width=8).pack(side='left')
        self.cmb_profile = ttk.Combobox(row, width=14, state='readonly')
        self.cmb_profile.pack(side='left')

        self._fill_sources()

        row2 = ttk.Frame(box)
        row2.pack(fill='x', pady=(8, 2))
        self.btn_fetch = ttk.Button(
            row2, text='Download and import', command=self.fetch_character)
        self.btn_fetch.pack(side='left')
        ttk.Button(row2, text='Import from file...',
                   command=self.import_file).pack(side='left', padx=6)
        ttk.Button(row2, text='Export selected...',
                   command=self.export_selected).pack(side='left')
        ttk.Button(row2, text='Edit items...',
                   command=self.edit_items).pack(side='left', padx=6)
        ttk.Button(row2, text='Refresh',
                   command=self.refresh_accounts).pack(side='right')

        self.lbl_save = ttk.Label(box, text='', wraplength=820,
                                  justify='left')
        self.lbl_save.pack(anchor='w', pady=(8, 0))

        if not savegame.is_supported():
            self.btn_fetch.state(['disabled'])
            self.lbl_save.configure(
                text='Downloading needs the game installed on this machine '
                     '(serial key and stored login come from it).')
        return frame

    def _fill_sources(self):
        servers = []
        if savegame.is_supported():
            for name, host in savegame.read_servers():
                if host in ('127.0.0.1', 'localhost', '::1'):
                    continue  # a local test server has nothing to offer
                servers.append(f'{name} ({host})')
        if not servers:
            servers = [f'WarNet Europe ({savegame.DEFAULT_SERVER})']
        self.cmb_server['values'] = servers
        self.cmb_server.current(0)

        profiles = savegame.list_profiles() if savegame.is_supported() else []
        self.cmb_profile['values'] = profiles or [savegame.DEFAULT_PROFILE]
        self.cmb_profile.current(0)

    def _attach_logging(self):
        self._log_handler = QueueLogHandler(self.logq)
        self._log_handler.setLevel(logging.DEBUG)
        root_logger = logging.getLogger('tw1mp')
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(self._log_handler)

    def _detach_logging(self):
        logging.getLogger('tw1mp').removeHandler(self._log_handler)

    # -- server control -------------------------------------------------

    def start_server(self):
        try:
            self.controller.start()
        except OSError as exc:
            messagebox.showerror(
                'Cannot start server',
                f'Port is probably already in use.\n\n{exc}')
        except Exception as exc:
            log.exception('Server start failed')
            messagebox.showerror('Cannot start server', str(exc))
        self._refresh(reschedule=False)

    def stop_server(self):
        self.controller.stop()
        self._refresh(reschedule=False)

    def toggle_server(self):
        if self.controller.running:
            self.stop_server()
        else:
            self.start_server()

    # -- character handling ---------------------------------------------

    def refresh_accounts(self):
        for item in self.tree_saves.get_children():
            self.tree_saves.delete(item)
        db, needs_close = self.controller.open_database()
        try:
            for name, _last in db.list_users():
                blob = db.get_playerdata(name, savegame.DEFAULT_FORM)
                if blob:
                    size = f'{len(blob):,} bytes'.replace(',', ' ')
                    stamp = self._playerdata_mtime(db, name)
                else:
                    size, stamp = 'none', ''
                if db.has_modded_playerdata(name, savegame.DEFAULT_FORM):
                    size += '  (modified variant present)'
                self.tree_saves.insert('', 'end', text=name,
                                       values=(size, stamp))
        except Exception:
            log.exception('Could not list accounts')
        finally:
            if needs_close:
                db.close()

    @staticmethod
    def _playerdata_mtime(db, name):
        try:
            path = db._playerdata_file(name, savegame.DEFAULT_FORM, False)
            if path and os.path.exists(path):
                return datetime.datetime.fromtimestamp(
                    os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M')
        except Exception:
            pass
        return ''

    def _selected_account(self):
        sel = self.tree_saves.selection()
        return self.tree_saves.item(sel[0], 'text') if sel else None

    def fetch_character(self):
        """Download from the chosen server, then import (worker thread)."""
        if self.busy:
            return
        label = self.cmb_server.get()
        host = label.rsplit('(', 1)[-1].rstrip(')') if '(' in label else label
        profile = self.cmb_profile.get() or savegame.DEFAULT_PROFILE
        self._set_busy(True, f'Connecting to {host} ...')

        def work():
            try:
                username, blob = savegame.fetch_from_registry(
                    server=host, profile=profile)
                self._call_soon(self._fetched, username, blob, host)
            except savegame.SavegameError as exc:
                self._call_soon(self._fetch_failed, str(exc))
            except Exception as exc:
                log.exception('Character download failed')
                self._call_soon(self._fetch_failed, str(exc))

        threading.Thread(target=work, daemon=True,
                         name='tw1mp-fetch').start()

    def _fetched(self, username, blob, host):
        if not blob:
            self._set_busy(False, f'{host} has no character for {username!r}.')
            return
        db, needs_close = self.controller.open_database()
        try:
            if username in self.controller.players():
                if not messagebox.askokcancel(
                        'Player online',
                        f'{username} is connected right now. Importing may '
                        'be overwritten when they next save.\n\nContinue?'):
                    self._set_busy(False, 'Import cancelled.')
                    return
            previous = savegame.import_playerdata(db, username, blob)
            backup = self._write_backup(username, previous)
        except savegame.SavegameError as exc:
            # Keep the replaced character even when the import went wrong.
            self._write_backup(username, getattr(exc, 'previous', b''))
            self._set_busy(False, f'Import failed: {exc}')
            messagebox.showerror('Import failed', str(exc))
            return
        except Exception as exc:
            log.exception('Import failed')
            self._set_busy(False, f'Import failed: {exc}')
            return
        finally:
            if needs_close:
                db.close()
        msg = (f'Imported {len(blob):,} bytes for {username} from {host}.'
               .replace(',', ' '))
        if backup:
            msg += f' Previous character kept as {os.path.basename(backup)}.'
        self._set_busy(False, msg)
        self.refresh_accounts()

    def _fetch_failed(self, message):
        self._set_busy(False, f'Download failed: {message}')

    def _write_backup(self, username, previous):
        """Keep the character that was replaced, if there was one."""
        if not previous:
            return None
        folder = os.path.join(self.root_dir, 'PlayerData', 'backup')
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        safe = ''.join(c if c.isalnum() or c in '-_' else '_'
                       for c in username)
        path = os.path.join(folder, f'{safe}_{stamp}.bin')
        try:
            with open(path, 'wb') as f:
                f.write(previous)
            return path
        except OSError:
            log.exception('Could not write backup')
            return None

    def import_file(self):
        account = self._selected_account()
        if not account:
            messagebox.showinfo('No account selected',
                                'Select the account to import into first.')
            return
        path = filedialog.askopenfilename(
            title=f'Import character for {account}',
            filetypes=[('Character data', '*.bin'), ('All files', '*.*')])
        if not path:
            return
        try:
            with open(path, 'rb') as f:
                blob = f.read()
            db, needs_close = self.controller.open_database()
            try:
                previous = savegame.import_playerdata(db, account, blob)
            finally:
                if needs_close:
                    db.close()
            backup = self._write_backup(account, previous)
        except (OSError, savegame.SavegameError) as exc:
            messagebox.showerror('Import failed', str(exc))
            return
        msg = f'Imported {len(blob)} bytes for {account}.'
        if backup:
            msg += f' Previous character kept as {os.path.basename(backup)}.'
        self._set_busy(False, msg)
        self.refresh_accounts()

    def export_selected(self):
        account = self._selected_account()
        if not account:
            messagebox.showinfo('No account selected',
                                'Select an account to export.')
            return
        db, needs_close = self.controller.open_database()
        try:
            blob = db.get_playerdata(account, savegame.DEFAULT_FORM)
        finally:
            if needs_close:
                db.close()
        if not blob:
            messagebox.showinfo('Nothing to export',
                                f'{account} has no stored character.')
            return
        path = filedialog.asksaveasfilename(
            title=f'Export character of {account}', defaultextension='.bin',
            initialfile=f'{account}_Playerdata.bin',
            filetypes=[('Character data', '*.bin')])
        if not path:
            return
        try:
            with open(path, 'wb') as f:
                f.write(blob)
        except OSError as exc:
            messagebox.showerror('Export failed', str(exc))
            return
        self._set_busy(False, f'Exported {len(blob)} bytes to {path}.')

    def edit_items(self):
        account = self._selected_account()
        if not account:
            messagebox.showinfo('No account selected',
                                'Select the account to edit first.')
            return
        db, needs_close = self.controller.open_database()
        try:
            # Edit on top of an existing modified variant if there is one,
            # otherwise start from the original.
            modded = db.has_modded_playerdata(account, savegame.DEFAULT_FORM)
            blob = db.get_playerdata(account, savegame.DEFAULT_FORM,
                                     modded=modded)
        finally:
            if needs_close:
                db.close()
        if not blob:
            messagebox.showinfo('Nothing to edit',
                                f'{account} has no stored character.')
            return
        try:
            ItemEditor(self, account, blob, based_on_modded=modded)
        except chardata.CharDataError as exc:
            messagebox.showerror('Cannot read character', str(exc))

    def save_modified_character(self, account, blob):
        """Called by ItemEditor: store the edited blob as the variant."""
        db, needs_close = self.controller.open_database()
        try:
            db.set_playerdata(account, savegame.DEFAULT_FORM, blob,
                              modded=True)
        finally:
            if needs_close:
                db.close()
        self._set_busy(False,
                       f'Modified character for {account} saved. It is only '
                       'served while the player is alone on the server; the '
                       'original stays untouched.')
        self.refresh_accounts()

    def remove_modified_character(self, account):
        db, needs_close = self.controller.open_database()
        try:
            db.delete_modded_playerdata(account, savegame.DEFAULT_FORM)
        finally:
            if needs_close:
                db.close()
        self._set_busy(False, f'Modified character for {account} removed.')
        self.refresh_accounts()

    def _set_busy(self, busy, message=None):
        self.busy = busy
        if message is not None:
            self.lbl_save.configure(text=message)
        if savegame.is_supported():
            self.btn_fetch.state(['disabled'] if busy else ['!disabled'])

    # -- periodic UI updates --------------------------------------------

    def _call_soon(self, func, *args):
        """Queue work for the UI thread without blocking the caller."""
        self.callq.put((func, args))

    def _drain_calls(self):
        try:
            while True:
                func, args = self.callq.get_nowait()
                try:
                    func(*args)
                except Exception:
                    log.exception('Queued UI call failed')
        except queue.Empty:
            pass
        self.root.after(_POLL_MS, self._drain_calls)

    def _drain_log(self):
        lines = 0
        try:
            while lines < 200:
                level, text = self.logq.get_nowait()
                self._append_log(level, text)
                lines += 1
        except queue.Empty:
            pass
        self.root.after(_POLL_MS, self._drain_log)

    def _append_log(self, level, text):
        tag = ''
        if level >= logging.ERROR:
            tag = 'err'
        elif level >= logging.WARNING:
            tag = 'warn'
        elif level <= logging.DEBUG:
            tag = 'dbg'
        self.txt_log.configure(state='normal')
        at_bottom = self.txt_log.yview()[1] > 0.999
        self.txt_log.insert('end', text + '\n', tag)
        excess = int(self.txt_log.index('end-1c').split('.')[0]) - _MAX_LOG_LINES
        if excess > 0:
            self.txt_log.delete('1.0', f'{excess}.0')
        self.txt_log.configure(state='disabled')
        if at_bottom:
            self.txt_log.see('end')

    def _refresh(self, reschedule=True):
        running = self.controller.running
        players = self.controller.players()
        self.lbl_dot.configure(fg=_COL_OK if running else _COL_OFF)
        self.lbl_status.configure(text='Running' if running else 'Stopped')
        self.btn_toggle.configure(
            text='Stop server' if running else 'Start server')
        if running:
            port = self.controller.server.config.port
            count = len(players)
            self.lbl_info.configure(
                text=f'Port {port}   |   {count} '
                     f'{"player" if count == 1 else "players"}   |   '
                     f'uptime {self.controller.uptime()}')
        else:
            self.lbl_info.configure(text='')

        known = set()
        for name, info in players.items():
            known.add(name)
            town = (info.get('town') or '').split('#')[0]
            since = (info.get('loginTime') or '')[11:16]
            if self.tree_players.exists(name):
                self.tree_players.item(name, values=(town, since))
            else:
                self.tree_players.insert('', 'end', iid=name, text=name,
                                         values=(town, since))
        for item in self.tree_players.get_children():
            if item not in known:
                self.tree_players.delete(item)

        if self.has_tray:
            tip = f'TW1MP - {"running" if running else "stopped"}'
            if running:
                tip += f', {len(players)} online'
            self.tray.update(running, tip)
        if reschedule:
            self.root.after(_REFRESH_MS, self._refresh)

    # -- window / lifecycle ---------------------------------------------

    def hide_window(self):
        if self.has_tray:
            self.root.withdraw()
        else:
            self.root.iconify()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        """Closing the window keeps the server alive in the tray."""
        if self.has_tray and self.controller.running:
            self.hide_window()
            return
        self.quit_app()

    def quit_app(self):
        if self._quitting:
            return
        if self.controller.running and not messagebox.askokcancel(
                'Quit', 'The server is running. Stop it and quit?'):
            return
        self._quitting = True
        self.controller.stop()
        if self.has_tray:
            self.tray.stop()
        self._detach_logging()
        self.root.quit()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


class ItemEditor:
    """Modal dialog to change item stack quantities of one character.

    Saves the result as the account's *modified variant* - the original
    file is never written. The server only serves the variant to a player
    who is alone (solo session), so it cannot leak into multiplayer.
    """

    def __init__(self, owner, account, blob, based_on_modded=False):
        self.owner = owner
        self.account = account
        self.raw = chardata.decompress(blob)
        self.items = chardata.parse_items(self.raw)
        if not self.items:
            raise chardata.CharDataError(
                'No item stacks recognised in this character')
        self.changes = {}
        # Localised names from the installed game ("Steinpilz" instead of
        # MUSHROOM_01); falls back to the internal id when not installed.
        self.names = [gamelang.item_name(s.name) for s in self.items]

        self.win = tk.Toplevel(owner.root)
        self.win.title(f'Item editor - {account}'
                       + (' (modified variant)' if based_on_modded else ''))
        self.win.geometry('640x560')
        self.win.transient(owner.root)
        self.win.grab_set()

        ttk.Label(self.win, padding=(10, 8, 10, 0), justify='left',
                  text='Double-click an editable stack to change its '
                       'quantity. Saved as a separate "modified" character; '
                       'the original is kept and is always used as soon as '
                       'another player is online.').pack(anchor='w')

        filters = ttk.Frame(self.win, padding=(10, 6, 10, 0))
        filters.pack(fill='x')
        self.var_editable = tk.BooleanVar(value=True)
        ttk.Checkbutton(filters, text='Editable items only',
                        variable=self.var_editable,
                        command=self._apply_filter).pack(side='left')
        ttk.Label(filters, text='Search:').pack(side='left', padx=(16, 4))
        self.var_search = tk.StringVar()
        self.var_search.trace_add('write',
                                  lambda *_: self._apply_filter())
        ttk.Entry(filters, textvariable=self.var_search,
                  width=24).pack(side='left')
        ttk.Label(filters, text='(name, id or exact quantity)'
                  ).pack(side='left', padx=6)

        wrap = ttk.Frame(self.win, padding=10)
        wrap.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(wrap, columns=('id', 'qty', 'state'),
                                 show='tree headings', selectmode='browse')
        self.tree.heading('#0', text='Item')
        self.tree.heading('id', text='Internal id')
        self.tree.heading('qty', text='Qty / Class')
        self.tree.heading('state', text='')
        self.tree.column('#0', width=200)
        self.tree.column('id', width=160)
        self.tree.column('qty', width=80, anchor='e')
        self.tree.column('state', width=100, anchor='center')
        scroll = ttk.Scrollbar(wrap, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(side='left', fill='both', expand=True)
        self.tree.tag_configure('locked', foreground='#8a8a8a')
        self.tree.tag_configure('changed', foreground='#2e7d32')
        self.tree.bind('<Double-1>', self._on_double_click)

        self._apply_filter()

        bar = ttk.Frame(self.win, padding=(10, 0, 10, 10))
        bar.pack(fill='x')
        self.btn_save = ttk.Button(bar, text='Save as modified character',
                                   command=self._save, state='disabled')
        self.btn_save.pack(side='left')
        ttk.Button(bar, text='Remove modified character',
                   command=self._remove).pack(side='left', padx=6)
        ttk.Button(bar, text='Close',
                   command=self.win.destroy).pack(side='right')

    def _apply_filter(self):
        editable_only = self.var_editable.get()
        term = self.var_search.get().strip().lower()
        term_qty = int(term) if term.isdigit() else None
        for row in self.tree.get_children():
            self.tree.delete(row)
        for idx, stack in enumerate(self.items):
            if editable_only and not stack.editable:
                continue
            display = self.names[idx]
            qty = self.changes.get(stack.qty_offset, stack.quantity)
            if term:
                hit = (term in display.lower()
                       or term in stack.name.lower()
                       or term_qty is not None
                       and term_qty in (qty, stack.quantity))
                if not hit:
                    continue
            if stack.qty_offset in self.changes:
                tag, state = 'changed', f'was {stack.quantity}'
            elif not stack.editable:
                tag, state = 'locked', 'read-only'
            elif stack.kind == 'class':
                tag, state = '', 'class'
            else:
                tag, state = '', ''
            self.tree.insert('', 'end', iid=str(idx), text=display,
                             values=(stack.name, qty, state), tags=(tag,))

    def _on_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        idx = int(row)
        stack = self.items[idx]
        if not stack.editable:
            return
        from tkinter import simpledialog
        display = self.names[idx]
        what = 'class' if stack.kind == 'class' else 'quantity'
        current = self.changes.get(stack.qty_offset, stack.quantity)
        value = simpledialog.askinteger(
            display, f'New {what} for {display}:',
            parent=self.win, initialvalue=current,
            minvalue=0, maxvalue=stack.max_value)
        if value is None:
            return
        if value == stack.quantity:
            self.changes.pop(stack.qty_offset, None)
        else:
            self.changes[stack.qty_offset] = value
        self._apply_filter()
        self.btn_save.configure(
            state='normal' if self.changes else 'disabled')

    def _save(self):
        try:
            blob = chardata.edit_quantities(
                chardata.compress(self.raw), self.changes)
        except chardata.CharDataError as exc:
            messagebox.showerror('Edit failed', str(exc), parent=self.win)
            return
        self.owner.save_modified_character(self.account, blob)
        self.win.destroy()

    def _remove(self):
        if messagebox.askokcancel(
                'Remove modified character',
                'Delete the modified variant? The original character is '
                'not affected.', parent=self.win):
            self.owner.remove_modified_character(self.account)
            self.win.destroy()


def run(root_dir=None, start_server=True, minimised=False):
    MainWindow(root_dir=root_dir, start_server=start_server,
               minimised=minimised).run()
