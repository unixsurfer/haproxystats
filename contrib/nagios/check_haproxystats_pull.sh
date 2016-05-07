#! /bin/bash
osversion=$(/usr/local/bin/facter operatingsystemmajrelease)
if [ "${osversion}" -lt 7 ]; then
    echo "OK: haproxystats-pull doesn't run here as it only runs on CentOS version 7 and higher"
    exit 0
fi
message=$(systemctl is-active haproxystats-pull.service)
if [[ $? -ne 0 ]]; then
    echo "CRITICAL:" "${message}"
    exit 2
else
    echo "OK:" "${message}"
    exit 0
fi
