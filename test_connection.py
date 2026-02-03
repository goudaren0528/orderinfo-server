import requests
import sys

url = "https://orderinfo.speedstarsunblocked.online/api/activate"
print(f"Testing connection to: {url}")

try:
    # 模拟一个空的 POST 请求，看看服务器返回什么
    # 注意：正常的 activate 请求需要签名，这里主要看是否连通以及返回是不是 HTML
    response = requests.post(url, timeout=10)
    
    print(f"Status Code: {response.status_code}")
    print("Headers:")
    for k, v in response.headers.items():
        print(f"  {k}: {v}")
    
    print("\nContent (first 500 chars):")
    print(response.text[:500])
    
    try:
        json_data = response.json()
        print("\nJSON Response:")
        print(json_data)
    except Exception as e:
        print(f"\nFailed to parse JSON: {e}")

except Exception as e:
    print(f"Connection failed: {e}")
