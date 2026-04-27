---
name: adi-python-debugger
description: Use ADI from a terminal to debug Python programs with function-level frame discovery, frame lifetime traces, in-frame exec inspection, and call trees. Prefer this when test output or logs do not explain a Python value, branch, exception, call path, or patch invariant.
metadata:
  short-description: Debug Python runtime behavior with the ADI CLI
---

# ADI Python Debugger

ADI is a terminal-first Python debugging CLI for agents. Use it when ordinary
logs, tracebacks, or test output identify a symptom but not the decisive runtime
value, object, branch, or call path.

The default interface is the `adi` command in bash/terminal. Do not use MCP as
the default path.

## Preconditions

Install once when needed:

```bash
npm install -g @adi-tools/py
adi --help
```

Requirements:

- Node.js `>=18`
- Python `>=3.9`

If ADI itself must run with a specific Python interpreter:

```bash
ADI_PYTHON=/path/to/python adi --help
```

`ADI_PYTHON` controls the Python used to run ADI. The target program uses the
Python executable inside the command passed to ADI.

## When to Use ADI

Use ADI when you need runtime evidence for questions such as:

- Which call to this function matters?
- What were the arguments, locals, and return value in that call?
- Which caller led to this state?
- Did the patch change the object, value, type, or invariant that caused the bug?
- Where is the last useful Python frame before a native extension boundary?

Do not use ADI as a replacement for tests. Use it to decide what to patch and to
verify the key runtime behavior after patching.

## Core Workflow

### 1. Reproduce with a plain Python command

Start with the simplest command that reaches the behavior:

```bash
python script.py
python -m pytest tests/test_file.py::test_case -q
python -c "import pkg; pkg.run()"
```

### 2. List candidate frames

```bash
adi list-frames "python script.py" "/abs/path/file.py:function"
```

For pytest:

```bash
adi list-frames \
  "python -m pytest tests/test_file.py::test_case -q" \
  "/abs/path/file.py:function"
```

If setup/import-time calls are noisy, filter by caller:

```bash
adi list-frames \
  "python -m pytest tests/test_file.py::test_case -q" \
  "/abs/path/file.py:function" \
  --caller "test_file.py"
```

### 3. Copy `target:` into `break`

Prefer the `target:` value printed by `list-frames`:

```bash
adi break "python script.py" "/abs/path/file.py:Qualified.function#1" --no-count
```

Use `--no-count` when you only need the selected frame quickly. It stops after
capturing the focus call instead of continuing to count all calls.

### 4. Inspect values with `exec`

```bash
adi exec "python script.py" "/abs/path/file.py:Qualified.function#1" \
  -s "print(type(obj), obj)"
```

Use `-l <line>` to inspect at a specific line inside the selected function:

```bash
adi exec "python script.py" "/abs/path/file.py:Qualified.function#1" \
  -l 123 \
  -s "print(locals())"
```

### 5. Inspect call structure when needed

```bash
adi call-tree "python script.py" "/abs/path/file.py:Qualified.function#1"
```

Use call trees when the caller/callee relationship is unclear or when a framework
helper silently derives a value.

### 6. Verify after patching

After editing source code, rerun the most relevant ADI command. In your reasoning,
state what changed in the same object, value, type, or invariant. A passing test
is not the same as runtime evidence for the root cause.

## Frame Target Rules

ADI accepts function targets and line targets:

```text
/path/to/file.py:function#N
/path/to/file.py:Class.method#N
/path/to/file.py:123#N
```

Line targets resolve to the containing function. They are useful when a traceback
points at a line and the function name is unclear.

`list-frames` prints both:

```text
target: /repo/src/model.py:Model.forward#3
line-target: /repo/src/model.py:123#3
```

Rules:

- Prefer copying `target:` into `break`, `exec`, and `call-tree`.
- Keep `line-target:` as a fallback for traceback-line workflows.
- Frame indices are 1-based: `#1`, `#2`, `#3`, ...
- If `list-frames` shows many calls, choose the call whose caller or arguments
  match the failing behavior.
- If using zsh variables, write `${FILE}:func`, not `$FILE:func`.

## Command Contract

ADI target commands should be simple Python commands:

```bash
adi break "python script.py" "/repo/script.py:main#1"
adi break "python -m package.module --flag" "/repo/package/module.py:main#1"
adi break "python -c 'import pkg; pkg.run()'" "/repo/pkg/core.py:run#1"
```

Environment prefixes are allowed:

