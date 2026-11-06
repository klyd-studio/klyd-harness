"""klyd - architectural memory for coding agents."""

__version__ = "0.2.4"

import sys

if __name__ == '__main__':
    from klyd.cli import cli
    sys.argv[0] = 'python -m klyd'
    cli()
