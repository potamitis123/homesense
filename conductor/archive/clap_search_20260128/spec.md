# Spec: Embed CLAP-based search in the Dash Dashboard

## 1. Overview
Implement a natural language search functionality in the Dash dashboard using CLAP (Contrastive Language-Audio Pretraining) embeddings. This allows users to search for audio events by describing them in plain text (e.g., "dog barking", "car siren").

## 2. Functional Requirements
- **FR1: Search UI:** Add a search input field and a "Search" button to the "Filter & Search" section of the `audio_details.py` page.
- **FR2: Similarity Threshold:** Add a slider to adjust the cosine similarity threshold for CLAP results.
- **FR3: Text Embedding:** Use the `CLAPService` to convert the user's text query into an embedding.
- **FR4: Vector Search:** Implement a method to compare the query embedding with all stored `clap_embedding` BLOBs in the `ast_table` database using cosine similarity.
- **FR5: Result Display:** Update the Ag-Grid table to show only records that meet the threshold, sorted by similarity score (descending).
- **FR6: Integration:** Ensure the search works alongside existing date filters.

## 3. Technical Requirements
- Use `CLAPService` (singleton) for embedding generation.
- Implement similarity calculation in `SQL_to_Pandas` or `Ast_table`.
- Ensure efficient memory usage by processing embeddings in batches if necessary.
- Follow existing TDD workflow and Conductor protocols.
