# Shared by the terminal wrapper and the settings API. Keep provider-text
# matching here; API consumers use the stable code, never transport prose.
def codex_pairing_error_code(error):
    """Classify the explicit account-policy rejection, not unrelated 403s."""
    error = str(error or "")
    if (error and "HTTP 403" in error
            and "Multi-factor authentication required" in error):
        return "mfa_required"
    return None
