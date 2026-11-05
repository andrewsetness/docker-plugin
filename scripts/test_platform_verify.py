"""Test Platform verifier for the docker-plugin on-demand E2E suite.

The suite is Jenkins-owned and credential-free. It requires a Java 21+
runtime, provisions a pinned Maven distribution when one is not already
available, runs the repository formatter and the complete Maven test suite,
and writes the eight approved evidence artifacts under
``test-platform-evidence/docker-plugin/``.

Repository tests that need a Docker daemon self-skip through Testcontainers
when no daemon is reachable. The suite records those skips rather than
treating them as passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = ROOT / "test-platform-evidence" / "docker-plugin"
SUITE_ID = "docker-plugin-verify"
REPOSITORY = "andrewsetness/docker-plugin"
MAVEN_VERSION = "3.9.9"
MAVEN_SHA256 = "4ec3f26fb1a692473aea0235c300bd20f0f9fe741947c82c1234cefd76ac3a3c"
MAVEN_URL = (
    "https://archive.apache.org/dist/maven/maven-3/"
    f"{MAVEN_VERSION}/binaries/apache-maven-{MAVEN_VERSION}-bin.zip"
)
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_JSON_BYTES = 256 * 1024
MAX_JUNIT_BYTES = 8 * 1024 * 1024
ARTIFACT_NAMES = (
    "suite.log",
    "quality-static.json",
    "quality-deterministic.json",
    "quality-contract.json",
    "quality-integration.json",
    "quality-end-to-end.json",
    "quality-adversarial-security.json",
    "tests.xml",
)

CONTRACT_CLASSES = (
    "ConfigurationAsCodeTest",
    "DockerClientParametersTest",
    "DockerTemplateBaseTest",
    "DockerImagePullStrategyTest",
    "ContainerNodeNameMapTest",
    "HostPortComparatorTest",
    "DockerEnvUtilsTest",
)
INTEGRATION_CLASSES = (
    "DockerAPITest",
    "DockerCloudTest",
    "DockerComputerAttachConnectorTest",
    "DockerComputerJNLPConnectorTest",
    "DockerComputerSSHConnectorTest",
    "DockerNodeStepExecutionTest",
    "DockerNodeStepTest",
    "DockerAgentTest",
    "JenkinsUtilsTest",
    "FastNodeProvisionerStrategyTest",
)
END_TO_END_CLASSES = (
    "DockerCloudTest",
    "DockerAPITest",
    "DockerNodeStepExecutionTest",
    "DockerNodeStepTest",
    "DockerAgentTest",
)
SECURITY_CLASSES = (
    "DockerDisabledTest",
    "PortUtilsTest",
    "DockerClientParametersTest",
    "DockerContainerWatchdogTest",
    "UsageTrackingCacheTest",
)

SENSITIVE_ENV_NAME = re.compile(
    r"(?i)(token|password|secret|api[_-]?key|private[_-]?key|authorization|credential)"
)
REDACTION_PATTERNS = (
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)(\b(?:token|password|secret|api[_-]?key|access[_-]?key|"
            r"private[_-]?key|authorization|credential)\b\s*[:=]\s*)[^\s,;]+"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
            r"sk-(?:proj-)?[A-Za-z0-9_-]{20,})\b"
        ),
        "[REDACTED]",
    ),
)


def sanitize_text(value: str) -> str:
    for pattern, replacement in REDACTION_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def safe_evidence_root() -> Path:
    """Create the exact evidence path while rejecting symlinked components."""
    current = ROOT
    for part in ("test-platform-evidence", "docker-plugin"):
        current = current / part
        if current.exists() and current.is_symlink():
            raise RuntimeError(f"evidence path component is a symlink: {part}")
        current.mkdir(exist_ok=True)
    resolved = current.resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        raise RuntimeError("evidence path resolves outside the repository")
    return current


def artifact_path(evidence: Path, name: str) -> Path:
    if evidence != EVIDENCE_ROOT or name not in ARTIFACT_NAMES:
        raise RuntimeError("artifact path is outside the approved evidence set")
    current = ROOT
    for part in ("test-platform-evidence", "docker-plugin"):
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise RuntimeError(f"evidence path component is not a real directory: {part}")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(ROOT.resolve(strict=True)):
        raise RuntimeError("evidence directory resolves outside the repository")
    path = current / name
    if path.is_symlink():
        raise RuntimeError(f"refusing to follow a symlinked evidence artifact: {name}")
    if path.exists() and not path.is_file():
        raise RuntimeError(f"evidence artifact path is not a regular file: {name}")
    return path


def clean_old_artifacts(evidence: Path) -> None:
    for name in ARTIFACT_NAMES:
        path = artifact_path(evidence, name)
        if path.is_file():
            path.unlink()


def append_log(evidence: Path, message: str) -> None:
    path = artifact_path(evidence, "suite.log")
    remaining = MAX_LOG_BYTES - (path.stat().st_size if path.exists() else 0)
    if remaining <= 0:
        return
    encoded = (sanitize_text(message.rstrip()) + "\n").encode("utf-8", errors="replace")
    with path.open("ab") as stream:
        stream.write(encoded[:remaining])


def sanitized_environment(java_home: str) -> dict[str, str]:
    """Pass only runtime essentials; omit provider, cloud, and feature-flag env."""
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "MAVEN_OPTS",
    }
    result = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in allowed and not SENSITIVE_ENV_NAME.search(name)
    }
    result["JAVA_HOME"] = java_home
    result.setdefault("MAVEN_OPTS", "-Djava.awt.headless=true")
    result["PYTHONUTF8"] = "1"
    result["PYTHONNOUSERSITE"] = "1"
    return result


def source_sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else ""


def working_tree_clean() -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and not completed.stdout.strip()


def run_step(
    evidence: Path,
    name: str,
    argv: list[str],
    display: str,
    env: dict[str, str],
    *,
    timeout: int,
) -> dict[str, Any]:
    started = time.monotonic()
    append_log(evidence, f"\n== {name} ==\n$ {display}")
    try:
        completed = subprocess.run(
            argv,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = completed.stdout or ""
        code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        raw_output = exc.stdout or ""
        output = (
            raw_output.decode("utf-8", errors="replace")
            if isinstance(raw_output, bytes)
            else raw_output
        )
        output += f"\nTimed out after {timeout} seconds."
        code = 124
    except OSError as exc:
        output = f"Could not start approved local tool: {type(exc).__name__}."
        code = 127
    append_log(evidence, output)
    status = "pass" if code == 0 else "fail"
    result: dict[str, Any] = {
        "name": name,
        "status": status,
        "exit_code": code,
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    print(f"{status.upper()} {name} ({result['duration_seconds']}s)", flush=True)
    return result


def java_runtime(env: dict[str, str]) -> tuple[str, str, str]:
    """Return (java executable, JAVA_HOME, version) and require Java 21+."""
    executable = shutil.which("java")
    home = os.environ.get("JAVA_HOME", "")
    if home:
        candidate = Path(home) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if candidate.is_file():
            executable = str(candidate)
    if not executable:
        raise RuntimeError(
            "Java 21+ is required; set JAVA_HOME or put java on PATH"
        )
    completed = subprocess.run(
        [executable, "-XshowSettings:properties", "-version"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    version_match = re.search(r"java\.version = (\S+)", completed.stdout)
    home_match = re.search(r"java\.home = (.+)", completed.stdout)
    if not version_match or not home_match:
        raise RuntimeError("Could not determine the Java runtime version")
    version = version_match.group(1).strip()
    major_match = re.match(r"(\d+)", version)
    if not major_match or int(major_match.group(1)) < 21:
        raise RuntimeError(f"Java 21+ is required; found {version}")
    return executable, home_match.group(1).strip(), version


def maven_home(temp_root: Path, explicit_home: str | None) -> Path:
    """Return a Maven home from MAVEN_HOME, PATH, or the pinned download."""
    homes: list[str] = []
    if explicit_home:
        homes.append(explicit_home)
    if os.environ.get("MAVEN_HOME"):
        homes.append(os.environ["MAVEN_HOME"])
    on_path = shutil.which("mvn")
    if on_path:
        homes.append(str(Path(on_path).resolve().parent.parent))
    for home in homes:
        candidate = Path(home)
        if (candidate / "boot").is_dir() and (candidate / "bin" / "m2.conf").is_file():
            return candidate

    archive = temp_root / f"apache-maven-{MAVEN_VERSION}-bin.zip"
    urllib.request.urlretrieve(MAVEN_URL, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != MAVEN_SHA256:
        raise RuntimeError("Provisioned Maven archive failed its SHA-256 check")
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(temp_root)
    return temp_root / f"apache-maven-{MAVEN_VERSION}"


def maven_argv(
    java_executable: str, maven_home_path: Path, settings: Path, args: list[str]
) -> list[str]:
    """Invoke Maven's launcher directly so a spaced path never enters a shell."""
    classworlds = sorted((maven_home_path / "boot").glob("plexus-classworlds-*.jar"))
    if not classworlds:
        raise RuntimeError("Maven distribution has no classworlds launcher")
    return [
        java_executable,
        "-classpath",
        str(classworlds[0]),
        f"-Dclassworlds.conf={maven_home_path / 'bin' / 'm2.conf'}",
        f"-Dmaven.home={maven_home_path}",
        f"-Dmaven.multiModuleProjectDirectory={ROOT}",
        "org.codehaus.plexus.classworlds.launcher.Launcher",
        "-s",
        str(settings),
        "-B",
        "-ntp",
        *args,
    ]


