# -*- coding: utf-8 -*-
# vim:fenc=utf-8
# pylint: disable=too-many-locals
# pylint: disable=too-many-branches
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
import time
import shutil
import socket
import fileinput
from collections import defaultdict
from configparser import ConfigParser, ExtendedInterpolation, ParsingError
from docopt import docopt
import pyinotify
import pandas

from haproxystats import __version__ as VERSION
from haproxystats import DEFAULT_OPTIONS
from haproxystats.utils import (dispatcher, GraphiteHandler, get_files,
                                FileHandler, EventHandler, concat_csv,
                                FILE_SUFFIX_INFO, FILE_SUFFIX_STAT,
                                load_file_content, configuration_check,
                                read_write_access, check_metrics,
                                daemon_percentage_metrics, send_wlc,
                                calculate_percentage_per_column,
                                calculate_percentage_per_row)
from haproxystats.metrics import (DAEMON_AVG_METRICS, DAEMON_METRICS,
                                  SERVER_AVG_METRICS, SERVER_METRICS,
                                  BACKEND_AVG_METRICS, BACKEND_METRICS,
                                  FRONTEND_METRICS)

LOG_FORMAT = ('%(asctime)s [%(process)d] [%(processName)-11s] '
              '[%(funcName)-20s] %(levelname)-8s %(message)s')
logging.basicConfig(format=LOG_FORMAT)
log = logging.getLogger('root')  # pylint: disable=I0011,C0103

watcher = pyinotify.WatchManager()  # pylint: disable=I0011,C0103
# watched events
MASK = pyinotify.IN_CREATE | pyinotify.IN_MOVED_TO  # pylint: disable=no-member

STOP_SIGNAL = 'STOP'


