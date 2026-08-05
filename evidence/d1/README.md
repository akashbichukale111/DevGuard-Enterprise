# Write-path proof

Every catalog write DevGuard performs, proven individually against a live
DataHub before anything was built on top of it.

**Result: all five write-back artifact types work. Four discrepancies against
the documented API were found, and in every case the live server was treated as
authoritative.**

**Environment:** DataHub Core `v1.6.0` (read from the running instance) · MCP
server `mcp-server-datahub@0.6.0`

## Captured artifacts

| File | Write path |
|---|---|
| `01-raiseIncident.json` | Raise an incident against a dataset |
| `02-incidents-active.json` | Read it back as active |
| `03-updateIncidentStatus.json` | Resolve it |
| `04-incidents-after-resolve.json` | Read back the resolved state |
| `05-add_tags-column.json` | Column-level tag on a `schemaField` |
| `06-update_description-column.json` | Column-level description |
| `07-save_document.json` | Post-mortem runbook as a `document` |
| `08-structured-property-definitions.json` | Property definitions |
| `09-add_structured_properties.json` | Values written against a dataset |

`incident_urn.txt` and `target_urn.txt` hold the URNs these responses refer to,
so each payload can be traced back to the entity it touched.

## Why this was done first

Building a nine-agent loop on an unverified write path risks discovering at the
end that the catalog will not accept what the system produces. Proving each
write in isolation, against the real server, meant every later failure could be
attributed to the loop rather than to the API.

The discrepancies found here are why the Scribe constructs payloads the way it
does — see `backend/v2/agents/scribe.py`, where each live-server behaviour is
documented at the line it constrains.
