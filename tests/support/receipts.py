class Receipt:
    def __init__(self, value="sent"):
        self.value = value
        self.error = None
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.value
