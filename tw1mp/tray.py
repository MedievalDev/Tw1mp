"""Windows notification-area (tray) icon via the Win32 API.

Implemented with ctypes so the project keeps its "install Python, done"
property - no pystray, no Pillow. The icon owns a message-only window on
its own thread; callbacks are handed back to the caller, which is
responsible for marshalling them onto its UI thread.

No-op on non-Windows platforms: available() returns False and the class
can still be constructed so callers need no platform branches.
"""

import ctypes
import logging
import threading

log = logging.getLogger('tw1mp.tray')

try:
    from ctypes import wintypes
    _WIN = True
except (ImportError, ValueError):  # pragma: no cover - non-Windows
    _WIN = False

# Win32 constants
_WM_DESTROY = 0x0002
_WM_COMMAND = 0x0111
_WM_USER = 0x0400
_WM_TRAYICON = _WM_USER + 20
_WM_UPDATE = _WM_USER + 21
_WM_QUIT_LOOP = _WM_USER + 22

_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205

_NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0, 1, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP = 0x01, 0x02, 0x04
_IMAGE_ICON = 1
_IDI_APPLICATION = 32512
_TPM_RIGHTBUTTON = 0x0002
_TPM_RETURNCMD = 0x0100
_MF_STRING, _MF_SEPARATOR = 0x0000, 0x0800
_CW_USEDEFAULT = -2147483648
_HWND_MESSAGE = -3

_ID_SHOW, _ID_TOGGLE, _ID_QUIT = 1001, 1002, 1003


def available():
    """True if a tray icon can be created on this platform."""
    return _WIN


if _WIN:
    _user32 = ctypes.WinDLL('user32', use_last_error=True)
    _shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)

    _LRESULT = ctypes.c_ssize_t
    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)

    class _WNDCLASS(ctypes.Structure):
        _fields_ = [('style', wintypes.UINT),
                    ('lpfnWndProc', _WNDPROC),
                    ('cbClsExtra', ctypes.c_int),
                    ('cbWndExtra', ctypes.c_int),
                    ('hInstance', wintypes.HINSTANCE),
                    ('hIcon', wintypes.HICON),
                    ('hCursor', wintypes.HANDLE),
                    ('hbrBackground', wintypes.HBRUSH),
                    ('lpszMenuName', wintypes.LPCWSTR),
                    ('lpszClassName', wintypes.LPCWSTR)]

    class _NOTIFYICONDATA(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD),
                    ('hWnd', wintypes.HWND),
                    ('uID', wintypes.UINT),
                    ('uFlags', wintypes.UINT),
                    ('uCallbackMessage', wintypes.UINT),
                    ('hIcon', wintypes.HICON),
                    ('szTip', wintypes.WCHAR * 128),
                    ('dwState', wintypes.DWORD),
                    ('dwStateMask', wintypes.DWORD),
                    ('szInfo', wintypes.WCHAR * 256),
                    ('uVersion', wintypes.UINT),
                    ('szInfoTitle', wintypes.WCHAR * 64),
                    ('dwInfoFlags', wintypes.DWORD),
                    ('guidItem', ctypes.c_byte * 16),
                    ('hBalloonIcon', wintypes.HICON)]

    class _POINT(ctypes.Structure):
        _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]

    # Full prototypes: without argtypes/restype ctypes marshals handles
    # through 32-bit c_int, truncating 64-bit HWND/HMENU/HMODULE values.
    _user32.DefWindowProcW.restype = _LRESULT
    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                    ctypes.c_size_t, wintypes.LPCWSTR]
    _user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT,
                                       ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, wintypes.HWND,
                                       wintypes.LPVOID]
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    _user32.LoadIconW.restype = wintypes.HICON
    _user32.DestroyIcon.argtypes = [wintypes.HICON]
    # CreateIcon lives in user32, not gdi32
    _user32.CreateIcon.restype = wintypes.HICON
    _user32.CreateIcon.argtypes = [wintypes.HINSTANCE, ctypes.c_int,
                                   ctypes.c_int, wintypes.BYTE, wintypes.BYTE,
                                   ctypes.c_char_p, ctypes.c_char_p]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


def _make_icon(rgb):
    """Build a 16x16 filled-circle icon in the given (r, g, b) colour."""
    size = 16
    radius = 7.0
    centre = 7.5
    xor = bytearray()
    and_mask = bytearray()
    for y in range(size):
        row_bits = 0
        for x in range(size):
            inside = ((x - centre) ** 2 + (y - centre) ** 2) <= radius ** 2
            if inside:
                # slight vertical shading so it does not look flat
                shade = 1.0 - (y / size) * 0.35
                xor += bytes((int(rgb[2] * shade), int(rgb[1] * shade),
                              int(rgb[0] * shade), 0xFF))
            else:
                xor += b'\x00\x00\x00\x00'
                row_bits |= 1 << (15 - x)  # 1 = transparent in the AND mask
        and_mask += bytes(((row_bits >> 8) & 0xFF, row_bits & 0xFF))
    icon = _user32.CreateIcon(None, size, size, 1, 32,
                              bytes(and_mask), bytes(xor))
    if icon:
        return icon, True  # owned: must be DestroyIcon'd on teardown
    # MAKEINTRESOURCE: the id is passed in the pointer itself
    icon = _user32.LoadIconW(
        None, ctypes.cast(ctypes.c_void_p(_IDI_APPLICATION),
                          wintypes.LPCWSTR))
    return icon, False  # shared system icon, never destroy


