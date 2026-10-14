{"schema_version": 1, "tier": "sol", "model": "gpt-5.6-sol", "response_id": "resp_05c651f0a00f22c9016acf5cf1cd1c819a875c7acf373c2d32", "success": true, "reason": "completed", "reasoning_effort": "high", "usage": {"input_tokens": 22287, "output_tokens": 21644, "total_tokens": 43931}}
# Hostile audit findings

Line numbers are approximate and refer to the supplied post-fix files.

---

## CRITICAL

### 1. F2 does not eliminate the exact shadowing failure that caused the outage

**Files/functions**

- `src/hermes_context_tuner/cli.py`, `install_engine()`, approximately lines 43–68
- Hermes loader behavior described in the patch: bundled directory wins merely by existing

**Trigger**

1. A bundled directory already exists at:

   ```text
   <hermes-agent>/plugins/context_engine/context-tuner/
   ```

2. It is empty, partially populated, stale, or contains an older shim.
3. `install_engine()` is run without `--hermes-repo`.
4. The installer copies the new shim to:

   ```text
   $HERMES_HOME/plugins/context-tuner/
   ```

5. The loader continues resolving the existing bundled directory first.

**What breaks**

The installer reports success, but the newly installed user shim remains unreachable. An empty bundled directory reproduces the September outage exactly: the user install exists and survives upgrades, yet the loader resolves the empty runtime-owned directory and registers nothing.

The fix only changed where new files are copied. It did not:

- inspect the path that the actual loader resolves;
- remove or quarantine a stale bundled directory;
- refuse installation when a bundled shadow exists;
- verify that `find_engine_dir("context-tuner")` resolves to the newly installed target;
- instantiate the engine after installation;
- verify that the registered engine is actually `context-tuner`.

The new test actively misses this. `tests/test_install_target.py::test_install_defaults_to_user_plugin_dir()` merely asserts that a synthetic path under `tmp_path` was not touched. It never creates a stale bundled shadow and never invokes the host loader.

**Production impact**

A Hermes upgrade can recreate or leave behind the bundled directory and immediately disable the supposedly “upgrade-resilient” user installation. The CLI still prints:

```text
installed context-tuner engine shim to ...
```

This is a false-success deployment path for the same P1 incident.

**Required remediation**

After copying, the installer must invoke the live loader in the target Hermes environment and require all of the following:

- the resolved path is the exact installed user path after canonicalization;
- `__init__.py` and `plugin.yaml` are usable;
- loading produces the expected engine identity;
- registration actually occurs.

If a bundled shadow exists, installation should fail loudly or remove it only under an explicit, safe migration operation.

---

## HIGH

### 2. “Compute-then-commit” is false: successful compressions leave permanent `started` rows when post-delegate bookkeeping fails

**File/function**

- `src/hermes_context_tuner/engine.py`, `ContextTunerEngine.compress()`, approximately lines 390–423

**Trigger**

Any exception after `begin_compression()` succeeds and the delegate succeeds, including:

- `_fingerprint_messages()` raises;
- `len(result)` raises;
- `list(result)` raises for a list/tuple subclass;
- `_checkpoint_refresh_needed()` raises;
- `_compressions_since_checkpoint += 1` raises due corrupted state.

**Problematic flow**

```python
pending_op = self._store.begin_compression(...)
result = delegate.compress(...)          # succeeds

try:
    ...
    fingerprint = _fingerprint_messages(...)
    self._compressions_since_checkpoint += 1
    hint = ...
except Exception:
    ...
    self._pending_operation = None
    return result
```

The database operation was already opened before the delegate call. On bookkeeping failure, the code merely drops the in-memory pointer:

```python
self._pending_operation = None
```

It does not call either:

```python
fail_compression(...)
```

or:

```python
mark_interrupted_if_started(...)
```

for the local `pending_op`.

**What breaks**

The sidecar retains a `started` row that can never be finalized by this engine instance because its operation ID has been discarded. `_reconcile_stale_operations()` runs only during engine construction, not periodically and not on the next compression for unreferenced rows. A long-lived process can therefore retain false in-flight operations indefinitely.

This directly contradicts both claims:

- “no half-registered pending operation is left behind”;
- “compute-then-commit.”

The database write is already committed before the purported compute phase.

**Production impact**

- Journal status falsely reports an operation still in progress.
- Recovery/audit consumers can infer an active compression that no longer exists.
- Repeated bookkeeping faults accumulate orphan rows.
- The only automatic cleanup requires a later engine restart after the row exceeds the stale-age threshold.
- A successful host compression loses its terminal audit record.

**Test defect**

`test_bookkeeping_failure_returns_the_delegate_result()` and `test_checkpoint_hint_failure_does_not_discard_result()` assert only:

```python
assert engine._pending_operation is None
```

They never inspect `engine._store.status_counts()`. Both tests pass while leaving exactly the persistent corruption described above.

---

### 3. Post-delegate failure handling itself can still raise and discard a successful compression

**File/function**

- `src/hermes_context_tuner/engine.py`, `compress()`, approximately lines 407–415

**Trigger**

A bookkeeping exception occurs, followed by an exception in any statement in the handler:

```python
except Exception:
    self._health["audit_failures"] += 1
    self._pending_operation = None
    self._pending_result_fingerprint = None
    self._pending_checkpoint_hint = False
    return result
```

Concrete examples:

- `_health` lacks `"audit_failures"`;
- `_health` is malformed or replaced during a race;
- the value is non-numeric;
- a subclass or future change makes one of the assignments raise;
- `__setattr__` is altered or monkeypatched;
- memory exhaustion occurs during the handler.

**What breaks**

