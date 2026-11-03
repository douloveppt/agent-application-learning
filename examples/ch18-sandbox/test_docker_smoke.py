"""No daemon calls: protocol and cleanup tests use a strict fake Docker CLI."""
from contextlib import redirect_stderr, redirect_stdout
import errno
import io
import json
import socket
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import docker_smoke as smoke


IMAGE = "sha256:" + "a" * 64
CONTAINER_ID = "b" * 64
CONTEXT = "verified-local"


def completed(arguments, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(arguments, returncode, stdout=stdout, stderr=stderr)


def run_fixed_probe(*, root_errno=errno.EROFS, interface_flags=None,
                    ipv4_errno=errno.ENETUNREACH, ipv6_errno=errno.ENETUNREACH,
                    ipv6_socket_errno=None):
    """Execute reviewed fixed code with all filesystem/socket operations mocked."""
    flags_by_name = {"lo": 9} if interface_flags is None else interface_flags

    class FakePath:
        def __init__(self, value):
            self.value = value

        def exists(self):
            return False

        def read_text(self):
            if self.value == "/proc/self/status":
                return "NoNewPrivs:\t1\nCapEff:\t00000000\n"
            if self.value.endswith("/flags"):
                value = flags_by_name[self.value.split("/")[-2]]
                if isinstance(value, Exception):
                    raise value
                return hex(value)
            return "ok"

        def write_text(self, value):
            if self.value == "/ch18-root-probe":
                raise OSError(root_errno, "fixed test fixture")

    def socket_factory(family, kind):
        if family == socket.AF_INET6 and ipv6_socket_errno is not None:
            raise OSError(ipv6_socket_errno, "fixed IPv6 socket fixture")
        client = MagicMock()
        client.__enter__.return_value = client
        code = ipv6_errno if family == socket.AF_INET6 else ipv4_errno
        client.connect.side_effect = OSError(code, "fixed route fixture") if code is not None else None
        return client

    printed = []
    exit_code = None
    with patch("pathlib.Path", FakePath), patch("os.geteuid", return_value=65532), \
            patch("os.listdir", return_value=list(flags_by_name)), \
            patch("socket.if_nameindex", return_value=list(enumerate(flags_by_name, start=1))), \
            patch("socket.socket", side_effect=socket_factory), \
            patch("builtins.print", side_effect=lambda value, **kwargs: printed.append((value, kwargs))):
        try:
            exec(smoke.PROBE, {})
        except SystemExit as exc:
            exit_code = exc.code
    evidence = json.loads(next(value for value, kwargs in printed if "file" not in kwargs))
    diagnostics = [json.loads(value) for value, kwargs in printed if "file" in kwargs]
    return evidence, exit_code, diagnostics


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.endpoint = "unix:///verified/docker.sock"
        self.evidence = {key: True for key in smoke.PROBE_KEYS}
        self.owner_mismatch = False
        self.id_mismatch = False
        self.creation_unknown = False
        self.healthcheck = ["NONE"]
        self.name = None
        self.owner = None

    def __call__(self, arguments, timeout=15, *, context):
        self.calls.append((arguments, context))
        if arguments[:2] == ["context", "inspect"]:
            return completed(arguments, json.dumps([{
                "Name": CONTEXT, "Endpoints": {"docker": {"Host": self.endpoint}}
            }]))
        if arguments[:2] == ["image", "inspect"]:
            return completed(arguments, json.dumps([{"Os": "linux"}]))
        if arguments[0] == "create":
            self.name = arguments[arguments.index("--name") + 1]
            self.owner = arguments[arguments.index("--label") + 1].split("=", 1)[1]
            if self.creation_unknown:
                raise subprocess.TimeoutExpired(arguments, timeout)
            return completed(arguments, CONTAINER_ID + "\n")
        if arguments[0] == "inspect":
            spec = {
                "Id": "c" * 64 if self.id_mismatch else CONTAINER_ID, "Name": "/" + self.name,
                "Config": {"User": "65532:65532", "Labels": {
                    smoke.OWNER_LABEL: "other-owner" if self.owner_mismatch else self.owner,
                }, "Healthcheck": {"Test": self.healthcheck}},
                "HostConfig": {"Binds": None, "NetworkMode": "none", "ReadonlyRootfs": True,
                               "Memory": 128 * 1024 * 1024, "PidsLimit": 32,
                               "NanoCpus": 500_000_000},
                "Mounts": [],
            }
            return completed(arguments, json.dumps([spec]))
        if arguments[0] == "start":
            return completed(arguments, json.dumps(self.evidence))
        if arguments[0] == "rm" or arguments[:2] == ["container", "ls"]:
            return completed(arguments)
        raise AssertionError("unexpected Docker command: " + repr(arguments))


class DockerSmokeTest(unittest.TestCase):
    def run_main(self, fake):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["docker_smoke.py", "--context", CONTEXT, "--image", IMAGE]), \
                patch.object(smoke, "command", side_effect=fake), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = smoke.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_explicit_context_is_passed_as_global_docker_argument(self):
        with patch.object(smoke.subprocess, "run") as runner:
            smoke.command(["image", "ls"], context=CONTEXT)
        self.assertEqual(runner.call_args.args[0], ["docker", "--context", CONTEXT, "image", "ls"])

    def test_success_uses_verified_id_for_start_and_cleanup(self):
        fake = FakeDocker()
        code, stdout, _ = self.run_main(fake)
        self.assertEqual(code, 0)
        self.assertIn('"status": "probe_passed"', stdout)
        self.assertTrue(all(context == CONTEXT for _, context in fake.calls))
        self.assertIn((["start", "--attach", CONTAINER_ID], CONTEXT), fake.calls)
        self.assertIn((["rm", "--force", "--volumes", CONTAINER_ID], CONTEXT), fake.calls)
        creation = next(args for args, _ in fake.calls if args[0] == "create")
        self.assertIn("--no-healthcheck", creation)

    def test_missing_probe_key_cannot_pass(self):
        fake = FakeDocker()
        del fake.evidence["root_write_denied"]
        code, stdout, _ = self.run_main(fake)
        self.assertEqual(code, 1)
        self.assertNotIn('"status": "probe_passed"', stdout)

    def test_extra_key_and_non_boolean_evidence_rejected(self):
        for evidence in ({**{key: True for key in smoke.PROBE_KEYS}, "extra": True},
                         {**{key: True for key in smoke.PROBE_KEYS}, "non_root": 1}):
            fake = FakeDocker()
            fake.evidence = evidence
            with self.subTest(evidence=evidence):
                self.assertEqual(self.run_main(fake)[0], 1)

    def test_owner_mismatch_never_deletes_resource(self):
        fake = FakeDocker()
        fake.owner_mismatch = True
        code, stdout, _ = self.run_main(fake)
        self.assertEqual(code, 2)
        self.assertIn('"status": "cleanup_pending"', stdout)
        self.assertFalse(any(args[0] == "rm" for args, _ in fake.calls))

    def test_unknown_create_never_deletes_by_name(self):
        fake = FakeDocker()
        fake.creation_unknown = True
        code, stdout, _ = self.run_main(fake)
        self.assertEqual(code, 2)
        self.assertIn('"status": "cleanup_pending"', stdout)
        self.assertFalse(any(args[0] in {"rm", "inspect", "start"} for args, _ in fake.calls))

    def test_mismatched_container_id_never_deletes_resource(self):
        fake = FakeDocker()
        fake.id_mismatch = True
        self.assertEqual(self.run_main(fake)[0], 2)
        self.assertFalse(any(args[0] == "rm" for args, _ in fake.calls))

    def test_remote_context_rejected_before_image_or_create(self):
        for endpoint in ("ssh://production", "tcp://127.0.0.1:2375", "unix://other-host/socket"):
            fake = FakeDocker()
            fake.endpoint = endpoint
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.run_main(fake)[0], 1)
                self.assertEqual(len(fake.calls), 1)

    def test_enabled_healthcheck_rejected_before_start(self):
        fake = FakeDocker()
        fake.healthcheck = ["CMD", "unwanted-job"]
        self.assertEqual(self.run_main(fake)[0], 1)
        self.assertFalse(any(args[0] == "start" for args, _ in fake.calls))
        self.assertTrue(any(args[0] == "rm" for args, _ in fake.calls))

    def test_root_write_probe_requires_erofs_not_eacces(self):
        for error_number, expected in ((errno.EROFS, True), (errno.EACCES, False)):
            with self.subTest(error_number=error_number):
                evidence, code, _ = run_fixed_probe(root_errno=error_number)
                self.assertIs(evidence["root_write_denied"], expected)
                self.assertEqual(code, 0 if expected else 1)

    def test_down_fallback_tunnels_do_not_fail_active_interface_property(self):
        evidence, code, _ = run_fixed_probe(interface_flags={"lo": 9, "gre0": 128, "erspan0": 4098})
        self.assertIs(evidence["no_active_nonloopback"], True)
        self.assertEqual(code, 0)

    def test_up_nonloopback_interface_rejected(self):
        evidence, code, _ = run_fixed_probe(interface_flags={"lo": 9, "eth0": 0x1003})
        self.assertIs(evidence["no_active_nonloopback"], False)
        self.assertEqual(code, 1)

    def test_unknown_interface_flags_rejected(self):
        evidence, code, _ = run_fixed_probe(interface_flags={"lo": 9, "gre0": OSError(errno.EIO, "fixture")})
        self.assertIs(evidence["no_active_nonloopback"], False)
        self.assertEqual(code, 1)

    def test_excess_interface_count_rejected(self):
        evidence, code, _ = run_fixed_probe(interface_flags={"if" + str(i): 128 for i in range(17)})
        self.assertIs(evidence["no_active_nonloopback"], False)
        self.assertEqual(code, 1)

    def test_unexpected_ipv6_error_or_success_does_not_prove_route_denial(self):
        for failure in (errno.EINVAL, None):
            with self.subTest(failure=failure):
                evidence, code, _ = run_fixed_probe(ipv6_errno=failure)
                self.assertIs(evidence["ipv6_external_route_denied"], False)
                self.assertEqual(code, 1)

    def test_explicit_ipv6_family_unavailable_is_reported(self):
        evidence, code, diagnostics = run_fixed_probe(ipv6_socket_errno=errno.EAFNOSUPPORT)
        self.assertIs(evidence["ipv6_external_route_denied"], True)
        self.assertEqual(code, 0)
        self.assertIn({"ipv6_diagnostics": {"status": "address_family_unavailable",
                                           "errno": errno.EAFNOSUPPORT}}, diagnostics)

    def test_unexpected_ipv4_error_cannot_prove_route_denial(self):
        evidence, code, _ = run_fixed_probe(ipv4_errno=errno.EINVAL)
        self.assertIs(evidence["external_route_denied"], False)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