def parse_surefire(path: Path) -> list[dict[str, Any]]:
    root = ET.parse(path).getroot()
    cases: list[dict[str, Any]] = []
    for node in root.iter("testcase"):
        classname = node.attrib.get("classname", "")
        name = node.attrib.get("name", "")
        failed = any(child.tag in {"failure", "error"} for child in node)
        skipped = any(child.tag == "skipped" for child in node)
        cases.append(
            {
                "classname": classname,
                "name": name,
                "id": f"{classname}::{name}" if classname else name,
                "failed": failed,
                "skipped": skipped,
            }
        )
    return cases


def combine_junit(paths: list[Path], destination: Path) -> list[dict[str, Any]]:
    destination = artifact_path(destination.parent, destination.name)
    combined = ET.Element("testsuites")
    all_cases: list[dict[str, Any]] = []
    tests = failures = errors = skipped = 0
    elapsed = 0.0
    for path in paths:
        cases, root = parse_surefire(path), ET.parse(path).getroot()
        all_cases.extend(cases)
        children = list(root) if root.tag == "testsuites" else [root]
        for child in children:
            combined.append(child)
            tests += int(child.attrib.get("tests", "0"))
            failures += int(child.attrib.get("failures", "0"))
            errors += int(child.attrib.get("errors", "0"))
            skipped += int(child.attrib.get("skipped", "0"))
            try:
                elapsed += float(child.attrib.get("time", "0"))
            except ValueError:
                pass
    combined.attrib.update(
        tests=str(tests),
        failures=str(failures),
        errors=str(errors),
        skipped=str(skipped),
        time=f"{elapsed:.3f}",
    )
    ET.ElementTree(combined).write(destination, encoding="utf-8", xml_declaration=True)
    if destination.stat().st_size > MAX_JUNIT_BYTES:
        raise RuntimeError("JUnit report exceeds the approved 8 MiB artifact limit")
    return all_cases


