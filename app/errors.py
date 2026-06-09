"""Meta-style error envelope. `details` must state the actual cause."""
from app import ids

TITLES = {
    100: "Invalid parameter",
    401: "Invalid API key",
    131009: "Parameter value is not valid",
    131047: "Re-engagement message",
    131049: "This message was not delivered to maintain healthy ecosystem engagement",
    131056: "Username change rate limit hit",
    131062: "Unsupported recipient",
    132001: "Template name does not exist in the translation",
    147001: "Username not available",
}


def envelope(code: int, details: str, *, subcode: int | None = None, user_title: str | None = None,
             title: str | None = None) -> dict:
    err = {
        "message": f"(#{code}) {title or TITLES.get(code, 'Sandbox error')}",
        "type": "OAuthException",
        "code": code,
        "error_data": {"messaging_product": "whatsapp", "details": details},
        "fbtrace_id": ids.fbtrace_id(str(code), details[:32]),
    }
    if subcode is not None:
        err["error_subcode"] = subcode
    if user_title is not None:
        err["error_user_title"] = user_title
    return {"error": err}


class ApiError(Exception):
    def __init__(self, code: int, details: str, http_status: int = 400, *,
                 subcode: int | None = None, user_title: str | None = None,
                 title: str | None = None):
        self.http_status = http_status
        self.body = envelope(code, details, subcode=subcode, user_title=user_title, title=title)
