# CLAP Service - Usage Guide

## Overview
This improved CLAP service provides advanced audio search capabilities with:
- **Emotion-aware prompts** for subjective queries
- **Ensemble search** with related queries
- **Contrastive search** to filter false positives
- **Automatic CUDA detection** and VRAM management
- **Auto-offloading** to conserve GPU memory

---

## Basic Usage

### 1. Initialize the Service (Singleton)
```python
from clap_service_improved import CLAPService

clap = CLAPService()
# Singleton pattern - always returns same instance
```

### 2. Process Your Audio Files
```python
# Pre-compute embeddings for all your 15-second recordings
audio_embeddings = []
audio_paths = ["recording_001.wav", "recording_002.wav", ...]

for audio_path in audio_paths:
    emb = clap.get_audio_embedding(audio_path, vad_is_valid=True)
    audio_embeddings.append(emb)

# Stack into array for batch processing
import numpy as np
audio_embeddings = np.vstack(audio_embeddings)  # Shape: (N, 512)
```

### 3. Simple Search
```python
results = clap.search(
    query_text="sound of pain",
    audio_embeddings=audio_embeddings
)

# Get top 5 matches
top_5_indices = results["ranked_indices"][:5]
top_5_scores = results["scores"][top_5_indices]
```

---

## Advanced Features

### Emotional/Subjective Queries (Improved)

The service automatically detects emotional queries and uses specialized prompts:

```python
# These are automatically detected as emotional
queries = [
    "sound of pain",
    "unhappy voices", 
    "crying baby",
    "angry shouting",
    "scared screaming"
]

for query in queries:
    results = clap.search(query, audio_embeddings, threshold=0.25)
    matches = results["matches"]  # Boolean array
    print(f"Found {matches.sum()} matches for '{query}'")
```

**Automatic prompt variants for emotional queries:**
- Original: "sound of pain"
- "A person expressing sound of pain"
- "The sound of someone in sound of pain"
- "Audio of sound of pain"
- "A recording of human sound of pain"

### Ensemble Search with Related Queries

Improve accuracy by searching multiple related terms:

```python
results = clap.search(
    query_text="sound of pain",
    audio_embeddings=audio_embeddings,
    related_queries=["screaming", "distress", "someone hurt", "crying out"],
    threshold=0.25
)

# The primary query is weighted 2x more than related queries
```

**Recommended related queries for common searches:**

| Primary Query | Related Queries |
|--------------|----------------|
| "sound of pain" | ["screaming", "distress", "someone hurt", "agonized voice"] |
| "unhappy voices" | ["sad speech", "crying", "sobbing", "depressed talking"] |
| "angry shouting" | ["yelling", "screaming angrily", "furious voice"] |
| "scared voice" | ["frightened screaming", "terrified speech", "panic"] |
| "laughing" | ["giggling", "happy laughter", "joy", "chuckling"] |

### Contrastive Search (Reduce False Positives)

Filter out unwanted matches by specifying what you DON'T want:

```python
results = clap.search(
    query_text="sound of pain",
    audio_embeddings=audio_embeddings,
    negative_queries=["silence", "background noise", "music", "calm speech"],
    negative_weight=0.3,  # How much to penalize negative matches
    threshold=0.25
)
```

**Recommended negative queries:**
- General filtering: `["silence", "background noise", "static"]`
- For emotional searches: `["calm speech", "music", "neutral talking"]`
- For speech searches: `["music", "instrumental", "nature sounds"]`

### Full-Featured Search Example

```python
results = clap.search(
    query_text="unhappy voices",
    audio_embeddings=audio_embeddings,
    
    # Boost with related terms
    related_queries=["sad speech", "crying", "sobbing"],
    
    # Filter out false positives
    negative_queries=["silence", "music", "happy talking"],
    negative_weight=0.3,
    
    # Only return confident matches
    threshold=0.25
)

# Access results
all_scores = results["scores"]                    # All similarity scores
ranked_indices = results["ranked_indices"]        # Sorted by score (high to low)
matches = results["matches"]                      # Boolean: above threshold

# Get your matches
matching_indices = np.where(matches)[0]
matching_paths = [audio_paths[i] for i in matching_indices]
matching_scores = all_scores[matching_indices]

print(f"Found {len(matching_paths)} matches")
for path, score in zip(matching_paths, matching_scores):
    print(f"  {path}: {score:.3f}")
```

---

## Recommended Thresholds

Based on CLAP model characteristics:

| Use Case | Recommended Threshold |
|----------|----------------------|
| Emotional queries (pain, crying, etc.) | 0.20 - 0.25 |
| Literal sounds (dog barking, door slam) | 0.25 - 0.30 |
| Very specific queries | 0.30 - 0.35 |
| Exploratory search (see more results) | 0.15 - 0.20 |

**Tip:** Start with 0.25 and adjust based on your precision/recall needs.

---

## VRAM Management

The service automatically manages GPU memory:

### Auto-Offload (Default)
```python
# Model automatically offloads after 60 seconds of inactivity
# No action needed - it's automatic!
```

### Manual Control
```python
# Force immediate offload to free VRAM
clap.force_offload()

# Check current status
status = clap.get_model_status()  # "loaded" or "offloaded"
```

