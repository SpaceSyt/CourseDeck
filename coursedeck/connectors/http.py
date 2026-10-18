import httpx

from ..domain import Outcome


class TransportError(Exception):
    def __init__(self, outcome: Outcome, message: str):
        self.outcome = outcome
        self.safe_message = message
        super().__init__(message)


async def json_get(client: httpx.AsyncClient, path: str, params: dict | None = None):
    try:
        response = await client.get(path, params=params)
    except httpx.RequestError as exc:
        raise TransportError(
            Outcome.NETWORK_ERROR, "Network request failed; cached data retained."
        ) from exc
    if response.status_code == 401:
        raise TransportError(Outcome.AUTH_REQUIRED, "Session expired. Reconnect this source.")
    if response.status_code == 403:
        raise TransportError(
            Outcome.PARTIAL, "Access denied. Check account permissions and API access."
        )
    if response.status_code == 429:
        raise TransportError(Outcome.RATE_LIMITED, "Rate limited. Wait before syncing again.")
    if response.status_code >= 500:
        raise TransportError(Outcome.NETWORK_ERROR, "Provider temporarily unavailable.")
    if response.status_code != 200:
        raise TransportError(
            Outcome.PARSE_ERROR, f"Unexpected provider response ({response.status_code})."
        )
    try:
        return response.json()
    except ValueError as exc:
        raise TransportError(
            Outcome.PARSE_ERROR, "Expected JSON but provider returned another format."
        ) from exc