class TrayIcon:
    """A tray icon whose menu drives the application.

    on_show / on_quit / on_toggle are called from the tray thread.
    """

    COLOUR_RUNNING = (0x3F, 0xC1, 0x60)
    COLOUR_STOPPED = (0x8A, 0x8A, 0x8A)

    def __init__(self, title='TW1MP', on_show=None, on_quit=None,
                 on_toggle=None):
        self.title = title
        self.on_show = on_show
        self.on_quit = on_quit
        self.on_toggle = on_toggle
        self._hwnd = None
        self._thread = None
        self._ready = threading.Event()
        self._running = False
        self._tip = title[:127]  # szTip holds at most 127 chars + NUL
        self._icons = {}
        self._nid = None
        self._wndproc_ref = None  # must outlive the window
        self._classname = None

    # -- public API -----------------------------------------------------

    def start(self):
        """Create the icon on a dedicated thread. Returns True on success."""
        if not available():
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._hwnd = None
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='tw1mp-tray')
        self._thread.start()
        self._ready.wait(timeout=5)
        return self._hwnd is not None

    def update(self, running, tooltip=None):
        """Switch the icon colour/tooltip (safe from any thread)."""
        self._running = running
        if tooltip:
            self._tip = tooltip[:127]
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, _WM_UPDATE, 0, 0)

    def stop(self):
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, _WM_QUIT_LOOP, 0, 0)
            if self._thread:
                self._thread.join(timeout=3)
        self._hwnd = None
        self._ready.clear()

    # -- internals ------------------------------------------------------

    def _run(self):
        try:
            self._create_window()
            self._add_icon()
        except Exception:
            log.exception('Tray icon unavailable')
            self._hwnd = None
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        # teardown on the tray thread, after the window is gone
        self._remove_icon()
        self._hwnd = None
        for icon, owned in self._icons.values():
            if owned:
                _user32.DestroyIcon(icon)
        self._icons = {}
        if self._classname:
            _user32.UnregisterClassW(self._classname,
                                     _kernel32.GetModuleHandleW(None))

    def _create_window(self):
        self._wndproc_ref = _WNDPROC(self._wndproc)
        self._classname = f'TW1MPTray{id(self):x}'
        hinst = _kernel32.GetModuleHandleW(None)
        cls = _WNDCLASS()
        cls.lpfnWndProc = self._wndproc_ref
        cls.hInstance = hinst
        cls.lpszClassName = self._classname
        if not _user32.RegisterClassW(ctypes.byref(cls)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._class_ref = cls  # keep the class (and its proc) alive
        # A normal (never shown) window rather than a message-only one:
        # TrackPopupMenu needs a window that can take the foreground, or
        # the menu refuses to close when clicking elsewhere.
        hwnd = _user32.CreateWindowExW(
            0, self._classname, self.title, 0,
            _CW_USEDEFAULT, _CW_USEDEFAULT, 0, 0,
            None, None, hinst, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hwnd = hwnd

    def _icon(self):
        key = 'run' if self._running else 'stop'
        if key not in self._icons:
            colour = self.COLOUR_RUNNING if self._running \
                else self.COLOUR_STOPPED
            self._icons[key] = _make_icon(colour)  # (handle, owned)
        return self._icons[key][0]

    def _add_icon(self):
        nid = _NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        nid.uCallbackMessage = _WM_TRAYICON
        nid.hIcon = self._icon()
        nid.szTip = self._tip
        if not _shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(nid)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._nid = nid

    def _modify_icon(self):
        if not self._nid:
            return
        self._nid.hIcon = self._icon()
        self._nid.szTip = self._tip
        _shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(self._nid))

    def _remove_icon(self):
        if self._nid:
            _shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None

    def _show_menu(self):
        menu = _user32.CreatePopupMenu()
        _user32.AppendMenuW(menu, _MF_STRING, _ID_SHOW, 'Show window')
        _user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
        _user32.AppendMenuW(menu, _MF_STRING, _ID_TOGGLE,
                            'Stop server' if self._running
                            else 'Start server')
        _user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
        _user32.AppendMenuW(menu, _MF_STRING, _ID_QUIT, 'Exit')
        pos = _POINT()
        _user32.GetCursorPos(ctypes.byref(pos))
        # Required so the menu closes when the user clicks elsewhere.
        _user32.SetForegroundWindow(self._hwnd)
        choice = _user32.TrackPopupMenu(
            menu, _TPM_RIGHTBUTTON | _TPM_RETURNCMD, pos.x, pos.y, 0,
            self._hwnd, None)
        _user32.DestroyMenu(menu)
        if choice:
            self._dispatch(choice)

    def _dispatch(self, command):
        try:
            if command == _ID_SHOW and self.on_show:
                self.on_show()
            elif command == _ID_TOGGLE and self.on_toggle:
                self.on_toggle()
            elif command == _ID_QUIT and self.on_quit:
                self.on_quit()
        except Exception:
            log.exception('Tray menu action failed')

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == _WM_TRAYICON:
            event = lparam & 0xFFFF
            if event == _WM_RBUTTONUP:
                self._show_menu()
            elif event in (_WM_LBUTTONDBLCLK, _WM_LBUTTONUP):
                if self.on_show:
                    self._dispatch(_ID_SHOW)
            return 0
        if msg == _WM_UPDATE:
            self._modify_icon()
            return 0
        if msg == _WM_QUIT_LOOP:
            _user32.DestroyWindow(hwnd)
            return 0
        if msg == _WM_DESTROY:
            self._remove_icon()
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
