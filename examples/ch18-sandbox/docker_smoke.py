"""Optional fixed Linux-container probes. No host mounts, shell, or image pull.

This is a smoke test for a local Docker configuration, not a hostile-code runner.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import uuid4


PROBE_KEYS = frozenset({
    "non_root", "no_new_privileges", "capabilities_dropped", "no_docker_socket",
    "root_write_denied", "workspace_writable", "no_active_nonloopback",
    "external_route_denied", "ipv6_external_route_denied",
})
OWNER_LABEL = "org.agent-learning.ch18.owner"


PROBE = r'''
import errno, json, os, pathlib, socket, sys
results = {}
results["non_root"] = os.geteuid() == 65532
status = pathlib.Path("/proc/self/status").read_text()
values = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
results["no_new_privileges"] = values["NoNewPrivs"].strip() == "1"
results["capabilities_dropped"] = int(values["CapEff"].strip(), 16) == 0
results["no_docker_socket"] = not pathlib.Path("/var/run/docker.sock").exists()
try:
    pathlib.Path("/ch18-root-probe").write_text("unexpected")
    results["root_write_denied"] = False
except OSError as exc:
    results["root_write_denied"] = exc.errno == errno.EROFS
pathlib.Path("/work/probe.txt").write_text("ok")
results["workspace_writable"] = pathlib.Path("/work/probe.txt").read_text() == "ok"
interface_flags = {}
flags_errors = {}
socket_interfaces = []
interface_enumeration_error = None
try:
    socket_interfaces = [name for _, name in socket.if_nameindex()]
    for name in socket_interfaces[:16]:
        try:
            flags = int(pathlib.Path("/sys/class/net/" + name + "/flags").read_text().strip(), 0)
            if flags < 0 or flags > 0xffffffff:
                raise ValueError("invalid flags range")
            interface_flags[name] = flags
        except (OSError, ValueError) as exc:
            flags_errors[name] = type(exc).__name__
except OSError as exc:
    interface_enumeration_error = type(exc).__name__
results["no_active_nonloopback"] = (
    bool(socket_interfaces) and len(socket_interfaces) <= 16
    and interface_enumeration_error is None and not flags_errors
    and len(interface_flags) == len(socket_interfaces)
    and not any((flags & 0x1) and not (flags & 0x8) for flags in interface_flags.values())
)
route_denied_errors = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EACCES, errno.EPERM}
try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        try:
            client.connect(("192.0.2.1", 9))
            results["external_route_denied"] = False
        except OSError as exc:
            results["external_route_denied"] = exc.errno in route_denied_errors
except OSError:
    results["external_route_denied"] = False
ipv6_diagnostic = None
try:
    ipv6_client = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
except OSError as exc:
    unavailable = exc.errno in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT}
    results["ipv6_external_route_denied"] = unavailable
    ipv6_diagnostic = {"status": "address_family_unavailable" if unavailable else "socket_error",
                       "errno": exc.errno}
else:
    with ipv6_client as client:
        try:
            client.connect(("2001:db8::1", 9))
            results["ipv6_external_route_denied"] = False
        except OSError as exc:
            results["ipv6_external_route_denied"] = exc.errno in route_denied_errors
            ipv6_diagnostic = {"status": "route_denied" if results["ipv6_external_route_denied"]
                               else "unexpected_connect_error", "errno": exc.errno}
if ipv6_diagnostic is not None:
    print(json.dumps({"ipv6_diagnostics": ipv6_diagnostic}, sort_keys=True),
          file=sys.stderr, flush=True)
print(json.dumps(results, sort_keys=True), flush=True)
if not all(results.values()):
    # Names/flags only: no interface addresses, routes, environment or credentials.
    try:
        sysfs_interfaces = os.listdir("/sys/class/net")
        diagnostics = {"sysfs_interface_names": sorted(sysfs_interfaces)[:16],
                       "sysfs_names_truncated": len(sysfs_interfaces) > 16}
    except OSError as exc:
        diagnostics = {"sysfs_interface_error_type": type(exc).__name__}
    diagnostics["socket_interface_names"] = sorted(socket_interfaces)[:16]
    diagnostics["socket_names_truncated"] = len(socket_interfaces) > 16
    diagnostics["socket_interface_flags"] = interface_flags
    if flags_errors:
        diagnostics["interface_flags_error_types"] = flags_errors
    if interface_enumeration_error:
        diagnostics["socket_interface_error_type"] = interface_enumeration_error
    print(json.dumps({"interface_diagnostics": diagnostics}, sort_keys=True),
          file=sys.stderr, flush=True)
raise SystemExit(0 if all(results.values()) else 1)
'''


def command(arguments: list[str], timeout: int = 15, *, context: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "--context", context, *arguments], check=False, text=True,
                          capture_output=True, timeout=timeout)


def validate_probe_evidence(evidence: object) -> bool:
    return (isinstance(evidence, dict) and set(evidence) == PROBE_KEYS
            and all(type(value) is bool and value for value in evidence.values()))


def inspect_owned(container_id: str, name: str, owner: str, *, context: str) -> dict:
    actual = command(["inspect", container_id], context=context)
    if actual.returncode:
        raise RuntimeError("cannot inspect created container")
    decoded = json.loads(actual.stdout)
    if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict):
        raise RuntimeError("invalid container identity receipt")
    spec = decoded[0]
    config = spec.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (spec.get("Id") != container_id or spec.get("Name") != "/" + name
            or not isinstance(labels, dict) or labels.get(OWNER_LABEL) != owner):
        raise RuntimeError("container identity or ownership mismatch")
    return spec


def cleanup_owned(container_id: str, name: str, owner: str, *, context: str) -> bool:
    # Never delete by reusable name; recheck the returned immutable ID and owner.
    inspect_owned(container_id, name, owner, context=context)
    removal = command(["rm", "--force", "--volumes", container_id], context=context)
    if removal.returncode:
        return False
    lookup = command(["container", "ls", "--all", "--quiet", "--no-trunc",
                      "--filter", "id=" + container_id], context=context)
    return lookup.returncode == 0 and not lookup.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True,
                        help="explicitly verified local Unix-socket Docker context")
    parser.add_argument("--image", required=True,
                        help="local Python Linux image ID: sha256:<64 hex>; never pulled")
    options = parser.parse_args()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", options.image):
        parser.error("use an immutable local sha256 image ID, not a tag")
    owner = uuid4().hex
    name = "ch18-probe-" + owner
    created = False
    container_id = None
    result = "failed"
    try:
        selected = command(["context", "inspect", options.context], context=options.context)
        if selected.returncode:
            raise RuntimeError("cannot inspect selected Docker context")
        context_metadata = json.loads(selected.stdout)
        if (not isinstance(context_metadata, list) or len(context_metadata) != 1
                or context_metadata[0].get("Name") != options.context):
            raise RuntimeError("selected context identity mismatch")
        endpoint = context_metadata[0]["Endpoints"]["docker"]["Host"]
        parsed_endpoint = urlsplit(endpoint)
        if (parsed_endpoint.scheme != "unix" or parsed_endpoint.netloc
                or not parsed_endpoint.path.startswith("/")
                or parsed_endpoint.query or parsed_endpoint.fragment):
            raise RuntimeError("only an explicitly verified local unix:// endpoint is admitted")
        print(json.dumps({"docker_context": options.context, "endpoint": endpoint}))
        inspected = command(["image", "inspect", options.image], context=options.context)
        if inspected.returncode:
            raise RuntimeError("image not present locally; no pull was attempted")
        metadata = json.loads(inspected.stdout)[0]
        if metadata.get("Os") != "linux":
            raise RuntimeError("a local Linux Python image is required")
        args = ["create", "--name", name, "--label", OWNER_LABEL + "=" + owner,
                "--pull=never", "--network=none",
                "--read-only", "--user=65532:65532", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--cpus=0.5", "--memory=128m",
                "--memory-swap=128m", "--pids-limit=32", "--ulimit=nofile=64:64",
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=8m,mode=1777",
                "--tmpfs=/work:rw,noexec,nosuid,nodev,size=1m,mode=1777",
                "--workdir=/work", "--log-driver=none", "--no-healthcheck", "--entrypoint=python3",
                options.image, "-I", "-c", PROBE]
        # Set before create: a CLI timeout could occur after daemon-side creation.
        created = True
        creation = command(args, context=options.context)
        if creation.returncode:
            raise RuntimeError("container creation failed: " + creation.stderr[:500])
        candidate_id = creation.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", candidate_id):
            raise RuntimeError("create returned no valid immutable container ID")
        container_id = candidate_id
        spec = inspect_owned(container_id, name, owner, context=options.context)
        host = spec["HostConfig"]
        checks = {
            "no_bind_mounts": not host.get("Binds"),
            "network_none": host.get("NetworkMode") == "none",
            "readonly_rootfs": host.get("ReadonlyRootfs") is True,
            "memory_128m": host.get("Memory") == 128 * 1024 * 1024,
            "pids_32": host.get("PidsLimit") == 32,
            "cpu_half": host.get("NanoCpus") == 500_000_000,
            "nonroot_user": spec["Config"].get("User") == "65532:65532",
            "healthcheck_disabled": spec["Config"].get("Healthcheck", {}).get("Test") == ["NONE"],
        }
        # Images declaring VOLUME can create anonymous mounts. Only the two
        # explicit tmpfs destinations are admitted if Docker lists them here.
        checks["only_declared_tmpfs"] = all(
            mount.get("Type") == "tmpfs" and mount.get("Destination") in {"/tmp", "/work"}
            for mount in spec.get("Mounts", [])
        )
        if not all(checks.values()):
            raise RuntimeError("effective container configuration rejected: " + json.dumps(checks))
        execution = command(["start", "--attach", container_id], timeout=15, context=options.context)
        if execution.stderr:
            print(execution.stderr[:2000], file=sys.stderr, end="")
        if execution.returncode:
            raise RuntimeError("fixed probe failed: " + (execution.stdout + execution.stderr)[:1000])
        evidence = json.loads(execution.stdout.strip())
        if not validate_probe_evidence(evidence):
            raise RuntimeError("fixed probe did not confirm every condition")
        print(json.dumps({"configuration": checks, "probes": evidence}, ensure_ascii=False))
        result = "probe_passed"
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError,
            RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)[:1000]}, ensure_ascii=False), file=sys.stderr)
    finally:
        if created:
            try:
                # With no acknowledged ID, do not delete a same-name resource
                # that cannot be bound to this create request.
                cleanup = bool(container_id) and cleanup_owned(
                    container_id, name, owner, context=options.context)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError,
                    subprocess.TimeoutExpired):
                cleanup = False
            print(json.dumps({"container": name, "container_id": container_id,
                              "owner_label": owner, "cleanup_confirmed": cleanup,
                              "status": result if cleanup else "cleanup_pending"}))
            if not cleanup:
                return 2
    return 0 if result == "probe_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
