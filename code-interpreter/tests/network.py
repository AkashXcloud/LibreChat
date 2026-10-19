"""
Description
    Accessing external resources could be potentially dangerous

"""

import urllib.request
contents = urllib.request.urlopen("https://example.com").read()