# Source transfer

The initial standalone import comes from the MiniPC checkout at revision `f772eb9333a4933b3c5e0678d2c759dc4e1bdf4b`. The service working-tree diff at import had SHA-256 `2289f4c85389dcdd06099dca2fd859e99122df8cd9294b92805a102360ad902b`. It includes the live Docker network inventory changes in the native panel.

| Source in MiniPC | Standalone destination |
|---|---|
| docs/services/outbound-proxy/README.md | README.md |
| docs/services/outbound-proxy/docs/ | docs/ |
| docs/services/outbound-proxy/scripts/ | scripts/ |
| docs/services/outbound-proxy/tests/ | tests/ |
| scripts/outbound_proxy/ | scripts/outbound_proxy/ |
| scripts/outbound-proxy | scripts/outbound-proxy |
| scripts/minipc_proxy_apply.py | scripts/minipc_proxy_apply.py |

The import updates repository-relative paths in installers, templates, tests and operator guides. Private catalogs, runtime generations and browser credentials stay on their respective hosts. The native migration implementation remains available for an existing MiniPC installation. The original repository records site-specific deployment history. Reusable product documentation and new delivery evidence belong to the outbound-proxy iwiki domain.
