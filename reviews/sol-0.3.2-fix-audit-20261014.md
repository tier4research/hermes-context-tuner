{"schema_version": 1, "tier": "sol", "model": "gpt-5.6-sol", "response_id": "resp_0525b34e82769e6d016acf61983c10819ba625b711f6e2f80b", "success": true, "reason": "completed", "reasoning_effort": "high", "usage": {"input_tokens": 19740, "output_tokens": 17752, "total_tokens": 37492}}
## Findings

### CRITICAL — `verify_install()` does not verify that the engine can load; it verifies only directory resolution

**Code path:** `src/hermes_context_tuner/cli.py`, approximately lines 55–143:

- `_LOADER_PROBE` calls:
  - `find_engine_dir("context-tuner")`
  - `discover_context_engines()`
- `verify_install()` considers the install successful solely when `find_engine_dir()` returns a path resolving to `target`.

The result of `discover_context_engines()` is copied into the report but never validated:

```python
report["discover"] = data.get("discover")
...
report["ok"] = not report["reasons"]
```

`discover_error` is also ignored. There is no attempt to:

- import the installed shim,
- parse or validate its `plugin.yaml`,
- call its `register()` function,
- instantiate `ContextTunerEngine`,
- verify the engine appears in discovery,
- verify discovery marks it loadable,
- or exercise the same code path Hermes uses to activate the selected engine.

**Reachability:** Trivial. A loader that resolves the target directory while failing during discovery produces `ok=True`:

```python
def find_engine_dir(name):
    return TARGET

def discover_context_engines():
    raise ImportError("broken plugin")
```

The probe catches the discovery exception, emits JSON with `discover_error`, and exits normally. `verify_install()` sees the correct `resolved` path and no reason, so it returns success.

A broken shim has the same outcome if `find_engine_dir()` only checks directory existence. The new clean-tree test proves this weakness directly: `tests/test_install_verify.py`, approximately lines 50–67, creates an **empty** `__init__.py` and a skeletal one-line manifest:

```python
(target / "__init__.py").write_text("", encoding="utf-8")
(target / "plugin.yaml").write_text("name: context-tuner\n", encoding="utf-8")
```

That target is not proven to be a loadable Hermes engine, yet `test_clean_tree_verifies_against_the_loader` expects verification to succeed.

**Observable consequence:** `install-hermes` can still print:

```text
installed context-tuner engine shim ...
verified: the loader resolves ...
```

while Hermes cannot discover, import, register, or instantiate the engine. This preserves the central false-success deployment class that the change claims to eliminate.

**Required correction:** The probe must validate the actual production load/activation result, not merely `find_engine_dir()`. At minimum:

1. require successful process exit;
2. require discovery to complete without error;
3. require exactly the expected engine entry to appear and be loadable;
4. import/load the resolved plugin through Hermes’s production plugin-loading function;
5. ideally instantiate or register it through the actual Hermes entry point.

---

### HIGH — Successful compression can be recorded as `failed`

**Code path:** `src/hermes_context_tuner/engine.py`, approximately lines 174–197:

```python
try:
    self._store.mark_interrupted_if_started(operation_id, reason=reason[:200])
    return
except Exception:
    pass
try:
    self._store.fail_compression(operation_id, reason=reason[:200])
except Exception:
    pass
```

`_terminalize_operation()` is called specifically after the delegate has returned successfully:

- post-delegate bookkeeping failure, approximately lines 460–478;
- pending-state publication failure, approximately lines 490–502.

Its docstring correctly says that `interrupted` is the honest state because the compression itself succeeded. The fallback then contradicts that statement by invoking `fail_compression()`.

**Reachability:**

- `mark_interrupted_if_started()` is absent because an old/incompatible store object is injected;
- the first database operation encounters a transient lock or transaction error while the second succeeds;
- the first operation changes the row but raises during commit/final cleanup;
- a test, proxy, telemetry wrapper, or subclass raises from `mark_interrupted_if_started()`.

**Observable consequence:** The journal says the compression failed even though the host received a successful delegate result. That is provenance corruption in the opposite direction from the original orphaned-`started` bug.

