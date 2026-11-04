# Secrets

Create `control.enc.env` and `worker.enc.env` from `../../.env.example`, then encrypt in place:

    sops --encrypt --in-place infra/secrets/control.enc.env

Deploy with `sops exec-env infra/secrets/control.enc.env 'docker compose -f infra/control/docker-compose.yml up -d'`
so plaintext never touches disk. GitHub App private keys (`<role>.pem`) live in
`/etc/agent-company/github-apps/` on the servers (mode 0400), never in the repo and never in a sandbox.
