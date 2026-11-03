# Setup runbook (Phase 0 → first pilot ticket)

These steps need your accounts, so they can't be automated from this repo.

## 1. Servers
- Two Linux servers. Control: about 4 vCPU and 16 GB. Worker: about 8 vCPU and 32 GB. Both get Docker and
  a private network (Tailscale or WireGuard). Allow SSH keys only, and a firewall that opens only 80/443 on
  the control server.
- Worker: install gVisor and register it with Docker as runtime `runsc`
  (`runsc install && systemctl restart docker`). Check it with `docker run --runtime runsc hello-world`.

## 2. GitHub Apps (one per role)

| App | Repository permissions |
| --- | --- |
| `orchestrator` | Issues: write; Pull requests: read; Checks: read; Metadata: read. Organization projects: write, if you mirror to a board |
| `engineer` | Contents: write; Pull requests: write; Issues: read |
| `reviewer` | Contents: read; Pull requests: write; Checks: read |

Install all three on each product repo. Save each private key as
`/etc/agent-company/github-apps/<role>.pem` (mode 0400) on both servers. Only the `orchestrator` App
needs a webhook: `https://<PUBLIC_HOSTNAME>/webhooks/github`, events **Issues** and **Pull requests**.

On each product repo, protect `main`: require a PR, require passing CI, require one code-owner
review, and disallow force pushes. Copy `templates/product-brain/` into the repo and fill it in.

## 3. Secrets
Fill `.env.example` into `infra/secrets/control.enc.env` and `worker.enc.env`, then encrypt them with
`sops` (see `infra/secrets/README.md`).

## 4. Bring up the stacks
    # both servers
    sandbox-images/build.sh                       # worker only: sandbox images
    # control server
    sops exec-env infra/secrets/control.enc.env 'docker compose -f infra/control/docker-compose.yml up -d --build'
    # worker server
    sops exec-env infra/secrets/worker.enc.env 'docker compose -f infra/worker/docker-compose.yml up -d --build'

Register the product. Use the stack image that matches its language:

    docker compose -f infra/control/docker-compose.yml run --rm migrate \
      python -m orchestrator.admin add-product shop acme/shop agent-company/sandbox-python:latest

Schedule `infra/backup/backup.sh` daily, and run `restore-test.sh` once now and then monthly.

## 5. Pilot
Label a small, well-specified issue `agent-ready`. Watch it in the Temporal UI
(`http://<control private IP>:8080`), in Slack, and in the `runs` table. Run 10 small tickets and tune
`agents/*/prompt.md` from the failures. That is the Phase 1 exit gate.
