import json
import requests

url = "http://127.0.0.1:8888/api/v1/checkout"

payload = {
    "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_example_session_id",
    "card": {
        "number": "4242424242424242",
        "exp_month": "12",
        "exp_year": "2030",
        "cvc": "123",
        "name": "Test User",
        "email": "test.user@example.com",
        "address": {
            "line1": "123 Test Street",
            "line2": "Apt 8",
            "city": "Shanghai",
            "state": "Shanghai",
            "postal_code": "200000",
            "country": "CN"
        }
    },
    "proxy": {
        "host": "127.0.0.1",
        "port": 7890,
        "user": "",
        "password": ""
    },
    "publishable_key": "pk_test_placeholder"
}

response = requests.post(url, json=payload, timeout=60)

print(f"HTTP Status: {response.status_code}")

try:
    print("Response JSON:")
    print(json.dumps(response.json(), indent=2, ensure_ascii=False))
except ValueError:
    print("Response Text:")
    print(response.text)