The fallback is also unsafe under an uncertain-write result. If `mark_interrupted_if_started()` committed and then raised, `fail_compression()` may perform a second terminal transition. Whether it overwrites `interrupted` or an independently finalized row depends on the SQL in `RecoveryPointerStore.fail_compression()`, which was not supplied.

**Required correction:** Do not use a semantic `failed` state as a fallback for a successful delegate operation. Use an idempotent, conditional terminal operation such as:

```sql
UPDATE operations
SET status = 'interrupted', ...
WHERE operation_id = ? AND status = 'started';
```

If the first write has an uncertain outcome, re-read the row before attempting another transition. Every terminal writer should enforce `WHERE status = 'started'`.

---

### HIGH — The pending-publication failure path leaves a live operation pointer after terminalizing the row

**Code path:** `src/hermes_context_tuner/engine.py`, approximately lines 480–503:

```python
try:
    self._pending_operation = pending_op
    self._pending_count = count
    self._pending_result_fingerprint = fingerprint
    self._pending_old_session_id = self._session_id
    self._pending_checkpoint_hint = hint
except Exception:
    try:
        self._audit_failure()
        self._terminalize_operation(
            pending_op, "pending-state publication failed"
        )
    except Exception:
        pass
```

The assignments are incremental. If `_pending_operation` succeeds and any later assignment fails, the exception handler terminalizes the row but does not clear the already-published pointer or the other partially published fields.

**Reachability:** A subclass, data descriptor, instrumentation layer, or custom `__setattr__` can allow `_pending_operation` and then raise on `_pending_count`, `_pending_result_fingerprint`, or a later field. A one-shot failure makes this deterministic:

1. `_pending_operation = pending_op` succeeds.
2. `_pending_count = count` raises.
3. `_terminalize_operation()` marks the row interrupted.
4. The exception handler returns without clearing `_pending_operation`.
5. A later callback sees the same operation ID.

The same class of partial state is possible if cleanup in the post-bookkeeping exception handler fails, because all cleanup assignments are placed after terminalization in a single outer `try`.

**Later paths touching the same operation ID:**

- `on_session_start()`, approximately lines 507–565, calls `finalize_compression()`;
- the next `compress()`, approximately lines 335–348, calls `_close_pending_on_teardown()`;
- `on_session_end()` and `on_session_reset()`, approximately lines 585–598, call `_close_pending_on_teardown()`.

**Observable consequence:**

- At minimum, an already terminalized operation is treated as pending and receives a second terminalization attempt.
- If `finalize_compression()` does not enforce `status='started'`, an interrupted operation can later become finalized.
- Even if the store protects the row, the callback uses partially stale count, fingerprint, checkpoint, or session state.
- A subsequent compression may close the wrong in-memory operation and increment health failures.

**Required correction:** Build pending state in a single immutable object and publish it with one assignment. On publication failure, clear every pending field in a `finally` block using `object.__setattr__()` if subclass interception must be bypassed. At minimum:

```python
except Exception:
    try:
        self._terminalize_operation(...)
    finally:
        object.__setattr__(self, "_pending_operation", None)
        object.__setattr__(self, "_pending_count", 0)
        object.__setattr__(self, "_pending_result_fingerprint", None)
        object.__setattr__(self, "_pending_old_session_id", None)
        object.__setattr__(self, "_pending_checkpoint_hint", False)
```

---

### HIGH — Trigger-construction failure discards a valid budget plan and silently suppresses the journal row

**Code path:** `src/hermes_context_tuner/engine.py`, approximately lines 350–390:

```python
try:
    lossy = bool(plan is not None and any(...))
    trigger = {
        "force": bool(kwargs.get("force", False)),
        "focus_topic_present": bool(focus_topic),
        "focus_topic_chars": len(focus_topic) if focus_topic else 0,
        "current_tokens": current_tokens,
    }
except Exception:
    self._audit_failure()
    plan = None
    lossy = False
    trigger = {}
```

The subsequent journal write is gated by `plan is not None`:

```python
if plan is not None:
    pending_op = self._store.begin_compression(...)
```

A failure in unrelated trigger metadata destroys an already valid plan.

**Reachability:** For example:

```python
engine.compress(messages, current_tokens=1000, focus_topic=1)
```

