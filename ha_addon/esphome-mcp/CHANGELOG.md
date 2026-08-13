# Changelog

## 2026-08-13

- Initial Home Assistant add-on release: reuses the existing multi-arch image,
  adds an ingress status/connect-instructions page, and secures the MCP endpoint
  behind a persisted, high-entropy secret path instead of a fixed `/mcp`.
