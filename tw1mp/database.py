"""Account and playerdata storage (SQLite + flat files).

The schema is identical to TW1CS 0.2.0 version 1, so an existing
ServerData.db and PlayerData folder can be reused as a drop-in.
"""

import datetime
import hashlib
import logging
import os
import sqlite3
import threading

log = logging.getLogger('tw1mp.db')

# Increasing improves security at the cost of login time; stored per user
# and transparently upgraded on the next successful login.
HASH_ITERATIONS = 100000

_SQL_dbInfoExists = 'SELECT name FROM sqlite_master WHERE name="_DBINFO"'
_SQL_dbVersion = 'SELECT VERSION FROM _DBINFO'
_SQLINIT_dbInfoTable = 'CREATE TABLE _DBINFO(VERSION)'
_DBCURVER = 1
_SQLINIT_dbInfoVersion = f'INSERT INTO _DBINFO VALUES ({_DBCURVER})'
_SQLINIT_dbUserTable = ('CREATE TABLE userTable(username UNIQUE, passHash, '
                        'serial, uniqueSalt, hashIter, lastLogin TIMESTAMP, '
                        'email, location, yob, gender, description)')
_SQLINIT_dbFormTable = 'CREATE TABLE formTable(form UNIQUE)'
# Ban list: kind is 'name' or 'serial', value is the username or serial hex.
# Created on demand (IF NOT EXISTS) so it works with an existing v1 database
# without a schema-version bump.
_SQLINIT_dbBannedTable = ('CREATE TABLE IF NOT EXISTS bannedTable('
                          'kind TEXT, value TEXT, ts TIMESTAMP, reason TEXT, '
                          'UNIQUE(kind, value))')

_SQL_userID = 'SELECT rowid FROM userTable WHERE username = ?'
_SQL_userID_Schk = 'SELECT rowid FROM userTable WHERE serial = ?'
_SQL_userID_strict = 'SELECT rowid FROM userTable WHERE username = ? AND serial = ?'
_SQL_registerUser = 'INSERT INTO userTable VALUES (?,?,?,?,?,?,?,?,?,?,?)'
_SQL_getLogin = 'SELECT username, passHash, uniqueSalt, hashIter FROM userTable WHERE rowid = ?'
_SQL_getInfo = ('SELECT email, location, yob, gender, description '
                'FROM userTable WHERE username = ?')
_SQL_listUsers = ('SELECT username, lastLogin FROM userTable '
                  'ORDER BY username COLLATE NOCASE')
_SQLUPD_info = ('UPDATE userTable SET email = ?, location = ?, yob = ?, '
                'gender = ?, description = ? WHERE username = ?')
_SQLUPD_passHash = 'UPDATE userTable SET passHash = ?, hashIter = ? WHERE rowid = ?'
_SQL_loginUpdate = 'UPDATE userTable SET lastLogin = ? WHERE rowid = ?'
_SQL_formID = 'SELECT rowid from formTable WHERE form = ?'
_SQLADD_formID = 'INSERT INTO formTable VALUES (?)'
_FORM_PDFile = '{:x}_{:x}.bin'  # PlayerData/userID_formID.bin
# A modified ("cheat") variant living next to the original; only ever
# served to a player who is alone on the server.
_FORM_PDFile_Modded = '{:x}_{:x}.modded.bin'

# Login/registration results
OK = 0
ERR_BAD_CREDENTIALS = 1
ERR_ALREADY_ONLINE = 2
ERR_SHORT_PASSWORD = 3
ERR_NO_USERNAME = 4
ERR_USER_EXISTS = 5
ERR_SERIAL_IN_USE = 6
ERR_BANNED = 7
ERR_REGISTRATION_CLOSED = 8


def _serial_hex(serial):
    if isinstance(serial, (bytes, bytearray)):
        return bytes(serial).hex()
    return str(serial)


def _safe_snap(snapname):
    """Sanitise a snapshot name into a filesystem-safe base name."""
    return ''.join(c for c in str(snapname)
                   if c.isalnum() or c in ' -_').strip()


def _salt_hash(password, salt, iterations):
    return hashlib.pbkdf2_hmac('sha256', password.encode('latin-1', 'replace'),
                               salt, iterations)


