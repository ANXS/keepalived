"""Testinfra tests for anxs-keepalived role."""
import time


# --- Phase 5: Basic convergence tests ---


def test_keepalived_installed(host):
    """keepalived package is installed."""
    pkg = host.package("keepalived")
    assert pkg.is_installed


def test_keepalived_running(host):
    """keepalived service is running and enabled."""
    svc = host.service("keepalived")
    assert svc.is_running
    assert svc.is_enabled


def test_keepalived_config_exists(host):
    """keepalived.conf is rendered."""
    cfg = host.file("/etc/keepalived/keepalived.conf")
    assert cfg.exists
    assert cfg.user == "root"
    assert cfg.mode == 0o644


def test_keepalived_config_contains_instance(host):
    """keepalived.conf contains the test VRRP instance."""
    cfg = host.file("/etc/keepalived/keepalived.conf")
    content = cfg.content_string
    assert "vrrp_instance test" in content
    assert "172.28.0.10" in content
    assert "unicast_peer" in content
    assert "nopreempt" in content
    assert "preempt_delay" not in content


def test_keepalived_config_global_defs(host):
    """keepalived.conf has global_defs block."""
    cfg = host.file("/etc/keepalived/keepalived.conf")
    content = cfg.content_string
    assert "global_defs" in content
    assert "enable_script_security" in content


def test_garp_master_refresh_absent_when_unset(host):
    """Neither garp directive is emitted when the instance does not set them.

    Keeps the rendered config byte-identical for consumers that never
    configure them.
    """
    content = host.file("/etc/keepalived/keepalived.conf").content_string
    assert "garp_master_refresh" not in content, (
        "garp_master_refresh was rendered without being configured -- "
        "existing consumers would get a changed config:\n" + content
    )
    assert "garp_master_refresh_repeat" not in content, \
        "garp_master_refresh_repeat was rendered without being configured"


def test_vmac_directives_absent_when_disabled(host):
    """use_vmac false emits neither use_vmac nor vmac_xmit_base.

    vmac_xmit_base defaults to true, so only its nesting inside the use_vmac
    branch keeps it out.
    """
    content = host.file("/etc/keepalived/keepalived.conf").content_string
    assert "use_vmac" not in content, \
        "use_vmac rendered even though the instance sets it false"
    assert "vmac_xmit_base" not in content, \
        "vmac_xmit_base rendered outside the use_vmac branch"


def test_rendered_config_passes_keepalived_config_test(host):
    """keepalived's own parser accepts the config the role shipped."""
    result = host.run("keepalived -t -f /etc/keepalived/keepalived.conf")
    assert result.rc == 0, (
        "keepalived --config-test rejected the shipped config:\n"
        + result.stdout + result.stderr
    )


def test_notify_script_deployed(host):
    """notify script is deployed with correct permissions."""
    script = host.file("/etc/keepalived/notify_test.sh")
    assert script.exists
    assert script.user == "root"
    assert oct(script.mode) == "0o770"
    content = script.content_string
    assert "keepalived_test" in content


def test_check_wrapper_deployed(host):
    """check wrapper is deployed (maint_file is set)."""
    script = host.file("/etc/keepalived/check_wrapper_test.sh")
    assert script.exists
    assert oct(script.mode) == "0o770"
    content = script.content_string
    assert "/var/cache/keepalived-maint" in content


def test_state_directory_exists(host):
    """state directory is created."""
    d = host.file("/var/run/keepalived")
    assert d.exists
    assert d.is_directory


def test_sysctl_nonlocal_bind(host):
    """ip_nonlocal_bind sysctl is set."""
    result = host.sysctl("net.ipv4.ip_nonlocal_bind")
    assert result == 1


# --- Phase 6: VIP convergence and state ---


def _has_vip(host):
    """Check if VIP 172.28.0.10 is present on any interface."""
    ip_output = host.check_output("ip addr show")
    return "172.28.0.10" in ip_output


def _wait_for_vip(host, expected, timeout=15):
    """Wait for VIP presence to match expected state."""
    for _ in range(timeout):
        if _has_vip(host) == expected:
            return True
        time.sleep(1)
    return _has_vip(host) == expected


def test_vip_initial_convergence(host):
    """The elected master owns the VIP and the backup does not."""
    # give keepalived time to settle
    time.sleep(5)
    state = host.file("/var/run/keepalived/test").content_string.strip()
    if _has_vip(host):
        assert state == "leader", "VIP owner should report leader state"
    else:
        assert state == "failover", "node without VIP should report failover state"


def test_state_file_written(host):
    """notify script writes state file on transition."""
    time.sleep(5)
    state_file = host.file("/var/run/keepalived/test")
    assert state_file.exists, "state file should exist after convergence"
    content = state_file.content_string.strip()
    if _has_vip(host):
        assert content == "leader", "VIP owner state file should say 'leader'"
    else:
        assert content == "failover", \
            "node without VIP state file should say 'failover'"


