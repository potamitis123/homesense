# Spec: Enhance and Refactor Dash Frontend

## 1. Overview

This track focuses on enhancing and refactoring the existing Dash frontend to improve user experience, align with the new product guidelines, and provide better visualization of audio analysis data. The refactor should aim for a more modular, maintainable, and performant codebase.

## 2. Key Objectives

- **Improve User Experience:** The application should be more intuitive and easier to navigate. The interface should be clean, minimalist, and responsive, as per the product guidelines.
- **Enhance Data Visualization:** Introduce more powerful and intuitive ways to visualize audio data and analysis results. This includes real-time dashboards and detailed analytical views like spectrograms.
- **Modular Refactoring:** Refactor the existing Dash application (`ast_dash/`) into a more modular structure. Break down large components into smaller, reusable ones.
- **Improve Performance:** Optimize the application to handle data updates and user interactions more efficiently.
- **Align with Product Guidelines:** Ensure the frontend's tone, voice, and design are consistent with the `product-guidelines.md`.

## 3. Functional Requirements

### 3.1. Dashboard Refinements
- **Real-time Monitoring View:** A clean, primary dashboard showing live audio input and real-time event classification.
- **Historical Analysis View:** A separate view or page to explore historical data with filtering options (date, device, event type).
- **Audio Details View:** A dedicated view to inspect a single audio event, showing its waveform, spectrogram, classification results, and metadata.

### 3.2. Component Refactoring
- **`ast_app.py`:** Should be refactored to be the main entry point, with layouts and callbacks organized into separate files or modules.
- **`pages/`:** The pages should be reviewed and refactored for clarity and consistency. The existing `audio_details.py`, `date_sounds.py`, and `statistics.py` should be integrated into a more cohesive user flow.
- **`react/`:** The existing React components should be reviewed and potentially replaced with Dash-native components or integrated more cleanly if they are still needed. The presence of multiple similar files (`audio_component copy 2.js`, `audio_component.js`, `audio_component3.js`) suggests a need for cleanup.

### 3.3. New Features
- **Customizable Alerts UI:** A new page in the settings to configure alert conditions and notification methods.
- **Data Export Button:** Implement a user-friendly button to export the current view's data (e.g., as CSV or JSON).

## 4. Non-Functional Requirements

- **Performance:** The dashboard should load quickly and update smoothly, even with a significant amount of data. Callbacks should be optimized.
- **Maintainability:** The code should be well-documented, with clear separation of concerns to facilitate future development.
- **Usability:** The application should be easy to use for its target audience (researchers and data analysts) with minimal training.

## 5. Out of Scope

- Changes to the backend audio processing logic (`ast_src/`).
- Modifications to the database schema (`ast_sql/`).
- Alterations to the MQTT communication protocol (`mqtt_clent/`).
- Changes to the WiFi connection code.
