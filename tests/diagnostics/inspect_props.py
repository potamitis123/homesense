
import speechwidgets
try:
    print("Props:", speechwidgets.SpectrogramPlayer._prop_names)
except Exception as e:
    print(f"Could not read _prop_names: {e}")
