# Setup runbook (Phase 0 → rehearsal → pilot)

These steps need your accounts, so they can't be automated from this repo. `docs/design-v2.md`
explains what each piece is for.

## 1. Servers

- Two Linux servers with Docker on a private network (Tailscale or WireGuard):
  - control: about 4 vCPU and 16 GB;
  - worker: about 8 vCPU and 32 GB.
- SSH keys only. The firewall opens 80/443 on the control server and nothing else to the public.
- On the worker, install gVisor and register it with Docker as runtime `runsc`
  (`runsc install && systemctl restart docker`). Check it with `docker run --runtime runsc hello-world`.

## 2. GitHub Apps

| App | Repository permissions |
| --- | --- |
| `orchestrator` | Contents: write (auto-merge, branch updates, reverts); Issues: write; Pull requests: write; Checks: write; Actions: write (re-run failed CI); Metadata: read (also covers reading rulesets) |
| `engineer` | Contents: write; Pull requests: write |
| `reviewer` | Contents: read; Pull requests: write |

- **Install** all three on each product repo.
- **Private keys:** save each one as `/etc/agent-company/github-apps/<app>.pem` (mode 0400) on both servers.
- **Webhook:** only the `orchestrator` App needs one, at `https://<PUBLIC_HOSTNAME>/webhooks/github`.
  - Events: Issues, Issue comment, Pull request, Pull request review, Check suite, Check run, Status.
- **Trusted labelers:** add the orchestrator App's bot login (`<slug>[bot]`) to `TRUSTED_LABELERS`
  alongside yours, so the tickets the tech lead splits out stay trusted.

## 3. Each product repo

1. Copy `templates/product-brain/` into the repo and fill it in. Review two files for this product
   in particular:
   - `.agent-company/policy.yaml`, which decides what may ever merge without you;
   - `CODEOWNERS`, which lists only the sensitive paths, with no catch-all line.
2. In **Settings → General**, turn on "Allow auto-merge" and "Automatically delete head branches".
3. Add a **ruleset** for the default branch with:
   - require a pull request, with 0 required approvals;
   - require review from Code Owners;
   - dismiss stale approvals;
   - require approval of the most recent push;
   - required status checks: your CI checks plus `agent-company/policy`, with the source set to the
     orchestrator App;
   - require branches to be up to date, or use a merge queue (recommended for organization repos,
     since it avoids re-approval churn);
   - block force pushes and deletion;
   - no bypass list.

   The reconciler verifies this ruleset every few minutes. Until it's right, nothing merges without
   you and you get one alert a day saying what's missing.

## 4. Secrets and stacks

Fill `.env.example` into `infra/secrets/control.enc.env` and `worker.enc.env`, and encrypt them
with `sops` (see `infra/secrets/README.md`). Then:

    # worker server
    sandbox-images/build.sh
    sops exec-env infra/secrets/worker.enc.env 'docker compose -f infra/worker/docker-compose.yml up -d --build'
    # control server
    sops exec-env infra/secrets/control.enc.env 'docker compose -f infra/control/docker-compose.yml up -d --build'

Schedule `infra/backup/backup.sh` daily, and run `infra/backup/restore-test.sh` now and then monthly.

## 5. Rehearsal ($0)

1. Create a scratch repo set up as in step 3, and register it with the scripted-agent image:

       docker compose -f infra/control/docker-compose.yml run --rm migrate python -m orchestrator.admin \
         add-product scratch you/scratch agent-company/sandbox-rehearsal:latest

2. Label an issue `agent-ready`. Expect a PR from the engineer App and an approval from the reviewer
   App. Then expect a pending `agent-company/policy` check with a comment asking for your approval,
   since the product is at level 0.
3. Approve the PR. It should merge on its own, and the digest should list it the next morning.
4. Test the safety paths on purpose:
   - label an issue from an account not on the trusted list: it should be refused;
   - run `orchestrator.admin pause`: new work should stop;
   - set the scratch product to level 1 with `orchestrator.admin set-level scratch 1`, and push a
     commit that breaks CI on `main` right after an auto-merge: it should be reverted
     automatically, and the product demoted.
5. Confirm in LiteLLM that per-run keys and per-role teams appear. The rehearsal makes no model
   calls, so do one real run on the scratch repo with the normal image to check the
   `/anthropic` pass-through route.

## 6. Pilot

Register the real product with its stack image (for example `agent-company/sandbox-python:latest`).
It starts at level 0, shadow mode: you approve every merge while the platform records what it
would have merged.

Label 10 small, well-specified issues `agent-ready`. Watch Slack, the digest and the `runs` table,
and tune `agents/*/prompt.md` from the failures (prompt changes need an image rebuild). Once the
track record supports it, the platform proposes promotion to level 1. Approve it with
`orchestrator.admin approve <id>`.
