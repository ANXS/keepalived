"""Testinfra tests for the anxs-keepalived vmac scenario.

The VIP here carries its own virtual MAC, refreshes its gratuitous ARP on a
timer, has builtin checks and notify actions populated, and a second instance
covers the nopreempt: false + preempt_delay path.

Where it matters these tests ask the RUNNING DAEMON what it parsed, via its
SIGUSR1 data dump, rather than grepping the file we just wrote: "the text is
in the config" and "keepalived accepted the directive" are different claims.
"""
import re
import time

CONFIG = "/etc/keepalived/keepalived.conf"
DATA_DUMP = "/tmp/keepalived.data"
STATE_DIR = "/var/run/keepalived"

VMAC = "vmac"
PREEMPT = "preempt"

VMAC_MAINT = "/var/cache/keepalived-vmac-maint"
PREEMPT_MAINT = "/var/cache/keepalived-preempt-maint"

GARP_REFRESH = 7
GARP_REFRESH_REPEAT = 2
PREEMPT_DELAY = 5
HIGH_PRIORITY = 200

RENDERED_SCRIPTS = [
    "/etc/keepalived/notify_vmac.sh",
    "/etc/keepalived/notify_preempt.sh",
    "/etc/keepalived/vmac_quiet_vmac.sh",
    "/etc/keepalived/check_wrapper_vmac.sh",
    "/etc/keepalived/check_wrapper_preempt.sh",
    "/etc/keepalived/check_builtin_vmac.sh",
    "/etc/keepalived/check_builtin_preempt.sh",
]


# --- helpers -------------------------------------------------------------


def _config(host):
    return host.file(CONFIG).content_string


