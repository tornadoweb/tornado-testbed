#!/usr/bin/env bash
# Turn results/<name>.txt into a report, and decide the exit code.
#
# Shared by both ways the testbed runs: scripts/ci.sh (serial, one machine) and
# the per-package job matrix in .github/workflows/testbed.yml, which downloads
# each job's result files and then calls this. Keeping one implementation means
# the two paths cannot disagree about whether a run passed.
#
# Writes a markdown summary to stdout and to $GITHUB_STEP_SUMMARY, exposes
# total/passed/failed via $GITHUB_OUTPUT, and echoes the tail of each failing
# package's log so a traceback is readable in the web UI without downloading
# the artifact.
#
# Inputs (environment):
#   TORNADO_SPEC        What was tested, for the report header.
#   FAIL_ON_REGRESSION  "1" (default) exits non-zero unless every package
#                       passed. "0" reports only.
#   LOG_TAIL            Lines of each failing log to echo (default 200).
set -uo pipefail
source "$(dirname "$0")/common.sh"

TORNADO_SPEC="${TORNADO_SPEC:-tornado}"
FAIL_ON_REGRESSION="${FAIL_ON_REGRESSION:-1}"
LOG_TAIL="${LOG_TAIL:-200}"

group()    { [[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::group::$*" || echo "=== $* ==="; }
endgroup() { [[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::endgroup::" || true; }

# ── Render the summary ───────────────────────────────────────────────────────
summary_md="$(python3 - "$RESULTS_DIR" "$TORNADO_SPEC" "$ROOT_DIR" <<'PY'
import os, sys, glob, json

results_dir, tornado_spec, root = sys.argv[1], sys.argv[2], sys.argv[3]

# Each package's coverage floor, for the "worth raising" hint below. Best
# effort: the report is what is left standing when something else has broken,
# so it must still render if the definitions cannot be read.
sys.path.insert(0, os.path.join(root, "scripts"))
try:
    import pkglib

    floors = {
        pkg.name: pkg.min_coverage
        for pkg in pkglib.load_all()
        if pkg.min_coverage is not None
    }
except Exception:
    floors = {}

rows, counts, unlocked, adjusted = [], {}, [], []
for path in sorted(glob.glob(os.path.join(results_dir, "*.txt"))):
    if os.path.basename(path) == "summary.txt":
        continue
    kv = {}
    with open(path) as f:
        for line in f:
            if "=" in line:
                k, _, v = line.strip().partition("=")
                kv[k] = v
    name = kv.get("package") or os.path.splitext(os.path.basename(path))[0]
    status = kv.get("status", "UNKNOWN")
    counts[status] = counts.get(status, 0) + 1
    # Only an explicit "unlocked": results from before locks have no lock= line,
    # and a refresh run is unlocked on purpose.
    if kv.get("lock") == "unlocked":
        unlocked.append(name)
    # Anything that changed what this package's suite checks. A malformed
    # record is shown as such rather than dropped: hiding it would hide the
    # very thing this section is for.
    try:
        known = json.loads(kv.get("known_failures") or "[]")
    except ValueError:
        known = [{"issue": "(unreadable known_failures record)", "action": "?",
                  "tests": [], "reason": kv.get("known_failures", "")}]
    patches = kv.get("patches", "").split()
    if known or patches:
        adjusted.append((name, patches, known))
    rows.append((name, status, kv.get("exit_code", "?"),
                 kv.get("tornado", "?"), kv.get("test_secs", "?"),
                 kv.get("coverage_pct", "n/a")))

emoji = {"PASS": "✅", "FAIL": "❌", "TIMEOUT": "⏱️",
         "INSTALL_FAIL": "🛠️", "SETUP_FAIL": "🛠️",
         "TORNADO_INSTALL_FAIL": "🌪️", "TORNADO_MISMATCH": "🌪️",
         "COVERAGE_LOW": "📉"}

out = []
out.append("## Tornado testbed results")
out.append("")
out.append(f"**Tornado under test:** `{tornado_spec}`")
out.append("")
total = sum(counts.values())
order = ["PASS", "FAIL", "TIMEOUT", "INSTALL_FAIL", "SETUP_FAIL",
         "TORNADO_INSTALL_FAIL", "TORNADO_MISMATCH", "COVERAGE_LOW", "UNKNOWN"]
badge = " · ".join(f"{emoji.get(s, '•')} {s}: {counts[s]}"
                   for s in order if s in counts)
out.append(f"{total} package(s) — {badge}")
out.append("")
measured = [r for r in rows if r[5] not in ("n/a", "")]
show_cov = bool(measured)

if show_cov:
    out.append("| Package | Status | RC | Tornado | Time (s) | Coverage |")
    out.append("|---------|--------|----|---------|----------|----------|")
else:
    out.append("| Package | Status | RC | Tornado | Time (s) |")
    out.append("|---------|--------|----|---------|----------|")
for name, status, rc, tornado, secs, cov in rows:
    row = f"| {name} | {emoji.get(status, '')} {status} | {rc} | {tornado} | {secs} |"
    if show_cov:
        row += f" {cov if cov in ('n/a', '') else cov + '%'} |"
    out.append(row)

# Not a failure, but worth seeing: these resolved their dependencies fresh, so
# a red one may be drift rather than Tornado.
if unlocked:
    out.append("")
    out.append("⚠️ **Ran without a current lock:** "
               + ", ".join(f"`{n}`" for n in unlocked)
               + ". Their dependencies were resolved fresh, so a failure here "
               "may be downstream drift rather than Tornado. The `locks` "
               "workflow regenerates `constraints.lock`; each log says why it "
               "was not used.")

# Always listed, green or red: an expected failure that nobody can see is one
# nobody removes. Each entry stops applying on its own (strict xfail goes red
# when the test passes; a patch is refused once the pin moves), and this is
# where a reader sees what is currently being excused and why.
if adjusted:
    out.append("")
    out.append("**Known downstream failures in effect** (not counted as Tornado "
               "regressions; see each package's `known_failures.toml` and "
               "`patches/`):")
    out.append("")
    for name, patches, known in adjusted:
        for patch in patches:
            out.append(f"- `{name}`: patched with `{patch}`")
        for entry in known:
            conds = ", ".join(f"{k} `{entry[k]}`" for k in ("tornado", "version")
                              if k in entry)
            n = entry.get("matched", len(entry.get("tests", [])))
            verb = "strict xfail" if entry.get("action") == "xfail" else entry.get("action")
            out.append(f"- `{name}`: {verb}, {n} test(s), while {conds} — "
                       f"{entry.get('issue')}: {entry.get('reason')}")

# A floor that sits far below what a package actually covers stops being a
# check. Nothing else would ever prompt anyone to raise it, so say so here.
stale_floors = []
for name, status, _rc, _t, _s, cov in rows:
    if status != "PASS" or not cov.isdigit():
        continue
    floor = floors.get(name)
    if floor is not None and int(cov) - floor >= 10:
        stale_floors.append((name, floor, int(cov)))
if stale_floors:
    out.append("")
    out.append("<details><summary>📈 Coverage floors worth raising</summary>")
    out.append("")
    for name, floor, cov in stale_floors:
        out.append(f"- `{name}`: floor {floor}%, now measuring {cov}% — "
                   f"consider raising `min_coverage` in `packages/{name}/package.env`")
    out.append("")
    out.append("</details>")
print("\n".join(out))

gh_out = os.environ.get("GITHUB_OUTPUT")
if gh_out:
    passed = counts.get("PASS", 0)
    failed = total - passed
    with open(gh_out, "a") as f:
        f.write(f"total={total}\n")
        f.write(f"passed={passed}\n")
        f.write(f"failed={failed}\n")
PY
)"

echo ""
echo "${summary_md}"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    echo "${summary_md}" >> "${GITHUB_STEP_SUMMARY}"
fi

# ── Surface failing-package logs inline ──────────────────────────────────────
not_passed=0
for res in "${RESULTS_DIR}"/*.txt; do
    [[ -e "${res}" ]] || continue
    [[ "$(basename "${res}")" == "summary.txt" ]] && continue
    status="$(awk -F= '/^status=/ {print $2}' "${res}")"
    [[ "${status}" == "PASS" ]] && continue
    not_passed=$((not_passed + 1))

    name="$(awk -F= '/^package=/ {print $2}' "${res}")"
    name="${name:-$(basename "${res}" .txt)}"
    log="${LOGS_DIR}/${name}.log"
    [[ -f "${log}" ]] || continue

    group "FAILED ${name} (${status}) — last ${LOG_TAIL} lines of ${name}.log"
    tail -n "${LOG_TAIL}" "${log}"
    endgroup

    # Pytest's "short test summary info" block is the most useful one-glance
    # digest; fall back to the log tail if the run never reached that point.
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
        digest="$(sed 's/\x1b\[[0-9;]*m//g' "${log}" \
                  | grep -E '^(FAILED|ERROR) |[0-9]+ (failed|passed|error)' \
                  | head -n 60)"
        [[ -z "${digest}" ]] && digest="$(tail -n "${LOG_TAIL}" "${log}" \
                  | sed 's/\x1b\[[0-9;]*m//g')"
        {
            echo ""
            echo "<details><summary>❌ ${name} (${status}) — failure detail</summary>"
            echo ""
            echo '```'
            echo "${digest}"
            echo '```'
            echo ""
            echo "</details>"
        } >> "${GITHUB_STEP_SUMMARY}"
    fi
done

# ── Exit code ────────────────────────────────────────────────────────────────
# The report is fully written by this point, so failing here still leaves
# everything a human needs behind.
#
# The harness curates a green baseline: known-bad downstream tests are
# deselected in each package's test.sh with the reason recorded next to them,
# and failures from deliberate Tornado changes are strict xfails in
# known_failures.toml (listed above). So anything other than PASS is a
# regression to look at, and the gate says so.
if [[ "${not_passed}" -gt 0 ]]; then
    echo ""
    echo "${not_passed} package(s) did not pass against ${TORNADO_SPEC}."
    if [[ "${FAIL_ON_REGRESSION}" == "1" ]]; then
        exit 1
    fi
    echo "FAIL_ON_REGRESSION=0, reporting only."
fi
exit 0
