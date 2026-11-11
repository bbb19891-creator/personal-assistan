# Deploy

Operator steps for the baseline installers in this repository. Replace `example.invalid` with the DNS name for that step (YOUR_HOST). Do not commit the real name, an address, a key, or a phone number.

Commands are run from the repository root on the server. Copy the repository onto the server without putting a credential on the command line. The clone URL is not written here.

This file is a list of steps to run. It is not a report that they succeeded on a live server. The last section says what was executed in this change.

Exit code 2 from a wrapper means the installer was not started. Any other non-zero code means the installer ran and may already have changed files. Use the archive path the wrapper printed.

## Human-only steps

Nothing in this repository does these. Do not treat them as part of `install-host.sh`, `deploy.sh`, or `install.sh`.

### Temporary 019 lines

Buy one temporary line per friend from 019. No script buys a line, stores a number, or sends a WhatsApp message. Pairing stays on the setup page after install. This runbook does not change bridge pairing.

### OpenAI keys, 20 USD hard cap

In the OpenAI account, create one key per friend and set a hard usage cap of 20 USD on that key. No script creates a key or sets that provider cap.

`pkg/.env.example` contains `COST_CEILING_USD=30`. That is a bot-side example field. It is not the provider hard cap, and it is not a stand-in for the 20 USD cap.

### Hetzner project transfer

Do this in the Hetzner Cloud console:

1. The friend invites the current project owner as a member of the friend's project.
2. The current owner uses Transfer to Project to move the server into the friend's project.

No script calls a Hetzner API for this. Do not paste a friend's API token. `central/onboard.py` still has a page at `/own-server` that asks for a token. That page is not this transfer. This runbook does not use it.

### Also done by hand

- Create the DNS record for the onboarding name.
- Replace the bind placeholder in the unit file on the server checkout.
- Place `agent-template.tar.gz` (the packager is not in this tree; see below).
- Add the Caddy site block and reload Caddy.
- Paste cloud-init into the Hetzner create-server form, if you use that path.
- Pair WhatsApp on the setup page.

## Scripts that are not in this repository

`central/deploy.sh` exits unless `<repo>/agent-template.tar.gz` exists. Its message says to run `tools/package.sh` first. `tools/package.sh` is not in this repository. This runbook does not invent that script or any flags for it.

The installer that does exist for a server is `pkg/install.sh`. `central/deploy.sh` only copies whatever file you placed at `<repo>/agent-template.tar.gz`. `central/onboard.py` serves that file. The cloud-init text in `central/onboard.py` and in `pkg/deploy/cloud-init.yaml` unpacks an archive and runs `install.sh` from the unpacked directory. This change did not build an archive and does not check that an archive is valid. It only checks that the file is present before it will call `deploy.sh`.

`PUBLIC_INSTALL_URL` is not read by the baseline onboarding code. Do not add a curl command that contains an account name.

`platform/assistant_manager.py` is not part of this runbook. Its usage line is `serve|provision|tick|render`. No installer here wraps it. This change did not start it.

## Wrappers

| Script | What it calls |
| --- | --- |
| `docs/deploy/apply-tenant-host.sh shared` | `pkg/host/install-host.sh`, and `--onboard-host` only when you pass it |
| `docs/deploy/apply-onboarding-host.sh` | `central/deploy.sh HOST` with no flags |
| `docs/deploy/apply-tenant-host.sh dedicated` | `pkg/install.sh --yes --domain HOST`, and `--restore FILE` only when you pass it |
| `docs/deploy/print-caddy-snippet.sh` | the same `sed` replacement as `central/deploy.sh`; it does not edit Caddy |
| `docs/deploy/backup.sh` | nothing; it copies allowlisted paths into a new archive |
| `docs/deploy/rollback.sh` | nothing; it restores one archive |

`--print-only` prints the installer command and does not take a backup. Repeating an apply script takes a new backup, then runs the same installer command again. Repeating `backup.sh` writes another archive and does not change the live paths. Repeating `rollback.sh` on the same archive restores the same files. That repeat was tested in a temporary directory by `docs/deploy/self-check.sh`, not on a live server.

`rollback.sh` restores only paths on its allowlist (`path_allowed` in `docs/deploy/lib.sh`). It does not call `systemctl`, delete users, uninstall packages, or change the firewall.

