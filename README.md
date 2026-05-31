# Belgrade Rental Notifier

A personal scraper that turns the noise of four Belgrade rental portals into one
calm Telegram feed. It scrapes listings, filters them against fixed rules and a
language model, ranks the survivors by how close they are on foot to the places
that matter, and delivers a once-a-day digest — with instant pushes when a new
perfect match appears.

It runs on a schedule as a GitHub Actions workflow, keeps its memory in a single
SQLite file on Cloudflare R2, and costs about **$2/month** to operate.

```text
scrape 4 portals → drop hidden → structural filter → LLM extract → LLM filter
   → walking-distance filter → dedup → rank → daily digest / instant push
```

## Documentation

Read these in order if you're new to the codebase.

| Doc | What it covers |
| --- | --- |
| [docs/overview.md](docs/overview.md) | What the project does, where the listings come from, and the daily rhythm of digests and instant pushes. |
| [docs/architecture.md](docs/architecture.md) | The two moving parts — the GitHub Actions pipeline and the always-on VM webhook — and how state lives in R2. |
| [docs/filtering-and-ranking.md](docs/filtering-and-ranking.md) | How a raw listing becomes a ranked card: structural rules, LLM extraction, walking distance, dedup, scoring. |
| [docs/telegram.md](docs/telegram.md) | What a digest card looks like, the 🙈 Hide and ⭐ Favorite buttons, and how instant push differs from the daily digest. |
| [docs/configuration.md](docs/configuration.md) | Every knob in `config.yaml` and every secret the run reads from the environment. |
| [docs/operations.md](docs/operations.md) | Run book: trigger a run, watch it, check API spend, reset state. |

For the VM webhook deploy steps, see [deploy/README.md](deploy/README.md).

## Other references

- [STATUS.md](STATUS.md) — the live state tracker: what works, what's pending,
  and why decisions diverge from the spec. Update it as the project moves.
- [belgrade-rental-notifier-SPEC.md](belgrade-rental-notifier-SPEC.md) — the
  original execution spec. STATUS.md records every deviation from it.
- [CLAUDE.md](CLAUDE.md) — orientation for Claude Code sessions in this repo.

## Quick start (local)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in the secrets
.venv/bin/python -m src.main  # one full run; halooglasi needs Docker, so it shows ⚠️ locally
.venv/bin/python -m pytest tests/ -q
```

See [docs/operations.md](docs/operations.md) for triggering a real run on CI.
