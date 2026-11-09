# Security policy

## Supported versions

speclens is in early development. Fixes go into the latest release and `main` only.

## Reporting a vulnerability

Please do not open a public issue. Report it privately through GitHub: on this repository's **Security** tab,
choose **Report a vulnerability**. You will get a reply there.

speclens reads OpenAPI files and answers questions about them. It makes no network requests and runs no code from
the specs it reads. Reports of particular interest: a spec file that makes speclens read files outside the folder
it was given, use unbounded memory or time, or crash in a way that hides problems from `validate`.