```bash
adi break "PYTHONPATH=/repo/src python script.py" "/repo/src/pkg/core.py:run#1"
adi break "env FOO=1 python script.py" "/repo/script.py:main#1"
```

Avoid shell-heavy target commands:

- `cd dir && python ...`
- pipes or redirection
- `poetry run ...`
- `conda run ...`
- commands that rely on shell aliases or interactive input

Instead, enter or activate the environment first, then pass ADI a plain
`python ...` command.

## Common Commands

```bash
# Discover frames.
adi list-frames "python repro.py" "/testbed/file.py:func"
adi list-frames "python repro.py" "/testbed/file.py:func" --caller "test_"

# Capture a frame lifetime trace.
adi break "python repro.py" "/testbed/file.py:func#1" --no-count
adi break "python repro.py" "/testbed/file.py:123#1" --no-count

# Conditions.
adi break "python repro.py" "/testbed/file.py:func#1" --condition "x is None" --on-entry --no-count
adi break "python repro.py" "/testbed/file.py:func#1" --condition "_return is None" --on-return --no-count
adi break "python repro.py" "/testbed/file.py:func#1" --condition "len(buf) > 0" --if-eval-lineno 150 --no-count

# Inspect values.
adi exec "python repro.py" "/testbed/file.py:func#1" -s "print(x)"
adi exec "python repro.py" "/testbed/file.py:func#1" -l 150 -s "print(locals())"
adi exec "python repro.py" "/testbed/file.py:func#1" -f /tmp/inspect.py

# Calls and comparisons.
adi call-tree "python repro.py" "/testbed/file.py:func#1"
adi diff "python repro.py" "/testbed/file.py:func#1" "/testbed/file.py:func#2"

# Navigation and state.
adi continue "python repro.py"
adi step-in "python repro.py" "/testbed/file.py:callee#1"
adi step-out "python repro.py"
adi state
adi clear
```

## External Files

ADI normally focuses on project Python code. If you intentionally target stdlib
or `site-packages`, pass `--allow-external` to commands that need it:

```bash
adi break "python repro.py" "/usr/lib/python3.12/path/file.py:func#1" \
  --allow-external \
  --no-count
```

Even with `--allow-external`, ADI does not step into native C/C++/Fortran
extension code. Use ADI to inspect the last Python frame and values before the
native boundary.

## Concurrent Agents

For multiple agents on one machine, isolate CLI state. Prefer one state directory
per agent/session:

```bash
ADI_STATE_DIR=/tmp/adi-agent-a adi list-frames "python repro.py" "repro.py:f"
ADI_STATE_DIR=/tmp/adi-agent-a adi break "python repro.py" "repro.py:f#1"
```

State priority in `@adi-tools/py>=2.6.0`:

1. `ADI_STATE_DIR`
2. `ADI_DATA_DIR`
3. `ADI_SESSION_ID`
4. `CODEX_THREAD_ID`
5. workspace-scoped temp fallback

Do not intentionally share one `ADI_STATE_DIR` across independent agents if you
need isolation. Shared state is last-writer-wins.

## Containers and SWE-bench-style Environments

Container images may not include Node.js. Install Node.js `>=18` before installing
ADI:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
npm install -g @adi-tools/py
```

Some project containers need ADI to use the same Python as the target project:

```bash
ADI_PYTHON=/opt/miniconda3/envs/testbed/bin/python adi --help
```

If a container or SWE-bench config already mounts a source checkout and sets
`PYTHONPATH`, that validates source ADI, not npm-installed ADI. For npm validation,
make sure `which adi` points to the npm-installed executable.

## Reading ADI Results

- Read stdout even if the ADI process returns nonzero. If the target program
  raises, ADI may still print the useful frame, exec output, or call tree.
- If no frame is reached, check that the target command is the same command that
  reproduces the behavior.
- If a line target says the line is not inside a function, choose a line inside
  the target function body or use function-name format.
- If `--caller` filters out everything, rerun without it. Framework dispatchers
  may be the direct caller rather than the test file.
- Prefer one focused ADI command at a time and record the output before deciding
  the next command.

## Minimal Report Template

When using ADI for debugging, record:

```text
Command:
  adi ...

Result:
  return code, key frame target, key stdout/stderr

Runtime evidence:
  argument/local/return/caller value that explains the issue

Patch implication:
  what source behavior should change

Post-patch verification:
  same ADI command or equivalent runtime evidence
```