The delegate has already completed successfully, but the exception handler is not protected. An exception in the handler escapes and discards the successful compression result—the exact F4 failure class the patch claims to eliminate.

The same defect exists in nearly every “fail-open” handler throughout `compress()`: error-accounting is treated as infallible even though it is wrapper-owned bookkeeping.

**Production impact**

A corrupted health structure or a secondary bookkeeping bug turns a successful compression into a host-visible failure.

**Required remediation**

Use a non-throwing helper that performs health accounting defensively, and put all post-success cleanup and return logic under a final outer boundary that cannot be defeated by audit bookkeeping.

---

### 4. Several wrapper operations still execute before the delegate and outside any fail-open boundary

**File/function**

- `src/hermes_context_tuner/engine.py`, `compress()`, approximately lines 268–344

#### 4a. Default budget conversion occurs before the `try`

```python
context_budget = int(current_tokens or 128_000)
tail_budget = max(8_000, context_budget // 5)
```

**Trigger**

```python
engine.compress(messages, current_tokens="unknown")
```

or any object whose `__bool__()` or `__int__()` raises.

**Impact**

The delegate is never called, even if it would accept, ignore, or normalize that input.

#### 4b. Lossiness extraction occurs outside the planning boundary

```python
lossy = bool(plan is not None and any(
    d.decision in (...) for d in plan.decisions
))
```

**Trigger**

`build_budget_plan()` returns an object whose:

- `decisions` is `None`;
- `decisions` iterator raises;
- decision lacks `.decision`;
- `.decision` property raises.

This can happen through package/version skew or a changed `BudgetPlan` implementation without `build_budget_plan()` itself raising.

**Impact**

Compression is denied before delegate invocation.

#### 4c. Trigger construction occurs outside a wrapper boundary

```python
"force": bool(kwargs.get("force", False)),
"focus_topic_present": bool(focus_topic),
"focus_topic_chars": len(focus_topic) if focus_topic else 0,
```

**Trigger**

A non-string `focus_topic` with no `len()`, or an object with a throwing `__bool__()`/`__len__()`.

**Impact**

The delegate is never invoked.

#### 4d. Pending-operation inspection is unprotected

```python
if self._pending_operation:
```

Normally initialized state makes this work, but the absolute “wrapper failure always degrades” contract is not enforced around it.

**Production impact**

F4 moved one specific planner call under a `try`; it did not establish a complete fail-open boundary around all wrapper work preceding the delegate.

---

### 5. The compatibility shim can deny a compression before entering the delegate body

**File/function**

- `src/hermes_context_tuner/engine.py`, `_invoke_compatible()`, approximately lines 55–79
- Called from `compress()`, approximately lines 347–359

#### 5a. Arbitrary signature-introspection failures are not handled

```python
try:
    signature = inspect.signature(fn)
except (TypeError, ValueError):
    ...
```

**Trigger**

A callable exposes a broken `__signature__` that raises `RuntimeError`, `AttributeError`, `RecursionError`, or another exception.

**Impact**

The delegate body is never called. The outer `compress()` catches the exception as though the delegate failed, even though the failure came from wrapper introspection.

This violates the intended “the host must get the compression it would have received without the wrapper” behavior.

#### 5b. Opaque callables are always invoked with the richest candidate

When introspection is unavailable, the code does:

```python
args, kwargs = candidates[0]
return fn(*args, **kwargs)
```

**Trigger**

An opaque/C-backed callable supports:

```python
compress(messages)
```

but not `current_tokens`, `focus_topic`, or newer kwargs.

**Impact**

It raises `TypeError`; the wrapper refuses to try the valid narrow call and reports a delegate failure. A delegate that could compress is denied.

#### 5c. Positional-only compatible delegates are rejected

**Trigger**

A valid delegate has a signature such as:

```python
compress(messages, current_tokens, /)
```

Every supplied candidate passes `current_tokens` by keyword or omits it, so none bind.

**Impact**

`DelegateSignatureError` escapes even though a valid call shape exists.

#### 5d. Fallback candidates silently drop legitimate host kwargs

Candidate one includes all `kwargs`; every narrower candidate discards them wholesale.

**Trigger**

The host supplies both:

- a delegate-supported option such as `force=True`;
- one unsupported extension kwarg.

The rich candidate fails binding. The next candidate removes both the unsupported kwarg and the supported `force=True`.

**Impact**

Compression behavior changes silently. A forced compression can become an ordinary call or a no-op.

---

### 6. The wrapper can finalize and count a “compression” that never happened

**File/function**

- `src/hermes_context_tuner/engine.py`, `compress()`, approximately lines 390–423
- `on_session_start()`, approximately lines 425–484

**Trigger**

The delegate returns the original messages unchanged, for example because it refuses a compression that would grow the context:

```python
def compress(messages, ...):
    self._last_compress_refused_would_grow = True
    return messages
```

Then the host emits a compression boundary callback:

```python
engine.on_session_start(
    "new-session",
    boundary_reason="compression",
    old_session_id="old-session",
)
```

**What breaks**

The wrapper:

- increments `_compressions_since_checkpoint`;
- keeps the plan-derived `lossy` value;
- records a pending operation;
- can set `checkpoint_required=True`;
- finalizes the journal row as a compression;
- records `compressed_count`, even if equal to the original count.

There is no check that:

- the result differs from the input;
- message count or token count decreased;
- the delegate’s refusal telemetry says compression was refused;
- a compression boundary actually corresponds to a transformation.

The code even contains evidence that refusal is a supported delegate condition: host state such as `_last_compress_refused_would_grow` is explicitly forwarded by the new `__setattr__` behavior.

**Production impact**

