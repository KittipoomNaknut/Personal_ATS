# Security Policy

This project handles an Anthropic API key and personal data (resumes). Security
is the top design priority — see [docs/ARCHITECTURE.md §2](docs/ARCHITECTURE.md).

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/KittipoomNaknut/Personal_ATS/security/advisories/new).
You can expect an acknowledgement within 7 days.

## Rules for contributors

- Never commit secrets: API keys, tokens, `.env` files, kubeconfigs, certificates.
  `gitleaks` runs in pre-commit and in CI on the full history.
- Never commit real resumes or job applications. Use synthetic fixtures.
- The Anthropic API key lives only in the OS keyring and in an encrypted
  Kubernetes Secret mounted into `llm-gateway`. No other component may read it.
- Logs must go through `ats_core.redaction.SecretRedactingFilter`.

## If a key is leaked

1. Revoke the key immediately in the Anthropic Console.
2. Review usage in the Console for unexpected activity.
3. Create a new key and rotate it (`scripts/rotate-key.sh`, added in phase 2).
4. If the key reached git history, treat the history as compromised: rotating
   the key is mandatory; rewriting history alone is not sufficient.
