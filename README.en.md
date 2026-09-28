# MoocManus — an Agent system that is *measured*, not just demoed

Two layers live in this repository, and **the second one is the point**:

| Layer | Path | Role |
|---|---|---|
| A general-purpose Agent (Manus-style: A2A + MCP + sandbox) | `api/` `ui/` `sandbox/` `nginx/` | **System under test (SUT)** |
| An Agent **reliability lab** | `lab/` | **The subject of this repo** |

`lab/` treats the Agent above as a black box and answers four questions with numbers:

| Question | How it is answered |
|---|---|
| Did it do the right thing? | 28 deterministic tasks + pure-function graders (never the Agent's own claim) |
| Does it do it *every* time? | `pass^k` (per-task, k consecutive successes) + Wilson / bootstrap intervals |
| Where did the money go? | span-level traces (task → step → tool → LLM) + cost attribution per tool |
| What do we do when it breaks? | 10 fault kinds + a hardening middleware layer + controlled A/B switches |

## Three commands (no API key, no cost)

```bash
cd mooc-manus
python -m lab bench validate        # 28/28 task self-check (grader sanity + fixture authenticity)
python -m lab bench purpose-audit   # label vs measured: flags tasks whose tag no longer matches data
python -m lab sandbox env           # the environment fingerprint the Agent will actually see
```

## What makes the deltas trustworthy

Three pieces of infrastructure exist because "our number went up" is worthless without them:

- **Noise floor** — run the *same* configuration twice and measure the spread. Without it,
  a 20% improvement claim is indistinguishable from re-running the same code.
  Measured floor: **±16.6% cost** on `deepseek-reasoner`, **±46.8%** after switching the SUT to
  `deepseek-v4.1-flash` (cost deltas below that are invisible — and we say so).
- **Interleaved A/B** — both arms alternate inside one suite (A,B,B,A…), so time-correlated
  server drift cannot masquerade as a treatment effect.
- **LLM record/replay** — a real run (24.5 s / $0.057) replays offline in **0.4 s / $0**,
  which is what makes debugging a failure cheap and CI-able.

## What is already measured (and what is *not*)

| Result | Status |
|---|---|
| Task set + graders | 28 tasks, `bench validate` 28/28 |
| Hardening 2×2 (`partial_write`, write path) | **negative result**, mechanism explained: the Agent re-reads its own output |
| Read-path silent fault 2×2 (`truncated_result`) | **negative result**: every truncation triggered a re-read (see `docs/step9-readpath-2x2.md`) |
| Cost attribution | reconciled to the database to 0.00% (per-tool / per-step) |
| LLM-judge + Cohen's κ | **skeleton only** — 100-row labelling sheet generated; **not calibrated yet**, and we do not claim otherwise |

> Methodological honesty is part of the deliverable: every document carries a
> "what you may say / what you may not say" table, and the README documents its own mistakes
> (`lab/README.md` → "我错在哪" / *where I was wrong*, 10 self-corrections including one
> where a device defect was misdiagnosed and one where a fix was almost credited for a
> floor change it did not cause).

## Deep dives

- [`lab/README.md`](./lab/README.md) — the lab itself (design principles, CLI, how to add a task)
- [`docs/step9-readpath-2x2.md`](./docs/step9-readpath-2x2.md) — the read-path experiment + mechanism
- [`docs/noise-floor-measured.md`](./docs/noise-floor-measured.md) — how wide the floor is and what it is made of
- [`docs/step8-device-fixes.md`](./docs/step8-device-fixes.md) — device-fidelity fixes (and a debunked earlier conclusion)

## Deploying the SUT (the Agent itself)

See the Chinese [`README.md`](./README.md) for the `docker compose up -d --build` deployment of
`api/` + `ui/` + `sandbox/` + `nginx/`, and the model configuration notes.

## License

MIT — see [`LICENSE`](./LICENSE).
