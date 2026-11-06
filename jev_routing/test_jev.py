"""Run one live Jev routing decision: python jev_routing/test_jev.py."""

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def load_key():
    key = os.environ.get("JEV_API_KEY")
    if key:
        return key
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "JEV_API_KEY":
                return value.strip().strip("\"'")
    return None


def main():
    key = load_key()
    if not key:
        raise SystemExit("JEV_API_KEY is missing or empty.")

    payload = {
        "model": "jev-latest",
        "state": "My account is locked and I cannot log in.",
        "questions": {
            "account_support": {
                "type": "noul",
                "instructions": "Does this request need account support?",
            }
        },
    }
    request = Request(
        "https://api.typesafe.ai/v1/systemone",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise SystemExit(f"Jev request failed with HTTP {exc.code}.") from None
    except URLError as exc:
        raise SystemExit(f"Could not connect to Jev: {type(exc.reason).__name__}.") from None

    print("Jev request succeeded (HTTP 200).")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
