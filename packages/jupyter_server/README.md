# jupyter_server

Core backend for Jupyter Notebook / JupyterLab; Tornado is its HTTP engine.

## What it exercises

The full suite runs against **live Tornado servers**: the `pytest-jupyter`
fixtures (`jp_serverapp`, `jp_fetch`, `jp_ws_fetch`) boot a real `ServerApp` on
a real port. ~1136 tests, the longest run in the testbed.

- `tornado.testing` as jupyter_server's own test base infrastructure
- `tornado.websocket` for kernel and terminal communication channels

Tornado coverage from this package alone reaches the web/HTTP layers most
broadly of any entry here (websocket, web, iostream, httpserver).

## Why this package matters most

It is the package that proved the testbed works. At jupyter_server 2.14.2 it
caught [tornado#3724](https://github.com/tornadoweb/tornado/issues/3724):
`StaticFileHandler.allowed_symlink_directory`, added in 6.5.9, was set only in
`initialize()`, so subclasses that replace `initialize()` -- like
jupyter_server's `FileFindHandler` -- raised `AttributeError` on every
static-file request. Four tests failed on 6.5.9 and passed on 6.5.8 and 6.5.10:

- `tests/base/test_handlers.py::test_static_handler`
- `tests/extension/test_handler.py::test_base_url`
- `tests/services/kernelspecs/test_api.py::test_get_kernel_resource_file`
- `tests/services/kernelspecs/test_api.py::test_get_nonexistant_resource`

**The current pin no longer detects it.** All four pass on 6.5.9 under
jupyter_server 2.21.1, which changed `FileFindHandler` enough not to trip the
bug. That is the normal cost of tracking current downstream code, and it is the
right trade -- the testbed exists to protect the code people are running now --
but it means a routine green run is no longer evidence that the gate can catch a
real regression.

### Validating the harness

To get that evidence back, pin this package temporarily to the release that
still trips the bug. This is a deliberate manual procedure, not something a
normal run does:

```bash
sed -i 's/^jupyter_server==.*/jupyter_server==2.14.2/' packages/jupyter_server/requirements.txt
mv packages/jupyter_server/patches /tmp/jupyter_server-patches   # made for the real pin
rm -rf checkouts/jupyter_server && ./scripts/setup.sh jupyter_server

TORNADO_SPEC="tornado==6.5.9"  ./scripts/run_one.sh jupyter_server   # must FAIL, 4 tests
TORNADO_SPEC="tornado==6.5.10" ./scripts/run_one.sh jupyter_server   # must PASS

git checkout packages/jupyter_server/requirements.txt
mv /tmp/jupyter_server-patches packages/jupyter_server/patches
```

(The patch has to move aside: it is pinned to the current release, and
`run_one.sh` refuses it on any other pin with `SETUP_FAIL`.)

The 6.5.9 run should show
`AttributeError: 'FileFindHandler' object has no attribute
'allowed_symlink_directory'`. If it does not, something in the harness has
stopped reporting real failures and that is worth chasing before trusting a
green release gate.

(2.14.2 needs the `hatchling<1.32.1` entry in `build-constraints.txt` to install
at all; it is already there.)

## Deselects

Every `--deselect` in `test.sh` carries its reason inline. They fall into two
kinds: jupyter_server's own order-dependent, timing-sensitive or
subprocess-launching tests, and one case of dependency drift that is red on
every Tornado version. Both would
otherwise make this package permanently red and hide real regressions.

## Patch: PR 1717, until the next release

`patches/pr1717-request-start-line.patch` is the first commit of
[jupyter_server#1717](https://github.com/jupyter-server/jupyter_server/pull/1717),
merged upstream but not in 2.21.1. Tornado 6.6 deprecated building an
`HTTPServerRequest` from method/uri/version and master (6.7) requires
`start_line`. jupyter_server's own tests build requests the old way, so on
master **42 tests** fail before reaching what they test: the
`allow_origin_pat` bypass tests, `check_xsrf_cookie`, the static handler
(including `follow_dir_symlinks`), websocket pings and the kernel connection.

Measured on master 494aee6 with coverage on:

| | result | tornado coverage |
|---|---|---|
| unpatched | 38 failed, 4 errors, 1105 passed | not recorded |
| strict xfail of the 42 | 42 xfailed, 1104 passed | 42% |
| this patch | 1147 passed | 42% |

Line coverage hardly moves, because the live-server tests reach the same code,
but an xfail list would switch off the assertions in exactly the tests closest
to Tornado's security-sensitive behaviour. The patch changes only test files,
so jupyter_server's runtime code is tested as released. It also passes on
tornado 6.6.1, so the release gate for 6.6.x is unaffected. (Both the xfail
run and the 6.6.1 run hit the known `test_restart_kernel[jp_server_config0]`
timing flake described in `REPORT.md`; it is unrelated to the patch.)

PR 1717's other commit (trash on a tmpfs `/tmp`) has nothing to do with Tornado
and does not fail here, so it is left out. The patch is pinned to 2.21.1:
dependabot's next bump fails `validate_packages.py` until someone confirms the
new release contains the fix and deletes the patch.
