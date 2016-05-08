# -*- coding: utf-8 -*-
# vim:fenc=utf-8
# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-arguments
"""
haproxstats.utils
~~~~~~~~~~~~~~~~~~

This module provides functions, constants and classes that are used within
haproxystats.
"""
import os
import stat
from collections import defaultdict, deque
from functools import wraps
import io
import socket
import shutil
import logging
import time
import configparser
import glob
import re
import pyinotify
import pandas

from haproxystats.metrics import MetricNamesPercentage

log = logging.getLogger('root')  # pylint: disable=I0011,C0103

FILE_SUFFIX_INFO = '_info'
FILE_SUFFIX_STAT = '_stat'
CMD_SUFFIX_MAP = {'info': FILE_SUFFIX_INFO, 'stat': FILE_SUFFIX_STAT}

OPTIONS_TYPE = {
    'paths': {
        'base-dir': 'get',
    },
    'pull': {
        'loglevel': 'get',
        'socket-dir': 'get',
        'retries': 'getint',
        'timeout': 'getfloat',
        'interval': 'getfloat',
        'pull-timeout': 'getfloat',
        'pull-interval': 'getint',
        'dst-dir': 'get',
        'tmp-dst-dir': 'get',
        'workers': 'getint',
        'queue-size': 'getint',
    },
    'process': {
        'workers': 'getint',
        'src-dir': 'get',
        'aggr-server-metrics': 'getboolean',
        'per-process-metrics': 'getboolean',
        'calculate-percentages': 'getboolean',
    },
    'graphite': {
        'server': 'get',
        'port': 'getint',
        'retries': 'getint',
        'interval': 'getfloat',
        'connect-timeout': 'getfloat',
        'write-timeout': 'getfloat',
        'delay': 'getfloat',
        'backoff': 'getfloat',
        'namespace': 'get',
        'prefix-hostname': 'getboolean',
        'fqdn': 'getboolean',
        'queue-size': 'getint',
    },
    'local-store': {
        'dir': 'get',
    },
}


class BrokenConnection(Exception):
    """
    A wrapper of all possible exception during a TCP connect
    """
    def __init__(self, raised):
        self.raised = raised

        super().__init__()


def load_file_content(filename):
    """
    Build list from the content of a file

    Arguments:
        filename (str): A absolute path of a filename

    Returns:
        A list
    """
    commented = re.compile(r'\s*?#')
    try:
        with open(filename, 'r') as _file:
            _content = [line.strip() for line in _file.read().splitlines()
                        if not commented.match(line)]
    except OSError as exc:
        log.error('failed to read %s:%s', filename, exc)
        return []
    else:
        return _content


def is_unix_socket(path):
    """
    Check if path is a valid UNIX socket.

    Arguments:
        path (str): A file name path

    Returns:
        True if path is a valid UNIX socket otherwise False.
    """
    mode = os.stat(path).st_mode

    return stat.S_ISSOCK(mode)


def concat_csv(csv_files):
    """
    Perform a concatenation along several csv files.

    Arguments:
        csv_files (lst): A list of csv files.

    Returns:
        A pandas data frame object or None if fails to parse csv_files
    """
    data_frames = []
    for csv_file in csv_files:
        try:
            data_frame = pandas.read_csv(csv_file, low_memory=False)
        except (ValueError, OSError) as exc:
            log.error('Pandas failed to parse %s file with: %s', csv_file, exc)
        else:
            data_frames.append(data_frame)
    if data_frames:
        return pandas.concat(data_frames)
    else:
        return None


def get_files(path, suffix):
    """
    Return the filenames from a directory which match a suffix

    Arguments:
        path (str): Pathname
        suffix (str): Suffix to match against

    Returns:
        A list of filenames
    """
    files = [filename
             for filename in glob.glob(path + '/*{s}'.format(s=suffix))]

    return files


