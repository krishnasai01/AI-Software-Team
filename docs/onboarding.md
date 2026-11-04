# Onboarding assistant

An interactive assistant that takes a team from nothing to running. It checks what is already done,
asks only for what is missing, does the automatable steps itself (each after your yes), and walks
you through the rest. The engine is generic: a **playbook** describes one team, and
`onboarding/playbooks/software-team/` is the one for this platform. `docs/setup.md` stays as the
manual reference for the same steps.

## Running it

    uv run python -m onboarding software-team               # chat with a model
    uv run python -m onboarding software-team --no-model    # step-by-step wizard: no model, no API key
    uv run python -m onboarding software-team --status      # print what is done; exit 1 if not finished
    uv run python -m onboarding --list

Options: `--model` (default `claude-haiku-5-5`; a checklist needs no more, use `claude-sonnet-5-5`
for harder troubleshooting), `--budget` (model spend cap per session, default $2), `--state-dir`,
`--reset` (forget progress and stored secrets).

The chat uses your own credentials, `ANTHROPIC_API_KEY` or a Claude Code login, not the model
proxy, which does not exist yet at this point. If it cannot start, it offers the wizard instead.

Run it from a checkout of this repo, on your own machine or on the control server. Servers are
reached over ssh (`user@host`) or run locally (`local`). Progress is saved after every step, so
quitting (`quit`, Ctrl-D or Ctrl-C) is always safe, and running it again resumes: nothing is
remembered as "done"; every step is re-detected each time.

## What it does and what stays with you

| It does, after you say yes | You do |
| --- | --- |
| install gVisor on the worker | provision the two servers and Docker |
| clone this repo onto both servers | install `sops` and `age` |
| create age keys, list them in `.sops.yaml` | create the three GitHub Apps in your browser |
| put each App's key on the servers (root, 0400) | install the Apps on each repo |
| set the webhook URL and secret through the API | merge the product-brain PR and the rehearsal PR |
| write, encrypt and copy `control/worker.enc.env` | run the safety checks (guided) |
| build the sandbox images, start both stacks, migrate | |
| open the product-brain PR, set auto-merge, create the ruleset | |
| register products, create the rehearsal issue | |

## How it keeps you safe

- **The model only steers.** Its whole world is nine tools (`status`, `explain`, `detect`, `ask`,
  `run`, `add_target`, `acknowledge`, `skip`, `finish`). It has no shell, no file access and no
  GitHub access. It can only run actions the playbook registered.
- **Your yes is asked by the engine, at your terminal.** Before any action it prints exactly what it
  will do. A model cannot answer for you, acknowledge a step, or skip one.
- **A step is done only when its probes say so.** The model saying "done" changes nothing.
- **Secrets never reach the model.** They are typed at hidden prompts or generated. Every string
  that goes to the model, the log or the terminal is scrubbed of every known secret value first.
  They go to `sops` on stdin and to servers over ssh, never into a command line. The session file
  holds none. Secrets the assistant generates (the webhook secret must match in GitHub and in the
  encrypted file, so it has to survive between sessions) are kept in a `0600` file under
  `~/.agent-company/onboarding/`. Secrets you type (Anthropic key, Slack token, GitHub token) stay in
  memory only, and you are asked again when a step needs them. A finished step stays finished without them.
- **Text from servers and GitHub reaches the model as `<untrusted>` data.**
- **Least privilege is checked, not assumed.** The App probe fails if an App has fewer permissions
  than its role needs, or more (a reviewer that can write code is flagged).
- **The ruleset is judged by the same function the reconciler uses**
  (`orchestrator.sweeps.config_problems`), so onboarding and runtime agree on what "protected" means.

## The software-team checklist

`operator` → `servers` → `tools` → `gvisor` → `checkout` → `age-keys` → `app-orchestrator`,
`app-engineer`, `app-reviewer` → `webhook` → `secrets` → `images` → `stacks` → `database` →
`scratch-repo` → per repo: `repo-access`, `repo-brain`, `repo-settings`, `repo-ruleset`,
`repo-register` → `rehearsal` → `rehearsal-safety` → `product-repo` (then the same per-repo steps for
your real product).

Two orderings matter. The brain PR must merge **before** the ruleset exists, or the ruleset would
block your own merge. And a real product cannot start until the scratch rehearsal has passed.
Every product starts at autonomy level 0, where you approve every merge.

