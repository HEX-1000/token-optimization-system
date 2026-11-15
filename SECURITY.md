# Security

## Supported versions

The latest release only. Installed copies do not update themselves: a fix comes as a new version, which you install with the Update steps in `README.md`.

## Reporting a vulnerability

Report it privately through this repository's Security tab: "Report a vulnerability". Please do not open a public issue or discussion for a vulnerability. Do not attach your documents or your index file; describe them instead.

## In scope

- The server opening one of your documents, any other file except its own index, or a network connection while it answers Claude.
- A tool call that changes or deletes indexed data. The tools only read the index, apart from writing a usage record.
- The indexer reading files outside the folders you pass to `scripts\index.ps1`.
- `scripts\install.ps1` installing a file in `src\` that does not match `SHA256SUMS`, or a version of a package in `requirements.txt` other than the pinned one.
- `scripts\connect-claude-desktop.ps1` changing entries of the Claude Desktop config other than its own `token-optimization-system` entry, or writing the config without making a backup first.
- Anything that sends indexed text, your questions or the index file off the PC, apart from the sections the server returns to Claude.

## Out of scope

- Instructions inside your own indexed files that Claude then follows (prompt injection). The server returns stored text as it is, so index only files you trust.
- Returned sections becoming part of your conversation with Claude. That is what the server does (see Privacy in `README.md`).
- Access to the index file (`data\optimizer.db` in the install folder) by anyone who can use your Windows account. It is a plain SQLite file without encryption.
- Versions of packages that `requirements.txt` does not name. It pins the five packages it lists; pip chooses the versions of their dependencies at install time.
- Vulnerabilities in Claude Desktop, Python or the pinned packages themselves. Report those to their projects.

There is no promise about response time.