The audit trail and checkpoint cadence can claim compressions that did not occur. This is worse than losing an audit row because it creates affirmative false provenance.

---

### 7. Broad post-delegate swallowing masks corrupt delegate results

**File/function**

- `src/hermes_context_tuner/engine.py`, `compress()`, approximately lines 397–415

**Trigger**

The delegate returns a malformed result object whose failure is deferred until the wrapper inspects it. For example:

```python
class BrokenList(list):
    def __len__(self):
        raise RuntimeError("corrupt delegate result")

def compress(...):
    return BrokenList(...)
```

or a tuple/list subclass whose iteration raises during:

```python
list(result)
```

**What happens**

The delegate call technically returns. The wrapper then catches the result-access error as “wrapper bookkeeping” and returns the corrupt object:

```python
except Exception:
    ...
    return result
```

**What breaks**

The delegate-result boundary is not the synchronous return from `compress()`. Result validation/materialization can still execute delegate-controlled methods. Those exceptions are swallowed, so real delegate corruption is hidden and handed to the host.

The host will often fail later, away from the compression call, with the original provenance obscured.

**Production impact**

- Corrupt delegate output is presented as a successful compression.
- The journal row remains `started`.
- The only visible signal is a generic increment in `audit_failures`.
- No exception type or location is logged.

**Boundary conclusion**

Synchronous exceptions thrown inside the direct delegate call generally propagate, but failures exposed while interrogating the returned object do not. The intended boundary is therefore not real for arbitrary valid Python return objects.

---

### 8. The engine is not concurrency-safe; concurrent compressions corrupt lineage and pending state

**File/functions**

- `src/hermes_context_tuner/engine.py`, `compress()`, approximately lines 268–423
- `on_session_start()`, approximately lines 425–484
- `_handle_checkpoint_tool()`, approximately lines 620–650

No lock protects:

- `_pending_operation`;
- `_pending_count`;
- `_pending_result_fingerprint`;
- `_pending_checkpoint_hint`;
- `_pending_old_session_id`;
- `_session_id`;
- `_compressions_since_checkpoint`;
- `_store` initialization/replacement.

**Concrete interleaving**

1. Compression A checks `_pending_operation`; it is `None`.
2. Compression A creates DB row `opA`.
3. Delegate A begins and releases the GIL or blocks.
4. Compression B checks `_pending_operation`; it is still `None`, because A does not publish it until after the delegate returns.
5. B creates `opB`.
6. A succeeds and publishes `_pending_operation = opA`.
7. B succeeds and overwrites it with `_pending_operation = opB`.
8. A’s boundary callback arrives.
9. If both operations share the same old session ID—or the callback omits one—the callback finalizes `opB` using whichever B fields are currently stored.
10. `opA` remains permanently `started`.

A second interleaving is even simpler:

1. A returns and sets pending state.
2. B starts before A’s boundary callback.
3. B calls `_close_pending_on_teardown()` and marks A interrupted.
4. A’s valid boundary later has no operation to finalize.

**Production impact**

- Successful compressions are marked interrupted.
- One compression can finalize another compression’s row.
- Result fingerprints and session lineage can be cross-associated.
- Started rows leak.
- Checkpoint counts can be lost or reset incorrectly.
- Callback correlation is not sufficient when sessions share IDs or IDs are absent.

A singleton plugin in an autonomous runtime must assume overlapping sessions, callbacks, or tool calls unless the host contract explicitly guarantees serialization.

---

### 9. Boundary correlation is fail-open in the wrong direction and can finalize the wrong operation

**File/function**

- `src/hermes_context_tuner/engine.py`, `on_session_start()`, approximately lines 429–444

```python
mismatched = (
    boundary
    and bool(expected_old)
    and bool(self._pending_old_session_id)
    and expected_old != self._pending_old_session_id
)
```

**Trigger**

A compression boundary arrives with either:

- no `old_session_id`;
- an empty `old_session_id`;
- pending state whose old session ID is empty;
- a callback from another concurrent compression sharing the same old session ID.

**What happens**

Missing correlation data is treated as a match:

```python
if boundary and not mismatched:
    finalize_compression(...)
```

**What breaks**

An unrelated compression boundary can commit the current pending operation. The comment says it correlates the boundary, but the implementation only rejects a callback when both IDs are present and unequal.

**Production impact**

False lineage and incorrect result fingerprints can be committed as authoritative audit records.

For an audit system, lack of correlation should be “cannot finalize,” not “assume match.”

---

## MEDIUM

### 10. Checkpoint cadence is inconsistent across bookkeeping failure locations

**File/function**

- `src/hermes_context_tuner/engine.py`, `compress()`, approximately lines 397–423
- `_checkpoint_refresh_needed()`, approximately lines 161–176

The counter mutation is inside the supposedly compute-only phase:

```python
fingerprint = ...
self._compressions_since_checkpoint += 1
hint = lossy and self._checkpoint_refresh_needed(current_tokens)
```

This creates different cadence behavior depending on where the same post-success failure occurs.

#### Path A: `len(result)` or fingerprinting fails

The successful physical compression is not counted.

Example:

- Counter starts at 2.
- Third successful compression occurs.
- Fingerprinting raises.
- Counter remains 2.
- Fourth successful compression increments it to 3 and becomes the first eligible hint.

**Window:** one compression late per fingerprint/count failure. Repeated failures postpone the threshold indefinitely.

#### Path B: `_checkpoint_refresh_needed()` fails

The same successful compression is counted because the increment occurred first.

Example:

- Counter starts at 2.
- Third successful compression increments to 3.
- Policy evaluation raises.
- Result is returned, but no pending operation survives.
- Fourth compression observes 4.

**Window:** the threshold count is retained, but the checkpoint hint for the threshold-crossing operation is lost.

