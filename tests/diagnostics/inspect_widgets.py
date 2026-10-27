
try:
    import speechwidgets
    print("speechwidgets is installed.")
    print("Components:", dir(speechwidgets))
    # Try to print help/props for the likely component if found
    for item in dir(speechwidgets):
        if item[0].isupper(): # heuristic for component class
            print(f"\n--- Properties for {item} ---")
            try:
                print(getattr(speechwidgets, item).__doc__)
            except:
                print("No docstring.")
except ImportError:
    print("speechwidgets is NOT installed.")
