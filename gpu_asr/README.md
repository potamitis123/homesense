# gpu_asr

Holds the one CUDA library that GPU transcription needs and that nothing else
in the environment provides under the right filename.

## Why `cublas64_12.dll` is here

CTranslate2 4.6.3 (the backend of `faster-whisper`) loads cuBLAS by hard-coded
filename, and it asks for the CUDA **12** name `cublas64_12.dll`. This project
runs `torch 2.9.1+cu130`, which ships CUDA **13** libraries, so nothing in the
environment answers to that name and transcription fails with:

```
ASR Error: Library cublas64_12.dll is not found or cannot be loaded
```

The file here is CUDA 13's `cublas64_13.dll` copied under the `_12` name. The
ABI CTranslate2 uses is compatible, so the rename is enough.

`ast_src/streaming_ASR.py` adds this folder via `os.add_dll_directory()` and
prepends it to `PATH` before importing `faster_whisper`.

## Why nothing else is here

This folder previously contained the full 2.5 GB CUDA redistributable set. A
runtime probe of the loaded modules during AST inference, pyannote diarization,
speaker embedding and Whisper transcription showed that **only**
`cublas64_12.dll` was ever resolved from here — every other CUDA library
(cuDNN, cuFFT, cuRAND, cuSOLVER, cuSPARSE, NVRTC, and cuBLASLt) is loaded from
`site-packages/torch/lib`, and `cudnn64_9.dll` from `site-packages/ctranslate2`.
The duplicates were removed.

## Dependency to be aware of

`cublas64_12.dll` statically imports **`cublasLt64_13.dll`**, which is supplied
by `site-packages/torch/lib`. Reinstalling PyTorch against a different CUDA
version will therefore break GPU transcription. `boot/preflight.py` checks for
both halves of this arrangement and warns if either is missing.

If you ever need to recreate the shim:

```powershell
Copy-Item "$env:CONDA_PREFIX\Lib\site-packages\torch\lib\cublas64_13.dll" `
          "gpu_asr\cublas64_12.dll"
```
