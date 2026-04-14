## [ANXS](https://github.com/ANXS) - keepalived

[![CI Status](https://img.shields.io/github/actions/workflow/status/anxs/keepalived/ci.yml)](https://github.com/ANXS/keepalived/actions/workflows/ci.yml)
[![Maintenance](https://img.shields.io/maintenance/yes/2026.svg)](https://github.com/ANXS/keepalived)
[![Ansible Role](https://img.shields.io/ansible/role/d/anxs/keepalived)](https://galaxy.ansible.com/ui/standalone/roles/ANXS/keepalived/)
[![License](https://img.shields.io/github/license/ANXS/keepalived)](https://github.com/ANXS/keepalived/blob/master/LICENSE)

Ansible role for keepalived VRRP management. Supports multiple VRRP instances with independent failover, pluggable health check scripts, first-class maintenance mode, unicast peers for environments without multicast, and systemd timer-based service watchdogs.

## Requirements & Dependencies

* Ansible 2.14 or higher.
* Debian 12+ or Ubuntu 22.04+.
* `ansible.posix` collection (for the `sysctl` module).

## Variables

Some commonly adjusted variables. See [`defaults/main.yml`](https://github.com/ANXS/keepalived/blob/master/defaults/main.yml) for the full set.

* `keepalived_instances` (default `[]`) is the list of VRRP instance definitions. Each entry specifies `name`, `interface`, `router_id`, `priority`, `vip`, plus optional health check, notify action, unicast peer, and watchdog settings.
* `keepalived_check_scripts` (default `[]`) is a list of user-provided check script templates to render into the keepalived config directory.
* Per-instance `builtin_checks` (DNS dig, HTTP health) lets instances opt into common health checks without shipping a custom script.
* Per-instance `maint_file` enables a check wrapper that forces FAULT when the file exists, for graceful maintenance drains.
* Per-instance `watchdog_services` plus `watchdog_interval` deploys a systemd timer that reconciles service state against the current VRRP state.

## Testing

Tests use [Molecule](https://github.com/ansible/molecule) with Docker and [Testinfra](https://testinfra.readthedocs.io/). Run the full suite with `make test`, or target a specific platform (e.g. `make test-debian12`). Scenarios spin up two containers with unicast VRRP so real failover can be exercised.

The test suite verifies package installation and sysctl, config rendering across single-instance and multi-instance setups, notify script state transitions and state-file handling, check wrapper maintenance-mode logic, VIP convergence (master holds the VIP, backup does not), maintenance-triggered failover and preemption recovery, and idempotence. Tests run across Debian 12/13 and Ubuntu 22.04/24.04.

## Note on AI Usage

This project has been developed with AI assistance. Contributions making use of AI generated content are welcome, however they _must_ be human reviewed prior to submission as pull requests, or issues. All contributors must be able to fully explain and defend any AI generated code, documentation, issues, or tests they submit. Contributions making use of AI must have this explicitly declared in the pull request or issue. This also applies to utilization of AI for reviewing of pull requests.

## Feedback, bug-reports, requests, ...

Are [welcome](https://github.com/ANXS/keepalived/issues)!
