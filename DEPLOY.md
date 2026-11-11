# Deploy

Apply the open pull requests in the order below, then use the wrappers in `docs/deploy/`. Replace `example.invalid` with the DNS name for that step (YOUR_HOST). Do not commit the real name, an address, a key, or a phone number.

This file is a list of steps to run. It is not a report that a live install, a Caddy reload, or `systemctl enable` succeeded. The last section lists the commands that were actually run.

## Human-only steps

Nothing in this repository does these.

- Buy one temporary line per friend from 019. No script buys a line or sends a WhatsApp message.
- In the OpenAI account, create one key per friend and set a hard usage cap of 20 USD. `pkg/.env.example` contains `COST_CEILING_USD=30`. That example field is not the provider cap.
- Move the server in the Hetzner console. After the host pull request, the click-path in `pkg/host/hetzner-transfer.md` is: open the friend's project, choose Security, open Members, choose Add member, and invite the current project owner. The current owner accepts, opens the project that holds the server, opens the server, chooses Transfer to Project, and confirms the friend's project. No script calls a Hetzner API for this. Do not paste a friend's API token. The host and dedicated installers refuse `--hetzner-token`, `--hcloud-token`, and `--api-token`.

DNS, the bind edit, `PUBLIC_INSTALL_URL`, the Caddy paste, and a cloud-init paste are also hand steps.

## 1. Assemble the checkout

Start from a clean checkout of the baseline branch `cursor/import-baseline-secret-scan-9b4c`. Copy the repository onto the server without putting a credential on the command line.

Before each merge, keep the current commit:

```bash
git branch "backup-$(date -u +%Y%m%dT%H%M%SZ)"
```

Repeating that creates another backup branch and does not change files. To undo one merge that has not been shared, and only when the work tree is clean:

```bash
git merge --abort
git reset --hard BACKUP-BRANCH
```

`git merge --abort` applies only while a merge is in progress. `git reset --hard` discards uncommitted work. Do not use it on a dirty tree.

Merge in this order. A second merge of a branch that is already contained exits 0 and prints `Already up to date`.

```bash
git merge --no-edit origin/cursor/security-qa-ci-5f37
git merge --no-edit origin/cursor/host-selfhost-isolation-1e28
git merge --no-edit origin/cursor/onboarding-own-server-support-4d16
git merge --no-edit origin/cursor/bot-release-runtime-9c0b
git merge --no-edit origin/cursor/pilot-ledger-1078
git merge --no-edit origin/cursor/deploy-runbook-a741
```

That is baseline, pull request 2, pull request 5, pull request 6, pull request 4, pull request 7, then this runbook. These branches do not share edited files, so the merges do not need conflict resolution. Do not merge them to the default branch from this runbook.

## 2. Pull request 2, tests only

`.github/workflows/tests.yml` runs `bash .github/scripts/run-area-tests.sh` in CI. That script runs pytest in directories that exist and, when `pkg/host/check-selfhost.sh` exists, runs that too. It does not install the assistant and it does not replace the secrets workflow.

On the assembled tree:

```bash
python3 -m pip install --disable-pip-version-check pytest==9.2.0
bash .github/scripts/run-area-tests.sh
```

There is nothing to roll back on the server. The script does not enable a service.

## 3. Pull request 5, host and one dedicated server

`pkg/host/install-host.sh` still accepts only `--onboard-host` for a real run. It also accepts `--dry-run`, which does not install packages and does not need root. It refuses an API token flag.

`pkg/install.sh` still accepts `--yes`, `-y`, `--setup-token`, `--domain`, and `--restore`. It also accepts `--dry-run` and `--render-dir`. `SELF_HOST` defaults to 1. Any other value makes the script exit. It refuses an API token flag.

Check the tree before a root install:

```bash
bash pkg/host/install-host.sh --dry-run
bash pkg/install.sh --dry-run --yes --render-dir /tmp/assistant-selfhost-proof
python3 pkg/host/agentsctl self-check
bash pkg/host/check-selfhost.sh
```

