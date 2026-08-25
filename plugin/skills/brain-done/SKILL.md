---
name: brain-done
description: Confirm the task just completed succeeded, so Brainite can distil it into a reusable procedure. Use when the user says the work is done, correct, or working — "that's it", "brain done", "/brain-done", "yes that worked", "ship it" — or when the user explicitly asks to record the task as a success.
---

# Confirm this task succeeded

Mark the current task a **human-confirmed success**. Run:

```bash
brainite-hook done
```

(If that is not on PATH, use `"${CLAUDE_PLUGIN_ROOT}/hooks/brainite-hook" done`.)

Then tell the user in one line that the task is recorded as a confirmed success.

## Why this matters

Confirmation is the strongest signal in the system. Without it a run is labelled
`ambiguous` and is excluded from distillation entirely; with it, one run is enough
to distil a procedure, where inferred successes have to wait for three
corroborating runs in the same task cluster.

## Never do this on your own

Only run it when a **person** says the work is right. Never infer it from tests
passing, from your own judgement that the task went well, or from the absence of
complaints — the inferred signals are already collected automatically and are
deliberately weaker. Asserting a human confirmed something they did not is the one
way this plugin can put a wrong procedure in front of the whole company.
