import requests

API_KEY = "NTYxMTZCQ0ZENUVGNkJBMzlFOTY3MjQyQ0MxMURBRjFDNUM0QkI3REI4MzI4QUIxNjVFOTNGRUE1MDk4RDlEMTNFNThCQTRCNTExMDJGNDQ2QjQ2REFEM0M1OERBODBC"   # <-- hier je echte key
BASE_URL = "https://public.ep-online.nl/api/v5/PandEnergielabel/Adres"

def get_energielabel(postcode, huisnummer, huisletter=None, toevoeging=None):
    headers = {
        "Authorization": API_KEY,
        "Accept": "application/json"
    }

    params = {
        "postcode": postcode,
        "huisnummer": huisnummer
    }

    # optionele parameters
    if huisletter:
        params["huisletter"] = huisletter
    if toevoeging:
        params["huisnummertoevoeging"] = toevoeging

    r = requests.get(BASE_URL, headers=headers, params=params)
    r.raise_for_status()

    return r.json()


if __name__ == "__main__":
    # testadres
    postcode = "3771WJ"
    huisnummer = "29"

    data = get_energielabel(postcode, huisnummer)
    print(data)
    print("Volledige response:")
    for k, v in data.items():
        print(k, ":", v)
