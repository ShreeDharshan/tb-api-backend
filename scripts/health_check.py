import requests

URL = "http://localhost:10000/healthz"

if __name__ == "__main__":
    response = requests.get(URL, timeout=5)
    print(response.status_code, response.text)
