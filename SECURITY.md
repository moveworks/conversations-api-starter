# Security Policy

## Reporting a vulnerability

If you find a security issue in this repo (a bug in the sample code that could compromise a customer integration, or a vulnerability in a dependency), report it privately.

Email: `security@moveworks.com`

Do not open a public GitHub issue for security reports.

## Scope

This repo contains reference implementations for the Moveworks Conversations API. Bugs in the sample code that could lead to a customer integration accepting forged webhooks, leaking user tokens, or otherwise compromising authentication are in scope.

Vulnerabilities in the Moveworks Conversations API itself, or in your production integration, are handled through your Moveworks account team and are out of scope for this repo.

## Disclaimer

This repository is reference code, provided as-is under its license. It is not a supported Moveworks product and carries no SLA. The Conversations API is in Controlled Availability and its behavior can change; verify against the [official documentation](https://docs.moveworks.com/api-reference/conversations-api) before using patterns from this repository in production.
