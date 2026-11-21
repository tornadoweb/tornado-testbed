#!/usr/bin/env python3
"""Reader and validator for the per-package definitions under packages/.

Each package is a directory, packages/<name>/, holding:

    requirements.txt  the pinned downstream version -- the single source of
                      truth for which release we test, and the file dependabot
                      updates. For install_method=pypi it is also what gets
                      installed; for a git checkout it is the version we derive
                      the tag from.
    package.env       strict key=value metadata (see REQUIRED_KEYS below).
                      Parsed, never sourced: the matrix job needs repo and
                      tag_template before any package's own code runs.
    test.sh           the test command, as a script.
    setup.sh          optional post-install hook.
    README.md         what the package exercises and why its suite is shaped
                      the way it is.
    constraints.lock  generated, never hand-edited: every other distribution in
                      the package's test environment, frozen from a refresh
                      run (see LOCK_MODE in run_one.sh). Applied as uv
                      constraints so drift arrives in one reviewable pull
                      request instead of in the weekly or release run.
    known_failures.toml
                      optional: downstream tests known to fail under some
                      Tornado or downstream versions, each tied to an upstream
                      issue and a condition. Applied by the pytest plugin in
                      scripts/pytest_plugins/ as strict xfails.
    patches/*.patch   optional: upstream fixes not yet released, each made
                      for exactly one pin and refused once the pin moves.

The git tag is derived, not stored: tag_template applied to the version in
requirements.txt. Storing both invites them to disagree.

Used from bash via the subcommands at the bottom, and directly by
validate_packages.py.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import shlex
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKGDEFS_DIR = ROOT / "packages"

REQUIRED_KEYS = {"repo", "tag_template", "subdir"}
OPTIONAL_KEYS = {
    "install_method",
    "pypi_extra_deps",
    "optional_dependency_group",
    "min_coverage",
    "apt_packages",
}
KNOWN_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS
INSTALL_METHODS = {"", "editable", "pypi"}

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
# An extra name from [project.optional-dependencies] (PEP 685 normalizes these).
EXTRA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# A single `name==version` requirement. Deliberately strict: these files exist
# to pin exactly one downstream release, and dependabot writes them back in
# this shape.
REQ_RE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[A-Za-z0-9._+!-]+)$")


# The lock file's name. Deliberately not *.txt: dependabot's pip updater reads
# every .txt file in a directory it watches as a requirements file, and would
# then open a pull request per locked transitive dependency -- exactly the
# noise the lock exists to avoid.
LOCK_NAME = "constraints.lock"
# Header lines that tie a lock to what it was generated for. A lock made for a
# different pin or interpreter is stale, not merely old: it can make the
# install unsatisfiable, or pin a dependency the new release no longer accepts.
LOCK_PIN_RE = re.compile(r"^# pin: (?P<pin>\S+)$")
LOCK_PYTHON_RE = re.compile(r"^# python: (?P<python>\d+\.\d+)$")

# Known failures: see packages/README.md#known_failurestoml.
KNOWN_FAILURES_NAME = "known_failures.toml"
KNOWN_FAILURE_REQUIRED = {"issue", "reason", "tests"}
# The conditions. An entry applies only while every condition it names holds,
# and must name at least one: an unconditional entry never expires.
KNOWN_FAILURE_CONDITIONS = {"tornado", "version"}
KNOWN_FAILURE_KEYS = KNOWN_FAILURE_REQUIRED | KNOWN_FAILURE_CONDITIONS | {"action"}
# xfail is the default and the norm: strict, so a test that starts passing
# turns the run red and the entry gets removed. deselect is only for tests that
# hang or are flaky, where an xfail would be a timeout or a coin toss.
KNOWN_FAILURE_ACTIONS = {"xfail", "deselect"}
# A pytest node id, or a prefix of one ending at a file, class or test name.
NODEID_RE = re.compile(r"^[^\s:\[\]]+\.py(::[A-Za-z_][A-Za-z0-9_]*)*$")

# Patches: see packages/README.md#patches. Header lines before the diff, which
# git apply ignores.
PATCHES_DIR = "patches"
PATCH_PIN_RE = re.compile(r"^# pin: (?P<pin>\S+)$")
PATCH_UPSTREAM_RE = re.compile(r"^# upstream: (?P<upstream>https://\S+)$")


def normalize(dist: str) -> str:
    """PEP 503 name normalization, for comparing distribution names."""
    return re.sub(r"[-_.]+", "-", dist).lower()


class PackageError(Exception):
    """A package definition is malformed."""


@dataclasses.dataclass(frozen=True)
class Package:
    name: str
    repo: str
    tag_template: str
    subdir: str
    install_method: str
    pypi_extra_deps: str
    optional_dependency_group: str
    min_coverage: int | None
    apt_packages: str
    dist_name: str
    version: str
    directory: pathlib.Path

    @property
    def ref(self) -> str:
        """The git ref to check out, derived from the pinned version."""
        return self.tag_template.replace("{version}", self.version)

    @property
    def requirements(self) -> pathlib.Path:
        return self.directory / "requirements.txt"

    @property
    def lock(self) -> pathlib.Path:
        return self.directory / LOCK_NAME

    @property
    def pin(self) -> str:
        return f"{self.dist_name}=={self.version}"

    @property
    def setup_hook(self) -> pathlib.Path | None:
        path = self.directory / "setup.sh"
        return path if path.exists() else None

    @property
    def known_failures(self) -> pathlib.Path | None:
        path = self.directory / KNOWN_FAILURES_NAME
        return path if path.exists() else None

    @property
    def patches(self) -> list[pathlib.Path]:
        """Patches to apply to the checkout, in the order they apply."""
        return sorted((self.directory / PATCHES_DIR).glob("*.patch"))

    def shell_env(self) -> str:
        """Shell assignments for `eval "$(pkglib.py env <name>)"`."""
        pairs = {
            "PKG_NAME": self.name,
            "PKG_REPO": self.repo,
            "PKG_REF": self.ref,
            "PKG_SUBDIR": self.subdir,
            "PKG_INSTALL_METHOD": self.install_method or "editable",
            "PKG_PYPI_EXTRA_DEPS": self.pypi_extra_deps,
            "PKG_OPTIONAL_DEPENDENCY_GROUP": self.optional_dependency_group,
            "PKG_MIN_COVERAGE": "" if self.min_coverage is None else str(self.min_coverage),
            "PKG_APT_PACKAGES": self.apt_packages,
            "PKG_VERSION": self.version,
            "PKG_DIST_NAME": self.dist_name,
            "PKG_DIR": str(self.directory),
            "PKG_REQUIREMENTS": str(self.requirements),
            "PKG_TEST_SH": str(self.directory / "test.sh"),
            "PKG_SETUP_SH": str(self.setup_hook or ""),
            "PKG_LOCK": str(self.lock),
            "PKG_KNOWN_FAILURES": str(self.known_failures or ""),
        }
        return "\n".join(f"{k}={shlex.quote(v)}" for k, v in pairs.items())


def parse_env(path: pathlib.Path) -> dict[str, str]:
    """Parse a strict key=value file. Blank lines and # comments are ignored."""
    out: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PackageError(f"{path}:{lineno}: expected key=value, got {raw!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        if key in out:
            raise PackageError(f"{path}:{lineno}: duplicate key {key!r}")
        out[key] = value.strip()
    return out


def parse_requirement(path: pathlib.Path) -> tuple[str, str]:
    """Return (dist_name, version) from a single-requirement file."""
    lines = [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if len(lines) != 1:
        raise PackageError(
            f"{path}: expected exactly one requirement, found {len(lines)}"
        )
    match = REQ_RE.match(lines[0])
    if not match:
        raise PackageError(f"{path}: expected 'name==version', got {lines[0]!r}")
    return match.group("name"), match.group("version")


def parse_lock(path: pathlib.Path) -> tuple[dict[str, str], list[str]]:
    """Return ({"pin": ..., "python": ...}, [requirement lines]) from a lock.

    Every requirement must be a plain name==version: a lock is a list of
    constraints, and anything else (a URL, an editable, an unpinned name) is
    either not a constraint or not locking anything.
    """
    header: dict[str, str] = {}
    reqs: list[str] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            for key, regex in (("pin", LOCK_PIN_RE), ("python", LOCK_PYTHON_RE)):
                match = regex.match(line)
                if match:
                    header[key] = match.group(key)
            continue
        if not REQ_RE.match(line):
            raise PackageError(f"{path}:{lineno}: expected 'name==version', got {raw!r}")
        reqs.append(line)
    for key in ("pin", "python"):
        if key not in header:
            raise PackageError(f"{path}: missing '# {key}: ...' header line")
    return header, reqs


def lock_status(pkg: Package, python: str) -> tuple[str, str]:
    """("current" | "missing" | "stale", reason) for running under `python`.

    `python` is the venv's major.minor. A lock records the environment of one
    interpreter, so on another one it is stale rather than approximately right.
    """
    if not pkg.lock.is_file():
        return "missing", f"no {LOCK_NAME}"
    try:
        header, _ = parse_lock(pkg.lock)
    except PackageError as exc:
        return "stale", str(exc)
    if header["pin"] != pkg.pin:
        return "stale", f"generated for {header['pin']}, pin is now {pkg.pin}"
    if header["python"] != python:
        return "stale", f"generated for python {header['python']}, running {python}"
    return "current", ""


def render_lock(pkg: Package, python: str, freeze: str) -> str:
    """A lock file for `pkg` from `uv pip freeze` output.

    Left out: tornado, which the harness force-installs and must never be held
    back by a constraint; the package under test, whose version requirements.txt
    owns; and anything not installed from an index (editables, URLs), which a
    version constraint cannot express.
    """
    skip = {"tornado", normalize(pkg.dist_name)}
    reqs = []
    for raw in freeze.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = REQ_RE.match(line)
        if not match:
            continue
        if normalize(match.group("name")) in skip:
            continue
        reqs.append(f"{normalize(match.group('name'))}=={match.group('version')}")
    reqs.sort()
    lines = [
        "# Generated by run_one.sh with LOCK_MODE=refresh. Do not edit by hand;",
        "# the weekly locks workflow regenerates it.",
        f"# pin: {pkg.pin}",
        f"# python: {python}",
        *reqs,
    ]
    return "\n".join(lines) + "\n"


def _check_specifier(path: pathlib.Path, where: str, spec: object) -> None:
    """Reject a condition that is not a PEP 440 specifier set.

    Checked properly when `packaging` is importable (it always is inside a
    package's venv, where the conditions are evaluated); otherwise only the
    shape is checked, so validation stays dependency-free.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise PackageError(f"{path}: {where} must be a non-empty version specifier")
    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
    except ImportError:
        if not re.match(r"^\s*(~=|===?|!=|<=?|>=?)", spec):
            raise PackageError(
                f"{path}: {where} must be a version specifier such as '>=6.7.dev1', "
                f"got {spec!r}"
            ) from None
        return
    try:
        SpecifierSet(spec)
    except InvalidSpecifier:
        raise PackageError(
            f"{path}: {where} must be a version specifier such as '>=6.7.dev1', "
            f"got {spec!r}"
        ) from None


def parse_known_failures(path: pathlib.Path) -> list[dict]:
    """The entries of a known_failures.toml, validated, with defaults filled in."""
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise PackageError(f"{path}: {exc}") from None
    unknown = set(data) - {"failure"}
    if unknown:
        raise PackageError(
            f"{path}: unknown top-level key(s): {', '.join(sorted(unknown))}; "
            "each entry is a [[failure]] table"
        )
    entries = data.get("failure", [])
    if not isinstance(entries, list) or not entries:
        raise PackageError(f"{path}: no [[failure]] entries; delete the file instead")
    out = []
    seen: dict[str, int] = {}
    for i, entry in enumerate(entries, 1):
        where = f"[[failure]] #{i}"
        unknown = set(entry) - KNOWN_FAILURE_KEYS
        if unknown:
            raise PackageError(f"{path}: {where}: unknown key(s): {', '.join(sorted(unknown))}")
        missing = KNOWN_FAILURE_REQUIRED - set(entry)
        if missing:
            raise PackageError(f"{path}: {where}: missing key(s): {', '.join(sorted(missing))}")
        if not isinstance(entry["issue"], str) or not entry["issue"].startswith("https://"):
            raise PackageError(f"{path}: {where}: issue must be an https:// link to the upstream issue or fix")
        if not isinstance(entry["reason"], str) or not entry["reason"].strip():
            raise PackageError(f"{path}: {where}: reason must say why this is not a Tornado regression")
        conditions = KNOWN_FAILURE_CONDITIONS & set(entry)
        if not conditions:
            raise PackageError(
                f"{path}: {where}: needs at least one condition "
                f"({', '.join(sorted(KNOWN_FAILURE_CONDITIONS))}) so it stops applying"
            )
        for key in sorted(conditions):
            _check_specifier(path, f"{where}: {key}", entry[key])
        action = entry.get("action", "xfail")
        if action not in KNOWN_FAILURE_ACTIONS:
            raise PackageError(
                f"{path}: {where}: action must be one of {sorted(KNOWN_FAILURE_ACTIONS)}, got {action!r}"
            )
        tests = entry["tests"]
        if not isinstance(tests, list) or not tests:
            raise PackageError(f"{path}: {where}: tests must be a non-empty list of pytest node ids")
        for test in tests:
            if not isinstance(test, str) or not NODEID_RE.match(test):
                raise PackageError(
                    f"{path}: {where}: {test!r} is not a pytest node id "
                    "(path/to/test_x.py[::Class]::test_name)"
                )
            # One test under two entries would make which issue explains it,
            # and which entry expires it, ambiguous.
            if test in seen:
                raise PackageError(
                    f"{path}: {where}: {test} is already listed in [[failure]] #{seen[test]}"
                )
            seen[test] = i
        out.append(
            {
                "issue": entry["issue"],
                "reason": entry["reason"].strip(),
                "action": action,
                "tests": list(tests),
                **{key: entry[key] for key in sorted(conditions)},
            }
        )
    return out


def active_known_failures(pkg: Package, tornado_version: str) -> list[dict]:
    """The entries whose conditions hold for this pin and this Tornado.

    `tornado_version` is what run_one.sh found importable after installing the
    Tornado under test, not the spec it asked for. Pre-releases always count:
    master is a .dev version, and a specifier like '>=6.7' must match it.
    Needs `packaging`, so it runs with the package venv's python.
    """
    from packaging.specifiers import SpecifierSet

    if pkg.known_failures is None:
        return []
    have = {"tornado": tornado_version, "version": pkg.version}
    return [
        entry
        for entry in parse_known_failures(pkg.known_failures)
        if all(
            SpecifierSet(entry[key]).contains(have[key], prereleases=True)
            for key in KNOWN_FAILURE_CONDITIONS & set(entry)
        )
    ]


def parse_patch_header(path: pathlib.Path) -> dict[str, str]:
    """{"pin": ..., "upstream": ...} from the comment lines before the diff."""
    header: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line.startswith("#"):
            break
        for key, regex in (("pin", PATCH_PIN_RE), ("upstream", PATCH_UPSTREAM_RE)):
            match = regex.match(line)
            if match:
                header[key] = match.group(key)
    for key, example in (("pin", "name==version"), ("upstream", "https://...")):
        if key not in header:
            raise PackageError(f"{path}: missing '# {key}: {example}' header line")
    return header


def check_patches(pkg: Package) -> list[str]:
    """A patch made for another pin must not be applied to this one.

    The point of pinning a patch is that it stops applying the moment the pin
    moves: the new release may already contain the fix, in which case the patch
    is dead weight, or may not, in which case it needs re-checking. Either way a
    person has to look, so this fails the pin bump's own pull request.
    """
    problems = []
    for patch in pkg.patches:
        try:
            header = parse_patch_header(patch)
        except PackageError as exc:
            problems.append(f"{pkg.name}: {exc}")
            continue
        if header["pin"] != pkg.pin:
            problems.append(
                f"{pkg.name}: {PATCHES_DIR}/{patch.name} was made for {header['pin']}, "
                f"but the pin is now {pkg.pin}. Check whether {pkg.pin} includes "
                f"{header['upstream']}; if it does, delete the patch (and any "
                f"known_failures.toml entry it made unnecessary), otherwise "
                f"regenerate it against the new tag."
            )
    return problems


def load(name: str) -> Package:
    directory = PKGDEFS_DIR / name
    if not directory.is_dir():
        raise PackageError(f"no such package: {name}")

    env = parse_env(directory / "package.env")
    unknown = set(env) - KNOWN_KEYS
    if unknown:
        raise PackageError(
            f"{directory/'package.env'}: unknown key(s): {', '.join(sorted(unknown))}"
        )
    missing = REQUIRED_KEYS - set(env)
    if missing:
        raise PackageError(
            f"{directory/'package.env'}: missing key(s): {', '.join(sorted(missing))}"
        )

    install_method = env.get("install_method", "")
    if install_method not in INSTALL_METHODS:
        raise PackageError(
            f"{directory/'package.env'}: install_method must be one of "
            f"{sorted(INSTALL_METHODS)}, got {install_method!r}"
        )
    # The package's own test extra, installed alongside the pinned wheel so the
    # harness does not keep a copy of that list. The editable path already tries
    # the common test-extra names itself, so this only means anything for pypi.
    group = env.get("optional_dependency_group", "")
    if group:
        if install_method != "pypi":
            raise PackageError(
                f"{directory/'package.env'}: optional_dependency_group is only "
                f"used with install_method=pypi"
            )
        if not EXTRA_RE.match(group):
            raise PackageError(
                f"{directory/'package.env'}: optional_dependency_group must be a "
                f"single extra name, got {group!r}"
            )
    # The floor below which this package's tornado coverage is treated as a
    # broken test command rather than a real measurement. Absent means unchecked.
    min_coverage: int | None = None
    if "min_coverage" in env:
        try:
            min_coverage = int(env["min_coverage"])
        except ValueError:
            raise PackageError(
                f"{directory/'package.env'}: min_coverage must be an integer "
                f"percent, got {env['min_coverage']!r}"
            ) from None
        if not 0 <= min_coverage <= 100:
            raise PackageError(
                f"{directory/'package.env'}: min_coverage must be between 0 and "
                f"100, got {min_coverage}"
            )

    dist_name, version = parse_requirement(directory / "requirements.txt")
    if "{version}" not in env["tag_template"]:
        raise PackageError(
            f"{directory/'package.env'}: tag_template must contain '{{version}}', "
            f"got {env['tag_template']!r}"
        )

    return Package(
        name=name,
        repo=env["repo"],
        tag_template=env["tag_template"],
        subdir=env["subdir"],
        install_method=install_method,
        pypi_extra_deps=env.get("pypi_extra_deps", ""),
        optional_dependency_group=group,
        min_coverage=min_coverage,
        apt_packages=env.get("apt_packages", ""),
        dist_name=dist_name,
        version=version,
        directory=directory,
    )


def load_all() -> list[Package]:
    """Every package, in alphabetical order (the order the reports use)."""
    names = sorted(p.name for p in PKGDEFS_DIR.iterdir() if (p / "package.env").is_file())
    return [load(n) for n in names]


def resolve(selector: str) -> Package:
    """Look up a package by name."""
    for pkg in load_all():
        if pkg.name == selector:
            return pkg
    raise PackageError(f"unknown package: {selector}")


def _check_cov_flags(name: str, test_sh: pathlib.Path) -> list[str]:
    """A --cov flag outside the COV_ARGS default breaks the release gate.

    With COVERAGE=0 the harness exports COV_ARGS empty and does not install
    pytest-cov, so any other --cov flag reaches a pytest that does not know it
    and the package fails with a usage error before a single test runs.
    """
    problems = []
    for lineno, line in enumerate(test_sh.read_text().splitlines(), 1):
        code = line.lstrip()
        if code.startswith("#") or "COV_ARGS" in code:
            continue
        if "--cov" in code:
            problems.append(
                f"{name}: test.sh:{lineno} passes a --cov flag outside the "
                "COV_ARGS default; with coverage off pytest-cov is not "
                "installed and pytest rejects it"
            )
    return problems


def check_files(pkg: Package) -> list[str]:
    """Structural problems with a package directory, as a list of messages."""
    problems = []
    if not NAME_RE.match(pkg.name):
        problems.append(f"{pkg.name}: directory name must match {NAME_RE.pattern}")
    for required in ("requirements.txt", "package.env", "test.sh", "README.md"):
        if not (pkg.directory / required).is_file():
            problems.append(f"{pkg.name}: missing {required}")
    test_sh = pkg.directory / "test.sh"
    if test_sh.is_file() and not test_sh.stat().st_mode & 0o111:
        problems.append(f"{pkg.name}: test.sh is not executable")
    if test_sh.is_file():
        problems.extend(_check_cov_flags(pkg.name, test_sh))
    setup_sh = pkg.directory / "setup.sh"
    if setup_sh.is_file() and not setup_sh.stat().st_mode & 0o111:
        problems.append(f"{pkg.name}: setup.sh is not executable")
    if pkg.lock.is_file():
        try:
            _, reqs = parse_lock(pkg.lock)
        except PackageError as exc:
            problems.append(f"{pkg.name}: {exc}")
        else:
            held = {normalize(REQ_RE.match(r).group("name")) for r in reqs}
            # A constraint on tornado would fight the force-install of the
            # Tornado under test; one on the package itself would fight the pin.
            for dist in sorted(held & {"tornado", normalize(pkg.dist_name)}):
                problems.append(f"{pkg.name}: {LOCK_NAME} must not constrain {dist}")
    if pkg.known_failures is not None:
        try:
            parse_known_failures(pkg.known_failures)
        except PackageError as exc:
            problems.append(f"{pkg.name}: {exc}")
    problems.extend(check_patches(pkg))
    if pkg.install_method == "pypi" and pkg.dist_name != pkg.name:
        # Not fatal, but the pypi install path installs requirements.txt by
        # name, so a mismatch is worth stating out loud.
        problems.append(
            f"{pkg.name}: requirements.txt pins {pkg.dist_name!r}, which differs "
            f"from the directory name"
        )
    return problems


def check_ref_exists(pkg: Package, timeout: int = 60) -> str | None:
    """Confirm the derived tag resolves upstream. Returns a message on failure.

    Dependabot bumps the version in requirements.txt; the tag it implies has to
    exist or setup.sh would fail much later, during a release build rather than
    on the bump's own pull request.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--exit-code", pkg.repo, pkg.ref, f"refs/tags/{pkg.ref}"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"{pkg.name}: timed out resolving {pkg.ref} in {pkg.repo}"
    if proc.returncode != 0:
        return (
            f"{pkg.name}: ref {pkg.ref!r} (from {pkg.dist_name}=={pkg.version}) "
            f"not found in {pkg.repo}"
        )
    return None


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "names":
            for pkg in load_all():
                print(pkg.name)
        elif cmd == "count":
            print(len(load_all()))
        elif cmd == "resolve":
            print(resolve(args[0]).name)
        elif cmd == "env":
            print(resolve(args[0]).shell_env())
        elif cmd == "apt":
            # System packages the workflow installs before running this one.
            print(resolve(args[0]).apt_packages)
        elif cmd == "lock-status":
            # lock-status <name> <python-major.minor> -> "current", "missing:
            # <reason>" or "stale: <reason>"
            state, reason = lock_status(resolve(args[0]), args[1])
            print(state if not reason else f"{state}: {reason}")
        elif cmd == "write-lock":
            # write-lock <name> <python-major.minor>, `uv pip freeze` on stdin.
            pkg = resolve(args[0])
            pkg.lock.write_text(render_lock(pkg, args[1], sys.stdin.read()))
        elif cmd == "known-failures":
            # known-failures <name> <tornado-version> -> JSON list of the
            # entries that apply. Run with the package venv's python.
            print(json.dumps(active_known_failures(resolve(args[0]), args[1])))
        elif cmd == "patches":
            # patches <name> -> one patch path per line; fails if any patch was
            # made for a different pin.
            pkg = resolve(args[0])
            problems = check_patches(pkg)
            if problems:
                raise PackageError("; ".join(problems))
            for patch in pkg.patches:
                print(patch)
        elif cmd == "refs":
            for pkg in load_all():
                print(f"{pkg.name}\t{pkg.dist_name}=={pkg.version}\t{pkg.ref}")
        elif cmd == "matrix":
            # Names as a JSON array, for `fromJson` in a workflow matrix.
            wanted = args[0].split() if args and args[0].strip() else None
            packages = [resolve(s) for s in wanted] if wanted else load_all()
            print(json.dumps([p.name for p in packages]))
        else:
            print(f"unknown subcommand: {cmd}", file=sys.stderr)
            return 2
    except (PackageError, IndexError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
