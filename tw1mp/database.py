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

    # -- accounts -----------------------------------------------------

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
