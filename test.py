from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--model", default="auto/bynara")
    parser.add_argument("--base-url", default="https://router.bynara.id/v1")
    parser.add_argument("--api-key", default=os.getenv("NARA_API_KEY"))
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()

    if not args.api_key:
        print("NARA_API_KEY is missing. Set it in env or pass --api-key.", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    url = f"{base_url}/chat/completions"

    payload = {
        "model": args.model,
        "messages": [
            {"role": "user", "content": "Say only: OK"}
        ],
        "temperature": 0.2,
        "max_tokens": 32,
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {args.api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(proxy=args.proxy, timeout=args.timeout) as client:
            response = client.post(url, headers=headers, json=payload)
    except Exception as exc:
        print(type(exc).__name__)
        print(str(exc))
        return 1

    print("status:", response.status_code)
    print("content-type:", response.headers.get("content-type", ""))

    try:
        data = response.json()
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        print(response.text[:2000])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())