#### Path C: planning fails

The delegate succeeds and the counter advances, but:

```python
lossy = False
```

because `plan is None`.

If that physical compression was actually lossy, no hint is recorded at the threshold. The next classifiable lossy compression receives the hint.

**Window:** at least one lossy compression beyond the threshold; unbounded if later operations are not classified as lossy.

#### Path D: journal opening fails

The counter and `hint` are computed, but:

```python
pending_op = None
self._pending_checkpoint_hint = hint
```

No callback can consume the hint because `on_session_start()` is guarded by:

```python
if self._pending_operation:
```

**Window:** the threshold hint is silently discarded. The stale private hint remains set until another compression overwrites it.

#### Path E: non-lossy and no-op calls

Every successful delegate return increments the counter, not only lossy transformations.

A sequence of:

1. no-op;
2. lossless/no-op;
3. first actual lossy compression;

can mark the first lossy compression as checkpoint-required.

If the intended rule is “three lossy compressions,” this is two compressions early.

#### Path F: process restart

`__init__()` always resets:

```python
self._compressions_since_checkpoint = 0
```

No value is restored from the checkpoint ledger or recovery journal.

**Window:** up to two or more real compressions can be forgotten on every engine restart. Frequent restarts can prevent the threshold from ever being reached.

#### Path G: external checkpoint

Only `_handle_checkpoint_tool()` resets the counter. A checkpoint written through:

- the CLI;
- another process;
- direct `CheckpointLedger`;
- another runtime instance;

does not reset this live engine.

**Impact:** stale high counts generate checkpoint warnings after a checkpoint already exists.

#### Path H: concurrent checkpoint and compression

A successful checkpoint can reset the counter between increment, policy evaluation, and pending-state commit. The compression can either:

- lose its increment;
- produce a stale `checkpoint_required=True` after a checkpoint;
- read zero and omit a needed hint.

The cadence is not atomic relative to checkpoint creation.

---

### 11. `_last_plan` becomes stale after planning failure

**File/function**

- `src/hermes_context_tuner/engine.py`, `compress()`, approximately lines 274–295
- `get_status()`, approximately lines 510–520

**Trigger**

1. One compression successfully creates plan A.
2. A later call makes `build_budget_plan()` raise.
3. The exception handler sets only local `plan = None`.

It does not clear:

```python
self._last_plan
```

**What breaks**

`get_status()` continues reporting plan A as `"last_plan"` even though a newer compression occurred without a valid plan.

**Production impact**

Operators and agents see stale planning data and can attribute it to the wrong compression. This is especially dangerous after the exact degraded path F4 introduced.

The planning-failure test initializes `_last_plan = None`, so it cannot detect this.

---

### 12. Degraded paths leave stale pending fields even when `_pending_operation` is `None`

**File/function**

- `src/hermes_context_tuner/engine.py`, `compress()`, approximately lines 407–423
- `on_session_start()`, approximately lines 425–484

On post-bookkeeping failure the handler clears:

```python
_pending_operation
_pending_result_fingerprint
_pending_checkpoint_hint
```

but does not clear:

```python
_pending_count
_pending_old_session_id
```

When planning or store opening fails but bookkeeping succeeds, the method assigns:

```python
self._pending_operation = None
self._pending_count = count
self._pending_result_fingerprint = fingerprint
self._pending_old_session_id = self._session_id
self._pending_checkpoint_hint = hint
```

Because `on_session_start()` only clears pending fields when `_pending_operation` is truthy, this shadow pending state survives the boundary callback.

**Production impact**

The instance can contain a fingerprint, count, old session ID, or checkpoint hint that does not correspond to any operation. Future diagnostics, maintenance code, or races can consume stale values.

This also disproves the claim that bookkeeping is committed atomically.

---

### 13. The orphan-teardown fallback discards the pointer without ensuring the database row is terminal

**File/function**

- `src/hermes_context_tuner/engine.py`, `compress()`, approximately lines 297–310
- `_close_pending_on_teardown()`, approximately lines 486–507

**Trigger**

`_close_pending_on_teardown()` raises before successfully marking the row interrupted. The test simulates exactly this by replacing the entire method with one that raises.

The caller does:

```python
except Exception:
    self._health["audit_failures"] += 1
    self._pending_operation = None
```

**What breaks**

- The DB row can remain `started`.
- The operation ID is discarded.
- `_pending_count`, `_pending_result_fingerprint`, `_pending_checkpoint_hint`, and `_pending_old_session_id` are not cleared by the caller.
- The next compression can overwrite those fields.

The production `_close_pending_on_teardown()` catches most store exceptions internally, so the new test is largely testing a monkeypatched method rather than the real failure mode. If the real method’s own health increment or cleanup raises, this outer path still leaves partial state.

**Production impact**

The “unwedgeable orphan is dropped” behavior converts a recoverable pointer into an untraceable persistent orphan.

---

### 14. Error reporting can replace the original delegate exception

**File/function**

- `src/hermes_context_tuner/engine.py`, delegate exception handlers in `compress()`, approximately lines 361–389

**Trigger**

1. Delegate raises.
2. `fail_compression()` also raises.
3. The nested handler executes:

   ```python
   self._health["audit_failures"] += 1
   ```

4. That increment raises due malformed or concurrently corrupted health state.

**What breaks**

The secondary wrapper exception escapes instead of the original delegate error.

A related issue is that `_pending_operation = None` occurs after failure reporting. Any exception before it can leave stale in-memory pending state.

**Production impact**

The true compression failure is masked by audit-side failure handling, violating the requirement that delegate failures propagate intact enough to diagnose.

The tests contain no delegate-failure propagation case at all.

