
import speechwidgets
import json

def inspect():
    component = speechwidgets.SpectrogramPlayer()
    # Dash components have a _prop_names attribute or __dict__ keys
    props = getattr(component, '_prop_names', [])
    if not props:
        # Fallback for some component types
        props = [k for k in component.__dict__.keys() if not k.startswith('_')]
    print(f"PROPS: {props}")

if __name__ == "__main__":
    inspect()
