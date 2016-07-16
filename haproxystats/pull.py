# -*- coding: utf-8 -*-
# vim:fenc=utf-8
# pylint: disable=no-member
# pylint: disable=too-many-statements
# pylint: disable=too-many-arguments
# pylint: disable=too-many-branches
# pylint: disable=too-many-locals
#
"""Pulls statistics from HAProxy daemon over UNIX socket(s)

Usage:
    haproxystats-pull [-f <file> ] [-p | -P]

Options:
    -f, --file <file>  configuration file with settings
                       [default: /etc/haproxystats.conf]
    -p, --print        show default settings
    -P, --print-conf   show configuration
    -h, --help         show this screen
    -v, --version      show version
"""
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor, ALL_COMPLETED
import sys
import time
import signal
import shutil
import logging
from functools import partial
from configparser import ConfigParser, ExtendedInterpolation, ParsingError
import copy
import glob
from docopt import docopt

from haproxystats import __version__ as VERSION
from haproxystats import DEFAULT_OPTIONS
from haproxystats.utils import (is_unix_socket, CMD_SUFFIX_MAP,
                                configuration_check)

LOG_FORMAT = ('%(asctime)s [%(process)d] [%(threadName)-10s:%(funcName)s] '
              '%(levelname)-8s %(message)s')
logging.basicConfig(format=LOG_FORMAT)
log = logging.getLogger('root')  # pylint: disable=I0011,C0103
CMDS = ['show info', 'show stat']


@asyncio.coroutine
def get(socket_file, cmd, storage_dir, loop, executor, config):
    """
    Fetch data from a UNIX socket.

    Sends a command to HAProxy over UNIX socket, reads the response and then
    offloads the writing of the received data to a thread, so we don't block
    this coroutine.

    Arguments:
        socket_file (str): The full path of the UNIX socket file to connect to.
        cmd (str): The command to send.
        storage_dir (str): The full path of the directory to save the response.
        loop (obj): A base event loop from asyncio module.
        executor (obj): A Threader executor to execute calls asynchronously.
        config (obj): A configParser object which holds configuration.

    Returns:
        True if statistics from a UNIX sockets are saved False otherwise.
    """
    # try to connect to the UNIX socket
    log.debug('connecting to UNIX socket %s', socket_file)
    retries = config.getint('pull', 'retries')
    timeout = config.getfloat('pull', 'timeout')
    interval = config.getfloat('pull', 'interval')
    attempt = 0  # times to attempt a connect after a failure
    raised = None

    if retries == -1:
        attempt = -1  # -1 means retry indefinitely
    elif retries == 0:
        attempt = 1  # Zero means don't retry
    else:
        attempt = retries + 1  # any other value means retry N times
    while attempt != 0:
        if raised:  # an exception was raised sleep before the next retry
            log.error('caught "%s" when connecting to UNIX socket %s, '
                      'remaining tries %s, sleeping for %.2f seconds',
                      raised, socket_file, attempt, interval)
            yield from asyncio.sleep(interval)
        try:
            connect = asyncio.open_unix_connection(socket_file)
            reader, writer = yield from asyncio.wait_for(connect, timeout)
        except (ConnectionRefusedError, PermissionError, asyncio.TimeoutError,
                OSError) as exc:
            raised = exc
        else:
            log.debug('connection established to UNIX socket %s', socket_file)
            raised = None
            break

        attempt -= 1

    if raised is not None:
        log.error('failed to connect to UNIX socket %s after %s retries',
                  socket_file, retries)
        return False
    else:
        log.debug('connection established to UNIX socket %s', socket_file)

    log.debug('sending command "%s" to UNIX socket %s', cmd, socket_file)
    writer.write('{c}\n'.format(c=cmd).encode())
    data = yield from reader.read()
    writer.close()

    if len(data) == 0:
        log.critical('received zero data')
        return False

    log.debug('received data from UNIX socket %s', socket_file)

    suffix = CMD_SUFFIX_MAP.get(cmd.split()[1])
    filename = os.path.basename(socket_file) + suffix
    filename = os.path.join(storage_dir, filename)
    log.debug('going to save data to %s', filename)
    # Offload the writing to a thread so we don't block ourselves.

    def write_file():
        """
        Write data to a file.

        Returns:
            True if succeeds False otherwise.
        """
        try:
            with open(filename, 'w') as file_handle:
                file_handle.write(data.decode())
        except OSError as exc:
            log.critical('failed to write data %s', exc)
            return False
        else:
            log.debug('data saved in %s', filename)
            return True

    result = yield from loop.run_in_executor(executor, write_file)

    return result


