## Summary

<!-- What does this change and why? Link the issue: Closes #123 -->

## Changes

-

## Verification

<!-- How was this tested? Paste commands and results. -->

- [ ] `uv run pytest` passes
- [ ] `uv run ruff check . && uv run ruff format --check .` passes
- [ ] `uv run mypy` passes

## Security checklist

- [ ] No secrets, keys, tokens or real personal data (resumes) in the diff
- [ ] Nothing new logs request/response bodies or headers
- [ ] New dependencies are justified and locked in `uv.lock`
- [ ] Changes touching `llm-gateway`, Secrets, RBAC or NetworkPolicy are described above
