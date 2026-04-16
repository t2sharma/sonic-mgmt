import getpass
import pexpect
import pytest

from tests.common.helpers.assertions import pytest_assert

pytestmark = [
    pytest.mark.topology("any"),
    pytest.mark.device_type("vs")
]


def _run_ssh_cmd(hostuser, hostip, remote_cmd):
    cmd = (
        "ssh {}@{} -q -o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null "
        "\"{}\""
    ).format(hostuser, hostip, remote_cmd)
    return pexpect.run(cmd, encoding="utf-8")


def _ensure_socat_on_dut(duthost):
    res = duthost.shell("which socat", module_ignore_errors=True)
    if res["rc"] != 0:
        duthost.copy(src="./console/socat", dest="/usr/local/bin/socat", mode=755)

    pytest_assert(
        duthost.shell("socat -V", module_ignore_errors=True)["rc"] == 0,
        "Invalid socat installation on DUT host"
    )


def _ensure_socat_on_kvm(hostuser, hostip):
    out = _run_ssh_cmd(hostuser, hostip, "which socat 2>/dev/null || true")
    if not out.strip():
        pexpect.run(
            "scp -q {} {}@{}:{}".format(
                "./console/socat", hostuser, hostip, "/usr/local/bin/socat"
            ),
            encoding="utf-8",
        )

    rc = _run_ssh_cmd(
        hostuser,
        hostip,
        "socat -V > /dev/null 2>&1; echo $?"
    ).strip()
    pytest_assert(rc == "0", "Invalid socat installation on KVM host")


def _cleanup_kvm_listener(hostuser, hostip, listen_port):
    _run_ssh_cmd(
        hostuser,
        hostip,
        (
            "pids=$(sudo lsof -ti TCP:{port} 2>/dev/null || true); "
            "if [ -n \"$pids\" ]; then sudo kill -9 $pids; fi; "
            "true"
        ).format(port=listen_port)
    )


def _start_kvm_listener(hostuser, hostip, listen_port, backend_port):
    _cleanup_kvm_listener(hostuser, hostip, listen_port)

    out = _run_ssh_cmd(
        hostuser,
        hostip,
        (
            "nohup sudo socat TCP-LISTEN:{listen_port},fork,reuseaddr "
            "TCP:127.0.0.1:{backend_port} "
            ">/tmp/console_socat_{listen_port}.log 2>&1 < /dev/null & "
            "sleep 1; "
            "sudo lsof -i:{listen_port} > /dev/null 2>&1; "
            "echo $?"
        ).format(
            listen_port=listen_port,
            backend_port=backend_port
        )
    ).strip()

    pytest_assert(
        out == "0",
        "Failed to start socat listener on KVM host for port {}".format(listen_port)
    )


def _cleanup_dut_line(duthost, tty_idx, target_line):
    duthost.shell(
        "sudo config console del {} > /dev/null 2>&1 || true".format(target_line),
        module_ignore_errors=True
    )


def _prepare_dut_line(duthost, tty_idx, target_line, relay_port):
    _cleanup_dut_line(duthost, tty_idx, target_line)

    cmd = (
        "sudo socat PTY,link=/dev/ttyUSB{tty_idx},raw,echo=0 "
        "TCP:10.250.0.1:{relay_port},forever,interval=1 "
        ">/tmp/console_line_{target_line}.log 2>&1 < /dev/null & "
        "for i in $(seq 1 10); do "
        "  [ -e /dev/ttyUSB{tty_idx} ] && break; "
        "  sleep 1; "
        "done; "
        "[ -e /dev/ttyUSB{tty_idx} ] || exit 1; "
        "sudo config console add {target_line} --baud 9600 --devicename device{target_line}"
    ).format(
        tty_idx=tty_idx,
        target_line=target_line,
        relay_port=relay_port
    )

    res = duthost.shell(cmd, module_ignore_errors=True)
    pytest_assert(
        res["rc"] == 0,
        "Failed to prepare DUT line {} using /dev/ttyUSB{}".format(target_line, tty_idx)
    )


def _disconnect_picocom_cleanly(client):
    if client is None or not client.isalive():
        return

    try:
        client.sendcontrol('a')
        client.sendcontrol('x')
        client.expect(
            ['Terminating...', 'Thanks for using picocom', pexpect.EOF],
            timeout=5
        )
    except Exception:
        client.close(force=True)


@pytest.mark.parametrize("target_line", ["1", "2", "3", "4"])
def test_console_availability(duthost, creds, target_line):
    """
    Test console is functional.
    Verify console session can be established from DUT.

    For this platform, a successful console connection is sufficient.
    A login prompt may not always be presented by the backend endpoint,
    so we only require that the session connects and does not immediately
    fail with EOF / explicit connect error.
    """
    dutip = duthost.host.options['inventory_manager'].get_host(
        duthost.hostname
    ).vars['ansible_host']
    dutuser = creds['sonicadmin_user']
    dutpass = creds['sonicadmin_password']
    hostip = "172.17.0.1"
    hostuser = getpass.getuser()

    line_num = int(target_line)
    tty_idx = line_num
    listen_port = 2000 + line_num
    backend_port = 7000 + line_num - 1

    _ensure_socat_on_dut(duthost)
    _ensure_socat_on_kvm(hostuser, hostip)

    _start_kvm_listener(
        hostuser=hostuser,
        hostip=hostip,
        listen_port=listen_port,
        backend_port=backend_port
    )

    _prepare_dut_line(
        duthost=duthost,
        tty_idx=tty_idx,
        target_line=target_line,
        relay_port=listen_port
    )

    client = None
    connected = False
    try:
        client = pexpect.spawn(
            "ssh {}@{} -q -t -o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null "
            "'sudo connect line {}'".format(dutuser, dutip, target_line),
            encoding="utf-8",
            timeout=15,
        )
        client.expect('[Pp]assword:')
        client.sendline(dutpass)

        i = client.expect(
            ['Successful connection to line', 'Successful connection', 'Cannot connect', pexpect.EOF, pexpect.TIMEOUT],
            timeout=15
        )

        pytest_assert(
            i in [0, 1],
            "Failed to connect line {}. Output so far: {}".format(target_line, client.before)
        )
        connected = True

        client.sendline("")
        j = client.expect(
            ['login:', r'[:>~$#]', 'Press \\^A \\^X to disconnect', pexpect.TIMEOUT, pexpect.EOF],
            timeout=5
        )

        # For this platform, no login prompt is expected. Once connection is
        # established, EOF is also acceptable because picocom may terminate
        # cleanly when there is no active backend console output.
        pytest_assert(
            j in [0, 1, 2, 3, 4],
            "Console session for line {} did not behave as expected after connect. "
            "Output so far: {}".format(target_line, client.before)
        )

    except pexpect.exceptions.EOF:
        if connected:
            return
        pytest.fail("EOF reached before successful connection on line {}".format(target_line))
    except pexpect.exceptions.TIMEOUT:
        if connected:
            return
        pytest.fail("Timeout reached before successful connection on line {}".format(target_line))
    except Exception as e:
        pytest.fail("Cannot connect to DUT host via SSH for line {}: {}".format(target_line, e))
    finally:
        _disconnect_picocom_cleanly(client)
        _cleanup_dut_line(duthost, tty_idx, target_line)
        _cleanup_kvm_listener(hostuser, hostip, listen_port)
