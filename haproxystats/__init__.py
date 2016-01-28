# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
"""
A collection of Python tools to process HAProxy stats
"""
__title__ = 'haproxystats'
__author__ = 'Pavlos Parissis'
__license__ = 'Apache 2.0'
__version__ = '0.0.2'
__copyright__ = 'Copyright 2016 Pavlos Parissis <pavlos.parissis@gmail.com'

DEFAULT_OPTIONS = {
    'DEFAULT': {
        'retry': '2',
        'timeout': '1',
        'interval': '2',
        'loglevel': 'info',
    },
    'paths': {
        'base-dir': '/var/lib/haproxystats',
    },
    'pull': {
        'socket-dir': '/run/haproxy',
        'retry': '1',
        'timeout': '1',
        'interval': '1',
        'pull-interval': '10',
        'dst-dir': '/var/lib/haproxystats/incoming',
        'tmp-dst-dir': '/var/lib/haproxystats/incoming.tmp',
        'workers': '8',
    },
    'process': {
        'workers': '4',
    },
    'graphite': {
        'server': '127.0.0.1',
        'port': '3002',
        'namespace': 'loadbalancers',
        'prefix_hostname': 'true',
        'fqdn': 'true',
    },
    'local-stire': {
        'dir': '/var/lib/haproxystats/local-store',
    }
}
