.. README.rst

============
haproxystats
============

    *A HAProxy statistics collection program*

.. contents::

Introduction
------------

**haproxystats** is a statistics collector for `HAProxy`_ load balancer which
processes various statistics and pushes them to graphing systems (Graphite).
It is designed to satisfy the following requirements:

#. Fast and configurable processing of HAProxy statistics
#. Support of multiprocess mode of HAProxy (nbproc > 1)
#. Pull statistics at very low intervals (10secs)
#. Flexible dispatching of statistics to different systems (Graphite,  kafka)

The main design characteristic is the split between pulling the statistics and
processing them. This provides the ability to pull data as frequently
as possible without worrying about the impact on processing time. It also
reduces the risk of losing data in case of trouble during the processing phase.
It runs locally on each load balancer node, offering a decentralized setup for
the processing phase, but it can be easily extended in the future to have a
centralized setup for the processing phase. In that centralized setup it will
be possible to perform aggregation as well. Until then users can deploy
`carbon-c-relay`_ for aggregation.

Because of this design haproxystats comes with two programs:
**haproxystats-pull** and **haproxystats-process**. The former pulls
statistics from HAProxy via `stats socket`_ and it uses the `asyncio`_ framework
from Python to achieve high concurrency and low footprint. The latter
processes the statistics and pushes them to various destinations. It utilizes
`Pandas`_ for data analysis and the multiprocess framework from Python.

haproxystats requires Python 3.4, docopt and Pandas to be available in the
system.

How haproxystats works
----------------------


.. image:: haproxystats-architecture.png


haproxystats-pull sends `info`_ and `stat`_ commands to all haproxy processes
in order to collect statistics for the daemon and for all
frontends/backends/servers. Data returned from each process and for each
command is stored in individual files which are saved under one directory. The
time (seconds since the epoch) of retrieval is used to name that directory.
haproxystats-process watches for changes on the parent directory and when a
directory is created it adds its full path to the queue. Multiple workers pick
up items from the queue (directories) and process statistics from those
directories.

haproxystats-pull
#################

haproxystats-pull leverages the asyncio framework from Python by utilizing
coroutines to multiplex I/O access over several `stats socket`_, which are
simple UNIX sockets. The actual task of storing the data to the file system is
off-loaded to a very light `pool of threads`_ in order to avoid blocking the
coroutines during the disk IO phase.

haproxystats-pull manages the *incoming* directory and makes sure directories
are created with correct names. It also suspends the collection when the number
of directories under the *incoming* directory exceeds a threshold. This avoids
filling up the disk when haproxystats-process is unavailable for sometime.
This an example of directory structure:

.. code-block:: bash

    incoming
    ├── 1457298067
    │   ├── admin1.sock_info
    │   ├── admin1.sock_stat
    │   ├── admin2.sock_info
    │   ├── admin2.sock_stat
    │   ├── admin3.sock_info
    │   ├── admin3.sock_stat
    │   ├── admin4.sock_info
    │   └── admin4.sock_stat
    └── 1457298072
        ├── admin1.sock_info
        ├── admin1.sock_stat
        ├── admin2.sock_info
        ├── admin2.sock_stat
        ├── admin3.sock_info
        ├── admin3.sock_stat
        ├── admin4.sock_info
        └── admin4.sock_stat

haproxystats-process
####################

haproxystats-process is a multiprocess program.The parent process uses the
the Linux kernel's `inotify`_ API to watch for changes in *incoming* directory.
It receives an event when a directory is either created or moved in *incoming*
directory. The event contains the absolute path name of that directory. It
maintains an internal queue in which it puts directory names. Multiple child
processes pick directory names from the queue and process the data.
Its worker dispatches statistics to various destinations. The directories are
removed from *incoming* directory when all statistics are successfully
processed. When haproxystats-process starts it scans the *incoming* directory
for new directories and processes them instantly, so you don't lose statistics
if haproxystats-process is unavailable for sometime.

Dispatchers
###########

haproxystats-process currently supports 2 different dispatchers.

1. **Graphite**

Pushes statistics to a Graphite system via a local or remote carbon-relay.
The recommended method is to use `carbon-c-relay`_. It is very fast and capable
of handling millions of metrics per second. This dispatcher utilizes an internal
queue to store metrics which are failed to be sent to Graphite.

