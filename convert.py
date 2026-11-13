"""MOCK ONLY: stands in for clicking ETF-Creation / ETF-Redemption in the RIT Client's Assets tab.
Usage: python convert.py redeem [quantity]    or    python convert.py create [quantity]   (blocks of 10,000)"""
import sys
import requests

kind = sys.argv[1] if len(sys.argv) > 1 else "redeem"
qty = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
r = requests.post("http://localhost:9999/v1/mock/convert", params={"kind": kind, "quantity": qty},
                  headers={"X-API-Key": "Rotman"})
print(r.status_code, r.text)