`check-selfhost.sh` exits non-zero when docker and podman are both missing. That is a failed check, not a pass. The shell linter and the rootless dry-run still run before that failure.

The real shared-host command is unchanged. The wrapper backs up the previous files, including the release units this pull request adds, then runs the installer:

```bash
bash docs/deploy/apply-tenant-host.sh shared --onboard-host example.invalid --print-only
sudo bash docs/deploy/apply-tenant-host.sh shared --onboard-host example.invalid
```

The installer command inside the wrapper is:

```bash
sudo bash pkg/host/install-host.sh --onboard-host example.invalid
```

That script's header says a re-run refreshes shared code and unit files and keeps tenant homes, `registry.json`, invite codes, and per-tenant secrets. It copies `pkg/host/systemd/assistant-*`, which includes `assistant-release@.service` and `assistant-release@.path` after this pull request. It runs `systemctl enable --now` for `assistant-status.timer` and `assistant-provision.path`. This runbook does not claim that enable succeeded. The wrapper does not pass `--dry-run` on this path.

File rollback, using the archive path the wrapper prints:

```bash
sudo bash docs/deploy/rollback.sh /var/backups/assistant-deploy/ARCHIVE.tar.gz
sudo systemctl daemon-reload
sudo systemctl stop assistant-status.timer assistant-provision.path assistant-provision.service
```

Run the `systemctl` lines yourself. `rollback.sh` does not run them, does not delete the user `assistant-onboard`, and does not uninstall packages.

`agentsctl` gains `release-access SLUG`, `check-isolation`, and `self-check`. The other subcommands are unchanged: `create`, `list`, `start`, `stop`, `restart`, `delete`, `link`, `status`, `capacity`, `export`, `invite`, `process-requests`. `create` still accepts `--bot-name`, `--owner-name`, `--owner-phone`, `--no-start`, and `--force`. Do not put `--owner-phone` in a saved command. Do not use `--force` here. Creating a bot enables the release path unit for that slug inside `agentsctl`. This runbook does not claim that enable succeeded.

```bash
sudo /usr/local/sbin/agentsctl capacity
sudo /usr/local/sbin/agentsctl create friend-one --bot-name assistant
sudo /usr/local/sbin/agentsctl list
```

`create` exits if the slug already exists. To undo that create:

```bash
sudo /usr/local/sbin/agentsctl delete friend-one --yes
```

`--purge` exists. Do not pass it here. `delete` archives under `/opt/agents/_archive` unless `--purge` is also present.

One dedicated server is still:

```bash
bash docs/deploy/apply-tenant-host.sh dedicated --domain example.invalid --print-only
sudo bash docs/deploy/apply-tenant-host.sh dedicated --domain example.invalid
```

The installer command is `sudo bash pkg/install.sh --yes --domain example.invalid`. The wrapper does not accept `--setup-token` or `--dry-run`. `--restore FILE` is passed through when you set it. Do not point `--restore` at a file inside `/opt/agent`.

`pkg/deploy/cloud-init.yaml` is still a paste into the Hetzner create-server form. Replace `__PKG_URL__` with an `https` URL that does not contain an account name. The placeholder is `https://example.invalid/agent.tar.gz`. Replace `__SETUP_TOKEN__` on the server, not in git:

```bash
head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32
```

The yaml runs `SELF_HOST=1 bash .../install.sh --yes --setup-token ...`. No script submits that file.

Move one bot with the existing commands `agentsctl stop`, `agentsctl export`, and `install.sh --restore`. The export contains secrets. Do not commit it.

## 4. Pull request 6, onboarding

`central/deploy.sh` no longer looks for `agent-template.tar.gz`. `tools/package.sh` is still not in the repository. The baseline copy of `deploy.sh`, before this pull request, does require that archive and names `tools/package.sh`. The onboarding wrapper refuses to start while the checkout's `deploy.sh` still contains `agent-template.tar.gz`.

`deploy.sh` still takes one host and no flags. It does not edit Caddy. If `PUBLIC_INSTALL_URL` is set, it must be an `https` URL whose path ends in `/install.sh`. The unit ships `PUBLIC_INSTALL_URL=https://example.invalid/install.sh`. The wrapper does not set the variable. Export the real URL in the server shell before apply, and do not commit it. If the variable is unset, `deploy.sh` leaves the example value in the unit.

