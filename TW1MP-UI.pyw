#!/usr/bin/env python3
"""Windowed launcher for the Tw1mp lobby server.

Double-click on Windows (the .pyw extension keeps the console hidden), or
run `python TW1MP-UI.pyw --help` for options.
"""

import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(
        prog='TW1MP-UI',
        description='Desktop front-end for the Two Worlds 1 lobby server.')
    parser.add_argument('-d', '--dir', default=None,
                        help='server directory (Config.ini, ServerData.db, '
                             'PlayerData); default: this script\'s folder')
    parser.add_argument('--no-start', action='store_true',
                        help='open the window without starting the server')
    parser.add_argument('--tray', action='store_true',
                        help='start minimised to the notification area')
    args = parser.parse_args()

    root_dir = args.dir or os.path.dirname(os.path.abspath(__file__))

    try:
        import tkinter  # noqa: F401  (probe before importing the UI)
    except ImportError:
        sys.stderr.write(
            'tkinter is missing. On Windows re-run the Python installer and '
            'enable "tcl/tk and IDLE"; on Linux install python3-tk.\n')
        return 2

    from tw1mp.gui import run
    run(root_dir=root_dir, start_server=not args.no_start,
        minimised=args.tray)
    return 0


if __name__ == '__main__':
    sys.exit(main())
