# Plan: Integrated Audio Ontology, Local Audio Processing, and Spectrogram Display

This plan outlines the phases and tasks to integrate audio ontology, process local audio example files, generate and display spectrograms, and enhance the dashboard. It adheres to the Test-Driven Development (TDD) workflow and includes phase completion verification.

## Phase 1: Ontology Integration and Display

This phase focuses on populating the 'Audio classes' tab with the ontology data and preparing for its use in filtering.

-   [ ] **Task: Refactor `ast_sql/ontology.py` and integrate `ontology.json` to retrieve the full ontology.**
    -   [ ] Write Tests: Create unit tests for `ast_sql/ontology.py` to ensure its methods accurately retrieve and format all ontology entries from the existing `ontology.json` and the database for frontend display.
    -   [ ] Implement Feature: Refactor `ast_sql/ontology.py`'s `__create_tables__` and `get_ontologies` methods to efficiently read and manage the ontology from `ontology.json` and the database. Ensure the methods can provide a comprehensive list of classes for display and filtering.
-   [ ] **Task: Create/Update `ast_dash/pages/audio_classes.py` to display the ontology.**
    -   [ ] Write Tests: Create tests for `ast_dash/pages/audio_classes.py` to verify that the layout renders correctly and displays the ontology data in a structured, hierarchical manner, potentially mocking the `ast_sql/ontology.py` interaction.
    -   [ ] Implement Feature: Develop the layout and necessary callbacks in `ast_dash/pages/audio_classes.py` to fetch and display the full audio ontology, allowing users to browse categories.
-   [ ] **Task: Implement basic ontology-based filtering/search preparation.**
    -   [ ] Write Tests: Create tests for a helper function or a new component that can filter a list of audio events based on selected ontology categories, verifying correct logic when mocking ontology data.
    -   [ ] Implement Feature: Develop the backend logic or helper functions required to perform filtering and searching of audio events using ontology data. This may involve enhancing existing data retrieval functions.
-   [ ] Task: Conductor - User Manual Verification 'Phase 1: Ontology Integration and Display' (Protocol in workflow.md)

## Phase 2: Audio Ingestion, Spectrogram Generation & Database Integration

This phase focuses on simulating the WiFi audio input, generating spectrograms using existing tools, and storing processed information in the database.

-   [x] **Task: Enhance `mock_data_loader.py` to simulate full audio event data.**
    -   [x] Write Tests: Create unit tests for `ast_dash/mock_data_loader.py` to ensure it generates realistic mock data including filenames, device names, timestamps, and placeholder classification results (e.g., sound1, conf_sound1, etc.) from `audio_examples` filenames.
    -   [x] Implement Feature: Modify `ast_dash/mock_data_loader.py` to extract more detailed information from `audio_examples` filenames and generate mock classification and RMS data.
-   [x] **Task: Integrate `ast_src/create_spectrogram.py` for spectrogram generation for local audio files.**
    -   [x] Write Tests: Create unit tests for the integration with `ast_src/create_spectrogram.py` that takes an audio file path and outputs a spectrogram image file, asserting correct output format and existence in the designated assets folder.
    -   [x] Implement Feature: Utilize and adapt `ast_src/create_spectrogram.py` to generate spectrograms from audio files in `audio_examples`, saving them to a designated `assets` folder.
-   [ ] **Task: Develop a script/function to populate `ast_table` with mock data and spectrogram info using `ast_sql/ast_table.py`.**
    -   [ ] Write Tests: Create integration tests that use `ast_sql/ast_table.py` to insert mock audio event data (including generated spectrogram paths) into `ast_table` and verify data integrity and correct field mapping.
    -   [ ] Implement Feature: Create a utility script or function that iterates through `audio_examples`, calls the spectrogram generation, and inserts the full set of mock data (metadata, classifications, RMS, spectrogram paths) into the `ast_table` database using existing `ast_sql/ast_table.py` methods.
-   [ ] Task: Conductor - User Manual Verification 'Phase 2: Audio Ingestion, Spectrogram Generation & Database Integration' (Protocol in workflow.md)

## Phase 3: Enhanced Dashboard UI & Interactivity

This phase enhances the main dashboard to display the new data, spectrograms, and interactivity.

-   [ ] **Task: Update `ast_dash/pages/audio_details.py` to display rich audio event data.**
    -   [ ] Write Tests: Create tests for `ast_dash/pages/audio_details.py` to verify that the table correctly displays the new columns (Event_ID, Time, Device Name, RMS value, and mock classification/confidence) from the database.
    -   [ ] Implement Feature: Modify `ast_dash/pages/audio_details.py` to fetch and display the comprehensive audio event data from `ast_table`, including mock classification results and confidence levels.
-   [ ] **Task: Implement interactive row selection to display spectrogram and audio player.**
    -   [ ] Write Tests: Create tests for the callbacks in `ast_dash/pages/audio_details.py` that handle row selection, ensuring the correct spectrogram image and audio file source are passed to the display components.
    -   [ ] Implement Feature: Develop the callbacks in `ast_dash/pages/audio_details.py` to update the spectrogram image display and audio player source dynamically upon table row selection. This will replace the existing placeholder.
-   [ ] **Task: Add ontology-based filtering and search to the dashboard UI.**
    -   [ ] Write Tests: Create tests for the filtering and search UI elements and their associated callbacks, ensuring they correctly interact with the backend filtering logic (from Phase 1).
    -   [ ] Implement Feature: Add UI elements (dropdowns, search bars) and callbacks to the dashboard page (`audio_details.py`) to enable filtering and searching by ontology categories, filenames, or device names.
-   [ ] **Task: Implement visual indication of classification confidence levels.**
    -   [ ] Write Tests: Create tests to verify that confidence levels are visually represented (e.g., color-coding, icons) alongside audio classifications on the dashboard.
    -   [ ] Implement Feature: Add visual cues or styling to the dashboard table or detailed view to indicate the confidence levels of detected audio classes.
-   [ ] Task: Conductor - User Manual Verification 'Phase 3: Enhanced Dashboard UI & Interactivity' (Protocol in workflow.md)

## Phase 4: Download Functionality

This phase adds the ability to select and download audio event snippets.

-   [ ] **Task: Implement multi-selection tick-box for audio events.**
    -   [ ] Write Tests: Create tests for the dashboard table to verify that tick-boxes are present and that multiple rows can be selected, and the selection state is managed correctly.
    -   [ ] Implement Feature: Add a selectable tick-box column to the dashboard table (`audio_details.py`) to allow multi-selection of audio event rows.
-   [ ] **Task: Develop a backend endpoint/callback for zipping and downloading selected snippets.**
    -   [ ] Write Tests: Create integration tests for a new Flask endpoint or Dash callback that takes a list of `Event_ID`s, retrieves associated audio files and spectrograms, zips them, and returns the zip file for download.
    -   [ ] Implement Feature: Implement the backend logic to retrieve the selected audio files and spectrograms, package them into a zip file, and serve it for download.
-   [ ] **Task: Integrate contextual download option into the dashboard UI.**
    -   [ ] Write Tests: Create tests to verify that the contextual download option appears and triggers the download callback with the correct selected `Event_ID`s.
    -   [ ] Implement Feature: Add a right-click context menu (or similar) on the dashboard table that, when activated with selected rows, triggers the download functionality.
-   [ ] Task: Conductor - User Manual Verification 'Phase 4: Download Functionality' (Protocol in workflow.md)