def _instance_block(host, name):
    """Return the body of `vrrp_instance <name> { ... }` from the config.

    Two instances render into one file, so a bare `"nopreempt" in content`
    would pass on the strength of the other instance and prove nothing.
    """
    content = _config(host)
    marker = "vrrp_instance {} {{".format(name)
    assert marker in content, \
        "instance {} is missing from the rendered config".format(name)
    start = content.index(marker) + len(marker)
    depth = 1
    for idx in range(start, len(content)):
        char = content[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[start:idx]
    raise AssertionError("unbalanced braces around instance " + name)


def _vip(host, name):
    """The VIP of an instance, read back out of the rendered config.

    Docker picks this scenario's subnet, so neither the scenario nor the tests
    can hardcode the VIPs.
    """
    block = _instance_block(host, name)
    match = re.search(r"virtual_ipaddress\s*\{\s*([0-9.]+)", block)
    assert match, "no virtual_ipaddress found for instance " + name
    return match.group(1)


def _addresses(host):
    return host.check_output("ip -o -4 addr show")


def _has_vip(host, vip):
    """Is this VIP present on any interface of this host?"""
    return _vip_interface(host, vip) is not None


def _vip_interface(host, vip):
    """The interface carrying this VIP, or None."""
    for line in _addresses(host).splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[2] == "inet" \
                and fields[3].split("/")[0] == vip:
            return fields[1]
    return None


def _wait_for_vip(host, vip, expected, timeout=20):
    """Wait for VIP presence to match expected state."""
    for _ in range(timeout):
        if _has_vip(host, vip) == expected:
            return True
        time.sleep(1)
    return _has_vip(host, vip) == expected


def _state(host, instance):
    return host.file(STATE_DIR + "/" + instance).content_string.strip()


def _data_dump(host):
    """Make the running daemon dump its parsed configuration and return it.

    keepalived writes /tmp/keepalived.data on SIGUSR1 (signal 10, which is
    what `keepalived --signum=DATA` reports). This is the only way to see what
    the daemon actually accepted.
    """
    host.run("systemctl kill -s SIGUSR1 keepalived.service")
    for _ in range(10):
        dump = host.file(DATA_DUMP)
        if dump.exists and "VRRP Instance" in dump.content_string:
            return dump.content_string
        time.sleep(1)
    raise AssertionError("keepalived never wrote " + DATA_DUMP)


def _marker(host, name):
    """Parse a key=value record written by the side_effect stage."""
    record = host.file(STATE_DIR + "/" + name)
    assert record.exists, (
        "the side_effect stage left no {} record -- it either did not run or "
        "failed before recording".format(name)
    )
    return dict(
        line.split("=", 1)
        for line in record.content_string.strip().splitlines()
        if "=" in line
    )


def _dump_instance_block(dump, name):
    """Slice one VRRP instance out of the running daemon's data dump."""
    marker = " VRRP Instance = {}\n".format(name)
    assert marker in dump, \
        "running daemon does not know about instance " + name
    start = dump.index(marker) + len(marker)
    nxt = dump.find(" VRRP Instance = ", start)
    return dump[start:nxt if nxt != -1 else len(dump)]


# --- the config has to be acceptable to keepalived at all ----------------


def test_keepalived_installed(host):
    """keepalived package is installed."""
    assert host.package("keepalived").is_installed


def test_keepalived_running_with_vmac_config(host):
    """keepalived is running AND enabled with the vmac config.

    A rendered directive keepalived refuses keeps the daemon down, and a daemon
    that is down holds no VIP.
    """
    svc = host.service("keepalived")
    assert svc.is_running, "keepalived is not running with use_vmac + garp refresh"
    assert svc.is_enabled, "keepalived is not enabled"


def test_rendered_config_passes_keepalived_config_test(host):
    """The config the role shipped passes keepalived's own parser."""
    result = host.run("keepalived -t -f " + CONFIG)
    assert result.rc == 0, (
        "keepalived --config-test rejected the shipped config:\n"
        + result.stdout + result.stderr
    )


def test_config_test_rejects_a_broken_config(host):
    """The validator discriminates -- otherwise the gate is theatre.

    A `keepalived -t` that returned 0 for everything would look identical in a
    passing run.
    """
    host.run(
        '/bin/sh -c "head -n 5 ' + CONFIG + ' > /tmp/ti-broken.conf"'
    )
    good = host.run("keepalived -t -f " + CONFIG)
    bad = host.run("keepalived -t -f /tmp/ti-broken.conf")
    assert good.rc == 0, "config-test failed on the good config"
    assert bad.rc != 0, (
        "config-test ACCEPTED a truncated config -- the validation gate "
        "cannot be trusted"
    )


# --- garp_master_refresh -------------------------------------------------


def test_garp_master_refresh_rendered(host):
    """garp_master_refresh renders with the configured value.

    Without it nothing re-announces the VIP's MAC while a nopreempt master
    holds it, and the upstream ARP entry expires.
    """
    block = _instance_block(host, VMAC)
    assert re.search(
        r"^\s*garp_master_refresh {}\s*$".format(GARP_REFRESH),
        block, re.M
    ), "garp_master_refresh {} missing from instance {}:\n{}".format(
        GARP_REFRESH, VMAC, block
    )


def test_garp_master_refresh_repeat_rendered(host):
    """garp_master_refresh_repeat renders with the configured value."""
    block = _instance_block(host, VMAC)
    assert re.search(
        r"^\s*garp_master_refresh_repeat {}\s*$".format(GARP_REFRESH_REPEAT),
        block, re.M
    ), "garp_master_refresh_repeat missing from instance " + VMAC


def test_garp_master_refresh_absent_on_instance_that_omits_it(host):
    """An instance that does not set it gets nothing rendered.

    Asserted against a second instance in the SAME file, so a template that
    emitted a default cannot hide behind the instance that does set it.
    """
    block = _instance_block(host, PREEMPT)
    assert "garp_master_refresh" not in block, (
        "garp_master_refresh leaked into instance {} which never set it:\n{}"
        .format(PREEMPT, block)
    )


def test_garp_master_refresh_accepted_by_running_daemon(host):
    """The RUNNING daemon reports the refresh timer we configured.

    keepalived is lenient: a value it dislikes is logged as "ignoring" and the
    daemon starts anyway, silently without the refresh.
    """
    dump = _data_dump(host)
    block = _dump_instance_block(dump, VMAC)
    assert "Gratuitous ARP refresh = {}".format(GARP_REFRESH) in block, (
        "running daemon did not accept garp_master_refresh {}:\n{}".format(
            GARP_REFRESH, block
        )
    )
    assert "Gratuitous ARP refresh repeat = {}".format(
        GARP_REFRESH_REPEAT) in block, (
        "running daemon did not accept garp_master_refresh_repeat"
    )


def test_running_daemon_has_no_refresh_where_unset(host):
    """The instance that omits the directive runs with the refresh off."""
    dump = _data_dump(host)
    block = _dump_instance_block(dump, PREEMPT)
    assert "Gratuitous ARP refresh = 0" in block, (
        "instance {} should be running with no ARP refresh:\n{}".format(
            PREEMPT, block
        )
    )


# --- use_vmac / vmac_xmit_base ------------------------------------------


def test_use_vmac_and_xmit_base_rendered(host):
    """use_vmac and vmac_xmit_base render when enabled."""
    block = _instance_block(host, VMAC)
    assert re.search(r"^\s*use_vmac\s*$", block, re.M), \
        "use_vmac missing from instance " + VMAC
    assert re.search(r"^\s*vmac_xmit_base\s*$", block, re.M), \
        "vmac_xmit_base missing from instance " + VMAC


def test_use_vmac_absent_where_disabled(host):
    """The instance with use_vmac false gets neither directive."""
    block = _instance_block(host, PREEMPT)
    assert "use_vmac" not in block, \
        "use_vmac leaked into instance " + PREEMPT
    assert "vmac_xmit_base" not in block, \
        "vmac_xmit_base leaked into instance " + PREEMPT


def test_use_vmac_accepted_by_running_daemon(host):
    """The running daemon built the macvlan and transmits on the base i/f."""
    dump = _data_dump(host)
    block = _dump_instance_block(dump, VMAC)
    assert "Use VMAC" in block, \
        "running daemon is not using a virtual MAC:\n" + block
    assert "xmit_base = true" in block, \
        "running daemon is not transmitting on the base interface"


# --- nopreempt / preempt_delay ------------------------------------------


def test_nopreempt_rendered_without_preempt_delay(host):
    """nopreempt renders, and preempt_delay does not, on the vmac instance."""
    block = _instance_block(host, VMAC)
    assert re.search(r"^\s*nopreempt\s*$", block, re.M), \
        "nopreempt missing from instance " + VMAC
    assert "preempt_delay" not in block, \
        "preempt_delay must not be rendered alongside nopreempt"


def test_preempt_delay_rendered_when_nopreempt_false(host):
    """preempt_delay renders when nopreempt is false."""
    block = _instance_block(host, PREEMPT)
    assert re.search(
        r"^\s*preempt_delay {}\s*$".format(PREEMPT_DELAY), block, re.M
    ), "preempt_delay {} missing from instance {}:\n{}".format(
        PREEMPT_DELAY, PREEMPT, block
    )
    assert not re.search(r"^\s*nopreempt\s*$", block, re.M), \
        "nopreempt must not be rendered when the instance sets it false"


def test_preempt_settings_accepted_by_running_daemon(host):
    """The running daemon reports preemption enabled with our delay."""
    dump = _data_dump(host)
    preempt_block = _dump_instance_block(dump, PREEMPT)
    vmac_block = _dump_instance_block(dump, VMAC)
    assert "Preempt = enabled" in preempt_block, \
        "instance {} should be preemptive:\n{}".format(PREEMPT, preempt_block)
    assert "Preempt delay = {} secs".format(PREEMPT_DELAY) in preempt_block, \
        "running daemon did not accept preempt_delay"
    assert "Preempt = disabled" in vmac_block, \
        "instance {} should be non-preemptive".format(VMAC)


# --- builtin_checks ------------------------------------------------------


def test_builtin_check_script_rendered(host):
    """builtin_checks render into a deployed check script when populated."""
    script = host.file("/etc/keepalived/check_builtin_vmac.sh")
    assert script.exists, "no builtin check script deployed for " + VMAC
    assert oct(script.mode) == "0o770"
    content = script.content_string
    assert "vip-check.molecule.test" in content, \
        "dns_dig_host did not render into the builtin check"
    assert "dig" in content, "dns_dig check did not render"
    assert "http://127.0.0.1:8080/health" in content, \
        "http_health_url did not render into the builtin check"
    assert "curl" in content, "http_health check did not render"


def test_builtin_check_runs_and_passes(host):
    """The rendered builtin check actually works, not just parses."""
    result = host.run("/etc/keepalived/check_builtin_preempt.sh")
    assert result.rc == 0, (
        "builtin http_health check failed against a healthy endpoint:\n"
        + result.stdout + result.stderr
    )


def test_builtin_check_wired_as_tracked_script(host):
    """An instance with check_script set tracks it through the wrapper."""
    block = _instance_block(host, PREEMPT)
    assert "track_script" in block, \
        "instance {} has no track_script block".format(PREEMPT)
    wrapper = host.file("/etc/keepalived/check_wrapper_preempt.sh")
    assert "exec /etc/keepalived/check_builtin_preempt.sh" in \
        wrapper.content_string, \
        "the wrapper does not exec the builtin check it was pointed at"


def test_builtin_check_honours_maintenance(host):
    """The builtin check fails while the maint file exists."""
    host.run("touch " + PREEMPT_MAINT)
    try:
        result = host.run("/etc/keepalived/check_builtin_preempt.sh")
        assert result.rc == 1, \
            "builtin check ignored the maintenance file"
    finally:
        host.run("rm -f " + PREEMPT_MAINT)


# --- notify_actions ------------------------------------------------------


def test_notify_actions_rendered(host):
    """Populated notify_actions render into the notify script."""
    content = host.file("/etc/keepalived/notify_vmac.sh").content_string
    assert "touch /var/run/keepalived/vmac_master_action" in content, \
        "master notify_actions did not render"
    assert "touch /var/run/keepalived/vmac_backup_action" in content, \
        "backup notify_actions did not render"
    assert "touch /var/run/keepalived/vmac_fault_action" in content, \
        "fault notify_actions did not render"
    assert 'if [ "$TARGET_STATE" == "MASTER" ]' in content, \
        "notify actions are not gated on the target state"


# --- the VIP, under a virtual MAC ---------------------------------------


def _wait_for_consistent_state(host, instance, vip, timeout=25):
    """Wait until the state file and VIP ownership agree."""
    for _ in range(timeout):
        if (_state(host, instance) == "leader") == _has_vip(host, vip):
            return True
        time.sleep(1)
    return (_state(host, instance) == "leader") == _has_vip(host, vip)


def test_vmac_vip_converged(host):
    """Exactly the leader owns the vmac VIP, and reports itself leader."""
    vip = _vip(host, VMAC)
    assert _wait_for_consistent_state(host, VMAC, vip), (
        "state file says {!r} but VIP {} presence is {}".format(
            _state(host, VMAC), vip, _has_vip(host, vip)
        )
    )
    if _has_vip(host, vip):
        assert _state(host, VMAC) == "leader", \
            "VIP owner should report leader state"
    else:
        assert _state(host, VMAC) == "failover", (
            "node without the VIP should report failover, not {!r}".format(
                _state(host, VMAC)
            )
        )


def test_vmac_vip_lives_on_the_virtual_mac_interface(host):
    """The VIP is carried on the macvlan child, not on the base interface.

    This is why the ARP refresh matters: the VIP answers from its own MAC,
    which nothing refreshes between VRRP transitions.
    """
    vip = _vip(host, VMAC)
    if not _has_vip(host, vip):
        return
    iface = _vip_interface(host, vip)
    assert iface is not None
    assert iface != "eth0", (
        "VIP {} is on the base interface {} -- use_vmac did not take effect"
        .format(vip, iface)
    )
    link = host.check_output("ip -o link show " + iface)
    assert "00:00:5e:00:01:" in link, (
        "{} does not carry a VRRP virtual MAC: {}".format(iface, link)
    )


def test_preempt_vip_converged(host):
    """The non-vmac instance converges too, on the base interface."""
    vip = _vip(host, PREEMPT)
    assert _wait_for_consistent_state(host, PREEMPT, vip), (
        "instance {} state file ({!r}) and VIP ownership ({}) disagree".format(
            PREEMPT, _state(host, PREEMPT), _has_vip(host, vip)
        )
    )
    if _has_vip(host, vip):
        assert _vip_interface(host, vip) == "eth0", \
            "instance with use_vmac false should hold its VIP on eth0"


# --- the virtual MAC stays quiet while BACKUP ----------------------------


def _vmac_interface(host):
    """The macvlan keepalived creates for the vmac instance, from its VRID."""
    match = re.search(
        r"virtual_router_id\s+(\d+)", _instance_block(host, VMAC)
    )
    assert match, "no virtual_router_id rendered for instance " + VMAC
    return "vrrp." + match.group(1)


def _quiet_chain(host):
    return host.run("nft list chain inet anxs_keepalived quiet_" + VMAC)


def test_nftables_installed_where_quieting_is_enabled(host):
    """The quiet helper needs nft, so the role installs it."""
    assert host.package("nftables").is_installed


def test_quiet_helper_rendered_only_where_enabled(host):
    """vmac_quiet_backup renders a helper and wires it into notify; the
    instance without it gets neither."""
    assert host.file("/etc/keepalived/vmac_quiet_vmac.sh").exists
    assert not host.file("/etc/keepalived/vmac_quiet_preempt.sh").exists
    notify_vmac = host.file("/etc/keepalived/notify_vmac.sh").content_string
    notify_preempt = host.file(
        "/etc/keepalived/notify_preempt.sh"
    ).content_string
    assert "vmac_quiet_vmac.sh" in notify_vmac, \
        "notify_vmac.sh does not invoke the quiet helper"
    assert "vmac_quiet" not in notify_preempt, \
        "the quiet helper leaked into an instance that did not enable it"


def test_virtual_mac_is_quiet_on_the_backup_and_open_on_the_master(host):
    """BACKUP: no MULTICAST flag and an egress drop on vrrp.<vrid>.
    MASTER: MULTICAST flag present and the chain empty.

    Decided from this node's own VIP ownership, so it holds whichever node
    pytest happens to be visiting.
    """
    vip = _vip(host, VMAC)
    assert _wait_for_consistent_state(host, VMAC, vip), (
        "state file says {!r} but VIP {} presence is {}".format(
            _state(host, VMAC), vip, _has_vip(host, vip)
        )
    )
    iface = _vmac_interface(host)
    link = host.check_output("ip -d -o link show dev " + iface)
    chain = _quiet_chain(host)
    assert chain.rc == 0, (
        "the quiet chain is missing:\n" + chain.stdout + chain.stderr
    )
    drop = re.search(
        r'oifname "{}".*drop'.format(re.escape(iface)), chain.stdout
    )
    if _has_vip(host, vip):
        assert "MULTICAST" in link, \
            "the master's {} lost its multicast flag: {}".format(iface, link)
        assert drop is None, \
            "the master still drops its own VIP egress:\n" + chain.stdout
    else:
        assert "MULTICAST" not in link, \
            "the backup's {} still has multicast on: {}".format(iface, link)
        assert drop is not None, (
            "the backup has no egress drop for {}:\n{}".format(
                iface, chain.stdout
            )
        )


def test_quieting_was_proven_on_the_wire(host):
    """Read back from the side_effect stage: with quieting lifted the master
    saw the backup's multicast under the virtual MAC (so the instrument
    works); with quieting applied the same send put nothing on the wire.

    On the backup the same records show the frames were either refused by
    the kernel or counted by the drop rule.
    """
    control = _marker(host, "vmac_leak_control")
    quiet = _marker(host, "vmac_leak_quiet")
    if control["is_master"] == "True":
        assert int(control["frames_seen"]) >= 1, (
            "control phase saw no virtual-MAC frames, so the capture or the "
            "sender is broken and the quiet result below would be "
            "theatre: " + str(control)
        )
        assert int(quiet["frames_seen"]) == 0, (
            "a quieted backup still put frames on the wire under the "
            "virtual MAC: " + str(quiet)
        )
        return
    sent_control = re.search(r"sent=(\d+)", control["sender_result"])
    assert sent_control and int(sent_control.group(1)) >= 1, \
        "the control send never left the socket: " + str(control)
    sent_quiet = int(re.search(r"sent=(\d+)", quiet["sender_result"]).group(1))
    before = int(quiet["counter_before"] or 0)
    after = int(quiet["counter_after"] or 0)
    assert sent_quiet == 0 or after - before >= sent_quiet, (
        "frames left the socket on the quieted backup without being "
        "dropped: " + str(quiet)
    )


# --- failover, under a virtual MAC --------------------------------------
# These mutate state: keep them after the convergence tests, and keep the
# manual notify-script invocations last of all.


def _is_high_priority(host):
    """Is this the higher-priority node for the vmac instance?"""
    return bool(re.search(
        r"^\s*priority {}\s*$".format(HIGH_PRIORITY),
        _instance_block(host, VMAC), re.M
    ))


def test_vmac_maintenance_failover_cycle(host):
    """Drain the vmac master, watch the VIP move, and watch it NOT come back.

    Runs on the HIGHER-priority node: a recovered node that outranks the
    current master must still not trigger a second cutover. The side_effect
    stage hands the VIP here deliberately, because pytest groups tests by host
    and one that acts only "if I happen to hold the VIP" would skip quietly.
    """
    if not _is_high_priority(host):
        return

    vip = _vip(host, VMAC)
    assert _wait_for_vip(host, vip, True, timeout=25), (
        "the priority {} node should hold {} -- the side_effect stage hands "
        "it over with a deliberate drain before verify runs".format(
            HIGH_PRIORITY, vip
        )
    )

    host.run("touch " + VMAC_MAINT)
    assert _wait_for_vip(host, vip, False, timeout=20), \
        "VIP should leave the master once the maint file exists"

    host.run("rm -f " + VMAC_MAINT)
    time.sleep(12)
    assert not _has_vip(host, vip), (
        "the priority {} node took {} back on recovery despite nopreempt -- "
        "that is the second cutover nopreempt exists to prevent".format(
            HIGH_PRIORITY, vip
        )
    )
    assert _state(host, VMAC) == "failover", \
        "the recovered node should report failover, not leader"


def test_vip_was_acquired_on_a_virtual_mac_interface(host):
    """A vmac VIP can be ACQUIRED on failover, not just held from startup.

    Read back from the side_effect stage rather than measured live: which host
    holds the VIP at verify time depends on pytest's host ordering, so a live
    check would be a coin flip dressed up as a test.
    """
    fields = _marker(host, "vip_handoff")
    if fields["held_vip_before_drain"] != "True":
        return
    assert fields["vip_iface_before_drain"] not in ("none", "eth0"), (
        "this node acquired the VIP on interface {!r} -- a vmac VIP must "
        "land on the macvlan child, not the base interface".format(
            fields["vip_iface_before_drain"]
        )
    )


# --- evidence recorded by the side_effect stage -------------------------


def test_bad_config_was_refused_not_shipped(host):
    """A config with an invalid directive value never reached disk.

    Reads back the side_effect stage's re-run with one invalid value: the gate
    fired, the previous config was untouched, keepalived was never restarted.
    """
    record = host.file(STATE_DIR + "/bad_config_refused")
    assert record.exists, \
        "the side_effect stage did not record a bad-config refusal"
    content = record.content_string
    assert "gate_fired=True" in content, \
        "the validation gate did not fire:\n" + content
    assert "config_unchanged=True" in content, \
        "the rejected config reached disk:\n" + content
    assert "service_unchanged=True" in content, \
        "keepalived was restarted despite the rejected config:\n" + content
    assert "garp_master_refresh" in content, \
        "the failure did not quote keepalived's own diagnostics:\n" + content


def test_single_node_restart_blast_radius(host):
    """Converging one node at a time stays inside its measured budget.

    Peer's vantage point only: the restarted node's own view is distorted by
    keepalived rebuilding the nftables rules that come with use_vmac.
    """
    budgets = {"backup_restart": 1.0, "master_restart": 15.0}
    seen = 0
    for phase, budget in budgets.items():
        fields = _marker(host, "vip_gap_" + phase)
        if fields["restarted_here"] == "True":
            continue
        seen += 1
        assert int(fields["samples"]) > 50, \
            "probe for {} barely sampled: {}".format(phase, fields)
        assert float(fields["gap_seconds"]) <= budget, (
            "VIP was unreachable for {}s during {}, budget {}s".format(
                fields["gap_seconds"], phase, budget
            )
        )
        assert fields["final_sample"].endswith("up"), \
            "VIP never came back after " + phase
    assert seen > 0, \
        "this host was restarted in every phase, so nothing was measured"


def test_deliberate_drain_returned_the_vip_to_the_higher_priority_node(host):
    """A deliberate drain is how the VIP gets back to the preferred node.

    With nopreempt a restarted master does NOT take its VIP back, so moving it
    is an operator action. This also establishes the known VIP owner the
    failover-cycle test above depends on, so a wrong owner fails here loudly
    instead of skipping that test quietly.
    """
    fields = _marker(host, "vip_handoff")
    assert fields["owner_is_high_priority"] == "True", (
        "after the deliberate drain the VIP is not on the higher-priority "
        "node: " + str(fields)
    )


# --- destructive: manual script invocation ------------------------------
# These overwrite the state files, so they go last.


def test_notify_actions_execute_on_transition(host):
    """The rendered notify actions actually run on a transition.

    Leaves "error" in the instance state file and, on an instance with
    vmac_quiet_backup, quiets the virtual MAC exactly as a real transition
    would, so it must run after every test that reads either.
    """
    host.run("rm -f " + STATE_DIR + "/vmac_fault_action")
    result = host.run("/etc/keepalived/notify_vmac.sh INSTANCE vmac FAULT")
    assert result.rc == 0, (
        "notify script failed on FAULT:\n" + result.stdout + result.stderr
    )
    assert host.file(STATE_DIR + "/vmac_fault_action").exists, \
        "the fault notify_action did not run"
    assert _state(host, VMAC) == "error"


def test_notify_master_action_executes(host):
    """The master notify action runs too, and writes its marker."""
    host.run("rm -f " + STATE_DIR + "/vmac_master_action")
    result = host.run("/etc/keepalived/notify_vmac.sh INSTANCE vmac MASTER")
    assert result.rc == 0, (
        "notify script failed on MASTER:\n" + result.stdout + result.stderr
    )
    assert host.file(STATE_DIR + "/vmac_master_action").exists, \
        "the master notify_action did not run"
    assert _state(host, VMAC) == "leader"


# --- rendered scripts must be clean -------------------------------------


def test_shellcheck_rendered_scripts(host):
    """Every script this scenario renders passes shellcheck."""
    failures = []
    for script in RENDERED_SCRIPTS:
        assert host.file(script).exists, "missing rendered script " + script
        result = host.run("shellcheck -S warning " + script)
        if result.rc != 0:
            failures.append(script + ":\n" + result.stdout + result.stderr)
    assert not failures, "shellcheck failed:\n" + "\n".join(failures)
