# Threat model

Security contract for the hosted pilot and for self-host. This document states the properties the product must hold. It does not claim the imported baseline already holds them. Product code stays in the area that owns it. This pull request adds the contract, the CI job that runs area tests when they exist, and tests that lock the scan workflow and that job.

CI runs `pkg/agent/tests`, `central/tests`, and `platform/tests` when those directories exist, and `pkg/host/check-selfhost.sh` when that file exists. A missing path logs `skip: <path> does not exist yet` and does not fail the job. A suite that runs and fails does fail the job. Until those paths land, the behaviors below are not verified.

Tests never send WhatsApp. Area tests must mock the bridge.

## Assets

- Friend memory, groups, settings, WhatsApp session, and Google tokens. These stay on the friend's server.
- OpenAI keys. During month 1 the key is ours and capped. After release the only key is the friend's, or there is no key.
- Manager ledger for hosted friends: status history, costs, requests, invite codes.
- Operator SSH keys and any support grant.
- The public git tree. It must not contain secrets, phone numbers, email addresses, private keys, server addresses, or denylist terms in plaintext.

## Month-1 access

The pilot is five friends. For each friend we pre-fund a small server, a temporary line, and an OpenAI key on our account with a 20 USD hard cap. The manager stores the cap and the amount spent. Real line numbers and real keys are created by a person, not by this repository.

During that month our side has technical access:

- SSH to the hosted server.
- Our OpenAI key, removed at release.
- Manager reports: status, costs, requests, and heartbeats.

That access is the hosted pilot, not support access. Support access is a separate grant the friend opens later.

The manager must not read message bodies, mail, memories, or key material. It must not store a private family-assistant path and must not read `/root`. The imported baseline still defaults the manager at a root env path and a root database path (`platform/assistant_manager.py`). That is an import leftover. Operations removes it. This document requires the removal. It does not claim the baseline already complies.

Month-1 access ends at release by construction. A flag that can be turned back on is not enough.

## Release

Default release is 30 days after the tenant starts. An admin can set a later `release_at` for one customer. Release also runs immediately once the friend has saved their own server, their own line, and their own OpenAI key.

On release, all of the following hold:

1. Zero outbound manager calls. Code that would POST or GET the manager is deleted or hard-disabled. Tests must show no outbound manager call is possible.
2. Our OpenAI key and every token of ours are gone.
3. Our SSH keys are removed.
4. Passwords and other secrets rotate to values only the friend can read.
5. The bot process stays up.
6. Reporting never resumes. Status, costs, requests, and heartbeats are not sent again.
7. The friend is removed from our list, and the manager data listed below is deleted.
8. If the friend has no OpenAI key: memory, groups, and settings stay. Replies are one fixed Hebrew sentence plus the link to the management page. Saving a key resumes full replies. The bot does not invent an answer while the key is missing.
9. Reminders: at most four. Roughly 7, 3, and 1 days before release, plus one on the day of release. No extra nudges. Hebrew, short, and no time-of-day estimate beyond the date the friend already has.

Server ownership moves by Hetzner project transfer, explained in the product: the friend invites the current project owner as a member, then that owner uses Transfer to Project. We do not call a Hetzner API with a friend's token. The onboarding flow has no step that asks the friend to paste a Hetzner API token.

The bot-runtime tests that lock "no outbound manager call", "our keys are gone", and "the bot still runs, including the no-key fallback" belong in `pkg/agent/tests`.

## Support-access expiry

Support access exists only after the friend opens it on the management page («פתח גישה לתמיכה»), picks a duration, sees that it is open, and can close it.

- Durations are 1, 4, 24, or 72 hours.
- The server enforces the end time. A grant that has expired does not authorize a login.
- Closing early ends the grant immediately.
- Every support login is appended to a log the friend can see.
- «מי יכול להיכנס לשרת שלי?» lists authorized SSH keys, active support grants, and recent logins. The friend can delete a key.

The tests that lock expiry, close, and the login log belong in `central/tests` (the management page lives with onboarding).

## Manager data deletion

On release the manager deletes that friend's data: status history, costs, requests, and invite codes. The friend is gone from the operator list. After deletion, a manager query must not return that friend's activity. Tests belong in `platform/tests`.

Deletion is part of release, not a separate favor. A hosted friend who reaches release is not retained "for the record."

## Self-host

A friend can run their own server from day one. They create the server in their own account and run one public install line. The onboarding page builds that line at runtime from the environment variable `PUBLIC_INSTALL_URL`. The only example value committed for that variable is `https://example.invalid/install.sh`. The deploy bundle sets the real raw URL on the server. No token or key of ours is in the flow. Committed files must not contain the GitHub account login. `install.sh` lives in this repository.

Self-host mode disables the manager, status, and team-request hooks. The install does not report. The friend is not listed on our side.

`pkg/host/check-selfhost.sh` is the check for that install (expected to cover shellcheck and a container dry-run). This pull request does not add that script. CI runs it when a later pull request adds it, and skips it until then.

## What CI enforces on this branch

`.github/workflows/secrets.yml` is unchanged by this pull request. It still:

- installs pinned gitleaks and checks the archive checksum
- runs `scripts/secret_scan.py` on the tree
- runs `gitleaks detect` on git history and `gitleaks detect --no-git` on the tree, redacted, exit code 1 on a hit
- runs `scripts/secret_scan_fixture.py`, which must reject a generated sample

Denylist terms stay encoded in `scripts/denylist.b64`. The workflow files, this document, and `tests/` must not contain those terms in plaintext. Tests check that.

`.github/workflows/tests.yml` runs `.github/scripts/run-area-tests.sh`. That script is the skip-if-missing behavior described above. It also runs `tests/` when that directory exists, which is the suite in this pull request.

## What is not verified on this branch

Release, support-access expiry, manager deletion, and the self-host install are not implemented here and are not claimed to work. The imported tree still has month-1 manager behavior, including a root env path and a root database path, and onboarding still contains a Hetzner-token step. Those are gaps for the owning areas. This pull request does not edit their files.

## Assumptions used

- Self-host install URL is built at runtime from `PUBLIC_INSTALL_URL`. Example value: `https://example.invalid/install.sh`.
- Hetzner ownership moves by project transfer, documented in the product. No friend token is sent to a Hetzner API.
- Release default is 30 days. Admin may set a later `release_at`. Release also runs when the friend has their own server, line, and key.
- After release, manager HTTP calls are removed or hard-disabled, not gated by a reversible flag.
- No friend key at release: one fixed Hebrew sentence plus the management link. Memory, groups, and settings stay.
- At most four reminders, at roughly 7, 3, and 1 days before, plus the day of release.
- Support durations are 1, 4, 24, and 72 hours. Expiry is server-enforced. Logins are logged for the friend.
- The pilot is five friends. Our key cap is 20 USD, stored with the amount spent.
- The manager stores no family-assistant path and does not read `/root`.
- Tests mock WhatsApp. Bridge pairing and auth stay as they are, except a line swap that keeps the existing session mechanism.