An example of graphite namespace::

    <loadbalancers>.<lb-01>.haproxy.frontend.<frontendname>.
    <loadbalancers>.<lb-01>.haproxy.backend.<backendname>.
    <loadbalancers>.<lb-01>.haproxy.backend.<backendname>.server.<servername>
    <loadbalancers>.<lb-01>.haproxy.server.<servername>.
    <loadbalancers>.<lb-01>.haproxy.daemon.

2. **local-store**

Stores statistics in the local disk. Use it only for debugging purposes.

Queuing system
##############

The *incoming* directory together with the inotify API provides a simple queueing
system which is used as a communication channel between haproxystats-pull
and haproxystats-process programs. There isn't any feedback mechanism in place,
thus haproxystats-pull monitors the number of directories before it pulls
data from HAProxy and suspends its job when the number of directories exceeds
a threshold.

Configuration
-------------

haproxystats uses the popular `INI`_ format for its configuration file.
This is an example configuration file (/etc/haproxystats.conf)::


    [DEFAULT]
    loglevel = info
    retries  = 2
    timeout  = 1
    interval = 2

    [paths]
    base-dir = /var/lib/haproxystats

    [pull]
    loglevel        = info
    socket-dir      = /run/haproxy
    retries         = 1
    timeout         = 0.1
    interval        = 0.5
    pull-timeout    = 2
    pull-interval   = 10
    dst-dir         = ${paths:base-dir}/incoming
    tmp-dst-dir     = ${paths:base-dir}/incoming.tmp
    workers         = 8

    [process]
    src-dir         = ${paths:base-dir}/incoming
    workers         = 4

    [graphite]
    server          = 127.0.0.1
    port            = 3002
    retries         = 3
    interval        = 1.8
    delay           = 10
    backoff         = 2
    namespace       = loadbalancers
    prefix_hostname = true
    fqdn            = true
    queue-size      = 1000000

    #[local-store]
    #dir = ${paths:base-dir}/local-store

All the above settings are optional as haproxystats comes with default values
for all of them.

DEFAULT section
###############

Settings in this section can be overwritten in other sections.

* **loglevel** Defaults to **info**

Log level to use, possible values are: debug, info, warning, error, critical

* **retries** Defaults to **2**

Number of times to retry a connection after a failure. Used by haproxystats-pull
and haproxystats-process when they open a connection to a UNIX socket and
Graphite respectively.

* **timeout** Defaults to **1** (seconds)

Time to wait for establishing a connection. Used by haproxystats-pull and
haproxystats-process when they open a connection to a UNIX socket and Graphite
respectively.

* **interval** Defaults to **2**

Time to wait before trying to open a connection. Used by haproxystats-pull and
haproxystats-process when they retry a connection to a UNIX socket and Graphite
respectively.

paths section
#############

* **base-dir** Defaults to **/var/lib/haproxystats**

The directory to use as the base of the directory structure.

pull section
############

* **socket-dir** Defaults to **/run/haproxy**

A directory with HAProxy socket files.

* **retries** Defaults to **1**

Number of times to reconnect to UNIX socket after a failure.

* **timeout** Defaults to **0.1** (seconds)

Time to wait for establishing a connection to UNIX socket. There is no need to
set it higher than few ms as haproxy accepts a connection within 1-2ms.

* **interval** Defaults to **0.5** (seconds)

Time to wait before trying to reconnect to UNIX socket after a failure. Tune it
based on the duration of the reload process of haproxy. haproxy reloads within
few ms but in some environments with hundreds different SSL certificates it can
take a bit more.

* **pull-interval** Defaults to **10** (seconds)

How often to pull statistics from HAProxy. A value of *1* second can overload
the haproxy processes in environments with thousands backends/servers.

* **pull-timeout** Defaults to **2** (seconds)

Total time to wait for the pull process to finish. Should be always less than
**pull-interval**.

* **dst-dir** Defaults **/var/lib/haproxystats/incoming**

A directory to store statistics retrieved by HAProxy.

* **tmp-dst-dir** Defaults **/var/lib/haproxystats/incoming.tmp**

A directory to use as temporary storage location before directories are moved
to **dst-dir**.  haproxystats-pull stores statistics for each process under
that directory and only when data from all haproxy processes are successfully
retrieved they are moved to **dst-dir**. Make sure **dst-dir** and
**tmp-dst-dir** are on the same file system, so the move of the directories
become a rename which is a quick and atomic operation.

* **workers**  Defaults to **8**

