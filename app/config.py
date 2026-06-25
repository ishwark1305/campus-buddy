# config.py
import os

# Model configuration
# Using the available gemini-3.1-flash-lite model
MODEL_NAME = os.getenv("CAMPUS_BUDDY_MODEL", "gemini-3.1-flash-lite")

# Study companion thresholds (cutoff percentage for weak topics)
WEAK_TOPIC_THRESHOLD = float(os.getenv("CAMPUS_BUDDY_WEAK_THRESHOLD", "0.60"))
