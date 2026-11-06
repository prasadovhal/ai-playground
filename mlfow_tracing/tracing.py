
import mlflow
from openai import OpenAI

# Connect to MLflow
mlflow.set_tracking_uri("http://127.0.0.1:5000")
mlflow.set_experiment("ollama-tracing-demo")

# Automatically instrument OpenAI-compatible calls
mlflow.openai.autolog()

# Connect to locally running Ollama
client = OpenAI(
    base_url="http://localhost:11434/v1",
    api_key="ollama",
)

response = client.chat.completions.create(
    model="mistral",
    messages=[
        {
            "role": "user",
            "content": "Explain RAG in two sentences.",
        }
    ],
    temperature=0.1,
    max_tokens=150,
)

print(response.choices[0].message.content)

# Ensure queued traces are sent before the script exits
mlflow.flush_trace_async_logging()