Planning does not use `focus_topic`, so `build_budget_plan()` can succeed. Trigger construction then evaluates:

```python
bool(1)          # succeeds
len(1)           # TypeError
```

The handler sets `plan=None`, skips `begin_compression()`, and still delegates compression. The same occurs for a `force` object whose `__bool__` raises or a focus object with hostile `__bool__`/`__len__`.

**Observable consequence:**

- A compression is performed with no persistent recovery row.
- Message snapshot/recovery information, plan decisions, token totals, and operation provenance are lost.
- `_last_plan` still points to the valid plan set earlier, so status can report a plan while the journal contains no corresponding operation.
- The only evidence is an in-memory health increment, which disappears on restart and may itself be unavailable.
- Checkpoint cadence/hint provenance for a lossy operation is lost.

This directly violates the stated ownership model that the journal records every compression performed by the wrapper. The comment “With no plan there is nothing truthful to journal” is factually wrong in this path: a valid plan existed; only optional trigger metadata failed.

**Required correction:** Isolate each optional trigger field and substitute safe sentinel values without discarding `plan`. For example, record:

```python
trigger = {"metadata_degraded": True}
```

and continue writing the operation. Plan inspection and trigger construction should also be separate failure boundaries; failure to compute `lossy` need not suppress the complete plan and journal row.

---

### HIGH — The probe imports the loader in a synthetic environment that is not equivalent to a real Hermes process

**Code path:** `src/hermes_context_tuner/cli.py`, approximately lines 55–73 and 112–130.

The probe manually imports:

```python
_spec = spec_from_file_location(
    '_ct_ctx_engine',
    _ha + '/plugins/context_engine/__init__.py'
)
_m = module_from_spec(_spec)
_spec.loader.exec_module(_m)
```

This is not the normal package import or Hermes process environment.

#### Relative-import false failures

The module is not inserted into `sys.modules` before `exec_module()`. A valid package initializer containing a relative import such as:

```python
from .discovery import find_engine_dir
```

can fail because the synthetic parent package `_ct_ctx_engine` is absent from `sys.modules`.

The tests do not expose this because `FAKE_LOADER` is a single self-contained file with only a standard-library absolute import.

#### Repository import false failures

The repository root is not inserted into `sys.path`, and `cwd` is not set to the Hermes repository. A valid loader that relies on imports available when Hermes starts from its normal launcher can fail under the probe.

#### Wrong interpreter

Only a Windows virtual-environment location is considered:

```python
python = Path(root) / "venv" / "Scripts" / "python.exe"
```

A normal POSIX Hermes checkout using `venv/bin/python` is ignored. The probe falls back to the context-tuner installer’s `sys.executable`, which may have different dependencies and import paths from Hermes.

Even on Windows, the actual `hermes` executable may be attached to a different virtual environment than `root/venv/Scripts/python.exe`.

#### Relative result paths

If `find_engine_dir()` returns a relative path, this comparison resolves it against the installer’s current working directory:

```python
Path(resolved).resolve() != target
```

It is not resolved relative to `root`. A legitimate loader result such as `plugins/context-tuner` can therefore be falsely rejected.

**Observable consequence:** A valid default or `--hermes-repo` installation can copy successfully and then return exit status 1. This is especially likely on POSIX, package-based Hermes installations, source checkouts without an initialized Windows-style venv, or loaders using relative imports.

**Required correction:** Invoke the actual Hermes executable or an official loader probe entry point. If manual import is unavoidable:

- use the actual runtime interpreter;
- support `venv/bin/python`;
- set `cwd=root`;
- add the expected root to `sys.path`;
- import under the real package name;
- add the module to `sys.modules`;
- and normalize loader-returned paths relative to the root.

---

### HIGH — `--set-config` can fail while the installer returns verified success

**Code path:** `src/hermes_context_tuner/cli.py`, approximately lines 148–172:

```python
if set_config:
    subprocess.run(
        ["hermes", "config", "set", "context.engine", "context-tuner"],
        check=False
    )
```

`main()`, approximately lines 236–255, subsequently reports success based only on path resolution.

**Reachability:** `hermes config set` can exit nonzero because of:

- permission errors;
- an invalid config location;
- an unsupported Hermes version;
- malformed existing configuration;
- a locked file;
- an unavailable backend.

Because `check=False` is used and the return code is ignored, verification continues. If the loader can resolve the installed directory, `main()` returns 0.

If the command is entirely absent, `subprocess.run()` raises `FileNotFoundError`, but ordinary nonzero command failures are silently ignored.

**Observable consequence:** A caller explicitly requesting `--set-config` receives successful installation output and exit status 0 even though Hermes remains configured to use another engine. The shim exists but will not be selected.

**Required correction:** Treat a nonzero config command as a verification failure and verify the effective `context.engine` value after writing it.

---

### HIGH — A legitimate pre-Hermes or pre-venv installation now becomes a fatal installer result

**Code path:** `src/hermes_context_tuner/cli.py`, approximately lines 13–22, 105–143, and 236–255.

When no explicit repository is supplied, `_repo_root_from_hermes()` tries `hermes --version`. If Hermes is not installed yet or does not emit the exact expected `Project:` line, it falls back to:

```python
Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent"
```

The user shim may nevertheless have been copied successfully to `$HERMES_HOME/plugins/context-tuner`. The probe then attempts to import a loader from the guessed repository and returns failure.

**Reachability:**

- installation sequences that install the plugin before Hermes;
- fresh machines where the Hermes venv is created later;
- POSIX systems without a Windows-style fallback tree;
- Hermes versions whose `--version` output has leading whitespace, ANSI formatting, localization, or no `Project:` line;
- package-managed Hermes installations without a source checkout.

**Observable consequence:** `install-hermes` now returns 1 after successfully copying the shim. Any caller using `set -e`, `&&`, a package-manager subprocess check, or CI’s default nonzero handling will abort. This is a concrete behavioral incompatibility even if fail-closed behavior is intended.

The requested `upgrade_hermes.sh` was not included in the supplied diff or post-fix source, so its exact handling cannot be verified. No claim that it safely handles the new exit status is supportable from the supplied material.

**Required correction:** Define and document separate outcomes, for example:

- `0`: copied and runtime-verified;
- a distinct nonzero code: copied but verification unavailable;
- another nonzero code: copied but verified broken.

Then update every installer caller explicitly. If policy requires all unprobeable cases to be fatal, the upgrade/install scripts must ensure Hermes and its runtime environment exist before invoking this command.

---

### MEDIUM — `verify_install()` ignores the probe process return code

**Code path:** `src/hermes_context_tuner/cli.py`, approximately lines 112–139:

```python
proc = subprocess.run(..., check=False)
...
data = json.loads(lines[-1]) if lines else {}
...
report["ok"] = not report["reasons"]
```

`proc.returncode` is never examined.

**Reachability:** A probe can print valid-looking JSON and then exit nonzero because of:

- a signal or process-level failure after output;
- an `atexit` handler;
- loader code calling `os._exit()` after flushing;
- wrapper/runtime instrumentation;
- a deliberately or accidentally nonzero interpreter shutdown.

If the parsed JSON names the target, verification succeeds despite the probe process failing.

**Observable consequence:** This violates the stated fail-closed rule: an unsuccessful probe process can produce `ok=True`.

**Required correction:** Add a reason whenever `proc.returncode != 0`, regardless of parseable stdout, and include bounded stderr diagnostics.

---

### MEDIUM — The stdout protocol can be spoofed or broken by loader output

**Code path:** `src/hermes_context_tuner/cli.py`, approximately lines 123–125:

```python
lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
data = json.loads(lines[-1]) if lines else {}
```

The last nonblank stdout line is assumed to be the probe response.

**Reachability:**

- loader import code logs JSON after the probe through an exit hook;
- loader code prints during interpreter teardown;
- instrumentation writes a trailing line;
- a compromised or merely unusual loader emits a final JSON object containing a forged `resolved` value.

Conversely, harmless trailing logging causes a false failure because the last line is not JSON.

**Observable consequence:** Verification can pass on forged output or fail on legitimate noisy output.

**Required correction:** Use a unique framed marker and parse only that marker, or write the response to a dedicated file descriptor. Also require a successful process exit.