On the live server, leave these unset: `ASSISTANT_REHEARSE`, `ASSISTANT_ROOT`, `ASSISTANT_UNIT_FILE`, `ASSISTANT_TEMPLATE_TAR`, `ASSISTANT_OS_RELEASE`. Apply mode exits 2 if any of them is set. `ASSISTANT_BACKUP_DIR` may be set when you want archives somewhere other than `/var/backups/assistant-deploy`.

Archives are mode `600`. They can contain copies of environment files. Leave them on the server. Do not commit them.

## 1. Shared tenant host

`pkg/host/install-host.sh` prepares one machine for isolated assistants. In this baseline, `central/deploy.sh` is installed on that same machine: it requires the user `assistant-onboard`, which `install-host.sh` creates. A dedicated server for one friend is section 3, and it does not use `install-host.sh`.

The only flag `install-host.sh` accepts is `--onboard-host`.

See the command without changing the machine:

```bash
bash docs/deploy/apply-tenant-host.sh shared --onboard-host example.invalid --print-only
```

Apply it:

```bash
sudo bash docs/deploy/apply-tenant-host.sh shared --onboard-host example.invalid
```

The wrapper's installer command is:

```bash
sudo bash pkg/host/install-host.sh --onboard-host example.invalid
```

The script header says it does not start a bot and does not edit Caddy. It installs distro packages when `node`, `python3` venv, `pdftotext`, or `sqlite3` is missing, and it exits if `node` exists and is older than 20. This change did not run it and did not run apt.

If it prints `refusing: /etc/systemd/system/assistant-bot@.service exists and is not ours` (or the same sentence for `assistant-bridge@` or `assistant-setup@`), stop. Do not delete that unit from this runbook.

The wrapper prints an archive path. To put the files back:

```bash
sudo bash docs/deploy/rollback.sh /var/backups/assistant-deploy/ARCHIVE.tar.gz
sudo systemctl daemon-reload
sudo systemctl stop assistant-status.timer assistant-provision.path assistant-provision.service
```

Run those `systemctl` commands yourself. `rollback.sh` does not run them. This change did not run them. Rollback does not delete the user `assistant-onboard` and does not uninstall packages. Do not restore an older archive after a later change you want to keep.

### One assistant on that host

Subcommands that exist in `pkg/host/agentsctl`: `create`, `list`, `start`, `stop`, `restart`, `delete`, `link`, `status`, `capacity`, `export`, `invite`, `process-requests`.

`create` accepts `SLUG` and the flags `--bot-name`, `--owner-name`, `--owner-phone`, `--no-start`, and `--force`. This runbook uses the slug and `--bot-name` only. Do not put `--owner-phone` in a saved command. `--force` exists and skips the capacity refusal; do not use it here.

```bash
sudo /usr/local/sbin/agentsctl capacity
sudo /usr/local/sbin/agentsctl create friend-one --bot-name assistant
sudo /usr/local/sbin/agentsctl list
```

The slug must be 3 to 21 characters, start with a letter, and use only `a-z`, digits, and hyphens. `create` exits if that slug already exists. If it does, stop and use `list`. Do not create it again.

To undo that create, the existing command is:

```bash
sudo /usr/local/sbin/agentsctl delete friend-one --yes
```

`delete` requires `--yes`. Without `--purge` it archives the directory under `/opt/agents/_archive`. `--purge` exists; this runbook does not use it. This change did not run `agentsctl`.

`invite --count 1` prints an invite code. That code is a secret. Do not commit it. This change did not generate one.

## 2. Onboarding host

Do this on the same machine, after section 1. `central/deploy.sh` takes one positional host and no flags. It does not edit Caddy. It copies `central/agent-onboarding.service` after replacing `__ONBOARD_HOST__`, enables `agent-onboarding`, and requests `http://<bind>:<port>/health`.

### 2a. Replace the bind placeholder

The unit ships with `ONBOARD_BIND=192.0.2.1` and `ONBOARD_PORT=8840`. `192.0.2.1` is a documentation placeholder. `deploy.sh` refuses `0.0.0.0`. It does not replace `192.0.2.1`. Leave `ONBOARD_PORT` as shipped unless `deploy.sh` exits because that port is in use. This runbook does not choose a different port.

On the server checkout, not in git, set `ONBOARD_BIND` to an address Caddy can use to reach the service. The comment in `central/onboard.py` says the process binds on `127.0.0.1` when Caddy is on the host. The unit comment says to use the docker bridge address when Caddy runs in Docker. This change did not discover that address.

Back up the unit before editing it. This path is the checkout, so it is not in the wrapper allowlist:

