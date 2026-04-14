"""Testinfra tests for anxs-keepalived role."""
import time


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


def test_keepalived_config_global_defs(host):
    """keepalived.conf has global_defs block."""
    cfg = host.file("/etc/keepalived/keepalived.conf")
    content = cfg.content_string
    assert "global_defs" in content
    assert "enable_script_security" in content


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