---

### MEDIUM — Verification is only point-in-time and does not prevent a later bundled shadow

**Code path:** `src/hermes_context_tuner/cli.py`, approximately lines 91–110 and 112–139.

The static shadow check and loader probe are separate filesystem observations. Nothing prevents the bundled directory from being created or replaced after verification.

**Reachability:**

- a Hermes upgrade creates `plugins/context_engine/context-tuner` after the user shim was verified;
- another process lazily creates the bundled directory immediately after the probe;
- a symlink or directory is swapped between the static check, probe, and actual Hermes startup.

A shadow created between the static check and the probe should normally be noticed by `find_engine_dir()`. A shadow created after the probe is necessarily missed. A symlink swap can also invalidate both prior path comparisons.

**Observable consequence:** The installer reports verified success, but a later startup resolves a newly created bundled shadow. The September failure mode can recur after verification unless every Hermes upgrade reruns the installer and treats failure correctly.

Stable symlinks are not inherently missed: because both sides are resolved, a bundled symlink to a different target should be detected, and a symlink to the installed target loads the same files. Differently-cased paths are subject to platform path semantics. The unresolved issue is mutation/race, not stable aliasing.

**Required correction:** Verification cannot provide a permanent guarantee. Either:

- make Hermes loader precedence/configuration durable;
- remove or repair stale bundled entries during installation;
- add startup-time verification;
- or ensure every Hermes upgrade reruns verification after all bundled directories are finalized.

---

### MEDIUM — `_audit_failure()` can mutate delegate-owned state when wrapper `_health` is unbound

**Code path:** `src/hermes_context_tuner/engine.py`, approximately lines 160–170, in combination with `__getattr__()`, approximately lines 225–239.

```python
health = self._health
```

If the wrapper’s `_health` attribute is absent, normal attribute lookup invokes `__getattr__()`, which forwards unknown reads to the delegate:

```python
return getattr(self._delegate, name)
```

Because `_WRAPPER_LOCAL` is only consulted by `__setattr__()`, it does not protect reads.

**Reachability:**

```python
del engine._health
engine._delegate._health = {}
engine._audit_failure()
```

The helper obtains `delegate._health` and inserts/increments `audit_failures` there.

**Observable consequence:** Error accounting silently corrupts delegate-owned health state instead of remaining a wrapper-local no-op. If the delegate’s health mapping has different semantics, it can also trigger arbitrary side effects.

**Required correction:** Read wrapper-local state without delegation:

```python
health = object.__getattribute__(self, "_health")
```

The same principle applies to other supposedly wrapper-local fields used in recovery/error paths.

---

### LOW — `_audit_failure()` is non-throwing for ordinary `Exception`s under the stock class, but the absolute guarantee is overstated

**Code path:** `src/hermes_context_tuner/engine.py`, approximately lines 160–170.

For the unmodified base class, exceptions from:

- missing `_health`,
- `__getattr__`,
- `health.get`,
- integer conversion,
- and item assignment

are caught. The existing custom `__setattr__()` is not involved in the mapping assignment, so it does not by itself defeat the helper. The eleven ordinary call sites are therefore protected against normal `Exception` subclasses.

However, the guarantee “without ever raising” is not absolute:

1. `except Exception` does not catch `KeyboardInterrupt`, `SystemExit`, or `GeneratorExit` raised by a hostile mapping or accessor.
2. The class explicitly allows class-defined methods to be shadowed on the instance:

   ```python
   if name in type(self).__dict__:
       object.__setattr__(self, name, value)
   ```

   Thus:

   ```python
   engine._audit_failure = malicious_callable
   ```

   replaces the helper. Subsequent error paths invoke the replacement before the original helper’s internal `try` can run.
3. A subclass overriding `__getattribute__()` can raise during resolution of `self._audit_failure` before the method body executes.

**Observable consequence:** In extended, instrumented, or adversarial engine instances, an audit error can still escape or replace the original exception.

**Required correction:** If this must be robust against method shadowing, invoke the implementation explicitly:

```python
ContextTunerEngine._audit_failure(self)
```

