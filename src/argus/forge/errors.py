"""Allow shared recovery without depending on a provider."""


class ForgeError(Exception):
    forge = ""


class ForgeAuthError(ForgeError):
    pass


class ForgeDenied(ForgeError):
    pass


class ForgeRateLimited(ForgeError):
    def __init__(self, reset_at: float, forge: str = ""):
        self.reset_at = reset_at
        self.forge = forge
        super().__init__(f"{forge or 'forge'} rate limited until {reset_at}")

    def __reduce__(self):
        return type(self), (self.reset_at, self.forge)
