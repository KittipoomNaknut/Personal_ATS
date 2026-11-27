# Personal ATS

[![CI](https://github.com/KittipoomNaknut/Personal_ATS/actions/workflows/ci.yml/badge.svg)](https://github.com/KittipoomNaknut/Personal_ATS/actions/workflows/ci.yml)

A personal **ATS resume checker** for Thai and English resumes. It scores a
resume against a job description the way applicant tracking systems and
recruiters do, and explains every point with evidence from the resume.

- **Parseability audit**: can an ATS read the file at all? Columns, tables,
  headers, hidden text, broken Thai text extraction.
- **Keyword match**: the exact-term filtering real ATSs use, with a bilingual
  skill taxonomy.
- **Semantic fit**: each JD requirement is judged by Claude with cited,
  verified evidence.

Runs locally on Kubernetes (k3d). Built security-first: the Claude API key is
isolated in a single gateway, personal data is redacted before leaving the
machine, and all other egress is denied.

> **Status:** Phase 2, AI analysis through a local gateway (first live run pending). See the [roadmap](docs/ARCHITECTURE.md#9-roadmap).

## Usage

Everything below runs on your machine; nothing is sent anywhere.

```bash
uv sync
uv run ats check resume.pdf --jd job.txt            # Thai report (default)
uv run ats check resume.pdf --jd job.txt --lang en  # English report
uv run ats check resume.docx --json                 # machine-readable
uv run ats check resume.pdf --show-redacted         # text as it would be sent to AI
```

The job description can be plain text, PDF or DOCX. The report covers:

- **ATS readability:** scanned pages, garbled Thai, hidden text, columns, tables,
  headers/footers, text boxes, missing sections, Buddhist-era dates.
- **Keywords:** required and preferred skills from the job description, what is
  missing, and where the resume uses a synonym instead of the job description's
  exact term. Hidden text earns no credit.
- **Privacy:** how many personal details would be removed before any AI call.
  Values are never printed. Use `--name` to make sure your name is caught.

Exit codes: `0` success, `2` usage error, `3` file unreadable, `4` file cannot be parsed.

### Starting and stopping the AI gateway

The Claude-powered parts run through a local gateway that holds the API key.
Store the key once in your OS keyring; it is typed at a prompt, never on the
command line:

```bash
secret-tool store --label='Anthropic personal-ats' service anthropic account personal-ats
```

```bash
uv run atsctl start     # key -> tmpfs -> gateway memory; the key file is deleted at once
uv run atsctl status    # running?, uptime, today's spend against the $2.00 daily budget
uv run atsctl logs -n 50
uv run atsctl stop      # stops the gateway; no key is left anywhere
```

### AI analysis and billing clients

With the gateway running, add `--ai` to have Claude judge every requirement of
the job description. The two cheap steps (reading the job description and the
resume) run first; then the cost of judging is estimated and you confirm:

```bash
uv run ats check resume.pdf --jd job.txt --ai --client "น้องเอ"   # asks before judging
uv run ats check resume.pdf --jd job.txt --ai --yes               # no question (scripts)
uv run ats costs --month 2026-12 --fx 36.20 --markup 50           # per client, USD and THB
uv run ats costs --client "น้องเอ" --month 2026-12 --fx 36.20 --invoice   # saves a snapshot
uv run ats forget --client "น้องเอ"                               # name and fingerprints only
```

Only redacted text is sent, and reports show evidence and rationales with
placeholders such as `[NAME_1]`, so a report never contains personal values.
Years of experience are computed in code; for a skill they count only the jobs
whose lines name that skill.

The gateway listens only on a private unix socket (mode 0600) under
`$XDG_RUNTIME_DIR/personal-ats/`, which must be tmpfs. The cost ledger lives in
`~/.local/share/personal-ats/ledger.db` (mode 0600).

## Documentation

- [Architecture](docs/ARCHITECTURE.md) (Thai)
- [Contributing](CONTRIBUTING.md): branching, commits, PRs
- [Security policy](SECURITY.md)

## Development

Requires [uv](https://docs.astral.sh/uv/) and [pre-commit](https://pre-commit.com/).

```bash
uv sync
pre-commit install --install-hooks
uv run pytest
```

## License

[MIT](LICENSE)
