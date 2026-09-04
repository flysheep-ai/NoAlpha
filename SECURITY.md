# Security Policy

## Supported versions

NoAlpha is currently pre-1.0. Security fixes are applied to the latest commit on
the `main` branch.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository when it
is available. If that option is not visible, contact the maintainer through the
[flysheep-ai GitHub profile](https://github.com/flysheep-ai) to request a private
reporting channel. Do not publish credentials, exploit details, or sensitive
market/broker data in a public issue.

Useful reports include reproduction steps, affected version or commit, expected
impact, and a minimal proof of concept. Please allow a reasonable period for
triage and remediation before public disclosure.

## Scope

Security-sensitive areas include credential storage, authenticated API routes,
broker policy gates, order state transitions, dependency supply chain, and any
path that could turn simulated behavior into a live order.

NoAlpha is research and educational software. The presence of a broker adapter
does not mean that a live-money path has been security-reviewed or validated.
