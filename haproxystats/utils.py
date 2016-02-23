"""
haproxstats.utils
~~~~~~~~~~~~~~~~~~

This module provides functions, constants and classes that are used within
haproxystats.
"""
import os
import stat
from collections import defaultdict, deque
import io
import socket
import shutil
import logging
import time
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


class BrokenConnection(Exception):
    def __init__(self, raised):
        self.raised = raised


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
        A pandas data frame object or None if fails to parse csv_files
    """
    data_frames = []
    for csv_file in csv_files:
        try:
            data_frame = pandas.read_csv(csv_file, low_memory=False)
        except ValueError as error:
            log.error('Pandas failed to parse %s file with: %s', csv_file,
                      error)
        else:
            data_frames.append(data_frame)
    if data_frames:
        return pandas.concat(data_frames)
    else:
        return None


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


def log_hook(raised_exception, func_name, remaining_tries, sleep_time):
    """Log a message with specific message.

    Used as a hook in the retries decorator.

    Arguments:
        raised_exception (obj): An exception object
        func_name (str): Name of the function
        remaining_tries (int): Number of remaing tries
        sleep_time (float): Sleep time
    """
    log.error('caught "%s" when "%s" was run, remaining tries %s, sleeping '
              'for %.2f seconds', raised_exception, func_name, remaining_tries,
              sleep_time)


def retries(retries=3,
            interval=0.9,
            backoff=3,
            delay=10,
            exceptions=(ConnectionResetError, ConnectionRefusedError,
                        ConnectionAbortedError, BrokenPipeError, OSError),
            hook=log_hook,
            exception_to_raise=BrokenConnection):
    """A decorator which implements a retrying logic.

    Arguments:
        retries (int): Maximum times to retry
        interval (float): Sleep this many seconds between retries
        backoff (int): Multiply interval by this factor after each failure
        exceptions (tuple): A list of exceptions to catch
        hook (callable obj): A function which accepts the following positional
        arguments:
            raised_exception, func_name, remaining_tries, sleep_time
        exception_to_raise (obj): An exception to raise when maximum tries
            have been reached.

    The decorator calls the function up to retries times if it raises an
    exception from the tuple. The decorated function will only be retried if
    it raises one of the specified exceptions. Additionally you may specify a
    hook function which will be called prior to retrying. This is primarily
    intended to give the opportunity to log the failure. Hook is not called
    after failure if no retries remain.
    """
    def dec(func):
        def decorated_func(*args, **kwargs):
            backoff_interval = interval
            raised = None
            attempt = 0  # times to attempt a connect after a failure
            if retries == -1:
                # -1 means retry indefinitely
                attempt = -1
            elif retries == 0:
                # Zero means don't retry
                attempt = 1
            else:
                # any other value means retry N times
                attempt = retries + 1
            while attempt != 0:
                # an exception was raised, sleep and bump backoff
                if raised:
                    if hook is not None:
                        hook(raised,
                             func.__name__,
                             attempt,
                             backoff_interval)
                    time.sleep(backoff_interval)
                    backoff_interval = backoff_interval * backoff
                try:
                    return func(*args, **kwargs)
                except exceptions as error:
                    raised = error
                else:
                    raised = None
                    break

                attempt -= 1

            if raised:
                raise exception_to_raise(raised=raised)

        return decorated_func

    return dec


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

    def unregister(self, signal, callback):
        """Unregister a callback to a singal

        Arguments:
            signal (str): The name of the signal
            callbacl (obj): A callable object to call for the given signal.
        """
        try:
            self.handlers[signal].remove(callback)
        except ValueError:
            log.debug('tried to unregister %s from unknown %s signal',
                      callback, signal)

    def signal(self, signal, **kwargs):
        """Run registered handlers

        Arguments:
            signal (str): A registered signal
        """
        if signal in self.handlers:
            for handler in self.handlers.get(signal):
                handler(**kwargs)


class GraphiteHandler():
    """A handler to send data to graphite.

    Arguments:
        server (str): Server name or IP address.
        port (int): Port to connect to
        retries (int): Numbers to retry on connection failure
        interval (float): Time to sleep between retries
        timeout (float): Timeout on connection
        delay (float): Time to delay a connection attempt after last failure
        backoff (float): Multiply interval by this factor after each failure
        queue_size (int): Maximum size of the queue
        """
    def __init__(self,
                 server,
                 port=3002,
                 retries=1,
                 interval=2,
                 timeout=10,
                 delay=4,
                 backoff=2,
                 queue_size=1000000):
        self.server = server
        self.port = port
        self.retries = retries
        self.interval = interval
        self.timeout = timeout
        self.delay = delay
        self.backoff = backoff
        self.queue_size = queue_size
        self.dqueue = deque([], maxlen=self.queue_size)
        self.connection = None
        self.timer = None
        self.failures = 1
        self.exceptions = (ConnectionResetError, ConnectionRefusedError,
                           ConnectionAbortedError, BrokenPipeError, OSError,
                           socket.timeout)

    def open(self):
        """Open a connection to graphite relay."""
        try:
            self.connect()
        except BrokenConnection as error:
            log.error('failed to connect to %s on port %s: %s', self.server,
                      self.port, error.raised)
        else:
            self.connection.settimeout(self.timeout)
            log.info('successfully connected to %s on port %s', self.server,
                     self.port)

    @property
    def connect(self):
        """A convenient wrapper so we can pass arguments to decorator"""
        @retries(retries=self.retries, interval=self.interval,
                 backoff=self.backoff, exceptions=self.exceptions,
                 exception_to_raise=BrokenConnection)
        def _create_connection():
            """Try to open a connection.

            Exceptions are caught by the decorator which implements the retry
            logic.
            """
            log.info('connecting to %s on port %s', self.server, self.port)
            self.connection =\
                socket.create_connection((self.server, self.port),
                                         timeout=self.timeout)

        return _create_connection

    def send(self, **kwargs):
        """Send data to graphite relay"""
        self.dqueue.appendleft(kwargs.get('data'))

        while len(self.dqueue) != 0:
            item = self.dqueue.popleft()
            try:
                self.connection.sendall(bytes(item, 'utf-8'))
            # AttributeError means that open() method failed, all other
            # exceptions indicate that connection died.
            except (AttributeError, BrokenPipeError, ConnectionResetError,
                    ConnectionAbortedError, ConnectionAbortedError) as exc:
                self.dqueue.appendleft(item)
                # Only try to connect again if some time has passed
                if self.timer is None:  # It's 1st failure
                    self.timer = time.time()
                elif time.time() - self.timer > self.delay:
                    log.error('caught %s while sending data to graphite', exc)
                    log.warning('%s secs passed since last connection failure '
                                'trying to connect graphite', self.delay)
                    self.timer = time.time()
                    self.open()
                return
            except socket.timeout:
                # Don't leak FDs
                self.close()
                self.open()
                return
            else:
                # Consume all items from the local deque before return to
                # the caller. This causes a small delay to the caller at the
                # benefit of flushing data as soon as possible which avoids
                # gaps in graphs.
                continue

    def close(self, **kwargs):
        """Close TCP connection to graphite relay"""
        log.info('closing connection to %s on port %s', self.server, self.port)
        try:
            self.connection.close()
        except (ConnectionRefusedError, ConnectionResetError,
                ConnectionAbortedError) as exc:
            log.warning('closing connection failed: %s', exc)
        else:
            log.info('successfully closed connection to %s on port %s',
                     self.server, self.port)


dispatcher = Dispatcher()  # pylint: disable=I0011,C0103


class FileHandler():
    """A handler to write data to a file"""
    def __init__(self):
        self._input = None
        self._output = None

    def open(self):
        self._input = io.StringIO()

    def send(self, **kwargs):
        """Write data to a file-like object"""
        self._input.write(kwargs.get('data'))

    def set_path(self, filepath):
        """Set the filepath to send data.

        Arguments:
            filepath (str): The pathname of the file
        """
        log.debug('filepath for local-store set to %s', filepath)
        try:
            self._output = open(filepath, 'w')
        except (OSError, PermissionError) as error:
            log.error('failed to create %s: %s', filepath, error)

    def loop(self, **kwargs):
        """Rotate the file"""
        base_dir = os.path.join(kwargs.get('local_store'),
                                kwargs.get('epoch_time'))
        try:
            os.makedirs(base_dir)
        except (OSError, PermissionError) as error:
            # errno 17 => file exists
            if error.errno != 17:
                log.error('failed to make directory %s: %s', base_dir, error)
        self.set_path(filepath=os.path.join(base_dir, 'stats'))

    def flush(self, **kwargs):
        """Flush data to disk"""
        self._input.seek(0)
        try:
            shutil.copyfileobj(self._input, self._output)
            self._output.flush()
            self._output.close()
        except (OSError, PermissionError, AttributeError) as error:
            log.error('failed to flush data to file: %s', error)
            self._input.close()

        self.open()


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
