# Goal: <product name>

<!--
Copy this file, fill it in, and run:  scripts/devteam.sh path/to/your-goal.md
Delete any section you can't answer yet; the planning phases will ask.
Placeholder lines that still start with "<" are ignored.
-->

## Product summary

<One paragraph: what it is, the problem it solves, and why now.>

## Users

- <Primary user: who they are and the job they need done>
- <Secondary user or admin, if any>

## Features

- <Feature: one line describing what the user can do>
- <Feature>
- <Feature>

## Critical features

<!--
List the features from above where a bug could:
  - lose money
  - expose or corrupt data
  - break security or access control
  - violate legal or compliance rules
  - cause an irreversible action
  - break the product's core value
These are built test-first with Superpowers, each in its own git worktree.
Everything else is standard and built with OMC.
Leave the list empty to have /devteam infer it; you approve the result either way.
-->

- <Feature> — <which criterion it meets, e.g. "takes payments: could lose money">

## Tech stack

- Language / framework: <e.g. TypeScript, Next.js>
- Data store: <e.g. Postgres>
- Hosting / deploy target: <e.g. Fly.io, Vercel, internal k8s>
- Constraints: <must-use libraries, things to avoid, existing systems to integrate with>

## Acceptance criteria

- [ ] <Observable outcome a user or reviewer can check>
- [ ] <Performance, security or compliance bar>
- [ ] <What "shipped" means: deployed where, behind which flag, for whom>