def class_cases(cases: list[dict[str, Any]], names: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        case
        for case in cases
        if any(case["classname"].endswith(name) for name in names)
    ]


def report(
    evidence_class: str,
    cases: list[dict[str, Any]],
    *,
    source: str,
    checks: dict[str, str] | None = None,
    forced_status: str | None = None,
) -> dict[str, Any]:
    failed = sum(bool(case["failed"]) for case in cases)
    skipped = sum(bool(case["skipped"]) for case in cases)
    status = forced_status or ("pass" if cases and failed == 0 else "fail")
    if checks is not None and any(value != "pass" for value in checks.values()):
        status = "fail"
    result: dict[str, Any] = {
        "schema_version": "1",
        "repository": REPOSITORY,
        "source_sha": source,
        "suite_id": SUITE_ID,
        "evidence_class": evidence_class,
        "status": status,
        "test_count": len(cases),
        "failed_count": failed,
        "skipped_count": skipped,
        "test_ids": [case["id"] for case in cases],
    }
    if checks is not None:
        result["checks"] = checks
    return result


def write_json(evidence: Path, name: str, value: dict[str, Any]) -> None:
    path = artifact_path(evidence, name)
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise RuntimeError(f"JSON evidence exceeds the approved 256 KiB limit: {name}")
    path.write_bytes(encoded)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-dirty-local",
        action="store_true",
        help="allow a local uncommitted candidate; Jenkins must use a clean checkout",
    )
    parser.add_argument(
        "--maven-home",
        default=None,
        help="Maven home directory; the script provisions a pinned Maven otherwise",
    )
    args = parser.parse_args()
    if sys.version_info[:2] < (3, 10):
        print("The docker-plugin Test Platform wrapper requires Python 3.10+", file=sys.stderr)
        return 2

    source = source_sha()
    if not source:
        print("Could not determine the checkout source SHA", file=sys.stderr)
        return 2
    clean_checkout = working_tree_clean()
    if not clean_checkout and not args.allow_dirty_local:
        print(
            "Refusing dirty checkout; use --allow-dirty-local only for local candidate verification",
            file=sys.stderr,
        )
        return 2

    try:
        evidence = safe_evidence_root()
        clean_old_artifacts(evidence)
        java_executable, java_home, java_version = java_runtime(dict(os.environ))
    except (OSError, RuntimeError) as exc:
        print(f"Test Platform preflight failed: {exc}", file=sys.stderr)
        return 2

    env = sanitized_environment(java_home)
    env["PATH"] = os.pathsep.join(
        [str(Path(java_home) / "bin"), env.get("PATH", "")]
    ).rstrip(os.pathsep)

    append_log(
        evidence,
        "\n".join(
            (
                f"repository={REPOSITORY}",
                f"suite={SUITE_ID}",
                f"source_sha={source}",
                f"checkout_clean={str(clean_checkout).lower()}",
                f"local_dirty_override={str(args.allow_dirty_local).lower()}",
                f"java={java_version}",
                "network=not requested by tests; Maven dependency resolution uses the configured mirror",
                "provider credentials and integration flags removed",
            )
        ),
    )
    print(f"docker-plugin E2E suite; source {source}; clean={clean_checkout}", flush=True)

    step_results: dict[str, dict[str, Any]] = {}
    overall_started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="docker-plugin-test-platform-") as temp_name:
        temp_root = Path(temp_name)
        settings = temp_root / "settings.xml"
        settings.write_text(
            '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"></settings>\n',
            encoding="utf-8",
        )
        try:
            maven_distribution = maven_home(temp_root, args.maven_home)
            version_run = subprocess.run(
                maven_argv(java_executable, maven_distribution, settings, ["-version"]),
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            match = re.search(r"Apache Maven (\S+)", version_run.stdout or "")
            maven_version = match.group(1) if match else "unknown"
        except (OSError, RuntimeError) as exc:
            print(f"Test Platform preflight failed: {exc}", file=sys.stderr)
            return 2
        append_log(evidence, f"maven={maven_version}")

        spotless = run_step(
            evidence,
            "maven-spotless-check",
            maven_argv(java_executable, maven_distribution, settings, ["spotless:check"]),
            "mvn -s <isolated-settings> -B -ntp spotless:check",
            env,
            timeout=900,
        )
        step_results[spotless["name"]] = spotless

        shutil.rmtree(ROOT / "target" / "surefire-reports", ignore_errors=True)
        tests = run_step(
            evidence,
            "maven-test",
            maven_argv(java_executable, maven_distribution, settings, ["test"]),
            "mvn -s <isolated-settings> -B -ntp test",
            env,
            timeout=3000,
        )
        step_results[tests["name"]] = tests

        reports = sorted((ROOT / "target" / "surefire-reports").glob("TEST-*.xml"))
        try:
            cases = combine_junit(reports, evidence / "tests.xml")
        except (OSError, ET.ParseError, RuntimeError) as exc:
            append_log(evidence, f"JUnit aggregation failed: {type(exc).__name__}: {exc}")
            cases = []
            step_results["junit-aggregation"] = {
                "name": "junit-aggregation",
                "status": "fail",
                "exit_code": 1,
                "duration_seconds": 0.0,
            }

    checks = {
        "maven-spotless-check": step_results["maven-spotless-check"]["status"],
        "maven-test": step_results["maven-test"]["status"],
        "java-21": "pass" if int(re.match(r"(\d+)", java_version).group(1)) >= 21 else "fail",
        "maven-present": "pass" if maven_version else "fail",
    }
    test_steps_pass = checks["maven-spotless-check"] == "pass" and checks["maven-test"] == "pass"
    if "junit-aggregation" in step_results:
        test_steps_pass = False

    reports_by_class = {
        "quality-deterministic.json": report(
            "deterministic", cases, source=source, forced_status="pass" if test_steps_pass else "fail"
        ),
        "quality-contract.json": report(
            "contract", class_cases(cases, CONTRACT_CLASSES), source=source
        ),
        "quality-integration.json": report(
            "integration", class_cases(cases, INTEGRATION_CLASSES), source=source
        ),
        "quality-end-to-end.json": report(
            "end-to-end", class_cases(cases, END_TO_END_CLASSES), source=source
        ),
        "quality-adversarial-security.json": report(
            "adversarial-security", class_cases(cases, SECURITY_CLASSES), source=source
        ),
    }
    static_status = "pass" if all(value == "pass" for value in checks.values()) else "fail"
    reports_by_class["quality-static.json"] = {
        "schema_version": "1",
        "repository": REPOSITORY,
        "source_sha": source,
        "suite_id": SUITE_ID,
        "evidence_class": "static",
        "status": static_status,
        "test_count": 0,
        "failed_count": 0 if static_status == "pass" else sum(v != "pass" for v in checks.values()),
        "skipped_count": 0,
        "test_ids": [],
        "checks": checks,
    }

    for name, value in reports_by_class.items():
        try:
            write_json(evidence, name, value)
        except (OSError, RuntimeError) as exc:
            append_log(evidence, f"Could not write report {name}: {type(exc).__name__}: {exc}")
            return 1

    summary = {
        "source_sha": source,
        "working_tree_clean": clean_checkout,
        "elapsed_seconds": round(time.monotonic() - overall_started, 3),
        "steps": step_results,
        "test_case_count": len(cases),
        "failed_case_count": sum(bool(case["failed"]) for case in cases),
        "skipped_case_count": sum(bool(case["skipped"]) for case in cases),
        "evidence_statuses": {name.removesuffix(".json"): value["status"] for name, value in reports_by_class.items()},
    }
    append_log(evidence, json.dumps(summary, sort_keys=True))
    passed = all(value["status"] == "pass" for value in reports_by_class.values())
    print(
        f"docker-plugin E2E {'PASS' if passed else 'FAIL'}: "
        f"{len(cases)} cases, {summary['failed_case_count']} failed, {summary['skipped_case_count']} skipped; "
        f"{summary['elapsed_seconds']}s",
        flush=True,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
