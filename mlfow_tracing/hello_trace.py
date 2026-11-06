import mlflow

mlflow.set_tracking_uri("http://127.0.0.1:5000")
mlflow.set_experiment("01-mlflow-basics")


@mlflow.trace
def process_question(question: str) -> str:
    return f"Processed: {question}"


result = process_question("What is RAG?")
print(result)

mlflow.flush_trace_async_logging()
