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
import re
import pyinotify
import pandas


log = logging.getLogger('root')  # pylint: disable=I0011,C0103

FILE_SUFFIX_INFO = '_info'
FILE_SUFFIX_STAT = '_stat'
CMD_SUFFIX_MAP = {'info': FILE_SUFFIX_INFO, 'stat': FILE_SUFFIX_STAT}


class BrokenConnection(Exception):
    """A wrapper of all possible exception during a TCP connect"""
    def __init__(self, raised):
        self.raised = raised


def load_file_content(filename):
    """Build list from the content of a file

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
        except (ValueError, OSError) as exc:
            log.error('Pandas failed to parse %s file with: %s', csv_file, exc)
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
    """A decorator which implements a retry logic.

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
        def decorated_func(*args, **kwargs):
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
                  self.connect_timeout, self.write_timeout)

    def open(self):
        """Open a connection to graphite relay."""
        try:
            self.connect()
        except BrokenConnection as error:
            self.connection = None
            log.error('failed to connect to %s on port %s: %s', self.server,
                      self.port, error.raised)
        else:
            self.connection.settimeout(self.write_timeout)
            log.info('successfully connected to %s on port %s, TCP info'
                     '%s', self.server, self.port, self.connection)

    @property
    def connect(self):
        """A convenient wrapper so we can pass arguments to decorator"""
        @retry_on_failures(retries=self.retries, interval=self.interval,
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

    def close(self, **kwargs):
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
                         ' this exception, it is a BUG', exc)
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
        """Build a stringIO object in memory read to be used."""
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
