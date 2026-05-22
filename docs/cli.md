# CLI Reference

All commands run via Docker or the `llmctl` entrypoint:
```bash
docker compose run --rm bridge python -m cli.llm_ctl <command>
# or
llmctl <command>
```

## User Management

```bash
llmctl users add <email>                              # create user (prompts for password)
llmctl users set-password <email>                     # reset password
llmctl users budget <email> --usd 50                  # set monthly spend cap
llmctl users credit-add <email> --usd 20              # add prepaid credit
llmctl users tiers <email>                            # show allowed tiers
llmctl users tiers <email> --set simple               # lock to one tier
llmctl users tiers <email> --set simple,architecture  # allow two tiers
llmctl users tiers <email> --set all                  # remove restriction
llmctl users deactivate <email>                       # soft-delete user
llmctl users list                                     # list all users
```

## API Keys

```bash
llmctl users keys-add <email> [--label "open-webui"]  # create key (shown ONCE)
llmctl users keys-list <email>                        # list active keys
llmctl users keys-revoke <key_id>                     # revoke one key by ID
llmctl users reset-key <email> [--label "name"]       # revoke all + issue fresh key
```

### Adding a User to Open WebUI

```bash
llmctl users add you@email.com
llmctl users keys-add you@email.com --label "open-webui"
# Copy the sk-llm-... key — displayed once only
```

In Open WebUI (http://localhost:3000):
1. Avatar → **Admin Panel** → **Settings** → **Connections**
2. Under **OpenAI API**, find `http://bridge:8000/v1`
3. Paste the `sk-llm-...` key → checkmark → **Save**

## Pods & Billing

```bash
llmctl pods ls [--status ready]                       # list pods
llmctl pods kill <pod_id>                             # terminate pod
llmctl start --tier architecture                      # prewarm a pod
llmctl bills run --month 2026-05                      # generate invoices
llmctl bills show <email> --month 2026-05             # per-user invoice + breakdown
```

## Observability

```bash
llmctl models [--user-type personal]                  # tier table with effective $/hr
llmctl status [--tier architecture]                   # active pods + running cost
llmctl budget [--email u@example.com]                 # spend vs cap progress bars
llmctl costs [--month 2026-05] [--email ...]          # per-tier cost breakdown
llmctl gain [--month 2026-05]                         # savings vs GPT-4o equivalent
```
