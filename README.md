# Personal assistants

Hebrew-first WhatsApp assistants for a small circle of friends.

Each friend gets an assistant with its own memory and its own WhatsApp connection. The assistant can use that friend's Google accounts and answers in everyday Hebrew. Friends join through the start site. An admin service tracks health and onboarding for assistants we host.

## Layout

- `pkg/` — one assistant: the bot, the WhatsApp bridge, single-server install, and the multi-assistant host tools
- `central/` — the onboarding site
- `platform/` — the admin service for hosted assistants

## Continuous integration

GitHub Actions runs [gitleaks](https://github.com/gitleaks/gitleaks) and `scripts/secret_scan.py`. The build fails if either one reports a hit. The custom scan checks for keys, tokens, private keys, phone numbers, email addresses, server addresses, and a fixed denylist. Denylist terms are stored encoded in `scripts/denylist.b64` and decoded only in memory while the scan runs. The job also builds a temporary fixture and requires both scanners to reject it. The repository tree itself must stay clean.

Allowed addresses are loopback `127.0.0.1`, the unspecified address `0.0.0.0`, and the documentation ranges `192.0.2.0/24`, `198.51.100.0/24`, and `203.0.113.0/24`. WhatsApp JIDs on `g.us` and `s.whatsapp.net` are not treated as email addresses.
