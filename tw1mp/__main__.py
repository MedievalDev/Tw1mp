"""Command line entry point: python -m tw1mp"""

import argparse
import logging
import os
import sys

from . import __version__
from .config import Config
from .server import run


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='tw1mp',
        description='Two Worlds 1 community multiplayer lobby server')
    parser.add_argument('--version', action='version',
                        version=f'tw1mp {__version__}')
    parser.add_argument('-r', '--root', default=os.getcwd(),
                        help='data directory for Config.ini, ServerData.db '
                             'and PlayerData (default: current directory)')
    parser.add_argument('-c', '--config',
                        help='path to Config.ini (default: <root>/Config.ini)')
    parser.add_argument('-p', '--port', type=int,
                        help='override the lobby port from the config')
    parser.add_argument('-l', '--log',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                        default='INFO', help='console log level')
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log),
        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s')

    config = Config(root=args.root, path=args.config)
    if args.port:
        config.port = args.port
    run(config)
    return 0


if __name__ == '__main__':
    sys.exit(main())
