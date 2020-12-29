FROM alpine:3.12

# ---------------- #
#   Installation   #
# ---------------- #

# Install and setup all prerequisites
RUN apk add --no-cache gcc g++ python3 py3-pip python3-dev supervisor                                              &&\
    wget -c -O /requirements.txt https://raw.githubusercontent.com/unixsurfer/haproxystats/master/requirements.txt &&\
    pip3 install --requirement /requirements.txt                                                                   &&\
    pip3 install haproxystats                                                                                      &&\
    mkdir -p  /etc/haproxystats  /var/lib/haproxy  /var/log/supervisor                                             &&\
    rm -rf /var/cache/apk/*                                                                                        &&\
    rm -rf /requirements.txt            

    
COPY ./conf_files/supervisor/   /etc/supervisor.d/


# -------- #
#   Run!   #
# -------- #

CMD ["/usr/bin/supervisord", "--nodaemon", "--configuration", "/etc/supervisord.conf"]