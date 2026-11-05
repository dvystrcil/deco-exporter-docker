# Fixtures

Recorded Deco API responses, copied verbatim (the JSON only) from the
`tplinkrouterc6u` 5.38.0 sdist's `test/test_client_deco.py`
(sdist sha256 `63dcd0bda7e2a50bee7bf092c411d8e3bcf6fdcb314dfa3840176372af52f8e9`):

| File | Endpoint | Source test |
|---|---|---|
| `client_list.json` | `admin/client?form=client_list` | `test_get_status` |
| `performance.json` | `admin/network?form=performance` | `test_get_status` |
| `device_list.json` | `admin/device?form=device_list` | `test_get_mesh_nodes` (a Deco X50, firmware 1.8.0) |

They hold no credentials: the library records responses after decryption, and
none of these endpoints returns a password. Names in `client_list.json` are
base64, as the real router sends them.

They are **not** from our BE85. The live router was deliberately not polled to
build fixtures (homelab#1931 handoff); the BE85's own response shape is
checked by the live receipt script once the exporter is deployed.