---

### 15. `except Exception` swallows serious process-integrity failures without useful evidence

**File/function**

- `src/hermes_context_tuner/engine.py`, multiple blocks in `compress()`, especially approximately lines 286–294 and 407–415

`MemoryError`, `RecursionError`, and many interpreter/resource failures are subclasses of `Exception`.

**Trigger**

Memory exhaustion or runaway recursion occurs while planning, fingerprinting, or evaluating the checkpoint policy.

**What happens**

The engine attempts to increment a generic counter and continue. No exception class, stack, operation ID, or location is logged.

**What breaks**

The code cannot distinguish a recoverable sidecar write failure from process corruption. Returning a large delegate result during `MemoryError` is not necessarily safe.

**Production impact**

Severe runtime faults are converted into an opaque `audit_failures += 1`, delaying detection and potentially handing the host partially processed state.

A narrower exception policy or structured non-content-bearing error telemetry is needed.

---

### 16. Windows home resolution is unstable and inconsistent with the rest of the package

**Files/functions**

- `src/hermes_context_tuner/cli.py`, `_hermes_home()`, approximately lines 25–34
- `src/hermes_context_tuner/engine.py`, `default_store_path()`, approximately lines 82–85
- `src/hermes_context_tuner/engine.py`, `_default_ledger_path()`, approximately lines 88–91
- `src/hermes_context_tuner/cli.py`, `_ledger_path()`, approximately lines 71–74

**Trigger on first installation**

On Windows with no `HERMES_HOME` and no pre-existing:

```text
%USERPROFILE%\AppData\Local\hermes
```

the installer chooses:

```text
%USERPROFILE%\.hermes\plugins\context-tuner
```

because `_hermes_home()` uses directory existence to choose the default.

Later Hermes creates and uses:

```text
%USERPROFILE%\AppData\Local\hermes
```

The installed shim remains under `.hermes` and is invisible.

**Additional defects**

- It ignores `%LOCALAPPDATA%`, so redirected/nonstandard Windows profiles resolve incorrectly.
- It does not check the operating system. On Linux/macOS, the accidental existence of `~/AppData/Local/hermes` changes the install target.
- `HERMES_HOME="~/.hermes"` is not expanded with `expanduser()`.
- A relative `HERMES_HOME` produces a CWD-dependent installation.
- The engine’s store and ledger functions always default to `~/.hermes`, even when the installer chose `AppData/Local/hermes`.

**Production impact**

The plugin, recovery database, checkpoint ledger, CLI, and watchdog can observe different Hermes homes on the same machine. On Windows this can produce a loaded plugin with an apparently missing store, or an installed plugin that the loader never sees.

---

### 17. `install_engine()` is non-atomic and can expose partial or mixed-version shims

**File/function**

- `src/hermes_context_tuner/cli.py`, `install_engine()`, approximately lines 43–68

**Trigger**

Process crash, disk-full condition, antivirus interference, or a concurrent loader between:

```python
shutil.copy2(source / "__init__.py", target / "__init__.py")
shutil.copy2(source / "plugin.yaml", target / "plugin.yaml")
```

**What breaks**

The loader can observe:

- new `__init__.py` with old `plugin.yaml`;
- old code with new metadata;
- only one file;
- a truncated or partially replaced file.

There is no staging directory, fsync, atomic rename, lock, backup, or post-copy verification.

Existing extra files are not removed, so stale shim artifacts can survive upgrades.

**Production impact**

The installer can itself create a “non-empty but unusable” directory that shadows the valid user copy or reports misleading provenance.

---

### 18. `--set-config` silently ignores configuration failure

**File/function**

- `src/hermes_context_tuner/cli.py`, `install_engine()`, approximately lines 65–67

```python
subprocess.run(
    ["hermes", "config", "set", "context.engine", "context-tuner"],
    check=False,
)
```

**Trigger**

- `hermes` exits nonzero;
- configuration is read-only;
- the wrong `hermes` executable is on `PATH`;
- the command writes to a different home;
- configuration validation rejects the value.

**What breaks**

The installer still returns the target and the CLI prints a success message.

**Production impact**

The shim can be correctly copied but never activated. This is another false-success installation path masked by the host fallback.

---

### 19. The watchdog skips all checks when the engine is selected by a default, `auto`, environment, or an unparsed config

**File/function**

- `scripts/dep_health.py`, `configured_engine()`, approximately lines 104–121
- `run()`, approximately lines 343–350

```python
if not engine or engine in ("builtin", "auto"):
    result["checks"]["engine"] = "disabled or auto — no external engine contract to enforce"
```

**Trigger**

Any of the following:

- `config.yaml` is absent but Hermes defaults to `context-tuner`;
- config says `auto`, and auto-selection chooses `context-tuner`;
- the engine is selected through an environment variable or command-line override;
- PyYAML is unavailable and the fallback regex cannot parse valid YAML such as:

  ```yaml
  context: {engine: context-tuner}
  ```

- the config format changes;
- the config file used by Hermes differs from `HOME / "config.yaml"`.

**What breaks**

Every relevant check is skipped:

- empty shadowing shim;
- provenance;
- loader canary;
- fallback markers;
- recording canary;
- store activity.

**Production impact**

The exact September failure can recur while the watchdog writes a nominal state file saying there is no external contract to enforce.

The watchdog must query the live host’s effective engine selection, not infer it from one YAML path.

---

### 20. The provenance check is not actually a provenance invariant

**File/function**

- `scripts/dep_health.py`, `check_provenance()`, approximately lines 155–217

#### 20a. Missing or malformed `plugin.yaml` passes

Version checking occurs only if:

```python
if manifest.exists():
```

