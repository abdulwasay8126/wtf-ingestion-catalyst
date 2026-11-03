# WTF Ingestion and Catalyst Dashboard

This private repository combines two separately packaged components:

- \`platform/ingestion\` — public-market and earnings ingestion, validation, persistence, and read APIs.
- \`platform/catalyst-doot\` — the earnings dashboard, consuming the sibling ingestion package.

Both packages require Python 3.12. Start with each package's README for setup and local fixture workflows. The Catalyst package resolves \`wtf-ingestion\` from the sibling \`platform/ingestion\` directory.

The package defaults are designed for fixture review. Live sources require their approved runtime configuration and source-specific permissions. No credentials, retained raw captures, local outputs, backups, or project wiki have been added to this repository.