class Consumer(multiprocessing.Process):
    """
    Process statistics and dispatch them to handlers.
    """
    def __init__(self, tasks, config):
        multiprocessing.Process.__init__(self)
        self.tasks = tasks  # A queue to consume items
        self.config = config  # Holds configuration
        self.local_store = None
        self.file_handler = None
        self.timestamp = None  # The time that statistics were retrieved

        # Build graphite path (<namespace>.<hostname>.haproxy)
        graphite_tree = []
        graphite_tree.append(self.config.get('graphite', 'namespace'))
        if self.config.getboolean('graphite', 'prefix-hostname'):
            if self.config.getboolean('graphite', 'fqdn'):
                graphite_tree.append(socket.gethostname().replace('.', '_'))
            else:
                graphite_tree.append(socket.gethostname().split('.')[0])
        graphite_tree.append('haproxy')
        self.graphite_path = '.'.join(graphite_tree)

    def run(self):
        """
        Consume item from queue and process it.

        It is the target function of Process class. Consumes items from
        the queue, processes data which are pulled down by haproxystats-pull
        program and uses Pandas to perform all computations of statistics.

        It exits when it receives STOP_SIGNAL as item.

        To avoid orphan processes on the system, it must be robust against
        failures and try very hard recover from failures.
        """
        if self.config.has_section('local-store'):
            self.local_store = self.config.get('local-store', 'dir')
            self.file_handler = FileHandler()
            dispatcher.register('open', self.file_handler.open)
            dispatcher.register('send', self.file_handler.send)
            dispatcher.register('flush', self.file_handler.flush)
            dispatcher.register('loop', self.file_handler.loop)

        timeout = self.config.getfloat('graphite', 'timeout')
        connect_timeout = self.config.getfloat('graphite',
                                               'connect-timeout',
                                               fallback=timeout)
        write_timeout = self.config.getfloat('graphite',
                                             'write-timeout',
                                             fallback=timeout)
        graphite = GraphiteHandler(
            server=self.config.get('graphite', 'server'),
            port=self.config.getint('graphite', 'port'),
            connect_timeout=connect_timeout,
            write_timeout=write_timeout,
            retries=self.config.getint('graphite', 'retries'),
            interval=self.config.getfloat('graphite', 'interval'),
            delay=self.config.getfloat('graphite', 'delay'),
            backoff=self.config.getfloat('graphite', 'backoff'),
            queue_size=self.config.getint('graphite', 'queue-size')
        )
        dispatcher.register('open', graphite.open)
        dispatcher.register('send', graphite.send)

        dispatcher.signal('open')

        try:
            while True:
                log.info('waiting for item from the queue')
                incoming_dir = self.tasks.get()
                log.info('received item %s', incoming_dir)
                if incoming_dir == STOP_SIGNAL:
                    break
                start_time = time.time()

                # incoming_dir => /var/lib/haproxystats/incoming/1454016646
                # timestamp => 1454016646
                self.timestamp = os.path.basename(incoming_dir)

                # update filename for file handler.
                # This *does not* error if a file handler is not registered.
                dispatcher.signal('loop',
                                  local_store=self.local_store,
                                  timestamp=self.timestamp)

                self.process_stats(incoming_dir)

                # This flushes data to file
                dispatcher.signal('flush')

                # Remove directory as data have been successfully processed.
                log.debug('removing %s', incoming_dir)
                try:
                    shutil.rmtree(incoming_dir)
                except (FileNotFoundError, PermissionError, OSError) as exc:
                    log.critical('failed to remove directory %s with:%s. '
                                 'This should not have happened as it means '
                                 'another worker processed data from this '
                                 'directory or something/someone removed the '
                                 'directory!', incoming_dir, exc)
                elapsed_time = time.time() - start_time
                log.info('total wall clock time in seconds %.3f', elapsed_time)
                data = ("{p}.haproxystats.{m} {v} {t}\n"
                        .format(p=self.graphite_path,
                                m='TotalWallClockTime',
                                v="{t:.3f}".format(t=elapsed_time),
                                t=self.timestamp))
                dispatcher.signal('send', data=data)
                log.info('finished with %s', incoming_dir)
        except KeyboardInterrupt:
            log.critical('Ctrl-C received')

        return

    @send_wlc(output=dispatcher, name='AllStats')
    def process_stats(self, pathname):
        """
        Delegate the processing of statistics to other functions

        Arguments:
            pathname (str): Directory where statistics from HAProxy are saved.
        """
        # statistics for HAProxy daemon and for frontend/backend/server have
        # different format and haproxystats-pull save them using a different
        # file suffix, so we can distinguish them easier.
        files = get_files(pathname, FILE_SUFFIX_INFO)
        if not files:
            log.warning("%s directory doesn't contain any files with HAProxy "
                        "daemon statistics", pathname)
        else:
            self.haproxy_stats(files)
        files = get_files(pathname, FILE_SUFFIX_STAT)

        if not files:
            log.warning("%s directory doesn't contain any files with site "
                        "statistics", pathname)
        else:
            self.sites_stats(files)

    @send_wlc(output=dispatcher, name='HAProxy')
    def haproxy_stats(self, files):
        """
        Process statistics for HAProxy daemon.

        Arguments:
            files (list): A list of files which contain the output of 'show
            info' command on the stats socket.
        """
        cnt_metrics = 1  # a metric counter
        log.info('processing statistics for HAProxy daemon')
        log.debug('processing files %s', ' '.join(files))
        raw_info_stats = defaultdict(list)
        # Parse raw data and build a data structure, input looks like:
        #     Name: HAProxy
        #     Version: 1.6.3-4d747c-52
        #     Release_date: 2016/02/25
        #     Nbproc: 4
        #     Uptime_sec: 59277
        #     SslFrontendSessionReuse_pct: 0
        #     ....
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

        if not raw_info_stats:
            log.error('failed to parse daemon statistics')
            return
        else:
            # Here is where Pandas enters and starts its magic.
            try:
                dataframe = pandas.DataFrame(raw_info_stats)
            except ValueError as exc:
                log.error('failed to create Pandas object for daemon '
                          'statistics %s', exc)
                return

            sums = dataframe.loc[:, DAEMON_METRICS].sum()
            avgs = dataframe.loc[:, DAEMON_AVG_METRICS].mean()
            cnt_metrics += sums.size + avgs.size

            # Pandas did all the hard work, let's join above tables and extract
            # statistics
            for values in pandas.concat([sums, avgs], axis=0).items():
                data = ("{p}.daemon.{m} {v} {t}\n"
                        .format(p=self.graphite_path,
                                m=values[0].replace('.', '_'),
                                v=values[1],
                                t=self.timestamp))
                dispatcher.signal('send', data=data)

            if self.config.getboolean('process', 'calculate-percentages'):
                for metric in daemon_percentage_metrics():
                    cnt_metrics += 1
                    log.info('calculating percentage for %s', metric.name)
                    value = calculate_percentage_per_column(dataframe, metric)
                    data = ("{p}.daemon.{m} {v} {t}\n"
                            .format(p=self.graphite_path,
                                    m=metric.title,
                                    v=value,
                                    t=self.timestamp))
                    dispatcher.signal('send', data=data)

            if self.config.getboolean('process', 'per-process-metrics'):
                log.info("processing statistics per daemon")
                indexed_by_worker = dataframe.set_index('Process_num')
                metrics_per_worker = (indexed_by_worker
                                      .loc[:, DAEMON_METRICS +
                                           DAEMON_AVG_METRICS])
                cnt_metrics += metrics_per_worker.size

                for worker, row in metrics_per_worker.iterrows():
                    for values in row.iteritems():
                        data = ("{p}.daemon.process.{w}.{m} {v} {t}\n"
                                .format(p=self.graphite_path,
                                        w=worker,
                                        m=values[0].replace('.', '_'),
                                        v=values[1],
                                        t=self.timestamp))
                        dispatcher.signal('send', data=data)

                if self.config.getboolean('process', 'calculate-percentages'):
                    for metric in daemon_percentage_metrics():
                        log.info('calculating percentage for %s per daemon',
                                 metric.name)
                        _percentages = (metrics_per_worker
                                        .loc[:, [metric.limit, metric.name]]
                                        .apply(calculate_percentage_per_row,
                                               axis=1,
                                               args=(metric,)))

                        cnt_metrics += _percentages.size
                        for worker, row in _percentages.iterrows():
                            for values in row.iteritems():
                                data = ("{p}.daemon.process.{w}.{m} {v} {t}\n"
                                        .format(p=self.graphite_path,
                                                w=worker,
                                                m=values[0].replace('.', '_'),
                                                v=values[1],
                                                t=self.timestamp))
                                dispatcher.signal('send', data=data)

            data = ("{p}.haproxystats.MetricsHAProxy {v} {t}\n"
                    .format(p=self.graphite_path,
                            v=cnt_metrics,
                            t=self.timestamp))
            dispatcher.signal('send', data=data)

            log.info('number of HAProxy metrics %s', cnt_metrics)
            log.info('finished processing statistics for HAProxy daemon')

    def sites_stats(self, files):
        """
        Process statistics for frontends/backends/servers.

        Arguments:
            files (list): A list of files which contain the output of 'show
            stat' command on the stats socket of HAProxy.
        """
        log.info('processing statistics for sites')
        log.debug('processing files %s', ' '.join(files))
        log.debug('merging multiple csv files to one Pandas data frame')
        data_frame = concat_csv(files)
        filter_backend = None
        if data_frame is not None:
            # Perform some sanitization on the raw data
            if '# pxname' in data_frame.columns:
                log.debug('replace "# pxname" column with  "pxname"')
                data_frame.rename(columns={'# pxname': 'pxname'}, inplace=True)
            if 'Unnamed: 62' in data_frame.columns:
                log.debug('remove "Unnamed: 62" column')
                try:
                    data_frame.drop(labels=['Unnamed: 62'],
                                    axis=1,
                                    inplace=True)
                except ValueError as error:
                    log.warning("failed to drop 'Unnamed: 62' column with: %s",
                                error)

            if not isinstance(data_frame, pandas.DataFrame):
                log.warning('Pandas data frame was not created')
                return
            if len(data_frame.index) == 0:
                log.error('Pandas data frame is empty')
                return

            # For some metrics HAProxy returns nothing, so we replace them
            # with zeros
            data_frame.fillna(0, inplace=True)

            self.process_frontends(data_frame)

            exclude_backends_file = self.config.get('process',
                                                    'exclude-backends',
                                                    fallback=None)
            if exclude_backends_file is not None:
                excluded_backends = load_file_content(exclude_backends_file)
                if excluded_backends:
                    log.info('excluding backends %s', excluded_backends)
                    filter_backend = (~data_frame['pxname']
                                      .isin(excluded_backends))

            self.process_backends(data_frame, filter_backend=filter_backend)
            self.process_servers(data_frame, filter_backend=filter_backend)
            log.info('finished processing statistics for sites')
        else:
            log.error('failed to process statistics for sites')

    @send_wlc(output=dispatcher, name='Frontends')
    def process_frontends(self, data_frame):
        """
        Process statistics for frontends.

        Arguments:
            data_frame (obj): A pandas data_frame ready for processing.
        """
        # Filtering for Pandas
        cnt_metrics = 1
        log.debug('processing statistics for frontends')
        is_frontend = data_frame['svname'] == 'FRONTEND'
        filter_frontend = None
        metrics = self.config.get('process', 'frontend-metrics', fallback=None)

        if metrics is not None:
            metrics = metrics.split(' ')
        else:
            metrics = FRONTEND_METRICS
        log.debug('metric names for frontends %s', metrics)

        exclude_frontends_file = self.config.get('process',
                                                 'exclude-frontends',
                                                 fallback=None)
        if exclude_frontends_file is not None:
            excluded_frontends = load_file_content(exclude_frontends_file)
            if excluded_frontends:  # in case the file is empty
                log.info('excluding frontends %s', excluded_frontends)
                filter_frontend = (~data_frame['pxname']
                                   .isin(excluded_frontends))
        if filter_frontend is not None:
            frontend_stats = (data_frame[is_frontend & filter_frontend]
                              .loc[:, ['pxname'] + metrics])
        else:
            frontend_stats = (data_frame[is_frontend]
                              .loc[:, ['pxname'] + metrics])

        # Group by frontend name and sum values for each column
        frontend_aggr_stats = frontend_stats.groupby(['pxname']).sum()
        cnt_metrics += frontend_aggr_stats.size
        for index, row in frontend_aggr_stats.iterrows():
            name = index.replace('.', '_')
            for i in row.iteritems():
                data = ("{p}.frontend.{f}.{m} {v} {t}\n"
                        .format(p=self.graphite_path,
                                f=name,
                                m=i[0],
                                v=i[1],
                                t=self.timestamp))
                dispatcher.signal('send', data=data)

        data = ("{p}.haproxystats.MetricsFrontend {v} {t}\n"
                .format(p=self.graphite_path,
                        v=cnt_metrics,
                        t=self.timestamp))
        dispatcher.signal('send', data=data)
        log.info('number of frontend metrics %s', cnt_metrics)

        log.debug('finished processing statistics for frontends')

    @send_wlc(output=dispatcher, name='Backends')
    def process_backends(self, data_frame, *, filter_backend=None):
        """
        Process statistics for backends.

        Arguments:
            data_frame (obj): A pandas data_frame ready for processing.
            filter_backend: A filter to apply on data_frame.
        """
        cnt_metrics = 1
        log.debug('processing statistics for backends')
        # Filtering for Pandas
        is_backend = data_frame['svname'] == 'BACKEND'

        metrics = self.config.get('process', 'backend-metrics', fallback=None)
        if metrics is not None:
            metrics = metrics.split(' ')
        else:
            metrics = BACKEND_METRICS
        log.debug('metric names for backends %s', metrics)
        # Get rows only for backends. For some metrics we need the sum and
        # for others the average, thus we split them.
        if filter_backend is not None:
            stats_sum = (data_frame[is_backend & filter_backend]
                         .loc[:, ['pxname'] + metrics])
            stats_avg = (data_frame[is_backend & filter_backend]
                         .loc[:, ['pxname'] + BACKEND_AVG_METRICS])
        else:
            stats_sum = data_frame[is_backend].loc[:, ['pxname'] + metrics]
            stats_avg = (data_frame[is_backend]
                         .loc[:, ['pxname'] + BACKEND_AVG_METRICS])

        aggr_sum = stats_sum.groupby(['pxname'], as_index=False).sum()
        aggr_avg = stats_avg.groupby(['pxname'], as_index=False).mean()
        merged_stats = pandas.merge(aggr_sum, aggr_avg, on='pxname')

        rows, columns = merged_stats.shape
        cnt_metrics += rows * (columns - 1)  # minus the index

        for _, row in merged_stats.iterrows():
            name = row[0].replace('.', '_')
            for i in row[1:].iteritems():
                data = ("{p}.backend.{b}.{m} {v} {t}\n"
                        .format(p=self.graphite_path,
                                b=name,
                                m=i[0],
                                v=i[1],
                                t=self.timestamp))
                dispatcher.signal('send', data=data)

        data = ("{p}.haproxystats.MetricsBackend {v} {t}\n"
                .format(p=self.graphite_path,
                        v=cnt_metrics,
                        t=self.timestamp))
        dispatcher.signal('send', data=data)

        log.info('number of backend metrics %s', cnt_metrics)
        log.debug('finished processing statistics for backends')

    @send_wlc(output=dispatcher, name='Servers')
    def process_servers(self, data_frame, *, filter_backend=None):
        """
        Process statistics for servers.

        Arguments:
            data_frame (obj): A pandas data_frame ready for processing.
            filter_backend: A filter to apply on data_frame.
        """
        cnt_metrics = 1
        # A filter for rows with stats for servers
        is_server = data_frame['type'] == 2

        log.debug('processing statistics for servers')

        server_metrics = self.config.get('process',
                                         'server-metrics',
                                         fallback=None)
        if server_metrics is not None:
            server_metrics = server_metrics.split(' ')
        else:
            server_metrics = SERVER_METRICS
        log.debug('metric names for servers %s', server_metrics)
        # Get rows only for servers. For some metrics we need the sum and
        # for others the average, thus we split them.
        if filter_backend is not None:
            stats_sum = (data_frame[is_server & filter_backend]
                         .loc[:, ['pxname', 'svname'] + server_metrics])
            stats_avg = (data_frame[is_server & filter_backend]
                         .loc[:, ['pxname', 'svname'] + SERVER_AVG_METRICS])
        else:
            stats_sum = (data_frame[is_server]
                         .loc[:, ['pxname', 'svname'] + server_metrics])
            stats_avg = (data_frame[is_server]
                         .loc[:, ['pxname', 'svname'] + SERVER_AVG_METRICS])

        aggr_sum = (stats_sum
                    .groupby(['pxname', 'svname'], as_index=False)
                    .sum())
        aggr_avg = (stats_avg
                    .groupby(['pxname', 'svname'], as_index=False)
                    .mean())
        merged_stats = pandas.merge(aggr_sum,
                                    aggr_avg,
                                    on=['svname', 'pxname'])
        rows, columns = merged_stats.shape
        cnt_metrics += rows * (columns - 2)

        for _, row in merged_stats.iterrows():
            backend = row[0].replace('.', '_')
            server = row[1].replace('.', '_')
            for i in row[2:].iteritems():
                data = ("{p}.backend.{b}.server.{s}.{m} {v} {t}\n"
                        .format(p=self.graphite_path,
                                b=backend,
                                s=server,
                                m=i[0],
                                v=i[1],
                                t=self.timestamp))
                dispatcher.signal('send', data=data)

        if self.config.getboolean('process', 'aggr-server-metrics'):
            log.info('aggregate stats for servers across all backends')
            # Produce statistics for servers across all backends
            stats_sum = (data_frame[is_server]
                         .loc[:, ['svname'] + SERVER_METRICS])
            stats_avg = (data_frame[is_server]
                         .loc[:, ['svname'] + SERVER_AVG_METRICS])
            aggr_sum = (stats_sum
                        .groupby(['svname'], as_index=False)
                        .sum())
            aggr_avg = (stats_avg
                        .groupby(['svname'], as_index=False)
                        .mean())
            merged_stats = pandas.merge(aggr_sum, aggr_avg, on=['svname'])
            rows, columns = merged_stats.shape
            cnt_metrics += rows * (columns - 1)  # minus the index

            for _, row in merged_stats.iterrows():
                server = row[0].replace('.', '_')
                for i in row[1:].iteritems():
                    data = ("{p}.server.{s}.{m} {v} {t}\n"
                            .format(p=self.graphite_path,
                                    s=server,
                                    m=i[0],
                                    v=i[1],
                                    t=self.timestamp))
                    dispatcher.signal('send', data=data)

        data = ("{p}.haproxystats.MetricsServer {v} {t}\n"
                .format(p=self.graphite_path,
                        v=cnt_metrics,
                        t=self.timestamp))
        dispatcher.signal('send', data=data)

        log.info('number of server metrics %s', cnt_metrics)
        log.debug('finished processing statistics for servers')


