"""Picklable stand-ins for trained models (tests only)."""


class FakeModel:
    def __init__(self, value):
        self.value = value

    def predict(self, X):
        return [self.value for _ in X]
