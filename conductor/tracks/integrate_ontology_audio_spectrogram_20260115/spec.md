# Specification: Integrated Audio Ontology, Local Audio Processing, and Spectrogram Display

## 1. Overview

This track introduces a significant enhancement to the audio monitoring application, enabling the integration of a structured audio ontology, processing of local audio example files as if received from WiFi, and generating/displaying associated spectrograms on the dashboard. The goal is to provide a comprehensive and interactive interface for audio event analysis and management.

## 2. Functional Requirements

### 2.1 Audio Ontology Integration

*   **FR1.1: Ontology Display:** The 'Audio classes' tab shall display the audio ontology in a structured and hierarchical view, allowing users to browse and understand the available sound categories.
*   **FR1.2: Ontology-based Filtering:** The dashboard shall provide dynamic filtering options for audio events based on classifications defined in the integrated ontology.
*   **FR1.3: Ontology-based Search:** Users shall be able to search audio events using terms related to the ontology classifications.

### 2.3 Local Audio Processing and Ingestion (Mock WiFi Input)

*   **FR2.1: Local File Ingestion:** The application shall process audio files located in the `audio_examples` folder, treating them as if they were received from a WiFi source.
*   **FR2.2: Metadata Extraction:** For each audio file, the application shall extract and store relevant metadata including:
    *   Filename
    *   Filepath
    *   Timestamp of the recording (inferable from filename or file metadata)
    *   Device name (inferable from filename or a predefined mapping)
*   **FR2.3: Spectrogram Generation:** For each ingested audio file, the system shall generate a corresponding spectrogram image.
*   **FR2.4: Dashboard Integration:** The extracted metadata and generated spectrograms shall be made available for display on the main dashboard.

### 2.3 Enhanced Audio Event Dashboard

*   **FR3.1: Core Data Display:** The main dashboard (likely the "Audio Dashboard" page) shall display audio event data in a tabular format, including the existing columns: `Event_ID`, `Time`, `Device Name`, and `RMS value`.
*   **FR3.2: Interactive Row Selection:** Clicking on any row in the dashboard table shall:
    *   Display the associated spectrogram image for the selected audio event.
    *   Display an audio playback bar (player) underneath the spectrogram, allowing users to listen to the audio snippet.
*   **FR3.3: Ontology-driven Filtering & Search:** Implement filtering options and search functionality on the dashboard that leverage the integrated audio ontology (as per FR1.2 and FR1.3).
*   **FR3.4: Confidence Level Visualization:** Provide a visual indication of audio classification confidence levels for each displayed audio event.
*   **FR3.5: Multi-selection Tick-box:** A tick-box shall be added next to each `Event_ID` in the dashboard table, allowing users to select multiple audio event snippets.
*   **FR3.6: Contextual Download:** A right-click context menu (or similar intuitive interaction) shall be implemented on the dashboard, providing an option to download the currently selected audio snippets (based on the tick-boxes) in a zipped format. This zipped file should include both the audio files and their corresponding spectrograms/metadata.

## 3. Non-Functional Requirements

*   **NFR3.1: Performance:** The display and interaction with audio events and spectrograms on the dashboard should be responsive, even with a moderate number of entries.
*   **NFR3.2: Maintainability:** The code for ontology integration, audio processing, and dashboard enhancements shall follow existing project conventions and best practices for maintainability.
*   **NFR3.3: Offline Capability:** The processing of local audio examples and generation of spectrograms should function without requiring an active internet connection.
*   **NFR3.4: Error Handling:** Appropriate error handling and user feedback mechanisms should be in place for file processing and spectrogram generation.

## 4. Acceptance Criteria

*   The 'Audio classes' tab correctly displays a structured list of ontology items.
*   Filtering and search based on ontology classifications function as expected on the dashboard.
*   Audio files from `audio_examples` are processed, and their metadata and generated spectrograms are displayed on the dashboard.
*   Clicking a row in the dashboard table correctly loads and displays the corresponding spectrogram and audio player.
*   The audio player successfully plays the selected audio file.
*   Confidence levels for audio classifications are visually represented on the dashboard.
*   Multiple audio events can be selected using tick-boxes.
*   Selected audio events (audio files and spectrograms) can be downloaded as a zip file via a contextual menu.
*   The application starts and runs without runtime errors related to these new features.

## 5. Out of Scope

*   Real-time processing of new audio streams from actual WiFi connections (this track focuses on local files).
*   Advanced machine learning model training or re-training.
*   Redesign of the entire application layout, unless directly required by new components.
