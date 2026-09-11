"""Tweezgroup LinkedIn sourcing worker — a dumb browser hand driven by the applicant system's API.

    python -m worker login      # one-time: opens Chromium, you log into the recruiting LinkedIn account
    python -m worker run        # loop: searches, outreach queue, inbox — within the app's caps and hours
    python -m worker once       # a single pass (useful from a scheduler)
    python -m worker check      # prints API status + whether the LinkedIn session is still valid
"""
