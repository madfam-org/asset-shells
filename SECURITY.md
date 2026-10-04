# Security

asset-shells stores manufacturing records (instance shells and product passports) on behalf of other
MADFAM organisations. Please report vulnerabilities privately to **security@madfam.io** — not in a public
issue. We acknowledge reports within three business days.

Scope reminders for reviewers:

- Type shells are public by design. Instance shells and passport events must never be readable or
  writable across organisations; a finding that shows one tenant reaching another's rows — through the
  API or the database role — is the highest severity we recognise.
- Tokens are Janua RS256 service tokens verified against Janua's JWKS (audience `asset-shells-api`).
  Any path that accepts another algorithm, a missing `kid`, a wrong issuer or audience, or that treats an
  invalid token as anonymous, is a vulnerability.
- Passport events are append-only and type shells immutable; a path that edits or deletes either is a
  vulnerability.
- Logs must not contain database error detail or request bodies.