def main():
    """
    Parse CLI arguments and launches main program
    """
    args = docopt(__doc__, version=VERSION)

    config = ConfigParser(interpolation=ExtendedInterpolation())
    # Set defaults for all sections
    config.read_dict(copy.copy(DEFAULT_OPTIONS))
    try:
        config.read(args['--file'])
    except ParsingError as exc:
        sys.exit(str(exc))

    incoming_dir = config.get('process', 'src-dir')

    if args['--print']:
        for section in sorted(DEFAULT_OPTIONS):
            if section == 'pull':
                continue
            print("[{}]".format(section))
            for key, value in sorted(DEFAULT_OPTIONS[section].items()):
                print("{k} = {v}".format(k=key, v=value))
            print()
        sys.exit(0)
    if args['--print-conf']:
        for section in sorted(config):
            if section == 'pull':
                continue
            print("[{}]".format(section))
            for key, value in sorted(config[section].items()):
                print("{k} = {v}".format(k=key, v=value))
            print()
        sys.exit(0)

    try:
        configuration_check(config, 'paths')
        configuration_check(config, 'process')
        configuration_check(config, 'graphite')
        read_write_access(config.get('process', 'src-dir'))
        check_metrics(config)
    except ValueError as exc:
        sys.exit(str(exc))

    tasks = multiprocessing.Queue()
    handler = EventHandler(tasks=tasks)
    notifier = pyinotify.Notifier(watcher, handler)
    num_consumers = config.getint('process', 'workers')
    incoming_dir = config.get('process', 'src-dir')

    loglevel =\
        config.get('process', 'loglevel').upper()  # pylint: disable=no-member
    log.setLevel(getattr(logging, loglevel, None))

    log.info('haproxystats-processs %s version started', VERSION)
    # process incoming data which were retrieved while processing was stopped
    for pathname in glob.iglob(incoming_dir + '/*'):
        if os.path.isdir(pathname):
            log.info('putting %s in queue', pathname)
            tasks.put(pathname)

    def shutdown(signalnb=None, frame=None):
        """
        Signal processes to exit

        It adds STOP_SIGNAL to the queue, which causes processes to exit in a
        clean way.

        Arguments:
            signalnb (int): The ID of signal
            frame (obj): Frame object at the time of receiving the signal
        """
        log.info('received %s at %s', signalnb, frame)
        notifier.stop()
        for _ in range(num_consumers):
            log.info('sending stop signal to worker')
            tasks.put(STOP_SIGNAL)
        log.info('waiting for workers to finish their work')
        for consumer in consumers:
            consumer.join()
        log.info('exiting')
        sys.exit(0)

    # Register our graceful shutdown process to termination signals
    signal.signal(signal.SIGHUP, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Add our watcher
    while True:
        try:
            log.info('adding a watch for %s', incoming_dir)
            watcher.add_watch(incoming_dir, MASK, quiet=False, rec=False)
        except pyinotify.WatchManagerError as error:
            log.error('received error (%s), going to retry in few seconds',
                      error)
            time.sleep(3)
        else:
            break

    log.info('creating %d consumers', num_consumers)
    consumers = [Consumer(tasks, config) for i in range(num_consumers)]
    for consumer in consumers:
        consumer.start()

    log.info('watching %s directory for incoming data', incoming_dir)
    notifier.loop(daemonize=False)

if __name__ == '__main__':
    main()