and use `object.__getattribute__()` internally. Catching `BaseException` is generally undesirable, so the documentation should promise non-throwing only for ordinary `Exception`s, not literally every possible exception.

---

### LOW — `_terminalize_operation()` can raise before reaching either protected block

**Code path:** `src/hermes_context_tuner/engine.py`, approximately lines 173–197:

```python
if not operation_id or self._store is None:
    return
```

The access to `self._store` is outside either `try`.

**Reachability:** If `_store` is unbound, a custom `__getattr__`/delegate accessor can raise. A subclass can also intercept `_store` reads through `__getattribute__()`.

The current internal callers wrap `_terminalize_operation()` in another `try`, so the ordinary host-visible impact is limited. However, when it raises inside the post-bookkeeping handler, the subsequent cleanup assignments in that same outer block are skipped. That can compound the stale-pointer issue.

**Observable consequence:** The helper violates its documented “Never raises” contract and may prevent pending-state cleanup.

**Required correction:** Put all argument checks and attribute reads inside the protected block, using `object.__getattribute__()` for wrapper-local state.

---

### LOW — `bundled.resolve()` is outside the exception boundary used for shadow inspection

**Code path:** `src/hermes_context_tuner/cli.py`, approximately lines 91–103:

```python
try:
    shadow_exists = bundled.is_dir()
except Exception:
    shadow_exists = False

if shadow_exists and bundled.resolve() != target:
```

Only `is_dir()` is protected. Resolution occurs afterward.

**Reachability:** Filesystem permission errors, path-resolution failures, unusual network filesystems, or concurrent path manipulation can make `resolve()` fail after `is_dir()` succeeded.

**Observable consequence:** `verify_install()` itself raises instead of returning a structured failed report. `main()` does not catch it, so the installer emits a traceback rather than the promised verification failure message and exit path.

**Required correction:** Resolve the path inside the same guarded block, and treat inability to inspect the bundled path as a verification reason rather than pretending no shadow exists.

---

## Test deficiencies

### MEDIUM — `test_clean_tree_verifies_against_the_loader` exercises the fake loader, not the real Hermes loader

**Code path:** `tests/test_install_verify.py`, approximately lines 20–67.

The test writes `FAKE_LOADER` into a temporary `plugins/context_engine/__init__.py`. The subprocess imports that file. No installed or checked-out Hermes loader is involved.

The fake also omits the conditions most likely to break the production probe:

- relative imports;
- package-level dependencies;
- Hermes runtime initialization;
- config lookup;
- manifest parsing;
- plugin import;
- registration;
- engine instantiation.

It additionally resolves the “user” plugin under `root/plugins/context-tuner`, while the production default installer uses `$HERMES_HOME/plugins/context-tuner`. The test therefore does not validate default-install topology.

**Observable consequence:** The test can remain green while the real Hermes loader cannot be imported by the synthetic probe or while the installed engine cannot load.

---

### MEDIUM — The clean-tree test positively encodes the path-only false-success bug

**Code path:** `tests/test_install_verify.py`, approximately lines 50–67.

The installed target contains an empty `__init__.py` and skeletal manifest, yet the test expects `report["ok"] is True`. This proves only that the fake loader returns the directory. It does not prove that an engine exists there.

**Observable consequence:** Strengthening `verify_install()` to actually import/register the engine may require changing this test; as written, it protects the inadequate behavior.

---

### MEDIUM — No test exercises `main()` or the new installer exit-code contract

No supplied test invokes:

```python
main(["install-hermes", ...])
```

or runs the console entry point in a subprocess.

Therefore the tests do not establish that:

- success returns 0;
- shadow/unprobeable cases return 1;
- diagnostics are printed as expected;
- `--set-config` failure is propagated;
- a successful copy followed by failed verification is handled by callers;
- `--hermes-repo` works under a real checkout;
- automation tolerates the new nonzero result.

`test_install_engine_writes_engine_files_into_user_dir` only checks copying and never calls `verify_install()`.

---

### MEDIUM — Journal tests cover only the normal successful interrupt method

**Code path:** `tests/test_pending_row_terminal_state.py`, approximately lines 20–63.

The tests force `_fingerprint_messages()` to raise and then use the normal real store. They do not exercise:

