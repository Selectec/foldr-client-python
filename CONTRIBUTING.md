# Contributing

Bug reports and focused pull requests are welcome. For a larger change, an issue
describing the problem and proposed behavior helps contributors coordinate.
Please follow the [community conduct guidance](CODE_OF_CONDUCT.md).

For bugs, include the package and Python versions, what you expected, what
happened, and a small reproduction using synthetic data. Use an HTTPX mock
transport where possible so others can reproduce the behavior without a Foldr
site or credentials. Describe feature requests in terms of the caller's problem
and the behavior that would help.

Keep tokens, client secrets, callback codes, signed URLs, tenant addresses,
customer information and production response bodies out of issues, pull
requests and commits. Error attributes and response headers can contain
sensitive data even when the exception message does not. Report suspected
vulnerabilities through the [private reporting guidance](SECURITY.md).

## Making a change

See [development instructions](docs/development.md) for setup and package checks.
Keep changes focused, document public behavior changes, and add or update tests
for behavior that needs coverage. Tests should use synthetic responses and run
without network access or live credentials. Maintain request expectations
independently of the implementation; offline tests do not verify a live server.

For code changes, run:

```console
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

For packaging changes, also run `python -m build` and the isolated installation
checks in the development instructions. In your pull request, explain the
problem, the resulting behavior and the checks you ran. For documentation-only
changes, checking the text and links is sufficient.

## Contribution terms

Contributions are distributed under this project's [MIT License](LICENSE).
By submitting a contribution, you agree to those terms and confirm that you have
the right to provide it. Include appropriate attribution and license notices
for any third-party material. Do not add vendor manuals, downloaded API schemas
or other material without permission to redistribute it. Prefer small,
locally authored synthetic test data.
