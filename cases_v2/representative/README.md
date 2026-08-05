# Representative CaseSpec V2 migrations

These examples migrate four positive CaseSpec V1 cases without replacing the originals:

- `falling_block_on_floor.json`: gravity and support contact
- `low_speed_single_contact.json`: contact causality
- `five_domino_chain.json`: ordered contact propagation
- `upward_throw_arc.json`: ballistic gravity motion

Each file keeps the original logical `case_id`, capability, object IDs, initial physical state, and expected behavior. The examples explicitly allow analytic proxies and disable external and generated acquisition, matching the original cases' built-in primitive execution policy. They pass through the normal V2-to-V1 runtime projection and exactly one Asset Resolve.

Load these files through the schema-dispatching `load_case_spec_document` API. The legacy `load_case_spec` API intentionally remains V1-only.
