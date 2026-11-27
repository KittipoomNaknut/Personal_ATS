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

> **Status:** Phase 0, repository foundation. See the [roadmap](docs/ARCHITECTURE.md#9-roadmap).

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