- `mark_interrupted_if_started()` raising;
- fallback to `fail_compression()`;
- an uncertain first write;
- a finalized row;
- concurrent finalization;
- partial pending-state publication;
- `_pending_operation` succeeding while a later field assignment fails;
- later `on_session_start()` touching the same operation ID;
- `_store` being missing/unbound;
- store methods updating and then raising.

As a result, they prove the single happy terminalization path but not the double-terminalization/provenance cases introduced by the fallback and partial publication handler.

---

### LOW — The `_audit_failure()` test does not cover the unbound or delegated-health cases

**Code path:** `tests/test_pending_row_terminal_state.py`, approximately lines 50–57.

The test assigns malformed values to `_health`, but does not delete it. It therefore does not invoke `__getattr__()` or detect mutation of a delegate-owned `_health`.

It also does not cover:

- a custom mapping whose `get()` raises;
- a custom mapping whose `__setitem__()` raises;
- instance shadowing of `_audit_failure`;
- a subclass with custom `__getattribute__`;
- `BaseException` subclasses.

For ordinary malformed values, the test is adequate; it does not prove the absolute wording.

---

## Direct answers to A–F

### A. Double-terminalization / provenance corruption

The normal fingerprint-failure path now terminalizes and clears the pointer, so that specific test path does not later finalize the same ID.

Two unresolved paths remain:

1. `_terminalize_operation()` can fall back to `fail_compression()` after a successful delegate operation, directly recording false failure provenance.
2. Partial pending-state publication can leave `_pending_operation` set after the row was marked interrupted. `on_session_start()`, the next `compress()`, session end, or reset can then touch the same ID again. Whether interrupted can actually be overwritten as finalized depends on the conditional SQL in `RecoveryPointerStore.finalize_compression()`, which was not provided.

### B. `_audit_failure()` non-throwing behavior

Under the stock class, ordinary `Exception`s from malformed, missing, or hostile `_health` values are caught at all shown call sites. The custom base `__setattr__()` does not interfere with dictionary item assignment.

It is not absolute: the method can be shadowed, subclass attribute lookup can fail before entry, and `BaseException` subclasses escape. An unbound `_health` can also be forwarded to and mutate the delegate’s `_health`.

### C. New silent-drop path

Yes. A valid plan can be discarded solely because `focus_topic` length or optional `force` truth conversion fails. The operation then has no journal row despite successful compression. Recovery data, plan decisions, token totals, provenance, and checkpoint information are lost. Setting `plan=None` is not truthful when the plan was already built successfully.

### D. What the tests prove

`test_clean_tree_verifies_against_the_loader` exercises the fake loader, not the real Hermes loader. It proves only path equality.

The shadow test would fail if the code that appends the `"loader resolves ..."` mismatch reason were deleted, because it explicitly asserts that phrase. However, it does not prove actual loader integration or engine loading; the static bundled-directory check and fake resolver dominate the scenario. It would remain green if discovery/import/registration verification were absent—which they currently are.

### E. Wrong passes and wrong failures

Wrong pass cases include:

- target directory resolves correctly but plugin import fails;
- discovery raises or returns no valid engine;
- manifest is invalid;
- registration/instantiation fails;
- `--set-config` exits nonzero;
- probe process exits nonzero after emitting acceptable JSON.

Wrong failure cases include:

- legitimate loader uses relative imports;
- actual Hermes runtime is `venv/bin/python`;
- Hermes depends on repository `sys.path` or working directory;
- loader returns a relative path;
- Hermes is package-installed without the guessed source tree;
- valid `--hermes-repo` checkout lacks the assumed Windows venv layout.

A stable symlink to a different target should be detected; a stable symlink to the installed target is not a harmful shadow. A shadow created or swapped after the probe is missed.

### F. Operational impact

Returning 1 is a breaking behavioral change for any caller using standard fatal-on-nonzero semantics. It is especially problematic because the command mutates the filesystem first and can then fail solely because Hermes, its checkout, or its venv is not yet available.

`upgrade_hermes.sh` and other installer scripts were not supplied, so their exact behavior cannot be audited here. There is also no test demonstrating that any caller was updated to account for the new return code.