#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
"""Checks the size of the queue which is consumed by haproxystats-process

Usage:
    check_haproxystats_queue_size [-f <file> -o <ok> -w <warning> ]

Options:
    -f, --file <file>        configuration file with settings
                             [default: /etc/haproxystats.conf]
    -o, --ok <ok>            OK threshold [default: 60]
    -w, --warning <warning>  WARNING threshold [default: 120]
    -h, --help               show this screen
"""
import os
import sys
from configparser import (ConfigParser, ExtendedInterpolation, NoSectionError,
                          NoOptionError)
from docopt import docopt


def main():
    """
    main code
    """
    args = docopt(__doc__)
    config = ConfigParser(interpolation=ExtendedInterpolation())
    config.read(args['--file'])
    try:
        base_dir = config.get('pull', 'dst-dir')
    except (NoSectionError, NoOptionError) as exc:
        print('OK: missing configuration as I got: {e}'.format(e=exc))
        sys.exit(0)
    std_msg = (": Queue location={b}, Thresholds OK={ok} WARNING={w} and any "
               "higher value is critical").format(b=base_dir,
                                                  ok=args['--ok'],
                                                  w=args['--warning'])
    try:
        dirs = [os.path.join(base_dir, x) for x in os.listdir(base_dir) if
                os.path.isdir(os.path.join(base_dir, x))]
    except (PermissionError, FileNotFoundError, OSError) as exc:
        print("UNKNOWN: can't check {d} due to {e}".format(d=base_dir,
                                                           e=exc))
        sys.exit(3)
    queue_size = len(dirs)
    if queue_size <= int(args['--ok']):
        print('OK: queue size {q}{s}'.format(q=queue_size, s=std_msg))
        sys.exit(0)
    elif int(args['--ok']) < queue_size <= int(args['--warning']):
        print('WARNING: queue size {q}{s}'.format(q=queue_size, s=std_msg))
        sys.exit(1)
    else:
        print('CRITICAL: queue size {q}{s}'.format(q=queue_size, s=std_msg))
        sys.exit(2)

# This is the standard boilerplate that calls the main() function.
if __name__ == '__main__':
    main()