Number of threads to use for writing statistics to disk. These are very
light threads and don't consume a lot of resources. Shouldn't be set higher
than the number of haproxy processes.

process section
###############

* **src-dir** Defaults **/var/lib/haproxystats/incoming**


A directory to watch for changes. It should point to the same directory as
the **dst-dir** setting from *pull* section.

* **workers** Defaults to **4**

Number of workers to use for processing statistics. These are real processes
which can consume a fair bit of CPU.

* **frontend-metrics** Unset by default

A list of frontend metric names separated by space to process. By default all
statistics are processed and this overwrites the default selection.

* **backend-metrics** Unset by default

A list of backend metric names separated by space to process. By default all
statistics are processed and this overwrites the default selection.

* **server-metrics** Unset by default

A list of server metric names separated by space to process. By default all
statistics are processed and this overwrites the default selection.

* **aggr-server-metrics** Defaults to **false**

Aggregates server's statistics across all backends.

graphite section
################

This dispatcher **is enabled** by default and it can't be disabled.

* **server** Defaults to **127.0.0.1**

Graphite server to connect to.

* **port**  Defaults to **3002**

Graphite port to connect to.

* **retries** Defaults to **3**

Number of times to reconnect to Graphite after a failure.

* **interval** Defaults to **1.8** (seconds)

Time to wait before trying to reconnect to Graphite after a failure.

* **delay** Defaults to **10** (seconds)

How long to wait before trying to connect again after number of retries has
exceeded the threshold set in **retries**. During the delay period metrics are
stored in the queue of the dispatcher, see **queue-size**.

* **backoff** Defaults to **2**

A simple exponential backoff to apply for each retry.

* **namespace** Defaults to **loadbalancers**

A top level graphite namespace.

* **prefix_hostname** Defaults to **true**

Insert the hostname of the load balancer in the Graphite namespace, example::

    loadbalancers.lb-01.haproxy.

* **fqdn** Defaults to **true**

Use FQDN or short name in the graphite namespace

* **queue-size**  Defaults to **1000000**

haproxystats-process uses a queue to store metrics which failed to be sent due
to a connection error/timeout. This is a First In First Out queueing system.
When the queue reaches the limit, the oldest items are removed to free space.

local-store section
###################

This dispatcher **isn't** enabled by default.

The primarily use of local-store dispatcher is to debug/troubleshoot possible
problems with the processing or/and with Graphite. There isn't any clean-up
process in place, thus you need remove the files after they are created.
Don't leave it enabled for more than 1 hour as it can easily fill up the disk
in environments with hundreds frontends/backends and thousands servers.

* **dir** Defaults to **/var/lib/haproxystats/local-store**

A directory to stores statistics after they have been processed. The correct
format is compatible with Graphite.

Systemd integration
-------------------

haproxystats-pull and haproxystats-process are simple programs which are not
daemonized and they output logging messages to stdout. This is by design as it
simplifies the code. The daemonenization and logging is off-loaded to systemd
which has everything we need for that job.

In the root directory of the project there are service files for both programs.
These are functional systemd Unit files which are used in production.

The order in which these 2 programs start doesn't matter and there isn't any
soft or hard dependency between them.

Furthermore, these programs don't need to run as root. It highly recommended to
create a dedicated user to run them. You need to add that user to the group of
*haproxy* and adjust socket configuration of haproxy to allow write for the
group, see below an example configuration::

    stats socket /run/haproxy/sock1 user haproxy group haproxy mode 660 level admin process 1
    stats socket /run/haproxy/sock2 user haproxy group haproxy mode 660 level admin process 2
    stats socket /run/haproxy/sock3 user haproxy group haproxy mode 660 level admin process 3

systemd Unit files use haproxystats user which has to be created prior running
haproxystats programs.

Graceful shutdown
-----------------

In an effort to reduce the loss of statistics both programs support graceful
shutdown. When *SIGHUP* or *SIGTERM* signals are sent they perform a clean exit.
When a signal is sent to haproxystats-process it may take some time for the
program to exit, as it waits for all workers to empty the queue.

Puppet module
-------------

A puppet module is available which provides classes for configuring both
programs. Because haproxystats-process is CPU bound program, CPU Affinity is
configured using systemd. By default it pins the workers to the last CPUs.
You should take care of pinning haproxy processes to other CPUs in order to
avoid haproxystats-process *stealing* CPU cycles from haproxy. In production
servers you usually pin the first 80% of CPUs to haproxy processes and you
leave the rest of CPUs for other processes. The default template of puppet
module enforces this logic.

