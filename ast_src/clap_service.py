import os
# torch/transformers and numpy can load different OpenMP runtimes (libomp vs
# libiomp5md). Without this, CLAP search aborts mid-matmul with OMP Error #15.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import torch
import numpy as np
import librosa
from pathlib import Path
from transformers import ClapModel, ClapProcessor
import gc
import time
import threading

# Configuration
MODEL_ID = "laion/larger_clap_music_and_speech"
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a"}
IDLE_OFFLOAD_TIME = 60  # seconds

class CLAPService:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(CLAPService, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.model = None
        self.processor = None
        # Force CPU to save VRAM
        self.device = "cpu"
        self._last_used_time = time.time()
        self._offload_timer = None
        self._initialized = True
        self.model_state_lock = threading.Lock()
        
        # Emotional/subjective keywords for automatic query type detection
        self.emotional_keywords = {
            "pain", "happy", "sad", "angry", "scared", "unhappy", "crying", 
            "screaming", "laughing", "distress", "joy", "fear", "anxiety",
            "excited", "calm", "stressed", "frustrated", "relieved", "worried",
            "suffering", "agony", "pleasure", "surprise", "disgust", "hurt",
            "sobbing", "weeping", "moaning", "groaning", "shouting", "yelling"
        }
        
        print(f"CLAPService initialized. Device: {self.device}")

    def _load_model(self):
        """Load model to GPU/CPU if not already loaded"""
        with self.model_state_lock:
            if self.model is None:
                print(f"Loading CLAP model: {MODEL_ID} to {self.device}...")
                self.model = ClapModel.from_pretrained(MODEL_ID)
                self.processor = ClapProcessor.from_pretrained(MODEL_ID)
                self.model.to(self.device)
                print("CLAP model loaded.")
            self._last_used_time = time.time()
            self._cancel_offload_timer()

    def _offload_model(self):
        """Offload model from GPU to free VRAM"""
        with self.model_state_lock:
            if self.model is not None:
                print("Offloading CLAP model from GPU to free VRAM...")
                self.model.cpu()
                # Do not del attributes, just set to None to avoid AttributeError race conditions
                self.model = None
                self.processor = None
                if self.device == "cuda":
                    torch.cuda.empty_cache()
                gc.collect()
                print("CLAP model offloaded.")

    def _reset_offload_timer(self):
        """Reset the automatic offload timer"""
        self._cancel_offload_timer()
        self._offload_timer = threading.Timer(IDLE_OFFLOAD_TIME, self._offload_model)
        self._offload_timer.start()

    def _cancel_offload_timer(self):
        """Cancel the automatic offload timer"""
        if self._offload_timer:
            self._offload_timer.cancel()
            self._offload_timer = None

    def _detect_query_type(self, text):
        """
        Automatically detect if query is emotional/subjective vs literal
        Returns: "emotional" or "literal"
        """
        clean_text = text.lower()
        if any(keyword in clean_text for keyword in self.emotional_keywords):
            return "emotional"
        return "literal"

    def get_audio_embedding(self, audio_path, vad_is_valid=False):
        """
        Loads audio, optionally applies VAD, and returns the normalized CLAP embedding.
        
        Args:
            audio_path: Path to audio file
            vad_is_valid: Whether VAD has already validated this audio contains speech
            
        Returns:
            Normalized CLAP embedding as numpy array
        """
        self._load_model()
        
        # Load audio at 48kHz for CLAP
        y, sr = librosa.load(audio_path, sr=48000, mono=True)
        
        # VAD Integration (should be handled by caller - main.py)
        # If vad_is_valid is False, the caller should have already filtered
        # this audio or determined it doesn't need VAD validation
        
        inputs = self.processor(audios=y, sampling_rate=48000, return_tensors="pt").to(self.device)
        
        with torch.no_grad():
            audio_embeds = self.model.get_audio_features(**inputs)
        
        # Normalize
        audio_embeds = audio_embeds / audio_embeds.norm(p=2, dim=-1, keepdim=True)
        
        # Clean up VRAM
        if self.device == "cuda":
            torch.cuda.empty_cache()
        gc.collect()
        
        self._reset_offload_timer()
        return audio_embeds.cpu().numpy()

    def get_text_embedding(self, text, query_type="auto"):
        """
        Generate text embedding with prompt ensembling.
        Automatically detects emotional vs literal queries and applies appropriate templates.
        
        Args:
            text: Query text
            query_type: "auto", "emotional", or "literal"
            
        Returns:
            Normalized text embedding as numpy array
        """
        self._load_model()
        
        # Clean input: remove common UI prefixes
        clean_text = text.lower().replace("find sounds that include", "").strip()
        clean_text = clean_text.replace("find sounds of", "").strip()
        clean_text = clean_text.replace("search for", "").strip()
        
        # Detect query type if auto
        if query_type == "auto":
            query_type = self._detect_query_type(clean_text)
        
        # Create variants based on query type
        if query_type == "emotional":
            variants = [
                clean_text,
                f"A person expressing {clean_text}",
                f"The sound of someone in {clean_text}",
                f"Audio of {clean_text}",
                f"A recording of human {clean_text}"
            ]
        else:
            # Literal/acoustic queries
            variants = [
                clean_text,
                f"The sound of {clean_text}",
                f"A recording of {clean_text}"
            ]

        # Batch process all variations at once
        inputs = self.processor(text=variants, return_tensors="pt", padding=True).to(self.device)
        
        with torch.no_grad():
            text_features = self.model.get_text_features(**inputs)
            
            # Average the embeddings in the latent space
            mean_embedding = torch.mean(text_features, dim=0, keepdim=True)
            
            # Normalize the averaged result
            norm_embedding = mean_embedding / mean_embedding.norm(p=2, dim=-1, keepdim=True)
        
        # Clean up VRAM
        if self.device == "cuda":
            torch.cuda.empty_cache()
        gc.collect()
        
        self._reset_offload_timer()
        return norm_embedding.cpu().numpy()

    def get_ensemble_query_embedding(self, primary_query, related_queries=None, query_type="auto"):
        """
        Create ensemble embedding from multiple related queries.
        Primary query is weighted more heavily.
        
        Args:
            primary_query: Main search query
            related_queries: List of related/synonym queries (optional)
            query_type: "auto", "emotional", or "literal"
            
        Returns:
            Normalized ensemble embedding as numpy array
            
        Example:
            get_ensemble_query_embedding(
                "sound of pain",
                related_queries=["screaming", "distress", "someone hurt"]
            )
        """
        self._load_model()
        
        all_queries = [primary_query]
        if related_queries:
            all_queries.extend(related_queries)
        
        # Get embeddings for all queries
        embeddings = []
        for query in all_queries:
            emb = self.get_text_embedding(query, query_type=query_type)
            embeddings.append(emb)
        
        # Weight primary query 2x more than related queries
        weights = [2.0] + [1.0] * (len(all_queries) - 1)
        
        # Apply weights
        weighted_embeddings = [emb * w for emb, w in zip(embeddings, weights)]
        weighted_stack = np.vstack(weighted_embeddings)
        
        # Average and renormalize
        ensemble = np.mean(weighted_stack, axis=0, keepdims=True)
        ensemble = ensemble / np.linalg.norm(ensemble, axis=1, keepdims=True)
        
        return ensemble

    def compute_similarity(self, query_embedding, audio_embeddings):
        """
        Compute cosine similarity between query and audio embeddings.
        
        Args:
            query_embedding: Text query embedding (1, D) or (D,)
            audio_embeddings: Audio embeddings (N, D) or (D,)
            
        Returns:
            Similarity scores (N,)
        """
        query = np.asarray(query_embedding, dtype=np.float32)
        audio = np.asarray(audio_embeddings, dtype=np.float32)

        if query.ndim == 2 and query.shape[0] == 1:
            query = query.reshape(-1)
        elif query.ndim > 1:
            query = query.flatten()

        if audio.ndim == 1:
            audio = audio.reshape(1, -1)

        # Explicit contiguous float32 matmul avoids OpenMP crashes seen with
        # mixed torch/numpy backends on this conda env.
        query = np.ascontiguousarray(query, dtype=np.float32)
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        return audio @ query

    def search_with_contrast(self, query_embedding, audio_embeddings, 
                           negative_queries=None, negative_weight=0.3):
        """
        Compute similarity scores with optional negative examples (contrastive search).
        Helps reduce false positives by penalizing audio similar to negative examples.
        
        Args:
            query_embedding: Positive query embedding
            audio_embeddings: Audio embeddings to search
            negative_queries: List of negative example queries (e.g., ["silence", "noise"])
            negative_weight: How much to penalize negative similarity (0.0-1.0)
            
        Returns:
            Adjusted similarity scores
            
        Example:
            search_with_contrast(
                query_embedding,
                audio_embeddings,
                negative_queries=["silence", "background noise", "music"],
                negative_weight=0.3
            )
        """
        # Compute positive similarity
        similarities = self.compute_similarity(query_embedding, audio_embeddings)
        
        # Apply negative examples if provided
        if negative_queries and len(negative_queries) > 0:
            # One matrix-vector compute_similarity() call per negative query
            # rather than stacking them into a matrix and doing a single
            # matrix-matrix multiply: on this machine (mixed torch/MKL and
            # numpy/MKL runtimes) a (N,D)@(D,M) GEMM with M>=2 right after
            # CLAP inference hard-crashes the process (no Python exception,
            # confirmed reproducible) even with the contiguous-float32
            # workaround that fixes the single-query case. Matrix-vector
            # multiplies (M==1) do not trigger it, so looping is what
            # actually stays inside the process.
            neg_similarity_columns = [
                self.compute_similarity(self.get_text_embedding(nq), audio_embeddings)
                for nq in negative_queries
            ]
            max_neg_similarity = np.maximum.reduce(neg_similarity_columns)
            
            # Penalize positive scores by negative similarity
            adjusted_scores = similarities - (negative_weight * max_neg_similarity)
            
            return adjusted_scores
        
        return similarities

    def search(self, query_text, audio_embeddings, 
              related_queries=None, negative_queries=None,
              negative_weight=0.3, threshold=None):
        """
        High-level search interface combining all features.
        
        Args:
            query_text: Main search query
            audio_embeddings: Pre-computed audio embeddings (N, D)
            related_queries: Optional list of related queries for ensemble
            negative_queries: Optional list of negative examples for contrast
            negative_weight: Weight for negative examples (default 0.3)
            threshold: Optional similarity threshold for filtering results
            
        Returns:
            Dictionary with:
                - scores: Similarity scores for all audio
                - matches: Boolean array if threshold provided
                - ranked_indices: Indices sorted by score (descending)
                
        Example:
            results = clap_service.search(
                query_text="sound of pain",
                audio_embeddings=audio_embeds,
                related_queries=["screaming", "distress"],
                negative_queries=["silence", "music"],
                threshold=0.25
            )
        """
        # Create query embedding (with ensemble if related queries provided)
        if related_queries:
            query_emb = self.get_ensemble_query_embedding(query_text, related_queries)
        else:
            query_emb = self.get_text_embedding(query_text)
        
        # Compute scores (with contrast if negative queries provided)
        if negative_queries:
            scores = self.search_with_contrast(
                query_emb, 
                audio_embeddings,
                negative_queries=negative_queries,
                negative_weight=negative_weight
            )
        else:
            scores = self.compute_similarity(query_emb, audio_embeddings)
        
        # Ensure scores is 1D
        if scores.ndim > 1:
            scores = scores.squeeze()
        
        # Rank by score
        ranked_indices = np.argsort(scores)[::-1]  # Descending order
        
        result = {
            "scores": scores,
            "ranked_indices": ranked_indices
        }
        
        # Apply threshold if provided
        if threshold is not None:
            result["matches"] = scores > threshold
            result["threshold"] = threshold
        
        return result

    def get_model_status(self):
        """Check if model is currently loaded"""
        return "loaded" if self.model is not None else "offloaded"

    def force_offload(self):
        """Manually trigger model offload (useful for VRAM management)"""
        self._cancel_offload_timer()
        self._offload_model()


# --- Example usage (for testing) ---
if __name__ == "__main__":
    clap_service = CLAPService()
    
    print("\n=== Testing Text Embeddings ===")
    
    # Test literal query
    literal_query = "dog barking"
    literal_emb = clap_service.get_text_embedding(literal_query)
    print(f"Literal query '{literal_query}' embedding shape: {literal_emb.shape}")
    
    # Test emotional query
    emotional_query = "sound of pain"
    emotional_emb = clap_service.get_text_embedding(emotional_query)
    print(f"Emotional query '{emotional_query}' embedding shape: {emotional_emb.shape}")
    
    # Test ensemble query
    ensemble_emb = clap_service.get_ensemble_query_embedding(
        "unhappy voices",
        related_queries=["sad speech", "crying", "distressed talking"]
    )
    print(f"Ensemble query embedding shape: {ensemble_emb.shape}")
    
    print(f"\nModel status: {clap_service.get_model_status()}")
    
    print("\n=== Testing Audio Embedding ===")
    
    # Create a dummy audio file for testing
    dummy_audio_path = "temp_test_audio.wav"
    if not os.path.exists(dummy_audio_path):
        from scipy.io.wavfile import write
        samplerate = 48000
        duration = 1  # seconds
        frequency = 440  # Hz
        t = np.linspace(0., duration, int(samplerate * duration), endpoint=False)
        amplitude = np.iinfo(np.int16).max * 0.5
        data = amplitude * np.sin(2. * np.pi * frequency * t)
        write(dummy_audio_path, samplerate, data.astype(np.int16))
        print(f"Created dummy audio file: {dummy_audio_path}")

    # Get audio embedding
    audio_emb = clap_service.get_audio_embedding(dummy_audio_path, vad_is_valid=True)
    print(f"Audio embedding shape: {audio_emb.shape}")
    
    print("\n=== Testing Search Functionality ===")
    
    # Create some dummy audio embeddings for testing
    num_audio_files = 10
    dummy_audio_embeddings = np.random.randn(num_audio_files, audio_emb.shape[1])
    # Normalize them
    dummy_audio_embeddings = dummy_audio_embeddings / np.linalg.norm(
        dummy_audio_embeddings, axis=1, keepdims=True
    )
    
    # Add our test audio to the collection
    dummy_audio_embeddings[0] = audio_emb.squeeze()
    
    # Perform search with all features
    results = clap_service.search(
        query_text="sound of pain",
        audio_embeddings=dummy_audio_embeddings,
        related_queries=["screaming", "distress"],
        negative_queries=["silence", "music", "background noise"],
        negative_weight=0.3,
        threshold=0.20
    )
    
    print(f"Search scores shape: {results['scores'].shape}")
    print(f"Top 3 matches (indices): {results['ranked_indices'][:3]}")
    print(f"Top 3 scores: {results['scores'][results['ranked_indices'][:3]]}")
    if "matches" in results:
        print(f"Number of matches above threshold: {results['matches'].sum()}")
    
    print("\n=== Testing Offload Timer ===")
    print(f"Current model status: {clap_service.get_model_status()}")
    print(f"Waiting {IDLE_OFFLOAD_TIME + 5} seconds to test auto-offload...")
    time.sleep(IDLE_OFFLOAD_TIME + 5)
    print(f"Model status after idle time: {clap_service.get_model_status()}")
    
    # Clean up
    if os.path.exists(dummy_audio_path):
        os.remove(dummy_audio_path)
        print(f"\nCleaned up dummy audio file")
    
    print("\n=== Test Complete ===")
