"""
haproxstats.utils
~~~~~~~~~~~~~~~~~~

This module provides functions, constants and classes that are used within
haproxystats.
"""
import os
import stat
from collections import defaultdict
import io
import socket
import shutil
import logging
import sys
import glob
import pyinotify
import pandas


log = logging.getLogger('root')  # pylint: disable=I0011,C0103

FILE_SUFFIX_INFO = '_info'
FILE_SUFFIX_STAT = '_stat'
CMD_SUFFIX_MAP = {'info': FILE_SUFFIX_INFO, 'stat': FILE_SUFFIX_STAT}

DAEMON_METRICS = [
    'CompressBpsIn',
    'CompressBpsOut',
    'CompressBpsRateLim',
    'ConnRate',
    'ConnRateLimit'
    'CumConns',
    'CumReq',
    'CumSslConns',
    'CurrConns',
    'CurrSslConns',
    'Hard_maxcon',
    'MaxConnRate',
    'MaxSessRate',
    'MaxSslConns',
    'MaxSslRate',
    'MaxZlibMemUsage',
    'Maxconn',
    'Maxpipes',
    'Maxsock',
    'Memmax_MB',
    'PipesFree',
    'PipesUsed',
    'Run_queue',
    'SessRate',
    'SessRateLimit',
    'SslBackendKeyRate',
    'SslBackendMaxKeyRate',
    'SslCacheLookups',
    'SslCacheMisses',
    'SslFrontendKeyRate',
    'SslFrontendMaxKeyRate',
    'SslFrontendSessionReuse_pct',
    'SslRate',
    'SslRateLimit',
    'Tasks',
    'Ulimit-n',
    'Uptime_sec',
    'ZlibMemUsage',
]

DAEMON_AVG_METRICS = [
    'Idle_pct',
]
AVG_METRICS = [
    'act',
    'bck',
    'check_duration',
    'ctime',
    'downtime',
    'qlimit',
    'qtime',
    'rtime',
    'throttle',
    'ttime',
    'weight',
]
SERVER_METRICS = [
    'qcur',
    'qmax',
    'scur',
    'smax',
    'stot',
    'bin',
    'bout',
    'dresp',
    'econ',
    'eresp',
    'wretr',
    'wredis',
    'act',
    'bck',
    'chkfail',
    'qlimit',
    'lbtot',
    'rate',
    'rate_max',
    'hrsp_1xx',
    'hrsp_2xx',
    'hrsp_3xx',
    'hrsp_4xx',
    'hrsp_5xx',
    'hrsp_other',
    'cli_abrt',
    'srv_abrt',
]
SERVER_AVG_METRICS = [
    'qtime',
    'rtime',
    'throttle',
    'ttime',
    'weight',
]
BACKEND_METRICS = [
    'act',
    'bck',
    'bin',
    'bout',
    'chkdown',
    'cli_abrt',
    'comp_byp',
    'comp_in',
    'comp_out',
    'comp_rsp',
    'downtime',
    'dreq',
    'dresp',
    'econ',
    'eresp',
    'hrsp_1xx',
    'hrsp_2xx',
    'hrsp_3xx',
    'hrsp_4xx',
    'hrsp_5xx',
    'hrsp_other',
    'lbtot',
    'qcur',
    'qmax',
    'rate',
    'rate_max',
    'scur',
    'slim',
    'smax',
    'srv_abrt',
    'stot',
    'wredis',
    'wretr',
]
BACKEND_AVG_METRICS = [
    'rtime',
    'ctime',
    'qtime',
    'ttime',
    'weight',
]
FRONTEND_METRICS = [
    'bin',
    'bout',
    'comp_byp',
    'comp_in',
    'comp_out',
    'comp_rsp',
    'dreq',
    'dresp',
    'ereq',
    'hrsp_1xx',
    'hrsp_2xx',
    'hrsp_3xx',
    'hrsp_4xx',
    'hrsp_5xx',
    'hrsp_other',
    'rate',
    'rate_lim',
    'rate_max',
    'req_rate',
    'req_rate_max',
    'req_tot',
    'scur',
    'slim',
    'smax',
    'stot',
]


def is_unix_socket(path):
    """Check if path is a valid UNIX socket.

    Arguments:
        path (str): A file name path

    Returns:
        True if path is a valid UNIX socket otherwise False.
    """
    mode = os.stat(path).st_mode

    return stat.S_ISSOCK(mode)