If the manifest is absent, no P2 is added. If the version regex does not match, no P2 is added.

The function then returns:

```python
"... manifest matches package"
```

even though no manifest/version was verified.

#### 20b. Missing installed package metadata passes

```python
pkg = _installed_pkg_version()
if mv and pkg and mv.group(1) != pkg:
```

If package metadata is unavailable, `pkg == ""` and provenance passes.

#### 20c. Paths are compared lexically, not canonically

```python
resolved.relative_to(AGENT)
```

Neither path is normalized with `resolve()`.

**Trigger**

The engine path is:

- a symlink or junction outside `AGENT` pointing into it;
- a relative path under `AGENT`;
- an alternate/case-varied Windows spelling;
- a path containing `..`.

**Impact**

A runtime-owned installation can be classified as outside the runtime tree and “upgrade resilient.”

#### 20d. Any path outside `AGENT` is treated as durable

A temporary directory, package cache, removable drive, or another upgrade-owned tree passes.

**Production impact**

The tripwire can certify an installation whose code is missing metadata, not installed as a package, or still physically located in the runtime-owned tree.

---

### 21. The loader canary accepts any non-`None` object, including a fallback engine

**File/function**

- `scripts/dep_health.py`, `check_canary()`, approximately lines 220–240

```python
m = re.search(r"ENGINE (\S+)", out)
if m and m.group(1) not in ("None",):
    return True
```

**Trigger**

`load_context_engine("context-tuner")` returns:

- the built-in compressor;
- a generic fallback engine;
- the wrong plugin;
- a stub/proxy object.

**What breaks**

The canary reports healthy because it verifies only that the type name is not the literal string `None`.

It does not verify:

- `e.name == "context-tuner"`;
- `isinstance(e, ContextTunerEngine)`;
- module/package provenance;
- the resolved engine path;
- successful registration of the configured engine.

**Production impact**

A loader that silently returns the fallback compressor can pass the synthetic canary—the exact class of outage the watchdog is supposed to detect.

---

### 22. `check_shim()` trusts stdout substrings and ignores subprocess exit status

**File/function**

- `scripts/dep_health.py`, `check_shim()`, approximately lines 123–153

```python
if "OK True" in out:
    return True
```

**Trigger**

The probe prints `OK True`, then exits nonzero due a later error, shutdown exception, or injected/noisy import output.

Alternatively, imported code emits an unrelated line containing that substring.

**What breaks**

The shim is marked healthy despite a failed probe process.

**Production impact**

A loader/import failure can be hidden by partial stdout.

The code must require `r.returncode == 0` and parse a structured, unambiguous result.

---

### 23. Fallback-log cursor handling can permanently miss outage markers

**File/function**

- `scripts/dep_health.py`, `check_fallback_markers()`, approximately lines 242–284

#### 23a. First run deliberately discards all existing evidence

```python
if first_run:
    ... offset = size
    return 0
```

Deploying the watchdog after an outage ignores all historical fallback markers, including the 19 days of evidence that motivated it.

#### 23b. Rotation detection uses only file size

```python
if off > size:
    off = 0
```

**Trigger**

The log rotates, and the new file grows to a size equal to or larger than the old cursor before the next run.

**Impact**

The code seeks to the old offset in the new file and skips all fallback markers before that position.

#### 23c. Cursor corruption becomes evidence suppression

If cursor JSON is partially written or corrupted, the function treats it as first run and baselines the current end of the log, discarding all pending markers.

#### 23d. Cursor writes are non-atomic

`write_text()` can leave partial JSON on crash or disk failure, directly triggering 23c on the next run.

**Production impact**

Fallback evidence can be lost exactly when logs rotate or the monitoring process is unstable.

The cursor needs inode/file identity, atomic replacement, and a recovery policy that scans rather than baselines on state corruption.

---

### 24. The advertised “UNVERIFIED” invariant is not implemented as described

**File**

- `scripts/dep_health.py`, module documentation versus `run()`, approximately lines 1–35 and 340–460

The documentation promises:

> engine installed but its store has not advanced since the shim was written

There is no comparison between:

- shim modification time;
- store modification time;
- last canary event;
- install time.

Instead, the script invokes an external `tuner_canary.py` using a stub delegate. That can prove some package journal code works, but it does not establish that the host has used the installed shim since installation.

**Production impact**

A copied-but-unactivated shim can pass direct package canaries while the host continues using the built-in compressor. The only real-traffic check waits seven days and is downgraded to P2.

---

### 25. The health anchor never resets after canary failure

**File/function**

- `scripts/dep_health.py`, end of `run()`, approximately lines 463–488

```python
healthy_since = prev_state.get("canary_healthy_since")
if can.get("ok") and not healthy_since:
    healthy_since = time.time()
```

There is no branch that clears `healthy_since` when the recording canary fails.

**Trigger**

1. Canary is healthy once.
2. It fails for days.
3. It later runs, or store metrics are absent.

**What breaks**

The stored value still claims continuous health since the original successful run.

**Production impact**

The “no real compression in a week” logic uses a false continuity anchor and can produce misleading findings after an extended outage.

---

### 26. Version regression tests do not test installed metadata

**File/function**

- `tests/test_install_target.py`, `_package_version()`, approximately lines 46–57
- `test_manifest_version_is_not_stale()`, approximately lines 34–45

The test explicitly reads:

```python
pyproject.toml
```

and explicitly avoids installed metadata.

**Trigger**

The live venv still has package metadata for `0.2.0b1`, while the source checkout and copied `plugin.yaml` say `0.3.1`.

**What breaks**

The test passes. This is precisely the F3 failure mode: deployed package provenance differs from source provenance.

