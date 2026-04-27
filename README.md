# ADI: Agent-centric Debugging Interface

> [!NOTE]
> 🎉 The ADI paper, _Empowering Autonomous Debugging Agents with Efficient Dynamic Analysis_, has been accepted to the ACM International Conference on the Foundations of Software Engineering (FSE 2026)!
> 📦 The artifact for the accepted FSE paper is available in [`FSE26-Artifact/`](./FSE26-Artifact/).

ADI is the agent-centric debugging interface proposed in our paper, _Empowering Autonomous Debugging Agents with Efficient Dynamic Analysis_.
It is a terminal-first Python debugging interface designed for coding agents.
ADI lets an agent ask targeted runtime questions about a Python program without
editing the source code to add print statements.


The current release is `2.6.0`.

```bash
npm install -g @adi-tools/py
adi --help
```

## Why ADI?

Modern coding agents mostly interact with software through a shell. They can run
tests, inspect files, and edit patches, but they often lack a compact way to ask:

- Which call to this function matters?
- What were the arguments and local values in that call?
- Which caller led to this state?
- Did the patch change the runtime value that caused the bug?

A human developer may answer these questions with an IDE debugger, print
statements, logging, or manual reasoning. For agents, those options are often
slow or noisy. Print statements require code edits. Full interactive debuggers
are hard to drive robustly from a non-interactive terminal. Test output often
shows the symptom but not the decisive runtime value.

ADI provides a small command-line contract for these cases:

1. run a normal Python command,
2. select a function frame by file and function or line,
3. capture a frame lifetime trace,
4. inspect values inside that frame,
5. repeat after a patch.

ADI is intentionally modest. It is not a replacement for tests, static analysis,
or full IDE debugging. It is a practical runtime probe for agent workflows.

## Installation

### npm, recommended

```bash
npm install -g @adi-tools/py
adi --help
```

Requirements:

- Node.js `>=18`
- Python `>=3.9`

The npm package is a lightweight launcher with vendored ADI Python sources. It
runs ADI with a local Python interpreter. If the correct Python is not named
`python3` or `python`, set `ADI_PYTHON`:

```bash
ADI_PYTHON=/path/to/python adi --help
```

`ADI_PYTHON` controls the Python used to run ADI itself. The target program uses
the Python executable in the command you pass to ADI:

```bash
ADI_PYTHON=/opt/py312/bin/python adi break \
  "/project/.venv/bin/python script.py" \
  "/project/script.py:main#1"
```

### Containers

Container images often do not include Node.js. Some Debian/Ubuntu images install
an old Node.js version through `apt`, which is not sufficient for the npm
launcher. Install Node.js `>=18` first.

Example:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
npm install -g @adi-tools/py
adi --help
```

In project-specific containers, use the same Python environment for ADI and the
target when needed:

```bash
ADI_PYTHON=/opt/miniconda3/envs/testbed/bin/python adi --help
```

### Source checkout

```bash
cd /path/to/ADI
export PYTHONPATH=$PWD/src
python -m ADI --help
```

## Quick start

Create a small program:

```bash
cat > /tmp/adi_demo.py <<'PY'
def normalize(x):
    y = x + 1
    return y * 2

print(normalize(20))
PY
```

List the runtime calls to `normalize`:

```bash
adi list-frames "python /tmp/adi_demo.py" "/tmp/adi_demo.py:normalize"
```

Typical output:

```text
Frames (1 total):
  #1  1#1 (normalize)  target: /tmp/adi_demo.py:normalize#1 line-target: /tmp/adi_demo.py:1#1 <- adi_demo.py:<module>:5