`ONBOARD_BIND=192.0.2.1` and `ONBOARD_PORT=8840` are still the shipped examples. `deploy.sh` refuses `0.0.0.0`. The wrapper also refuses the placeholder `192.0.2.1`. Edit the unit on the server checkout only. Back it up first:

```bash
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$HOME/assistant-deploy-notes"
cp -a central/agent-onboarding.service "$HOME/assistant-deploy-notes/agent-onboarding.service.$stamp"
```

To undo that edit:

```bash
cp -a "$HOME/assistant-deploy-notes/agent-onboarding.service.$stamp" central/agent-onboarding.service
```

Then:

```bash
bash docs/deploy/apply-onboarding-host.sh --host example.invalid --print-only
sudo bash docs/deploy/apply-onboarding-host.sh --host example.invalid
```

The installer command is `sudo bash central/deploy.sh example.invalid`. It enables and restarts `agent-onboarding` before its health request. This runbook does not claim that enable, that restart, or that health request succeeded. If the script exits after copying files, roll back the archive it printed, then:

```bash
sudo systemctl stop agent-onboarding
sudo systemctl daemon-reload
```

Those two lines were not run here.

Print the Caddy block. The script substitutes `__ONBOARD_HOST__`, `__ONBOARD_BIND__`, and `__ONBOARD_PORT__` the same way `deploy.sh` does after this pull request. It does not edit Caddy.

```bash
bash docs/deploy/print-caddy-snippet.sh --host example.invalid
```

If the host is already in the Caddy file, do not add a second block. Backup the file you will edit:

```bash
sudo bash docs/deploy/backup.sh onboarding-caddy --path /etc/caddy/Caddyfile
```

After this pull request, `central/Caddyfile.snippet` says to validate and reload with `CADDY_CONTAINER` set (its example name is `caddy`):

```bash
docker exec "$CADDY_CONTAINER" caddy validate --config /etc/caddy/Caddyfile && docker exec "$CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile
```

`central/README.he.md` still says `caddy validate` and `systemctl reload caddy`. Use the command that matches the Caddy you find. If validate fails, do not reload. Restore the `onboarding-caddy` archive. This was not run. Do not restore an earlier onboarding archive after the Caddy edit, or the Caddy file from before the edit comes back.

## 5. Pull request 4, release runtime

There is no new installer and no new flag. `pkg/host/install-host.sh` copies `pkg/agent/*.py` into the shared code directory when it is actually run. `pkg/install.sh` copies the same tree on a dedicated server. Re-running the wrapper from section 3 after this pull request is how that code is installed. That re-run was not done here.

`assistant-release@.path` starts `agentsctl release-access SLUG` when a tenant writes its release request. `release-access` is the subcommand from pull request 5. Do not run it against a live slug from this runbook. `agentsctl self-check` exercises it in a temporary directory only.

## 6. Pull request 7, pilot ledger

`platform/assistant_manager.py` accepts only `serve`, `provision`, `tick`, and `render`. `AM_DATA` is required. A path whose second component is `root` is rejected. There is no unit file in this pull request. Do not invent a `systemctl enable` for it.

Use `/var/lib/assistant-manager` unless you have another path outside `/root`. Back up before the first write:

```bash
sudo mkdir -p /var/lib/assistant-manager
sudo bash docs/deploy/backup.sh manager --path /var/lib/assistant-manager
```

Set the origin yourself. The placeholder is `https://example.invalid`. `AM_BIND_PROXY` ships as `192.0.2.1`. Replace it before `serve`. `AM_DRY_RUN=1` is the switch that keeps the WhatsApp alert path from sending.

```bash
sudo AM_DATA=/var/lib/assistant-manager AM_DRY_RUN=1 AM_PUBLIC_ORIGIN=https://example.invalid AM_BIND_PROXY=127.0.0.1 python3 platform/assistant_manager.py render
```

