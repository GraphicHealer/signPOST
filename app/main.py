"""signPOST launcher — kept tiny on purpose.

prealloc is imported first: it grabs the display hardware and all large
buffers while the heap is still contiguous — once this app's full code is
compiled and loaded, those blocks would be gone. The app lives in
signpost.py.
"""
try:
    import prealloc  # noqa: F401  (must stay first)
    import signpost  # noqa: F401  (runs the server)
except Exception as _e:
    try:
        with open("errors.log", "a") as _f:
            _f.write("boot: %r\n" % (_e,))
    except OSError:
        pass
    raise
