* sanity check config

* exit if graphite section is not configured

* Cross check list metrics

* Check if metrics are in pandas data frame before perform operations on data
  frames

* Implement a retry logic for connecting to /pulling data from UNIX sockets

* graphite namespace

    loadbalancers.lb203_lhr4_prod_foo_com
    loadbalancers.lb203_lhr4_prod_foo_com.haproxystats
    loadbalancers.lb203_lhr4_prod_foo_com.haproxystats.process_time
    loadbalancers.lb203_lhr4_prod_foo_com.haproxystats.process_time
    loadbalancers.lb203_lhr4_prod_foo_com.haproxystats.process_time

    loadbalancers.lb203_lhr4_prod_foo_com.haproxy
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.backend
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.backend.foo_bar_com
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.backend.foo_bar_com.rate
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.backend.foo_bar_com.server
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.backend.foo_bar_com.server.foo_srv1
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.backend.foo_bar_com.server.foo_srv1.lbtot
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.backend.foo_bar_com.server.foo_srv1.weight
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.frontend
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.frontend.foo_bar_com_https
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.frontend.foo_bar_com_https.rate
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.frontend.foo_bar_com_https.stot
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.daemon
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.daemon.Idle_pct
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.daemon.MaxSslRate
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.server
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.server.foo_srv1
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.server.foo_srv1.lbtot
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.server.foo_srv1.rate
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.server.foo_srv2
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.server.foo_srv2.lbtot
    loadbalancers.lb203_lhr4_prod_foo_com.haproxy.server.foo_srv2.rate
