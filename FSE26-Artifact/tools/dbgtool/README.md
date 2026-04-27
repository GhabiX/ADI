# dbgtool

A function-level debugging CLI tool.
It lets you set breakpoints at functions, step into/out frames, continue, go back, and display method frame call graphs.

---

## Install
```
cd dbgtool
pip install .
```



---

## Usage

General format:

```bash
dbgtool <command> [options]
```

---


## Frame ID Format

Most commands require a `FRAME_ID` to specify a target frame that you want to observe.

```
file_path:method_name#frame_index
```

* **file_path**: path to the source file
* **method_name**: function name; if it belongs to a class, use `Class.method`
* **frame_index**: the N-th call of that function (1 for the first call, 2 for the second, etc. And -1 for the last)

Example:

```
/path/to/module.py:Foo.bar#3
```

---


### Commands

**Note:** Every command that changes the focus frame automatically prints that frame's runtime information for inspection.

#### 1. `break`

Set (or reset) a function-level breakpoint at the specified frame.
Optional --condition is a boolean expression evaluated at function entry, with the function's arguments (including *args and **kwargs) as the accessible variable context. The expression should be wrapped in double quotes.
If `--condition` is given, the breakpoint is set on the last frame of the specified method that satisfies the condition, and the `frame_index` in `FRAME_ID` is ignored (you can simply set it to 1).

```bash
dbgtool break --cmd <COMMAND> --id <FRAME_ID> [--condition "<EXPR>"] [--clear]
```

Example:

```bash
dbgtool break \
  --cmd "python /path/to/main.py" \
  --id /path/to/module.py:Foo.bar#1 \
  --condition "x > 10"
```

---

#### 2. `step-into`

Jump into a specific frame by frame ID.

```bash
dbgtool step-into --cmd <COMMAND> --id <FRAME_ID>
```

Example:

```bash
dbgtool step-into --cmd "python /path/to/main.py" --id /path/to/module.py:Foo.bar#3
```

---

#### 3. `step-out`

Go back to the caller frame.

```bash
dbgtool step-out --cmd <COMMAND>
```

---

#### 4. `continue`

Continue to the next breakpoint frame.

```bash
dbgtool continue --cmd <COMMAND>
```

---

#### 5. `prev`

Go back to the previous breakpoint frame.

```bash
dbgtool prev --cmd <COMMAND>
```

---

#### 6. `call-graph`

Render a 3-level call graph from the current focus frame (or from your given frame ID).

```bash
dbgtool call-graph --cmd <COMMAND> [--id <FRAME_ID>]
```

---

#### 7. `execute`

Execute Python statements dynamically at runtime.

```bash
dbgtool execute --cmd <COMMAND> --id <FRAME_ID> --stmt <STATEMENT> --lineno <LINE_NUMBER> [--execution-index <INDEX>] [--mode <MODE>]
```

Parameters:
* `--stmt`: Python statement to execute
* `--lineno`: Line number where to execute
* `--execution-index`: Execution index (which time the line is executed to trigger execution, default: 1)
* `--mode`: Execution mode
  - `temp`: One-time execution (default)
  - `persistent`: Stays active for future runs
  - `reset`: Clear all persistent executions

Examples:

```bash
# Temporary execution
dbgtool execute \
  --cmd "python /path/to/main.py" \
  --id /path/to/module.py:Foo.bar#1 \
  --stmt "print(f'x = {x}')" \
  --lineno 10

# Persistent execution
dbgtool execute \
  --cmd "python /path/to/main.py" \
  --id /path/to/module.py:Foo.bar#1 \
  --stmt "print(f'debug: x = {x}')" \
  --lineno 10 \
  --mode persistent

# Clear all persistent executions
dbgtool execute --cmd "python /path/to/main.py" --mode reset
```

---