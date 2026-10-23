# input/

One folder per task. Nothing here is committed: a task carries its instruction,
its evidence, its canary GUIDs and its gold answers, which is benchmark data and
must not be published. `SETUP.md` documents the layout; this is a reminder of it.

```
input/<task>/
├── instruction.md          # what the agent is told; keeps its own instruction-canary
├── task.toml               # [task] and [task.metadata] only
├── data/                   # evidence, mounted read-only at /workspace/data
├── world_data/             # overlay that seeds the Light apps
├── tests/
│   ├── test_outputs.py     # Channel A: deterministic checks, weights 1/3/5, guards negative
│   ├── test_weights.json   # {"tests": {name: weight}, "threshold", "null_floor"}
│   ├── judge.toml          # Channel B: [[criterion]] tables for the codex judge; [judge] weight
│   └── oracle.json, old_env.json
├── solution/               # solve.py, gt_env.json, TRUTH.md - graders only
└── environment/
    ├── Dockerfile          # the agent's image
    └── light-servers/software/<App>/<file>.py   # optional: server code the task changes,
                            # copied over the vendor copy at build (*.py, desc.json only)
```

To start from a task that is already built:

    .venv/bin/python -m harness.import_task --from <built task> --name <name>
