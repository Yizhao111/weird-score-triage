# weird-score-triage

This repository contains a lightweight audit workflow for finding suspicious or inconsistent scores on the Harbor leaderboard.

## Contents

- `leaderboard-audit-v1.md` — the main audit guide and analysis workflow

## What it does

The audit focuses on spotting:

- model ranking inversions
- agent ranking inversions
- near-zero or negative score outliers
- possible harness or benchmark-specific failures

## Leaderboard

Live leaderboard: [Harbor Leaderboard](https://harborsubabase.vercel.app/leaderboard)

## Usage

This repo is meant to be passed to an LLM agent such as Codex, Claude Code, or a similar coding assistant.

Give the agent `leaderboard-audit-v1.md` and ask it to run the audit workflow. The agent should:

1. fetch leaderboard data
2. run the anomaly analysis
3. compare flagged results against historical and adapter reference data
4. produce a structured audit report

## Notes

The current workflow is centered on selected OpenAI, Anthropic, and Google model families and uses Harbor leaderboard data as the source of truth for analysis.
