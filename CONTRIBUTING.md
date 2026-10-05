# Contributing

Contributions are welcome through pull requests. Keep changes small and keep
each library module usable on its own.

Before opening a PR:

- Install with `pip install -e ".[app,test]"` and run `python -m pytest` from the
  repo root. The suite runs offline; tests must not require a Moveworks
  instance.
- Run the linters CI runs: `pip install ruff==0.16.10 pyright==1.1.414`, then
  `ruff check .` and `pyright`.
- Put helpers that change how message content is displayed in
  `moveworks_capi/rendering.py`.
- Add a line under "Unreleased" in `CHANGELOG.md` for any change a user of the
  library would notice.
- Keep the reference app plain. Visual polish belongs in a fork or in the
  [demo app](https://github.com/moveworks/conversations-api-demo-app).
- Never commit credentials, `.env` files, or private keys.

For anything security-sensitive, see [SECURITY.md](SECURITY.md) instead of
opening a public PR. Participation in this project is covered by the
[Code of Conduct](CODE_OF_CONDUCT.md).
