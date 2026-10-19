#!/usr/bin/env python3
"""
Alias entrypoint for scraper.py
"""
import sys
import os

try:
    from Scrapper.scraper import main
except ImportError:
    from scraper import main

if __name__ == "__main__":
    main()