def test_maintenance_mode_failover(host):
    """Non-preemptive failover retains the healthy backup after recovery."""
    if not _has_vip(host):
        # only run this test on the actually elected master
        return

    # enter maintenance mode
    host.run("touch /var/cache/keepalived-maint")

    # wait for check to fail (fall=2, interval=1 -> ~3s) + transition
    assert _wait_for_vip(host, False, timeout=15), \
        "VIP should leave master after maint file touched"

    # recover the higher-priority node, then wait beyond check recovery.
    host.run("rm -f /var/cache/keepalived-maint")
    time.sleep(10)

    assert not _has_vip(host), \
        "recovered higher-priority node must not preempt the healthy master"


def test_recovered_former_master_stays_backup(host):
    """The recovered former master remains backup after the failover cycle."""
    if _has_vip(host):
        # only inspect the node that does not own the VIP
        return

    state_file = host.file("/var/run/keepalived/test")
    assert state_file.exists
    assert state_file.content_string.strip() == "failover", \
        "node without VIP should remain in failover state"


def test_check_wrapper_maint_logic(host):
    """Check wrapper script correctly detects maint file."""
    # without maint file, wrapper should exit 0
    result = host.run("/etc/keepalived/check_wrapper_test.sh")
    assert result.rc == 0, "wrapper should pass without maint file"

    # with maint file, wrapper should exit 1
    host.run("touch /var/cache/keepalived-maint")
    result = host.run("/etc/keepalived/check_wrapper_test.sh")
    assert result.rc == 1, "wrapper should fail with maint file present"

    # clean up
    host.run("rm -f /var/cache/keepalived-maint")


# --- Shellcheck: lint rendered scripts ---

RENDERED_SCRIPTS = [
    "/etc/keepalived/notify_test.sh",
    "/etc/keepalived/check_wrapper_test.sh",
]


def test_shellcheck_notify_script(host):
    """notify script passes shellcheck."""
    result = host.run("shellcheck -S warning /etc/keepalived/notify_test.sh")
    assert result.rc == 0, (
        "shellcheck failed on notify_test.sh:\n" + result.stdout + result.stderr
    )


def test_shellcheck_check_wrapper(host):
    """check wrapper passes shellcheck."""
    result = host.run(
        "shellcheck -S warning /etc/keepalived/check_wrapper_test.sh"
    )
    assert result.rc == 0, (
        "shellcheck failed on check_wrapper_test.sh:\n"
        + result.stdout + result.stderr
    )


# --- Script functionality tests ---


def test_notify_script_handles_master(host):
    """notify script correctly processes MASTER transition."""
    result = host.run(
        "/etc/keepalived/notify_test.sh INSTANCE test MASTER"
    )
    assert result.rc == 0, (
        "notify script failed on MASTER:\n" + result.stdout + result.stderr
    )
    state = host.file("/var/run/keepalived/test")
    assert state.content_string.strip() == "leader"


def test_notify_script_handles_backup(host):
    """notify script correctly processes BACKUP transition."""
    result = host.run(
        "/etc/keepalived/notify_test.sh INSTANCE test BACKUP"
    )
    assert result.rc == 0, (
        "notify script failed on BACKUP:\n" + result.stdout + result.stderr
    )
    state = host.file("/var/run/keepalived/test")
    assert state.content_string.strip() == "failover"


def test_notify_script_handles_fault(host):
    """notify script correctly processes FAULT transition."""
    result = host.run(
        "/etc/keepalived/notify_test.sh INSTANCE test FAULT"
    )
    assert result.rc == 0, (
        "notify script failed on FAULT:\n" + result.stdout + result.stderr
    )
    state = host.file("/var/run/keepalived/test")
    assert state.content_string.strip() == "error"


def test_notify_script_rejects_invalid_state(host):
    """notify script exits non-zero on invalid state."""
    result = host.run(
        "/etc/keepalived/notify_test.sh INSTANCE test BOGUS"
    )
    assert result.rc != 0, "notify script should fail on invalid state"


def test_notify_state_file_permissions(host):
    """notify script creates state file with correct permissions."""
    host.run("/etc/keepalived/notify_test.sh INSTANCE test MASTER")
    state = host.file("/var/run/keepalived/test")
    assert state.exists
    assert state.mode == 0o644


def test_check_wrapper_without_check_script(host):
    """check wrapper exits 0 when no maint file and no user check."""
    # the wrapper execs the user check_script if set, otherwise exits 0
    result = host.run("/etc/keepalived/check_wrapper_test.sh")
    assert result.rc == 0
