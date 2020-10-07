FROM alpine:3.12

# ---------------- #
#   Installation   #
# ---------------- #

# Install and setup all prerequisites
RUN apk add --no-cache gcc g++ python3 py3-pip python3-dev supervisor                &&\
    rm -rf /var/cache/apk/*                                                          &&\
    pip3 install numpy                                                               &&\
    pip3 install haproxystats                                                        &&\
    mkdir -p  /etc/haproxystats  /var/lib/haproxy  /var/log/supervisor               

    
COPY ./conf_files/supervisor/   /etc/supervisor.d/


# -------- #
#   Run!   #
# -------- #

CMD ["/usr/bin/supervisord", "--nodaemon", "--configuration", "/etc/supervisord.conf"]