def retry_on_failures(retries=3,
                      interval=0.9,
                      backoff=3,
                      exceptions=(ConnectionResetError, ConnectionRefusedError,
                                  ConnectionAbortedError, BrokenPipeError,
                                  OSError),
                      exception_to_raise=BrokenConnection):
    """
    A decorator which implements a retry logic.

    Arguments:
        retries (int): Maximum times to retry
        interval (float): Sleep this many seconds between retries
        backoff (int): Multiply interval by this factor after each failure
        exceptions (tuple): A list of exceptions to catch
        exception_to_raise (obj): An exception to raise when maximum tries
            have been reached.

    The decorator calls the function up to retries times if it raises an
    exception from the tuple. The decorated function will only be retried if
    it raises one of the specified exceptions.
    """
    def dec(func):
        """
        The real decorator.

        Arguments:
            func (obj): A function to decorate
        """
        def decorated_func(*args, **kwargs):
            """
            Retry decorated functions.
            """
            backoff_interval = interval
            raised = None
            attempt = 0  # times to attempt a connect after a failure
            if retries == -1:  # -1 means retry indefinitely
                attempt = -1
            elif retries == 0:  # Zero means don't retry
                attempt = 1
            else:  # any other value means retry N times
                attempt = retries + 1
            while attempt != 0:
                if raised:
                    log.error('caught "%s" at "%s", remaining tries %s, '
                              'sleeping for %.2f seconds', raised,
                              func.__name__, attempt, backoff_interval)
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
    """
    Dispatch data to different handlers
    """
    def __init__(self):
        self.handlers = defaultdict(list)

    def register(self, signal, callback):
        """
        Register a callback to a signal

        Multiple callbacks can be assigned to the same signal.

        Arguments:
            signal (str): The name of the signal
            callbacl (obj): A callable object to call for the given signal.
       """
        self.handlers[signal].append(callback)

    def unregister(self, signal, callback):
        """
        Unregister a callback to a signal

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
        """
        Run registered handlers

        Arguments:
            signal (str): A registered signal
        """
        if signal in self.handlers:
            for handler in self.handlers.get(signal):
                handler(**kwargs)


class GraphiteHandler():
    """
    A handler to send data to graphite.

    Arguments:
        server (str): Server name or IP address.
        port (int): Port to connect to
        retries (int): Numbers to retry on connection failure
        interval (float): Time to sleep between retries
        connect_timeout (float): Timeout on connection
        write_timeout (float): Timeout on sending data
        delay (float): Time to delay a connection attempt after last failure
        backoff (float): Multiply interval by this factor after each failure
        queue_size (int): Maximum size of the queue
        """
    def __init__(self,
                 server,
                 port=3002,
                 retries=1,
                 interval=2,
                 connect_timeout=1,
                 write_timeout=1,
                 delay=4,
                 backoff=2,
                 queue_size=1000000):
        self.server = server
        self.port = port
        self.retries = retries
        self.interval = interval
        self.connect_timeout = connect_timeout
        self.write_timeout = write_timeout
        self.delay = delay
        self.backoff = backoff
        self.queue_size = queue_size
        self.dqueue = deque([], maxlen=self.queue_size)
        self.connection = None
        self.timer = None
        self.exceptions = (ConnectionResetError, ConnectionRefusedError,
                           ConnectionAbortedError, BrokenPipeError, OSError,
                           socket.timeout)

        log.debug('connect timeout %.2fsecs write timeout %.2fsecs',
                  self.connect_timeout,
                  self.write_timeout)

    def open(self):
        """Open a connection to graphite relay."""
        try:
            self.connect()
        except BrokenConnection as error:
            self.connection = None
            log.error('failed to connect to %s on port %s: %s',
                      self.server,
                      self.port,
                      error.raised)
        else:
            self.connection.settimeout(self.write_timeout)
            log.info('successfully connected to %s on port %s, TCP info %s',
                     self.server,
                     self.port,
                     self.connection)

    @property
    def connect(self):
        """A convenient wrapper so we can pass arguments to decorator"""
        @retry_on_failures(retries=self.retries,
                           interval=self.interval,
                           backoff=self.backoff,
                           exceptions=self.exceptions,
                           exception_to_raise=BrokenConnection)
        def _create_connection():
            """Try to open a connection.

            Exceptions are caught by the decorator which implements the retry
            logic.
            """
            log.info('connecting to %s on port %s', self.server, self.port)
            self.connection = socket.create_connection(
                (self.server, self.port),
                timeout=self.connect_timeout)

        return _create_connection

    def send(self, **kwargs):
        """Send data to graphite relay"""
        self.dqueue.appendleft(kwargs.get('data'))

        while len(self.dqueue) != 0:
            item = self.dqueue.popleft()
            try:
                self.connection.sendall(bytes(item, 'utf-8'))
            # AttributeError means that open() method failed, all other
            # exceptions indicate connection problems
            except (AttributeError, BrokenPipeError, ConnectionResetError,
                    ConnectionAbortedError, socket.timeout) as exc:
                self.dqueue.appendleft(item)
                # Only try to connect again if some time has passed
                if self.timer is None:
                    self.timer = time.time()
                    log.warning('graphite connection problem is detected')
                    log.debug('timer is set to:%s', self.timer)
                elif time.time() - self.timer > self.delay:
                    log.error('caught %s while sending data to graphite', exc)
                    log.warning('%s secs since last failure', self.delay)
                    log.info('TCP info: %s', self.connection)
                    self.timer = None
                    if not isinstance(exc, AttributeError):
                        self.close()
                    else:
                        log.warning('connection is not available')
                    self.open()
                return
            except OSError as exc:
                # Unclear under which conditions we may get OSError
                log.warning('caught %s while sending data to graphite', exc)
                log.info('TCP info: %s', self.connection)
                self.close()
                self.open()
                return
            else:
                # Consume all items from the local deque before return to
                # the caller. This causes a small delay to the caller at the
                # benefit of flushing data as soon as possible which avoids
                # gaps in graphs.
                continue

    def close(self, **kwargs):  # pylint: disable=unused-argument
        """Close TCP connection to graphite relay"""
        log.info('closing connection to %s on port %s', self.server, self.port)
        log.info('TCP info: %s', self.connection)
        try:
            self.connection.close()
        except (ConnectionRefusedError, ConnectionResetError, socket.timeout,
                ConnectionAbortedError) as exc:
            log.warning('closing connection failed: %s', exc)
        except (AttributeError, OSError) as exc:
            log.critical('closing connection failed: %s. We should not receive'
                         ' this exception, it is a BUG',
                         exc)
        else:
            log.info('successfully closed connection to %s on port %s',
                     self.server,
                     self.port)


dispatcher = Dispatcher()  # pylint: disable=I0011,C0103


class FileHandler():
    """
    A handler to write data to a file
    """
    def __init__(self):
        self._input = None
        self._output = None

    def open(self):
        """Build a stringIO object in memory ready to be used."""
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
                                kwargs.get('timestamp'))
        try:
            os.makedirs(base_dir)
        except (OSError, PermissionError) as error:
            # errno 17 => file exists
            if error.errno != 17:
                log.error('failed to make directory %s: %s', base_dir, error)
        self.set_path(filepath=os.path.join(base_dir, 'stats'))

    def flush(self, **kwargs):  # pylint: disable=unused-argument
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
    """
    An event handler for inotify to push items to a queue.

    If the event isn't for a directory no action is taken.

    Arguments:
        tasks (queue obj): A queue to put items.
    """
    def my_init(self, tasks):  # pylint: disable=arguments-differ
        self.tasks = tasks

    def _put_item_to_queue(self, pathname):
        """Add item to queue if and only if the pathname is a directory"""
        if os.path.isdir(pathname):
            log.info('putting %s in queue', pathname)
            self.tasks.put(pathname)
        else:
            log.info("ignore %s as it isn't directory", pathname)

    def process_IN_CREATE(self, event):  # pylint: disable=C0103
        """Invoked when a directory is created"""
        log.debug('received an event for CREATE')
        self._put_item_to_queue(event.pathname)

    def process_IN_MOVED_TO(self, event):  # pylint: disable=C0103
        """Invoked when a directory/file is moved"""
        log.debug('received an event for MOVE')
        self._put_item_to_queue(event.pathname)


def configuration_check(config, section):
    """
    Perform a sanity check on configuration

    Arguments:
        config (obg): A configparser object which holds our configuration.
        section (str): Section name

    Raises:
        ValueError on the first occureance of invalid configuration

    Returns:
        None if all checks are successful.
    """
    loglevel = config[section]['loglevel']
    num_level = getattr(logging, loglevel.upper(), None)
    if not isinstance(num_level, int):
        raise ValueError("invalid configuration, section:'{s}' option:'{o}' "
                         "error: invalid loglevel '{l}'"
                         .format(s=section,
                                 o='loglevel',
                                 l=loglevel))

    for option, getter in OPTIONS_TYPE[section].items():
        try:
            getattr(config, getter)(section, option)
        except (configparser.Error, ValueError) as exc:
            # For some errors ConfigParser mention section/option names and
            # for others not. We want for all possible errors to mention
            # section andoption names in order to make the life of our user
            # easier.
            if 'section' not in str(exc):
                raise ValueError("invalid configuration, section:'{s}' "
                                 "option:'{p}' error:{e}"
                                 .format(s=section,
                                         p=option,
                                         e=str(exc)))
            else:
                raise ValueError("invalid configuration, error:{e}"
                                 .format(e=str(exc)))


def check_metrics(config):
    """
    Check if metrics set by user are valid

    Arguments:
        config (obg): A configparser object which holds our configuration.

    Raises:
        ValueError when metrics are not valid

    Returns:
        None if all checks are successful.
    """
    for metric_type in ['server', 'frontend', 'backend']:
        option = '{t}-metrics'.format(t=metric_type)
        user_metrics = config.get('process', option, fallback=None)
        if user_metrics is not None:
            metrics = set(user_metrics.split(' '))
            if not metrics:
                break
            valid_metrics =\
                globals().get('{}_METRICS'.format(metric_type.upper()))
            if not set(valid_metrics).issuperset(metrics):
                raise ValueError("invalid configuration, section:'{s}' "
                                 "option:'{p}' error:'{e}'"
                                 .format(s='process',
                                         p=option,
                                         e='invalid list of metrics'))


def read_write_access(directory):
    """
    Check if read/write access is granted on a directory

    Arguments:
        directory (str): Directory name

    Raises:
        OSError if either read or write access isn't granted

    Returns:
        None if read/write access is granted
    """
    check_file = os.path.join(directory, '.read_write_check')
    try:
        with open(check_file, 'w') as _file:
            _file.write('')
    except OSError as exc:
        raise ValueError("invalid configuration, read and write access is not "
                         "granted for '{d}' directory, error:{e}"
                         .format(d=directory,
                                 e=str(exc)))
    else:
        os.remove(check_file)


def daemon_percentage_metrics():
    """
    Build a list of namedtuples which holds metric names for HAProxy
    daemon for which we calculate a percentage.
    """
    _list = []
    _list.append(MetricNamesPercentage(name='CurrConns',
                                       limit='Maxconn',
                                       title='ConnPercentage'))
    _list.append(MetricNamesPercentage(name='ConnRate',
                                       limit='ConnRateLimit',
                                       title='ConnRatePercentage'))
    _list.append(MetricNamesPercentage(name='CurrSslConns',
                                       limit='MaxSslConns',
                                       title='SslConnPercentage'))
    _list.append(MetricNamesPercentage(name='SslRate',
                                       limit='SslRateLimit',
                                       title='SslRatePercentage'))

    return _list


def calculate_percentage_per_row(row, metric):
    """
    Calculate the percentage per row for 2 columns

    It selects per row 2 columns, metric.name and metric.limit, out of the
    dataframe and then calculate the percentage.

    Example where metric.name is 'CurrConns' and metric.limit is 'MaxConn'.
    +-------------+---------+-----------+
    |             | MaxConn | CurrConns |
    +-------------+---------+-----------+
    | Process_num |         |           |
    | 0           | 300     | 13        |
    | 1           | 300     | 15        |
    | 2           | 300     | 11        |
    +-------------+---------+-----------+

    It returns a Pandas Series with a column name set to metric.title
    +-------------+----------------+
    |             | ConnPercentage |
    +-------------+----------------+
    | Process_num |                |
    | 0           | 13             |
    | 1           | 15             |
    | 2           | 11             |
    +-------------+----------------+

    Arguments:

        dataframe (obj): Pandas dataframe with statistics for HAProxy workers
        metric (tuple): A namedtuple of MetricNamesPercentage

    Returns:
        A Pandas Series with percentage as integer
    """
    if row[metric.limit] == 0:
        return pandas.Series({metric.title: 0})

    return pandas.Series(
        {
            metric.title: (100 * row[metric.name]
                           / row[metric.limit]).astype('int')
        }
    )


def calculate_percentage_per_column(dataframe, metric):
    """
    Calculate the percentage against 2 Pandas Series

    It selects 2 columns, metric.name and metric.limit, out of the dataframe,
    sums the values per column and then calculate the percentage.

    Example where metric.name is 'CurrConns' and metric.limit is 'MaxConn'.
    It calculates the sum per column and the retuns the percentage of CurrConns
    as part of Maxconn.
    +---+---------+-----------+
    |   | MaxConn | CurrConns |
    +---+---------+-----------+
    | 0 | 300     | 13        |
    | 1 | 300     | 15        |
    | 2 | 300     | 11        |
    +---+---------+-----------+

    Arguments:

        dataframe (obj): Pandas dataframe with statistics for HAProxy workers
        metric (tuple): A namedtuple of MetricNamesPercentage


    Returns:
        A percentage as integer
    """
    _sum = dataframe.loc[:, [metric.name]].sum()[0]
    _sum_limit = dataframe.loc[:, [metric.limit]].sum()[0]
    if _sum_limit == 0:
        return 0
    else:
        return int(100 * _sum / _sum_limit)


def send_wlc(output, name):
    """
    A decorator to send to graphite the wall clock time of a method

    The decorated method must have the following attributes:
        graphite_path (str): The graphite path to use for storing the metric
        timestamp (int): Time to credit the wallclock time

    Arguments:
        output (obj): A dispatcher object which has send method registered
        name (str): A name to append to the metric.
    """
    def decorated(func):
        """
        The real decorator.

        Arguments:
            func (obj): A function to decorate
        """
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            """
            Time the execution of decorated function
            """
            start_time = time.time()
            result = func(self, *args, **kwargs)
            elapsed_time = '{t:.3f}'.format(t=time.time() - start_time)
            data = ("{p}.haproxystats.{m} {v} {t}\n"
                    .format(p=getattr(self, 'graphite_path'),
                            m='WallClockTime' + name,
                            v=elapsed_time,
                            t=getattr(self, 'timestamp')))
            log.info("wall clock time in seconds for %s %s",
                     func.__name__,
                     elapsed_time)
            output.signal('send', data=data)

            return result

        return wrapper

    return decorated