def concat_csv(csv_files):
    """Performs a concatenation along several csv files.

    Arguments:
        csv_files (lst): A list of csv files.

    Returns:
        A pandas data frame object.
    """
    data_frames = []
    for csv_file in csv_files:
        data_frames.append(pandas.read_csv(csv_file, low_memory=False))

    return pandas.concat(data_frames)


def get_files(path, suffix):
    """Returns the files from a directory which match a suffix

    Arguments:
        path (str): Pathname
        suffix (str): Suffix to match against

    Returns:
        A list of filenames
    """
    files = []

    for filename in glob.glob(path + '/*{s}'.format(s=suffix)):
        files.append(filename)

    return files


class Dispatcher(object):
    """Dispatch data to different handlers"""
    def __init__(self):
        self.handlers = defaultdict(list)

    def register(self, signal, callback):
        """Register a callback to a singal

        Multiple callbacks can be assigned to the same signal.

        Arguments:
            signal (str): The name of the signal
            callbacl (obj): A callable object to call for the given signal.
            """
        self.handlers[signal].append(callback)

    def signal(self, signal, **kwargs):
        """Run registered handlers

        Arguments:
            signal (str): A registered signal
        """
        for handler in self.handlers.get(signal):
            handler(**kwargs)


class GraphiteHandler():
    """A handler to send data to graphite.

    Arguments:
        server (str): Server name or IP address.
        port (int): Port to connect to
        timeout (int): Timeout on connection
        retry (int): Numbers to retry on connection failure
        """
    def __init__(self, server, port=3002, timeout=1, retry=1):
        self.server = server
        self.port = port
        self.retry = retry
        self.timeout = timeout
        self.connection = socket.create_connection((self.server, self.port),
                                                   timeout=self.timeout)

    def send(self, **kwargs):
        """Send data to a graphite relay"""
        self.connection.sendall(bytes(kwargs.get('data'), 'utf-8'))

    def close(self, **kwargs):
        """Close TCP connection to Graphite relay"""
        self.connection.close()
        self.connection = socket.create_connection((self.server, self.port),
                                                   timeout=self.timeout)


dispatcher = Dispatcher()  # pylint: disable=I0011,C0103


class FileHandler():
    """A handler to write data to a file"""
    def __init__(self):
        self._input = io.StringIO()
        self._output = None

    def send(self, **kwargs):
        """Write data to a file-like object"""
        self._input.write(kwargs.get('data'))

    def set_path(self, filepath):
        """Set the filemath to send data.

        Arguments:
            filepath (str): The pathname of the file
        """
        log.debug('filepath for local-store set to %s', filepath)
        self._output = open(filepath, 'w')

    def loop(self, **kwargs):
        """Rotate the file"""
        base_dir = os.path.join(kwargs.get('local_store'),
                                kwargs.get('epoch_time'))
        try:
            os.makedirs(base_dir)
        except OSError as exc:
            # errno 17 => file exists
            if exc.errno != 17:
                sys.exit("failed to make directory {d}:{e}".format(
                    d=base_dir, e=exc))
        self.set_path(filepath=os.path.join(base_dir, 'stats'))

    def close(self, **kwargs):
        """Flush data to disk"""
        self._input.seek(0)
        shutil.copyfileobj(self._input, self._output)
        self._output.flush()
        self._output.close()
        self._input.close()
        self._input = io.StringIO()


class EventHandler(pyinotify.ProcessEvent):
    """An event handler for inotify to push items to a queue.

    If the event isn't for a directory no action is taken.

    Arguments:
        tasks (queue obj): A queue to put items.
    """
    def my_init(self, tasks):
        self.tasks = tasks  # pylint: disable=W0201

    def _put_item_to_queue(self, pathname):
        """Add item to queue if and only if the pathname is a directory"""
        if os.path.isdir(pathname):
            log.info('putting %s in queue', pathname)
            self.tasks.put(pathname)
        else:
            log.info("ignore %s as it isn't directory", pathname)

    def process_IN_CREATE(self, event):  # pylint: disable=C0103
        "Invoked when a directory is created."
        log.debug('received an event for CREATE')
        self._put_item_to_queue(event.pathname)

    def process_IN_MOVED_TO(self, event):  # pylint: disable=C0103
        "Invoked when a directory/file is moved"
        log.debug('received an event for MOVE')
        self._put_item_to_queue(event.pathname)
