import torch
import os

# Set device to CUDA if available, otherwise CPU
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def check_and_log_device():
    """Prints information about the device being used."""
    print("-" * 30)
    print(f"PyTorch using device: {device}")
    if device.type == "cuda":
        print(f"CUDA Device Name: {torch.cuda.get_device_name(0)}")
        total_mem = torch.cuda.get_device_properties(0).total_memory
        print(f"Total VRAM: {round(total_mem / (1024**3), 2)} GB")
    # Suppress the "Running on CPU" message if the user doesn't want it
    elif os.environ.get("SUPPRESS_CPU_WARNING", "0") != "1":
        print("CUDA not found. Running on CPU. Performance may be degraded for deep learning tasks.")
    print("-" * 30)

# Run the check once on import
check_and_log_device()
