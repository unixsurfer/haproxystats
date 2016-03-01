# == Class: haproxystats
#
# A class to configure HAProxy statistics collection tool haproxystats.
# See more information about haproxystats here
# https://github.com/unixsurfer/haproxystats
#
# === Parameters
#
# Document parameters here.
#
# [*sample_parameter*]
#
# === Examples
#
#  class { 'haproxystats':
#  }
#
# === Actions
#
# - Create user and group haproxystats
#
# === Requires
#
# - 'haproxystats' user and group defined in profile_base::user
# - syslog::activate{ 'haproxystats':}
# - daemon-reload exec resource
#   exec {
#     'systemd-daemon-reload':
#       refreshonly => true,
#       command     => '/bin/systemctl daemon-reload',
#       logoutput   => true;
#   }
# === Authors
#
# Author Name <pavlos.parissis@gmail.com>
#
# === Copyright
#
# Copyright 2016 Pavlos Parissis
#
class haproxystats (
  $package_name                 = $::haproxystats::params::package_name,
  $version                      = $::haproxystats::params::version,
  $enable                       = $::haproxystats::params::enable,
  $autostart                    = $::haproxystats::params::autostart,
  $enable_monit                 = $::haproxystats::params::enable_monit,
  $user                         = $::haproxystats::params::user,
  $group                        = $::haproxystats::params::group,
  $groups                       = $::haproxystats::params::groups,
  $log_rotate                   = $::haproxystats::params::log_rotate,
  $log_rotate_freq              = $::haproxystats::params::log_rotate_freq,
  $default_loglevel             = $::haproxystats::params::default_loglevel,
  $default_retries              = $::haproxystats::params::default_retries,
  $default_timeout              = $::haproxystats::params::default_timeout,
  $default_interval             = $::haproxystats::params::default_interval,
  $paths_base_dir               = $::haproxystats::params::paths_base_dir,
  $pull_loglevel                = $::haproxystats::params::pull_loglevel,
  $pull_retries                 = $::haproxystats::params::pull_retries,
  $pull_timeout                 = $::haproxystats::params::pull_timeout,
  $pull_interval                = $::haproxystats::params::pull_interval,
  $pull_socket_dir              = $::haproxystats::params::pull_socket_dir,
  $pull_pull_timeout            = $::haproxystats::params::pull_pull_timeout,
  $pull_pull_interval           = $::haproxystats::params::pull_pull_interval,
  $pull_dst_dir                 = $::haproxystats::params::pull_dst_dir,
  $pull_tmp_dst_dir             = $::haproxystats::params::pull_tmp_dst_dir,
  $pull_workers                 = $::haproxystats::params::pull_workers,
  $pull_queue_size              = $::haproxystats::params::pull_queue_size,
  $pull_CPUAffinity             = $::haproxystats::params::pull_CPUAffinity,
  $process_workers              = $::haproxystats::params::process_workers,
  $process_src_dir              = $::haproxystats::params::process_src_dir,
  $process_loglevel             = $::haproxystats::params::process_loglevel,
  $process_CPUAffinity          = $::haproxystats::params::process_CPUAffinity,
  $process_aggr_server_metrics  = $::haproxystats::params::process_aggr_server_metrics,
  $graphite_server              = $::haproxystats::params::graphite_server,
  $graphite_port                = $::haproxystats::params::graphite_port,
  $graphite_retries             = $::haproxystats::params::graphite_retries,
  $graphite_interval            = $::haproxystats::params::graphite_interval,
  $graphite_connect_timeout     = $::haproxystats::params::graphite_connect_timeout,
  $graphite_write_timeout       = $::haproxystats::params::graphite_write_timeout,
  $graphite_delay               = $::haproxystats::params::graphite_delay,
  $graphite_backoff             = $::haproxystats::params::graphite_backoff,
  $graphite_queue_size          = $::haproxystats::params::graphite_queue_size,
  $graphite_namespace           = $::haproxystats::params::graphite_namespace,
  $graphite_prefix_hostname     = $::haproxystats::params::graphite_prefix_hostname,
  $graphite_fqdn                = $::haproxystats::params::graphite_fqdn,
  $local_store_enabled          = $::haproxystats::params::local_store_enabled,
  $local_store_dir              = $::haproxystats::params::local_store_dir,
) inherits haproxystats::params {

  realize ( Group[$user] )
  User  <| title == "${user}" |> {
    groups  => $groups,
  }

  package {
    $package_name:
      ensure => $version,
  }

  file {
    $paths_base_dir:
      ensure  => directory,
      owner   => $user,
      group   => $group,
      require => [
        User[$user],
        Group[$group]
      ],
      mode    => '0755';
    ['/etc/systemd/system/haproxystats-process.service.d',
     '/etc/systemd/system/haproxystats-pull.service.d']:
      ensure   => directory,
      owner    => root,
      group    => root,
      mode     => '0755',
      purge    => true,
      recurse  => true;
    '/etc/systemd/system/haproxystats-pull.service.d/overwrites.conf':
      ensure   => file,
      owner    => root,
      group    => root,
      mode     => '0444',
      content  => template('haproxystats/pull-systemd-overwrites.conf.erb'),
      notify  => [
        Exec['systemd-daemon-reload'],
        Service['haproxystats-pull'],
      ],
      require  => File['/etc/systemd/system/haproxystats-pull.service.d'];
    '/etc/systemd/system/haproxystats-process.service.d/overwrites.conf':
      ensure   => file,
      owner    => root,
      group    => root,
      mode     => '0444',
      content  => template('haproxystats/process-systemd-overwrites.conf.erb'),
      notify  => [
        Exec['systemd-daemon-reload'],
        Service['haproxystats-process'],
      ],
      require  => File['/etc/systemd/system/haproxystats-process.service.d'];
  }
  concat {
    '/etc/haproxystats.conf':
      mode    => 0444,
      owner   => $user,
      group   => $group,
      require => [Package[$package_name]],
      notify  => [
        Service['haproxystats-pull'],
        Service['haproxystats-process'],
      ];
  }
  concat::fragment {
    'defaults':
      target  => '/etc/haproxystats.conf',
      order   => '00',
      content => template('haproxystats/defaults.conf.erb');
    'pull':
      target  => '/etc/haproxystats.conf',
      order   => '01',
      content => template('haproxystats/pull.conf.erb'),
      notify  => Service['haproxystats-pull'];
    'process':
      target  => '/etc/haproxystats.conf',
      order   => '02',
      content => template('haproxystats/process.conf.erb'),
      notify  => Service['haproxystats-process'];
  }
  service {
    'haproxystats-pull':
      ensure  => $enable,
      enable  => $autostart,
      require => [
        Package[$package_name],
        Concat['/etc/haproxystats.conf'],
      ];
    'haproxystats-process':
      ensure  => $enable,
      enable  => $autostart,
      require => [
        Package[$package_name],
        Concat['/etc/haproxystats.conf'],
      ];
  }
  syslog::activate{ 'haproxystats':
    rotate      => $log_rotate,
    rotate_freq => $log_rotate_freq;
  }

  if $enable_monit {
    monit::program {
      'haproxystats-process':
        scriptname => '/usr/lib64/nagios/plugins/check_haproxystats_process_number_of_procs.sh -v' ,
        email      => 'prod.gtd@booking.com',
        tolerance  => 2,
        priority   => 'priority_1',
        nrestarts  => 2,
    }
  }
}
