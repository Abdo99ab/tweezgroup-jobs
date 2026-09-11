"""Target regions for sourcing — one click instead of typing locations by hand.

A region is a preset that expands into location strings per channel:
  linkedin  → one people search per country (LinkedIn keyword search + country name works well; cities
              are added for big markets so results are not all from the capital)
  github    → one API search per `location:"…"` qualifier (GitHub matches the free-text location field)
  schema    → country names for the JobPosting `applicantLocationRequirements` (Google for Jobs)
Regions are stored on the role (default targets) and on each search (`region` column).
"""

REGIONS = {
    "france": {"label": "France", "countries": ["France"],
               "cities": ["Paris", "Lyon", "Marseille", "Lille", "Bordeaux", "Toulouse", "Nantes"]},
    "algeria": {"label": "Algeria", "countries": ["Algeria"], "cities": ["Algiers", "Oran", "Constantine"]},
    "morocco": {"label": "Morocco", "countries": ["Morocco"], "cities": ["Casablanca", "Rabat", "Marrakech", "Tangier"]},
    "tunisia": {"label": "Tunisia", "countries": ["Tunisia"], "cities": ["Tunis", "Sfax"]},
    "maghreb": {"label": "Maghreb", "countries": ["Algeria", "Morocco", "Tunisia"],
                "cities": ["Algiers", "Casablanca", "Tunis"]},
    "benelux": {"label": "Benelux", "countries": ["Belgium", "Netherlands", "Luxembourg"], "cities": ["Brussels", "Amsterdam"]},
    "spain_portugal": {"label": "Spain & Portugal", "countries": ["Spain", "Portugal"],
                       "cities": ["Madrid", "Barcelona", "Lisbon", "Porto"]},
    "uk_ireland": {"label": "UK & Ireland", "countries": ["United Kingdom", "Ireland"], "cities": ["London", "Manchester", "Dublin"]},
    "dach": {"label": "Germany, Austria, Switzerland", "countries": ["Germany", "Austria", "Switzerland"],
             "cities": ["Berlin", "Munich", "Vienna", "Zurich"]},
    "italy": {"label": "Italy", "countries": ["Italy"], "cities": ["Milan", "Rome"]},
    "europe_east": {"label": "Eastern Europe", "countries": ["Poland", "Romania", "Bulgaria", "Ukraine", "Serbia",
                                                            "Hungary", "Czech Republic"],
                    "cities": ["Warsaw", "Bucharest", "Sofia", "Kyiv", "Belgrade"]},
    "baltics_nordics": {"label": "Nordics & Baltics", "countries": ["Estonia", "Latvia", "Lithuania", "Sweden",
                                                                   "Denmark", "Finland", "Norway"],
                        "cities": ["Tallinn", "Stockholm", "Copenhagen"]},
    "europe": {"label": "Europe (all)", "countries": ["France", "Spain", "Portugal", "Italy", "Germany", "Belgium",
                                                     "Netherlands", "United Kingdom", "Ireland", "Poland", "Romania",
                                                     "Estonia"],
               "cities": []},
    "egypt": {"label": "Egypt", "countries": ["Egypt"], "cities": ["Cairo", "Alexandria"]},
    "gulf": {"label": "Gulf (UAE, KSA, Qatar)", "countries": ["United Arab Emirates", "Saudi Arabia", "Qatar"],
             "cities": ["Dubai", "Abu Dhabi", "Riyadh", "Doha"]},
    "turkey": {"label": "Turkey", "countries": ["Turkey"], "cities": ["Istanbul", "Ankara"]},
    "mena": {"label": "MENA (all)", "countries": ["Algeria", "Morocco", "Tunisia", "Egypt", "Jordan", "Lebanon",
                                                 "United Arab Emirates", "Saudi Arabia", "Turkey"],
             "cities": []},
    "west_africa": {"label": "West Africa (FR)", "countries": ["Senegal", "Ivory Coast", "Cameroon", "Benin", "Mali"],
                    "cities": ["Dakar", "Abidjan", "Douala"]},
    "north_america": {"label": "USA & Canada", "countries": ["United States", "Canada"],
                      "cities": ["New York", "Los Angeles", "Miami", "Toronto", "Montreal"]},
    "latam": {"label": "Latin America", "countries": ["Brazil", "Mexico", "Argentina", "Colombia", "Chile"],
              "cities": ["São Paulo", "Mexico City", "Buenos Aires", "Bogotá"]},
    "south_asia": {"label": "India & Pakistan", "countries": ["India", "Pakistan", "Bangladesh"],
                   "cities": ["Bangalore", "Mumbai", "Delhi", "Lahore", "Karachi"]},
    "southeast_asia": {"label": "Philippines & SE Asia", "countries": ["Philippines", "Vietnam", "Indonesia", "Malaysia"],
                       "cities": ["Manila", "Cebu", "Ho Chi Minh City", "Jakarta"]},
    "china": {"label": "China", "countries": ["China"], "cities": ["Shenzhen", "Guangzhou", "Shanghai", "Yiwu"]},
    "remote": {"label": "Remote — worldwide", "countries": [], "cities": [], "remote": True},
}

ORDER = ["france", "algeria", "morocco", "tunisia", "maghreb", "benelux", "spain_portugal", "uk_ireland", "dach",
         "italy", "europe_east", "baltics_nordics", "europe", "egypt", "gulf", "turkey", "mena", "west_africa",
         "north_america", "latam", "south_asia", "southeast_asia", "china", "remote"]


def parse(keys):
    """'france, algeria' or ['france','algeria'] -> valid keys in preset order."""
    if not keys:
        return []
    if isinstance(keys, str):
        keys = keys.replace(";", ",").split(",")
    wanted = {k.strip().lower() for k in keys if k and k.strip()}
    return [k for k in ORDER if k in wanted]


def labels(keys):
    return [REGIONS[k]["label"] for k in parse(keys)]


def countries(keys):
    out = []
    for k in parse(keys):
        for c in REGIONS[k]["countries"]:
            if c not in out:
                out.append(c)
    return out


def is_remote(keys):
    return "remote" in parse(keys)


def expand(keys, channel="linkedin", custom=None, with_cities=False):
    """Location strings to run one search each for the given channel (deduplicated, ordered).
    custom: free-text locations typed by the recruiter (comma-separated) — always included first.
    Returns [] when only 'remote' is selected (search without a location filter)."""
    out = []
    for c in (custom or "").replace(";", ",").split(","):
        c = c.strip()
        if c and c not in out:
            out.append(c)
    for k in parse(keys):
        r = REGIONS[k]
        for c in r["countries"]:
            if c not in out:
                out.append(c)
        if with_cities and channel in ("linkedin", "github"):
            for c in r["cities"]:
                if c not in out:
                    out.append(c)
    return out


def describe(keys, custom=None):
    parts = labels(keys)
    if custom:
        parts += [c.strip() for c in custom.replace(";", ",").split(",") if c.strip()]
    return ", ".join(parts) if parts else "anywhere"
