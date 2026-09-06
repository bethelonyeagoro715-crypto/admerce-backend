import ollama

print("Starting download of llama3.2:1b...")
for chunk in ollama.pull('llama3.2:1b', stream=True):
    if 'completed' in chunk and 'total' in chunk:
        print(f"Progress: {chunk['completed']}/{chunk['total']} bytes")
    elif 'status' in chunk:
        print(f"Status: {chunk['status']}")

print("Model downloaded and ready.")
