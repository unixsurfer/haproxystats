#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Processes statistics from HAProxy and pushes them to Graphite

Usage:
    haproxystats-process [-f <file> ] [-p | -P]

Options:
    -f, --file <file>  configuration file with settings
                       [default: /etc/haproxystats.conf]
    -p, --print        show default settings
    -P, --print-conf   show configuration
    -h, --help         show this screen
    -v, --version      show version
"""
import os
import multiprocessing
import signal
import logging
import glob
import copy
import sys
import shutil
import socket
import fileinput
from collections import defaultdict
from configparser import ConfigParser, ExtendedInterpolation
from docopt import docopt
import pyinotify
import pandas

from haproxystats import __version__ as VERSION
from haproxystats import DEFAULT_OPTIONS
from haproxystats.utils import (dispatcher, GraphiteHandler, get_files,
                                FileHandler, EventHandler, concat_csv,
                                FILE_SUFFIX_INFO, FILE_SUFFIX_STAT,
                                DAEMON_AVG_METRICS, DAEMON_METRICS,
                                SERVER_AVG_METRICS, SERVER_METRICS,
                                BACKEND_AVG_METRICS, BACKEND_METRICS,
                                FRONTEND_METRICS)

LOG_FORMAT = ('%(asctime)s [%(process)d] [%(processName)-11s] '
              '[%(funcName)-20s] %(levelname)-8s %(message)s')
logging.basicConfig(format=LOG_FORMAT)
log = logging.getLogger('root')  # pylint: disable=I0011,C0103

watcher = pyinotify.WatchManager()  # pylint: disable=I0011,C0103
# watched events
MASK = pyinotify.IN_CREATE | pyinotify.IN_MOVED_TO

STOP_SIGNAL = 'STOP'


class Consumer(multiprocessing.Process):
    """Process statistics and dispatch them to handlers."""
    def __init__(self, tasks, config):
        multiprocessing.Process.__init__(self)
        self.tasks = tasks
        self.config = config
        self.local_store = None
        self.file_handler = None
        self.epoch = None
        graphite_tree = []

        graphite_tree.append(self.config.get('graphite', 'namespace'))
        if self.config.getboolean('graphite', 'prefix_hostname'):
            if self.config.getboolean('graphite', 'fqdn'):
                graphite_tree.append(socket.gethostname())
            else:
                graphite_tree.append(socket.gethostname().split('.')[0])

        self.graphite_path = '.'.join([x.replace('.', '_')
                                       for x in graphite_tree])

    def run(self):
        try:
            if self.config.has_section('local-store'):
                self.local_store = self.config.get('local-store', 'dir')
                self.file_handler = FileHandler()
                dispatcher.register('send', self.file_handler.send)
                dispatcher.register('close', self.file_handler.close)
                dispatcher.register('loop', self.file_handler.loop)
            else:
                self.local_store = None

            graphite = GraphiteHandler(self.config.get('graphite', 'server'),
                                       self.config.getint('graphite', 'port'))
            dispatcher.register('send', graphite.send)
            dispatcher.register('close', graphite.close)
            while True:
                log.info('waiting for item from the queue')
                incoming_dir = self.tasks.get()
                # incoming_dir => /var/lib/haproxystats/incoming/1454016646
                self.epoch = os.path.basename(incoming_dir)

                # update filename for file handler.
                # This *does not* error if a file handler is not registered.
                dispatcher.signal('loop', local_store=self.local_store,
                                  epoch_time=self.epoch)

                log.info('received item %s', incoming_dir)
                if incoming_dir == STOP_SIGNAL:
                    break

                self.process_stats(incoming_dir)

                # This flushes data to file and closes the TCP connection to
                # graphite relay.
                dispatcher.signal('close')

                # Remove directory from incoming as we have sucessfully
                # processed the statistics.
                log.debug('removing %s', incoming_dir)
                shutil.rmtree(incoming_dir)
                log.info('finished with %s', incoming_dir)
        except KeyboardInterrupt:
            log.critical('Ctrl-C received')

        return

    def process_stats(self, pathname):
        """Process all statistics.

        Arguments:
            pathname (str): A directory pathname where statistics from HAProxy
            are saved.
        """
        self.haproxy_stats(get_files(pathname, FILE_SUFFIX_INFO))
        self.sites_stats(get_files(pathname, FILE_SUFFIX_STAT))

    def haproxy_stats(self, files):
        """Process statistics for HAProxy process.

        Arguments:
            files (list): A list of file which contain raw data from HAProxy
        """
        log.debug('processing files %s', ','.join(files))

        raw_info_stats = defaultdict(list)
        # Parse raw data and build a data structure
        with fileinput.input(files=files) as file_input:
            for line in file_input:
                if ': ' in line:
                    key, value = line.split(': ', 1)
                    try:
                        numeric_value = int(value)
                    except ValueError:
                        pass
                    else:
                        raw_info_stats[key].append(numeric_value)

        # Here is where Pandas enters and starts its magic.
        dataframe = pandas.DataFrame(raw_info_stats)
        # Get sum/average for metric
        sums = dataframe.loc[:, DAEMON_METRICS].sum()
        avgs = dataframe.loc[:, DAEMON_AVG_METRICS].mean()
        # Pandas did all the hard work, let's join above tables and extract
        # the statistics
        for values in pandas.concat([sums, avgs], axis=0).items():
            data = "{path}.haproxy.daemon.{metric} {value} {time}\n".format(
                path=self.graphite_path, metric=values[0].replace('.', '_'),
                value=values[1], time=self.epoch)
            dispatcher.signal('send', data=data)

    def sites_stats(self, files):
        """Process statistics for frontends/backends/servers.

        Arguments:
            files (list): A list of file which contain raw data from HAProxy
        """
        log.debug('processing statistics from %s', files)

        # Merge multiple csv files to one pandas data frame
        data_frame = concat_csv(files)

        # Perform some sanitization on the raw data
        if '# pxname' in '# pxname':
            data_frame.rename(columns={'# pxname': 'pxname'}, inplace=True)
        if 'Unnamed: 62' in data_frame.columns:
            data_frame.drop(labels=['Unnamed: 62'], axis=1, inplace=True)

        # For some metric HAProxy returns nothing and replace them with zero
        data_frame.fillna(0, inplace=True)

        self.process_frontends(data_frame)
        self.process_backends(data_frame)
        self.process_servers(data_frame)

    def process_frontends(self, data_frame):
        """Process statistics for frontends.

        Arguments:
            data_frame (obj): A pandas data_frame ready for processing.
        """
        # Filtering for Pandas
        is_frontend = data_frame['svname'] == 'FRONTEND'

        # Get rows only for frontends and only a selection of columns
        frontend_stats = data_frame[is_frontend].loc[:, ['pxname'] +
                                                     FRONTEND_METRICS]
        # Group by frontend name and sum values for each column
        frontend_aggr_stats = frontend_stats.groupby(['pxname']).sum()
        for index, row in frontend_aggr_stats.iterrows():
            name = index.replace('.', '_')
            for i in row.iteritems():
                data = ("{path}.frontend.{frontend}.{metric} {value} "
                        "{time}\n").format(path=self.graphite_path,
                                           frontend=name, metric=i[0],
                                           value=i[1], time=self.epoch)
                dispatcher.signal('send', data=data)

    def process_backends(self, data_frame):
        """Process statistics for backends.

        Arguments:
            data_frame (obj): A pandas data_frame ready for processing.
        """
        # Filtering for Pandas
        is_backend = data_frame['svname'] == 'BACKEND'

        # Get rows only for backends. For some metrics we need the sum and
        # for others the average, thus we split them.
        backend_stats_sum = data_frame[is_backend].loc[:, ['pxname'] +
                                                       BACKEND_METRICS]
        backend_stats_avg = data_frame[is_backend].loc[:, ['pxname'] +
                                                       BACKEND_AVG_METRICS]
        backend_aggr_sum = backend_stats_sum.groupby(['pxname'],
                                                     as_index=False).sum()

        backend_aggr_avg = backend_stats_avg.groupby(['pxname'],
                                                     as_index=False).mean()
        backend_merged_stats = pandas.merge(backend_aggr_sum, backend_aggr_avg,
                                            on='pxname')
        for _, row in backend_merged_stats.iterrows():
            name = row[0].replace('.', '_')
            for i in row[1:].iteritems():
                data = ("{path}.backend.{backend}.{metric} {value} "
                        "{time}\n").format(path=self.graphite_path,
                                           backend=name, metric=i[0],
                                           value=i[1], time=self.epoch)
                dispatcher.signal('send', data=data)

    def process_servers(self, data_frame):
        """Process statistics for servers.

        Arguments:
            data_frame (obj): A pandas data_frame ready for processing.
        """
        # A filter for rows with stats for servers
        is_server = data_frame['type'] == 2

        # Get rows only for servers. For some metrics we need the sum and
        # for others the average, thus we split them.
        server_stats_sum = data_frame[is_server].loc[:, ['pxname', 'svname'] +
                                                     SERVER_METRICS]
        server_aggr_sum = server_stats_sum.groupby(['pxname', 'svname'],
                                                   as_index=False).sum()
        server_stats_avg = data_frame[is_server].loc[:, ['pxname', 'svname'] +
                                                     SERVER_AVG_METRICS]
        server_aggr_avg = server_stats_avg.groupby(['pxname', 'svname'],
                                                   as_index=False).mean()
        server_merged_stats = pandas.merge(server_aggr_sum, server_aggr_avg,
                                           on=['svname', 'pxname'])
        for _, row in server_merged_stats.iterrows():
            backend = row[0].replace('.', '_')
            server = row[1].replace('.', '_')
            for i in row[2:].iteritems():
                data = ("{path}.backend.{backend}.server.{server}.{metric} "
                        "{value} {time}\n").format(path=self.graphite_path,
                                                   backend=backend,
                                                   server=server,
                                                   metric=i[0],
                                                   value=i[1],
                                                   time=self.epoch)
                dispatcher.signal('send', data=data)

        # Produce statistics for servers across all backends
        server_sum_metrics = data_frame[is_server].loc[:, ['svname'] +
                                                       SERVER_METRICS]
        server_avg_metrics = data_frame[is_server].loc[:, ['svname'] +
                                                       SERVER_AVG_METRICS]
        server_sum_values = server_sum_metrics.groupby(['svname'],
                                                       as_index=False).sum()
        server_avg_values = server_avg_metrics.groupby(['svname'],
                                                       as_index=False).mean()
        server_values = pandas.merge(server_sum_values, server_avg_values,
                                     on=['svname'])
        for _, row in server_values.iterrows():
            server = row[0].replace('.', '_')
            for i in row[1:].iteritems():
                data = ("{path}.server.{server}.{metric} {value} "
                        "{time}\n").format(path=self.graphite_path,
                                           server=server,
                                           metric=i[0],
                                           value=i[1],
                                           time=self.epoch)
                dispatcher.signal('send', data=data)


def main():
    """Parses CLI arguments and launches main program."""
    args = docopt(__doc__, version=VERSION)

    config = ConfigParser(interpolation=ExtendedInterpolation())
    # Set defaults for all sections
    config.read_dict(copy.copy(DEFAULT_OPTIONS))
    config.read(args['--file'])

    if args['--print']:
        for section in sorted(DEFAULT_OPTIONS):
            print("[{}]".format(section))
            for key, value in sorted(DEFAULT_OPTIONS[section].items()):
                print("{k} = {v}".format(k=key, v=value))
            print()
        sys.exit(0)
    if args['--print-conf']:
        for section in sorted(config):
            print("[{}]".format(section))
            for key, value in sorted(config[section].items()):
                print("{k} = {v}".format(k=key, v=value))
            print()
        sys.exit(0)

    tasks = multiprocessing.Queue()
    handler = EventHandler(tasks=tasks)
    notifier = pyinotify.Notifier(watcher, handler)
    num_consumers = config.getint('process', 'workers')
    incoming_dir = config.get('pull', 'dst-dir')

    log.setLevel(getattr(logging, config.get('process', 'loglevel').upper(),
                         None))

    # process incoming data which were created while processing was stoppped
    for pathname in glob.iglob(incoming_dir + '/*'):
        if os.path.isdir(pathname):
            log.info('putting %s in queue', pathname)
            tasks.put(pathname)

    def shutdown(signalnb=None, frame=None):
        """Signal processes to exit

        It adds STOP_SIGNAL to the queue, which causes processes to exit in a
        clean way.

        Arguments:
            signalnb (int): The ID of signal
            frame (obj): Frame object at the time of receiving the signal
        """
        log.info('received %s at %s', signalnb, frame)
        notifier.stop()
        for _ in range(num_consumers):
            log.info('sending stop signal to workers')
            tasks.put(STOP_SIGNAL)
        log.info('waiting for workers to finish their work')
        for consumer in consumers:
            consumer.join()
        log.info('exiting')
        sys.exit(0)

    # Register our gracefull shutdown to termination signals
    signal.signal(signal.SIGHUP, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Add our watcher.
    watcher.add_watch(incoming_dir, MASK, rec=False)

    log.info('creating %d consumers', num_consumers)
    consumers = [Consumer(tasks, config) for i in range(num_consumers)]
    for consumer in consumers:
        consumer.start()

    log.info('watching %s directory for incoming data', incoming_dir)
    notifier.loop(daemonize=False)

if __name__ == '__main__':
    main()