```bash
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$HOME/assistant-deploy-notes"
cp -a central/agent-onboarding.service "$HOME/assistant-deploy-notes/agent-onboarding.service.$stamp"
```

Repeating that uses a new stamp and does not overwrite the previous copy. To undo the edit:

```bash
cp -a "$HOME/assistant-deploy-notes/agent-onboarding.service.$stamp" central/agent-onboarding.service
```

Do not commit the edited unit.

### 2b. Archive file

Put the package archive at `<repo>/agent-template.tar.gz` if you have one. If the file is missing, the wrapper exits 2 and does not call `deploy.sh`. There is no `tools/package.sh` to run.

### 2c. Plan, then apply

```bash
bash docs/deploy/apply-onboarding-host.sh --host example.invalid --print-only
```

On an unedited checkout this exits 2. That is the preflight. It must not print a `run:` line.

After the bind is no longer `192.0.2.1` or `0.0.0.0`, the port line is present, and the archive file exists:

```bash
sudo bash docs/deploy/apply-onboarding-host.sh --host example.invalid
```

The installer command is:

```bash
sudo bash central/deploy.sh example.invalid
```

The wrapper also requires the user `assistant-onboard`. If that user is missing it exits 2 before the backup.

`deploy.sh` copies files and enables the unit before the health request. If that request fails, the script exits because of `set -e`, and it may exit before printing the Caddy block. Roll back the archive the wrapper printed, then reload systemd yourself:

```bash
sudo bash docs/deploy/rollback.sh /var/backups/assistant-deploy/ARCHIVE.tar.gz
sudo systemctl stop agent-onboarding
sudo systemctl daemon-reload
```

This change did not run `deploy.sh` and did not run those `systemctl` commands. Re-running the wrapper takes a new backup and runs `deploy.sh` again. Whether that is safe to repeat is what the script does (copy files, restart the unit). It was not repeated here.

### 2d. Caddy, by hand

`deploy.sh` does not edit Caddy. Print the block:

```bash
bash docs/deploy/print-caddy-snippet.sh --host example.invalid
```

The printed block still contains `192.0.2.1`. Replace that address with the bind you set in the unit. The port in the shipped unit is `8840`. If the host is already in the Caddy file, do not add a second block:

```bash
grep -F -n -e 'example.invalid' /path/to/Caddyfile
```

Two files describe the reload, and they do not match:

- `central/Caddyfile.snippet` says Caddy runs in Docker, names the container `n8n-setup-caddy-1`, and gives this command:

```bash
docker exec n8n-setup-caddy-1 caddy validate --config /etc/caddy/Caddyfile && docker exec n8n-setup-caddy-1 caddy reload --config /etc/caddy/Caddyfile
```

- `central/README.he.md` says to add the block to `/etc/caddy/Caddyfile`, then run `caddy validate` and `systemctl reload caddy`.

Before editing, see which Caddy is running (`systemctl is-active caddy`, and `docker ps`). This change did not run either command on the live server. If `docker ps` does not show `n8n-setup-caddy-1`, do not invent another container name. Back up the file that is actually in use, then validate with the command that matches that Caddy. If validate fails, do not reload.

Backup before the edit. For a host file:

```bash
sudo bash docs/deploy/backup.sh onboarding-caddy --path /etc/caddy/Caddyfile
```

For the container named in the snippet, only after `docker ps` shows that exact name:

```bash
sudo mkdir -p /var/backups/assistant-deploy
sudo docker cp n8n-setup-caddy-1:/etc/caddy/Caddyfile /var/backups/assistant-deploy/Caddyfile.pre-edit
```

To undo a host-file edit, restore the archive from `backup.sh`. To undo the docker copy, `docker cp` the saved file back, then validate again. This change did not reload Caddy.

The onboarding apply backup also includes `/etc/caddy/Caddyfile` from before `deploy.sh`. Do not restore that earlier archive after you have edited Caddy, or you will put the old file back. Use the archive from the backup you took immediately before the edit.

## 3. One dedicated server

A different machine. `pkg/install.sh` asks for Ubuntu 22.04 or 24.04. Pick one path. Do not run both on the same new server.

### 3a. SSH and `pkg/install.sh`

`pkg/deploy/` holds `Caddyfile.tmpl`, `agentctl`, the `assistant-*.service` units, and `cloud-init.yaml`. `pkg/install.sh` is the installer that uses those files.