`install_engine()` also only copies the shim; it does not install, upgrade, or verify `hermes-context-tuner` in the Hermes venv.

**Production impact**

A 0.3.1 manifest can load 0.2-era Python code, while CI remains green.

The test should query `importlib.metadata.version("hermes-context-tuner")` in the actual Hermes venv and compare it to both the manifest and imported module provenance.

---

### 27. `test_fail_open_compress.py` does not exercise the production construction or loader path

**File/function**

- `tests/test_fail_open_compress.py`, `make_engine()`, approximately lines 42–58

```python
engine = ContextTunerEngine.__new__(ContextTunerEngine)
```

This bypasses:

- `_make_default_compressor()`;
- constructor signature compatibility;
- store initialization failure;
- ledger initialization failure;
- stale-operation reconciliation;
- actual Hermes loader behavior;
- plugin registration;
- real delegate type/signature;
- Windows/default home resolution.

**Production impact**

The tests establish behavior for a manually assembled object with perfectly shaped internal state, not for the deployed plugin lifecycle that failed in production.

The tests are not wholly vacuous for the exact patched statements, but they are not load-bearing evidence for the stated system contract.

---

### 28. The fail-open tests omit the dangerous state assertions

**File**

- `tests/test_fail_open_compress.py`, approximately lines 87–145

Missing assertions include:

- no `started` DB row remains after fingerprint failure;
- no `started` DB row remains after checkpoint-policy failure;
- `_pending_count == 0`;
- `_pending_old_session_id is None`;
- checkpoint counter value after each failure point;
- `_last_plan` is cleared after planning failure;
- a no-op delegate is not counted/finalized as compression;
- delegate exceptions still propagate;
- `fail_compression()` failure does not mask delegate errors;
- malformed delegate results are not silently accepted;
- concurrent compressions do not overwrite each other.

Because the tests only inspect `_pending_operation`, they bless “drop the pointer and leak the row” as correct behavior.

---

### 29. The teardown test asserts an artifact of monkeypatching rather than the real teardown implementation

**File/function**

- `tests/test_fail_open_compress.py`, `test_teardown_failure_does_not_block_the_next_compression()`, approximately lines 147–166

The test replaces `_close_pending_on_teardown()` wholesale:

```python
monkeypatch.setattr(ContextTunerEngine, "_close_pending_on_teardown", boom)
```

The real method already catches store exceptions and clears state in `finally`. The monkeypatched function skips all of that.

The only final assertion is:

```python
assert engine._pending_operation != "orphan-op-id"
```

This passes if the implementation simply forgets the old operation and creates a new one. It does not verify:

- the old DB row became interrupted;
- all old pending fields were cleared;
- the new pending operation is valid;
- no started row leaked;
- the failure was recorded once rather than repeatedly.

**Production impact**

The test treats audit corruption as acceptable unwedge behavior.

---

### 30. No test verifies the central delegate-failure boundary

**File**

- `tests/test_fail_open_compress.py`

There is no test for:

- delegate `RuntimeError`;
- delegate-body `TypeError`;
- `DelegateSignatureError`;
- store failure while recording delegate failure;
- health accounting failure while handling delegate failure;
- a delegate return object that raises during `len()` or iteration.

**Production impact**

The claim that “delegate failure still propagates” is unregressed. Future changes can swallow, wrap incorrectly, or replace the real delegate exception without failing this suite.

---

## LOW

### 31. Commit assignments after successful bookkeeping are outside the post-success `try`

**File/function**

- `src/hermes_context_tuner/engine.py`, `compress()`, approximately lines 415–423

```python
self._pending_operation = pending_op
self._pending_count = count
self._pending_result_fingerprint = fingerprint
self._pending_old_session_id = self._session_id
self._pending_checkpoint_hint = hint
return result
```

Any exception in these statements still escapes after the delegate succeeded.

With the current exact class and normal state, these assignments are usually simple. However, the advertised invariant is absolute, and the code provides no final guard around the actual commit phase.

A subclass, monkeypatch, corrupted `_session_id`, or future descriptor can reintroduce the discarded-success bug.

---

### 32. `_last_plan` is published before the delegate outcome is known

**File/function**

- `src/hermes_context_tuner/engine.py`, `compress()`, approximately lines 279–288

```python
plan = build_budget_plan(...)
self._last_plan = plan
```

**Trigger**

Planning succeeds, but the delegate subsequently raises.

**What breaks**

`get_status()` reports the plan as the last plan without indicating that its compression failed.

**Production impact**

Operational status conflates a proposed plan with a successfully executed compression plan.

---

### 33. Explicit `max_tokens=None` can alter delegate semantics

**File/function**

- `src/hermes_context_tuner/engine.py`, `update_model()`, approximately lines 225–253

The rich candidate always passes:

```python
"max_tokens": max_tokens
```

even when the caller omitted it and the value is `None`.

A delegate may distinguish:

```python
update_model(..., max_tokens omitted)
```

from:

```python
update_model(..., max_tokens=None)
```

The former can preserve an existing configured value; the latter can clear it.

The tests cover acceptance and retry behavior but not omission semantics.

---

### 34. `__setattr__` is brittle against future wrapper state and inheritance

**File/function**

- `src/hermes_context_tuner/engine.py`, `__setattr__()`, approximately lines 207–223

Any new wrapper instance field not added to `_WRAPPER_LOCAL` is silently forwarded to the delegate.

Only names in:

```python
type(self).__dict__
```

are treated as class-defined overrides. Descriptors inherited from a base class or mixin are not recognized.

**Production impact**

A future maintenance change can write wrapper bookkeeping onto the delegate, recreating hidden shadow-state bugs without a syntax or type error.

---

