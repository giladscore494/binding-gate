## SEC dataset (layer 2) generated from EDGAR

- content_sha256: `1310f020dd2c47ad1a0d7950cc13564bd3bda16bc7dd048e8f32bf656ec21e2c` - NOT the pin in the configs (UNPINNED); pin it in a pull request (docs/RUNNING.md step 8)
- items: 891 from 297 companies; by split {'dev': 378, 'test': 513}; by stratum {'absent/peer': 297, 'present/none': 297, 'present/peer': 297}; excluded companies or items: 3
- EDGAR requests: 2333 over the network, 0 from the cache
- next step: pin this content_sha256 as data_sha256 in every sec_financials config and commit data/raw/sec-financials/selection.json as the selection_file of the builder config
