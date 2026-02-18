import re
import ssl
from dataclasses import dataclass
from typing import Optional

import aiohttp
import certifi


@dataclass
class RestaurantInfo:
    name: str
    address: str
    phone_number: str
    place_id: str


def normalize_phone_number(phone: str) -> str:
    """Convert any phone format to E.164 (+15551234567) for Plivo."""
    digits = re.sub(r"[^\d]", "", phone)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    # Already has country code or international
    return f"+{digits}"


async def search_restaurant(
    session: aiohttp.ClientSession,
    query: str,
    api_key: str,
) -> Optional[RestaurantInfo]:
    """Search for a restaurant using Google Places Text Search (New) API."""
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.nationalPhoneNumber,places.id",
    }
    payload = {
        "textQuery": query,
        "languageCode": "en",
    }

    async with session.post(url, json=payload, headers=headers) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise Exception(f"Google Places API error {resp.status}: {text}")
        data = await resp.json()

    places = data.get("places", [])
    if not places:
        return None

    place = places[0]
    phone = place.get("nationalPhoneNumber", "")
    if not phone:
        return None

    return RestaurantInfo(
        name=place.get("displayName", {}).get("text", "Unknown"),
        address=place.get("formattedAddress", ""),
        phone_number=normalize_phone_number(phone),
        place_id=place.get("id", ""),
    )
