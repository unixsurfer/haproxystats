#! /bin/bash
VERBOSE_ARG=

while getopts ":v" opt; do
  case $opt in
    v)
       VERBOSE_ARG=" -vv"
       ;;
  esac
done
if [[ -x /opt/blue-python/3.4/bin/haproxystats-process && -r /etc/haproxystats.conf ]]; then
    WORKERS=$(/opt/blue-python/3.4/bin/haproxystats-process -f /etc/haproxystats.conf -P|grep workers |awk '{print $3}')
    if [ $? -ne 0 ]; then
        echo "OK: haproxystats-process doesn't run here"
        exit 0
    fi
    PROCESSES=$(($WORKERS+1))
    msg=$(/usr/lib64/nagios/plugins/check_procs\
        -c "${PROCESSES}":"${PROCESSES}"\
        --ereg-argument-array='^/usr/local/bin/blue-python3.4 /opt/blue-python/3.4/bin/haproxystats-process -f /etc/haproxystats.conf$'\
        -u haproxystats\
        $VERBOSE_ARG)
    EXITCODE=$?
    if [[ ${EXITCODE} -ne 0 ]]; then
        echo "${msg}" "Number of processes must be ${PROCESSES} OPDOC: TBD"
    else
        echo "${msg}"
    fi
    exit ${EXITCODE}
else
    echo "OK: haproxystats-process isn't installed here"
    exit 0
fi
