import os
import sys
import ctypes
from pprint import pprint

def force_cuda_paths():
    # Define potential standard CUDA installation paths
    cuda_version = "13.1"
    program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
    standard_cuda_path = os.path.join(program_files, f"NVIDIA GPU Computing Toolkit\\CUDA\\v{cuda_version}\\bin")

    # Get project root for user-provided DLLs
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    user_dll_path = os.path.join(project_root, 'gpu_asr')
    
    # Prioritize the standard CUDA toolkit path, then the user's folder
    paths_to_add = [standard_cuda_path, user_dll_path]
    
    print("--- Injecting CUDA Paths ---")
    
    for p in paths_to_add:
        if os.path.exists(p):
            # 1. Modern DLL directory addition
            try:
                os.add_dll_directory(p)
                print(f"[SUCCESS] Added to DLL search path: {p}")
            except AttributeError:
                pass # Ignore if function doesn't exist
            
            # 2. Prepending to system PATH
            if p not in os.environ["PATH"]:
                os.environ["PATH"] = p + os.pathsep + os.environ["PATH"]
                print(f"[SUCCESS] Prepended to System PATH: {p}")
        else:
            print(f"[INFO] Path not found, skipping: {p}")

def debug_load():
    print("\n--- Environment PATH (Top 10 entries) ---")
    pprint(os.environ['PATH'].split(os.pathsep)[:10])
    print("-" * 35)

    dll_name = "cublas64_13.dll"
    print(f"\nAttempting to load '{dll_name}' using ctypes...")
    
    try:
        # This will search the modified PATH
        ctypes.CDLL(dll_name, winmode=0)
        print(f"\n[SUCCESS] Successfully loaded '{dll_name}'!")
    except OSError as e:
        print(f"\n[ERROR] Failed to load '{dll_name}'.")
        print("\n--- CTypes Error ---")
        print(e)
        print("-" * 21)

if __name__ == "__main__":
    force_cuda_paths()
    debug_load()