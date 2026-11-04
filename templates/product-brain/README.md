# Product brain template

Copy into a product repo to onboard it. Every agent reads `docs/brain/*` (appended to its system
prompt, which makes it the main prompt-caching block) and the repo's `CLAUDE.md` before working.
Keep it short and current: stale brain content misleads every agent on every run.

It also carries `.agent-company/policy.yaml` (which changes may merge without you) and a
`CODEOWNERS` that lists only the sensitive paths. Review both for this product before onboarding.

Then register the product: `python -m orchestrator.admin add-product <name> <owner/repo> <sandbox image>`.
It starts at autonomy level 0 (shadow mode): you merge everything while the platform builds a
track record, and it proposes promotions when that record earns them.