## Writing a playbook for another team

A playbook is a directory: `playbook.yaml`, and optionally `pack.py` for team-specific probes and
actions. Run it with `python -m onboarding path/to/dir`. A `pack.py` is code that runs with your
powers: only load playbooks you trust.

```yaml
name: support-team
title: Support team
intro: ...            # shown at the start
guide: ...            # extra context for the model
finish: ...           # shown when every step is done; may use {input_name}
inputs:               # asked when a step first needs them
  zendesk_url: {prompt: Zendesk URL, pattern: 'https://[a-z0-9.-]+'}
  api_token: {prompt: API token, kind: secret}          # hidden; never reaches the model
  region: {prompt: Region, kind: choice, options: [eu, us], default: eu}
targets:              # things the checklist repeats for (here: queues)
  queue: {title: queue, min: 1, inputs: {name: {prompt: Queue name}}}
steps:
  - id: account
    title: Zendesk account
    needs: []                          # earlier steps only
    inputs: [zendesk_url, api_token]
    detect: [{probe: zendesk_ok, args: {url: '{zendesk_url}'}}]
    do: {action: create_webhook, args: {url: '{zendesk_url}'}}   # optional; always confirmed first
    manual: "What the operator does by hand."                   # optional
  - id: queues
    title: Queues
    needs: [account]
    collect: queue                      # done when `min` queues exist; running it adds one
  - id: queue-routing
    title: Routing for {name}
    per: [queue]                        # repeats for each queue
    needs: [queues]
    inputs: [name]
    detect: [{probe: routing_ok, args: {queue: '{name}'}}]
```

Step fields: `needs`, `inputs`, `detect` (all probes must say done), `do`, `manual`, `per`,
`collect`, `when` (apply only if inputs have these values; otherwise the step counts as satisfied),
`ack` (nothing can detect it: you confirm it), `optional` (you may skip it). Input fields: `prompt`,
`kind` (`text`, `secret`, `choice`), `help`, `default`, `options`, `pattern` (full match), `optional`,
`persist: false` (secrets kept in memory only), `generate: true` (a random secret, never asked),
`suggest` (look in `{env: NAME}` or `{cmd: [argv]}` first).

Templates use `{name}`; write `{{` and `}}` for literal braces. Secrets cannot be used in templates.
A step that needs a per-target step waits for every instance of it. The loader rejects, with a list
of every problem: unknown fields, ids and inputs, forward `needs`, placeholders a step cannot see,
secrets in templates, bad patterns, and unknown probes or actions.

`pack.py` defines `register(reg)`:

```python
def register(reg):
    @reg.probe("zendesk_ok")
    async def zendesk_ok(ctx, url):  # ctx.values, ctx.secret(name), ctx.shell(host), ctx.root
        return done("reachable") if ... else missing("why not, and what to do")

    @reg.action("create_webhook", preview=lambda ctx, url: f"Create a webhook on {url}")
    async def create_webhook(ctx, url):
        return ActionResult(True, "created")  # the summary reaches the model: no secrets in it
```

Rules for probes and actions: probes only read. Actions are idempotent, return a result instead of
raising, pass secrets on stdin (never in argv), and quote everything they put in a shell command.
`onboarding/lib/` has ready-made probes (`ssh_ok`, `docker_ok`, `commands`, `docker_runtime`,
`docker_images`, `path_exists`, `http_status`, `containers_up`, `repo_access`, `repo_settings`,
`file_on_branch`) and actions (`shell`, `set_repo_settings`). Test a playbook with
`tests/onboarding_fakes.py` (`Toy`, `FakeShells`, `FakeRepoGitHub`, `FakeAdmin`).

## What has and has not been verified

Verified in tests: the engine (ordering, gating, targets, resume), every probe and action against
fakes, the exact commands and payloads sent to servers and GitHub (including that secrets only
travel on stdin), the ruleset against `config_problems`, the compose files' variables against what
onboarding writes, and a live chat with a real model.

Not verified against real systems, because this repo's tests cannot reach them: ssh and sudo on real
servers, the gVisor installer, `sops` encrypting from stdin, GitHub's rulesets, git-data and
App-webhook APIs on a real repo (rulesets on a private repo need a paid GitHub plan), and Caddy's
certificate. That is why the checklist sends you through a scratch repo first.
