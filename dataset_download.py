from datasets import load_dataset
import json
import os

# Load the exact dataset you specified
print("Loading dataset from Hugging Face...")
ds = load_dataset("community-datasets/disaster_response_messages", split="train")

# Inspect first example to see field names (uncomment if needed)
# print("Available fields:", ds[0].keys())
# print("First example:", ds[0])

# Ensure output directory exists
os.makedirs("config/seed", exist_ok=True)

# Save as JSONL (one JSON object per line)
output_path = "config/seed/disaster_response.jsonl"
count = 0

with open(output_path, "w", encoding="utf-8") as f:
    for example in ds:
        # Adjust field name based on what you see in ds[0].keys()
        # Common possibilities: 'message', 'text', 'content', 'input'
        text = example.get("message") or example.get("text") or example.get("content", "")
        if not text or not text.strip():
            continue
            
        record = {
            "id": f"dr_{count:06d}",
            "text": text.strip(),
            "source": "disaster_response_messages",
            "type": "text"
        }
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        count += 1

print(f"✅ Saved {count} disaster response messages to {output_path}")

# Quick verification
print("\nFirst 2 lines:")
with open(output_path, "r", encoding="utf-8") as f:
    for i in range(2):
        line = f.readline()
        if not line:
            break
        print(line.rstrip())