Summary:
  normalize: 1 calls (#1)
```

Copy the `target:` value into `break`:

```bash
adi break "python /tmp/adi_demo.py" "/tmp/adi_demo.py:normalize#1" --no-count
```

ADI prints a frame lifetime trace (FLT):

```text
Frame: /tmp/adi_demo.py:normalize#1
Args: x = 20

1 | def normalize(x):
2 | y = x + 1
  |   + y = 21
3 | return y * 2

Return: 42
```

Inspect values inside the frame without editing the program:

```bash
adi exec "python /tmp/adi_demo.py" "/tmp/adi_demo.py:normalize#1" \
  -s "print(x, y)"
```

## Core workflow for agents

ADI works best as a small evidence loop around a failing behavior.

### 1. Reproduce normally

Start with a plain Python command:

```bash
python -m pytest tests/test_parser.py::test_edge_case -q
```

### 2. Find candidate frames

Use `list-frames` on a project function or line:

```bash
adi list-frames \
  "python -m pytest tests/test_parser.py::test_edge_case -q" \
  "/repo/src/parser.py:Parser.parse"
```

If there are many setup-time calls, filter by caller:

```bash
adi list-frames \
  "python -m pytest tests/test_parser.py::test_edge_case -q" \
  "/repo/src/parser.py:Parser.parse" \
  --caller "test_parser.py"
```

### 3. Capture a frame lifetime trace

```bash
adi break \
  "python -m pytest tests/test_parser.py::test_edge_case -q" \
  "/repo/src/parser.py:Parser.parse#2" \
  --no-count
```

### 4. Inspect exact values

```bash
adi exec \
  "python -m pytest tests/test_parser.py::test_edge_case -q" \
  "/repo/src/parser.py:Parser.parse#2" \
  -s "print(type(node), node, tokens[:5])"
```

### 5. Understand local call structure

```bash
adi call-tree \
  "python -m pytest tests/test_parser.py::test_edge_case -q" \
  "/repo/src/parser.py:Parser.parse#2"
```

### 6. Verify the patch

After editing the source, rerun the same ADI command. The goal is not merely to
show that a test passed; it is to show that the runtime object, value, type, or
invariant that caused the bug changed as expected.

## Agent skill

ADI includes a Codex-style skill that packages the recommended terminal
workflow, frame target rules, simple-command contract, concurrency guidance, and
container notes for agents:

```text
src/ADI/skills/adi-python-debugger/SKILL.md
```

If your agent runtime supports local skills, install or copy the
`adi-python-debugger` skill directory into the runtime's skills directory. The
skill is intentionally terminal-first: it tells agents to use the `adi` CLI via
bash rather than relying on an MCP server.

For Codex-style local skills:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R src/ADI/skills/adi-python-debugger \
  "${CODEX_HOME:-$HOME/.codex}/skills/adi-python-debugger"
```

Restart the agent runtime after installing the skill.

## Command reference

| Command | Purpose |
| --- | --- |
| `adi list-frames "cmd" "file.py:func"` | list calls and copyable frame targets |
| `adi break "cmd" "file.py:func#N"` | capture a frame lifetime trace |
| `adi exec "cmd" "file.py:func#N" -s "stmt"` | execute a statement inside the frame |
| `adi call-tree "cmd" "file.py:func#N"` | show calls under the selected frame |
| `adi diff "cmd" "frame#1" "frame#2"` | compare two calls of the same function |
| `adi continue "cmd"` | continue to the next saved breakpoint hit |
| `adi step-in "cmd" "callee#N"` | navigate to a callee shown in an FLT |
| `adi step-out "cmd"` | navigate back to the caller |
| `adi state` | inspect the last tracer state for diagnostics |
| `adi clear` | clear saved breakpoints |
| `adi list` | list saved breakpoints |

Use `adi <command> --help` for command-specific flags.

## Frame targets

ADI accepts two target styles.

### Function target

```text
/path/to/file.py:function#N
/path/to/file.py:Class.method#N
```

Example:

```bash
adi break "python script.py" "/repo/src/model.py:Model.forward#3"
```

### Line target

```text
/path/to/file.py:123#N
```

The line is resolved to the containing function. A line target is useful when an
exception traceback points at a line and the agent does not know the function
name.

Example:

```bash
adi break "python script.py" "/repo/src/model.py:123#1"
```

`list-frames` prints both:

```text
target: /repo/src/model.py:Model.forward#3
line-target: /repo/src/model.py:123#3
```

Prefer copying `target:` into `break`, `exec`, and `call-tree`. Use
`line-target:` as a fallback.

## Supported target commands

ADI is designed for simple Python commands:

```bash
adi break "python script.py" "/repo/script.py:main#1"
adi break "python -m package.module --flag" "/repo/package/module.py:main#1"
adi break "python -c 'import pkg; pkg.run()'" "/repo/pkg/core.py:run#1"
```

Environment prefixes are supported:

```bash
adi break "PYTHONPATH=/repo/src python script.py" "/repo/src/pkg/core.py:run#1"
adi break "env FOO=1 python script.py" "/repo/script.py:main#1"
```

Avoid shell-heavy target commands such as pipes, redirection, `cd ... &&`,
`poetry run`, or `conda run`. Enter the environment first, then pass ADI a plain
`python ...` command.

## State and concurrent agents

ADI commands are separate terminal processes, but some operations such as
`continue`, `step-out`, and `state` need a small amount of persistent CLI state.
In `@adi-tools/py>=2.6.0`, the CLI state directory is selected in this order:

1. `ADI_STATE_DIR`
2. `ADI_DATA_DIR` for backward compatibility
3. `ADI_SESSION_ID`
4. `CODEX_THREAD_ID`
5. a workspace-scoped directory under `${TMPDIR:-/tmp}/adi-state/`

For multiple agents on the same machine, prefer an explicit state directory per
agent/session:

```bash
ADI_STATE_DIR=/tmp/adi-agent-a adi list-frames "python repro.py" "repro.py:f"
ADI_STATE_DIR=/tmp/adi-agent-a adi break "python repro.py" "repro.py:f#1"
```

Do not intentionally share the same `ADI_STATE_DIR` across independent agents if
you need isolation. Shared state is last-writer-wins.

## What ADI is good at

ADI is most useful when the bug depends on a runtime value that is hard to infer
from static code alone:

- a parser returns the wrong object shape,
- a framework silently derives an endpoint or option,
- a numerical library crosses from Python into native code,
- a test fails only on the second or third call to a function,
- a patch should preserve a specific invariant.

In these cases, ADI gives the agent compact runtime evidence that can be copied
into its reasoning and checked again after a patch.

## Limitations

ADI is a Python function-level tracer. It has deliberate boundaries.

- It does not step into native C/C++/Fortran extension code. It can still show the
  last Python frame and values before the native boundary.
- It is not a full time-travel debugger. It reruns the target command to capture
  selected frames.
- It is not a statistical fault-localization system. It does not rank suspicious
  lines across many passing/failing runs.
- It is not meant for arbitrary shell pipelines. Keep target commands simple.
- A target program that raises an exception may make `exec` or `call-tree` return
  nonzero while still printing useful diagnostic output. Read stdout before
  judging the command useless.

## Relation to prior work

ADI builds on a long line of debugging ideas, but adapts them to the way coding
agents work in a terminal.

- **Interactive debuggers** such as `gdb`, `pdb`, and IDE debuggers provide rich
  stepping and inspection for humans. ADI keeps a smaller, scriptable surface that
  agents can call repeatedly from shell commands.
- **Omniscient and back-in-time debugging** systems record enough execution
  history to query past states. ADI does not record the whole execution; it
  reruns the program and captures selected function frames.
- **Whyline-style debugging** asks why a program did or did not produce a
  behavior. ADI does not infer why automatically, but it gives agents concrete
  runtime evidence for answering such questions.
- **Delta debugging and fault localization** reduce or rank failure causes across
  executions. ADI is complementary: once an agent has a candidate path or
  invariant, ADI inspects the decisive frame.
- **Program tracing and observability** tools collect broad traces or logs. ADI is
  narrower: it focuses on a specific function call and the values needed for a
  patch decision.

Useful references include:

- Andreas Zeller and Ralf Hildebrandt, “[Simplifying and Isolating
  Failure-Inducing Input](https://www.st.cs.uni-saarland.de/papers/tse2002/),”
  IEEE TSE, 2002.
- Andrew J. Ko and Brad A. Myers, “[Designing the Whyline: A Debugging
  Interface for Asking Questions about Program
  Behavior](https://dblp.org/rec/conf/chi/KoM04),” CHI, 2004.
- Bil Lewis, “[Debugging Backwards in
  Time](https://arxiv.org/abs/cs/0310016),” 2003.
- Ben Liblit et al., “[Scalable Statistical Bug
  Isolation](https://pages.cs.wisc.edu/~liblit/pldi-2005/),” PLDI, 2005.
- James A. Jones, Mary Jean Harrold, and John Stasko, “[Visualization of Test
  Information to Assist Fault
  Localization](https://isr.uci.edu/content/visualization-test-information-assist-fault-localization.html),”
  ICSE, 2002.

ADI should be read as an engineering interface in this tradition, not as a claim
that any single command can replace debugging judgment.

## Citation

If you use ADI in research, please cite the ADI paper or technical report once it
is available. Until then, cite the repository and version used in your
experiments.

```text
ADI: Agent-centric Debugging Interface, version 2.6.0.
https://www.npmjs.com/package/@adi-tools/py
```

## Development

Run the ADI test suite from the repository checkout:

```bash
# From the monorepo root:
bash src/ADI/test/run_tests.sh

# Or from the ADI package root:
cd src/ADI
bash test/run_tests.sh
```

Build and smoke-test the npm package:

```bash
# From the monorepo root:
cd packages/adi-npm
npm pack
npm run smoke
```

Before publishing, use a clean prefix and verify that `adi` works without the
repository on `PYTHONPATH`.