`render` writes the page to stdout and creates files under `AM_DATA`. `serve` binds a port and was not run. `provision` and `tick` were not run. To undo the data directory, restore the `manager` archive. That removes files the backup recorded, including a directory that was absent when the backup was taken.

## 7. Wrappers

| Script | What it calls |
| --- | --- |
| `docs/deploy/apply-tenant-host.sh shared` | `pkg/host/install-host.sh`, and `--onboard-host` only when you pass it |
| `docs/deploy/apply-onboarding-host.sh` | `central/deploy.sh HOST` with no flags, after the pull request 6 copy of that script is in the tree |
| `docs/deploy/apply-tenant-host.sh dedicated` | `pkg/install.sh --yes --domain HOST`, and `--restore FILE` only when you pass it |
| `docs/deploy/print-caddy-snippet.sh` | the same host, bind, and port substitution `central/deploy.sh` prints; it does not edit Caddy |
| `docs/deploy/backup.sh` | copies allowlisted paths into a new archive |
| `docs/deploy/rollback.sh` | restores one archive |

`--print-only` prints the command and does not take a backup. Repeating an apply takes a new backup, then runs the same installer command. Repeating `rollback.sh` on the same archive restores the same files. That repeat was tested in a temporary directory by `docs/deploy/self-check.sh`, not on a live server.

`rollback.sh` does not call `systemctl`, delete users, uninstall packages, or change the firewall. Archives are mode `600`. Leave them on the server.

On the live server, leave these unset: `ASSISTANT_REHEARSE`, `ASSISTANT_ROOT`, `ASSISTANT_UNIT_FILE`, `ASSISTANT_DEPLOY_SH`, `ASSISTANT_OS_RELEASE`. `ASSISTANT_BACKUP_DIR` may point somewhere other than `/var/backups/assistant-deploy`.

## Executed for this update

In a temporary checkout, not on a live server, these merges were run with `git merge --no-edit` and finished without a conflict:

- `origin/cursor/security-qa-ci-5f37` (fast-forward)
- `origin/cursor/host-selfhost-isolation-1e28`
- `origin/cursor/onboarding-own-server-support-4d16`
- `origin/cursor/bot-release-runtime-9c0b`
- `origin/cursor/pilot-ledger-1078`

A second `git merge --no-edit origin/cursor/pilot-ledger-1078` printed `Already up to date` and exited 0.

On that assembled tree:

| Command | Result |
| --- | --- |
| `bash pkg/host/install-host.sh --dry-run` | exit 0, `install-host dry-run: ok` |
| `bash pkg/install.sh --dry-run --yes --render-dir /tmp/assistant-selfhost-proof` | exit 0, `dry-run rendered: /tmp/assistant-selfhost-proof` |
| `python3 pkg/host/agentsctl self-check` | exit 0, `self-check: ok` |
| `bash pkg/host/check-selfhost.sh` | exit 1, `container dry-run: fail (no docker or podman)` |
| `python3 -m pip install --disable-pip-version-check pytest==9.2.0` then `bash .github/scripts/run-area-tests.sh` | pytest passed in `pkg/agent/tests` (21), `central/tests` (16), `platform/tests` (18), and `tests` (9). The script then ran `check-selfhost.sh` and exited 1 for the same missing container runtime |
| `AM_DATA=/tmp/am-data-proof AM_DRY_RUN=1 AM_PUBLIC_ORIGIN=https://example.invalid AM_AGENTS_ROOT=/tmp/am-agents-proof python3 platform/assistant_manager.py render` | exit 0, HTML on stdout |

`docs/deploy/self-check.sh` is the wrapper test on this runbook branch. It does not run the installers.

Not executed: `install-host.sh` without `--dry-run`, `install.sh` without `--dry-run`, `central/deploy.sh`, Caddy validate or reload, `systemctl`, `docker`, `agentsctl` against `/opt/agents`, `agentctl`, `assistant_manager.py serve`, `provision`, or `tick`, the Hetzner console, a 019 purchase, and OpenAI key creation. No live `/opt` or `/etc` path was changed.