@asyncio.coroutine
def pull_stats(config, storage_dir, loop, executor):
    """
    Launch coroutines for pulling statistics from UNIX sockets.

    This a delegating routine.

    Arguments:
        config (obj): A configParser object which holds configuration.
        storage_dir (str): The absolute directory path to save the statistics.
        loop (obj): A base event loop.
        executor(obj): A ThreadPoolExecutor object.

    Returns:
        True if statistics from *all* UNIX sockets are fetched False otherwise.
    """
    # absolute directory path which contains UNIX socket files.
    results = []  # stores the result of finished tasks
    socket_dir = config.get('pull', 'socket-dir')
    pull_timeout = config.getfloat('pull', 'pull-timeout')
    if int(pull_timeout) == 0:
        pull_timeout = None

    socket_files = [f for f in glob.glob(socket_dir + '/*')
                    if is_unix_socket(f)]
    if not socket_files:
        log.error("found zero UNIX sockets under %s to connect to", socket_dir)
        return False

    log.debug('pull statistics')
    coroutines = [get(socket_file, cmd, storage_dir, loop, executor, config)
                  for socket_file in socket_files
                  for cmd in CMDS]
    # Launch all connections.
    done, pending = yield from asyncio.wait(coroutines,
                                            timeout=pull_timeout,
                                            return_when=ALL_COMPLETED)
    for task in done:
        log.debug('task status: %s', task)
        results.append(task.result())

    log.debug('task report, done:%s pending:%s succeed:%s failed:%s',
              len(done),
              len(pending),
              results.count(True),
              results.count(False))

    for task in pending:
        log.warning('cancelling task %s as it reached its timeout threshold of'
                    ' %.2f seconds', task, pull_timeout)
        task.cancel()

    # only when all tasks are finished successfully we claim success
    return not pending and len(set(results)) == 1 and True in set(results)


def supervisor(loop, config, executor):
    """
    Coordinate the pulling of HAProxy statistics from UNIX sockets.

    This is the client routine which launches requests to all HAProxy
    UNIX sockets for retrieving statistics and save them to file-system.
    It runs indefinitely until main program is terminated.

    Arguments:
        loop (obj): A base event loop from asyncio module.
        config (obj): A configParser object which holds configuration.
        executor(obj): A ThreadPoolExecutor object.
    """
    dst_dir = config.get('pull', 'dst-dir')
    tmp_dst_dir = config.get('pull', 'tmp-dst-dir')
    exit_code = 1

    interval = config.getint('pull', 'pull-interval')
    start_offset = time.time() % interval

    while True:
        timestamp = time.time()
        log.debug('entering while loop')
        try:
            queue = [x for x in os.listdir(dst_dir)
                     if os.path.isdir(os.path.join(dst_dir, x))]
        except FileNotFoundError as exc:
            log.warning('%s disappeared: %s. Going to create it', dst_dir, exc)
            try:
                os.makedirs(dst_dir)
            except OSError as exc:
                # errno 17 => file exists
                if exc.errno != 17:
                    sys.exit("failed to make directory {d}:{e}".format(
                        d=dst_dir, e=exc))
        else:
            if len(queue) >= config.getint('pull', 'queue-size'):
                log.warning("queue reached max size of %s, pulling statistics "
                            "is suspended", len(queue))
                # calculate sleep time
                sleep = start_offset - time.time() % interval
                if sleep < 0:
                    sleep += interval
                log.info('sleeping for %.3fs secs', sleep)
                time.sleep(sleep)
                continue
        # HAProxy statistics are stored in a directory and we use retrieval
        # time(seconds since the Epoch) as a name of the directory.
        # We first store them in a temporary place until we receive statistics
        # from all UNIX sockets.
        storage_dir = os.path.join(tmp_dst_dir, str(int(timestamp)))

        # Exit if our storage directory can't be created
        try:
            os.makedirs(storage_dir)
        except OSError as exc:
            # errno 17 => file exists
            if exc.errno == 17:
                old_data_files = glob.glob(storage_dir + '/*')
                for old_file in old_data_files:
                    log.info('removing old data file %s', old_file)
                    os.remove(old_file)
            else:
                msg = ("failed to make directory {d}:{e}"
                       .format(d=storage_dir,
                               e=exc))
                log.critical(msg)
                log.critical('a fatal error has occurred, exiting..')
                break

        try:
            log.debug('launching delegating coroutine')
            result = loop.run_until_complete(pull_stats(config, storage_dir,
                                                        loop, executor))
            log.debug('delegating coroutine finished')
        except asyncio.CancelledError:
            log.info('Received CancelledError exception')
            exit_code = 0
            break

        # if and only if we received statistics from all sockets then move
        # statistics to the permanent directory.
        # NOTE: when temporary and permanent storage directory are on the same
        # file-system the move is actual a rename, which is an atomic
        # operation.
        if result:
            log.debug('move %s to %s', storage_dir, dst_dir)
            try:
                shutil.move(storage_dir, dst_dir)
            except OSError as exc:
                log.critical("failed to move %s to %s: %s",
                             storage_dir,
                             dst_dir,
                             exc)
                log.critical('a fatal error has occurred, exiting..')
                break
            else:
                log.info('statistics are stored in %s',
                         os.path.join(dst_dir, os.path.basename(storage_dir)))
        else:
            log.critical('failed to pull stats')
            log.debug('removing temporary directory %s', storage_dir)
            try:
                shutil.rmtree(storage_dir)
            except (FileNotFoundError, PermissionError, OSError) as exc:
                log.error('failed to remove temporary directory %s with:%s',
                          storage_dir,
                          exc)

        log.info('wall clock time in seconds: %.3f', time.time() - timestamp)
        # calculate sleep time
        sleep = start_offset - time.time() % interval
        if sleep < 0:
            sleep += interval
        log.info('sleeping for %.3fs secs', sleep)
        time.sleep(sleep)

    # It is very unlikely that threads haven't finished their job by now, but
    # they perform disk IO operations which can take some time in certain
    # situations, thus we want to wait for them in order to perform a clean
    # shutdown.
    log.info('waiting for threads to finish any pending IO tasks')
    executor.shutdown(wait=True)
    log.info('closing asyncio event loop')
    loop.close()
    log.info('exiting with status %s', exit_code)
    sys.exit(exit_code)


