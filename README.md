# paperless-ai-classify

Let a **local LLM** (via [Ollama](https://ollama.com)) fill in the document type, title and
correspondent of your [Paperless-ngx](https://docs.paperless-ngx.com) documents, overnight. Your
documents never leave your network.

It is deliberately conservative, because small models running on a home server do make things up:

- **Type**: only one of the document types you already have in Paperless (the model's answer
  is constrained by a JSON schema), or nothing.
- **Correspondent**: only one of your existing correspondents, and only if its name actually
  appears in the document text.
- **Title**: only replaced when the current one looks generated (`Scan_0012`, `IMG2026...`,
  `Fwd: ...`, a file name, all caps), and written in your language (`DOC_LANGUAGE`).
- Every changed document gets the tag **"AI to review"**, so you can check them in one saved
  view; every processed document gets **"AI seen"** and is not processed again. Paperless keeps
  the change history, so any change can be undone.
- The log contains document ids and changed fields only, never content or titles.

```
doc 912: type=Invoice, correspondent=Acme Energy, title (41s)
doc 911: type=Payslip, title (38s)
doc 907: no change (35s)
SUMMARY {"seen": 77, "type": 31, "title": 41, "correspondent": 9, "errors": 0, "minutes": 52}
```

## Requirements

- Paperless-ngx with an API token, and some document types (and correspondents) already defined.
- Ollama with an instruct model. `qwen3:4b` works well on a CPU-only mini PC (30-60 s per
  document); bigger models are better and slower.
- Docker, or Python 3.10+ with no extra packages (standard library only).

## Quick start

```bash
git clone https://github.com/aledv/paperless-ai-classify.git && cd paperless-ai-classify
cp .env.example .env    # PAPERLESS_URL, PAPERLESS_TOKEN, OLLAMA_URL, OLLAMA_MODEL, DOC_LANGUAGE
docker compose run --rm -e RUN_AT= paperless-ai-classify --dry-run --max 5   # try it, changes nothing
docker compose up -d    # then every night at RUN_AT
```

Without the built-in scheduler (`RUN_AT` empty), run it from cron instead:

```
15 2 * * * docker run --rm --env-file /path/.env ghcr.io/aledv/paperless-ai-classify:latest >> /var/log/paperless-ai.log 2>&1
```

Options: `--dry-run` (no changes), `--max N` (at most N documents), `--ids 12,15` (only these).

## Configuration (`.env`)

Names only, see `.env.example` for defaults.

| Variable | Purpose |
|---|---|
| `PAPERLESS_URL`, `PAPERLESS_TOKEN` | Paperless-ngx base URL and API token |
| `OLLAMA_URL`, `OLLAMA_MODEL`, `NUM_CTX` | Ollama server, model and context size |
| `DOC_LANGUAGE` | Language of your documents (titles are written in it) |
| `TAG_REVIEW`, `TAG_SEEN` | Tag names (created if missing) |
| `SET_TYPE`, `SET_TITLE`, `SET_CORRESPONDENT` | Which fields to fill in |
| `STOP_AT`, `MAX_DOCS`, `MAX_CHARS` | Time window and limits of one run; text sent per document |
| `RUN_AT` | Daily start time for the built-in scheduler; empty = run once |

Documents are processed newest first, only those without a document type and without the
`TAG_SEEN` tag, so a long backlog is worked through over a few nights.

## Development

```bash
python -m unittest discover -s tests -v          # tests (fake Paperless API and model)
ruff check . && ruff format --check .            # Python linter
hadolint --config .hadolint.yaml Dockerfile      # Dockerfile linter
```

CI runs the linters, then the tests, then publishes `ghcr.io/aledv/paperless-ai-classify`
(`linux/arm64`, `linux/amd64`).

## License

MIT, see [LICENSE](LICENSE).