haproxystats-pull is a single threaded program which doesn't use a lot of CPU
cycles and by default is assigned to the last CPU.

Nagios checks
-------------

Several nagios checks are provided for monitoring purposes, they can be found
under nagios directory at the root of the project.

* check_haproxystats_process_number_of_procs.sh

Monitor the number of processes of haproxystats-process program. Systemd
monitors only the parent process and this check helps to detect cases where
some worker(s) die unexpectedly

* check_haproxystats_process.sh

A wrapper around systemctl tool to detect a dead parent process.

* check_haproxystats_pull.sh

A wrapper around systemctl tool to a check if haproxystats-pull is running.

* check_haproxystats_queue_size.py

Checks the size of the *incoming* directory queue which is consumed by
haproxystats-process and alert when exceeds a threshold.

Monit check
-----------

If a child process of haproxystats-process dies then monit can restart
haproxystats-process. There is a monit check configuration available under
monit directory which does that.

Starting the programs
---------------------

::

    haproxystats-pull -f ./haproxystats.conf

::

    haproxystats-process -f ./haproxystats.conf

Usage::

    % haproxystats-pull -h
    Pulls statistics from HAProxy daemon over UNIX socket(s)

    Usage:
        haproxystats-pull [-f <file> ] [-p | -P]

    Options:
        -f, --file <file>  configuration file with settings
                           [default: /etc/haproxystats.conf]
        -p, --print        show default settings
        -P, --print-conf   show configuration
        -h, --help         show this screen
        -v, --version      show version


    % haproxystats-process -h
    Processes statistics from HAProxy and pushes them to Graphite

    Usage:
        haproxystats-process [-f <file> ] [-p | -P]

    Options:
        -f, --file <file>  configuration file with settings
                           [default: /etc/haproxystats.conf]
        -p, --print        show default settings
        -P, --print-conf   show configuration
        -h, --help         show this screen
        -v, --version      show version


Development
-----------
I would love to hear what other people think about **haproxystats** and provide
feedback. Please post your comments, bug reports and wishes on my `issues page
<https://github.com/unixsurfer/haproxystats/issues>`_.

Installation
------------

From Source::

   sudo python setup.py install

Build (source) RPMs::

   python setup.py clean --all; python setup.py bdist_rpm

Build a source archive for manual installation::

   python setup.py sdist


Release
-------

#. Bump version in haproxystats/__init__.py

#. Commit above change with::

      git commit -av -m'RELEASE 0.1.3 version'

#. Create a signed tag, pbr will use this for the version number::

      git tag -s 0.1.3 -m 'bump release'

#. Create the source distribution archive (the archive will be placed in the
   **dist** directory)::

      python setup.py sdist

#. pbr updates ChangeLog file and we want to squeeze this change to the
   previous commit, thus run::

      git commit -av --amend

#. Move current tag to the last commit::

      git tag -fs 0.1.3 -m 'bump release'

#. Push changes::

      git push;git push --tags


Contributers
------------

The following people have contributed to project with feedback and code reviews

- Károly Nagy https://github.com/charlesnagy

- Dan Achim https://github.com/danakim

Licensing
---------

Apache 2.0

Acknowledgement
---------------
This program was originally developed for Booking.com.  With approval
from Booking.com, the code was generalised and published as Open Source
on github, for which the author would like to express his gratitude.

Contacts
--------

**Project website**: https://github.com/unixsurfer/haproxystats

**Author**: Pavlos Parissis <pavlos.parissis@gmail.com>

.. _HAProxy: http://www.haproxy.org/
.. _stats socket: http://cbonte.github.io/haproxy-dconv/configuration-1.5.html#9.2
.. _carbon-c-relay: https://github.com/grobian/carbon-c-relay
.. _Pandas: http://pandas.pydata.org/
.. _asyncio: https://docs.python.org/3/library/asyncio.html
.. _inotify: http://linux.die.net/man/7/inotify
.. _stat: http://cbonte.github.io/haproxy-dconv/configuration-1.5.html#9.2-show%20stat
.. _info: http://cbonte.github.io/haproxy-dconv/configuration-1.5.html#9.2-show%20info
.. _pool of threads: https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.ThreadPoolExecutor
.. _INI: https://en.wikipedia.org/wiki/INI_file
.. _carbon-c-relay: https://github.com/grobian/carbon-c-relay