def main():
    """Parse CLI arguments and launch main program"""
    args = docopt(__doc__, version=VERSION)

    config = ConfigParser(interpolation=ExtendedInterpolation())
    # Set defaults for all sections
    config.read_dict(copy.copy(DEFAULT_OPTIONS))
    # Load configuration from a file. NOTE: ConfigParser doesn't warn if user
    # sets a filename which doesn't exist, in this case defaults will be used.
    try:
        config.read(args['--file'])
    except ParsingError as exc:
        sys.exit(str(exc))

    if args['--print']:
        for section in sorted(DEFAULT_OPTIONS):
            if section == 'pull' or section == 'DEFAULT':
                print("[{}]".format(section))
                for key, value in sorted(DEFAULT_OPTIONS[section].items()):
                    print("{k} = {v}".format(k=key, v=value))
                print()
        sys.exit(0)
    if args['--print-conf']:
        for section in sorted(config):
            if section == 'pull' or section == 'DEFAULT':
                print("[{}]".format(section))
                for key, value in sorted(config[section].items()):
                    print("{k} = {v}".format(k=key, v=value))
                print()
        sys.exit(0)

    try:
        configuration_check(config, 'pull')
    except ValueError as exc:
        sys.exit(str(exc))

    loglevel = (config.get('pull', 'loglevel')
                .upper())  # pylint: disable=no-member
    log.setLevel(getattr(logging, loglevel, None))

    log.info('haproxystats-pull %s version started', VERSION)
    # Setup our event loop
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=config.getint('pull',
                                                            'workers'))
    # Register shutdown to signals

    def shutdown(signalname):
        """Performs a clean shutdown

        Arguments:
            signalname (str): Signal name
        """
        tasks_running = False
        log.info('received %s', signalname)

        for task in asyncio.Task.all_tasks():
            if not task.done():
                tasks_running = True
                log.info('cancelling %s task', task)
                task.cancel()

        if not tasks_running:
            log.info('no tasks were running when %s signal received', signal)
            log.info('waiting for threads to finish any pending IO tasks')
            executor.shutdown(wait=True)
            sys.exit(0)

    loop.add_signal_handler(signal.SIGHUP, partial(shutdown, 'SIGHUP'))
    loop.add_signal_handler(signal.SIGTERM, partial(shutdown, 'SIGTERM'))

    # a temporary directory to store fetched data
    tmp_dst_dir = config['pull']['tmp-dst-dir']
    # a permanent directory to move data from the temporary directory. Data are
    # picked up by the process daemon from that directory.
    dst_dir = config['pull']['dst-dir']
    for directory in dst_dir, tmp_dst_dir:
        try:
            os.makedirs(directory)
        except OSError as exc:
            # errno 17 => file exists
            if exc.errno != 17:
                sys.exit("failed to make directory {d}:{e}".format(d=directory,
                                                                   e=exc))
    supervisor(loop, config, executor)

# This is the standard boilerplate that calls the main() function.
if __name__ == '__main__':
    main()
