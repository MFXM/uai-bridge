"""Use the bridge with the official OpenAI Python SDK (pip install openai)."""
import os

from openai import OpenAI

client = OpenAI(base_url=os.getenv("BRIDGE_URL", "http://127.0.0.1:8000") + "/v1",
                api_key=os.environ["BRIDGE_KEY"])

r = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "system", "content": "Answer in one sentence."},
              {"role": "user", "content": "What is the University of Vienna known for?"}],
    max_tokens=100,
)
print(r.choices[0].message.content)
print("tokens:", r.usage.total_tokens)