```bash
bash docs/deploy/apply-tenant-host.sh dedicated --domain example.invalid --print-only
sudo bash docs/deploy/apply-tenant-host.sh dedicated --domain example.invalid
```

The installer command is:

```bash
sudo bash pkg/install.sh --yes --domain example.invalid
```

Flags that exist on `pkg/install.sh` and that the wrapper does not accept: `--setup-token` and `-y`. `--setup-token` is a secret. Do not put it on a command line you save. The wrapper always passes `--yes`, which skips the question `Install the assistant into /opt/agent ?`. Before it calls `install.sh`, the wrapper refuses an OS other than 22.04 or 24.04. `--print-only` does not perform that OS check. Passing `--yes` straight to `install.sh` does not refuse a different OS; it only skips that exit. Use the wrapper.

`--domain` is required here so the wrapper does not use the path in `pkg/install.sh` that looks up a public address when `--domain` is omitted. This change did not execute that path.

`install.sh` replaces `/etc/caddy/Caddyfile` from `pkg/deploy/Caddyfile.tmpl` (`127.0.0.1:8790` in the template). The script header says a re-run keeps `.env`, `data/`, WhatsApp auth, and Google tokens. It also enables ufw for ports 22, 80, and 443, and it installs Node from the NodeSource setup script and Caddy from the Cloudsmith list when those commands are missing. This change did not run any of that. The wrapper backs up `/etc/caddy/Caddyfile` before each run. Re-running still rewrites the Caddyfile from the template.

File rollback:

```bash
sudo systemctl stop assistant-bridge assistant-setup assistant-bot
sudo bash docs/deploy/rollback.sh /var/backups/assistant-deploy/ARCHIVE.tar.gz
sudo systemctl daemon-reload
```

This does not stop `caddy` and does not revert ufw. After the Caddyfile is restored, validate and reload the Caddy that `install.sh` would have installed, using the reload that matches that install. This change did not install Caddy and did not run these `systemctl` commands.

The setup link is written to `/root/SETUP_LINK.txt`. That file is a secret. `pkg/deploy/agentctl` accepts `link`, `new-link`, `status`, `logs`, `restart`, and `update`. This change did not run `agentctl`.

### 3b. Cloud-init paste

`pkg/deploy/cloud-init.yaml` is the baseline file. No script submits it to Hetzner.

1. Copy the file outside the repository.
2. Replace `__PKG_URL__` with the package URL. The placeholder is `https://example.invalid/agent.tar.gz`. Keep the real URL on the server copy. Do not embed an account name.
3. Replace `__SETUP_TOKEN__` with a secret you generate on the server. Do not save the value in git, in a pull request, or in chat:

```bash
head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32
```

That pipeline is the `rnd` helper in `pkg/install.sh`. This change did not run it.

4. In Hetzner Cloud, create the server with Ubuntu 24.04 and paste the edited file into Cloud config. That is a click, not a script.

The yaml runs `install.sh --yes --setup-token` from the unpacked archive. There is still no packager in this repository for that archive.

### Move one assistant off the shared host

```bash
sudo /usr/local/sbin/agentsctl stop friend-one
sudo /usr/local/sbin/agentsctl export friend-one /var/backups/assistant-deploy/friend-one.tar.gz
```

The export contains secrets. Do not commit it. `export` tells you to stop the bot on the old host before starting it on the new one. This change did not export a bot.

On the new server:

```bash
sudo bash docs/deploy/apply-tenant-host.sh dedicated --domain example.invalid --restore /var/backups/assistant-deploy/friend-one.tar.gz
```

`--restore` is an existing `pkg/install.sh` flag. The wrapper passes it through. `install.sh` refuses the restore when `/opt/agent/data/memory.db` already exists. This change did not run a restore.

## Executed in this change

Executed on a checkout, not on a live server:

- `bash docs/deploy/self-check.sh` (backup and rollback against a temporary directory, preflight refusals, `--print-only` plans, shellcheck)
- `python3 scripts/secret_scan.py` and gitleaks, the same pair the secrets workflow runs

Not executed:

- `pkg/host/install-host.sh`
- `central/deploy.sh`
- `pkg/install.sh`
- `pkg/deploy/cloud-init.yaml`
- Caddy validate or reload
- `docker ps` or `systemctl` on the live server
- `agentsctl` and `agentctl`
- the Hetzner console, including Transfer to Project
- buying a 019 line
- creating an OpenAI key or setting the 20 USD cap
- the health request inside `deploy.sh`