class Database:
    def __init__(self, config):
        self.cfg = config
        self.lock = threading.RLock()
        os.makedirs(config.playerdata_path, exist_ok=True)
        self.db = sqlite3.connect(config.database_path,
                                  check_same_thread=False,
                                  detect_types=sqlite3.PARSE_DECLTYPES |
                                  sqlite3.PARSE_COLNAMES)
        cur = self.db.cursor()
        if cur.execute(_SQL_dbInfoExists).fetchone() is None:
            version = 0
        else:
            version = cur.execute(_SQL_dbVersion).fetchone()[0]
        self._upgrade_from(version)
        # Auxiliary tables that aren't part of the versioned TW1CS schema are
        # created idempotently so an existing database gains them in place.
        cur.execute(_SQLINIT_dbBannedTable)
        self.db.commit()
        cur.close()

    def close(self):
        with self.lock:
            self.db.close()

    def _upgrade_from(self, version):
        log.info('Database version: %s', version)
        if version >= _DBCURVER:
            return
        log.info('Updating database to version %s', _DBCURVER)
        with self.lock:
            cur = self.db.cursor()
            if version == 0:
                cur.execute(_SQLINIT_dbInfoTable)
                cur.execute(_SQLINIT_dbInfoVersion)
                cur.execute(_SQLINIT_dbUserTable)
                cur.execute(_SQLINIT_dbFormTable)
            self.db.commit()
            cur.close()

    # -- playerdata ---------------------------------------------------

    def _playerdata_file(self, name, form, create, modded=False):
        with self.lock:
            cur = self.db.cursor()
            try:
                uidres = cur.execute(_SQL_userID, (name,)).fetchone()
                if uidres is None:
                    return None  # user doesn't exist
                fidres = cur.execute(_SQL_formID, (form,)).fetchone()
                if fidres is None:
                    if not create:
                        return None
                    cur.execute(_SQLADD_formID, (form,))
                    self.db.commit()
                    fidres = cur.execute(_SQL_formID, (form,)).fetchone()
            finally:
                cur.close()
            template = _FORM_PDFile_Modded if modded else _FORM_PDFile
            filename = template.format(uidres[0], fidres[0])
            fpath = os.path.join(self.cfg.playerdata_path, filename)
            if os.path.exists(fpath) or create:
                return fpath
            return None

    def get_playerdata(self, name, form, modded=False):
        # File body I/O stays under the lock so a UI-side import and a
        # server-side save for the same user serialize.
        with self.lock:
            path = self._playerdata_file(name, form, False, modded)
            if not path:
                return b''
            try:
                with open(path, 'rb') as f:
                    return f.read()
            except OSError:
                log.exception('Failed reading playerdata for %s', name)
                return b''

    def set_playerdata(self, name, form, data, modded=False):
        with self.lock:
            path = self._playerdata_file(name, form, True, modded)
            if not path:
                log.warning('No playerdata path for unknown user %s', name)
                return False
            try:
                with open(path, 'wb') as f:
                    f.write(data)
                return True
            except OSError:
                log.exception('Failed writing playerdata for %s', name)
                return False

    def has_modded_playerdata(self, name, form):
        with self.lock:
            return self._playerdata_file(name, form, False, True) is not None

    def delete_modded_playerdata(self, name, form):
        with self.lock:
            path = self._playerdata_file(name, form, False, True)
            if not path:
                return False
            try:
                os.remove(path)
                return True
            except OSError:
                log.exception('Failed deleting modded playerdata for %s', name)
                return False

    # -- character snapshots (save slots) -----------------------------

    def _snapshot_dir(self, name, create=False):
        with self.lock:
            cur = self.db.cursor()
            try:
                row = cur.execute(_SQL_userID, (name,)).fetchone()
            finally:
                cur.close()
        if row is None:
            return None
        path = os.path.join(self.cfg.playerdata_path, 'snapshots',
                            str(row[0]))
        if create:
            os.makedirs(path, exist_ok=True)
        return path

    def list_snapshots(self, name):
        """Saved character slots as [(snapname, size, mtime), ...]."""
        folder = self._snapshot_dir(name)
        result = []
        if folder and os.path.isdir(folder):
            for fn in os.listdir(folder):
                if fn.endswith('.bin'):
                    fpath = os.path.join(folder, fn)
                    try:
                        result.append((fn[:-4], os.path.getsize(fpath),
                                       os.path.getmtime(fpath)))
                    except OSError:
                        pass
        result.sort(key=lambda r: r[0].lower())
        return result

    def save_snapshot(self, name, snapname, data):
        safe = _safe_snap(snapname)
        if not safe:
            return False
        folder = self._snapshot_dir(name, create=True)
        if not folder:
            return False
        with self.lock:
            try:
                with open(os.path.join(folder, safe + '.bin'), 'wb') as f:
                    f.write(data)
                return True
            except OSError:
                log.exception('Failed writing snapshot for %s', name)
                return False

    def get_snapshot(self, name, snapname):
        folder = self._snapshot_dir(name)
        if not folder:
            return b''
        fpath = os.path.join(folder, _safe_snap(snapname) + '.bin')
        try:
            with open(fpath, 'rb') as f:
                return f.read()
        except OSError:
            return b''

    def delete_snapshot(self, name, snapname):
        folder = self._snapshot_dir(name)
        if not folder:
            return False
        fpath = os.path.join(folder, _safe_snap(snapname) + '.bin')
        try:
            os.remove(fpath)
            return True
        except OSError:
            return False

    def rename_snapshot(self, name, snapname, newname):
        folder = self._snapshot_dir(name)
        if not folder:
            return False
        safe_new = _safe_snap(newname)
        if not safe_new:
            return False
        src = os.path.join(folder, _safe_snap(snapname) + '.bin')
        dst = os.path.join(folder, safe_new + '.bin')
        if os.path.exists(dst):
            return False
        try:
            os.replace(src, dst)
            return True
        except OSError:
            return False

    # -- accounts -----------------------------------------------------

    def get_serial(self, name):
        """Stored serial (bytes) for an account, or None."""
        with self.lock:
            cur = self.db.cursor()
            try:
                row = cur.execute('SELECT serial FROM userTable '
                                  'WHERE username = ?', (name,)).fetchone()
                return row[0] if row else None
            finally:
                cur.close()

    def delete_playerdata(self, name, form):
        """Remove a character (original + modified variant) for one form,
        leaving the account intact. Returns True if anything was removed."""
        with self.lock:
            removed = False
            for modded in (False, True):
                path = self._playerdata_file(name, form, False, modded)
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                        removed = True
                    except OSError:
                        log.exception('Failed deleting playerdata for %s', name)
            return removed

    def delete_user(self, name):
        """Delete an account and every playerdata file it owns."""
        with self.lock:
            cur = self.db.cursor()
            try:
                uidres = cur.execute(_SQL_userID, (name,)).fetchone()
                if uidres is None:
                    return False
                uid = uidres[0]
                forms = cur.execute('SELECT rowid FROM formTable').fetchall()
                for (fid,) in forms:
                    for tmpl in (_FORM_PDFile, _FORM_PDFile_Modded):
                        fpath = os.path.join(self.cfg.playerdata_path,
                                             tmpl.format(uid, fid))
                        if os.path.exists(fpath):
                            try:
                                os.remove(fpath)
                            except OSError:
                                log.exception('Failed deleting %s', fpath)
                cur.execute('DELETE FROM userTable WHERE rowid = ?', (uid,))
                self.db.commit()
                return True
            finally:
                cur.close()

    # -- bans ---------------------------------------------------------

    def is_banned(self, username, serial):
        with self.lock:
            cur = self.db.cursor()
            try:
                row = cur.execute(
                    'SELECT 1 FROM bannedTable WHERE '
                    '(kind = "name" AND value = ?) OR '
                    '(kind = "serial" AND value = ?) LIMIT 1',
                    (username, _serial_hex(serial))).fetchone()
                return row is not None
            finally:
                cur.close()

    def add_ban(self, kind, value, reason=''):
        if kind not in ('name', 'serial'):
            return False
        with self.lock:
            cur = self.db.cursor()
            try:
                cur.execute('INSERT OR REPLACE INTO bannedTable VALUES '
                            '(?, ?, ?, ?)',
                            (kind, value, datetime.datetime.now(), reason))
                self.db.commit()
                return True
            finally:
                cur.close()

    def remove_ban(self, kind, value):
        with self.lock:
            cur = self.db.cursor()
            try:
                cur.execute('DELETE FROM bannedTable WHERE kind = ? '
                            'AND value = ?', (kind, value))
                self.db.commit()
                return cur.rowcount > 0
            finally:
                cur.close()

    def list_bans(self):
        """All bans as [(kind, value, ts, reason), ...]."""
        with self.lock:
            cur = self.db.cursor()
            try:
                return [tuple(row) for row in cur.execute(
                    'SELECT kind, value, ts, reason FROM bannedTable '
                    'ORDER BY ts DESC').fetchall()]
            finally:
                cur.close()

    def list_users(self):
        """All accounts as [(username, lastLogin or None), ...]."""
        with self.lock:
            cur = self.db.cursor()
            try:
                return [(row[0], row[1]) for row in
                        cur.execute(_SQL_listUsers).fetchall()]
            finally:
                cur.close()

    def login(self, username, serial, password):
        """Verify credentials. Returns OK or an ERR_* code."""
        if self.is_banned(username, serial):
            return ERR_BANNED
        if self.cfg.allow_any_login:
            return OK
        with self.lock:
            cur = self.db.cursor()
            try:
                if self.cfg.bind_serial:
                    uidres = cur.execute(_SQL_userID_strict,
                                         (username, serial)).fetchone()
                else:
                    uidres = cur.execute(_SQL_userID, (username,)).fetchone()
                if uidres is None:
                    return ERR_BAD_CREDENTIALS
                uid = uidres[0]
                (ruser, passhash, salt, hiter) = cur.execute(
                    _SQL_getLogin, (uid,)).fetchone()
                if username != ruser:
                    return ERR_BAD_CREDENTIALS
                tpas = _salt_hash(password, salt, hiter)
                if tpas != passhash:
                    return ERR_BAD_CREDENTIALS
                if hiter != HASH_ITERATIONS:
                    npsh = _salt_hash(password, salt, HASH_ITERATIONS)
                    cur.execute(_SQLUPD_passHash, (npsh, HASH_ITERATIONS, uid))
                cur.execute(_SQL_loginUpdate, (datetime.datetime.now(), uid))
                self.db.commit()
                return OK
            finally:
                cur.close()

    def register(self, username, serial, password, email='', location='',
                 age=1, gender=0, description=''):
        """Create an account. Returns OK or an ERR_* code."""
        if self.is_banned(username, serial):
            return ERR_BANNED
        if not getattr(self.cfg, 'allow_registration', True):
            return ERR_REGISTRATION_CLOSED
        with self.lock:
            cur = self.db.cursor()
            try:
                if cur.execute(_SQL_userID, (username,)).fetchone() is not None:
                    return ERR_USER_EXISTS
                if self.cfg.bind_serial:
                    if cur.execute(_SQL_userID_Schk, (serial,)).fetchone() is not None:
                        return ERR_SERIAL_IN_USE
                salt = os.urandom(16)
                phash = _salt_hash(password, salt, HASH_ITERATIONS)
                now = datetime.datetime.now()
                try:
                    age = int(age)
                except (TypeError, ValueError):
                    age = 0
                yob = now.year - age
                cur.execute(_SQL_registerUser, (
                    username, phash, serial, salt, HASH_ITERATIONS,
                    now, email, location, yob, gender, description))
                self.db.commit()
                return OK
            finally:
                cur.close()

    def get_userinfo(self, username):
        """Return (email, location, age, gender, description) or None."""
        with self.lock:
            cur = self.db.cursor()
            try:
                row = cur.execute(_SQL_getInfo, (username,)).fetchone()
            finally:
                cur.close()
        if row is None:
            return None
        (email, location, yob, gender, description) = row
        try:
            age = max(0, datetime.datetime.now().year - int(yob))
        except (TypeError, ValueError):
            age = 0
        return (email or '', location or '', age, gender or 0, description or '')

    def update_userinfo(self, username, email, location, age, gender, description):
        try:
            age = int(age)
        except (TypeError, ValueError):
            age = 0
        try:
            gender = int(gender)
        except (TypeError, ValueError):
            gender = 0
        yob = datetime.datetime.now().year - age
        with self.lock:
            cur = self.db.cursor()
            try:
                cur.execute(_SQLUPD_info,
                            (email, location, yob, gender, description, username))
                self.db.commit()
                return cur.rowcount > 0
            finally:
                cur.close()