### Batch Processing Strategy
```python
# For large batches, process in chunks and force offload between tasks
chunk_size = 100

for i in range(0, len(audio_paths), chunk_size):
    chunk = audio_paths[i:i+chunk_size]
    
    # Process chunk
    embeddings = [clap.get_audio_embedding(p, vad_is_valid=True) for p in chunk]
    
    # Free VRAM before next chunk
    clap.force_offload()
```

---

## Integration with Your App

### Typical Workflow

```python
# 1. Initialize service once at app startup
clap = CLAPService()

# 2. Pre-compute embeddings for all audio files (one-time, can be cached)
audio_embeddings = []
for audio_path in your_audio_files:
    # VAD should be done by your existing pipeline before this
    emb = clap.get_audio_embedding(audio_path, vad_is_valid=True)
    audio_embeddings.append(emb)

audio_embeddings = np.vstack(audio_embeddings)

# Optional: Save embeddings to avoid recomputation
np.save("audio_embeddings.npy", audio_embeddings)

# 3. Handle user searches
def handle_search(user_query):
    results = clap.search(
        query_text=user_query,
        audio_embeddings=audio_embeddings,
        threshold=0.25
    )
    
    # Return top results to user
    top_10 = results["ranked_indices"][:10]
    return [(audio_paths[i], results["scores"][i]) for i in top_10]
```

### Performance Tips

1. **Cache audio embeddings** - They don't change, so compute once and save
2. **Use batch processing** for initial embedding computation
3. **Let auto-offload work** - Don't load/unload constantly
4. **Pre-filter with VAD** - Only embed audio with actual content
5. **Use thresholds** - Filter low-confidence matches early

---

## Troubleshooting

### Low Quality Results for Emotional Queries

**Try:**
1. Use ensemble search with related queries
2. Add contrastive negative examples
3. Lower threshold to 0.20
4. Check if VAD is filtering too aggressively

```python
# Instead of:
results = clap.search("sound of pain", audio_embeddings)

# Try:
results = clap.search(
    query_text="sound of pain",
    audio_embeddings=audio_embeddings,
    related_queries=["screaming", "distress", "agonized voice"],
    negative_queries=["silence", "calm speech", "music"],
    threshold=0.20
)
```

### Too Many False Positives

**Try:**
1. Increase threshold (0.30+)
2. Add more negative examples
3. Increase negative_weight (0.4-0.5)

```python
results = clap.search(
    query_text=user_query,
    audio_embeddings=audio_embeddings,
    negative_queries=["silence", "background noise", "static", "music"],
    negative_weight=0.4,  # Increased from default 0.3
    threshold=0.30        # Increased from 0.25
)
```

### Out of VRAM Errors

**Try:**
1. Reduce batch size when processing audio
2. Force offload more frequently
3. Check if model is being reloaded too often

```python
# Process smaller batches
for audio_path in audio_paths:
    emb = clap.get_audio_embedding(audio_path, vad_is_valid=True)
    audio_embeddings_list.append(emb)
    
    # Offload every 50 files if VRAM is tight
    if len(audio_embeddings_list) % 50 == 0:
        clap.force_offload()
```

---

## Query Examples by Category

### Emotional States
```python
# Pain/Distress
clap.search("sound of pain", embeddings, 
            related_queries=["screaming", "distress", "agonized"])

# Happiness
clap.search("happy voices", embeddings,
            related_queries=["laughing", "joyful speech", "cheerful"])

# Sadness
clap.search("crying", embeddings,
            related_queries=["sobbing", "weeping", "sad voice"])

# Anger
clap.search("angry shouting", embeddings,
            related_queries=["yelling", "furious voice", "screaming angrily"])
```

### Literal Sounds
```python
# Animals
clap.search("dog barking", embeddings)

# Household
clap.search("door slamming", embeddings)

# Nature
clap.search("rain falling", embeddings)
```

### Speech Characteristics
```python
# Whispering
clap.search("whispered speech", embeddings,
            negative_queries=["loud talking", "shouting"])

# Fast speech
clap.search("rapid talking", embeddings,
            negative_queries=["slow speech", "silence"])
```

---

## API Reference

### `get_audio_embedding(audio_path, vad_is_valid=False)`
Returns normalized CLAP embedding for audio file.

### `get_text_embedding(text, query_type="auto")`
Returns normalized embedding for text query.
- `query_type`: "auto" (detects), "emotional", or "literal"

### `get_ensemble_query_embedding(primary_query, related_queries=None, query_type="auto")`
Returns weighted average of multiple query embeddings.

### `search(query_text, audio_embeddings, related_queries=None, negative_queries=None, negative_weight=0.3, threshold=None)`
High-level search interface combining all features.

### `compute_similarity(query_embedding, audio_embeddings)`
Simple cosine similarity computation.

### `search_with_contrast(query_embedding, audio_embeddings, negative_queries=None, negative_weight=0.3)`
Similarity with negative example filtering.

### `get_model_status()`
Returns "loaded" or "offloaded".

### `force_offload()`
Manually trigger model offload.

---

## Notes

- Service uses **singleton pattern** - one instance per process
- Model auto-loads on first use of any embedding method
- CUDA automatically detected - falls back to CPU if unavailable
- All embeddings are L2-normalized for cosine similarity
- Auto-offload timer resets on each model use
