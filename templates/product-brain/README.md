# Product brain template

Copy into a product repo to onboard it. Every agent reads `docs/brain/*` (appended to its system
prompt, which makes it the main prompt-caching block) and the repo's `CLAUDE.md` before working.
Keep it short and current: stale brain content misleads every agent on every run.

Then register the product: `python -m orchestrator.admin add-product <name> <owner/repo> <sandbox image>`.
