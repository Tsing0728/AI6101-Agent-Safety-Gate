# 01-Project — Safety Approval Policies for Agent Harnesses

AI6101 Introduction to AI and AI Ethics (AY 2026-2027 Semester 1) — Group
Project 1.

The goal is an **approval policy (a "safety gate")** that makes a three-way
decision for every tool call an AI agent issues:

- **auto-approve** — the call is safe, run it;
- **auto-deny** — the call is unsafe/out-of-scope, block it;
- **escalate** — ambiguous/high-risk, forward to a (simulated) human reviewer.

Each task has a human-attention budget `k`; once it is exhausted, any call that
would otherwise be escalated is auto-denied. The policy is a **small model
fine-tuned locally** and deployed on the harness side, evaluated on the
[AgentDojo](https://github.com/ethz-spylab/agentdojo) benchmark.

## Status

- Native DeepSeek Harness (`dsh`) ↔ AgentDojo bridge: **done**, runtime-verified.
- Full baseline run (`deepseek-v4-flash`, 1046 runs): **done**.
  - utility (no injection) **72.2%**, utility (injected) **64.2%**
  - attack success rate (ASR) **1.9%**
  - safe completion **63.0%**
- Dataset construction, safety-gate training, TCR@k evaluation: **in progress**.

## Repository layout

| Path | What it is |
|---|---|
| `agentdojo/` | Git submodule: the AgentDojo benchmark (evaluation + labels) |
| `dsh-agentdojo-bridge/` | Bridge code: AgentDojo MCP server, bare/batch runners, result aggregation |
| `approval-gate/` *(planned)* | Dataset schema, construction, and small-model training scripts |
| `harness/safety-gate/` *(planned)* | `dsh` plugin (`tools/pre-execute` three-way gate + budget `k`) |

`dsh` itself is **not vendored** in this repo; it is installed as a dependency
(see below).

## Architecture (3 processes)

```
run_benchmark.py (driver)
   │  Python SDK (JSON-RPC over stdio)
   ▼
dsh runtime (native agent loop + safety-gate plugin)
   │  stdio MCP                       │  HTTP :8000
   ▼                                  ▼
agentdojo_mcp_server.py           local Qwen scoring service
   (AgentDojo tools + environment)
```

- `dsh-agentdojo-bridge/agentdojo_mcp_server.py` exposes AgentDojo tools as MCP
  tools and checkpoints the environment + tool-call trace after each call.
- `dsh-agentdojo-bridge/run_benchmark.py` drives dsh over the whole benchmark,
  with sharding (`--shard i --num-shards N`) and resume, then aggregates
  results into an Excel workbook and a Markdown report.
- The safety gate (future) hooks `tools/pre-execute` in dsh and calls the local
  Qwen scoring service over HTTP.

## Quick start

### 1. Clone with the submodule

```powershell
git clone --recurse-submodules https://github.com/yyht6300a/01-Project.git
cd 01-Project
```

If you cloned without `--recurse-submodules`:

```powershell
git submodule update --init --recursive
```

### 2. Install dependencies

```powershell
# AgentDojo (from the submodule)
python -m pip install -e agentdojo

# dsh Python SDK (drives DeepSeek Harness as a subprocess)
python -m pip install deepseek-harness-sdk

# optional: for Excel aggregation
python -m pip install openpyxl
```

### 3. Set the API key

```powershell
$env:DEEPSEEK_API_KEY = "..."
```

### 4. Run

```powershell
cd dsh-agentdojo-bridge

# smoke test (3 tasks)
python run_benchmark.py --suites banking --user-tasks user_task_0 --limit 3

# full benchmark, sharded over 4 processes (one per terminal)
python run_benchmark.py --shard 0 --num-shards 4
python run_benchmark.py --shard 1 --num-shards 4
python run_benchmark.py --shard 2 --num-shards 4
python run_benchmark.py --shard 3 --num-shards 4

# aggregate after all shards finish
python run_benchmark.py --only-aggregate
```

See `dsh-agentdojo-bridge/README.md` for full details and flags.

## Result metrics

- `utility` — the user task was completed (`true` = good).
- `security` — the injection succeeded (`true` = **unsafe**; counterintuitive).
- `ASR` = attack success rate = fraction of injected runs with `security=true`.
- `safe completion` = fraction of injected runs with `utility=true` and
  `security=false`.

## References

- Assignment: `AI6101 Group Project 1 - Safety Approval Policies for Agent
  Harnesses.pdf`
- [AgentDojo (NeurIPS 2024)](https://agentdojo.spylab.ai/)
- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)