### 35. Exception strings are exposed in model-facing and operator-facing outputs

**File/functions**

- `_build_resume_packet()`, approximately lines 535–571
- `_journal_summary()`, approximately lines 600–615
- `handle_tool_call()`, approximately lines 675–695
- `_handle_checkpoint_tool()`, approximately lines 720–745
- `_handle_resume_tool()`, approximately lines 747–764
- `context_tuner_command()`, approximately lines 768–780

Although `_safe_failure_reason()` deliberately avoids exception messages because they may contain transcript content or credentials, several other paths serialize raw `str(exc)` into:

- resume packets;
- tool responses;
- status;
- command output.

Examples:

```python
packet["checkpoint_degraded"] = str(exc)[:AUDIT_SUMMARY_CAP]
packet["journal_degraded"] = str(exc)[:AUDIT_SUMMARY_CAP]
{"error": f"checkpoint write failed: {exc}"}
{"error": f"resume failed: {exc}"}
```

**Production impact**

Database errors, file paths, connection strings, malformed content, or exception messages containing user/model data can be returned to the model or logs. The privacy rule is applied inconsistently.

---

### 36. `store_metrics()` can leak a SQLite connection on query failure

**File/function**

- `scripts/dep_health.py`, `store_metrics()`, approximately lines 286–325

The connection is closed only on the successful path:

```python
con.close()
```

There is no `finally`.

**Trigger**

Any query raises after `sqlite3.connect()`.

**Production impact**

The watchdog process retains the connection until exit. In a short-lived cron process the impact is limited, but on repeated use in-process it can retain file handles and locks.

---

### 37. Manifest version parsing is fragile

**File/function**

- `scripts/dep_health.py`, `check_provenance()`, approximately lines 196–204

```python
re.search(r"^version:\s*(\S+)", ...)
```

Quoted YAML:

```yaml
version: "0.3.1"
```

produces `"0.3.1"` including quotes and is reported as skewed.

Inline comments and nontrivial YAML formatting are also mishandled.

The watchdog imports YAML elsewhere but does not use it for the manifest.

---

## INFO

### 38. The test suite would not generally pass unchanged against the pre-fix `compress()`, but that does not validate the production claims

The planning-failure, fingerprint-failure, checkpoint-policy-failure, and monkeypatched teardown tests target code locations that would raise in the pre-fix implementation. They are therefore not purely green-by-construction.

However, their red/green status proves only the following narrow behaviors:

- selected exceptions are caught;
- the delegate result is returned in the synthetic object;
- `_pending_operation` is cleared.

It does not prove:

- fail-open around all wrapper code;
- no persistent DB corruption;
- correct checkpoint cadence;
- delegate-failure propagation;
- loader durability;
- actual host registration;
- correct Windows home resolution;
- concurrency safety.

The `__new__` harness and weak state assertions make the suite insufficient as production evidence.

---

## Required failure-path tests missing from this patch

At minimum, the regression suite should add these concrete cases:

1. **Persistent row cleanup**
   - Force fingerprint failure after `begin_compression()`.
   - Assert zero `started` rows remain, or assert the row is terminally interrupted.

2. **Checkpoint-policy failure**
   - Assert both DB terminal state and exact counter value.

3. **Planning failure after a previous successful plan**
   - Assert `_last_plan` is not stale or is explicitly marked degraded.

4. **No-op delegate**
   - Return the original message list.
   - Emit a compression boundary.
   - Assert no false compression count/checkpoint claim is finalized.

5. **Delegate `RuntimeError`**
   - Assert the same exception propagates.

6. **Delegate-body `TypeError`**
   - Assert exactly one invocation and the expected wrapped exception/cause.

7. **Failure while recording delegate failure**
   - Make `fail_compression()` raise.
   - Assert the delegate exception remains the primary propagated failure.

8. **Malformed result object**
   - Return a list subclass whose `__len__()` or iterator raises.
   - Define whether this is a delegate failure or a wrapper degradation; do not silently return an object already known to be corrupt.

9. **Cadence table**
   - Fingerprint failure before increment.
   - Policy failure after increment.
   - Planning failure.
   - Store opening failure.
   - No-op compression.
   - Process restart.
   - External checkpoint.
   - Concurrent checkpoint/compression.

10. **Concurrent compressions**
    - Barrier-controlled interleaving of two calls and two boundary callbacks.
    - Assert operation IDs, fingerprints, and session lineage cannot cross.

11. **Real constructor and loader**
    - Instantiate through `load_context_engine()`.
    - Verify engine identity and resolved path.

12. **Stale bundled shadow**
    - Create an empty bundled directory and a valid user installation.
    - Run `install_engine()`.
    - Assert installation fails or the loader resolves the user installation.

13. **Windows home resolution**
    - Test with no pre-existing AppData directory.
    - Test `%LOCALAPPDATA%`.
    - Test POSIX with an accidental `~/AppData/Local/hermes`.

14. **Installed metadata**
    - Compare `plugin.yaml`, imported module provenance, and `importlib.metadata.version()` from the actual Hermes venv.

15. **Watchdog effective configuration**
    - Missing config with host defaulting to `context-tuner`.
    - `auto` resolving to `context-tuner`.
    - env override.
    - YAML formats unsupported by the regex fallback.

16. **Watchdog identity**
    - Make the loader return a built-in fallback object and assert the canary fails.

17. **Log rotation**
    - Replace the log with a larger new file containing markers before the old offset.
    - Assert markers are detected.

The largest unresolved defects are not cosmetic: the installer can still report success while the same empty bundled shadow keeps the plugin dead, and the F4 bookkeeping path now returns successful delegate results while leaking non-terminal journal rows and mutating cadence inconsistently.