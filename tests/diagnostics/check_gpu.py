import torch

def check_gpu():
    """
    Prints information about the available CUDA device.
    """
    print("--- GPU Verification ---")
    if not torch.cuda.is_available():
        print("CUDA is not available. PyTorch is using CPU.")
        return

    device_id = 0
    torch.cuda.set_device(device_id)

    try:
        device_name = torch.cuda.get_device_name(device_id)
        print(f"CUDA Device: {device_name}")

        total_mem = torch.cuda.get_device_properties(device_id).total_memory
        total_mem_gb = round(total_mem / (1024**3), 2)
        print(f"Total VRAM: {total_mem_gb} GB")

        # A quick check
        print("\nPerforming a quick tensor operation on GPU...")
        x = torch.tensor([1.0, 2.0]).to("cuda")
        print("Success! Tensor created on GPU:", x)

    except Exception as e:
        print(f"An error occurred during GPU verification: {e}")

if __name__ == "__main__":
    check_gpu()
