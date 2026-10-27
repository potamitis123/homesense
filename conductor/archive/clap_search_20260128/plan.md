# Plan: Embed CLAP-based search in the Dash Dashboard

This plan outlines the steps to implement a CLAP-based natural language search in the audio dashboard.

## Phase 1: Backend Implementation

This phase focuses on adding the necessary logic to perform vector similarity searches against the stored CLAP embeddings.

- [x] **Task: Add cosine similarity search logic to `ast_dash/sql_to_pandas.py`.** [5f9a2e3]
    - [x] Implement `get_clap_search_results(query_text, threshold, start_date, end_date)` method.
- [ ] **Task: Write unit tests for the CLAP search logic.**
    - [ ] Create `tests/dash_tests/test_clap_search.py`.
    - [ ] Mock `CLAPService` and `sqlite3` to verify correctly filtered and sorted results.
- [ ] Task: Conductor - User Manual Verification 'Phase 1: Backend Implementation' (Protocol in workflow.md)

## Phase 2: UI Integration

This phase integrates the search functionality into the Dash frontend.

- [x] **Task: Update `ast_dash/pages/audio_details.py` layout.** [a1b2c3d]
    - [x] Add `dbc.Input` (id='clap-search-input'), `dbc.Button` (id='clap-search-button'), and `dcc.Slider` (id='clap-threshold-slider') to the layout.
- [x] **Task: Implement search callback in `audio_details.py`.** [e4f5g6h]
    - [x] Add a callback triggered by the search button that updates the `rowData` of `audio_grid_table`.
    - [x] Ensure the "Refresh" button also works correctly (clearing search or integrating with it).
- [x] **Task: Add a 'Similarity' column to the Ag-Grid.** [i7j8k9l]
    - [x] Update `SQL_to_Pandas.get_columns` to conditionally include a similarity score column when a search is active.
- [ ] Task: Conductor - User Manual Verification 'Phase 2: UI Integration' (Protocol in workflow.md)

## Phase 3: Finalization

- [ ] **Task: Ensure CLAP model offloading works correctly after UI searches.**
- [ ] **Task: Final code review and cleanup.**
- [ ] Task: Conductor - User Manual Verification 'Phase 3: Finalization' (Protocol in workflow.md)
